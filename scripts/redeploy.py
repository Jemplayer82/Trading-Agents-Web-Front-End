"""Out-of-band ops script: render + redeploy the tradingagents stack (67).

The repo `docker-compose.yml` is the source of truth. This script first RENDERS
the deploy payload from it (scripts/render_stack_payload.py — substitutes secrets
from the gitignored `.env`), then PUTs that payload to Portainer. So compose
changes (new services, new env vars) take effect, and secrets are never wiped
(they're inline literals in the rendered StackFileContent; Portainer Env[] stays
empty).

Flow:
  0. Render payload from docker-compose.yml + .env  -> STACK_PAYLOAD_OUT.
  1. Pull both :latest images on the endpoint (anonymous; images are public).
  2. PUT the rendered payload to the stack.
  3. Recreate any container still on an older image id (a PUT alone often won't
     recreate just because :latest moved).
  4. Print final container state.

Credentials come from the environment — NEVER hardcode a Portainer token in a
committed file. Run e.g.:

    $env:PORTAINER_TOKEN="ptr_..."; python scripts/redeploy.py   # PowerShell
    PORTAINER_TOKEN=ptr_... python scripts/redeploy.py            # bash

Optional overrides: PORTAINER_URL, PORTAINER_ENDPOINT_ID, TRADINGAGENTS_STACK,
STACK_PAYLOAD_OUT.
"""
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("PORTAINER_URL", "https://192.168.7.50:9443").rstrip("/")
TOKEN = os.environ.get("PORTAINER_TOKEN")  # pragma: allowlist secret
ENDPOINT_ID = int(os.environ.get("PORTAINER_ENDPOINT_ID", "3"))
STACK_NAME = os.environ.get("TRADINGAGENTS_STACK", "tradingagents")
PAYLOAD_OUT = os.environ.get("STACK_PAYLOAD_OUT", r"C:\tmp\stack67_payload.json")
RENDER = Path(__file__).resolve().parent / "render_stack_payload.py"
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


# -1. pre-flight: refuse to deploy over a LIVE scan. Every redeploy recreates
# the portfolio container, killing any in-flight worker — the scan then sits
# 'running' with a dead worker until the reaper flags it "abandoned" an hour
# later (bitten three times: S&P scan 17 on 07-18, options 35 + portfolio 11
# on 07-31). Override deliberately with REDEPLOY_FORCE=1.
def _scan_running() -> dict | None:
    tok = None
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("INTERNAL_API_TOKEN="):
                tok = line.split("=", 1)[1].strip()
    if not tok:
        print("pre-flight: no INTERNAL_API_TOKEN in .env — skipping scan check")
        return None
    for base in ("http://192.168.7.50:8080", "https://trading.txferguson.net"):
        try:
            r = urllib.request.Request(base + "/api/portfolio/status",
                                       headers={"X-Internal-Token": tok})
            with urllib.request.urlopen(r, context=ctx, timeout=10) as resp:
                return (json.loads(resp.read()) or {}).get("running")
        except Exception:
            continue
    print("pre-flight: status endpoint unreachable — proceeding (can't confirm idle)")
    return None


_running = _scan_running()
if _running and os.environ.get("REDEPLOY_FORCE") != "1":
    sys.exit(
        f"ABORT: a scan is RUNNING ({_running.get('scan_type')} #{_running.get('id')}, "
        f"kind {_running.get('kind')}) — deploying now would kill its worker and the "
        "reaper would flag it 'abandoned' an hour later. Wait for it to finish, or "
        "rerun with REDEPLOY_FORCE=1 if you accept killing it."
    )

# 0. render payload from the repo compose (+ .env secrets)
print("rendering payload from docker-compose.yml ...")
proc = subprocess.run([sys.executable, str(RENDER)], env={**os.environ, "STACK_PAYLOAD_OUT": PAYLOAD_OUT})
if proc.returncode != 0:
    sys.exit("render failed — aborting deploy.")
payload = json.loads(Path(PAYLOAD_OUT).read_text(encoding="utf-8"))

# 1. pull both images (public)
for image in IMAGES:
    req("POST", f"/api/endpoints/{ENDPOINT_ID}/docker/images/create?fromImage={image}&tag=latest")
    print(f"pulled {image}:latest")

# 2. locate stack + PUT the rendered payload (secrets are inline literals)
stacks = req("GET", "/api/stacks")
stack_id = next(s["Id"] for s in stacks if s["Name"] == STACK_NAME)
print(f"stack id: {stack_id}")
result = req("PUT", f"/api/stacks/{stack_id}?endpointId={ENDPOINT_ID}", payload)
print(f"redeployed: {result.get('Name')} status={result.get('Status')}")

# 3. recreate any container still on an older image id
latest_ids = {}
for image in IMAGES:
    info = req(
        "GET",
        f"/api/endpoints/{ENDPOINT_ID}/docker/images/{urllib.request.quote(f'{image}:latest', safe='')}/json",
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

# 4. show final container state
time.sleep(4)
containers = req(
    "GET",
    f"/api/endpoints/{ENDPOINT_ID}/docker/containers/json?all=true&filters={filters}",
)
print("\n--- stack containers ---")
for c in sorted(containers, key=lambda x: x["Names"][0]):
    print(f"  {c['Names'][0].lstrip('/'):32} {c.get('State',''):9} {c.get('Status','')}")
