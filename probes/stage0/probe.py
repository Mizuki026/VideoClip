"""Isolated stage-0 media and SiliconFlow capability probe.

This deliberately contains no application task state, persistence checkpoints,
or business-model integration. Generated media and reports belong under data/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = (REPO_ROOT / "data" / "mvp-stage-0").resolve()
AUTHORIZED_VIDEO_ROOT = (REPO_ROOT / "video").resolve()
DEFAULT_ENV_FILE = DATA_ROOT / ".env"
SUPPORTED_FORMAT_TOKENS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
MAX_SUMMARY_REQUESTS = 3
CHAT_MESSAGE_OVERHEAD_TOKENS = 64
SYSTEM_PROMPT = "输出严格 JSON。不得臆造引文或时间。"
SUMMARY_SCHEMA = {
    "chapters": [
        {
            "title": "章节标题",
            "evidence": [{"segment_id": 0}],
        }
    ],
    "key_points": [
        {
            "text": "知识要点",
            "evidence": [{"segment_id": 0}],
        }
    ],
}
SUMMARY_RESPONSE_JSON_SCHEMA = {
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
                                "properties": {
                                    "segment_id": {"type": "integer", "minimum": 0},
                                },
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
                                "properties": {
                                    "segment_id": {"type": "integer", "minimum": 0},
                                },
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


class ProbeError(RuntimeError):
    """A bounded, report-safe probe failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    asr_model: str
    llm_model: str
    asr_price_evidence: str
    llm_price_evidence: str
    asr_usd_per_minute: float
    llm_input_usd_per_million: float
    llm_output_usd_per_million: float
    context_tokens: int
    max_output_tokens: int
    temperature: float
    llm_enable_thinking: bool
    prompt_version: str
    http_total_timeout: float
    http_connect_timeout: float
    http_read_timeout: float
    media_total_timeout: float
    media_stop_grace: float
    max_response_bytes: int
    max_diagnostic_bytes: int
    max_video_seconds: int
    segment_seconds: int
    audio_sample_rate: int
    max_summary_requests: int


@dataclass(frozen=True)
class Segment:
    segment_id: int
    audio_start: float
    audio_end: float
    video_start: float
    video_end: float
    text: str


