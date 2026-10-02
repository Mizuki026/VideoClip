from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "data" / "mvp-stage-2" / ".env"
COMPOSE = ROOT / "compose.yaml"
STOP_SCRIPT = ROOT / "scripts" / "stage2" / "stop-and-confirm-worker.ps1"
DEFAULT_SAMPLE = ROOT / "video" / "如何选择适合自己的agent.mp4"
STAGE2_PROJECT = "videoclip-mvp-stage2"


def chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            yield block


def require(response: httpx.Response, expected: int, action: str) -> httpx.Response:
    if response.status_code != expected:
        raise RuntimeError(f"{action} returned HTTP {response.status_code}")
    return response


def compose(config: dict[str, Any], *arguments: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-p",
            STAGE2_PROJECT,
            "-f",
            str(COMPOSE),
            *arguments,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Stage 2 Compose action failed ({arguments[0]})")
    _ = config
    return result.stdout.strip()


def database_value(config: dict[str, Any], sql: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-p",
            STAGE2_PROJECT,
            "-f",
            str(COMPOSE),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-qAt",
            "-U",
            str(config["POSTGRES_USER"]),
            "-d",
            str(config["POSTGRES_DB"]),
            "-c",
            sql,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Stage 2 read-only database preflight failed")
    return result.stdout.strip()


def status_counts(config: dict[str, Any]) -> str:
    return database_value(
        config,
        "SELECT COALESCE(string_agg(status || ':' || amount, ',' ORDER BY status), '') "
        "FROM (SELECT status, count(*) AS amount FROM analysis_tasks GROUP BY status) AS counts",
    )


def checkpoint_fingerprints(config: dict[str, Any], attempt_id: str) -> dict[int, tuple[str, str]]:
    parsed_attempt_id = uuid.UUID(attempt_id)
    rows = database_value(
        config,
        "SELECT COALESCE(string_agg(segment_number::text || ':' || audio_sha256 || ':' "
        "|| request_fingerprint, chr(10) ORDER BY segment_number), '') "
        "FROM transcript_segments "
        f"WHERE attempt_id = '{parsed_attempt_id}' AND status = 'succeeded'",
    )
    fingerprints: dict[int, tuple[str, str]] = {}
    for row in rows.splitlines():
        segment, audio_sha256, request_fingerprint = row.split(":", 2)
        fingerprints[int(segment)] = (audio_sha256, request_fingerprint)
    return fingerprints


def preflight(config: dict[str, Any]) -> str:
    if (
        database_value(config, "SELECT count(*) FROM analysis_executions WHERE stop_confirmed_at IS NULL")
        != "0"
    ):
        raise RuntimeError("Stage 2 has an unconfirmed execution; acceptance was not started")
    workers = compose(config, "ps", "-q", "worker").splitlines()
    if len(workers) != 1:
        raise RuntimeError("Stage 2 must have exactly one Worker before acceptance")
    return status_counts(config)


def make_fixture(source: Path, duration: int) -> Path:
    ffmpeg = shutil.which("ffmpeg") or r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"
    if not Path(ffmpeg).is_file():
        raise RuntimeError("FFmpeg is not available for the controlled Stage 3 fixture")
    output_dir = ROOT / "data" / "mvp-stage-2" / "app" / "recovery-acceptance"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"fixture-{uuid.uuid4().hex}.mp4"
    result = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(source),
            "-t",
            str(duration),
            "-vf",
            "scale=640:-2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "34",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0 or not output.is_file():
        output.unlink(missing_ok=True)
        raise RuntimeError("Could not create the Stage 3 controlled media fixture")
    return output


def csrf_headers(client: httpx.Client, origin: str) -> dict[str, str]:
    token = require(client.get("/auth/csrf"), 200, "CSRF token").json()["csrf_token"]
    return {"Origin": origin, "X-CSRF-Token": token}


def stop_execution(execution_id: str) -> None:
    powershell = shutil.which("pwsh") or "powershell"
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(STOP_SCRIPT),
            "-ExecutionId",
            execution_id,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stdout + "\n" + result.stderr).strip()
        raise RuntimeError(
            "The Stage 2 stop-and-confirm procedure did not confirm the execution"
            + (f": {details}" if details else "")
        )


def wait_for_api(api_url: str, timeout_seconds: int = 180) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{api_url}/health", timeout=3)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Stage 2 API did not become healthy after restart")


