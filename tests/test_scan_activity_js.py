"""JS unit tests for the scan-activity banner in ``web/static/portfolio.js``.

Verifies the rewritten ``pollScanActivity()`` hits the cheap single-row
``/api/portfolio/status`` endpoint and correctly renders portfolio/spy progress.
"""

import json

import pytest

from tests.jsvm import run_js


pytestmark = pytest.mark.unit


BOOTSTRAP = r"""
globalThis.$ = (id) => document.getElementById(id);
globalThis.__fetchPayload = {};
globalThis.fetch = function (url) {
  __fetches.push(url);
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async function () { return globalThis.__fetchPayload; }
  });
};
"""


def _run_poll(payload):
    return run_js(
        sources=["utils.js", "portfolio.js"],
        bootstrap=BOOTSTRAP,
        script=(
            "globalThis.__fetchPayload = " + json.dumps(payload) + ";\n"
            "return (async () => {\n"
            "  await pollScanActivity();\n"
            "  const box = $('scan-activity');\n"
            "  return {fetches: __fetches, hidden: box.hidden, html: box.innerHTML};\n"
            "})();"
        ),
    )


def test_polls_only_the_status_endpoint():
    result = _run_poll({"running": None, "queued": [], "waiting": []})
    assert result["fetches"] == ["/api/portfolio/status"]
    assert "/api/portfolio-scans" not in result["fetches"]
    assert "/api/spy-scans" not in result["fetches"]
    assert result["hidden"] is True
    assert result["html"] == ""


def test_renders_portfolio_progress():
    payload = {
        "running": {
            "scan_type": "portfolio",
            "kind": "equity",
            "status": "running",
            "id": 7,
            "scanned_count": 3,
            "scan_total": 10,
            "current_ticker": "NVDA",
            "quick_count": None,
            "quick_total": None,
            "deep_count": None,
            "deep_total": None,
        },
        "queued": [],
        "waiting": [],
    }
    result = _run_poll(payload)
    assert result["hidden"] is False
    assert "Portfolio scan" in result["html"]
    assert "3/10 analyzed" in result["html"]
    assert "NVDA" in result["html"]
    assert result["fetches"] == ["/api/portfolio/status"]


def test_renders_spy_progress():
    payload = {
        "running": {
            "scan_type": "spy",
            "kind": "equity",
            "status": "running_quick",
            "id": 8,
            "quick_count": 100,
            "quick_total": 500,
            "deep_count": 5,
            "deep_total": 50,
        },
        "queued": [],
        "waiting": [],
    }
    result = _run_poll(payload)
    assert result["hidden"] is False
    assert "Quick 100/500" in result["html"]
    assert "Deep 5/50" in result["html"]
    assert len(result["fetches"]) == 1


def test_waiting_spy_scan_still_renders():
    payload = {
        "running": None,
        "queued": [],
        "waiting": [
            {
                "scan_type": "spy",
                "id": 5,
                "status": "running_wait_market",
                "quick_count": 500,
                "quick_total": 500,
                "deep_count": 50,
                "deep_total": 50,
            }
        ],
    }
    result = _run_poll(payload)
    assert result["hidden"] is False
    assert "Quick 500/500" in result["html"]


def test_options_run_does_not_render_spy_banner():
    payload = {
        "running": {
            "scan_type": "spy",
            "kind": "options",
            "status": "running_quick",
            "id": 9,
            "quick_count": 100,
            "quick_total": 500,
            "deep_count": 5,
            "deep_total": 50,
        },
        "queued": [],
        "waiting": [],
    }
    result = _run_poll(payload)
    assert result["hidden"] is True
    assert result["html"] == ""


def test_pending_scan_does_not_render():
    payload = {
        "running": {
            "scan_type": "portfolio",
            "kind": "equity",
            "status": "pending",
            "id": 10,
            "scanned_count": 0,
            "scan_total": 0,
        },
        "queued": [],
        "waiting": [],
    }
    result = _run_poll(payload)
    assert result["hidden"] is True
    assert result["html"] == ""


def test_fetch_failure_hides_banner():
    result = run_js(
        sources=["utils.js", "portfolio.js"],
        bootstrap=BOOTSTRAP,
        script=(
            "globalThis.fetch = function (url) { __fetches.push(url); "
            "return Promise.resolve({ok: false, status: 502}); };\n"
            "return (async () => {\n"
            "  await pollScanActivity();\n"
            "  const box = $('scan-activity');\n"
            "  return {fetches: __fetches, hidden: box.hidden, html: box.innerHTML};\n"
            "})();"
        ),
    )
    assert result["hidden"] is True
    assert result["html"] == ""
