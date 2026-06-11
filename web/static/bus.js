// TradingAgents Web — Agent Bus live feed

(function () {
  "use strict";

  // ===== Agent display name map =====
  var AGENT_NAMES = {
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
    "langgraph-orchestrator":"Orchestrator",
  };

  // ===== State =====
  var ws = null;
  var desiredChannel = null;   // channel to join on connect/reconnect
  var reconnectAttempt = 0;
  var reconnectTimer = null;
  var busConfigured = true;    // flips false when server says "bus not configured"
  var lastBusStatusOk = null;  // null = unknown, true/false from bus_status frames
  var DOM_CAP = 300;

  var feed     = null;
  var dotEl    = null;
  var chanEl   = null;
  var pillEl   = null;         // "↓ live" pill (created on first need)

  // ===== Auto-scroll state =====
  var stuckToBottom = true;

  // ===== DOM helpers =====
  function $(id) { return document.getElementById(id); }

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
    var d = new Date(ms);
    var pad = function (n) { return String(n).padStart(2, "0"); };
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  // ===== Feed rendering =====
  function clearFeed() {
    if (feed) feed.innerHTML = "";
  }

  function renderMessage(msg) {
    if (!feed) return;
    var agentId = msg.agent || "";
    var displayName = AGENT_NAMES[agentId] || agentId;

    var li = document.createElement("li");
    li.className = "bus-msg";
    li.dataset.agent = agentId;

    var badge = document.createElement("span");
    badge.className = "bus-badge";
    badge.textContent = displayName;

    var timeEl = document.createElement("span");
    timeEl.className = "bus-time";
    timeEl.textContent = fmtTime(msg.ts);

    var header = document.createElement("div");
    header.className = "bus-msg-header";
    header.appendChild(badge);

    if (msg.msg_type === "result" || msg.msg_type === "instruction") {
      var chip = document.createElement("span");
      chip.className = "bus-chip bus-chip-" + msg.msg_type;
      chip.textContent = msg.msg_type;
      header.appendChild(chip);
    }

    header.appendChild(timeEl);

    var content = document.createElement("div");
    content.className = "bus-msg-content";
    content.textContent = msg.content || "";  // NEVER innerHTML for content

    li.appendChild(header);
    li.appendChild(content);

    var wasAtBottom = isAtBottom();
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
    // Insert after the feed's parent panel
    var panel = feed && feed.parentElement;
    if (panel) panel.appendChild(pillEl);
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
    var delay;
    if (code === 4401) {
      delay = 10000;
    } else {
      var steps = [1000, 2000, 5000, 10000];
      delay = steps[Math.min(reconnectAttempt, steps.length - 1)];
    }
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  // ===== WebSocket connection =====
  function connect() {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

    setDot("amber", "connecting…");
    lastBusStatusOk = null;

    var proto = location.protocol === "https:" ? "wss" : "ws";
    var url = proto + "://" + location.host + "/api/bus";
    if (desiredChannel) url += "?channel=" + encodeURIComponent(desiredChannel);

    try {
      ws = new WebSocket(url);
    } catch (e) {
      setDot("amber", "reconnecting…");
      scheduleReconnect(0);
      return;
    }

    ws.addEventListener("open", function () {
      reconnectAttempt = 0;
      // If we have a desired channel and didn't put it in the URL (e.g. reconnect
      // without constructing a fresh URL), send a switch frame.
      // Actually we always put it in the URL above, so this is belt-and-suspenders.
      if (desiredChannel) {
        try { ws.send(JSON.stringify({ channel: desiredChannel })); } catch (e) {}
      }
      // Dot will go green when we receive a bus_status ok:true
      setDot("amber", "connected, awaiting bus status…");
    });

    ws.addEventListener("message", function (ev) {
      var frame;
      try { frame = JSON.parse(ev.data); } catch (e) { return; }
      handleFrame(frame);
    });

    ws.addEventListener("close", function (ev) {
      ws = null;
      if (ev.code === 4401) {
        setDot("amber", "not authenticated, retrying in 10s…");
      } else {
        setDot("amber", "disconnected, reconnecting…");
      }
      reconnectAttempt++;
      if (reconnectAttempt > 1) {
        console.warn("[bus] disconnected (code " + ev.code + "), reconnecting (attempt " + reconnectAttempt + ")");
      }
      scheduleReconnect(ev.code);
    });

    ws.addEventListener("error", function () {
      // close will fire after error; dot update happens there
    });
  }

  // ===== Frame handler =====
  function handleFrame(frame) {
    switch (frame.type) {
      case "bus_status":
        lastBusStatusOk = frame.ok;
        if (!frame.ok && frame.reason === "bus not configured") {
          busConfigured = false;
          setDot("grey", "bus not configured");
        } else if (!frame.ok) {
          busConfigured = true;
          setDot("red", "bus outage: " + (frame.reason || "unknown"));
        } else {
          busConfigured = true;
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
    var channel = "analysis-" + ev.detail;
    desiredChannel = channel;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ channel: channel })); } catch (e) {}
    }
    // If not open, desiredChannel is set and will be used on next connect
  });

  // ===== Boot =====
  document.addEventListener("DOMContentLoaded", function () {
    initDom();
    if (feed) feed.addEventListener("scroll", onFeedScroll);
    connect();
  });

}());
