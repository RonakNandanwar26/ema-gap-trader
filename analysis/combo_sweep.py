"""Combo sweep: extra_entry_mode × gap_floor exit.

Tests whether the two independent winners (pure crossover vs midtrend,
with or without gap_contract exit at floor 0.05/0.10) stack profitably.

Run from repo root: .venv/bin/python analysis/combo_sweep.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtester as bt
from config import StrategyConfig, INSTRUMENTS, get_strategy_config
from results import compute_stats
from strategy import check_exit as orig_check_exit


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


def make_check_exit(gap_floor: float | None):
    if gap_floor is None:
        return orig_check_exit
    def wrapped(row, trade_dir, candles_held, max_hold):
        reason = orig_check_exit(row, trade_dir, candles_held, max_hold)
        if reason is not None:
            return reason
        if candles_held >= 2 and row["ema_gap_pct"] < gap_floor:
            return "gap_contract"
        return None
    return wrapped


CONFIGS = [
    # (label,                    mode,         gap_min, gap_floor)
    ("none       gap_min=0.03 no_exit", "none",      0.03, None),
    ("none       gap_min=0.03 floor=0.05", "none",      0.03, 0.05),
    ("none       gap_min=0.03 floor=0.10", "none",      0.03, 0.10),
    ("midtrend   gap_min=0.03 no_exit", "midtrend",  0.03, None),
    ("midtrend   gap_min=0.03 floor=0.05", "midtrend",  0.03, 0.05),
    ("midtrend   gap_min=0.03 floor=0.10", "midtrend",  0.03, 0.10),
    ("expanding  gap_min=0.03 no_exit", "expanding", 0.03, None),
    ("expanding  gap_min=0.03 floor=0.10", "expanding", 0.03, 0.10),
]


def main():
    ic = INSTRUMENTS["NIFTY"]
    header = (
        f"{'config':<40} {'N':>5} {'WR%':>5} {'PnL':>13} {'PF':>6} "
        f"{'MaxDD':>10} {'AvgW':>9} {'AvgL':>9}"
    )
    print(header)
    print("-" * len(header))
    for label, mode, gm, floor in CONFIGS:
        sc = sc_with(mode, gm)
        bt.check_exit = make_check_exit(floor)
        trades = bt.run_backtest(strat_config=sc, inst_config=ic)
        s = compute_stats(trades)
        pf = s["pf"] if s["pf"] != float("inf") else 999.0
        print(
            f"{label:<40} {s['n']:>5} {s['wr']:>5.0f} {s['pnl']:>13,.0f} "
            f"{pf:>6.2f} {s['max_dd']:>10,.0f} "
            f"{s['avg_win']:>9,.0f} {s['avg_loss']:>9,.0f}"
        )


if __name__ == "__main__":
    main()
