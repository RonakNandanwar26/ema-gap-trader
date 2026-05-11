"""Tests for trader.py — Trader class methods with mocked broker/notifications."""

from __future__ import annotations

import pytest
import pandas as pd
from datetime import datetime, date
from unittest.mock import patch, MagicMock

from trader import Trader
from config import StrategyConfig, InstrumentConfig
from persistence import TradeStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trader(sc, ic, store=None, live=False):
    """Create a Trader instance for testing (no side effects)."""
    t = Trader(strat_config=sc, inst_config=ic, capital=100000, live=live, trade_store=store)
    return t


def _make_row(**overrides):
    """Build a minimal pd.Series that satisfies process_exit / process_entry."""
    defaults = {
        "close": 21500.0, "high": 21520.0, "low": 21480.0,
        "ema9": 21510.0, "ema21": 21500.0,
        "st_dir": 1, "rsi": 55.0, "ema_gap_pct": 0.05,
        "ema_gap_expanding": True,
        "timestamp": pd.Timestamp("2024-01-02 10:00"),
    }
    defaults.update(overrides)
    return pd.Series(defaults)


def _make_open_trade(**overrides):
    """Build a minimal open_trade dict with all required keys."""
    defaults = {
        "dir": "CE",
        "entry_time": datetime(2024, 1, 2, 9, 30),
        "entry_price": 150.0,
        "spot_entry": 21500.0,
        "strike": 21500,
        "quantity": 65,
        "symbol": "NIFTY02JAN24C21500",
        "token": "12345",
        "candle_num": 5,
        "rsi": 55.0,
        "gap": 0.05,
    }
    defaults.update(overrides)
    return defaults


# ===========================================================================
# TestProcessExit
# ===========================================================================

class TestProcessExit:
    """Tests for Trader._process_exit()."""

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_exit_with_valid_premium(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Standard exit: premium fetched, pnl computed, trade closed."""
        mock_broker.fetch_ltp.return_value = 180.0

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = _make_open_trade(entry_price=150.0, quantity=65)

        t._process_exit("ST_flip", _make_row())

        # pnl = (180 - 150) * 65 = 1950
        assert len(t.trades) == 1
        assert t.trades[0]["prem_pnl"] == pytest.approx(1950.0)
        assert t.trades[0]["prem_out"] == 180.0
        assert t.trades[0]["reason"] == "ST_flip"
        assert t.open_trade is None

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_exit_with_none_premium(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Regression: exit_premium=None must not crash the f-string."""
        mock_broker.fetch_ltp.return_value = None

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = _make_open_trade(entry_price=150.0)

        t._process_exit("ST_flip", _make_row())

        assert len(t.trades) == 1
        assert t.trades[0]["prem_pnl"] is None  # no premium -> no pnl
        assert t.open_trade is None

        # Telegram message should contain "Rs.0.0" for the exit price
        msg = mock_tg.call_args[0][0]
        assert "Rs.0.0" in msg

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_exit_with_zero_premium(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Contract expired worthless: premium=0.0 should not crash."""
        mock_broker.fetch_ltp.return_value = 0.0

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = _make_open_trade(entry_price=150.0, quantity=65)

        t._process_exit("max_hold", _make_row())

        assert len(t.trades) == 1
        # pnl is None because exit_premium is 0 (falsy), so the `if exit_premium and ...` check fails
        assert t.trades[0]["prem_pnl"] is None
        assert t.open_trade is None

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_equity_updated(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """current_equity should increase by pnl after a winning exit."""
        mock_broker.fetch_ltp.return_value = 200.0

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        assert t.current_equity == 100000.0
        t.open_trade = _make_open_trade(entry_price=150.0, quantity=65)

        t._process_exit("EMA_cross", _make_row())

        # pnl = (200 - 150) * 65 = 3250
        assert t.current_equity == pytest.approx(103250.0)

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_equity_not_updated_when_pnl_none(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Equity must remain unchanged when pnl is None (no premium fetched)."""
        mock_broker.fetch_ltp.return_value = None

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = _make_open_trade()

        t._process_exit("ST_flip", _make_row())

        assert t.current_equity == 100000.0

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_store_persisted(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config, tmp_data_dir):
        """With a real TradeStore, verify append_trade and clear_open_trade are called."""
        mock_broker.fetch_ltp.return_value = 180.0

        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        t = _make_trader(sample_strategy_config, sample_instrument_config, store=store)
        t.open_trade = _make_open_trade()

        t._process_exit("ST_flip", _make_row())

        # Trade should be persisted
        trades = store.load_trades()
        assert len(trades) == 1
        assert trades[0]["reason"] == "ST_flip"

        # Open trade file should be cleared
        assert store.load_open_trade() is None

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_exit_no_open_trade(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Calling _process_exit with no open_trade should be a no-op."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = None

        t._process_exit("ST_flip", _make_row())

        assert len(t.trades) == 0
        mock_tg.assert_not_called()

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_exit_row_none(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Exit with row=None (WebSocket exit) should not crash; spot_out=None."""
        mock_broker.fetch_ltp.return_value = 170.0

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = _make_open_trade(entry_price=150.0, quantity=65)

        t._process_exit("ws_ST_flip", None)

        assert len(t.trades) == 1
        assert t.trades[0]["spot_out"] is None
        assert t.trades[0]["prem_pnl"] == pytest.approx(1300.0)  # (170-150)*65


# ===========================================================================
# TestProcessEntry
# ===========================================================================

class TestProcessEntry:
    """Tests for Trader._process_entry()."""

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_paper_entry_ce(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Successful CE paper entry at ATM strike."""
        mock_broker.resolve_option.return_value = ("NIFTY02JAN24C21500", "12345")
        mock_broker.fetch_ltp.return_value = 150.0  # affordable: 150*65=9750 < 25000

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._scrip_master = {}
        t._current_expiry = date(2024, 1, 4)

        row = _make_row(close=21500.0, rsi=55.0, ema_gap_pct=0.05)
        t._process_entry("CE", row, date(2024, 1, 2), candle_num=5)

        assert t.open_trade is not None
        assert t.open_trade["dir"] == "CE"
        assert t.open_trade["entry_price"] == 150.0
        assert t.open_trade["quantity"] == 65
        assert t.open_trade["symbol"] == "NIFTY02JAN24C21500"

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_paper_entry_pe(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Successful PE paper entry."""
        mock_broker.resolve_option.return_value = ("NIFTY02JAN24P21500", "12346")
        mock_broker.fetch_ltp.return_value = 120.0

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._scrip_master = {}
        t._current_expiry = date(2024, 1, 4)

        row = _make_row(close=21500.0)
        t._process_entry("PE", row, date(2024, 1, 2), candle_num=3)

        assert t.open_trade is not None
        assert t.open_trade["dir"] == "PE"
        assert t.open_trade["entry_price"] == 120.0

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_skip_unaffordable(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """All 3 strikes unaffordable -> open_trade stays None."""
        mock_broker.resolve_option.return_value = ("NIFTY02JAN24C21500", "12345")
        # 500 * 65 = 32500, budget = 100000 * 0.25 = 25000
        mock_broker.fetch_ltp.return_value = 500.0

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._scrip_master = {}
        t._current_expiry = date(2024, 1, 4)

        row = _make_row(close=21500.0)
        t._process_entry("CE", row, date(2024, 1, 2), candle_num=5)

        assert t.open_trade is None

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_skip_none_premium(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """LTP returns None -> open_trade stays None."""
        mock_broker.resolve_option.return_value = ("NIFTY02JAN24C21500", "12345")
        mock_broker.fetch_ltp.return_value = None

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._scrip_master = {}
        t._current_expiry = date(2024, 1, 4)

        row = _make_row(close=21500.0)
        t._process_entry("CE", row, date(2024, 1, 2), candle_num=5)

        assert t.open_trade is None

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_telegram_sent(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Verify send_telegram called with 'ENTRY' in message on successful entry."""
        mock_broker.resolve_option.return_value = ("NIFTY02JAN24C21500", "12345")
        mock_broker.fetch_ltp.return_value = 150.0

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._scrip_master = {}
        t._current_expiry = date(2024, 1, 4)

        row = _make_row(close=21500.0)
        t._process_entry("CE", row, date(2024, 1, 2), candle_num=5)

        mock_tg.assert_called()
        msg = mock_tg.call_args[0][0]
        assert "ENTRY" in msg

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_skip_telegram_sent(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """On skip (unaffordable), telegram should contain 'SKIP'."""
        mock_broker.resolve_option.return_value = ("NIFTY02JAN24C21500", "12345")
        mock_broker.fetch_ltp.return_value = 500.0

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._scrip_master = {}
        t._current_expiry = date(2024, 1, 4)

        row = _make_row(close=21500.0)
        t._process_entry("CE", row, date(2024, 1, 2), candle_num=5)

        mock_tg.assert_called()
        msg = mock_tg.call_args[0][0]
        assert "SKIP" in msg

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_store_persisted_on_entry(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config, tmp_data_dir):
        """With a real TradeStore, verify open trade is saved to disk."""
        mock_broker.resolve_option.return_value = ("NIFTY02JAN24C21500", "12345")
        mock_broker.fetch_ltp.return_value = 150.0

        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        t = _make_trader(sample_strategy_config, sample_instrument_config, store=store)
        t._scrip_master = {}
        t._current_expiry = date(2024, 1, 4)

        row = _make_row(close=21500.0)
        t._process_entry("CE", row, date(2024, 1, 2), candle_num=5)

        recovered = store.load_open_trade()
        assert recovered is not None
        assert recovered["dir"] == "CE"
        assert recovered["entry_price"] == 150.0


# ===========================================================================
# TestHandleRecovery
# ===========================================================================

class TestHandleRecovery:
    """Tests for Trader._handle_recovery()."""

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_expired_contract(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Expired contract: closed with full loss, open_trade=None."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)

        recovered = {
            "expiry": date(2024, 1, 1),
            "entry_time": datetime(2024, 1, 1, 10, 0),
            "entry_price": 100.0,
            "spot_entry": 21500.0,
            "strike": 21500,
            "quantity": 65,
            "symbol": "NIFTY01JAN24C21500",
            "token": "12345",
            "dir": "CE",
            "candle_num": 5,
            "rsi": 55.0,
            "gap": 0.05,
        }
        today = date(2024, 1, 5)  # after expiry

        t._handle_recovery(recovered, today)

        # Trade should be recorded as expired loss
        assert len(t.trades) == 1
        assert t.trades[0]["reason"] == "recovery_expired"
        assert t.trades[0]["prem_pnl"] == pytest.approx(-(100.0 * 65))
        assert t.open_trade is None

        # Equity should be reduced
        assert t.current_equity == pytest.approx(100000.0 - 6500.0)

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_expired_contract_telegram(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Expired recovery should send telegram with 'RECOVERY CLOSED'."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)

        recovered = {
            "expiry": date(2024, 1, 1),
            "entry_time": datetime(2024, 1, 1, 10, 0),
            "entry_price": 100.0,
            "spot_entry": 21500.0,
            "strike": 21500,
            "quantity": 65,
            "symbol": "NIFTY01JAN24C21500",
            "token": "12345",
            "dir": "CE",
            "candle_num": 5,
            "rsi": 55.0,
            "gap": 0.05,
        }
        t._handle_recovery(recovered, date(2024, 1, 5))

        # At least 2 calls: initial recovery message + closed message
        assert mock_tg.call_count >= 2
        all_msgs = [call[0][0] for call in mock_tg.call_args_list]
        assert any("RECOVERY CLOSED" in m for m in all_msgs)

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_valid_contract_restored(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Valid contract (not expired): position restored to open_trade."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)

        recovered = {
            "expiry": date(2024, 2, 1),  # well in the future
            "entry_time": datetime(2024, 1, 2, 10, 0),
            "entry_price": 150.0,
            "spot_entry": 21500.0,
            "strike": 21500,
            "quantity": 65,
            "symbol": "NIFTY01FEB24C21500",
            "token": "12345",
            "dir": "CE",
            "candle_num": 3,
            "rsi": 55.0,
            "gap": 0.05,
        }
        today = date(2024, 1, 5)  # before expiry

        t._handle_recovery(recovered, today)

        assert t.open_trade is not None
        assert t.open_trade["dir"] == "CE"
        assert t.open_trade["entry_price"] == 150.0
        assert t.open_trade["symbol"] == "NIFTY01FEB24C21500"
        assert t.open_trade["token"] == "12345"
        assert t.open_trade["quantity"] == 65
        assert len(t.trades) == 0  # no trade recorded, position is still open

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_valid_contract_telegram(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """Restored position should send telegram with 'RECOVERY: position restored'."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)

        recovered = {
            "expiry": date(2024, 2, 1),
            "entry_time": datetime(2024, 1, 2, 10, 0),
            "entry_price": 150.0,
            "spot_entry": 21500.0,
            "strike": 21500,
            "quantity": 65,
            "symbol": "NIFTY01FEB24C21500",
            "token": "12345",
            "dir": "CE",
            "candle_num": 3,
            "rsi": 55.0,
            "gap": 0.05,
        }
        t._handle_recovery(recovered, date(2024, 1, 5))

        all_msgs = [call[0][0] for call in mock_tg.call_args_list]
        assert any("position restored" in m for m in all_msgs)

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_expired_store_persisted(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config, tmp_data_dir):
        """Expired recovery should persist the closed trade to store."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        t = _make_trader(sample_strategy_config, sample_instrument_config, store=store)

        recovered = {
            "expiry": date(2024, 1, 1),
            "entry_time": datetime(2024, 1, 1, 10, 0),
            "entry_price": 100.0,
            "spot_entry": 21500.0,
            "strike": 21500,
            "quantity": 65,
            "symbol": "NIFTY01JAN24C21500",
            "token": "12345",
            "dir": "CE",
            "candle_num": 5,
            "rsi": 55.0,
            "gap": 0.05,
        }
        t._handle_recovery(recovered, date(2024, 1, 5))

        trades = store.load_trades()
        assert len(trades) == 1
        assert trades[0]["reason"] == "recovery_expired"


# ===========================================================================
# TestCheckVirtualExit
# ===========================================================================

class TestCheckVirtualExit:
    """Tests for Trader._check_virtual_exit()."""

    def test_no_tick_manager(self, sample_strategy_config, sample_instrument_config):
        """No tick manager -> returns None immediately."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._tick_manager = None
        t.open_trade = _make_open_trade()
        t._last_df = pd.DataFrame()

        result = t._check_virtual_exit(candle_count=10)
        assert result is None

    def test_no_open_trade(self, sample_strategy_config, sample_instrument_config):
        """No open trade -> returns None immediately."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._tick_manager = MagicMock()
        t.open_trade = None
        t._last_df = pd.DataFrame()

        result = t._check_virtual_exit(candle_count=10)
        assert result is None

    def test_no_last_df(self, sample_strategy_config, sample_instrument_config):
        """No cached DataFrame -> returns None immediately."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._tick_manager = MagicMock()
        t.open_trade = _make_open_trade()
        t._last_df = None

        result = t._check_virtual_exit(candle_count=10)
        assert result is None

    def test_no_spot_ltp(self, sample_strategy_config, sample_instrument_config):
        """Tick manager returns None for spot LTP -> returns None."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t._tick_manager = MagicMock()
        t._tick_manager.get_ltp.return_value = None
        t.open_trade = _make_open_trade()
        t._last_df = pd.DataFrame({"close": [100.0]})

        result = t._check_virtual_exit(candle_count=10)
        assert result is None

    @patch("trader.compute_indicators", side_effect=Exception("bad data"))
    def test_compute_indicators_exception(self, mock_ci, sample_strategy_config, sample_instrument_config, sample_ohlcv_df):
        """compute_indicators raises -> returns None instead of crashing."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)

        t._tick_manager = MagicMock()
        t._tick_manager.get_ltp.return_value = 21500.0
        t.open_trade = _make_open_trade()

        # Need a real DataFrame with all required columns
        df = sample_ohlcv_df(n=20)
        t._last_df = df

        result = t._check_virtual_exit(candle_count=10)
        assert result is None
        mock_ci.assert_called_once()

    @patch("trader.check_exit", return_value="ST_flip")
    @patch("trader.compute_indicators")
    def test_virtual_exit_triggered(self, mock_ci, mock_check_exit, sample_strategy_config, sample_instrument_config, sample_ohlcv_df):
        """When check_exit returns a reason, _check_virtual_exit passes it through."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)

        t._tick_manager = MagicMock()
        t._tick_manager.get_ltp.return_value = 21500.0
        t.open_trade = _make_open_trade(candle_num=5)

        df = sample_ohlcv_df(n=20)
        t._last_df = df

        # compute_indicators returns the df as-is (already has needed cols from mock)
        mock_ci.return_value = df

        result = t._check_virtual_exit(candle_count=10)
        assert result == "ST_flip"

    @patch("trader.check_exit", return_value=None)
    @patch("trader.compute_indicators")
    def test_virtual_exit_not_triggered(self, mock_ci, mock_check_exit, sample_strategy_config, sample_instrument_config, sample_ohlcv_df):
        """When check_exit returns None, _check_virtual_exit returns None."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)

        t._tick_manager = MagicMock()
        t._tick_manager.get_ltp.return_value = 21500.0
        t.open_trade = _make_open_trade(candle_num=5)

        df = sample_ohlcv_df(n=20)
        t._last_df = df
        mock_ci.return_value = df

        result = t._check_virtual_exit(candle_count=10)
        assert result is None


# ===========================================================================
# TestIntradayForceExit
# ===========================================================================

class TestIntradayForceExit:
    """Tests for intraday force-exit / no-carry-forward behavior."""

    def test_config_constants(self):
        """Force-exit times must be wired to 14:55 / 15:10."""
        from datetime import time as dtime
        from config import NO_ENTRY_AFTER, TIME_EXIT, MARKET_CLOSE
        assert NO_ENTRY_AFTER == dtime(14, 55)
        assert TIME_EXIT == dtime(15, 10)
        assert TIME_EXIT < MARKET_CLOSE  # exit before close

    def test_trader_imports_time_constants(self):
        """trader.py must import TIME_EXIT and NO_ENTRY_AFTER."""
        import trader as trader_mod
        assert hasattr(trader_mod, "TIME_EXIT")
        assert hasattr(trader_mod, "NO_ENTRY_AFTER")

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_force_exit_closes_open_trade(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """_force_exit_position closes the open trade with reason 'time_exit'."""
        mock_broker.fetch_ltp.return_value = 170.0
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = _make_open_trade(entry_price=150.0, quantity=65)
        t._last_df = pd.DataFrame([_make_row().to_dict()])

        t._force_exit_position("time_exit")

        assert t.open_trade is None
        assert len(t.trades) == 1
        assert t.trades[0]["reason"] == "time_exit"
        assert t.trades[0]["prem_pnl"] == pytest.approx((170.0 - 150.0) * 65)

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_force_exit_noop_when_no_open_trade(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """_force_exit_position is a no-op when no trade is open."""
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = None
        t._last_df = None

        t._force_exit_position("time_exit")  # should not raise

        assert t.open_trade is None
        assert t.trades == []

    @patch("trader.send_telegram")
    @patch("trader.broker")
    def test_force_exit_works_without_cached_df(self, mock_broker, mock_tg, sample_strategy_config, sample_instrument_config):
        """If _last_df is None, force-exit still closes (spot_out becomes None)."""
        mock_broker.fetch_ltp.return_value = 160.0
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = _make_open_trade(entry_price=150.0, quantity=65)
        t._last_df = None

        t._force_exit_position("time_exit")

        assert t.open_trade is None
        assert len(t.trades) == 1
        assert t.trades[0]["reason"] == "time_exit"
        assert t.trades[0]["spot_out"] is None

    def test_should_block_new_entry_before_cutoff(self, sample_strategy_config, sample_instrument_config):
        """Entries allowed before NO_ENTRY_AFTER."""
        from datetime import time as dtime
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        assert t._should_block_new_entry(dtime(9, 30)) is False
        assert t._should_block_new_entry(dtime(14, 54, 59)) is False

    def test_should_block_new_entry_at_or_after_cutoff(self, sample_strategy_config, sample_instrument_config):
        """Entries blocked at and after NO_ENTRY_AFTER."""
        from datetime import time as dtime
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        assert t._should_block_new_entry(dtime(14, 55)) is True
        assert t._should_block_new_entry(dtime(14, 56)) is True
        assert t._should_block_new_entry(dtime(15, 9)) is True

    def test_should_exit_loop_predicate(self, sample_strategy_config, sample_instrument_config):
        """Loop must exit at or after TIME_EXIT (15:10)."""
        from datetime import time as dtime
        t = _make_trader(sample_strategy_config, sample_instrument_config)
        assert t._should_exit_loop(dtime(15, 9, 59)) is False
        assert t._should_exit_loop(dtime(15, 10)) is True
        assert t._should_exit_loop(dtime(15, 30)) is True

    # ---- Integration: drive run() end-to-end with a mocked clock ----

    @patch("trader.send_telegram")
    @patch("trader.broker")
    @patch("trader.WS_ENABLED", False)
    def test_run_force_exits_open_trade_at_time_exit(
        self, mock_broker, mock_tg,
        sample_strategy_config, sample_instrument_config,
    ):
        """Loop guard at TIME_EXIT must break run() and force-close any open trade."""
        from datetime import datetime as real_dt, date as real_date

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return real_dt(2026, 5, 11, 15, 11, 0)

        mock_broker.login.return_value = None
        mock_broker.load_scrip_master.return_value = {}
        mock_broker.get_current_expiry.return_value = real_date(2026, 5, 13)
        mock_broker.fetch_ltp.return_value = 200.0

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = _make_open_trade(entry_price=150.0, quantity=65)
        t._wait_until = MagicMock()
        t._is_market_open_today = MagicMock(return_value=True)

        with patch("trader.datetime", FakeDT):
            t.run()

        assert t.open_trade is None, "force-exit must close the open trade"
        assert len(t.trades) == 1
        assert t.trades[0]["reason"] == "time_exit"
        assert t.trades[0]["prem_pnl"] == pytest.approx((200.0 - 150.0) * 65)
        # Verify the old carry-forward telegram never went out
        all_tg_calls = " ".join(str(c) for c in mock_tg.call_args_list)
        assert "CARRY FORWARD" not in all_tg_calls

    @patch("trader.send_telegram")
    @patch("trader.broker")
    @patch("trader.WS_ENABLED", False)
    def test_run_no_op_at_time_exit_when_no_open_trade(
        self, mock_broker, mock_tg,
        sample_strategy_config, sample_instrument_config,
    ):
        """Loop exit at TIME_EXIT with no open trade is a clean no-op (no trades, no exceptions)."""
        from datetime import datetime as real_dt, date as real_date

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return real_dt(2026, 5, 11, 15, 11, 0)

        mock_broker.login.return_value = None
        mock_broker.load_scrip_master.return_value = {}
        mock_broker.get_current_expiry.return_value = real_date(2026, 5, 13)

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = None
        t._wait_until = MagicMock()
        t._is_market_open_today = MagicMock(return_value=True)

        with patch("trader.datetime", FakeDT):
            t.run()  # must not raise

        assert t.open_trade is None
        assert t.trades == []

    @patch("trader.check_entry")
    @patch("trader.compute_indicators")
    @patch("trader.send_telegram")
    @patch("trader.broker")
    @patch("trader.WS_ENABLED", False)
    def test_run_blocks_entries_after_no_entry_cutoff(
        self, mock_broker, mock_tg, mock_ci, mock_check_entry,
        sample_strategy_config, sample_instrument_config, sample_ohlcv_df,
    ):
        """When current time >= NO_ENTRY_AFTER, check_entry() must NOT be called."""
        from datetime import datetime as real_dt, date as real_date

        # Shared mutable clock — iter 1 at 14:56, then advances past TIME_EXIT to break the loop
        clock = {"now": real_dt(2026, 5, 11, 14, 56, 0)}

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]

        mock_broker.login.return_value = None
        mock_broker.load_scrip_master.return_value = {}
        mock_broker.get_current_expiry.return_value = real_date(2026, 5, 13)

        # Build a DF that has all indicator columns compute_indicators would add
        from datetime import date as real_date2
        base = sample_ohlcv_df(n=20)
        # Force timestamps onto today (= real date.today()) so the
        # "no fresh candles" guard in run() doesn't short-circuit the loop.
        today_real = real_date2.today()
        base["timestamp"] = pd.date_range(
            pd.Timestamp(today_real) + pd.Timedelta(hours=9, minutes=15),
            periods=len(base), freq="15min",
        )
        base["ema9"] = 21500.0
        base["ema21"] = 21495.0
        base["ema_gap_pct"] = 0.05
        base["ema_gap_expanding"] = True
        base["st_dir"] = 1
        base["rsi"] = 55.0
        mock_ci.return_value = base
        mock_broker.fetch_candles.return_value = base
        mock_check_entry.return_value = None  # in case it's somehow called

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = None
        t._wait_until = MagicMock()
        t._is_market_open_today = MagicMock(return_value=True)

        # Advance the clock past TIME_EXIT after one iteration so the loop exits cleanly
        def advance_clock(_candle_count):
            clock["now"] = real_dt(2026, 5, 11, 15, 11, 0)
        t._sleep_with_exit_monitoring = advance_clock

        with patch("trader.datetime", FakeDT):
            t.run()

        # The critical assertion: check_entry must NOT have been called when now >= 14:55
        mock_check_entry.assert_not_called()

    @patch("trader.check_entry")
    @patch("trader.compute_indicators")
    @patch("trader.send_telegram")
    @patch("trader.broker")
    @patch("trader.WS_ENABLED", False)
    def test_run_allows_entries_before_no_entry_cutoff(
        self, mock_broker, mock_tg, mock_ci, mock_check_entry,
        sample_strategy_config, sample_instrument_config, sample_ohlcv_df,
    ):
        """Sanity: when current time < NO_ENTRY_AFTER, check_entry() IS called."""
        from datetime import datetime as real_dt, date as real_date

        clock = {"now": real_dt(2026, 5, 11, 10, 30, 0)}

        class FakeDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]

        mock_broker.login.return_value = None
        mock_broker.load_scrip_master.return_value = {}
        mock_broker.get_current_expiry.return_value = real_date(2026, 5, 13)

        from datetime import date as real_date2
        base = sample_ohlcv_df(n=20)
        # Force timestamps onto today (= real date.today()) so the
        # "no fresh candles" guard in run() doesn't short-circuit the loop.
        today_real = real_date2.today()
        base["timestamp"] = pd.date_range(
            pd.Timestamp(today_real) + pd.Timedelta(hours=9, minutes=15),
            periods=len(base), freq="15min",
        )
        base["ema9"] = 21500.0
        base["ema21"] = 21495.0
        base["ema_gap_pct"] = 0.05
        base["ema_gap_expanding"] = True
        base["st_dir"] = 1
        base["rsi"] = 55.0
        mock_ci.return_value = base
        mock_broker.fetch_candles.return_value = base
        mock_check_entry.return_value = None

        t = _make_trader(sample_strategy_config, sample_instrument_config)
        t.open_trade = None
        t._wait_until = MagicMock()
        t._is_market_open_today = MagicMock(return_value=True)

        def advance_clock(_candle_count):
            clock["now"] = real_dt(2026, 5, 11, 15, 11, 0)
        t._sleep_with_exit_monitoring = advance_clock

        with patch("trader.datetime", FakeDT):
            t.run()

        # The complementary assertion: check_entry IS called when now < 14:55
        mock_check_entry.assert_called()
