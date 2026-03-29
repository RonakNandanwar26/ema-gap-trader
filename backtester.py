"""backtester.py — Run strategy on historical Dhan option data."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pandas as pd

from config import StrategyConfig, InstrumentConfig, get_strategy_config, get_instrument_config, get_dhan_db_path, get_backtest_dates, get_capital
from indicators import compute_indicators
from strategy import check_entry, check_exit


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    # Ensure index exists for fast strike+timestamp lookups
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_option_strike_ts "
        "ON dhan_option_candle(instrument, direction, expiry_flag, interval_min, strike, timestamp)"
    )
    return conn


def _load_spot_candles(conn: sqlite3.Connection, instrument: str, interval: int) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT timestamp, open, high, low, close FROM dhan_spot_candle "
        "WHERE instrument=? AND candle_type='intraday' AND interval_min=? ORDER BY timestamp",
        conn, params=(instrument, interval),
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _get_option_premium(conn: sqlite3.Connection, timestamp, direction: str, instrument: str, expiry_flag: str):
    """Get ATM option premium nearest to timestamp (±10 min). Returns (premium, strike) or (None, None)."""
    ts = pd.Timestamp(timestamp)
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    cur = conn.execute(
        "SELECT close, strike FROM dhan_option_candle "
        "WHERE instrument=? AND strike_offset='ATM' AND direction=? "
        "AND expiry_flag=? AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (instrument, dhan_dir, expiry_flag, lo, hi, ts.strftime("%Y-%m-%d %H:%M:%S")),
    )
    row = cur.fetchone()
    return (float(row[0]), float(row[1])) if row else (None, None)


def _get_premium_by_strike(conn: sqlite3.Connection, timestamp, direction: str, instrument: str, expiry_flag: str, target_strike: float):
    """Get option premium for a specific strike nearest to timestamp (±10 min)."""
    ts = pd.Timestamp(timestamp)
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    cur = conn.execute(
        "SELECT close FROM dhan_option_candle "
        "WHERE instrument=? AND direction=? "
        "AND expiry_flag=? AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? AND ABS(strike-?)<1 "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (instrument, dhan_dir, expiry_flag, lo, hi, target_strike, ts.strftime("%Y-%m-%d %H:%M:%S")),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def _get_premium_low_by_strike(
    conn: sqlite3.Connection, timestamp, direction: str,
    instrument: str, expiry_flag: str, target_strike: float,
) -> float | None:
    """Get option candle LOW for a specific strike nearest to timestamp (+-10 min)."""
    ts = pd.Timestamp(timestamp)
    lo = (ts - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (ts + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    dhan_dir = "CALL" if direction == "CE" else "PUT"
    cur = conn.execute(
        "SELECT low FROM dhan_option_candle "
        "WHERE instrument=? AND direction=? "
        "AND expiry_flag=? AND interval_min=5 "
        "AND timestamp>=? AND timestamp<=? AND ABS(strike-?)<1 "
        "ORDER BY ABS(julianday(timestamp)-julianday(?)) LIMIT 1",
        (instrument, dhan_dir, expiry_flag, lo, hi, target_strike,
         ts.strftime("%Y-%m-%d %H:%M:%S")),
    )
    row = cur.fetchone()
    return float(row[0]) if row else None


def run_backtest(
    strat_config: StrategyConfig | None = None,
    inst_config: InstrumentConfig | None = None,
    db_path: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    capital: float | None = None,
) -> list[dict]:
    """Run backtest and return list of trade dicts."""
    sc = strat_config or get_strategy_config()
    ic = inst_config or get_instrument_config()
    db = db_path or get_dhan_db_path()
    dates = get_backtest_dates()
    start = start_date or dates[0]
    end = end_date or dates[1]

    cap = capital or get_capital()
    conn = _get_conn(db)
    df = _load_spot_candles(conn, ic.name, sc.candle_interval)
    df = compute_indicators(df)

    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    indices = df.index[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].tolist()

    trades: list[dict] = []
    open_trade: dict | None = None
    last_exit_idx = -999

    # Daily drawdown tracking
    daily_pnl: float = 0.0
    daily_limit_hit: bool = False
    current_trading_day = None

    for i in indices:
        if i == 0:
            continue

        row = df.iloc[i]
        prev = df.iloc[i - 1]
        ts = row["timestamp"]

        # Day change — reset daily drawdown tracking
        row_date = ts.date() if hasattr(ts, 'date') else pd.Timestamp(ts).date()
        if row_date != current_trading_day:
            current_trading_day = row_date
            daily_pnl = 0.0
            daily_limit_hit = False

        # === EXIT ===
        if open_trade is not None:
            candles_held = i - open_trade["idx"]
            reason = check_exit(row, open_trade["dir"], candles_held, sc.max_hold_candles)

            # Per-trade stop loss check (only query DB if no other exit fired)
            sl_exit_premium = None
            if reason is None and sc.stop_loss_pct > 0 and open_trade["prem_in"] is not None and open_trade["strike"] is not None:
                sl_threshold = open_trade["prem_in"] * (1 - sc.stop_loss_pct / 100)
                prem_low = _get_premium_low_by_strike(
                    conn, ts, open_trade["dir"], ic.name, ic.expiry_flag, open_trade["strike"],
                )
                if prem_low is not None and prem_low <= sl_threshold:
                    reason = "stop_loss"
                    sl_exit_premium = sl_threshold

            if reason:
                if sl_exit_premium is not None:
                    ep = sl_exit_premium
                else:
                    ep = None
                    if open_trade["strike"]:
                        ep = _get_premium_by_strike(conn, ts, open_trade["dir"], ic.name, ic.expiry_flag, open_trade["strike"])
                    if ep is None:
                        ep, _ = _get_option_premium(conn, ts, open_trade["dir"], ic.name, ic.expiry_flag)
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

                # Daily drawdown tracking
                if pp is not None:
                    daily_pnl += pp
                    if (sc.daily_loss_limit_pct > 0
                            and daily_pnl <= -(sc.daily_loss_limit_pct / 100 * cap)):
                        daily_limit_hit = True

        # === ENTRY ===
        if open_trade is not None or daily_limit_hit:
            continue

        direction = check_entry(row, prev, sc.extra_entry_mode, sc.ema_gap_min, last_exit_idx, i, sc.cooldown_candles)
        if direction is None:
            continue

        # Determine if this was crossover or extra mode
        crossover_up = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
        crossover_down = prev["ema9"] >= prev["ema21"] and row["ema9"] < row["ema21"]
        is_crossover = (direction == "CE" and crossover_up) or (direction == "PE" and crossover_down)
        entry_type = "crossover" if is_crossover else sc.extra_entry_mode

        prem, strike = _get_option_premium(conn, ts, direction, ic.name, ic.expiry_flag)
        open_trade = {
            "ts": ts, "idx": i, "dir": direction, "spot_in": row["close"],
            "prem_in": prem, "strike": strike,
            "rsi": row["rsi"], "gap": row["ema_gap_pct"],
            "entry_type": entry_type,
        }

    # Close remaining open trade
    if open_trade is not None and indices:
        last = df.iloc[indices[-1]]
        ep = None
        if open_trade["strike"]:
            ep = _get_premium_by_strike(conn, last["timestamp"], open_trade["dir"], ic.name, ic.expiry_flag, open_trade["strike"])
        if ep is None:
            ep, _ = _get_option_premium(conn, last["timestamp"], open_trade["dir"], ic.name, ic.expiry_flag)
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
