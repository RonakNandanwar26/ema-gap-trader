"""Edge research: where does the P&L actually come from, and is it still alive?

Answers four questions on the cost-adjusted 3-year backtest:
1. Concentration — how much of net P&L sits in the top handful of trades?
2. Decay timeline — trailing 50-trade win rate and monthly P&L through time.
3. Premium-level edge — does the cheap-premium (Rs.0-50) edge persist by year?
4. Direction split by year — CE vs PE.

Run: .venv/bin/python analysis/edge_research.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from backtester import run_backtest
from costs import CostConfig


def main():
    trades = run_backtest(costs=CostConfig.from_env())
    df = pd.DataFrame([t for t in trades if t["prem_pnl"] is not None])
    df["entry"] = pd.to_datetime(df["entry"])
    df["year"] = df["entry"].dt.year
    df["month"] = df["entry"].dt.to_period("M")
    total = df["prem_pnl"].sum()
    print(f"\nTrades: {len(df)} | Net P&L: Rs.{total:,.0f}\n")

    # 1. Concentration
    print("=== 1. PROFIT CONCENTRATION ===")
    s = df["prem_pnl"].sort_values(ascending=False)
    for k in (5, 10, 20, 50):
        print(f"  Top {k:>2} trades: Rs.{s.head(k).sum():>10,.0f}  ({s.head(k).sum()/total*100:5.1f}% of net)")
    print(f"  Net P&L excluding top 10 winners: Rs.{total - s.head(10).sum():,.0f}")
    print(f"  Median trade: Rs.{df['prem_pnl'].median():,.0f}")

    # 2. Decay timeline
    print("\n=== 2. TRAILING 50-TRADE WIN RATE (quarterly snapshots) ===")
    df = df.sort_values("entry").reset_index(drop=True)
    df["win"] = df["prem_pnl"] > 0
    df["trail_wr"] = df["win"].rolling(50).mean() * 100
    df["trail_pnl"] = df["prem_pnl"].rolling(50).sum()
    snap = df.groupby(df["entry"].dt.to_period("Q")).tail(1)
    for _, r in snap.iterrows():
        if pd.notna(r["trail_wr"]):
            print(f"  {r['entry'].date()}  WR(50): {r['trail_wr']:5.1f}%   P&L(50): Rs.{r['trail_pnl']:>10,.0f}")

    print("\n=== MONTHLY P&L (last 12 months) ===")
    monthly = df.groupby("month")["prem_pnl"].agg(["sum", "count"])
    for m, r in monthly.tail(12).iterrows():
        bar = "#" * max(0, int(r["sum"] / 5000)) or ("-" * int(-r["sum"] / 5000))
        print(f"  {m}  Rs.{r['sum']:>9,.0f}  ({int(r['count']):3d} trades)  {bar}")

    # 3. Premium buckets by year
    print("\n=== 3. ENTRY PREMIUM BUCKETS x YEAR (P&L / win rate) ===")
    df["bucket"] = pd.cut(df["prem_in"], [0, 50, 100, 200, 10000],
                          labels=["0-50", "50-100", "100-200", "200+"])
    pv = df.groupby(["year", "bucket"], observed=True).agg(
        pnl=("prem_pnl", "sum"), n=("prem_pnl", "count"), wr=("win", "mean"))
    for (y, b), r in pv.iterrows():
        print(f"  {y} {str(b):>8}: Rs.{r['pnl']:>10,.0f}  ({int(r['n']):3d} trades, WR {r['wr']*100:4.1f}%)")

    # 4. Direction by year
    print("\n=== 4. CE vs PE x YEAR ===")
    dv = df.groupby(["year", "dir"]).agg(pnl=("prem_pnl", "sum"), n=("prem_pnl", "count"), wr=("win", "mean"))
    for (y, d), r in dv.iterrows():
        print(f"  {y} {d}: Rs.{r['pnl']:>10,.0f}  ({int(r['n']):3d} trades, WR {r['wr']*100:4.1f}%)")


if __name__ == "__main__":
    main()
