# Changelog

All notable changes to TradingAgents are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Breaking changes within the 0.x line are called out explicitly.

## [Unreleased]

### Added

- **Daily options paper trading.** New "Options" dashboard tab paper-trades
  long single-leg calls/puts on S&P 500 movers, $100k per options paper
  account. Every weekday (07:30 ET): momentum/volume pre-screen ranks the
  universe, the top 250 movers + SPY get the cheap quick-LLM scan, the top 100
  directional names + SPY (BUY *and* SELL — puts need bearish candidates; SPY
  is deep-dived every run) get the full multi-agent deep dive, then after the
  open (09:35 ET gate for live
  quotes) a deterministic contract selector (~21 DTE, ~0.45 delta or near-ATM,
  liquidity gates against zero-bid/wide/illiquid quotes) feeds an LLM
  allocator that opens/holds/closes contracts under hard guardrails: force-
  close at DTE ≤ 3 or premium −60% (stop-loss), per-position and total-premium
  caps scaled by aggressiveness, max 15 open positions, and a deterministic
  conviction-ranked fallback when the LLM fails.
  - Positions and cash live in new normalized tables (`options_positions` +
    append-only `options_cash_ledger`) with transactional, idempotent
    open/close/settle helpers — real realized-P&L accounting, unlike the
    equity snapshot portfolio.
  - Expiry settlement models OCC auto-exercise (ITM ≥ $0.01 settles at
    intrinsic vs the last close on/before expiry; catch-up safe after
    downtime), swept nightly at 20:00 ET and opportunistically before every
    build/refresh.
  - Marks refresh hourly via Schwab bulk option quotes (new
    `getOptionChain`/`getOptionExpirationChain` wrappers and an option-safe
    `mark → mid → last` price extractor in
    `tradingagents/dataflows/schwab_mcp.py` — still zero order tools, paper
    only) with a yfinance chain fallback; missing quotes carry the last mark
    floored at intrinsic, never zero.
  - Options runs are `spy_scans` rows with a new `kind` column, so quick/deep
    progress bars, cooperative cancel, and the stuck-run reaper work
    unchanged; `paper_accounts.kind` separates equity and options accounts.
  - Three new scheduler jobs (scan / marks / settlement) with `--run-*-now`
    CLI flags, `/api/options*` routes + nginx location, and
    `web/static/options.js`.
  - Options runs participate in the scan-serialization queue: they enqueue
    behind a running portfolio/S&P scan and vice versa, and the dequeuer
    dispatches `kind='options'` rows to the options build (not the equity
    pipeline).

### Fixed

- **Background scans silently did nothing on a fresh database.** `build_config`
  defaulted the *provider* to Ollama while `DEFAULT_CONFIG` supplied OpenAI
  *model* names, so with no saved preferences every background LLM call 404'd
  (`model "gpt-5.4-mini" not found`). Interactive runs were unaffected because
  the form always posts an explicit model — which is also the only thing that
  ever wrote preferences, so the bug was invisible on any instance that had run
  one analysis. `build_config` now resolves a provider-appropriate default from
  the provider's own catalog, after any explicit param and any
  `TRADINGAGENTS_*_THINK_LLM` operator override, and only when the configured
  name belongs to a different provider.
- **Four retired models were still offered in the model picker.** Ollama Cloud
  retires models and returns HTTP 410 for them; `kimi-k2:1t-cloud` (the *deep*
  default), `glm-4.6:cloud`, `deepseek-v3.1:671b-cloud` and
  `qwen3-coder:480b-cloud` had all gone dead. Catalog refreshed against the
  live model list, with the revalidation command documented alongside it.
- **A totally failed scan reported success.** Per-ticker errors degrade to
  HOLD/conviction-1 by design (one bad ticker must not sink 500), but nothing
  inspected the aggregate — so a dead backend was indistinguishable from a
  quiet market and the run completed green with an empty portfolio and no
  alert. Quick scans now fail the run when ≥50% error, deep dives when 100%
  fail (the stricter bar reflects that partial deep-dive failure is normal),
  and the alert quotes the underlying error. Missing price data carries no
  `error` key, so it never counts toward the rate.
