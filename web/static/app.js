// TradingAgents Web — "Run Analysis" tab (the default dashboard view).
//
// Drives a single-ticker analysis end-to-end over the /api/analyze WebSocket
// (backend: web/main.py accepts the socket, web/runner.py streams the run) and
// renders the results: agent progress grid, live reasoning timeline, the 9
// report tabs (REPORT_KEYS), final decision badge, plus the per-analysis
// technical chart (lightweight-charts) and Q&A panel.
//
// Endpoints used (all proxied to the api app by web/nginx.conf's generic /api/):
//   GET    /api/providers                  provider / model / depth / analyst menus
//   GET    /api/preferences                last-used form values
//   GET    /api/analyses                   history sidebar list
//   GET    /api/analyses/{id}              load a saved run
//   DELETE /api/analyses/{id}              delete a saved run
//   GET    /api/analyses/{id}/chart-data   point-in-time OHLC + indicators
//   POST   /api/analyses/{id}/ask          Q&A about a saved run
//   GET    /api/ticker-info/{ticker}       company name / website header
//   GET    /api/auth/schwab/status         master Schwab switch (can be slow — see
//                                          applySchwabVisibility)
//   WS     /api/analyze                    the run itself
//
// WebSocket frames (see handleFrame): started, status, report_update, messages,
// token, debate, done, error. "started" comes from web/main.py; the rest are
// emitted by web/runner.py.
//
// Classic <script defer> file sharing ONE global scope with the other modules —
// see the utils.js header. Consumes $, escapeHtml, fmtTs, renderMarkdown from
// utils.js; never redeclare those names at top level here. Defines
// applySchwabVisibility (called as window.applySchwabVisibility by credentials.js
// when the SCHWAB_ENABLED toggle flips). Dispatches the "analysis-started" window
// event (consumed by bus.js to join the run's bus channel) and listens for
// "load-analysis" (dispatched by portfolio.js / spy.js cross-links).

// The 9 report tabs, in display order: [state key, tab label]. The two debate
// keys have no direct report field — they're synthesized in debateToMarkdown().
const REPORT_KEYS = [
  ["market_report", "Market"],
  ["sentiment_report", "Sentiment"],
  ["news_report", "News"],
  ["fundamentals_report", "Fundamentals"],
  ["investment_debate", "Bull vs Bear"],
  ["investment_plan", "Research Plan"],
  ["trader_investment_plan", "Trader Plan"],
  ["risk_debate", "Risk Debate"],
  ["final_trade_decision", "Final Decision"],
];

// analyst checkbox key -> the report key whose arrival marks that analyst done.
const ANALYST_REPORT_MAP = {
  market: "market_report",
  social: "sentiment_report",
  news: "news_report",
  fundamentals: "fundamentals_report",
};

let providersData = null;
let runningSocket = null;
let runStart = 0;
let elapsedTimer = null;
let activeReportKey = null;
let currentReports = {};
let activeHistoryId = null;
let lastRunMeta = null;
let reasoningNodes = {}; // node -> {el, buffer}

// $, escapeHtml, fmtTs and renderMarkdown live in utils.js (loaded first).

document.addEventListener("DOMContentLoaded", async () => {
  await loadProviders();
  await loadPreferences();
  await loadHistory();
  buildReportTabs();
  $("btn-run").addEventListener("click", startRun);
  $("btn-stop").addEventListener("click", stopRun);
  $("f-quick-provider").addEventListener("change", onQuickProviderChange);
  $("f-deep-provider").addEventListener("change", onDeepProviderChange);

  // Aggressiveness slider live label
  const aggSlider = $("f-aggressiveness");
  if (aggSlider) {
    aggSlider.addEventListener("input", () => { $("f-aggressiveness-val").textContent = aggSlider.value; });
  }

  // Bias toggle
  document.querySelectorAll(".bias-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".bias-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
    });
  });
  // Open the native calendar on a click/focus anywhere in the date field, not
  // just on its icon. showPicker() is recent — guard it for older browsers.
  const fdate = $("f-date");
  if (fdate) {
    const openPicker = () => { try { fdate.showPicker?.(); } catch (e) { /* not supported / not allowed */ } };
    fdate.addEventListener("focus", openPicker);
    fdate.addEventListener("click", openPicker);
  }
  setupTickerSearch();
  setupScanActivity();
  const rt = $("reasoning-toggle");
  if (rt) {
    rt.addEventListener("click", () => {
      const log = $("reasoning");
      const hidden = log.style.display === "none";
      log.style.display = hidden ? "" : "none";
      rt.textContent = hidden ? "hide" : "show";
    });
  }
  // Cross-tab links (S&P 500 / Portfolio Scan) ask us to open an analysis.
  window.addEventListener("load-analysis", (ev) => {
    if (ev.detail != null) loadHistoryItem(ev.detail);
  });
  applySchwabVisibility();
});

