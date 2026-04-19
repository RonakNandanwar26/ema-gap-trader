"""dual_confirm_test.py — Spot exits gated by premium confirmation.

Same entry as current system (spot indicators).
Same exit rules, BUT: ST_flip and EMA_cross only fire if the premium's
SuperTrend also confirms (bearish). If premium is still bullish, hold —
the spot signal is likely false.

gap_contract and max_hold remain ungated (they're already good).
"""

from __future__ import annotations

import sqlite3
from datetime import time as _t, timedelta

import pandas as pd

from config import (
    StrategyConfig, InstrumentConfig,
    get_strategy_config, get_instrument_config, get_dhan_db_path, get_backtest_dates,
)
from indicators import compute_indicators
from strategy import check_entry


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_spot_candles(conn, instrument, interval):
    df = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close FROM dhan_spot_candle "
        "WHERE instrument=? AND candle_type='intraday' AND interval_min=? ORDER BY timestamp",
        conn, params=(instrument, interval),
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _load_strike_candles_for_day(conn, instrument, direction, expiry_flag, strike, date_str):
    """Load 5-min candles for a fixed strike on a given date, resample to 15-min."""
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    df = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close FROM dhan_option_candle "
        "WHERE instrument=? AND direction=? AND expiry_flag=? "
        "AND strike BETWEEN ? AND ? AND interval_min=5 "
        "AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
        conn, params=(instrument, dhan_dir, expiry_flag,
                      strike - 0.5, strike + 0.5,
                      f"{date_str} 00:00:00", f"{date_str} 23:59:59"),
    )
    if len(df) < 6:
        return None
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.drop_duplicates(subset="timestamp", keep="first")
    df = df.set_index("timestamp").resample("15min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
    }).dropna().reset_index()
    if len(df) < 5:
        return None
    df = compute_indicators(df)
    return df


