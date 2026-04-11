"""
dhan_data.py — Dhan API client for fetching historical expired option data.

Provides functions to fetch expired option candle data (OHLCV + IV + OI + spot)
from Dhan's rolling option API, with SQLite caching to avoid redundant API calls.

Usage:
    from dhan_data import init_dhan, prefetch_option_data

    dhan = init_dhan()
    data = prefetch_option_data("NIFTY", "WEEK", 5, "2024-01-01", "2024-03-31")
    # data[("ATM", "CALL")] -> DataFrame with option candles

Environment variables:
    DHAN_CLIENT_ID   — Dhan client ID
    DHAN_ACCESS_TOKEN — Dhan access token (JWT)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(".env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dhan security IDs for underlying indices
# ---------------------------------------------------------------------------

_SECURITY_IDS = {
    "NIFTY": 13,
    "BANKNIFTY": 25,
    "SENSEX": 51,
}

_EXPIRY_FLAGS = {
    "NIFTY": "WEEK",
    "BANKNIFTY": "MONTH",
    "SENSEX": "WEEK",
}

_EXCHANGE_SEGMENTS = {
    "NIFTY": "NSE_FNO",
    "BANKNIFTY": "NSE_FNO",
    "SENSEX": "BSE_FNO",
}

# expiryCode mapping (verified against real market premiums Mar 16-20):
# NSE_FNO (Nifty, BankNifty): code=1 is always the active contract.
# BSE_FNO (Sensex): code shifts mid-week after Tue expiry.
#   Mon, Tue (expiry), Fri → code=1 (rolled/active)
#   Wed, Thu → code=2 (code=1 still shows expired contract)
_EXPIRY_CODES = {
    "NIFTY": 1,
    "BANKNIFTY": 1,
}

# Days where BSE Sensex needs expiryCode=2 (0=Mon ... 4=Fri)
_BSE_CODE2_WEEKDAYS = {2, 3}  # Wed, Thu


def _get_expiry_code(instrument_name: str, weekday: int) -> int:
    """Return the correct Dhan expiryCode for an instrument on a given weekday."""
    code = _EXPIRY_CODES.get(instrument_name.upper())
    if code is not None:
        return code
    # Sensex (BSE): dynamic based on day of week
    if weekday in _BSE_CODE2_WEEKDAYS:
        return 2
    return 1

# Max date range per Dhan API call
_MAX_CHUNK_DAYS = 30

# Rate limit: 5 req/sec → sleep 0.25s between calls
_RATE_LIMIT_DELAY = 0.25

# SQLite DB for caching Dhan option candles (shared with backtester via DHAN_DB_PATH)
_DHAN_CACHE_DB = os.environ.get("DHAN_DB_PATH", "dhan_option_cache.db")

# Strike offsets to prefetch: ATM ± 7
_STRIKE_OFFSETS = (
    ["ATM"]
    + [f"ATM+{i}" for i in range(1, 8)]
    + [f"ATM-{i}" for i in range(1, 8)]
)


# ---------------------------------------------------------------------------
# Dhan client initialization
# ---------------------------------------------------------------------------

_DHAN_API_BASE = "https://api.dhan.co/v2"

_DHAN_TOKEN_PATH = os.environ.get("DHAN_TOKEN_PATH", "/etc/ema-trader/dhan.token")


def _get_dhan_token() -> str:
    """Read the Dhan access token just-in-time.

    The token is rotated daily by the user via the Streamlit sidebar widget
    in app.py, which writes ``/etc/ema-trader/dhan.token``. Reading per call
    means the next Dhan API request always picks up the fresh value with no
    service restart. Falls back to ``DHAN_ACCESS_TOKEN`` env var when the
    file is absent — used for local dev and pytest.
    """
    try:
        return Path(_DHAN_TOKEN_PATH).read_text().strip()
    except FileNotFoundError:
        return os.environ.get("DHAN_ACCESS_TOKEN", "")


def init_dhan() -> dict:
    """Return Dhan API credentials as a dict.

    Reads ``DHAN_CLIENT_ID`` and ``DHAN_ACCESS_TOKEN`` from environment.

    Returns:
        Dict with ``client_id`` and ``access_token``.

    Raises:
        RuntimeError: If credentials are not set.
    """
    client_id = os.environ.get("DHAN_CLIENT_ID", "")
    access_token = _get_dhan_token()

    if not client_id or not access_token:
        raise RuntimeError(
            "Dhan API credentials not set. "
            "Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env file. "
            "Get them from https://dhanhq.co/"
        )

    # Quick validation
    resp = requests.get(
        f"{_DHAN_API_BASE}/fundlimit",
        headers={"access-token": access_token},
        timeout=10,
    )
    if resp.status_code == 401:
        raise RuntimeError("Dhan access token is invalid or expired. Regenerate from dhanhq.co")

    logger.info("Dhan credentials validated for client %s", client_id)
    return {"client_id": client_id, "access_token": access_token}


# ---------------------------------------------------------------------------
# SQLite cache for Dhan option candles
# ---------------------------------------------------------------------------

def _get_cache_db() -> sqlite3.Connection:
    """Open (and create if needed) the Dhan cache database."""
    conn = sqlite3.connect(_DHAN_CACHE_DB)

    # Migrate legacy dhan_option_candle (no expiry_code column) to the new schema
    # that distinguishes current-week (code=1) from next-week (code=2) contracts.
    legacy = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dhan_option_candle'"
    ).fetchone()
    if legacy:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(dhan_option_candle)")]
        if "expiry_code" not in cols:
            logger.info("Migrating dhan_option_candle: adding expiry_code column")
            conn.executescript("""
                BEGIN;
                CREATE TABLE dhan_option_candle_new (
                    instrument TEXT NOT NULL,
                    strike_offset TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    expiry_flag TEXT NOT NULL,
                    expiry_code INTEGER NOT NULL DEFAULT 1,
                    interval_min INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL,
                    volume INTEGER, iv REAL, oi INTEGER, spot REAL, strike REAL,
                    UNIQUE(instrument, strike_offset, direction, expiry_flag,
                           expiry_code, interval_min, timestamp)
                );
                INSERT INTO dhan_option_candle_new
                    (instrument, strike_offset, direction, expiry_flag, expiry_code,
                     interval_min, timestamp, open, high, low, close, volume, iv, oi, spot, strike)
                SELECT instrument, strike_offset, direction, expiry_flag, 1,
                       interval_min, timestamp, open, high, low, close, volume, iv, oi, spot, strike
                FROM dhan_option_candle;
                DROP TABLE dhan_option_candle;
                ALTER TABLE dhan_option_candle_new RENAME TO dhan_option_candle;
                COMMIT;
            """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dhan_option_candle (
            instrument TEXT NOT NULL,
            strike_offset TEXT NOT NULL,
            direction TEXT NOT NULL,
            expiry_flag TEXT NOT NULL,
            expiry_code INTEGER NOT NULL DEFAULT 1,
            interval_min INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            iv REAL,
            oi INTEGER,
            spot REAL,
            strike REAL,
            UNIQUE(instrument, strike_offset, direction, expiry_flag,
                   expiry_code, interval_min, timestamp)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_dhan_candle_lookup
        ON dhan_option_candle(instrument, strike_offset, direction,
                              expiry_flag, expiry_code, interval_min, timestamp)
    """)
    # Spot candle cache (intraday + daily)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dhan_spot_candle (
            instrument TEXT NOT NULL,
            candle_type TEXT NOT NULL,
            interval_min INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            UNIQUE(instrument, candle_type, interval_min, timestamp)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_dhan_spot_lookup
        ON dhan_spot_candle(instrument, candle_type, interval_min, timestamp)
    """)
    conn.commit()
    return conn


def _next_day(date_str: str) -> str:
    """Return date_str + 1 day as 'YYYY-MM-DD' for exclusive upper bound queries."""
    dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
    return (dt + timedelta(days=1)).strftime("%Y-%m-%d")


def _load_cached(
    instrument: str, strike_offset: str, direction: str,
    expiry_flag: str, expiry_code: int, interval_min: int,
    from_date: str, to_date: str,
) -> pd.DataFrame:
    """Load cached candles for the given parameters and date range."""
    conn = _get_cache_db()
    query = """
        SELECT timestamp, open, high, low, close, volume, iv, oi, spot, strike
        FROM dhan_option_candle
        WHERE instrument = ? AND strike_offset = ? AND direction = ?
          AND expiry_flag = ? AND expiry_code = ? AND interval_min = ?
          AND timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
    """
    df = pd.read_sql_query(
        query, conn,
        params=(instrument, strike_offset, direction, expiry_flag, expiry_code,
                interval_min, from_date, _next_day(to_date)),
    )
    conn.close()
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _save_to_cache(
    instrument: str, strike_offset: str, direction: str,
    expiry_flag: str, expiry_code: int, interval_min: int,
    df: pd.DataFrame,
) -> None:
    """Save candle data to the cache database."""
    if df.empty:
        return
    conn = _get_cache_db()
    rows = []
    for _, row in df.iterrows():
        rows.append((
            instrument, strike_offset, direction, expiry_flag, expiry_code, interval_min,
            str(row["timestamp"]),
            row.get("open"), row.get("high"), row.get("low"), row.get("close"),
            row.get("volume"), row.get("iv"), row.get("oi"),
            row.get("spot"), row.get("strike"),
        ))
    conn.executemany("""
        INSERT OR IGNORE INTO dhan_option_candle
        (instrument, strike_offset, direction, expiry_flag, expiry_code, interval_min,
         timestamp, open, high, low, close, volume, iv, oi, spot, strike)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()
    logger.debug("Cached %d Dhan candles for %s %s %s code=%d",
                 len(rows), instrument, strike_offset, direction, expiry_code)