// Master Schwab switch (SCHWAB_ENABLED): when off, hide every Schwab surface so
// users without a brokerage still get reports + the S&P 500 paper builder.
async function applySchwabVisibility(enabledOverride) {
  // Callers may pass the known master-switch value (e.g. right after saving the
  // toggle) to skip the /api/auth/schwab/status round-trip, which can block on a
  // 30s MCP call when Schwab is enabled.
  let enabled = true;
  if (typeof enabledOverride === "boolean") {
    enabled = enabledOverride;
  } else {
    try {
      const s = await (await fetch("/api/auth/schwab/status")).json();
      enabled = s.enabled !== false;
    } catch (e) { /* default to showing */ }
  }
  window.schwabEnabled = enabled;
  const tabBtn = $("tab-portfolio");
  const acctBtn = $("btn-spy-account");
  if (tabBtn) tabBtn.style.display = enabled ? "" : "none";
  if (acctBtn) acctBtn.style.display = enabled ? "" : "none";
  if (!enabled) {
    const acctPanel = $("spy-account-panel");
    if (acctPanel) acctPanel.innerHTML = "";
    const portPane = document.querySelector('[data-pane="portfolio"]');
    if (portPane && !portPane.hidden) {
      document.querySelector('.main-tab[data-tab="analyze"]')?.click();
    }
  }
}

// Fetch + render the company name and website link for the loaded analysis.
async function showCompanyHeader(ticker) {
  const el = $("decision-company");
  if (!el) return;
  el.innerHTML = "";
  if (!ticker) return;
  try {
    const r = await fetch(`/api/ticker-info/${encodeURIComponent(ticker)}`);
    if (!r.ok) return;
    const info = await r.json();
    const name = info.name || ticker;
    const site = info.website;
    let html = `<strong style="color:var(--text);">${escapeHtml(name)}</strong>`;
    if (site) {
      const label = site.replace(/^https?:\/\//, "").replace(/\/$/, "");
      html += ` · <a href="${escapeHtml(site)}" target="_blank" rel="noopener">${escapeHtml(label)} ↗</a>`;
    }
    el.innerHTML = html;
  } catch (e) { /* non-fatal */ }
}

// ===== Run form: providers, models, preferences =====

async function loadProviders() {
  const resp = await fetch("/api/providers");
  providersData = await resp.json();

  const quickProvSel = $("f-quick-provider");
  const deepProvSel = $("f-deep-provider");
  quickProvSel.innerHTML = "";
  deepProvSel.innerHTML = "";
  providersData.providers.forEach((p) => {
    const optQ = document.createElement("option");
    optQ.value = p.key;
    optQ.textContent = p.label;
    quickProvSel.appendChild(optQ);
    const optD = document.createElement("option");
    optD.value = p.key;
    optD.textContent = p.label;
    deepProvSel.appendChild(optD);
  });

  const langSel = $("f-language");
  langSel.innerHTML = "";
  providersData.languages.forEach((l) => {
    const opt = document.createElement("option");
    opt.value = l;
    opt.textContent = l;
    langSel.appendChild(opt);
  });

  const depthSel = $("f-depth");
  depthSel.innerHTML = "";
  providersData.depth_presets.forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d.value;
    opt.textContent = d.label;
    depthSel.appendChild(opt);
  });

  const analystBox = $("f-analysts");
  analystBox.innerHTML = "";
  providersData.analysts.forEach((a) => {
    const id = `an-${a.key}`;
    const lbl = document.createElement("label");
    lbl.innerHTML = `<input type="checkbox" id="${escapeHtml(id)}" value="${escapeHtml(a.key)}" /> ${escapeHtml(a.label)}`;
    analystBox.appendChild(lbl);
  });
}

function findProvider(key) {
  return providersData.providers.find((p) => p.key === key);
}

function onQuickProviderChange() {
  const prov = findProvider($("f-quick-provider").value);
  fillModelSelect("f-quick-model", "f-quick-model-custom", prov?.models?.quick || []);
}

function onDeepProviderChange() {
  const prov = findProvider($("f-deep-provider").value);
  fillModelSelect("f-deep-model", "f-deep-model-custom", prov?.models?.deep || []);
}

function fillModelSelect(selectId, customInputId, options) {
  const sel = $(selectId);
  const customEl = $(customInputId);
  const prevValue = sel.value;
  sel.innerHTML = "";
  options.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.value;
    opt.textContent = m.label || m.value;
    sel.appendChild(opt);
  });
  const customOpt = document.createElement("option");
  customOpt.value = "__custom__";
  customOpt.textContent = "Custom…";
  sel.appendChild(customOpt);
  // Restore previous value if still present
  if (prevValue && [...sel.options].some((o) => o.value === prevValue)) {
    sel.value = prevValue;
  }
  customEl.style.display = sel.value === "__custom__" ? "" : "none";
  sel.onchange = () => {
    customEl.style.display = sel.value === "__custom__" ? "" : "none";
    if (sel.value === "__custom__") customEl.focus();
  };
}

function getModelValue(selectId, customInputId) {
  const sel = $(selectId);
  return sel.value === "__custom__" ? $(customInputId).value.trim() : sel.value;
}

function restoreModelPref(selectId, customInputId, saved) {
  const sel = $(selectId);
  const customEl = $(customInputId);
  if ([...sel.options].some((o) => o.value === saved)) {
    sel.value = saved;
  } else {
    sel.value = "__custom__";
    customEl.value = saved;
    customEl.style.display = "";
  }
}

