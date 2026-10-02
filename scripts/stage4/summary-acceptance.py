from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from videoclip.analysis import build_source_fingerprint, canonical_sha256
from videoclip.db import create_database_engine, create_session_factory
from videoclip.models import (
    AnalysisAttempt,
    AnalysisExecution,
    AnalysisSummary,
    AnalysisTask,
    SessionRecord,
    SummaryRequest,
    TranscriptSegment,
    Video,
)
from videoclip.security import new_csrf_token, new_session_token, token_hash
from videoclip.settings import Settings
from videoclip.storage import MediaStore
from videoclip.summarization import (
    MAX_SUMMARY_REQUESTS,
    SummaryFailure,
    resolve_summary_contract,
    validate_transcript_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
STAGE2_ENV = ROOT / "data" / "mvp-stage-2" / ".env"
STAGE2_DB_PASSWORD = ROOT / "data" / "mvp-stage-2" / "secrets" / "postgres_password"
STAGE2_APP_DATA = ROOT / "data" / "mvp-stage-2" / "app"
SESSION_COOKIE = "videoclip_session"
CSRF_COOKIE = "videoclip_csrf"
POLL_SECONDS = 2
MAX_WAIT_SECONDS = 900
FRESH_SAMPLE_MAX_WAIT_SECONDS = 10_800


class AcceptanceFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe_api_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        detail = payload.get("detail") if isinstance(payload, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
    except (ValueError, json.JSONDecodeError):
        code = None
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code) else "API_REQUEST_FAILED"


def _settings() -> Settings:
    if not STAGE2_ENV.is_file() or not STAGE2_DB_PASSWORD.is_file():
        raise AcceptanceFailure("STAGE2_PRIVATE_CONFIGURATION_UNAVAILABLE")
    try:
        return Settings(
            _env_file=str(STAGE2_ENV),
            db_host="127.0.0.1",
            db_port=55443,
            db_password_file=STAGE2_DB_PASSWORD,
            app_data_root=STAGE2_APP_DATA,
        )  # type: ignore[call-arg]
    except Exception as exc:
        raise AcceptanceFailure("STAGE2_PRIVATE_CONFIGURATION_INVALID") from exc


def _fingerprint_rows(rows: list[TranscriptSegment]) -> str:
    return canonical_sha256(
        [
            {
                "segment_number": row.segment_number,
                "text_sha256": hashlib.sha256((row.text or "").encode("utf-8")).hexdigest(),
                "audio_sha256": row.audio_sha256,
                "request_fingerprint": row.request_fingerprint,
                "status": row.status,
            }
            for row in rows
        ]
    )


def _prepare(factory: sessionmaker[Session], task_id: uuid.UUID) -> dict[str, Any]:
    with factory() as session:
        task = session.get(AnalysisTask, task_id)
        if task is None or task.current_attempt_id is None:
            raise AcceptanceFailure("AUTHORIZED_TASK_UNAVAILABLE")
        attempt = session.get(AnalysisAttempt, task.current_attempt_id)
        video = session.get(Video, task.video_id)
        if attempt is None or video is None:
            raise AcceptanceFailure("AUTHORIZED_TASK_UNAVAILABLE")
        execution = session.scalar(
            select(AnalysisExecution).where(AnalysisExecution.attempt_id == attempt.id)
        )
        rows = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.attempt_id == attempt.id)
                .order_by(TranscriptSegment.segment_number)
            )
        )
        try:
            contract = resolve_summary_contract(attempt.config_snapshot)
        except Exception as exc:
            raise AcceptanceFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE") from exc
        if (
            task.status != "awaiting_summary"
            or task.summary_status != "waiting"
            or attempt.status != "awaiting_summary"
            or attempt.claim_count != 1
            or attempt.stop_confirmed_at is None
            or execution is None
            or execution.stop_confirmed_at is None
            or video.status != "ready"
        ):
            raise AcceptanceFailure("AUTHORIZED_TASK_NOT_AWAITING_SUMMARY")
        if len(rows) != 13 or any(row.status != "succeeded" or not row.text for row in rows):
            raise AcceptanceFailure("AUTHORIZED_TRANSCRIPT_NOT_COMPLETE_13_OF_13")
        snapshot = attempt.config_snapshot
        if canonical_sha256(snapshot) != attempt.config_sha256:
            raise AcceptanceFailure("AUTHORIZED_CONFIG_SNAPSHOT_INVALID")
        source = snapshot.get("source") if isinstance(snapshot, dict) else None
        if not isinstance(source, dict) or source.get("fingerprint") != attempt.source_fingerprint:
            raise AcceptanceFailure("AUTHORIZED_TRANSCRIPT_SOURCE_UNVERIFIED")
        try:
            if (
                source.get("fingerprint")
                != build_source_fingerprint(video, int(source["audio_stream_index"]))
                or source.get("storage_name") != video.storage_name
                or source.get("byte_count") != video.byte_count
                or source.get("sha256") != video.content_sha256
                or source.get("duration_seconds") != video.duration_seconds
            ):
                raise AcceptanceFailure("AUTHORIZED_VIDEO_SOURCE_MISMATCH")
            validate_transcript_manifest(
                [
                    {
                        "segment_number": row.segment_number,
                        "audio_start_seconds": row.audio_start_seconds,
                        "audio_end_seconds": row.audio_end_seconds,
                        "actual_audio_seconds": row.actual_audio_seconds,
                        "video_start_seconds": row.video_start_seconds,
                        "video_end_seconds": row.video_end_seconds,
                        "audio_sha256": row.audio_sha256,
                        "request_fingerprint": row.request_fingerprint,
                        "status": row.status,
                        "text": row.text,
                    }
                    for row in rows
                ],
                snapshot=snapshot,
                video_duration_seconds=video.duration_seconds,
                expected_manifest_fingerprint=attempt.manifest_fingerprint,
            )
        except SummaryFailure as exc:
            if exc.code == "TRANSCRIPT_SOURCE_MISMATCH":
                slicing = snapshot.get("slicing") if isinstance(snapshot, dict) else None
                audio = snapshot.get("audio") if isinstance(snapshot, dict) else None
                if not isinstance(slicing, dict) or not isinstance(audio, dict):
                    raise AcceptanceFailure("AUTHORIZED_TRANSCRIPT_SOURCE_CONFIGURATION_INVALID") from exc
                if slicing.get("timeline_mapping_version") != "zero-offset-v1":
                    raise AcceptanceFailure("AUTHORIZED_TIMELINE_MAPPING_INCOMPATIBLE") from exc
                if (
                    type(slicing.get("segment_seconds")) is not int
                    or slicing["segment_seconds"] <= 0
                    or type(audio.get("sample_rate")) is not int
                    or audio["sample_rate"] <= 0
                    or type(audio.get("channels")) is not int
                    or audio["channels"] != 1
                    or audio.get("sample_format") != "s16le"
                ):
                    raise AcceptanceFailure("AUTHORIZED_TRANSCRIPT_SOURCE_CONFIGURATION_INVALID") from exc
                manifest_segments = [
                    {
                        "segment_number": row.segment_number,
                        "audio_start_seconds": row.audio_start_seconds,
                        "audio_end_seconds": row.audio_end_seconds,
                        "actual_audio_seconds": row.actual_audio_seconds,
                        "video_start_seconds": row.video_start_seconds,
                        "video_end_seconds": row.video_end_seconds,
                        "audio_sha256": row.audio_sha256,
                        "request_fingerprint": row.request_fingerprint,
                    }
                    for row in rows
                ]
                if any(
                    not isinstance(row["audio_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", row["audio_sha256"])
                    or not isinstance(row["request_fingerprint"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", row["request_fingerprint"])
                    for row in manifest_segments
                ):
                    raise AcceptanceFailure("AUTHORIZED_TRANSCRIPT_SEGMENT_FINGERPRINT_INVALID") from exc
                expected_manifest = canonical_sha256(
                    {
                        "version": slicing.get("manifest_version"),
                        "timeline_mapping_version": slicing.get("timeline_mapping_version"),
                        "audio": dict(audio),
                        "source_fingerprint": source.get("fingerprint"),
                        "segments": manifest_segments,
                    }
                )
                if expected_manifest != attempt.manifest_fingerprint:
                    raise AcceptanceFailure("AUTHORIZED_TRANSCRIPT_MANIFEST_FINGERPRINT_MISMATCH") from exc
            raise AcceptanceFailure(f"AUTHORIZED_{exc.code}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise AcceptanceFailure("AUTHORIZED_VIDEO_SOURCE_MISMATCH") from exc
        result = {
            "task_id": task.id,
            "parent_attempt_id": attempt.id,
            "owner_id": task.owner_id,
            "video_id": video.id,
            "video_sha256": video.content_sha256,
            "config_snapshot": attempt.config_snapshot,
            "transcript_fingerprint": _fingerprint_rows(rows),
            "video_status": video.status,
            "provider": contract.provider,
            "model": contract.model,
            "prompt_version": contract.prompt_version,
            "schema_version": contract.schema_version,
            "legacy_compatibility": contract.legacy_compatibility,
        }
        session.commit()
        return result


def _create_ephemeral_session(
    factory: sessionmaker[Session], owner_id: uuid.UUID, settings: Settings
) -> tuple[uuid.UUID, str, str]:
    raw_session = new_session_token()
    raw_csrf = new_csrf_token(settings.app_secret_key.get_secret_value())
    session_id = uuid.uuid4()
    with factory.begin() as session:
        session.add(
            SessionRecord(
                id=session_id,
                user_id=owner_id,
                token_hash=token_hash(raw_session),
                csrf_hash=hashlib.sha256(raw_csrf.encode("ascii")).hexdigest(),
                expires_at=datetime.now(UTC) + timedelta(hours=2),
            )
        )
    return session_id, raw_session, raw_csrf


def _delete_ephemeral_session(factory: sessionmaker[Session], session_id: uuid.UUID) -> None:
    with factory.begin() as session:
        record = session.get(SessionRecord, session_id, with_for_update=True)
        if record is not None:
            record.revoked_at = datetime.now(UTC)
            session.delete(record)


def _verify_published_result(
    factory: sessionmaker[Session],
    before: dict[str, Any],
    child_id: uuid.UUID,
    summary_payload: dict[str, Any],
    transcript_payload: dict[str, Any],
) -> dict[str, Any]:
    with factory() as session:
        task = session.get(AnalysisTask, before["task_id"])
        video = session.get(Video, before["video_id"])
        parent = session.get(AnalysisAttempt, before["parent_attempt_id"])
        child = session.get(AnalysisAttempt, child_id)
        summary = session.scalar(select(AnalysisSummary).where(AnalysisSummary.task_id == before["task_id"]))
        requests = list(
            session.scalars(
                select(SummaryRequest)
                .where(SummaryRequest.attempt_id == child_id)
                .order_by(SummaryRequest.request_number)
            )
        )
        transcript_rows = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.attempt_id == child_id)
                .order_by(TranscriptSegment.segment_number)
            )
        )
        parent_rows = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.attempt_id == before["parent_attempt_id"])
                .order_by(TranscriptSegment.segment_number)
            )
        )
        if (
            task is None
            or video is None
            or parent is None
            or child is None
            or summary is None
            or task.status != "succeeded"
            or task.summary_status != "succeeded"
            or task.current_attempt_id != child_id
            or child.status != "succeeded"
            or child.execution_mode != "summary_only"
            or video.status != before["video_status"]
            or video.content_sha256 != before["video_sha256"]
            or parent.status != "awaiting_summary"
        ):
            raise AcceptanceFailure("PUBLISHED_DATABASE_STATE_INVALID")
        if len(transcript_rows) != 13 or any(
            row.status != "succeeded" or row.request_count != 0 or row.source_attempt_id != parent.id
            for row in transcript_rows
        ):
            raise AcceptanceFailure("SUMMARY_ATTEMPT_DID_NOT_REUSE_TRANSCRIPT")
        if len(parent_rows) != 13 or _fingerprint_rows(parent_rows) != before["transcript_fingerprint"]:
            raise AcceptanceFailure("ORIGINAL_TRANSCRIPT_CHANGED")
        if not requests or len(requests) > 3 or requests[-1].status != "succeeded":
            raise AcceptanceFailure("SUMMARY_REQUEST_AUDIT_INVALID")
        if summary.model != before["model"] or summary.prompt_version != before["prompt_version"]:
            raise AcceptanceFailure("SUMMARY_FROZEN_METADATA_MISMATCH")

        source_by_number = {row.segment_number: row for row in transcript_rows}
        citations = 0
        matched_quotes = 0
        for item in summary_payload.get("chapters", []) + summary_payload.get("key_points", []):
            evidence = item.get("evidence")
            if not isinstance(evidence, list) or len(evidence) != 1:
                raise AcceptanceFailure("SUMMARY_CITATION_SHAPE_INVALID")
            reference = evidence[0]
            source = source_by_number.get(reference.get("segment_id"))
            quote = reference.get("quote")
            start = reference.get("start_seconds")
            end = reference.get("end_seconds")
            if (
                source is None
                or not isinstance(quote, str)
                or not 0 < len(quote) <= 48
                or source.text is None
                or quote not in source.text
                or not isinstance(start, int | float)
                or isinstance(start, bool)
                or not isinstance(end, int | float)
                or isinstance(end, bool)
                or not math.isfinite(start)
                or not math.isfinite(end)
                or start != source.video_start_seconds
                or end != source.video_end_seconds
                or not 0 <= start < end <= video.duration_seconds
            ):
                raise AcceptanceFailure("SUMMARY_CITATION_SOURCE_VALIDATION_FAILED")
            citations += 1
            matched_quotes += 1
        if not transcript_payload.get("complete") or len(transcript_payload.get("items", [])) != 13:
            raise AcceptanceFailure("TRANSCRIPT_READBACK_INVALID")
        if summary_payload.get("chapters") is None or summary_payload.get("key_points") is None:
            raise AcceptanceFailure("SUMMARY_READBACK_INVALID")

        return {
            "chapters": len(summary_payload["chapters"]),
            "key_points": len(summary_payload["key_points"]),
            "citations": citations,
            "source_quote_matches": matched_quotes,
            "transcript_segments": len(transcript_rows),
            "asr_requests_on_summary_attempt": sum(row.request_count for row in transcript_rows),
            "summary_requests": len(requests),
            "request_seconds": round(sum(row.elapsed_seconds or 0.0 for row in requests), 3),
            "finish_reasons": [row.finish_reason for row in requests],
            "all_requests_audited": all(row.status in {"failed", "succeeded"} for row in requests),
            "video_and_original_transcript_preserved": True,
            "summary_sha256": summary.payload_sha256,
            "prompt_version": summary.prompt_version,
            "schema_version": summary.structure_version,
            "model": summary.model,
        }


