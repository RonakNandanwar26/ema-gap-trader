"""Tests for persistence.py — TradeStore JSON file-based persistence."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from persistence import TradeStore


class TestTradeStore:
    """Tests for TradeStore save/load/clear operations."""

    def test_save_and_load_open_trade(self, tmp_data_dir, sample_trade):
        """Round-trip: save with datetime fields, load back, compare."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        store.save_open_trade(sample_trade, current_equity=100_000.0)

        loaded = store.load_open_trade()
        assert loaded is not None
        assert loaded["dir"] == sample_trade["dir"]
        assert loaded["spot_in"] == sample_trade["spot_in"]
        assert loaded["spot_out"] == sample_trade["spot_out"]
        assert loaded["strike"] == sample_trade["strike"]
        assert loaded["prem_in"] == sample_trade["prem_in"]
        assert loaded["prem_out"] == sample_trade["prem_out"]
        assert loaded["entry"] == sample_trade["entry"]
        assert loaded["exit"] == sample_trade["exit"]
        assert loaded["current_equity"] == 100_000.0

    def test_clear_open_trade(self, tmp_data_dir, sample_trade):
        """After clear, load returns None."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        store.save_open_trade(sample_trade, current_equity=100_000.0)
        assert store.load_open_trade() is not None

        store.clear_open_trade()
        assert store.load_open_trade() is None

    def test_load_open_trade_no_file(self, tmp_data_dir):
        """No file -> None."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        assert store.load_open_trade() is None

    def test_append_trade_creates_file(self, tmp_data_dir, sample_trade):
        """First append creates the trades.json file."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        trades_path = Path(tmp_data_dir) / "NIFTY_paper_trades.json"
        assert not trades_path.exists()

        store.append_trade(sample_trade)
        assert trades_path.exists()

        trades = store.load_trades()
        assert len(trades) == 1

    def test_append_trade_deduplication(self, tmp_data_dir, sample_trade):
        """Same entry time -> not duplicated."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        store.append_trade(sample_trade)
        store.append_trade(sample_trade)

        trades = store.load_trades()
        assert len(trades) == 1

    def test_multiple_trades_accumulate(self, tmp_data_dir):
        """Append 3 trades, load all 3."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        for i in range(3):
            trade = {
                "entry": datetime(2024, 1, 2, 10 + i, 0),
                "exit": datetime(2024, 1, 2, 11 + i, 0),
                "dir": "CE",
                "spot_in": 21500.0,
                "spot_out": 21550.0,
                "strike": 21500,
                "prem_in": 150.0,
                "prem_out": 180.0,
                "prem_pnl": 1950.0,
                "reason": "ST_flip",
            }
            store.append_trade(trade)

        trades = store.load_trades()
        assert len(trades) == 3

    def test_save_and_load_equity(self, tmp_data_dir):
        """Round-trip for equity."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        store.save_equity(250_000.0)

        loaded = store.load_equity()
        assert loaded == 250_000.0

    def test_load_equity_no_file(self, tmp_data_dir):
        """No file -> None."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        assert store.load_equity() is None

    def test_corrupt_open_trade_file(self, tmp_data_dir):
        """Write invalid JSON, load returns None, .corrupt created."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        open_path = Path(tmp_data_dir) / "NIFTY_paper_open.json"
        open_path.write_text("{invalid json content!!!}")

        result = store.load_open_trade()
        assert result is None

        corrupt_path = open_path.with_suffix(".corrupt")
        assert corrupt_path.exists()
        assert not open_path.exists()

    def test_corrupt_trades_file(self, tmp_data_dir):
        """Write invalid JSON to trades, load returns []."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        trades_path = Path(tmp_data_dir) / "NIFTY_paper_trades.json"
        trades_path.write_text("{invalid json content!!!}")

        result = store.load_trades()
        assert result == []

        corrupt_path = trades_path.with_suffix(".corrupt")
        assert corrupt_path.exists()
        assert not trades_path.exists()

    def test_datetime_serialization(self, tmp_data_dir):
        """datetime preserved through save/load cycle."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        entry_dt = datetime(2024, 3, 15, 10, 30, 45)
        exit_dt = datetime(2024, 3, 15, 14, 15, 0)
        trade = {
            "entry": entry_dt,
            "exit": exit_dt,
            "entry_time": entry_dt,
            "dir": "CE",
            "spot_in": 21500.0,
            "spot_out": 21550.0,
            "strike": 21500,
            "prem_in": 150.0,
            "prem_out": 180.0,
            "prem_pnl": 1950.0,
            "reason": "ST_flip",
        }
        store.save_open_trade(trade, current_equity=100_000.0)

        loaded = store.load_open_trade()
        assert isinstance(loaded["entry"], datetime)
        assert isinstance(loaded["exit"], datetime)
        assert isinstance(loaded["entry_time"], datetime)
        assert loaded["entry"] == entry_dt
        assert loaded["exit"] == exit_dt
        assert loaded["entry_time"] == entry_dt

    def test_date_serialization(self, tmp_data_dir):
        """date (expiry) preserved through save/load cycle."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        expiry_date = date(2024, 3, 21)
        trade = {
            "entry": datetime(2024, 3, 15, 10, 0),
            "exit": datetime(2024, 3, 15, 11, 0),
            "expiry": expiry_date,
            "dir": "CE",
            "spot_in": 21500.0,
            "spot_out": 21550.0,
            "strike": 21500,
            "prem_in": 150.0,
            "prem_out": 180.0,
            "prem_pnl": 1950.0,
            "reason": "ST_flip",
        }
        store.save_open_trade(trade, current_equity=100_000.0)

        loaded = store.load_open_trade()
        assert isinstance(loaded["expiry"], date)
        assert loaded["expiry"] == expiry_date

    def test_pandas_timestamp_serialization(self, tmp_data_dir):
        """pd.Timestamp converted and preserved through save/load cycle."""
        store = TradeStore("NIFTY", "paper", data_dir=tmp_data_dir)
        ts = pd.Timestamp("2024-03-15 10:30:00")
        trade = {
            "entry": ts,
            "exit": pd.Timestamp("2024-03-15 14:15:00"),
            "dir": "CE",
            "spot_in": 21500.0,
            "spot_out": 21550.0,
            "strike": 21500,
            "prem_in": 150.0,
            "prem_out": 180.0,
            "prem_pnl": 1950.0,
            "reason": "ST_flip",
        }
        store.save_open_trade(trade, current_equity=100_000.0)

        loaded = store.load_open_trade()
        # After round-trip, known datetime fields are deserialized as datetime
        assert isinstance(loaded["entry"], datetime)
        assert loaded["entry"] == datetime(2024, 3, 15, 10, 30, 0)
        assert isinstance(loaded["exit"], datetime)
        assert loaded["exit"] == datetime(2024, 3, 15, 14, 15, 0)
