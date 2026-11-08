from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from quant_research.log import configure_logging

_CONFIG_ENV_VARS = ("APP_ENV", "LOG_LEVEL", "GREETING_NAME")


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every test in an empty cwd (no local .env) with config env vars cleared and logs on stderr."""
    monkeypatch.chdir(tmp_path)
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    configure_logging("DEBUG", json=True, cache=False)
    yield
    structlog.reset_defaults()
