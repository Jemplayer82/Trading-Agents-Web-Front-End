# Running Cleo as a systemd service

Cleo (`scripts/cleo_llm_handler.py`) is the bridge that lets TradingAgents use a
**free local `claude -p` subscription session** as an LLM backend instead of the
paid Anthropic API. It registers on the internal switchboard bus as agent `cleo`,
receives `llm_request` DMs, drives the local Claude CLI, and streams replies back.

It runs as a **bare process on the host** (not in the Docker stack) because it
needs that host user's `claude login`. This unit gives it the three things a bare
process otherwise lacks: **auto-restart**, **log aggregation** (journald), and a
**single-instance guard** so a stray second copy can't split the request stream.

> ⚠️ Cleo must run as the user who ran `claude login`. Confirm first:
> ```sh
> sudo -u <that-user> claude -p "hi"
> ```
> If that doesn't print a reply, fix the CLI login before going further.

## Install

```sh
# 1. Env file (secrets live here, mode 600 — never in git or the unit).
sudo install -D -m 600 deploy/cleo/cleo.env.example /etc/cleo/cleo.env
sudo $EDITOR /etc/cleo/cleo.env          # set SWITCHBOARD_MCP_TOKEN

# 2. Edit the three CHANGEME lines in the unit (User, WorkingDirectory, ExecStart).
$EDITOR deploy/cleo/cleo.service

# 3. Make sure the chosen interpreter has httpx:
#    <interpreter> -c "import httpx"   # if it errors: pip install httpx

# 4. Install + start.
sudo cp deploy/cleo/cleo.service /etc/systemd/system/cleo.service
sudo systemctl daemon-reload
sudo systemctl enable --now cleo
```

## Verify

```sh
systemctl is-active cleo          # -> active
journalctl -u cleo -n 50 -f       # watch it register + handle requests
```

A healthy start logs `registered as 'cleo'`. A refused duplicate logs
`Another Cleo instance already holds … Exiting.` and stops — that's the
single-instance guard doing its job, not a bug.

## Deploy a code update

Cleo ships in the repo, so updating it is a pull + restart:

```sh
cd /opt/TradingAgents && git pull
sudo systemctl restart cleo
```

The kernel releases the single-instance lock the moment the old process exits, so
the restart re-acquires it immediately — no crash loop.

## Knobs

All optional env vars are documented in [`cleo.env.example`](cleo.env.example).
The important one is `CLEO_CALL_TIMEOUT_S` (default 150s): the hard ceiling on a
single `claude -p` call. Keep it **below** the analysis client's 180s timeout so
Cleo fails itself — and emits a real error — before the requester gives up.

## Why bare instead of containerized?

Cleo drives the host's Claude Code subscription login (`~/.claude`).
Containerizing means mounting that credential dir in read-write (for token
refresh), which is the one wrinkle that makes the host-process approach simpler.
If you later want it in the stack, the credential mount is the thing to validate
first.
