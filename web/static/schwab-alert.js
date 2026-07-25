// TradingAgents Web — full-screen "Schwab not connected" alert.
//
// Checks GET /api/auth/schwab/status on load and every RECHECK_MS while the
// tab stays open (catches a token dying mid-session, not just at load).
// Shows #schwab-alert-overlay (index.html) whenever enabled && !connected.
// Dismissible per-view only — no localStorage suppression, so it comes back
// on the next load/recheck until actually reconnected. That's the point:
// this is the "you're looking at the dashboard" channel. The "away from the
// dashboard" channel is the scheduler's hourly job_token_health email/webhook
// alert (web/scheduler.py) — the two are complementary, not redundant.
//
// Runs independently of auth.js's login gate: if the dashboard session isn't
// authenticated yet, /api/auth/schwab/status 401s and this silently no-ops;
// auth.js's post-login location.reload() re-triggers this check naturally.
//
// $ comes from utils.js (loaded first).

(function () {
  const RECHECK_MS = 5 * 60 * 1000; // 5 min — complements the hourly server-side check
  let dismissedForThisWarning = false;

  async function check() {
    let data;
    try {
      const r = await fetch("/api/auth/schwab/status");
      if (!r.ok) return; // not authenticated yet, or transient — say nothing
      data = await r.json();
    } catch (e) {
      return; // network hiccup — don't flash a false alarm
    }

    const overlay = $("schwab-alert-overlay");
    if (!overlay) return;

    const needsAttention = data.enabled !== false && data.connected === false;
    if (!needsAttention) {
      dismissedForThisWarning = false; // reconnected — arm for the next outage
      overlay.hidden = true;
      return;
    }
    if (dismissedForThisWarning) return; // user already acknowledged this outage
    overlay.hidden = false;
  }

  document.addEventListener("DOMContentLoaded", () => {
    const overlay = $("schwab-alert-overlay");
    $("schwab-alert-dismiss")?.addEventListener("click", () => {
      dismissedForThisWarning = true;
      if (overlay) overlay.hidden = true;
    });
    $("schwab-alert-fix")?.addEventListener("click", () => {
      window.open("https://schwab.txferguson.net/auth", "_blank", "noopener");
    });
    check();
    setInterval(check, RECHECK_MS);
  });
})();
