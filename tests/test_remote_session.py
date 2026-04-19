"""Unit tests for remote_session.RemoteSession (HTTP client)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from config import INSTRUMENTS, StrategyConfig
from remote_session import RemoteSession, TradingStatus, daemon_is_reachable


def _sc() -> StrategyConfig:
    return StrategyConfig(
        extra_entry_mode="midtrend", ema_gap_min=0.03, max_hold_candles=20,
        cooldown_candles=3, candle_interval=15, max_capital_per_trade_pct=0.25,
        orb_filter=True,
    )


def _session(live=False, variant=""):
    return RemoteSession(
        _sc(), INSTRUMENTS["NIFTY"], capital=100000, live=live, variant=variant,
        daemon_url="http://test:1234",
    )


def _mk_response(status_code: int, json_body: dict | None = None, text: str = ""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.text = text
    return r


def test_start_paper_sends_correct_payload():
    s = _session()
    with patch("remote_session.requests.post", return_value=_mk_response(202, {"session_key": "paper:NIFTY", "status": "starting"})) as m:
        s.start()
    args, kwargs = m.call_args
    assert args[0] == "http://test:1234/sessions/start"
    body = kwargs["json"]
    assert body["mode"] == "paper"
    assert body["instrument"] == "NIFTY"
    assert body["capital"] == 100000
    assert "ema_gap_min" in body["strategy"]
    assert body["instrument_config"]["name"] == "NIFTY"
    assert "X-Confirmed-Live" not in (kwargs["headers"] or {})


def test_start_live_adds_confirm_header():
    s = _session(live=True)
    with patch("remote_session.requests.post", return_value=_mk_response(202)) as m:
        s.start()
    headers = m.call_args.kwargs["headers"]
    assert headers["X-Confirmed-Live"] == "true"


def test_start_409_is_treated_as_success():
    s = _session()
    with patch("remote_session.requests.post", return_value=_mk_response(409)):
        s.start()  # must not raise
    assert s._last_error is None


def test_start_daemon_unreachable_records_error():
    s = _session()
    with patch("remote_session.requests.post", side_effect=requests.ConnectionError("no route")):
        s.start()
    assert s._last_error is not None
    assert "unreachable" in s._last_error


def test_is_running_true_from_daemon():
    s = _session()
    body = {"is_running": True, "current_equity": 100500, "initial_capital": 100000,
            "trade_count": 2, "open_trade": None, "recent_trades": [], "error": None}
    with patch("remote_session.requests.get", return_value=_mk_response(200, body)):
        assert s.is_running is True


def test_is_running_false_when_daemon_returns_404():
    s = _session()
    with patch("remote_session.requests.get", return_value=_mk_response(404)):
        assert s.is_running is False


def test_is_running_false_when_daemon_unreachable():
    s = _session()
    with patch("remote_session.requests.get", side_effect=requests.ConnectionError("x")):
        assert s.is_running is False


def test_get_status_propagates_fields_from_daemon():
    s = _session()
    body = {
        "is_running": True,
        "current_equity": 102500.5,
        "initial_capital": 100000,
        "trade_count": 3,
        "open_trade": {"dir": "CE", "entry_price": 120.5},
        "recent_trades": [{"pnl": 500}],
        "error": None,
    }
    with patch("remote_session.requests.get", return_value=_mk_response(200, body)):
        status = s.get_status()
    assert isinstance(status, TradingStatus)
    assert status.is_running is True
    assert status.current_equity == 102500.5
    assert status.trade_count == 3
    assert status.open_trade["dir"] == "CE"
    assert status.recent_trades == [{"pnl": 500}]
    assert status.error is None


def test_get_status_returns_error_when_daemon_unreachable():
    s = _session()
    with patch("remote_session.requests.get", side_effect=requests.ConnectionError("x")):
        status = s.get_status()
    assert status.is_running is False
    assert status.current_equity == 100000.0
    assert "unreachable" in (status.error or "")


def test_status_is_cached_within_ttl():
    s = _session()
    body = {"is_running": True, "current_equity": 1, "initial_capital": 1,
            "trade_count": 0, "open_trade": None, "recent_trades": [], "error": None}
    with patch("remote_session.requests.get", return_value=_mk_response(200, body)) as m:
        s.get_status()
        s.get_status()
        s.get_status()
    # 3 calls within < 1s TTL → only 1 HTTP round-trip
    assert m.call_count == 1


def test_stop_calls_daemon_endpoint():
    s = _session()
    with patch("remote_session.requests.post", return_value=_mk_response(200)) as m:
        s.stop()
    assert m.call_args.args[0] == "http://test:1234/sessions/paper:NIFTY/stop"


def test_stop_tolerates_daemon_unreachable():
    s = _session()
    with patch("remote_session.requests.post", side_effect=requests.ConnectionError("x")):
        s.stop()  # must not raise


def test_session_key_with_variant():
    s = _session(variant="variant")
    assert s._session_key == "paper:NIFTY:variant"


def test_live_session_key():
    s = _session(live=True)
    assert s._session_key == "live:NIFTY"


def test_daemon_is_reachable_true_on_200():
    with patch("remote_session.requests.get", return_value=_mk_response(200)):
        assert daemon_is_reachable("http://x") is True


def test_daemon_is_reachable_false_on_connection_error():
    with patch("remote_session.requests.get", side_effect=requests.ConnectionError("x")):
        assert daemon_is_reachable("http://x") is False
