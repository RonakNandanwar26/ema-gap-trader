"""RemoteSession — drop-in replacement for app.py's TradingSession that
delegates to the trader daemon over loopback HTTP.

The surface area matches TradingSession exactly so app.py can swap
classes behind a feature flag without touching any call sites:

- ``start()``, ``stop()``, ``is_running`` (property), ``get_status()``
- constructor ``RemoteSession(sc, ic, capital, live=False, variant="")``

Internal state is cached for 1s to avoid hammering the daemon during
Streamlit's 3s auto-rerun loop (several calls per second across tabs).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from typing import Any

import requests

# We import TradingStatus from app.py-adjacent code so the shape stays
# identical. Importing the dataclass directly from app.py creates a circular
# import via Streamlit, so we redefine it here with the same fields.
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_DAEMON_URL = os.environ.get("TRADER_DAEMON_URL", "http://127.0.0.1:8787")
DEFAULT_TIMEOUT_SEC = float(os.environ.get("TRADER_DAEMON_TIMEOUT_SEC", "2"))
STATUS_CACHE_TTL_SEC = float(os.environ.get("TRADER_DAEMON_CACHE_TTL_SEC", "1"))


@dataclass
class TradingStatus:
    """Mirror of app.TradingStatus — declared here to avoid a circular import."""

    is_running: bool
    current_equity: float
    initial_capital: float
    trade_count: int
    open_trade: Optional[dict]
    recent_trades: list[dict] = field(default_factory=list)
    error: Optional[str] = None


def daemon_is_reachable(url: str = DEFAULT_DAEMON_URL, timeout: float = 1.0) -> bool:
    """Cheap health check used by app.py to render a 'daemon down' banner."""
    try:
        r = requests.get(f"{url}/healthz", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _session_key(instrument: str, variant: str, live: bool) -> str:
    mode = "live" if live else "paper"
    return f"{mode}:{instrument}:{variant}" if variant else f"{mode}:{instrument}"


class RemoteSession:
    """HTTP client mirroring TradingSession's public API."""

    def __init__(
        self,
        strat_config: Any,
        inst_config: Any,
        capital: int,
        live: bool = False,
        variant: str = "",
        *,
        daemon_url: str = DEFAULT_DAEMON_URL,
    ) -> None:
        self.sc = strat_config
        self.ic = inst_config
        self.capital = capital
        self.live = live
        self.variant = variant
        self._mode = "live" if live else "paper"
        self._daemon_url = daemon_url.rstrip("/")
        self._session_key = _session_key(inst_config.name, variant, live)
        self._status_cache: dict | None = None
        self._status_cache_at: float = 0.0
        self._cache_lock = threading.Lock()
        self._last_error: str | None = None

    # -----------------------------------------------------------------
    # Public API — matches TradingSession
    # -----------------------------------------------------------------

    def start(self) -> None:
        payload = {
            "mode": self._mode,
            "instrument": self.ic.name,
            "variant": self.variant,
            "capital": self.capital,
            "strategy": dataclasses.asdict(self.sc),
            "instrument_config": dataclasses.asdict(self.ic),
        }
        headers = {"X-Confirmed-Live": "true"} if self.live else {}
        try:
            r = requests.post(
                f"{self._daemon_url}/sessions/start",
                json=payload,
                headers=headers,
                timeout=DEFAULT_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            self._last_error = f"daemon unreachable: {exc}"
            log.warning("start failed: %s", exc)
            return
        if r.status_code == 409:
            # Already running — treat as success (idempotent).
            self._invalidate_cache()
            return
        if r.status_code not in (200, 202):
            self._last_error = f"daemon returned {r.status_code}: {r.text[:200]}"
            log.warning("start non-202: %s %s", r.status_code, r.text[:200])
            return
        self._last_error = None
        self._invalidate_cache()

    def stop(self) -> None:
        try:
            requests.post(
                f"{self._daemon_url}/sessions/{self._session_key}/stop",
                timeout=DEFAULT_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            log.warning("stop failed: %s", exc)
        self._invalidate_cache()

    @property
    def is_running(self) -> bool:
        status = self._fetch_status()
        if status is None:
            return False
        return bool(status.get("is_running", False))

    def get_status(self) -> TradingStatus:
        data = self._fetch_status()
        if data is None:
            # Daemon unreachable — expose the error via TradingStatus so the UI
            # can render it the same way it renders in-process errors today.
            return TradingStatus(
                is_running=False,
                current_equity=float(self.capital),
                initial_capital=float(self.capital),
                trade_count=0,
                open_trade=None,
                recent_trades=[],
                error=self._last_error or "daemon unreachable",
            )
        return TradingStatus(
            is_running=bool(data.get("is_running", False)),
            current_equity=float(data.get("current_equity", self.capital)),
            initial_capital=float(data.get("initial_capital", self.capital)),
            trade_count=int(data.get("trade_count", 0)),
            open_trade=data.get("open_trade"),
            recent_trades=data.get("recent_trades", []) or [],
            error=data.get("error"),
        )

    # -----------------------------------------------------------------
    # Internal
    # -----------------------------------------------------------------

    def _fetch_status(self) -> dict | None:
        with self._cache_lock:
            now = time.time()
            if self._status_cache is not None and (now - self._status_cache_at) < STATUS_CACHE_TTL_SEC:
                return self._status_cache
        try:
            r = requests.get(
                f"{self._daemon_url}/sessions/{self._session_key}",
                timeout=DEFAULT_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            self._last_error = f"daemon unreachable: {exc}"
            return None
        if r.status_code == 404:
            # Session unknown to daemon — return a stopped-looking snapshot.
            data = {
                "is_running": False, "current_equity": float(self.capital),
                "initial_capital": float(self.capital), "trade_count": 0,
                "open_trade": None, "recent_trades": [], "error": None,
            }
        elif r.status_code == 200:
            data = r.json()
        else:
            self._last_error = f"daemon returned {r.status_code}"
            return None
        with self._cache_lock:
            self._status_cache = data
            self._status_cache_at = time.time()
        return data

    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._status_cache = None
            self._status_cache_at = 0.0
