"""Brute-force throttling on POST /api/auth/login.

Closes the "login rate-limiting" finding deferred in SECURITY_AUDIT.md:
failed attempts are recorded per username and per client IP in the shared
SQLite DB; once either count crosses its threshold inside the sliding
window, the endpoint answers 429 before any PBKDF2 work happens. A
successful login clears the username's counter.

The DB is redirected to a tmp path (web.db.DB_PATH is read per-call), and
PBKDF2 iterations are dropped so repeated hash/verify calls stay fast.
"""
from __future__ import annotations

import pytest

WRONG = {"username": "alice", "password": "wrong-password"}
RIGHT = {"username": "alice", "password": "correct-horse"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-secret-token")
    from web import auth_app, db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    monkeypatch.setattr(auth_app, "_PBKDF2_ITERS", 1_000)

    from fastapi.testclient import TestClient

    from web.main import app

    with TestClient(app) as c:
        db.create_user("alice", auth_app.hash_password("correct-horse"))
        yield c


def _age_all_attempts(minutes: int) -> None:
    """Backdate every recorded attempt, simulating the window sliding past."""
    from datetime import datetime, timedelta

    from web import db

    old = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    with db.connect() as conn:
        conn.execute("UPDATE login_attempts SET attempted_at = ?", (old,))


@pytest.mark.unit
def test_wrong_password_still_401_below_threshold(client):
    for _ in range(4):
        assert client.post("/api/auth/login", json=WRONG).status_code == 401
    # Counter not yet at 5 -> correct password still works.
    assert client.post("/api/auth/login", json=RIGHT).status_code == 200


@pytest.mark.unit
def test_five_failures_lock_the_username(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json=WRONG).status_code == 401
    # Locked: even the correct password is refused with 429, not 401/200.
    assert client.post("/api/auth/login", json=RIGHT).status_code == 429


@pytest.mark.unit
def test_lockout_expires_when_window_slides(client):
    for _ in range(5):
        client.post("/api/auth/login", json=WRONG)
    assert client.post("/api/auth/login", json=RIGHT).status_code == 429
    _age_all_attempts(minutes=16)
    assert client.post("/api/auth/login", json=RIGHT).status_code == 200


@pytest.mark.unit
def test_successful_login_resets_the_counter(client):
    for _ in range(4):
        client.post("/api/auth/login", json=WRONG)
    assert client.post("/api/auth/login", json=RIGHT).status_code == 200
    # Fresh window after success: four more failures still return 401 ...
    for _ in range(4):
        assert client.post("/api/auth/login", json=WRONG).status_code == 401
    # ... and the correct password still gets in.
    assert client.post("/api/auth/login", json=RIGHT).status_code == 200


@pytest.mark.unit
def test_unknown_username_is_throttled_the_same_way(client):
    """Lockout must not become a user-enumeration oracle."""
    ghost = {"username": "nobody", "password": "whatever"}
    for _ in range(5):
        assert client.post("/api/auth/login", json=ghost).status_code == 401
    assert client.post("/api/auth/login", json=ghost).status_code == 429


@pytest.mark.unit
def test_ip_threshold_catches_username_spraying(client):
    """20 failures from one address lock the address, whatever the username."""
    for i in range(20):
        r = client.post(
            "/api/auth/login",
            json={"username": f"user{i}", "password": "x"},
            headers={"x-real-ip": "203.0.113.7"},
        )
        assert r.status_code == 401
    r = client.post(
        "/api/auth/login", json=RIGHT, headers={"x-real-ip": "203.0.113.7"}
    )
    assert r.status_code == 429
    # A different source address is unaffected (alice herself has no failures).
    r = client.post(
        "/api/auth/login", json=RIGHT, headers={"x-real-ip": "198.51.100.9"}
    )
    assert r.status_code == 200


@pytest.mark.unit
def test_startup_purge_drops_stale_rows(client):
    from web import db

    for _ in range(3):
        client.post("/api/auth/login", json=WRONG)
    _age_all_attempts(minutes=16)
    assert db.purge_stale_login_attempts(15) == 3
    with db.connect() as conn:
        left = conn.execute("SELECT COUNT(*) AS n FROM login_attempts").fetchone()["n"]
    assert left == 0