def load_env(path: Path) -> dict[str, str]:
    """Read simple KEY=value config without printing values or allowing expansion."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ProbeError("INVALID_CONFIG", f"line {line_number} is not KEY=value")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ProbeError("INVALID_CONFIG", f"line {line_number} has an invalid key")
        values[key] = value.strip().strip('"').strip("'")
    return values


def _positive_float(values: dict[str, str], key: str, default: float) -> float:
    try:
        result = float(values.get(key, str(default)))
    except ValueError as exc:
        raise ProbeError("INVALID_CONFIG", f"{key} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise ProbeError("INVALID_CONFIG", f"{key} must be positive")
    return result


def _positive_int(values: dict[str, str], key: str, default: int) -> int:
    try:
        result = int(values.get(key, str(default)))
    except ValueError as exc:
        raise ProbeError("INVALID_CONFIG", f"{key} must be an integer") from exc
    if result <= 0:
        raise ProbeError("INVALID_CONFIG", f"{key} must be positive")
    return result


def _boolean(values: dict[str, str], key: str, default: bool) -> bool:
    raw = values.get(key, str(default)).strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ProbeError("INVALID_CONFIG", f"{key} must be true or false")


def _nonnegative_float(values: dict[str, str], key: str) -> float:
    try:
        result = float(values[key])
    except (KeyError, ValueError) as exc:
        raise ProbeError("INVALID_CONFIG", f"{key} must be a numeric price from the account model catalog") from exc
    if not math.isfinite(result) or result < 0:
        raise ProbeError("INVALID_CONFIG", f"{key} cannot be negative")
    return result


def settings_from(values: dict[str, str]) -> Settings:
    required = (
        "SILICONFLOW_API_KEY",
        "SILICONFLOW_ASR_MODEL",
        "SILICONFLOW_LLM_MODEL",
        "SILICONFLOW_ASR_PRICE_EVIDENCE",
        "SILICONFLOW_LLM_PRICE_EVIDENCE",
        "SILICONFLOW_ASR_USD_PER_MINUTE",
        "SILICONFLOW_LLM_INPUT_USD_PER_MILLION",
        "SILICONFLOW_LLM_OUTPUT_USD_PER_MILLION",
    )
    missing = [key for key in required if not values.get(key, "").strip()]
    if missing:
        raise ProbeError("MISSING_CONFIG", "required configuration is missing: " + ", ".join(missing))
    if values.get("SILICONFLOW_FREE_USE_CONFIRMED", "") != "I_CONFIRMED_BOTH_MODELS_ARE_FREE_IN_MY_ACCOUNT":
        raise ProbeError("FREE_USE_NOT_CONFIRMED", "confirm both selected models are free in this account before making requests")
    context_tokens = _positive_int(values, "LLM_CONTEXT_TOKENS", 0)
    asr_model = values["SILICONFLOW_ASR_MODEL"].strip()
    llm_model = values["SILICONFLOW_LLM_MODEL"].strip()
    asr_rate = _nonnegative_float(values, "SILICONFLOW_ASR_USD_PER_MINUTE")
    llm_input_rate = _nonnegative_float(values, "SILICONFLOW_LLM_INPUT_USD_PER_MILLION")
    llm_output_rate = _nonnegative_float(values, "SILICONFLOW_LLM_OUTPUT_USD_PER_MILLION")
    if any((asr_rate, llm_input_rate, llm_output_rate)):
        raise ProbeError("PAID_MODEL_REFUSED", "non-zero model prices are outside the free-only stage-0 probe")
    if asr_model.lower().startswith("pro/") or llm_model.lower().startswith("pro/"):
        raise ProbeError("PAID_MODEL_REFUSED", "Pro model IDs are disabled in this free-model probe")
    base_url = values.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
    if not base_url.startswith("https://"):
        raise ProbeError("INVALID_CONFIG", "SILICONFLOW_BASE_URL must use HTTPS")
    temperature = _positive_float(values, "LLM_TEMPERATURE", 0.2)
    if temperature > 2:
        raise ProbeError("INVALID_CONFIG", "LLM_TEMPERATURE must be <= 2")
    max_summary_requests = _positive_int(values, "MAX_SUMMARY_REQUESTS", MAX_SUMMARY_REQUESTS)
    if max_summary_requests > MAX_SUMMARY_REQUESTS:
        raise ProbeError("INVALID_CONFIG", "MAX_SUMMARY_REQUESTS cannot exceed 3")
    return Settings(
        api_key=values["SILICONFLOW_API_KEY"].strip(),
        base_url=base_url,
        asr_model=asr_model,
        llm_model=llm_model,
        asr_price_evidence=values.get("SILICONFLOW_ASR_PRICE_EVIDENCE", "").strip(),
        llm_price_evidence=values.get("SILICONFLOW_LLM_PRICE_EVIDENCE", "").strip(),
        asr_usd_per_minute=asr_rate,
        llm_input_usd_per_million=llm_input_rate,
        llm_output_usd_per_million=llm_output_rate,
        context_tokens=context_tokens,
        max_output_tokens=_positive_int(values, "LLM_MAX_OUTPUT_TOKENS", 3072),
        temperature=temperature,
        llm_enable_thinking=_boolean(values, "LLM_ENABLE_THINKING", False),
        prompt_version=values.get("PROMPT_VERSION", "stage0-summary-v4").strip(),
        http_total_timeout=_positive_float(values, "HTTP_TOTAL_TIMEOUT_SECONDS", 240),
        http_connect_timeout=_positive_float(values, "HTTP_CONNECT_TIMEOUT_SECONDS", 15),
        http_read_timeout=_positive_float(values, "HTTP_READ_TIMEOUT_SECONDS", 45),
        media_total_timeout=_positive_float(values, "MEDIA_TOTAL_TIMEOUT_SECONDS", 180),
        media_stop_grace=_positive_float(values, "MEDIA_STOP_GRACE_SECONDS", 5),
        max_response_bytes=_positive_int(values, "MAX_RESPONSE_BYTES", 1_048_576),
        max_diagnostic_bytes=_positive_int(values, "MAX_DIAGNOSTIC_BYTES", 8192),
        max_video_seconds=_positive_int(values, "MAX_VIDEO_SECONDS", 1200),
        segment_seconds=_positive_int(values, "SEGMENT_SECONDS", 30),
        audio_sample_rate=_positive_int(values, "AUDIO_SAMPLE_RATE", 16_000),
        max_summary_requests=max_summary_requests,
    )


def normalize_quote(value: str) -> str:
    """NFKC + casefold + remove whitespace; punctuation remains significant."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(normalized.split())


def citation_options(text: str) -> list[str]:
    """Build exact, source-derived quote choices for reliable model selection."""
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
    """Select a short exact transcript span that best overlaps a summary item."""
    target = re.sub(r"[^\w\u4e00-\u9fff]", "", normalize_quote(summary_text))
    options = citation_options(transcript_text)
    best_quote = ""
    best_score = -1
    for quote in options:
        candidate = re.sub(r"[^\w\u4e00-\u9fff]", "", normalize_quote(quote))
        previous = [0] * (len(target) + 1)
        score = 0
        for candidate_character in candidate:
            current = [0] * (len(target) + 1)
            for index, target_character in enumerate(target, 1):
                if candidate_character == target_character:
                    current[index] = previous[index - 1] + 1
                    score = max(score, current[index])
            previous = current
        if score > best_score:
            best_quote = quote
            best_score = score
    return best_quote


def build_manifest(duration: float, segment_seconds: int) -> list[tuple[int, float, float]]:
    if not math.isfinite(duration) or duration <= 0 or segment_seconds <= 0:
        raise ProbeError("INVALID_DURATION", "duration and segment length must be positive")
    count = math.ceil(duration / segment_seconds)
    return [
        (index, index * segment_seconds, min((index + 1) * segment_seconds, duration))
        for index in range(count)
    ]