def cleanup_test_video(
    config: dict[str, Any],
    api_url: str,
    origin: str,
    username: str,
    password: str,
    video_id: str,
    task_id: str | None,
) -> None:
    with httpx.Client(base_url=api_url, timeout=60.0, follow_redirects=False) as client:
        login_headers = csrf_headers(client, origin)
        require(
            client.post(
                "/auth/login",
                json={"username": username, "password": password},
                headers=login_headers,
            ),
            200,
            "acceptance cleanup login",
        )
        execution_id = None
        if task_id is not None:
            execution_id = (
                database_value(
                    config,
                    "SELECT id::text FROM analysis_executions "
                    f"WHERE task_id = '{task_id}' AND stop_confirmed_at IS NULL LIMIT 1",
                )
                or None
            )

        delete = client.delete(f"/videos/{video_id}", headers=csrf_headers(client, origin))
        if delete.status_code not in {202, 204}:
            require(delete, 204, "acceptance video cleanup")
        if delete.status_code == 202 and execution_id is not None:
            try:
                stop_execution(execution_id)
            except RuntimeError:
                # The worker may have finished and recorded its own stop proof
                # between the database read and the guarded maintenance command.
                remaining = database_value(
                    config,
                    "SELECT count(*) FROM analysis_executions "
                    f"WHERE id = '{execution_id}' AND stop_confirmed_at IS NULL",
                )
                if remaining != "0":
                    raise

        for _ in range(60):
            if delete.status_code == 204:
                return
            time.sleep(1)
            delete = client.delete(f"/videos/{video_id}", headers=csrf_headers(client, origin))
            if delete.status_code not in {202, 204}:
                require(delete, 204, "acceptance video cleanup retry")
        raise RuntimeError("Acceptance video deletion is still pending after 60 retries")


def cleanup_test_account(config: dict[str, Any], username: str) -> None:
    if len(username) != 19 or not username.startswith("stage3-") or not username[7:].isalnum():
        raise RuntimeError("Refusing to clean up an account outside the generated acceptance namespace")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-p",
            STAGE2_PROJECT,
            "-f",
            str(COMPOSE),
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            'psql -X -v ON_ERROR_STOP=1 -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
            "-c",
            f"DELETE FROM users WHERE username = '{username}' "
            "AND NOT EXISTS (SELECT 1 FROM videos WHERE owner_id = users.id) "
            "AND NOT EXISTS (SELECT 1 FROM analysis_tasks WHERE owner_id = users.id)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("The generated Stage 3 acceptance account could not be removed")


