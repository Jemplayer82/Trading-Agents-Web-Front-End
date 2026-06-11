# Project Consistency & Conciseness Refactor

## Context

The user asked to "refactor the whole project — make things consistent and concise."
Scope confirmed: **whole project (JS + all Python)**, depth: **consolidate, don't
restructure** (keep the existing file/folder layout; unify patterns and remove
duplication, no bundler/rewrite/TypeScript).

Despite the repo name, this is a large **Python multi-agent trading framework**
(`tradingagents/`, `web/`, `cli/`, ~91 source `.py` files, ~18k LOC) with a small
**vanilla-JS frontend** (`web/static/`, 5 JS files, ~2.3k LOC). The two halves have
very different states:

- **Frontend JS** is where the clearest inconsistency and duplication live and where
  the payoff is highest and risk lowest. Three different DOM-query aliases, 3–4 copies
  each of timestamp/HTML-escape/number-pad helpers, ad-hoc `fetch` error handling, and
  mixed quote styles. The files are loaded as **classic `<script defer>` tags (not ES
  modules)** — see `web/static/index.html:8-14` — so all top-level `const`s share one
  global lexical scope. That shared scope is *why* the aliases differ (`$`, `$$p`,
  `$$spy`): a second `const $` would throw "already declared." The fix is a single
  shared `utils.js` loaded first, with duplicates removed from each file.
- **Python** is already well-maintained: 0 bare `except`, 0 TODO/FIXME, no `%`-string
  formatting, no debug `print` spam, consistent `logging.getLogger(__name__)`. Its
  inconsistencies are narrow and mechanical: mixed `Optional[X]` (37) vs `X | None`,
  partial `from __future__ import annotations` (33/91 files), 13 `.format()` calls vs
  f-strings, and several `web/` modules that build LLM clients ad hoc instead of going
  through the existing `web/llm_helpers.py:llm_for()` / `create_llm_client()` factory.

Goal: one consistent style and a small shared-utility layer, achieved with surgical,
reviewable diffs that don't change runtime behavior.

## Guiding principles

- **Behavior-preserving.** No functional/UI changes. Each helper extracted must be
  byte-for-byte equivalent to the copies it replaces.
- **Reuse what exists.** Don't invent new abstractions where canonical ones already
  exist (the LLM factory, `safe_ticker_component`, `dataflows/utils.py` date helpers,
  `dataflows/config.py` singleton). Consolidate toward them.
- **Consolidate, don't restructure.** Keep filenames and module boundaries. No new
  folders, no bundler, no TypeScript.
- **Verify continuously.** Run the existing pytest suite after Python changes; load the
  web UI after JS changes.

---

## Workstream 1 — Frontend JS consolidation (highest value, lowest risk)

**New file: `web/static/utils.js`** (loaded first, before `auth.js`, via a new
`<script src="/static/utils.js" defer></script>` at `web/static/index.html:8`).

Define the shared helpers **once** here as top-level globals (classic-script globals,
matching the existing pattern — no `export`, no `window.TA` namespace needed since the
files already share global scope):

- `$ = (id) => document.getElementById(id)` — the single canonical DOM-by-id helper.
- `escapeHtml(s)` — the exact implementation currently duplicated in `app.js`,
  `portfolio.js`, and `spy.js` (as `escHtml`).
- `fmtTs(iso)` — the timestamp formatter currently duplicated as `formatTimestamp`
  (`app.js`) and `fmtTs` (`portfolio.js`, `spy.js`).
- Number formatters currently in `portfolio.js` (`fmt$`, `fmtAbs$`, `fmtShares`,
  `fmtPct`) plus `spy.js`'s `fmtReturn` — move the ones used in more than one file.
- `apiFetch(url, options)` — a thin wrapper standardizing the repeated
  `fetch → !resp.ok → resp.json()` dance found 15+ times. Returns parsed JSON, throws
  on non-OK. Keep it minimal so call sites that need custom handling can still use raw
  `fetch`.

**Then in each of `app.js`, `portfolio.js`, `spy.js`, `credentials.js`, `auth.js`:**

- Delete the now-duplicated local definitions (`formatTimestamp`/`fmtTs`/`fmtReturn`,
  `escapeHtml`/`escHtml`, `pad`, the per-file `$`/`$$p`/`$$spy` aliases).
- Replace `$$p(...)`/`$$spy(...)` call sites with `$(...)`.
- Replace `escHtml(...)` / `formatTimestamp(...)` call sites with `escapeHtml(...)` /
  `fmtTs(...)`.
- Migrate obvious `fetch` blocks to `apiFetch` where the error handling is the standard
  pattern; leave bespoke ones alone.
- Normalize string quotes to **double quotes** (the dominant style) within touched
  lines; avoid reformatting untouched lines to keep the diff legible.

**Representative files:** `web/static/utils.js` (new), `web/static/index.html`,
`web/static/app.js`, `web/static/portfolio.js`, `web/static/spy.js`,
`web/static/credentials.js`, `web/static/auth.js`.

**Note on script order:** because these are classic scripts in global scope, `utils.js`
must load first. `defer` scripts execute in document order, so placing it as the first
`/static/*.js` tag guarantees its globals exist before the others run.

---

## Workstream 2 — Python consolidation (surgical, mechanical)

