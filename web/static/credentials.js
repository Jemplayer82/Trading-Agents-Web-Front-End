// TradingAgents Web — API Keys tab

(function () {
  const $ = (id) => document.getElementById(id);

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
        tdCurrent.innerHTML = `${tag} <code>${c.masked || "set"}</code>`;
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

  // Reload on every tab activation — keeps the masked preview fresh
  // after the user has saved/cleared from another browser/tab.
  document.addEventListener("tab-shown", (ev) => {
    if (ev.detail === "keys") loadCredentials();
  });

  document.addEventListener("DOMContentLoaded", () => {
    // If page loads with #keys active, load immediately
    const initial = document.querySelector(".tab-pane.active");
    if (initial && initial.dataset.pane === "keys") {
      loadCredentials();
    }
  });

  // Expose a manual refresh hook for the console
  window.reloadCredentials = loadCredentials;
})();