async function loadPreferences() {
  const today = new Date().toISOString().slice(0, 10);
  $("f-date").value = today;

  let prefs = {};
  try {
    const resp = await fetch("/api/preferences");
    prefs = await resp.json();
  } catch (e) {}

  $("f-ticker").value = prefs.ticker || "";
  $("f-date").value = prefs.trade_date || today;
  $("f-language").value = prefs.language || "English";
  $("f-quick-provider").value = prefs.quick_provider || prefs.provider || "ollama";
  onQuickProviderChange();
  $("f-deep-provider").value = prefs.deep_provider || prefs.provider || "ollama";
  onDeepProviderChange();
  if (prefs.deep_model) restoreModelPref("f-deep-model", "f-deep-model-custom", prefs.deep_model);
  if (prefs.quick_model) restoreModelPref("f-quick-model", "f-quick-model-custom", prefs.quick_model);
  $("f-depth").value = prefs.research_depth || 1;

  if ($("f-aggressiveness")) {
    $("f-aggressiveness").value = prefs.aggressiveness || 5;
    $("f-aggressiveness-val").textContent = $("f-aggressiveness").value;
  }
  if (prefs.bias) {
    document.querySelectorAll(".bias-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.val === prefs.bias);
    });
  }

  const wanted = new Set(prefs.analysts || ["market", "social", "news", "fundamentals"]);
  document.querySelectorAll("#f-analysts input[type=checkbox]").forEach((cb) => {
    cb.checked = wanted.has(cb.value);
  });
}

// ===== History sidebar =====

async function loadHistory() {
  const resp = await fetch("/api/analyses");
  const { analyses } = await resp.json();
  const ul = $("history");
  ul.innerHTML = "";
  if (!analyses.length) {
    const li = document.createElement("li");
    li.className = "dim empty";
    li.textContent = "(no runs yet)";
    li.style.cursor = "default";
    ul.appendChild(li);
    return;
  }
  analyses.forEach((a) => {
    const li = document.createElement("li");
    li.dataset.id = a.id;
    if (String(a.id) === String(activeHistoryId)) li.classList.add("active");
    const sig = (a.processed_signal || "—").toUpperCase();
    li.innerHTML = `
      <span class="h-main">
        <span class="h-top">
          <span class="h-tk">${escapeHtml(a.ticker)}</span>
          <span class="h-sig ${sig}">${sig}</span>
        </span>
        <span class="h-ts">${fmtTs(a.created_at)}</span>
      </span>
      <button class="h-del" title="Delete this run" aria-label="Delete">×</button>
    `;
    li.querySelector(".h-main").addEventListener("click", () => loadHistoryItem(a.id));
    li.querySelector(".h-del").addEventListener("click", (ev) => {
      ev.stopPropagation();
      deleteHistoryItem(a.id, a.ticker);
    });
    ul.appendChild(li);
  });
}

async function loadHistoryItem(id) {
  activeHistoryId = id;
  document.querySelectorAll("#history li").forEach((li) => {
    li.classList.toggle("active", String(li.dataset.id) === String(id));
  });
  const resp = await fetch(`/api/analyses/${id}`);
  const a = await resp.json();
  const fs = a.full_state || {};
  currentReports = {};
  REPORT_KEYS.forEach(([k]) => {
    if (k === "investment_debate" || k === "risk_debate") return; // synthesized below
    const v = fs[k] || a[k] || "";
    if (v) currentReports[k] = v;
  });
  // Synthesize the bull/bear + risk debate transcripts from the saved state.
  const invMd = debateToMarkdown(fs.investment_debate_state);
  if (invMd) currentReports.investment_debate = invMd;
  const riskMd = debateToMarkdown(fs.risk_debate_state);
  if (riskMd) currentReports.risk_debate = riskMd;
  resetReasoning(); // reasoning timeline is live-only
  refreshReportTabs();
  if (a.processed_signal || a.final_decision) {
    showDecision(a.processed_signal, a.final_decision, `${a.ticker} • ${a.trade_date} • ${fmtTs(a.created_at)}`);
  }
  showCompanyHeader(a.ticker);
  setupQaForAnalysis(a);
  loadChartForAnalysis(a);
  setStatus("done", `loaded #${a.id}`);
}

async function deleteHistoryItem(id, ticker) {
  if (!confirm(`Delete saved analysis for ${ticker} (#${id})?`)) return;
  const resp = await fetch(`/api/analyses/${id}`, { method: "DELETE" });
  if (!resp.ok) {
    alert("Delete failed.");
    return;
  }
  // If the deleted entry was loaded, clear the main panel.
  if (String(activeHistoryId) === String(id)) {
    activeHistoryId = null;
    currentReports = {};
    activeReportKey = null;
    refreshReportTabs();
    renderActiveReport();
    $("decision-panel").hidden = true;
    hideChartAndQa();
    setStatus("idle", "idle");
  }
  await loadHistory();
}

// ===== Ticker type-ahead (company name -> symbol) =====
// Additive to the plain text input: the box still accepts a raw symbol typed
// directly (startRun reads #f-ticker.value unchanged). Backed by
// GET /api/ticker-search (Yahoo Finance search, web/main.py).

