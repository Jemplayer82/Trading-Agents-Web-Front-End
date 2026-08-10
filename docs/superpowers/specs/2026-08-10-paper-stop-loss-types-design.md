# Paper Stop-Loss Types — Design

**Date:** 2026-08-10
**Status:** Approved by Landon (conversation, 2026-08-10)
**Scope:** Simulated paper-trading stop-loss policy only. There is no order
execution anywhere in this codebase and this feature does not add any —
it only changes when the simulation decides to close a paper position.

## Problem

Stop-loss behavior today is inconsistent and not user-configurable:

- **Options accounts**: every account gets the same hardcoded layered
  policy — a fixed −60% stop (`STOP_LOSS_PCT` in `web/options_allocator.py`)
  combined with a trailing stop that arms once a position peaks at +50%
  and then trails 30% below peak (`options_trailing_stop`/
  `options_trail_arm_pct`/`options_trail_give_back` in
  `tradingagents/default_config.py`). Global, not per-account, not visible
  in any UI.
- **Equity (S&P) accounts**: no stop-loss mechanism exists at all. The only
  way a position exits is the weekly allocator's own rebalance decision.

The user wants each paper account to configure its own stop policy,
modeled on the real order types Schwab itself offers, surfaced in the same
account create/edit modals as the scan-scheduling feature (companion spec:
`2026-08-10-per-account-scan-scheduling-design.md`).

## Design decisions (confirmed with the user)

1. **Migration default**: options accounts backfill to the closest
   single-type equivalent of today's behavior (a `stop` at 60%); equity
   accounts backfill to `none` (opt-in — this is new behavior for them, not
   a defaults change).
2. **Stop model**: **strict single type per account**, matching Schwab's
   own order semantics (one order type at a time) rather than the current
   layered fixed+trailing combination. The old arm/give-back trailing
   architecture is retired in favor of one clean trailing definition.
3. **Granularity**: **per-account only**. No per-position override in this
   pass — every open position in an account is governed by that account's
   one policy. (Per-position overrides are a plausible future extension,
   explicitly deferred.)

## Stop types

One `stop_type` per account, from:

| type | meaning | value fields |
|---|---|---|
| `none` | no automatic stop | — |
| `stop` | close when price falls `stop_value`% below entry | `stop_value` (%) |
| `stop_limit` | trigger at `stop_value`% below entry, but only fill at or above a limit `stop_limit_offset`% below the trigger — a real fill can be skipped if the price gaps through | `stop_value` (%), `stop_limit_offset` (%) |
| `trailing_pct` | close when price falls `stop_value`% below its peak since entry | `stop_value` (%) |
| `trailing_dollar` | close when price falls `stop_value` dollars below its peak since entry | `stop_value` ($) |

