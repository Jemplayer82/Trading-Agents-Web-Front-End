"""Options learning loop: P&L attribution, track-record stats, batch reflection.

Attribution splits realized P&L into a directional component (delta x underlying
move) and a residual ("time/vol decay") — the split that reveals the key
options-native failure mode: direction RIGHT but premium still lost.
"""

import json
from unittest.mock import MagicMock

import pytest

from web import db, options_learning

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


@pytest.fixture()
def account_id(tmp_db):
    aid = db.create_paper_account("Learn Test", starting_capital=100_000.0, kind="options")
    db.append_options_cash(aid, "deposit", 100_000.0, note="initial deposit")
    return aid


def _row(**over):
    """A closed CALL position row shaped like options_positions SELECT *."""
    base = {
        "id": 1, "put_call": "CALL", "strike": 230.0, "underlying": "AAPL",
        "signal": "BUY", "conviction": 8,
        "entry_premium": 4.00, "exit_premium": 6.00,
        "cost_basis": 800.0, "realized_pnl": 400.0, "contracts": 2,
        "entry_underlying": 200.0, "exit_underlying": 210.0,
        "exit_underlying_source": "live", "entry_delta": 0.45,
        "expiration_date": "2026-08-21",
        "opened_at": "2026-07-20T14:00:00Z", "closed_at": "2026-07-27T14:00:00Z",
        "exit_reason": "llm_close", "status": "closed", "settlement_close": None,
    }
    base.update(over)
    return base


# ── grade_position ───────────────────────────────────────────────────────────

def test_call_win_attribution():
    g = options_learning.grade_position(_row())
    assert g["attributed"] and g["won"] and g["direction_correct"]
    assert g["quadrant"] == "right_won"
    # dU=+10, |delta|=.45 → directional +4.5pt; total +2.0pt → residual -2.5pt
    assert g["directional_points"] == pytest.approx(4.5)
    assert g["residual_points"] == pytest.approx(-2.5)
    assert g["return_pct"] == pytest.approx(0.5)
    assert g["days_held"] == 7
    assert g["dte_entry"] == 32


def test_put_win_attribution():
    g = options_learning.grade_position(_row(
        put_call="PUT", entry_underlying=200.0, exit_underlying=190.0,
        entry_premium=4.0, exit_premium=7.0, realized_pnl=600.0, signal="SELL",
    ))
    assert g["attributed"] and g["direction_correct"] and g["quadrant"] == "right_won"
    # dU=-10, sign(PUT)=-1 → directional = .45*-1*-10 = +4.5pt
    assert g["directional_points"] == pytest.approx(4.5)


def test_right_but_lost_is_the_decay_quadrant():
    """Underlying moved the right way but premium still died — decay toll."""
    g = options_learning.grade_position(_row(
        exit_premium=2.0, realized_pnl=-400.0,
        entry_underlying=200.0, exit_underlying=204.0,  # +2% — right direction
    ))
    assert g["direction_correct"] is True and g["won"] is False
    assert g["quadrant"] == "right_lost"
    assert g["residual_points"] < 0  # decay ate more than direction gave


def test_flat_move_is_neither_right_nor_wrong():
    """A flat underlying is a PURE-theta outcome, not a wrong directional call —
    blaming direction for what decay did would poison the lessons."""
    g = options_learning.grade_position(_row(exit_underlying=200.5))  # +0.25% < 0.5%
    assert g["direction_correct"] is None
    g_lost = options_learning.grade_position(_row(
        exit_underlying=200.5, exit_premium=2.0, realized_pnl=-400.0))
    assert g_lost["quadrant"] == "flat_lost"


