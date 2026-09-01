"""Logging configuration.

One loguru logger for the whole application: a rotating file sink under the
app data `logs` folder, plus a console sink. `print()` is never used anywhere
in the engine — a packaged build has no console to print to.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from engine.settings import get_settings
from engine.storage.paths import get_app_paths

LOG_FILE_NAME = "cqdc_{time:YYYY-MM-DD}.log"
ROTATION = "10 MB"
RETENTION = 5

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
)
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{process}:{thread} | {name}:{function}:{line} - {message}"
)

_configured = False


def setup_logging(log_dir: Path | None = None, level: str | None = None) -> Path:
    """Configure loguru. Returns the directory the log files are written to.

    Safe to call more than once; the second call replaces the sinks rather
    than adding duplicates.
    """
    global _configured

    settings = get_settings()
    resolved_level = (level or settings.log_level).upper()
    directory = log_dir if log_dir is not None else get_app_paths().logs
    directory.mkdir(parents=True, exist_ok=True)

    logger.remove()

    # A packaged build runs with console=False, so stderr may not exist.
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            level=resolved_level,
            format=_CONSOLE_FORMAT,
            colorize=True,
            backtrace=False,
            diagnose=settings.dev_mode,
        )

    logger.add(
        directory / LOG_FILE_NAME,
        level=resolved_level,
        format=_FILE_FORMAT,
        rotation=ROTATION,
        retention=RETENTION,
        encoding="utf-8",
        enqueue=True,  # safe across the Uvicorn thread and the process pool
        backtrace=True,
        diagnose=False,  # never write variable values to disk
    )

    _configured = True
    logger.debug(
        "Logging ready | level={} | dir={} | dev_mode={}",
        resolved_level,
        directory,
        settings.dev_mode,
    )
    return directory


def is_configured() -> bool:
    """True once :func:`setup_logging` has run in this process."""
    return _configured
