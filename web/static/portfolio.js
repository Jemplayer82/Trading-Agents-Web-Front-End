// TradingAgents Web — "Portfolio Scan" tab + the dashboard's main tab strip.
//
// Two DIFFERENT data sources render into this tab — don't conflate them:
//   * Holding cards (renderHoldingCard) are LIVE brokerage positions from
//     GET /api/accounts (Schwab today, normalized by web/brokerages.py).
//   * Scan-result cards (loadPortfolioScan) are HISTORICAL — frozen at scan time.
// Both render into the same #portfolio-tickers grid, so loading a scan replaces
// the live holdings view and selecting an account tab replaces the scan cards.
//
// Endpoints — routed to the PORTFOLIO app (web/portfolio_main.py) by
// web/nginx.conf's /api/portfolio and /api/accounts prefix locations (note
// /api/portfolio-scan(s) is caught by the /api/portfolio string prefix):
//   GET    /api/portfolio-scans          history list
//   GET    /api/portfolio-scans/{id}     scan detail (polled at 5s while running)
//   DELETE /api/portfolio-scans/{id}     delete a scan
//   POST   /api/portfolio-scan           start a scan (idempotent for today)
//   GET    /api/accounts                 live holdings, all brokerage accounts
// GET /api/auth/schwab/status goes to the API app via the generic /api/ block.
//
// Account tabs: /api/accounts returns a synthetic {id: "all"} aggregate first,
// then real accounts with ids namespaced "<provider>:<number>" (e.g.
// "schwab:12345678"). Selection matches by dataset.id — never by label text.
//
// Gotcha: setupTabs() below wires the TOP-LEVEL tab strip for the entire
// dashboard and dispatches the "tab-shown" document event that app.js / spy.js /
// credentials.js all listen to. This file is load-bearing for every tab.
//
// Globals consumed from utils.js (loaded first): $, escapeHtml, fmtTs,
// renderMarkdown, progressBar. All top-level names here share the classic-script
// global scope — don't redeclare utils.js names.

let activePortfolioId = null;
let _accountsData = null;
let _activeAccountId = "all";
let portfolioPollTimer = null;

function stopPortfolioPoll() {
  if (portfolioPollTimer) { clearInterval(portfolioPollTimer); portfolioPollTimer = null; }
}

