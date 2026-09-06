"""Structured logging: snake_case event names with keyword fields, never sentences."""

from __future__ import annotations

from typing import Optional

import structlog


def configure_logging(log_level: str = "INFO") -> None:
    """Configure structlog for console output."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            structlog.processors.NAME_TO_LEVEL.get(log_level.lower(), 20)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: Optional[str] = None):
    return structlog.get_logger(name)
