// S&P 500 Scanner tab — scan history, progress, quick results table, portfolio view

const $$spy = (id) => document.getElementById(id);

let activeSpyId = null;
let spyPollTimer = null;

function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function escHtml(s) {
  if (s == null) return "";
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function fmtReturn(val, basis) {
  basis = basis || 100000;
  const ret = ((val - basis) / basis) * 100;
  const sign = ret >= 0 ? "+" : "";
  const color = ret >= 0 ? "var(--accent-green)" : "var(--accent-red)";
  return "<span style=\"color:" + color + ";font-weight:700;\">" + sign + ret.toFixed(2) + "%</span>";
}

// Colored percent change: green for >= 0, red (with leading -) for < 0.
function pctCell(pct) {
  if (pct == null || isNaN(pct)) return "<span class=\"dim\">—</span>";
  const up = pct >= 0;
  const color = up ? "var(--accent-green)" : "var(--accent-red)";
  const sign = up ? "+" : "";
  return "<span style=\"color:" + color + ";font-weight:700;\">" + sign + pct.toFixed(2) + "%</span>";
}

// Whole shares for a position, with legacy fallback for pre-whole-share scans.
function sharesOf(a) {
  if (a.shares != null) return a.shares;
  const ep = a.entry_price || 0;
  return ep > 0 ? Math.floor((a.dollar_amount || 0) / ep) : 0;
}

async function loadSpyHistory() {
  const ul = $$spy("spy-history");
  if (!ul) return;
  ul.innerHTML = "<li class=\"dim empty\">loading…</li>";
  try {
    const r = await fetch("/api/spy-scans");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    const scans = data.scans || [];
    ul.innerHTML = "";
    if (!scans.length) {
      ul.innerHTML = "<li class=\"dim empty\">(no scans yet)</li>";
      return;
    }
    scans.forEach((s) => {
      const li = document.createElement("li");
      li.dataset.id = s.id;
      if (String(s.id) === String(activeSpyId)) li.classList.add("active");
      const running = s.status && s.status.startsWith("running");
      const stopping = running && s.cancel_requested;
      const statusLabel = stopping ? "STOPPING" : (s.status || "—").toUpperCase();
      const statusClass = stopping ? "HOLD"
        : (s.status === "completed" ? "BUY" : (running ? "HOLD" : "SELL"));
      const basis = s.starting_value || 100000;
      const returnBadge = s.current_value != null
        ? " · " + fmtReturn(s.current_value, basis)
        : "";
      const typeTag = s.previous_scan_id
        ? "<span style=\"font-size:9px;color:var(--accent-magenta);margin-left:4px;\">REBALANCE</span>"
        : "<span style=\"font-size:9px;color:var(--dim);margin-left:4px;\">INITIAL</span>";
      li.innerHTML = (
        "<span class=\"h-main\">" +
          "<span class=\"h-top\">" +
            "<span class=\"h-tk\">#" + s.id + " · " + escHtml(s.trade_date) + typeTag + "</span>" +
            "<span class=\"h-sig " + statusClass + "\">" + statusLabel + "</span>" +
          "</span>" +
          "<span class=\"h-ts\">" + fmtTs(s.created_at) + returnBadge + "</span>" +
        "</span>" +
        "<button class=\"h-del\" title=\"Delete this scan\" aria-label=\"Delete\">×</button>"
      );
      li.querySelector(".h-main").addEventListener("click", () => loadSpyScan(s.id));
      li.querySelector(".h-del").addEventListener("click", (ev) => {
        ev.stopPropagation();
        deleteSpyScan(s.id);
      });
      ul.appendChild(li);
    });

    // Cumulative return: latest scan current_value vs the very first scan's starting_value ($100k)
    const completed = scans.filter((s) => s.status === "completed" && s.current_value != null);
    if (completed.length > 0) {
      const latest = completed[0]; // scans are DESC ordered
      const inception = 100000; // always started with $100k
      const footer = document.createElement("li");
      footer.className = "empty dim";
      footer.style.borderTop = "1px solid var(--panel-border)";
      footer.style.marginTop = "4px";
      footer.style.paddingTop = "6px";
      footer.innerHTML = "Running portfolio: " + fmtReturn(latest.current_value, inception) + " over " + completed.length + " scan(s)";
      ul.appendChild(footer);
    }
  } catch (e) {
    ul.innerHTML = "<li class=\"empty\" style=\"color:var(--accent-red);\">" + e + "</li>";
  }
}

async function loadSpyScan(id) {
  activeSpyId = id;
  document.querySelectorAll("#spy-history li").forEach((li) =>
    li.classList.toggle("active", String(li.dataset.id) === String(id))
  );
  stopSpyPoll();

  const r = await fetch("/api/spy-scans/" + id);
  if (!r.ok) {
    renderSpyScanError("Not found (HTTP " + r.status + ")");
    return;
  }
  const scan = await r.json();
  renderSpyScan(scan);
  updateStopButton(scan);

  if (scan.status && scan.status.startsWith("running")) {
    spyPollTimer = setInterval(async () => {
      const pr = await fetch("/api/spy-scans/" + id);
      if (!pr.ok) { stopSpyPoll(); return; }
      const updated = await pr.json();
      renderSpyScan(updated);
      updateStopButton(updated);
      if (!updated.status || !updated.status.startsWith("running")) stopSpyPoll();
    }, 5000);
  }
}

function stopSpyPoll() {
  if (spyPollTimer) { clearInterval(spyPollTimer); spyPollTimer = null; }
}

// Show the Stop button only while the active scan is running.
function updateStopButton(scan) {
  const btn = $$spy("btn-spy-stop");
  if (!btn) return;
  const running = scan && scan.status && scan.status.startsWith("running");
  btn.hidden = !running;
  if (running && scan.cancel_requested) {
    btn.disabled = true;
    btn.textContent = "Stopping…";
  } else {
    btn.disabled = false;
    btn.textContent = "Stop Scan";
  }
}

async function triggerSpyStop() {
  if (!activeSpyId) return;
  if (!confirm("Stop scan #" + activeSpyId + "?\n\nIn-progress deep dives are full analyses and will finish first (this can take a few minutes). Partial results are kept.")) return;
  const btn = $$spy("btn-spy-stop");
  const status = $$spy("spy-scan-status");
  if (btn) { btn.disabled = true; btn.textContent = "Stopping…"; }
  if (status) status.textContent = "Cancelling scan #" + activeSpyId + "…";
  try {
    const r = await fetch("/api/spy-scans/" + activeSpyId + "/cancel", { method: "POST" });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    if (status) status.textContent = "Cancellation requested — winding down in-progress deep dives…";
  } catch (e) {
    if (status) status.textContent = "Cancel failed: " + e;
    if (btn) { btn.disabled = false; btn.textContent = "Stop Scan"; }
  }
}

async function deleteSpyScan(id) {
  if (!confirm("Delete S&P 500 scan #" + id + "?\n\nThis removes the scan, its quick-scan results, and its portfolio. The deep-dive analyses it created stay in the Run Analysis history.")) return;
  try {
    const r = await fetch("/api/spy-scans/" + id, { method: "DELETE" });
    if (!r.ok) { alert("Delete failed (HTTP " + r.status + ")."); return; }
    if (String(activeSpyId) === String(id)) {
      activeSpyId = null;
      stopSpyPoll();
      const main = $$spy("spy-main");
      if (main) main.innerHTML = "";
      updateStopButton(null);
    }
    await loadSpyHistory();
  } catch (e) {
    alert("Delete failed: " + e);
  }
}

function renderSpyScanError(msg) {
  const main = $$spy("spy-main");
  if (main) main.innerHTML = "<div class=\"panel\"><p style=\"color:var(--accent-red);\">" + escHtml(msg) + "</p></div>";
}

function renderSpyScan(scan) {
  const main = $$spy("spy-main");
  if (!main) return;

  // Map ticker -> deep-dive analysis id (only deep-dived tickers have one).
  const aidByTicker = {};
  (scan.quick_results || []).forEach((r) => {
    if (r.analysis_id) aidByTicker[r.ticker] = r.analysis_id;
  });
  // Render a ticker as a link to its full report when an analysis exists.
  function tickerCell(ticker) {
    const aid = aidByTicker[ticker];
    if (aid) {
      return "<a href=\"#\" class=\"spy-analysis-link\" data-id=\"" + aid +
        "\" title=\"Open full report\" style=\"color:var(--accent-cyan);font-weight:700;\">" +
        escHtml(ticker) + " ↗</a>";
    }
    return "<strong style=\"color:var(--accent-cyan);\">" + escHtml(ticker) + "</strong>";
  }

  // Status banner for terminal/cancel states
  const running = scan.status && scan.status.startsWith("running");
  let bannerHtml = "";
  if (scan.status === "cancelled") {
    bannerHtml = "<div class=\"panel\"><p style=\"color:var(--accent-yellow);\"><strong>Scan #" + scan.id + " was stopped.</strong> Partial results below.</p></div>";
  } else if (scan.cancel_requested && running) {
    bannerHtml = (
      "<div class=\"panel\"><p style=\"color:var(--accent-yellow);\">" +
        "<strong>Stopping scan #" + scan.id + "…</strong> " +
        "In-progress deep dives are finishing (each is a full analysis and can take a few minutes); " +
        "remaining tickers are being skipped. Partial results are kept." +
      "</p></div>"
    );
  }

  // Progress section (only while running)
  let progressHtml = "";
  if (scan.status && scan.status.startsWith("running")) {
    const qt = scan.quick_total || 500;
    const qc = scan.quick_count || 0;
    const dt = scan.deep_total || 50;
    const dc = scan.deep_count || 0;
    const qpct = qt > 0 ? Math.round((qc / qt) * 100) : 0;
    const dpct = dt > 0 ? Math.round((dc / dt) * 100) : 0;
    progressHtml = (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\">[ Progress ]</div>" +
        "<div style=\"margin-bottom:8px;\">" +
          "<div style=\"margin-bottom:4px;\">Quick scan: " + qc + "/" + qt + "</div>" +
          "<div class=\"scan-progress\"><div class=\"scan-progress-bar\" style=\"width:" + qpct + "%\"></div></div>" +
        "</div>" +
        "<div>" +
          "<div style=\"margin-bottom:4px;\">Deep dive: " + dc + "/" + dt + "</div>" +
          "<div class=\"scan-progress\"><div class=\"scan-progress-bar\" style=\"width:" + dpct + "%\"></div></div>" +
        "</div>" +
      "</div>"
    );
  }

  // Performance card (once we have a portfolio)
  let perfHtml = "";
  if (scan.portfolio_json && scan.portfolio_json.length) {
    const basis = scan.starting_value || 100000;
    const cv = scan.current_value;
    const isRebalance = scan.previous_scan_id != null;
    const deployed = scan.portfolio_json
      .filter((a) => a.action !== "EXITED" && sharesOf(a) > 0)
      .reduce((s, a) => s + (a.cost_basis != null ? a.cost_basis : sharesOf(a) * (a.entry_price || 0)), 0);
    const cash = Math.max(0, basis - deployed);
    const cashSpan = "<span>Deployed: <strong>$" + Math.round(deployed).toLocaleString() +
      "</strong> · Cash: <strong>$" + Math.round(cash).toLocaleString() +
      "</strong> (" + (basis > 0 ? (cash / basis * 100).toFixed(0) : "0") + "%)</span>";
    if (cv != null) {
      const ret = ((cv - basis) / basis) * 100;
      const sign = ret >= 0 ? "+" : "";
      const retColor = ret >= 0 ? "var(--accent-green)" : "var(--accent-red)";
      perfHtml = (
        "<div class=\"panel\">" +
          "<div class=\"panel-title\">[ Performance ]</div>" +
          "<div style=\"display:flex;gap:24px;align-items:baseline;flex-wrap:wrap;\">" +
            "<span class=\"dim\" style=\"font-size:11px;\">" + (isRebalance ? "Rebalance" : "Initial") + "</span>" +
            "<span>Basis: <strong>$" + Math.round(basis).toLocaleString() + "</strong></span>" +
            cashSpan +
            "<span>Current: <strong style=\"color:" + retColor + ";\">$" + Math.round(cv).toLocaleString() + "</strong></span>" +
            "<span>Return: <strong style=\"color:" + retColor + ";\">" + sign + ret.toFixed(2) + "% (" + sign + "$" + Math.abs(Math.round(cv - basis)).toLocaleString() + ")</strong></span>" +
            "<span class=\"dim\">Updated: " + fmtTs(scan.last_price_check) + "</span>" +
          "</div>" +
          (scan.rebalance_notes ? "<div style=\"color:var(--accent-yellow);margin-top:8px;white-space:pre-wrap;font-size:12px;\">" + escHtml(scan.rebalance_notes) + "</div>" : "") +
          "<div style=\"margin-top:10px;\">" +
            "<button class=\"ghost\" style=\"font-size:12px;\" onclick=\"refreshSpyPrices(" + scan.id + ")\">Refresh prices</button>" +
          "</div>" +
        "</div>"
      );
    } else {
      perfHtml = (
        "<div class=\"panel\">" +
          "<div class=\"panel-title\">[ Performance ]</div>" +
          "<div style=\"margin-bottom:8px;color:var(--dim);font-size:12px;\">" +
            (isRebalance ? "Rebalance — starting capital: <strong>$" + Math.round(basis).toLocaleString() + "</strong>" : "Initial portfolio — $100,000 basis") +
            " · Deployed $" + Math.round(deployed).toLocaleString() + " · Cash $" + Math.round(cash).toLocaleString() +
          "</div>" +
          "<p class=\"dim\">Prices not yet refreshed. " +
          "<button class=\"ghost\" style=\"font-size:12px;\" onclick=\"refreshSpyPrices(" + scan.id + ")\">Refresh now</button></p>" +
        "</div>"
      );
    }
  }

  // Quick results table
  let tableHtml = "";
  if (scan.quick_results && scan.quick_results.length) {
    const sorted = [...scan.quick_results].sort((a, b) => (b.conviction || 0) - (a.conviction || 0));
    const rows = sorted.map((r) => {
      const sig = (r.signal || "HOLD").toUpperCase();
      const conv = r.conviction || 0;
      const convColor = conv >= 8 ? "var(--accent-green)" : (conv >= 5 ? "var(--accent-yellow)" : "var(--accent-red)");
      return (
        "<tr>" +
          "<td>" + tickerCell(r.ticker) + "</td>" +
          "<td><span class=\"badge " + sig + "\">" + sig + "</span></td>" +
          "<td><span class=\"conviction-badge\" style=\"color:" + convColor + ";font-weight:700;\">" + conv + "/10</span></td>" +
          "<td style=\"color:var(--dim);font-size:11px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;\">" + escHtml(r.reasoning || "") + "</td>" +
        "</tr>"
      );
    }).join("");
    tableHtml = (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\">[ Quick Scan Results — " + sorted.length + " tickers ]</div>" +
        "<div style=\"overflow-x:auto;\">" +
          "<table class=\"spy-table\">" +
            "<thead><tr>" +
              "<th>Ticker</th><th>Signal</th><th>Conviction</th><th>Reasoning</th>" +
            "</tr></thead>" +
            "<tbody>" + rows + "</tbody>" +
          "</table>" +
        "</div>" +
      "</div>"
    );
  }

  // Portfolio allocation table
  let portfolioHtml = "";
  if (scan.portfolio_json && scan.portfolio_json.length) {
    const actionColor = {
      NEW: "var(--accent-cyan)", HOLD: "var(--dim)", ADDED: "var(--accent-green)",
      TRIMMED: "var(--accent-yellow)", EXITED: "var(--accent-red)",
    };
    // Active positions (whole shares) sorted by cost basis, exits at the bottom
    const active = scan.portfolio_json.filter(a => a.action !== "EXITED" && sharesOf(a) > 0);
    const exited = scan.portfolio_json.filter(a => a.action === "EXITED" || sharesOf(a) === 0);
    const costOf = (a) => (a.cost_basis != null ? a.cost_basis : sharesOf(a) * (a.entry_price || 0));
    const valueOf = (a) => (a.current_value != null
      ? a.current_value
      : sharesOf(a) * (a.current_price || a.entry_price || 0));
    const allocs = [
      ...active.sort((a, b) => costOf(b) - costOf(a)),
      ...exited,
    ];
    const deployed = active.reduce((s, a) => s + costOf(a), 0);
    const positionsValue = active.reduce((s, a) => s + valueOf(a), 0);
    const basis = scan.starting_value || 100000;
    const cash = Math.max(0, basis - deployed);
    const curTotal = scan.current_value != null ? scan.current_value : (positionsValue + cash);
    const totalPct = basis > 0 ? ((curTotal - basis) / basis) * 100 : 0;
    const isRebalance = scan.previous_scan_id != null;

    const rows = allocs.map((a) => {
      const sig = (a.signal || "—").toUpperCase();
      const act = (a.action || "NEW").toUpperCase();
      const actCol = actionColor[act] || "var(--dim)";
      if (act === "EXITED") {
        return (
          "<tr style=\"opacity:0.5;\">" +
            "<td>" + tickerCell(a.ticker) + "</td>" +
            "<td><span style=\"font-size:10px;font-weight:700;text-transform:uppercase;color:" + actCol + ";\">EXITED</span></td>" +
            "<td><span class=\"badge " + sig + "\">" + sig + "</span></td>" +
            "<td class=\"dim\">—</td><td class=\"dim\">—</td><td class=\"dim\">—</td><td class=\"dim\">—</td><td class=\"dim\">—</td>" +
            "<td style=\"color:var(--dim);font-size:11px;\">" + escHtml((a.rationale || "").slice(0, 100)) + "</td>" +
          "</tr>"
        );
      }
      const shares = sharesOf(a);
      const buy = a.entry_price || 0;
      const cur = a.current_price;
      const pct = (cur != null && buy > 0) ? ((cur - buy) / buy) * 100 : null;
      const value = valueOf(a);
      return (
        "<tr>" +
          "<td>" + tickerCell(a.ticker) + "</td>" +
          "<td><span style=\"font-size:10px;font-weight:700;text-transform:uppercase;color:" + actCol + ";\">" + act + "</span></td>" +
          "<td><span class=\"badge " + sig + "\">" + sig + "</span></td>" +
          "<td style=\"font-weight:600;\">" + shares + "</td>" +
          "<td>$" + buy.toFixed(2) + "</td>" +
          "<td>" + (cur != null ? "$" + cur.toFixed(2) : "<span class=\"dim\">—</span>") + "</td>" +
          "<td>" + pctCell(pct) + "</td>" +
          "<td style=\"font-weight:600;\">$" + Math.round(value).toLocaleString() + "</td>" +
          "<td style=\"color:var(--dim);font-size:11px;\">" + escHtml((a.rationale || "").slice(0, 90)) + "</td>" +
        "</tr>"
      );
    }).join("");

    const titleLabel = isRebalance
      ? "Rebalanced Portfolio — $" + Math.round(basis).toLocaleString() + " capital"
      : "$100k Paper Portfolio";

    portfolioHtml = (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\">[ " + titleLabel + " — " + active.length + " active positions ]</div>" +
        "<div style=\"overflow-x:auto;\">" +
          "<table class=\"spy-table\">" +
            "<thead><tr><th>Ticker</th><th>Action</th><th>Signal</th><th>Shares</th><th>Buy $</th><th>Cur $</th><th>%</th><th>Value</th><th>Rationale</th></tr></thead>" +
            "<tbody>" + rows + "</tbody>" +
            "<tfoot>" +
              "<tr style=\"border-top:1px solid var(--panel-border);\">" +
                "<td colspan=\"7\">Deployed (cost basis)</td>" +
                "<td>$" + Math.round(deployed).toLocaleString() + "</td><td></td>" +
              "</tr>" +
              "<tr style=\"color:var(--accent-yellow);\">" +
                "<td colspan=\"7\">Cash (uninvested)</td>" +
                "<td>$" + Math.round(cash).toLocaleString() + "</td><td></td>" +
              "</tr>" +
              "<tr style=\"font-weight:700;border-top:1px solid var(--panel-border);\">" +
                "<td colspan=\"6\">CURRENT VALUE</td>" +
                "<td>" + pctCell(totalPct) + "</td>" +
                "<td>$" + Math.round(curTotal).toLocaleString() + "</td><td></td>" +
              "</tr>" +
            "</tfoot>" +
          "</table>" +
        "</div>" +
      "</div>"
    );
  }

  // Allocator report
  let reportHtml = "";
  if (scan.allocator_report) {
    const md = scan.allocator_report;
    reportHtml = (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\">[ Allocator Report ]</div>" +
        "<div class=\"report-body\">" +
          (window.marked ? window.marked.parse(md) : "<pre>" + escHtml(md) + "</pre>") +
        "</div>" +
      "</div>"
    );
  } else if (scan.error) {
    reportHtml = (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\" style=\"color:var(--accent-red);\">[ Scan Failed ]</div>" +
        "<p style=\"color:var(--accent-red);white-space:pre-wrap;\">" + escHtml(scan.error) + "</p>" +
        "<p class=\"dim\" style=\"font-size:12px;margin-top:8px;\">Any partial results above are still available. You can start a new scan from the top of this tab.</p>" +
      "</div>"
    );
  }

  main.innerHTML = bannerHtml + progressHtml + perfHtml + portfolioHtml + tableHtml + reportHtml;

  // Wire up analysis cross-links
  main.querySelectorAll(".spy-analysis-link").forEach((a) => {
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      window.dispatchEvent(new CustomEvent("load-analysis", { detail: parseInt(a.dataset.id, 10) }));
      document.querySelector(".main-tab[data-tab=\"analyze\"]").click();
    });
  });
}

