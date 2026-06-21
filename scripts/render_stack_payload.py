"""Render the Portainer stack-67 deploy payload from the repo compose.

`docker-compose.yml` is the SINGLE SOURCE OF TRUTH (committed, `${VAR}`
placeholders only). This script resolves the real secret values from a
gitignored `.env` using `docker compose config` (the canonical interpolation
engine — handles `${VAR}`, `${VAR:-default}`, `${VAR:?err}`, empty strings, and
validation correctly) and writes the literal-baked payload Portainer expects:

    {"StackFileContent": "<resolved compose>", "Env": [],
     "Prune": false, "PullImage": true}

The payload is a build artifact — NEVER hand-edit it, and never commit it or the
`.env`. To deploy after rendering, PUT the payload to
`/api/stacks/67?endpointId=3` (see scripts/redeploy.py or the deploy memory).

The top-level `name:` that `docker compose config` injects is stripped so
Portainer owns the stack/project name (matching how the stack was created).

Usage:
    python scripts/render_stack_payload.py
    STACK_PAYLOAD_OUT=C:\\tmp\\stack67_payload.json python scripts/render_stack_payload.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
ENV_FILE = ROOT / ".env"
DEFAULT_OUT = os.environ.get("STACK_PAYLOAD_OUT", r"C:\tmp\stack67_payload.json")


def render_compose() -> str:
    """Resolve docker-compose.yml against .env into a literal compose string."""
    if not ENV_FILE.exists():
        sys.exit(f"render: {ENV_FILE} not found — create it (gitignored secret source).")
    cmd = ["docker", "compose", "-f", str(COMPOSE), "--env-file", str(ENV_FILE), "config"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"render: `docker compose config` failed:\n{proc.stderr.strip()}")
    doc = yaml.safe_load(proc.stdout)
    doc.pop("name", None)  # Portainer owns the project/stack name
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False, width=4096)


def main() -> None:
    rendered = render_compose()
    leftover = sorted(set(re.findall(r"\$\{[^}]+\}", rendered)))
    if leftover:
        sys.exit(f"render: unresolved placeholders remain: {leftover}")
    payload = {
        "StackFileContent": rendered,
        "Env": [],            # secrets are inline literals in StackFileContent
        "Prune": False,       # never delete foreign services on update
        "PullImage": True,
    }
    out = Path(DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    n_services = len(yaml.safe_load(rendered).get("services", {}))
    print(f"wrote {out} ({n_services} services, {len(rendered)} chars of compose)")


if __name__ == "__main__":
    main()
