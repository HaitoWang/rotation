from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed runtime configuration.

    Configuration is environment based so API and worker processes share the
    same contract. Secrets are represented as ``SecretStr`` to avoid accidental
    logging.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "regert-register"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = Field(default=8765, ge=1, le=65535)
    log_level: str = "INFO"
    auto_create_schema: bool = False

    database_url: str = "postgresql+asyncpg://regert:regert@localhost:5432/regert"
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    database_pool_timeout: int = Field(default=30, ge=1, le=300)

    redis_url: str = "redis://localhost:6379/0"
    redis_queue_name: str = "regert"
    redis_event_channel_prefix: str = "regert:events"
    redis_stream_maxlen: int = Field(default=1000, ge=10, le=100000)

    worker_max_jobs: int = Field(default=10, ge=1, le=1000)
    worker_job_timeout: int = Field(default=1800, ge=30, le=86400)

    team_sso_enabled: bool = False
    team_sso_url: str = ""
    team_sso_sync_key: SecretStr = SecretStr("")
    team_sso_timeout: float = Field(default=10, ge=2, le=120)
    team_rotation_enabled: bool = False
    team_rotation_proxy: str = ""

    secret_key: SecretStr = SecretStr("change-me-in-production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
