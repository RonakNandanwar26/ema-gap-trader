"""Shared fixtures for ema-gap-trader test suite."""

from __future__ import annotations

import sys
import os
from datetime import datetime, date, timedelta

import numpy as np
import pandas as pd
import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def sample_ohlcv_df():
    """Generate a realistic OHLCV DataFrame with random-walk prices."""
    def _make(n=100, start_price=20000.0, seed=42):
        rng = np.random.default_rng(seed)
        prices = [start_price]
        for _ in range(n - 1):
            prices.append(prices[-1] + rng.normal(0, 20))
        closes = np.array(prices)
        highs = closes + rng.uniform(5, 30, n)
        lows = closes - rng.uniform(5, 30, n)
        opens = closes + rng.normal(0, 10, n)
        timestamps = pd.date_range("2024-01-02 09:15", periods=n, freq="15min")
        return pd.DataFrame({
            "timestamp": timestamps,
            "open": opens, "high": highs, "low": lows, "close": closes,
            "volume": rng.integers(1000, 10000, n),
        })
    return _make


@pytest.fixture
def constant_ohlcv_df():
    """DataFrame with constant prices (edge case for div-by-zero)."""
    def _make(n=25, price=100.0):
        return pd.DataFrame({
            "timestamp": pd.date_range("2024-01-02 09:15", periods=n, freq="15min"),
            "open": [price] * n, "high": [price] * n,
            "low": [price] * n, "close": [price] * n,
            "volume": [1000] * n,
        })
    return _make


@pytest.fixture
def increasing_ohlcv_df():
    """Monotonically increasing prices."""
    def _make(n=25):
        prices = [100.0 + i * 2 for i in range(n)]
        return pd.DataFrame({
            "timestamp": pd.date_range("2024-01-02 09:15", periods=n, freq="15min"),
            "open": prices, "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices], "close": prices,
            "volume": [1000] * n,
        })
    return _make


@pytest.fixture
def decreasing_ohlcv_df():
    """Monotonically decreasing prices."""
    def _make(n=25):
        prices = [200.0 - i * 2 for i in range(n)]
        return pd.DataFrame({
            "timestamp": pd.date_range("2024-01-02 09:15", periods=n, freq="15min"),
            "open": prices, "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices], "close": prices,
            "volume": [1000] * n,
        })
    return _make


@pytest.fixture
def zero_ohlcv_df():
    """All-zero prices (edge case for EMA gap div-by-zero)."""
    def _make(n=25):
        return pd.DataFrame({
            "timestamp": pd.date_range("2024-01-02 09:15", periods=n, freq="15min"),
            "open": [0.0] * n, "high": [0.0] * n,
            "low": [0.0] * n, "close": [0.0] * n,
            "volume": [1000] * n,
        })
    return _make


@pytest.fixture
def sample_strategy_config():
    from config import StrategyConfig
    return StrategyConfig(
        extra_entry_mode="midtrend",
        ema_gap_min=0.03,
        max_hold_candles=20,
        cooldown_candles=3,
        candle_interval=15,
        max_capital_per_trade_pct=0.25,
        orb_filter=False,
    )


@pytest.fixture
def sample_instrument_config():
    from config import InstrumentConfig
    return InstrumentConfig(
        name="NIFTY", token="99926000", exchange="NSE", nfo_exchange="NFO",
        lot_size=65, strike_interval=50, expiry_weekday=1,
        expiry_type="weekly", expiry_flag="WEEK",
    )


@pytest.fixture
def sample_trade():
    """A single completed winning trade."""
    return {
        "entry": datetime(2024, 1, 2, 10, 0),
        "exit": datetime(2024, 1, 2, 11, 0),
        "dir": "CE", "spot_in": 21500.0, "spot_out": 21550.0,
        "strike": 21500, "prem_in": 150.0, "prem_out": 180.0,
        "prem_pnl": (180.0 - 150.0) * 65,
        "reason": "ST_flip", "entry_rsi": 55.0,
        "entry_gap": 0.05, "entry_type": "crossover",
    }


@pytest.fixture
def sample_trades():
    """Generate a list of mixed winning/losing trades."""
    def _make(n=10, seed=42):
        rng = np.random.default_rng(seed)
        trades = []
        base_time = datetime(2024, 1, 2, 9, 30)
        for i in range(n):
            pnl = float(rng.normal(0, 2000))
            direction = "CE" if rng.random() > 0.5 else "PE"
            reason = str(rng.choice(["ST_flip", "EMA_cross", "max_hold"]))
            entry_type = str(rng.choice(["crossover", "midtrend"]))
            trades.append({
                "entry": base_time + timedelta(hours=i * 2),
                "exit": base_time + timedelta(hours=i * 2 + 1),
                "dir": direction, "spot_in": 21500.0, "spot_out": 21500.0 + pnl / 65,
                "strike": 21500, "prem_in": 150.0,
                "prem_out": 150.0 + pnl / 65,
                "prem_pnl": pnl,
                "reason": reason, "entry_rsi": 55.0,
                "entry_gap": 0.05, "entry_type": entry_type,
            })
        return trades
    return _make


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Temporary directory for TradeStore file tests."""
    return str(tmp_path / "data")
