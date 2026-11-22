from datetime import UTC, datetime
from pathlib import Path

import pydantic
import pytest

from quant_research.config import Settings
from quant_research.log import redact_secrets
from quant_research.main import run


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)


def test_settings_defaults() -> None:
    settings = Settings()
    assert (settings.app_env, settings.log_level, settings.greeting_name) == (
        "development",
        "INFO",
        "world",
    )


def test_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("GREETING_NAME", "ops")
    settings = Settings()
    assert settings.app_env == "production"
    assert settings.greeting_name == "ops"


def test_settings_reads_dotenv_file() -> None:
    Path(".env").write_text("GREETING_NAME=from-file\n", encoding="utf-8")
    assert Settings().greeting_name == "from-file"


def test_settings_invalid_log_level_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(pydantic.ValidationError):
        Settings()


def test_run_prints_greeting_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(Settings(greeting_name="dev"), FixedClock()) == 0
    captured = capsys.readouterr()
    assert captured.out == "Hello, dev!\n"
    assert '"event": "greeting.done"' in captured.err


def test_run_blank_name_returns_one(capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings.model_construct(greeting_name="  ")  # bypass validation to reach the domain error path
    assert run(settings, FixedClock()) == 1
    assert '"event": "greeting.failed"' in capsys.readouterr().err


def test_redact_secrets_masks_sensitive_keys() -> None:
    event = {"event": "db.connect", "password": "p", "api_key": "k", "user": "u"}
    assert redact_secrets(None, "info", event) == {
        "event": "db.connect",
        "password": "***",
        "api_key": "***",
        "user": "u",
    }


def test_tracebacks_can_leave_out_local_variables(capsys: pytest.CaptureFixture[str]) -> None:
    """v2.3 step 9 review 1: structlog's dict tracebacks print frame locals by default; the validator's log file
    must not carry holdout values (a holdout SR in a local of the failing frame)."""
    import structlog

    from quant_research.log import configure_logging

    def judge() -> None:
        fwd_sr = 0.0345  # stands in for a holdout number held in a local
        raise RuntimeError(f"failed after {fwd_sr > 0}")

    try:
        for show, expect in ((True, True), (False, False)):
            configure_logging("INFO", json=True, cache=False, show_locals=show)
            try:
                judge()
            except RuntimeError:
                structlog.get_logger("t").exception("validator.request_failed")
            err = capsys.readouterr().err
            assert "validator.request_failed" in err and ("0.0345" in err) is expect
    finally:
        configure_logging("DEBUG", json=True, cache=False)  # the suite's default (tests/conftest.py)