def _load_spot_cached(
    instrument: str, candle_type: str, interval_min: int,
    from_date: str, to_date: str,
) -> pd.DataFrame:
    """Load cached spot candles for the given parameters."""
    conn = _get_cache_db()
    query = """
        SELECT timestamp, open, high, low, close, volume
        FROM dhan_spot_candle
        WHERE instrument = ? AND candle_type = ? AND interval_min = ?
          AND timestamp >= ? AND timestamp < ?
        ORDER BY timestamp
    """
    df = pd.read_sql_query(
        query, conn,
        params=(instrument, candle_type, interval_min, from_date, _next_day(to_date)),
    )
    conn.close()
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _save_spot_to_cache(
    instrument: str, candle_type: str, interval_min: int,
    df: pd.DataFrame,
) -> None:
    """Save spot candle data to the cache database."""
    if df.empty:
        return
    conn = _get_cache_db()
    rows = []
    for _, row in df.iterrows():
        rows.append((
            instrument, candle_type, interval_min,
            str(row["timestamp"]),
            row.get("open"), row.get("high"), row.get("low"), row.get("close"),
            row.get("volume"),
        ))
    conn.executemany("""
        INSERT OR IGNORE INTO dhan_spot_candle
        (instrument, candle_type, interval_min,
         timestamp, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()
    logger.debug("Cached %d spot candles for %s %s %dmin",
                 len(rows), instrument, candle_type, interval_min)


# ---------------------------------------------------------------------------
# Core API fetching
# ---------------------------------------------------------------------------

def _parse_dhan_response(resp: dict, direction: str) -> pd.DataFrame:
    """Parse Dhan expired_options_data response into a DataFrame.

    The response has structure: ``{data: {ce: {...}, pe: {...}}}``
    where each side has arrays for open, high, low, close, etc.
    """
    if not resp or resp.get("status") == "failure":
        logger.warning("Dhan API error: %s", resp)
        return pd.DataFrame()

    data = resp.get("data", {})
    if not data:
        return pd.DataFrame()

    side_key = "ce" if direction == "CALL" else "pe"
    side = data.get(side_key)
    if not side:
        return pd.DataFrame()

    timestamps = side.get("timestamp", [])
    if not timestamps:
        return pd.DataFrame()

    df = pd.DataFrame({
        "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
        "open": side.get("open", []),
        "high": side.get("high", []),
        "low": side.get("low", []),
        "close": side.get("close", []),
        "volume": side.get("volume", []),
        "iv": side.get("iv", []),
        "oi": side.get("oi", []),
        "spot": side.get("spot", []),
        "strike": side.get("strike", []),
    })

    # Convert UTC to IST (UTC+5:30)
    df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)

    return df


def fetch_expired_option_candles(
    dhan_creds: dict,
    instrument_name: str,
    strike_offset: str,
    direction: str,
    expiry_flag: str,
    interval: int,
    from_date: str,
    to_date: str,
    expiry_code: int | None = None,
) -> pd.DataFrame:
    """Fetch expired option candle data from Dhan API.

    Handles 30-day chunking and rate limiting internally.

    Args:
        dhan_creds: Dict with ``access_token`` from :func:`init_dhan`.
        instrument_name: "NIFTY", "BANKNIFTY", or "SENSEX".
        strike_offset: "ATM", "ATM+1", "ATM-5", etc.
        direction: "CALL" or "PUT".
        expiry_flag: "WEEK" or "MONTH".
        interval: Candle interval in minutes (1, 5, 15).
        from_date: Start date "YYYY-MM-DD".
        to_date: End date "YYYY-MM-DD" (inclusive).
        expiry_code: Override Dhan's expiryCode. ``None`` (default) preserves
            legacy behavior — use the per-instrument default (1 for NIFTY/
            BankNifty, dynamic for Sensex). Pass ``2`` to fetch the next-week
            contract instead of the active rolling weekly.

    Returns:
        DataFrame with columns: timestamp, open, high, low, close,
        volume, iv, oi, spot, strike.
    """
    security_id = _SECURITY_IDS.get(instrument_name.upper())
    if security_id is None:
        raise ValueError(f"Unknown instrument: {instrument_name}")

    exchange_segment = _EXCHANGE_SEGMENTS[instrument_name.upper()]
    headers = {
        "access-token": _get_dhan_token(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    instrument = instrument_name.upper()
    needs_dynamic_code = expiry_code is None and instrument not in _EXPIRY_CODES

    dt_from = datetime.strptime(from_date, "%Y-%m-%d")
    dt_to = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)

    all_dfs = []
    chunk_start = dt_from

    while chunk_start < dt_to:
        if needs_dynamic_code:
            # Sensex: fetch day-by-day since expiryCode changes mid-week
            chunk_end = chunk_start + timedelta(days=1)
        else:
            chunk_end = min(chunk_start + timedelta(days=_MAX_CHUNK_DAYS), dt_to)

        if expiry_code is not None:
            request_code = expiry_code
        else:
            request_code = _get_expiry_code(instrument, chunk_start.weekday())

        logger.debug(
            "Dhan fetch: %s %s %s %s code=%d %s -> %s",
            instrument, strike_offset, direction,
            expiry_flag, request_code,
            chunk_start.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
        )

        payload = {
            "exchangeSegment": exchange_segment,
            "interval": interval,
            "securityId": str(security_id),
            "instrument": "OPTIDX",
            "expiryFlag": expiry_flag,
            "expiryCode": request_code,
            "strike": strike_offset,
            "drvOptionType": direction,
            "requiredData": ["open", "high", "low", "close",
                             "iv", "volume", "strike", "oi", "spot"],
            "fromDate": chunk_start.strftime("%Y-%m-%d"),
            "toDate": chunk_end.strftime("%Y-%m-%d"),
        }

        for attempt in range(1, 4):  # up to 3 retries
            try:
                resp = requests.post(
                    f"{_DHAN_API_BASE}/charts/rollingoption",
                    headers=headers, json=payload, timeout=30,
                )
                if resp.status_code == 200:
                    df_chunk = _parse_dhan_response(resp.json(), direction)
                    if not df_chunk.empty:
                        all_dfs.append(df_chunk)
                    break
                elif resp.status_code in (502, 503, 429):
                    wait = attempt * 2
                    logger.warning(
                        "Dhan API %d (attempt %d/3), retrying in %ds...",
                        resp.status_code, attempt, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.warning("Dhan API %d: %s", resp.status_code, resp.text[:200])
                    break
            except Exception:
                logger.exception(
                    "Dhan API call failed for %s %s %s %s-%s (attempt %d/3)",
                    instrument, strike_offset, direction,
                    chunk_start.strftime("%Y-%m-%d"),
                    chunk_end.strftime("%Y-%m-%d"),
                    attempt,
                )
                if attempt < 3:
                    time.sleep(attempt * 2)

        chunk_start = chunk_end
        time.sleep(_RATE_LIMIT_DELAY)

    if not all_dfs:
        return pd.DataFrame()

    result = pd.concat(all_dfs, ignore_index=True)
    result.sort_values("timestamp", inplace=True)
    result.reset_index(drop=True, inplace=True)
    return result


# ---------------------------------------------------------------------------
# Cached fetching
# ---------------------------------------------------------------------------

def fetch_and_cache_dhan(
    dhan_client,
    instrument_name: str,
    strike_offset: str,
    direction: str,
    expiry_flag: str,
    interval: int,
    from_date: str,
    to_date: str,
    expiry_code: int = 1,
) -> pd.DataFrame:
    """Fetch expired option candles with SQLite caching.

    Checks cache first, fetches only missing data from Dhan API.
    ``expiry_code`` selects current-week (1) vs next-week (2) etc.
    """
    instrument = instrument_name.upper()

    # Check cache
    cached = _load_cached(
        instrument, strike_offset, direction, expiry_flag, expiry_code, interval,
        from_date, to_date,
    )

    if not cached.empty:
        # Check if cache covers the full range
        cache_start = cached["timestamp"].min().strftime("%Y-%m-%d")
        cache_end = cached["timestamp"].max().strftime("%Y-%m-%d")
        if cache_start <= from_date and cache_end >= to_date:
            logger.debug("Full cache hit for %s %s %s code=%d",
                         instrument, strike_offset, direction, expiry_code)
            return cached

    # Fetch from API
    logger.info(
        "Fetching from Dhan: %s %s %s code=%d %dmin %s to %s",
        instrument, strike_offset, direction, expiry_code, interval, from_date, to_date,
    )
    fresh = fetch_expired_option_candles(
        dhan_client, instrument, strike_offset, direction,
        expiry_flag, interval, from_date, to_date, expiry_code=expiry_code,
    )

    if not fresh.empty:
        _save_to_cache(instrument, strike_offset, direction, expiry_flag, expiry_code, interval, fresh)

    # Return combined (cached + fresh), deduplicated
    if not cached.empty and not fresh.empty:
        combined = pd.concat([cached, fresh], ignore_index=True)
        combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
        combined.sort_values("timestamp", inplace=True)
        combined.reset_index(drop=True, inplace=True)
        return combined

    return fresh if not fresh.empty else cached


# ---------------------------------------------------------------------------
# Bulk prefetch
# ---------------------------------------------------------------------------

def prefetch_option_data(
    dhan_client,
    instrument_name: str,
    expiry_flag: str,
    interval: int,
    from_date: str,
    to_date: str,
    expiry_codes: tuple[int, ...] = (1,),
) -> dict[tuple[str, str], pd.DataFrame]:
    """Prefetch option data for ATM ± 7 strikes, both CE and PE.

    ``expiry_codes`` is a tuple of Dhan expiryCode values to fetch (e.g.
    ``(1, 2)`` populates both current-week and next-week contracts). The
    returned dict is keyed by ``(strike_offset, direction)`` and contains
    only the LAST fetched code's DataFrame; the cache holds all codes.
    """
    data: dict[tuple[str, str], pd.DataFrame] = {}
    total = len(_STRIKE_OFFSETS) * 2 * len(expiry_codes)
    fetched = 0

    for code in expiry_codes:
        for strike_offset in _STRIKE_OFFSETS:
            for direction in ("CALL", "PUT"):
                fetched += 1
                logger.info(
                    "Prefetching [%d/%d]: %s %s %s %s code=%d",
                    fetched, total, instrument_name, strike_offset,
                    direction, expiry_flag, code,
                )
                print(
                    f"  Fetching {instrument_name} {strike_offset} {direction} "
                    f"code={code} ({fetched}/{total})...",
                    end=" ", flush=True,
                )

                df = fetch_and_cache_dhan(
                    dhan_client, instrument_name, strike_offset, direction,
                    expiry_flag, interval, from_date, to_date, expiry_code=code,
                )

                data[(strike_offset, direction)] = df
                candles = len(df) if not df.empty else 0
                print(f"{candles} candles")

    return data


def reorganize_by_absolute_strike(
    option_data: dict[tuple[str, str], pd.DataFrame],
) -> dict[tuple[int, str], pd.DataFrame]:
    """Reorganize prefetched option data from relative offsets to absolute strikes.

    The Dhan rolling option API returns data keyed by relative offset
    (e.g., "ATM", "ATM+1") where the actual strike changes as spot moves.
    This function regroups all candles by their **absolute strike price**
    so that a single contract can be tracked consistently over time.

    Args:
        option_data: Dict from :func:`prefetch_option_data`, keyed by
            ``(strike_offset, direction)`` → DataFrame with a ``strike`` column.

    Returns:
        Dict keyed by ``(absolute_strike, direction)`` → DataFrame.
        Each DataFrame is deduplicated by timestamp and sorted chronologically.
    """
    from collections import defaultdict

    buckets: dict[tuple[int, str], list[pd.DataFrame]] = defaultdict(list)

    for (offset, direction), df in option_data.items():
        if df is None or df.empty:
            continue

        # Drop rows with missing strike
        valid = df.dropna(subset=["strike"])
        if valid.empty:
            continue

        # Group rows by their actual absolute strike
        valid = valid.copy()
        valid["_abs_strike"] = valid["strike"].astype(int)
        for abs_strike, group in valid.groupby("_abs_strike"):
            buckets[(int(abs_strike), direction)].append(
                group.drop(columns=["_abs_strike"])
            )

    # Merge, deduplicate, and sort each absolute-strike series
    result: dict[tuple[int, str], pd.DataFrame] = {}
    for key, dfs in buckets.items():
        merged = pd.concat(dfs, ignore_index=True)
        merged["timestamp"] = pd.to_datetime(merged["timestamp"])
        merged.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
        merged.sort_values("timestamp", inplace=True)
        merged.reset_index(drop=True, inplace=True)
        result[key] = merged

    logger.info(
        "Reorganized option data: %d relative streams -> %d absolute strike series",
        len(option_data), len(result),
    )
    return result


# ---------------------------------------------------------------------------
# Index candle fetching (spot OHLCV — no Angel One dependency)
# ---------------------------------------------------------------------------

_INDEX_SEGMENTS = {
    "NIFTY": "IDX_I",
    "BANKNIFTY": "IDX_I",
    "SENSEX": "IDX_I",
}


def _fetch_intraday_chunk(
    dhan_creds: dict,
    instrument_name: str,
    exchange_segment: str,
    interval: int,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    """Fetch a single chunk of intraday data (must be ≤ 90 days)."""
    headers = {
        "access-token": _get_dhan_token(),
        "Content-Type": "application/json",
    }
    security_id = _SECURITY_IDS[instrument_name.upper()]
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": exchange_segment,
        "instrument": "INDEX",
        "interval": str(interval),
        "fromDate": from_date,
        "toDate": to_date,
    }

    for attempt in range(1, 4):
        try:
            resp = requests.post(
                f"{_DHAN_API_BASE}/charts/intraday",
                headers=headers, json=payload, timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                timestamps = data.get("timestamp", [])
                if not timestamps:
                    return pd.DataFrame()
                df = pd.DataFrame({
                    "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
                    "open": data.get("open", []),
                    "high": data.get("high", []),
                    "low": data.get("low", []),
                    "close": data.get("close", []),
                    "volume": data.get("volume", [0] * len(timestamps)),
                })
                df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
                return df
            elif resp.status_code in (502, 503, 429):
                wait = attempt * 2
                logger.warning("Dhan intraday %d (attempt %d/3), retrying in %ds...",
                               resp.status_code, attempt, wait)
                time.sleep(wait)
            else:
                logger.warning("Dhan intraday API %d: %s", resp.status_code, resp.text[:300])
                return pd.DataFrame()
        except Exception:
            logger.exception("Dhan intraday API call failed (attempt %d/3)", attempt)
            if attempt < 3:
                time.sleep(attempt * 2)

    return pd.DataFrame()


# Max days per intraday API call (Dhan limit is 90)
_MAX_INTRADAY_DAYS = 85


def _fetch_index_intraday_api(
    dhan_creds: dict,
    instrument_name: str,
    interval: int,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    """Fetch intraday OHLCV candles from Dhan API (no cache).

    Handles 85-day chunking automatically (Dhan limit is 90 days).
    """
    security_id = _SECURITY_IDS.get(instrument_name.upper())
    if security_id is None:
        raise ValueError(f"Unknown instrument: {instrument_name}")

    exchange_segment = _INDEX_SEGMENTS[instrument_name.upper()]

    dt_from = datetime.strptime(from_date, "%Y-%m-%d %H:%M:%S")
    dt_to = datetime.strptime(to_date, "%Y-%m-%d %H:%M:%S")

    total_days = (dt_to - dt_from).days
    if total_days <= _MAX_INTRADAY_DAYS:
        logger.info("Dhan intraday fetch: %s %dmin %s -> %s",
                    instrument_name, interval, from_date, to_date)
        df = _fetch_intraday_chunk(
            dhan_creds, instrument_name, exchange_segment,
            interval, from_date, to_date,
        )
        if not df.empty:
            df.sort_values("timestamp", inplace=True)
            df.reset_index(drop=True, inplace=True)
        return df

    logger.info("Dhan intraday fetch: %s %dmin %s -> %s (%d days, chunking)",
                instrument_name, interval, from_date, to_date, total_days)

    all_dfs = []
    chunk_start = dt_from

    while chunk_start < dt_to:
        chunk_end = min(chunk_start + timedelta(days=_MAX_INTRADAY_DAYS), dt_to)
        chunk_from_str = chunk_start.strftime("%Y-%m-%d %H:%M:%S")
        chunk_to_str = chunk_end.strftime("%Y-%m-%d %H:%M:%S")

        df_chunk = _fetch_intraday_chunk(
            dhan_creds, instrument_name, exchange_segment,
            interval, chunk_from_str, chunk_to_str,
        )
        if not df_chunk.empty:
            all_dfs.append(df_chunk)

        chunk_start = chunk_end
        time.sleep(_RATE_LIMIT_DELAY)

    if not all_dfs:
        return pd.DataFrame()

    result = pd.concat(all_dfs, ignore_index=True)
    result.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
    result.sort_values("timestamp", inplace=True)
    result.reset_index(drop=True, inplace=True)
    return result


def fetch_index_intraday(
    dhan_creds: dict,
    instrument_name: str,
    interval: int,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    """Fetch intraday OHLCV candles with SQLite caching.

    Checks cache first, fetches from API only if cache miss.
    """
    instrument = instrument_name.upper()
    from_date_short = from_date.split(" ")[0]
    to_date_short = to_date.split(" ")[0]

    cached = _load_spot_cached(
        instrument, "intraday", interval, from_date_short, to_date_short,
    )
    if not cached.empty:
        cache_start = cached["timestamp"].min().strftime("%Y-%m-%d")
        cache_end = cached["timestamp"].max().strftime("%Y-%m-%d")
        if cache_start <= from_date_short and cache_end >= to_date_short:
            logger.info("Spot intraday cache hit: %s %dmin (%d candles)",
                        instrument, interval, len(cached))
            print(f"  Spot {interval}-min: {len(cached)} candles (cached)")
            return cached

    fresh = _fetch_index_intraday_api(
        dhan_creds, instrument_name, interval, from_date, to_date,
    )
    if not fresh.empty:
        _save_spot_to_cache(instrument, "intraday", interval, fresh)
        logger.info("Dhan intraday: got %d candles for %s (saved to cache)",
                    len(fresh), instrument)

    if not cached.empty and not fresh.empty:
        combined = pd.concat([cached, fresh], ignore_index=True)
        combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
        combined.sort_values("timestamp", inplace=True)
        combined.reset_index(drop=True, inplace=True)
        return combined

    return fresh if not fresh.empty else cached


def _fetch_index_daily_api(
    dhan_creds: dict,
    instrument_name: str,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    """Fetch daily OHLCV candles from Dhan API (no cache)."""
    security_id = _SECURITY_IDS.get(instrument_name.upper())
    if security_id is None:
        raise ValueError(f"Unknown instrument: {instrument_name}")

    exchange_segment = _INDEX_SEGMENTS[instrument_name.upper()]
    headers = {
        "access-token": _get_dhan_token(),
        "Content-Type": "application/json",
    }

    payload = {
        "securityId": str(security_id),
        "exchangeSegment": exchange_segment,
        "instrument": "INDEX",
        "expiryCode": 0,
        "fromDate": from_date,
        "toDate": to_date,
    }

    logger.info("Dhan daily fetch: %s %s -> %s",
                instrument_name, from_date, to_date)

    for attempt in range(1, 4):
        try:
            resp = requests.post(
                f"{_DHAN_API_BASE}/charts/historical",
                headers=headers, json=payload, timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                timestamps = data.get("timestamp", [])
                if not timestamps:
                    return pd.DataFrame()
                df = pd.DataFrame({
                    "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
                    "open": data.get("open", []),
                    "high": data.get("high", []),
                    "low": data.get("low", []),
                    "close": data.get("close", []),
                    "volume": data.get("volume", [0] * len(timestamps)),
                })
                df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
                df.sort_values("timestamp", inplace=True)
                df.reset_index(drop=True, inplace=True)
                return df
            elif resp.status_code in (502, 503, 429):
                wait = attempt * 2
                logger.warning("Dhan daily %d (attempt %d/3), retrying in %ds...",
                               resp.status_code, attempt, wait)
                time.sleep(wait)
            else:
                logger.warning("Dhan historical API %d: %s", resp.status_code, resp.text[:300])
                return pd.DataFrame()
        except Exception:
            logger.exception("Dhan historical API call failed (attempt %d/3)", attempt)
            if attempt < 3:
                time.sleep(attempt * 2)

    return pd.DataFrame()


def fetch_index_daily(
    dhan_creds: dict,
    instrument_name: str,
    from_date: str,
    to_date: str,
) -> pd.DataFrame:
    """Fetch daily OHLCV candles with SQLite caching.

    Checks cache first, fetches from API only if cache miss.
    """
    instrument = instrument_name.upper()

    cached = _load_spot_cached(instrument, "daily", 0, from_date, to_date)
    if not cached.empty:
        cache_start = cached["timestamp"].min().strftime("%Y-%m-%d")
        cache_end = cached["timestamp"].max().strftime("%Y-%m-%d")
        if cache_start <= from_date and cache_end >= to_date:
            logger.info("Spot daily cache hit: %s (%d candles)",
                        instrument, len(cached))
            print(f"  Spot daily: {len(cached)} candles (cached)")
            return cached

    fresh = _fetch_index_daily_api(dhan_creds, instrument_name, from_date, to_date)
    if not fresh.empty:
        _save_spot_to_cache(instrument, "daily", 0, fresh)
        logger.info("Dhan daily: got %d candles for %s (saved to cache)",
                    len(fresh), instrument)

    if not cached.empty and not fresh.empty:
        combined = pd.concat([cached, fresh], ignore_index=True)
        combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
        combined.sort_values("timestamp", inplace=True)
        combined.reset_index(drop=True, inplace=True)
        return combined

    return fresh if not fresh.empty else cached


def extract_spot_candles(option_data: dict) -> pd.DataFrame:
    """Extract spot price candles from the ATM option data.

    Uses the 'spot' column from the ATM CALL data (spot is the same
    for CE and PE at any given timestamp).

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
        where OHLC are the spot prices.
    """
    atm_df = option_data.get(("ATM", "CALL"))
    if atm_df is None or atm_df.empty:
        atm_df = option_data.get(("ATM", "PUT"))

    if atm_df is None or atm_df.empty:
        raise ValueError("No ATM data available to extract spot candles")

    # The spot column gives us the spot close at each candle
    # For proper OHLC we'd need spot OHLC, but Dhan only gives spot close
    # Use it as close; for indicators this is sufficient
    spot_df = pd.DataFrame({
        "timestamp": atm_df["timestamp"],
        "open": atm_df["spot"],
        "high": atm_df["spot"],
        "low": atm_df["spot"],
        "close": atm_df["spot"],
        "volume": 0,
    })

    return spot_df


# ---------------------------------------------------------------------------
# Gap-aware incremental fetch helpers
# ---------------------------------------------------------------------------

def _get_cached_date_range(instrument: str, table: str, **filters) -> tuple[str | None, str | None]:
    """Return (min_date, max_date) of existing cache for the given filters, or (None, None)."""
    conn = _get_cache_db()
    where_parts = ["instrument=?"] + [f"{k}=?" for k in filters]
    where = " AND ".join(where_parts)
    row = conn.execute(
        f"SELECT MIN(timestamp), MAX(timestamp) FROM {table} WHERE {where}",
        (instrument.upper(), *filters.values()),
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return None, None
    return row[0][:10], row[1][:10]


def _resolve_incremental_range(
    cached_min: str | None,
    cached_max: str | None,
    req_from: str,
    req_to: str,
) -> tuple[str, str] | None:
    """Given cache extent and requested range, return the gap to actually fetch.

    Returns None if cache already covers the request. Currently optimizes the
    common tail-extension case (cache covers a prefix of the request); other
    gap shapes fall back to fetching the full requested range, which is still
    safe because all writes go through INSERT OR IGNORE.
    """
    if cached_min is None:
        return req_from, req_to
    if cached_max >= req_to and cached_min <= req_from:
        return None  # full cache hit
    if cached_max < req_to and cached_min <= req_from:
        new_from = (datetime.strptime(cached_max, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        return new_from, req_to
    return req_from, req_to


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description="Fetch Dhan historical spot + option data into the SQLite cache "
                    "consumed by backtester.py. Gap-aware: by default only fetches the "
                    "missing tail when the cache already contains older data.",
    )
    parser.add_argument("--instrument", default="NIFTY", choices=["NIFTY", "BANKNIFTY", "SENSEX"])
    parser.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--spot-interval", type=int, default=15, help="Spot candle interval in minutes")
    parser.add_argument("--option-interval", type=int, default=5, help="Option candle interval (backtester reads 5)")
    parser.add_argument(
        "--expiry-codes", default="1,2",
        help="Comma-separated Dhan expiryCodes to fetch (1=current weekly, 2=next weekly). "
             "Default 1,2 covers both — needed so backtest can mirror live's expiry-day-rollover behavior.",
    )
    parser.add_argument("--full", action="store_true", help="Disable gap detection; fetch the full requested range")
    parser.add_argument("--skip-spot", action="store_true")
    parser.add_argument("--skip-options", action="store_true")
    args = parser.parse_args()

    creds = init_dhan()
    inst = args.instrument
    expiry_flag = _EXPIRY_FLAGS[inst]
    expiry_codes = tuple(int(c) for c in args.expiry_codes.split(",") if c.strip())

    if not args.skip_spot:
        if args.full:
            spot_range = (args.from_date, args.to_date)
        else:
            cmin, cmax = _get_cached_date_range(
                inst, "dhan_spot_candle",
                candle_type="intraday", interval_min=args.spot_interval,
            )
            spot_range = _resolve_incremental_range(cmin, cmax, args.from_date, args.to_date)
        if spot_range is None:
            print(f"Spot {inst} {args.spot_interval}m: cache already covers {args.from_date}..{args.to_date}")
        else:
            print(f"Spot {inst} {args.spot_interval}m: fetching {spot_range[0]}..{spot_range[1]}")
            fetch_index_intraday(
                creds, inst, args.spot_interval,
                f"{spot_range[0]} 09:15:00", f"{spot_range[1]} 15:30:00",
            )

    if not args.skip_options:
        for code in expiry_codes:
            if args.full:
                opt_range = (args.from_date, args.to_date)
            else:
                cmin, cmax = _get_cached_date_range(
                    inst, "dhan_option_candle",
                    strike_offset="ATM", direction="CALL",
                    expiry_flag=expiry_flag, expiry_code=code,
                    interval_min=args.option_interval,
                )
                opt_range = _resolve_incremental_range(cmin, cmax, args.from_date, args.to_date)
            if opt_range is None:
                print(f"Options {inst} code={code}: cache already covers {args.from_date}..{args.to_date}")
            else:
                print(f"Options {inst} code={code}: fetching {opt_range[0]}..{opt_range[1]} (30 strike/direction streams)")
                prefetch_option_data(
                    creds, inst, expiry_flag, args.option_interval,
                    opt_range[0], opt_range[1], expiry_codes=(code,),
                )

    print(f"Done. DB: {_DHAN_CACHE_DB}")
