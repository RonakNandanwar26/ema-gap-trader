"""Tests for indicators.py — SuperTrend, EMA, RSI, EMA Gap."""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import compute_supertrend, compute_indicators


# ---------------------------------------------------------------------------
# SuperTrend
# ---------------------------------------------------------------------------
class TestComputeSupertrend:
    def test_returns_two_series(self, sample_ohlcv_df):
        df = sample_ohlcv_df(n=50)
        st, direction = compute_supertrend(df)
        assert isinstance(st, pd.Series)
        assert isinstance(direction, pd.Series)

    def test_direction_values(self, sample_ohlcv_df):
        df = sample_ohlcv_df(n=100)
        _, direction = compute_supertrend(df)
        allowed = {-1, 0, 1}
        assert set(direction.unique()).issubset(allowed)

    def test_short_data(self, sample_ohlcv_df):
        """When data length < period, most values should be None."""
        df = sample_ohlcv_df(n=5)
        st, direction = compute_supertrend(df, period=10)
        # With only 5 rows and period=10, no ATR can be computed
        assert st.isna().all() or all(v is None for v in st)


# ---------------------------------------------------------------------------
# Compute Indicators
# ---------------------------------------------------------------------------
class TestComputeIndicators:
    EXPECTED_COLUMNS = [
        "ema9", "ema21", "st", "st_dir",
        "ema_gap_pct", "ema_gap_expanding", "rsi",
    ]

    def test_all_columns_present(self, sample_ohlcv_df):
        df = compute_indicators(sample_ohlcv_df(n=50))
        for col in self.EXPECTED_COLUMNS:
            assert col in df.columns, f"Missing column: {col}"

    def test_rsi_range_valid(self, sample_ohlcv_df):
        """RSI must be in [0, 100] with no NaN on normal data."""
        df = compute_indicators(sample_ohlcv_df(n=100))
        rsi = df["rsi"]
        assert not rsi.isna().any(), "RSI contains NaN"
        assert (rsi >= 0).all() and (rsi <= 100).all()

    def test_rsi_constant_prices(self, constant_ohlcv_df):
        """BUG REGRESSION: constant prices must produce RSI = 50, not NaN."""
        df = compute_indicators(constant_ohlcv_df(n=25, price=100.0))
        rsi = df["rsi"]
        assert not rsi.isna().any(), "RSI contains NaN on constant prices"
        # After the warm-up window, all RSI values should be 50
        assert (rsi.iloc[14:] == 50).all()

    def test_rsi_monotonic_increase(self, increasing_ohlcv_df):
        """All gains -> RSI should approach or equal 100."""
        df = compute_indicators(increasing_ohlcv_df(n=25))
        rsi_tail = df["rsi"].iloc[-1]
        assert rsi_tail == 100.0

    def test_rsi_monotonic_decrease(self, decreasing_ohlcv_df):
        """All losses -> RSI should approach or equal 0."""
        df = compute_indicators(decreasing_ohlcv_df(n=25))
        rsi_tail = df["rsi"].iloc[-1]
        assert rsi_tail == 0.0

    def test_ema_gap_zero_prices(self, zero_ohlcv_df):
        """BUG REGRESSION: zero prices must produce ema_gap_pct = 0, not NaN/inf."""
        df = compute_indicators(zero_ohlcv_df(n=25))
        gap = df["ema_gap_pct"]
        assert not gap.isna().any(), "ema_gap_pct contains NaN on zero prices"
        assert np.isfinite(gap).all(), "ema_gap_pct contains inf on zero prices"
        assert (gap == 0).all()

    def test_ema_gap_expanding(self, sample_ohlcv_df):
        """When gap widens, ema_gap_expanding should be True."""
        df = compute_indicators(sample_ohlcv_df(n=100))
        gap = df["ema_gap_pct"]
        expanding = df["ema_gap_expanding"]
        # For every row where gap increased, expanding must be True
        widened = gap > gap.shift(1)
        # Skip row 0 (shift produces NaN)
        mask = widened.iloc[1:]
        assert (expanding.iloc[1:][mask] == True).all()  # noqa: E712

    def test_ema9_reacts_faster(self, sample_ohlcv_df):
        """EMA9 should track close more tightly than EMA21."""
        df = compute_indicators(sample_ohlcv_df(n=100))
        err9 = (df["close"] - df["ema9"]).abs().mean()
        err21 = (df["close"] - df["ema21"]).abs().mean()
        assert err9 < err21

    def test_idempotent(self, sample_ohlcv_df):
        """Calling compute_indicators twice should not corrupt data."""
        df = sample_ohlcv_df(n=50)
        df1 = compute_indicators(df.copy())
        df2 = compute_indicators(df1.copy())
        for col in self.EXPECTED_COLUMNS:
            pd.testing.assert_series_equal(df1[col], df2[col], check_names=False)

    def test_no_nan_in_rsi_after_warmup(self, sample_ohlcv_df):
        """After the first 14 candles, RSI should have no NaN."""
        df = compute_indicators(sample_ohlcv_df(n=100))
        rsi_after_warmup = df["rsi"].iloc[14:]
        assert not rsi_after_warmup.isna().any()
