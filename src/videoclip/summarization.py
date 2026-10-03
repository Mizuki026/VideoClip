from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

LEGACY_PROMPT_VERSION = "stage0-summary-v4"
PROMPT_VERSION = "stage0-summary-v5"
SUMMARY_SCHEMA_VERSION = "summary-v1"
SUMMARY_REQUEST_CONTRACT_VERSION = "summary-request-v1"
SUMMARY_TEMPLATE_HASH_VERSION = "summary-template-v1"
SUMMARY_TRANSCRIPT_HASH_VERSION = "summary-transcript-v1"
SUMMARY_LLM_FINGERPRINT_VERSION = "summary-llm-request-v1"
QUOTE_NORMALIZATION_VERSION = "nfkc-casefold-whitespace-v1"
MAX_SUMMARY_REQUESTS = 3
CHAT_MESSAGE_OVERHEAD_TOKENS = 64
SUPPORTED_FREE_MODEL = "Qwen/Qwen3.5-4B"
SUPPORTED_BASE_URL = "https://api.siliconflow.cn/v1"
SYSTEM_PROMPT = "输出严格 JSON。不得臆造引文或时间。"

SUMMARY_SCHEMA: dict[str, Any] = {
    "chapters": [{"title": "章节标题", "evidence": [{"segment_id": 0}]}],
    "key_points": [{"text": "知识要点", "evidence": [{"segment_id": 0}]}],
}

