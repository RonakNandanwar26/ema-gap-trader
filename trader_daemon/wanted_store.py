"""WantedStore — persists the list of sessions that ought to be running.

On /start the daemon writes the start request to wanted.json. On /stop it
removes the key. On daemon startup the FastAPI lifespan reads the file
and replays every entry — that's how sessions survive systemd restarts,
EC2 reboots, and the 'always be running' requirement.

Writes are atomic via tempfile + os.replace() so a crash mid-write can't
leave a truncated file. The file is whole-rewritten on every change
(small, O(N) where N is number of active sessions — typically ≤5).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class WantedStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict[str, Any]]:
        """Return {session_key: start_request_dict}. Missing file → empty dict."""
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                raw = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("wanted.json unreadable (%s); ignoring", exc)
                return {}
            if not isinstance(raw, dict):
                log.warning("wanted.json is not an object; ignoring")
                return {}
            return raw

    def add(self, session_key: str, start_request: dict[str, Any]) -> None:
        with self._lock:
            current = self._read_unlocked()
            current[session_key] = start_request
            self._write_unlocked(current)

    def remove(self, session_key: str) -> None:
        with self._lock:
            current = self._read_unlocked()
            if session_key in current:
                del current[session_key]
                self._write_unlocked(current)

    def clear(self) -> None:
        with self._lock:
            self._write_unlocked({})

    # -----------------------------------------------------------------
    # Private — must be called with self._lock held
    # -----------------------------------------------------------------

    def _read_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return raw

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".wanted.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
