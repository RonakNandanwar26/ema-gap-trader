"""broker.py — Angel One SmartAPI: auth, candles, LTP, orders, scrip master."""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

_smart_api: SmartConnect | None = None
_login_time: datetime | None = None
_auth_token: str | None = None
_feed_token: str | None = None
SESSION_MAX_AGE_HOURS = 5

_SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Chunk sizes (SmartAPI 500-candle limit)
_CHUNK_DAYS = {
    "ONE_MINUTE": 1, "FIVE_MINUTE": 8, "FIFTEEN_MINUTE": 27,
    "THIRTY_MINUTE": 55, "ONE_HOUR": 100, "ONE_DAY": 700,
}

_INTERVAL_MAP = {5: "FIVE_MINUTE", 15: "FIFTEEN_MINUTE"}


def login() -> SmartConnect:
    """Authenticate with Angel One SmartAPI using TOTP."""
    global _smart_api, _login_time, _auth_token, _feed_token

    api_key = os.getenv("API_KEY", "")
    secret = os.getenv("SECRET_KEY", "")
    client_id = os.getenv("CLIENT_ID", "")
    password = os.getenv("PASSWORD", "")
    totp_secret = os.getenv("TOTP_SECRET", "")

    obj = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(totp_secret).now()

    data = obj.generateSession(client_id, password, totp)
    if data is None or data.get("status") is False:
        raise RuntimeError(f"SmartAPI login failed: {data}")

    # Store WebSocket credentials
    session_data = data.get("data", {})
    _auth_token = session_data.get("jwtToken")
    _feed_token = session_data.get("feedToken")
    logger.debug("Login response keys: %s", list(session_data.keys()) if session_data else "None")
    logger.debug("jwtToken present: %s, feedToken present: %s", _auth_token is not None, _feed_token is not None)

    _smart_api = obj
    _login_time = datetime.now()
    logger.info("SmartAPI login successful for %s", client_id)
    return obj


def get_feed_credentials() -> tuple[str, str, str, str]:
    """Return (auth_token, feed_token, api_key, client_code) for WebSocket."""
    if _auth_token is None or _feed_token is None:
        raise RuntimeError("Login first — feed credentials not available")
    return _auth_token, _feed_token, os.getenv("API_KEY", ""), os.getenv("CLIENT_ID", "")


def get_api() -> SmartConnect:
    """Get cached SmartAPI session, auto-refresh if stale."""
    global _smart_api, _login_time
    if _smart_api is not None and _login_time is not None:
        age = (datetime.now() - _login_time).total_seconds() / 3600
        if age < SESSION_MAX_AGE_HOURS:
            return _smart_api
    return login()


def _refresh() -> SmartConnect:
    """Force fresh login."""
    global _smart_api
    _smart_api = None
    time.sleep(3)
    return login()


# ---------------------------------------------------------------------------
# Candle fetching
# ---------------------------------------------------------------------------

def fetch_candles(token: str, exchange: str, interval_min: int, from_date: str, to_date: str) -> pd.DataFrame:
    """Fetch OHLCV candles from SmartAPI with auto-chunking."""
    interval = _INTERVAL_MAP.get(interval_min, "FIVE_MINUTE")
    chunk_days = _CHUNK_DAYS.get(interval, 8)

    start = datetime.strptime(from_date, "%Y-%m-%d %H:%M") if " " in from_date else datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d %H:%M") if " " in to_date else datetime.strptime(to_date, "%Y-%m-%d") + timedelta(hours=15, minutes=30)

    all_rows = []
    current = start
    while current < end:
        chunk_end = min(current + timedelta(days=chunk_days), end)
        rows = _fetch_chunk(token, exchange, interval, current, chunk_end)
        all_rows.extend(rows)
        current = chunk_end + timedelta(minutes=1)
        time.sleep(0.35)

    if not all_rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


def _fetch_chunk(token: str, exchange: str, interval: str, start: datetime, end: datetime) -> list[dict]:
    """Fetch a single chunk of candles."""
    obj = get_api()
    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }
    try:
        resp = obj.getCandleData(params)
    except Exception:
        obj = _refresh()
        resp = obj.getCandleData(params)

    if resp is None or resp.get("data") is None:
        return []

    rows = []
    for r in resp["data"]:
        rows.append({
            "timestamp": r[0], "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": int(r[5]),
        })
    return rows


