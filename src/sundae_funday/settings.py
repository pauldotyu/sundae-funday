"""Shared settings primitives."""

from pydantic_settings import BaseSettings, SettingsConfigDict

from sundae_funday import APP_VERSION


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_version: str = APP_VERSION


def normalize_url(value: str) -> str:
    return f"{value.rstrip('/')}/"
