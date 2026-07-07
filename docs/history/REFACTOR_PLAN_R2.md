# Consistency & Security Refactor — Round 2

## Context

The original refactor ([`REFACTOR_PLAN.md`](REFACTOR_PLAN.md), PR #27) set the project's
conventions: a shared `web/static/utils.js`, `web/_logging.py`, `tradingagents/constants.py`,
`web/secret_box.py` (secrets at rest), `hmac.compare_digest` for token comparisons,
allow-list DB updates, and the `ARCHITECTURE.md` / `SECURITY_AUDIT.md` docs.

Since then ~4,400 lines landed **without** that pass — the Agent Bus (`web/bus.py`,
`web/bus_mirror.py`, the `/api/bus` WebSocket, `web/static/bus.js`), the brokerage
abstraction (`web/brokerages.py`), option handling, the portfolio-card redesign, and the
scan progress bar. Round 2 brings that new code up to the established standard.

Three parallel audits found the new **Python** is largely clean (logging, `SIGNALS`,
`X | None`, allow-lists, docstrings all already followed) — with **one real security bug**,
a handful of frontend dedup/escape items, a **permanently-red CI gate**, and **stale docs**.

Same process as round 1: a direct branch, per-workstream commits, behavior-preserving,
consolidate-don't-restructure.

## Guiding principles

- **Behavior-preserving.** Extracted helpers are byte-for-byte equivalent to the copies
  they replace; the security fix only changes behavior for a malicious timing attacker.
- **Reuse what exists.** `progressBar` joins the `utils.js` shared layer; the legacy Schwab
  parser is pointed at the canonical `brokerages.fetch_all_accounts()`.
- **Consolidate, don't restructure.** No new modules of substance, no rewrites.
- **Verify continuously.** `pytest` green (368 passed), `ruff check .` green, JS syntax-checked.

---

## Workstream 1 — Frontend JS consolidation

- **`web/static/utils.js`**: new shared `progressBar(count, total)` — the track+fill bar
  markup was copied **4×** across `portfolio.js` and `spy.js`.
- **`web/static/portfolio.js`**: the `[ Progress ]` panel was built **verbatim twice**
  (initial render + the 5s poll re-render). Extracted `portfolioProgressHtml(scan)`, used by
  both, so they can't drift. Output identical.
- **`web/static/spy.js`**: the quick + deep bars now call `progressBar()`; dropped the
  redundant `qpct`/`dpct` locals.
- **`web/static/portfolio.js`**: escape `pos.strike` / `pos.put_call` in the option-card
  subtext (the rest of the card already escapes — escape-at-render discipline).
- **`web/static/bus.js`**: dropped the local `$` redefinition; the IIFE closes over the
  `utils.js` global instead.
- **`web/static/styles.css`**: removed dead `.pcard-meta` (0 references after the card redesign).

## Workstream 2 — Python consolidation (thin — the new Python was already clean)

- **`web/bus_mirror.py`**: `RunMirror.__init__(publisher: Any)` → `publisher: BusPublisher`
  (imported from `.bus`; dropped the now-unused `Any` import).
- **`web/portfolio_main.py`**: deprecation note on the legacy Schwab-only
  `_parse_schwab_account` pointing at `brokerages.fetch_all_accounts()`. Actual migration of
  `/api/spy-account` deferred (behavior risk not worth this pass).

## Workstream 3 — CI scoping + lint cleanup

The CI gate ran `ruff check .` repo-wide and was **permanently red** on ~213 pre-existing
`cli/` + `tradingagents/` errors, so nobody read it — and the new code had itself landed
unlinted (65 errors across `web/` + `tests/`).

- **`pyproject.toml`**: `extend-exclude = ["cli", "tradingagents"]` so the gate means
  "**`web/` + `tests/` are clean**"; `per-file-ignores` `tests/** = ["B017"]` for the WS /
  error-path tests that intentionally assert a broad raise. The upstream backlog is
  explicitly deferred (run `ruff check tradingagents` to see it).
- Brought `web/` + `tests/` + root scripts to **zero** ruff errors: import sorting (I001),
  unused imports (F401), `Optional[X]` → `X | None` (UP045), quoted-annotation removal
  (UP037), module-level imports to resolve forward-ref F821 in the bus tests, and removal of
  dead F841 locals. `ruff check .` now passes.

## Workstream 4 — Security

- **`web/main.py`**: the `/api/bus` WebSocket auth gate used `internal == expected` while the
  identical `/api/analyze` gate uses `hmac.compare_digest` — a **regression of round-1
  finding #9** (timing side-channel on `INTERNAL_API_TOKEN`). Fixed to `hmac.compare_digest`;
  added `tests/test_bus_bridge.py::test_wrong_internal_token_closes_with_4401`.
- **`SECURITY_AUDIT.md`**: added a Round-2 section — the regression (fixed), plus reviewed
  (bus content via `textContent`, bus client URL, brokerage data path) and newly deferred
  (bus-token lifecycle, scan-id enumeration) items.

## Workstream 5 — Documentation

- **`ARCHITECTURE.md`**: added the `switchboard` container to the runtime table; documented
  the **nginx route priority** (`/api/spy`,`/api/accounts`,`/api/portfolio` before the
  generic `/api/`); new **Agent Bus** and **Brokerage data** sections; long-scan progress
  polling in the frontend section; refreshed the ruff/tooling note.
- **`.env.example`**: added `SWITCHBOARD_URL` and `SCHWAB_MCP_URL` (with the
  Tailscale-unreachable-from-Docker caveat).
- **`README.md`**: features list now lists 5 tabs (adds Credentials), the per-account
  tabs + brokerage abstraction, option handling, and the scan progress bars.
- This file.

---

## Deferred (carried forward / new)

From round 1, still open: login rate-limiting (High), admin/RBAC (Medium),
`SCHWAB_MCP_URL`/callback SSRF validation (Medium). New in round 2: `SWITCHBOARD_MCP_TOKEN`
rotation (Low), scan-id ownership when multi-user lands (Low), migrating `/api/spy-account`
off the legacy parser, and expanding ruff coverage into `cli/` + `tradingagents/`.

## Verification

1. `uv run python -m pytest -q` — 368 passed, 1 skipped (incl. the new bus-auth test).
2. `uvx ruff check .` — clean (was red repo-wide).
3. `node --check` on each modified JS file; `progressBar` wired at all call sites; no
   orphaned locals.
4. Import smoke: `web.main, web.portfolio_main, web.bus, web.bus_mirror, web.brokerages`.

## Status: shipped to `tradingagents-switchboard` (+ mirrored to the original repo), deployed to Web Server stack 67.
