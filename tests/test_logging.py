"""Logging setup: a real rotating file sink, and no reliance on a console."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from engine.utils.logging_setup import LOG_FILE_NAME, RETENTION, ROTATION, setup_logging


def test_setup_logging_creates_the_directory_and_writes_a_file(tmp_path: Path):
    log_dir = tmp_path / "logs"
    returned = setup_logging(log_dir=log_dir, level="DEBUG")

    assert returned == log_dir
    assert log_dir.is_dir()

    logger.info("a line that must reach the file")
    logger.complete()  # flush the enqueued sink

    files = list(log_dir.glob("*.log"))
    assert files, "no log file was written"
    assert "a line that must reach the file" in files[0].read_text(encoding="utf-8")


def test_calling_setup_twice_does_not_duplicate_lines(tmp_path: Path):
    log_dir = tmp_path / "logs"
    setup_logging(log_dir=log_dir, level="INFO")
    setup_logging(log_dir=log_dir, level="INFO")

    logger.info("written once")
    logger.complete()

    text = "".join(f.read_text(encoding="utf-8") for f in log_dir.glob("*.log"))
    assert text.count("written once") == 1


def test_rotation_policy_is_set():
    """10 MB, keep 5 files. A long run must not fill the user's disk."""
    assert ROTATION == "10 MB"
    assert RETENTION == 5
    assert LOG_FILE_NAME.endswith(".log")


def test_level_filtering_is_applied(tmp_path: Path):
    log_dir = tmp_path / "logs"
    setup_logging(log_dir=log_dir, level="WARNING")

    logger.debug("this must not appear")
    logger.warning("this must appear")
    logger.complete()

    text = "".join(f.read_text(encoding="utf-8") for f in log_dir.glob("*.log"))
    assert "this must not appear" not in text
    assert "this must appear" in text
