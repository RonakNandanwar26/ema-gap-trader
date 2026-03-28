#!/usr/bin/env python3
"""Analyze losing trades — find patterns and test what-if scenarios."""

import pandas as pd
from backtester import run_backtest
from config import get_strategy_config, get_instrument_config


def analyze():
    sc = get_strategy_config()
    ic = get_instrument_config()
    print(f"Running backtest: {ic.name} | {sc.extra_entry_mode} | gap>={sc.ema_gap_min}%")

    trades = run_backtest()
    priced = [t for t in trades if t["prem_pnl"] is not None]
    winners = [t for t in priced if t["prem_pnl"] > 0]
    losers = [t for t in priced if t["prem_pnl"] <= 0]

    print(f"\nTotal: {len(priced)} | Winners: {len(winners)} | Losers: {len(losers)}")
    print(f"Win Rate: {len(winners)/len(priced)*100:.0f}%")

    # ═══════════════════════════════════════════════════════════════════
    # 1. HOW FAST DO LOSERS EXIT?
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  1. EXIT SPEED — Candles Held Before Exit")
    print(f"{'='*80}")

    for reason in ("EMA_cross", "ST_flip", "max_hold"):
        r_trades = [t for t in priced if t["reason"] == reason]
        r_winners = [t for t in r_trades if t["prem_pnl"] > 0]
        r_losers = [t for t in r_trades if t["prem_pnl"] <= 0]
        if not r_trades:
            continue

        avg_candles_all = sum(t["candles"] for t in r_trades) / len(r_trades)
        avg_candles_win = sum(t["candles"] for t in r_winners) / len(r_winners) if r_winners else 0
        avg_candles_lose = sum(t["candles"] for t in r_losers) / len(r_losers) if r_losers else 0

        print(f"\n  {reason}:")
        print(f"    Total: {len(r_trades)} | W: {len(r_winners)} | L: {len(r_losers)}")
        print(f"    Avg candles held — All: {avg_candles_all:.1f} | Winners: {avg_candles_win:.1f} | Losers: {avg_candles_lose:.1f}")

        # Distribution of candles for losers
        if r_losers:
            candle_buckets = {}
            for t in r_losers:
                bucket = min(t["candles"], 20)
                candle_buckets[bucket] = candle_buckets.get(bucket, 0) + 1
            print(f"    Loser candle distribution:")
            for c in sorted(candle_buckets):
                bar = "#" * candle_buckets[c]
                print(f"      {c:>3} candles: {candle_buckets[c]:>3} trades {bar}")

    # ═══════════════════════════════════════════════════════════════════
    # 2. ENTRY GAP — Losers vs Winners
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  2. ENTRY GAP % — Losers vs Winners")
    print(f"{'='*80}")

    gap_buckets = [(0, 0.03), (0.03, 0.05), (0.05, 0.08), (0.08, 0.15), (0.15, 999)]
    print(f"\n  {'Gap Range':<15} {'Total':>6} {'Win':>5} {'Loss':>5} {'WR':>6} {'Avg P&L':>10}")
    print(f"  {'-'*15} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*10}")
    for lo, hi in gap_buckets:
        label = f"{lo:.2f}-{hi:.2f}%" if hi < 999 else f">={lo:.2f}%"
        bucket = [t for t in priced if t.get("entry_gap") is not None and lo <= t["entry_gap"] < hi]
        if not bucket:
            continue
        w = sum(1 for t in bucket if t["prem_pnl"] > 0)
        l = len(bucket) - w
        wr = w / len(bucket) * 100
        avg = sum(t["prem_pnl"] for t in bucket) / len(bucket)
        print(f"  {label:<15} {len(bucket):>6} {w:>5} {l:>5} {wr:>5.0f}% {avg:>10,.0f}")

    # ═══════════════════════════════════════════════════════════════════
    # 3. ENTRY RSI — Losers vs Winners
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  3. ENTRY RSI — Losers vs Winners")
    print(f"{'='*80}")

    rsi_buckets = [(50, 55), (55, 60), (60, 65), (65, 70), (70, 100)]
    # CE trades (RSI > 50)
    ce_trades = [t for t in priced if t["dir"] == "CE" and t.get("entry_rsi") is not None]
    print(f"\n  CE trades (RSI > 50):")
    print(f"  {'RSI Range':<12} {'Total':>6} {'Win':>5} {'Loss':>5} {'WR':>6} {'Avg P&L':>10}")
    print(f"  {'-'*12} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*10}")
    for lo, hi in rsi_buckets:
        bucket = [t for t in ce_trades if lo <= t["entry_rsi"] < hi]
        if not bucket:
            continue
        w = sum(1 for t in bucket if t["prem_pnl"] > 0)
        wr = w / len(bucket) * 100
        avg = sum(t["prem_pnl"] for t in bucket) / len(bucket)
        print(f"  {lo}-{hi:<8} {len(bucket):>6} {w:>5} {len(bucket)-w:>5} {wr:>5.0f}% {avg:>10,.0f}")

    # PE trades (RSI < 50)
    pe_trades = [t for t in priced if t["dir"] == "PE" and t.get("entry_rsi") is not None]
    rsi_pe_buckets = [(0, 30), (30, 35), (35, 40), (40, 45), (45, 50)]
    print(f"\n  PE trades (RSI < 50):")
    print(f"  {'RSI Range':<12} {'Total':>6} {'Win':>5} {'Loss':>5} {'WR':>6} {'Avg P&L':>10}")
    print(f"  {'-'*12} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*10}")
    for lo, hi in rsi_pe_buckets:
        bucket = [t for t in pe_trades if lo <= t["entry_rsi"] < hi]
        if not bucket:
            continue
        w = sum(1 for t in bucket if t["prem_pnl"] > 0)
        wr = w / len(bucket) * 100
        avg = sum(t["prem_pnl"] for t in bucket) / len(bucket)
        print(f"  {lo}-{hi:<8} {len(bucket):>6} {w:>5} {len(bucket)-w:>5} {wr:>5.0f}% {avg:>10,.0f}")

    # ═══════════════════════════════════════════════════════════════════
    # 4. ENTRY TYPE — Crossover vs Midtrend
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  4. ENTRY TYPE — Crossover vs Midtrend")
    print(f"{'='*80}")

    for etype in ("crossover", "midtrend", "expanding"):
        et = [t for t in priced if t.get("entry_type") == etype]
        if not et:
            continue
        w = sum(1 for t in et if t["prem_pnl"] > 0)
        pnl = sum(t["prem_pnl"] for t in et)
        avg = pnl / len(et)
        print(f"\n  {etype}: {len(et)} trades | WR: {w}/{len(et)} ({w/len(et)*100:.0f}%) | P&L: Rs.{pnl:,.0f} | Avg: Rs.{avg:,.0f}")

        # Exit breakdown per entry type
        for reason in ("EMA_cross", "ST_flip", "max_hold"):
            rt = [t for t in et if t["reason"] == reason]
            if not rt:
                continue
            rw = sum(1 for t in rt if t["prem_pnl"] > 0)
            rpnl = sum(t["prem_pnl"] for t in rt)
            print(f"    {reason:>12}: {len(rt):>3} trades, WR {rw/len(rt)*100:.0f}%, P&L: Rs.{rpnl:>10,.0f}")

    # ═══════════════════════════════════════════════════════════════════
    # 5. TIME OF DAY
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  5. HOUR OF DAY — When Do Losers Enter?")
    print(f"{'='*80}")

    print(f"\n  {'Hour':<6} {'Total':>6} {'Win':>5} {'Loss':>5} {'WR':>6} {'Avg P&L':>10}")
    print(f"  {'-'*6} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*10}")
    for h in range(9, 16):
        ht = [t for t in priced if pd.Timestamp(t["entry"]).hour == h]
        if not ht:
            continue
        w = sum(1 for t in ht if t["prem_pnl"] > 0)
        avg = sum(t["prem_pnl"] for t in ht) / len(ht)
        print(f"  {h:>2}:00 {len(ht):>6} {w:>5} {len(ht)-w:>5} {w/len(ht)*100:>5.0f}% {avg:>10,.0f}")

    # ═══════════════════════════════════════════════════════════════════
    # 6. DIRECTION
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  6. DIRECTION — CE vs PE")
    print(f"{'='*80}")

    for d in ("CE", "PE"):
        dt = [t for t in priced if t["dir"] == d]
        w = sum(1 for t in dt if t["prem_pnl"] > 0)
        pnl = sum(t["prem_pnl"] for t in dt)
        avg_w = sum(t["prem_pnl"] for t in dt if t["prem_pnl"] > 0) / w if w else 0
        avg_l = sum(t["prem_pnl"] for t in dt if t["prem_pnl"] <= 0) / (len(dt) - w) if (len(dt) - w) else 0
        print(f"\n  {d}: {len(dt)} trades | WR: {w/len(dt)*100:.0f}% | P&L: Rs.{pnl:,.0f}")
        print(f"    Avg Winner: Rs.{avg_w:,.0f} | Avg Loser: Rs.{avg_l:,.0f}")

    # ═══════════════════════════════════════════════════════════════════
    # 7. WHAT-IF: Remove EMA_cross exit
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  7. WHAT-IF SCENARIOS")
    print(f"{'='*80}")

    # Current
    total_pnl = sum(t["prem_pnl"] for t in priced)
    print(f"\n  Current: {len(priced)} trades, Rs.{total_pnl:,.0f}")

    # Without EMA_cross exits (those trades would have gone to max_hold or ST_flip instead)
    ema_cross_loss = sum(t["prem_pnl"] for t in priced if t["reason"] == "EMA_cross")
    print(f"  If remove EMA_cross exit: saves Rs.{abs(ema_cross_loss):,.0f} in losses (but trades would still need an exit)")

    # Only max_hold exits
    max_hold_pnl = sum(t["prem_pnl"] for t in priced if t["reason"] == "max_hold")
    max_hold_n = len([t for t in priced if t["reason"] == "max_hold"])
    max_hold_w = len([t for t in priced if t["reason"] == "max_hold" and t["prem_pnl"] > 0])
    print(f"  If ONLY max_hold trades counted: {max_hold_n} trades, {max_hold_w/max_hold_n*100:.0f}% WR, Rs.{max_hold_pnl:,.0f}")

    # ═══════════════════════════════════════════════════════════════════
    # 8. LOSS SIZE DISTRIBUTION
    # ═══════════════════════════════════════════════════════════════════
    print(f"\n{'='*80}")
    print("  8. LOSS SIZE DISTRIBUTION")
    print(f"{'='*80}")

    loss_amounts = sorted([t["prem_pnl"] for t in losers])
    print(f"\n  Total losers: {len(losers)}")
    print(f"  Avg loss: Rs.{sum(loss_amounts)/len(loss_amounts):,.0f}")
    print(f"  Median loss: Rs.{loss_amounts[len(loss_amounts)//2]:,.0f}")
    print(f"  Worst loss: Rs.{loss_amounts[0]:,.0f}")
    print(f"  Best loss (smallest): Rs.{loss_amounts[-1]:,.0f}")

    # Loss buckets
    loss_buckets = [(0, 1000), (1000, 2000), (2000, 3000), (3000, 5000), (5000, 10000), (10000, 99999)]
    print(f"\n  {'Loss Range':<15} {'Count':>6} {'Total Loss':>12}")
    print(f"  {'-'*15} {'-'*6} {'-'*12}")
    for lo, hi in loss_buckets:
        bucket = [t for t in losers if lo <= abs(t["prem_pnl"]) < hi]
        if not bucket:
            continue
        label = f"Rs.{lo:,}-{hi:,}" if hi < 99999 else f"Rs.{lo:,}+"
        total = sum(t["prem_pnl"] for t in bucket)
        print(f"  {label:<15} {len(bucket):>6} {total:>12,.0f}")


if __name__ == "__main__":
    analyze()
