"""Logging setup (TZ v4 §29 / §3.5).

Rule enforced by convention, not code: never log credentials, tokens,
cookies or session data. This module only sets up *where* logs go and
at what level - callers are responsible for not passing secrets into
log messages in the first place.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def setup_logging(log_dir: str | Path, level: int = logging.INFO) -> None:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers if setup_logging() is called more than once
    # (e.g. in tests that import run.py indirectly).
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
