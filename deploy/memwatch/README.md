# memwatch — find out what's actually eating the host

## 🎯 Why this exists

The WebServer's RAM climbs over days until it needs a reboot. When that was first
investigated there was **no evidence at all**: no sysstat, no Prometheus, no netdata,
no cadvisor. Nothing had ever recorded memory over time, and the reboot wiped live
process state — so the growth couldn't be attributed to anything, and any fix would
have been a guess.

memwatch records the six things you need to stop guessing, into plain CSVs, with **no
new containers** on a box that is already under suspicion.

> ⚠️ This is a diagnostic, not a dashboard. It answers "what grew?" — it does not
> alert, graph, or page anyone. Once it names the culprit, fix the culprit.

## 📦 What it records

Every 5 minutes, into `/var/log/memwatch/`:

| File | Contents | Why it's here |
|---|---|---|
| `host.csv` | total / used / free / **available** / buffers / cached / shmem / swap | `used` rising while `available` holds steady means page cache, **not** a leak |
| `meminfo.csv` | Slab, SReclaimable, **SUnreclaim**, KernelStack, PageTables, AnonPages, Mapped, VmallocUsed, Percpu | A box that churns containers and prunes images nightly can leak in **kernel slab**, which per-process RSS will never show |
| `procs.csv` | top 25 by RSS: pid, ppid, rss, age, comm, args | The usual suspects |
| `containers.csv` | per-container memory + limit + pid count | All 34 containers, one `docker stats` pass |
| `orphans.csv` | claude process count/age/RSS, PPID=1 node count, zombies, total procs | The crisp test for the cleo grandchild leak — see below |
| `claude_procs.csv` | one row per matching process, with args | So you can *see* the orphans, not just count them |

Roughly **1 MB/day**. logrotate keeps ~3 weeks in the live file plus 6 compressed
generations.

### The orphan column is a real experiment

cleo SIGKILLs the `claude` CLI on **every** call. SIGKILL can't be caught, so Node
never reaps its own children — they reparent to PID 1 and survive forever. cleo caps a
call at 150 s, so **a `claude` process older than 300 s is proof of that leak**, and a
flat `claude_stale` column is proof it *isn't* happening. Either answer is useful.

## 🔧 Install

```bash
sudo install -m 755 memwatch.sh /usr/local/bin/memwatch.sh
sudo install -m 755 analyze_memwatch.py /usr/local/bin/analyze_memwatch.py
sudo install -m 644 memwatch.service /etc/systemd/system/memwatch.service
sudo install -m 644 memwatch.timer   /etc/systemd/system/memwatch.timer
sudo install -m 644 memwatch.logrotate /etc/logrotate.d/memwatch
sudo systemctl daemon-reload
sudo systemctl enable --now memwatch.timer
```

Verify it's scheduled and producing rows:

```bash
systemctl list-timers memwatch --no-pager
sudo systemctl start memwatch.service   # force one sample now
wc -l /var/log/memwatch/*.csv
```

## 📊 Read the results

```bash
sudo analyze_memwatch.py --hours 24
```

It prints a ranked "what grew" table per category, then a verdict:

```
VERDICT
  host 'used'      4.8 GiB → 8.6 GiB  (3.8 GiB, 54.2 MiB/hr)
  host 'available' 15.7 GiB → 13.8 GiB  (-1.9 GiB)

  attributed to containers : 1.9 GiB
  attributed to kernel     : 341.4 MiB  (unreclaimable slab + page tables)
  unexplained              : 1.6 GiB

  → container 'qdrant' grew 1.9 GiB
  → process group 'gunicorn' grew 1.1 GiB
  → kernel slab/pagetables grew 341.4 MiB — no userland fix will touch this
```

> ⚠️ **Give it time.** The last climb took six days. A 24-hour window may show
> nothing at all — that is not the same as "no leak". Check again at 72 h and a week:
> `analyze_memwatch.py --hours 72`, `--hours 168`.

## 🎛 Knobs

| Variable | Default | Purpose |
|---|---|---|
| `MEMWATCH_DIR` | `/var/log/memwatch` | Output directory |
| `MEMWATCH_TOP_N` | `25` | How many processes to sample per pass. Raise it if the verdict reports a large "unexplained" share |
| `MEMWATCH_UNITS` | `cleo.service docker.service containerd.service` | Which systemd units to track |
| `MEMWATCH_ORPHAN_AGE_S` | `300` | Age above which a `claude` process counts as orphaned |

## 🐛 Gotcha worth knowing

The orphan detector passes its search term, strip pattern, and output path through the
**environment**, never via `awk -v` or the program body. `ps` lists an awk program's
source inside that process's own `argv`, so any occurrence of the target name in this
script — even in a comment or a `/regex/` literal — makes the detector match *itself*
and report a phantom hit on every sample. Both of those bugs happened during
development. Environment variables don't appear in `argv`; that's the fix.

Keep the awk body free of the literal search string if you edit it.

## 📄 License

Apache 2.0, same as the rest of the repository. See [LICENSE](../../LICENSE).
