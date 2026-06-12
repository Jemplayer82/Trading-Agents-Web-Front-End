<p align="center">
  <img src="assets/TauricResearch.png" style="width: 60%; height: auto;">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="./assets/wechat.png" target="_blank"><img alt="WeChat" src="https://img.shields.io/badge/WeChat-TauricResearch-brightgreen?logo=wechat&logoColor=white"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <br>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/Join_GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>

---

# TradingAgents: Multi-Agent LLM Financial Trading Framework

## News

- **[2026-05]** **TradingAgents v0.2.5** released with the grounded Sentiment Analyst, GPT-5.5 model coverage, Qwen/GLM/MiniMax dual-region support, `TRADINGAGENTS_*` env-var configurability with API-key auto-detection, remote Ollama support, non-US alpha benchmarks, and ticker path-traversal hardening. See [CHANGELOG.md](CHANGELOG.md) for the full list.
- **[2026-04]** **TradingAgents v0.2.4** released with structured-output agents (Research Manager, Trader, Portfolio Manager), LangGraph checkpoint resume, persistent decision log, DeepSeek/Qwen/GLM/Azure provider support, Docker, and a Windows UTF-8 encoding fix.
- **[2026-03]** **TradingAgents v0.2.3** released with multi-language support, GPT-5.4 family models, unified model catalog, backtesting date fidelity, and proxy support.
- **[2026-03]** **TradingAgents v0.2.2** released with GPT-5.4/Gemini 3.1/Claude 4.6 model coverage, five-tier rating scale, OpenAI Responses API, Anthropic effort control, and cross-platform stability.
- **[2026-02]** **TradingAgents v0.2.0** released with multi-provider LLM support (GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x) and improved system architecture.
- **[2026-01]** **Trading-R1** [Technical Report](https://arxiv.org/abs/2509.11420) released.

<div align="center">
<a href="https://www.star-history.com/#TauricResearch/TradingAgents&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date" />
   <img alt="TradingAgents Star History" src="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date" style="width: 80%; height: auto;" />
 </picture>
</a>
</div>

> 🎉 **TradingAgents** officially released! We have received numerous inquiries about the work, and we would like to express our thanks for the enthusiasm in our community.
>
> So we decided to fully open-source the framework. Looking forward to building impactful projects with you!

---

## TradingAgents Framework

TradingAgents is a multi-agent trading framework that mirrors the dynamics of real-world trading firms. By deploying specialized LLM-powered agents — from fundamental analysts, sentiment experts, and technical analysts, to a trader and risk management team — the platform collaboratively evaluates market conditions and informs trading decisions. Moreover, these agents engage in dynamic discussions to pinpoint the optimal strategy.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> TradingAgents is designed for **research purposes**. Trading performance may vary based on many factors, including the chosen backbone language models, model temperature, trading periods, the quality of data, and other non-deterministic factors. [It is not intended as financial, investment, or trading advice.](https://tauric.ai/disclaimer/)

Our framework decomposes complex trading tasks into specialized roles.

### Analyst Team

- **Fundamentals Analyst:** Evaluates company financials and performance metrics, identifying intrinsic values and potential red flags.
- **Sentiment Analyst:** Aggregates news headlines, StockTwits, and Reddit chatter into a single sentiment read to gauge short-term market mood.
- **News Analyst:** Monitors global news and macroeconomic indicators, interpreting the impact of events on market conditions.
- **Technical Analyst:** Utilizes technical indicators (like MACD and RSI) to detect trading patterns and forecast price movements.

<p align="center">
  <img src="assets/analyst.png" style="width: 100%; height: auto;">
</p>

### Researcher Team

Comprises both bullish and bearish researchers who critically assess the insights provided by the Analyst Team. Through structured debates, they balance potential gains against inherent risks.

<p align="center">
  <img src="assets/researcher.png" style="width: 100%; height: auto;">
</p>

### Trader Agent

Composes reports from the analysts and researchers to make informed trading decisions, determining the timing and magnitude of trades.

<p align="center">
  <img src="assets/trader.png" style="width: 100%; height: auto;">
</p>

### Risk Management and Portfolio Manager

Continuously evaluates portfolio risk by assessing market volatility, liquidity, and other risk factors. The risk management team evaluates and adjusts trading strategies, providing assessment reports to the Portfolio Manager for final decision. The Portfolio Manager approves/rejects the transaction proposal. If approved, the order will be sent to the simulated exchange and executed.

<p align="center">
  <img src="assets/risk.png" style="width: 100%; height: auto;">
</p>

---

## Fork Enhancements: Web Dashboard & Production Features

This fork extends the original **[TradingAgents](https://github.com/TauricResearch/TradingAgents)** framework with a **full-featured web dashboard**, real-time streaming, portfolio scanning, S&P 500 automation, and production-ready deployment infrastructure.

### ⭐ What's New in This Fork

**🖥️ Web Dashboard**
- Terminal-aesthetic UI with dark theme and color-coded signals
- 5-tab interface: Run Analysis, Schwab Connection, Portfolio Scan, S&P 500 Scanner, Credentials
- Real-time WebSocket streaming of agent progress and reports
- Interactive technical charts with RSI, MACD, Bollinger Bands overlays
- Per-analysis Q&A thread (multi-turn conversation without re-running)
- Live Agent Bus feed — watch analysts, researchers, and the risk team communicate in real time as the pipeline runs
- Live progress bars for the long-running portfolio and S&P 500 scans

**📊 Portfolio & Market Automation**
- Schwab OAuth integration for brokerage account scanning
- Per-account tabs (plus an "All Accounts" view) over a brokerage-provider abstraction
- Holdings cards with current price / cost basis / current worth / % change; option positions show expiration, strike, and put/call (and are skipped by AI scans)
- Selectable data source — Schwab MCP server or built-in collection method (toggle in settings)
- Automated nightly portfolio analysis of all holdings
- S&P 500 weekly scanner (all ~500 tickers, deep-dive top 50, $100k portfolio builder)
- Background job scheduler (APScheduler with cron expressions)

**🔐 Provider & Credential Management**
- Ollama Cloud as the deployed default backend, with 14+ LLM providers supported (OpenAI, Anthropic, Google, xAI, DeepSeek, Qwen, GLM, MiniMax, OpenRouter, Azure, Ollama, Mistral, custom)
- Dashboard API key management — add/update/delete provider keys without `.env`
- Dynamic model selection with custom model name input
- Secure credential storage in SQLite (masked in UI)

**🏗️ Deployment Architecture**
- Two pre-built images: `tradingagents` (interactive CLI) and `tradingagents-web` (FastAPI dashboard)
- FastAPI serves both the REST/WebSocket API and the static dashboard SPA from a single container
- SQLite with WAL mode for concurrent access and persistence
- Background job scheduler (APScheduler) for nightly portfolio and weekly S&P 500 scans
- Deployed as a Portainer edge stack; images built and pushed to `ghcr.io` by GitHub Actions CI

**🔗 Companion: [schwab-mcp](https://github.com/Jemplayer82/schwab-mcp)**
- Containerized Node.js MCP server that gives TradingAgents a direct, real-time connection to your Schwab brokerage account
- Enables the dashboard to query live quotes, account positions, orders, and transaction history straight from Schwab
- Optional — a settings checkbox switches between the MCP server and the built-in data collection method

---

## Screenshots

### Run Analysis

![Run Analysis — live multi-agent streaming](assets/screenshot-agent-team.jpg)

*Submit a ticker and watch each agent — Market, Sentiment, News, Fundamentals, Research, Trader, Risk, and Portfolio Manager — stream its progress in real time, ending in a BUY/SELL/HOLD decision with full reports.*

### Q&A & Technical Chart

![Multi-turn Q&A and interactive technical chart](assets/screenshot-qa-chart.gif)

*Ask follow-up questions grounded in the saved analysis (e.g. "what would a good entrance point be?") and explore the interactive price chart with indicators.*

### Agent Team

![Full agent team completed with strategy report](assets/screenshot-run-analysis.gif)

*Every analyst, the Research and Risk teams, the Trader, and the Portfolio Manager complete in sequence, producing a final decision and a scaling / risk-management strategy.*

### S&P 500 Scanner

![S&P 500 scanner with $100k paper portfolio](assets/screenshot-spy-scanner.jpg)

*Scans all ~500 tickers, deep-dives the top 50 by conviction, and builds a $100k paper portfolio with live performance tracking. Runs automatically every Saturday.*

---

## Quick Start

### Web Dashboard (This Fork)

#### Prerequisites

- Docker & Docker Compose
- An Ollama Cloud API key (or another supported provider's key)
- Python 3.12+ (for local CLI development)
- Portainer (optional — for edge stack deployment on a home lab / remote host)

#### Docker Deployment (Recommended)

The dashboard runs from a pre-built image published to `ghcr.io` by GitHub Actions. A minimal `docker-compose.yml`:

```yaml
services:
  tradingagents-web:
    image: ghcr.io/jemplayer82/tradingagents-web:latest
    ports:
      - "8080:8000"        # host 8080 → container 8000
    environment:
      - OLLAMA_API_KEY=${OLLAMA_API_KEY}
      - OLLAMA_BASE_URL=https://ollama.com/v1
    volumes:
      - tradingagents-data:/home/appuser/.tradingagents
    restart: unless-stopped

volumes:
  tradingagents-data:
```

```bash
export OLLAMA_API_KEY=your_key      # never commit this
docker compose up -d
```

Open **http://localhost:8080** — FastAPI serves the dashboard directly (no separate web server). On a Portainer host, browse to `http://<host>:8080`.

> **Portainer edge stack:** set `OLLAMA_API_KEY` in the stack environment (never in the committed compose file). After each fresh CI build, force-pull `tradingagents-web:latest` on the host before redeploying so the cached image isn't reused.

#### Interactive CLI

The `tradingagents` CLI image is run as a separate container and accessed via the Portainer console (or `docker attach`):

```bash
docker run -it \
  -e OLLAMA_API_KEY=your_key \
  -e OLLAMA_BASE_URL=https://ollama.com/v1 \
  ghcr.io/jemplayer82/tradingagents:latest
```

#### Local Development

```bash
pip install -e .
pip install -r web/requirements.txt

export DATABASE_URL=sqlite:///./tradingagents.db
export OLLAMA_API_KEY=your_key
export OLLAMA_BASE_URL=https://ollama.com/v1

# Run the FastAPI dashboard (serves API + SPA on one port)
uvicorn tradingagents.web.main:app --reload --port 8000
```

Open `http://localhost:8000`.

---

## Features in Detail

### Run Analysis Tab

Single-ticker deep analysis with real-time streaming:

1. **Input Form** — Ticker, date, language, LLM provider, deep/quick models, research depth, analyst selection
2. **Progress Panel** — Live status of each agent via WebSocket
3. **Reports** — Market, sentiment, news, fundamentals, research plan, trader plan, final decision
4. **Technical Chart** — Price candles + RSI + MACD with interactive overlays
5. **Q&A Thread** — Multi-turn follow-up questions without re-running the full analysis
6. **Messages Log** — Every agent message and tool call

### Schwab Tab

Connect your Charles Schwab account via OAuth 2.0 to enable automated portfolio scanning. Tokens are stored securely and refresh automatically.

**Data source toggle.** A checkbox in settings lets you choose how account and market data is collected:

- **Schwab MCP server** *(checked)* — Routes all Schwab requests through the [schwab-mcp](https://github.com/Jemplayer82/schwab-mcp) server for live quotes, positions, orders, and transaction history.
- **Built-in method** *(unchecked)* — Uses the dashboard's own market-data retrieval (yfinance) and does not connect to Schwab at all.

When the MCP server is enabled, account positions and market data come directly from your Schwab brokerage account; the built-in method instead pulls public market data via yfinance.

### Portfolio Scan Tab

View results from automated Schwab portfolio scans:

- Aggregated portfolio briefing
- Per-ticker analysis cards with signals and rationales
- Links to full detailed analyses

### S&P 500 Tab

Weekly automated scan of all ~500 S&P 500 tickers, run in three phases:

- **Phase 1 (Quick)** — All ~500 tickers scored via yfinance + lightweight LLM
- **Phase 2 (Deep)** — Top 50 by conviction via full TradingAgentsGraph
- **Phase 3 (Allocate)** — Build a $100k portfolio with position sizing

The scan re-runs automatically every Saturday, and the AI agent rebalances the paper portfolio — adding, trimming, or exiting positions as it sees fit. Results include an interactive allocation table with entry prices and performance tracking.

### Credentials Tab

API keys are managed directly from the Credentials tab — no `.env` editing required. Supports all major providers. Keys are masked in the UI (last 4 characters visible).

---

## Agent Bus — Watch the Agents Talk

The **Agent Bus** mirrors every inter-agent handoff from the LangGraph pipeline onto a dedicated [mcp-switchboard](https://github.com/Jemplayer82/mcp-switchboard) instance running inside the stack, then streams the messages to a live feed panel in the dashboard. A visitor can watch analysts deliver reports, bull/bear researchers debate, the risk team stress-test, and the portfolio manager reach a final decision — in real time, as the run happens.

LangGraph remains the pipeline orchestrator. The bus is a **read-only mirror** — graph code is untouched; all taps live in the `web/` layer.

### Architecture

```
  LangGraph Pipeline               Bus Mirror                     Dashboard
  ─────────────────    ──────────────────────────────────    ──────────────────

  4 Analysts ────────→ on_report_delta → result messages  →┐
  Bull/Bear debate ──→ on_state        → chat turns        →├─ switchboard :3107
  Research Mgr ──────→ handoff         → instructions      →│  analysis-{id}
  Trader ────────────→ handoff         → instructions      →│       │
  Risk team ─────────→ on_state        → chat turns        →│       │ /api/bus WS
  Portfolio Mgr ─────→ on_done         → FINAL decision    →┘       │ (poll + backfill)
                                                                     ↓
  LangGraph stays as the orchestrator.                       [ Agent Bus ]  ●
  Bus is a read-only mirror of handoffs.                     Orchestrator   instruction
                                                             Market Analyst result
                                                             Bull           chat
                                                             Bear           chat
                                                             Portfolio Mgr  result
```

### Enabling the Agent Bus

The switchboard service is already in `docker-compose.yml`. Set one environment variable and it starts on the next `docker compose up`:

```bash
# Generate a fresh bearer token — keep this secret, like a password
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add the output to your `.env` (or Portainer stack environment):

```bash
SWITCHBOARD_MCP_TOKEN=<your-generated-token>
```

The `switchboard` container starts automatically, `tradingagents-web` connects to it at `http://switchboard:3107`, and the **[ Agent Bus ]** panel appears live on the Run Analysis tab.

### Fresh-Stack Quickstart

```bash
git clone https://github.com/Jemplayer82/Trading-Agents-Web-Front-End.git
cd Trading-Agents-Web-Front-End
cp .env.example .env

# Edit .env — fill in the three required values:
#   OLLAMA_API_KEY=        your Ollama Cloud key (https://ollama.com)
#   TOKEN_ENCRYPTION_KEY=  python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
#   SWITCHBOARD_MCP_TOKEN= python -c "import secrets; print(secrets.token_urlsafe(32))"

docker compose up -d
```

Open `http://localhost:8080` → Run Analysis tab → **[ Agent Bus ]** panel at the bottom.

### Agent Bus Environment Variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SWITCHBOARD_MCP_TOKEN` | Yes | — | Bearer token for the in-stack switchboard. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `BUS_MIRROR` | No | `analysis` | Set to `off` to disable all bus publishing without stopping the switchboard container |
| `SWITCHBOARD_URL` | Auto | `http://switchboard:3107` | Resolved by compose — only override if running the switchboard outside the stack |

### Future: Bus-Native Orchestration

Bus-native orchestration — replacing LangGraph edges with agents that long-poll `wait_for_message` so they react to each other rather than following a fixed graph — is pinned as a future branch (`bus-native-orchestration`). The `BUS_MIRROR=all` env value and the `wait_for_message` method in `web/bus.py` are its hooks. Nothing in this branch builds bus-native orchestration.

---

## Schwab MCP Companion

[**schwab-mcp**](https://github.com/Jemplayer82/schwab-mcp) is a companion containerized Node.js MCP server that gives TradingAgents a direct, real-time connection to your Schwab brokerage account. Rather than relying on manual data exports or delayed feeds, the dashboard communicates with Schwab through schwab-mcp to pull live quotes, account positions, open orders, and transaction history — enabling the Portfolio Scan, S&P 500 scanner, and OAuth token management to work seamlessly.

Using schwab-mcp is optional. A checkbox in the Schwab settings lets you switch between the MCP server and the dashboard's built-in data collection method, so you can run with or without the companion server.

### Available Tools

| Tool | Description |
|------|-------------|
| `get_quotes` | Real-time quote data for one or more symbols |
| `get_accounts` | Account balances and positions |
| `get_orders` | Open and historical orders |
| `place_order` | Submit equity orders |
| `get_transactions` | Account transaction history |
| `get_market_hours` | Market open/close status |

```bash
docker run -p 3000:3000 \
  -e SCHWAB_CLIENT_ID=your_client_id \
  -e SCHWAB_CLIENT_SECRET=your_secret \
  ghcr.io/jemplayer82/schwab-mcp:latest
```

See [Jemplayer82/schwab-mcp](https://github.com/Jemplayer82/schwab-mcp) for full setup and MCP client configuration.

---

## Architecture

### Deployment Topology

The fork ships as **two pre-built images**, built by GitHub Actions and pushed to `ghcr.io`, deployed as a Portainer edge stack:

```
Portainer Edge Stack  (openclaw home lab)
│
├─ tradingagents-web    ghcr.io/jemplayer82/tradingagents-web   (FastAPI · Dockerfile.web)
│    host 8080 → container 8000
│    ├─ Serves the dashboard SPA      (index.html, app.js, portfolio.js, spy.js …)
│    ├─ REST + WebSocket API          (analysis, Schwab OAuth, scans, chart data, Q&A)
│    ├─ APScheduler                   (nightly portfolio · weekly S&P 500 · token health)
│    └─ SQLite (WAL)                  (preferences · analyses · portfolio_scans · spy_scans · credentials)
│
└─ tradingagents        ghcr.io/jemplayer82/tradingagents       (CLI · Dockerfile)
     Interactive single-ticker analysis via Portainer console attach

LLM backend:  Ollama Cloud  (https://ollama.com/v1, OLLAMA_API_KEY)
```

FastAPI (via Uvicorn) serves both the API and the static SPA from the single `tradingagents-web` container — there is no separate reverse proxy. Secrets such as `OLLAMA_API_KEY` live in the Portainer stack environment and are never committed.

### Data Models

| Model | Purpose |
|-------|---------|
| **Preferences** | User settings (LLM provider, models, language, analysts, research depth) |
| **Analyses** | Single-ticker runs with reports and signals (BUY/SELL/HOLD) |
| **Portfolio Scans** | Batch analysis of Schwab holdings |
| **S&P 500 Scans** | Multi-phase SPX analysis with portfolio allocations |
| **Provider Credentials** | API keys (encrypted, masked in UI) |

---

## Configuration

### Environment Variables

```bash
# Database (persisted to a Docker volume)
DATABASE_URL=sqlite:////home/appuser/.tradingagents/tradingagents.db

# LLM backend — Ollama Cloud (deployed default)
OLLAMA_API_KEY=your_key                 # Portainer stack env only — never commit
OLLAMA_BASE_URL=https://ollama.com/v1

# Optional additional providers
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

# Schwab OAuth (required for portfolio scans)
SCHWAB_CLIENT_ID=your_client_id
SCHWAB_REDIRECT_URI=http://localhost:8080/api/auth/schwab/callback

# Logging
LOG_LEVEL=INFO

# Agent Bus (in-stack switchboard — optional but recommended)
SWITCHBOARD_MCP_TOKEN=<generated>       # bearer token — generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
BUS_MIRROR=analysis                      # set to "off" to disable bus publishing without stopping the container
```

> **Security:** `OLLAMA_API_KEY` and all other secrets must never be committed to the public fork — inject them via the Portainer stack environment or local environment variables only.

---

## Technical Stack

| Layer | Technology |
|-------|------------|
| Backend & Web Server | FastAPI + Uvicorn (serves API and SPA from one container) |
| Frontend | Vanilla JS + HTML5 (no build step) |
| Database | SQLite with WAL mode |
| Charting | lightweight-charts |
| Task Scheduling | APScheduler |
| Markdown Rendering | marked.js |
| Containers | Docker, deployed via Portainer edge stack |
| CI/CD | GitHub Actions matrix → `ghcr.io` images |
| LLM Backend | Ollama Cloud (default), LangChain multi-provider abstraction |
| Stock Data | yfinance + 5-year caching |
| Technical Indicators | stockstats |
| Schwab Integration | OAuth 2.0 + [schwab-mcp](https://github.com/Jemplayer82/schwab-mcp) |

---

## Project Structure

```
tradingagents/
├── tradingagents/
│   ├── graph/              # Core multi-agent graph
│   ├── dataflows/          # Data fetching & indicators
│   └── tools/              # LLM tool definitions
├── web/
│   ├── main.py             # FastAPI app — API + serves the SPA
│   ├── scheduler.py        # APScheduler background jobs
│   ├── db.py               # SQLite schema
│   ├── credentials.py      # API key management
│   ├── llm_helpers.py      # Multi-provider LLM abstraction
│   ├── spy_scanner.py      # S&P 500 3-phase scanner
│   ├── spy_allocator.py    # $100k portfolio builder
│   ├── bus.py              # Switchboard MCP client + resilient publisher
│   ├── bus_mirror.py       # Mirror LangGraph handoffs onto the agent bus
│   └── static/             # SPA files
│       ├── index.html
│       ├── app.js
│       ├── bus.js          # Agent Bus WebSocket client + live feed panel
│       ├── portfolio.js
│       ├── spy.js
│       ├── credentials.js
│       └── styles.css
├── Dockerfile              # CLI image  → ghcr.io/jemplayer82/tradingagents
├── Dockerfile.web          # Web image  → ghcr.io/jemplayer82/tradingagents-web
└── docker-compose.yml
```

### Running Tests

```bash
pytest tradingagents/tests/
pytest web/tests/ -v
```

---

## Troubleshooting

### Chart endpoint returns 500 error

**Cause:** Legacy cached OHLCV CSV files have an `index` column instead of `Date`.

**Fix:** Code normalizes column names automatically. If it persists, clear the cache:

```bash
rm -rf ~/.tradingagents/cache/*.csv
```

### Schwab scan doesn't start

**Cause:** OAuth token not saved or expired.

**Fix:** Click "Connect to Schwab" in the Schwab tab, complete the OAuth flow, and check token status.

### S&P 500 scan hangs

**Cause:** Network slowness, LLM provider overload, or database lock.

**Fix:** Check logs, retry, and verify the Ollama Cloud key:

```bash
docker logs tradingagents-web
```

### API key not taking effect

**Cause:** Process running with stale config.

**Fix:**

```bash
docker restart tradingagents-web
```

### Stale image after a fresh CI build

**Cause:** The host reused a cached `tradingagents-web:latest` (the name previously existed as a different image).

**Fix:** Force-pull the latest image before redeploying the stack:

```bash
docker compose pull && docker compose up -d
```

### Port 8080 already in use

**Fix:** Change the host side of the port mapping in `docker-compose.yml`:

```yaml
ports:
  - "9090:8000"   # change 9090 to any free host port
```

---

## Contributing

Pull requests welcome! Areas of focus:

- UI/UX improvements (mobile responsiveness, keyboard shortcuts)
- New LLM providers
- Chart enhancements (more indicators, drawing tools)
- Performance optimizations
- Testing (unit, integration, E2E)
- Documentation

---

## License

Respects the original TradingAgents license. See [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) for details.

---

## Acknowledgments

- **TauricResearch** — Original TradingAgents framework and multi-agent architecture
- **Community** — All contributors and users providing feedback
- **Libraries** — FastAPI, LangChain, yfinance, stockstats, lightweight-charts, and many others

---

## Citation

If you use TradingAgents (original or fork), please cite the original framework:

```bibtex
@article{Trading-R1,
  title  = {Trading-R1: Technical Report},
  author = {TauricResearch},
  year   = {2026},
  url    = {https://arxiv.org/abs/2509.11420}
}
```

---

**Last Updated:** June 11, 2026
**Fork:** https://github.com/Jemplayer82/Trading-Agents-Web-Front-End
**Original:** https://github.com/TauricResearch/TradingAgents