Trailing types trail from the very first peak observed after entry — no
arm threshold (the current options trailing stop's "must reach +50% first"
behavior does not carry over; if the user wants an arm threshold back later
that's a follow-up, not blocking this pass).

## Data model

New nullable-where-appropriate columns on `paper_accounts` (via the
existing `_COLUMN_MIGRATIONS` pattern in `web/db.py`):

- `stop_type TEXT NOT NULL DEFAULT 'none'`
- `stop_value REAL` (NULL when `stop_type='none'`)
- `stop_limit_offset REAL` (NULL unless `stop_type='stop_limit'`)

Same enumeration sites as the companion scheduling spec's `schedule_time`
column: the CREATE TABLE, the SELECT column lists in
`list_paper_accounts`/`get_paper_account`, the INSERT in
`create_paper_account`, and the whitelist in `update_paper_account`. CRUD
routes (`web/spy_routes.py`, shared by both account kinds) validate:
`stop_type` in the allowed set; `stop_value` required and > 0 for every
non-`none` type; `stop_limit_offset` required and > 0 only for
`stop_limit`.

Options positions gain one new nullable column,
`options_positions.stop_triggered_at`, to support stop-limit's resting-fill
semantics (see below) — a stop_limit trigger that can't fill immediately
needs to remember it's armed and keep checking.

## Options enforcement

`web/options_allocator.py::effective_stop_level` is today the single
function computing a stop level, called from exactly two places:
`forced_closes` (the daily allocator backstop) and
`options_engine._apply_intraday_stops` (the hourly refresh). Both already
have the position's `paper_account_id` available. The function is extended
to take the account's stop policy instead of reading the global
`DEFAULT_CONFIG` knobs directly — this is the one seam both callers share.

- `stop` / `trailing_pct` / `trailing_dollar`: compute the level directly
  from the policy; identical fill convention to today
  (`_apply_intraday_stops`'s existing crossing-interval-vs-gap-through
  logic is unchanged, only the level computation changes).
- `stop_limit`: on trigger, check whether the current fresh mark is at or
  above the limit price. If yes, close normally. If no (the price gapped
  below the limit), do NOT close — set `stop_triggered_at` on the position
  instead. On every subsequent refresh, a triggered-but-unfilled position
  is checked first: fill at the limit price the first time a fresh mark is
  at or above it (a resting limit-sell). This mirrors a real stop-limit
  order's actual risk (it can fail to fill in a fast-moving market) rather
  than pretending it always executes.
- The global `options_intraday_stop` kill switch stays as-is — a master
  off-switch regardless of any account's configured policy.
- Prompt text in `options_allocator.py` describing the stop policy to the
  LLM allocator is updated to describe the account's actual policy instead
  of the old hardcoded knobs.
- `DTE_FLOOR` force-close (options expiring soon) is NOT a stop — it stays
  unconditional regardless of `stop_type`.

## Equity enforcement (new capability)

`web/spy_scanner.py::refresh_portfolio_prices` is today mark-to-market
only; equity cash is *derived* every call as
`starting_value − Σ live cost_basis`, with no ledger. A naive stop-sell
that just zeroed a position's `cost_basis` would silently return its full
original cost to cash and erase the realized loss, so this needs explicit
accounting, not just a snapshot mutation:

- Each live position (skipping `action=="EXITED"`) gets a new `peak_price`
  key, seeded at `entry_price`, ratcheted upward every refresh.
- When the account's stop policy fires for a position: mark it
  `action="EXITED"`, and add `exit_reason` (`stop_loss` / `trail_stop` /
  `stop_limit`), `exit_price`, and `exit_proceeds` (`shares × fill_price`).
  Fill convention matches options: filled at the stop level if the price
  crossed it this interval, otherwise at the observed price if it gapped
  through.
- Cash for the refresh becomes
  `starting_value − Σ live cost_basis + Σ (exit_proceeds − cost_basis)`
  over rows carrying `exit_proceeds` — realized losses (or gains) now
  persist across refreshes instead of evaporating.
- A stopped-out position gets a line in `rebalance_notes` (the existing
  signal-flip-notes field), and the weekly rebalance's LLM input (which
  reads the previous scan's `portfolio_json`) sees the EXITED row with its
  stop reason, so the allocator knows the position was stopped out rather
  than assuming it's still open or was manually closed.
- `stop`/`stop_limit` measure from `entry_price`; `trailing_pct`/
  `trailing_dollar` measure from the ratcheted `peak_price`. Equity
  stop-limit uses the same resting-fill semantics as options (a
  `pending_stop_limit` flag alongside the position instead of a DB column,
  since equity positions live in the JSON snapshot, not a row table).

## UI

Both the S&P and Options paper-account modals (the same create/edit forms
targeted by the companion scheduling spec) gain a "Stop Policy" section: a
`stop_type` select (None / Stop / Stop-limit / Trailing % / Trailing $)
and the relevant numeric value field(s), shown/hidden based on the selected
type. Same populate/reset/save JS functions and CRUD request bodies as the
scheduling fields. Helper copy states plainly that this is a simulated
stop enforced at the next hourly refresh, not a real brokerage order.

## Testing

- `effective_stop_level` (options): one test per type, including the
  stop-limit trigger-but-no-fill and subsequent resting-fill cases.
- Backfill migration: existing options accounts land on `stop`/60;
  existing equity accounts land on `none`.
- Both call sites (`forced_closes`, `_apply_intraday_stops`) exercise a
  non-default per-account policy, not just the migrated default.
- Equity: stop fire mutates the snapshot correctly for each type, cash
  derivation reflects a realized loss, peak ratchet only moves up,
  crossed-vs-gap-through fill selection, EXITED rows never re-evaluated.
- Route validation: rejects a non-`none` type with no value, rejects
  `stop_limit` with no limit offset, rejects an unknown type.
- Existing options tests that assert the old hardcoded −60%/trailing
  behavior are updated to exercise the policy-driven equivalent — not
  weakened or deleted.

## Risks / notes

- This is strictly a simulation change. No order-placement code exists in
  this repo today and none is added by this feature.
- Retiring the old combined fixed+trailing options behavior is a real
  behavior change for any options account that doesn't get an explicit
  policy choice post-migration beyond the `stop`/60 backfill — trailing
  protection an account relied on before this ships will need to be
  re-enabled by explicitly choosing `trailing_pct`/`trailing_dollar`.
- Equity accounts gaining stop-loss capability at all is new exposure to
  understand: a stop firing on a Monday could exit a position the weekly
  Saturday rebalance would otherwise have held through a dip. This is the
  intended behavior (that's what a stop-loss is for), noted here so it
  isn't a surprise once equity accounts start opting in.
