"""Configuration: parsed and validated once at startup (boundary validation)."""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True)

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    greeting_name: str = Field(default="world", min_length=1)
    # Data lives outside the repository (design v2.1 §3.1).
    data_root: Path = Path("C:/dev/qr-data")
    binance_top_n: int = Field(default=50, ge=1, le=300)
    upbit_top_n: int = Field(default=20, ge=1, le=100)


def load_settings() -> Settings:
    return Settings()