SUMMARY_RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "name": "course_summary",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "chapters": {
                "type": "array",
                "minItems": 3,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {"segment_id": {"type": "integer", "minimum": 0}},
                                "required": ["segment_id"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["title", "evidence"],
                    "additionalProperties": False,
                },
            },
            "key_points": {
                "type": "array",
                "minItems": 3,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {"segment_id": {"type": "integer", "minimum": 0}},
                                "required": ["segment_id"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["text", "evidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["chapters", "key_points"],
        "additionalProperties": False,
    },
}

_PROMPT_INSTRUCTIONS_V4 = (
    "你是中文技术课程助教。只依据完整转写整理章节和知识要点，不补充视频中未讲内容。"
    "返回 3–6 个简洁章节和 3–8 条简洁知识要点。每项只给 1 条最直接的引用。"
    "严格返回键为 chapters 与 key_points 的 JSON 对象；章节字段只能是 title 与 evidence，"
    "知识要点字段只能是 text 与 evidence。"
    "章节数量为 3–6，知识要点数量为 3–8。每项 evidence 必须是单元素数组，元素只能有整数 segment_id。"
    "每项只输出一个来自完整转写的 segment_id，选择最能支持该项的单个片段。"
    "不要输出 quote 或时间；程序会从所选片段中提取最相关的连续原文作为 quote，"
    "并按片段推导时间。"
)
_PROMPT_INSTRUCTIONS_V5 = (
    "你是中文技术课程助教。只依据完整转写整理章节和知识要点，不补充视频中未讲内容。"
    "固定返回 3 个章节和 3 条知识要点；章节标题不超过 24 个汉字，每条知识要点不超过 80 个汉字。"
    "严格返回键为 chapters 与 key_points 的 JSON 对象；章节字段只能是 title 与 evidence，"
    "知识要点字段只能是 text 与 evidence。每项 evidence 必须是单元素数组，元素只能有整数 segment_id。"
    "每项只选择一个来自完整转写的最直接有效 segment_id。不要输出 quote 或时间；程序会提取引用原文和时间。"
    "不要输出解释、Markdown、额外字段或重复内容。"
)
_PROMPT_INSTRUCTIONS_BY_VERSION = {
    LEGACY_PROMPT_VERSION: _PROMPT_INSTRUCTIONS_V4,
    PROMPT_VERSION: _PROMPT_INSTRUCTIONS_V5,
}
_REPAIR_INSTRUCTIONS = (
    "请返回完整且严格符合指定 JSON 结构的对象。每条内容只引用一个有效 segment_id，"
    "不要输出 quote 或时间，不要增加任何字段。"
)


class SummaryFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SummaryContract:
    provider: str
    model: str
    base_url: str
    prompt_version: str
    schema_version: str
    context_tokens: int
    max_output_tokens: int
    temperature: float
    enable_thinking: bool
    timeouts: dict[str, float]
    max_response_bytes: int
    max_diagnostic_bytes: int
    max_requests: int
    template_sha256: str
    response_schema_sha256: str
    legacy_compatibility: bool


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _template_document(prompt_version: str, schema_version: str) -> dict[str, Any]:
    instructions = _PROMPT_INSTRUCTIONS_BY_VERSION.get(prompt_version)
    if instructions is None or schema_version != SUMMARY_SCHEMA_VERSION:
        raise SummaryFailure("SUMMARY_TEMPLATE_UNSUPPORTED")
    return {
        "version": SUMMARY_TEMPLATE_HASH_VERSION,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "system_prompt": SYSTEM_PROMPT,
        "instructions": instructions,
        "repair_instructions": _REPAIR_INSTRUCTIONS,
        "example": SUMMARY_SCHEMA,
        "response_schema": SUMMARY_RESPONSE_JSON_SCHEMA,
    }


def template_sha256(prompt_version: str, schema_version: str) -> str:
    return canonical_sha256(_template_document(prompt_version, schema_version))


def response_schema_sha256() -> str:
    return canonical_sha256(SUMMARY_RESPONSE_JSON_SCHEMA)


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _positive_number(value: Any) -> bool:
    return _finite_number(value) and float(value) > 0


def resolve_summary_contract(snapshot: Mapping[str, Any]) -> SummaryContract:
    """Resolve only explicitly versioned requests or the measured legacy contract."""
    summary = snapshot.get("summary")
    limits = snapshot.get("limits")
    if not isinstance(summary, Mapping) or not isinstance(limits, Mapping):
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
    prompt_version = summary.get("prompt_version")
    schema_version = summary.get("schema_version")
    if not isinstance(prompt_version, str) or not isinstance(schema_version, str):
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
    try:
        template_hash = template_sha256(prompt_version, schema_version)
        schema_hash = response_schema_sha256()
    except SummaryFailure as exc:
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE") from exc

    if summary.get("provider") != "siliconflow" or summary.get("model") != SUPPORTED_FREE_MODEL:
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
    context_tokens = summary.get("context_tokens")
    max_output_tokens = summary.get("max_output_tokens")
    if (
        type(context_tokens) is not int
        or context_tokens <= 0
        or type(max_output_tokens) is not int
        or max_output_tokens <= 0
        or max_output_tokens >= context_tokens
    ):
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")

    request = summary.get("request")
    legacy = request is None
    if legacy:
        # analysis-config-v1 never persisted these request parameters. The only
        # supported bridge is the measured stage0-summary-v4 / summary-v1 contract.
        asr = snapshot.get("asr")
        base_url = asr.get("base_url") if isinstance(asr, Mapping) else None
        total = limits.get("http_total_seconds")
        connect = limits.get("http_connect_seconds")
        read = limits.get("http_read_seconds")
        response_limit = limits.get("response_max_bytes")
        diagnostic_limit = limits.get("diagnostic_max_bytes")
        if (
            snapshot.get("version") != "analysis-config-v1"
            or summary.get("prompt_version") != LEGACY_PROMPT_VERSION
            or summary.get("schema_version") != SUMMARY_SCHEMA_VERSION
            or context_tokens != 256_000
            or max_output_tokens != 3072
            or not isinstance(base_url, str)
            or base_url != SUPPORTED_BASE_URL
            or not all(_positive_number(value) for value in (total, connect, read))
            or type(response_limit) is not int
            or response_limit <= 0
            or type(diagnostic_limit) is not int
            or diagnostic_limit <= 0
        ):
            raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
        request = {
            "contract_version": "stage0-summary-v4-legacy-compat-v1",
            "base_url": base_url,
            "temperature": 0.2,
            "enable_thinking": False,
            "timeouts": {
                "total": float(cast(int | float, total)),
                "connect": float(cast(int | float, connect)),
                "read": float(cast(int | float, read)),
                "write": float(cast(int | float, read)),
                "pool": float(cast(int | float, connect)),
            },
            "max_response_bytes": response_limit,
            "max_diagnostic_bytes": diagnostic_limit,
            "max_requests": MAX_SUMMARY_REQUESTS,
            "context_overhead_tokens": CHAT_MESSAGE_OVERHEAD_TOKENS,
            "template_sha256": template_hash,
            "response_schema_sha256": schema_hash,
            "quote_normalization_version": QUOTE_NORMALIZATION_VERSION,
        }
    if not isinstance(request, Mapping):
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
    request_version = request.get("contract_version")
    if request_version not in {"summary-request-v1", "stage0-summary-v4-legacy-compat-v1"}:
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
    timeouts = request.get("timeouts")
    if (
        not isinstance(timeouts, Mapping)
        or set(timeouts) != {"total", "connect", "read", "write", "pool"}
        or not all(_positive_number(timeouts[key]) for key in timeouts)
    ):
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
    temperature = request.get("temperature")
    enable_thinking = request.get("enable_thinking")
    max_response_bytes = request.get("max_response_bytes")
    max_diagnostic_bytes = request.get("max_diagnostic_bytes")
    max_requests = request.get("max_requests")
    if (
        not _finite_number(temperature)
        or not 0 <= float(cast(int | float, temperature)) <= 2
        or type(enable_thinking) is not bool
        or type(max_response_bytes) is not int
        or max_response_bytes <= 0
        or type(max_diagnostic_bytes) is not int
        or max_diagnostic_bytes <= 0
        or max_requests != MAX_SUMMARY_REQUESTS
        or request.get("context_overhead_tokens") != CHAT_MESSAGE_OVERHEAD_TOKENS
        or request.get("template_sha256") != template_hash
        or request.get("response_schema_sha256") != schema_hash
        or request.get("quote_normalization_version") != QUOTE_NORMALIZATION_VERSION
    ):
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
    assert isinstance(timeouts, Mapping)
    assert _finite_number(temperature)
    assert type(enable_thinking) is bool
    base_url = request.get("base_url")
    if base_url != SUPPORTED_BASE_URL:
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
    if request_version == "summary-request-v1" and not isinstance(request, dict):
        raise SummaryFailure("SUMMARY_CONFIGURATION_INCOMPATIBLE")
    return SummaryContract(
        provider="siliconflow",
        model=SUPPORTED_FREE_MODEL,
        base_url=SUPPORTED_BASE_URL,
        prompt_version=prompt_version,
        schema_version=schema_version,
        context_tokens=context_tokens,
        max_output_tokens=max_output_tokens,
        temperature=float(cast(int | float, temperature)),
        enable_thinking=enable_thinking,
        timeouts={key: float(value) for key, value in timeouts.items()},
        max_response_bytes=max_response_bytes,
        max_diagnostic_bytes=max_diagnostic_bytes,
        max_requests=MAX_SUMMARY_REQUESTS,
        template_sha256=template_hash,
        response_schema_sha256=schema_hash,
        legacy_compatibility=legacy,
    )


def normalize_quote(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(normalized.split())


def citation_options(text: str) -> list[str]:
    source = text.strip()
    if not source:
        return []
    if len(source) <= 48:
        return [source]
    options: list[str] = []
    start = 0
    while start < len(source):
        excerpt = source[start : start + 48]
        if len(excerpt) >= 12 and excerpt not in options:
            options.append(excerpt)
        start += 36
    tail = source[max(0, len(source) - 48) :]
    if len(tail) >= 12 and tail not in options:
        options.append(tail)
    return options


def best_source_excerpt(summary_text: str, transcript_text: str) -> str:
    target = re.sub(r"[^\w\u4e00-\u9fff]", "", normalize_quote(summary_text))
    best_quote = ""
    best_score = -1
    for quote in citation_options(transcript_text):
        candidate = re.sub(r"[^\w\u4e00-\u9fff]", "", normalize_quote(quote))
        previous = [0] * (len(target) + 1)
        score = 0
        for character in candidate:
            current = [0] * (len(target) + 1)
            for index, target_character in enumerate(target, 1):
                if character == target_character:
                    current[index] = previous[index - 1] + 1
                    score = max(score, current[index])
            previous = current
        if score > best_score:
            best_quote, best_score = quote, score
    return best_quote


def validate_transcript_manifest(
    rows: Sequence[Mapping[str, Any]],
    *,
    snapshot: Mapping[str, Any],
    video_duration_seconds: float,
    expected_manifest_fingerprint: str | None,
) -> tuple[list[dict[str, Any]], str]:
    """Check the complete persisted manifest and return an opaque text fingerprint."""
    if not math.isfinite(video_duration_seconds) or video_duration_seconds <= 0 or not rows:
        raise SummaryFailure("TRANSCRIPT_MANIFEST_INCOMPLETE")
    source = snapshot.get("source")
    slicing = snapshot.get("slicing")
    audio = snapshot.get("audio")
    if not isinstance(source, Mapping) or not isinstance(slicing, Mapping) or not isinstance(audio, Mapping):
        raise SummaryFailure("TRANSCRIPT_SOURCE_MISMATCH")
    if slicing.get("timeline_mapping_version") != "zero-offset-v1":
        raise SummaryFailure("TRANSCRIPT_SOURCE_MISMATCH")
    segment_seconds = slicing.get("segment_seconds")
    sample_rate = audio.get("sample_rate")
    channels = audio.get("channels")
    if (
        type(segment_seconds) is not int
        or segment_seconds <= 0
        or type(sample_rate) is not int
        or sample_rate <= 0
        or type(channels) is not int
        or channels != 1
        or audio.get("sample_format") != "s16le"
    ):
        raise SummaryFailure("TRANSCRIPT_SOURCE_MISMATCH")
    normalized: list[dict[str, Any]] = []
    manifest_segments: list[dict[str, Any]] = []
    transcript_segments: list[dict[str, Any]] = []
    previous_audio_end = 0.0
    previous_video_end = 0.0
    tolerance = max(1.1 / sample_rate, 0.00001)
    for expected_number, row in enumerate(rows):
        number = row.get("segment_number")
        if type(number) is not int or number != expected_number:
            raise SummaryFailure("TRANSCRIPT_MANIFEST_INCOMPLETE")
        if (
            row.get("status") != "succeeded"
            or not isinstance(row.get("text"), str)
            or not row["text"].strip()
        ):
            raise SummaryFailure("TRANSCRIPT_MANIFEST_INCOMPLETE")
        times = {
            key: row.get(key)
            for key in (
                "audio_start_seconds",
                "audio_end_seconds",
                "actual_audio_seconds",
                "video_start_seconds",
                "video_end_seconds",
            )
        }
        if not all(_finite_number(value) for value in times.values()):
            raise SummaryFailure("TRANSCRIPT_TIME_INVALID")
        audio_start = float(cast(int | float, times["audio_start_seconds"]))
        audio_end = float(cast(int | float, times["audio_end_seconds"]))
        actual_audio = float(cast(int | float, times["actual_audio_seconds"]))
        video_start = float(cast(int | float, times["video_start_seconds"]))
        video_end = float(cast(int | float, times["video_end_seconds"]))
        if (
            audio_start < 0
            or video_start < 0
            or audio_end <= audio_start
            or video_end <= video_start
            or video_end > video_duration_seconds
            or abs(audio_start - video_start) > tolerance
            or abs(audio_end - video_end) > tolerance
            or abs((audio_end - audio_start) - actual_audio) > tolerance
            or actual_audio > segment_seconds + tolerance
            or abs(audio_start - previous_audio_end) > tolerance
            or abs(video_start - previous_video_end) > tolerance
        ):
            raise SummaryFailure("TRANSCRIPT_TIME_INVALID")
        audio_sha256 = row.get("audio_sha256")
        request_fingerprint = row.get("request_fingerprint")
        if not isinstance(audio_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", audio_sha256):
            raise SummaryFailure("TRANSCRIPT_SOURCE_MISMATCH")
        if not isinstance(request_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", request_fingerprint):
            raise SummaryFailure("TRANSCRIPT_SOURCE_MISMATCH")
        item = {
            "segment_number": number,
            **times,
            "audio_sha256": audio_sha256,
            "request_fingerprint": request_fingerprint,
            "text": row["text"],
        }
        normalized.append(item)
        manifest_segments.append(
            {
                "segment_number": number,
                "audio_start_seconds": times["audio_start_seconds"],
                "audio_end_seconds": times["audio_end_seconds"],
                "actual_audio_seconds": times["actual_audio_seconds"],
                "video_start_seconds": times["video_start_seconds"],
                "video_end_seconds": times["video_end_seconds"],
                "audio_sha256": audio_sha256,
                "request_fingerprint": request_fingerprint,
            }
        )
        transcript_segments.append(
            {
                "segment_number": number,
                "text_sha256": hashlib.sha256(row["text"].encode("utf-8")).hexdigest(),
                "request_fingerprint": request_fingerprint,
            }
        )
        previous_audio_end = audio_end
        previous_video_end = video_end
    if abs(video_duration_seconds - previous_video_end) > 1.0:
        raise SummaryFailure("TRANSCRIPT_MANIFEST_INCOMPLETE")
    manifest_fingerprint = canonical_sha256(
        {
            "version": slicing.get("manifest_version"),
            "timeline_mapping_version": slicing.get("timeline_mapping_version"),
            "audio": dict(audio),
            "source_fingerprint": source.get("fingerprint"),
            "segments": manifest_segments,
        }
    )
    if expected_manifest_fingerprint != manifest_fingerprint:
        raise SummaryFailure("TRANSCRIPT_SOURCE_MISMATCH")
    transcript_fingerprint = canonical_sha256(
        {
            "version": SUMMARY_TRANSCRIPT_HASH_VERSION,
            "manifest_fingerprint": manifest_fingerprint,
            "segments": transcript_segments,
        }
    )
    return normalized, transcript_fingerprint


def build_summary_prompt(segments: Sequence[Mapping[str, Any]], prompt_version: str) -> str:
    instructions = _PROMPT_INSTRUCTIONS_BY_VERSION.get(prompt_version)
    if instructions is None:
        raise SummaryFailure("SUMMARY_TEMPLATE_UNSUPPORTED")
    transcript = "\n".join(
        f"[{int(segment['segment_number'])}] {float(segment['audio_start_seconds']):.3f}-"
        f"{float(segment['audio_end_seconds']):.3f}s {segment['text']}"
        for segment in segments
    )
    return (
        f"提示词版本：{prompt_version}\n"
        + instructions
        + f"\n目标结构示例：{json.dumps(SUMMARY_SCHEMA, ensure_ascii=False)}\n完整转写：\n{transcript}"
    )


def validate_context_budget(prompt: str, contract: SummaryContract) -> int:
    required = len(prompt.encode("utf-8")) + contract.max_output_tokens + CHAT_MESSAGE_OVERHEAD_TOKENS
    if required > contract.context_tokens:
        raise SummaryFailure("SUMMARY_CONTEXT_EXCEEDS_LIMIT")
    return required


def validate_transcript_context_budget(
    segments: Sequence[Mapping[str, Any]], contract: SummaryContract
) -> int:
    transcript_bytes = sum(len(str(segment["text"]).encode("utf-8")) for segment in segments)
    required = transcript_bytes + contract.max_output_tokens + CHAT_MESSAGE_OVERHEAD_TOKENS
    if required > contract.context_tokens:
        raise SummaryFailure("SUMMARY_CONTEXT_EXCEEDS_LIMIT")
    return required


def summary_request_fingerprint(
    *, contract: SummaryContract, transcript_fingerprint: str, correction_code: str | None
) -> str:
    return canonical_sha256(
        {
            "version": SUMMARY_LLM_FINGERPRINT_VERSION,
            "provider": contract.provider,
            "model": contract.model,
            "base_url": contract.base_url,
            "prompt_version": contract.prompt_version,
            "schema_version": contract.schema_version,
            "template_sha256": contract.template_sha256,
            "response_schema_sha256": contract.response_schema_sha256,
            "quote_normalization_version": QUOTE_NORMALIZATION_VERSION,
            "temperature": contract.temperature,
            "enable_thinking": contract.enable_thinking,
            "max_output_tokens": contract.max_output_tokens,
            "context_tokens": contract.context_tokens,
            "context_overhead_tokens": CHAT_MESSAGE_OVERHEAD_TOKENS,
            "timeouts": contract.timeouts,
            "max_response_bytes": contract.max_response_bytes,
            "max_diagnostic_bytes": contract.max_diagnostic_bytes,
            "max_requests": contract.max_requests,
            "transcript_fingerprint": transcript_fingerprint,
            "correction_code": correction_code,
        }
    )


def _quote_for(text: str, source: str) -> str:
    quote = best_source_excerpt(text, source)
    if not quote or len(quote) > 48 or normalize_quote(quote) not in normalize_quote(source):
        raise SummaryFailure("SUMMARY_QUOTE_VALIDATION_FAILED")
    return quote


def validate_summary_output(
    raw: str,
    segments: Sequence[Mapping[str, Any]],
    *,
    video_duration_seconds: float,
) -> dict[str, Any]:
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SummaryFailure("SUMMARY_INVALID_JSON") from exc
    if not isinstance(result, dict) or set(result) != {"chapters", "key_points"}:
        raise SummaryFailure("SUMMARY_INVALID_SCHEMA")
    by_id: dict[int, Mapping[str, Any]] = {}
    for item in segments:
        number = item.get("segment_number")
        if type(number) is not int or number in by_id:
            raise SummaryFailure("TRANSCRIPT_MANIFEST_INCOMPLETE")
        by_id[number] = item
    if not by_id or sorted(by_id) != list(range(len(by_id))):
        raise SummaryFailure("TRANSCRIPT_MANIFEST_INCOMPLETE")
    if not math.isfinite(video_duration_seconds) or video_duration_seconds <= 0:
        raise SummaryFailure("TRANSCRIPT_TIME_INVALID")

    def validate_items(items: Any, label: str) -> list[dict[str, Any]]:
        minimum, maximum = (3, 6) if label == "chapters" else (3, 8)
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            raise SummaryFailure("SUMMARY_INVALID_SCHEMA")
        text_key = "title" if label == "chapters" else "text"
        validated: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {text_key, "evidence"}:
                raise SummaryFailure("SUMMARY_INVALID_SCHEMA")
            text_value = item.get(text_key)
            evidence = item.get("evidence")
            if (
                not isinstance(text_value, str)
                or not text_value.strip()
                or not isinstance(evidence, list)
                or len(evidence) != 1
            ):
                raise SummaryFailure("SUMMARY_INVALID_SCHEMA")
            reference = evidence[0]
            if not isinstance(reference, dict) or set(reference) != {"segment_id"}:
                raise SummaryFailure("SUMMARY_INVALID_SCHEMA")
            segment_id = reference.get("segment_id")
            if type(segment_id) is not int or segment_id not in by_id:
                raise SummaryFailure("SUMMARY_UNKNOWN_SEGMENT")
            source = by_id[segment_id]
            quote = _quote_for(text_value.strip(), str(source["text"]))
            start = float(source["video_start_seconds"])
            end = float(source["video_end_seconds"])
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or not 0 <= start < end <= video_duration_seconds
            ):
                raise SummaryFailure("TRANSCRIPT_TIME_INVALID")
            validated.append(
                {
                    text_key: text_value.strip(),
                    "evidence": [
                        {
                            "segment_id": segment_id,
                            "quote": quote,
                            "start_seconds": start,
                            "end_seconds": end,
                        }
                    ],
                    "start_seconds": start,
                    "end_seconds": end,
                }
            )
        return validated

    return {
        "chapters": validate_items(result["chapters"], "chapters"),
        "key_points": validate_items(result["key_points"], "key_points"),
    }


def correction_for_failure(code: str) -> str:
    return f"{code}: {_REPAIR_INSTRUCTIONS}"


def is_repairable_summary_failure(code: str) -> bool:
    return code in {
        "LLM_OUTPUT_TRUNCATED",
        "SUMMARY_INVALID_JSON",
        "SUMMARY_INVALID_SCHEMA",
        "SUMMARY_UNKNOWN_SEGMENT",
        "SUMMARY_QUOTE_VALIDATION_FAILED",
        "TRANSCRIPT_TIME_INVALID",
    }
