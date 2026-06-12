# Security Audit — TradingAgents Web

_Scope: the web layer (`web/`, `web/auth/`, `web/static/`) — credential storage,
authentication/session handling, the FastAPI attack surface, and the Schwab OAuth
flow. Date: 2026-06._

This document records what was reviewed, the issues found with severities, what was
fixed in this pass, and what is deliberately deferred. Code references point at the
modules as they stand after the fixes.

## What was already solid (no change needed)

- **Password hashing**: PBKDF2-HMAC-SHA256, 600k iterations, per-user 16-byte salt,
  verified with `hmac.compare_digest` (`web/auth_app.py`). OWASP-current.
- **Session tokens**: `secrets.token_urlsafe(32)`; stored server-side in SQLite with
  expiry and startup purge.
- **SQL**: all data-row queries are parameterized (`?` placeholders).
- **No dangerous sinks**: no `eval`/`exec`/`pickle`/`yaml.load`/`shell=True`.
- **CORS**: no wildcard middleware — same-origin only.
- **Schwab OAuth tokens**: already Fernet-encrypted at rest (`web/auth/token_store.py`).
- **API responses mask secrets** before returning them (`web/credentials.py`).
- **Login error** is generic ("invalid username or password") — no user enumeration.
- **`.gitignore`** excludes `.env` and `*.db`.

## Findings & fixes (applied in this pass)

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 1 | **Critical** | Provider API keys and app settings stored **in plaintext** in SQLite (`provider_credentials.api_key`, `app_settings.value`) — incl. `SCHWAB_APP_SECRET`, `SMTP_PASS`, `ALPACA_API_SECRET`. | **Fixed** |
| 2 | High | Schwab OAuth flow had **no `state` parameter** → CSRF / account-link forgery on the callback. | **Fixed** |
| 3 | Medium | DB file created with umask-default perms; secrets could be group/world-readable. | **Fixed** |
| 4 | Medium | `TOKEN_ENCRYPTION_KEY` accepted unvalidated → cryptic failure deep in a request if malformed. | **Fixed** |
| 5 | Medium | `/api/ticker-info/{ticker}` did not validate the ticker before it reached yfinance / Schwab / a URL. | **Fixed** |
| 6 | Medium | LLM-authored markdown rendered into `innerHTML` **without sanitization** (`app.js`, `portfolio.js`, `spy.js`). | **Fixed** |
| 7 | Low/Med | Schwab callback echoed raw upstream `error`/exception text into the HTML response (info disclosure). | **Fixed** |
| 8 | Low | `update_spy_scan()` interpolates kwargs keys into SQL (`SET` clause) without an allow-list. | **Fixed** |
| 9 | Low | WebSocket internal-token compared with `==` (timing side-channel) instead of `hmac.compare_digest`. | **Fixed** |
| 10| Low | Session cookie `SameSite=lax`; `strict` is safe here and stronger. | **Fixed** |

### Fix details

1. **Encryption at rest** — new `web/secret_box.py` reuses the existing
   `TOKEN_ENCRYPTION_KEY` (Fernet) to encrypt secrets on write / decrypt on read in
   `web/db.py`. Ciphertext carries an `enc:v1:` prefix. **Backward-compatible**:
   encryption activates only when the key is set; existing plaintext rows are migrated
   on `init_db()`; keyless deployments keep working (with a logged warning). Verified:
   on-disk values are ciphertext, reads decrypt transparently, plaintext→encrypted
   migration is idempotent.
2. **OAuth `state`** — `web/main.py` mints a `secrets.token_urlsafe(32)` nonce, stores
   it in a short-lived `SameSite=lax` cookie, includes it in the Schwab auth URL, and
   verifies it (`hmac.compare_digest`) on the callback before exchanging the code.
   `web/auth/schwab.py:build_auth_url(state)` now requires the nonce.
3. **DB perms** — `init_db()` `chmod 0o600` on the SQLite file.
4. **Key validation** — `secret_box.validate_key()` (called from `init_db()`) fails fast
   with an actionable message if `TOKEN_ENCRYPTION_KEY` is set but not a valid Fernet key.
5. **Ticker validation** — reuses `tradingagents.dataflows.utils.safe_ticker_component`
   at the web boundary; bad charset → HTTP 400.
6. **Markdown sanitization** — `utils.js:renderMarkdown` wraps `marked.parse` with
   DOMPurify; all report/Q&A render sites use it.
7. **Error hygiene** — callback logs the real error server-side and returns a generic
   message to the browser.
8. **SQL allow-list** — `update_spy_scan` rejects any column not in an explicit set.
9. **Timing-safe compare** — WS handler uses `hmac.compare_digest`.
10. **`SameSite=strict`** — the dashboard only uses the session cookie on same-origin
    XHR; the OAuth return is a separate public callback, so strict has no UX cost.

## Corrected audit finding

An earlier pass flagged `INTERNAL_API_TOKEN` as "optional → inter-container calls proceed
unauthenticated." Re-reading `auth_app.is_authorized`, the logic is **fail-closed**: when
the token is unset, the internal-token branch is skipped and the request falls through to
the session-cookie check, which rejects an unauthenticated internal call. No fix needed;
making the env var mandatory would risk breaking valid keyless/dev deployments. (The one
real adjacent issue — the WS path using `==` — is fixed, item 9.)

