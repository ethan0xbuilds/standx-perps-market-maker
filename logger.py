"""
Lightweight logging helper for the project.

Provides configure_logging() and get_logger(name).
Defaults to INFO level and RotatingFileHandler writing to logs/market_maker.log.
"""

# 标准库导入
from __future__ import annotations
import logging
import logging.handlers
import os
from typing import Optional


def configure_logging(
    level: Optional[str] = None,
    log_file: Optional[str] = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
):
    """Configure root logger if not already configured.

    This is intentionally simple for a small project.
    """
    if logging.root.handlers:
        return

    env_level = level or os.getenv("LOG_LEVEL", "INFO")
    env_file = log_file or os.getenv("LOG_FILE", "logs/market_maker.log")
    try:
        numeric_level = getattr(logging, env_level.upper(), logging.INFO)
    except Exception:
        numeric_level = logging.INFO

    logger = logging.getLogger()
    logger.setLevel(numeric_level)

    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    formatter = logging.Formatter(fmt)

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(numeric_level)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler (rotating)
    try:
        os.makedirs(os.path.dirname(env_file), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(env_file, maxBytes=max_bytes, backupCount=backup_count)
        fh.setLevel(numeric_level)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception:
        # If file handler cannot be created, continue with console only
        logger.warning("Could not create log file '%s', continuing with console logging", env_file)


def get_logger(name: str):
    """Return a logger; ensure logging configured with defaults if not yet configured."""
    if not logging.root.handlers:
        configure_logging()
    return logging.getLogger(name)
