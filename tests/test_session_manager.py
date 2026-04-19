"""Unit tests for trader_daemon.session_manager.

The real Trader class logs into Angel One on construction, so we patch
`trader_daemon.session_manager.Trader` with a FakeTrader that mimics the
attributes SessionManager reads and a ``.run()`` that loops on ``.running``.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from config import INSTRUMENTS, StrategyConfig
from trader_daemon.session_manager import SessionManager


class FakeTrader:
    """Stand-in for trader.Trader that lets SessionManager smoke-run safely."""

    def __init__(
        self, sc, ic, capital, *, live=False, trade_store=None, label=""
    ) -> None:
        self.sc = sc
        self.ic = ic
        self.capital = capital
        self.live = live
        self.trade_store = trade_store
        self.label = label
        self.running = True
        self.current_equity = float(capital)
        self.initial_capital = float(capital)
        self.trades: list[dict] = []
        self.open_trade = None
        self._run_started = threading.Event()

    def run(self) -> None:
        self._run_started.set()
        while self.running:
            time.sleep(0.01)


def _sc() -> StrategyConfig:
    return StrategyConfig(
        extra_entry_mode="midtrend",
        ema_gap_min=0.03,
        max_hold_candles=20,
        cooldown_candles=3,
        candle_interval=15,
        max_capital_per_trade_pct=0.25,
        orb_filter=True,
    )


def _ic() -> object:
    return INSTRUMENTS["NIFTY"]


@pytest.fixture
def manager(tmp_path, monkeypatch):
    # TradeStore writes to get_data_dir() — redirect to tmp_path.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with patch("trader_daemon.session_manager.Trader", FakeTrader):
        yield SessionManager()


def test_start_launches_thread_and_sets_running(manager):
    entry, created = manager.start(
        session_key="paper:NIFTY",
        mode="paper",
        instrument="NIFTY",
        variant="",
        capital=100000,
        sc=_sc(),
        ic=_ic(),
    )
    assert created is True
    # Wait for FakeTrader.run() to actually enter its loop.
    assert entry.trader._run_started.wait(timeout=1.0)
    assert entry.is_running
    # status transitions: starting → running
    snapshot = manager.snapshot(entry)
    assert snapshot["status"] == "running"
    assert snapshot["is_running"] is True
    # Ensure the thread was registered
    assert manager.get("paper:NIFTY") is entry
    assert len(manager.list()) == 1

    # cleanup
    manager.shutdown_all(join_timeout=1.0)


def test_start_is_idempotent(manager):
    first, created1 = manager.start(
        session_key="paper:NIFTY", mode="paper", instrument="NIFTY", variant="",
        capital=100000, sc=_sc(), ic=_ic(),
    )
    assert created1 is True
    first.trader._run_started.wait(timeout=1.0)

    second, created2 = manager.start(
        session_key="paper:NIFTY", mode="paper", instrument="NIFTY", variant="",
        capital=100000, sc=_sc(), ic=_ic(),
    )
    assert created2 is False
    assert second is first   # same entry returned
    assert len(manager.list()) == 1

    manager.shutdown_all(join_timeout=1.0)


def test_stop_sets_running_false_and_thread_exits(manager):
    entry, _ = manager.start(
        session_key="paper:NIFTY", mode="paper", instrument="NIFTY", variant="",
        capital=100000, sc=_sc(), ic=_ic(),
    )
    entry.trader._run_started.wait(timeout=1.0)

    assert manager.stop("paper:NIFTY") is True
    entry.thread.join(timeout=1.0)
    assert not entry.thread.is_alive()
    assert manager.snapshot(entry)["is_running"] is False


def test_stop_unknown_session_returns_false(manager):
    assert manager.stop("paper:BOGUS") is False


def test_shutdown_all_stops_every_session(manager):
    keys = ["paper:NIFTY", "paper:BANKNIFTY"]
    for key in keys:
        inst = key.split(":")[1]
        manager.start(
            session_key=key, mode="paper", instrument=inst, variant="",
            capital=100000, sc=_sc(), ic=INSTRUMENTS[inst],
        )
    for key in keys:
        manager.get(key).trader._run_started.wait(timeout=1.0)

    manager.shutdown_all(join_timeout=2.0)
    for key in keys:
        entry = manager.get(key)
        assert not entry.is_running
        assert not entry.thread.is_alive()


def test_snapshot_when_no_trader_still_returns_dict(manager):
    """Snapshot must not crash for a session entry with no in-memory trader."""
    entry, _ = manager.start(
        session_key="paper:NIFTY", mode="paper", instrument="NIFTY", variant="",
        capital=100000, sc=_sc(), ic=_ic(),
    )
    entry.trader._run_started.wait(timeout=1.0)
    # Simulate a state where trader has exited (stop + joined)
    manager.stop("paper:NIFTY")
    entry.thread.join(timeout=1.0)
    snap = manager.snapshot(entry)
    assert snap["session_key"] == "paper:NIFTY"
    assert snap["is_running"] is False
    assert snap["trade_count"] == 0


def test_trader_exception_is_captured(manager):
    class CrashingTrader(FakeTrader):
        def run(self):
            self._run_started.set()
            raise RuntimeError("boom")

    with patch("trader_daemon.session_manager.Trader", CrashingTrader):
        entry, _ = manager.start(
            session_key="paper:NIFTY", mode="paper", instrument="NIFTY", variant="",
            capital=100000, sc=_sc(), ic=_ic(),
        )
        entry.trader._run_started.wait(timeout=1.0)
        entry.thread.join(timeout=1.0)

    snap = manager.snapshot(entry)
    assert snap["status"] == "error"
    assert "boom" in (snap["error"] or "")
