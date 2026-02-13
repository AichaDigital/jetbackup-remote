"""Logging configuration for jetbackup-remote."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(
    log_file: str = "/var/log/jetbackup-remote.log",
    max_bytes: int = 10485760,
    backup_count: int = 5,
    console: bool = True,
    verbose: bool = False,
) -> logging.Logger:
    """Configure application logging with rotation.

    Args:
        log_file: Path to log file.
        max_bytes: Max size before rotation (default 10MB).
        backup_count: Number of rotated files to keep.
        console: Also log to stderr.
        verbose: Use DEBUG level (default INFO).

    Returns:
        Configured root logger for the application.
    """
    logger = logging.getLogger("jetbackup_remote")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Clear existing handlers
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler with rotation
    log_path = Path(log_file)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except (PermissionError, OSError) as e:
        # Fall back to console-only if file is not writable
        if console:
            pass  # Console handler will be added below
        else:
            raise

    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger
