from __future__ import annotations

import hashlib
import http.cookiejar as cookiejar
import json
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STAGE_ROOT = ROOT / "data" / "mvp-stage-2"
APP_ROOT = STAGE_ROOT / "app"
INCOMING = APP_ROOT / "incoming"
BASE_URL = "http://127.0.0.1:58002"
ORIGIN = "http://127.0.0.1:5174"
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    str(STAGE_ROOT / ".env"),
    "-p",
    "videoclip-mvp-stage2",
    "-f",
    str(ROOT / "compose.yaml"),
]
UPLOAD_CHUNK = b"x"
CHUNK_HASH = hashlib.sha256(UPLOAD_CHUNK).hexdigest()
CYCLE_RE = re.compile(r"reclaim_cycle_finished status=completed reclaimed=(\d+)")


def run(command: list[str], *, timeout: int = 30, input_text: str | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        input=input_text,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {Path(command[0]).name}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def compose(*args: str, timeout: int = 30) -> str:
    return run([*COMPOSE, *args], timeout=timeout)


def private_setting(name: str) -> str:
    for line in (STAGE_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip()
            if not re.fullmatch(r"[A-Za-z0-9_]+", value):
                raise RuntimeError(f"unsafe private setting format: {name}")
            return value
    raise RuntimeError(f"missing private setting: {name}")


DB_NAME = private_setting("POSTGRES_DB")
DB_USER = private_setting("POSTGRES_USER")


def psql(query: str, **variables: str) -> str:
    command = [
        *COMPOSE,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-qAt",
        "-U",
        DB_USER,
        "-d",
        DB_NAME,
    ]
    for name, value in variables.items():
        command.extend(["-v", f"{name}={value}"])
    command.extend(["-f", "-"])
    return run(command, input_text=query)


def http(
    opener: urllib.request.OpenerDirector,
    path: str,
    *,
    method: str = "GET",
    payload: Any | None = None,
    extra_headers: dict[str, str] | None = None,
    expected: tuple[int, ...] = (200,),
) -> tuple[int, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=120) as response:
            status = response.status
            content = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        content = exc.read()
    if status not in expected:
        raise RuntimeError(f"HTTP contract failed: {method} {path} returned {status}")
    return status, json.loads(content) if content else None


def logs() -> str:
    return compose("logs", "--no-color", "--since", "20m", "beat", "maintenance-worker")


def count_cycles(log_text: str) -> list[int]:
    return [int(value) for value in CYCLE_RE.findall(log_text)]


def create_fixture(opener: urllib.request.OpenerDirector, csrf: str, file_name: str) -> str:
    key = uuid.uuid4().hex
    _, created = http(
        opener,
        "/uploads",
        method="POST",
        payload={"file_name": file_name, "total_bytes": 1, "sha256": CHUNK_HASH},
        extra_headers={"Origin": ORIGIN, "X-CSRF-Token": csrf, "Idempotency-Key": key},
        expected=(201,),
    )
    upload_id = created["upload_id"]
    request = urllib.request.Request(
        f"{BASE_URL}/uploads/{upload_id}/chunks/0",
        data=UPLOAD_CHUNK,
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "X-Chunk-SHA256": CHUNK_HASH,
            "Content-Type": "application/octet-stream",
        },
        method="PUT",
    )
    with opener.open(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("fixture chunk was not accepted")
        chunk_result = json.loads(response.read())
    if chunk_result["confirmed_offset"] != 1:
        raise RuntimeError("fixture chunk did not advance the server offset")
    part_path = INCOMING / f"{uuid.UUID(upload_id).hex}.part"
    if not part_path.is_file() or part_path.read_bytes() != UPLOAD_CHUNK:
        raise RuntimeError("fixture .part file did not match the accepted byte")
    return upload_id


def update_expiry(upload_ids: list[str], username: str) -> None:
    for upload_id in upload_ids:
        returned = psql(
            "UPDATE upload_handoffs SET expires_at = now() - interval '1 minute' "
            "WHERE id = :'upload_id'::uuid AND owner_id = "
            "(SELECT id FROM users WHERE username = :'username') "
            "AND protocol_type = 'resumable' AND state = 'receiving' "
            "AND declared_total_bytes = 1 RETURNING id",
            upload_id=upload_id,
            username=username,
        )
        if returned != upload_id:
            raise RuntimeError("expiry injection did not match the isolated fixture row")


def state(upload_id: str) -> str:
    query = "SELECT state FROM upload_handoffs WHERE id = :'upload_id'::uuid"
    return psql(query, upload_id=upload_id) or "missing"


def cleanup_intent_state(upload_id: str) -> str:
    query = (
        "SELECT state FROM cleanup_intents "
        "WHERE upload_id = :'upload_id'::uuid AND kind = 'upload'"
    )
    return psql(query, upload_id=upload_id) or "missing"


def delete_test_user(username: str) -> None:
    removed = psql(
        "DELETE FROM users WHERE username = :'username' "
        "AND NOT EXISTS (SELECT 1 FROM videos WHERE owner_id = users.id) "
        "AND NOT EXISTS (SELECT 1 FROM upload_handoffs WHERE owner_id = users.id) "
        "RETURNING username",
        username=username,
    )
    if removed != username:
        raise RuntimeError("test user still owns business data; preserved for investigation")


def main() -> None:
    health = urllib.request.urlopen(f"{BASE_URL}/health", timeout=5).read()
    if json.loads(health).get("status") != "ok":
        raise RuntimeError("Stage 2 API is not healthy")

    username = f"stage2-maintenance-{secrets.token_hex(6)}"
    password = f"Maintenance-{secrets.token_urlsafe(24)}!"
    cookie_jar = cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    _, csrf_body = http(opener, "/auth/csrf")
    csrf = csrf_body["csrf_token"]
    http(
        opener,
        "/auth/register",
        method="POST",
        payload={"username": username, "password": password},
        extra_headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        expected=(201,),
    )
    http(
        opener,
        "/auth/login",
        method="POST",
        payload={"username": username, "password": password},
        extra_headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    _, csrf_body = http(opener, "/auth/csrf")
    csrf = csrf_body["csrf_token"]

    before_counts = psql(
        "SELECT concat_ws(',', (SELECT count(*) FROM videos), (SELECT count(*) FROM analysis_tasks), "
        "(SELECT count(*) FROM analysis_attempts), (SELECT count(*) FROM transcript_segments), "
        "(SELECT count(*) FROM analysis_summaries))"
    )
    baseline_logs = logs()
    baseline_cycles = len(count_cycles(baseline_logs))
    baseline_beat_sends = baseline_logs.count("Scheduler: Sending due task reclaim-expired-resumable-uploads")
    analysis_queue_before = compose("exec", "-T", "redis", "redis-cli", "LLEN", "analysis")
    if analysis_queue_before != "0":
        raise RuntimeError("analysis queue must be empty before maintenance acceptance")

    normal_id = create_fixture(opener, csrf, "maintenance-normal.mp4")
    active_id = create_fixture(opener, csrf, "maintenance-active.mp4")
    failed_id = create_fixture(opener, csrf, "maintenance-retry.mp4")
    fixture_ids = [normal_id, active_id, failed_id]
    failed_part = INCOMING / f"{uuid.UUID(failed_id).hex}.part"
    failed_part.unlink()
    failed_part.mkdir()

    token = secrets.token_hex(6)
    release_path = APP_ROOT / f"maintenance-lease-release-{token}"
    ready_path = APP_ROOT / f"maintenance-lease-ready-{token}"
    done_path = APP_ROOT / f"maintenance-lease-done-{token}"
    for marker in (release_path, ready_path, done_path):
        if marker.exists():
            raise RuntimeError("acceptance marker already exists")

    maintenance_container = compose("ps", "-q", "maintenance-worker")
    release_arg = f"/appdata/{release_path.name}"
    ready_arg = f"/appdata/{ready_path.name}"
    done_arg = f"/appdata/{done_path.name}"
    lock_code = """
import os, sys, time, uuid
from pathlib import Path
from videoclip.storage import MediaStore
upload_id = uuid.UUID(sys.argv[1])
release = Path(sys.argv[2])
ready = Path(sys.argv[3])
done = Path(sys.argv[4])
lease = MediaStore(Path('/appdata')).acquire_lease(upload_id)
if lease is None:
    raise SystemExit('lease unavailable')
ready.write_text(str(os.getpid()))
while not release.exists():
    time.sleep(0.1)
lease.release()
done.write_text('released')
"""
    run([
        "docker", "exec", "-d", maintenance_container, "python", "-c", lock_code,
        active_id, release_arg, ready_arg, done_arg,
    ])
    deadline = time.monotonic() + 30
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if not ready_path.exists():
        raise RuntimeError("active upload lease fixture did not acquire the lease")

    update_expiry(fixture_ids, username)
    print("fixtures=3 expired; one live lease and one cleanup failure are armed", flush=True)
    try:
        first_deadline = time.monotonic() + 180
        first_logs = ""
        while time.monotonic() < first_deadline:
            first_logs = logs()
            cycles = count_cycles(first_logs)
            reclaimed_fixtures = sum(state(value) == "missing" for value in fixture_ids)
            if len(cycles) > baseline_cycles and reclaimed_fixtures == 1:
                break
            time.sleep(2)
        else:
            raise RuntimeError("Beat-triggered scan did not reach the injected cleanup failure")
        first_cycles = count_cycles(first_logs)
        normal_part = INCOMING / f"{uuid.UUID(normal_id).hex}.part"
        active_part = INCOMING / f"{uuid.UUID(active_id).hex}.part"
        if state(normal_id) != "missing" or normal_part.exists():
            raise RuntimeError("Beat scan did not safely reclaim the ordinary expired fixture")
        if state(active_id) != "receiving" or not active_part.is_file():
            raise RuntimeError("expired upload under an active lease was not skipped")
        if state(failed_id) != "expired" or not failed_part.is_dir():
            raise RuntimeError("failed cleanup intent was not retained for retry")
        if cleanup_intent_state(failed_id) != "cleanup_failed":
            raise RuntimeError("failed cleanup intent did not retain its retryable state")
        first_reclaimed = first_cycles[-1]
        if first_reclaimed < 1:
            raise RuntimeError("the first scheduled scan did not reclaim the eligible fixture")

        release_path.write_text("release", encoding="ascii")
        deadline = time.monotonic() + 15
        while not done_path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not done_path.exists():
            raise RuntimeError("active lease fixture did not release its lock")

        failed_part.rmdir()
        failed_part.write_bytes(UPLOAD_CHUNK)
        compose("restart", "maintenance-worker", timeout=60)
        print("maintenance_worker=restarted; retry fixture restored", flush=True)

        second_deadline = time.monotonic() + 180
        second_logs = ""
        while time.monotonic() < second_deadline:
            second_logs = logs()
            cycles = count_cycles(second_logs)
            active_removed = state(active_id) == "missing"
            failed_removed = state(failed_id) == "missing"
            if len(cycles) > len(first_cycles) and active_removed and failed_removed:
                break
            time.sleep(2)
        else:
            raise RuntimeError("later Beat scan did not retry the expired cleanup intents")
        second_cycles = count_cycles(second_logs)
        for upload_id in fixture_ids:
            part_path = INCOMING / f"{uuid.UUID(upload_id).hex}.part"
            lease_path = APP_ROOT / "leases" / f"{uuid.UUID(upload_id).hex}.lock"
            if state(upload_id) != "missing" or part_path.exists():
                raise RuntimeError("periodic retry left an isolated fixture behind")
            if not lease_path.is_file() or lease_path.stat().st_size != 1:
                raise RuntimeError("stable lease marker inode was unexpectedly removed")

        sends_after = second_logs.count("Scheduler: Sending due task reclaim-expired-resumable-uploads")
        worker_received = second_logs.count("Task videoclip.maintenance.reclaim_expired_uploads[")
        if sends_after <= baseline_beat_sends or worker_received < baseline_cycles + 2:
            raise RuntimeError("Beat/Redis/maintenance Worker delivery evidence is incomplete")
        if "Scheduler: Sending due task" not in second_logs or "reclaim_cycle_finished" not in second_logs:
            raise RuntimeError("scheduled publication or maintenance task evidence is missing")
        queue_after = compose("exec", "-T", "redis", "redis-cli", "LLEN", "maintenance")
        analysis_queue_after = compose("exec", "-T", "redis", "redis-cli", "LLEN", "analysis")
        if queue_after != "0" or analysis_queue_after != "0":
            raise RuntimeError("a queue accumulated unexpected messages during the acceptance")
        unconfirmed_query = "SELECT count(*) FROM analysis_executions WHERE stop_confirmed_at IS NULL"
        unconfirmed = compose(
            "exec", "-T", "postgres", "psql", "-X", "-qAt", "-U", DB_USER, "-d", DB_NAME,
            "-c", unconfirmed_query,
        )
        if int(unconfirmed) != 0:
            raise RuntimeError("maintenance acceptance altered or created an unconfirmed analysis execution")
        if psql(
            "SELECT concat_ws(',', (SELECT count(*) FROM videos), (SELECT count(*) FROM analysis_tasks), "
            "(SELECT count(*) FROM analysis_attempts), (SELECT count(*) FROM transcript_segments), "
            "(SELECT count(*) FROM analysis_summaries))"
        ) != before_counts:
            raise RuntimeError("maintenance acceptance changed historical analysis/video table counts")

        delete_test_user(username)
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "beat_schedule_sends_observed": sends_after - baseline_beat_sends,
                    "maintenance_worker_received": worker_received,
                    "first_cycle_reclaimed": first_reclaimed,
                    "retry_cycle_reclaimed": second_cycles[-1],
                    "active_lease_skipped_then_reclaimed": True,
                    "cleanup_failure_retained_then_retried": True,
                    "lease_markers_preserved": True,
                    "analysis_queue_depth": int(analysis_queue_after),
                    "unconfirmed_analysis_executions": 0,
                    "test_account_removed": True,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        if not done_path.exists():
            release_path.write_text("release", encoding="ascii")
        for marker in (release_path, ready_path, done_path):
            try:
                marker.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