let tickerSearchTimer = null;
let tickerSuggestItems = [];
let tickerSuggestActive = -1;

function setupTickerSearch() {
  const input = $("f-ticker");
  const box = $("ticker-suggest");
  if (!input || !box) return;

  input.addEventListener("input", () => {
    const q = input.value.trim();
    tickerSuggestActive = -1;
    if (tickerSearchTimer) clearTimeout(tickerSearchTimer);
    if (q.length < 2) { hideTickerSuggest(); return; }
    // Debounce so we issue one request after typing settles, not per keystroke.
    tickerSearchTimer = setTimeout(() => runTickerSearch(q), 250);
  });

  input.addEventListener("keydown", (e) => {
    if (box.hidden || !tickerSuggestItems.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      tickerSuggestActive = Math.min(tickerSuggestActive + 1, tickerSuggestItems.length - 1);
      renderTickerActive();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      tickerSuggestActive = Math.max(tickerSuggestActive - 1, 0);
      renderTickerActive();
    } else if (e.key === "Enter" && tickerSuggestActive >= 0) {
      e.preventDefault();
      pickTicker(tickerSuggestItems[tickerSuggestActive].symbol);
    } else if (e.key === "Escape") {
      hideTickerSuggest();
    }
  });

  // Close on blur, but delay so a row click lands before the box disappears.
  input.addEventListener("blur", () => setTimeout(hideTickerSuggest, 150));
}

async function runTickerSearch(q) {
  let items = [];
  try {
    const resp = await fetch(`/api/ticker-search?q=${encodeURIComponent(q)}`);
    if (resp.ok) items = await resp.json();
  } catch (e) { /* fail soft — a raw symbol typed directly still runs */ }
  // Stale guard: drop the response if the box moved on while we waited.
  if ($("f-ticker").value.trim() !== q) return;
  tickerSuggestItems = Array.isArray(items) ? items : [];
  renderTickerSuggest();
}

function renderTickerSuggest() {
  const box = $("ticker-suggest");
  if (!box) return;
  if (!tickerSuggestItems.length) { hideTickerSuggest(); return; }
  box.innerHTML = tickerSuggestItems
    .map((it, i) =>
      `<li class="ticker-suggest-item${i === tickerSuggestActive ? " active" : ""}" data-symbol="${escapeHtml(it.symbol)}">` +
        `<span class="ts-sym">${escapeHtml(it.symbol)}</span>` +
        `<span class="ts-name">${escapeHtml(it.name || "")}</span>` +
        `<span class="ts-exch">${escapeHtml(it.exchange || "")}</span>` +
      "</li>")
    .join("");
  // mousedown (not click) so the pick fires before the input's blur handler.
  box.querySelectorAll("li").forEach((li) => {
    li.addEventListener("mousedown", (e) => { e.preventDefault(); pickTicker(li.dataset.symbol); });
  });
  box.hidden = false;
}

function renderTickerActive() {
  const box = $("ticker-suggest");
  if (!box) return;
  box.querySelectorAll("li").forEach((li, i) => li.classList.toggle("active", i === tickerSuggestActive));
}

function pickTicker(symbol) {
  if (symbol) $("f-ticker").value = symbol;
  hideTickerSuggest();
}

function hideTickerSuggest() {
  const box = $("ticker-suggest");
  if (box) { box.hidden = true; box.innerHTML = ""; }
  tickerSuggestItems = [];
  tickerSuggestActive = -1;
}

// ===== Running-scan activity banner =====
// Surfaces a live progress bar on the Run Analysis tab whenever a Portfolio or
// S&P 500 scan is running in the portfolio container. The list endpoints live on
// the portfolio app but nginx routes /api/portfolio* and /api/spy* there, so a
// plain fetch from here reaches them. Polls every 5s while this tab is visible.

function setupScanActivity() {
  const box = $("scan-activity");
  if (!box) return;
  // Delegated "view →" link: jump to the owning tab (box is rewritten each poll).
  box.addEventListener("click", (e) => {
    const link = e.target.closest(".scan-activity-link");
    if (!link) return;
    e.preventDefault();
    document.querySelector(`.main-tab[data-tab="${link.dataset.tab}"]`)?.click();
  });
  pollScanActivity();  // analyze is the default tab — poll right away
  document.addEventListener("tab-shown", (ev) => {
    if (ev.detail === "analyze") pollScanActivity();
  });
  setInterval(() => {
    const pane = document.querySelector('[data-pane="analyze"]');
    if (pane && !pane.hidden) pollScanActivity();
  }, 5000);
}