async function triggerSpyScan() {
  const btn = $$spy("btn-spy-scan");
  const status = $$spy("spy-scan-status");
  if (btn) btn.disabled = true;
  if (status) status.textContent = "Starting scan…";
  try {
    const r = await fetch("/api/spy-scan", { method: "POST" });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    const msg = data.new ? "Scan #" + data.scan_id + " started" : "Scan #" + data.scan_id + " already running";
    if (status) status.textContent = msg;
    await loadSpyHistory();
    loadSpyScan(data.scan_id);
  } catch (e) {
    if (status) status.textContent = "Error: " + e;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function refreshSpyPrices(scanId) {
  const r = await fetch("/api/spy-scans/" + scanId + "/refresh-prices", { method: "POST" });
  const data = await r.json();
  if (data.error) { alert("Refresh failed: " + data.error); return; }
  await loadSpyHistory();
  loadSpyScan(scanId);
}

document.addEventListener("tab-shown", (ev) => {
  if (ev.detail === "spy") {
    loadSpyHistory();
  } else {
    stopSpyPoll();
  }
});

// ===== Refresh paper-portfolio prices =====
// Re-prices the active (or latest) S&P 500 paper portfolio. Uses whatever
// market-data source is wired up — Schwab quotes when Schwab market data is
// enabled, otherwise free yfinance — handled server-side in
// spy_scanner.refresh_portfolio_prices. No real-account data is shown here.

async function refreshActiveSpy() {
  const btn = $$spy("btn-spy-account");
  const status = $$spy("spy-scan-status");
  let scanId = activeSpyId;
  if (btn) { btn.disabled = true; btn.textContent = "Refreshing…"; }
  if (status) status.textContent = "Refreshing prices…";
  try {
    if (!scanId) {
      const r = await fetch("/api/spy-scans?limit=1");
      const { scans } = await r.json();
      if (!scans || !scans.length) {
        if (status) status.textContent = "No S&P 500 scan to refresh yet.";
        return;
      }
      scanId = scans[0].id;
    }
    await refreshSpyPrices(scanId);
    if (status) status.textContent = "Prices refreshed.";
  } catch (e) {
    if (status) status.textContent = "Refresh failed: " + e;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Refresh"; }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const btn = $$spy("btn-spy-scan");
  if (btn) btn.addEventListener("click", triggerSpyScan);
  const stopBtn = $$spy("btn-spy-stop");
  if (stopBtn) stopBtn.addEventListener("click", triggerSpyStop);
  const acctBtn = $$spy("btn-spy-account");
  if (acctBtn) acctBtn.addEventListener("click", refreshActiveSpy);
});

// Auto-refresh history every 15s while tab is visible
setInterval(() => {
  const pane = document.querySelector("[data-pane=\"spy\"]");
  if (pane && !pane.hidden) loadSpyHistory();
}, 15000);