## SMTP credential handling (reviewed, no change)

`web/newsletter.py` catches SMTP errors with `log.exception`, which records the traceback
(file/line/function) but **not** local variables, so `SMTP_PASS` is not exposed. No
`set_debuglevel` is enabled. Considered safe as-is.

## Deferred (behavioral / infrastructure — not in this PR)

These were intentionally left out of a "consolidate + safe-hardening" change because they
add dependencies or a schema/privilege model and need their own testing:

- **Login rate-limiting / lockout** (High). `/api/auth/login` has no throttling; PBKDF2
  slows but does not stop brute force. Recommend `slowapi` or a DB-backed attempt counter.
- **Admin / RBAC** (Medium). User-management, settings-write and credential-read endpoints
  authenticate but do not check for an admin role. Recommend an `is_admin` column + a
  privilege check (requires a DB migration).
- **`SCHWAB_MCP_URL` / `SCHWAB_CALLBACK_URL` validation** (Medium, SSRF/open-redirect).
  Operator-controlled config today; recommend host-allowlisting `SCHWAB_MCP_URL` and
  asserting the callback URL targets this app's own callback path.

## Round 2 — Agent Bus, brokerages, scan progress (2026-06-12)

A second pass covering the ~4.4k lines that landed after the original audit (Agent Bus,
brokerage abstraction, option handling, scan progress bar).

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 11 | Low | **Regression of #9.** The new `/api/bus` WebSocket re-introduced `internal == expected` for the `X-Internal-Token` gate (a copy of the `/api/analyze` gate that *does* use `hmac.compare_digest`). Timing side-channel on `INTERNAL_API_TOKEN`. | **Fixed** — `web/main.py` now uses `hmac.compare_digest`; regression test added (`tests/test_bus_bridge.py::test_wrong_internal_token_closes_with_4401`). |

### Reviewed, no change

- **Bus message content** (`web/bus_mirror.py`): all message text is sourced from the
  LangGraph state (agent output), travels as JSON over the bus, and is rendered in the
  browser via `textContent` (never `innerHTML`) — no injection path. Content is also
  length-capped (500–700 chars) at publish.
- **Bus client URL** (`web/bus.py`): `base_url` comes from `SWITCHBOARD_URL` (env, set at
  container start), never from request data — no SSRF via the bus client.
- **Brokerage data path** (`web/brokerages.py`): positions come from the trusted
  `mcp-schwab` server; values are coerced to `float`/strings and only ever rendered through
  `escapeHtml`. The option subtext (`strike`/`put_call`) is now escaped too (WS1).

### Deferred (round 2)

- **`SWITCHBOARD_MCP_TOKEN` lifecycle** (Low). Dedicated bus token lives in the container
  env in plaintext with no rotation; a switchboard compromise exposes only the per-run
  analysis mirror (no trading authority). Acceptable for an internal-only service;
  revisit if the bus is ever exposed beyond the Docker network.
- **Scan-progress endpoints** (Low). `/api/portfolio-scans/{id}` and `/api/spy-scans/{id}`
  use sequential integer ids and are session-gated; a logged-in user could enumerate scan
  ids, but all scans belong to the single-tenant deployment. Tie to an owner if multi-user
  / RBAC lands (see the deferred RBAC item above).

## Round 3 — stored XSS in the Settings tab (2026-06-12)

Found during a maintainer-documentation pass over the frontend.

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| 12 | **High** | **Stored XSS via username.** `credentials.js:loadUsers` interpolated `u.username` into `innerHTML` unescaped. Usernames are user-supplied and `POST /api/auth/users` is reachable by **any** logged-in user (no admin role — see the deferred RBAC item), so a username like `<img src=x onerror=...>` executed for anyone viewing the Settings tab's Users table. | **Fixed** — `escapeHtml()` at render. |
| 13 | Medium | **Stored XSS via non-secret setting values.** `mask_setting()` returns non-secret values **verbatim**, and `loadSettings` rendered `s.masked` into `innerHTML` unescaped — a user-set value (e.g. `DASHBOARD_URL`) executed as HTML for any settings viewer. | **Fixed** — `escapeHtml()` at render. |
| 14 | Low | Same-pattern cleanups: secret-masked values (≤4 attacker chars, no practical payload), analyst checkbox labels from the server registry (`app.js`), and raw `${e}` exception objects in `innerHTML` error paths (`portfolio.js` ×3, `spy.js` ×1). | **Fixed** — `escapeHtml()` everywhere. |

Reviewed, no change: custom setting **keys** are server-validated against `^[A-Z][A-Z0-9_]*$`
(`web/main.py`) before storage, so their interpolation is safe; the remaining `innerHTML`
templates in `credentials.js` interpolate only registry constants.

## Operational recommendation

Set `TOKEN_ENCRYPTION_KEY` in every deployment so secrets are encrypted at rest. Generate
one with:

```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