async function pollScanActivity() {
  const box = $("scan-activity");
  if (!box) return;
  const blocks = [];
  try {
    const scans = await (await fetch("/api/portfolio-scans")).json();
    const run = Array.isArray(scans) ? scans.find((s) => s.status === "running") : null;
    if (run) blocks.push(scanActivityPortfolio(run));
  } catch (e) { /* portfolio app unreachable / not authed — skip */ }
  try {
    const scans = await (await fetch("/api/spy-scans")).json();
    const run = Array.isArray(scans) ? scans.find((s) => s.status && s.status.startsWith("running")) : null;
    if (run) blocks.push(scanActivitySpy(run));
  } catch (e) { /* skip */ }

  if (!blocks.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.innerHTML = '<div class="panel-title">[ Scan in progress ]</div>' + blocks.join("");
  box.hidden = false;
}

function scanActivityPortfolio(scan) {
  const sc = scan.scanned_count || 0;
  const st = scan.scan_total || 0;
  const ticker = scan.current_ticker ? ` · <strong>${escapeHtml(scan.current_ticker)}</strong>` : "";
  return (
    '<div class="scan-activity-row">' +
      '<div class="scan-activity-head">' +
        '<a href="#" class="scan-activity-link" data-tab="portfolio">Portfolio scan →</a> ' +
        `<span class="dim">${sc}/${st} analyzed${ticker}</span>` +
      "</div>" +
      progressBar(sc, st) +
    "</div>"
  );
}

function scanActivitySpy(scan) {
  const qt = scan.quick_total || 500;
  const qc = scan.quick_count || 0;
  const dt = scan.deep_total || 50;
  const dc = scan.deep_count || 0;
  return (
    '<div class="scan-activity-row">' +
      '<div class="scan-activity-head">' +
        '<a href="#" class="scan-activity-link" data-tab="spy">S&amp;P 500 scan →</a>' +
      "</div>" +
      `<div class="scan-activity-sub">Quick ${qc}/${qt}</div>` +
      progressBar(qc, qt) +
      `<div class="scan-activity-sub">Deep ${dc}/${dt}</div>` +
      progressBar(dc, dt) +
    "</div>"
  );
}

// ===== Run lifecycle (WebSocket) =====

function collectParams() {
  const analysts = [];
  document.querySelectorAll("#f-analysts input[type=checkbox]:checked").forEach((cb) =>
    analysts.push(cb.value)
  );
  const activeBiasBtn = document.querySelector(".bias-btn.active");
  return {
    ticker: $("f-ticker").value.trim(),
    trade_date: $("f-date").value,
    language: $("f-language").value,
    quick_provider: $("f-quick-provider").value,
    deep_provider: $("f-deep-provider").value,
    provider: $("f-deep-provider").value, // legacy alias, kept for back-compat readers
    deep_model: getModelValue("f-deep-model", "f-deep-model-custom"),
    quick_model: getModelValue("f-quick-model", "f-quick-model-custom"),
    research_depth: parseInt($("f-depth").value, 10),
    aggressiveness: parseInt($("f-aggressiveness")?.value || "5", 10),
    bias: activeBiasBtn?.dataset?.val || "neutral",
    analysts,
  };
}

function startRun() {
  const params = collectParams();
  if (!params.ticker) { alert("Enter a ticker symbol."); return; }
  if (!params.analysts.length) { alert("Select at least one analyst."); return; }

  currentReports = {};
  lastRunMeta = { ticker: params.ticker, trade_date: params.trade_date };
  $("progress").innerHTML = "";
  resetReasoning();
  hideChartAndQa();
  refreshReportTabs();
  $("decision-panel").hidden = true;

  setStatus("running", "connecting...");
  $("btn-run").disabled = true;
  $("btn-stop").disabled = false;
  startElapsedTimer();

  params.analysts.forEach((a) => setAgentState(a, "pending"));
  setAgentState("research_debate", "pending");
  setAgentState("trader", "pending");
  setAgentState("risk_debate", "pending");
  setAgentState("portfolio_manager", "pending");

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/analyze`);
  runningSocket = ws;

  ws.addEventListener("open", () => { ws.send(JSON.stringify(params)); });
  ws.addEventListener("message", (ev) => { handleFrame(JSON.parse(ev.data)); });
  ws.addEventListener("close", () => {
    runningSocket = null;
    $("btn-run").disabled = false;
    $("btn-stop").disabled = true;
    stopElapsedTimer();
    loadHistory();
  });
  ws.addEventListener("error", () => { setStatus("error", "connection error"); });
}

function stopRun() { if (runningSocket) runningSocket.close(); }

// One frame from the /api/analyze socket. Frame shapes are defined by
// web/runner.py (and "started" by web/main.py) — keep this switch in sync.
function handleFrame(frame) {
  switch (frame.type) {
    case "started":
      activeHistoryId = frame.analysis_id;
      setStatus("running", `running #${frame.analysis_id}`);
      window.dispatchEvent(new CustomEvent("analysis-started", { detail: frame.analysis_id }));
      break;
    case "status":
      setStatus("running", frame.message);
      if (frame.analysts) frame.analysts.forEach((a) => setAgentState(a, "pending"));
      if (frame.agent) setAgentState(frame.agent, "in_progress");
      break;
    case "report_update":
      // Agent completion is inferred from WHICH report key arrived — there is no
      // separate "agent done" frame.
      Object.entries(frame.reports).forEach(([k, v]) => {
        currentReports[k] = v;
        const analystKey = Object.entries(ANALYST_REPORT_MAP).find(([, rk]) => rk === k)?.[0];
        if (analystKey) setAgentState(analystKey, "completed");
        if (k === "investment_plan") setAgentState("research_debate", "completed");
        if (k === "trader_investment_plan") setAgentState("trader", "completed");
        if (k === "final_trade_decision") {
          setAgentState("risk_debate", "completed");
          setAgentState("portfolio_manager", "completed");
          activeReportKey = "final_trade_decision";
        }
      });
      refreshReportTabs();
      if (!activeReportKey) activeReportKey = Object.keys(currentReports)[0];
      renderActiveReport();
      break;
    case "messages":
      // The tool-calls feed was removed; tool calls now surface only inside the
      // Live Reasoning timeline via appendReasoningMessage.
      frame.messages.forEach(appendReasoningMessage);
      break;
    case "token":
      appendReasoningToken(frame.node, frame.text, frame.channel);
      break;
    case "debate":
      if (frame.scope === "investment") setAgentState("research_debate", "in_progress");
      if (frame.scope === "risk") setAgentState("risk_debate", "in_progress");
      break;
    case "done":
      showDecision(frame.signal, frame.final_decision, `analysis #${frame.analysis_id}`);
      setStatus("done", "complete");
      // Reveal the per-analysis Q&A + chart now that the run is persisted.
      if (frame.analysis_id) {
        const a = {
          id: frame.analysis_id,
          ticker: (lastRunMeta && lastRunMeta.ticker) || "",
          trade_date: (lastRunMeta && lastRunMeta.trade_date) || "",
        };
        activeHistoryId = frame.analysis_id;
        showCompanyHeader(a.ticker);
        setupQaForAnalysis(a);
        loadChartForAnalysis(a);
      }
      break;
    case "error":
      // The status line is the error surface now that the messages feed is gone.
      setStatus("error", frame.message);
      break;
  }
}

