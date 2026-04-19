"""entry_analysis.py — Deep research on entry quality with cached data.

Loads spot candles and premium data ONCE, caches everything in memory,
then runs all parameter sweeps without touching the DB again.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import time as _t, timedelta

import numpy as np
import pandas as pd

from config import StrategyConfig, get_strategy_config, get_instrument_config, get_dhan_db_path, get_backtest_dates
from indicators import compute_supertrend
from strategy import check_entry, check_exit


# ---------------------------------------------------------------------------
# Cached data loading
# ---------------------------------------------------------------------------

class CachedData:
    """Load all data once, cache premium lookups."""

    def __init__(self):
        self.ic = get_instrument_config()
        self.db_path = get_dhan_db_path()
        self.dates = get_backtest_dates()
        self.conn = sqlite3.connect(self.db_path)

        # Load spot candles (raw, without indicators — we'll compute per sweep)
        print("  Loading spot candles...", flush=True)
        self.spot_raw = pd.read_sql_query(
            "SELECT timestamp, open, high, low, close FROM dhan_spot_candle "
            "WHERE instrument=? AND candle_type='intraday' AND interval_min=? ORDER BY timestamp",
            self.conn, params=(self.ic.name, 15),
        )
        self.spot_raw["timestamp"] = pd.to_datetime(self.spot_raw["timestamp"])
        print(f"  {len(self.spot_raw)} spot candles loaded.", flush=True)

        # Premium cache: (ts_str, direction, strike_or_atm) → (premium, strike)
        self._prem_cache: dict = {}

        # Pre-load ALL ATM premium data into memory for fast lookup
        print("  Pre-loading ATM premium data...", flush=True)
        atm_data = pd.read_sql_query(
            "SELECT timestamp, close, strike, direction FROM dhan_option_candle "
            "WHERE instrument=? AND strike_offset='ATM' AND interval_min=5 AND expiry_flag=?",
            self.conn, params=(self.ic.name, self.ic.expiry_flag),
        )
        atm_data["timestamp"] = pd.to_datetime(atm_data["timestamp"])
        print(f"  {len(atm_data)} ATM premium rows loaded.", flush=True)

        # Index by (direction, timestamp) for fast nearest-lookup
        self._atm_ce = atm_data[atm_data["direction"] == "CALL"].sort_values("timestamp").reset_index(drop=True)
        self._atm_pe = atm_data[atm_data["direction"] == "PUT"].sort_values("timestamp").reset_index(drop=True)

        # Build timestamp index for binary search
        self._atm_ce_ts = self._atm_ce["timestamp"].values
        self._atm_pe_ts = self._atm_pe["timestamp"].values

    def get_atm_premium(self, ts, direction):
        """Get ATM premium nearest to timestamp. Returns (premium, strike) or (None, None)."""
        cache_key = (ts.isoformat(), direction, "ATM")
        if cache_key in self._prem_cache:
            return self._prem_cache[cache_key]

        df_ts = self._atm_ce_ts if direction == "CE" else self._atm_pe_ts
        df = self._atm_ce if direction == "CE" else self._atm_pe

        if len(df) == 0:
            self._prem_cache[cache_key] = (None, None)
            return None, None

        ts_np = np.datetime64(ts)
        idx = np.searchsorted(df_ts, ts_np)
        # Check nearest (idx-1 and idx)
        best = None
        best_dist = timedelta(minutes=11)
        for j in [idx - 1, idx]:
            if 0 <= j < len(df):
                row_ts = pd.Timestamp(df_ts[j])
                dist = abs(ts - row_ts)
                if dist < best_dist:
                    best_dist = dist
                    best = j
        if best is not None and best_dist <= timedelta(minutes=10):
            result = (float(df.iloc[best]["close"]), float(df.iloc[best]["strike"]))
        else:
            result = (None, None)

        self._prem_cache[cache_key] = result
        return result

    def get_premium_by_strike(self, ts, direction, strike):
        """Get premium for specific strike nearest to timestamp."""
        cache_key = (ts.isoformat(), direction, strike)
        if cache_key in self._prem_cache:
            return self._prem_cache[cache_key]

        lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        dhan_dir = "CALL" if direction == "CE" else "PUT"
        row = self.conn.execute(
            "SELECT close FROM dhan_option_candle "
            "WHERE instrument=? AND direction=? AND expiry_flag=? "
            "AND strike BETWEEN ? AND ? AND interval_min=5 "
            "AND timestamp>=? AND timestamp<=? "
            "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
            (self.ic.name, dhan_dir, self.ic.expiry_flag, strike - 0.5, strike + 0.5,
             lo, hi, ts.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
        result = float(row[0]) if row else None
        self._prem_cache[cache_key] = result
        return result


def compute_indicators_custom(df, ema_short=9, ema_long=21, st_period=10, st_mult=3.0):
    """Compute indicators with configurable params on a copy."""
    df = df.copy()
    df["ema9"] = df["close"].ewm(span=ema_short, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=ema_long, adjust=False).mean()
    df["st"], df["st_dir"] = compute_supertrend(df, period=st_period, multiplier=st_mult)
    df["ema_gap_pct"] = (df["ema9"] - df["ema21"]).abs() / df["ema21"].replace(0, np.nan) * 100
    df["ema_gap_pct"] = df["ema_gap_pct"].fillna(0)
    df["ema_gap_expanding"] = df["ema_gap_pct"] > df["ema_gap_pct"].shift(1)
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss_s = delta.where(delta < 0, 0).abs().rolling(14).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gain / loss_s
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)
    return df


def _pick_expiry_code(entry_ts, expiry_weekday):
    return 2 if pd.Timestamp(entry_ts).weekday() == expiry_weekday else 1


# ---------------------------------------------------------------------------
# Fast backtest using cached data
# ---------------------------------------------------------------------------

def run_cached_backtest(cache: CachedData, df: pd.DataFrame, sc: StrategyConfig) -> list[dict]:
    """Run backtest on pre-computed indicator DataFrame with cached premium lookups."""
    ic = cache.ic
    start_ts = pd.Timestamp(cache.dates[0])
    end_ts = pd.Timestamp(cache.dates[1])
    if end_ts == end_ts.normalize():
        end_ts += pd.Timedelta(hours=23, minutes=59, seconds=59)
    indices = df.index[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].tolist()

    orb_ranges = {}
    if sc.orb_filter:
        df_tmp = df.copy()
        df_tmp["_date"] = df_tmp["timestamp"].dt.date
        df_tmp["_time"] = df_tmp["timestamp"].dt.time
        for d, grp in df_tmp.groupby("_date"):
            first_30 = grp[(grp["_time"] >= _t(9, 15)) & (grp["_time"] <= _t(9, 30))]
            if len(first_30) >= 2:
                orb_ranges[d] = (first_30["high"].max(), first_30["low"].min())

    trades = []
    open_trade = None
    last_exit_idx = -999

    for i in indices:
        if i == 0:
            continue
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        ts = row["timestamp"]

        if open_trade is not None:
            candles_held = i - open_trade["idx"]
            reason = check_exit(row, open_trade["dir"], candles_held, sc.max_hold_candles, sc.ema_gap_floor)
            if reason:
                ep = None
                if open_trade["strike"]:
                    ep = cache.get_premium_by_strike(ts, open_trade["dir"], open_trade["strike"])
                if ep is None:
                    ep, _ = cache.get_atm_premium(ts, open_trade["dir"])
                pp = (ep - open_trade["prem_in"]) * ic.lot_size if ep and open_trade["prem_in"] else None
                trades.append({
                    "entry": open_trade["ts"], "exit": ts,
                    "dir": open_trade["dir"], "strike": open_trade["strike"],
                    "prem_in": open_trade["prem_in"], "prem_out": ep,
                    "prem_pnl": pp, "candles": candles_held, "reason": reason,
                    "entry_rsi": open_trade.get("rsi"), "entry_gap": open_trade.get("gap"),
                    "entry_type": open_trade.get("entry_type"),
                })
                open_trade = None
                last_exit_idx = i

        if open_trade is not None:
            continue

        orb = orb_ranges.get(ts.date())
        orb_h, orb_l = orb if (orb and ts.time() > _t(9, 30)) else (None, None)
        direction = check_entry(row, prev, sc.extra_entry_mode, sc.ema_gap_min,
                                last_exit_idx, i, sc.cooldown_candles, orb_h, orb_l, sc.orb_filter)
        if direction is None:
            continue

        crossover_up = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
        crossover_down = prev["ema9"] >= prev["ema21"] and row["ema9"] < row["ema21"]
        is_cross = (direction == "CE" and crossover_up) or (direction == "PE" and crossover_down)

        prem, strike = cache.get_atm_premium(ts, direction)
        open_trade = {
            "ts": ts, "idx": i, "dir": direction,
            "prem_in": prem, "strike": strike,
            "rsi": row["rsi"], "gap": row["ema_gap_pct"],
            "entry_type": "crossover" if is_cross else sc.extra_entry_mode,
        }

    # Close remaining
    if open_trade is not None and indices:
        last = df.iloc[indices[-1]]
        ep = cache.get_premium_by_strike(last["timestamp"], open_trade["dir"], open_trade["strike"]) if open_trade["strike"] else None
        if ep is None:
            ep, _ = cache.get_atm_premium(last["timestamp"], open_trade["dir"])
        pp = (ep - open_trade["prem_in"]) * ic.lot_size if ep and open_trade["prem_in"] else None
        trades.append({
            "entry": open_trade["ts"], "exit": last["timestamp"],
            "dir": open_trade["dir"], "strike": open_trade["strike"],
            "prem_in": open_trade["prem_in"], "prem_out": ep,
            "prem_pnl": pp, "candles": 0, "reason": "end",
            "entry_rsi": open_trade.get("rsi"), "entry_gap": open_trade.get("gap"),
            "entry_type": open_trade.get("entry_type"),
        })

    return trades


def quick_stats(trades):
    valid = [t for t in trades if t.get("prem_pnl") is not None]
    n = len(valid)
    if n == 0:
        return 0, 0, 0, 0, 0
    w = sum(1 for t in valid if t["prem_pnl"] > 0)
    pnl = sum(t["prem_pnl"] for t in valid)
    gw = sum(t["prem_pnl"] for t in valid if t["prem_pnl"] > 0)
    gl = abs(sum(t["prem_pnl"] for t in valid if t["prem_pnl"] <= 0))
    pf = gw / gl if gl else float("inf")
    return n, w, w / n * 100 if n else 0, pnl, pf


# ---------------------------------------------------------------------------
# Part 1: Pattern analysis
# ---------------------------------------------------------------------------

def part1_patterns(cache, df_default, sc):
    trades = run_cached_backtest(cache, df_default, sc)
    valid = [t for t in trades if t["prem_pnl"] is not None]
    df = pd.DataFrame(valid)
    df["entry"] = pd.to_datetime(df["entry"])
    df["win"] = df["prem_pnl"] > 0
    df["entry_hour"] = df["entry"].dt.hour
    df["entry_dow"] = df["entry"].dt.dayofweek
    df["entry_date"] = df["entry"].dt.date
    df["entry_month"] = df["entry"].dt.month
    df["rsi_dist"] = df["entry_rsi"].apply(lambda x: abs(x - 50) if pd.notna(x) else None)

    n = len(df)
    w = df["win"].sum()
    total_pnl = df["prem_pnl"].sum()

    print(f"\n{'=' * 90}")
    print(f"  PART 1: TRADE PATTERNS — {n} trades, {w} winners ({w/n*100:.0f}%), Rs.{total_pnl:+,.0f}")
    print(f"{'=' * 90}")

    def _pb(title, groups):
        print(f"\n  {title}")
        print(f"  {'Label':>16} {'Trades':>7} {'WR':>6} {'P&L':>12} {'Avg P&L':>10} {'PF':>6}")
        for label, sub in groups:
            if len(sub) == 0:
                continue
            sw = sub["win"].sum()
            spnl = sub["prem_pnl"].sum()
            gw = sub[sub["prem_pnl"] > 0]["prem_pnl"].sum()
            gl = abs(sub[sub["prem_pnl"] <= 0]["prem_pnl"].sum())
            pf = gw / gl if gl else float("inf")
            print(f"  {label:>16} {len(sub):>7} {sw/len(sub)*100:>5.0f}% {spnl:>+11,.0f} {spnl/len(sub):>+10,.0f} {pf:>6.2f}")

    _pb("ENTRY TYPE", [(et, df[df["entry_type"] == et]) for et in sorted(df["entry_type"].unique())])
    _pb("DIRECTION", [(d, df[df["dir"] == d]) for d in ("CE", "PE")])

    groups = []
    for h in range(9, 16):
        for m in (0, 30):
            sub = df[(df["entry_hour"] == h) & ((df["entry"].dt.minute >= m) & (df["entry"].dt.minute < m + 30))]
            if len(sub) >= 5:
                groups.append((f"{h:02d}:{m:02d}", sub))
    _pb("TIME OF DAY (30-min)", groups)

    names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    _pb("DAY OF WEEK", [(names[d], df[df["entry_dow"] == d]) for d in range(5)])

    gap_bins = [(0.03, 0.05), (0.05, 0.08), (0.08, 0.12), (0.12, 0.20), (0.20, 0.35), (0.35, 0.50)]
    _pb("EMA GAP AT ENTRY", [(f"{lo:.2f}-{hi:.2f}%", df[(df["entry_gap"] >= lo) & (df["entry_gap"] < hi)]) for lo, hi in gap_bins])

    rsi_bins = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 30), (30, 50)]
    _pb("RSI STRENGTH (|RSI-50|)", [(f"{lo}-{hi}", df[(df["rsi_dist"] >= lo) & (df["rsi_dist"] < hi)]) for lo, hi in rsi_bins])

    prem_bins = [(0, 50), (50, 100), (100, 150), (150, 200), (200, 300), (300, 500)]
    _pb("PREMIUM AT ENTRY", [(f"Rs.{lo}-{hi}", df[(df["prem_in"] >= lo) & (df["prem_in"] < hi)]) for lo, hi in prem_bins])

    _pb("EXIT REASON", [(r, df[df["reason"] == r]) for r in sorted(df["reason"].unique())])

    hold_bins = [(1, 5), (5, 10), (10, 15), (15, 20), (20, 21)]
    _pb("CANDLES HELD", [(f"{lo}-{hi-1}", df[(df["candles"] >= lo) & (df["candles"] < hi)]) for lo, hi in hold_bins])

    tpd = df.groupby("entry_date").size()
    _pb("TRADES PER DAY", [(f"{cnt}/day", df[df["entry_date"].isin(tpd[tpd == cnt].index)]) for cnt in sorted(tpd.unique())])

    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    _pb("MONTH", [(month_names[m - 1], df[df["entry_month"] == m]) for m in range(1, 13)])


# ---------------------------------------------------------------------------
# Part 2: Parameter sweeps
# ---------------------------------------------------------------------------

def part2_sweeps(cache, sc):
    def _print_sweep(title, results):
        print(f"\n  {title}")
        print(f"  {'Value':>20} {'Trades':>7} {'WR':>6} {'P&L':>12} {'PF':>6} {'Note':>8}")
        best_pnl = max(r[4] for r in results) if results else 0
        best_pf = max(r[5] for r in results if r[5] < float("inf")) if results else 0
        for label, current, n, wr, pnl, pf in results:
            note = " <curr" if current else ""
            if pnl == best_pnl:
                note += " *PNL"
            if pf == best_pf and pf < float("inf"):
                note += " *PF"
            print(f"  {label:>20} {n:>7} {wr:>5.0f}% {pnl:>+11,.0f} {pf:>6.2f}{note}")

    print(f"\n{'=' * 90}")
    print(f"  PART 2: PARAMETER SWEEPS (cached — fast)")
    print(f"{'=' * 90}")

    # Default indicators (9/21 EMA, 10/3.0 ST)
    df_default = compute_indicators_custom(cache.spot_raw)

    # --- Config sweeps (reuse df_default) ---
    for sweep_name, param, values, current_val in [
        ("EMA GAP MINIMUM", "ema_gap_min", [0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.08, 0.10], sc.ema_gap_min),
        ("ENTRY MODE", "extra_entry_mode", ["none", "midtrend", "expanding"], sc.extra_entry_mode),
        ("ORB FILTER", "orb_filter", [True, False], sc.orb_filter),
        ("COOLDOWN CANDLES", "cooldown_candles", [0, 1, 2, 3, 5, 7], sc.cooldown_candles),
        ("MAX HOLD CANDLES", "max_hold_candles", [8, 10, 12, 15, 18, 20, 25, 30], sc.max_hold_candles),
        ("EMA GAP FLOOR (exit)", "ema_gap_floor", [0, 0.02, 0.05, 0.08, 0.10, 0.12, 0.15], sc.ema_gap_floor),
    ]:
        results = []
        for val in values:
            cfg = replace(sc, **{param: val})
            trades = run_cached_backtest(cache, df_default, cfg)
            n, w, wr, pnl, pf = quick_stats(trades)
            label = f"{param}={val}"
            results.append((label, val == current_val, n, wr, pnl, pf))
            print(f"\r    {sweep_name}: {label}...", end="", flush=True)
        print(f"\r{' ' * 60}\r", end="", flush=True)
        _print_sweep(sweep_name, results)

    # --- EMA span sweeps (recompute indicators) ---
    print(f"\n  EMA SPANS (recomputing indicators)")
    print(f"  {'Spans':>14} {'Trades':>7} {'WR':>6} {'P&L':>12} {'PF':>6}")
    for short, long in [(5, 13), (5, 21), (8, 21), (9, 21), (9, 26), (13, 21), (13, 34)]:
        df_custom = compute_indicators_custom(cache.spot_raw, ema_short=short, ema_long=long)
        trades = run_cached_backtest(cache, df_custom, sc)
        n, w, wr, pnl, pf = quick_stats(trades)
        curr = " <curr" if (short == 9 and long == 21) else ""
        print(f"  {f'{short}/{long}':>14} {n:>7} {wr:>5.0f}% {pnl:>+11,.0f} {pf:>6.2f}{curr}")

    # --- SuperTrend param sweeps ---
    print(f"\n  SUPERTREND PARAMS (period/multiplier)")
    print(f"  {'Params':>14} {'Trades':>7} {'WR':>6} {'P&L':>12} {'PF':>6}")
    for period, mult in [(7, 2.0), (7, 3.0), (10, 2.0), (10, 3.0), (10, 4.0), (14, 3.0), (14, 4.0)]:
        df_custom = compute_indicators_custom(cache.spot_raw, st_period=period, st_mult=mult)
        trades = run_cached_backtest(cache, df_custom, sc)
        n, w, wr, pnl, pf = quick_stats(trades)
        curr = " <curr" if (period == 10 and mult == 3.0) else ""
        print(f"  {f'{period}/{mult:.1f}':>14} {n:>7} {wr:>5.0f}% {pnl:>+11,.0f} {pf:>6.2f}{curr}")

    print()


# ---------------------------------------------------------------------------
# Part 3: Hardcoded param sweeps (RSI cutoff, EMA gap max, ORB window)
# ---------------------------------------------------------------------------

def _check_entry_custom(row, prev, extra_mode, gap_min, last_exit_idx, current_idx,
                        cooldown, orb_high, orb_low, orb_enabled,
                        rsi_cutoff=50, gap_max=0.5):
    """check_entry with configurable RSI cutoff and EMA gap max."""
    direction = None
    crossover_up = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
    crossover_down = prev["ema9"] >= prev["ema21"] and row["ema9"] < row["ema21"]
    if crossover_up and row["st_dir"] == 1:
        direction = "CE"
    elif crossover_down and row["st_dir"] == -1:
        direction = "PE"
    if direction is None and extra_mode != "none":
        if current_idx - last_exit_idx < cooldown:
            return None
        if extra_mode == "expanding" and not row["ema_gap_expanding"]:
            return None
        if row["ema9"] > row["ema21"] and row["st_dir"] == 1:
            direction = "CE"
        elif row["ema9"] < row["ema21"] and row["st_dir"] == -1:
            direction = "PE"
    if direction is None:
        return None
    # RSI with configurable cutoff
    if direction == "CE" and (pd.isna(row["rsi"]) or row["rsi"] <= rsi_cutoff):
        return None
    if direction == "PE" and (pd.isna(row["rsi"]) or row["rsi"] >= (100 - rsi_cutoff)):
        return None
    # EMA gap min
    if gap_min > 0 and row["ema_gap_pct"] < gap_min:
        return None
    # EMA gap max (configurable)
    if gap_max > 0 and row["ema_gap_pct"] > gap_max:
        return None
    # ORB
    if orb_enabled:
        if orb_high is None or orb_low is None:
            return None
        if direction == "CE" and row["close"] < orb_high:
            return None
        if direction == "PE" and row["close"] > orb_low:
            return None
    return direction


def run_custom_backtest(cache, df, sc, rsi_cutoff=50, gap_max=0.5, orb_window_min=30):
    """Backtest with custom RSI cutoff, gap max, and ORB window."""
    ic = cache.ic
    start_ts = pd.Timestamp(cache.dates[0])
    end_ts = pd.Timestamp(cache.dates[1])
    if end_ts == end_ts.normalize():
        end_ts += pd.Timedelta(hours=23, minutes=59, seconds=59)
    indices = df.index[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].tolist()

    orb_ranges = {}
    if sc.orb_filter:
        df_tmp = df.copy()
        df_tmp["_date"] = df_tmp["timestamp"].dt.date
        df_tmp["_time"] = df_tmp["timestamp"].dt.time
        orb_end = _t(9, 15 + orb_window_min) if (15 + orb_window_min) < 60 else _t(10, (15 + orb_window_min) - 60)
        for d, grp in df_tmp.groupby("_date"):
            first_n = grp[(grp["_time"] >= _t(9, 15)) & (grp["_time"] <= orb_end)]
            if len(first_n) >= 2:
                orb_ranges[d] = (first_n["high"].max(), first_n["low"].min())

    # Determine ORB entry time threshold
    orb_entry_after = _t(9, 15 + orb_window_min) if (15 + orb_window_min) < 60 else _t(10, (15 + orb_window_min) - 60)

    trades = []
    open_trade = None
    last_exit_idx = -999

    for i in indices:
        if i == 0:
            continue
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        ts = row["timestamp"]

        if open_trade is not None:
            candles_held = i - open_trade["idx"]
            reason = check_exit(row, open_trade["dir"], candles_held, sc.max_hold_candles, sc.ema_gap_floor)
            if reason:
                ep = None
                if open_trade["strike"]:
                    ep = cache.get_premium_by_strike(ts, open_trade["dir"], open_trade["strike"])
                if ep is None:
                    ep, _ = cache.get_atm_premium(ts, open_trade["dir"])
                pp = (ep - open_trade["prem_in"]) * ic.lot_size if ep and open_trade["prem_in"] else None
                trades.append({"prem_pnl": pp})
                open_trade = None
                last_exit_idx = i

        if open_trade is not None:
            continue

        orb = orb_ranges.get(ts.date())
        orb_h, orb_l = orb if (orb and ts.time() > orb_entry_after) else (None, None)
        direction = _check_entry_custom(row, prev, sc.extra_entry_mode, sc.ema_gap_min,
                                        last_exit_idx, i, sc.cooldown_candles,
                                        orb_h, orb_l, sc.orb_filter,
                                        rsi_cutoff=rsi_cutoff, gap_max=gap_max)
        if direction is None:
            continue

        prem, strike = cache.get_atm_premium(ts, direction)
        open_trade = {"ts": ts, "idx": i, "dir": direction, "prem_in": prem, "strike": strike}

    if open_trade is not None and indices:
        last = df.iloc[indices[-1]]
        ep = cache.get_premium_by_strike(last["timestamp"], open_trade["dir"], open_trade["strike"]) if open_trade["strike"] else None
        if ep is None:
            ep, _ = cache.get_atm_premium(last["timestamp"], open_trade["dir"])
        pp = (ep - open_trade["prem_in"]) * ic.lot_size if ep and open_trade["prem_in"] else None
        trades.append({"prem_pnl": pp})

    return trades


def part3_missing_sweeps(cache, sc):
    print(f"\n{'=' * 90}")
    print(f"  PART 3: MISSING PARAMETER SWEEPS")
    print(f"{'=' * 90}")

    df_default = compute_indicators_custom(cache.spot_raw)

    def _ps(title, results):
        print(f"\n  {title}")
        print(f"  {'Value':>20} {'Trades':>7} {'WR':>6} {'P&L':>12} {'PF':>6} {'Note':>8}")
        best_pnl = max(r[4] for r in results) if results else 0
        best_pf = max(r[5] for r in results if r[5] < float("inf")) if results else 0
        for label, current, n, wr, pnl, pf in results:
            note = " <curr" if current else ""
            if pnl == best_pnl: note += " *PNL"
            if pf == best_pf and pf < float("inf"): note += " *PF"
            print(f"  {label:>20} {n:>7} {wr:>5.0f}% {pnl:>+11,.0f} {pf:>6.2f}{note}")

    # RSI CUTOFF
    results = []
    for rsi in [40, 45, 48, 50, 52, 55, 60]:
        trades = run_custom_backtest(cache, df_default, sc, rsi_cutoff=rsi)
        n, w, wr, pnl, pf = quick_stats(trades)
        results.append((f"rsi_cutoff={rsi}", rsi == 50, n, wr, pnl, pf))
        print(f"\r    RSI CUTOFF: rsi={rsi}...", end="", flush=True)
    print(f"\r{' ' * 60}\r", end="", flush=True)
    _ps("RSI CUTOFF (CE: rsi>X, PE: rsi<100-X)", results)

    # EMA GAP MAX
    results = []
    for gmax in [0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 2.0, 0]:
        trades = run_custom_backtest(cache, df_default, sc, gap_max=gmax)
        n, w, wr, pnl, pf = quick_stats(trades)
        label = f"gap_max={gmax:.1f}%" if gmax > 0 else "gap_max=none"
        results.append((label, gmax == 0.5, n, wr, pnl, pf))
        print(f"\r    EMA GAP MAX: {label}...", end="", flush=True)
    print(f"\r{' ' * 60}\r", end="", flush=True)
    _ps("EMA GAP MAXIMUM", results)

    # ORB WINDOW
    results = []
    for window in [15, 20, 30, 45, 60]:
        trades = run_custom_backtest(cache, df_default, sc, orb_window_min=window)
        n, w, wr, pnl, pf = quick_stats(trades)
        results.append((f"orb_window={window}min", window == 30, n, wr, pnl, pf))
        print(f"\r    ORB WINDOW: {window}min...", end="", flush=True)
    print(f"\r{' ' * 60}\r", end="", flush=True)
    _ps("ORB WINDOW (minutes)", results)

    # --- COMBINED OPTIMAL TEST ---
    print(f"\n{'=' * 90}")
    print(f"  COMBINED OPTIMAL TEST")
    print(f"{'=' * 90}")
    print(f"\n  Testing best params from all sweeps combined:")

    # Current
    trades_curr = run_cached_backtest(cache, df_default, sc)
    n, w, wr, pnl, pf = quick_stats(trades_curr)
    print(f"\n  CURRENT:  gap_min=0.03, cooldown=3, max_hold=20, gap_floor=0, EMA 9/21, ST 10/3.0")
    print(f"            {n} trades, WR {wr:.0f}%, P&L Rs.{pnl:+,.0f}, PF {pf:.2f}")

    # Optimal config sweep params
    cfg_opt = replace(sc, ema_gap_min=0, cooldown_candles=2, max_hold_candles=15, ema_gap_floor=0.10)
    trades_opt = run_cached_backtest(cache, df_default, cfg_opt)
    n, w, wr, pnl, pf = quick_stats(trades_opt)
    print(f"\n  OPTIMAL CONFIG: gap_min=0, cooldown=2, max_hold=15, gap_floor=0.10")
    print(f"            {n} trades, WR {wr:.0f}%, P&L Rs.{pnl:+,.0f}, PF {pf:.2f}")

    # Optimal config + optimal indicators
    df_opt = compute_indicators_custom(cache.spot_raw, ema_short=5, ema_long=13, st_period=10, st_mult=2.0)
    trades_opt2 = run_cached_backtest(cache, df_opt, cfg_opt)
    n, w, wr, pnl, pf = quick_stats(trades_opt2)
    print(f"\n  OPTIMAL CONFIG + EMA 5/13 + ST 10/2.0:")
    print(f"            {n} trades, WR {wr:.0f}%, P&L Rs.{pnl:+,.0f}, PF {pf:.2f}")

    # Optimal config + best indicators from sweep
    df_opt3 = compute_indicators_custom(cache.spot_raw, ema_short=5, ema_long=21, st_period=10, st_mult=2.0)
    trades_opt3 = run_cached_backtest(cache, df_opt3, cfg_opt)
    n, w, wr, pnl, pf = quick_stats(trades_opt3)
    print(f"\n  OPTIMAL CONFIG + EMA 5/21 + ST 10/2.0:")
    print(f"            {n} trades, WR {wr:.0f}%, P&L Rs.{pnl:+,.0f}, PF {pf:.2f}")

    print()


if __name__ == "__main__":
    cache = CachedData()
    sc = get_strategy_config()
    df_default = compute_indicators_custom(cache.spot_raw)

    part1_patterns(cache, df_default, sc)
    part2_sweeps(cache, sc)
    part3_missing_sweeps(cache, sc)
