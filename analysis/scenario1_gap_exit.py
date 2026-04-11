"""Scenario 1: Exit on EMA-gap contraction to save theta in sideways markets.

Wraps check_exit with an additional rule: if `ema_gap_pct` drops below an
absolute floor, exit as 'gap_contract'. Sweeps floor thresholds.

Run from repo root: .venv/bin/python analysis/scenario1_gap_exit.py
"""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backtester as bt
from config import get_strategy_config, INSTRUMENTS
from results import compute_stats
from strategy import check_exit as orig_check_exit


def make_check_exit(gap_floor: float):
    def wrapped(row, trade_dir, candles_held, max_hold):
        reason = orig_check_exit(row, trade_dir, candles_held, max_hold)
        if reason is not None:
            return reason
        # Only trigger after a minimum hold so we don't bail on 1-candle wiggles.
        if candles_held >= 2 and row["ema_gap_pct"] < gap_floor:
            return "gap_contract"
        return None
    return wrapped


CONFIGS = [
    ("baseline (no gap exit)", None),
    ("gap_floor=0.02",         0.02),
    ("gap_floor=0.03",         0.03),
    ("gap_floor=0.05",         0.05),
    ("gap_floor=0.08",         0.08),
    ("gap_floor=0.10",         0.10),
]


def main():
    sc = get_strategy_config()
    ic = INSTRUMENTS["NIFTY"]

    header = (
        f"{'config':<26} {'N':>5} {'WR%':>5} {'PnL':>13} {'PF':>6} "
        f"{'MaxDD':>10} {'gap_cut':>8} {'gap_pnl':>12}"
    )
    print(header)
    print("-" * len(header))

    for label, gf in CONFIGS:
        bt.check_exit = orig_check_exit if gf is None else make_check_exit(gf)
        trades = bt.run_backtest(strat_config=sc, inst_config=ic)
        s = compute_stats(trades)
        pf = s["pf"] if s["pf"] != float("inf") else 999.0
        gap_cut = sum(1 for t in trades if t["reason"] == "gap_contract")
        gap_pnl = sum(t["prem_pnl"] for t in trades
                      if t["reason"] == "gap_contract" and t["prem_pnl"] is not None)
        print(
            f"{label:<26} {s['n']:>5} {s['wr']:>5.0f} {s['pnl']:>13,.0f} "
            f"{pf:>6.2f} {s['max_dd']:>10,.0f} {gap_cut:>8} {gap_pnl:>12,.0f}"
        )


if __name__ == "__main__":
    main()