def ensure_authorized_sample_path(sample: Path) -> Path:
    """Accept only user-authorized samples or private probe inputs."""
    resolved = sample.resolve()
    allowed_roots = (AUTHORIZED_VIDEO_ROOT, (DATA_ROOT / "input").resolve())
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ProbeError(
        "SAMPLE_OUTSIDE_AUTHORIZED_DIRECTORY",
        "sample must be inside the repository video directory or private probe input directory",
    )


def validate_sample_duration(duration: float, max_seconds: int) -> None:
    if not math.isfinite(duration) or duration <= 0:
        raise ProbeError("INVALID_DURATION", "sample duration must be greater than zero")
    if duration > max_seconds:
        raise ProbeError("VIDEO_DURATION_LIMIT", "sample exceeds the configured duration limit")


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ProbeError("INVALID_MEDIA", f"invalid {field}") from exc
    if not math.isfinite(result):
        raise ProbeError("INVALID_MEDIA", f"invalid {field}")
    return result


def map_supported_mp4(probe: dict[str, Any]) -> tuple[float, float]:
    """Conservative browser mapping: MP4/H.264/AAC with a zero-based shared A/V clock."""
    fmt = probe.get("format") or {}
    format_names = set(str(fmt.get("format_name", "")).split(","))
    if not (format_names & SUPPORTED_FORMAT_TOKENS):
        raise ProbeError("UNSUPPORTED_CONTAINER", "container is outside the MP4 family allowlist")
    streams = probe.get("streams") or []
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(video_streams) != 1 or len(audio_streams) != 1:
        raise ProbeError("UNSUPPORTED_STREAM_LAYOUT", "exactly one video and one audio stream are required")
    video = video_streams[0]
    audio = audio_streams[0]
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        raise ProbeError("UNSUPPORTED_VIDEO_CODEC", "only H.264 yuv420p video is allowlisted")
    if audio.get("codec_name") != "aac":
        raise ProbeError("UNSUPPORTED_AUDIO_CODEC", "only AAC audio is allowlisted")
    if "start_time" not in fmt:
        raise ProbeError("INSUFFICIENT_TIMESTAMP_EVIDENCE", "ffprobe did not expose the container start timestamp")
    format_start = _number(fmt["start_time"], "format.start_time")
    if any(key not in video for key in ("start_time", "time_base")) or any(
        key not in audio for key in ("start_time", "time_base")
    ):
        raise ProbeError("INSUFFICIENT_TIMESTAMP_EVIDENCE", "ffprobe did not expose stream starts and time bases")
    video_start = _number(video["start_time"], "video.start_time")
    audio_start = _number(audio["start_time"], "audio.start_time")
    if abs(format_start) > 0.001 or abs(video_start) > 0.001 or abs(audio_start) > 0.001:
        raise ProbeError(
            "UNSUPPORTED_TIMELINE_OFFSET",
            "non-zero or delayed track timestamps require playback-tested mapping",
        )
    video_duration = _number(video.get("duration", fmt.get("duration")), "video.duration")
    audio_duration = _number(audio.get("duration", fmt.get("duration")), "audio.duration")
    if video_duration <= 0 or audio_duration <= 0:
        raise ProbeError("INVALID_MEDIA", "audio and video durations must be positive")
    return 0.0, min(video_duration, audio_duration)


def validate_summary(raw: str, segments: list[Segment]) -> dict[str, Any]:
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProbeError("INVALID_SUMMARY_JSON", "summary was not valid JSON") from exc
    if not isinstance(result, dict) or set(result) != {"chapters", "key_points"}:
        raise ProbeError("INVALID_SUMMARY_SCHEMA", "summary must contain chapters and key_points only")
    by_id = {segment.segment_id: segment for segment in segments}
    if len(by_id) != len(segments) or not segments:
        raise ProbeError("INCOMPLETE_SEGMENT_MANIFEST", "the transcript segment manifest is empty or duplicated")

    def validate_items(items: Any, label: str) -> list[dict[str, Any]]:
        minimum, maximum = (3, 6) if label == "chapters" else (3, 8)
        if not isinstance(items, list) or not minimum <= len(items) <= maximum:
            raise ProbeError(
                "INVALID_SUMMARY_SCHEMA",
                f"{label} must contain between {minimum} and {maximum} items",
            )
        validated: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ProbeError("INVALID_SUMMARY_SCHEMA", f"{label} items must be objects")
            expected = {"title", "evidence"} if label == "chapters" else {"text", "evidence"}
            if set(item) != expected:
                raise ProbeError("INVALID_SUMMARY_SCHEMA", f"invalid {label} item fields")
            text_key = "title" if label == "chapters" else "text"
            text = item[text_key]
            evidence = item["evidence"]
            if not isinstance(text, str) or not text.strip() or not isinstance(evidence, list) or len(evidence) != 1:
                raise ProbeError("INVALID_SUMMARY_SCHEMA", f"{label} text and evidence are required")
            valid_evidence: list[dict[str, Any]] = []
            referenced: list[Segment] = []
            for ref in evidence:
                if not isinstance(ref, dict) or set(ref) != {"segment_id"}:
                    raise ProbeError("INVALID_CITATION_SCHEMA", "evidence needs only a segment_id")
                segment_id = ref["segment_id"]
                if type(segment_id) is not int or segment_id not in by_id:
                    raise ProbeError("UNKNOWN_CITATION_SEGMENT", "citation does not refer to this complete run")
                source = by_id[segment_id]
                quote = best_source_excerpt(text, source.text)
                if not normalize_quote(quote) or normalize_quote(quote) not in normalize_quote(by_id[segment_id].text):
                    raise ProbeError("QUOTE_MISMATCH", "citation is not a continuous normalized transcript substring")
                referenced.append(source)
                valid_evidence.append(
                    {
                        "segment_id": segment_id,
                        "quote": quote,
                        "audio_start": source.audio_start,
                        "audio_end": source.audio_end,
                        "video_start": source.video_start,
                        "video_end": source.video_end,
                    }
                )
            validated.append(
                {
                    text_key: text.strip(),
                    "evidence": valid_evidence,
                    "audio_start": min(ref.audio_start for ref in referenced),
                    "audio_end": max(ref.audio_end for ref in referenced),
                    "video_start": min(ref.video_start for ref in referenced),
                    "video_end": max(ref.video_end for ref in referenced),
                }
            )
        return validated

    return {
        "chapters": validate_items(result["chapters"], "chapters"),
        "key_points": validate_items(result["key_points"], "key_points"),
    }


