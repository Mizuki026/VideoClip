from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import subprocess
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "data" / "mvp-stage-2" / ".env"
COMPOSE = ROOT / "compose.yaml"
DEFAULT_SAMPLE = ROOT / "video" / "如何选择适合自己的agent.mp4"


class Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def chunks(path: Path, digest: Digest) -> Iterator[bytes]:
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
            yield block


def check(response: httpx.Response, expected: int, action: str) -> httpx.Response:
    if response.status_code != expected:
        raise RuntimeError(f"{action} returned HTTP {response.status_code}")
    return response


def restart_services() -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-p",
            "videoclip-mvp-stage2",
            "-f",
            str(COMPOSE),
            "restart",
            "api",
            "worker",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    _ = result


def wait_for_api(client: httpx.Client) -> None:
    deadline = time.monotonic() + 120
    while True:
        try:
            check(client.get("/health", timeout=3), 200, "API restart health")
            return
        except (httpx.HTTPError, RuntimeError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Stage 2 API did not return after restart") from None
            time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real Stage 2 upload, ASR, and persistence acceptance.")
    parser.add_argument("sample", nargs="?", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument(
        "--skip-restart", action="store_true", help="Skip the post-transcription restart check"
    )
    arguments = parser.parse_args()
    sample = arguments.sample.resolve(strict=True)
    if not ENV_FILE.is_file():
        raise SystemExit("Run scripts/stage2/prepare-stage2.ps1 before acceptance.")
    config = dotenv_values(ENV_FILE)
    origin = str(config.get("APP_ORIGIN") or "http://127.0.0.1:5174")
    api_url = str(config.get("MVP_STAGE2_API_URL") or "http://127.0.0.1:58002")
    username = "stage2-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    digest = hashlib.sha256()
    timeout = httpx.Timeout(1800.0, connect=15.0)
    with httpx.Client(base_url=api_url, timeout=timeout, follow_redirects=False) as client:
        csrf = check(client.get("/auth/csrf"), 200, "anonymous CSRF token").json()["csrf_token"]
        headers = {"Origin": origin, "X-CSRF-Token": csrf}
        check(
            client.post("/auth/register", json={"username": username, "password": password}, headers=headers),
            201,
            "registration",
        )
        check(
            client.post("/auth/login", json={"username": username, "password": password}, headers=headers),
            200,
            "login",
        )
        csrf = check(client.get("/auth/csrf"), 200, "authenticated CSRF token").json()["csrf_token"]
        upload_headers = {
            "Origin": origin,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/octet-stream",
            "X-Video-Filename": urllib.parse.quote(sample.name, safe=""),
        }
        uploaded = check(
            client.post("/videos", content=chunks(sample, digest), headers=upload_headers),
            201,
            "authorized source upload",
        ).json()["video"]
        byte_count = sample.stat().st_size
        source_digest = digest.hexdigest()
        if uploaded["byte_count"] != byte_count or uploaded["content_sha256"] != source_digest:
            raise RuntimeError("persisted upload size or SHA-256 does not match the authorized source")

        csrf = check(client.get("/auth/csrf"), 200, "task submission CSRF token").json()["csrf_token"]
        task = check(
            client.post(
                f"/videos/{uploaded['id']}/tasks",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
            ),
            202,
            "analysis task submission",
        ).json()
        if task["delivery_status"] not in {"sent", "unknown"}:
            raise RuntimeError(f"unexpected task delivery state: {task['delivery_status']}")
        started = time.monotonic()
        deadline = started + 10_900
        while True:
            task = check(
                client.get(f"/videos/{uploaded['id']}/tasks/{task['id']}", timeout=15),
                200,
                "task polling",
            ).json()
            if (
                task["status"] == "awaiting_summary"
                and task["execution_state"] == "stopped"
                and task["execution_stop_confirmed"] is True
            ):
                break
            if task["status"] == "failed":
                raise RuntimeError(f"analysis attempt failed: {task['failure_code']}")
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for the Stage 2 transcript")
            time.sleep(1)
        elapsed = round(time.monotonic() - started, 2)
        expected = task["expected_segments"]
        calls = task["asr_calls"]
        if (
            expected <= 0
            or task["completed_segments"] != expected
            or task["asr_request_count"] != expected
            or len(calls) != expected
            or [item["segment_number"] for item in calls] != list(range(expected))
            or any(item["http_status"] != 200 for item in calls)
        ):
            raise RuntimeError("ASR request and transcript checkpoint counts are inconsistent")
        if task["transcript_complete"] is not True or task["summary_status"] != "waiting":
            raise RuntimeError("the completed transcript is not marked as awaiting summary")
        if task["execution_state"] != "stopped" or task["execution_stop_confirmed"] is not True:
            raise RuntimeError("the Worker stop confirmation is missing")
        if task["status"] == "succeeded":
            raise RuntimeError("transcription alone must not mark the analysis as succeeded")

        transcript = check(client.get(f"/videos/{uploaded['id']}/transcript"), 200, "transcript read").json()
        if (
            transcript["complete"] is not True
            or len(transcript["items"]) != expected
            or [item["segment_number"] for item in transcript["items"]] != list(range(expected))
            or any(item["status"] != "succeeded" or not item["text"] for item in transcript["items"])
            or transcript["summary_status"] != "waiting"
        ):
            raise RuntimeError("the complete transcript checkpoint list is invalid")
        for item in transcript["items"]:
            if not (
                0
                <= item["video_start_seconds"]
                < item["video_end_seconds"]
                <= uploaded["duration_seconds"] + 0.001
            ):
                raise RuntimeError("a transcript segment falls outside the uploaded video timeline")

        if not arguments.skip_restart:
            restart_services()
            wait_for_api(client)
            reloaded = check(
                client.get(f"/videos/{uploaded['id']}/tasks/{task['id']}"),
                200,
                "task read after API and Worker restart",
            ).json()
            persisted = check(
                client.get(f"/videos/{uploaded['id']}/transcript"),
                200,
                "transcript read after API and Worker restart",
            ).json()
            if (
                reloaded["status"] != "awaiting_summary"
                or reloaded["completed_segments"] != expected
                or reloaded["expected_segments"] != expected
                or persisted["complete"] is not True
                or len(persisted["items"]) != expected
            ):
                raise RuntimeError("transcript checkpoints did not survive the service restart")

        print(
            json.dumps(
                {
                    "result": "passed",
                    "video_id": uploaded["id"],
                    "task_id": task["id"],
                    "source_bytes": byte_count,
                    "source_sha256": source_digest,
                    "video_duration_seconds": uploaded["duration_seconds"],
                    "status": task["status"],
                    "summary_status": task["summary_status"],
                    "execution_stop_confirmed": task["execution_stop_confirmed"],
                    "asr_model": task["asr_model"],
                    "configuration_sha256": task["config_sha256"],
                    "source_fingerprint": task["source_fingerprint"],
                    "manifest_fingerprint": task["manifest_fingerprint"],
                    "expected_segments": expected,
                    "completed_segments": task["completed_segments"],
                    "asr_request_count": task["asr_request_count"],
                    "asr_elapsed_seconds": task["asr_elapsed_seconds"],
                    "processing_elapsed_seconds": elapsed,
                    "asr_calls": calls,
                    "restart_persistence": "passed" if not arguments.skip_restart else "skipped",
                    "transcript_texts": "withheld",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )


if __name__ == "__main__":
    main()
