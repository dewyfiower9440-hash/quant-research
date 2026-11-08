"""Composition root: load config, wire adapters, run the use case."""

import sys
import time

import structlog

from quant_research.adapters.clock import Clock, SystemClock
from quant_research.config import Settings, load_settings
from quant_research.domain.greeting import build_greeting
from quant_research.errors import AppError
from quant_research.log import configure_logging


def run(settings: Settings, clock: Clock) -> int:
    log = structlog.get_logger(__name__)
    started = time.perf_counter()
    try:
        message = build_greeting(settings.greeting_name)
    except AppError as e:
        log.error("greeting.failed", error=str(e))
        return 1
    print(message)  # CLI output
    log.info(
        "greeting.done",
        at=clock.now().isoformat(),
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return 0


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, json=settings.app_env == "production")
    sys.exit(run(settings, SystemClock()))
