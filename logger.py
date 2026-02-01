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
    log_prefix: Optional[str] = None,
):
    """Configure root logger if not already configured.

    Args:
        level: Log level (e.g., 'INFO', 'DEBUG')
        log_file: Path to log file
        max_bytes: Max file size before rotation
        backup_count: Number of backup files to keep
        log_prefix: Optional prefix for log file name (e.g., 'account1')

    This is intentionally simple for a small project.
    """
    if logging.root.handlers:
        return

    env_level = level or os.getenv("LOG_LEVEL", "INFO")
    
    # 处理日志文件路径和前缀
    if log_prefix:
        # 如果指定了前缀，修改日志文件名
        default_log = f"logs/{log_prefix}_market_maker.log"
    else:
        default_log = "logs/market_maker.log"
    
    env_file = log_file or os.getenv("LOG_FILE", default_log)
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
