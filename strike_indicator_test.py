"""strike_indicator_test.py — Exact same strategy, but on premium candles.

Same entry rules (crossover + midtrend + EMA gap + RSI + ORB).
Same exit rules (ST flip + EMA cross + gap contraction + max hold).
Same config params. Just swap spot candles for 15-min resampled premium candles.

Spot is used ONLY for calculating which strike is ATM.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import time as _t, timedelta

import numpy as np
import pandas as pd

from config import (
    get_strategy_config, get_instrument_config, get_dhan_db_path,
    get_backtest_dates, get_capital, MARKET_OPEN, TIME_EXIT,
)
from indicators import compute_indicators
from strategy import check_entry, check_exit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _calc_atm(spot_price: float, strike_interval: int) -> float:
    return round(spot_price / strike_interval) * strike_interval


def _get_trading_days(conn: sqlite3.Connection, instrument: str, start: str, end: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT DATE(timestamp) FROM dhan_spot_candle "
        "WHERE instrument=? AND candle_type='intraday' AND interval_min=5 "
        "AND DATE(timestamp) >= ? AND DATE(timestamp) <= ? "
        "ORDER BY DATE(timestamp)",
        (instrument, start, end),
    ).fetchall()
    return [r[0] for r in rows]


def _load_strike_candles(
    conn: sqlite3.Connection,
    instrument: str,
    direction: str,
    expiry_flag: str,
    strike: float,
    dates: list[str],
) -> pd.DataFrame | None:
    """Load 5-min candles for a fixed strike, resample to 15-min, compute indicators."""
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    conditions = " OR ".join(
        f"(timestamp >= '{d} 00:00:00' AND timestamp <= '{d} 23:59:59')"
        for d in dates
    )
    df = pd.read_sql_query(
        f"SELECT timestamp, open, high, low, close, spot FROM dhan_option_candle "
        f"WHERE instrument=? AND direction=? AND expiry_flag=? "
        f"AND strike BETWEEN ? AND ? AND interval_min=5 "
        f"AND ({conditions}) ORDER BY timestamp",
        conn, params=(instrument, dhan_dir, expiry_flag, strike - 0.5, strike + 0.5),
    )
    if len(df) < 6:
        return None

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.drop_duplicates(subset="timestamp", keep="first")

    # Resample 5-min → 15-min
    df = df.set_index("timestamp").resample("15min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "spot": "last",
    }).dropna().reset_index()

    if len(df) < 10:
        return None

    df = compute_indicators(df)
    return df


def _get_spot_at(conn, instrument, ts):
    """Get spot price at a timestamp from spot candle table."""
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT close FROM dhan_spot_candle "
        "WHERE instrument=? AND candle_type='intraday' AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (instrument, lo, hi, ts.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return float(row[0]) if row else None


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
        (instrument, dhan_dir, expiry_flag, strike - 0.5, strike + 0.5, lo, hi,
         ts.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return float(row[0]) if row else None


def _pick_affordable_strike(conn, ts, direction, instrument, expiry_flag,
                            atm, strike_interval, max_premium, lot_size):
    offsets = [0, 1, 2] if direction == "CE" else [0, -1, -2]
    for off in offsets:
        s = atm + off * strike_interval
        prem = _get_premium_at(conn, ts, direction, instrument, expiry_flag, s)
        if prem is not None and prem * lot_size <= max_premium:
            return s, prem
    return None, None


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------

def run_strike_backtest() -> list[dict]:
    sc = get_strategy_config()
    ic = get_instrument_config()
    db_path = get_dhan_db_path()
    dates = get_backtest_dates()
    capital = get_capital()
    max_premium = capital * sc.max_capital_per_trade_pct

    conn = sqlite3.connect(db_path)
    trading_days = _get_trading_days(conn, ic.name, dates[0], dates[1])
    print(f"  {len(trading_days)} trading days from {dates[0]} to {dates[1]}", flush=True)

    trades: list[dict] = []

    def _load_atm_data(strike, load_dates, today_start, today_end):
        """Load CE+PE for a strike, build today's index lists."""
        ce = _load_strike_candles(conn, ic.name, "CE", ic.expiry_flag, strike, load_dates)
        pe = _load_strike_candles(conn, ic.name, "PE", ic.expiry_flag, strike, load_dates)
        # Get today-only indices
        ce_today = []
        pe_today = []
        if ce is not None:
            ce_today = ce.index[
                (ce["timestamp"] >= today_start) & (ce["timestamp"] <= today_end)
            ].tolist()
        if pe is not None:
            pe_today = pe.index[
                (pe["timestamp"] >= today_start) & (pe["timestamp"] <= today_end)
            ].tolist()
        return ce, pe, ce_today, pe_today

    for day_idx, day in enumerate(trading_days):
        if day_idx % 50 == 0:
            print(f"\r  Day {day_idx}/{len(trading_days)} | trades: {len(trades)}", end="", flush=True)

        prev_day = trading_days[day_idx - 1] if day_idx > 0 else None
        load_dates = [prev_day, day] if prev_day else [day]

        today_start = pd.Timestamp(f"{day} 09:15:00")
        today_end = pd.Timestamp(f"{day} 15:30:00")

        # Get spot open → ATM
        spot_row = conn.execute(
            "SELECT close FROM dhan_spot_candle "
            "WHERE instrument=? AND candle_type='intraday' AND interval_min=5 "
            "AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp LIMIT 1",
            (ic.name, f"{day} 09:15:00", f"{day} 09:20:00"),
        ).fetchone()
        if not spot_row:
            continue
        atm_strike = _calc_atm(float(spot_row[0]), ic.strike_interval)

        ce_df, pe_df, ce_today, pe_today = _load_atm_data(
            atm_strike, load_dates, today_start, today_end)
        if not ce_today and not pe_today:
            continue

        # ORB: first 30 min of premium candles (computed per side)
        orb_ce = (None, None)
        orb_pe = (None, None)
        if sc.orb_filter:
            orb_end = _t(9, 30)
            if ce_df is not None:
                orb_candles = ce_df.iloc[ce_today]
                orb_candles = orb_candles[orb_candles["timestamp"].dt.time <= orb_end]
                if len(orb_candles) >= 2:
                    orb_ce = (orb_candles["high"].max(), orb_candles["low"].min())
            if pe_df is not None:
                orb_candles = pe_df.iloc[pe_today]
                orb_candles = orb_candles[orb_candles["timestamp"].dt.time <= orb_end]
                if len(orb_candles) >= 2:
                    orb_pe = (orb_candles["high"].max(), orb_candles["low"].min())

        open_trade = None
        last_exit_pos = -999  # position in today's candle list

        # Walk through today's candles on EACH side independently
        # Build unified timeline from both CE and PE today indices
        timeline = []  # (timestamp, "CE"|"PE", df_index)
        if ce_df is not None:
            for idx in ce_today:
                timeline.append((ce_df.iloc[idx]["timestamp"], "CE", idx))
        if pe_df is not None:
            for idx in pe_today:
                timeline.append((pe_df.iloc[idx]["timestamp"], "PE", idx))
        timeline.sort(key=lambda x: (x[0], x[1]))

        # We need to iterate candle-by-candle matching 15-min timestamps.
        # Group by timestamp so we process CE and PE for the same candle together.
        from itertools import groupby
        candle_groups = []
        for ts, items in groupby(timeline, key=lambda x: x[0]):
            candle_groups.append((ts, list(items)))

        for pos, (ts, items) in enumerate(candle_groups):
            ce_idx = None
            pe_idx = None
            for _, side, idx in items:
                if side == "CE":
                    ce_idx = idx
                else:
                    pe_idx = idx

            # === EXIT ===
            if open_trade is not None:
                candles_held = pos - open_trade["entry_pos"]

                trade_dir = open_trade["dir"]
                ind_df = ce_df if trade_dir == "CE" else pe_df
                df_idx = ce_idx if trade_dir == "CE" else pe_idx

                exit_reason = None
                if df_idx is not None and ind_df is not None:
                    row = ind_df.iloc[df_idx]
                    # Always use "CE" as trade_dir for premium data because
                    # we BOUGHT the option (CE or PE) — we want premium UP.
                    # "CE" logic: exit when ST flips bearish or EMA crosses down,
                    # which is correct for any bought option premium.
                    exit_reason = check_exit(row, "CE", candles_held,
                                            sc.max_hold_candles, sc.ema_gap_floor)

                # Time exit
                if exit_reason is None and ts.time() >= TIME_EXIT:
                    exit_reason = "time_exit"

                if exit_reason:
                    ep = _get_premium_at(conn, ts, trade_dir, ic.name,
                                        ic.expiry_flag, open_trade["bought_strike"])
                    pnl = (ep - open_trade["prem_in"]) * ic.lot_size if ep and open_trade["prem_in"] else None
                    trades.append({
                        "entry": open_trade["entry_ts"], "exit": ts,
                        "dir": trade_dir,
                        "indicator_strike": open_trade["indicator_strike"],
                        "bought_strike": open_trade["bought_strike"],
                        "prem_in": open_trade["prem_in"], "prem_out": ep,
                        "prem_pnl": pnl, "candles": candles_held,
                        "reason": exit_reason,
                        "entry_rsi": open_trade.get("rsi"),
                        "entry_gap": open_trade.get("gap"),
                        "entry_type": open_trade.get("entry_type"),
                    })
                    open_trade = None
                    last_exit_pos = pos

                    # Refresh ATM from current spot
                    current_spot = None
                    for df, di in [(ce_df, ce_idx), (pe_df, pe_idx)]:
                        if df is not None and di is not None:
                            s = df.iloc[di]["spot"]
                            if not pd.isna(s) and s > 0:
                                current_spot = s
                                break
                    if current_spot is None:
                        current_spot = _get_spot_at(conn, ic.name, ts)
                    if current_spot:
                        new_atm = _calc_atm(current_spot, ic.strike_interval)
                        if new_atm != atm_strike:
                            atm_strike = new_atm
                            ce_df, pe_df, ce_today_new, pe_today_new = _load_atm_data(
                                atm_strike, load_dates, today_start, today_end)
                            # Rebuild remaining timeline
                            remaining = []
                            if ce_df is not None:
                                for idx in ce_today_new:
                                    t = ce_df.iloc[idx]["timestamp"]
                                    if t > ts:
                                        remaining.append((t, "CE", idx))
                            if pe_df is not None:
                                for idx in pe_today_new:
                                    t = pe_df.iloc[idx]["timestamp"]
                                    if t > ts:
                                        remaining.append((t, "PE", idx))
                            remaining.sort(key=lambda x: (x[0], x[1]))
                            new_groups = []
                            for gts, gitems in groupby(remaining, key=lambda x: x[0]):
                                new_groups.append((gts, list(gitems)))
                            # Replace remaining candle_groups
                            candle_groups[pos + 1:] = new_groups
                            # Rebuild ORB for new ATM
                            if sc.orb_filter:
                                orb_end_t = _t(9, 30)
                                orb_ce = (None, None)
                                orb_pe = (None, None)
                                if ce_df is not None:
                                    oc = ce_df[ce_df["timestamp"].dt.time <= orb_end_t]
                                    oc = oc[(oc["timestamp"] >= today_start)]
                                    if len(oc) >= 2:
                                        orb_ce = (oc["high"].max(), oc["low"].min())
                                if pe_df is not None:
                                    oc = pe_df[pe_df["timestamp"].dt.time <= orb_end_t]
                                    oc = oc[(oc["timestamp"] >= today_start)]
                                    if len(oc) >= 2:
                                        orb_pe = (oc["high"].max(), oc["low"].min())
                continue

            # === ENTRY ===
            if open_trade is not None:
                continue
            if ts.time() >= TIME_EXIT:
                continue

            # Try CE entry
            direction = None
            entry_type = None
            entry_rsi = None
            entry_gap = None

            # check_entry() returns "CE" for uptrend (EMA9 > EMA21 + ST bullish)
            # and "PE" for downtrend. Since we're always BUYING options, we want
            # UPTREND in the premium = "CE" signal from check_entry().
            # - CE premium uptrend → buy CE
            # - PE premium uptrend → buy PE

            if ce_df is not None and ce_idx is not None and ce_idx > 0:
                row = ce_df.iloc[ce_idx]
                prev = ce_df.iloc[ce_idx - 1]
                orb_h, orb_l = orb_ce if (orb_ce[0] is not None and ts.time() > _t(9, 30)) else (None, None)
                sig = check_entry(row, prev, sc.extra_entry_mode, sc.ema_gap_min,
                                  last_exit_pos, pos, sc.cooldown_candles,
                                  orb_h, orb_l, sc.orb_filter)
                if sig == "CE":  # CE premium uptrend → buy CE
                    direction = "CE"
                    cross_up = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
                    entry_type = "crossover" if cross_up else sc.extra_entry_mode
                    entry_rsi = row["rsi"]
                    entry_gap = row["ema_gap_pct"]

            # Try PE entry (only if CE didn't fire)
            if direction is None and pe_df is not None and pe_idx is not None and pe_idx > 0:
                row = pe_df.iloc[pe_idx]
                prev = pe_df.iloc[pe_idx - 1]
                orb_h, orb_l = orb_pe if (orb_pe[0] is not None and ts.time() > _t(9, 30)) else (None, None)
                sig = check_entry(row, prev, sc.extra_entry_mode, sc.ema_gap_min,
                                  last_exit_pos, pos, sc.cooldown_candles,
                                  orb_h, orb_l, sc.orb_filter)
                if sig == "CE":  # PE premium uptrend → buy PE
                    direction = "PE"
                    cross_up = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
                    entry_type = "crossover" if cross_up else sc.extra_entry_mode
                    entry_rsi = row["rsi"]
                    entry_gap = row["ema_gap_pct"]

            if direction is None:
                continue

            # Get current spot → ATM → affordability
            current_spot = None
            for df, di in [(ce_df, ce_idx), (pe_df, pe_idx)]:
                if df is not None and di is not None:
                    s = df.iloc[di]["spot"]
                    if not pd.isna(s) and s > 0:
                        current_spot = s
                        break
            if current_spot is None:
                current_spot = _get_spot_at(conn, ic.name, ts)
            if current_spot is None:
                continue
            current_atm = _calc_atm(current_spot, ic.strike_interval)

            bought_strike, prem = _pick_affordable_strike(
                conn, ts, direction, ic.name, ic.expiry_flag,
                current_atm, ic.strike_interval, max_premium, ic.lot_size,
            )
            if bought_strike is None:
                continue

            open_trade = {
                "entry_ts": ts, "entry_pos": pos,
                "dir": direction, "bought_strike": bought_strike,
                "prem_in": prem, "indicator_strike": atm_strike,
                "rsi": entry_rsi, "gap": entry_gap, "entry_type": entry_type,
            }

        # Close remaining open trade at day end
        if open_trade is not None and candle_groups:
            last_ts = candle_groups[-1][0]
            ep = _get_premium_at(conn, last_ts, open_trade["dir"], ic.name,
                                ic.expiry_flag, open_trade["bought_strike"])
            candles_held = len(candle_groups) - 1 - open_trade["entry_pos"]
            pnl = (ep - open_trade["prem_in"]) * ic.lot_size if ep and open_trade["prem_in"] else None
            trades.append({
                "entry": open_trade["entry_ts"], "exit": last_ts,
                "dir": open_trade["dir"],
                "indicator_strike": open_trade["indicator_strike"],
                "bought_strike": open_trade["bought_strike"],
                "prem_in": open_trade["prem_in"], "prem_out": ep,
                "prem_pnl": pnl, "candles": candles_held,
                "reason": "day_end",
                "entry_rsi": open_trade.get("rsi"),
                "entry_gap": open_trade.get("gap"),
                "entry_type": open_trade.get("entry_type"),
            })

    print(f"\r  Done. {len(trades)} trades across {len(trading_days)} days.                ", flush=True)
    conn.close()
    return trades