- **Failed analyses could be traded on.** A crashed deep dive keeps the
  quick-scan `signal`/`conviction` in its result row, and neither the options
  contract vetter nor the equity allocator inspected `error` — so a partial
  deep-dive outage could open paper positions off analyses that never
  completed. Both paths now drop errored rows before allocation.
- **Zero-trade runs now explain themselves** in the allocator report, telling
  "nothing scored directional" apart from "dives failed" and "nothing passed
  contract vetting".
- **Scan queue never dequeued.** `_dequeue_next_scan`'s `ORDER BY created_at`
  referenced a column absent from its UNION's result set — SQLite raised
  `OperationalError` on the first queued scan, so queued runs sat forever.
  Also closed the create→start race by counting `pending` scans as busy
  (back-to-back scan requests could previously both start).

## [1.2.1] — 2026-07-07

### Added

- **Login brute-force throttling.** Failed dashboard logins are recorded per
  username and per client IP (`login_attempts` table); 5 failures per username
  or 20 per IP within a 15-minute window answer 429 before any PBKDF2 work.
  Closes the High-severity item deferred in `SECURITY_AUDIT.md`.

### Removed

- Orphaned root `test.py` scratch script and the completed
  `tradingagents/llm_clients/TODO.md`.
- Unused dependencies `redis`, `backtrader`, `parsel`, `pytz`; `setuptools`
  demoted to `[build-system]` only. Slims the install and the Docker image.

### Changed

- Shipped refactor plans archived under `docs/history/`.

## [1.2.0] — 2026-06-18

Version jumped 1.0.0 → 1.2.0 with the switchboard-orchestrator line; there was
no 1.1.x release.

### Added

- **Switchboard as universal LLM gateway.** Analysis LLM calls can route over
  the Agent Bus (`llm_request`/`llm_response`) to a pluggable backend; the
  `tradingagents-llm-router` compose service bridges to Ollama/OpenAI.
- **Cleo daemon** (`scripts/cleo_llm_handler.py`): bare-host bus responder that
  streams tokens from the `claude` CLI (no API key needed), with single-instance
  flock guard, stderr-drain + watchdog against subprocess hangs, and a systemd
  unit under `deploy/cleo/`.
- **Live token streaming** to the dashboard via the bus mirror.
- **Multi-account S&P 500 paper trading**, per-tab aggressiveness/bias
  controls, and a technical-indicator selector.
- **Portainer redeploy tooling** (`scripts/redeploy.py`,
  `scripts/render_stack_payload.py`) — repo compose is the source of truth for
  the deployed stack.
- `OLLAMA_MAX_CONCURRENCY` setting exposed in the UI registry.

### Changed

- **`SwitchboardOrchestrator` replaces the langgraph path for all web runs**
  (single-ticker, portfolio, S&P scans). The CLI still uses
  `TradingAgentsGraph`; langgraph removal is deferred until the CLI's
  streaming migration.
- Bus agent id renamed `langgraph-orchestrator` → `switchboard-orchestrator`.

## [1.0.0] — 2026-06-14

First release of the web-dashboard era (the repo's front-end line; core
framework changes continue to be listed per-feature below).

### Added

- **FastAPI + nginx dashboard**: Run Analysis (streamed over WebSocket),
  Portfolio Scan, S&P 500 Scanner, and Settings tabs; vanilla-JS SPA.
- **Dashboard auth**: PBKDF2 password login with server-side sessions,
  first-run setup flow, internal-token service auth.
- **Schwab integration**: OAuth (CSRF-protected), encrypted token store,
  live holdings with cost basis, optional MCP data source toggle.
- **Scheduler container**: nightly portfolio scan (Mon–Fri), newsletter
  email, token health checks, run-failure alerts (webhook + email).
- **Agent Bus** (mcp-switchboard) with the Live Reasoning panel.
- **Whole-share paper trading** for the S&P scanner with per-position P&L.
- **Security hardening** (rounds 1–3 in `SECURITY_AUDIT.md`): Fernet
  encryption at rest for credentials, XSS escapes at every render site,
  DOMPurify for LLM markdown, timing-safe token comparisons, SQL allow-lists.
- **CI gates**: ruff + pytest on every PR; README claims guarded by tests.

### Changed