# ---------------------------------------------------------------------------
# LTP
# ---------------------------------------------------------------------------

def fetch_ltp(exchange: str, symbol: str, token: str) -> float | None:
    """Get last traded price for an option contract."""
    obj = get_api()
    try:
        resp = obj.ltpData(exchange, symbol, token)
        if resp and resp.get("status") and resp.get("data"):
            ltp = float(resp["data"]["ltp"])
            logger.debug("LTP %s = Rs.%.1f", symbol, ltp)
            return ltp
        logger.warning("LTP response empty for %s: %s", symbol, resp)
    except Exception:
        logger.exception("LTP fetch failed for %s", symbol)
    return None


# ---------------------------------------------------------------------------
# Order execution (live trading only)
# ---------------------------------------------------------------------------

def place_order(params: dict) -> str | None:
    """Place a market order. Returns order_id or None."""
    obj = get_api()
    try:
        order_id = obj.placeOrder(params)
    except Exception:
        obj = _refresh()
        try:
            order_id = obj.placeOrder(params)
        except Exception:
            logger.exception("Order placement failed")
            return None

    if order_id is None:
        logger.error("placeOrder returned None")
        return None
    return str(order_id)


def verify_order(order_id: str, max_retries: int = 3) -> dict | None:
    """Poll until order is filled. Returns {"status": "complete", "averageprice": float} or None."""
    obj = get_api()
    for attempt in range(1 + max_retries):
        try:
            resp = obj.individual_order_details(order_id)
            if isinstance(resp, list):
                for item in resp:
                    if str(item.get("orderid")) == order_id:
                        resp = item
                        break
            if isinstance(resp, dict):
                status = resp.get("orderstatus", resp.get("status", ""))
                if status == "complete":
                    return resp
                if status == "rejected":
                    logger.error("Order %s rejected: %s", order_id, resp.get("text", ""))
                    return None
        except Exception:
            logger.exception("Order verify attempt %d failed", attempt)
        if attempt < max_retries:
            time.sleep(2)
    return None


# ---------------------------------------------------------------------------
# Scrip master
# ---------------------------------------------------------------------------

def load_scrip_master() -> dict:
    """Download Angel One scrip master, return lookup dict."""
    logger.info("Downloading scrip master...")
    resp = requests.get(_SCRIP_MASTER_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    master: dict[tuple, dict] = {}
    valid_names = {"NIFTY", "BANKNIFTY", "SENSEX"}
    valid_exchanges = {"NFO", "BFO"}

    for item in data:
        if item.get("instrumenttype") != "OPTIDX":
            continue
        if item.get("exch_seg", "") not in valid_exchanges:
            continue
        name = item.get("name", "")
        if name not in valid_names:
            continue

        expiry_str = item.get("expiry", "")
        if not expiry_str:
            continue
        try:
            expiry = datetime.strptime(expiry_str, "%d%b%Y").date()
        except ValueError:
            continue

        try:
            strike = int(float(item.get("strike", "0")) / 100)
        except (ValueError, TypeError):
            continue
        if strike <= 0:
            continue

        symbol = item.get("symbol", "")
        if symbol.endswith("CE"):
            direction = "CE"
        elif symbol.endswith("PE"):
            direction = "PE"
        else:
            continue

        master[(name, strike, direction, expiry)] = {
            "symbol": symbol,
            "token": item.get("token", ""),
            "lot_size": int(item.get("lotsize", "0")),
        }

    logger.info("Scrip master: %d option contracts", len(master))
    return master


def resolve_option(master: dict, instrument: str, strike: int, direction: str, expiry: date) -> tuple[str, str]:
    """Resolve option symbol and token from scrip master."""
    key = (instrument, strike, direction, expiry)
    entry = master.get(key)
    if entry is None:
        raise ValueError(f"Contract not found: {key}")
    return entry["symbol"], entry["token"]


def get_current_expiry(master: dict, instrument: str, today: date) -> date:
    """Find nearest future expiry from scrip master (excluding today)."""
    expiries = sorted({exp for (inst, _, _, exp) in master if inst == instrument and exp > today})
    if expiries:
        return expiries[0]
    # Fallback: next week
    from config import get_instrument_config
    ic = get_instrument_config(instrument)
    days_ahead = (ic.expiry_weekday - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)
