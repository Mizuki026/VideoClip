from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from videoclip.media import MediaError, _run_bounded, validate_probe


def supported_probe() -> dict[str, Any]:
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "61.25", "start_time": "0.000000"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "start_time": "0.000000",
                "duration": "61.250000",
                "time_base": "1/30000",
                "width": 1280,
                "height": 720,
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "start_time": "0.000000",
                "duration": "61.250000",
                "time_base": "1/48000",
                "sample_rate": "48000",
                "channels": 2,
            },
        ],
    }


def test_supported_zero_based_mp4_media_mapping() -> None:
    media = validate_probe(supported_probe())
    assert media.duration_seconds == 61.25
    assert media.container == "mp4"
    assert media.timeline_start_seconds == 0
    assert media.video_codec == "h264" and media.pixel_format == "yuv420p"
    assert media.audio_codec == "aac"
    assert len(media.streams) == 2


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda probe: probe["format"].update(format_name="matroska,webm"), "UNSUPPORTED_CONTAINER"),
        (lambda probe: probe["streams"].pop(), "UNSUPPORTED_STREAM_LAYOUT"),
        (lambda probe: probe["streams"][0].update(codec_name="hevc"), "UNSUPPORTED_VIDEO_CODEC"),
        (lambda probe: probe["streams"][0].update(pix_fmt="yuv422p"), "UNSUPPORTED_VIDEO_CODEC"),
        (lambda probe: probe["streams"][1].update(codec_name="opus"), "UNSUPPORTED_AUDIO_CODEC"),
        (lambda probe: probe["streams"][1].update(start_time="0.5"), "UNSUPPORTED_TIMELINE_OFFSET"),
        (lambda probe: probe["format"].update(start_time="-0.25"), "UNSUPPORTED_TIMELINE_OFFSET"),
        (lambda probe: probe["streams"][0].pop("time_base"), "INSUFFICIENT_TIMESTAMP_EVIDENCE"),
        (lambda probe: probe["format"].update(duration="nan"), "INVALID_MEDIA"),
        (lambda probe: probe["format"].update(duration="0"), "INVALID_DURATION"),
        (lambda probe: probe["format"].update(duration="1200.01"), "VIDEO_DURATION_LIMIT"),
    ],
)
def test_media_policy_rejects_unverified_inputs(mutate, expected: str) -> None:
    probe = supported_probe()
    mutate(probe)
    with pytest.raises(MediaError) as raised:
        validate_probe(probe)
    assert raised.value.code == expected


@pytest.mark.parametrize("duration", [0.001, 368.6, 1200.0])
def test_media_policy_accepts_positive_duration_through_twenty_minutes(duration: float) -> None:
    probe = supported_probe()
    probe["format"]["duration"] = str(duration)
    for stream in probe["streams"]:
        stream["duration"] = str(duration)
    assert validate_probe(probe).duration_seconds == duration


def test_ffprobe_timeout_stops_child_process_with_bounded_diagnostics(tmp_path: Path) -> None:
    pid_file = tmp_path / "child-pid.txt"
    script = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(10)"
    )
    started = time.monotonic()
    timeout = 1.5 if sys.platform == "win32" else 0.2
    with pytest.raises(MediaError) as raised:
        _run_bounded([sys.executable, "-c", script], timeout, 0.2, 1024)
    assert raised.value.code == "MEDIA_TIMEOUT"
    assert time.monotonic() - started < timeout + 2
    assert pid_file.exists()


def test_ffprobe_output_limit_stops_noisy_child() -> None:
    with pytest.raises(MediaError) as raised:
        _run_bounded([sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"], 2, 0.2, 1024)
    assert raised.value.code == "MEDIA_DIAGNOSTIC_LIMIT"