def test_decay_share_uses_attributed_losers_denominator():
    """Numerator and denominator must cover the same population: unattributed
    losers (no delta / no exit spot) can't be classified, so they must not
    dilute the decay-toll share."""
    rows = (
        _many(2, exit_premium=2.0, realized_pnl=-400.0,
              entry_underlying=200.0, exit_underlying=204.0)  # right_lost
        + _many(2, exit_premium=2.0, realized_pnl=-400.0, entry_delta=None)  # unattributable losers
        + _many(8)  # winners
    )
    stats = options_learning.compute_options_stats(rows, min_closed=10)
    assert stats["n_attributed_losers"] == 2
    assert stats["decay_lost_share_of_losers"] == pytest.approx(1.0)


def test_null_delta_skips_attribution_keeps_stats():
    g = options_learning.grade_position(_row(entry_delta=None))
    assert g["attributed"] is False
    assert g["return_pct"] == pytest.approx(0.5)  # still counted in win-rate stats
    assert "quadrant" not in g


def test_expiry_uses_settlement_close():
    g = options_learning.grade_position(_row(
        exit_reason="expiry", status="expired_itm",
        exit_underlying=None, settlement_close=212.0,
    ))
    assert g["attributed"] is True
    assert g["underlying_move_pct"] == pytest.approx(0.06)


# ── compute_options_stats + format_track_record ──────────────────────────────

def _many(n, **over):
    return [_row(id=i, **over) for i in range(n)]


def test_stats_gated_below_min_closed():
    assert options_learning.compute_options_stats(_many(9), min_closed=10) == {}
    assert options_learning.format_track_record({}, "irrelevant") == ""


def test_stats_and_block_render():
    rows = _many(8) + _many(4, exit_premium=1.0, realized_pnl=-600.0, exit_reason="stop_loss")
    stats = options_learning.compute_options_stats(rows, min_closed=10)
    assert stats["n"] == 12
    assert stats["win_rate"] == pytest.approx(8 / 12)
    assert "stop_loss" in stats["by_exit_reason"]
    assert "[n<5 — ignore]" in stats["by_exit_reason"]["stop_loss"]  # only 4 stops
    block = options_learning.format_track_record(stats, "- watch for decay on slow movers")
    assert block.startswith("=== OPTIONS TRACK RECORD")
    assert "watch for decay" in block
    assert block.rstrip().endswith(options_learning._CAUTION)


def test_block_char_cap_preserves_caution():
    stats = options_learning.compute_options_stats(_many(15), min_closed=10)
    block = options_learning.format_track_record(stats, "- watch for x" * 400, max_chars=600)
    assert len(block) <= 600 + len(options_learning._CAUTION) + 2
    assert block.rstrip().endswith(options_learning._CAUTION)


# ── run_batch_reflection ─────────────────────────────────────────────────────

def _open_and_close(account_id, n, scan_id):
    for i in range(n):
        pid = db.open_options_position(account_id, scan_id, {
            "occ_symbol": f"AAPL  260821C0023{i:04d}0", "underlying": "AAPL",
            "put_call": "CALL", "strike": 230.0 + i, "expiration_date": "2026-08-21",
            "contracts": 1, "entry_premium": 4.0, "entry_underlying": 200.0,
            "entry_delta": 0.45, "entry_bid": 3.9, "entry_ask": 4.1, "entry_oi": 500,
            "signal": "BUY", "conviction": 7, "rationale": "t", "data_source": "test",
        })
        db.close_options_position(pid, 5.0, "llm_close",
                                  exit_underlying=205.0, exit_underlying_source="live")


def test_reflection_one_llm_call_and_row(account_id):
    scan = db.create_spy_scan("2026-07-29", paper_account_id=account_id, kind="options")
    _open_and_close(account_id, 6, scan)
    llm = MagicMock()
    llm.invoke.return_value.content = "- watch for entry DTE shorter than realistic hold time"
    ok = options_learning.run_batch_reflection(
        account_id, llm, {"options_reflect_min_new_closed": 5, "quick_think_llm": "m"})
    assert ok and llm.invoke.call_count == 1
    lesson = db.latest_options_lesson(account_id)
    assert lesson and "watch for entry DTE" in lesson["lessons_md"]
    assert lesson["n_new"] == 6
    assert json.loads(lesson["stats_json"])["n"] == 6


