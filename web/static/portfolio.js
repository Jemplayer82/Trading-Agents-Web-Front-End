// Portfolio Scan tab — history list, briefing render, per-ticker grid, live holdings

const $$p = (id) => document.getElementById(id);

let activePortfolioId = null;
let _accountsData = null;
let _activeAccountId = "all";

// ---- number formatters ----
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

function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function loadPortfolioHistory() {
  const ul = $$p("portfolio-history");
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
    ul.innerHTML = `<li class="empty" style="color: var(--accent-red);">${e}</li>`;
  }
}

async function loadPortfolioScan(id) {
  activePortfolioId = id;
  document.querySelectorAll("#portfolio-history li").forEach((li) =>
    li.classList.toggle("active", String(li.dataset.id) === String(id))
  );
  const meta = $$p("portfolio-meta");
  const brief = $$p("portfolio-briefing");
  const grid = $$p("portfolio-tickers");
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
    if (scan.aggregator_report) {
      brief.innerHTML = window.marked ? window.marked.parse(scan.aggregator_report) : `<pre>${escapeHtml(scan.aggregator_report)}</pre>`;
    } else if (scan.error) {
      brief.innerHTML = `<p style="color: var(--accent-red);">${escapeHtml(scan.error)}</p>`;
    } else {
      brief.innerHTML = '<p class="dim">No briefing yet — scan still running.</p>';
    }
    (scan.tickers || []).forEach((t) => {
      const card = document.createElement("div");
      card.className = "pcard";
      const sig = (t.signal || "UNKNOWN").toUpperCase();
      card.innerHTML = `
        <div class="pcard-row">
          <span class="pcard-tk">${escapeHtml(t.ticker)}</span>
          <span class="badge ${sig}">${sig}</span>
        </div>
        <div class="pcard-meta">${(t.quantity || 0).toFixed(0)} sh · $${(t.market_value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}</div>
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
    brief.innerHTML = `<p style="color: var(--accent-red);">${e}</p>`;
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
    activePortfolioId = null;
    $$p("portfolio-briefing").innerHTML = '<p class="dim">Select a scan from the sidebar.</p>';
    $$p("portfolio-tickers").innerHTML = "";
    $$p("portfolio-meta").textContent = "";
  }
  loadPortfolioHistory();
}

// Run a Schwab portfolio scan now (moved from the old Schwab tab).
async function runScanNow() {
  const btn = $$p("btn-scan-now");
  const out = $$p("scan-now-result");
  if (btn) btn.disabled = true;
  if (out) out.textContent = "starting…";
  try {
    const r = await fetch("/api/portfolio-scan", { method: "POST" });
    const data = await r.json();
    if (!r.ok) {
      out.innerHTML = `<span style="color: var(--accent-red);">${data.detail || JSON.stringify(data)}</span>`;
    } else {
      out.innerHTML = `Scan <strong>#${data.scan_id}</strong> ${data.new ? "started" : "already running (idempotent)"}.`;
      loadPortfolioHistory();
    }
  } catch (e) {
    out.innerHTML = `<span style="color: var(--accent-red);">${e}</span>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---- Live account holdings ----

async function loadAccountHoldings() {
  const tabsEl = $$p("account-tabs");
  if (!tabsEl) return;
  try {
    const r = await fetch("/api/accounts");
    const data = await r.json();
    if (!data.enabled || !data.connected || !data.accounts) {
      tabsEl.innerHTML = "";
      const panel = $$p("portfolio-totals-panel");
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
  const tabsEl = $$p("account-tabs");
  if (!tabsEl) return;
  tabsEl.innerHTML = "";
  accounts.forEach((acct) => {
    const btn = document.createElement("button");
    btn.className = "account-tab" + (acct.id === _activeAccountId ? " active" : "");
    btn.textContent = acct.label;
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
    btn.classList.toggle("active", btn.textContent === acct.label);
  });

  renderTotals(acct);

  const grid = $$p("portfolio-tickers");
  if (!grid) return;
  grid.innerHTML = "";
  if (!acct.positions.length) {
    grid.innerHTML = '<p class="dim">No positions in this account.</p>';
    return;
  }
  acct.positions.forEach((pos) => grid.appendChild(renderHoldingCard(pos)));
}

function renderTotals(acct) {
  const panel = $$p("portfolio-totals-panel");
  const el = $$p("portfolio-totals");
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

function renderHoldingCard(pos) {
  const card = document.createElement("div");
  card.className = "pcard";
  const sig = (pos.signal || "").toUpperCase();
  const gainCls = pos.gain_dollars >= 0 ? "up" : "down";
  const gainSign = pos.gain_dollars >= 0 ? "+" : "-";

  card.innerHTML = `
    <div class="pcard-row">
      <span class="pcard-tk">${escapeHtml(pos.symbol)}</span>
      ${sig ? `<span class="badge ${sig}">${sig}</span>` : ""}
    </div>
    <div class="pcard-line"><span>Shares</span><span>${fmtShares(pos.shares)}</span></div>
    <div class="pcard-line"><span>Buy</span><span>${fmt$(pos.average_price)}</span></div>
    <div class="pcard-line"><span>Now</span><span>${fmt$(pos.current_price)}</span></div>
    <div class="pcard-line"><span>Value</span><span>${fmt$(pos.market_value)}</span></div>
    <div class="pcard-gain ${gainCls}">${gainSign}${fmtAbs$(pos.gain_dollars)} (${fmtPct(pos.gain_percent)})</div>
    ${pos.analysis_id ? `<div class="pcard-link"><a href="#" data-analysis="${pos.analysis_id}">Open full analysis →</a></div>` : ""}
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
  const el = $$p("schwab-mcp-status");
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

// Tab switching
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
  $$p("btn-scan-now")?.addEventListener("click", runScanNow);
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