// ---- number formatters (Portfolio-tab specific: currency / shares / percent) ----
function fmt$(v) {
  return "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtAbs$(v) {
  return "$" + Math.abs(Number(v)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtShares(v) {
  return Number(v).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 4 });
}
function fmtPct(v) {
  return (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%";
}
function fmtExpDate(iso) {
  // "2026-01-17" -> "01/17/26" (string split — avoids Date() UTC off-by-one)
  const [y, m, d] = iso.split("-");
  return `${m}/${d}/${y.slice(2)}`;
}

// The "[ Progress ]" panel shown while a portfolio scan runs. Built in one place so
// the initial render and the 5s poll re-render can't drift apart. `progressBar` is
// the shared bar primitive from utils.js.
function portfolioProgressHtml(scan) {
  const sc = scan.scanned_count || 0;
  const st = scan.scan_total || 0;
  const tickerLine = scan.current_ticker
    ? `<div style="margin-bottom:4px;">Analyzing: <strong>${escapeHtml(scan.current_ticker)}</strong></div>`
    : "";
  return (
    '<div class="panel">' +
      '<div class="panel-title">[ Progress ]</div>' +
      `<div style="margin-bottom:4px;">${sc}/${st} tickers analyzed</div>` +
      tickerLine +
      progressBar(sc, st) +
    "</div>"
  );
}

// ---- Scan history + detail (HISTORICAL scan results) ----

async function loadPortfolioHistory() {
  const ul = $("portfolio-history");
  if (!ul) return;
  ul.innerHTML = '<li class="dim empty">loading…</li>';
  try {
    const r = await fetch("/api/portfolio-scans");
    const { scans } = await r.json();
    ul.innerHTML = "";
    if (!scans.length) {
      ul.innerHTML = '<li class="dim empty">(no scans yet)</li>';
      return;
    }
    scans.forEach((s) => {
      const counts = s.signal_counts || {};
      const sigStr = `${counts.BUY || 0} · ${counts.HOLD || 0} · ${counts.SELL || 0}`;
      const li = document.createElement("li");
      li.dataset.id = s.id;
      if (String(s.id) === String(activePortfolioId)) li.classList.add("active");
      const statusBadge = s.status === "completed" ? "BUY" : (s.status === "running" ? "HOLD" : "SELL");
      li.innerHTML = `
        <span class="h-main">
          <span class="h-top">
            <span class="h-tk">#${s.id} · ${escapeHtml(s.trade_date)}</span>
            <span class="h-sig ${statusBadge}">${(s.status || "—").toUpperCase()}</span>
          </span>
          <span class="h-ts">${fmtTs(s.created_at)} · ${s.num_tickers || 0} tickers</span>
          <span class="h-ts" style="font-size:10px;">BUY · HOLD · SELL = ${sigStr}</span>
        </span>
        <button class="h-del" title="Delete" aria-label="Delete">×</button>
      `;
      li.querySelector(".h-main").addEventListener("click", () => loadPortfolioScan(s.id));
      li.querySelector(".h-del").addEventListener("click", (ev) => {
        ev.stopPropagation();
        deletePortfolioScan(s.id);
      });
      ul.appendChild(li);
    });
  } catch (e) {
    ul.innerHTML = `<li class="empty" style="color: var(--accent-red);">${escapeHtml(e)}</li>`;
  }
}

async function loadPortfolioScan(id) {
  stopPortfolioPoll();
  activePortfolioId = id;
  document.querySelectorAll("#portfolio-history li").forEach((li) =>
    li.classList.toggle("active", String(li.dataset.id) === String(id))
  );
  const meta = $("portfolio-meta");
  const brief = $("portfolio-briefing");
  const grid = $("portfolio-tickers");
  brief.innerHTML = '<p class="dim">loading…</p>';
  grid.innerHTML = "";
  try {
    const r = await fetch(`/api/portfolio-scans/${id}`);
    if (!r.ok) {
      brief.innerHTML = `<p style="color: var(--accent-red);">Not found (HTTP ${r.status})</p>`;
      return;
    }
    const scan = await r.json();
    const counts = scan.signal_counts || {};
    meta.innerHTML = `
      Scan <strong>#${scan.id}</strong> · ${escapeHtml(scan.trade_date)} · ${fmtTs(scan.created_at)}
      · status: <strong>${scan.status}</strong> · ${scan.num_tickers || 0} tickers
      · ${counts.BUY || 0} BUY · ${counts.HOLD || 0} HOLD · ${counts.SELL || 0} SELL
      ${scan.newsletter_sent_at ? `· newsletter sent ${fmtTs(scan.newsletter_sent_at)}` : ""}
    `;

    // Progress panel — shown only while scan is running; disappears once it stops.
    if (scan.status === "running") {
      brief.innerHTML = portfolioProgressHtml(scan);

      // Poll every 5s while the scan is running; stop when status leaves "running".
      portfolioPollTimer = setInterval(async () => {
        // Guard: if the user switched to a different scan, cancel this stale timer.
        if (String(activePortfolioId) !== String(id)) { stopPortfolioPoll(); return; }
        const pr = await fetch(`/api/portfolio-scans/${id}`);
        if (!pr.ok) { stopPortfolioPoll(); return; }
        const updated = await pr.json();
        if (updated.status !== "running") {
          stopPortfolioPoll();
          loadPortfolioScan(id);
          return;
        }
        // Re-render just the progress panel in-place (avoid full re-render cost).
        brief.innerHTML = portfolioProgressHtml(updated);
      }, 5000);
    } else if (scan.aggregator_report) {
      brief.innerHTML = renderMarkdown(scan.aggregator_report);
    } else if (scan.error) {
      brief.innerHTML = `<p style="color: var(--accent-red);">${escapeHtml(scan.error)}</p>`;
    } else {
      brief.innerHTML = '<p class="dim">No briefing yet.</p>';
    }
    // Scan-result cards: HISTORICAL per-ticker outcomes from this scan (shares /
    // value as of scan time) — not live data. Rendered into the same grid the
    // live holding cards use (see header).
    (scan.tickers || []).forEach((t) => {
      const card = document.createElement("div");
      card.className = "pcard";
      const sig = (t.signal || "UNKNOWN").toUpperCase();
      card.innerHTML = `
        <div class="pcard-row">
          <span class="pcard-tk">${escapeHtml(t.ticker)}</span>
          <span class="badge ${sig}">${sig}</span>
        </div>
        <div class="pcard-divider" style="margin-top:6px;"></div>
        <div class="pcard-metrics">
          <div>
            <div class="pcard-metric-label">Shares</div>
            <div class="pcard-metric-val">${(t.quantity || 0).toFixed(0)}</div>
          </div>
          <div class="pcard-metrics-right">
            <div class="pcard-metric-label">Current worth</div>
            <div class="pcard-metric-val">$${(t.market_value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
          </div>
        </div>
        ${t.error ? `<div class="pcard-err">scan failed: ${escapeHtml(t.error)}</div>` : ""}
        ${t.analysis_id ? `<div class="pcard-link"><a href="#" data-analysis="${t.analysis_id}">Open full analysis →</a></div>` : ""}
      `;
      const link = card.querySelector("[data-analysis]");
      if (link) {
        link.addEventListener("click", (ev) => {
          ev.preventDefault();
          // switch to analyze tab and load the analysis
          window.dispatchEvent(new CustomEvent("load-analysis", { detail: parseInt(link.dataset.analysis, 10) }));
          document.querySelector('.main-tab[data-tab="analyze"]')?.click();
        });
      }
      grid.appendChild(card);
    });
  } catch (e) {
    brief.innerHTML = `<p style="color: var(--accent-red);">${escapeHtml(e)}</p>`;
  }
}

async function deletePortfolioScan(id) {
  if (!confirm(`Delete portfolio scan #${id}?`)) return;
  const r = await fetch(`/api/portfolio-scans/${id}`, { method: "DELETE" });
  if (!r.ok) {
    alert("Delete failed.");
    return;
  }
  if (String(activePortfolioId) === String(id)) {
    stopPortfolioPoll();
    activePortfolioId = null;
    $("portfolio-briefing").innerHTML = '<p class="dim">Select a scan from the sidebar.</p>';
    $("portfolio-tickers").innerHTML = "";
    $("portfolio-meta").textContent = "";
  }
  loadPortfolioHistory();
}

// Run a Schwab portfolio scan now (moved from the old Schwab tab).
async function runScanNow() {
  const btn = $("btn-scan-now");
  const out = $("scan-now-result");
  if (btn) btn.disabled = true;
  if (out) out.textContent = "starting…";
  const aggressiveness = parseInt($("pf-aggressiveness")?.value || "5", 10);
  const bias = document.querySelector("#pf-bias .bias-btn.active")?.dataset?.val || "neutral";
  try {
    const r = await fetch("/api/portfolio-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ aggressiveness, bias }),
    });
    const data = await r.json();
    if (!r.ok) {
      out.innerHTML = `<span style="color: var(--accent-red);">${data.detail || JSON.stringify(data)}</span>`;
    } else {
      out.innerHTML = `Scan <strong>#${data.scan_id}</strong> ${data.new ? "started" : "already running (idempotent)"}.`;
      loadPortfolioHistory();
    }
  } catch (e) {
    out.innerHTML = `<span style="color: var(--accent-red);">${escapeHtml(e)}</span>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---- Live account holdings ----
// Everything below renders LIVE brokerage data from GET /api/accounts; hides the
// account tabs + totals panel when brokerages are disabled or disconnected.

async function loadAccountHoldings() {
  const tabsEl = $("account-tabs");
  if (!tabsEl) return;
  try {
    const r = await fetch("/api/accounts");
    const data = await r.json();
    if (!data.enabled || !data.connected || !data.accounts) {
      tabsEl.innerHTML = "";
      const panel = $("portfolio-totals-panel");
      if (panel) panel.hidden = true;
      return;
    }
    _accountsData = data.accounts;
    renderAccountTabs(_accountsData);
    selectAccount(_activeAccountId);
  } catch (e) {
    tabsEl.innerHTML = "";
  }
}

function renderAccountTabs(accounts) {
  const tabsEl = $("account-tabs");
  if (!tabsEl) return;
  tabsEl.innerHTML = "";
  accounts.forEach((acct) => {
    const btn = document.createElement("button");
    btn.className = "account-tab" + (acct.id === _activeAccountId ? " active" : "");
    btn.textContent = acct.label;
    btn.dataset.id = acct.id;
    btn.addEventListener("click", () => selectAccount(acct.id));
    tabsEl.appendChild(btn);
  });
}

function selectAccount(id) {
  _activeAccountId = id;
  if (!_accountsData) return;
  const acct = _accountsData.find((a) => a.id === id) || _accountsData[0];
  if (!acct) return;

  document.querySelectorAll(".account-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.id === acct.id);
  });

  renderTotals(acct);

  const grid = $("portfolio-tickers");
  if (!grid) return;
  grid.innerHTML = "";
  if (!acct.positions.length) {
    grid.innerHTML = '<p class="dim">No positions in this account.</p>';
    return;
  }
  acct.positions.forEach((pos) => grid.appendChild(renderHoldingCard(pos)));
}

function renderTotals(acct) {
  const panel = $("portfolio-totals-panel");
  const el = $("portfolio-totals");
  if (!panel || !el) return;

  const gainCls = acct.gain_dollars >= 0 ? "up" : "down";
  const gainSign = acct.gain_dollars >= 0 ? "+" : "-";

  el.innerHTML = `
    <span class="totals-item">
      <span class="dim">Total Value</span>
      <strong>${fmt$(acct.total_value)}</strong>
    </span>
    <span class="totals-item">
      <span class="dim">Gain / Loss</span>
      <strong class="${gainCls}">${gainSign}${fmtAbs$(acct.gain_dollars)}</strong>
    </span>
    <span class="totals-item">
      <strong class="${gainCls}">${fmtPct(acct.gain_percent)}</strong>
    </span>
    <span class="totals-item">
      <span class="dim">Cash</span>
      <strong>${fmt$(acct.cash)}</strong>
    </span>
  `;
  panel.hidden = false;
}

// One LIVE holding card. Option positions (asset_type "OPTION", decoded from the
// OCC symbol by web/brokerages.py) render differently from equities: the
// underlying ticker with the expiration date alongside, an OPTION chip instead
// of a signal badge, and a contracts/strike/put-call subtext line. `shares`
// means contracts for options. Scan signal + analysis link only apply to
// equities (signals are merged onto positions server-side by symbol).
function renderHoldingCard(pos) {
  const card = document.createElement("div");
  card.className = "pcard";
  const isOption = pos.asset_type === "OPTION";
  const sig = (pos.signal || "").toUpperCase();
  const isUp = pos.gain_dollars >= 0;
  const gainCls = isUp ? "up" : "down";
  const gainSign = isUp ? "+" : "−";
  const arrow = isUp ? "▲" : "▼";
  const pct = Math.abs(Number(pos.gain_percent)).toFixed(2) + "%";
  const ticker = isOption ? (pos.underlying || pos.symbol) : pos.symbol;
  const expSpan = isOption && pos.expiration_date
    ? `<span class="pcard-exp">exp ${fmtExpDate(pos.expiration_date)}</span>` : "";
  const chip = isOption
    ? '<span class="badge OPTION">OPTION</span>'
    : (sig ? `<span class="badge ${sig}">${sig}</span>` : "");
  const subtext = isOption
    ? `${fmtShares(pos.shares)} contracts${pos.strike != null && pos.put_call ? ` · $${escapeHtml(pos.strike)} ${escapeHtml(pos.put_call)}` : ""} · ${gainSign}${fmtAbs$(pos.gain_dollars)}`
    : `${fmtShares(pos.shares)} shares · ${gainSign}${fmtAbs$(pos.gain_dollars)}`;

  card.innerHTML = `
    <div class="pcard-row">
      <span><span class="pcard-tk">${escapeHtml(ticker)}</span>${expSpan}</span>
      ${chip}
    </div>
    <div class="pcard-price-row">
      <span class="pcard-price">${fmt$(pos.current_price)}</span>
      <span class="pcard-pct ${gainCls}">${arrow} ${gainSign}${pct}</span>
    </div>
    <div class="pcard-divider"></div>
    <div class="pcard-metrics">
      <div>
        <div class="pcard-metric-label">Purchase price</div>
        <div class="pcard-metric-val">${fmt$(pos.average_price)}</div>
      </div>
      <div class="pcard-metrics-right">
        <div class="pcard-metric-label">Current worth</div>
        <div class="pcard-metric-val">${fmt$(pos.market_value)}</div>
      </div>
    </div>
    <div class="pcard-subtext">${subtext}</div>
    ${!isOption && pos.analysis_id ? `<div class="pcard-link"><a href="#" data-analysis="${pos.analysis_id}">Open full analysis →</a></div>` : ""}
  `;

  const link = card.querySelector("[data-analysis]");
  if (link) {
    link.addEventListener("click", (ev) => {
      ev.preventDefault();
      window.dispatchEvent(new CustomEvent("load-analysis", { detail: parseInt(link.dataset.analysis, 10) }));
      document.querySelector('.main-tab[data-tab="analyze"]')?.click();
    });
  }
  return card;
}

// Schwab (MCP) connection status line at the top of the Portfolio tab.
async function loadSchwabStatusLine() {
  const el = $("schwab-mcp-status");
  if (!el) return;
  try {
    const s = await (await fetch("/api/auth/schwab/status")).json();
    if (s.enabled === false) {
      el.innerHTML = '<span class="badge SELL">SCHWAB OFF</span> Enable Schwab in Settings to run a portfolio scan.';
    } else if (s.connected) {
      el.innerHTML = `<span class="badge BUY">SCHWAB MCP</span> connected · ${s.num_accounts || 0} account(s).`;
    } else {
      el.innerHTML = '<span class="badge SELL">NOT CONNECTED</span> Re-authorize at <a href="https://schwab.txferguson.net/auth" target="_blank" rel="noopener">schwab.txferguson.net/auth</a>.';
    }
  } catch (e) {
    el.textContent = "Schwab status unavailable.";
  }
}

// ---- Tab strip (whole dashboard) ----
// Wires the top-level Run Analysis / Portfolio / S&P 500 / Settings tabs and
// dispatches "tab-shown" (detail = tab name) on every switch. Every module's
// per-tab refresh hangs off that event — this is dashboard-wide plumbing that
// happens to live in this file.
function setupTabs() {
  document.querySelectorAll(".main-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const name = btn.dataset.tab;
      document.querySelectorAll(".main-tab").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-pane").forEach((p) => {
        const show = p.dataset.pane === name;
        p.hidden = !show;
        p.classList.toggle("active", show);
      });
      document.dispatchEvent(new CustomEvent("tab-shown", { detail: name }));
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  $("btn-scan-now")?.addEventListener("click", runScanNow);

  // Aggressiveness slider live label
  const aggSlider = $("pf-aggressiveness");
  if (aggSlider) {
    aggSlider.addEventListener("input", () => {
      const v = $("pf-aggressiveness-val");
      if (v) v.textContent = aggSlider.value;
    });
  }
  // Bias toggle
  document.querySelectorAll("#pf-bias .bias-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#pf-bias .bias-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    });
  });
});

document.addEventListener("tab-shown", (ev) => {
  if (ev.detail === "portfolio") {
    loadPortfolioHistory();
    loadSchwabStatusLine();
    loadAccountHoldings();
  }
});

// Auto-refresh portfolio history every 10s while the tab is visible
setInterval(() => {
  const pane = document.querySelector('[data-pane="portfolio"]');
  if (pane && !pane.hidden) loadPortfolioHistory();
}, 10000);
