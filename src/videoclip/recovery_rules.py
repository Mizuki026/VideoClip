from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

QUEUE_RECOVERY_GRACE_SECONDS = 300


@dataclass(frozen=True)
class RecoveryEligibility:
    eligible: bool
    error_code: str | None
    available_at: datetime | None = None


def evaluate_recovery(
    *,
    video_status: str,
    task_status: str,
    attempt_status: str,
    delivery_status: str,
    claim_count: int,
    delivery_updated_at: datetime | None,
    attempt_stop_confirmed: bool,
    execution_stop_confirmed: bool,
    is_current_attempt: bool,
    now: datetime,
) -> RecoveryEligibility:
    """Classify explicit recovery eligibility without treating timeouts as stop proof."""
    if video_status != "ready":
        return RecoveryEligibility(False, "VIDEO_DELETION_IN_PROGRESS")
    if task_status == "awaiting_summary" or attempt_status == "awaiting_summary":
        return RecoveryEligibility(False, "TRANSCRIPT_COMPLETE_AWAITING_SUMMARY")
    if not is_current_attempt:
        return RecoveryEligibility(False, "ATTEMPT_NOT_CURRENT")

    if claim_count == 0:
        if attempt_status == "queued" and task_status == "queued":
            if delivery_status in {"pending", "unknown", "failed"}:
                return RecoveryEligibility(True, None)
            if delivery_status == "sent":
                if delivery_updated_at is None:
                    return RecoveryEligibility(False, "DELIVERY_FACT_INCOMPLETE")
                available_at = delivery_updated_at + timedelta(seconds=QUEUE_RECOVERY_GRACE_SECONDS)
                if now >= available_at:
                    return RecoveryEligibility(True, None, available_at)
                return RecoveryEligibility(False, "DELIVERY_NOT_STALE", available_at)
            return RecoveryEligibility(False, "DELIVERY_STATE_UNSUPPORTED")
        if attempt_status == "failed" and task_status == "failed":
            return RecoveryEligibility(True, None)
        return RecoveryEligibility(False, "ATTEMPT_NOT_RECOVERABLE")

    if claim_count != 1:
        return RecoveryEligibility(False, "ATTEMPT_STATE_INVALID")
    if attempt_stop_confirmed and execution_stop_confirmed:
        if attempt_status in {"failed", "running"} and task_status in {"failed", "running"}:
            return RecoveryEligibility(True, None)
    return RecoveryEligibility(False, "STOP_CONFIRMATION_REQUIRED")


def checkpoint_matches(
    previous: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    previous_manifest_fingerprint: str | None,
    expected_manifest_fingerprint: str,
) -> bool:
    """Reuse only a committed successful row from the exact same immutable manifest."""
    if (
        previous_manifest_fingerprint is None
        or previous_manifest_fingerprint != expected_manifest_fingerprint
        or previous.get("status") != "succeeded"
        or not isinstance(previous.get("text"), str)
    ):
        return False
    fields = (
        "segment_number",
        "audio_start_seconds",
        "audio_end_seconds",
        "actual_audio_seconds",
        "video_start_seconds",
        "video_end_seconds",
        "audio_sha256",
        "request_fingerprint",
    )
    return all(previous.get(field) == expected.get(field) for field in fields)