# ---------------------------------------------------------------------------
# Comparison report
# ---------------------------------------------------------------------------

def print_comparison(strike_trades: list[dict]) -> None:
    from backtester import run_backtest
    from results import compute_stats

    baseline_trades = run_backtest()
    bs = compute_stats(baseline_trades)

    valid = [t for t in strike_trades if t["prem_pnl"] is not None]
    n = len(valid)
    if n == 0:
        print("  No valid trades from strike-driven approach.")
        return

    winners = sum(1 for t in valid if t["prem_pnl"] > 0)
    total_pnl = sum(t["prem_pnl"] for t in valid)
    gw = sum(t["prem_pnl"] for t in valid if t["prem_pnl"] > 0)
    gl = abs(sum(t["prem_pnl"] for t in valid if t["prem_pnl"] <= 0))
    pf = gw / gl if gl else float("inf")
    avg_win = gw / winners if winners else 0
    avg_loss = gl / (n - winners) if (n - winners) else 0

    eq = pk = dd = 0.0
    streak = mx_streak = 0
    for t in valid:
        eq += t["prem_pnl"]
        if eq > pk: pk = eq
        if pk - eq > dd: dd = pk - eq
        if t["prem_pnl"] <= 0:
            streak += 1
            mx_streak = max(mx_streak, streak)
        else:
            streak = 0

    monthly: dict[str, float] = {}
    for t in valid:
        m = pd.Timestamp(t["entry"]).strftime("%Y-%m")
        monthly[m] = monthly.get(m, 0) + t["prem_pnl"]
    green = sum(1 for v in monthly.values() if v >= 0)
    total_months = len(monthly)

    yearly: dict[int, dict] = {}
    for t in valid:
        y = pd.Timestamp(t["entry"]).year
        if y not in yearly: yearly[y] = {"pnl": 0, "n": 0, "w": 0}
        yearly[y]["pnl"] += t["prem_pnl"]
        yearly[y]["n"] += 1
        if t["prem_pnl"] > 0: yearly[y]["w"] += 1

    reasons: dict[str, dict] = {}
    for t in valid:
        r = t["reason"]
        if r not in reasons: reasons[r] = {"n": 0, "pnl": 0, "w": 0}
        reasons[r]["n"] += 1
        reasons[r]["pnl"] += t["prem_pnl"]
        if t["prem_pnl"] > 0: reasons[r]["w"] += 1

    entry_types: dict[str, dict] = {}
    for t in valid:
        et = t.get("entry_type", "crossover")
        if et not in entry_types: entry_types[et] = {"n": 0, "pnl": 0, "w": 0}
        entry_types[et]["n"] += 1
        entry_types[et]["pnl"] += t["prem_pnl"]
        if t["prem_pnl"] > 0: entry_types[et]["w"] += 1

    dir_stats: dict[str, dict] = {}
    for d in ("CE", "PE"):
        dt = [t for t in valid if t["dir"] == d]
        if dt:
            dpnl = sum(t["prem_pnl"] for t in dt)
            dw = sum(1 for t in dt if t["prem_pnl"] > 0)
            dir_stats[d] = {"n": len(dt), "pnl": dpnl, "w": dw}

    # --- Print ---
    bs_w_str = f"{bs['winners']}/{bs['n']}"
    st_w_str = f"{winners}/{n}"
    bs_pnl_str = f"Rs.{bs['pnl']:>+,.0f}"
    st_pnl_str = f"Rs.{total_pnl:>+,.0f}"
    bs_gm = bs['green_months']
    bs_rm = bs['red_months']

    print(f"\n{'=' * 90}")
    print(f"  SPOT (current)  vs  PREMIUM-DRIVEN (same rules, premium data)")
    print(f"{'=' * 90}")
    print(f"  {'':30} {'SPOT (current)':>18}  {'PREMIUM-DRIVEN':>18}")
    print(f"  {'Trades:':30} {bs['n']:>18}  {n:>18}")
    print(f"  {'Winners:':30} {bs_w_str:>18}  {st_w_str:>18}")
    print(f"  {'Win Rate:':30} {bs['wr']:>17.0f}%  {winners/n*100:>17.0f}%")
    print(f"  {'Net P&L:':30} {bs_pnl_str:>18}  {st_pnl_str:>18}")
    print(f"  {'Profit Factor:':30} {bs['pf']:>18.2f}  {pf:>18.2f}")
    bs_aw = f"Rs.{bs['avg_win']:>,.0f}"
    st_aw = f"Rs.{avg_win:>,.0f}"
    bs_al = f"Rs.{bs['avg_loss']:>,.0f}"
    st_al = f"Rs.{avg_loss:>,.0f}"
    bs_dd = f"Rs.{bs['max_dd']:>,.0f}"
    st_dd = f"Rs.{dd:>,.0f}"
    bs_gm_str = f"{bs_gm}/{bs_gm+bs_rm}"
    st_gm_str = f"{green}/{total_months}"
    print(f"  {'Avg Win:':30} {bs_aw:>18}  {st_aw:>18}")
    print(f"  {'Avg Loss:':30} {bs_al:>18}  {st_al:>18}")
    print(f"  {'Max Drawdown:':30} {bs_dd:>18}  {st_dd:>18}")
    print(f"  {'Max Loss Streak:':30} {bs['max_streak']:>18}  {mx_streak:>18}")
    print(f"  {'Green Months:':30} {bs_gm_str:>18}  {st_gm_str:>18}")

    print(f"\n  Yearly:")
    print(f"  {'Year':>6}  {'Spot P&L':>12} {'WR':>5}  {'Prem P&L':>14} {'WR':>5}  {'Diff':>12}")
    for y in sorted(set(list(bs.get("yearly", {})) + list(yearly))):
        sp = bs.get("yearly", {}).get(y, {"pnl": 0, "n": 1, "w": 0})
        st = yearly.get(y, {"pnl": 0, "n": 1, "w": 0})
        swr = sp["w"]/sp["n"]*100 if sp["n"] else 0
        stwr = st["w"]/st["n"]*100 if st["n"] else 0
        print(f"  {y:>6}  Rs.{sp['pnl']:>+10,.0f} {swr:>4.0f}%  Rs.{st['pnl']:>+12,.0f} {stwr:>4.0f}%  Rs.{st['pnl']-sp['pnl']:>+10,.0f}")

    print(f"\n  Exit reasons:")
    for r in sorted(reasons):
        d = reasons[r]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        print(f"    {r:>14}: {d['n']:>3} trades, WR {wr:.0f}%, P&L: Rs.{d['pnl']:>+10,.0f}")

    if entry_types:
        print(f"\n  Entry types:")
        for et in sorted(entry_types):
            d = entry_types[et]
            wr = d["w"] / d["n"] * 100 if d["n"] else 0
            print(f"    {et:>14}: {d['n']:>3} trades, WR {wr:.0f}%, P&L: Rs.{d['pnl']:>+10,.0f}")

    print(f"\n  Direction:")
    for d_name in ("CE", "PE"):
        if d_name in dir_stats:
            d = dir_stats[d_name]
            wr_pct = d['w'] / d['n'] * 100 if d['n'] else 0
            print(f"    {d_name}: {d['n']} trades, Rs.{d['pnl']:>+9,.0f}, WR {d['w']}/{d['n']} ({wr_pct:.0f}%)")

    print()


if __name__ == "__main__":
    trades = run_strike_backtest()
    print_comparison(trades)
