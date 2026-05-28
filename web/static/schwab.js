// Schwab tab — connection status, connect/disconnect, run scan now

const $$s = (id) => document.getElementById(id);

async function loadSchwabStatus() {
  const box = $$s("schwab-status-box");
  const dis = $$s("btn-schwab-disconnect");
  const connect = $$s("btn-schwab-connect");
  if (!box) return;
  box.innerHTML = '<p class="dim">Loading…</p>';
  try {
    const resp = await fetch("/api/auth/schwab/status");
    const s = await resp.json();
    if (!s.connected) {
      box.innerHTML = '<p><span class="badge SELL">NOT CONNECTED</span> Click below to authorize via Schwab.</p>';
      dis.hidden = true;
      connect.textContent = "Connect to Schwab";
      return;
    }
    const days = s.days_until_refresh_expires;
    const colorClass = days >= 3 ? "BUY" : (days >= 1 ? "HOLD" : "SELL");
    box.innerHTML = `
      <p><span class="badge ${colorClass}">CONNECTED</span>
      Refresh token valid for <strong>${days} day${days === 1 ? "" : "s"}</strong>.</p>
      <p class="dim">Issued ${s.refresh_issued_at || ""}. After ${days < 1 ? "today" : "7 days from issue"} you'll need to reconnect.</p>
    `;
    dis.hidden = false;
    connect.textContent = "Reconnect";
  } catch (e) {
    box.innerHTML = `<p style="color: var(--accent-red);">Failed to load status: ${e}</p>`;
  }
}

function connectSchwab() {
  // open OAuth in a new tab; on success Schwab redirects to /api/auth/schwab/callback which closes itself
  const w = window.open("/api/auth/schwab", "schwab-oauth", "width=520,height=720");
  // poll status until connected
  const poll = setInterval(async () => {
    if (w && w.closed) {
      clearInterval(poll);
      loadSchwabStatus();
      return;
    }
    try {
      const s = await (await fetch("/api/auth/schwab/status")).json();
      if (s.connected) {
        clearInterval(poll);
        try { w && w.close(); } catch (e) {}
        loadSchwabStatus();
      }
    } catch (e) {}
  }, 1500);
}

async function disconnectSchwab() {
  if (!confirm("Disconnect Schwab? You'll need to re-auth before the next scan.")) return;
  await fetch("/api/auth/schwab", { method: "DELETE" });
  loadSchwabStatus();
}

async function runScanNow() {
  const btn = $$s("btn-scan-now");
  const out = $$s("scan-now-result");
  btn.disabled = true;
  out.textContent = "starting…";
  try {
    const r = await fetch("/api/portfolio-scan", { method: "POST" });
    const data = await r.json();
    if (!r.ok) {
      out.innerHTML = `<span style="color: var(--accent-red);">${data.detail || JSON.stringify(data)}</span>`;
    } else {
      out.innerHTML = `Scan <strong>#${data.scan_id}</strong> ${data.new ? "started" : "already running (idempotent)"}. Switch to the Portfolio Scan tab to watch.`;
    }
  } catch (e) {
    out.innerHTML = `<span style="color: var(--accent-red);">${e}</span>`;
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $$s("btn-schwab-connect")?.addEventListener("click", connectSchwab);
  $$s("btn-schwab-disconnect")?.addEventListener("click", disconnectSchwab);
  $$s("btn-scan-now")?.addEventListener("click", runScanNow);
});

document.addEventListener("tab-shown", (ev) => {
  if (ev.detail === "schwab") loadSchwabStatus();
});
