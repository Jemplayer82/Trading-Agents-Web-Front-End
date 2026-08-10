"""Browser-side S&P 500 paper-account form tests.

These tests exercise the account create/edit form helpers in
``web/static/spy.js`` using the Node.js vm harness in ``tests/jsvm.py``.
"""

import pytest

from tests.jsvm import run_js

pytestmark = pytest.mark.unit

# Fetch stub installed before the tested sources are loaded.  It records the
# account save request(s) and returns empty lists for the follow-up refreshes.
BOOTSTRAP = """
globalThis.__posts = [];
globalThis.alert = function () {};
globalThis.confirm = function () { return true; };
globalThis.renderScanQueue = function () {};
globalThis.fetch = function (url, options) {
    __posts.push([url, options]);
    return Promise.resolve({
        ok: true,
        status: 200,
        json: function () { return Promise.resolve({accounts: [], scans: []}); }
    });
};
"""


def _run(script):
    return run_js(
        sources=["utils.js", "spy.js"],
        bootstrap=BOOTSTRAP,
        script=script,
    )


def test_saves_new_account_with_schedule_and_stop():
    result = _run(
        """
        return (async () => {
            document.getElementById('new-acct-name').value = 'Test';
            document.getElementById('new-acct-capital').value = 100000;
            document.getElementById('new-acct-agg').value = 5;
            document.getElementById('new-acct-schedule').value = '09:15';
            document.getElementById('new-acct-stop-type').value = 'stop_limit';
            document.getElementById('new-acct-stop-value').value = '8';
            document.getElementById('new-acct-stop-offset').value = '1';
            await savePaperAccount();
            const posts = __posts.filter(p => p[0] === '/api/paper-accounts');
            const body = JSON.parse(posts[0][1].body);
            return { count: posts.length, body: body };
        })();
        """
    )
    assert result["count"] == 1
    body = result["body"]
    assert body["schedule_time"] == "09:15"
    assert body["stop_type"] == "stop_limit"
    assert body["stop_value"] == 8
    assert isinstance(body["stop_value"], float) or isinstance(body["stop_value"], int)
    assert body["stop_limit_offset"] == 1
    assert isinstance(body["stop_limit_offset"], float) or isinstance(body["stop_limit_offset"], int)


def test_blank_numerics_serialize_as_null_when_no_stop():
    result = _run(
        """
        return (async () => {
            document.getElementById('new-acct-name').value = 'Blank';
            document.getElementById('new-acct-capital').value = 100000;
            document.getElementById('new-acct-agg').value = 5;
            document.getElementById('new-acct-schedule').value = '';
            document.getElementById('new-acct-stop-type').value = 'none';
            document.getElementById('new-acct-stop-value').value = '';
            document.getElementById('new-acct-stop-offset').value = '';
            await savePaperAccount();
            const posts = __posts.filter(p => p[0] === '/api/paper-accounts');
            return JSON.parse(posts[0][1].body);
        })();
        """
    )
    assert result["stop_value"] is None
    assert result["stop_limit_offset"] is None


def test_blank_schedule_serializes_as_empty_string():
    result = _run(
        """
        return (async () => {
            document.getElementById('new-acct-name').value = 'NoSched';
            document.getElementById('new-acct-capital').value = 100000;
            document.getElementById('new-acct-agg').value = 5;
            document.getElementById('new-acct-schedule').value = '';
            document.getElementById('new-acct-stop-type').value = 'none';
            await savePaperAccount();
            const posts = __posts.filter(p => p[0] === '/api/paper-accounts');
            return JSON.parse(posts[0][1].body).schedule_time;
        })();
        """
    )
    assert result == ""


def test_populate_then_save_round_trip():
    result = _run(
        """
        return (async () => {
            populateAccountForm({
                name: 'Acct', starting_capital: 100000, aggressiveness: 5, bias: 'neutral',
                schedule_time: '09:15', stop_type: 'stop_limit', stop_value: 60, stop_limit_offset: 5
            });
            await savePaperAccount();
            const posts = __posts.filter(p => p[0] === '/api/paper-accounts');
            return JSON.parse(posts[0][1].body);
        })();
        """
    )
    assert result["schedule_time"] == "09:15"
    assert result["stop_type"] == "stop_limit"
    assert result["stop_value"] == 60
    assert result["stop_limit_offset"] == 5


def test_null_stop_value_renders_as_empty_input():
    result = _run(
        """
        populateAccountForm({
            name: 'X', starting_capital: 100000, aggressiveness: 5, bias: 'neutral',
            schedule_time: '', stop_type: 'none', stop_value: null, stop_limit_offset: null
        });
        return document.getElementById('new-acct-stop-value').value === '';
        """
    )
    assert result is True


def test_stop_field_visibility():
    result = _run(
        """
        function check(type) {
            document.getElementById('new-acct-stop-type').value = type;
            stopFieldVisibility('new-acct');
            return {
                valueHidden: document.getElementById('new-acct-stop-value-wrap').hidden,
                offsetHidden: document.getElementById('new-acct-stop-offset-wrap').hidden,
                label: document.getElementById('new-acct-stop-value-label').textContent
            };
        }
        return {
            none: check('none'),
            stop: check('stop'),
            stopLimit: check('stop_limit'),
            trailingDollar: check('trailing_dollar')
        };
        """
    )
    assert result["none"]["valueHidden"] is True
    assert result["none"]["offsetHidden"] is True

    assert result["stop"]["valueHidden"] is False
    assert result["stop"]["offsetHidden"] is True

    assert result["stopLimit"]["valueHidden"] is False
    assert result["stopLimit"]["offsetHidden"] is False

    assert result["trailingDollar"]["valueHidden"] is False
    assert result["trailingDollar"]["offsetHidden"] is True
    label = result["trailingDollar"]["label"]
    assert "$" in label or "Trail" in label


def test_reset_form_defaults():
    result = _run(
        """
        resetAccountForm();
        return {
            type: document.getElementById('new-acct-stop-type').value,
            value: document.getElementById('new-acct-stop-value').value,
            offset: document.getElementById('new-acct-stop-offset').value,
            schedule: document.getElementById('new-acct-schedule').value
        };
        """
    )
    assert result["type"] == "none"
    assert result["value"] == ""
    assert result["offset"] == ""
    assert result["schedule"] == "00:00"


def test_blank_stop_value_blocks_submission():
    result = _run(
        """
        return (async () => {
            document.getElementById('new-acct-name').value = 'Bad';
            document.getElementById('new-acct-capital').value = 100000;
            document.getElementById('new-acct-agg').value = 5;
            document.getElementById('new-acct-stop-type').value = 'stop';
            document.getElementById('new-acct-stop-value').value = '';
            await savePaperAccount();
            return __posts.length;
        })();
        """
    )
    assert result == 0