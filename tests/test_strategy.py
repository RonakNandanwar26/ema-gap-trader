import numpy as np
import pandas as pd

from strategy import check_entry, check_exit


def _make_row(ema9=100, ema21=95, st_dir=1, rsi=60, ema_gap_pct=0.2,
              ema_gap_expanding=True, close=105):
    """Helper to build a pd.Series with sensible defaults."""
    return pd.Series({
        "ema9": ema9,
        "ema21": ema21,
        "st_dir": st_dir,
        "rsi": rsi,
        "ema_gap_pct": ema_gap_pct,
        "ema_gap_expanding": ema_gap_expanding,
        "close": close,
    })


# ---------------------------------------------------------------------------
# check_entry tests
# ---------------------------------------------------------------------------


class TestCheckEntry:
    """Tests for check_entry."""

    # -- Crossover entries --------------------------------------------------

    def test_crossover_up_ce(self):
        """Bullish crossover with SuperTrend confirmation -> CE."""
        prev = _make_row(ema9=90, ema21=95)           # prev ema9 <= ema21
        row = _make_row(ema9=100, ema21=95, st_dir=1, rsi=60, ema_gap_pct=0.2)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result == "CE"

    def test_crossover_down_pe(self):
        """Bearish crossover with SuperTrend confirmation -> PE."""
        prev = _make_row(ema9=100, ema21=95)          # prev ema9 >= ema21
        row = _make_row(ema9=90, ema21=95, st_dir=-1, rsi=40, ema_gap_pct=0.2)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result == "PE"

    def test_no_crossover_no_entry(self):
        """No crossover and extra_mode='none' -> None."""
        prev = _make_row(ema9=100, ema21=95)          # already above
        row = _make_row(ema9=101, ema21=95, st_dir=1, rsi=60)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result is None

    # -- Midtrend / expanding entries ---------------------------------------

    def test_midtrend_ce(self):
        """Midtrend mode, ema9 > ema21, st=1, past cooldown -> CE."""
        prev = _make_row(ema9=100, ema21=95)          # no crossover
        row = _make_row(ema9=101, ema21=95, st_dir=1, rsi=60, ema_gap_pct=0.2)
        result = check_entry(row, prev, extra_mode="midtrend", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result == "CE"

    def test_midtrend_pe(self):
        """Midtrend mode, ema9 < ema21, st=-1, past cooldown -> PE."""
        prev = _make_row(ema9=90, ema21=95)           # no crossover
        row = _make_row(ema9=89, ema21=95, st_dir=-1, rsi=40, ema_gap_pct=0.2)
        result = check_entry(row, prev, extra_mode="midtrend", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result == "PE"

    def test_midtrend_cooldown_blocks(self):
        """Within cooldown window -> None."""
        prev = _make_row(ema9=100, ema21=95)
        row = _make_row(ema9=101, ema21=95, st_dir=1, rsi=60, ema_gap_pct=0.2)
        result = check_entry(row, prev, extra_mode="midtrend", gap_min=0,
                             last_exit_idx=98, current_idx=100, cooldown=5)
        assert result is None

    def test_expanding_blocks_non_expanding(self):
        """Expanding mode with ema_gap_expanding=False -> None."""
        prev = _make_row(ema9=100, ema21=95)
        row = _make_row(ema9=101, ema21=95, st_dir=1, rsi=60,
                        ema_gap_pct=0.2, ema_gap_expanding=False)
        result = check_entry(row, prev, extra_mode="expanding", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result is None

    # -- RSI filter ---------------------------------------------------------

    def test_rsi_blocks_low_ce(self):
        """CE entry with rsi=45 (<=50) -> blocked."""
        prev = _make_row(ema9=90, ema21=95)
        row = _make_row(ema9=100, ema21=95, st_dir=1, rsi=45, ema_gap_pct=0.2)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result is None

    def test_rsi_blocks_high_pe(self):
        """PE entry with rsi=55 (>=50) -> blocked."""
        prev = _make_row(ema9=100, ema21=95)
        row = _make_row(ema9=90, ema21=95, st_dir=-1, rsi=55, ema_gap_pct=0.2)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result is None

    def test_rsi_nan_blocks(self):
        """NaN rsi -> blocked for both directions."""
        prev = _make_row(ema9=90, ema21=95)
        row = _make_row(ema9=100, ema21=95, st_dir=1, rsi=np.nan,
                        ema_gap_pct=0.2)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result is None

    # -- Gap filters --------------------------------------------------------

    def test_gap_min_blocks(self):
        """ema_gap_pct below gap_min -> blocked."""
        prev = _make_row(ema9=90, ema21=95)
        row = _make_row(ema9=100, ema21=95, st_dir=1, rsi=60,
                        ema_gap_pct=0.05)
        result = check_entry(row, prev, extra_mode="none", gap_min=0.1,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result is None

    def test_gap_max_blocks(self):
        """ema_gap_pct=0.6 (>0.5 max) -> blocked."""
        prev = _make_row(ema9=90, ema21=95)
        row = _make_row(ema9=100, ema21=95, st_dir=1, rsi=60,
                        ema_gap_pct=0.6)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5)
        assert result is None

    # -- ORB filter ---------------------------------------------------------

    def test_orb_ce_below_high_blocks(self):
        """ORB enabled, CE, close < orb_high -> blocked."""
        prev = _make_row(ema9=90, ema21=95)
        row = _make_row(ema9=100, ema21=95, st_dir=1, rsi=60,
                        ema_gap_pct=0.2, close=104)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5,
                             orb_high=105, orb_low=95, orb_enabled=True)
        assert result is None

    def test_orb_pe_above_low_blocks(self):
        """ORB enabled, PE, close > orb_low -> blocked."""
        prev = _make_row(ema9=100, ema21=95)
        row = _make_row(ema9=90, ema21=95, st_dir=-1, rsi=40,
                        ema_gap_pct=0.2, close=96)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5,
                             orb_high=105, orb_low=95, orb_enabled=True)
        assert result is None

    def test_orb_not_ready_blocks(self):
        """ORB enabled but orb_high/orb_low are None -> blocked."""
        prev = _make_row(ema9=90, ema21=95)
        row = _make_row(ema9=100, ema21=95, st_dir=1, rsi=60,
                        ema_gap_pct=0.2, close=110)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5,
                             orb_high=None, orb_low=None, orb_enabled=True)
        assert result is None

    def test_orb_ce_passes(self):
        """ORB enabled, CE, close >= orb_high -> CE."""
        prev = _make_row(ema9=90, ema21=95)
        row = _make_row(ema9=100, ema21=95, st_dir=1, rsi=60,
                        ema_gap_pct=0.2, close=106)
        result = check_entry(row, prev, extra_mode="none", gap_min=0,
                             last_exit_idx=0, current_idx=100, cooldown=5,
                             orb_high=105, orb_low=95, orb_enabled=True)
        assert result == "CE"


