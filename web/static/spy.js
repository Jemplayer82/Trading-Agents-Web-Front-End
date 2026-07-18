// TradingAgents Web — "S&P 500" tab (paper-portfolio scanner).
//
// Scan history sidebar, dual progress bars (quick scan + deep dive), quick-results
// table, the $100k PAPER portfolio table, and the allocator report. Every dollar
// figure on this tab is simulated — no live brokerage data here (live holdings
// belong to the Portfolio tab, portfolio.js).
//
// Endpoints — all start with /api/spy, so web/nginx.conf's `location /api/spy`
// string-prefix match routes them to the PORTFOLIO app (web/portfolio_main.py);
// the prefix also catches /api/spy-scans and /api/spy-account, not just /api/spy:
//   POST   /api/spy-scan                          start a scan (idempotent per day)
//   GET    /api/spy-scans[?limit=N]               history list (DESC by id)
//   GET    /api/spy-scans/{id}                    scan detail (polled while running)
//   DELETE /api/spy-scans/{id}                    delete scan + results
//   POST   /api/spy-scans/{id}/cancel             request cancel (deep dives finish)
//   POST   /api/spy-scans/{id}/refresh-prices     re-price the paper portfolio
//
// Globals consumed from utils.js (loaded first): $, escapeHtml, fmtTs,
// renderMarkdown, apiFetch, progressBar. Everything here is top-level in the
// shared classic-script scope; refreshSpyPrices in particular MUST stay global
// because renderSpyScan emits inline onclick="refreshSpyPrices(...)" handlers.
//
// Poll lifecycle: while the loaded scan's status starts with "running",
// loadSpyScan polls the detail endpoint every 5s and re-renders. The timer is
// cleared on completion / fetch failure, when another scan is loaded
// (loadSpyScan calls stopSpyPoll first), on delete, and when the user leaves the
// tab ("tab-shown" listener at the bottom).

let activeSpyId = null;
let spyPollTimer = null;
let paperAccounts = [];
let activePaperAccountId = null;
let editingAccountId = null;

// ===== Paper account management =====

async function loadPaperAccounts() {
  try {
    const data = await apiFetch("/api/paper-accounts");
    paperAccounts = data.accounts || [];
  } catch (e) {
    paperAccounts = [];
  }
  renderAccountSelector();
  renderAccountsModal();
}

function renderAccountSelector() {
  const sel = $("spy-account-sel");
  if (!sel) return;
  sel.innerHTML = "";

  // "All accounts" option (no filter)
  const allOpt = document.createElement("option");
  allOpt.value = "";
  allOpt.textContent = "All accounts";
  sel.appendChild(allOpt);

  paperAccounts.forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = a.name;
    if (a.id === activePaperAccountId) opt.selected = true;
    sel.appendChild(opt);
  });

  if (!activePaperAccountId && paperAccounts.length) {
    // Default to first account
    activePaperAccountId = paperAccounts[0].id;
    sel.value = activePaperAccountId;
  }

  updateAccountMeta();
}

function updateAccountMeta() {
  const meta = $("spy-account-meta");
  if (!meta) return;
  const acct = paperAccounts.find((a) => a.id === activePaperAccountId);
  if (!acct) { meta.textContent = ""; return; }
  const biasLabel = { bullish: "🟢 Bullish", neutral: "⬜ Neutral", bearish: "🔴 Bearish" }[acct.bias] || acct.bias;
  meta.textContent = `$${(acct.starting_capital || 100000).toLocaleString()} · Aggressiveness ${acct.aggressiveness}/10 · ${biasLabel}`;
}

function renderAccountsModal() {
  const list = $("paper-accounts-list");
  if (!list) return;
  if (!paperAccounts.length) {
    list.innerHTML = "<p class=\"dim\" style=\"font-size:12px;\">No accounts yet. Create one below.</p>";
    return;
  }
  list.innerHTML = paperAccounts.map((a) => {
    const biasLabel = { bullish: "Bullish", neutral: "Neutral", bearish: "Bearish" }[a.bias] || a.bias;
    return (
      "<div style=\"display:flex;align-items:center;gap:10px;padding:8px;border:1px solid var(--panel-border);border-radius:3px;margin-bottom:6px;\">" +
        "<div style=\"flex:1;\">" +
          "<strong>" + escapeHtml(a.name) + "</strong>" +
          "<span class=\"dim\" style=\"font-size:11px;margin-left:8px;\">" +
            "$" + (a.starting_capital || 100000).toLocaleString() + " · " +
            "Agg " + a.aggressiveness + "/10 · " + biasLabel +
          "</span>" +
        "</div>" +
        "<button type=\"button\" class=\"ghost\" style=\"font-size:11px;padding:3px 8px;\" " +
          "onclick=\"editPaperAccount(" + a.id + ")\">Edit</button>" +
        "<button type=\"button\" class=\"ghost\" style=\"font-size:11px;padding:3px 8px;color:var(--accent-red);border-color:var(--accent-red);\" " +
          "onclick=\"deletePaperAccount(" + a.id + ")\">Delete</button>" +
      "</div>"
    );
  }).join("");
}