- License corrected to **AGPL-3.0** (Fathom Consulting LLC).
- Deploys standardized on prebuilt `ghcr.io/jemplayer82/*` images; six-service
  docker-compose stack.

## [0.2.5] — 2026-05-11

### Added

- **Grounded Sentiment Analyst.** The renamed `sentiment_analyst` now reads
  real Yahoo News, StockTwits, and Reddit data before generating its report,
  replacing the prior flow that could fabricate social posts under prompt
  pressure. (#557, #607)
- **MiniMax provider** with the full M2.x catalog (M2.7 / M2.5 / M2.1 / M2
  plus highspeed variants, 204K context). Dual-region: Global
  (`MINIMAX_API_KEY`) and China (`MINIMAX_CN_API_KEY`).
- **Dual-region Qwen and GLM** with separate keys per region — international
  (`DASHSCOPE_API_KEY`, `ZHIPU_API_KEY`) and China (`DASHSCOPE_CN_API_KEY`,
  `ZHIPU_CN_API_KEY`), selectable via a secondary region prompt. (#758)
- **`TRADINGAGENTS_*` env-var configurability for `DEFAULT_CONFIG`.** Override
  `llm_provider`, deep/quick model IDs, `backend_url`, `output_language`,
  debate-round counts, checkpoint flag, and benchmark ticker via `.env` with
  type-aware coercion (string / int / bool). (#602)
- **Interactive API-key detection in the CLI.** When the selected provider's
  key is missing, the CLI prompts for it and persists the value to `.env`
  so the analysis run continues without restart.
- **Remote Ollama support.** `OLLAMA_BASE_URL` points the CLI and the
  programmatic client at a remote `ollama-serve`. The CLI surfaces the
  resolved endpoint and warns on common malformed inputs. Adds a
  `"Custom model ID"` option for models pulled via `ollama pull`. (#648, #768)
- **Configurable news-fetch parameters** in `DEFAULT_CONFIG` — per-ticker
  article limit, macro headline limit, lookback window, and macro search
  queries. (#606, #683)
- **Configurable alpha benchmark** for non-US tickers. Replaces hardcoded
  SPY with regional indices for `.NS` (^NSEI), `.T` (^N225), `.HK` (^HSI),
  `.L` (^FTSE), `.TO` (^GSPTSE), `.AX` (^AXJO), `.BO` (^BSESN); explicit
  `benchmark_ticker` override available. Eliminates FX drift dominating
  alpha for non-USD listings. (#628, #684)
- **Multi-language output covers every user-facing agent** — researchers,
  risk debators, research manager, and trader, ending the previous
  partial-localization reports. (#575)
- **Model catalog refresh.** OpenAI GPT-5.5 frontier, Anthropic Claude Opus
  4.7, Gemini 3.1 Flash-Lite GA, xAI Grok 4.20, Qwen 3.6 line. Versioned IDs
  only; auto-shifting aliases moved to the `"Custom model ID"` option.

### Changed

- **Sentiment Analyst** is now consistently named across the CLI dropdown,
  status panel, and final reports (previously the backend was renamed but
  the CLI still said "Social Analyst"). The `AnalystType.SOCIAL = "social"`
  wire value is kept for saved-config back-compat.

### Fixed

- **Structured output works on DeepSeek V4 / reasoner and MiniMax M2.x.**
  Those providers reject `tool_choice` per their tool-calling docs; the
  binding flow now skips it automatically via a capability table.
- **`pip install .` installations pick up the project `.env`** when running
  the CLI as a console script. (#747)
- **Reports save end-to-end** — streamed chunks were previously dropped from
  `complete_report.md`. (#719, #736)
- **Ticker prompt preserves exchange suffixes** (`.SH`, `.SZ`, `.SS`, `.HK`,
  `.T`, etc.) for A-share, HK, Tokyo, and other non-US flows. (#770)
- **Docker permission errors** no longer block first-run write to
  `~/.tradingagents/`. (#519, #627, #672, #771)
- **Config state no longer leaks between runs** when sub-dicts are mutated;
  `set_config` partial updates preserve sibling defaults. (#788)
- **`max_recur_limit` config actually applies** — previously read but not
  forwarded to the propagator. (#764)
- **Missing-API-key error** names the exact env var to set. (#680)
- **Quieter startup** — suppressed the noisy upstream
  `LangChainPendingDeprecationWarning` from langgraph-checkpoint; will be
  removed once that package ships its fix.

### Security

- **Ticker path-traversal validation** at every filesystem-path site (cache,
  checkpoint database, results) so a malicious ticker cannot escape its
  intended directory. (#618)

## [0.2.4] — 2026-04-25

### Added

- **Structured-output decision agents.** Research Manager, Trader, and Portfolio
  Manager now use `llm.with_structured_output(Schema)` on their primary call
  and return typed Pydantic instances. Each provider's native structured-output
  mode is used (`json_schema` for OpenAI / xAI, `response_schema` for Gemini,
  tool-use for Anthropic, function-calling for OpenAI-compatible providers).
  Render helpers preserve the existing markdown shape so memory log, CLI
  display, and saved reports keep working unchanged. (#434)
- **LangGraph checkpoint resume** — opt-in via `--checkpoint`. State is saved
  after each node so crashed or interrupted runs resume from the last
  successful step. Per-ticker SQLite databases under
  `~/.tradingagents/cache/checkpoints/`. `--clear-checkpoints` resets them. (#594)
- **Persistent decision log** replacing the per-agent BM25 memory. Decisions
  are stored automatically at the end of `propagate()`; the next same-ticker
  run resolves prior pending entries with realised return, alpha vs SPY, and
  a one-paragraph reflection. Override path with `TRADINGAGENTS_MEMORY_LOG_PATH`.
  Optional `memory_log_max_entries` config caps resolved entries; pending
  entries are never pruned. (#578, #563, #564, #579)
- **DeepSeek, Qwen (Alibaba DashScope), GLM (Zhipu), and Azure OpenAI**
  providers, plus dynamic OpenRouter model selection.
- **Docker support** — multi-stage build with separate dev and runtime images.
- **`scripts/smoke_structured_output.py`** — diagnostic that exercises the
  three structured-output agents against any provider so contributors can
  verify their setup with one command.
- **5-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell) used
  consistently by Research Manager, Portfolio Manager, signal processor, and
  the memory log; Trader keeps 3-tier (Buy / Hold / Sell) since transaction
  direction is naturally ternary.
- **Pytest fixtures** — lazy LLM client imports plus placeholder API keys so
  the test suite runs cleanly without credentials. (#588)

### Changed

- **`backend_url` default is now `None`** rather than the OpenAI URL. Each
  provider client falls back to its native default. The previous default
  leaked the OpenAI URL into non-OpenAI clients (e.g. Gemini), producing
  malformed request URLs for Python users who switched providers without
  overriding `backend_url`. The CLI flow is unaffected.
- All file I/O passes explicit `encoding="utf-8"` so Windows users no longer
  hit `UnicodeEncodeError` with the cp1252 default. (#543, #550, #576)
- Cache and log directories moved to `~/.tradingagents/` to resolve Docker
  permission issues. (#519)
- `SignalProcessor` reads the rating from the Portfolio Manager's rendered
  markdown via a deterministic heuristic — no extra LLM call.
- OpenAI structured-output calls default to `method="function_calling"` to
  avoid noisy `PydanticSerializationUnexpectedValue` warnings emitted by
  langchain-openai's Responses-API parse path. Same typed result, no warnings.

### Fixed

- Empty memory no longer triggers fabricated past-lessons in agent prompts;
  the memory-log redesign makes this structurally impossible since only the
  Portfolio Manager consults memory and only when entries exist. (#572)
- Tool-call logging processes every chunk message, not just the last one, and
  memory score normalization handles empty score arrays. (#534, #531)

### Removed

- `FinancialSituationMemory` (the per-agent BM25 system) and the dead
  `reflect_and_remember()` plumbing; subsumed by the persistent decision log.
- Hardcoded Google endpoint that caused 404 when `langchain-google-genai`
  changed its API path. (#493, #496)

### Contributors

Thanks to everyone who shaped this release through code, design, and reports:

- [@claytonbrown](https://github.com/claytonbrown) — checkpoint resume (#594), test fixtures (#588), design feedback on cost tracking (#582) and structured validation (#583)
- [@Bcardo](https://github.com/Bcardo) — memory-log redesign (#579), empty-memory hallucination report (#572), encoding fix proposal (#570)
- [@voidborne-d](https://github.com/voidborne-d) — memory persistence design (#564), portfolio manager state fix (#503)
- [@mannubaveja007](https://github.com/mannubaveja007) — structured-output feature request (#434)
- [@kelder66](https://github.com/kelder66) — RAM-only memory issue (#563)
- [@Gujiassh](https://github.com/Gujiassh) — tool-call logging fix (#534), test stub PR (#533)
- [@iuyup](https://github.com/iuyup) — memory score normalization fix (#531)
- [@kaihg](https://github.com/kaihg) — Google base_url fix (#496)
- [@32ryh98yfe](https://github.com/32ryh98yfe) — Gemini 404 report (#493)
- [@uppb](https://github.com/uppb) — OpenRouter dynamic model selection (#482)
- [@guoz14](https://github.com/guoz14) — OpenRouter limited-model report (#337)
- [@samchenku](https://github.com/samchenku) — indicator name normalization (#490)
- [@JasonOA888](https://github.com/JasonOA888) — y_finance pandas import fix (#488)
- [@tiffanychum](https://github.com/tiffanychum) — stale import cleanup (#499)
- [@zaizou](https://github.com/zaizou) — Docker permission issue (#519)
- [@Stosman123](https://github.com/Stosman123), [@mauropuga](https://github.com/mauropuga), [@hotwind2015](https://github.com/hotwind2015) — Windows encoding bug reports (#543, #550, #576)
- [@nnishad](https://github.com/nnishad), [@atharvajoshi01](https://github.com/atharvajoshi01) — encoding fix proposals (#568, #549)

## [0.2.3] — 2026-03-29

### Added

- **Multi-language output** for analyst reports and final decisions, with a
  CLI selector. Internal agent debate stays in English for reasoning quality. (#472)
- **GPT-5.4 family models** in the default catalog, with deep/quick model split.
- **Unified model catalog** as a single source of truth for CLI options and
  provider validation.

### Changed

- `base_url` is forwarded to Google and Anthropic clients so corporate proxies
  work consistently across providers. (#427)
- Standardised the Google `api_key` parameter to the unified `api_key` form.

### Fixed

- Backtesting fetchers no longer leak look-ahead data when `curr_date` is in
  the middle of a fetched window. (#475)
- Invalid indicator names from the LLM are caught at the tool boundary instead
  of crashing the run. (#429)
- yfinance news fetchers respect the same exponential-backoff retry as price
  fetchers. (#445)

### Contributors

- [@ahmedk20](https://github.com/ahmedk20) — multi-language output (#472)
- [@CadeYu](https://github.com/CadeYu) — model catalog typing (#464)
- [@javierdejesusda](https://github.com/javierdejesusda) — unified Google API key parameter (#453)
- [@voidborne-d](https://github.com/voidborne-d) — yfinance news retry (#445)
- [@kostakost2](https://github.com/kostakost2) — look-ahead bias report (#475)
- [@lu-zhengda](https://github.com/lu-zhengda) — proxy/base_url support request (#427)
- [@VamsiKrishna2021](https://github.com/VamsiKrishna2021) — invalid indicator crash report (#429)

## [0.2.2] — 2026-03-22

### Added

- **Five-tier rating scale** (Buy / Overweight / Hold / Underweight / Sell)
  introduced for the Portfolio Manager.
- **Anthropic effort level** support for Claude models.
- **OpenAI Responses API** path for native OpenAI models.

### Changed

- `risk_manager` renamed to `portfolio_manager` to match the role description
  shown in the CLI display.
- Exchange-qualified tickers (e.g. `7203.T`, `BRK.B`) preserved across all
  agent prompts and tool calls.
- Process-level UTF-8 default attempted for cross-platform consistency
  (note: this approach did not actually take effect; replaced in v0.2.4 with
  explicit per-call `encoding="utf-8"` arguments).

### Fixed

- yfinance rate-limit errors are retried with exponential backoff. (#426)
- HTTP client SSL customisation is supported for environments that need
  custom certificate bundles. (#379)
- Report-section writes handle list-of-string content gracefully.

### Contributors

- [@CadeYu](https://github.com/CadeYu) — exchange-qualified ticker preservation (#413)
- [@yang1002378395-cmyk](https://github.com/yang1002378395-cmyk) — HTTP client SSL customisation (#379)

## [0.2.1] — 2026-03-15

### Security

- Patched `langchain-core` vulnerability (LangGrinch). (#335)
- Removed `chainlit` dependency affected by CVE-2026-22218.

### Added

- `pyproject.toml` build-system configuration; the project now installs via
  modern packaging tooling.

### Removed

- `setup.py` — dependencies consolidated to `pyproject.toml`.

### Fixed

- Risk manager reads the correct fundamental report source. (#341)
- All `open()` calls receive an explicit UTF-8 encoding (initial pass).
- `get_indicators` tool handles comma-separated indicator names from the LLM. (#368)
- `Propagation` initialises every debate-state field so risk debaters never
  see missing keys.
- Stock data parsing tolerates malformed CSVs and NaN values.
- Conditional debate logic respects the configured round count. (#361)

### Contributors

- [@RinZ27](https://github.com/RinZ27) — `langchain-core` security patch (#335)
- [@Ljx-007](https://github.com/Ljx-007) — risk manager fundamental-report fix (#341)
- [@makk9](https://github.com/makk9) — debate-rounds config issue (#361)

## [0.2.0] — 2026-02-04

This is the largest release since the initial public version. The framework
moved from single-provider to a multi-provider architecture and grew several
production-ready surfaces.

### Added

- **Multi-provider LLM support** (OpenAI, Google, Anthropic, xAI, OpenRouter,
  Ollama) via a factory pattern, with provider-specific thinking configurations.
- **Alpha Vantage** integration as a configurable primary data provider, with
  yfinance as a community-stability fallback.
- **Footer statistics** in the CLI: real-time tracking of LLM calls, tool
  calls, and token usage via LangChain callbacks.
- **Post-analysis report saving** — the framework writes per-section markdown
  files (analyst reports, debate transcripts, final decision) when a run
  completes.
- **Announcements panel** — fetches updates from `api.tauric.ai/v1/announcements`
  for the CLI welcome screen.
- **Tool fallbacks** so a single vendor outage does not stop the pipeline.

### Changed

- Risky / Safe risk debaters renamed to **Aggressive / Conservative** for
  consistency with the displayed agent labels.
- Default data vendor switched to balance reliability and quota across
  community deployments.
- Ollama and OpenRouter model lists updated; default endpoints clarified.

### Fixed

- Analyst status tracking and message deduplication in the live display.
- Infinite-loop guard in the agent loop; reflection and logging hardened.
- Various data-vendor implementation bugs and tool-signature mismatches.

### Contributors

This release is the first with substantial outside contributions; many community
PRs from late 2025 also landed here.

- [@luohy15](https://github.com/luohy15) — Alpha Vantage data-vendor integration (#235)
- [@EdwardoSunny](https://github.com/EdwardoSunny) — yfinance fetching optimisations (#245)
- [@Mirza-Samad-Ahmed-Baig](https://github.com/Mirza-Samad-Ahmed-Baig) — infinite-loop guard, reflection, and logging fixes (#89)
- [@ZeroAct](https://github.com/ZeroAct) — saved results path support (#29)
- [@Zhongyi-Lu](https://github.com/Zhongyi-Lu) — `.env` gitignore (#49)
- [@csoboy](https://github.com/csoboy) — local Ollama setup (#53)
- [@chauhang](https://github.com/chauhang) — initial Docker support attempt (#47, later reverted; the merged Docker support shipped in v0.2.4)

## [0.1.1] — 2025-06-07

### Removed

- Static site assets that had been bundled with v0.1.0; the public site now
  lives separately.

## [0.1.0] — 2025-06-05

### Added

- **Initial public release** of the TradingAgents multi-agent trading
  framework: market / sentiment / news / fundamentals analysts; bull and bear
  researchers; trader; aggressive, conservative, and neutral risk debaters;
  portfolio manager. LangGraph orchestration, yfinance data, per-agent
  BM25 memory, single-provider OpenAI integration, interactive CLI.

[0.2.4]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/TauricResearch/TradingAgents/releases/tag/v0.1.0
