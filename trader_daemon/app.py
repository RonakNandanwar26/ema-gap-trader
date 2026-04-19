from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, status

from config import INSTRUMENTS, InstrumentConfig, StrategyConfig, get_data_dir

from .schemas import (
    HealthResponse,
    SessionListResponse,
    SessionStatus,
    StartRequest,
    StartResponse,
    TradesResponse,
    build_session_key,
)
from .session_manager import SessionManager, _SessionEntry
from .wanted_store import WantedStore

log = logging.getLogger(__name__)

_STARTED_AT = time.time()
_MANAGER = SessionManager()
_WANTED = WantedStore(Path(get_data_dir()) / "wanted.json")


def _rehydrate_ic(instrument: str, overrides: dict) -> InstrumentConfig:
    base = INSTRUMENTS.get(instrument)
    if base is None:
        raise HTTPException(status_code=400, detail=f"unknown instrument '{instrument}'")
    if not overrides:
        return base
    # Merge base + overrides so callers can ship partial dicts (e.g. only lot_size).
    merged = {**base.__dict__, **overrides}
    try:
        return InstrumentConfig(**merged)
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid instrument_config: {exc}") from exc


def _rehydrate_sc(payload: dict) -> StrategyConfig:
    if not payload:
        raise HTTPException(status_code=400, detail="strategy config is required")
    try:
        return StrategyConfig(**payload)
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid strategy: {exc}") from exc


def _entry_to_status(entry: _SessionEntry) -> SessionStatus:
    return SessionStatus(**_MANAGER.snapshot(entry))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Startup: resume every wanted session.
    wanted = _WANTED.load()
    for session_key, payload in wanted.items():
        try:
            req = StartRequest(**payload)
            sc = _rehydrate_sc(req.strategy)
            ic = _rehydrate_ic(req.instrument, req.instrument_config)
            _MANAGER.start(
                session_key=session_key,
                mode=req.mode,
                instrument=req.instrument,
                variant=req.variant,
                capital=req.capital,
                sc=sc,
                ic=ic,
            )
            log.info("auto-resumed %s", session_key)
        except Exception:
            log.exception("failed to auto-resume %s; skipping", session_key)
    yield
    # Shutdown: stop all sessions so systemd's TimeoutStopSec has a bounded deadline.
    log.info("shutdown: stopping all sessions")
    _MANAGER.shutdown_all(join_timeout=5.0)


app = FastAPI(title="ema-trader-daemon", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(
        status="ok",
        broker_logged_in=False,  # populated in Phase D via broker._smart_api check
        sessions=len(_MANAGER.list()),
        uptime_sec=int(time.time() - _STARTED_AT),
    )


@app.get("/sessions", response_model=SessionListResponse)
def list_sessions() -> SessionListResponse:
    return SessionListResponse(sessions=[_entry_to_status(e) for e in _MANAGER.list()])


@app.get("/sessions/{session_key}", response_model=SessionStatus)
def get_session(session_key: str) -> SessionStatus:
    entry = _MANAGER.get(session_key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"session '{session_key}' not found")
    return _entry_to_status(entry)


@app.post(
    "/sessions/start",
    response_model=StartResponse,
)
def start_session(
    req: StartRequest,
    x_confirmed_live: Optional[str] = Header(default=None, alias="X-Confirmed-Live"),
):
    if req.mode == "live" and x_confirmed_live != "true":
        raise HTTPException(
            status_code=400,
            detail="live mode requires 'X-Confirmed-Live: true' header",
        )
    session_key = build_session_key(req.mode, req.instrument, req.variant)
    sc = _rehydrate_sc(req.strategy)
    ic = _rehydrate_ic(req.instrument, req.instrument_config)

    entry, created = _MANAGER.start(
        session_key=session_key,
        mode=req.mode,
        instrument=req.instrument,
        variant=req.variant,
        capital=req.capital,
        sc=sc,
        ic=ic,
    )

    if not created:
        # Already running — return 409 so the UI can be idempotent without error.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"session_key": session_key, "status": "running"},
        )

    _WANTED.add(session_key, req.model_dump())
    return StartResponse(session_key=session_key, status="starting")


@app.post("/sessions/{session_key}/stop", response_model=StartResponse)
def stop_session(session_key: str):
    if not _MANAGER.stop(session_key):
        raise HTTPException(status_code=404, detail=f"session '{session_key}' not found")
    _WANTED.remove(session_key)
    return StartResponse(session_key=session_key, status="stopping")


@app.get("/sessions/{session_key}/trades", response_model=TradesResponse)
def get_session_trades(session_key: str, limit: int = 5) -> TradesResponse:
    entry = _MANAGER.get(session_key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"session '{session_key}' not found")
    return TradesResponse(
        session_key=session_key,
        recent_trades=_MANAGER.recent_trades(entry, limit=limit),
    )


# -- Testing hooks -----------------------------------------------------------
# Exposed so unit tests can swap in a mock SessionManager / WantedStore.


def _reset_for_tests(manager: SessionManager, wanted: WantedStore) -> None:
    """Replace module-level singletons. Used ONLY from pytest fixtures."""
    global _MANAGER, _WANTED
    _MANAGER = manager
    _WANTED = wanted
