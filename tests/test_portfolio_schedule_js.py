"""Browser-side nightly portfolio-scan schedule tests.

These tests exercise ``loadNightlyScanTime()`` and ``saveNightlyScanTime()``
from ``web/static/portfolio.js`` using the Node.js vm harness in
``tests/jsvm.py``.
"""

import json

import pytest

from tests.jsvm import run_js

pytestmark = pytest.mark.unit


BOOTSTRAP = """
globalThis.__calls = [];
globalThis.__settingsResponse = {registry: [], custom: []};
globalThis.__settingsReject = false;
globalThis.__putOk = true;
globalThis.__putStatus = 200;
globalThis.fetch = function (url, options) {
    __calls.push({ url: url, options: options });
    if (url === "/api/settings") {
        if (globalThis.__settingsReject) {
            return Promise.reject(new Error("settings outage"));
        }
        return Promise.resolve({
            ok: true,
            status: 200,
            json: function () { return Promise.resolve(globalThis.__settingsResponse); }
        });
    }
    if (url === "/api/settings/SCHEDULE_NIGHTLY_SCAN_TIME") {
        return Promise.resolve({
            ok: globalThis.__putOk,
            status: globalThis.__putStatus,
            json: function () { return Promise.resolve({status: "saved"}); }
        });
    }
    return Promise.reject(new Error("unexpected fetch url: " + url));
};
"""


SETTINGS_PAYLOAD = {
    "registry": [
        {"key": "SCHEDULE_NIGHTLY_SCAN_TIME", "masked": "06:45", "has_value": True}
    ],
    "custom": [],
}


def test_loads_existing_nightly_time_and_updates_caption():
    script = (
        "globalThis.__settingsResponse = " + json.dumps(SETTINGS_PAYLOAD) + ";\n"
        "return (async () => {\n"
        "    await loadNightlyScanTime();\n"
        "    return {\n"
        "        timeValue: document.getElementById('pf-nightly-time').value,\n"
        "        caption: document.getElementById('pf-nightly-caption').textContent,\n"
        "    };\n"
        "})();"
    )
    result = run_js(sources=["utils.js", "portfolio.js"], bootstrap=BOOTSTRAP, script=script)
    assert result["timeValue"] == "06:45"
    assert "06:45 ET" in result["caption"]


def test_absent_key_leaves_caption_unchanged():
    script = (
        "globalThis.__settingsResponse = " + json.dumps({"registry": [], "custom": []}) + ";\n"
        "return (async () => {\n"
        "    const cap = document.getElementById('pf-nightly-caption');\n"
        "    cap.textContent = 'initial caption';\n"
        "    await loadNightlyScanTime();\n"
        "    return { caption: cap.textContent, calls: __calls.length };\n"
        "})();"
    )
    result = run_js(sources=["utils.js", "portfolio.js"], bootstrap=BOOTSTRAP, script=script)
    assert result["caption"] == "initial caption"
    assert result["calls"] == 1


def test_save_valid_time_issues_one_put_and_updates_caption():
    script = (
        "return (async () => {\n"
        "    const inp = document.getElementById('pf-nightly-time');\n"
        "    inp.value = '07:15';\n"
        "    await saveNightlyScanTime();\n"
        "    const call = __calls[0];\n"
        "    const body = call && call.options && call.options.body ? JSON.parse(call.options.body) : null;\n"
        "    return {\n"
        "        calls: __calls.length,\n"
        "        url: call ? call.url : null,\n"
        "        body: body,\n"
        "        caption: document.getElementById('pf-nightly-caption').textContent,\n"
        "    };\n"
        "})();"
    )
    result = run_js(sources=["utils.js", "portfolio.js"], bootstrap=BOOTSTRAP, script=script)
    assert result["calls"] == 1
    assert result["url"] == "/api/settings/SCHEDULE_NIGHTLY_SCAN_TIME"
    assert result["body"] == {"value": "07:15"}
    assert "07:15" in result["caption"]


def test_save_blank_input_hints_and_skips_api():
    script = (
        "return (async () => {\n"
        "    const inp = document.getElementById('pf-nightly-time');\n"
        "    inp.value = '';\n"
        "    const status = document.getElementById('pf-nightly-status');\n"
        "    status.textContent = '';\n"
        "    await saveNightlyScanTime();\n"
        "    return { calls: __calls.length, status: status.textContent };\n"
        "})();"
    )
    result = run_js(sources=["utils.js", "portfolio.js"], bootstrap=BOOTSTRAP, script=script)
    assert result["calls"] == 0
    assert "HH:MM" in result["status"]


def test_save_malformed_input_hints_and_skips_api():
    script = (
        "return (async () => {\n"
        "    const inp = document.getElementById('pf-nightly-time');\n"
        "    inp.value = '7:15';\n"
        "    const status = document.getElementById('pf-nightly-status');\n"
        "    status.textContent = '';\n"
        "    await saveNightlyScanTime();\n"
        "    return { calls: __calls.length, status: status.textContent };\n"
        "})();"
    )
    result = run_js(sources=["utils.js", "portfolio.js"], bootstrap=BOOTSTRAP, script=script)
    assert result["calls"] == 0
    assert "HH:MM" in result["status"]


def test_load_handles_settings_fetch_failure():
    script = (
        "globalThis.__settingsReject = true;\n"
        "return (async () => {\n"
        "    let threw = false;\n"
        "    try {\n"
        "        await loadNightlyScanTime();\n"
        "    } catch (e) {\n"
        "        threw = true;\n"
        "    }\n"
        "    return { threw: threw, calls: __calls.length };\n"
        "})();"
    )
    result = run_js(sources=["utils.js", "portfolio.js"], bootstrap=BOOTSTRAP, script=script)
    assert result["threw"] is False
    assert result["calls"] == 1