def upper_bound_context_bytes(
    transcript_text: str, prompt_text: str, max_output_tokens: int, context_tokens: int
) -> dict[str, int | bool]:
    """Use UTF-8 byte count as a conservative token upper bound for text payloads."""
    input_byte_upper_bound = len((prompt_text + transcript_text).encode("utf-8"))
    required = input_byte_upper_bound + max_output_tokens + CHAT_MESSAGE_OVERHEAD_TOKENS
    return {
        "input_utf8_bytes_upper_bound": input_byte_upper_bound,
        "output_token_reservation": max_output_tokens,
        "chat_message_overhead_reservation": CHAT_MESSAGE_OVERHEAD_TOKENS,
        "required_context_upper_bound": required,
        "advertised_context_tokens": context_tokens,
        "fits_conservative_upper_bound": required <= context_tokens,
    }


def estimate_20m_context(segments: list[Segment], prompt_version: str, context_tokens: int, max_output_tokens: int) -> dict[str, Any]:
    """Project a complete 20-minute transcript at 1.5x observed speech density.

    UTF-8 bytes are a conservative upper bound for text token counts. The
    projection remains an estimate because speech density varies by course.
    """
    if not segments:
        raise ProbeError("INCOMPLETE_SEGMENT_MANIFEST", "cannot project an empty transcript")
    observed_duration = max(segment.audio_end for segment in segments)
    if observed_duration <= 0:
        raise ProbeError("INVALID_DURATION", "observed transcript duration must be positive")
    transcript = "\n".join(segment.text for segment in segments)
    observed_bytes = len(transcript.encode("utf-8"))
    projected_transcript_bytes = math.ceil(observed_bytes * (1200 / observed_duration) * 1.5)
    prompt_overhead_bytes = (
        len(SYSTEM_PROMPT.encode("utf-8"))
        + len(summary_prompt([], prompt_version).encode("utf-8"))
        + 40 * 36
    )
    required = (
        projected_transcript_bytes
        + prompt_overhead_bytes
        + max_output_tokens
        + CHAT_MESSAGE_OVERHEAD_TOKENS
    )
    return {
        "target_duration_seconds": 1200,
        "observed_duration_seconds": round(observed_duration, 3),
        "observed_transcript_utf8_bytes": observed_bytes,
        "projection_factor": 1.5,
        "projected_transcript_utf8_bytes": projected_transcript_bytes,
        "prompt_and_40_segment_headers_utf8_bytes": prompt_overhead_bytes,
        "output_token_reservation": max_output_tokens,
        "chat_message_overhead_reservation": CHAT_MESSAGE_OVERHEAD_TOKENS,
        "required_context_upper_bound": required,
        "advertised_context_tokens": context_tokens,
        "fits_conservative_estimate": required <= context_tokens,
        "method": "observed UTF-8 transcript bytes scaled to 1200 seconds at 1.5x speech density; UTF-8 byte upper bound plus system/fixed prompt, 40 segment headers, reserved output tokens, and 64 chat-framing tokens",
        "limitation": "estimate from the sample transcript; the observed provider prompt_tokens and an actual 20-minute course remain required before production sizing is locked",
    }