async function openManageAccounts() {
  await loadPaperAccounts();
  resetAccountForm();
  const modal = $("paper-accounts-modal");
  if (modal) { modal.hidden = false; modal.style.display = "flex"; }
}

function closeManageAccounts() {
  const modal = $("paper-accounts-modal");
  if (modal) { modal.hidden = true; modal.style.display = "none"; }
}

// Load an account's values into the New/Edit account form.
function populateAccountForm(acct) {
  if ($("new-acct-name")) $("new-acct-name").value = acct.name || "";
  if ($("new-acct-capital")) $("new-acct-capital").value = acct.starting_capital || 100000;
  if ($("new-acct-agg")) $("new-acct-agg").value = acct.aggressiveness || 5;
  if ($("new-acct-agg-val")) $("new-acct-agg-val").textContent = acct.aggressiveness || 5;
  const bias = acct.bias || "neutral";
  document.querySelectorAll("#new-acct-bias .bias-btn").forEach((x) => {
    x.classList.toggle("active", x.dataset.val === bias);
  });
}

// Clear the form back to "create" defaults.
function resetAccountForm() {
  editingAccountId = null;
  populateAccountForm({ name: "", starting_capital: 100000, aggressiveness: 5, bias: "neutral" });
  const btn = $("btn-create-account");
  if (btn) btn.textContent = "Create Account";
}

// Load an existing account into the form for editing (PUT on save).
function editPaperAccount(id) {
  const acct = paperAccounts.find((a) => a.id === id);
  if (!acct) return;
  editingAccountId = id;
  populateAccountForm(acct);
  const btn = $("btn-create-account");
  if (btn) btn.textContent = "Save Changes";
  const name = $("new-acct-name");
  if (name) name.focus();
}

// Create (POST) or update (PUT) a paper account from the form.
async function savePaperAccount() {
  const name = ($("new-acct-name") || {}).value?.trim();
  if (!name) { alert("Enter an account name."); return; }
  const capital = parseFloat(($("new-acct-capital") || {}).value || "100000");
  const agg = parseInt(($("new-acct-agg") || {}).value || "5", 10);
  const activeBiasBtn = document.querySelector("#new-acct-bias .bias-btn.active");
  const bias = activeBiasBtn?.dataset?.val || "neutral";
  const body = JSON.stringify({ name, starting_capital: capital, aggressiveness: agg, bias });

  try {
    if (editingAccountId) {
      await apiFetch("/api/paper-accounts/" + editingAccountId, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body,
      });
    } else {
      await apiFetch("/api/paper-accounts", {
        method: "POST", headers: { "Content-Type": "application/json" }, body,
      });
    }
    resetAccountForm();        // back to create mode; list refresh shows the change
    await loadPaperAccounts();
    await loadSpyHistory();
  } catch (e) {
    alert("Failed to save account: " + e);
  }
}

async function deletePaperAccount(id) {
  const acct = paperAccounts.find((a) => a.id === id);
  if (!confirm("Delete account \"" + (acct?.name || id) + "\"?\n\nExisting scans are kept but lose their account association.")) return;
  try {
    await apiFetch("/api/paper-accounts/" + id, { method: "DELETE" });
    if (activePaperAccountId === id) activePaperAccountId = null;
    await loadPaperAccounts();
    await loadSpyHistory();
  } catch (e) {
    alert("Failed to delete: " + e);
  }
}

// ===== Formatting helpers =====

// Percent return of `val` against `basis` (defaults to the $100k starting pot).
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

// ===== Scan queue/history sidebar =====

function _setupSpyStabs() {
  document.querySelectorAll("[data-stab-target^=\"spy-\"]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.stabTarget;
      document.querySelectorAll("[data-stab-target^=\"spy-\"]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      [$("spy-queue"), $("spy-history")].forEach((el) => {
        if (el) el.hidden = el.id !== target;
      });
    });
  });
}

