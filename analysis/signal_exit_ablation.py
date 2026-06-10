"""Ablation: disable ST_flip / EMA_cross exits — hold every trade to max_hold.

Tests the 2026-06-10 finding that all backtest profit comes from max_hold exits
while signal exits lose consistently. If max-hold-only performs >= full strategy,
the exit signals are noise (or worse).

Run: .venv/bin/python analysis/signal_exit_ablation.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtester
from costs import CostConfig
from results import print_stats
from strategy import check_exit as original_check_exit


def max_hold_only_exit(row, trade_dir, candles_held, max_hold, ema_gap_floor=0.0):
    if candles_held >= max_hold:
        return "max_hold"
    return None


def main():
    costs = CostConfig.from_env()

    backtester.check_exit = original_check_exit
    trades = backtester.run_backtest(costs=costs)
    print_stats(trades, "FULL strategy (all exits) | costs ON")

    backtester.check_exit = max_hold_only_exit
    trades = backtester.run_backtest(costs=costs)
    print_stats(trades, "ABLATED (max_hold exit only) | costs ON")


if __name__ == "__main__":
    main()
