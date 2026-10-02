from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_FORMAT_TOKENS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
MAX_FFPROBE_OUTPUT_BYTES = 64_000


class MediaError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MediaInfo:
    duration_seconds: float
    container: str
    video_codec: str
    pixel_format: str
    audio_codec: str
    timeline_start_seconds: float
    streams: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "container": self.container,
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "audio_codec": self.audio_codec,
            "timeline_start_seconds": self.timeline_start_seconds,
            "streams": self.streams,
        }


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MediaError("INVALID_MEDIA", f"invalid {field}") from exc
    if not math.isfinite(result):
        raise MediaError("INVALID_MEDIA", f"invalid {field}")
    return result


def validate_probe(probe: dict[str, Any], max_seconds: float = 1200.0) -> MediaInfo:
    fmt = probe.get("format")
    if not isinstance(fmt, dict):
        raise MediaError("INVALID_MEDIA", "media container metadata is missing")
    names = set(str(fmt.get("format_name", "")).split(","))
    if not names.intersection(SUPPORTED_FORMAT_TOKENS):
        raise MediaError("UNSUPPORTED_CONTAINER", "container is outside the verified MP4 family")
    streams = probe.get("streams")
    if not isinstance(streams, list):
        raise MediaError("INVALID_MEDIA", "media stream metadata is missing")
    videos = [
        stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    audios = [
        stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if len(videos) != 1 or len(audios) != 1:
        raise MediaError("UNSUPPORTED_STREAM_LAYOUT", "exactly one video and one audio stream are required")
    video, audio = videos[0], audios[0]
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        raise MediaError("UNSUPPORTED_VIDEO_CODEC", "only H.264 yuv420p video is allowlisted")
    if audio.get("codec_name") != "aac":
        raise MediaError("UNSUPPORTED_AUDIO_CODEC", "only AAC audio is allowlisted")
    if "start_time" not in fmt:
        raise MediaError("INSUFFICIENT_TIMESTAMP_EVIDENCE", "container timestamp evidence is missing")
    format_start = _number(fmt["start_time"], "format.start_time")
    for stream, name in ((video, "video"), (audio, "audio")):
        if "start_time" not in stream or "time_base" not in stream:
            raise MediaError("INSUFFICIENT_TIMESTAMP_EVIDENCE", f"{name} timestamp evidence is missing")
    video_start = _number(video["start_time"], "video.start_time")
    audio_start = _number(audio["start_time"], "audio.start_time")
    if max(abs(format_start), abs(video_start), abs(audio_start)) > 0.001:
        raise MediaError("UNSUPPORTED_TIMELINE_OFFSET", "non-zero track timestamps are not playback-verified")

    container_duration = _number(fmt.get("duration"), "format.duration")
    if container_duration <= 0 or container_duration > max_seconds:
        code = "VIDEO_DURATION_LIMIT" if container_duration > max_seconds else "INVALID_DURATION"
        raise MediaError(code, "video duration must be greater than zero and at most 1200 seconds")
    video_duration = _number(video.get("duration", container_duration), "video.duration")
    audio_duration = _number(audio.get("duration", container_duration), "audio.duration")
    if video_duration <= 0 or audio_duration <= 0:
        raise MediaError("INVALID_DURATION", "audio and video durations must be positive")
    effective_duration = min(container_duration, video_duration, audio_duration)
    if effective_duration > max_seconds:
        raise MediaError("VIDEO_DURATION_LIMIT", "video exceeds the 1200 second duration limit")
    sanitized_streams = [
        {
            "index": int(stream.get("index", index)),
            "type": str(stream.get("codec_type", ""))[:16],
            "codec": str(stream.get("codec_name", ""))[:32],
            "start_time": _number(stream["start_time"], "stream.start_time"),
            "duration": _number(stream.get("duration", container_duration), "stream.duration"),
            "time_base": str(stream["time_base"])[:32],
            **({"pixel_format": str(stream["pix_fmt"])[:24]} if stream.get("codec_type") == "video" else {}),
            **({"width": int(stream["width"]), "height": int(stream["height"])} if "width" in stream else {}),
            **({"sample_rate": str(stream["sample_rate"])[:16]} if "sample_rate" in stream else {}),
            **({"channels": int(stream["channels"])} if "channels" in stream else {}),
        }
        for index, stream in enumerate((video, audio))
    ]
    preferred_name = next(name for name in ("mp4", "mov", "m4a", "3gp", "3g2", "mj2") if name in names)
    return MediaInfo(
        duration_seconds=effective_duration,
        container=preferred_name,
        video_codec="h264",
        pixel_format="yuv420p",
        audio_codec="aac",
        timeline_start_seconds=0.0,
        streams=sanitized_streams,
    )


def _run_bounded(command: list[str], timeout: float, grace: float, max_output: int) -> bytes:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    if process.stdout is None:
        process.kill()
        process.wait(timeout=grace)
        raise MediaError("MEDIA_PROCESS_FAILED", "could not capture bounded ffprobe output")
    stdout = process.stdout
    captured = bytearray()
    exceeded = threading.Event()

    def read_output() -> None:
        while chunk := stdout.read(16 * 1024):
            if len(captured) + len(chunk) > max_output:
                exceeded.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            captured.extend(chunk)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    os_runtime: Any = os
    signal_runtime: Any = signal
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            process.terminate()
        else:
            try:
                os_runtime.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os_runtime.killpg(process.pid, signal_runtime.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired as stop_exc:
                raise MediaError(
                    "MEDIA_STOP_UNCONFIRMED", "ffprobe did not stop after its deadline"
                ) from stop_exc
        reader.join(timeout=grace)
        stdout.close()
        reader.join(timeout=grace)
        if reader.is_alive():
            raise MediaError("MEDIA_STOP_UNCONFIRMED", "ffprobe output reader did not stop") from exc
        raise MediaError("MEDIA_TIMEOUT", "ffprobe exceeded its total deadline") from exc
    reader.join(timeout=grace)
    stdout.close()
    if reader.is_alive() or process.poll() is None:
        process.kill()
        process.wait(timeout=grace)
        raise MediaError("MEDIA_STOP_UNCONFIRMED", "ffprobe output reader did not stop")
    if exceeded.is_set():
        raise MediaError("MEDIA_DIAGNOSTIC_LIMIT", "ffprobe metadata exceeded its output limit")
    if process.returncode:
        raise MediaError("MEDIA_COMMAND_FAILED", "ffprobe could not parse this media file")
    return bytes(captured)


def probe_media(path: Path, executable: str, timeout: float, grace: float) -> MediaInfo:
    command = [
        executable,
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration,start_time:stream=index,codec_type,codec_name,pix_fmt,start_time,duration,time_base,width,height,sample_rate,channels",
        "-of",
        "json",
        str(path),
    ]
    try:
        output = _run_bounded(command, timeout, grace, MAX_FFPROBE_OUTPUT_BYTES)
        raw = json.loads(output)
    except json.JSONDecodeError as exc:
        raise MediaError("INVALID_MEDIA", "ffprobe did not return valid metadata") from exc
    except OSError as exc:
        raise MediaError("FFPROBE_UNAVAILABLE", "ffprobe is not available") from exc
    if not isinstance(raw, dict):
        raise MediaError("INVALID_MEDIA", "ffprobe returned an invalid metadata object")
    return validate_probe(raw)
