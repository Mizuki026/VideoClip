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


class AnalysisAttemptResponse(BaseModel):
    id: UUID
    attempt_number: int
    status: str
    execution_id: UUID | None
    first_claimed_at: datetime | None
    stop_confirmed_at: datetime | None
    cleanup_state: str
    failure_code: str | None


class AnalysisTaskResponse(BaseModel):
    id: UUID
    video_id: UUID
    status: str
    stage: str
    delivery_status: str
    delivery_error_code: str | None
    completed_segments: int
    expected_segments: int
    transcript_complete: bool
    asr_model: str
    config_sha256: str
    source_fingerprint: str
    manifest_fingerprint: str | None
    asr_request_count: int
    asr_elapsed_seconds: float
    asr_calls: list[dict[str, Any]]
    summary_status: str
    execution_state: str
    execution_stop_confirmed: bool | None
    failure_code: str | None
    created_at: datetime
    attempt: AnalysisAttemptResponse


class TranscriptSegmentResponse(BaseModel):
    segment_number: int
    audio_start_seconds: float
    audio_end_seconds: float
    actual_audio_seconds: float
    video_start_seconds: float
    video_end_seconds: float
    text: str | None
    status: str


class TranscriptResponse(BaseModel):
    task_id: UUID
    complete: bool
    summary_status: str
    items: list[TranscriptSegmentResponse]
