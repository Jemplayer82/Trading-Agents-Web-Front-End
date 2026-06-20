// TradingAgents Web — "Settings" tab (data-pane "keys"): app settings + toggles,
// custom env vars, LLM provider API keys, and user management.
//
// Endpoints (all via web/nginx.conf's generic /api/ block -> the api app):
//   GET /api/credentials, PUT/DELETE /api/credentials/{provider}   LLM keys
//   GET /api/settings,    PUT/DELETE /api/settings/{key}           app settings
//   GET/POST /api/auth/users, POST /api/auth/password              users
//
// Secrets are write-only from the browser's point of view: the server returns
// only masked previews ("masked"), never the stored value. Source tags show
// where the effective value comes from (DB override vs .env fallback).
//
// Unlike app.js / portfolio.js / spy.js this file is wrapped in an IIFE, so
// nothing leaks into the shared global scope except window.reloadCredentials
// (a console convenience hook). Everything reloads on every "tab-shown" with
// detail "keys" to keep masked previews fresh.

(function () {
  // $ comes from utils.js (loaded first).

  function providerLabel(key) {
    // Match labels used elsewhere in the UI
    const map = {
      openai: "OpenAI",
      anthropic: "Anthropic",
      google: "Google",
      azure: "Azure OpenAI",
      xai: "xAI",
      deepseek: "DeepSeek",
      qwen: "Qwen (Global)",
      "qwen-cn": "Qwen (China)",
      glm: "GLM (Z.AI)",
      "glm-cn": "GLM (BigModel)",
      minimax: "MiniMax (Global)",
      "minimax-cn": "MiniMax (China)",
      openrouter: "OpenRouter",
      ollama: "Ollama",
      switchboard: "Switchboard (Bus LLM)",
    };
    return map[key] || key;
  }

  async function loadCredentials() {
    const status = $("keys-status");
    const body = $("keys-body");
    if (!body) return;
    status.textContent = "loading…";
    let creds = [];
    try {
      const resp = await fetch("/api/credentials");
      const data = await resp.json();
      creds = data.credentials || [];
    } catch (e) {
      status.textContent = `failed to load: ${e}`;
      return;
    }
    body.innerHTML = "";
    creds.forEach((c) => {
      const tr = document.createElement("tr");
      tr.dataset.provider = c.provider;

      const tdProv = document.createElement("td");
      tdProv.textContent = providerLabel(c.provider);
      tr.appendChild(tdProv);

      const tdEnv = document.createElement("td");
      tdEnv.innerHTML = c.env_var
        ? `<code>${c.env_var}</code>`
        : '<span class="dim">— no key needed</span>';
      tr.appendChild(tdEnv);

      const tdCurrent = document.createElement("td");
      if (!c.env_var) {
        tdCurrent.innerHTML = '<span class="dim">n/a</span>';
      } else if (c.has_key) {
        const tag = c.source === "db" ? '<span class="key-src db">DB</span>'
                                       : '<span class="key-src env">.env</span>';
        tdCurrent.innerHTML = `${tag} <code>${escapeHtml(c.masked || "set")}</code>`;
      } else {
        tdCurrent.innerHTML = '<span class="dim">— none set</span>';
      }
      tr.appendChild(tdCurrent);

      const tdNew = document.createElement("td");
      if (c.env_var) {
        const inp = document.createElement("input");
        inp.type = "password";
        inp.placeholder = "Paste new key (will be hidden)";
        inp.autocomplete = "off";
        inp.spellcheck = false;
        tdNew.appendChild(inp);
      } else {
        tdNew.innerHTML = '<span class="dim">—</span>';
      }
      tr.appendChild(tdNew);

      const tdActions = document.createElement("td");
      tdActions.className = "keys-actions";
      if (c.env_var) {
        const saveBtn = document.createElement("button");
        saveBtn.type = "button";
        saveBtn.className = "primary";
        saveBtn.textContent = "Save";
        saveBtn.addEventListener("click", () => saveCred(c.provider, tdNew.querySelector("input")));
        tdActions.appendChild(saveBtn);

        if (c.source === "db") {
          const clearBtn = document.createElement("button");
          clearBtn.type = "button";
          clearBtn.className = "ghost";
          clearBtn.textContent = "Clear";
          clearBtn.style.marginLeft = "6px";
          clearBtn.addEventListener("click", () => clearCred(c.provider));
          tdActions.appendChild(clearBtn);
        }
      }
      tr.appendChild(tdActions);

      body.appendChild(tr);
    });
    status.textContent = `${creds.length} providers — values from .env shown as fallback unless overridden in DB.`;
  }

  async function saveCred(provider, input) {
    const apiKey = (input && input.value || "").trim();
    if (!apiKey) { alert("Paste a key first."); return; }
    const status = $("keys-status");
    status.textContent = `saving ${provider}…`;
    try {
      const resp = await fetch(`/api/credentials/${encodeURIComponent(provider)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: apiKey }),
      });
      if (!resp.ok) {
        const text = await resp.text();
        status.textContent = `save failed: ${text}`;
        return;
      }
      input.value = "";
      await loadCredentials();
    } catch (e) {
      status.textContent = `save failed: ${e}`;
    }
  }

  async function clearCred(provider) {
    if (!confirm(`Clear stored ${providerLabel(provider)} key? (env-var fallback, if any, will still be used.)`)) return;
    const status = $("keys-status");
    status.textContent = `clearing ${provider}…`;
    try {
      const resp = await fetch(`/api/credentials/${encodeURIComponent(provider)}`, {
        method: "DELETE",
      });
      if (!resp.ok) {
        const text = await resp.text();
        status.textContent = `clear failed: ${text}`;
        return;
      }
      await loadCredentials();
    } catch (e) {
      status.textContent = `clear failed: ${e}`;
    }
  }

  // ---------- App settings (Schwab, Ollama, SMTP, etc.) + custom ----------

  function srcTag(source) {
    if (source === "db") return '<span class="key-src db">DB</span>';
    if (source === "env") return '<span class="key-src env">.env</span>';
    return "";
  }

  async function loadSettings() {
    const status = $("settings-status");
    const body = $("settings-body");
    if (!body) return;
    status.textContent = "loading…";
    let data;
    try {
      const resp = await fetch("/api/settings");
      data = await resp.json();
    } catch (e) {
      status.textContent = `failed to load: ${e}`;
      return;
    }
    const registry = data.registry || [];
    const custom = data.custom || [];

    body.innerHTML = "";
    let lastGroup = null;
    registry.forEach((s) => {
      if (s.group !== lastGroup) {
        lastGroup = s.group;
        const gtr = document.createElement("tr");
        gtr.innerHTML = `<td colspan="5" class="settings-group-title">${s.group}</td>`;
        body.appendChild(gtr);
      }
      const tr = document.createElement("tr");

      const tdLabel = document.createElement("td");
      tdLabel.textContent = s.label;
      tr.appendChild(tdLabel);

      const tdEnv = document.createElement("td");
      tdEnv.innerHTML = `<code>${s.key}</code>`;
      tr.appendChild(tdEnv);

      const tdCur = document.createElement("td");
      if (s.has_value) {
        // masked is the VERBATIM user-set value for non-secret settings — must escape.
        tdCur.innerHTML = `${srcTag(s.source)} <code>${escapeHtml(s.masked || "set")}</code>`;
      } else {
        tdCur.innerHTML = '<span class="dim">— none set</span>';
      }
      tr.appendChild(tdCur);

      const tdNew = document.createElement("td");
      let inp = null;
      if (s.type === "toggle") {
        // Toggles aren't actually masked — `masked` carries the literal stored
        // value ("0"/"1"/"false"/...). Unset means On by default.
        const cur = s.has_value ? String(s.masked).trim().toLowerCase() : "1";
        const on = !["0", "false", "no", ""].includes(cur);
        const lbl = document.createElement("label");
        lbl.className = "toggle-switch";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = on;
        const txt = document.createElement("span");
        txt.className = "toggle-label";
        txt.style.marginLeft = "6px";
        const onLabel  = s.on_label  || "On";
        const offLabel = s.off_label || "Off";
        txt.textContent = on ? onLabel : offLabel;
        cb.addEventListener("change", () => {
          saveToggle(s.key, cb.checked, cb, txt, onLabel, offLabel);
        });
        lbl.appendChild(cb);
        lbl.appendChild(txt);
        tdNew.appendChild(lbl);
      } else if (s.type === "select") {
        inp = document.createElement("select");
        (s.options || []).forEach((o) => {
          const val = typeof o === "string" ? o : o.value;
          const lab = typeof o === "string" ? o : (o.label || o.value);
          const opt = document.createElement("option");
          opt.value = val; opt.textContent = lab;
          inp.appendChild(opt);
        });
        if (s.has_value && s.masked) inp.value = String(s.masked).trim();
        if (s.placeholder) inp.title = s.placeholder;
        tdNew.appendChild(inp);
      } else {
        inp = document.createElement("input");
        inp.type = s.secret ? "password" : "text";
        inp.placeholder = s.placeholder || "New value";
        inp.autocomplete = "off";
        inp.spellcheck = false;
        tdNew.appendChild(inp);
      }
      tr.appendChild(tdNew);

      const tdAct = document.createElement("td");
      tdAct.className = "keys-actions";
      if (s.type !== "toggle") {
        const saveBtn = document.createElement("button");
        saveBtn.type = "button"; saveBtn.className = "primary"; saveBtn.textContent = "Save";
        saveBtn.addEventListener("click", () => saveSetting(s.key, inp));
        tdAct.appendChild(saveBtn);
      }
      if (s.source === "db") {
        const clr = document.createElement("button");
        clr.type = "button"; clr.className = "ghost"; clr.textContent = "Clear";
        clr.style.marginLeft = "6px";
        const confirmMsg = s.type === "toggle"
          ? `Clear ${s.key}? The switch will revert to the .env default (typically On).`
          : `Clear ${s.key}? Any .env fallback will be used instead.`;
        clr.addEventListener("click", () => clearSetting(s.key, confirmMsg));
        tdAct.appendChild(clr);
      }
      tr.appendChild(tdAct);
      body.appendChild(tr);
    });
    status.textContent = "Saved values apply immediately; .env values are used as fallback.";

    // Custom settings table
    const ctable = $("custom-settings-table");
    const cbody = $("custom-settings-body");
    cbody.innerHTML = "";
    if (custom.length) {
      ctable.hidden = false;
      custom.forEach((c) => {
        const tr = document.createElement("tr");
        // c.key is server-validated (^[A-Z][A-Z0-9_]*$); masked still escaped for discipline.
        tr.innerHTML = `<td><code>${c.key}</code></td><td>${srcTag("db")} <code>${escapeHtml(c.masked || "set")}</code></td>`;
        const tdAct = document.createElement("td");
        tdAct.className = "keys-actions";
        const clr = document.createElement("button");
        clr.type = "button"; clr.className = "ghost"; clr.textContent = "Clear";
        clr.addEventListener("click", () => clearSetting(c.key));
        tdAct.appendChild(clr);
        tr.appendChild(tdAct);
        cbody.appendChild(tr);
      });
    } else {
      ctable.hidden = true;
    }
  }

  async function saveSetting(key, input) {
    const value = (input && input.value || "").trim();
    if (!value) { alert("Enter a value first."); return; }
    const status = $("settings-status");
    status.textContent = `saving ${key}…`;
    try {
      const resp = await fetch(`/api/settings/${encodeURIComponent(key)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
      if (!resp.ok) {
        const d = await resp.json().catch(() => ({}));
        status.textContent = `save failed: ${d.detail || resp.status}`;
        return;
      }
      input.value = "";
      await loadSettings();
    } catch (e) {
      status.textContent = `save failed: ${e}`;
    }
  }

  // Toggle-type settings (e.g. SCHWAB_ENABLED) save "1"/"0" on flip — no Save button.
  async function saveToggle(key, on, cb, txt, onLabel, offLabel) {
    const status = $("settings-status");
    const setLabel = (state) => {
      if (txt) txt.textContent = state ? (onLabel || "On") : (offLabel || "Off");
    };
    const revert = () => {
      if (cb) { cb.checked = !on; cb.disabled = false; }
      setLabel(!on);
    };
    setLabel(on);
    status.textContent = `saving ${key}…`;
    if (cb) cb.disabled = true;   // block overlapping flips while this save is in flight
    try {
      const resp = await fetch(`/api/settings/${encodeURIComponent(key)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: on ? "1" : "0" }),
      });
      if (!resp.ok) {
        const d = await resp.json().catch(() => ({}));
        status.textContent = `save failed: ${d.detail || resp.status}`;
        revert();
        return;
      }
      // Drive Schwab surfaces directly from the value we just saved — pass it to
      // applySchwabVisibility so it skips the /api/auth/schwab/status fetch, which
      // can block on a 30s MCP call when enabled and would freeze "saving…".
      if (key === "SCHWAB_ENABLED" && window.applySchwabVisibility) {
        await window.applySchwabVisibility(on);
      }
      await loadSettings();   // rebuilds the table with a fresh, enabled checkbox
    } catch (e) {
      status.textContent = `save failed: ${e}`;
      revert();
    }
  }

  async function clearSetting(key, confirmMsg) {
    if (!confirm(confirmMsg || `Clear ${key}? Any .env fallback will be used instead.`)) return;
    try {
      const resp = await fetch(`/api/settings/${encodeURIComponent(key)}`, { method: "DELETE" });
      if (!resp.ok) {
        const d = await resp.json().catch(() => ({}));
        $("settings-status").textContent = `clear failed: ${d.detail || resp.status}`;
        return;
      }
      await loadSettings();
    } catch (e) {
      $("settings-status").textContent = `clear failed: ${e}`;
    }
  }

  function wireCustomForm() {
    const form = $("custom-setting-form");
    if (!form || form.dataset.wired) return;
    form.dataset.wired = "1";
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const key = $("custom-setting-key").value.trim().toUpperCase();
      const value = $("custom-setting-value").value;
      if (!/^[A-Z][A-Z0-9_]*$/.test(key)) { alert("Key must be UPPER_SNAKE_CASE (letters, digits, underscores)."); return; }
      if (!value) { alert("Enter a value."); return; }
      try {
        const resp = await fetch(`/api/settings/${encodeURIComponent(key)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value }),
        });
        if (!resp.ok) {
          const d = await resp.json().catch(() => ({}));
          alert(`Failed: ${d.detail || resp.status}`);
          return;
        }
        $("custom-setting-key").value = "";
        $("custom-setting-value").value = "";
        await loadSettings();
      } catch (e) { alert(`Failed: ${e}`); }
    });
  }

  // ---------- Users ----------

  async function loadUsers() {
    const body = $("users-body");
    if (!body) return;
    try {
      const resp = await fetch("/api/auth/users");
      const data = await resp.json();
      body.innerHTML = "";
      (data.users || []).forEach((u) => {
        const tr = document.createElement("tr");
        // username is user-supplied — escape-at-render (stored-XSS sink otherwise).
        tr.innerHTML = `<td>${escapeHtml(u.username)}</td><td class="dim">${escapeHtml((u.created_at || "").slice(0, 19).replace("T", " "))}</td>`;
        body.appendChild(tr);
      });
    } catch (e) { /* gated until logged in */ }
  }

  function wireUserForms() {
    const addF = $("add-user-form");
    if (addF && !addF.dataset.wired) {
      addF.dataset.wired = "1";
      addF.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const username = $("new-user-name").value.trim();
        const password = $("new-user-pass").value;
        const msg = $("users-msg");
        try {
          const resp = await fetch("/api/auth/users", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
          });
          const d = await resp.json().catch(() => ({}));
          msg.textContent = resp.ok ? `User '${username}' created.` : `Failed: ${d.detail || resp.status}`;
          if (resp.ok) { $("new-user-name").value = ""; $("new-user-pass").value = ""; loadUsers(); }
        } catch (e) { msg.textContent = `Failed: ${e}`; }
      });
    }
    const passF = $("change-pass-form");
    if (passF && !passF.dataset.wired) {
      passF.dataset.wired = "1";
      passF.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const msg = $("users-msg");
        try {
          const resp = await fetch("/api/auth/password", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              current_password: $("cur-pass").value,
              new_password: $("new-pass").value,
            }),
          });
          const d = await resp.json().catch(() => ({}));
          msg.textContent = resp.ok ? "Password updated." : `Failed: ${d.detail || resp.status}`;
          if (resp.ok) { $("cur-pass").value = ""; $("new-pass").value = ""; }
        } catch (e) { msg.textContent = `Failed: ${e}`; }
      });
    }
  }

  function loadAll() {
    loadCredentials();
    loadSettings();
    loadUsers();
    wireCustomForm();
    wireUserForms();
  }

  // Reload on every tab activation — keeps masked previews fresh.
  document.addEventListener("tab-shown", (ev) => {
    if (ev.detail === "keys") loadAll();
  });

  document.addEventListener("DOMContentLoaded", () => {
    const initial = document.querySelector(".tab-pane.active");
    if (initial && initial.dataset.pane === "keys") loadAll();
  });

  // Expose a manual refresh hook for the console
  window.reloadCredentials = loadAll;
})();
