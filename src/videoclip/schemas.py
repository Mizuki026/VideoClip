from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: UUID
    username: str


class CsrfResponse(BaseModel):
    csrf_token: str


class VideoSummary(BaseModel):
    id: UUID
    display_name: str
    byte_count: int
    content_sha256: str
    duration_seconds: float
    media_info: dict[str, Any]
    status: str
    deletion_error: str | None
    created_at: datetime


class VideoListResponse(BaseModel):
    items: list[VideoSummary]


class VideoDetailResponse(VideoSummary):
    analysis_task_exists: bool


class UploadResponse(BaseModel):
    video: VideoDetailResponse
