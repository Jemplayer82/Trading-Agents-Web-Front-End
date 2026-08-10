import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web import db, spy_routes

pytestmark = pytest.mark.unit


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    app = FastAPI()
    app.include_router(spy_routes.router)
    with TestClient(app) as c:
        yield c


def _create_account(client, **kwargs):
    defaults = {"name": "Test", "kind": "equity"}
    defaults.update(kwargs)
    resp = client.post("/api/paper-accounts", json=defaults)
    assert resp.status_code == 200
    return resp.json()["account"]


def test_reject_bad_schedule_time(client):
    for bad in ("9:15", "25:00", "abc"):
        resp = client.post(
            "/api/paper-accounts",
            json={"name": f"bad-{bad}", "kind": "equity", "schedule_time": bad},
        )
        assert resp.status_code == 400, bad
        assert "schedule_time" in resp.json()["detail"]


def test_reject_bad_stop_type(client):
    resp = client.post(
        "/api/paper-accounts",
        json={"name": "bad-stop", "kind": "equity", "stop_type": "parabolic"},
    )
    assert resp.status_code == 400
    assert "stop_type" in resp.json()["detail"]


def test_reject_stop_without_value(client):
    resp = client.post(
        "/api/paper-accounts",
        json={"name": "no-value", "kind": "equity", "stop_type": "stop"},
    )
    assert resp.status_code == 400
    assert "stop_value" in resp.json()["detail"]


def test_reject_trailing_pct_zero(client):
    resp = client.post(
        "/api/paper-accounts",
        json={
            "name": "trail-zero",
            "kind": "equity",
            "stop_type": "trailing_pct",
            "stop_value": 0,
        },
    )
    assert resp.status_code == 400


def test_reject_trailing_dollar_negative(client):
    resp = client.post(
        "/api/paper-accounts",
        json={
            "name": "trail-neg",
            "kind": "equity",
            "stop_type": "trailing_dollar",
            "stop_value": -5,
        },
    )
    assert resp.status_code == 400


def test_reject_stop_limit_without_offset(client):
    resp = client.post(
        "/api/paper-accounts",
        json={
            "name": "sl-no-off",
            "kind": "equity",
            "stop_type": "stop_limit",
            "stop_value": 60,
        },
    )
    assert resp.status_code == 400
    assert "stop_limit_offset" in resp.json()["detail"]


def test_reject_stop_value_abc(client):
    resp = client.post(
        "/api/paper-accounts",
        json={
            "name": "val-abc",
            "kind": "equity",
            "stop_type": "stop",
            "stop_value": "abc",
        },
    )
    assert resp.status_code == 400


def test_default_schedule_time_equity(client):
    acct = _create_account(client, name="eq-default")
    assert acct["schedule_time"] == "00:00"


def test_default_schedule_time_options(client):
    acct = _create_account(client, name="opt-default", kind="options")
    assert acct["schedule_time"] == "07:30"


def test_schedule_time_roundtrip(client):
    acct = _create_account(client, name="eq-945", schedule_time="09:45")
    assert acct["schedule_time"] == "09:45"


def test_schedule_time_null_manual(client):
    acct = _create_account(client, name="eq-null", schedule_time=None)
    assert acct["schedule_time"] is None


def test_schedule_time_empty_manual(client):
    acct = _create_account(client, name="eq-empty", schedule_time="")
    assert acct["schedule_time"] is None


def test_no_stop_keys_defaults_to_none(client):
    acct = _create_account(client, name="no-stop")
    assert acct["stop_type"] == "none"
    assert acct["stop_value"] is None
    assert acct["stop_limit_offset"] is None


def test_stop_roundtrip(client):
    acct = _create_account(client, name="stop-60", stop_type="stop", stop_value=60)
    assert acct["stop_type"] == "stop"
    assert acct["stop_value"] == 60.0
    assert acct["stop_limit_offset"] is None


def test_stop_limit_roundtrip(client):
    acct = _create_account(
        client,
        name="sl-60-5",
        stop_type="stop_limit",
        stop_value=60,
        stop_limit_offset=5,
    )
    assert acct["stop_type"] == "stop_limit"
    assert acct["stop_value"] == 60.0
    assert acct["stop_limit_offset"] == 5.0


