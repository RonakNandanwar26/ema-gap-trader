"""Unit tests for trader_daemon.app HTTP routes via FastAPI TestClient."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from trader_daemon import app as daemon_app
from trader_daemon.session_manager import SessionManager
from trader_daemon.wanted_store import WantedStore
from tests.test_session_manager import FakeTrader


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Sandbox data/ dir
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Swap singletons so each test gets a fresh manager + wanted.json
    fresh_manager = SessionManager()
    fresh_wanted = WantedStore(tmp_path / "wanted.json")
    daemon_app._reset_for_tests(fresh_manager, fresh_wanted)

    with patch("trader_daemon.session_manager.Trader", FakeTrader):
        with TestClient(daemon_app.app) as c:
            yield c
    # ensure we leave no threads behind between tests
    fresh_manager.shutdown_all(join_timeout=1.0)


def _valid_start_payload(mode="paper", instrument="NIFTY", variant=""):
    return {
        "mode": mode,
        "instrument": instrument,
        "variant": variant,
        "capital": 100000,
        "strategy": {
            "extra_entry_mode": "midtrend",
            "ema_gap_min": 0.03,
            "max_hold_candles": 20,
            "cooldown_candles": 3,
            "candle_interval": 15,
            "max_capital_per_trade_pct": 0.25,
            "orb_filter": True,
        },
        "instrument_config": {},
    }


def test_healthz_returns_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["sessions"] == 0


def test_list_sessions_initially_empty(client):
    r = client.get("/sessions")
    assert r.status_code == 200
    assert r.json() == {"sessions": []}


def test_get_unknown_session_returns_404(client):
    r = client.get("/sessions/bogus")
    assert r.status_code == 404


def test_start_paper_returns_202(client):
    r = client.post("/sessions/start", json=_valid_start_payload())
    assert r.status_code == 200 or r.status_code == 202  # FastAPI default since we dropped status_code
    body = r.json()
    assert body["session_key"] == "paper:NIFTY"
    assert body["status"] == "starting"


def test_start_is_idempotent_returns_409_second_time(client):
    r1 = client.post("/sessions/start", json=_valid_start_payload())
    assert r1.status_code in (200, 202)
    # give FakeTrader a moment to enter its loop
    time.sleep(0.1)
    r2 = client.post("/sessions/start", json=_valid_start_payload())
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["session_key"] == "paper:NIFTY"


def test_start_live_without_header_returns_400(client):
    payload = _valid_start_payload(mode="live")
    r = client.post("/sessions/start", json=payload)
    assert r.status_code == 400
    assert "X-Confirmed-Live" in r.json()["detail"]


def test_start_live_with_header_returns_accepted(client):
    payload = _valid_start_payload(mode="live")
    r = client.post(
        "/sessions/start",
        json=payload,
        headers={"X-Confirmed-Live": "true"},
    )
    assert r.status_code in (200, 202)
    assert r.json()["session_key"] == "live:NIFTY"


def test_start_unknown_instrument_returns_400(client):
    payload = _valid_start_payload(instrument="DOGECOIN")
    r = client.post("/sessions/start", json=payload)
    assert r.status_code == 400


def test_start_missing_strategy_fields_returns_400(client):
    payload = _valid_start_payload()
    payload["strategy"] = {"extra_entry_mode": "midtrend"}  # missing required fields
    r = client.post("/sessions/start", json=payload)
    assert r.status_code == 400


def test_stop_known_session_returns_200(client):
    client.post("/sessions/start", json=_valid_start_payload())
    time.sleep(0.05)
    r = client.post("/sessions/paper:NIFTY/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "stopping"


def test_stop_unknown_session_returns_404(client):
    r = client.post("/sessions/paper:BOGUS/stop")
    assert r.status_code == 404


def test_list_shows_started_session(client):
    client.post("/sessions/start", json=_valid_start_payload())
    time.sleep(0.05)
    r = client.get("/sessions")
    body = r.json()
    assert len(body["sessions"]) == 1
    sess = body["sessions"][0]
    assert sess["session_key"] == "paper:NIFTY"
    assert sess["is_running"] is True


def test_get_trades_on_running_session_returns_list(client):
    client.post("/sessions/start", json=_valid_start_payload())
    time.sleep(0.05)
    r = client.get("/sessions/paper:NIFTY/trades")
    assert r.status_code == 200
    assert r.json()["session_key"] == "paper:NIFTY"
    assert r.json()["recent_trades"] == []


def test_start_persists_to_wanted_json(client, tmp_path):
    r = client.post("/sessions/start", json=_valid_start_payload())
    assert r.status_code in (200, 202)
    import json
    wanted = json.loads((tmp_path / "wanted.json").read_text())
    assert "paper:NIFTY" in wanted


def test_stop_removes_from_wanted_json(client, tmp_path):
    client.post("/sessions/start", json=_valid_start_payload())
    client.post("/sessions/paper:NIFTY/stop")
    import json
    wanted = json.loads((tmp_path / "wanted.json").read_text())
    assert wanted == {}


def test_lifespan_auto_resumes_wanted_sessions(tmp_path, monkeypatch):
    """Seed wanted.json BEFORE creating TestClient; lifespan must start the session."""
    import json
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    fresh_manager = SessionManager()
    wanted_path = tmp_path / "wanted.json"
    wanted_path.write_text(json.dumps({
        "paper:NIFTY": _valid_start_payload(),
    }))
    fresh_wanted = WantedStore(wanted_path)
    daemon_app._reset_for_tests(fresh_manager, fresh_wanted)

    with patch("trader_daemon.session_manager.Trader", FakeTrader):
        with TestClient(daemon_app.app) as c:
            time.sleep(0.1)  # let the auto-resumed thread enter its loop
            r = c.get("/sessions")
            body = r.json()
            assert len(body["sessions"]) == 1
            assert body["sessions"][0]["session_key"] == "paper:NIFTY"
            assert body["sessions"][0]["is_running"] is True

    fresh_manager.shutdown_all(join_timeout=1.0)