def wait_for_task(
    client: httpx.Client,
    video_id: str,
    task_id: str,
    *,
    timeout_seconds: int,
    stop_after_checkpoint: bool,
) -> tuple[dict[str, Any], str | None]:
    deadline = time.monotonic() + timeout_seconds
    stopped_execution_id: str | None = None
    while time.monotonic() < deadline:
        task = require(
            client.get(f"/videos/{video_id}/tasks/{task_id}", timeout=15), 200, "task polling"
        ).json()
        if (
            stop_after_checkpoint
            and task["status"] == "running"
            and task["expected_segments"] >= 2
            and 0 < task["completed_segments"] < task["expected_segments"]
        ):
            execution_id = task["attempt"].get("execution_id")
            if execution_id:
                stop_execution(execution_id)
                stopped_execution_id = execution_id
                task = require(
                    client.get(f"/videos/{video_id}/tasks/{task_id}"), 200, "stopped task polling"
                ).json()
                break
        if not stop_after_checkpoint and (
            task["status"] == "awaiting_summary"
            and task["execution_stop_confirmed"] is True
            and task["current_attempt_id"] == task["attempt"]["id"]
        ):
            return task, stopped_execution_id
        if task["status"] == "awaiting_summary" and stop_after_checkpoint:
            raise RuntimeError("The controlled task completed before the stop could be confirmed")
        if task["status"] == "failed" and task["execution_stop_confirmed"] is True:
            if not stop_after_checkpoint:
                raise RuntimeError(f"Recovery attempt failed: {task['failure_code']}")
            break
        time.sleep(0.2)
    if stop_after_checkpoint:
        if stopped_execution_id is None:
            raise RuntimeError("Timed out before a partial transcript checkpoint was available")
        return task, stopped_execution_id
    raise RuntimeError("Timed out waiting for the recovered transcript")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Exercise one real Stage 2 ASR stop, manual recovery, and checkpoint reuse."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--duration-seconds", type=int, default=368)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    arguments = parser.parse_args()
    source = arguments.source.resolve(strict=True)
    if not ENV_FILE.is_file() or arguments.duration_seconds < 61:
        raise SystemExit("Stage 2 must be prepared and the fixture must span at least two ASR segments.")
    config = dotenv_values(ENV_FILE)
    origin = str(config.get("APP_ORIGIN") or "http://127.0.0.1:5174")
    api_url = str(config.get("MVP_STAGE2_API_URL") or "http://127.0.0.1:58002")
    baseline_tasks = preflight(config)
    fixture = make_fixture(source, arguments.duration_seconds)
    username = "stage3-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    video_id: str | None = None
    task_id: str | None = None
    child_worker_started = False
    registered = False
    result: dict[str, Any] = {"result": "failed"}
    try:
        timeout = httpx.Timeout(1800.0, connect=15.0)
        with httpx.Client(base_url=api_url, timeout=timeout, follow_redirects=False) as client:
            headers = csrf_headers(client, origin)
            require(
                client.post(
                    "/auth/register",
                    json={"username": username, "password": password},
                    headers=headers,
                ),
                201,
                "registration",
            )
            registered = True
            require(
                client.post(
                    "/auth/login",
                    json={"username": username, "password": password},
                    headers=headers,
                ),
                200,
                "login",
            )
            headers = csrf_headers(client, origin)
            digest = hashlib.sha256()

            def upload_blocks() -> Iterator[bytes]:
                with fixture.open("rb") as media:
                    while block := media.read(1024 * 1024):
                        digest.update(block)
                        yield block

            uploaded = require(
                client.post(
                    "/videos",
                    content=upload_blocks(),
                    headers={
                        **headers,
                        "Content-Type": "application/octet-stream",
                        "X-Video-Filename": urllib.parse.quote(fixture.name, safe=""),
                    },
                ),
                201,
                "controlled media upload",
            ).json()["video"]
            video_id = uploaded["id"]
            assert video_id is not None
            original_task = require(
                client.post(f"/videos/{video_id}/tasks", headers=csrf_headers(client, origin)),
                202,
                "first controlled task submission",
            ).json()
            task_id = original_task["id"]
            assert task_id is not None
            original_attempt_id = original_task["attempt"]["id"]

            failed, execution_id = wait_for_task(
                client,
                video_id,
                task_id,
                timeout_seconds=arguments.timeout_seconds,
                stop_after_checkpoint=True,
            )
            if (
                execution_id is None
                or failed["execution_stop_confirmed"] is not True
                or failed["attempt"]["id"] != original_attempt_id
                or not (0 < failed["completed_segments"] < failed["expected_segments"])
            ):
                raise RuntimeError("The original attempt did not stop with a partial checkpoint set")
            before = require(
                client.get(f"/videos/{video_id}/transcript"), 200, "partial transcript read"
            ).json()
            prior_fingerprints = checkpoint_fingerprints(config, original_attempt_id)
            prior_success = {
                item["segment_number"]: {
                    "audio_start_seconds": item["audio_start_seconds"],
                    "audio_end_seconds": item["audio_end_seconds"],
                    "actual_audio_seconds": item["actual_audio_seconds"],
                    "video_start_seconds": item["video_start_seconds"],
                    "video_end_seconds": item["video_end_seconds"],
                    "text_sha256": hashlib.sha256(item["text"].encode("utf-8")).hexdigest(),
                }
                for item in before["items"]
                if item["status"] == "succeeded" and item["text"] is not None
            }
            if len(prior_success) != failed["completed_segments"] or not prior_success:
                raise RuntimeError("Partial transcript rows and committed checkpoint count do not agree")
            if len(prior_fingerprints) != len(prior_success) or failed["manifest_fingerprint"] is None:
                raise RuntimeError("Partial checkpoint fingerprint metadata is incomplete")

            recovered = require(
                client.post(
                    f"/videos/{video_id}/tasks/{task_id}/recover",
                    json={"attempt_id": original_attempt_id},
                    headers=csrf_headers(client, origin),
                ),
                202,
                "manual recovery request",
            ).json()
            child_attempt_id = recovered["attempt"]["id"]
            if (
                child_attempt_id == original_attempt_id
                or recovered["attempt"]["parent_attempt_id"] != original_attempt_id
                or recovered["attempt"]["attempt_number"] != 2
            ):
                raise RuntimeError("Recovery did not create one auditable child attempt")
            compose(config, "up", "-d", "worker")
            child_worker_started = True
            completed, _ = wait_for_task(
                client,
                video_id,
                task_id,
                timeout_seconds=arguments.timeout_seconds,
                stop_after_checkpoint=False,
            )
            if (
                completed["status"] != "awaiting_summary"
                or completed["summary_status"] != "waiting"
                or completed["execution_stop_confirmed"] is not True
                or completed["completed_segments"] != completed["expected_segments"]
                or completed["reused_segments"] != len(prior_success)
                or completed["asr_request_count"] != completed["expected_segments"] - len(prior_success)
                or completed["manifest_fingerprint"] != failed["manifest_fingerprint"]
                or completed["source_fingerprint"] != failed["source_fingerprint"]
                or completed["config_sha256"] != failed["config_sha256"]
            ):
                raise RuntimeError("Recovered attempt counts or terminal state are inconsistent")
            recovered_fingerprints = checkpoint_fingerprints(config, child_attempt_id)
            if any(
                recovered_fingerprints.get(segment_number) != fingerprints
                for segment_number, fingerprints in prior_fingerprints.items()
            ):
                raise RuntimeError("A reused checkpoint changed its audio or ASR request fingerprint")
            after = require(
                client.get(f"/videos/{video_id}/transcript"), 200, "recovered transcript read"
            ).json()
            for item in after["items"]:
                prior = prior_success.get(item["segment_number"])
                if prior is None:
                    continue
                current = {
                    "audio_start_seconds": item["audio_start_seconds"],
                    "audio_end_seconds": item["audio_end_seconds"],
                    "actual_audio_seconds": item["actual_audio_seconds"],
                    "video_start_seconds": item["video_start_seconds"],
                    "video_end_seconds": item["video_end_seconds"],
                    "text_sha256": hashlib.sha256(item["text"].encode("utf-8")).hexdigest(),
                }
                if item["source_attempt_id"] != original_attempt_id or current != prior:
                    raise RuntimeError("A reused checkpoint changed its text, range, or source attempt")
            if not after["complete"] or len(after["items"]) != completed["expected_segments"]:
                raise RuntimeError("The recovered transcript is incomplete")

            compose(config, "restart", "api", "worker")
            child_worker_started = True
            wait_for_api(api_url)
            persisted = require(
                client.get(f"/videos/{video_id}/tasks/{task_id}"), 200, "post-restart task read"
            ).json()
            persisted_transcript = require(
                client.get(f"/videos/{video_id}/transcript"), 200, "post-restart transcript read"
            ).json()
            if (
                persisted["status"] != "awaiting_summary"
                or persisted["completed_segments"] != completed["expected_segments"]
                or persisted["reused_segments"] != len(prior_success)
                or not persisted_transcript["complete"]
            ):
                raise RuntimeError("Recovered checkpoints did not survive the API and Worker restart")
            result = {
                "result": "passed",
                "video_id": video_id,
                "task_id": task_id,
                "source_bytes": fixture.stat().st_size,
                "source_sha256": digest.hexdigest(),
                "duration_seconds": uploaded["duration_seconds"],
                "original_attempt_id": original_attempt_id,
                "stopped_execution_id": execution_id,
                "original_completed_segments": failed["completed_segments"],
                "original_asr_request_count": failed["asr_request_count"],
                "recovered_attempt_id": child_attempt_id,
                "reused_segments": completed["reused_segments"],
                "reused_fingerprints": "unchanged",
                "recovery_asr_request_count": completed["asr_request_count"],
                "expected_segments": completed["expected_segments"],
                "final_status": completed["status"],
                "summary_status": completed["summary_status"],
                "restart_persistence": "passed",
                "transcript_texts": "withheld; reused texts compared by SHA-256",
            }
    finally:
        try:
            if video_id is not None:
                cleanup_test_video(config, api_url, origin, username, password, video_id, task_id)
            if registered:
                cleanup_test_account(config, username)
        finally:
            try:
                compose(config, "up", "-d", "worker")
            except RuntimeError:
                if child_worker_started:
                    raise
            finally:
                fixture.unlink(missing_ok=True)

    if status_counts(config) != baseline_tasks:
        raise RuntimeError("The acceptance run changed pre-existing Stage 2 task records")
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
