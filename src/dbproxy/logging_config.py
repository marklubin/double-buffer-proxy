"""Structured logging via structlog with hourly rotating file output."""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler

import structlog


def setup_logging(log_dir: str = "logs", log_level: str = "DEBUG") -> None:
    """Configure structlog with JSON output to hourly rotating files + stderr."""
    os.makedirs(log_dir, exist_ok=True)

    # File handler: hourly rotation, JSON lines
    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "dbproxy.jsonl"),
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
        logger_factory=structlog.WriteLoggerFactory(
            # Write to root logger's handlers via stdlib bridge
        ),
        cache_logger_on_first_use=True,
    )

    # Also configure stdlib logging to use structlog formatting
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
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
