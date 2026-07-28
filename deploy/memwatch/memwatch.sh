#!/usr/bin/env bash
#
# memwatch — lightweight host memory sampler.
#
# Appends one timestamped row-set to CSVs under $MEMWATCH_DIR each time it runs
# (a systemd timer drives it every 5 minutes). Deliberately dependency-free: no
# exporter, no database, no extra containers on a box that is already suspected
# of leaking. Costs roughly 1 MB/day.
#
# Deliberately NOT `set -e`: one failing probe (docker wedged, a unit removed)
# must still leave the other files with a row for this timestamp. Otherwise the
# series develop holes exactly when the box is sick — which is the only time
# they matter.

set -uo pipefail

OUT_DIR="${MEMWATCH_DIR:-/var/log/memwatch}"
TOP_N="${MEMWATCH_TOP_N:-25}"
UNITS="${MEMWATCH_UNITS:-cleo.service docker.service containerd.service}"
# cleo caps a single claude CLI call at CLEO_CALL_TIMEOUT_S (150s default), so a
# live one is always young. Anything older than this outlived its parent.
ORPHAN_AGE_S="${MEMWATCH_ORPHAN_AGE_S:-300}"

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$OUT_DIR" || exit 1

# Write the header only into a new/empty file. logrotate uses copytruncate, so
# after a rotation the live file is empty again and gets a fresh header here.
hdr() { [ -s "$1" ] || printf '%s\n' "$2" >"$1"; }

# ---------------------------------------------------------------------------
# 1. Host totals  +  2. Kernel-side fields
# ---------------------------------------------------------------------------
# Both come out of /proc/meminfo, so read it once.
#
# meminfo.csv is NOT optional padding. This box churns Docker containers and
# prunes images nightly; that workload can leak in kernel slab, which no amount
# of per-process RSS will ever reveal. Without these fields we could stare at
# userland forever and never find it.
HOST_CSV="$OUT_DIR/host.csv"
MEMINFO_CSV="$OUT_DIR/meminfo.csv"
hdr "$HOST_CSV" "ts,total_kb,used_kb,free_kb,available_kb,buffers_kb,cached_kb,shmem_kb,swap_total_kb,swap_used_kb"
hdr "$MEMINFO_CSV" "ts,slab_kb,sreclaimable_kb,sunreclaim_kb,kernelstack_kb,pagetables_kb,anonpages_kb,mapped_kb,vmallocused_kb,percpu_kb"

awk -v ts="$TS" -v host="$HOST_CSV" -v mi="$MEMINFO_CSV" '
  /^[A-Za-z_()]+:/ { gsub(":", "", $1); v[$1] = $2 }
  END {
    # Mirrors what `free` calls "used": total, minus free, minus the page cache
    # that is actually reclaimable. Shmem is NOT reclaimable, so it counts used.
    used = v["MemTotal"] - v["MemFree"] - v["Buffers"] - v["Cached"] - v["SReclaimable"] + v["Shmem"]
    printf "%s,%d,%d,%d,%d,%d,%d,%d,%d,%d\n", ts, \
      v["MemTotal"], used, v["MemFree"], v["MemAvailable"], v["Buffers"], \
      v["Cached"], v["Shmem"], v["SwapTotal"], v["SwapTotal"] - v["SwapFree"] >> host
    printf "%s,%d,%d,%d,%d,%d,%d,%d,%d,%d\n", ts, \
      v["Slab"], v["SReclaimable"], v["SUnreclaim"], v["KernelStack"], \
      v["PageTables"], v["AnonPages"], v["Mapped"], v["VmallocUsed"], v["Percpu"] >> mi
  }
' /proc/meminfo

# ---------------------------------------------------------------------------
# 3. Top processes by RSS
# ---------------------------------------------------------------------------
PROCS_CSV="$OUT_DIR/procs.csv"
hdr "$PROCS_CSV" "ts,pid,ppid,rss_kb,etimes,comm,args"
ps -eo pid=,ppid=,rss=,etimes=,comm=,args= --sort=-rss 2>/dev/null \
  | head -n "$TOP_N" \
  | awk -v ts="$TS" '
      {
        pid = $1; ppid = $2; rss = $3; et = $4; comm = $5
        args = ""
        for (i = 6; i <= NF; i++) args = args (i > 6 ? " " : "") $i
        # Commas and quotes would break the CSV; args is free-form.
        gsub(/[",]/, " ", args); gsub(/[",]/, " ", comm)
        if (length(args) > 120) args = substr(args, 1, 120)
        printf "%s,%s,%s,%s,%s,%s,%s\n", ts, pid, ppid, rss, et, comm, args
      }' >> "$PROCS_CSV"

