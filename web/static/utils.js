// TradingAgents Web — shared frontend utilities.
//
// This file is loaded FIRST in index.html, before auth.js / app.js / portfolio.js
// / spy.js / credentials.js. Those are plain classic <script defer> tags, NOT ES
// modules, so every top-level `const`/`function` declared here lives in one shared
// global lexical scope that the other files can see directly (no import needed).
//
// Two consequences worth knowing before you edit:
//   1. Load order matters. `defer` scripts run in document order, so utils.js must
//      stay the first /static/*.js tag — otherwise these globals won't exist yet
//      when the other modules run their DOMContentLoaded handlers.
//   2. The other modules must NOT redeclare these names at top level. A second
//      top-level `const $` in the same shared scope throws
//      "Identifier '$' has already been declared". (That global-scope collision is
//      historically why the modules used divergent aliases like `$$p` / `$$spy`.)
//
// Shared globals defined here: $, escapeHtml, renderMarkdown, fmtTs, apiFetch,
// progressBar.
//
// Escape-at-render discipline (project-wide): every interpolation of server- or
// LLM-sourced data into an HTML string must pass through escapeHtml(); LLM-authored
// markdown is rendered ONLY via renderMarkdown() (marked + DOMPurify). Assigning
// unescaped data to innerHTML is the known-bad pattern these helpers exist to stop.

/** Shorthand for document.getElementById. */
const $ = (id) => document.getElementById(id);

/**
 * Escape a string for safe insertion into HTML text or attribute contexts.
 * Handles the five HTML-significant characters (& < > " '); escaping the quotes
 * as well as the angle brackets makes the result safe inside attribute values,
 * not just text nodes. Returns "" for null/undefined.
 */
function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Render markdown to sanitized HTML for assignment to innerHTML.
 *
 * Reports and Q&A answers are authored by the LLM, not the end user, but we still
 * run the marked.js output through DOMPurify so a model that emits raw HTML (e.g.
 * an <img onerror=...> tag) can't inject script into the page. Falls back to an
 * escaped <pre> block if marked.js failed to load from the CDN.
 */
function renderMarkdown(md) {
  if (window.marked) {
    const html = window.marked.parse(md);
    return window.DOMPurify ? window.DOMPurify.sanitize(html) : html;
  }
  return `<pre>${escapeHtml(md)}</pre>`;
}

/**
 * Format an ISO timestamp as "YYYY-MM-DD HH:MM" in the browser's local time.
 * Returns "—" for empty input and echoes the raw value back if it can't be parsed.
 */
function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/**
 * Fetch JSON from a same-origin API endpoint, throwing on a non-2xx status so the
 * caller's catch block can surface the error. Use this for the common
 * "GET → JSON, report failures via catch" pattern; call fetch() directly when an
 * endpoint needs bespoke handling (e.g. reading resp.text() on error, or treating
 * an application-level {error: ...} field in a 200 response).
 */
async function apiFetch(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return resp.json();
}

/**
 * Build the track+fill element for a "[ Progress ]" bar. The portfolio scan and the
 * S&P 500 scan both render this exact markup; callers own the surrounding panel and
 * the "N/total" label line (those differ per scan type). Width is the count/total
 * ratio as a whole percent, 0% when total is 0.
 */
function progressBar(count, total) {
  const pct = total > 0 ? Math.round((count / total) * 100) : 0;
  return `<div class="scan-progress"><div class="scan-progress-bar" style="width:${pct}%"></div></div>`;
}

/**
 * Shared scan-queue rendering for every tab's sidebar. The queue is one global
 * FIFO (only one scan runs at a time), served by /api/portfolio/status as
 * { running, queued: [] }. Each item carries scan_type ('portfolio'|'spy') and
 * kind ('equity'|'options'); scanTypeKey collapses those to a single label key
 * so a tab can filter to just its own runs — or, on Run Analysis, show them all.
 */
const SCAN_TYPE_TAG = { portfolio: "pf", spy: "spy", options: "opt" };

function scanTypeKey(item) {
  if (!item) return "";
  if (item.scan_type === "portfolio") return "portfolio";
  if (item.scan_type === "spy") return item.kind === "options" ? "options" : "spy";
  return item.scan_type || "";
}

/**
 * Render a queue list into `ul` from a /api/portfolio/status payload.
 *   opts.only  — array of type keys to include (e.g. ["spy"]); omit for all.
 *   opts.onOpen(item) — click handler for the RUNNING item (optional).
 */
function renderScanQueue(ul, data, opts) {
  if (!ul) return;
  opts = opts || {};
  const only = opts.only || null;
  const running = data && data.running ? [data.running] : [];
  const queued = (data && data.queued) || [];
  let items = [...running, ...queued];
  if (only) items = items.filter((it) => only.includes(scanTypeKey(it)));
  ul.innerHTML = "";
  if (!items.length) {
    ul.innerHTML = '<li class="dim empty">(queue empty)</li>';
    return;
  }
  const runningShown = data && data.running && (!only || only.includes(scanTypeKey(data.running))) ? 1 : 0;
  items.forEach((item, idx) => {
    const li = document.createElement("li");
    li.dataset.id = item.id;
    const isRunning = data && item === data.running;
    const label = isRunning ? "RUNNING" : ("#" + (idx - runningShown + 1) + " IN QUEUE");
    const badgeClass = isRunning ? "HOLD" : "QUEUED";
    const tag = SCAN_TYPE_TAG[scanTypeKey(item)] || "scan";
    li.innerHTML =
      '<span class="h-main">' +
        '<span class="h-top">' +
          '<span class="h-tk">' + tag + " #" + item.id + " · " + escapeHtml(item.trade_date || "") + "</span>" +
          '<span class="h-sig ' + badgeClass + '">' + label + "</span>" +
        "</span>" +
        '<span class="h-ts">' + fmtTs(item.created_at) + "</span>" +
      "</span>";
    if (isRunning && opts.onOpen) {
      li.querySelector(".h-main").addEventListener("click", () => opts.onOpen(item));
    }
    ul.appendChild(li);
  });
}
