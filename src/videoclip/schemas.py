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
    parent_attempt_id: UUID | None
    status: str
    execution_mode: str
    execution_id: UUID | None
    first_claimed_at: datetime | None
    stop_confirmed_at: datetime | None
    cleanup_state: str
    cleanup_error_code: str | None
    failure_code: str | None


class AnalysisTaskResponse(BaseModel):
    id: UUID
    video_id: UUID
    current_attempt_id: UUID
    status: str
    stage: str
    delivery_status: str
    delivery_updated_at: datetime
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
    reused_segments: int
    summary_status: str
    execution_state: str
    execution_stop_confirmed: bool | None
    failure_code: str | None
    recovery_eligible: bool
    recovery_error_code: str | None
    recovery_available_at: datetime | None
    is_current_attempt: bool
    created_at: datetime
    attempt: AnalysisAttemptResponse


class AnalysisSummaryResponse(BaseModel):
    video_id: UUID
    task_id: UUID
    attempt_id: UUID
    status: str
    structure_version: str
    provider: str
    model: str
    prompt_version: str
    prompt_sha256: str
    manifest_fingerprint: str
    transcript_fingerprint: str
    summary_sha256: str
    payload: dict[str, Any]
    created_at: datetime


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: UUID


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: UUID


class CleanupResponse(BaseModel):
    video_id: UUID
    attempt_id: UUID
    cleanup_state: str
    cleanup_error_code: str | None


class VideoDeletionProgress(BaseModel):
    video_id: UUID
    state: str
    error_code: str | None
    pending_attempt_ids: list[UUID]
    stop_confirmation_required: bool


class TranscriptSegmentResponse(BaseModel):
    segment_number: int
    source_attempt_id: UUID | None
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