def _get_atm_premium(conn, ts, direction, instrument, expiry_flag):
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    row = conn.execute(
        "SELECT close, strike FROM dhan_option_candle "
        "WHERE instrument=? AND strike_offset='ATM' AND direction=? "
        "AND expiry_flag=? AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (instrument, dhan_dir, expiry_flag, lo, hi,
         ts.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return (float(row[0]), float(row[1])) if row else (None, None)


def _get_premium_by_strike(conn, ts, direction, instrument, expiry_flag, strike):
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    row = conn.execute(
        "SELECT close FROM dhan_option_candle "
        "WHERE instrument=? AND direction=? AND expiry_flag=? "
        "AND strike BETWEEN ? AND ? AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (instrument, dhan_dir, expiry_flag, strike - 0.5, strike + 0.5,
         lo, hi, ts.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return float(row[0]) if row else None


# ---------------------------------------------------------------------------
# Dual-confirmation exit
# ---------------------------------------------------------------------------

def check_exit_dual(row, trade_dir, candles_held, max_hold, ema_gap_floor,
                    prem_st_dir):
    """Same exit rules as strategy.check_exit, but ST_flip and EMA_cross
    require premium SuperTrend confirmation.

    prem_st_dir: premium's SuperTrend direction (1=bullish, -1=bearish),
                 or None if no premium data available.
    """
    # 1. SuperTrend flip — GATED by premium confirmation
    spot_st_flip = (
        (trade_dir == "CE" and row["st_dir"] == -1) or
        (trade_dir == "PE" and row["st_dir"] == 1)
    )
    if spot_st_flip:
        # Premium confirms? (bearish = -1 means premium trend also reversed)
        if prem_st_dir is None or prem_st_dir == -1:
            return "ST_flip_confirmed"
        # Premium still bullish — false alarm, hold
        # (will be tracked as "ST_flip_blocked")

    # 2. EMA cross reversal — GATED by premium confirmation
    spot_ema_cross = (
        (trade_dir == "CE" and row["ema9"] < row["ema21"]) or
        (trade_dir == "PE" and row["ema9"] > row["ema21"])
    )
    if spot_ema_cross:
        if prem_st_dir is None or prem_st_dir == -1:
            return "EMA_cross_confirmed"
        # Premium still bullish — hold

    # 3. EMA gap contraction — UNGATED (already good signal)
    if ema_gap_floor > 0 and candles_held >= 2 and row["ema_gap_pct"] < ema_gap_floor:
        return "gap_contract"

    # 4. Max hold — UNGATED
    if candles_held >= max_hold:
        return "max_hold"

    return None


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------

def run_dual_backtest():
    sc = get_strategy_config()
    ic = get_instrument_config()
    db_path = get_dhan_db_path()
    dates = get_backtest_dates()

    conn = sqlite3.connect(db_path)
    print("  Loading spot candles...", flush=True)
    df = _load_spot_candles(conn, ic.name, sc.candle_interval)
    df = compute_indicators(df)
    print(f"  {len(df)} spot candles loaded.", flush=True)

    start_ts = pd.Timestamp(dates[0])
    end_ts = pd.Timestamp(dates[1])
    if end_ts == end_ts.normalize():
        end_ts += pd.Timedelta(hours=23, minutes=59, seconds=59)
    indices = df.index[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].tolist()

    # ORB ranges (same as backtester)
    orb_ranges = {}
    if sc.orb_filter:
        df["_date"] = df["timestamp"].dt.date
        df["_time"] = df["timestamp"].dt.time
        for d, grp in df.groupby("_date"):
            first_30 = grp[(grp["_time"] >= _t(9, 15)) & (grp["_time"] <= _t(9, 30))]
            if len(first_30) >= 2:
                orb_ranges[d] = (first_30["high"].max(), first_30["low"].min())
        df.drop(columns=["_date", "_time"], inplace=True)

    # --- Run both baseline and dual-confirm in one pass ---
    baseline_trades = []
    dual_trades = []
    open_baseline = None
    open_dual = None
    last_exit_idx_b = -999
    last_exit_idx_d = -999

    # Cache for strike candles per (date, strike, direction)
    strike_cache = {}
    trade_count = 0

    for progress, i in enumerate(indices):
        if i == 0:
            continue
        if progress % 500 == 0:
            print(f"\r  Candle {progress}/{len(indices)} | trades: {trade_count}", end="", flush=True)

        row = df.iloc[i]
        prev = df.iloc[i - 1]
        ts = row["timestamp"]

        # ======== BASELINE EXIT (current system — no premium) ========
        if open_baseline is not None:
            candles_held = i - open_baseline["idx"]
            from strategy import check_exit
            reason = check_exit(row, open_baseline["dir"], candles_held,
                                sc.max_hold_candles, sc.ema_gap_floor)
            if reason:
                ep = None
                if open_baseline["strike"]:
                    ep = _get_premium_by_strike(conn, ts, open_baseline["dir"],
                                               ic.name, ic.expiry_flag, open_baseline["strike"])
                if ep is None:
                    ep, _ = _get_atm_premium(conn, ts, open_baseline["dir"],
                                            ic.name, ic.expiry_flag)
                pnl = (ep - open_baseline["prem_in"]) * ic.lot_size if ep and open_baseline["prem_in"] else None
                baseline_trades.append({
                    "entry": open_baseline["ts"], "exit": ts,
                    "dir": open_baseline["dir"], "strike": open_baseline["strike"],
                    "prem_in": open_baseline["prem_in"], "prem_out": ep,
                    "prem_pnl": pnl, "candles": candles_held, "reason": reason,
                    "entry_type": open_baseline.get("entry_type"),
                })
                open_baseline = None
                last_exit_idx_b = i

        # ======== DUAL-CONFIRM EXIT ========
        if open_dual is not None:
            candles_held = i - open_dual["idx"]

            # Get premium SuperTrend direction for this candle
            prem_st_dir = None
            cache_key = (ts.date().isoformat(), open_dual["strike"], open_dual["dir"])
            if cache_key not in strike_cache:
                sdf = _load_strike_candles_for_day(
                    conn, ic.name, open_dual["dir"], ic.expiry_flag,
                    open_dual["strike"], ts.date().isoformat())
                strike_cache[cache_key] = sdf
            sdf = strike_cache[cache_key]
            if sdf is not None:
                match = sdf[sdf["timestamp"] == ts]
                if not match.empty:
                    prem_st_dir = int(match.iloc[0]["st_dir"])

            reason = check_exit_dual(row, open_dual["dir"], candles_held,
                                     sc.max_hold_candles, sc.ema_gap_floor,
                                     prem_st_dir)
            if reason:
                ep = None
                if open_dual["strike"]:
                    ep = _get_premium_by_strike(conn, ts, open_dual["dir"],
                                               ic.name, ic.expiry_flag, open_dual["strike"])
                if ep is None:
                    ep, _ = _get_atm_premium(conn, ts, open_dual["dir"],
                                            ic.name, ic.expiry_flag)
                pnl = (ep - open_dual["prem_in"]) * ic.lot_size if ep and open_dual["prem_in"] else None
                dual_trades.append({
                    "entry": open_dual["ts"], "exit": ts,
                    "dir": open_dual["dir"], "strike": open_dual["strike"],
                    "prem_in": open_dual["prem_in"], "prem_out": ep,
                    "prem_pnl": pnl, "candles": candles_held, "reason": reason,
                    "entry_type": open_dual.get("entry_type"),
                })
                open_dual = None
                last_exit_idx_d = i
                trade_count += 1

        # ======== ENTRY (same for both, run in parallel) ========
        orb = orb_ranges.get(ts.date())
        orb_h, orb_l = orb if (orb and ts.time() > _t(9, 30)) else (None, None)

        if open_baseline is None:
            direction = check_entry(row, prev, sc.extra_entry_mode, sc.ema_gap_min,
                                    last_exit_idx_b, i, sc.cooldown_candles,
                                    orb_h, orb_l, sc.orb_filter)
            if direction:
                crossover_up = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
                crossover_down = prev["ema9"] >= prev["ema21"] and row["ema9"] < row["ema21"]
                is_cross = (direction == "CE" and crossover_up) or (direction == "PE" and crossover_down)
                prem, strike = _get_atm_premium(conn, ts, direction, ic.name, ic.expiry_flag)
                open_baseline = {
                    "ts": ts, "idx": i, "dir": direction,
                    "prem_in": prem, "strike": strike,
                    "entry_type": "crossover" if is_cross else sc.extra_entry_mode,
                }

        if open_dual is None:
            direction = check_entry(row, prev, sc.extra_entry_mode, sc.ema_gap_min,
                                    last_exit_idx_d, i, sc.cooldown_candles,
                                    orb_h, orb_l, sc.orb_filter)
            if direction:
                crossover_up = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
                crossover_down = prev["ema9"] >= prev["ema21"] and row["ema9"] < row["ema21"]
                is_cross = (direction == "CE" and crossover_up) or (direction == "PE" and crossover_down)
                prem, strike = _get_atm_premium(conn, ts, direction, ic.name, ic.expiry_flag)
                open_dual = {
                    "ts": ts, "idx": i, "dir": direction,
                    "prem_in": prem, "strike": strike,
                    "entry_type": "crossover" if is_cross else sc.extra_entry_mode,
                }

    # Close remaining
    for trades_list, open_trade in [(baseline_trades, open_baseline), (dual_trades, open_dual)]:
        if open_trade is not None and indices:
            last = df.iloc[indices[-1]]
            ep = _get_premium_by_strike(conn, last["timestamp"], open_trade["dir"],
                                       ic.name, ic.expiry_flag, open_trade["strike"]) if open_trade["strike"] else None
            if ep is None:
                ep, _ = _get_atm_premium(conn, last["timestamp"], open_trade["dir"],
                                        ic.name, ic.expiry_flag)
            pnl = (ep - open_trade["prem_in"]) * ic.lot_size if ep and open_trade["prem_in"] else None
            trades_list.append({
                "entry": open_trade["ts"], "exit": last["timestamp"],
                "dir": open_trade["dir"], "strike": open_trade["strike"],
                "prem_in": open_trade["prem_in"], "prem_out": ep,
                "prem_pnl": pnl, "candles": 0, "reason": "end",
                "entry_type": open_trade.get("entry_type"),
            })

    print(f"\r  Done. Baseline: {len(baseline_trades)}, Dual: {len(dual_trades)} trades.          ", flush=True)
    conn.close()
    return baseline_trades, dual_trades


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _stats(trades):
    valid = [t for t in trades if t["prem_pnl"] is not None]
    n = len(valid)
    if n == 0:
        return None
    w = sum(1 for t in valid if t["prem_pnl"] > 0)
    pnl = sum(t["prem_pnl"] for t in valid)
    gw = sum(t["prem_pnl"] for t in valid if t["prem_pnl"] > 0)
    gl = abs(sum(t["prem_pnl"] for t in valid if t["prem_pnl"] <= 0))
    pf = gw / gl if gl else float("inf")
    aw = gw / w if w else 0
    al = gl / (n - w) if (n - w) else 0
    eq = pk = dd = 0.0
    streak = mx = 0
    for t in valid:
        eq += t["prem_pnl"]
        if eq > pk: pk = eq
        if pk - eq > dd: dd = pk - eq
        if t["prem_pnl"] <= 0:
            streak += 1
            mx = max(mx, streak)
        else:
            streak = 0
    monthly = {}
    for t in valid:
        m = pd.Timestamp(t["entry"]).strftime("%Y-%m")
        monthly[m] = monthly.get(m, 0) + t["prem_pnl"]
    green = sum(1 for v in monthly.values() if v >= 0)
    yearly = {}
    for t in valid:
        y = pd.Timestamp(t["entry"]).year
        if y not in yearly: yearly[y] = {"pnl": 0, "n": 0, "w": 0}
        yearly[y]["pnl"] += t["prem_pnl"]
        yearly[y]["n"] += 1
        if t["prem_pnl"] > 0: yearly[y]["w"] += 1
    reasons = {}
    for t in valid:
        r = t["reason"]
        if r not in reasons: reasons[r] = {"n": 0, "pnl": 0, "w": 0}
        reasons[r]["n"] += 1
        reasons[r]["pnl"] += t["prem_pnl"]
        if t["prem_pnl"] > 0: reasons[r]["w"] += 1
    entry_types = {}
    for t in valid:
        et = t.get("entry_type", "crossover")
        if et not in entry_types: entry_types[et] = {"n": 0, "pnl": 0, "w": 0}
        entry_types[et]["n"] += 1
        entry_types[et]["pnl"] += t["prem_pnl"]
        if t["prem_pnl"] > 0: entry_types[et]["w"] += 1
    return {
        "n": n, "w": w, "wr": w/n*100, "pnl": pnl, "pf": pf,
        "aw": aw, "al": al, "dd": dd, "mx": mx,
        "green": green, "months": len(monthly),
        "yearly": yearly, "reasons": reasons, "entry_types": entry_types,
    }


def print_report(baseline, dual):
    bs = _stats(baseline)
    ds = _stats(dual)
    if not bs or not ds:
        print("  Insufficient data.")
        return

    print(f"\n{'=' * 90}")
    print(f"  CURRENT SYSTEM  vs  DUAL-CONFIRM (premium gates ST_flip & EMA_cross)")
    print(f"{'=' * 90}")
    print(f"  {'':30} {'CURRENT':>18}  {'DUAL-CONFIRM':>18}")
    print(f"  {'Trades:':30} {bs['n']:>18}  {ds['n']:>18}")
    bw = f"{bs['w']}/{bs['n']}"
    dw = f"{ds['w']}/{ds['n']}"
    print(f"  {'Winners:':30} {bw:>18}  {dw:>18}")
    print(f"  {'Win Rate:':30} {bs['wr']:>17.0f}%  {ds['wr']:>17.0f}%")
    bp = f"Rs.{bs['pnl']:>+,.0f}"
    dp = f"Rs.{ds['pnl']:>+,.0f}"
    print(f"  {'Net P&L:':30} {bp:>18}  {dp:>18}")
    print(f"  {'Profit Factor:':30} {bs['pf']:>18.2f}  {ds['pf']:>18.2f}")
    baw = f"Rs.{bs['aw']:>,.0f}"
    daw = f"Rs.{ds['aw']:>,.0f}"
    bal = f"Rs.{bs['al']:>,.0f}"
    dal = f"Rs.{ds['al']:>,.0f}"
    print(f"  {'Avg Win:':30} {baw:>18}  {daw:>18}")
    print(f"  {'Avg Loss:':30} {bal:>18}  {dal:>18}")
    bdd = f"Rs.{bs['dd']:>,.0f}"
    ddd = f"Rs.{ds['dd']:>,.0f}"
    print(f"  {'Max Drawdown:':30} {bdd:>18}  {ddd:>18}")
    print(f"  {'Max Loss Streak:':30} {bs['mx']:>18}  {ds['mx']:>18}")
    bgm = f"{bs['green']}/{bs['months']}"
    dgm = f"{ds['green']}/{ds['months']}"
    print(f"  {'Green Months:':30} {bgm:>18}  {dgm:>18}")

    diff = ds['pnl'] - bs['pnl']
    print(f"\n  P&L difference (dual - baseline): Rs.{diff:>+,.0f}")

    print(f"\n  Yearly:")
    print(f"  {'Year':>6}  {'Current P&L':>12} {'WR':>5}  {'Dual P&L':>14} {'WR':>5}  {'Diff':>12}")
    all_years = sorted(set(list(bs["yearly"]) + list(ds["yearly"])))
    for y in all_years:
        b = bs["yearly"].get(y, {"pnl": 0, "n": 1, "w": 0})
        d = ds["yearly"].get(y, {"pnl": 0, "n": 1, "w": 0})
        bwr = b["w"]/b["n"]*100 if b["n"] else 0
        dwr = d["w"]/d["n"]*100 if d["n"] else 0
        print(f"  {y:>6}  Rs.{b['pnl']:>+10,.0f} {bwr:>4.0f}%  Rs.{d['pnl']:>+12,.0f} {dwr:>4.0f}%  Rs.{d['pnl']-b['pnl']:>+10,.0f}")

    print(f"\n  Current exit reasons:")
    for r in sorted(bs["reasons"]):
        d = bs["reasons"][r]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        print(f"    {r:>20}: {d['n']:>3} trades, WR {wr:.0f}%, P&L: Rs.{d['pnl']:>+10,.0f}")

    print(f"\n  Dual-confirm exit reasons:")
    for r in sorted(ds["reasons"]):
        d = ds["reasons"][r]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        print(f"    {r:>20}: {d['n']:>3} trades, WR {wr:.0f}%, P&L: Rs.{d['pnl']:>+10,.0f}")

    print(f"\n  Entry types (dual):")
    for et in sorted(ds["entry_types"]):
        d = ds["entry_types"][et]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        print(f"    {et:>14}: {d['n']:>3} trades, WR {wr:.0f}%, P&L: Rs.{d['pnl']:>+10,.0f}")

    print()


if __name__ == "__main__":
    baseline, dual = run_dual_backtest()
    print_report(baseline, dual)
