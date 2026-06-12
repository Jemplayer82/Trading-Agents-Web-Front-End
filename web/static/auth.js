// TradingAgents Web — login / first-run setup gate.
//
// Reveals the full-screen #auth-overlay (index.html) until GET /api/auth/me says
// the session cookie is valid. When the backend reports setup_required (zero
// users in the DB yet), the form switches to first-run "create admin account"
// mode and posts to /api/auth/setup instead of /api/auth/login.
//
// Endpoints: GET /api/auth/me, POST /api/auth/login | /api/auth/setup |
// /api/auth/logout. On success the page is reloaded so every module
// re-initialises in an authenticated state (their DOMContentLoaded fetches fire
// regardless of the overlay, so a reload is the simplest correct reset).
//
// The overlay is UX only — real enforcement is server-side (web/auth_app.py
// returns 401s on everything else); hiding the overlay grants nothing.
//
// IIFE-scoped; exposes window.taLogout for the Settings tab's logout path.
// $ comes from utils.js (loaded first).

(function () {
  // $ comes from utils.js (loaded first).
  let mode = "login"; // "login" | "setup"

  function showOverlay() { $("auth-overlay").hidden = false; }
  function hideOverlay() { $("auth-overlay").hidden = true; }

  function setMode(setupRequired) {
    mode = setupRequired ? "setup" : "login";
    $("auth-msg").textContent = setupRequired
      ? "First run — create the admin account for this dashboard."
      : "Sign in to continue.";
    $("auth-submit").textContent = setupRequired ? "Create account" : "Log in";
    $("auth-confirm-wrap").hidden = !setupRequired;
    $("auth-password").setAttribute("autocomplete", setupRequired ? "new-password" : "current-password");
  }

  async function checkAuth() {
    try {
      const resp = await fetch("/api/auth/me");
      const data = await resp.json();
      if (data.authenticated) {
        hideOverlay();
        const lo = $("btn-logout");
        if (lo) {
          lo.hidden = false;
          lo.addEventListener("click", doLogout);
        }
        return true;
      }
      setMode(!!data.setup_required);
      showOverlay();
      $("auth-username").focus();
      return false;
    } catch (e) {
      // Network/parse error — show login so the user isn't stuck on a blank gate
      setMode(false);
      showOverlay();
      return false;
    }
  }

  async function submit(ev) {
    ev.preventDefault();
    const err = $("auth-error");
    err.textContent = "";
    const username = $("auth-username").value.trim();
    const password = $("auth-password").value;
    if (!username || !password) { err.textContent = "Username and password required."; return; }

    if (mode === "setup") {
      if (password.length < 8) { err.textContent = "Password must be at least 8 characters."; return; }
      if (password !== $("auth-confirm").value) { err.textContent = "Passwords do not match."; return; }
    }

    const url = mode === "setup" ? "/api/auth/setup" : "/api/auth/login";
    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        err.textContent = data.detail || `Failed (${resp.status})`;
        return;
      }
      // Success — reload so the whole app initialises cleanly while authed.
      location.reload();
    } catch (e) {
      err.textContent = "Network error — try again.";
    }
  }

  async function doLogout() {
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch (e) {}
    location.reload();
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("auth-form").addEventListener("submit", submit);
    checkAuth();
  });

  // Let other scripts trigger logout (e.g. Settings tab button)
  window.taLogout = doLogout;
})();
