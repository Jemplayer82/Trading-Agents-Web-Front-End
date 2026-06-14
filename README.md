<p align="center">
  <img src="assets/hero.svg" alt="TradingAgents — multi-agent LLM trading intelligence, streamed live and self-hosted" width="100%">
</p>

# TradingAgents — Web Dashboard & Deployment

A self-hosted web dashboard for running multi-agent LLM trading analysis: real-time agent streaming, Schwab portfolio scanning, S&P 500 automation, and container-based deployment.

<div align="center">
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-Uvicorn-009688?logo=fastapi&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-ghcr.io-2496ED?logo=docker&logoColor=white">
  <img alt="Ollama" src="https://img.shields.io/badge/LLM-Ollama_Cloud-000000?logo=ollama&logoColor=white">
  <img alt="SQLite" src="https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white">
</div>

> **For research and educational purposes only.** Trading performance varies with the chosen models, data quality, and market conditions. This is **not** financial, investment, or trading advice.

---

## Overview

TradingAgents runs a team of specialized LLM agents that mirror the desks of a real trading firm — analysts, researchers, a trader, and a risk/portfolio manager — and surfaces the whole pipeline in a real-time web dashboard. Submit a ticker and watch each agent stream its reasoning, ending in a BUY / SELL / HOLD decision with full reports; connect a Schwab account to scan a live portfolio; or let the scheduler sweep the entire S&P 500 every week and rebalance a paper portfolio on its own.

The project ships as container images and deploys as a Portainer edge stack, backed by a single FastAPI service and a SQLite database. The repository is **self-contained** — the underlying TradingAgents agent framework is vendored in directly, so everything needed to build and run the dashboard lives in this repo with no dependency on the upstream project.

---

## The Agent Team

Each agent owns a narrow slice of the decision and hands its findings to the next stage.

**Analysts** — four agents each study one angle of a ticker:
- **Fundamentals** — financial statements, valuation, and balance-sheet health
- **Sentiment** — news headlines and social chatter distilled into a single mood read
- **News** — macro events and market-moving headlines
- **Technical** — price action and indicators (MACD, RSI, Bollinger Bands)

**Researchers** — a bull and a bear argue the analysts' findings in a structured debate, weighing upside against risk.

**Trader** — synthesizes every report into a concrete call: direction, timing, and size.

**Risk Management & Portfolio Manager** — stress-tests the trade against volatility and liquidity, then the Portfolio Manager approves, trims, or rejects it before it reaches the (paper) book.

---

## Features

**🖥️ Web Dashboard**
- Terminal-aesthetic UI with dark theme and color-coded signals
- 5-tab interface: Run Analysis, Schwab Connection, Portfolio Scan, S&P 500 Scanner, Credentials
- Real-time WebSocket streaming of agent progress and reports
- Interactive technical charts with RSI, MACD, Bollinger Bands overlays
- Per-analysis Q&A thread (multi-turn conversation without re-running)

**📊 Portfolio & Market Automation**
- Schwab OAuth integration for brokerage account scanning
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

### Prerequisites

- Docker & Docker Compose
- An Ollama Cloud API key (or another supported provider's key)
- Python 3.12+ (for local CLI development)
- Portainer (optional — for edge stack deployment on a home lab / remote host)

### Docker Deployment (Recommended)

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

### Interactive CLI

The `tradingagents` CLI image is run as a separate container and accessed via the Portainer console (or `docker attach`):

```bash
docker run -it \
  -e OLLAMA_API_KEY=your_key \
  -e OLLAMA_BASE_URL=https://ollama.com/v1 \
  ghcr.io/jemplayer82/tradingagents:latest
```

### Local Development

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
- **Phase 2 (Deep)** — Top 50 by conviction via the full multi-agent graph
- **Phase 3 (Allocate)** — Build a $100k portfolio with position sizing

The scan re-runs automatically every Saturday, and the AI agent rebalances the paper portfolio — adding, trimming, or exiting positions as it sees fit. Results include an interactive allocation table with entry prices and performance tracking.

### Credentials Tab

API keys are managed directly from the Credentials tab — no `.env` editing required. Supports all major providers. Keys are masked in the UI (last 4 characters visible).

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

The project ships as **two pre-built images**, built by GitHub Actions and pushed to `ghcr.io`, deployed as a Portainer edge stack:

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
```

> **Security:** `OLLAMA_API_KEY` and all other secrets must never be committed to the public repo — inject them via the Portainer stack environment or local environment variables only.

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
│   └── static/             # SPA files
│       ├── index.html
│       ├── app.js
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

## License & Credits

This project builds on the open-source [TradingAgents](https://github.com/TauricResearch/TradingAgents) multi-agent framework. The repository is **self-contained** — the framework is vendored in and extended here with a full web dashboard, Schwab integration, and container-based deployment, so it builds and runs independently. See the repository for license details.

Powered by FastAPI, LangChain, yfinance, stockstats, lightweight-charts, APScheduler, and many other open-source libraries.

---

**Last Updated:** June 13, 2026
**Repo:** https://github.com/Jemplayer82/TradingAgents
