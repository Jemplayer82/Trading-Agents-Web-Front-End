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
      const statusClass = s.status === "completed" ? "BUY" : (s.status.startsWith("running") ? "HOLD" : "SELL");
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
            "<span class=\"h-sig " + statusClass + "\">" + (s.status || "—").toUpperCase() + "</span>" +
          "</span>" +
          "<span class=\"h-ts\">" + fmtTs(s.created_at) + returnBadge + "</span>" +
        "</span>"
      );
      li.querySelector(".h-main").addEventListener("click", () => loadSpyScan(s.id));
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
  const btn = $$spy("btn-spy-stop");
  const status = $$spy("spy-scan-status");
  if (btn) { btn.disabled = true; btn.textContent = "Stopping…"; }
  if (status) status.textContent = "Cancelling scan #" + activeSpyId + "…";
  try {
    const r = await fetch("/api/spy-scans/" + activeSpyId + "/cancel", { method: "POST" });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    if (status) status.textContent = "Cancellation requested — finishing in-flight calls…";
  } catch (e) {
    if (status) status.textContent = "Cancel failed: " + e;
    if (btn) { btn.disabled = false; btn.textContent = "Stop Scan"; }
  }
}

function renderSpyScanError(msg) {
  const main = $$spy("spy-main");
  if (main) main.innerHTML = "<div class=\"panel\"><p style=\"color:var(--accent-red);\">" + escHtml(msg) + "</p></div>";
}

function renderSpyScan(scan) {
  const main = $$spy("spy-main");
  if (!main) return;

  // Status banner for terminal/cancel states
  let bannerHtml = "";
  if (scan.status === "cancelled") {
    bannerHtml = "<div class=\"panel\"><p style=\"color:var(--accent-yellow);\">Scan #" + scan.id + " was cancelled. Partial results below.</p></div>";
  } else if (scan.cancel_requested && scan.status && scan.status.startsWith("running")) {
    bannerHtml = "<div class=\"panel\"><p style=\"color:var(--accent-yellow);\">Cancellation requested — finishing in-flight calls…</p></div>";
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
      const analysisLink = r.analysis_id
        ? "<a href=\"#\" class=\"spy-analysis-link\" data-id=\"" + r.analysis_id + "\">view →</a>"
        : "";
      return (
        "<tr>" +
          "<td><strong style=\"color:var(--accent-cyan);\">" + escHtml(r.ticker) + "</strong></td>" +
          "<td><span class=\"badge " + sig + "\">" + sig + "</span></td>" +
          "<td><span class=\"conviction-badge\" style=\"color:" + convColor + ";font-weight:700;\">" + conv + "/10</span></td>" +
          "<td style=\"color:var(--dim);font-size:11px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;\">" + escHtml(r.reasoning || "") + "</td>" +
          "<td style=\"font-size:12px;\">" + analysisLink + "</td>" +
        "</tr>"
      );
    }).join("");
    tableHtml = (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\">[ Quick Scan Results — " + sorted.length + " tickers ]</div>" +
        "<div style=\"overflow-x:auto;\">" +
          "<table class=\"spy-table\">" +
            "<thead><tr>" +
              "<th>Ticker</th><th>Signal</th><th>Conviction</th><th>Reasoning</th><th></th>" +
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
    // Active positions (non-exited) sorted by $ amount, then exits at the bottom
    const active = scan.portfolio_json.filter(a => a.action !== "EXITED" && (a.dollar_amount || 0) > 0);
    const exited = scan.portfolio_json.filter(a => a.action === "EXITED" || (a.dollar_amount || 0) === 0);
    const allocs = [
      ...active.sort((a, b) => (b.dollar_amount || 0) - (a.dollar_amount || 0)),
      ...exited,
    ];
    const total = active.reduce((s, a) => s + (a.dollar_amount || 0), 0);
    const basis = scan.starting_value || 100000;
    const isRebalance = scan.previous_scan_id != null;

    const rows = allocs.map((a) => {
      const sig = (a.signal || "—").toUpperCase();
      const act = (a.action || "NEW").toUpperCase();
      const actCol = actionColor[act] || "var(--dim)";
      const dimRow = act === "EXITED" ? " style=\"opacity:0.5;\"" : "";
      return (
        "<tr" + dimRow + ">" +
          "<td><strong style=\"color:var(--accent-cyan);\">" + escHtml(a.ticker) + "</strong></td>" +
          "<td><span style=\"font-size:10px;font-weight:700;text-transform:uppercase;color:" + actCol + ";\">" + act + "</span></td>" +
          "<td><span class=\"badge " + sig + "\">" + sig + "</span></td>" +
          "<td>" + (act === "EXITED" ? "<span class=\"dim\">—</span>" : "$" + Math.round(a.dollar_amount || 0).toLocaleString()) + "</td>" +
          "<td>" + (act === "EXITED" ? "<span class=\"dim\">—</span>" : (a.allocation_pct || 0).toFixed(1) + "%") + "</td>" +
          "<td style=\"color:var(--dim);font-size:11px;\">" + escHtml((a.rationale || "").slice(0, 100)) + "</td>" +
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
            "<thead><tr><th>Ticker</th><th>Action</th><th>Signal</th><th>$ Amount</th><th>%</th><th>Rationale</th></tr></thead>" +
            "<tbody>" + rows + "</tbody>" +
            "<tfoot><tr style=\"font-weight:700;border-top:1px solid var(--panel-border);\">" +
              "<td colspan=\"3\">TOTAL DEPLOYED</td>" +
              "<td>$" + Math.round(total).toLocaleString() + "</td>" +
              "<td>" + (basis > 0 ? (total / basis * 100).toFixed(1) : "0.0") + "%</td><td></td>" +
            "</tr></tfoot>" +
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
    reportHtml = "<div class=\"panel\"><p style=\"color:var(--accent-red);\">" + escHtml(scan.error) + "</p></div>";
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

document.addEventListener("DOMContentLoaded", () => {
  const btn = $$spy("btn-spy-scan");
  if (btn) btn.addEventListener("click", triggerSpyScan);
  const stopBtn = $$spy("btn-spy-stop");
  if (stopBtn) stopBtn.addEventListener("click", triggerSpyStop);
});

// Auto-refresh history every 15s while tab is visible
setInterval(() => {
  const pane = document.querySelector("[data-pane=\"spy\"]");
  if (pane && !pane.hidden) loadSpyHistory();
}, 15000);
