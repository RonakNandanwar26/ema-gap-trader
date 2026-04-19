from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


SessionMode = Literal["paper", "live"]
SessionStatusEnum = Literal["starting", "running", "stopping", "stopped", "error"]


class StartRequest(BaseModel):
    mode: SessionMode
    instrument: str
    variant: str = ""
    capital: int
    strategy: dict[str, Any] = Field(
        default_factory=dict,
        description="StrategyConfig as a dict; daemon rehydrates via StrategyConfig(**payload).",
    )
    instrument_config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "InstrumentConfig as a dict (lot_size may already be multiplied by the UI "
            "lot-multiplier). If empty, daemon falls back to config.INSTRUMENTS[instrument]."
        ),
    )


class StartResponse(BaseModel):
    session_key: str
    status: SessionStatusEnum


class SessionStatus(BaseModel):
    session_key: str
    mode: SessionMode
    instrument: str
    variant: str
    status: SessionStatusEnum
    is_running: bool
    current_equity: float
    initial_capital: float
    trade_count: int
    open_trade: Optional[dict[str, Any]] = None
    recent_trades: list[dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = None
    started_at: Optional[str] = None


class SessionListResponse(BaseModel):
    sessions: list[SessionStatus]


class TradesResponse(BaseModel):
    session_key: str
    recent_trades: list[dict[str, Any]]


class HealthResponse(BaseModel):
    status: Literal["ok"]
    broker_logged_in: bool
    sessions: int
    uptime_sec: int


def build_session_key(mode: SessionMode, instrument: str, variant: str = "") -> str:
    return f"{mode}:{instrument}:{variant}" if variant else f"{mode}:{instrument}"