async function loadSpyQueue() {
  const ul = $("spy-queue");
  if (!ul) return;
  try {
    const data = await apiFetch("/api/portfolio/status");
    ul.innerHTML = "";
    const running = data.running && data.running.scan_type === "spy" ? [data.running] : [];
    const queuedSpy = (data.queued || []).filter((q) => q.scan_type === "spy");
    const items = [...running, ...queuedSpy];
    if (!items.length) {
      ul.innerHTML = "<li class=\"dim empty\">(queue empty)</li>";
      return;
    }
    items.forEach((item, idx) => {
      const li = document.createElement("li");
      li.dataset.id = item.id;
      const isRunning = item === data.running;
      const label = isRunning ? "RUNNING" : ("#" + (idx - (running.length ? 1 : 0) + 1) + " IN QUEUE");
      const badgeClass = isRunning ? "HOLD" : "QUEUED";
      li.innerHTML = (
        "<span class=\"h-main\">" +
          "<span class=\"h-top\">" +
            "<span class=\"h-tk\">spy #" + item.id + " · " + escapeHtml(item.trade_date || "") + "</span>" +
            "<span class=\"h-sig " + badgeClass + "\">" + label + "</span>" +
          "</span>" +
          "<span class=\"h-ts\">" + fmtTs(item.created_at) + "</span>" +
        "</span>"
      );
      if (isRunning) li.querySelector(".h-main").addEventListener("click", () => loadSpyScan(item.id));
      ul.appendChild(li);
    });
  } catch (e) {
    ul.innerHTML = "<li class=\"empty\" style=\"color:var(--accent-red);\">" + escapeHtml(String(e)) + "</li>";
  }
}

async function loadSpyHistory() {
  loadSpyQueue();  // keep queue in sync whenever history refreshes
  const ul = $("spy-history");
  if (!ul) return;
  ul.innerHTML = "<li class=\"dim empty\">loading…</li>";
  try {
    const accountParam = activePaperAccountId ? "&account_id=" + activePaperAccountId : "";
    const url = "/api/spy-scans?status=completed&status=failed&status=cancelled" + accountParam;
    const data = await apiFetch(url);
    const scans = data.scans || [];
    ul.innerHTML = "";
    $("spy-history-clear-btn").disabled = !scans.length;
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
      // Status badge reuses the signal colors: completed=green (BUY class),
      // running/stopping=yellow (HOLD), failed/cancelled=red (SELL).
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
            "<span class=\"h-tk\">#" + s.id + " · " + escapeHtml(s.trade_date) + typeTag + "</span>" +
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
    ul.innerHTML = "<li class=\"empty\" style=\"color:var(--accent-red);\">" + escapeHtml(e) + "</li>";
  }
}

// ===== Scan detail: load + 5s poll =====

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
    // Note: unlike portfolio.js there is no active-id guard inside this callback.
    // Switching scans clears the interval (stopSpyPoll above), but a response
    // already in flight can still paint the previous scan once after a switch.
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

// ===== Stop / delete =====

// Show the Stop button only while the active scan is running.
function updateStopButton(scan) {
  const btn = $("btn-spy-stop");
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
  const btn = $("btn-spy-stop");
  const status = $("spy-scan-status");
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
      const main = $("spy-main");
      if (main) main.innerHTML = "";
      updateStopButton(null);
    }
    await loadSpyHistory();
  } catch (e) {
    alert("Delete failed: " + e);
  }
}

async function clearSpyHistory() {
  const data = await apiFetch("/api/spy-scans");
  const scans = data.scans || [];
  if (!scans.length) return;
  if (!confirm(
    "Clear all " + scans.length + " S&P 500 scans across all paper accounts?\n\n" +
    "This removes every scan, its quick-scan results, and its portfolio. Deep-dive analyses stay in the Run Analysis history."
  )) return;
  try {
    const r = await fetch("/api/spy-scans", { method: "DELETE" });
    if (!r.ok) { alert("Clear failed (HTTP " + r.status + ")."); return; }
    activeSpyId = null;
    stopSpyPoll();
    const main = $("spy-main");
    if (main) main.innerHTML = "";
    updateStopButton(null);
    await loadSpyHistory();
  } catch (e) {
    alert("Clear failed: " + e);
  }
}

// ===== Scan detail render =====
// renderSpyScan builds the whole #spy-main column as one HTML string (banner,
// progress, performance, portfolio table, quick results, allocator report) and
// assigns it in a single innerHTML write. Server/LLM text goes through
// escapeHtml; the allocator report (LLM markdown) only through renderMarkdown.

function renderSpyScanError(msg) {
  const main = $("spy-main");
  if (main) main.innerHTML = "<div class=\"panel\"><p style=\"color:var(--accent-red);\">" + escapeHtml(msg) + "</p></div>";
}