// ===== Agent progress grid =====

function setAgentState(name, state) {
  const grid = $("progress");
  let row = grid.querySelector(`[data-name="${name}"]`);
  if (!row) {
    row = document.createElement("div");
    row.className = "row";
    row.dataset.name = name;
    row.innerHTML = `<span class="name">${prettyAgentName(name)}</span><span class="state ${state}">${state}</span>`;
    grid.appendChild(row);
    return;
  }
  const stateEl = row.querySelector(".state");
  stateEl.className = `state ${state}`;
  stateEl.textContent = state;
}

function prettyAgentName(key) {
  return {
    market: "Market Analyst",
    social: "Sentiment Analyst",
    news: "News Analyst",
    fundamentals: "Fundamentals Analyst",
    research_debate: "Research Team",
    trader: "Trader",
    risk_debate: "Risk Team",
    portfolio_manager: "Portfolio Manager",
  }[key] || key;
}

// ===== Report tabs =====

function buildReportTabs() {
  const tabs = $("report-tabs");
  tabs.innerHTML = "";
  REPORT_KEYS.forEach(([k, label]) => {
    const b = document.createElement("button");
    b.className = "tab";
    b.dataset.key = k;
    b.textContent = label;
    b.addEventListener("click", () => {
      activeReportKey = k;
      refreshReportTabs();
      renderActiveReport();
    });
    tabs.appendChild(b);
  });
}

function refreshReportTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    const k = tab.dataset.key;
    tab.classList.toggle("has-content", !!currentReports[k]);
    tab.classList.toggle("active", k === activeReportKey);
  });
}

function renderActiveReport() {
  const body = $("report-body");
  if (!activeReportKey || !currentReports[activeReportKey]) {
    body.innerHTML = `<p class="dim">Reports stream in here as agents complete.</p>`;
    return;
  }
  const md = currentReports[activeReportKey];
  body.innerHTML = renderMarkdown(md);
}

// Synthesize a readable transcript from a saved LangGraph debate-state object:
// prefer the combined `history` field, else concatenate the per-role histories,
// then append the judge's decision. Returns "" when there is nothing to show.
function debateToMarkdown(state) {
  if (!state || typeof state !== "object") return "";
  const parts = [];
  const history = (state.history || "").trim();
  if (history) {
    parts.push(history);
  } else {
    ["bull_history", "bear_history", "aggressive_history", "conservative_history", "neutral_history"].forEach((k) => {
      const v = (state[k] || "").trim();
      if (v) parts.push(v);
    });
  }
  const judge = (state.judge_decision || "").trim();
  if (judge) parts.push("---\n\n### Judge Decision\n\n" + judge);
  return parts.filter(Boolean).join("\n\n").trim();
}

// ===== Live reasoning timeline (train of thought) =====

