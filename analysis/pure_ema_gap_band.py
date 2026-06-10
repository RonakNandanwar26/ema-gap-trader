"""Pure EMA 9/21 gap-band experiment (user idea, 2026-06-10).

Entry: |EMA9-EMA21| gap >= GAP_BAND% in EMA direction (CE above / PE below).
Exit:  gap < GAP_BAND%. Nothing else — no SuperTrend, no RSI, no ORB.
Variants: + max_hold safety, + max entry premium cap.

Run: .venv/bin/python analysis/pure_ema_gap_band.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtester
from costs import CostConfig
from results import print_stats

GAP_BAND = float(os.getenv("GAP_BAND", "0.20"))


def band_entry(row, prev, extra_mode, gap_min, last_exit_idx, current_idx,
               cooldown, orb_high=None, orb_low=None, orb_enabled=False):
    if row["ema_gap_pct"] < GAP_BAND:
        return None
    if row["ema9"] > row["ema21"]:
        return "CE"
    if row["ema9"] < row["ema21"]:
        return "PE"
    return None


def band_exit_pure(row, trade_dir, candles_held, max_hold, ema_gap_floor=0.0):
    if row["ema_gap_pct"] < GAP_BAND:
        return "gap_floor"
    return None


def band_exit_maxhold(row, trade_dir, candles_held, max_hold, ema_gap_floor=0.0):
    if row["ema_gap_pct"] < GAP_BAND:
        return "gap_floor"
    if candles_held >= max_hold:
        return "max_hold"
    return None


def main():
    costs = CostConfig.from_env()
    backtester.check_entry = band_entry

    backtester.check_exit = band_exit_pure
    print_stats(backtester.run_backtest(costs=costs),
                f"PURE gap band {GAP_BAND}% (enter gap>=, exit gap<) | costs ON")

    backtester.check_exit = band_exit_maxhold
    print_stats(backtester.run_backtest(costs=costs),
                f"gap band {GAP_BAND}% + max_hold 20 | costs ON")

    print_stats(backtester.run_backtest(costs=costs, max_entry_premium=100),
                f"gap band {GAP_BAND}% + max_hold 20 + premium cap <=100 | costs ON")


if __name__ == "__main__":
    main()