def _run_limited(command: list[str], timeout: float, grace: float, max_output: int) -> bytes:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        start_new_session=os.name != "nt",
    )
    if process.stdout is None:
        process.kill()
        process.wait(timeout=grace)
        raise ProbeError("MEDIA_PROCESS_FAILED", "could not capture bounded media tool output")
    stdout = process.stdout
    captured = bytearray()
    exceeded = threading.Event()

    def drain_stdout() -> None:
        while True:
            chunk = stdout.read(64 * 1024)
            if not chunk:
                return
            if len(captured) + len(chunk) > max_output:
                exceeded.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            captured.extend(chunk)

    reader = threading.Thread(target=drain_stdout, daemon=True)
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired as stop_exc:
                raise ProbeError("MEDIA_STOP_UNCONFIRMED", "media process did not exit after kill") from stop_exc
        reader.join(timeout=grace)
        stdout.close()
        reader.join(timeout=grace)
        if reader.is_alive():
            raise ProbeError("MEDIA_STOP_UNCONFIRMED", "media process output reader did not stop") from exc
        raise ProbeError("MEDIA_TIMEOUT", "media process exceeded its total deadline and was stopped") from exc
    reader.join(timeout=grace)
    stdout.close()
    if reader.is_alive() or process.poll() is None:
        process.kill()
        process.wait(timeout=grace)
        raise ProbeError("MEDIA_STOP_UNCONFIRMED", "media process output reader did not stop")
    if exceeded.is_set():
        raise ProbeError("MEDIA_DIAGNOSTIC_LIMIT", "media tool output exceeded the configured cap")
    if process.returncode:
        # Do not store or print unbounded/possibly sensitive command diagnostics.
        raise ProbeError("MEDIA_COMMAND_FAILED", f"media command exited with status {process.returncode}")
    return bytes(captured)


def ffprobe_media(path: Path, settings: Settings) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ProbeError("FFPROBE_MISSING", "ffprobe is not installed or not on PATH")
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration,start_time:stream=index,codec_type,codec_name,pix_fmt,start_time,duration,time_base,avg_frame_rate,sample_rate,channels",
        "-of",
        "json",
        str(path),
    ]
    try:
        return cast(
            dict[str, Any],
            json.loads(_run_limited(command, settings.media_total_timeout, settings.media_stop_grace, 64_000)),
        )
    except json.JSONDecodeError as exc:
        raise ProbeError("INVALID_FFPROBE_OUTPUT", "ffprobe did not return valid JSON") from exc


def extract_segments(video: Path, work_dir: Path, duration: float, settings: Settings) -> list[dict[str, Any]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ProbeError("FFMPEG_MISSING", "ffmpeg is not installed or not on PATH")
    manifest = build_manifest(duration, settings.segment_seconds)
    audio_path = work_dir / "audio.wav"
    extract = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(video),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(settings.audio_sample_rate),
        "-c:a",
        "pcm_s16le",
        "-y",
        str(audio_path),
    ]
    _run_limited(extract, settings.media_total_timeout, settings.media_stop_grace, settings.max_diagnostic_bytes)
    audio_duration = audio_path.stat().st_size / (settings.audio_sample_rate * 2)
    if audio_duration < duration - 1.0 or audio_duration > duration + 1.0:
        raise ProbeError("AUDIO_DURATION_MISMATCH", "extracted audio duration differs from the mapped media duration")
    results: list[dict[str, Any]] = []
    for segment_id, start, end in manifest:
        path = work_dir / f"segment-{segment_id:04d}.wav"
        length = end - start
        command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-ss",
            f"{start:.6f}",
            "-i",
            str(audio_path),
            "-t",
            f"{length:.6f}",
            "-ac",
            "1",
            "-ar",
            str(settings.audio_sample_rate),
            "-c:a",
            "pcm_s16le",
            "-y",
            str(path),
        ]
        _run_limited(command, settings.media_total_timeout, settings.media_stop_grace, settings.max_diagnostic_bytes)
        results.append(
            {
                "segment_id": segment_id,
                "audio_start": start,
                "audio_end": end,
                "audio_path": path,
            }
        )
    return results


async def _safe_response_bytes(response: httpx.Response, maximum: int) -> bytes:
    length_header = response.headers.get("content-length")
    if length_header and length_header.isdecimal() and int(length_header) > maximum:
        raise ProbeError("RESPONSE_TOO_LARGE", "model response exceeded the configured byte cap")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > maximum:
            raise ProbeError("RESPONSE_TOO_LARGE", "model response exceeded the configured byte cap")
    return bytes(body)


def _classify_status(status: int) -> str:
    return {
        401: "AUTHENTICATION_FAILED",
        403: "ACCESS_FORBIDDEN",
        404: "MODEL_OR_ENDPOINT_NOT_FOUND",
        408: "UPSTREAM_TIMEOUT",
        429: "RATE_LIMITED",
        503: "MODEL_OVERLOADED",
        504: "UPSTREAM_TIMEOUT",
    }.get(status, f"HTTP_{status}")


