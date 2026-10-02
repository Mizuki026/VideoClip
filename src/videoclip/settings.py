from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


def _missing_app_secret() -> SecretStr:
    raise RuntimeError("APP_SECRET_KEY is required")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="data/mvp-stage-1/.env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    app_env: Literal["development", "test", "production"] = Field(default="development", alias="APP_ENV")
    app_origin: str = Field(default="http://127.0.0.1:58001", alias="APP_ORIGIN")
    app_secret_key: SecretStr = Field(
        default_factory=_missing_app_secret,
        alias="APP_SECRET_KEY",
        repr=False,
    )
    app_data_root: Path = Field(default=Path("data/mvp-stage-1/app"), alias="APP_DATA_ROOT")
    cookie_secure: bool | None = Field(default=None, alias="COOKIE_SECURE")
    cookie_samesite: Literal["lax", "strict"] = Field(default="lax", alias="COOKIE_SAMESITE")
    session_ttl_hours: int = Field(default=24, ge=1, le=168, alias="SESSION_TTL_HOURS")
    max_sessions_per_user: int = Field(default=8, ge=1, le=32, alias="MAX_SESSIONS_PER_USER")
    max_json_body_bytes: int = Field(default=16_384, ge=1024, le=65_536, alias="MAX_JSON_BODY_BYTES")
    upload_total_timeout_seconds: int = Field(
        default=1800, ge=30, le=7200, alias="UPLOAD_TOTAL_TIMEOUT_SECONDS"
    )
    ffprobe_timeout_seconds: int = Field(default=45, ge=1, le=180, alias="FFPROBE_TIMEOUT_SECONDS")
    ffprobe_stop_grace_seconds: int = Field(default=5, ge=1, le=30, alias="FFPROBE_STOP_GRACE_SECONDS")
    cleanup_timeout_seconds: int = Field(default=30, ge=1, le=300, alias="CLEANUP_TIMEOUT_SECONDS")
    ffprobe_path: str = Field(default="ffprobe", alias="FFPROBE_PATH")
    db_host: str = Field(default="127.0.0.1", alias="DB_HOST")
    db_port: int = Field(default=55442, ge=1, le=65535, alias="DB_PORT")
    db_name: str = Field(default="videoclip_mvp_stage1", alias="DB_NAME")
    db_user: str = Field(default="videoclip_stage1", alias="DB_USER")
    db_password_file: Path = Field(
        default=Path("data/mvp-stage-1/secrets/postgres_password"), alias="DB_PASSWORD_FILE"
    )

    @model_validator(mode="after")
    def validate_security_settings(self) -> Settings:
        if len(self.app_secret_key.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("APP_SECRET_KEY must contain at least 32 bytes")
        origin = urlsplit(self.app_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.netloc
            or origin.path
            or origin.query
            or origin.fragment
            or self.app_origin.endswith("/")
        ):
            raise ValueError("APP_ORIGIN must be an exact http(s) origin without a trailing slash")
        if self.app_env == "production" and self.cookie_secure is False:
            raise ValueError("COOKIE_SECURE cannot be false in production")
        return self

    @property
    def secure_cookie(self) -> bool:
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.app_env == "production"

    def database_url(self) -> URL:
        try:
            password = self.db_password_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("database password file is not available") from exc
        if len(password) < 32:
            raise RuntimeError("database password file is invalid")
        return URL.create(
            "postgresql+psycopg",
            username=self.db_user,
            password=password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
