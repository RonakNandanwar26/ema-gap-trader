"""Tests for tick_manager.py — TickManager LTP cache and subscription logic."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from tick_manager import TickManager, EXCHANGE_NSE_CM, EXCHANGE_NSE_FO


def _make_manager() -> TickManager:
    """Create a TickManager without starting the WebSocket."""
    return TickManager("auth", "key", "client", "feed")


class TestTickManager:
    def test_get_ltp_unknown_token(self):
        """Fresh manager returns None for an unknown token."""
        tm = _make_manager()
        assert tm.get_ltp("unknown") is None

    def test_on_data_stores_ltp(self):
        """_on_data stores the LTP and get_ltp retrieves it."""
        tm = _make_manager()
        tm._on_data(None, {"token": "12345", "last_traded_price": 15050})
        assert tm.get_ltp("12345") == 150.50

    def test_on_data_paisa_conversion(self):
        """Paisa-to-rupees conversion: 9999 paisa -> 99.99 rupees."""
        tm = _make_manager()
        tm._on_data(None, {"token": "99999", "last_traded_price": 9999})
        assert tm.get_ltp("99999") == 99.99

    def test_subscribe_tracks(self):
        """subscribe() appends the correct structure to _subscriptions."""
        tm = _make_manager()
        tm.subscribe("12345", EXCHANGE_NSE_FO)

        assert len(tm._subscriptions) == 1
        sub = tm._subscriptions[0]
        assert sub["exchangeType"] == EXCHANGE_NSE_FO
        assert sub["tokens"] == ["12345"]

    def test_unsubscribe_removes_from_cache(self):
        """unsubscribe() clears the LTP cache entry and removes the subscription."""
        tm = _make_manager()
        # Pre-populate cache and subscription
        tm._ltp_cache["12345"] = 100.0
        tm._subscriptions.append({"exchangeType": EXCHANGE_NSE_FO, "tokens": ["12345"]})

        tm.unsubscribe("12345", EXCHANGE_NSE_FO)

        assert tm.get_ltp("12345") is None
        assert len(tm._subscriptions) == 0

    def test_on_open_sets_connected(self):
        """_on_open sets _connected to True."""
        tm = _make_manager()
        assert tm._connected is False
        # _on_open resubscribes, so give it an empty subscriptions list (default)
        tm._on_open(None)
        assert tm._connected is True

    def test_on_close_sets_disconnected(self):
        """_on_close sets _connected to False."""
        tm = _make_manager()
        tm._connected = True
        tm._on_close(None)
        assert tm._connected is False

    def test_on_error_sets_disconnected(self):
        """_on_error sets _connected to False."""
        tm = _make_manager()
        tm._connected = True
        tm._on_error(None, "some error")
        assert tm._connected is False

    def test_get_ltp_thread_safety(self):
        """Concurrent reads/writes to LTP cache should not raise exceptions."""
        tm = _make_manager()
        errors: list[Exception] = []

        def writer(tid: int):
            try:
                for i in range(100):
                    tm._on_data(None, {"token": str(tid), "last_traded_price": i * 100})
            except Exception as exc:
                errors.append(exc)

        def reader(tid: int):
            try:
                for _ in range(100):
                    tm.get_ltp(str(tid))
            except Exception as exc:
                errors.append(exc)

        threads = []
        for t in range(10):
            threads.append(threading.Thread(target=writer, args=(t,)))
            threads.append(threading.Thread(target=reader, args=(t,)))

        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5)

        assert errors == [], f"Thread-safety errors: {errors}"

    def test_subscribe_connected_snapshot_inside_lock(self):
        """Verify the race-condition fix: _connected is read inside the lock.

        When connected with a live _sws, subscribe() should call _sws.subscribe.
        When disconnected, it should NOT call _sws.subscribe.
        """
        tm = _make_manager()
        mock_sws = MagicMock()
        tm._sws = mock_sws

        # --- connected: should forward to _sws.subscribe ---
        tm._connected = True
        tm.subscribe("11111", EXCHANGE_NSE_FO)
        mock_sws.subscribe.assert_called_once()
        call_args = mock_sws.subscribe.call_args
        assert call_args[0][0] == "sub_11111"  # correlation_id
        assert call_args[0][1] == 1             # LTP_MODE
        assert call_args[0][2] == [{"exchangeType": EXCHANGE_NSE_FO, "tokens": ["11111"]}]

        mock_sws.reset_mock()

        # --- disconnected: should NOT forward to _sws.subscribe ---
        tm._connected = False
        tm.subscribe("22222", EXCHANGE_NSE_CM)
        mock_sws.subscribe.assert_not_called()
