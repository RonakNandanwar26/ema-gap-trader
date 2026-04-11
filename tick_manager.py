"""tick_manager.py — Thread-safe WebSocket LTP cache using SmartAPI SmartWebSocketV2.

Subscribes to option + spot tokens, caches latest LTP.
Used by trader.py for real-time exit monitoring between candle boundaries.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

# Exchange types for SmartWebSocketV2
EXCHANGE_NSE_CM = 1   # NSE Cash (for spot index)
EXCHANGE_NSE_FO = 2   # NSE F&O (for NIFTY/BANKNIFTY options)
EXCHANGE_BSE_FO = 4   # BSE F&O (for SENSEX options)


class TickManager:
    """Thread-safe WebSocket wrapper for real-time LTP monitoring."""

    def __init__(self, auth_token: str, api_key: str, client_code: str, feed_token: str):
        self._auth_token = auth_token
        self._api_key = api_key
        self._client_code = client_code
        self._feed_token = feed_token

        self._lock = threading.Lock()
        self._ltp_cache: dict[str, float] = {}  # token -> latest LTP
        self._last_update: dict[str, datetime] = {}  # token -> timestamp
        self._subscriptions: list[dict] = []  # for resubscribe on reconnect

        self._sws = None
        self._thread: threading.Thread | None = None
        self._connected = False

    def start(self) -> None:
        """Launch WebSocket in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return

        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2
            self._sws = SmartWebSocketV2(
                self._auth_token,
                self._api_key,
                self._client_code,
                self._feed_token,
                max_retry_attempt=5,
                retry_strategy=1,  # exponential backoff
                retry_delay=5,
                retry_multiplier=2,
                retry_duration=300,
            )
            self._sws.on_open = self._on_open
            self._sws.on_data = self._on_data
            self._sws.on_error = self._on_error
            self._sws.on_close = self._on_close

            self._thread = threading.Thread(target=self._connect, daemon=True, name="tick-ws")
            self._thread.start()
            logger.info("TickManager WebSocket thread started")
        except Exception:
            logger.exception("Failed to start TickManager")

    def stop(self) -> None:
        """Close WebSocket connection."""
        self._connected = False
        if self._sws is not None:
            try:
                self._sws.close_connection()
            except Exception:
                pass
        logger.info("TickManager stopped")

    def subscribe(self, token: str, exchange_type: int) -> None:
        """Subscribe to a token for LTP updates."""
        sub = {"exchangeType": exchange_type, "tokens": [token]}
        with self._lock:
            # Track for resubscribe on reconnect
            self._subscriptions.append(sub)
            connected = self._connected

        if connected and self._sws:
            try:
                self._sws.subscribe(f"sub_{token}", 1, [sub])  # LTP_MODE = 1
                logger.info("Subscribed to token %s (exchange %d)", token, exchange_type)
            except Exception:
                logger.exception("Subscribe failed for token %s", token)

    def unsubscribe(self, token: str, exchange_type: int) -> None:
        """Unsubscribe from a token."""
        sub = {"exchangeType": exchange_type, "tokens": [token]}
        with self._lock:
            self._subscriptions = [s for s in self._subscriptions
                                   if not (s["exchangeType"] == exchange_type and token in s["tokens"])]
            self._ltp_cache.pop(token, None)
            self._last_update.pop(token, None)
            connected = self._connected

        if connected and self._sws:
            try:
                self._sws.unsubscribe(f"unsub_{token}", 1, [sub])
            except Exception:
                pass

    def get_ltp(self, token: str) -> float | None:
        """Get cached LTP for a token. Thread-safe, <1ms."""
        with self._lock:
            return self._ltp_cache.get(token)

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Internal callbacks
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Blocking WebSocket connect (runs in daemon thread)."""
        try:
            self._sws.connect()
        except Exception:
            logger.exception("WebSocket connect failed")
            self._connected = False

    def _on_open(self, wsapp) -> None:
        """Called when WebSocket connects. Resubscribe to all tokens."""
        self._connected = True
        logger.info("WebSocket connected")
        with self._lock:
            subs = list(self._subscriptions)
        for sub in subs:
            try:
                token = sub["tokens"][0]
                self._sws.subscribe(f"resub_{token}", 1, [sub])
            except Exception:
                logger.exception("Resubscribe failed")

    def _on_data(self, wsapp, data: dict) -> None:
        """Called on every tick. Update LTP cache."""
        try:
            token = str(data.get("token", ""))
            ltp_raw = data.get("last_traded_price")
            if token and ltp_raw is not None:
                ltp = ltp_raw / 100.0  # paisa to rupees
                with self._lock:
                    old = self._ltp_cache.get(token)
                    self._ltp_cache[token] = ltp
                    self._last_update[token] = datetime.now()
                # Log first tick and significant moves only (avoid spam)
                if old is None:
                    logger.info("WS first tick: token=%s ltp=%.2f", token, ltp)
                elif abs(ltp - old) > 0.5:
                    logger.debug("WS tick: token=%s ltp=%.2f (was %.2f)", token, ltp, old)
        except Exception:
            logger.exception("Error processing tick data")

    def _on_error(self, wsapp, error) -> None:
        logger.error("WebSocket error: %s", error)
        self._connected = False

    def _on_close(self, wsapp) -> None:
        logger.info("WebSocket closed")
        self._connected = False