def _verify_fresh_result(
    factory: sessionmaker[Session],
    source_task_id: uuid.UUID,
    task_id: uuid.UUID,
    summary_payload: dict[str, Any],
    transcript_payload: dict[str, Any],
) -> dict[str, Any]:
    with factory() as session:
        source_task = session.get(AnalysisTask, source_task_id)
        task = session.get(AnalysisTask, task_id)
        video = session.scalar(select(Video).where(Video.current_analysis_task_id == task_id))
        source_video = session.get(Video, source_task.video_id) if source_task is not None else None
        source_attempt = (
            session.get(AnalysisAttempt, source_task.current_attempt_id)
            if source_task is not None and source_task.current_attempt_id is not None
            else None
        )
        attempt = (
            session.get(AnalysisAttempt, task.current_attempt_id)
            if task is not None and task.current_attempt_id is not None
            else None
        )
        summary = session.scalar(select(AnalysisSummary).where(AnalysisSummary.task_id == task_id))
        if (
            source_task is None
            or source_video is None
            or source_attempt is None
            or task is None
            or video is None
            or attempt is None
            or summary is None
            or task.status != "succeeded"
            or task.summary_status != "succeeded"
            or attempt.status != "succeeded"
            or attempt.execution_mode not in {"full", "summary_only"}
            or source_task.status != "awaiting_summary"
            or source_task.current_attempt_id != source_attempt.id
            or video.owner_id != source_video.owner_id
            or video.status != "ready"
            or video.content_sha256 != source_video.content_sha256
            or video.byte_count != source_video.byte_count
        ):
            raise AcceptanceFailure("ISOLATED_SAMPLE_DATABASE_STATE_INVALID")
        try:
            contract = resolve_summary_contract(attempt.config_snapshot)
        except Exception as exc:
            raise AcceptanceFailure("ISOLATED_SAMPLE_CONFIGURATION_INVALID") from exc
        transcript_rows = list(
            session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.attempt_id == attempt.id)
                .order_by(TranscriptSegment.segment_number)
            )
        )
        summary_only = attempt.execution_mode == "summary_only"
        if not transcript_rows or any(
            row.request_count != 0 if summary_only else row.request_count < 1 for row in transcript_rows
        ):
            raise AcceptanceFailure("ISOLATED_SAMPLE_TRANSCRIPT_INVALID")
        reused_rows: list[TranscriptSegment] = []
        if summary_only:
            parent_attempt = (
                session.get(AnalysisAttempt, attempt.parent_attempt_id)
                if attempt.parent_attempt_id is not None
                else None
            )
            if parent_attempt is None or parent_attempt.task_id != task.id:
                raise AcceptanceFailure("ISOLATED_SAMPLE_TRANSCRIPT_REUSE_INVALID")
            reused_rows = list(
                session.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.attempt_id == parent_attempt.id)
                    .order_by(TranscriptSegment.segment_number)
                )
            )
            if (
                not reused_rows
                or any(row.status != "succeeded" or row.request_count < 1 for row in reused_rows)
                or _fingerprint_rows(reused_rows) != _fingerprint_rows(transcript_rows)
                or any(row.source_attempt_id != parent_attempt.id for row in transcript_rows)
            ):
                raise AcceptanceFailure("ISOLATED_SAMPLE_TRANSCRIPT_REUSE_INVALID")
        try:
            validate_transcript_manifest(
                [
                    {
                        "segment_number": row.segment_number,
                        "audio_start_seconds": row.audio_start_seconds,
                        "audio_end_seconds": row.audio_end_seconds,
                        "actual_audio_seconds": row.actual_audio_seconds,
                        "video_start_seconds": row.video_start_seconds,
                        "video_end_seconds": row.video_end_seconds,
                        "audio_sha256": row.audio_sha256,
                        "request_fingerprint": row.request_fingerprint,
                        "status": row.status,
                        "text": row.text,
                    }
                    for row in transcript_rows
                ],
                snapshot=attempt.config_snapshot,
                video_duration_seconds=video.duration_seconds,
                expected_manifest_fingerprint=attempt.manifest_fingerprint,
            )
        except SummaryFailure as exc:
            raise AcceptanceFailure(f"ISOLATED_SAMPLE_{exc.code}") from exc
        task_attempts = list(
            session.scalars(select(AnalysisAttempt).where(AnalysisAttempt.task_id == task_id))
        )
        attempt_order = {item.id: item.attempt_number for item in task_attempts}
        requests = list(session.scalars(select(SummaryRequest).where(SummaryRequest.task_id == task_id)))
        requests.sort(key=lambda row: (attempt_order.get(row.attempt_id, 0), row.request_number))
        if (
            not requests
            or len(requests) > MAX_SUMMARY_REQUESTS
            or requests[-1].status != "succeeded"
            or any(row.status not in {"failed", "succeeded"} for row in requests)
            or summary.model != contract.model
            or summary.prompt_version != contract.prompt_version
            or summary.structure_version != contract.schema_version
        ):
            raise AcceptanceFailure("ISOLATED_SAMPLE_REQUEST_AUDIT_INVALID")
        source_by_number = {row.segment_number: row for row in transcript_rows}
        citations = 0
        for item in summary_payload.get("chapters", []) + summary_payload.get("key_points", []):
            evidence = item.get("evidence")
            if not isinstance(evidence, list) or len(evidence) != 1:
                raise AcceptanceFailure("SUMMARY_CITATION_SHAPE_INVALID")
            reference = evidence[0]
            source = source_by_number.get(reference.get("segment_id"))
            quote = reference.get("quote")
            start = reference.get("start_seconds")
            end = reference.get("end_seconds")
            if (
                source is None
                or not isinstance(quote, str)
                or not 0 < len(quote) <= 48
                or source.text is None
                or quote not in source.text
                or not isinstance(start, int | float)
                or isinstance(start, bool)
                or not isinstance(end, int | float)
                or isinstance(end, bool)
                or not math.isfinite(start)
                or not math.isfinite(end)
                or start != source.video_start_seconds
                or end != source.video_end_seconds
                or not 0 <= start < end <= video.duration_seconds
            ):
                raise AcceptanceFailure("SUMMARY_CITATION_SOURCE_VALIDATION_FAILED")
            citations += 1
        if (
            not transcript_payload.get("complete")
            or len(transcript_payload.get("items", [])) != len(transcript_rows)
            or not summary_payload.get("chapters")
            or not summary_payload.get("key_points")
        ):
            raise AcceptanceFailure("ISOLATED_SAMPLE_READBACK_INVALID")
        return {
            "state": "success",
            "video_id": str(video.id),
            "task_id": str(task.id),
            "model": summary.model,
            "provider": contract.provider,
            "prompt_version": summary.prompt_version,
            "schema_version": summary.structure_version,
            "chapters": len(summary_payload["chapters"]),
            "key_points": len(summary_payload["key_points"]),
            "citations": citations,
            "source_quote_matches": citations,
            "transcript_segments": len(transcript_rows),
            "execution_mode": attempt.execution_mode,
            "asr_requests_on_summary_attempt": sum(row.request_count for row in transcript_rows),
            "asr_requests_on_recovered_parent": sum(row.request_count for row in reused_rows),
            "summary_requests": len(requests),
            "request_seconds": round(sum(row.elapsed_seconds or 0.0 for row in requests), 3),
            "finish_reasons": [row.finish_reason for row in requests],
            "all_requests_audited": all(row.status in {"failed", "succeeded"} for row in requests),
            "source_video_and_legacy_task_preserved": source_task.status == "awaiting_summary",
            "video_sha256_matches_authorized_source": True,
        }


