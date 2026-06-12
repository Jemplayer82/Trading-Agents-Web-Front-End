# Architecture

A map of the system for anyone picking this up cold. It explains the big pieces, how
they run, and where to look (and extend) for common changes.

## Big picture

TradingAgents is a **multi-agent LLM trading-analysis framework** (`tradingagents/`)
with three front doors on top of it:

- an **interactive CLI** (`cli/`),
- a **web dashboard** (`web/` — FastAPI backend + a vanilla-JS frontend in
  `web/static/`),
- and a set of **background jobs** (nightly portfolio scans, a morning newsletter).

The core takes a ticker + date, runs a graph of LLM agents (analysts → researchers →
trader → risk team → portfolio manager), and produces a structured BUY/HOLD/SELL
decision plus per-stage reports. The web and CLI layers are thin: they collect inputs,
call the core, and persist/present the results.

```
            ┌───────────────────────────────────────────────┐
            │              tradingagents/ (core)             │
   ticker ─▶│  graph/  ──orchestrates──▶ agents/             │──▶ BUY/HOLD/SELL
   + date   │     │                         │  (analysts,    │    + reports
            │     │                         │   researchers, │
            │  llm_clients/ (provider       │   trader,      │
            │   factory)   dataflows/       │   risk, PM)    │
            │              (market/news/     └───────────────│
            │               fundamentals data)               │
            └───────────────────────────────────────────────┘
                ▲                  ▲                  ▲
                │                  │                  │
            cli/main.py       web/main.py       web/portfolio_main.py
            (Typer TUI)       (api process)     (portfolio process)
```

## Runtime processes (production, `docker-compose.yml`)

All share one Docker image and one data volume (`tradingagents_data`, mounted at
`~/.tradingagents`), which holds the SQLite DB and the encrypted Schwab token file.

| Service | Entry point | Role |
|---------|-------------|------|
| `tradingagents-api` | `uvicorn web.main:app` | Dashboard backend: single-ticker analysis (WebSocket-streamed), auth/login, settings/credentials, Schwab OAuth. |
| `tradingagents-portfolio` | `uvicorn web.portfolio_main:app` | **Separate** app so long portfolio/S&P scans don't block the ad-hoc api. Runs each holding through the core, then the aggregator. |
| `tradingagents-scheduler` | `python -m web.scheduler` | APScheduler daemon: nightly portfolio scan (22:00 ET), 5am newsletter, hourly Schwab-token health check. Calls the api/portfolio apps over the Docker network. |
| `tradingagents-web` | nginx image | Serves `web/static/` and reverse-proxies the `/api/*` routes (see route priority below). Holds no secrets. |
| `switchboard` | `ghcr.io/jemplayer82/mcp-switchboard` | **Optional, internal-only** message bus for the live Agent Bus feed (see below). No host port; reachable only on the Docker network at `switchboard:3107`. Own SQLite volume (`switchboard_data`). |
| `tradingagents` | (CLI) | The Typer TUI, attached to interactively. |

**nginx route priority** (`web/nginx.conf`): more-specific `/api/` prefixes are matched
**before** the generic block, and they route to different apps. `/api/spy`,
`/api/accounts`, and `/api/portfolio` go to the **portfolio** app; everything else under
`/api/` (including the `/api/analyze` and `/api/bus` WebSockets) goes to the **api** app.
Order matters — if the generic `/api/` block came first it would swallow `/api/accounts`
and the account tabs would 404.

Inter-service HTTP calls (scheduler → api/portfolio) authenticate with an
`X-Internal-Token` header (`INTERNAL_API_TOKEN`, compared with `hmac.compare_digest`);
browser requests use a session cookie. The auth gate lives in `web/auth_app.py` and is
fail-closed.

## The agent graph (`tradingagents/graph/`, `tradingagents/agents/`)

`TradingAgentsGraph` (`graph/trading_graph.py`) builds a LangGraph state machine
(`graph/setup.py`, routing in `graph/conditional_logic.py`) and runs it:

1. **Analysts** (`agents/analysts/`) — market, sentiment/social, news, fundamentals —
   each calls data tools and writes a report.
2. **Researchers** (`agents/researchers/`) — a bull vs. bear debate, refereed by the
   **research manager** (`agents/managers/`).
3. **Trader** (`agents/trader/`) proposes a plan.
4. **Risk team** (`agents/risk_mgmt/`) — aggressive/neutral/conservative debate —
   refereed by the **portfolio manager**, which emits the final decision.

State shapes are in `agents/utils/agent_states.py`; long runs can checkpoint/resume via
`graph/checkpointer.py`. `graph/signal_processing.py` extracts the final BUY/HOLD/SELL.
`graph/portfolio_graph.py` wraps all of the above to sweep a whole Schwab portfolio.

The data tools the analysts call are re-exported from
`agents/utils/agent_utils.py` (see its `__all__`) and bound onto the graph in
`trading_graph.py`.

## Agent Bus (live inter-agent feed)

An **optional read-only mirror** of the pipeline's agent handoffs, streamed to the
dashboard so a viewer can watch the agents "talk" in real time. LangGraph stays the
orchestrator — the bus only observes.

