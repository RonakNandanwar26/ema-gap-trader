"""SessionManager — owns all Trader threads running inside the daemon process.

Mirrors the old TradingSession wrapper in app.py but:
- There is ONE manager per daemon process (not per browser session).
- Sessions live across Streamlit restarts (the daemon is the source of truth).
- `start()` is idempotent by session_key so duplicate calls from the UI
  (F5 mid-start, two browsers racing) don't spawn twos.
- Shutdown joins every thread with a timeout so systemd TimeoutStopSec has
  a real deadline to bound.

Trader itself is unchanged; `SessionManager._run_session` is just a copy of
`TradingSession._run` (app.py:151-156) that updates per-session status.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Optional

from config import InstrumentConfig, StrategyConfig, get_data_dir
from persistence import TradeStore
from trader import Trader

log = logging.getLogger(__name__)


@dataclass
class _SessionEntry:
    session_key: str
    mode: str              # "paper" | "opt" | "live"
    instrument: str
    variant: str
    capital: int
    sc: StrategyConfig
    ic: InstrumentConfig
    live: bool
    store_key: str

    # Lifecycle state (all read/written under SessionManager._lock)
    trader: Optional[Trader] = None
    thread: Optional[threading.Thread] = None
    store: Optional[TradeStore] = None
    status: str = "starting"   # starting | running | stopping | stopped | error
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    user_stopped: bool = False

    @property
    def is_running(self) -> bool:
        if self.user_stopped:
            return False
        return self.thread is not None and self.thread.is_alive()


class SessionManager:
    """Thread-safe registry of Trader instances keyed by session_key."""

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionEntry] = {}
        self._lock = threading.Lock()

    # -----------------------------------------------------------------
    # Query
    # -----------------------------------------------------------------

    def get(self, session_key: str) -> Optional[_SessionEntry]:
        with self._lock:
            return self._sessions.get(session_key)

    def list(self) -> list[_SessionEntry]:
        with self._lock:
            return list(self._sessions.values())

    def has_running(self, session_key: str) -> bool:
        entry = self.get(session_key)
        return entry is not None and entry.is_running

    # -----------------------------------------------------------------
    # Start / Stop
    # -----------------------------------------------------------------

    def start(
        self,
        *,
        session_key: str,
        mode: str,
        instrument: str,
        variant: str,
        capital: int,
        sc: StrategyConfig,
        ic: InstrumentConfig,
    ) -> tuple[_SessionEntry, bool]:
        """Idempotent start. Returns (entry, created) — ``created`` is False if
        the session was already running and we just returned the existing entry.
        """
        live = mode == "live"
        store_key = f"{instrument}-{variant}" if variant else instrument
        with self._lock:
            existing = self._sessions.get(session_key)
            if existing is not None and existing.is_running:
                log.info("start() ignored — %s already running", session_key)
                return existing, False

            entry = _SessionEntry(
                session_key=session_key,
                mode=mode,
                instrument=instrument,
                variant=variant,
                capital=capital,
                sc=sc,
                ic=ic,
                live=live,
                store_key=store_key,
            )
            entry.store = TradeStore(store_key, "live" if live else "paper", get_data_dir())
            entry.trader = Trader(
                sc, ic, capital,
                live=live,
                trade_store=entry.store,
                label=variant,
            )
            entry.thread = threading.Thread(
                target=self._run_session,
                args=(entry,),
                daemon=False,  # daemon=False so shutdown handler can join cleanly
                name=f"trader-{session_key}",
            )
            self._sessions[session_key] = entry
            entry.thread.start()
        log.info("started session %s (mode=%s instrument=%s)", session_key, mode, instrument)
        return entry, True

    def stop(self, session_key: str) -> bool:
        """Request a stop. Returns False if the session is unknown.

        Does NOT join the thread — caller polls status or calls
        ``shutdown_all()`` during daemon shutdown.
        """
        with self._lock:
            entry = self._sessions.get(session_key)
            if entry is None:
                return False
            entry.user_stopped = True
            entry.status = "stopping"
            if entry.trader is not None:
                entry.trader.running = False
        log.info("stop requested: %s", session_key)
        return True

    def shutdown_all(self, join_timeout: float = 5.0) -> None:
        """Set running=False on every Trader and join each thread.

        Called from the FastAPI lifespan shutdown handler. Systemd's
        ``TimeoutStopSec=20`` bounds the outer deadline; we use 5s per
        thread which leaves headroom for multiple sessions.
        """
        with self._lock:
            entries = list(self._sessions.values())
        for entry in entries:
            if entry.trader is not None:
                entry.trader.running = False
            entry.user_stopped = True
            entry.status = "stopping"
        for entry in entries:
            if entry.thread is not None and entry.thread.is_alive():
                entry.thread.join(timeout=join_timeout)
                if entry.thread.is_alive():
                    log.warning("thread %s did not exit in %.1fs", entry.session_key, join_timeout)

    # -----------------------------------------------------------------
    # Snapshotting — called by routes to build SessionStatus responses
    # -----------------------------------------------------------------

    def snapshot(self, entry: _SessionEntry) -> dict[str, Any]:
        """Return the current status as a dict (matches TradingStatus shape)."""
        with self._lock:
            trader = entry.trader
            store = entry.store
            status = entry.status
            error = entry.error
            if entry.is_running:
                status = "running"
            elif entry.status == "stopping":
                # thread already exited but status wasn't updated yet
                status = "stopped"
            if trader is not None and entry.is_running:
                current_equity = trader.current_equity
                initial_capital = trader.initial_capital
                trade_count = len(trader.trades)
                open_trade = copy.deepcopy(trader.open_trade)
                recent_trades = [copy.deepcopy(t) for t in trader.trades[-5:]]
            else:
                # Use disk-backed history for stopped/error sessions.
                historical = store.load_trades() if store is not None else []
                saved_equity = store.load_equity() if store is not None else None
                current_equity = (
                    saved_equity if saved_equity is not None else float(entry.capital)
                )
                initial_capital = float(entry.capital)
                trade_count = len(historical)
                open_trade = None
                recent_trades = historical[-5:]
            return {
                "session_key": entry.session_key,
                "mode": entry.mode,
                "instrument": entry.instrument,
                "variant": entry.variant,
                "status": status,
                "is_running": entry.is_running,
                "current_equity": float(current_equity),
                "initial_capital": float(initial_capital),
                "trade_count": trade_count,
                "open_trade": open_trade,
                "recent_trades": recent_trades,
                "error": error,
                "started_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(entry.started_at)
                ),
            }

    def recent_trades(self, entry: _SessionEntry, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            trader = entry.trader
            store = entry.store
            if trader is not None and entry.is_running:
                return [copy.deepcopy(t) for t in trader.trades[-limit:]]
            historical = store.load_trades() if store is not None else []
            return historical[-limit:]

    # -----------------------------------------------------------------
    # Internal — thread target
    # -----------------------------------------------------------------

    def _run_session(self, entry: _SessionEntry) -> None:
        """Thread body — mirrors TradingSession._run in app.py."""
        with self._lock:
            entry.status = "running"
        try:
            entry.trader.run()
        except Exception:
            tb = traceback.format_exc()
            with self._lock:
                entry.error = tb
                entry.status = "error"
            log.exception("trading session crashed: %s", entry.session_key)
            return
        with self._lock:
            if entry.user_stopped:
                entry.status = "stopped"
            elif entry.status == "running":
                # Trader exited on its own (market close, etc.)
                entry.status = "stopped"
