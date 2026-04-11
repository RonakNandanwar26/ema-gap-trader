"""Scenario 3: midtrend vs expanding extra-entry mode comparison.

Runs NIFTY baseline with several (mode, gap_min) pairs and reports the
profit/drawdown tradeoff.

Run from repo root: .venv/bin/python analysis/scenario3_mode_sweep.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtester import run_backtest
from config import StrategyConfig, INSTRUMENTS, get_strategy_config
from results import compute_stats


BASE = get_strategy_config()


def sc_with(mode: str, gap_min: float) -> StrategyConfig:
    return StrategyConfig(
        extra_entry_mode=mode,
        ema_gap_min=gap_min,
        max_hold_candles=BASE.max_hold_candles,
        cooldown_candles=BASE.cooldown_candles,
        candle_interval=BASE.candle_interval,
        max_capital_per_trade_pct=BASE.max_capital_per_trade_pct,
        orb_filter=BASE.orb_filter,
    )


CONFIGS = [
    ("none       gap=0.03", "none",      0.03),
    ("midtrend   gap=0.03", "midtrend",  0.03),
    ("midtrend   gap=0.10", "midtrend",  0.10),
    ("expanding  gap=0.03", "expanding", 0.03),
    ("expanding  gap=0.10", "expanding", 0.10),
    ("expanding  gap=0.15", "expanding", 0.15),
]


def main():
    ic = INSTRUMENTS["NIFTY"]
    header = (
        f"{'config':<22} {'N':>5} {'WR%':>5} {'PnL':>13} {'PF':>6} "
        f"{'MaxDD':>10} {'AvgW':>9} {'AvgL':>9} {'MaxStk':>7}"
    )
    print(header)
    print("-" * len(header))
    for label, mode, gm in CONFIGS:
        sc = sc_with(mode, gm)
        trades = run_backtest(strat_config=sc, inst_config=ic)
        s = compute_stats(trades)
        pf = s["pf"] if s["pf"] != float("inf") else 999.0
        print(
            f"{label:<22} {s['n']:>5} {s['wr']:>5.0f} {s['pnl']:>13,.0f} "
            f"{pf:>6.2f} {s['max_dd']:>10,.0f} "
            f"{s['avg_win']:>9,.0f} {s['avg_loss']:>9,.0f} {s['max_streak']:>7}"
        )


if __name__ == "__main__":
    main()
