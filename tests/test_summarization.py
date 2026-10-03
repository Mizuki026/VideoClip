from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest

from videoclip.llm_client import LlmCallError, request_completion
from videoclip.summarization import (
    QUOTE_NORMALIZATION_VERSION,
    SUMMARY_LLM_FINGERPRINT_VERSION,
    SummaryContract,
    SummaryFailure,
    build_summary_prompt,
    canonical_sha256,
    correction_for_failure,
    is_repairable_summary_failure,
    normalize_quote,
    resolve_summary_contract,
    response_schema_sha256,
    summary_request_fingerprint,
    template_sha256,
    validate_context_budget,
    validate_summary_output,
    validate_transcript_context_budget,
    validate_transcript_manifest,
)


def _legacy_snapshot() -> dict[str, Any]:
    return {
        "version": "analysis-config-v1",
        "asr": {"base_url": "https://api.siliconflow.cn/v1"},
        "summary": {
            "provider": "siliconflow",
            "model": "Qwen/Qwen3.5-4B",
            "prompt_version": "stage0-summary-v4",
            "schema_version": "summary-v1",
            "context_tokens": 256000,
            "max_output_tokens": 3072,
        },
        "limits": {
            "http_total_seconds": 240,
            "http_connect_seconds": 15,
            "http_read_seconds": 45,
            "response_max_bytes": 1_048_576,
            "diagnostic_max_bytes": 8192,
        },
    }


def _segments() -> list[dict[str, Any]]:
    return [
        {
            "segment_number": 0,
            "audio_start_seconds": 0.0,
            "audio_end_seconds": 30.0,
            "actual_audio_seconds": 30.0,
            "video_start_seconds": 0.0,
            "video_end_seconds": 30.0,
            "audio_sha256": "a" * 64,
            "request_fingerprint": "b" * 64,
            "status": "succeeded",
            "text": "FastAPI 通过依赖注入完成参数校验，并由 PostgreSQL 保存任务状态。" * 2,
        },
        {
            "segment_number": 1,
            "audio_start_seconds": 30.0,
            "audio_end_seconds": 60.0,
            "actual_audio_seconds": 30.0,
            "video_start_seconds": 30.0,
            "video_end_seconds": 60.0,
            "audio_sha256": "c" * 64,
            "request_fingerprint": "d" * 64,
            "status": "succeeded",
            "text": "索引能够减少数据库扫描，但需要结合查询条件和写入成本设计。" * 2,
        },
    ]


def _manifest_fingerprint(rows: list[dict[str, Any]]) -> str:
    return canonical_sha256(
        {
            "version": "pcm-s16le-slices-v1",
            "timeline_mapping_version": "zero-offset-v1",
            "audio": {"sample_rate": 16000, "channels": 1, "sample_format": "s16le"},
            "source_fingerprint": "e" * 64,
            "segments": [
                {
                    key: row[key]
                    for key in (
                        "segment_number",
                        "audio_start_seconds",
                        "audio_end_seconds",
                        "actual_audio_seconds",
                        "video_start_seconds",
                        "video_end_seconds",
                        "audio_sha256",
                        "request_fingerprint",
                    )
                }
                for row in rows
            ],
        }
    )


def _manifest_snapshot() -> dict[str, Any]:
    snapshot = _legacy_snapshot()
    snapshot["source"] = {"fingerprint": "e" * 64}
    snapshot["slicing"] = {
        "manifest_version": "pcm-s16le-slices-v1",
        "timeline_mapping_version": "zero-offset-v1",
        "segment_seconds": 30,
    }
    snapshot["audio"] = {"sample_rate": 16000, "channels": 1, "sample_format": "s16le"}
    return snapshot


def _valid_model_output(segment_id: int = 0) -> str:
    return json.dumps(
        {
            "chapters": [
                {"title": f"章节标题 {index}", "evidence": [{"segment_id": segment_id}]} for index in range(3)
            ],
            "key_points": [
                {"text": f"知识要点 {index}", "evidence": [{"segment_id": segment_id}]} for index in range(3)
            ],
        },
        ensure_ascii=False,
    )


def test_legacy_snapshot_uses_only_the_pinned_measured_compatibility_contract() -> None:
    contract = resolve_summary_contract(_legacy_snapshot())
    assert contract.legacy_compatibility
    assert contract.model == "Qwen/Qwen3.5-4B"
    assert contract.temperature == 0.2
    assert contract.enable_thinking is False
    assert contract.timeouts == {"total": 240.0, "connect": 15.0, "read": 45.0, "write": 45.0, "pool": 15.0}

    changed = _legacy_snapshot()
    changed["summary"]["model"] = "Pro/other-model"
    with pytest.raises(SummaryFailure, match="SUMMARY_CONFIGURATION_INCOMPATIBLE"):
        resolve_summary_contract(changed)

    changed = _legacy_snapshot()
    changed["limits"].pop("http_read_seconds")
    with pytest.raises(SummaryFailure, match="SUMMARY_CONFIGURATION_INCOMPATIBLE"):
        resolve_summary_contract(changed)


