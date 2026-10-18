from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# 定义 Pydantic 校验规则，字段没有默认值，所以必须提供。extra="forbid" 表示拒绝未定义的字段，例如额外传入 role 会校验失败
# 注册和登录模型限定接口可接收的字段与长度，额外字段会被拒绝。
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
    media_status: str
    media_error_code: str | None
    created_at: datetime


class VideoListResponse(BaseModel):
    items: list[VideoSummary]


class VideoDetailResponse(VideoSummary):
    analysis_task_exists: bool
    analysis_task_id: UUID | None


class UploadResponse(BaseModel):
    video: VideoDetailResponse


# 创建续传会话时声明文件名、总字节数和整文件摘要，服务端据此建立上传记录。
class ResumableUploadCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    file_name: str = Field(min_length=1, max_length=2048)
    total_bytes: int = Field(gt=0, le=9_223_372_036_854_775_807)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ResumableUploadCreateResponse(BaseModel):
    upload_id: UUID
    state: str
    chunk_size: int
    total_bytes: int
    confirmed_offset: int
    expires_at: datetime
    replayed: bool


class UploadChunkSummary(BaseModel):
    chunk_index: int
    byte_offset: int
    byte_count: int
    sha256: str


# 续传状态同时返回数据库确认偏移、下一片序号和已确认分片清单。
class ResumableUploadStatusResponse(BaseModel):
    upload_id: UUID
    state: str
    chunk_size: int
    total_bytes: int
    confirmed_offset: int
    next_chunk_index: int
    expires_at: datetime
    chunks: list[UploadChunkSummary]
    error_code: str | None
    video_id: UUID | None


class UploadChunkResponse(BaseModel):
    upload_id: UUID
    state: str
    chunk_index: int
    byte_offset: int
    byte_count: int
    sha256: str
    confirmed_offset: int
    expires_at: datetime
    replayed: bool


# 尝试响应单独展示停止确认和清理状态，避免把任务失败误当成执行已退出。
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


# 任务响应汇总前端恢复状态所需的投递、等待与重试信息。
class AnalysisTaskResponse(BaseModel):
    id: UUID
    video_id: UUID
    current_attempt_id: UUID
    notes_attempt_id: UUID | None
    status: str
    stage: str
    delivery_status: str
    delivery_updated_at: datetime
    delivery_error_code: str | None
    delivery_operation_generation: int | None
    queue_entered_at: datetime | None
    delivery_next_check_at: datetime | None
    delivery_retry_count: int | None
    delivery_retry_limit: int | None
    delivery_deadline_at: datetime | None
    delivery_wait_reason: str | None
    delivery_failure_code: str | None
    model_wait_reason: str | None
    model_wait_error_code: str | None
    model_wait_stage: str | None
    model_wait_until: datetime | None
    model_wait_single_deadline_at: datetime | None
    model_wait_seconds_used: int | None
    model_wait_total_limit_seconds: int | None
    auto_recovery_count: int | None
    auto_recovery_limit: int | None
    auto_recovery_budget_exhausted: bool | None
    summary_requests_used: int
    summary_requests_limit: int
    summary_requests_exhausted: bool
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


# 摘要响应携带转写清单与内容摘要，便于核对结果所依据的输入版本。
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


class AnalysisEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_id: UUID
    task_id: UUID
    attempt_id: UUID | None
    execution_id: UUID | None
    request_reference: str | None
    event_type: str
    stage: str | None
    error_code: str | None
    actor: str
    occurred_at: datetime


# 事件响应通过事件编号继续分页，避免一次性返回整条任务时间线。
class AnalysisEventsResponse(BaseModel):
    items: list[AnalysisEventResponse]
    has_more: bool
    next_before_id: int | None


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


# 媒体删除进度同时报告待停止的尝试，前端据此区分删除等待和清理失败。
class VideoMediaDeletionProgress(BaseModel):
    video_id: UUID
    media_status: str
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


# 转写响应明确区分转写是否完整与摘要状态，二者不会混为一个完成标志。
class TranscriptResponse(BaseModel):
    task_id: UUID
    complete: bool
    summary_status: str
    items: list[TranscriptSegmentResponse]
