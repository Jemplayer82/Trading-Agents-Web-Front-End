// TradingAgents Web — Agent Bus live feed

(function () {
  "use strict";

  // ===== Agent display name map =====
  const AGENT_NAMES = {
    "market-analyst":        "Market Analyst",
    "sentiment-analyst":     "Sentiment",
    "news-analyst":          "News",
    "fundamentals-analyst":  "Fundamentals",
    "bull-researcher":       "Bull",
    "bear-researcher":       "Bear",
    "research-manager":      "Research Mgr",
    "trader":                "Trader",
    "risk-aggressive":       "Risk:Aggro",
    "risk-conservative":     "Risk:Consv",
    "risk-neutral":          "Risk:Neutral",
    "portfolio-manager":     "Portfolio Mgr",
    "switchboard-orchestrator":"Orchestrator",
  };

  // ===== State =====
  let ws = null;
  let desiredChannel = null;   // channel to join on connect/reconnect
  let reconnectAttempt = 0;
  let reconnectTimer = null;
  let lastBusStatusOk = null;  // null = unknown, true/false from bus_status frames
  let hiddenPaused = false;    // true while the socket is intentionally closed for a hidden tab
  const DOM_CAP = 300;

  let feed  = null;
  let dotEl = null;
  let chanEl = null;
  let pillEl = null;           // "↓ live" pill (created on first need)

  // ===== Auto-scroll state =====
  let stuckToBottom = true;

  // ===== DOM helpers =====
  // `$` (getElementById) is the global from utils.js, which loads first; this IIFE
  // closes over it. No local redefinition needed.

  function initDom() {
    feed   = $("bus-feed");
    dotEl  = $("bus-dot");
    chanEl = $("bus-channel");
  }

  // ===== Dot state =====
  // green = ws open + last bus_status ok
  // amber = connecting/reconnecting
  // red   = ws open but bus_status ok:false (outage)
  // grey  = bus not configured
  function setDot(state, title) {
    if (!dotEl) return;
    dotEl.className = "bus-dot bus-dot-" + state;
    dotEl.title = title || state;
  }

  // ===== Time formatting =====
  function fmtTime(ms) {
    const d = new Date(ms);
    const pad = (n) => String(n).padStart(2, "0");
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  // ===== Feed rendering =====
  function clearFeed() {
    if (feed) feed.replaceChildren();
  }

  function renderMessage(msg) {
    if (!feed) return;
    const agentId = msg.agent || "";
    const displayName = AGENT_NAMES[agentId] || agentId;

    const li = document.createElement("li");
    li.className = "bus-msg";
    li.dataset.agent = agentId;

    const badge = document.createElement("span");
    badge.className = "bus-badge";
    badge.textContent = displayName;

    const timeEl = document.createElement("span");
    timeEl.className = "bus-time";
    timeEl.textContent = fmtTime(msg.ts);

    const header = document.createElement("div");
    header.className = "bus-msg-header";
    header.appendChild(badge);

    if (msg.msg_type === "result" || msg.msg_type === "instruction") {
      const chip = document.createElement("span");
      chip.className = "bus-chip bus-chip-" + msg.msg_type;
      chip.textContent = msg.msg_type;
      header.appendChild(chip);
    }

    header.appendChild(timeEl);

    const content = document.createElement("div");
    content.className = "bus-msg-content";
    content.textContent = msg.content || "";  // NEVER innerHTML for content

    li.appendChild(header);
    li.appendChild(content);

    const wasAtBottom = isAtBottom();
    feed.appendChild(li);

    // Enforce DOM cap
    while (feed.children.length > DOM_CAP) {
      feed.removeChild(feed.firstChild);
    }

    if (wasAtBottom) {
      stuckToBottom = true;
      scrollToBottom();
    } else {
      stuckToBottom = false;
      showPill();
    }
  }

  // ===== Scroll helpers =====
  function isAtBottom() {
    if (!feed) return true;
    return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  }

  function scrollToBottom() {
    if (feed) feed.scrollTop = feed.scrollHeight;
    hidePill();
    stuckToBottom = true;
  }

  function ensurePill() {
    if (pillEl) return;
    pillEl = document.createElement("button");
    pillEl.className = "bus-live-pill";
    pillEl.textContent = "↓ live";
    pillEl.addEventListener("click", scrollToBottom);
    // Append to the positioned wrapper that contains the feed
    const wrap = feed && feed.parentElement;
    if (wrap) wrap.appendChild(pillEl);
  }

  function showPill() {
    ensurePill();
    if (pillEl) pillEl.style.display = "";
  }

  function hidePill() {
    if (pillEl) pillEl.style.display = "none";
  }

  function onFeedScroll() {
    if (isAtBottom()) {
      stuckToBottom = true;
      hidePill();
    } else {
      stuckToBottom = false;
    }
  }

  // ===== Reconnect backoff =====
  // code 4401: retry every 10s flat (user may log in)
  // other: 1s, 2s, 5s, max 10s
  function scheduleReconnect(code) {
    if (reconnectTimer) return;
    let delay;
    if (code === 4401) {
      delay = 10000;
    } else {
      const steps = [1000, 2000, 5000, 10000];
      delay = steps[Math.min(reconnectAttempt, steps.length - 1)];
    }
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  // ===== Tab visibility =====
  // A backgrounded dashboard tab still holds a live WebSocket. The server pushes a
  // bus_status/keepalive frame for every live socket once per BUS_POLL_INTERVAL,
  // so a hidden tab costs the server a poll loop for nothing. Intentionally close
  // the socket while hidden and reconnect when visible again. This path must not
  // be confused with a genuine dropped connection: it must not schedule reconnects
  // while hidden and must not inflate the reconnect backoff.
  function pauseForHidden() {
    hiddenPaused = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (ws) {
      try { ws.close(); } catch (e) {}
    }
    setDot("amber", "paused — tab hidden");
  }

  function resumeFromHidden() {
    hiddenPaused = false;
    reconnectAttempt = 0;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    connect();
  }

  // ===== WebSocket connection =====
  function connect() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

    setDot("amber", "connecting…");
    lastBusStatusOk = null;

    const proto = location.protocol === "https:" ? "wss" : "ws";
    let url = proto + "://" + location.host + "/api/bus";
    if (desiredChannel) url += "?channel=" + encodeURIComponent(desiredChannel);

    try {
      ws = new WebSocket(url);
    } catch (e) {
      setDot("amber", "reconnecting…");
      scheduleReconnect(0);
      return;
    }

    // Stale-socket guard: a socket closed by the hide path can deliver its `close`
    // event after a later `connect()` has installed a replacement (the closing
    // socket's readyState is CLOSING, not CONNECTING/OPEN, so connect()'s own guard
    // does not stop it). Without this check that late event sets `ws = null` on the
    // live socket, orphaning it and leaving the module thinking it is disconnected.
    const sock = ws;

    ws.addEventListener("open", function () {
      if (ws !== sock) return;
      reconnectAttempt = 0;
      // Dot will go green when we receive a bus_status ok:true
      setDot("amber", "connected, awaiting bus status…");
    });

    ws.addEventListener("message", function (ev) {
      if (ws !== sock) return;
      let frame;
      try { frame = JSON.parse(ev.data); } catch (e) { return; }
      handleFrame(frame);
    });

    ws.addEventListener("close", function (ev) {
      if (ws !== sock) return;
      ws = null;
      if (hiddenPaused || document.hidden) {
        hiddenPaused = false;
        setDot("amber", "paused — tab hidden");
        return;
      }
      if (ev.code === 4401) {
        setDot("amber", "not authenticated, retrying in 10s…");
      } else {
        setDot("amber", "disconnected, reconnecting…");
      }
      reconnectAttempt++;
      if (reconnectAttempt >= 1) {
        console.warn("[bus] disconnected (code " + ev.code + "), reconnecting (attempt " + reconnectAttempt + ")");
      }
      scheduleReconnect(ev.code);
    });

    ws.addEventListener("error", function () {
      if (ws !== sock) return;
      // close will fire after error; dot update happens there
    });
  }

  // ===== Frame handler =====
  function handleFrame(frame) {
    switch (frame.type) {
      case "bus_status":
        lastBusStatusOk = frame.ok;
        if (!frame.ok && frame.reason === "bus not configured") {
          setDot("grey", "bus not configured");
        } else if (!frame.ok) {
          setDot("red", "bus outage: " + (frame.reason || "unknown"));
        } else {
          setDot("green", "bus connected");
        }
        break;

      case "channel":
        clearFeed();
        if (chanEl) chanEl.textContent = " " + (frame.name || frame.channel || "");
        desiredChannel = frame.channel || frame.name || desiredChannel;
        break;

      case "backfill":
        clearFeed();
        if (Array.isArray(frame.messages)) {
          frame.messages.forEach(renderMessage);
        }
        // After backfill, snap to bottom
        scrollToBottom();
        break;

      case "bus_message":
        renderMessage(frame);
        break;

      case "ping":
        // keepalive — ignore
        break;
    }
  }

  // ===== analysis-started event =====
  window.addEventListener("analysis-started", function (ev) {
    const channel = "analysis-" + ev.detail;
    desiredChannel = channel;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ channel: channel })); } catch (e) {}
    }
    // If not open, desiredChannel is set and will be used on next connect
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { pauseForHidden(); } else { resumeFromHidden(); }
  });

  // ===== Boot =====
  document.addEventListener("DOMContentLoaded", function () {
    initDom();
    if (feed) feed.addEventListener("scroll", onFeedScroll);
    connect();
  });

}());