def test_transcript_manifest_requires_complete_ordered_and_matching_source_rows() -> None:
    rows = _segments()
    snapshot = _manifest_snapshot()
    manifest = _manifest_fingerprint(rows)
    valid, transcript_hash = validate_transcript_manifest(
        rows,
        snapshot=snapshot,
        video_duration_seconds=60,
        expected_manifest_fingerprint=manifest,
    )
    assert [row["segment_number"] for row in valid] == [0, 1]
    assert len(transcript_hash) == 64

    for broken in (rows[:1], list(reversed(rows))):
        with pytest.raises(SummaryFailure):
            validate_transcript_manifest(
                broken,
                snapshot=snapshot,
                video_duration_seconds=60,
                expected_manifest_fingerprint=manifest,
            )

    duplicated = [rows[0], {**rows[1], "segment_number": 0}]
    with pytest.raises(SummaryFailure, match="TRANSCRIPT_MANIFEST_INCOMPLETE"):
        validate_transcript_manifest(
            duplicated,
            snapshot=snapshot,
            video_duration_seconds=60,
            expected_manifest_fingerprint=manifest,
        )

    missing = [{**rows[0], "status": "expected"}]
    with pytest.raises(SummaryFailure, match="TRANSCRIPT_MANIFEST_INCOMPLETE"):
        validate_transcript_manifest(
            missing,
            snapshot=snapshot,
            video_duration_seconds=30,
            expected_manifest_fingerprint="f" * 64,
        )

    wrong_source = {**rows[1], "request_fingerprint": "f" * 64}
    with pytest.raises(SummaryFailure, match="TRANSCRIPT_SOURCE_MISMATCH"):
        validate_transcript_manifest(
            [rows[0], wrong_source],
            snapshot=snapshot,
            video_duration_seconds=60,
            expected_manifest_fingerprint=manifest,
        )


def test_transcript_rejects_nonfinite_or_invalid_time_values() -> None:
    rows = _segments()
    rows[0]["video_end_seconds"] = math.nan
    with pytest.raises(SummaryFailure, match="TRANSCRIPT_TIME_INVALID"):
        validate_transcript_manifest(
            rows,
            snapshot=_manifest_snapshot(),
            video_duration_seconds=60,
            expected_manifest_fingerprint="f" * 64,
        )


def test_summary_schema_derives_short_quotes_and_time_from_one_segment() -> None:
    rows = _segments()
    validated = validate_summary_output(_valid_model_output(), rows, video_duration_seconds=60)
    evidence = validated["chapters"][0]["evidence"][0]
    assert evidence["segment_id"] == 0
    assert evidence["quote"] in rows[0]["text"]
    assert len(evidence["quote"]) <= 48
    assert evidence["start_seconds"] == 0
    assert evidence["end_seconds"] == 30

    with pytest.raises(SummaryFailure, match="SUMMARY_INVALID_SCHEMA"):
        validate_summary_output(
            _valid_model_output().replace('"segment_id": 0', '"segment_id": 0, "quote": "fabricated"', 1),
            rows,
            video_duration_seconds=60,
        )
    with pytest.raises(SummaryFailure, match="SUMMARY_UNKNOWN_SEGMENT"):
        validate_summary_output(_valid_model_output(999), rows, video_duration_seconds=60)
    with pytest.raises(SummaryFailure, match="SUMMARY_INVALID_SCHEMA"):
        validate_summary_output(
            _valid_model_output().replace('"title": "章节标题 0"', '"title": "  "', 1),
            rows,
            video_duration_seconds=60,
        )

    invalid_times = [{**rows[0], "video_end_seconds": 61.0}, rows[1]]
    with pytest.raises(SummaryFailure, match="TRANSCRIPT_TIME_INVALID"):
        validate_summary_output(_valid_model_output(), invalid_times, video_duration_seconds=60)


def test_prompt_budget_and_request_fingerprint_include_contract_and_repair_feedback() -> None:
    contract = resolve_summary_contract(_legacy_snapshot())
    prompt = build_summary_prompt(_segments(), contract.prompt_version)
    assert "[0] 0.000-30.000s" in prompt
    assert "不要输出 quote 或时间" in prompt
    assert normalize_quote(" Ａ  B\n") == "ab"
    assert validate_context_budget(prompt, contract) < contract.context_tokens

    small_context = SummaryContract(**{**contract.__dict__, "context_tokens": 1})
    with pytest.raises(SummaryFailure, match="SUMMARY_CONTEXT_EXCEEDS_LIMIT"):
        validate_context_budget(prompt, small_context)
    with pytest.raises(SummaryFailure, match="SUMMARY_CONTEXT_EXCEEDS_LIMIT"):
        validate_transcript_context_budget([{"text": "x" * contract.context_tokens}], contract)

    first = summary_request_fingerprint(
        contract=contract, transcript_fingerprint="a" * 64, correction_code=None
    )
    repaired = summary_request_fingerprint(
        contract=contract, transcript_fingerprint="a" * 64, correction_code="SUMMARY_INVALID_SCHEMA"
    )
    assert first != repaired
    assert len(first) == 64 and SUMMARY_LLM_FINGERPRINT_VERSION
    assert template_sha256("stage0-summary-v4", "summary-v1") == contract.template_sha256
    assert contract.template_sha256 == "2d3fc767670fce33a83bbfa93209cd1c0087ee7f50581ede1a7f613ff1354949"
    assert correction_for_failure("SUMMARY_INVALID_SCHEMA").startswith("SUMMARY_INVALID_SCHEMA:")
    assert is_repairable_summary_failure("LLM_OUTPUT_TRUNCATED")
    assert is_repairable_summary_failure("SUMMARY_INVALID_JSON")
    assert not is_repairable_summary_failure("LLM_RATE_LIMITED")