def run_fresh_authorized_copy(source_task_id: uuid.UUID) -> dict[str, Any]:
    settings = _settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    session_id: uuid.UUID | None = None
    try:
        with factory() as session:
            source_task = session.get(AnalysisTask, source_task_id)
            source_video = session.get(Video, source_task.video_id) if source_task is not None else None
            if source_task is None or source_video is None or source_video.status != "ready":
                raise AcceptanceFailure("AUTHORIZED_SOURCE_VIDEO_UNAVAILABLE")
            owner_id = source_task.owner_id
            expected_sha256 = source_video.content_sha256
            expected_byte_count = source_video.byte_count
            source_path = MediaStore(settings.app_data_root).target_path(source_video.storage_name)
            if not source_path.is_file() or source_path.stat().st_size != expected_byte_count:
                raise AcceptanceFailure("AUTHORIZED_SOURCE_FILE_UNAVAILABLE")

        session_id, raw_session, raw_csrf = _create_ephemeral_session(factory, owner_id, settings)
        api_origin = settings.app_origin
        headers = {"Origin": api_origin, "X-CSRF-Token": raw_csrf}
        timeout = httpx.Timeout(connect=15, read=1800, write=1800, pool=15)
        with (
            httpx.Client(base_url=api_origin, timeout=timeout) as client,
            source_path.open("rb") as source_file,
        ):
            client.cookies.set(SESSION_COOKIE, raw_session, path="/")
            client.cookies.set(CSRF_COOKIE, raw_csrf, path="/")
            if client.get("/auth/me").status_code != 200:
                raise AcceptanceFailure("TEMPORARY_OWNER_SESSION_REJECTED")
            upload_headers = dict(headers)
            upload_headers.update(
                {
                    "Content-Type": "application/octet-stream",
                    "X-Video-Filename": "stage4-authorized-sample.mp4",
                }
            )
            upload = client.post("/videos", content=source_file, headers=upload_headers)
            if upload.status_code != 201:
                raise AcceptanceFailure(_safe_api_error(upload))
            upload_payload = upload.json()
            video_payload = upload_payload.get("video") if isinstance(upload_payload, dict) else None
            if not isinstance(video_payload, dict) or not isinstance(video_payload.get("id"), str):
                raise AcceptanceFailure("ISOLATED_SAMPLE_UPLOAD_RESPONSE_INVALID")
            video_id = uuid.UUID(video_payload["id"])
            with factory() as session:
                copied_video = session.get(Video, video_id)
                if (
                    copied_video is None
                    or copied_video.owner_id != owner_id
                    or copied_video.content_sha256 != expected_sha256
                    or copied_video.byte_count != expected_byte_count
                ):
                    raise AcceptanceFailure("ISOLATED_SAMPLE_SOURCE_MISMATCH")

            created = client.post(f"/videos/{video_id}/tasks", headers=headers)
            if created.status_code != 202:
                raise AcceptanceFailure(_safe_api_error(created))
            task_payload = created.json()
            task_id = uuid.UUID(task_payload["id"])
            print(
                json.dumps({"state": "isolated_sample_running", "task_id": str(task_id)}),
                flush=True,
            )
            started = time.monotonic()
            deadline = started + FRESH_SAMPLE_MAX_WAIT_SECONDS
            last_progress = started
            task_state: dict[str, Any] | None = None
            task_endpoint = f"/videos/{video_id}/tasks/{task_id}"
            while time.monotonic() < deadline:
                response = client.get(task_endpoint)
                if response.status_code != 200:
                    raise AcceptanceFailure(_safe_api_error(response))
                polled = response.json()
                if not isinstance(polled, dict):
                    raise AcceptanceFailure("TASK_STATUS_RESPONSE_INVALID")
                task_state = polled
                if polled.get("status") in {"succeeded", "failed"}:
                    break
                if time.monotonic() - last_progress >= 30:
                    print(
                        json.dumps(
                            {
                                "state": "isolated_sample_running",
                                "elapsed_seconds": round(time.monotonic() - started, 1),
                                "stage": polled.get("stage"),
                            }
                        ),
                        flush=True,
                    )
                    last_progress = time.monotonic()
                time.sleep(POLL_SECONDS)
            if task_state is None or task_state.get("status") != "succeeded":
                code = task_state.get("failure_code") if isinstance(task_state, dict) else None
                if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code):
                    raise AcceptanceFailure(code)
                raise AcceptanceFailure("ISOLATED_SAMPLE_DEADLINE_EXCEEDED")
            summary_response = client.get(f"/videos/{video_id}/summary")
            transcript_response = client.get(f"/videos/{video_id}/transcript")
            if summary_response.status_code != 200:
                raise AcceptanceFailure(_safe_api_error(summary_response))
            if transcript_response.status_code != 200:
                raise AcceptanceFailure("TRANSCRIPT_READBACK_FAILED")
            result = _verify_fresh_result(
                factory,
                source_task_id,
                task_id,
                summary_response.json()["payload"],
                transcript_response.json(),
            )
            result["total_seconds"] = round(time.monotonic() - started, 3)
            return result
    finally:
        if session_id is not None:
            _delete_ephemeral_session(factory, session_id)
        engine.dispose()


