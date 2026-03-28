"""config.py — Load .env and provide typed configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Strategy config
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    extra_entry_mode: str  # "none", "midtrend", "expanding"
    ema_gap_min: float
    max_hold_candles: int
    cooldown_candles: int
    candle_interval: int  # 5 or 15


@dataclass
class InstrumentConfig:
    name: str
    token: str
    exchange: str
    nfo_exchange: str
    lot_size: int
    strike_interval: int
    expiry_weekday: int  # 0=Mon ... 6=Sun
    expiry_type: str  # "weekly" or "monthly"
    expiry_flag: str  # "WEEK" or "MONTH" (for Dhan DB)


INSTRUMENTS: dict[str, InstrumentConfig] = {
    "NIFTY": InstrumentConfig(
        name="NIFTY", token="99926000", exchange="NSE", nfo_exchange="NFO",
        lot_size=65, strike_interval=50, expiry_weekday=1,
        expiry_type="weekly", expiry_flag="WEEK",
    ),
    "BANKNIFTY": InstrumentConfig(
        name="BANKNIFTY", token="99926009", exchange="NSE", nfo_exchange="NFO",
        lot_size=15, strike_interval=100, expiry_weekday=3,
        expiry_type="monthly", expiry_flag="MONTH",
    ),
    "SENSEX": InstrumentConfig(
        name="SENSEX", token="99919000", exchange="BSE", nfo_exchange="BFO",
        lot_size=20, strike_interval=100, expiry_weekday=1,
        expiry_type="weekly", expiry_flag="WEEK",
    ),
}

# ---------------------------------------------------------------------------
# Trading hours
# ---------------------------------------------------------------------------

MARKET_OPEN = time(9, 15)
TRADING_START = time(9, 30)
TIME_EXIT = time(15, 10)
MARKET_CLOSE = time(15, 30)

# WebSocket
WS_ENABLED: bool = os.getenv("WS_ENABLED", "true").lower() == "true"
WS_EXIT_CHECK_INTERVAL: int = int(os.getenv("WS_EXIT_CHECK_INTERVAL", "2"))

# ---------------------------------------------------------------------------
# Load from .env
# ---------------------------------------------------------------------------


def get_strategy_config() -> StrategyConfig:
    return StrategyConfig(
        extra_entry_mode=os.getenv("EXTRA_ENTRY_MODE", "midtrend"),
        ema_gap_min=float(os.getenv("EMA_GAP_MIN", "0.03")),
        max_hold_candles=int(os.getenv("MAX_HOLD_CANDLES", "20")),
        cooldown_candles=int(os.getenv("COOLDOWN_CANDLES", "3")),
        candle_interval=int(os.getenv("CANDLE_INTERVAL", "15")),
    )


def get_instrument_config(name: str | None = None) -> InstrumentConfig:
    name = name or os.getenv("INSTRUMENT", "NIFTY")
    if name not in INSTRUMENTS:
        raise ValueError(f"Unknown instrument '{name}'. Choose from: {list(INSTRUMENTS)}")
    return INSTRUMENTS[name]


def get_capital() -> int:
    return int(os.getenv("CAPITAL", "100000"))


def get_dhan_db_path() -> str:
    return os.getenv("DHAN_DB_PATH", "dhan_option_cache.db")


def get_backtest_dates() -> tuple[str, str]:
    return (
        os.getenv("BACKTEST_START", "2023-04-01"),
        os.getenv("BACKTEST_END", "2026-03-28"),
    )
