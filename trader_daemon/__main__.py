"""Entry point: ``python -m trader_daemon``.

Launches the FastAPI app on 127.0.0.1:8787. Host/port can be overridden via
TRADER_DAEMON_HOST / TRADER_DAEMON_PORT env vars (useful for tests).
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("TRADER_DAEMON_HOST", "127.0.0.1")
    port = int(os.environ.get("TRADER_DAEMON_PORT", "8787"))
    uvicorn.run(
        "trader_daemon.app:app",
        host=host,
        port=port,
        log_level=os.environ.get("TRADER_DAEMON_LOG_LEVEL", "info"),
        access_log=True,
    )


if __name__ == "__main__":
    main()
