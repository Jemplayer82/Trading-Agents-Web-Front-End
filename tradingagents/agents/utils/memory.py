"""Append-only markdown decision log for TradingAgents."""

import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from tradingagents.agents.utils.rating import parse_rating

# Serializes all writers WITHIN one process. Deep dives call store_decision
# from a ThreadPoolExecutor (web/spy_scanner.py), and the read-check-append /
# read-mutate-replace bodies below are not atomic — without this, concurrent
# threads can interleave multi-KB appends or drop each other's entries.
# Module-level (not per-instance) because every orchestrator constructs its
# own TradingMemoryLog over the same file.
_WRITE_LOCK = threading.Lock()


class TradingMemoryLog:
    """Append-only markdown log of trading decisions and reflections.

    Concurrency model: all writes are serialized per-process via _WRITE_LOCK,
    and rewrites go through a pid-unique temp file + os.replace. CROSS-process
    races are NOT locked: the 22:00 nightly portfolio scan (api/portfolio
    container) can still be storing decisions when the scheduler's 23:30 sweep
    rewrites the file, so an append landing inside the sweep's read->replace
    window (sub-second) can be lost. Accepted: the worst case is ONE dropped
    pending entry, which the next scan of that ticker re-stores; never
    corruption (the replace is atomic). Readers retry once on OSError
    (Windows os.replace can momentarily deny reads); write paths treat an
    unreadable-but-existing log as a hard error rather than guessing.
    """

    # HTML comment: cannot appear in LLM prose output, safe as a hard delimiter
    _SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"
    # Precompiled patterns — avoids re-compilation on every load_entries() call
    _DECISION_RE = re.compile(r"DECISION:\n(.*?)(?=\nREFLECTION:|\Z)", re.DOTALL)
    _REFLECTION_RE = re.compile(r"REFLECTION:\n(.*?)$", re.DOTALL)

    def __init__(self, config: dict = None):
        cfg = config or {}
        self._config = cfg
        self._log_path = None
        path = cfg.get("memory_log_path")
        if path:
            self._log_path = Path(path).expanduser()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # Optional cap on resolved entries. None disables rotation.
        self._max_entries = cfg.get("memory_log_max_entries")

    def _read_log_text(self) -> str | None:
        """Read the log, retrying once on OSError.

        On Windows, os.replace during a concurrent rewrite (the nightly sweep)
        can momentarily deny reads with PermissionError; a missed read is
        benign, an unhandled exception inside a deep-dive worker thread is not.
        Returns None when the file is absent or unreadable after the retry.
        """
        if not self._log_path or not self._log_path.exists():
            return None
        for attempt in (0, 1):
            try:
                return self._log_path.read_text(encoding="utf-8")
            except OSError:
                if attempt:
                    return None
                time.sleep(0.05)
        return None

    # --- Write path (Phase A) ---

    def store_decision(
        self,
        ticker: str,
        trade_date: str,
        final_trade_decision: str,
    ) -> None:
        """Append pending entry at end of propagate(). No LLM call."""
        if not self._log_path:
            return
        with _WRITE_LOCK:
            # Idempotency guard: fast raw-text scan instead of full parse.
            # Matches pending AND resolved entries — re-running a (ticker, date)
            # whose entry already resolved must not append a duplicate (it would
            # double-count in calibration stats).
            raw = self._read_log_text()
            if raw is None and self._log_path.exists():
                # Log exists but is unreadable even after the retry: appending
                # blind would bypass the dedup guard and could double-count a
                # decision in calibration. No write beats a wrong write.
                raise OSError(f"memory log unreadable, store aborted: {self._log_path}")
            if raw:
                for line in raw.splitlines():
                    if line.startswith(f"[{trade_date} | {ticker} |"):
                        return
            # "Unrated" (not "Hold") on parse failure: a failed parse dumped into
            # the Hold bucket would silently pollute Hold calibration stats.
            rating = parse_rating(final_trade_decision, default="Unrated")
            tag = f"[{trade_date} | {ticker} | {rating} | pending]"
            entry = f"{tag}\n\nDECISION:\n{final_trade_decision}{self._SEPARATOR}"
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(entry)

    # --- Read path (Phase A) ---

    def load_entries(self) -> list[dict]:
        """Parse all entries from log. Returns list of dicts."""
        text = self._read_log_text()
        if text is None:
            return []
        raw_entries = [e.strip() for e in text.split(self._SEPARATOR) if e.strip()]
        entries = []
        for raw in raw_entries:
            parsed = self._parse_entry(raw)
            if parsed:
                entries.append(parsed)
        return entries

    def get_pending_entries(self) -> list[dict]:
        """Return entries with outcome:pending (for Phase B)."""
        return [e for e in self.load_entries() if e.get("pending")]

    def get_past_context(self, ticker: str, n_same: int = 5, n_cross: int = 3) -> str:
        """Return formatted past context string for agent prompt injection.

        INVARIANT: this context is injected into the Portfolio Manager only.
        Do not feed it to analysts/researchers — a wrong lesson biasing every
        agent at once is how a memory contamination spiral starts; one
        injection point keeps the blast radius bounded.

        Layout: aggregate calibration stats first (luck cancels out across the
        full log — that's the trustworthy signal), then recent anecdotal
        lessons, which are explicitly n=1 stories.
        """
        all_entries = self.load_entries()
        resolved = [e for e in all_entries if not e.get("pending")]
        if not resolved:
            return ""

        # Calibration runs over the FULL resolved log; the recency cutoff below
        # only trims which anecdotes get retold.
        from tradingagents.agents.utils.calibration import (
            compute_calibration,
            format_calibration,
        )

        calibration_block = format_calibration(
            compute_calibration(all_entries, self._config)
        )

        max_age = self._config.get("memory_context_max_age_days")
        if max_age:
            cutoff = (datetime.now() - timedelta(days=max_age)).strftime("%Y-%m-%d")
            resolved = [e for e in resolved if e["date"] >= cutoff]

        same, cross = [], []
        cross_tickers = set()
        for e in reversed(resolved):
            if len(same) >= n_same and len(cross) >= n_cross:
                break
            if e["ticker"] == ticker and len(same) < n_same:
                same.append(e)
            elif (
                e["ticker"] != ticker
                and len(cross) < n_cross
                and e["ticker"] not in cross_tickers
            ):
                # One lesson per ticker: a portfolio sweep resolves whole
                # batches at once, and without this a single name (or one
                # day's correlated batch) monopolises every cross slot.
                cross.append(e)
                cross_tickers.add(e["ticker"])

        parts = []
        if calibration_block:
            parts.append(calibration_block)
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(self._format_full(e) for e in same)
        if cross:
            cross_formatted = [
                f for f in (self._format_reflection_only(e) for e in cross) if f
            ]
            if cross_formatted:
                parts.append(
                    "Recent cross-ticker lessons (each is a single outcome — "
                    "weigh the calibration stats above them):"
                )
                parts.extend(cross_formatted)
        if not parts:
            return ""
        return "\n\n".join(parts)

    # --- Update path (Phase B) ---

    def update_with_outcome(
        self,
        ticker: str,
        trade_date: str,
        raw_return: float,
        alpha_return: float,
        holding_days: int,
        reflection: str,
    ) -> None:
        """Replace pending tag and append REFLECTION section using atomic write.

        Finds the first pending entry matching (trade_date, ticker), updates
        its tag with return figures, and appends a REFLECTION section.  Uses
        a temp-file + os.replace() so a crash mid-write never corrupts the log.
        """
        with _WRITE_LOCK:
            self._update_with_outcome_locked(
                ticker, trade_date, raw_return, alpha_return, holding_days, reflection
            )

    def _update_with_outcome_locked(
        self,
        ticker: str,
        trade_date: str,
        raw_return: float,
        alpha_return: float,
        holding_days: int,
        reflection: str,
    ) -> None:
        text = self._read_log_text()
        if text is None:
            if self._log_path and self._log_path.exists():
                # Unreadable-but-existing log: silently dropping the outcome
                # would leave the entry pending while the sweep reports success
                # (and re-bills its LLM reflection nightly). Fail loud instead —
                # the sweep's exception handler logs it.
                raise OSError(f"memory log unreadable, outcome write aborted: {self._log_path}")
            return
        blocks = text.split(self._SEPARATOR)

        pending_prefix = f"[{trade_date} | {ticker} |"
        raw_pct = f"{raw_return:+.1%}"
        alpha_pct = f"{alpha_return:+.1%}"

        updated = False
        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            if (
                not updated
                and tag_line.startswith(pending_prefix)
                and tag_line.endswith("| pending]")
            ):
                # Parse rating from the existing pending tag
                fields = [f.strip() for f in tag_line[1:-1].split("|")]
                rating = fields[2]
                new_tag = (
                    f"[{trade_date} | {ticker} | {rating}"
                    f" | {raw_pct} | {alpha_pct} | {holding_days}d]"
                )
                rest = "\n".join(lines[1:])
                new_blocks.append(
                    f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{reflection}"
                )
                updated = True
            else:
                new_blocks.append(block)

        if not updated:
            return

        new_blocks = self._apply_rotation(new_blocks)
        self._atomic_rewrite(self._SEPARATOR.join(new_blocks))

    def batch_update_with_outcomes(self, updates: list[dict]) -> None:
        """Apply multiple outcome updates in a single read + atomic write.

        Each element of updates must have keys: ticker, trade_date,
        raw_return, alpha_return, holding_days, reflection.
        """
        if not updates:
            return
        with _WRITE_LOCK:
            self._batch_update_locked(updates)

    def _batch_update_locked(self, updates: list[dict]) -> None:
        text = self._read_log_text()
        if text is None:
            if self._log_path and self._log_path.exists():
                # Same rationale as _update_with_outcome_locked: a silent drop
                # here loses a whole sweep's outcome batch invisibly.
                raise OSError(f"memory log unreadable, batch write aborted: {self._log_path}")
            return
        blocks = text.split(self._SEPARATOR)

        # Build lookup keyed by (trade_date, ticker) for O(1) dispatch
        update_map = {(u["trade_date"], u["ticker"]): u for u in updates}

        new_blocks = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                new_blocks.append(block)
                continue

            lines = stripped.splitlines()
            tag_line = lines[0].strip()

            matched = False
            for (trade_date, ticker), upd in list(update_map.items()):
                pending_prefix = f"[{trade_date} | {ticker} |"
                if tag_line.startswith(pending_prefix) and tag_line.endswith("| pending]"):
                    fields = [f.strip() for f in tag_line[1:-1].split("|")]
                    rating = fields[2]
                    raw_pct = f"{upd['raw_return']:+.1%}"
                    alpha_pct = f"{upd['alpha_return']:+.1%}"
                    new_tag = (
                        f"[{trade_date} | {ticker} | {rating}"
                        f" | {raw_pct} | {alpha_pct} | {upd['holding_days']}d]"
                    )
                    rest = "\n".join(lines[1:])
                    new_blocks.append(
                        f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{upd['reflection']}"
                    )
                    del update_map[(trade_date, ticker)]
                    matched = True
                    break

            if not matched:
                new_blocks.append(block)

        new_blocks = self._apply_rotation(new_blocks)
        self._atomic_rewrite(self._SEPARATOR.join(new_blocks))

    # --- Helpers ---

    def _atomic_rewrite(self, new_text: str) -> None:
        """Write via a pid-unique temp file + os.replace.

        The temp name embeds the pid so two containers rewriting the shared
        volume concurrently can't interleave on one fixed '.tmp' file (the old
        with_suffix('.tmp') name was a cross-process collision).
        """
        tmp_path = self._log_path.with_suffix(f".tmp{os.getpid()}")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._log_path)

    def _apply_rotation(self, blocks: list[str]) -> list[str]:
        """Drop oldest resolved blocks when their count exceeds max_entries.

        Pending blocks are always kept (they represent unprocessed work).
        Returns ``blocks`` unchanged when rotation is disabled or under cap.
        """
        if not self._max_entries or self._max_entries <= 0:
            return blocks

        # Tag each block with (kept, is_resolved) by parsing tag-line markers.
        decisions = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                decisions.append((block, False))
                continue
            tag_line = stripped.splitlines()[0].strip()
            is_resolved = (
                tag_line.startswith("[")
                and tag_line.endswith("]")
                and not tag_line.endswith("| pending]")
            )
            decisions.append((block, is_resolved))

        resolved_count = sum(1 for _, r in decisions if r)
        if resolved_count <= self._max_entries:
            return blocks

        to_drop = resolved_count - self._max_entries
        kept: list[str] = []
        for block, is_resolved in decisions:
            if is_resolved and to_drop > 0:
                to_drop -= 1
                continue
            kept.append(block)
        return kept

    def _parse_entry(self, raw: str) -> dict | None:
        lines = raw.strip().splitlines()
        if not lines:
            return None
        tag_line = lines[0].strip()
        if not (tag_line.startswith("[") and tag_line.endswith("]")):
            return None
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        if len(fields) < 4:
            return None
        entry = {
            "date": fields[0],
            "ticker": fields[1],
            "rating": fields[2],
            "pending": fields[3] == "pending",
            "raw": fields[3] if fields[3] != "pending" else None,
            "alpha": fields[4] if len(fields) > 4 else None,
            "holding": fields[5] if len(fields) > 5 else None,
        }
        body = "\n".join(lines[1:]).strip()
        decision_match = self._DECISION_RE.search(body)
        reflection_match = self._REFLECTION_RE.search(body)
        entry["decision"] = decision_match.group(1).strip() if decision_match else ""
        entry["reflection"] = reflection_match.group(1).strip() if reflection_match else ""
        return entry

    def _format_full(self, e: dict) -> str:
        raw = e["raw"] or "n/a"
        alpha = e["alpha"] or "n/a"
        holding = e["holding"] or "n/a"
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {raw} | {alpha} | {holding}]"
        parts = [tag, f"DECISION:\n{e['decision']}"]
        if e["reflection"]:
            parts.append(f"REFLECTION:\n{e['reflection']}")
        return "\n\n".join(parts)

    def _format_reflection_only(self, e: dict) -> str | None:
        """Short cross-ticker lesson format; None when there is no reflection.

        Alpha is included alongside raw — a +4% raw in a +5% benchmark week is
        a loss, and showing raw alone taught exactly the wrong lesson. Entries
        without a reflection are skipped entirely rather than leaking raw
        DECISION prose into prompts as if it were a graded lesson.
        """
        if not e["reflection"]:
            return None
        tag = (
            f"[{e['date']} | {e['ticker']} | {e['rating']}"
            f" | raw {e['raw'] or 'n/a'} | alpha {e['alpha'] or 'n/a'}]"
        )
        return f"{tag}\n{e['reflection']}"
