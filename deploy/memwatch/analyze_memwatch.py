#!/usr/bin/env python3
"""analyze_memwatch — read the memwatch CSVs and rank what actually grew.

The point of this script is to end the guessing. It answers three questions in
order:

  1. Did the host actually grow, or was it just page cache?
  2. Which containers / processes / units grew, ranked by how much?
  3. Does userland growth ADD UP to the host growth — and if not, how much is
     unaccounted for (i.e. kernel/slab)?

Question 3 is the one that matters most. A ranked list of processes is useless
if the leak is in kernel slab, and the only way to know is to check whether the
userland numbers sum to the host number.

Stdlib only — it runs on the box with no install step.

Usage:
    ./analyze_memwatch.py                  # last 24h
    ./analyze_memwatch.py --hours 72
    ./analyze_memwatch.py --dir /var/log/memwatch --hours 168 --top 15
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import io
import os
import sys
from datetime import datetime, timedelta, timezone

KB = 1024

# The report uses → and ⚠. On a console with a non-UTF-8 codepage (a Windows dev
# box reading a copied-down CSV, say) printing those would raise and kill the
# whole report. A diagnostic tool dying on its own output is unacceptable.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _open_any(path: str):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def load(base_dir: str, name: str) -> list[dict]:
    """Load name.csv plus any rotated siblings (name.csv.1, name.csv.2.gz, ...).

    logrotate is configured by size, not daily, but a long enough window will
    still cross a rotation — and silently analysing only the live file would
    quietly shorten the window and hide a slow climb.
    """
    rows: list[dict] = []
    for path in sorted(glob.glob(os.path.join(base_dir, name + ".csv*"))):
        try:
            with _open_any(path) as fh:
                for row in csv.DictReader(fh):
                    ts = (row.get("ts") or "").strip()
                    if not ts or ts == "ts":
                        continue  # a rotated file can carry a repeated header
                    try:
                        row["_t"] = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=timezone.utc
                        )
                    except ValueError:
                        continue
                    rows.append(row)
        except OSError as exc:
            print(f"  ! could not read {path}: {exc}", file=sys.stderr)
    rows.sort(key=lambda r: r["_t"])
    return rows


def num(row: dict, key: str):
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Series maths
# ---------------------------------------------------------------------------

class Series:
    """One named metric over time. Tracks first/last/delta and a fitted slope."""

    def __init__(self, key: str):
        self.key = key
        self.pts: list[tuple[float, float]] = []  # (epoch_seconds, value)

    def add(self, t: datetime, v: float) -> None:
        self.pts.append((t.timestamp(), v))

    @property
    def n(self) -> int:
        return len(self.pts)

    @property
    def first(self) -> float:
        return self.pts[0][1]

    @property
    def last(self) -> float:
        return self.pts[-1][1]

    @property
    def delta(self) -> float:
        return self.last - self.first

    @property
    def span_hours(self) -> float:
        if self.n < 2:
            return 0.0
        return (self.pts[-1][0] - self.pts[0][0]) / 3600.0

    @property
    def slope_per_hour(self) -> float:
        """Least-squares slope. More honest than first-vs-last, which a single
        spike at either end can completely fake."""
        if self.n < 3:
            return self.delta / self.span_hours if self.span_hours else 0.0
        t0 = self.pts[0][0]
        xs = [(t - t0) / 3600.0 for t, _ in self.pts]
        ys = [v for _, v in self.pts]
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        if denom == 0:
            return 0.0
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def collect(rows: list[dict], key_field: str | None, value_field: str,
            scale: float = 1.0) -> dict[str, Series]:
    """Build one Series per distinct key.

    key_field=None means the file has one row per timestamp (host, meminfo,
    orphans), so every numeric column becomes its own series.
    """
    out: dict[str, Series] = {}
    for row in rows:
        v = num(row, value_field)
        if v is None:
            continue
        k = (row.get(key_field) or "?") if key_field else value_field
        out.setdefault(k, Series(k)).add(row["_t"], v * scale)
    return out


def collect_wide(rows: list[dict], fields: list[str], scale: float = 1.0) -> dict[str, Series]:
    out: dict[str, Series] = {}
    for row in rows:
        for f in fields:
            v = num(row, f)
            if v is None:
                continue
            out.setdefault(f, Series(f)).add(row["_t"], v * scale)
    return out


def collect_summed(rows: list[dict], key_field: str, value_field: str,
                   scale: float = 1.0) -> dict[str, Series]:
    """Sum value_field across all rows sharing a timestamp AND key.

    Needed for processes: PIDs churn constantly, so keying on PID gives a mess of
    one-sample series. Keying on comm and summing per timestamp gives a stable
    'all node processes together' line, which is what you actually want to rank.
    """
    per_ts: dict[tuple[datetime, str], float] = {}
    for row in rows:
        v = num(row, value_field)
        if v is None:
            continue
        per_ts[(row["_t"], row.get(key_field) or "?")] = (
            per_ts.get((row["_t"], row.get(key_field) or "?"), 0.0) + v
        )
    out: dict[str, Series] = {}
    for (t, k), v in sorted(per_ts.items()):
        out.setdefault(k, Series(k)).add(t, v * scale)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def human(b: float) -> str:
    sign = "-" if b < 0 else ""
    b = abs(b)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if b < 1024 or unit == "TiB":
            return f"{sign}{b:,.1f} {unit}"
        b /= 1024
    return f"{sign}{b:.1f} TiB"


def plain(v: float) -> str:
    return f"{v:,.1f}"


def table(title: str, series: dict[str, Series], top: int, fmt=human,
          note: str = "") -> None:
    print(f"\n=== {title} ===")
    if note:
        print(f"    {note}")
    if not series:
        print("    (no data)")
        return
    ranked = sorted(series.values(), key=lambda s: s.delta, reverse=True)
    ranked = [s for s in ranked if s.n >= 2]
    if not ranked:
        print("    (need at least 2 samples — let it run longer)")
        return
    print(f"    {'metric':<34} {'first':>13} {'last':>13} {'delta':>13} {'per hr':>12}  {'n':>4}")
    print(f"    {'-' * 34} {'-' * 13} {'-' * 13} {'-' * 13} {'-' * 12}  {'-' * 4}")
    for s in ranked[:top]:
        print(f"    {s.key[:34]:<34} {fmt(s.first):>13} {fmt(s.last):>13} "
              f"{fmt(s.delta):>13} {fmt(s.slope_per_hour):>12}  {s.n:>4}")
    if len(ranked) > top:
        shrank = [s for s in ranked[top:] if s.delta < 0]
        print(f"    ... {len(ranked) - top} more (of which {len(shrank)} shrank)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="/var/log/memwatch")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print(f"No such directory: {args.dir}", file=sys.stderr)
        return 1

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    def win(rows: list[dict]) -> list[dict]:
        return [r for r in rows if r["_t"] >= cutoff]

    host = win(load(args.dir, "host"))
    meminfo = win(load(args.dir, "meminfo"))
    procs = win(load(args.dir, "procs"))
    conts = win(load(args.dir, "containers"))
    orph = win(load(args.dir, "orphans"))
    svcs = win(load(args.dir, "services"))

    if not host:
        print(f"No samples in the last {args.hours:g}h under {args.dir}.")
        print("Check:  systemctl list-timers memwatch")
        return 1

    span = (host[-1]["_t"] - host[0]["_t"]).total_seconds() / 3600.0
    print("=" * 78)
    print(f"memwatch analysis — {args.dir}")
    print(f"window: {host[0]['_t']:%Y-%m-%d %H:%M} → {host[-1]['_t']:%Y-%m-%d %H:%M} UTC "
          f"({span:.1f}h, {len(host)} samples)")
    print("=" * 78)

    # --- 1. host ----------------------------------------------------------
    host_s = collect_wide(
        host, ["used_kb", "available_kb", "free_kb", "cached_kb", "buffers_kb",
               "shmem_kb", "swap_used_kb"], scale=KB)
    table("HOST", host_s, top=10,
          note="If used climbs while available stays flat, it's page cache — not a leak.")

    used = host_s.get("used_kb")
    avail = host_s.get("available_kb")

    # --- 2. kernel --------------------------------------------------------
    kern_s = collect_wide(
        meminfo, ["slab_kb", "sreclaimable_kb", "sunreclaim_kb", "kernelstack_kb",
                  "pagetables_kb", "anonpages_kb", "mapped_kb", "vmallocused_kb",
                  "percpu_kb"], scale=KB)
    table("KERNEL / SLAB", kern_s, top=10,
          note="sunreclaim + kernelstack + pagetables growing = a kernel-side leak.")

    # --- 3. containers ----------------------------------------------------
    cont_s = collect(conts, "name", "mem_bytes")
    table("CONTAINERS", cont_s, top=args.top)

    # --- 4. processes -----------------------------------------------------
    proc_s = collect_summed(procs, "comm", "rss_kb", scale=KB)
    table("PROCESSES (RSS summed per command name)", proc_s, top=args.top,
          note="Only the top-25-by-RSS are sampled, so small-but-growing procs "
               "appear mid-window.")

    # --- 5. orphans -------------------------------------------------------
    orph_s = collect_wide(orph, ["claude_total", "claude_stale", "node_ppid1_count",
                                 "zombies", "proc_total"])
    table("ORPHAN / PROCESS COUNTS", orph_s, top=8, fmt=plain,
          note="claude_stale > 0 proves the cleo SIGKILL grandchild leak. Flat = ruled out.")
    orph_rss = collect_wide(orph, ["claude_rss_kb", "node_ppid1_rss_kb"], scale=KB)
    table("ORPHAN MEMORY", orph_rss, top=6)

    # --- 6. services ------------------------------------------------------
    svc_s = collect(svcs, "unit", "memory_current_bytes")
    table("SYSTEMD UNITS", svc_s, top=args.top)

    # --- 7. the verdict ---------------------------------------------------
    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)

    if not used or used.n < 3:
        print("  Not enough samples yet. Let it run at least a few hours.")
        return 0

    host_delta = used.delta
    print(f"  host 'used'      {human(used.first)} → {human(used.last)}  "
          f"({human(host_delta)}, {human(used.slope_per_hour)}/hr)")
    if avail and avail.n >= 2:
        print(f"  host 'available' {human(avail.first)} → {human(avail.last)}  "
              f"({human(avail.delta)})")
        if host_delta > 0 and avail.delta >= 0:
            print("  → 'used' rose but 'available' did NOT fall. That is reclaimable "
                  "cache, not a leak.")

    if host_delta <= 0:
        print("\n  No net growth in this window. Either the leak is slower than "
              f"{args.hours:g}h, or it needs a specific workload to trigger.")
        print("  Re-run with a longer --hours once more history has accumulated.")
        return 0

    cont_growth = sum(s.delta for s in cont_s.values() if s.delta > 0)
    slab = kern_s.get("sunreclaim_kb")
    slab_growth = slab.delta if slab and slab.n >= 2 else 0.0
    pt = kern_s.get("pagetables_kb")
    pt_growth = pt.delta if pt and pt.n >= 2 else 0.0
    kernel_growth = max(0.0, slab_growth) + max(0.0, pt_growth)

    print(f"\n  attributed to containers : {human(cont_growth)}")
    print(f"  attributed to kernel      : {human(kernel_growth)}  "
          f"(unreclaimable slab + page tables)")
    unexplained = host_delta - cont_growth - kernel_growth
    print(f"  unexplained               : {human(unexplained)}")
    print("  (rough split, not an accounting identity: a containerised process is "
          "counted\n   in BOTH the container and process tables, and shared pages "
          "are counted twice.)")

    print()
    # Rank the candidates so the next step is obvious rather than a judgement call.
    ranked = []
    if cont_s:
        top_c = max(cont_s.values(), key=lambda s: s.delta)
        if top_c.delta > 0:
            ranked.append((top_c.delta, f"container '{top_c.key}' grew {human(top_c.delta)}"))
    if proc_s:
        top_p = max(proc_s.values(), key=lambda s: s.delta)
        if top_p.delta > 0:
            ranked.append((top_p.delta, f"process group '{top_p.key}' grew {human(top_p.delta)}"))
    if kernel_growth > 0:
        ranked.append((kernel_growth,
                       f"kernel slab/pagetables grew {human(kernel_growth)} — "
                       "no userland fix will touch this"))
    for _, line in sorted(ranked, reverse=True):
        print(f"  → {line}")

    if unexplained > host_delta * 0.5:
        print("\n  ⚠ Over half the growth is unattributed. Likely causes: a process "
              "that never entered the top-25 sample, many small containers each "
              "growing a little, or kernel memory this script does not break out. "
              "Raise MEMWATCH_TOP_N and re-check.")

    stale = orph_s.get("claude_stale")
    if stale and stale.n >= 2:
        if stale.last > 0 or stale.delta > 0:
            print(f"\n  ⚠ claude_stale is {stale.last:.0f} (delta {stale.delta:+.0f}) — "
                  "orphaned claude processes ARE accumulating. The cleo SIGKILL "
                  "grandchild leak is real and confirmed.")
        else:
            print("\n  ✓ claude_stale flat at 0 — no orphaned claude processes. "
                  "The cleo grandchild theory is ruled out for this window.")

    print(f"\n  Projected over 7 days at the current rate: "
          f"{human(used.slope_per_hour * 24 * 7)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