# ---------------------------------------------------------------------------
# 4. Per-container memory
# ---------------------------------------------------------------------------
CONT_CSV="$OUT_DIR/containers.csv"
hdr "$CONT_CSV" "ts,name,mem_bytes,mem_limit_bytes,pids"
# A wedged dockerd can make `docker stats` hang forever; the timeout stops a
# sick daemon from wedging the sampler along with it.
timeout 60 docker stats --no-stream --format '{{.Name}}	{{.MemUsage}}	{{.PIDs}}' 2>/dev/null \
  | awk -v ts="$TS" '
      # Docker prints e.g. "1.234GiB / 23.45GiB". Treating KB and KiB alike is a
      # 2.4% error, which is irrelevant for detecting a growth trend.
      function to_bytes(s,   n, u) {
        n = s + 0
        u = s; sub(/^[0-9.]+/, "", u)
        if (u ~ /^[Kk]i?B/) return int(n * 1024)
        if (u ~ /^[Mm]i?B/) return int(n * 1024 * 1024)
        if (u ~ /^[Gg]i?B/) return int(n * 1024 * 1024 * 1024)
        if (u ~ /^[Tt]i?B/) return int(n * 1024 * 1024 * 1024 * 1024)
        return int(n)
      }
      BEGIN { FS = "\t" }
      NF >= 3 {
        split($2, mu, " / ")
        name = $1; gsub(/[",]/, " ", name)
        printf "%s,%s,%d,%d,%s\n", ts, name, to_bytes(mu[1]), to_bytes(mu[2]), $3
      }' >> "$CONT_CSV"

# ---------------------------------------------------------------------------
# 5. Orphan detector  (the crisp test for the cleo grandchild hypothesis)
# ---------------------------------------------------------------------------
# cleo SIGKILLs the claude CLI on every call. SIGKILL cannot be caught, so Node
# never reaps its own children — they reparent to PID 1 and survive. A claude
# process older than ORPHAN_AGE_S is therefore proof of that leak, and a flat
# claude_stale column is proof it is NOT happening.
ORPH_CSV="$OUT_DIR/orphans.csv"
CLAUDE_CSV="$OUT_DIR/claude_procs.csv"
hdr "$ORPH_CSV" "ts,claude_total,claude_stale,claude_rss_kb,node_ppid1_count,node_ppid1_rss_kb,zombies,proc_total"
hdr "$CLAUDE_CSV" "ts,pid,ppid,rss_kb,etimes,args"

# The search term, the strip pattern, and the output path all arrive through the
# ENVIRONMENT rather than -v or the program body. `ps` lists an awk program's
# source in that process's own argv, so ANY occurrence of the target name in this
# block — including in a comment or in a /regex/ literal — makes the detector
# match itself and report a phantom hit on every single sample. Keep the awk body
# free of that literal string; environment variables never appear in argv.
ps -eo pid=,ppid=,rss=,etimes=,stat=,comm=,args= 2>/dev/null \
  | MEMWATCH_DETAIL="$CLAUDE_CSV" \
    MEMWATCH_PAT='claude' \
    MEMWATCH_STRIP='\.claude' \
    awk -v ts="$TS" -v age="$ORPHAN_AGE_S" '
      BEGIN {
        detail = ENVIRON["MEMWATCH_DETAIL"]
        pat    = ENVIRON["MEMWATCH_PAT"]
        strip  = ENVIRON["MEMWATCH_STRIP"]
      }
      {
        pid = $1; ppid = $2; rss = $3; et = $4; st = $5; comm = $6
        total++
        if (st ~ /Z/) z++
        if (comm == "node" && ppid == 1) { n1++; n1rss += rss }

        # Drop the dot-prefixed config-directory form first, so only a real
        # binary or CLI entrypoint can match.
        probe = $0
        gsub(strip, "", probe)
        if (probe ~ pat) {
          ct++; crss += rss
          if (et + 0 > age) cstale++
          args = ""
          for (i = 7; i <= NF; i++) args = args (i > 7 ? " " : "") $i
          gsub(/[",]/, " ", args)
          if (length(args) > 160) args = substr(args, 1, 160)
          printf "%s,%s,%s,%s,%s,%s\n", ts, pid, ppid, rss, et, args >> detail
        }
      }
      END {
        # stdout — the shell redirect below appends it to orphans.csv. The
        # per-process detail above went straight to its own file.
        printf "%s,%d,%d,%d,%d,%d,%d,%d\n", ts, ct, cstale, crss, n1, n1rss, z, total
      }' >> "$ORPH_CSV"

# ---------------------------------------------------------------------------
# 6. Per-systemd-unit memory
# ---------------------------------------------------------------------------
SVC_CSV="$OUT_DIR/services.csv"
hdr "$SVC_CSV" "ts,unit,memory_current_bytes,memory_peak_bytes,tasks_current"
num_or_blank() { case "$1" in ''|*[!0-9]*) printf '' ;; *) printf '%s' "$1" ;; esac; }
for u in $UNITS; do
  cur="$(systemctl show "$u" -p MemoryCurrent --value 2>/dev/null)"
  peak="$(systemctl show "$u" -p MemoryPeak --value 2>/dev/null)"
  tasks="$(systemctl show "$u" -p TasksCurrent --value 2>/dev/null)"
  printf '%s,%s,%s,%s,%s\n' "$TS" "$u" \
    "$(num_or_blank "$cur")" "$(num_or_blank "$peak")" "$(num_or_blank "$tasks")" >> "$SVC_CSV"
done

exit 0