def test_versioned_summary_contract_preserves_v4_and_accepts_bounded_v5_output_budget() -> None:
    legacy = resolve_summary_contract(_legacy_snapshot())
    assert legacy.max_output_tokens == 3072
    assert legacy.legacy_compatibility is True

    snapshot = _legacy_snapshot()
    snapshot["summary"]["max_output_tokens"] = 6144
    snapshot["summary"]["request"] = {
        "contract_version": "summary-request-v1",
        "base_url": "https://api.siliconflow.cn/v1",
        "temperature": 0.2,
        "enable_thinking": False,
        "timeouts": {"total": 300, "connect": 15, "read": 240, "write": 45, "pool": 15},
        "max_response_bytes": 1_048_576,
        "max_diagnostic_bytes": 8192,
        "max_requests": 3,
        "context_overhead_tokens": 64,
        "template_sha256": template_sha256("stage0-summary-v4", "summary-v1"),
        "response_schema_sha256": response_schema_sha256(),
        "quote_normalization_version": QUOTE_NORMALIZATION_VERSION,
    }

    contract = resolve_summary_contract(snapshot)
    assert contract.max_output_tokens == 6144
    assert contract.legacy_compatibility is False
    prompt = build_summary_prompt(_segments(), contract.prompt_version)
    assert validate_context_budget(prompt, contract) < contract.context_tokens

    new_task = _legacy_snapshot()
    new_task["summary"]["prompt_version"] = "stage0-summary-v5"
    new_task["summary"]["max_output_tokens"] = 8192
    new_task["summary"]["request"] = {
        **snapshot["summary"]["request"],
        "template_sha256": template_sha256("stage0-summary-v5", "summary-v1"),
    }
    bounded_contract = resolve_summary_contract(new_task)
    bounded_prompt = build_summary_prompt(_segments(), bounded_contract.prompt_version)
    assert bounded_contract.max_output_tokens == 8192
    assert bounded_contract.prompt_version == "stage0-summary-v5"
    assert "固定返回 3 个章节和 3 条知识要点" in bounded_prompt
    assert "每条知识要点不超过 80 个汉字" in bounded_prompt
    assert validate_context_budget(bounded_prompt, bounded_contract) < bounded_contract.context_tokens


def _request() -> dict[str, Any]:
    return {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3.5-4B",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "prompt"},
        ],
        "temperature": 0.2,
        "max_output_tokens": 3072,
        "enable_thinking": False,
        "timeouts": {"total": 10, "connect": 2, "read": 3, "write": 3, "pool": 2},
        "max_response_bytes": 4096,
    }


def test_llm_client_sends_frozen_request_and_returns_bounded_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer private-test-token"
        assert payload["temperature"] == 0.2
        assert payload["enable_thinking"] is False
        assert payload["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            headers={"x-siliconcloud-trace-id": "trace-123"},
            json={
                "model": "Qwen/Qwen3.5-4B",
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16, "extra": 99},
            },
        )

    result = request_completion(
        _request(), api_key="private-test-token", transport=httpx.MockTransport(handler)
    )
    assert result["content"] == "{}"
    assert result["metadata"]["trace_id"] == "trace-123"
    assert result["metadata"]["usage"] == {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16}


@pytest.mark.parametrize(
    ("status", "error_code"),
    [(401, "LLM_AUTHENTICATION_FAILED"), (404, "LLM_MODEL_UNAVAILABLE"), (429, "LLM_RATE_LIMITED")],
)
def test_llm_client_maps_provider_errors_without_storing_body(status: int, error_code: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "sensitive provider diagnostic"}})

    with pytest.raises(LlmCallError) as error:
        request_completion(_request(), api_key="private-test-token", transport=httpx.MockTransport(handler))
    assert error.value.code == error_code
    assert "sensitive" not in str(error.value)


def test_llm_client_rejects_oversized_response_and_malformed_completion() -> None:
    too_large = {**_request(), "max_response_bytes": 8}
    with pytest.raises(LlmCallError, match="LLM_RESPONSE_TOO_LARGE"):
        request_completion(
            too_large,
            api_key="private-test-token",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 20)),
        )
    with pytest.raises(LlmCallError, match="LLM_INVALID_RESPONSE"):
        request_completion(
            _request(),
            api_key="private-test-token",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": []})),
        )