Keep this conservative; the Python is already clean. Limit to changes that reduce real
duplication or unify a clearly-mixed pattern:

1. **Unify LLM-client creation in `web/`.** Audit the files that instantiate clients
   directly — `web/main.py`, `web/spy_scanner.py`, `web/portfolio/aggregator.py`,
   `web/spy_allocator.py` — and route them through the existing
   `web/llm_helpers.py:llm_for()` (which already resolves key/base-url from config → DB
   → env → provider default) or `tradingagents.llm_clients.create_llm_client`, instead
   of repeating credential/base-url resolution.

2. **Centralize logging setup.** Add one small helper (`web/_logging.py:configure_logging()`)
   using the format already in `web/scheduler.py:24-28`, and call it from the entry points
   (`web/main.py`, `web/portfolio_main.py`, `web/scheduler.py`) instead of repeating
   `logging.basicConfig(...)`. Module loggers stay `logging.getLogger(__name__)`.

3. **Type-hint consistency.** Standardize on `X | None` over `Optional[X]` and add
   `from __future__ import annotations` to the files that lack it, but **only in files
   already being touched** for reasons 1–2.

4. **f-strings over `.format()`** in the 13 spots that mix them, again preferring files
   already touched.

5. **Date/env helpers.** Where modules re-roll `datetime.now().strftime("%Y-%m-%d")`,
   prefer the existing `tradingagents/dataflows/utils.py:get_current_date()`; where env
   access mixes `os.getenv`/`os.environ.get`, settle on `os.getenv`.

6. **De-duplicate magic constants:**
   - The Ollama base URL `"http://localhost:11434/v1"` was hardcoded 6×. Centralized to
     `tradingagents/llm_clients/defaults.py:DEFAULT_OLLAMA_BASE_URL`.
   - The signal strings `"BUY"/"HOLD"/"SELL"` were re-declared across 5 files. Centralized
     to `tradingagents/constants.py:SIGNALS`.

7. **`print()` → logging in library code.** Converted `print(f"Error …")` calls in
   `tradingagents/dataflows/` to `logging.getLogger(__name__)`.

8. **Remove unused imports** flagged in `tradingagents/agents/utils/agent_utils.py` —
   added `__all__` to document intentional re-exports (prevents ruff F401).

---

## Workstream 3 — Repo hygiene to lock in consistency

Added **`ruff`** config block to `pyproject.toml` (`[tool.ruff]`) with rules:
- F = pyflakes (unused imports/vars, undefined names)
- I = isort (import ordering)
- UP = pyupgrade (modern syntax)
- B = flake8-bugbear

Also added `.github/workflows/ci.yml` running `ruff check` + `pytest -q` on every pull
request, turning the manual verification into an automated merge gate.

---

## Workstream 4 — Security audit & hardening

Full findings documented in `SECURITY_AUDIT.md`. Summary of fixes applied:

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | Critical | Provider API keys stored in plaintext SQLite | Fixed — Fernet encrypt-on-write/decrypt-on-read via `web/secret_box.py` |
| 2 | High | Schwab OAuth had no `state` CSRF protection | Fixed — nonce minted, stored in cookie, verified on callback |
| 3 | Medium | DB file created with world-readable perms | Fixed — `chmod 0o600` in `init_db()` |
| 4 | Medium | `TOKEN_ENCRYPTION_KEY` accepted unvalidated | Fixed — `validate_key()` at startup |
| 5 | Medium | Ticker not validated before reaching yfinance | Fixed — `safe_ticker_component()` at web boundary |
| 6 | Medium | LLM markdown rendered into innerHTML unsanitized | Fixed — DOMPurify via `renderMarkdown()` in utils.js |
| 7 | Low/Med | Schwab callback echoed raw error text to browser | Fixed — log server-side, return generic message |
| 8 | Low | `update_spy_scan()` interpolated kwargs into SQL | Fixed — explicit column allow-list |
| 9 | Low | WebSocket token compared with `==` (timing leak) | Fixed — `hmac.compare_digest` |
| 10 | Low | Session cookie `SameSite=lax` | Fixed — `SameSite=strict` |

**Deferred (documented in SECURITY_AUDIT.md):**
- Login rate-limiting / lockout (High) — needs `slowapi` or DB-backed attempt counter
- Admin / RBAC enforcement (Medium) — needs `is_admin` column + DB migration

---

## Workstream 5 — Documentation

- `ARCHITECTURE.md` — big-picture diagram, runtime process table, agent pipeline,
  LLM factory extension guide, persistence/secrets, frontend load-order rule
- `SECURITY_AUDIT.md` — full written audit with severity table and fix details
- Module docstrings added to all key files explaining responsibility and system fit
- Inline comments for non-obvious decisions (load order, credential resolution order,
  encryption migration path, debate round routing)

---

## Verification (completed)

1. `python -m pytest -q` — all 231 tests pass
2. `ruff check .` — clean on all changed files
3. Entry points import cleanly: `python -c "import web.main, web.portfolio_main, web.scheduler"`
4. JS diff is net-negative LOC (duplicates removed, shared versions confirmed identical)
5. Security: `.gitignore` confirmed; ticker validation confirmed; DOMPurify wrapping confirmed;
   encrypt/decrypt round-trip confirmed

## Status: **Complete — merged to master (PR #27)**
