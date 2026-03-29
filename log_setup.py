"""log_setup.py — Configure logging for the entire app.

Call setup_logging() once at startup. Logs to console only.
"""

from __future__ import annotations

import logging


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger with console handler only."""
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)-15s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    # Clear existing handlers (avoid duplicates on Streamlit rerun)
    root.handlers.clear()
    root.addHandler(console)

    # Suppress noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("SmartApi").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)
