"""persistence.py — JSON file-based trade state persistence with atomic writes."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Fields that hold datetime objects
_DATETIME_FIELDS = {"entry", "exit", "entry_time", "saved_at"}
_DATE_FIELDS = {"expiry"}


def _serialize(obj: dict) -> dict:
    """Convert datetime/date values to ISO strings for JSON."""
    out = {}
    for k, v in obj.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, date):
            out[k] = v.isoformat()
        else:
            # Handle pandas Timestamp
            try:
                import pandas as pd
                if isinstance(v, pd.Timestamp):
                    out[k] = v.isoformat()
                    continue
            except ImportError:
                pass
            out[k] = v
    return out


def _deserialize(obj: dict) -> dict:
    """Parse ISO strings back to datetime/date for known fields."""
    out = dict(obj)
    for field in _DATETIME_FIELDS:
        if field in out and isinstance(out[field], str):
            try:
                out[field] = datetime.fromisoformat(out[field])
            except (ValueError, TypeError):
                pass
    for field in _DATE_FIELDS:
        if field in out and isinstance(out[field], str):
            try:
                out[field] = date.fromisoformat(out[field])
            except (ValueError, TypeError):
                pass
    return out


def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically: write to .tmp then os.replace."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, path)


class TradeStore:
    """Persistence layer for one instrument+mode pair.

    Files:
        data/{INSTRUMENT}_{mode}_trades.json  — completed trade history
        data/{INSTRUMENT}_{mode}_open.json    — current open position (exists only while open)
    """

    def __init__(self, instrument: str, mode: str, data_dir: str = "data"):
        self._instrument = instrument
        self._mode = mode
        self._dir = Path(data_dir)
        self._lock = threading.RLock()
        self._ensure_dir()

    @property
    def _trades_path(self) -> Path:
        return self._dir / f"{self._instrument}_{self._mode}_trades.json"

    @property
    def _open_path(self) -> Path:
        return self._dir / f"{self._instrument}_{self._mode}_open.json"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write operations (called from Trader thread)
    # ------------------------------------------------------------------

    def save_open_trade(self, trade: dict, current_equity: float) -> None:
        """Persist open trade state to disk. Called after every entry."""
        try:
            with self._lock:
                data = _serialize(trade)
                data["current_equity"] = current_equity
                data["saved_at"] = datetime.now().isoformat()
                _atomic_write(self._open_path, data)
                logger.info("Persisted open trade: %s %s", trade.get("dir"), trade.get("symbol"))
        except Exception:
            logger.exception("Failed to save open trade")

    def clear_open_trade(self) -> None:
        """Remove open trade file. Called after every exit."""
        try:
            with self._lock:
                if self._open_path.exists():
                    self._open_path.unlink()
                    logger.info("Cleared open trade file")
        except Exception:
            logger.exception("Failed to clear open trade file")

    def append_trade(self, trade: dict) -> None:
        """Append a completed trade to the history file. Called after every exit."""
        try:
            with self._lock:
                trades_data = self._load_trades_raw()
                serialized = _serialize(trade)

                # Deduplicate by entry time
                entry_key = serialized.get("entry")
                if entry_key and any(t.get("entry") == entry_key for t in trades_data["trades"]):
                    logger.warning("Duplicate trade skipped (entry=%s)", entry_key)
                    return

                trades_data["trades"].append(serialized)
                _atomic_write(self._trades_path, trades_data)
                logger.info("Appended trade #%d", len(trades_data["trades"]))
        except Exception:
            logger.exception("Failed to append trade")

    def save_equity(self, equity: float) -> None:
        """Update the running equity in the trades file metadata."""
        try:
            with self._lock:
                trades_data = self._load_trades_raw()
                trades_data["current_equity"] = equity
                _atomic_write(self._trades_path, trades_data)
        except Exception:
            logger.exception("Failed to save equity")

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def load_open_trade(self) -> dict | None:
        """Load unclosed position from previous session, or None."""
        try:
            with self._lock:
                if not self._open_path.exists():
                    return None
                text = self._open_path.read_text()
                data = json.loads(text)
                return _deserialize(data)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Corrupt open trade file: %s — renaming to .corrupt", e)
            try:
                corrupt = self._open_path.with_suffix(".corrupt")
                os.replace(self._open_path, corrupt)
            except OSError:
                pass
            return None
        except Exception:
            logger.exception("Failed to load open trade")
            return None

    def load_trades(self) -> list[dict]:
        """Load all historical trades across sessions."""
        try:
            with self._lock:
                trades_data = self._load_trades_raw()
                return [_deserialize(t) for t in trades_data["trades"]]
        except Exception:
            logger.exception("Failed to load trades")
            return []

    def load_equity(self) -> float | None:
        """Load last known equity from the trades file, or None."""
        try:
            with self._lock:
                trades_data = self._load_trades_raw()
                return trades_data.get("current_equity")
        except Exception:
            logger.exception("Failed to load equity")
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_trades_raw(self) -> dict:
        """Load the raw trades JSON, returning default structure if missing/corrupt."""
        if not self._trades_path.exists():
            return {
                "instrument": self._instrument,
                "mode": self._mode,
                "current_equity": None,
                "trades": [],
            }
        try:
            text = self._trades_path.read_text()
            data = json.loads(text)
            if "trades" not in data:
                data["trades"] = []
            return data
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Corrupt trades file: %s — renaming to .corrupt", e)
            try:
                corrupt = self._trades_path.with_suffix(".corrupt")
                os.replace(self._trades_path, corrupt)
            except OSError:
                pass
            return {
                "instrument": self._instrument,
                "mode": self._mode,
                "current_equity": None,
                "trades": [],
            }
