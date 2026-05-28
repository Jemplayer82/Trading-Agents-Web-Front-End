// TradingAgents Web — dashboard logic

const REPORT_KEYS = [
  ["market_report", "Market"],
  ["sentiment_report", "Sentiment"],
  ["news_report", "News"],
  ["fundamentals_report", "Fundamentals"],
  ["investment_plan", "Research Plan"],
  ["trader_investment_plan", "Trader Plan"],
  ["final_trade_decision", "Final Decision"],
];

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

const $ = (id) => document.getElementById(id);

document.addEventListener("DOMContentLoaded", async () => {
  await loadProviders();
  await loadPreferences();
  await loadHistory();
  buildReportTabs();
  $("btn-run").addEventListener("click", startRun);
  $("btn-stop").addEventListener("click", stopRun);
  $("f-provider").addEventListener("change", onProviderChange);
});

async function loadProviders() {
  const resp = await fetch("/api/providers");
  providersData = await resp.json();

  const provSel = $("f-provider");
  provSel.innerHTML = "";
  providersData.providers.forEach((p) => {
    const opt = document.createElement("option");
    opt.value = p.key;
    opt.textContent = p.label;
    provSel.appendChild(opt);
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
    lbl.innerHTML = `<input type="checkbox" id="${id}" value="${a.key}" /> ${a.label}`;
    analystBox.appendChild(lbl);
  });
}

function findProvider(key) {
  return providersData.providers.find((p) => p.key === key);
}

function onProviderChange() {
  const key = $("f-provider").value;
  const prov = findProvider(key);
  fillModelDatalist("deep-models", prov?.models?.deep || []);
  fillModelDatalist("quick-models", prov?.models?.quick || []);
}

function fillModelDatalist(id, options) {
  const dl = $(id);
  dl.innerHTML = "";
  options.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.value;
    opt.label = m.label;
    dl.appendChild(opt);
  });
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
  $("f-provider").value = prefs.provider || "ollama";
  onProviderChange();
  if (prefs.deep_model) $("f-deep-model").value = prefs.deep_model;
  if (prefs.quick_model) $("f-quick-model").value = prefs.quick_model;
  $("f-depth").value = prefs.research_depth || 1;

  const wanted = new Set(prefs.analysts || ["market", "social", "news", "fundamentals"]);
  document.querySelectorAll("#f-analysts input[type=checkbox]").forEach((cb) => {
    cb.checked = wanted.has(cb.value);
  });
}

function formatTimestamp(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

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
        <span class="h-ts">${formatTimestamp(a.created_at)}</span>
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
  currentReports = {};
  REPORT_KEYS.forEach(([k]) => {
    if (a[k]) currentReports[k] = a[k];
  });
  refreshReportTabs();
  if (a.processed_signal || a.final_decision) {
    showDecision(a.processed_signal, a.final_decision, `${a.ticker} • ${a.trade_date} • ${formatTimestamp(a.created_at)}`);
  }
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
    setStatus("idle", "idle");
  }
  await loadHistory();
}

function collectParams() {
  const analysts = [];
  document.querySelectorAll("#f-analysts input[type=checkbox]:checked").forEach((cb) =>
    analysts.push(cb.value)
  );
  return {
    ticker: $("f-ticker").value.trim(),
    trade_date: $("f-date").value,
    language: $("f-language").value,
    provider: $("f-provider").value,
    deep_model: $("f-deep-model").value,
    quick_model: $("f-quick-model").value,
    research_depth: parseInt($("f-depth").value, 10),
    analysts,
  };
}

function startRun() {
  const params = collectParams();
  if (!params.ticker) { alert("Enter a ticker symbol."); return; }
  if (!params.analysts.length) { alert("Select at least one analyst."); return; }

  currentReports = {};
  $("messages").innerHTML = "";
  $("progress").innerHTML = "";
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

function handleFrame(frame) {
  switch (frame.type) {
    case "started":
      activeHistoryId = frame.analysis_id;
      setStatus("running", `running #${frame.analysis_id}`);
      break;
    case "status":
      setStatus("running", frame.message);
      if (frame.analysts) frame.analysts.forEach((a) => setAgentState(a, "pending"));
      break;
    case "report_update":
      Object.entries(frame.reports).forEach(([k, v]) => {
        currentReports[k] = v;
        const analystKey = Object.entries(ANALYST_REPORT_MAP).find(([, rk]) => rk === k)?.[0];
        if (analystKey) setAgentState(analystKey, "completed");
        if (k === "investment_plan") setAgentState("research_debate", "completed");
        if (k === "trader_investment_plan") setAgentState("trader", "completed");
        if (k === "final_trade_decision") {
          setAgentState("risk_debate", "completed");
          setAgentState("portfolio_manager", "completed");
        }
      });
      refreshReportTabs();
      if (!activeReportKey) activeReportKey = Object.keys(currentReports)[0];
      renderActiveReport();
      break;
    case "messages":
      frame.messages.forEach(appendMessage);
      break;
    case "debate":
      if (frame.scope === "investment") setAgentState("research_debate", "in_progress");
      if (frame.scope === "risk") setAgentState("risk_debate", "in_progress");
      break;
    case "done":
      showDecision(frame.signal, frame.final_decision, `analysis #${frame.analysis_id}`);
      setStatus("done", "complete");
      break;
    case "error":
      setStatus("error", frame.message);
      appendMessage({ type: "error", text: frame.message });
      break;
  }
}

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
  body.innerHTML = window.marked ? window.marked.parse(md) : `<pre>${escapeHtml(md)}</pre>`;
}

function appendMessage(m) {
  const ul = $("messages");
  const li = document.createElement("li");
  const who = (m.type || "msg").toLowerCase();
  const text = m.text || "";
  let toolArgs = "";
  if (m.tool_calls && m.tool_calls.length) {
    toolArgs = `<span class="tool-args">→ ${m.tool_calls
      .map((t) => `${t.name}(${typeof t.args === "string" ? t.args : JSON.stringify(t.args || {})})`)
      .join(", ")}</span>`;
  }
  li.innerHTML = `<span class="who ${who}">${who}</span><span class="what">${escapeHtml(text)}${toolArgs}</span>`;
  ul.insertBefore(li, ul.firstChild);
  while (ul.children.length > 200) ul.removeChild(ul.lastChild);
}

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

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
