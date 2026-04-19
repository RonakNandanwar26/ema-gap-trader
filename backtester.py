"""backtester.py — Run strategy on historical Dhan option data."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pandas as pd

from config import StrategyConfig, InstrumentConfig, get_strategy_config, get_instrument_config, get_dhan_db_path, get_backtest_dates
from indicators import compute_indicators
from strategy import check_entry, check_exit


def _get_conn(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def _load_spot_candles(conn: sqlite3.Connection, instrument: str, interval: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close FROM dhan_spot_candle "
        "WHERE instrument=? AND candle_type='intraday' AND interval_min=? ORDER BY timestamp",
        conn, params=(instrument, interval),
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _pick_expiry_code(entry_ts, expiry_weekday: int) -> int:
    """Mirror live trader's `exp > today` rule: on the weekly expiry day,
    today's contract is filtered out so the next-week contract (Dhan code=2)
    is the one actually traded. On every other weekday the current rolling
    weekly (code=1) is correct."""
    return 2 if pd.Timestamp(entry_ts).weekday() == expiry_weekday else 1


def _query_premium(conn, ts, direction, instrument, expiry_flag, expiry_code, target_strike=None):
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    if target_strike is None:
        sql = (
            "SELECT close, strike FROM dhan_option_candle "
            "WHERE instrument=? AND strike_offset='ATM' AND direction=? "
            "AND expiry_flag=? AND expiry_code=? AND interval_min=5 "
            "AND timestamp>=? AND timestamp<=? "
            "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1"
        )
        params = (instrument, dhan_dir, expiry_flag, expiry_code, lo, hi, ts.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        sql = (
            "SELECT close, strike FROM dhan_option_candle "
            "WHERE instrument=? AND direction=? "
            "AND expiry_flag=? AND expiry_code=? AND interval_min=5 "
            "AND timestamp>=? AND timestamp<=? AND ABS(strike-?)<1 "
            "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1"
        )
        params = (instrument, dhan_dir, expiry_flag, expiry_code, lo, hi, target_strike, ts.strftime("%Y-%m-%d %H:%M:%S"))
    row = conn.execute(sql, params).fetchone()
    return row


def _get_option_premium(conn: sqlite3.Connection, timestamp, direction: str, instrument: str, expiry_flag: str, expiry_code: int):
    """Get ATM option premium nearest to timestamp (±10 min). Returns (premium, strike) or (None, None).

    Falls back to the other expiry_code if the requested one returns no rows,
    so the backtest degrades gracefully while the code=2 cache is still being backfilled.
    """
    ts = pd.Timestamp(timestamp)
    row = _query_premium(conn, ts, direction, instrument, expiry_flag, expiry_code)
    if row is None:
        fallback = 1 if expiry_code == 2 else 2
        row = _query_premium(conn, ts, direction, instrument, expiry_flag, fallback)
    return (float(row[0]), float(row[1])) if row else (None, None)


def _get_premium_by_strike(conn: sqlite3.Connection, timestamp, direction: str, instrument: str, expiry_flag: str, expiry_code: int, target_strike: float):
    """Get option premium for a specific strike nearest to timestamp (±10 min).
    Falls back to the other expiry_code on miss."""
    ts = pd.Timestamp(timestamp)
    row = _query_premium(conn, ts, direction, instrument, expiry_flag, expiry_code, target_strike)
    if row is None:
        fallback = 1 if expiry_code == 2 else 2
        row = _query_premium(conn, ts, direction, instrument, expiry_flag, fallback, target_strike)
    return float(row[0]) if row else None


def run_backtest(
    strat_config: StrategyConfig | None = None,
    inst_config: InstrumentConfig | None = None,
    db_path: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Run backtest and return list of trade dicts."""
    sc = strat_config or get_strategy_config()
    ic = inst_config or get_instrument_config()
    db = db_path or get_dhan_db_path()
    dates = get_backtest_dates()
    start = start_date or dates[0]
    end = end_date or dates[1]

    conn = _get_conn(db)
    df = _load_spot_candles(conn, ic.name, sc.candle_interval)
    df = compute_indicators(df, sc.ema_short, sc.ema_long, sc.st_period, sc.st_multiplier)

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    if end_ts == end_ts.normalize():  # date-only input — include full day
        end_ts = end_ts + pd.Timedelta(hours=23, minutes=59, seconds=59)
    indices = df.index[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].tolist()

    # Precompute ORB (opening range) per day
    orb_ranges: dict = {}
    if sc.orb_filter:
        from datetime import time as _t
        _orb_end = _t(9, min(15 + sc.orb_window_minutes, 59))
        df["_date"] = df["timestamp"].dt.date
        df["_time"] = df["timestamp"].dt.time
        for d, grp in df.groupby("_date"):
            first_30 = grp[(grp["_time"] >= _t(9, 15)) & (grp["_time"] <= _orb_end)]
            if len(first_30) >= 2:
                orb_ranges[d] = (first_30["high"].max(), first_30["low"].min())
        df.drop(columns=["_date", "_time"], inplace=True)

    trades: list[dict] = []
    open_trade: dict | None = None
    last_exit_idx = -999

    for i in indices:
        if i == 0:
            continue

        row = df.iloc[i]
        prev = df.iloc[i - 1]
        ts = row["timestamp"]

        # === EXIT ===
        if open_trade is not None:
            candles_held = i - open_trade["idx"]
            reason = check_exit(row, open_trade["dir"], candles_held, sc.max_hold_candles, sc.ema_gap_floor)

            if reason:
                ep = None
                trade_code = open_trade["expiry_code"]
                if open_trade["strike"]:
                    ep = _get_premium_by_strike(conn, ts, open_trade["dir"], ic.name, ic.expiry_flag, trade_code, open_trade["strike"])
                if ep is None:
                    ep, _ = _get_option_premium(conn, ts, open_trade["dir"], ic.name, ic.expiry_flag, trade_code)
                pp = (ep - open_trade["prem_in"]) * ic.lot_size if ep and open_trade["prem_in"] else None

                trades.append({
                    "entry": open_trade["ts"],
                    "exit": ts,
                    "dir": open_trade["dir"],
                    "spot_in": open_trade["spot_in"],
                    "spot_out": row["close"],
                    "strike": open_trade["strike"],
                    "prem_in": open_trade["prem_in"],
                    "prem_out": ep,
                    "prem_pnl": pp,
                    "candles": candles_held,
                    "reason": reason,
                    "entry_rsi": open_trade.get("rsi"),
                    "entry_gap": open_trade.get("gap"),
                    "entry_type": open_trade.get("entry_type"),
                })
                open_trade = None
                last_exit_idx = i

        # === ENTRY ===
        if open_trade is not None:
            continue

        orb = orb_ranges.get(ts.date())
        # Don't pass ORB for candles within the opening range itself
        from datetime import time as _t2
        orb_h, orb_l = orb if (orb and ts.time() > _t2(9, min(15 + sc.orb_window_minutes, 59))) else (None, None)
        direction = check_entry(row, prev, sc.extra_entry_mode, sc.ema_gap_min, last_exit_idx, i, sc.cooldown_candles, orb_h, orb_l, sc.orb_filter)
        if direction is None:
            continue

        # Determine if this was crossover or extra mode
        crossover_up = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
        crossover_down = prev["ema9"] >= prev["ema21"] and row["ema9"] < row["ema21"]
        is_crossover = (direction == "CE" and crossover_up) or (direction == "PE" and crossover_down)
        entry_type = "crossover" if is_crossover else sc.extra_entry_mode

        entry_code = _pick_expiry_code(ts, ic.expiry_weekday)
        prem, strike = _get_option_premium(conn, ts, direction, ic.name, ic.expiry_flag, entry_code)
        open_trade = {
            "ts": ts, "idx": i, "dir": direction, "spot_in": row["close"],
            "prem_in": prem, "strike": strike, "expiry_code": entry_code,
            "rsi": row["rsi"], "gap": row["ema_gap_pct"],
            "entry_type": entry_type,
        }

    # Close remaining open trade
    if open_trade is not None and indices:
        last = df.iloc[indices[-1]]
        ep = None
        trade_code = open_trade["expiry_code"]
        if open_trade["strike"]:
            ep = _get_premium_by_strike(conn, last["timestamp"], open_trade["dir"], ic.name, ic.expiry_flag, trade_code, open_trade["strike"])
        if ep is None:
            ep, _ = _get_option_premium(conn, last["timestamp"], open_trade["dir"], ic.name, ic.expiry_flag, trade_code)
        pp = (ep - open_trade["prem_in"]) * ic.lot_size if ep and open_trade["prem_in"] else None
        trades.append({
            "entry": open_trade["ts"], "exit": last["timestamp"],
            "dir": open_trade["dir"], "spot_in": open_trade["spot_in"],
            "spot_out": last["close"], "strike": open_trade["strike"],
            "prem_in": open_trade["prem_in"], "prem_out": ep,
            "prem_pnl": pp, "candles": 0, "reason": "end",
            "entry_rsi": open_trade.get("rsi"), "entry_gap": open_trade.get("gap"),
            "entry_type": open_trade.get("entry_type"),
        })

    conn.close()
    return trades


if __name__ == "__main__":
    from log_setup import setup_logging
    setup_logging()
    from results import print_stats
    trades = run_backtest()
    sc = get_strategy_config()
    ic = get_instrument_config()
    label = f"{ic.name} | {sc.extra_entry_mode} | gap>={sc.ema_gap_min}% | {sc.candle_interval}min"
    print_stats(trades, label)
