#!/usr/bin/env python3
"""Test proposed fixes for losing trades — side by side comparison."""

import sqlite3
from datetime import timedelta
import pandas as pd
from config import get_dhan_db_path, get_instrument_config
from indicators import compute_indicators

INSTRUMENT = "NIFTY"
LOT_SIZE = 65
START_DATE = "2023-04-01"
END_DATE = "2026-03-28"
GAP_MIN = 0.03
MAX_HOLD = 20
COOLDOWN = 3

_conn = None
def get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(get_dhan_db_path())
    return _conn

def load_candles():
    df = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close FROM dhan_spot_candle "
        "WHERE instrument=? AND candle_type='intraday' AND interval_min=15 ORDER BY timestamp",
        get_conn(), params=(INSTRUMENT,))
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df

def get_premium(timestamp, direction):
    ts = pd.Timestamp(timestamp)
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    ic = get_instrument_config(INSTRUMENT)
    cur = get_conn().execute(
        "SELECT close, strike FROM dhan_option_candle "
        "WHERE instrument=? AND strike_offset='ATM' AND direction=? "
        "AND expiry_flag=? AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (INSTRUMENT, dhan_dir, ic.expiry_flag, lo, hi, ts.strftime("%Y-%m-%d %H:%M:%S")))
    row = cur.fetchone()
    return (float(row[0]), float(row[1])) if row else (None, None)

def get_premium_strike(timestamp, direction, strike):
    ts = pd.Timestamp(timestamp)
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    ic = get_instrument_config(INSTRUMENT)
    cur = get_conn().execute(
        "SELECT close FROM dhan_option_candle "
        "WHERE instrument=? AND direction=? "
        "AND expiry_flag=? AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? AND ABS(strike-?)<1 "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (INSTRUMENT, dhan_dir, ic.expiry_flag, lo, hi, strike, ts.strftime("%Y-%m-%d %H:%M:%S")))
    row = cur.fetchone()
    return float(row[0]) if row else None


def backtest(df, label, exit_rules, entry_rules):
    """
    exit_rules: dict with keys:
      ema_cross: bool (use EMA cross exit)
      st_flip: bool (use ST flip exit)
      max_hold: int (max candles)
    entry_rules: dict with keys:
      midtrend: bool
      midtrend_after_hour: int or None (only allow midtrend after this hour, e.g. 13)
      crossover_always: bool
    """
    use_ema_cross = exit_rules.get("ema_cross", True)
    use_st_flip = exit_rules.get("st_flip", True)
    max_hold = exit_rules.get("max_hold", MAX_HOLD)
    midtrend = entry_rules.get("midtrend", True)
    midtrend_after = entry_rules.get("midtrend_after_hour", None)

    start, end = pd.Timestamp(START_DATE), pd.Timestamp(END_DATE)
    indices = df.index[(df["timestamp"] >= start) & (df["timestamp"] <= end)].tolist()

    trades = []
    open_trade = None
    last_exit_idx = -999

    for i in indices:
        if i == 0: continue
        row, prev = df.iloc[i], df.iloc[i-1]
        ts = row["timestamp"]

        # EXIT
        if open_trade is not None:
            candles_held = i - open_trade["idx"]
            reason = None

            if use_st_flip:
                if open_trade["dir"] == "CE" and row["st_dir"] == -1: reason = "ST_flip"
                elif open_trade["dir"] == "PE" and row["st_dir"] == 1: reason = "ST_flip"

            if not reason and use_ema_cross:
                if open_trade["dir"] == "CE" and row["ema9"] < row["ema21"]: reason = "EMA_cross"
                elif open_trade["dir"] == "PE" and row["ema9"] > row["ema21"]: reason = "EMA_cross"

            if not reason and candles_held >= max_hold: reason = "max_hold"

            if reason:
                ep = get_premium_strike(ts, open_trade["dir"], open_trade["strike"]) if open_trade["strike"] else None
                if ep is None: ep, _ = get_premium(ts, open_trade["dir"])
                pp = (ep - open_trade["prem"]) * LOT_SIZE if ep and open_trade["prem"] else None
                trades.append({"prem_pnl": pp, "reason": reason, "entry": open_trade["ts"],
                               "entry_type": open_trade["entry_type"]})
                open_trade = None
                last_exit_idx = i

        # ENTRY
        if open_trade is not None: continue

        direction = None
        entry_type = None

        # Crossover (always)
        cu = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
        cd = prev["ema9"] >= prev["ema21"] and row["ema9"] < row["ema21"]
        if cu and row["st_dir"] == 1: direction = "CE"; entry_type = "crossover"
        elif cd and row["st_dir"] == -1: direction = "PE"; entry_type = "crossover"

        # Midtrend
        if direction is None and midtrend:
            if i - last_exit_idx < COOLDOWN: continue
            # Hour filter for midtrend
            hour = pd.Timestamp(ts).hour
            if midtrend_after is not None and hour < midtrend_after: continue

            if row["ema9"] > row["ema21"] and row["st_dir"] == 1: direction = "CE"; entry_type = "midtrend"
            elif row["ema9"] < row["ema21"] and row["st_dir"] == -1: direction = "PE"; entry_type = "midtrend"

        if direction is None: continue

        # RSI alignment
        if direction == "CE" and (pd.isna(row["rsi"]) or row["rsi"] <= 50): continue
        if direction == "PE" and (pd.isna(row["rsi"]) or row["rsi"] >= 50): continue
        # Gap filter
        if row["ema_gap_pct"] < GAP_MIN: continue

        prem, strike = get_premium(ts, direction)
        open_trade = {"ts": ts, "idx": i, "dir": direction, "prem": prem,
                      "strike": strike, "entry_type": entry_type}

    # Close remaining
    if open_trade is not None and indices:
        last = df.iloc[indices[-1]]
        ep = get_premium_strike(last["timestamp"], open_trade["dir"], open_trade["strike"]) if open_trade["strike"] else None
        if ep is None: ep, _ = get_premium(last["timestamp"], open_trade["dir"])
        pp = (ep - open_trade["prem"]) * LOT_SIZE if ep and open_trade["prem"] else None
        trades.append({"prem_pnl": pp, "reason": "end", "entry": open_trade["ts"],
                       "entry_type": open_trade["entry_type"]})

    return _stats(trades, label)


def _stats(trades, label):
    priced = [t for t in trades if t["prem_pnl"] is not None]
    if not priced:
        print(f"  {label}: No trades!"); return {}
    n = len(priced)
    pw = sum(1 for t in priced if t["prem_pnl"] > 0)
    pnl = sum(t["prem_pnl"] for t in priced)
    gw = sum(t["prem_pnl"] for t in priced if t["prem_pnl"] > 0)
    gl = abs(sum(t["prem_pnl"] for t in priced if t["prem_pnl"] < 0))
    pf = gw / gl if gl > 0 else float("inf")

    # Max DD
    peak = dd = equity = 0
    for t in priced:
        equity += t["prem_pnl"]
        if equity > peak: peak = equity
        if peak - equity > dd: dd = peak - equity

    # Monthly
    monthly = {}
    for t in priced:
        m = pd.Timestamp(t["entry"]).strftime("%Y-%m")
        monthly[m] = monthly.get(m, 0) + t["prem_pnl"]
    green = sum(1 for v in monthly.values() if v >= 0)
    red = sum(1 for v in monthly.values() if v < 0)

    # Exit breakdown
    reasons = {}
    for t in priced:
        r = t["reason"]
        if r not in reasons: reasons[r] = {"n": 0, "pnl": 0, "w": 0}
        reasons[r]["n"] += 1; reasons[r]["pnl"] += t["prem_pnl"]
        if t["prem_pnl"] > 0: reasons[r]["w"] += 1

    print(f"\n  {label}")
    print(f"  Trades: {n} | WR: {pw}/{n} ({pw/n*100:.0f}%) | P&L: Rs.{pnl:,.0f} | PF: {pf:.2f} | DD: Rs.{dd:,.0f} | Green: {green}/{green+red}")
    for r in sorted(reasons):
        d = reasons[r]
        wr = d["w"]/d["n"]*100 if d["n"] else 0
        print(f"    {r:>12}: {d['n']:>3} trades, WR {wr:.0f}%, P&L: Rs.{d['pnl']:>10,.0f}")

    return {"label": label, "n": n, "wr": pw/n*100, "pnl": pnl, "pf": pf, "dd": dd, "green": green, "red": red}


def main():
    print("="*90)
    print("  LOSS FIX COMPARISON — Testing exit/entry rule changes")
    print(f"  {INSTRUMENT} | Gap>={GAP_MIN}% | 15min | {START_DATE} to {END_DATE}")
    print("="*90)

    print("\nLoading data...")
    df = load_candles()
    df = compute_indicators(df)
    print(f"  {len(df)} candles ready")

    results = []

    # A. Current (baseline)
    print("\n--- A: CURRENT (EMA_cross + ST_flip + max_hold) ---")
    results.append(backtest(df, "A. Current",
        exit_rules={"ema_cross": True, "st_flip": True, "max_hold": 20},
        entry_rules={"midtrend": True}))

    # B. Remove EMA_cross exit (only ST_flip + max_hold)
    print("\n--- B: NO EMA_CROSS EXIT ---")
    results.append(backtest(df, "B. No EMA_cross exit",
        exit_rules={"ema_cross": False, "st_flip": True, "max_hold": 20},
        entry_rules={"midtrend": True}))

    # C. Remove both early exits (only max_hold)
    print("\n--- C: ONLY MAX_HOLD EXIT ---")
    results.append(backtest(df, "C. Only max_hold",
        exit_rules={"ema_cross": False, "st_flip": False, "max_hold": 20},
        entry_rules={"midtrend": True}))

    # D. Current exits + midtrend only after 13:00
    print("\n--- D: MIDTREND ONLY AFTER 13:00 ---")
    results.append(backtest(df, "D. Midtrend after 13:00",
        exit_rules={"ema_cross": True, "st_flip": True, "max_hold": 20},
        entry_rules={"midtrend": True, "midtrend_after_hour": 13}))

    # E. No EMA_cross exit + midtrend after 13:00
    print("\n--- E: NO EMA_CROSS + MIDTREND AFTER 13:00 ---")
    results.append(backtest(df, "E. No EMA_cross + MT>13",
        exit_rules={"ema_cross": False, "st_flip": True, "max_hold": 20},
        entry_rules={"midtrend": True, "midtrend_after_hour": 13}))

    # F. Only max_hold + midtrend after 13:00
    print("\n--- F: ONLY MAX_HOLD + MIDTREND AFTER 13:00 ---")
    results.append(backtest(df, "F. Max_hold only + MT>13",
        exit_rules={"ema_cross": False, "st_flip": False, "max_hold": 20},
        entry_rules={"midtrend": True, "midtrend_after_hour": 13}))

    # G. No EMA_cross + higher max_hold (25 candles)
    print("\n--- G: NO EMA_CROSS + MAX_HOLD 25 ---")
    results.append(backtest(df, "G. No EMA_cross + hold 25",
        exit_rules={"ema_cross": False, "st_flip": True, "max_hold": 25},
        entry_rules={"midtrend": True}))

    # H. Crossover only (no midtrend) + no EMA_cross exit
    print("\n--- H: CROSSOVER ONLY + NO EMA_CROSS ---")
    results.append(backtest(df, "H. Cross only, no EMA_x",
        exit_rules={"ema_cross": False, "st_flip": True, "max_hold": 20},
        entry_rules={"midtrend": False}))

    # COMPARISON
    print(f"\n{'='*120}")
    print(f"  FINAL COMPARISON")
    print(f"{'='*120}")
    print(f"  {'Scenario':<30} {'Trades':>6} {'WR':>5} {'Net P&L':>12} {'PF':>5} {'MaxDD':>10} {'Green':>7} {'Avg/Tr':>8}")
    print(f"  {'-'*30} {'-'*6} {'-'*5} {'-'*12} {'-'*5} {'-'*10} {'-'*7} {'-'*8}")
    for r in results:
        if not r or r.get("n", 0) == 0: continue
        print(f"  {r['label']:<30} {r['n']:>6} {r['wr']:>4.0f}% {r['pnl']:>11,.0f} {r['pf']:>5.2f} "
              f"{r['dd']:>10,.0f} {r['green']:>3}/{r['green']+r['red']:<3} "
              f"{r['pnl']/r['n']:>8,.0f}")

    if _conn: _conn.close()


if __name__ == "__main__":
    main()
