# Per-Account Scan Scheduling — Design

**Date:** 2026-08-10
**Status:** Approved by Landon (conversation, 2026-08-10)
**Scope:** Scheduling only. The companion stop-loss-types feature (Schwab-style
stop varieties for the paper-trading simulation) is a separate spec, not yet
written.

## Problem

All automated scan times are hardcoded `CronTrigger`s in
`web/scheduler.py::register_jobs()`. The user wants to set fire times from the
dashboard, per unit:

- **Nightly real-Schwab portfolio scan** (`job_nightly_scan`, today Mon–Fri
  22:00 ET) — one global time, editable from the Portfolio tab.
- **S&P 500 paper accounts** (today: one un-attributed Sat 00:00 ET scan) —
  each named paper account fires its own scan at its own time.
- **Options paper accounts** (today: one shared Mon–Fri 07:30 ET build for all
  options accounts) — same per-account treatment.

Out of scope: newsletter, token health, price refreshes, marks, settle, grade,
outcome sweep, reaper — their times stay hardcoded. Day-of-week patterns stay
fixed (Mon–Fri for portfolio/options, any-day time for S&P accounts); only
time-of-day becomes editable.

## Current behavior being replaced

- The Saturday S&P cron fires ONE scan with no `account_id` — none of the named
  paper accounts are tied to it; they only run via the manual per-account "run
  now" button. Once per-account schedules exist, this orphaned global job is
  **retired** (a redundant unattributed scan would otherwise run alongside every
  account's own).
- The options cron (`job_options_scan`) fires one build covering every options
  account sequentially. It is likewise replaced by per-account jobs.
- `job_nightly_scan`'s trigger time comes from a new setting instead of the
  hardcoded 22:00; everything else about it is unchanged.

## Design

### Data model

- New nullable column `paper_accounts.schedule_time TEXT` (`"HH:MM"`, ET,
  matching `SCHEDULER_TIMEZONE`). NULL = never auto-runs (manual only).
  Migration via the existing `_COLUMN_MIGRATIONS` pattern in `web/db.py`.
- New-account creation pre-fills the current default for its kind (S&P:
  `00:00`; options: `07:30`) so a newly created account doesn't silently never
  run.
- Existing accounts at migration time get the same defaults backfilled
  (matching what the retired global jobs would have done for them).
- The global Schwab portfolio time is a new entry in the existing
  `SETTINGS_REGISTRY` (`web/credentials.py`): key
  `SCHEDULE_NIGHTLY_SCAN_TIME`, group "Automation Schedule", default `22:00`.
  It is surfaced in the Portfolio tab's "[ run scan now ]" box (see UI), not
  only on the Settings tab.

### Scheduler (web/scheduler.py)

- `register_jobs()` no longer registers the fixed `spy_scan` / `options_scan`
  crons. `nightly_scan` registers with its time read from the setting.
- New reconciler job (IntervalTrigger, every 60s — same pattern as
  `reap_stuck_runs`): reads `paper_accounts` (id, kind, schedule_time) + the
  nightly-scan setting, diffs against currently registered APScheduler jobs:
  - account with a time but no job → `add_job` (id `spy_scan_acct_{id}` /
    `options_scan_acct_{id}`)
  - time changed → `reschedule_job`
  - account deleted or time cleared → `remove_job`
  - nightly-scan time changed → `reschedule_job("nightly_scan")`
- Per-account job bodies mirror the existing `job_spy_scan` /
  `job_options_scan` HTTP-POST pattern but pass `{"account_id": id}` — the
  same path the manual "run now" buttons use, so idempotency, queueing, and
  busy-checks all apply unchanged. Day-of-week stays fixed per kind, matching
  today: S&P per-account jobs fire Saturdays only (`day_of_week="sat"`),
  options per-account jobs keep `day_of_week="mon-fri"`. Only time-of-day is
  configurable.
- Validation: `HH:MM` 24-hour, parsed server-side; malformed values are
  logged and skipped by the reconciler (never crash the scheduler loop). No
  business-hours constraints — the options build's own market-open gate
  already handles "too early/too late" safely; UI copy notes the 09:35
  allocation gate next to the options time field.

### API

- `POST /api/paper-accounts` and the account-update route gain
  `schedule_time` (nullable string, validated `HH:MM`). GET responses include
  it.
- No new endpoints — the reconciler reads the DB directly (scheduler container
  shares the SQLite volume).

### UI

- **S&P Paper Accounts modal** (`web/static/spy.js` + `index.html`) and
  **Options Paper Accounts modal** (`web/static/options.js`): a "SCAN TIME
  (ET)" `<input type="time">` in the NEW ACCOUNT form and the per-account
  Edit form, beside Aggressiveness/Bias. Blank = manual-only, stated in the
  field's helper text.
- **Portfolio tab "[ run scan now ]" box** (`web/static/portfolio.js`): a
  "NIGHTLY SCAN TIME (ET)" time input that reads/writes the
  `SCHEDULE_NIGHTLY_SCAN_TIME` setting via the existing settings API. The
  box's caption text ("the nightly cron also fires at 22:00 ET") becomes
  dynamic.

### Tier gating

`schedule_time` column and reconciler are tier-agnostic (harmless below tier
3 — no accounts exist). The UI fields ship inside files already tier-gated
(spy.js T3+, options.js T4). The nightly-scan setting sits in a TIER:2 block
of `SETTINGS_REGISTRY` alongside the other Schwab settings.

### Testing

- DB: migration + CRUD round-trip of `schedule_time`, validation rejects
  malformed values.
- Reconciler: unit tests with a fake scheduler object — add on new account,
  reschedule on time change, remove on delete/clear, malformed time skipped,
  nightly-scan setting change reschedules.
- Routes: create/update with and without `schedule_time`.
- UI: extend the jsvm harness tests for the modals if practical; otherwise
  route-level coverage suffices.

### Risks / notes

- Two accounts set to the same time simply queue behind each other via the
  existing scan queue — no new serialization needed.
- The scheduler container reads settings from the shared DB at reconcile
  time, so no restart is needed for any time change (~60s worst-case lag).
- Retiring the global un-attributed Saturday scan changes what the S&P tab's
  "ALL ACCOUNTS"-style history shows going forward (only per-account scans
  accumulate). Accepted.