def test_stop_none_normalizes_value(client):
    acct = _create_account(client, name="none-60", stop_type="none", stop_value=60)
    assert acct["stop_type"] == "none"
    assert acct["stop_value"] is None
    assert acct["stop_limit_offset"] is None


def test_put_name_leaves_other_fields_unchanged(client):
    acct = _create_account(
        client,
        name="sentinel",
        kind="equity",
        schedule_time="09:15",
        bias="bullish",
        aggressiveness=8,
        stop_type="stop",
        stop_value=60,
    )
    resp = client.put(f"/api/paper-accounts/{acct['id']}", json={"name": "Renamed"})
    assert resp.status_code == 200
    updated = resp.json()["account"]
    assert updated["name"] == "Renamed"
    assert updated["schedule_time"] == "09:15"
    assert updated["bias"] == "bullish"
    assert updated["aggressiveness"] == 8
    assert updated["stop_type"] == "stop"
    assert updated["stop_value"] == 60.0
    assert updated["stop_limit_offset"] is None


def test_put_schedule_time_null_clears(client):
    acct = _create_account(client, name="clear-null", schedule_time="09:15")
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"schedule_time": None}
    )
    assert resp.status_code == 200
    assert resp.json()["account"]["schedule_time"] is None


def test_put_schedule_time_empty_clears(client):
    acct = _create_account(client, name="clear-empty", schedule_time="09:15")
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"schedule_time": ""}
    )
    assert resp.status_code == 200
    assert resp.json()["account"]["schedule_time"] is None


def test_put_schedule_time_invalid_unchanged(client):
    acct = _create_account(client, name="bad-put", schedule_time="09:15")
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"schedule_time": "99:99"}
    )
    assert resp.status_code == 400
    row = db.get_paper_account(acct["id"])
    assert row["schedule_time"] == "09:15"


def test_put_stop_type_none_nulls_values(client):
    acct = _create_account(
        client, name="stop-to-none", stop_type="stop", stop_value=60
    )
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"stop_type": "none"}
    )
    assert resp.status_code == 200
    updated = resp.json()["account"]
    assert updated["stop_type"] == "none"
    assert updated["stop_value"] is None
    assert updated["stop_limit_offset"] is None


def test_put_stop_value_partial_update(client):
    acct = _create_account(
        client, name="stop-partial", stop_type="stop", stop_value=60
    )
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"stop_value": 45}
    )
    assert resp.status_code == 200
    updated = resp.json()["account"]
    assert updated["stop_type"] == "stop"
    assert updated["stop_value"] == 45.0


def test_put_stop_limit_without_offset_rejected(client):
    acct = _create_account(
        client, name="no-offset", stop_type="stop", stop_value=60
    )
    resp = client.put(
        f"/api/paper-accounts/{acct['id']}", json={"stop_type": "stop_limit"}
    )
    assert resp.status_code == 400
    row = db.get_paper_account(acct["id"])
    assert row["stop_type"] == "stop"
    assert row["stop_value"] == 60.0


def test_latest_refresh_prices_fans_out_across_accounts(client):
    acct1 = _create_account(client, name="fan-out-1")
    acct2 = _create_account(client, name="fan-out-2")

    scan1 = db.create_spy_scan("2026-08-10", paper_account_id=acct1["id"])
    db.update_spy_scan_prices(
        scan1, current_value=0.0, rebalance_notes="", portfolio_json=[]
    )
    scan2 = db.create_spy_scan("2026-08-10", paper_account_id=acct2["id"])
    db.update_spy_scan_prices(
        scan2, current_value=0.0, rebalance_notes="", portfolio_json=[]
    )

    with db.connect() as conn:
        conn.execute(
            "UPDATE spy_scans SET status = 'completed' WHERE id IN (?, ?)",
            (scan1, scan2),
        )

    resp = client.post("/api/spy-scans/latest/refresh-prices")
    assert resp.status_code == 200
    assert len(resp.json()["scans"]) == 2


def test_latest_refresh_prices_404_when_no_scans(client):
    resp = client.post("/api/spy-scans/latest/refresh-prices")
    assert resp.status_code == 404