def test_reflection_gated_below_min_new(account_id):
    scan = db.create_spy_scan("2026-07-29", paper_account_id=account_id, kind="options")
    _open_and_close(account_id, 3, scan)
    llm = MagicMock()
    ok = options_learning.run_batch_reflection(
        account_id, llm, {"options_reflect_min_new_closed": 5})
    assert not ok
    llm.invoke.assert_not_called()
    assert db.latest_options_lesson(account_id) is None


def test_reflection_idempotent_without_new_closes(account_id):
    """A rerun with no new closes since the last lessons row is a no-op."""
    scan = db.create_spy_scan("2026-07-29", paper_account_id=account_id, kind="options")
    _open_and_close(account_id, 6, scan)
    llm = MagicMock()
    llm.invoke.return_value.content = "- watch for x"
    cfg = {"options_reflect_min_new_closed": 5}
    assert options_learning.run_batch_reflection(account_id, llm, cfg)
    assert not options_learning.run_batch_reflection(account_id, llm, cfg)
    assert llm.invoke.call_count == 1


def test_reflection_deferred_without_llm(account_id):
    scan = db.create_spy_scan("2026-07-29", paper_account_id=account_id, kind="options")
    _open_and_close(account_id, 6, scan)
    assert not options_learning.run_batch_reflection(
        account_id, None, {"options_reflect_min_new_closed": 5})
    assert db.latest_options_lesson(account_id) is None  # stays pending → retries


# ── backfill_exit_underlyings ────────────────────────────────────────────────

def test_backfill_fills_only_null_nonexpiry_rows(account_id, monkeypatch):
    scan = db.create_spy_scan("2026-07-29", paper_account_id=account_id, kind="options")
    # One close WITHOUT live capture, one WITH.
    pid_null = db.open_options_position(account_id, scan, {
        "occ_symbol": "MSFT  260821C00400000", "underlying": "MSFT", "put_call": "CALL",
        "strike": 400.0, "expiration_date": "2026-08-21", "contracts": 1,
        "entry_premium": 4.0, "entry_underlying": 390.0, "entry_delta": 0.4,
        "entry_bid": 3.9, "entry_ask": 4.1, "entry_oi": 100,
        "signal": "BUY", "conviction": 6, "rationale": "t", "data_source": "test",
    })
    db.close_options_position(pid_null, 3.0, "stop_loss")  # no exit_underlying
    pid_live = db.open_options_position(account_id, scan, {
        "occ_symbol": "NVDA  260821C00190000", "underlying": "NVDA", "put_call": "CALL",
        "strike": 190.0, "expiration_date": "2026-08-21", "contracts": 1,
        "entry_premium": 4.0, "entry_underlying": 185.0, "entry_delta": 0.5,
        "entry_bid": 3.9, "entry_ask": 4.1, "entry_oi": 100,
        "signal": "BUY", "conviction": 6, "rationale": "t", "data_source": "test",
    })
    db.close_options_position(pid_live, 5.0, "llm_close",
                              exit_underlying=188.0, exit_underlying_source="live")

    from web import options_engine
    monkeypatch.setattr(options_engine, "underlying_close_on_or_before",
                        lambda und, date: 395.5)
    filled = options_learning.backfill_exit_underlyings(account_id)
    assert filled == 1

    rows = {r["id"]: r for r in db.list_options_positions(account_id, status="settled")}
    assert rows[pid_null]["exit_underlying"] == pytest.approx(395.5)
    assert rows[pid_null]["exit_underlying_source"] == "eod_close"
    # Live capture untouched by the backfill.
    assert rows[pid_live]["exit_underlying"] == pytest.approx(188.0)
    assert rows[pid_live]["exit_underlying_source"] == "live"
