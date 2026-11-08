"""Structured logging (structlog) with secret redaction. Configure once in the composition root."""

import logging
import sys

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "authorization",
    "api_key",
    "connection_string",
)


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if any(s in key.lower() for s in _SENSITIVE_KEYS):
            event_dict[key] = "***"
    return event_dict


def _stderr_logger(*_args: object) -> structlog.PrintLogger:
    # Resolve sys.stderr when the logger is created, not at configure time (keeps redirection/capture working).
    return structlog.PrintLogger(sys.stderr)


def configure_logging(level: str, *, json: bool, cache: bool = True) -> None:
    """Configure structlog once. Logs go to stderr so stdout stays free for CLI output.

    `cache=False` (tests) re-binds loggers on every use so captured streams are honored.
    """
    renderer: Processor = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.dict_tracebacks if json else structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=_stderr_logger,
        cache_logger_on_first_use=cache,
    )