async def asr_request(
    client: httpx.AsyncClient, settings: Settings, audio_path: Path
) -> tuple[str, dict[str, Any]]:
    start = time.monotonic()
    try:
        async with asyncio.timeout(settings.http_total_timeout):
            with audio_path.open("rb") as audio_file:
                async with client.stream(
                    "POST",
                    f"{settings.base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.api_key}"},
                    data={"model": settings.asr_model},
                    files={"file": (audio_path.name, audio_file, "audio/wav")},
                ) as response:
                    if response.status_code >= 400:
                        raise ProbeError(_classify_status(response.status_code), f"ASR returned HTTP {response.status_code}")
                    body = await _safe_response_bytes(response, settings.max_response_bytes)
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise ProbeError("INVALID_ASR_RESPONSE", "ASR returned invalid JSON") from exc
                    text = payload.get("text") if isinstance(payload, dict) else None
                    if not isinstance(text, str) or not text.strip():
                        raise ProbeError("INVALID_ASR_RESPONSE", "ASR response lacked non-empty text")
                    metadata = {
                        "duration_seconds": round(time.monotonic() - start, 3),
                        "model": settings.asr_model,
                        "http_status": response.status_code,
                        "trace_id": response.headers.get("x-siliconcloud-trace-id", ""),
                        "response_bytes": len(body),
                        "text_characters": len(text),
                    }
                    return text, metadata
    except TimeoutError as exc:
        raise ProbeError("HTTP_TOTAL_TIMEOUT", "ASR request exceeded its total deadline") from exc
    except httpx.TimeoutException as exc:
        raise ProbeError("HTTP_TIMEOUT", "ASR request exceeded a phase timeout") from exc
    except httpx.RequestError as exc:
        raise ProbeError("HTTP_TRANSPORT_ERROR", f"ASR transport error: {type(exc).__name__}") from exc


def summary_prompt(segments: list[Segment], prompt_version: str) -> str:
    transcript = "\n".join(
        f"[{segment.segment_id}] {segment.audio_start:.3f}-{segment.audio_end:.3f}s {segment.text}"
        for segment in segments
    )
    return (
        f"提示词版本：{prompt_version}\n"
        "你是中文技术课程助教。只依据完整转写整理章节和知识要点，不补充视频中未讲内容。"
        "返回 3–6 个简洁章节和 3–8 条简洁知识要点。每项只给 1 条最直接的引用。"
        "严格返回键为 chapters 与 key_points 的 JSON 对象；章节字段只能是 title 与 evidence，知识要点字段只能是 text 与 evidence。"
        "章节数量为 3–6，知识要点数量为 3–8。每项 evidence 必须是单元素数组，元素只能有整数 segment_id。"
        "每项只输出一个来自完整转写的 segment_id，选择最能支持该项的单个片段。不要输出 quote 或时间；程序会从所选片段中提取最相关的连续原文作为 quote，并按片段推导时间。\n"
        f"目标结构示例：{json.dumps(SUMMARY_SCHEMA, ensure_ascii=False)}\n完整转写：\n{transcript}"
    )


async def llm_request(
    client: httpx.AsyncClient,
    settings: Settings,
    prompt: str,
    correction: str = "",
) -> tuple[str, dict[str, Any]]:
    start = time.monotonic()
    content = prompt + (f"\n校验错误，请只修正这些问题：\n{correction}" if correction else "")
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": settings.temperature,
        "max_tokens": settings.max_output_tokens,
        "enable_thinking": settings.llm_enable_thinking,
        "response_format": {
            "type": "json_schema",
            "json_schema": SUMMARY_RESPONSE_JSON_SCHEMA,
        },
        "stream": False,
    }
    try:
        async with asyncio.timeout(settings.http_total_timeout):
            async with client.stream(
                "POST",
                f"{settings.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.api_key}"},
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    raise ProbeError(_classify_status(response.status_code), f"LLM returned HTTP {response.status_code}")
                body = await _safe_response_bytes(response, settings.max_response_bytes)
                try:
                    document = json.loads(body)
                    choice = document["choices"][0]
                    raw = choice["message"]["content"]
                except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                    raise ProbeError("INVALID_LLM_RESPONSE", "LLM returned an invalid completion structure") from exc
                if not isinstance(raw, str):
                    raise ProbeError("INVALID_LLM_RESPONSE", "LLM message content was not text")
                if document.get("model") not in (None, settings.llm_model):
                    raise ProbeError("MODEL_MISMATCH", "provider response model differs from the configured model")
                usage = document.get("usage") or {}
                metadata = {
                    "duration_seconds": round(time.monotonic() - start, 3),
                    "model": document.get("model", settings.llm_model),
                    "http_status": response.status_code,
                    "trace_id": response.headers.get("x-siliconcloud-trace-id", ""),
                    "response_bytes": len(body),
                    "finish_reason": choice.get("finish_reason"),
                    "usage": {
                        key: usage.get(key)
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                        if key in usage
                    },
                }
                return raw, metadata
    except TimeoutError as exc:
        raise ProbeError("HTTP_TOTAL_TIMEOUT", "LLM request exceeded its total deadline") from exc
    except httpx.TimeoutException as exc:
        raise ProbeError("HTTP_TIMEOUT", "LLM request exceeded a phase timeout") from exc
    except httpx.RequestError as exc:
        raise ProbeError("HTTP_TRANSPORT_ERROR", f"LLM transport error: {type(exc).__name__}") from exc


