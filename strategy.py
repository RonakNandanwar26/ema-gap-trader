"""strategy.py — Entry/exit logic for EMA Gap strategy.

Crossover entry is ALWAYS active.
Extra mode (midtrend/expanding) is additive — catches re-entries after exits.
"""

from __future__ import annotations

import pandas as pd


def check_entry(
    row: pd.Series,
    prev: pd.Series,
    extra_mode: str,
    gap_min: float,
    last_exit_idx: int,
    current_idx: int,
    cooldown: int,
    orb_high: float | None = None,
    orb_low: float | None = None,
    orb_enabled: bool = False,
) -> str | None:
    """Return direction ("CE"/"PE") if entry conditions met, else None.

    Crossover always checked first. If no crossover, extra_mode checked.
    """
    direction = None

    # --- 1. Crossover check (always active) ---
    crossover_up = prev["ema9"] <= prev["ema21"] and row["ema9"] > row["ema21"]
    crossover_down = prev["ema9"] >= prev["ema21"] and row["ema9"] < row["ema21"]

    if crossover_up and row["st_dir"] == 1:
        direction = "CE"
    elif crossover_down and row["st_dir"] == -1:
        direction = "PE"

    # --- 2. Extra mode (if no crossover fired) ---
    if direction is None and extra_mode != "none":
        # Cooldown check
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

    # --- 3. Common filters (RSI alignment + gap minimum) ---
    # RSI alignment
    if direction == "CE" and (pd.isna(row["rsi"]) or row["rsi"] <= 50):
        return None
    if direction == "PE" and (pd.isna(row["rsi"]) or row["rsi"] >= 50):
        return None

    # EMA gap minimum
    if gap_min > 0 and row["ema_gap_pct"] < gap_min:
        return None

    # EMA gap maximum — overextended entries (e.g. gap-and-crap) lose consistently
    if row["ema_gap_pct"] > 0.5:
        return None

    # ORB direction filter — only enter if price broke the opening range
    if orb_enabled:
        if orb_high is None or orb_low is None:
            return None  # ORB range not ready yet — block all entries
        if direction == "CE" and row["close"] < orb_high:
            return None
        if direction == "PE" and row["close"] > orb_low:
            return None

    return direction


def entry_premium_ok(premium: float | None, max_premium: float | None) -> bool:
    """Premium-level entry filter: the edge lives in cheap option entries.

    max_premium=None disables the filter. With it set, unknown premium blocks
    entry (can't verify affordability -> stay out).
    """
    if max_premium is None:
        return True
    return premium is not None and premium <= max_premium


def eod_exit_due(eod_rule: str | None, in_profit: bool | None) -> bool:
    """End-of-day (15:10) carry decision.

    None         -> hold everything overnight (full carry-forward)
    "exit_all"   -> flat by close (intraday only)
    "exit_losers"-> carry winners, close losers; unknown P&L closes for safety
    """
    if eod_rule is None:
        return False
    if eod_rule == "exit_all":
        return True
    return in_profit is not True  # exit_losers


def check_stop_loss(prem_in: float | None, prem_low: float | None,
                    stop_loss_pct: float) -> float | None:
    """Per-trade stop loss on option premium.

    Returns the stop level (assumed fill price) if the candle low breached
    entry_premium * (1 - pct/100), else None. pct=0 disables.
    """
    if not stop_loss_pct or prem_in is None or prem_low is None:
        return None
    threshold = prem_in * (1 - stop_loss_pct / 100)
    return threshold if prem_low <= threshold else None


def check_exit(
    row: pd.Series,
    trade_dir: str,
    candles_held: int,
    max_hold: int,
    ema_gap_floor: float = 0.0,
) -> str | None:
    """Return exit reason string if exit conditions met, else None."""
    # 1. SuperTrend flip
    if trade_dir == "CE" and row["st_dir"] == -1:
        return "ST_flip"
    if trade_dir == "PE" and row["st_dir"] == 1:
        return "ST_flip"

    # 2. EMA cross reversal
    if trade_dir == "CE" and row["ema9"] < row["ema21"]:
        return "EMA_cross"
    if trade_dir == "PE" and row["ema9"] > row["ema21"]:
        return "EMA_cross"

    # 3. EMA gap contraction — trend dying, bail before theta chews the premium.
    # Min 2 candles held so we don't bail on entry-candle wiggles.
    if ema_gap_floor > 0 and candles_held >= 2 and row["ema_gap_pct"] < ema_gap_floor:
        return "gap_contract"

    # 4. Max hold
    if candles_held >= max_hold:
        return "max_hold"

    return None
