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

## Prerequisites

| Requirement | Notes |
|---|---|
| **Claude Code CLI** installed and logged in | `claude -p "hi"` must print a reply as the service user |
| **Python 3.9+** | Must be the interpreter in `ExecStart`; 3.12 recommended |
| **`httpx` package** | `pip install httpx` (or `pip3`, or `uv pip`) |
| **Network access to the switchboard** | `http://<host>:3109` — no TLS required for LAN; the bearer token is the gate |

## Install

```sh
# 1. Env file (secrets live here, mode 600 — never in git or the unit).
sudo install -D -m 600 deploy/cleo/cleo.env.example /etc/cleo/cleo.env
sudo $EDITOR /etc/cleo/cleo.env          # set SWITCHBOARD_URL + SWITCHBOARD_MCP_TOKEN

# 2. Edit the three CHANGEME lines in the unit (User, WorkingDirectory, ExecStart).
$EDITOR deploy/cleo/cleo.service

# 3. Verify the interpreter has httpx:
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

## How Cleo stays available (keepalive)

Cleo holds its presence on the switchboard through a **long-poll loop**:

1. On startup it calls `register_agent` + `set_status` to announce itself.
2. It then calls `wait_for_message` with a 25-second timeout in a tight loop.
   Each poll refreshes its `last_seen` timestamp on the bus. The switchboard
   marks an agent offline after 60 seconds of silence, so the 25-second cadence
   keeps Cleo showing **online** continuously.
3. If a poll fails (network blip, switchboard restart), the loop catches the
   error, waits 2 seconds, then **re-registers and resumes polling** — no manual
   restart needed.

**What can take Cleo offline:**
- The process is killed / crashes (systemd restarts it automatically with `Restart=always`)
- The machine loses network to the switchboard for >60 seconds (auto-recovers once network returns)
- A second Cleo process starts and the flock guard kills it — the first one keeps running

**What does NOT affect availability:**
- The Docker stack restarting — Cleo is a bare host process, independent of the stack
- The switchboard container restarting — Cleo re-registers automatically on the next poll cycle

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

| Variable | Default | Purpose |
|---|---|---|
| `SWITCHBOARD_URL` | — | Switchboard base URL, no trailing `/mcp` (e.g. `http://host:3109`) |
| `SWITCHBOARD_MCP_TOKEN` | — | Bearer token — must match the stack's `SWITCHBOARD_MCP_TOKEN` |
| `SWITCHBOARD_AGENT_ID` | `cleo` | Bus agent name — change if you run multiple Cleo instances |
| `DEFAULT_MODEL` | `claude-sonnet-4-6` | Fallback model when the request doesn't specify one |
| `CLEO_CALL_TIMEOUT_S` | `150` | Hard per-call ceiling in seconds — keep below the client's 180s so Cleo fails itself first |
| `CLAUDE_BIN` | `claude` | Full path to the `claude` binary if it isn't on `PATH` for the service user |
| `CLEO_LOCK_FILE` | `/tmp/cleo-<agent_id>.lock` | Single-instance flock path — override if `/tmp` is on a tmpfs that's shared across hosts |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `registered as 'cleo'` never appears in logs | `claude -p "hi"` fails as service user | Fix CLI login: `sudo -u <user> claude login` |
| `Another Cleo instance already holds…` then exits | Stale lock or second copy running | `systemctl status cleo` to confirm only one; `rm /tmp/cleo-cleo.lock` if stale |
| `Poll error: … retrying` loops forever | Wrong `SWITCHBOARD_URL` or bad token | Check URL is reachable from host: `curl http://<host>:3109/mcp` |
| Analysis hangs, no reply from Cleo | `claude -p` subprocess hung past timeout | Check `journalctl -u cleo -f`; watchdog kills it after `CLEO_CALL_TIMEOUT_S` |
| Cleo shows offline on the bus | Process not running | `systemctl start cleo`; check `journalctl -u cleo -n 20` for crash reason |

## Why bare instead of containerized?

Cleo drives the host's Claude Code subscription login (`~/.claude`).
Containerizing means mounting that credential dir read-write (for token refresh),
which is the one wrinkle that makes the host-process approach simpler. If you
later want it in the stack, the credential mount is the thing to validate first.
