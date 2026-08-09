"""Smoke test for the Node-based browser-JS harness in ``tests/jsvm.py``.

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
