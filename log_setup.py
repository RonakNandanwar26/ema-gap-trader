"""log_setup.py — Configure logging for the entire app.

Call setup_logging() once at startup. Logs to both console and file.
File: logs/trader_YYYY-MM-DD.log
"""

from __future__ import annotations

import logging
import os
from datetime import date


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger with console + daily rotating file handler."""
    os.makedirs("logs", exist_ok=True)
    log_file = f"logs/trader_{date.today().isoformat()}.log"

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)-15s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)

    # File handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)  # file always gets DEBUG
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Clear existing handlers (avoid duplicates on Streamlit rerun)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)

    # Suppress noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("SmartApi").setLevel(logging.WARNING)
    logging.getLogger("websocket").setLevel(logging.WARNING)

    logging.info("Logging initialized → %s", log_file)
