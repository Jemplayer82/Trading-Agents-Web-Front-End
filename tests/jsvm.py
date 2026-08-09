"""Node.js vm-based harness for unit-testing the browser JavaScript in ``web/static/*.js``.

``run_js`` executes selected source files in one shared VM context with a
minimal stub DOM/clock/WebSocket so classic deferred-script JS can be tested
from pytest without a browser or any npm dependencies.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

# Repo root is two levels above tests/jsvm.py
_REPO_ROOT = Path(__file__).resolve().parents[1]
_STATIC_DIR = _REPO_ROOT / "web" / "static"


# Driver executed by Node.  It is generated fresh per call so the harness needs
# no files under web/ and no npm/package.json artifacts.
_DRIVER_MJS = r'''
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const payloadPath = process.argv[2];
const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));

const PRELUDE = `
(function () {
  "use strict";

  const __logs = [];
  globalThis.__logs = __logs;

  function fmt(args) {
    return args.map((a) => {
      if (a === null || a === undefined) return String(a);
      if (typeof a === 'object') {
        try { return JSON.stringify(a); } catch (e) { return String(a); }
      }
      return String(a);
    }).join(' ');
  }

  globalThis.console = {
    log:   (...args) => { const s = fmt(args); __logs.push(['log',   s]); process.stderr.write(s + '\\n'); },
    info:  (...args) => { const s = fmt(args); __logs.push(['info',  s]); process.stderr.write(s + '\\n'); },
    warn:  (...args) => { const s = fmt(args); __logs.push(['warn',  s]); process.stderr.write('WARN ' + s + '\\n'); },
    error: (...args) => { const s = fmt(args); __logs.push(['error', s]); process.stderr.write('ERROR ' + s + '\\n'); },
    debug: (...args) => { const s = fmt(args); __logs.push(['debug', s]); process.stderr.write('DEBUG ' + s + '\\n'); },
  };

  function makeFakeElement(tag = 'div', id = '') {
    const listeners = {};
    const el = {
      tagName: String(tag).toUpperCase(),
      id: id,
      className: '',
      innerHTML: '',
      textContent: '',
      title: '',
      hidden: false,
      dataset: {},
      style: {},
      children: [],
      parentElement: null,
      scrollHeight: 0,
      scrollTop: 0,
      clientHeight: 0,
      addEventListener(type, fn) { listeners[type] = (listeners[type] || []); listeners[type].push(fn); },
      appendChild(child) { el.children.push(child); child.parentElement = el; return child; },
      removeChild(child) {
        const idx = el.children.indexOf(child);
        if (idx >= 0) { el.children.splice(idx, 1); child.parentElement = null; }
        return child;
      },
      replaceChildren(...newChildren) {
        for (const c of el.children) c.parentElement = null;
        el.children.length = 0;
        for (const c of newChildren) { el.children.push(c); c.parentElement = el; }
      },
      closest(sel) { return null; },
      classList: {
        add(...cls) {},
        remove(...cls) {},
        toggle(cls, force) { return !!force; },
      },
      __listeners: listeners,
      __fire(type, ev) { (listeners[type] || []).forEach((fn) => fn(ev)); },
    };
    Object.defineProperty(el, 'firstChild', { get() { return el.children[0] || null; } });
    Object.defineProperty(el, 'lastChild',  { get() { return el.children[el.children.length - 1] || null; } });
    return el;
  }

  const byId = new Map();
  const docListeners = {};
  const document = {
    hidden: false,
    body: makeFakeElement('body'),
    createElement(tag) { return makeFakeElement(tag); },
    getElementById(id) {
      if (!byId.has(id)) byId.set(id, makeFakeElement('div', id));
      return byId.get(id);
    },
    querySelector(sel) { return null; },
    querySelectorAll(sel) { return []; },
    addEventListener(type, fn) { docListeners[type] = (docListeners[type] || []); docListeners[type].push(fn); },
    removeEventListener(type, fn) {},
    __listeners: docListeners,
    __fire(type, ev) { (docListeners[type] || []).forEach((fn) => fn(ev)); },
  };
  globalThis.document = document;
  globalThis.addEventListener = document.addEventListener.bind(document);
  globalThis.removeEventListener = document.removeEventListener.bind(document);
  globalThis.__fire = document.__fire.bind(document);

  globalThis.CustomEvent = function CustomEvent(type, opts) {
    opts = opts || {};
    return { type: type, detail: opts.detail };
  };

  globalThis.location = { protocol: 'http:', host: 'localhost:8000' };

  let now = 0;
  let timerId = 1;
  const timers = new Map();

  globalThis.setTimeout = function (fn, ms) {
    const id = timerId++;
    timers.set(id, { id: id, fn: fn, at: now + Math.max(0, ms | 0), repeat: null });
    return id;
  };
  globalThis.clearTimeout = function (id) { timers.delete(id); };
  globalThis.setInterval = function (fn, ms) {
    const id = timerId++;
    timers.set(id, { id: id, fn: fn, at: now, repeat: Math.max(0, ms | 0) });
    return id;
  };
  globalThis.clearInterval = function (id) {};

  globalThis.__advance = function (ms) {
    now += Math.max(0, ms | 0);
    const due = [];
    for (const t of timers.values()) {
      if (t.repeat === null && t.at <= now) due.push(t);
    }
    due.sort((a, b) => a.at - b.at);
    for (const t of due) {
      if (timers.has(t.id)) {
        timers.delete(t.id);
        try { t.fn(); } catch (e) { console.error('setTimeout callback error:', e); }
      }
    }
  };

  const sockets = [];
  globalThis.__sockets = sockets;

  globalThis.WebSocket = class WebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    constructor(url) {
      this.url = url;
      this.readyState = WebSocket.OPEN;
      this.closeCalls = [];
      this.sent = [];
      this.__listeners = {};
      sockets.push(this);
    }
    addEventListener(type, fn) { this.__listeners[type] = (this.__listeners[type] || []); this.__listeners[type].push(fn); }
    send(data) { this.sent.push(data); }
    close(code) { this.closeCalls.push(code); this.readyState = WebSocket.CLOSED; }
    __fire(type, ev) { (this.__listeners[type] || []).forEach((fn) => fn(ev)); }
  };

  const __fetches = [];
  globalThis.__fetches = __fetches;
  globalThis.fetch = function (...args) {
    __fetches.push(args);
    throw new Error('no fetch stub installed');
  };

  globalThis.window = globalThis;
})();
`;

const sandbox = { process };
const ctx = vm.createContext(sandbox);

vm.runInContext(PRELUDE, ctx, { filename: '<prelude>' });

if (payload.bootstrap) {
  vm.runInContext(payload.bootstrap, ctx, { filename: '<bootstrap>' });
}

for (const src of payload.sources) {
  const filePath = path.join(payload.staticDir, src);
  const code = fs.readFileSync(filePath, 'utf8');
  vm.runInContext(code, ctx, { filename: src });
}

(async () => {
  try {
    const wrapped = '(function () { ' + payload.script + ' })()';
    let value = vm.runInContext(wrapped, ctx, { filename: '<script>' });
    if (value && typeof value.then === 'function') {
      value = await Promise.resolve(value);
    }
    value = value ?? null;
    process.stdout.write('<<<JSON>>>' + JSON.stringify(value));
  } catch (err) {
    console.error(err && err.stack ? err.stack : err);
    process.exit(1);
  }
})();
'''


def run_js(
    sources: list[str],
    script: str,
    *,
    bootstrap: str = "",
    timeout: float = 30.0,
) -> Any:
    """Execute browser JS sources in a Node ``vm`` context with a stub DOM.

    ``sources`` are loaded from ``<repo>/web/static`` in order into one shared
    global scope, just like classic ``<script defer>`` tags.  ``bootstrap`` is
    evaluated first (use it to install fakes like ``fetch``).  ``script`` is
    evaluated last and its completion value is JSON-serialized back to Python.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed — JS unit tests skipped")

    payload = {
        "staticDir": str(_STATIC_DIR),
        "sources": sources,
        "bootstrap": bootstrap,
        "script": script,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        payload_path = tmp / "payload.json"
        driver_path = tmp / "driver.mjs"

        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        driver_path.write_text(_DRIVER_MJS, encoding="utf-8")

        proc = subprocess.run(
            [node, str(driver_path), str(payload_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    if proc.returncode != 0 or "<<<JSON>>>" not in proc.stdout:
        raise AssertionError(
            f"JSVM exited {proc.returncode} and did not emit result sentinel.\n"
            f"--- STDOUT ---\n{proc.stdout}\n"
            f"--- STDERR ---\n{proc.stderr}"
        )

    try:
        return json.loads(proc.stdout.split("<<<JSON>>>", 1)[1])
    except Exception as exc:
        raise AssertionError(
            f"JSVM result parse error: {exc}\n"
            f"--- STDOUT ---\n{proc.stdout}\n"
            f"--- STDERR ---\n{proc.stderr}"
        ) from exc
