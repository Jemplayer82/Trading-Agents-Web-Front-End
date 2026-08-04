"""Regression test for scripts/redeploy.py's image derivation — proves the
new payload-driven logic produces the same image list the old hardcoded
IMAGES tuple did for a standard :latest master/tier4 render, and is
tag-aware for tier renders.

redeploy.py has a real side effect at import time (TOKEN sys.exit check, at
module scope) so this test extracts and execs just the
`_images_from_payload` function's source via `ast`, rather than importing
the whole module.
"""
from __future__ import annotations

import ast
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "redeploy.py"


def _load_images_from_payload():
    source = _SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SCRIPT))
    func_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_images_from_payload"
    )
    func_src = ast.get_source_segment(source, func_node)
    namespace = {"yaml": yaml}
    exec(compile(func_src, str(_SCRIPT), "exec"), namespace)
    return namespace["_images_from_payload"]


def _payload(compose_yaml: str) -> dict:
    return {"StackFileContent": compose_yaml, "Env": [], "Prune": False, "PullImage": True}


def test_matches_legacy_hardcoded_tuple_for_latest_tagged_render():
    images_from_payload = _load_images_from_payload()
    # Mirrors the real docker-compose.yml's 8 services exactly (verified
    # live): 5 use the tradingagents image, 1 uses tradingagents-web, 2 are
    # unrelated third-party images that must be excluded.
    compose_yaml = """
services:
  tradingagents:
    image: ghcr.io/jemplayer82/tradingagents:latest
  tradingagents-api:
    image: ghcr.io/jemplayer82/tradingagents:latest
  tradingagents-portfolio:
    image: ghcr.io/jemplayer82/tradingagents:latest
  tradingagents-scheduler:
    image: ghcr.io/jemplayer82/tradingagents:latest
  tradingagents-web:
    image: ghcr.io/jemplayer82/tradingagents-web:latest
  switchboard:
    image: ghcr.io/jemplayer82/mcp-switchboard:latest
  tradingagents-llm-router:
    image: ghcr.io/jemplayer82/tradingagents:latest
  tradingagents-ollama:
    image: ollama/ollama:latest
"""
    result = images_from_payload(_payload(compose_yaml))
    legacy_hardcoded_tuple = (
        "ghcr.io/jemplayer82/tradingagents",
        "ghcr.io/jemplayer82/tradingagents-web",
    )
    assert result == [(repo, "latest") for repo in legacy_hardcoded_tuple]


def test_picks_up_non_latest_tier_tags():
    images_from_payload = _load_images_from_payload()
    compose_yaml = """
services:
  tradingagents:
    image: ghcr.io/jemplayer82/tradingagents:tier1
  tradingagents-web:
    image: ghcr.io/jemplayer82/tradingagents-web:tier1
  tradingagents-ollama:
    image: ollama/ollama:latest
"""
    result = images_from_payload(_payload(compose_yaml))
    assert result == [
        ("ghcr.io/jemplayer82/tradingagents", "tier1"),
        ("ghcr.io/jemplayer82/tradingagents-web", "tier1"),
    ]