async def summarize(
    client: httpx.AsyncClient,
    settings: Settings,
    segments: list[Segment],
    llm_results_sink: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    prompt = summary_prompt(segments, settings.prompt_version)
    correction = ""
    history = llm_results_sink if llm_results_sink is not None else []
    last_error: ProbeError | None = None
    for attempt in range(1, settings.max_summary_requests + 1):
        request_started = time.monotonic()
        try:
            raw, metadata = await llm_request(client, settings, prompt, correction)
        except ProbeError as exc:
            history.append(
                {
                    "attempt": attempt,
                    "status": "request_failed",
                    "duration_seconds": round(time.monotonic() - request_started, 3),
                    "error_code": exc.code,
                }
            )
            raise
        metadata["attempt"] = attempt
        metadata["status"] = "response_received"
        history.append(metadata)
        if metadata.get("finish_reason") == "length":
            metadata["validation_status"] = "failed"
            metadata["validation_error_code"] = "TRUNCATED_LLM_RESPONSE"
            metadata["validation_error"] = "LLM response ended at the configured token limit"
            last_error = ProbeError("TRUNCATED_LLM_RESPONSE", metadata["validation_error"])
            correction = f"{last_error.code}: {last_error}"
            continue
        try:
            validated = validate_summary(raw, segments)
        except ProbeError as exc:
            metadata["validation_status"] = "failed"
            metadata["validation_error_code"] = exc.code
            metadata["validation_error"] = str(exc)
            last_error = exc
            correction = f"{exc.code}: {exc}"
        else:
            metadata["validation_status"] = "passed"
            return validated, history
    assert last_error is not None
    raise ProbeError("SUMMARY_REPAIR_EXHAUSTED", f"summary invalid after {len(history)} requests ({last_error.code})")


def estimated_cost(settings: Settings, audio_duration: float, llm_calls: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_tokens = 0
    completion_tokens = 0
    usage_complete = True
    for call in llm_calls:
        usage = call.get("usage", {})
        if "prompt_tokens" not in usage or "completion_tokens" not in usage:
            usage_complete = False
            continue
        prompt_tokens += int(usage["prompt_tokens"])
        completion_tokens += int(usage["completion_tokens"])
    asr_cost = audio_duration / 60 * settings.asr_usd_per_minute
    llm_cost = None
    if usage_complete:
        llm_cost = (
            prompt_tokens * settings.llm_input_usd_per_million
            + completion_tokens * settings.llm_output_usd_per_million
        ) / 1_000_000
    return {
        "basis": "configured account model-catalog prices multiplied by input usage; compare with the account billing record",
        "asr_audio_minutes": round(audio_duration / 60, 4),
        "asr_estimated_usd": round(asr_cost, 8),
        "llm_prompt_tokens": prompt_tokens if usage_complete else None,
        "llm_completion_tokens": completion_tokens if usage_complete else None,
        "llm_estimated_usd": round(llm_cost, 8) if llm_cost is not None else None,
        "total_estimated_usd": round(asr_cost + llm_cost, 8) if llm_cost is not None else None,
        "usage_complete": usage_complete,
        "not_a_billing_receipt": True,
    }


async def run_probe(video: Path, settings: Settings) -> Path:
    video = ensure_authorized_sample_path(video)
    if not video.is_file() or video.stat().st_size <= 0:
        raise ProbeError("SAMPLE_NOT_FOUND", "sample video does not exist or is empty")
    if video.suffix.lower() != ".mp4":
        raise ProbeError("UNSUPPORTED_CONTAINER", "the stage-0 playback allowlist accepts .mp4 files only")
    run_id = uuid.uuid4().hex[:12]
    work_dir = DATA_ROOT / "work" / run_id
    report_dir = DATA_ROOT / "reports"
    work_dir.mkdir(parents=True, exist_ok=False)
    report_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    mapped_duration = 0.0
    report: dict[str, Any] = {
        "run_id": run_id,
        "sample_name": video.name,
        "sample_bytes": video.stat().st_size,
        "prompt_version": settings.prompt_version,
        "configured_models": {
            "asr": settings.asr_model,
            "llm": settings.llm_model,
            "llm_enable_thinking": settings.llm_enable_thinking,
        },
        "price_evidence": {
            "asr": settings.asr_price_evidence,
            "asr_usd_per_minute": settings.asr_usd_per_minute,
            "llm": settings.llm_price_evidence,
            "llm_input_usd_per_million": settings.llm_input_usd_per_million,
            "llm_output_usd_per_million": settings.llm_output_usd_per_million,
            "basis": "local account-console assertion; reconcile with the actual account bill after the run",
        },
        "pipeline": [],
    }
    try:
        raw_media = ffprobe_media(video, settings)
        report["pipeline"].append("ffprobe")
        duration = _number(raw_media.get("format", {}).get("duration"), "format.duration")
        validate_sample_duration(duration, settings.max_video_seconds)
        offset, mapped_duration = map_supported_mp4(raw_media)
        extracted = extract_segments(video, work_dir, mapped_duration, settings)
        expected = build_manifest(mapped_duration, settings.segment_seconds)
        if len(extracted) != len(expected) or [item["segment_id"] for item in extracted] != [item[0] for item in expected]:
            raise ProbeError("INCOMPLETE_SEGMENT_MANIFEST", "FFmpeg output did not match the complete manifest")
        report["pipeline"].append("ffmpeg extract + complete 30-second manifest")
        report["media"] = {
            "format_name": raw_media["format"].get("format_name"),
            "duration_seconds": round(duration, 3),
            "timeline_policy": "zero-based MP4 H.264/yuv420p + AAC; non-zero/delayed starts rejected",
            "audio_to_player_offset_seconds": offset,
            "sample_rate_hz": settings.audio_sample_rate,
            "channels": 1,
            "sample_format": "s16le",
            "segments": [
                {
                    "segment_id": item["segment_id"],
                    "audio_start": item["audio_start"],
                    "audio_end": item["audio_end"],
                    "video_start": item["audio_start"] + offset,
                    "video_end": item["audio_end"] + offset,
                    "audio_bytes": item["audio_path"].stat().st_size,
                }
                for item in extracted
            ],
        }
        timeout = httpx.Timeout(
            connect=settings.http_connect_timeout,
            read=settings.http_read_timeout,
            write=settings.http_read_timeout,
            pool=settings.http_connect_timeout,
        )
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            transcripts: list[Segment] = []
            asr_results: list[dict[str, Any]] = []
            for item in extracted:
                text, metadata = await asr_request(client, settings, item["audio_path"])
                asr_results.append(metadata)
                transcripts.append(
                    Segment(
                        segment_id=item["segment_id"],
                        audio_start=item["audio_start"],
                        audio_end=item["audio_end"],
                        video_start=item["audio_start"] + offset,
                        video_end=item["audio_end"] + offset,
                        text=text.strip(),
                    )
                )
            report["pipeline"].append("sequential real ASR for every segment")
            report["asr"] = asr_results
            report["transcript"] = [segment.__dict__ for segment in transcripts]
            prompt = summary_prompt(transcripts, settings.prompt_version)
            context_budget = upper_bound_context_bytes(
                "",
                SYSTEM_PROMPT + "\n" + prompt,
                settings.max_output_tokens,
                settings.context_tokens,
            )
            report["context_budget_sample"] = context_budget
            report["context_budget_20m"] = estimate_20m_context(
                transcripts,
                settings.prompt_version,
                settings.context_tokens,
                settings.max_output_tokens,
            )
            if not context_budget["fits_conservative_upper_bound"]:
                raise ProbeError("CONTEXT_BUDGET_EXCEEDED", "sample exceeds the conservative configured context budget")
            llm_started = time.monotonic()
            llm_results: list[dict[str, Any]] = []
            report["llm"] = llm_results
            try:
                summary, llm_results = await summarize(
                    client, settings, transcripts, llm_results_sink=llm_results
                )
            except ProbeError as exc:
                report["llm_attempt"] = {
                    "status": "failed",
                    "duration_seconds": round(time.monotonic() - llm_started, 3),
                    "error_code": exc.code,
                    "request_count": len(llm_results),
                }
                if mapped_duration > 0:
                    report["cost_estimate"] = estimated_cost(settings, mapped_duration, llm_results)
                raise
            report["llm_attempt"] = {
                "status": "passed",
                "duration_seconds": round(time.monotonic() - llm_started, 3),
                "request_count": len(llm_results),
            }
            report["pipeline"].append("real LLM summary + citation/time validation")
            report["summary"] = summary
            report["cost_estimate"] = estimated_cost(settings, mapped_duration, llm_results)
            report["technical_term_review"] = {
                "status": "requires human review against the authorized course sample",
                "transcript_is_saved_privately": True,
            }
        report["status"] = "passed"
        report["total_duration_seconds"] = round(time.monotonic() - started, 3)
    except ProbeError as exc:
        report["status"] = "failed"
        report["failure"] = {"code": exc.code, "message": str(exc)}
        report["total_duration_seconds"] = round(time.monotonic() - started, 3)
    report_path = report_dir / f"{run_id}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sample",
        type=Path,
        help="authorized MP4 under video/ or private data/mvp-stage-0/input; duration must be 0-20 minutes",
    )
    args = parser.parse_args(argv)
    try:
        # Credentials and run parameters come only from the ACL-protected local config.
        values = load_env(DEFAULT_ENV_FILE)
        settings = settings_from(values)
        report_path = asyncio.run(run_probe(args.sample, settings))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        # Never print keys, transcript bodies, quotes, response bodies, or absolute private paths.
        print(json.dumps({"status": report["status"], "report": report_path.name, "failure": report.get("failure")}, ensure_ascii=False))
        return 0 if report["status"] == "passed" else 2
    except ProbeError as exc:
        print(json.dumps({"status": "blocked", "code": exc.code, "message": str(exc)}, ensure_ascii=False))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}, ensure_ascii=False))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
