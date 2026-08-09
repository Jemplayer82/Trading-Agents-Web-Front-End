"""Regression test for scripts/redeploy.py's image derivation — proves the
new payload-driven logic produces the same image list the old hardcoded
IMAGES tuple did for a standard :latest render, and correctly handles
non-":latest" tags as generic tag-aware parsing.

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


def _load_recreate_helpers():
    source = _SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SCRIPT))
    wanted = (
        "_repo_tag_from_image",
        "_build_latest_ids",
        "_container_wants_image_id",
    )
    segments = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            segments[node.name] = ast.get_source_segment(source, node)
    func_src = "\n\n".join(segments[name] for name in wanted)
    namespace = {}
    exec(compile(func_src, str(_SCRIPT), "exec"), namespace)
    return (
        namespace["_repo_tag_from_image"],
        namespace["_build_latest_ids"],
        namespace["_container_wants_image_id"],
    )


def _load_blocking_scan():
    source = _SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_SCRIPT))
    func_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_blocking_scan"
    )
    func_src = ast.get_source_segment(source, func_node)
    namespace = {}
    exec(compile(func_src, str(_SCRIPT), "exec"), namespace)
    return namespace["_blocking_scan"]


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


def test_picks_up_non_latest_tags():
    """Tag values like "tier1" are just example non-":latest" data — this
    exercises generic tag parsing, not a wired tier-deploy code path.
    """
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


def test_same_repo_two_tags_do_not_collide_in_latest_ids():
    repo_tag_from_image, build_latest_ids, container_wants_image_id = _load_recreate_helpers()
    repo = "ghcr.io/jemplayer82/tradingagents"
    images = [(repo, "latest"), (repo, "tier1")]
    ids = {
        (repo, "latest"): "sha256:latest111",
        (repo, "tier1"): "sha256:tier1111",
    }

    def fake_fetch(r, t):
        return {"Id": ids[(r, t)]}

    latest_ids = build_latest_ids(images, fake_fetch)

    # Both tags survive in the lookup: no bare-repo overwrite.
    assert latest_ids[(repo, "latest")] == ids[(repo, "latest")]
    assert latest_ids[(repo, "tier1")] == ids[(repo, "tier1")]

    # Container running tier1 resolves to tier1's id, not latest's id.
    tier1_container = {
        "Names": ["/ta-tier1"],
        "Image": f"{repo}:tier1",
        "ImageID": "sha256:oldtier111",
    }
    assert container_wants_image_id(tier1_container, latest_ids) == ids[(repo, "tier1")]
    assert container_wants_image_id(tier1_container, latest_ids) != ids[(repo, "latest")]

    # Container running latest resolves to latest's id.
    latest_container = {
        "Names": ["/ta-latest"],
        "Image": f"{repo}:latest",
        "ImageID": "sha256:oldlatest1",
    }
    assert container_wants_image_id(latest_container, latest_ids) == ids[(repo, "latest")]

    # A container whose tag is not in the pulled set is not matched at all.
    unknown_container = {
        "Names": ["/ta-unknown"],
        "Image": f"{repo}:experimental",
        "ImageID": "sha256:experimental",
    }
    assert container_wants_image_id(unknown_container, latest_ids) is None


def test_blocking_scan_prefers_running_over_waiting():
    blocking_scan = _load_blocking_scan()
    running = {
        "scan_type": "spy",
        "id": 1,
        "kind": "options",
        "trade_date": "2025-08-01",
        "created_at": "2025-08-01T07:00:00",
    }
    waiting = [{
        "scan_type": "spy",
        "id": 2,
        "kind": "options",
        "trade_date": "2025-08-01",
        "created_at": "2025-08-01T07:30:00",
        "status": "running_wait_market",
    }]
    result = blocking_scan({"running": running, "waiting": waiting})
    assert result is running


def test_blocking_scan_returns_first_waiting_when_running_none():
    blocking_scan = _load_blocking_scan()
    waiting = [{
        "scan_type": "spy",
        "id": 3,
        "kind": "options",
        "trade_date": "2025-08-01",
        "created_at": "2025-08-01T07:30:00",
        "status": "running_wait_market",
    }]
    result = blocking_scan({"running": None, "waiting": waiting})
    assert result == waiting[0]


def test_blocking_scan_returns_none_when_both_empty():
    blocking_scan = _load_blocking_scan()
    result = blocking_scan({"running": None, "waiting": []})
    assert result is None


def test_blocking_scan_returns_none_when_waiting_absent():
    blocking_scan = _load_blocking_scan()
    result = blocking_scan({"running": None})
    assert result is None
