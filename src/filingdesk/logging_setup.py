"""Structured JSON logging. One trace_id per request, every tool call logged.

Renders to stdout, which systemd captures into journald and docker captures
into its log driver. Query with:

    journalctl -u filingdesk -o cat | jq -c 'select(.trace_id=="01J...")'
    journalctl -u filingdesk -o cat | jq -c 'select(.event=="tool.call")'
"""
import logging
import os
import sys
import time
import uuid

import structlog


def configure() -> None:
    dev = os.environ.get("FD_LOG_FORMAT", "json") == "console"
    renderer = (structlog.dev.ConsoleRenderer() if dev
                else structlog.processors.JSONRenderer())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def new_trace() -> str:
    """ULID-ish: sortable time prefix + random suffix. Good enough."""
    tid = f"{int(time.time() * 1000):011x}{uuid.uuid4().hex[:10]}".upper()
    structlog.contextvars.bind_contextvars(trace_id=tid)
    return tid


def clear() -> None:
    structlog.contextvars.clear_contextvars()


log = structlog.get_logger()