def recover_failed_isolated_task(source_task_id: uuid.UUID, task_id: uuid.UUID) -> dict[str, Any]:
    settings = _settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    session_id: uuid.UUID | None = None
    try:
        with factory() as session:
            source_task = session.get(AnalysisTask, source_task_id)
            source_video = session.get(Video, source_task.video_id) if source_task is not None else None
            task = session.get(AnalysisTask, task_id)
            video = session.get(Video, task.video_id) if task is not None else None
            parent = (
                session.get(AnalysisAttempt, task.current_attempt_id)
                if task is not None and task.current_attempt_id is not None
                else None
            )
            execution = (
                session.scalar(select(AnalysisExecution).where(AnalysisExecution.attempt_id == parent.id))
                if parent is not None
                else None
            )
            if (
                source_task is None
                or source_video is None
                or task is None
                or video is None
                or parent is None
                or task.status != "failed"
                or task.summary_status != "failed"
                or task.current_attempt_id != parent.id
                or parent.status != "failed"
                or parent.stop_confirmed_at is None
                or execution is None
                or execution.stop_confirmed_at is None
            ):
                raise AcceptanceFailure("ISOLATED_SAMPLE_NOT_RECOVERABLE")
            parent_rows = list(
                session.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.attempt_id == parent.id)
                    .order_by(TranscriptSegment.segment_number)
                )
            )
            if len(parent_rows) != 13 or any(
                row.status != "succeeded" or not row.text or row.request_count < 1 for row in parent_rows
            ):
                raise AcceptanceFailure("ISOLATED_SAMPLE_TRANSCRIPT_NOT_COMPLETE")
            try:
                validate_transcript_manifest(
                    [
                        {
                            "segment_number": row.segment_number,
                            "audio_start_seconds": row.audio_start_seconds,
                            "audio_end_seconds": row.audio_end_seconds,
                            "actual_audio_seconds": row.actual_audio_seconds,
                            "video_start_seconds": row.video_start_seconds,
                            "video_end_seconds": row.video_end_seconds,
                            "audio_sha256": row.audio_sha256,
                            "request_fingerprint": row.request_fingerprint,
                            "status": row.status,
                            "text": row.text,
                        }
                        for row in parent_rows
                    ],
                    snapshot=parent.config_snapshot,
                    video_duration_seconds=video.duration_seconds,
                    expected_manifest_fingerprint=parent.manifest_fingerprint,
                )
            except SummaryFailure as exc:
                raise AcceptanceFailure(f"ISOLATED_SAMPLE_{exc.code}") from exc
            owner_id = task.owner_id
            video_id = video.id
            parent_id = parent.id

        session_id, raw_session, raw_csrf = _create_ephemeral_session(factory, owner_id, settings)
        headers = {"Origin": settings.app_origin, "X-CSRF-Token": raw_csrf}
        started = time.monotonic()
        with httpx.Client(base_url=settings.app_origin, timeout=httpx.Timeout(10)) as client:
            client.cookies.set(SESSION_COOKIE, raw_session, path="/")
            client.cookies.set(CSRF_COOKIE, raw_csrf, path="/")
            if client.get("/auth/me").status_code != 200:
                raise AcceptanceFailure("TEMPORARY_OWNER_SESSION_REJECTED")
            endpoint = f"/videos/{video_id}/tasks/{task_id}"
            recovery = client.post(
                endpoint + "/recover",
                json={"attempt_id": str(parent_id)},
                headers=headers,
            )
            if recovery.status_code != 202:
                raise AcceptanceFailure(_safe_api_error(recovery))
            recovery_payload = recovery.json()
            child = recovery_payload.get("attempt") if isinstance(recovery_payload, dict) else None
            if (
                not isinstance(child, dict)
                or child.get("execution_mode") != "summary_only"
                or recovery_payload.get("reused_segments") != 13
            ):
                raise AcceptanceFailure("ISOLATED_SAMPLE_RECOVERY_DID_NOT_REUSE_TRANSCRIPT")
            print(
                json.dumps(
                    {
                        "state": "summary_recovery_running",
                        "task_id": str(task_id),
                        "asr_requests_on_recovery": 0,
                    }
                ),
                flush=True,
            )
            deadline = started + MAX_WAIT_SECONDS
            last_progress = started
            task_state: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                response = client.get(endpoint)
                if response.status_code != 200:
                    raise AcceptanceFailure(_safe_api_error(response))
                polled = response.json()
                if not isinstance(polled, dict):
                    raise AcceptanceFailure("TASK_STATUS_RESPONSE_INVALID")
                task_state = polled
                if polled.get("status") in {"succeeded", "failed"}:
                    break
                if time.monotonic() - last_progress >= 30:
                    print(
                        json.dumps(
                            {
                                "state": "summary_recovery_running",
                                "elapsed_seconds": round(time.monotonic() - started, 1),
                                "stage": polled.get("stage"),
                            }
                        ),
                        flush=True,
                    )
                    last_progress = time.monotonic()
                time.sleep(POLL_SECONDS)
            if task_state is None or task_state.get("status") != "succeeded":
                code = task_state.get("failure_code") if isinstance(task_state, dict) else None
                if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code):
                    raise AcceptanceFailure(code)
                raise AcceptanceFailure("SUMMARY_RECOVERY_DEADLINE_EXCEEDED")
            summary_response = client.get(f"/videos/{video_id}/summary")
            transcript_response = client.get(f"/videos/{video_id}/transcript")
            if summary_response.status_code != 200:
                raise AcceptanceFailure(_safe_api_error(summary_response))
            if transcript_response.status_code != 200:
                raise AcceptanceFailure("TRANSCRIPT_READBACK_FAILED")
            result = _verify_fresh_result(
                factory,
                source_task_id,
                task_id,
                summary_response.json()["payload"],
                transcript_response.json(),
            )
            result["total_seconds"] = round(time.monotonic() - started, 3)
            result["recovery_reused_transcript"] = True
            return result
    finally:
        if session_id is not None:
            _delete_ephemeral_session(factory, session_id)
        engine.dispose()