# ---------------------------------------------------------------------------
# check_exit tests
# ---------------------------------------------------------------------------


class TestCheckExit:
    """Tests for check_exit."""

    def test_st_flip_ce(self):
        """CE trade with SuperTrend flipping bearish -> ST_flip."""
        row = _make_row(st_dir=-1, ema9=100, ema21=95)
        assert check_exit(row, trade_dir="CE", candles_held=5, max_hold=20) == "ST_flip"

    def test_st_flip_pe(self):
        """PE trade with SuperTrend flipping bullish -> ST_flip."""
        row = _make_row(st_dir=1, ema9=90, ema21=95)
        assert check_exit(row, trade_dir="PE", candles_held=5, max_hold=20) == "ST_flip"

    def test_ema_cross_ce(self):
        """CE trade with ema9 < ema21 -> EMA_cross."""
        row = _make_row(st_dir=1, ema9=94, ema21=95)
        assert check_exit(row, trade_dir="CE", candles_held=5, max_hold=20) == "EMA_cross"

    def test_ema_cross_pe(self):
        """PE trade with ema9 > ema21 -> EMA_cross."""
        row = _make_row(st_dir=-1, ema9=96, ema21=95)
        assert check_exit(row, trade_dir="PE", candles_held=5, max_hold=20) == "EMA_cross"

    def test_max_hold(self):
        """candles_held=20, max_hold=20 -> max_hold exit."""
        row = _make_row(st_dir=1, ema9=100, ema21=95)
        assert check_exit(row, trade_dir="CE", candles_held=20, max_hold=20) == "max_hold"

    def test_no_exit(self):
        """All conditions favourable -> None (no exit)."""
        row = _make_row(st_dir=1, ema9=100, ema21=95)
        assert check_exit(row, trade_dir="CE", candles_held=5, max_hold=20) is None

    def test_st_flip_priority(self):
        """Both ST flip and EMA cross true -> ST_flip takes priority."""
        row = _make_row(st_dir=-1, ema9=94, ema21=95)   # ST flip + ema cross
        assert check_exit(row, trade_dir="CE", candles_held=5, max_hold=20) == "ST_flip"

    # ------------------------------------------------------------------
    # gap_contract exit (ema_gap_floor)
    # ------------------------------------------------------------------

    def test_gap_floor_disabled_by_default(self):
        """ema_gap_floor=0 (default) means gap_contract never fires."""
        row = _make_row(st_dir=1, ema9=100, ema21=95, ema_gap_pct=0.001)
        assert check_exit(row, trade_dir="CE", candles_held=5, max_hold=20) is None

    def test_gap_contract_fires_when_below_floor(self):
        """ema_gap_pct < floor + held >= 2 -> gap_contract."""
        row = _make_row(st_dir=1, ema9=100, ema21=95, ema_gap_pct=0.04)
        assert check_exit(row, trade_dir="CE", candles_held=5, max_hold=20,
                          ema_gap_floor=0.05) == "gap_contract"

    def test_gap_contract_pe_side(self):
        """gap_contract works for PE trades too (trade direction agnostic)."""
        row = _make_row(st_dir=-1, ema9=90, ema21=95, ema_gap_pct=0.04)
        assert check_exit(row, trade_dir="PE", candles_held=5, max_hold=20,
                          ema_gap_floor=0.05) == "gap_contract"

    def test_gap_contract_blocked_within_min_hold(self):
        """candles_held < 2 -> gap_contract suppressed (avoid entry-candle wiggles)."""
        row = _make_row(st_dir=1, ema9=100, ema21=95, ema_gap_pct=0.001)
        assert check_exit(row, trade_dir="CE", candles_held=1, max_hold=20,
                          ema_gap_floor=0.05) is None

    def test_gap_contract_at_min_hold_boundary(self):
        """candles_held == 2 -> gap_contract eligible."""
        row = _make_row(st_dir=1, ema9=100, ema21=95, ema_gap_pct=0.04)
        assert check_exit(row, trade_dir="CE", candles_held=2, max_hold=20,
                          ema_gap_floor=0.05) == "gap_contract"

    def test_gap_contract_above_floor_no_exit(self):
        """ema_gap_pct >= floor -> no gap_contract."""
        row = _make_row(st_dir=1, ema9=100, ema21=95, ema_gap_pct=0.06)
        assert check_exit(row, trade_dir="CE", candles_held=5, max_hold=20,
                          ema_gap_floor=0.05) is None

    def test_st_flip_takes_priority_over_gap_contract(self):
        """ST flip + small gap -> ST_flip wins (higher priority)."""
        row = _make_row(st_dir=-1, ema9=100, ema21=95, ema_gap_pct=0.01)
        assert check_exit(row, trade_dir="CE", candles_held=5, max_hold=20,
                          ema_gap_floor=0.05) == "ST_flip"

    def test_ema_cross_takes_priority_over_gap_contract(self):
        """EMA cross + small gap -> EMA_cross wins (higher priority)."""
        row = _make_row(st_dir=1, ema9=94, ema21=95, ema_gap_pct=0.01)
        assert check_exit(row, trade_dir="CE", candles_held=5, max_hold=20,
                          ema_gap_floor=0.05) == "EMA_cross"

    def test_gap_contract_takes_priority_over_max_hold(self):
        """gap_contract beats max_hold when both fire on the same candle."""
        row = _make_row(st_dir=1, ema9=100, ema21=95, ema_gap_pct=0.01)
        assert check_exit(row, trade_dir="CE", candles_held=20, max_hold=20,
                          ema_gap_floor=0.05) == "gap_contract"
