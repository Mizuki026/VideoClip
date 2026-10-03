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
    ffmpeg_path: str = Field(default="ffmpeg", alias="FFMPEG_PATH")
    asr_base_url: str = Field(default="https://api.siliconflow.cn/v1", alias="SILICONFLOW_BASE_URL")
    asr_model: str = Field(default="FunAudioLLM/SenseVoiceSmall", alias="SILICONFLOW_ASR_MODEL")
    asr_secret_reference: str = Field(default="environment:SILICONFLOW_API_KEY", alias="ASR_SECRET_REFERENCE")
    asr_request_parameters: dict[str, str] = Field(default_factory=dict, alias="ASR_REQUEST_PARAMETERS")
    llm_model: str = Field(default="Qwen/Qwen3.5-4B", alias="SILICONFLOW_LLM_MODEL")
    summary_prompt_version: str = Field(default="stage0-summary-v5", alias="PROMPT_VERSION")
    summary_schema_version: str = Field(default="summary-v1", alias="SUMMARY_SCHEMA_VERSION")
    llm_context_tokens: int = Field(default=256_000, ge=1, alias="LLM_CONTEXT_TOKENS")
    llm_max_output_tokens: int = Field(default=8192, ge=1, alias="LLM_MAX_OUTPUT_TOKENS")
    llm_temperature: float = Field(default=0.2, ge=0, le=2, alias="LLM_TEMPERATURE")
    llm_enable_thinking: bool = Field(default=False, alias="LLM_ENABLE_THINKING")
    llm_http_total_timeout_seconds: float = Field(
        default=300, gt=0, le=600, alias="LLM_HTTP_TOTAL_TIMEOUT_SECONDS"
    )
    llm_http_connect_timeout_seconds: float = Field(
        default=15, gt=0, le=120, alias="LLM_HTTP_CONNECT_TIMEOUT_SECONDS"
    )
    llm_http_read_timeout_seconds: float = Field(
        default=240, gt=0, le=300, alias="LLM_HTTP_READ_TIMEOUT_SECONDS"
    )
    llm_http_write_timeout_seconds: float = Field(
        default=45, gt=0, le=180, alias="LLM_HTTP_WRITE_TIMEOUT_SECONDS"
    )
    llm_http_pool_timeout_seconds: float = Field(
        default=15, gt=0, le=120, alias="LLM_HTTP_POOL_TIMEOUT_SECONDS"
    )
    max_llm_response_bytes: int = Field(default=1_048_576, ge=1, le=4_194_304, alias="MAX_LLM_RESPONSE_BYTES")
    max_llm_diagnostic_bytes: int = Field(default=8192, ge=512, le=65_536, alias="MAX_LLM_DIAGNOSTIC_BYTES")
    audio_sample_rate: int = Field(default=16_000, ge=1, alias="AUDIO_SAMPLE_RATE")
    audio_channels: int = Field(default=1, ge=1, le=1, alias="AUDIO_CHANNELS")
    audio_sample_format: str = Field(default="s16le", alias="AUDIO_SAMPLE_FORMAT")
    segment_seconds: int = Field(default=30, ge=1, le=120, alias="SEGMENT_SECONDS")
    timeline_mapping_version: str = Field(default="zero-offset-v1", alias="TIMELINE_MAPPING_VERSION")
    slice_manifest_version: str = Field(default="pcm-s16le-slices-v1", alias="SLICE_MANIFEST_VERSION")
    asr_http_total_timeout_seconds: float = Field(
        default=240, gt=0, le=600, alias="HTTP_TOTAL_TIMEOUT_SECONDS"
    )
    asr_http_connect_timeout_seconds: float = Field(
        default=15, gt=0, le=120, alias="HTTP_CONNECT_TIMEOUT_SECONDS"
    )
    asr_http_read_timeout_seconds: float = Field(default=45, gt=0, le=180, alias="HTTP_READ_TIMEOUT_SECONDS")
    media_total_timeout_seconds: int = Field(default=300, ge=1, le=1800, alias="MEDIA_TOTAL_TIMEOUT_SECONDS")
    process_stop_grace_seconds: int = Field(default=5, ge=1, le=30, alias="MEDIA_STOP_GRACE_SECONDS")
    attempt_total_timeout_seconds: int = Field(
        default=10_800, ge=60, le=10_800, alias="ATTEMPT_TOTAL_TIMEOUT_SECONDS"
    )
    max_asr_response_bytes: int = Field(default=1_048_576, ge=1, le=4_194_304, alias="MAX_RESPONSE_BYTES")
    max_media_diagnostic_bytes: int = Field(default=8192, ge=512, le=65_536, alias="MAX_DIAGNOSTIC_BYTES")
    celery_broker_url: str = Field(default="redis://127.0.0.1:6379/0", alias="CELERY_BROKER_URL")
    worker_id: str = Field(default="videoclip-worker", alias="WORKER_ID")
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
        asr_endpoint = urlsplit(self.asr_base_url)
        if asr_endpoint.scheme != "https" or not asr_endpoint.netloc or self.asr_base_url.endswith("/"):
            raise ValueError("SILICONFLOW_BASE_URL must be an HTTPS base URL without a trailing slash")
        if self.audio_sample_format != "s16le":
            raise ValueError("AUDIO_SAMPLE_FORMAT must be s16le")
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