def verify_published_task(source_task_id: uuid.UUID, task_id: uuid.UUID) -> dict[str, Any]:
    settings = _settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    session_id: uuid.UUID | None = None
    try:
        with factory() as session:
            task = session.get(AnalysisTask, task_id)
            if task is None or task.current_attempt_id is None:
                raise AcceptanceFailure("PUBLISHED_TASK_UNAVAILABLE")
            video = session.get(Video, task.video_id)
            if video is None:
                raise AcceptanceFailure("PUBLISHED_TASK_UNAVAILABLE")
            owner_id = task.owner_id
            video_id = video.id
        session_id, raw_session, raw_csrf = _create_ephemeral_session(factory, owner_id, settings)
        with httpx.Client(base_url=settings.app_origin, timeout=httpx.Timeout(10)) as client:
            client.cookies.set(SESSION_COOKIE, raw_session, path="/")
            client.cookies.set(CSRF_COOKIE, raw_csrf, path="/")
            if client.get("/auth/me").status_code != 200:
                raise AcceptanceFailure("TEMPORARY_OWNER_SESSION_REJECTED")
            summary_response = client.get(f"/videos/{video_id}/summary")
            transcript_response = client.get(f"/videos/{video_id}/transcript")
            if summary_response.status_code != 200:
                raise AcceptanceFailure(_safe_api_error(summary_response))
            if transcript_response.status_code != 200:
                raise AcceptanceFailure("TRANSCRIPT_READBACK_FAILED")
            return _verify_fresh_result(
                factory,
                source_task_id,
                task_id,
                summary_response.json()["payload"],
                transcript_response.json(),
            )
    finally:
        if session_id is not None:
            _delete_ephemeral_session(factory, session_id)
        engine.dispose()


