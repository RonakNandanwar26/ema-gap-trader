"""hybrid_exit_test.py — Compare spot-only exit vs strike-indicator exit.

Spot candles drive entry decisions (same as current system).
Once in a trade, we compute EMA/SuperTrend/RSI on the strike's own
premium candles and check if strike-based exits fire earlier — catching
sideways theta/IV decay that spot indicators miss.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import time as _t, timedelta

import numpy as np
import pandas as pd

from config import get_strategy_config, get_instrument_config, get_dhan_db_path, get_backtest_dates
from indicators import compute_indicators
from strategy import check_entry, check_exit


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_spot_candles(conn: sqlite3.Connection, instrument: str, interval: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close FROM dhan_spot_candle "
        "WHERE instrument=? AND candle_type='intraday' AND interval_min=? ORDER BY timestamp",
        conn, params=(instrument, interval),
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _load_strike_candles(
    conn: sqlite3.Connection,
    instrument: str,
    direction: str,
    expiry_flag: str,
    strike: float,
    date: str,
    interval: int = 5,
    resample_to: int = 15,
) -> pd.DataFrame | None:
    """Load 5-min candles for a specific strike on a given date,
    resample to 15-min to match the spot candle timeframe, then
    compute indicators.

    Returns DataFrame with premium OHLC + indicators, or None if
    insufficient data.
    """
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    day_start = f"{date} 00:00:00"
    day_end = f"{date} 23:59:59"
    df = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close FROM dhan_option_candle "
        "WHERE instrument=? AND direction=? AND expiry_flag=? "
        "AND strike BETWEEN ? AND ? AND interval_min=? "
        "AND timestamp >= ? AND timestamp <= ? "
        "ORDER BY timestamp",
        conn, params=(instrument, dhan_dir, expiry_flag, strike - 0.5, strike + 0.5, interval, day_start, day_end),
    )
    if len(df) < 6:
        return None

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.drop_duplicates(subset="timestamp", keep="first")

    # Resample 5-min → 15-min OHLC
    if resample_to > interval:
        df = df.set_index("timestamp").resample(f"{resample_to}min").agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
        }).dropna().reset_index()

    if len(df) < 10:
        return None

    df = compute_indicators(df)
    return df


def _get_atm_premium(conn, ts, direction, instrument, expiry_flag):
    """Get ATM premium and strike nearest to timestamp (±10 min)."""
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    row = conn.execute(
        "SELECT close, strike FROM dhan_option_candle "
        "WHERE instrument=? AND strike_offset='ATM' AND direction=? "
        "AND expiry_flag=? AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (instrument, dhan_dir, expiry_flag, lo, hi, ts.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return (float(row[0]), float(row[1])) if row else (None, None)


def _get_premium_at(conn, ts, direction, instrument, expiry_flag, strike):
    """Get premium for a specific strike nearest to timestamp."""
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    row = conn.execute(
        "SELECT close FROM dhan_option_candle "
        "WHERE instrument=? AND direction=? AND expiry_flag=? "
        "AND strike BETWEEN ? AND ? AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (instrument, dhan_dir, expiry_flag, strike - 0.5, strike + 0.5, lo, hi, ts.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return float(row[0]) if row else None


# ---------------------------------------------------------------------------
# Strike-based exit logic
# ---------------------------------------------------------------------------

def check_strike_exit(strike_row: pd.Series, trade_dir: str) -> str | None:
    """Check exit using 15-min premium indicators.

    Premium EMA gap contracting = premium trend dying (theta/IV eating gains).
    This is the smoothest signal — no binary flips, just trend strength fading.
    """
    # Premium EMA gap contracting — trend losing steam
    if not pd.isna(strike_row["ema_gap_pct"]) and strike_row["ema_gap_expanding"] == False:
        # Only trigger if gap was meaningful (avoid noise near flat EMAs)
        if strike_row["ema_gap_pct"] < 1.0:
            return "prem_gap_contract"

    # Premium SuperTrend flip bearish — premium trend reversing
    if strike_row["st_dir"] == -1:
        return "prem_ST_flip"

    return None


# ---------------------------------------------------------------------------
# Main comparison engine
# ---------------------------------------------------------------------------

def run_comparison() -> list[dict]:
    sc = get_strategy_config()
    ic = get_instrument_config()
    db_path = get_dhan_db_path()
    dates = get_backtest_dates()

    conn = sqlite3.connect(db_path)
    print(f"  Loading spot candles...", flush=True)
    df = _load_spot_candles(conn, ic.name, sc.candle_interval)
    df = compute_indicators(df)
    print(f"  Loaded {len(df)} spot candles, computing...", flush=True)

    start_ts = pd.Timestamp(dates[0])
    end_ts = pd.Timestamp(dates[1])
    if end_ts == end_ts.normalize():
        end_ts += pd.Timedelta(hours=23, minutes=59, seconds=59)
    indices = df.index[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].tolist()

    # ORB ranges
    orb_ranges: dict = {}
    if sc.orb_filter:
        df["_date"] = df["timestamp"].dt.date
        df["_time"] = df["timestamp"].dt.time
        for d, grp in df.groupby("_date"):
            first_30 = grp[(grp["_time"] >= _t(9, 15)) & (grp["_time"] <= _t(9, 30))]
            if len(first_30) >= 2:
                orb_ranges[d] = (first_30["high"].max(), first_30["low"].min())
        df.drop(columns=["_date", "_time"], inplace=True)

    results: list[dict] = []
    open_trade: dict | None = None
    last_exit_idx = -999
    strike_df: pd.DataFrame | None = None
    trade_count = 0

    total = len(indices)
    for progress, i in enumerate(indices):
        if i == 0:
            continue
        if progress % 500 == 0:
            print(f"\r  Processing candle {progress}/{total} | trades so far: {trade_count}", end="", flush=True)

        row = df.iloc[i]
        prev = df.iloc[i - 1]
        ts = row["timestamp"]

        # === EXIT (dual tracking) ===
        if open_trade is not None:
            candles_held = i - open_trade["idx"]

            # --- Spot exit (baseline) ---
            if open_trade["spot_exit_ts"] is None:
                spot_reason = check_exit(row, open_trade["dir"], candles_held, sc.max_hold_candles, sc.ema_gap_floor)
                if spot_reason:
                    ep = _get_premium_at(conn, ts, open_trade["dir"], ic.name, ic.expiry_flag, open_trade["strike"])
                    open_trade["spot_exit_ts"] = ts
                    open_trade["spot_exit_reason"] = spot_reason
                    open_trade["spot_exit_prem"] = ep
                    open_trade["spot_exit_candles"] = candles_held

            # --- Strike exit (combined — fires alongside spot) ---
            if open_trade["strike_exit_ts"] is None and strike_df is not None:
                # Find matching timestamp in strike candles
                match = strike_df[strike_df["timestamp"] == ts]
                if not match.empty:
                    strike_row = match.iloc[0]
                    # Skip first 2 candles (let indicators warm up post-entry)
                    if candles_held >= 2:
                        strike_reason = check_strike_exit(strike_row, open_trade["dir"])
                        if strike_reason:
                            ep = _get_premium_at(conn, ts, open_trade["dir"], ic.name, ic.expiry_flag, open_trade["strike"])
                            open_trade["strike_exit_ts"] = ts
                            open_trade["strike_exit_reason"] = strike_reason
                            open_trade["strike_exit_prem"] = ep
                            open_trade["strike_exit_candles"] = candles_held

            # --- Close trade when spot exit fires (baseline always determines end) ---
            # We wait for spot exit so we can compare baseline P&L properly.
            # The combined approach just picks whichever fired first.
            if open_trade["spot_exit_ts"] is not None:
                prem_in = open_trade["prem_in"]
                strike_fired = open_trade["strike_exit_ts"] is not None

                # Determine combined exit (whichever fired first)
                if strike_fired and open_trade["strike_exit_ts"] <= open_trade["spot_exit_ts"]:
                    combined_reason = open_trade["strike_exit_reason"]
                    combined_prem = open_trade["strike_exit_prem"]
                    combined_candles = open_trade["strike_exit_candles"]
                else:
                    combined_reason = open_trade["spot_exit_reason"]
                    combined_prem = open_trade["spot_exit_prem"]
                    combined_candles = open_trade["spot_exit_candles"]

                # P&L
                spot_pnl = None
                combined_pnl = None
                if prem_in is not None:
                    if open_trade["spot_exit_prem"] is not None:
                        spot_pnl = (open_trade["spot_exit_prem"] - prem_in) * ic.lot_size
                    if combined_prem is not None:
                        combined_pnl = (combined_prem - prem_in) * ic.lot_size

                results.append({
                    "entry_ts": open_trade["ts"],
                    "dir": open_trade["dir"],
                    "strike": open_trade["strike"],
                    "prem_in": prem_in,
                    # Spot (baseline — what current system does)
                    "spot_exit_ts": open_trade["spot_exit_ts"],
                    "spot_exit_reason": open_trade["spot_exit_reason"],
                    "spot_exit_prem": open_trade["spot_exit_prem"],
                    "spot_pnl": spot_pnl,
                    "spot_candles": open_trade["spot_exit_candles"],
                    # Combined (first of spot or strike to fire)
                    "strike_exit_ts": open_trade["strike_exit_ts"] if strike_fired else None,
                    "strike_exit_reason": combined_reason,
                    "strike_exit_prem": combined_prem,
                    "strike_pnl": combined_pnl,
                    "strike_candles": combined_candles,
                    # Data availability
                    "strike_data": open_trade.get("strike_data_ok", False),
                })

                open_trade = None
                strike_df = None
                last_exit_idx = i
                trade_count += 1

        # === ENTRY (same as baseline — spot indicators) ===
        if open_trade is not None:
            continue

        orb = orb_ranges.get(ts.date())
        orb_h, orb_l = orb if (orb and ts.time() > _t(9, 30)) else (None, None)
        direction = check_entry(row, prev, sc.extra_entry_mode, sc.ema_gap_min,
                                last_exit_idx, i, sc.cooldown_candles, orb_h, orb_l, sc.orb_filter)
        if direction is None:
            continue

        prem, strike = _get_atm_premium(conn, ts, direction, ic.name, ic.expiry_flag)
        if strike is None:
            continue

        # Load strike candles for this day and compute strike indicators
        date_str = ts.strftime("%Y-%m-%d")
        strike_df = _load_strike_candles(conn, ic.name, direction, ic.expiry_flag, strike, date_str, interval=5)

        open_trade = {
            "ts": ts, "idx": i, "dir": direction,
            "strike": strike, "prem_in": prem,
            "spot_exit_ts": None, "spot_exit_reason": None,
            "spot_exit_prem": None, "spot_exit_candles": None,
            "strike_exit_ts": None, "strike_exit_reason": None,
            "strike_exit_prem": None, "strike_exit_candles": None,
            "strike_data_ok": strike_df is not None,
        }

    # Close any remaining open trade
    if open_trade is not None and indices:
        last_row = df.iloc[indices[-1]]
        last_ts = last_row["timestamp"]
        ep = _get_premium_at(conn, last_ts, open_trade["dir"], ic.name, ic.expiry_flag, open_trade["strike"])
        prem_in = open_trade["prem_in"]
        pnl = (ep - prem_in) * ic.lot_size if ep and prem_in else None
        results.append({
            "entry_ts": open_trade["ts"], "dir": open_trade["dir"],
            "strike": open_trade["strike"], "prem_in": prem_in,
            "spot_exit_ts": last_ts, "spot_exit_reason": "end",
            "spot_exit_prem": ep, "spot_pnl": pnl, "spot_candles": 0,
            "strike_exit_ts": open_trade["strike_exit_ts"] or last_ts,
            "strike_exit_reason": open_trade["strike_exit_reason"] or "end",
            "strike_exit_prem": open_trade["strike_exit_prem"] or ep,
            "strike_pnl": ((open_trade["strike_exit_prem"] or ep or 0) - (prem_in or 0)) * ic.lot_size if prem_in else None,
            "strike_candles": open_trade["strike_exit_candles"] or 0,
            "strike_data": open_trade.get("strike_data_ok", False),
        })

    print(f"\r  Done. {trade_count} trades processed.                     ", flush=True)
    conn.close()
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_comparison(results: list[dict]) -> None:
    if not results:
        print("No trades found.")
        return

    # Filter to trades where both methods have valid P&L
    valid = [r for r in results if r["spot_pnl"] is not None and r["strike_pnl"] is not None]
    with_data = [r for r in valid if r["strike_data"]]
    no_data = len(valid) - len(with_data)

    print(f"\n{'=' * 110}")
    print(f"  COMBINED EXIT TEST — Spot + Strike (ST flip / RSI weak only)")
    print(f"  Exit on whichever fires first: spot indicators OR strike ST_flip / RSI_weak")
    print(f"{'=' * 110}")
    print(f"  Total trades: {len(results)}  |  Valid P&L: {len(valid)}  |  Strike data available: {len(with_data)}  |  No strike data: {no_data}")

    # --- Per-trade detail (only show trades where combined differs from spot) ---
    changed = [r for r in with_data
               if r["strike_exit_ts"] is not None
               and r["strike_exit_ts"] < r["spot_exit_ts"]]

    print(f"\n  Trades where strike exit fired BEFORE spot ({len(changed)} of {len(with_data)}):")
    print(f"  {'Entry':>19}  Dir  {'Spot Reason':>11} {'Spot P&L':>9}  {'Combined':>14} {'Comb P&L':>9}  {'Diff':>9}")
    print(f"  {'-' * 100}")

    saves = 0
    hurts = 0
    total_spot_pnl = 0
    total_combined_pnl = 0

    for r in with_data:
        total_spot_pnl += r["spot_pnl"]
        total_combined_pnl += r["strike_pnl"]

    for r in changed:
        sp = r["spot_pnl"]
        cp = r["strike_pnl"]
        diff = cp - sp
        if cp >= sp:
            saves += 1
            tag = "SAVE"
        else:
            hurts += 1
            tag = "HURT"

        entry_str = r["entry_ts"].strftime("%Y-%m-%d %H:%M")
        print(f"  {entry_str}   {r['dir']}  {r['spot_exit_reason']:>11} {sp:>+9,.0f}"
              f"  {r['strike_exit_reason']:>14} {cp:>+9,.0f}"
              f"  {diff:>+9,.0f}  {tag}")

    # --- Aggregate ---
    n = len(with_data) or 1
    spot_winners = sum(1 for r in with_data if r["spot_pnl"] and r["spot_pnl"] > 0)
    combined_winners = sum(1 for r in with_data if r["strike_pnl"] and r["strike_pnl"] > 0)

    # Compute drawdown/streak for both
    def _dd_streak(trades, key):
        eq = pk = dd = 0.0
        streak = mx_streak = 0
        for t in trades:
            eq += t[key]
            if eq > pk: pk = eq
            if pk - eq > dd: dd = pk - eq
            if t[key] <= 0:
                streak += 1
                mx_streak = max(mx_streak, streak)
            else:
                streak = 0
        return dd, mx_streak

    spot_dd, spot_streak = _dd_streak(with_data, "spot_pnl")
    comb_dd, comb_streak = _dd_streak(with_data, "strike_pnl")

    # Profit factor
    spot_gw = sum(r["spot_pnl"] for r in with_data if r["spot_pnl"] > 0)
    spot_gl = abs(sum(r["spot_pnl"] for r in with_data if r["spot_pnl"] <= 0))
    comb_gw = sum(r["strike_pnl"] for r in with_data if r["strike_pnl"] > 0)
    comb_gl = abs(sum(r["strike_pnl"] for r in with_data if r["strike_pnl"] <= 0))
    spot_pf = spot_gw / spot_gl if spot_gl else float("inf")
    comb_pf = comb_gw / comb_gl if comb_gl else float("inf")

    spot_avg_w = spot_gw / spot_winners if spot_winners else 0
    spot_avg_l = spot_gl / (n - spot_winners) if (n - spot_winners) else 0
    comb_avg_w = comb_gw / combined_winners if combined_winners else 0
    comb_avg_l = comb_gl / (n - combined_winners) if (n - combined_winners) else 0

    # Monthly
    spot_monthly: dict[str, float] = {}
    comb_monthly: dict[str, float] = {}
    for r in with_data:
        m = pd.Timestamp(r["entry_ts"]).strftime("%Y-%m")
        spot_monthly[m] = spot_monthly.get(m, 0) + r["spot_pnl"]
        comb_monthly[m] = comb_monthly.get(m, 0) + r["strike_pnl"]
    spot_green = sum(1 for v in spot_monthly.values() if v >= 0)
    comb_green = sum(1 for v in comb_monthly.values() if v >= 0)
    total_months = len(spot_monthly)

    print(f"\n{'=' * 110}")
    print(f"  AGGREGATE: Spot (current) vs Combined (spot + strike ST_flip/RSI_weak)")
    print(f"{'=' * 110}")
    print(f"  {'':30} {'SPOT (current)':>18}  {'COMBINED':>18}")
    print(f"  {'Trades:':30} {n:>18}  {n:>18}")
    print(f"  {'Winners:':30} {f'{spot_winners}/{n}':>18}  {f'{combined_winners}/{n}':>18}")
    print(f"  {'Win Rate:':30} {spot_winners/n*100:>17.0f}%  {combined_winners/n*100:>17.0f}%")
    print(f"  {'Net P&L:':30} {'Rs.'+f'{total_spot_pnl:>+,.0f}':>18}  {'Rs.'+f'{total_combined_pnl:>+,.0f}':>18}")
    print(f"  {'Profit Factor:':30} {spot_pf:>18.2f}  {comb_pf:>18.2f}")
    print(f"  {'Avg Win:':30} {'Rs.'+f'{spot_avg_w:>,.0f}':>18}  {'Rs.'+f'{comb_avg_w:>,.0f}':>18}")
    print(f"  {'Avg Loss:':30} {'Rs.'+f'{spot_avg_l:>,.0f}':>18}  {'Rs.'+f'{comb_avg_l:>,.0f}':>18}")
    print(f"  {'Max Drawdown:':30} {'Rs.'+f'{spot_dd:>,.0f}':>18}  {'Rs.'+f'{comb_dd:>,.0f}':>18}")
    print(f"  {'Max Loss Streak:':30} {spot_streak:>18}  {comb_streak:>18}")
    print(f"  {'Green Months:':30} {f'{spot_green}/{total_months}':>18}  {f'{comb_green}/{total_months}':>18}")
    print()
    print(f"  Strike exits that fired before spot: {len(changed)}/{len(with_data)}")
    print(f"    Saves (better P&L):  {saves}")
    print(f"    Hurts (worse P&L):   {hurts}")
    print(f"  P&L difference (combined - baseline):  Rs.{total_combined_pnl - total_spot_pnl:>+,.0f}")

    # --- Yearly ---
    print(f"\n  Yearly Comparison:")
    print(f"  {'Year':>6}  {'Spot P&L':>12} {'WR':>5}  {'Combined P&L':>14} {'WR':>5}  {'Diff':>12}")
    yearly_spot: dict[int, dict] = {}
    yearly_comb: dict[int, dict] = {}
    for r in with_data:
        y = pd.Timestamp(r["entry_ts"]).year
        for store, key in [(yearly_spot, "spot_pnl"), (yearly_comb, "strike_pnl")]:
            if y not in store: store[y] = {"pnl": 0, "n": 0, "w": 0}
            store[y]["pnl"] += r[key]
            store[y]["n"] += 1
            if r[key] > 0: store[y]["w"] += 1
    for y in sorted(yearly_spot):
        sp = yearly_spot[y]
        cp = yearly_comb.get(y, {"pnl": 0, "n": 0, "w": 0})
        swr = sp["w"]/sp["n"]*100 if sp["n"] else 0
        cwr = cp["w"]/cp["n"]*100 if cp["n"] else 0
        print(f"  {y:>6}  Rs.{sp['pnl']:>+10,.0f} {swr:>4.0f}%  Rs.{cp['pnl']:>+12,.0f} {cwr:>4.0f}%  Rs.{cp['pnl']-sp['pnl']:>+10,.0f}")

    # --- Combined exit reason breakdown ---
    comb_reasons: dict[str, dict] = {}
    for r in with_data:
        reason = r["strike_exit_reason"]
        if reason not in comb_reasons:
            comb_reasons[reason] = {"n": 0, "pnl": 0, "w": 0}
        comb_reasons[reason]["n"] += 1
        comb_reasons[reason]["pnl"] += r["strike_pnl"]
        if r["strike_pnl"] > 0:
            comb_reasons[reason]["w"] += 1

    print(f"\n  Combined exit reason breakdown:")
    for reason in sorted(comb_reasons):
        d = comb_reasons[reason]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        print(f"    {reason:>18}: {d['n']:>3} trades, WR {wr:.0f}%, P&L: Rs.{d['pnl']:>+10,.0f}")

    print()


if __name__ == "__main__":
    results = run_comparison()
    print_comparison(results)