function prettyNodeName(node) {
  if (!node) return "agent";
  return String(node)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function resetReasoning() {
  reasoningNodes = {};
  const log = $("reasoning");
  if (log) log.innerHTML = "";
  const wrap = $("reasoning-wrap");
  if (wrap) wrap.hidden = true;
}

function ensureReasoningBlock(node) {
  const key = node || "agent";
  if (reasoningNodes[key]) return reasoningNodes[key];
  const log = $("reasoning");
  const wrap = $("reasoning-wrap");
  if (wrap) wrap.hidden = false;
  const block = document.createElement("div");
  block.className = "reasoning-block";
  const head = document.createElement("div");
  head.className = "reasoning-node";
  head.textContent = prettyNodeName(key);
  const body = document.createElement("div");
  body.className = "reasoning-text";
  block.appendChild(head);
  block.appendChild(body);
  log.appendChild(block);
  const entry = { el: body, buffer: "" };
  reasoningNodes[key] = entry;
  return entry;
}

function scrollReasoning() {
  const log = $("reasoning");
  if (log) log.scrollTop = log.scrollHeight;
}

function appendReasoningToken(node, text, channel) {
  if (!text) return;
  const entry = ensureReasoningBlock(node);
  entry.buffer += text;
  // Cap per-node buffer so a very long run doesn't bloat the DOM.
  if (entry.buffer.length > 20000) entry.buffer = entry.buffer.slice(-20000);
  entry.el.textContent = entry.buffer;
  entry.el.classList.toggle("reasoning-thinking", channel === "reasoning");
  scrollReasoning();
}

function appendReasoningMessage(m) {
  // Fold tool calls into the timeline as a discrete line.
  if (!m || !m.tool_calls || !m.tool_calls.length) return;
  const log = $("reasoning");
  const wrap = $("reasoning-wrap");
  if (wrap) wrap.hidden = false;
  const line = document.createElement("div");
  line.className = "reasoning-tool";
  line.textContent =
    "↳ " +
    m.tool_calls
      .map((t) => `${t.name}(${typeof t.args === "string" ? t.args : JSON.stringify(t.args || {})})`)
      .join(", ");
  log.appendChild(line);
  scrollReasoning();
}

// ===== Decision panel + status bar =====

function showDecision(signal, decision, meta) {
  const panel = $("decision-panel");
  const sig = (signal || "—").toString().toUpperCase().trim();
  const badge = $("decision-signal");
  badge.className = `badge ${["BUY", "SELL", "HOLD"].includes(sig) ? sig : ""}`;
  badge.textContent = sig;
  $("decision-meta").textContent = meta || "";
  panel.hidden = false;
}

function setStatus(state, text) {
  const el = $("stat-status");
  el.className = state;
  el.textContent = text;
}

function startElapsedTimer() {
  runStart = Date.now();
  const tick = () => {
    const s = Math.floor((Date.now() - runStart) / 1000);
    $("stat-elapsed").textContent = `elapsed: ${s}s`;
  };
  tick();
  elapsedTimer = setInterval(tick, 1000);
}

function stopElapsedTimer() {
  if (elapsedTimer) clearInterval(elapsedTimer);
  elapsedTimer = null;
}

// ===== Technical chart + Q&A panels (per-analysis) =====

let chartPrice = null;
let chartRsi = null;
let chartMacd = null;
let chartResizeHandler = null;
let qaState = { analysisId: null, history: [], inFlight: false };

function hideChartAndQa() {
  $("chart-panel").hidden = true;
  $("qa-panel").hidden = true;
  destroyCharts();
  qaState = { analysisId: null, history: [], inFlight: false };
}

function destroyCharts() {
  [chartPrice, chartRsi, chartMacd].forEach((c) => {
    if (c) {
      try { c.remove(); } catch (e) {}
    }
  });
  chartPrice = chartRsi = chartMacd = null;
  ["chart-price", "chart-rsi", "chart-macd"].forEach((id) => {
    const el = $(id);
    if (el) el.innerHTML = "";
  });
  if (chartResizeHandler) {
    window.removeEventListener("resize", chartResizeHandler);
    chartResizeHandler = null;
  }
}

async function loadChartForAnalysis(a) {
  const panel = $("chart-panel");
  const meta = $("chart-meta");
  panel.hidden = false;
  meta.textContent = `${a.ticker} • ${a.trade_date} • loading point-in-time chart…`;
  destroyCharts();

  let data;
  try {
    const resp = await fetch(`/api/analyses/${a.id}/chart-data`);
    if (!resp.ok) {
      const err = await resp.text();
      meta.textContent = `${a.ticker} • chart unavailable: ${err.slice(0, 200)}`;
      return;
    }
    data = await resp.json();
  } catch (e) {
    meta.textContent = `${a.ticker} • chart unavailable: ${e}`;
    return;
  }

  if (!data.candles || !data.candles.length) {
    meta.textContent = `${a.ticker} • no price data in window`;
    return;
  }

  if (!window.LightweightCharts) {
    meta.textContent = `${a.ticker} • chart library failed to load`;
    return;
  }

  meta.textContent = `${data.ticker} • ${data.trade_date} • point-in-time, ${data.lookback_days}d`;
  renderCharts(data);
}

function renderCharts(data) {
  const LC = window.LightweightCharts;
  const opts = {
    layout: { background: { color: "#06090d" }, textColor: "#6b7d8f" },
    grid: {
      vertLines: { color: "#0f1820" },
      horzLines: { color: "#0f1820" },
    },
    rightPriceScale: { borderColor: "#1d2a36" },
    timeScale: { borderColor: "#1d2a36", timeVisible: false, secondsVisible: false },
    crosshair: { mode: 0 },
  };

  // Price pane
  const priceEl = $("chart-price");
  chartPrice = LC.createChart(priceEl, { ...opts, width: priceEl.clientWidth });
  const candle = chartPrice.addCandlestickSeries({
    upColor: "#2ecc71",
    downColor: "#ff7c7c",
    borderUpColor: "#2ecc71",
    borderDownColor: "#ff7c7c",
    wickUpColor: "#2ecc71",
    wickDownColor: "#ff7c7c",
  });
  candle.setData(data.candles);

  const ind = data.indicators || {};
  const addLine = (chart, series, color, width = 1) => {
    if (!series || !series.length) return null;
    const s = chart.addLineSeries({ color, lineWidth: width, priceLineVisible: false, lastValueVisible: false });
    s.setData(series);
    return s;
  };
  addLine(chartPrice, ind.sma_50,  "#2ecc71", 1);
  addLine(chartPrice, ind.sma_200, "#f4c95d", 1);
  addLine(chartPrice, ind.ema_10,  "#6cd5e6", 1);
  addLine(chartPrice, ind.boll_ub, "#d57bff", 1);
  addLine(chartPrice, ind.boll_lb, "#d57bff", 1);

  // RSI pane
  const rsiEl = $("chart-rsi");
  chartRsi = LC.createChart(rsiEl, { ...opts, width: rsiEl.clientWidth });
  const rsiLine = addLine(chartRsi, ind.rsi_14, "#6cd5e6", 1);
  if (rsiLine) {
    rsiLine.createPriceLine({ price: 70, color: "#ff7c7c", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "70" });
    rsiLine.createPriceLine({ price: 30, color: "#2ecc71", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "30" });
  }

  // MACD pane
  const macdEl = $("chart-macd");
  chartMacd = LC.createChart(macdEl, { ...opts, width: macdEl.clientWidth });
  addLine(chartMacd, ind.macd,  "#6cd5e6", 1);
  addLine(chartMacd, ind.macds, "#f4c95d", 1);
  if (ind.macdh && ind.macdh.length) {
    const hist = chartMacd.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
    hist.setData(ind.macdh.map((p) => ({
      time: p.time,
      value: p.value,
      color: p.value >= 0 ? "#2ecc7155" : "#ff7c7c55",
    })));
  }

  // Sync the three time scales
  const charts = [chartPrice, chartRsi, chartMacd];
  charts.forEach((src) => {
    src.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range) return;
      charts.forEach((tgt) => {
        if (tgt !== src) {
          try { tgt.timeScale().setVisibleLogicalRange(range); } catch (e) {}
        }
      });
    });
  });

  // Resize handling
  chartResizeHandler = () => {
    if (chartPrice) chartPrice.applyOptions({ width: priceEl.clientWidth });
    if (chartRsi)   chartRsi.applyOptions({ width: rsiEl.clientWidth });
    if (chartMacd)  chartMacd.applyOptions({ width: macdEl.clientWidth });
  };
  window.addEventListener("resize", chartResizeHandler);
}