def run(task_id: uuid.UUID, *, preflight_only: bool = False) -> dict[str, Any]:
    settings = _settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    session_id: uuid.UUID | None = None
    try:
        before = _prepare(factory, task_id)
        if preflight_only:
            return {
                "state": "preflight_ok",
                "video_status": before["video_status"],
                "transcript_segments": 13,
                "provider": before["provider"],
                "model": before["model"],
                "prompt_version": before["prompt_version"],
                "schema_version": before["schema_version"],
                "legacy_compatibility": before["legacy_compatibility"],
            }
        session_id, raw_session, raw_csrf = _create_ephemeral_session(factory, before["owner_id"], settings)
        api_origin = settings.app_origin
        headers = {"Origin": api_origin, "X-CSRF-Token": raw_csrf}
        started = time.monotonic()
        with httpx.Client(base_url=api_origin, timeout=httpx.Timeout(10)) as client:
            client.cookies.set(SESSION_COOKIE, raw_session, path="/")
            client.cookies.set(CSRF_COOKIE, raw_csrf, path="/")
            identity = client.get("/auth/me")
            if identity.status_code != 200:
                raise AcceptanceFailure("TEMPORARY_OWNER_SESSION_REJECTED")
            endpoint = f"/videos/{before['video_id']}/tasks/{before['task_id']}"
            continuation = client.post(
                endpoint + "/summarize",
                json={"attempt_id": str(before["parent_attempt_id"])},
                headers=headers,
            )
            if continuation.status_code != 202:
                raise AcceptanceFailure(_safe_api_error(continuation))
            child_id = uuid.UUID(continuation.json()["attempt"]["id"])
            repeated = client.post(
                endpoint + "/summarize",
                json={"attempt_id": str(before["parent_attempt_id"])},
                headers=headers,
            )
            if repeated.status_code != 202:
                raise AcceptanceFailure("SUMMARY_CONTINUATION_NOT_IDEMPOTENT")
            repeated_payload = repeated.json()
            repeated_attempt = repeated_payload.get("attempt") if isinstance(repeated_payload, dict) else None
            if not isinstance(repeated_attempt, dict) or repeated_attempt.get("id") != str(child_id):
                raise AcceptanceFailure("SUMMARY_CONTINUATION_NOT_IDEMPOTENT")

            print(json.dumps({"state": "summary_running", "model": before["model"]}), flush=True)
            deadline = time.monotonic() + MAX_WAIT_SECONDS
            last_progress = time.monotonic()
            task_state: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                response = client.get(endpoint)
                if response.status_code != 200:
                    raise AcceptanceFailure(_safe_api_error(response))
                polled = response.json()
                if not isinstance(polled, dict):
                    raise AcceptanceFailure("TASK_STATUS_RESPONSE_INVALID")
                task_state = polled
                if polled.get("status") in {"succeeded", "failed"}:
                    break
                if time.monotonic() - last_progress >= 30:
                    print(
                        json.dumps(
                            {
                                "state": "summary_running",
                                "elapsed_seconds": round(time.monotonic() - started, 1),
                            }
                        ),
                        flush=True,
                    )
                    last_progress = time.monotonic()
                time.sleep(POLL_SECONDS)
            if task_state is None or task_state.get("status") != "succeeded":
                with factory() as session:
                    task = session.get(AnalysisTask, task_id)
                    code = task.failure_code if task is not None else None
                if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", code):
                    raise AcceptanceFailure(code)
                raise AcceptanceFailure("SUMMARY_WORKER_DEADLINE_EXCEEDED")

            summary_response = client.get(f"/videos/{before['video_id']}/summary")
            transcript_response = client.get(f"/videos/{before['video_id']}/transcript")
            if summary_response.status_code != 200:
                raise AcceptanceFailure(_safe_api_error(summary_response))
            if transcript_response.status_code != 200:
                raise AcceptanceFailure("TRANSCRIPT_READBACK_FAILED")
            result = _verify_published_result(
                factory,
                before,
                child_id,
                summary_response.json()["payload"],
                transcript_response.json(),
            )
            result.update(
                {
                    "state": "success",
                    "total_seconds": round(time.monotonic() - started, 3),
                    "provider": before["provider"],
                    "legacy_compatibility": before["legacy_compatibility"],
                    "continuation_idempotent": True,
                }
            )
            return result
    finally:
        if session_id is not None:
            _delete_ephemeral_session(factory, session_id)
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the authorized Stage 4 real-model summary acceptance using a temporary session."
    )
    parser.add_argument("task_id", type=uuid.UUID)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate the authorized persisted task without creating a session or calling the API.",
    )
    parser.add_argument(
        "--fresh-authorized-copy",
        action="store_true",
        help="Upload the authorized source as an isolated sample and run the normal full API/Worker path.",
    )
    parser.add_argument(
        "--verify-published-task",
        action="store_true",
        help="Re-read a published isolated task through the API after a service restart.",
    )
    parser.add_argument(
        "--recover-failed-isolated-task",
        action="store_true",
        help="Manually recover a confirmed stopped isolated task while reusing its complete transcript.",
    )
    parser.add_argument(
        "--source-task-id",
        type=uuid.UUID,
        help="The preserved legacy task used as the authorized source and isolation reference.",
    )
    arguments = parser.parse_args()
    try:
        if arguments.fresh_authorized_copy:
            result = run_fresh_authorized_copy(arguments.task_id)
        elif arguments.recover_failed_isolated_task:
            if arguments.source_task_id is None:
                raise AcceptanceFailure("SOURCE_TASK_ID_REQUIRED")
            result = recover_failed_isolated_task(arguments.source_task_id, arguments.task_id)
        elif arguments.verify_published_task:
            if arguments.source_task_id is None:
                raise AcceptanceFailure("SOURCE_TASK_ID_REQUIRED")
            result = verify_published_task(arguments.source_task_id, arguments.task_id)
        else:
            result = run(arguments.task_id, preflight_only=arguments.preflight_only)
    except AcceptanceFailure as exc:
        print(json.dumps({"state": "failed", "code": exc.code}), flush=True)
        return 1
    except httpx.RequestError:
        print(json.dumps({"state": "failed", "code": "API_CONNECTION_FAILED"}), flush=True)
        return 1
    except Exception:
        print(json.dumps({"state": "failed", "code": "ACCEPTANCE_INTERNAL_ERROR"}), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
