"""Structured logging via structlog with hourly rotating file output."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

import structlog


def setup_logging(log_dir: str = "logs", log_level: str = "DEBUG") -> None:
    """Configure structlog with JSON output to hourly rotating files + stderr."""
    os.makedirs(log_dir, exist_ok=True)

    log_path = os.path.join(log_dir, "dbproxy.jsonl")

    # File handler: hourly rotation, JSON lines
    file_handler = TimedRotatingFileHandler(
        filename=log_path,
        when="H",
        interval=1,
        backupCount=168,  # 7 days of hourly logs
        utc=True,
    )
    file_handler.setLevel(log_level)

    # Stderr handler for console output
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(log_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stderr_handler)

    # WriteLoggerFactory writes to a file object. Use a custom writer
    # that writes to both the log file AND stderr for structured events.
    log_file = open(log_path, "a")  # noqa: SIM115

    class _TeeWriter:
        """Write structured log lines to both file and stderr."""

        def write(self, message: str) -> None:
            log_file.write(message)
            log_file.flush()
            sys.stderr.write(message)

        def flush(self) -> None:
            log_file.flush()
            sys.stderr.flush()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(log_level)
        ),
        context_class=dict,
        logger_factory=structlog.WriteLoggerFactory(file=_TeeWriter()),
        cache_logger_on_first_use=True,
    )