// ===== Q&A panel =====

function setupQaForAnalysis(a) {
  qaState = { analysisId: a.id, history: [], inFlight: false };
  const panel = $("qa-panel");
  const thread = $("qa-thread");
  const form = $("qa-form");
  const input = $("qa-input");
  panel.hidden = false;
  thread.innerHTML = "";
  input.value = "";
  input.disabled = false;

  // Rewire submit (clone to drop any prior listeners)
  const newForm = form.cloneNode(true);
  form.parentNode.replaceChild(newForm, form);
  const newInput = newForm.querySelector("#qa-input");
  newForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    submitQaQuestion(newInput);
  });
  newInput.focus();
}

function appendQaBubble(role, html) {
  const thread = $("qa-thread");
  const div = document.createElement("div");
  div.className = `qa-bubble ${role}`;
  div.innerHTML = html;
  thread.appendChild(div);
  thread.scrollTop = thread.scrollHeight;
  return div;
}

async function submitQaQuestion(input) {
  if (qaState.inFlight) return;
  const question = input.value.trim();
  if (!question) return;
  if (!qaState.analysisId) return;

  qaState.inFlight = true;
  input.disabled = true;
  appendQaBubble("user", escapeHtml(question));
  const placeholder = appendQaBubble("assistant", '<span class="dim">thinking…</span>');

  const priorHistory = qaState.history.slice();

  try {
    const resp = await fetch(`/api/analyses/${qaState.analysisId}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: priorHistory }),
    });
    if (!resp.ok) {
      const errText = await resp.text();
      placeholder.className = "qa-bubble error";
      placeholder.textContent = `Error ${resp.status}: ${errText.slice(0, 300)}`;
    } else {
      const data = await resp.json();
      const answer = data.answer || "(empty response)";
      placeholder.innerHTML = renderMarkdown(answer);
      qaState.history.push({ role: "user", content: question });
      qaState.history.push({ role: "assistant", content: answer });
      input.value = "";
    }
  } catch (e) {
    placeholder.className = "qa-bubble error";
    placeholder.textContent = `Network error: ${e}`;
  } finally {
    qaState.inFlight = false;
    input.disabled = false;
    input.focus();
  }
}