function renderSpyScan(scan) {
  const main = $("spy-main");
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
        escapeHtml(ticker) + " ↗</a>";
    }
    return "<strong style=\"color:var(--accent-cyan);\">" + escapeHtml(ticker) + "</strong>";
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
    // Two bars (quick + deep) in one panel; progressBar() (utils.js) is the shared
    // bar primitive also used by the portfolio scan.
    progressHtml = (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\">[ Progress ]</div>" +
        "<div style=\"margin-bottom:8px;\">" +
          "<div style=\"margin-bottom:4px;\">Quick scan: " + qc + "/" + qt + "</div>" +
          progressBar(qc, qt) +
        "</div>" +
        "<div>" +
          "<div style=\"margin-bottom:4px;\">Deep dive: " + dc + "/" + dt + "</div>" +
          progressBar(dc, dt) +
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
          (scan.rebalance_notes ? "<div style=\"color:var(--accent-yellow);margin-top:8px;white-space:pre-wrap;font-size:12px;\">" + escapeHtml(scan.rebalance_notes) + "</div>" : "") +
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
          "<td style=\"color:var(--dim);font-size:11px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;\">" + escapeHtml(r.reasoning || "") + "</td>" +
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
            "<td style=\"color:var(--dim);font-size:11px;\">" + escapeHtml((a.rationale || "").slice(0, 100)) + "</td>" +
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
          "<td style=\"color:var(--dim);font-size:11px;\">" + escapeHtml((a.rationale || "").slice(0, 90)) + "</td>" +
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
        "<div class=\"report-body\">" + renderMarkdown(md) + "</div>" +
      "</div>"
    );
  } else if (scan.error) {
    reportHtml = (
      "<div class=\"panel\">" +
        "<div class=\"panel-title\" style=\"color:var(--accent-red);\">[ Scan Failed ]</div>" +
        "<p style=\"color:var(--accent-red);white-space:pre-wrap;\">" + escapeHtml(scan.error) + "</p>" +
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

// ===== Start scan / refresh prices =====

async function triggerSpyScan() {
  const btn = $("btn-spy-scan");
  const status = $("spy-scan-status");
  if (btn) btn.disabled = true;
  if (status) status.textContent = "Starting scan…";
  try {
    const body = activePaperAccountId ? { account_id: activePaperAccountId } : {};
    const r = await fetch("/api/spy-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
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

// ===== Tab lifecycle + wiring =====

// "tab-shown" is dispatched by portfolio.js setupTabs() on every tab switch.
// Leaving this tab stops the 5s poll so a running scan isn't polled invisibly.
document.addEventListener("tab-shown", (ev) => {
  if (ev.detail === "spy") {
    loadPaperAccounts().then(() => loadSpyHistory());
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
  const btn = $("btn-spy-account");
  const status = $("spy-scan-status");
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
  const btn = $("btn-spy-scan");
  if (btn) btn.addEventListener("click", triggerSpyScan);
  const stopBtn = $("btn-spy-stop");
  if (stopBtn) stopBtn.addEventListener("click", triggerSpyStop);
  const acctBtn = $("btn-spy-account");
  if (acctBtn) acctBtn.addEventListener("click", refreshActiveSpy);
  const clearBtn = $("spy-history-clear-btn");
  if (clearBtn) clearBtn.addEventListener("click", clearSpyHistory);

  // Account selector
  const sel = $("spy-account-sel");
  if (sel) {
    sel.addEventListener("change", () => {
      activePaperAccountId = sel.value ? parseInt(sel.value, 10) : null;
      updateAccountMeta();
      loadSpyHistory();
    });
  }

  // Manage accounts modal
  const manageBtn = $("btn-manage-accounts");
  if (manageBtn) manageBtn.addEventListener("click", openManageAccounts);
  const closeBtn = $("btn-close-accounts");
  if (closeBtn) closeBtn.addEventListener("click", closeManageAccounts);
  const modal = $("paper-accounts-modal");
  if (modal) modal.addEventListener("click", (e) => { if (e.target === modal) closeManageAccounts(); });

  // New account form inside modal
  const createBtn = $("btn-create-account");
  if (createBtn) createBtn.addEventListener("click", savePaperAccount);

  // New account aggressiveness slider live label
  const newAggSlider = $("new-acct-agg");
  if (newAggSlider) {
    newAggSlider.addEventListener("input", () => {
      const v = $("new-acct-agg-val");
      if (v) v.textContent = newAggSlider.value;
    });
  }

  // New account bias toggle
  document.querySelectorAll("#new-acct-bias .bias-btn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll("#new-acct-bias .bias-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    });
  });

  // Load accounts on boot and wire up sidebar tabs
  loadPaperAccounts();
  _setupSpyStabs();
});

// Auto-refresh queue + history every 15s while tab is visible
setInterval(() => {
  const pane = document.querySelector("[data-pane=\"spy\"]");
  if (pane && !pane.hidden) {
    loadSpyQueue();
    loadSpyHistory();
  }
}, 15000);
