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