- `web/bus.py` — a small sync MCP client (`SwitchboardClient`) plus `BusPublisher`, a
  daemon-thread publisher with a bounded queue + circuit breaker that **never raises into
  the analysis**. `get_publisher()` returns `None` unless `SWITCHBOARD_URL` +
  `SWITCHBOARD_MCP_TOKEN` are set, so missing config = byte-identical old behavior.
- `web/bus_mirror.py` — `RunMirror` taps the `web/runner.py` stream (analyst results,
  bull/bear + risk debate turns, handoffs, final decision) and publishes them to a
  per-run `analysis-{id}` channel. `BUS_MIRROR=off` disables it.
- `web/main.py` `/api/bus` WebSocket — per-connection poll over cursor-free history reads:
  resolve channel → backfill → forward `bus_message` frames. Auth is identical to
  `/api/analyze` (session cookie or `X-Internal-Token` via `hmac.compare_digest`); bus
  outages send a status frame but never close the socket.
- `web/static/bus.js` — the `[ Agent Bus ]` panel (per-agent colored badges, auto-scroll,
  reconnect). Message text is inserted via `textContent`, never `innerHTML`.

Everything lives in the `web/` layer; the graph code is untouched. The bus runs in the
optional `switchboard` container.

## Brokerage data (`web/brokerages.py`)

A small provider abstraction so holdings aren't hardcoded to one broker. `BrokerageProvider`
(ABC) → `SchwabProvider` today; `fetch_all_accounts()` returns normalized account/position
dicts (account ids namespaced `schwab:<num>`), and `parse_occ_symbol()` decodes OCC option
symbols (expiration/strike/put-call/underlying). `web/portfolio_main.py` (`_accounts_split`,
`_mcp_positions`) consumes this; adding a broker = one new provider class. The legacy
`_parse_schwab_account` helper (Schwab-only, `/api/spy-account` drift views) is deprecated
in favor of it.

## LLM providers (`tradingagents/llm_clients/`)

One factory: `create_llm_client(provider, model, ...)` → a `BaseLLMClient` whose
`get_llm()` returns a LangChain chat model. Provider specifics (Anthropic effort,
OpenAI/DeepSeek/MiniMax response handling, Google, Azure) live in per-provider modules.
Supporting tables: `api_key_env.py` (provider → env var), `capabilities.py` (per-model
feature flags), `model_catalog.py` (UI menus), `defaults.py` (shared default endpoints).

**To add a provider:** add a client class, register it in `factory.py`, add its
key env var to `api_key_env.py`, and (for the UI menu) entries in `model_catalog.py`.

The web layer builds quick clients via `web/llm_helpers.py:llm_for()`, which wraps the
factory and resolves credentials in the order: explicit config → DB credential → env var
→ provider default.

## Data & persistence

- **SQLite** (`web/db.py`): users/sessions, saved preferences, provider credentials and
  app settings, analyses, portfolio + S&P scans. Created `0600`.
- **Secrets at rest** (`web/secret_box.py`): provider API keys and app settings are
  Fernet-encrypted (key = `TOKEN_ENCRYPTION_KEY`) when that key is set; encrypted values
  carry an `enc:v1:` prefix. Backward-compatible — keyless deployments store plaintext.
- **Schwab OAuth tokens** (`web/auth/token_store.py`): Fernet-encrypted file on the
  shared volume, refreshed by `web/auth/schwab.py`.
- **Config**: `tradingagents/default_config.py` (with `TRADINGAGENTS_*` env overrides) +
  the runtime singleton in `tradingagents/dataflows/config.py`.

## Frontend (`web/static/`)

Vanilla JS, no build step — classic `<script>` tags loaded in order from `index.html`.
`utils.js` loads first and holds the shared globals (`$`, `escapeHtml`, `fmtTs`,
`renderMarkdown`, `apiFetch`, `progressBar`); the per-tab modules (`app.js`,
`portfolio.js` — analysis + Schwab account tabs + option cards, `spy.js`, `bus.js` — the
Agent Bus panel, `credentials.js`, `auth.js`) build on them. Because these are non-module
scripts sharing one global scope, **load order matters and names must not be redeclared**
(see the header comment in `utils.js`).

**Long-scan progress.** Both the portfolio scan and the S&P 500 scan write progress
counters to their DB rows (`portfolio_scans.scanned_count/scan_total/current_ticker`;
`spy_scans.quick_count/quick_total/deep_count/deep_total`). The frontend polls the scan's
detail endpoint every 5s while `status` is running and renders a `[ Progress ]` bar via the
shared `progressBar(count, total)` helper, then stops the timer when the scan finishes.

## Tests & tooling

- `pytest` (config in `pyproject.toml`); `tests/conftest.py` mocks provider API keys so
  the suite runs offline. Markers: `unit`, `integration`, `smoke`.
- `ruff` is the linter/formatter (config in `pyproject.toml`). The maintained surface —
  `web/` and `tests/` — is kept clean and CI-gated; the upstream `cli/` and
  `tradingagents/` trees predate the linter and are `extend-exclude`d (run
  `ruff check tradingagents` to see that deferred backlog). `.github/workflows/ci.yml`
  runs `ruff check .` + `pytest` on every PR.
