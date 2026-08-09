"""Smoke tests for the Node-based browser-JS harness in ``tests/jsvm.py``.

This file does not import from ``web/`` (which is tier-agnostic); it only
exercises ``web/static/bus.js`` inside the stub DOM/WebSocket VM provided by
``jsvm.run_js``.
"""

import pytest

from tests.jsvm import run_js

pytestmark = pytest.mark.unit


def test_bus_domcontentloaded_opens_websocket():
    """bus.js connects to /api/bus when its DOMContentLoaded handler fires."""
    result = run_js(
        sources=["bus.js"],
        bootstrap="globalThis.$ = (id) => document.getElementById(id);",
        script=(
            "document.__fire('DOMContentLoaded', {type: 'DOMContentLoaded'}); "
            "return {sockets: __sockets.length, url: __sockets[0] && __sockets[0].url};"
        ),
    )
    assert result == {"sockets": 1, "url": "ws://localhost:8000/api/bus"}


def test_hidden_closes_socket_and_opens_nothing():
    """A hidden tab closes its socket and does not schedule reconnects."""
    result = run_js(
        sources=["bus.js"],
        bootstrap="globalThis.$ = (id) => document.getElementById(id);",
        script=(
            "document.__fire('DOMContentLoaded', {type: 'DOMContentLoaded'}); "
            "document.hidden = true; "
            "document.__fire('visibilitychange', {type: 'visibilitychange'}); "
            "__advance(60000); "
            "return {closeCalls: __sockets[0].closeCalls.length, sockets: __sockets.length};"
        ),
    )
    assert result == {"closeCalls": 1, "sockets": 1}


def test_visible_again_reconnects():
    """Coming back to a hidden tab opens a fresh WebSocket."""
    result = run_js(
        sources=["bus.js"],
        bootstrap="globalThis.$ = (id) => document.getElementById(id);",
        script=(
            "document.__fire('DOMContentLoaded', {type: 'DOMContentLoaded'}); "
            "document.hidden = true; "
            "document.__fire('visibilitychange', {type: 'visibilitychange'}); "
            "__sockets[0].__fire('close', {code: 1000}); "
            "document.hidden = false; "
            "document.__fire('visibilitychange', {type: 'visibilitychange'}); "
            "return {sockets: __sockets.length, url1: __sockets[1] && __sockets[1].url};"
        ),
    )
    assert result == {
        "sockets": 2,
        "url1": "ws://localhost:8000/api/bus",
    }


def test_network_drop_while_visible_still_reconnects():
    """A genuine network blip while the tab is visible uses the normal backoff."""
    result = run_js(
        sources=["bus.js"],
        bootstrap="globalThis.$ = (id) => document.getElementById(id);",
        script=(
            "document.__fire('DOMContentLoaded', {type: 'DOMContentLoaded'}); "
            "__sockets[0].__fire('close', {code: 1006}); "
            "__advance(2000); "
            "return {sockets: __sockets.length};"
        ),
    )
    assert result == {"sockets": 2}


def test_hide_cancels_a_pending_reconnect():
    """Going hidden while a reconnect is pending cancels the pending reconnect."""
    result = run_js(
        sources=["bus.js"],
        bootstrap="globalThis.$ = (id) => document.getElementById(id);",
        script=(
            "document.__fire('DOMContentLoaded', {type: 'DOMContentLoaded'}); "
            "__sockets[0].__fire('close', {code: 1006}); "
            "document.hidden = true; "
            "document.__fire('visibilitychange', {type: 'visibilitychange'}); "
            "__advance(60000); "
            "return {sockets: __sockets.length, closeCalls: __sockets[0].closeCalls.length};"
        ),
    )
    assert result == {"sockets": 1, "closeCalls": 0}


def test_hidden_close_does_not_schedule_reconnect():
    """A hidden-tab socket close must not schedule a reconnect while hidden."""
    result = run_js(
        sources=["bus.js"],
        bootstrap="globalThis.$ = (id) => document.getElementById(id);",
        script=(
            "document.__fire('DOMContentLoaded', {type: 'DOMContentLoaded'}); "
            "document.hidden = true; "
            "document.__fire('visibilitychange', {type: 'visibilitychange'}); "
            "__sockets[0].__fire('close', {code: 1006}); "
            "__advance(60000); "
            "return {sockets: __sockets.length, closeCalls: __sockets[0].closeCalls.length};"
        ),
    )
    # The hidden close is intentional; it must not create a new socket or
    # reconnect timer while the tab is still hidden.
    assert result == {"sockets": 1, "closeCalls": 1}


def test_late_close_from_hidden_socket_does_not_orphan_the_live_one():
    """A delayed close event from the old hidden socket must not kill the new one."""
    result = run_js(
        sources=["bus.js"],
        bootstrap="globalThis.$ = (id) => document.getElementById(id);",
        script=(
            "document.__fire('DOMContentLoaded', {type: 'DOMContentLoaded'}); "
            "document.hidden = true; "
            "document.__fire('visibilitychange', {type: 'visibilitychange'}); "
            "document.hidden = false; "
            "document.__fire('visibilitychange', {type: 'visibilitychange'}); "
            "__sockets[0].__fire('close', {code: 1000}); "
            "__advance(60000); "
            "var afterLateClose = __sockets.length; "
            "__sockets[1].__fire('close', {code: 1006}); "
            "__advance(1000); "
            "var at1000 = __sockets.length; "
            "__advance(1000); "
            "return {afterLateClose: afterLateClose, at1000: at1000, sockets: __sockets.length};"
        ),
    )
    assert result == {
        "afterLateClose": 2,
        "at1000": 2,
        "sockets": 3,
    }
