"""Out-of-band ops script: redeploy the tradingagents stack via Portainer.

Unlike a plain re-pull, this PUTs the *local* docker-compose.yml as the stack
file, so compose changes (new services like tradingagents-llm-router, new env
vars) actually take effect. It preserves the stack's existing Env[] verbatim so
secrets (SWITCHBOARD_MCP_TOKEN, OLLAMA_API_KEY, TOKEN_ENCRYPTION_KEY, ...) are
not wiped — the Portainer GET-redacts / PUT-wipes gotcha.

Flow:
  1. Pull both :latest images on the endpoint (anonymous; images are public).
  2. GET the stack (Env[]) — keep its secrets.
  3. PUT the stack with the LOCAL compose file + preserved Env[], prune=True.
  4. Recreate any container still on an older image id (a PUT alone often won't
     recreate just because :latest moved).
  5. Print container image ids so you can eyeball what actually rolled.

Credentials come from the environment — NEVER hardcode a Portainer token in a
committed file. Run e.g.:

    $env:PORTAINER_TOKEN="ptr_..."; python scripts/redeploy.py   # PowerShell
    PORTAINER_TOKEN=ptr_... python scripts/redeploy.py            # bash

Optional overrides: PORTAINER_URL, PORTAINER_ENDPOINT_ID, TRADINGAGENTS_STACK,
COMPOSE_FILE.
"""
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("PORTAINER_URL", "https://192.168.7.50:9443").rstrip("/")
TOKEN = os.environ.get("PORTAINER_TOKEN")  # pragma: allowlist secret
ENDPOINT_ID = int(os.environ.get("PORTAINER_ENDPOINT_ID", "3"))
STACK_NAME = os.environ.get("TRADINGAGENTS_STACK", "tradingagents")
COMPOSE_FILE = os.environ.get(
    "COMPOSE_FILE", str(Path(__file__).resolve().parent.parent / "docker-compose.yml")
)
IMAGES = (
    "ghcr.io/jemplayer82/tradingagents",
    "ghcr.io/jemplayer82/tradingagents-web",
)

if not TOKEN:
    sys.exit("PORTAINER_TOKEN is not set — export it and re-run (see module docstring).")

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def req(method, path, body=None, timeout=180):
    r = urllib.request.Request(
        BASE + path,
        method=method,
        headers={"X-API-Key": TOKEN, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(r, context=ctx, timeout=timeout) as resp:
        raw = resp.read()
        try:
            return json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return None


# 1. pull both images (public)
for image in IMAGES:
    req("POST", f"/api/endpoints/{ENDPOINT_ID}/docker/images/create?fromImage={image}&tag=latest")
    print(f"pulled {image}:latest")

# 2. locate stack + load LOCAL compose (so new services/env actually deploy)
stacks = req("GET", "/api/stacks")
stack_id = next(s["Id"] for s in stacks if s["Name"] == STACK_NAME)
print(f"stack id: {stack_id}")
compose_yaml = Path(COMPOSE_FILE).read_text(encoding="utf-8")
stack = req("GET", f"/api/stacks/{stack_id}")
env = stack.get("Env") or []
print(f"preserving {len(env)} existing env vars")

# 3. PUT the new compose, secrets preserved
result = req(
    "PUT",
    f"/api/stacks/{stack_id}?endpointId={ENDPOINT_ID}",
    {
        "stackFileContent": compose_yaml,
        "env": env,
        "prune": True,
        "pullImage": False,
    },
)
print(f"redeployed: {result.get('Name')} status={result.get('Status')}")

# 4. recreate any container still on an older image id
latest_ids = {}
for image in IMAGES:
    info = req(
        "GET",
        "/api/endpoints/%d/docker/images/%s/json"
        % (ENDPOINT_ID, urllib.request.quote(f"{image}:latest", safe="")),
    )
    latest_ids[image] = info["Id"]

filters = urllib.request.quote(
    json.dumps({"label": [f"com.docker.compose.project={STACK_NAME}"]})
)
containers = req(
    "GET",
    f"/api/endpoints/{ENDPOINT_ID}/docker/containers/json?all=true&filters={filters}",
)
for c in containers:
    name = c["Names"][0].lstrip("/")
    image = (c.get("Image") or "").split(":")[0]
    want = latest_ids.get(image)
    if want and c["ImageID"] != want:
        print(f"{name} on {c['ImageID'][:19]}, latest {want[:19]} -> recreating")
        req("POST", f"/api/docker/{ENDPOINT_ID}/containers/{c['Id']}/recreate", {"PullImage": False})
        print("  recreated")
    else:
        print(f"{name} up to date")

# 5. show final container state
time.sleep(4)
containers = req(
    "GET",
    f"/api/endpoints/{ENDPOINT_ID}/docker/containers/json?all=true&filters={filters}",
)
print("\n--- stack containers ---")
for c in sorted(containers, key=lambda x: x["Names"][0]):
    print(f"  {c['Names'][0].lstrip('/'):32} {c.get('State',''):9} {c.get('Status','')}")
