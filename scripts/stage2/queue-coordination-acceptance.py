from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "compose.yaml"
COMPOSE_OVERRIDE = ROOT / "scripts" / "stage2" / "queue-coordination-acceptance.override.yaml"
PROJECT_PREFIX = "videoclip-mvp-stage4-"


class AcceptanceFailure(RuntimeError):
    pass


class Runner:
    def __init__(self, project: str, environment_file: Path, output_dir: Path) -> None:
        self.project = project
        self.environment_file = environment_file
        self.output_dir = output_dir
        self.events_path = output_dir / "events.jsonl"
        self.compose = [
            "docker",
            "compose",
            "--env-file",
            str(environment_file),
            "-p",
            project,
            "-f",
            str(COMPOSE_FILE),
            "-f",
            str(COMPOSE_OVERRIDE),
        ]

    def record(self, event: str, **values: Any) -> None:
        with self.events_path.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(
                    {"at": datetime.now(UTC).isoformat(), "event": event, **values},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )

    def command(
        self,
        argv: list[str],
        *,
        timeout: float = 30,
        expected: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        self.record("command", argv=argv, exit_code=result.returncode)
        if result.returncode not in expected:
            raise AcceptanceFailure(f"controlled Stage 4 command failed: {argv[0]}")
        return result

    def compose_call(
        self,
        *arguments: str,
        timeout: float = 30,
        expected: tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[str]:
        return self.command(self.compose + list(arguments), timeout=timeout, expected=expected)

    def psql(self, sql: str) -> str:
        values = read_environment(self.environment_file)
        database = values.get("POSTGRES_DB", "")
        user = values.get("POSTGRES_USER", "")
        if not database.replace("_", "").isalnum() or not user.replace("_", "").isalnum():
            raise AcceptanceFailure("isolated Stage 4 database identifiers are invalid")
        result = self.compose_call(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-qAt",
            "-U",
            user,
            "-d",
            database,
            "-c",
            sql,
        )
        return result.stdout.strip()

    def database_snapshot(self) -> dict[str, Any]:
        raw = self.psql(
            "SELECT json_build_object("
            "'users',(SELECT count(*) FROM users),"
            "'videos',(SELECT count(*) FROM videos),"
            "'tasks',(SELECT count(*) FROM analysis_tasks),"
            "'attempts',(SELECT count(*) FROM analysis_attempts),"
            "'executions',(SELECT count(*) FROM analysis_executions),"
            "'unconfirmed',(SELECT count(*) FROM analysis_executions WHERE stop_confirmed_at IS NULL),"
            "'analysis_running',(SELECT count(*) FROM analysis_tasks WHERE status='running')"
            ")::text"
        )
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise AcceptanceFailure("isolated Stage 4 database snapshot was invalid")
        return value

    def analysis_queue_length(self) -> int:
        result = self.compose_call("exec", "-T", "redis", "redis-cli", "LLEN", "analysis")
        return int(result.stdout.strip())

    def stub_control(self, path: str) -> dict[str, Any]:
        method = "POST" if path == "__control/release" else "GET"
        request = urllib.request.Request(
            f"https://127.0.0.1:443/{path}",
            data=b"" if method == "POST" else None,
            method=method,
        )
        code = (
            "import json,ssl,urllib.request; "
            f"request=urllib.request.Request({request.full_url!r}, "
            f"data={request.data!r}, method={method!r}); "
            "r=urllib.request.urlopen(request, "
            "context=ssl._create_unverified_context(), timeout=5); "
            "print(r.read().decode())"
        )
        result = self.compose_call("exec", "-T", "model-stub", "python", "-c", code, timeout=10)
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise AcceptanceFailure("local model substitute returned invalid control state")
        return value


def read_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def require(response: httpx.Response, expected: int, action: str) -> httpx.Response:
    if response.status_code != expected:
        raise AcceptanceFailure(f"{action} returned HTTP {response.status_code}")
    return response


def csrf_headers(client: httpx.Client, origin: str) -> dict[str, str]:
    token = require(client.get("/auth/csrf"), 200, "CSRF bootstrap").json()["csrf_token"]
    return {"Origin": origin, "X-CSRF-Token": token}


def api_task(client: httpx.Client, video_id: str, task_id: str, origin: str) -> dict[str, Any]:
    response = require(client.get(f"/videos/{video_id}/tasks/{task_id}"), 200, "task status")
    value = response.json()
    if not isinstance(value, dict):
        raise AcceptanceFailure("task status response was not an object")
    _ = origin
    return value


def wait_for(
    predicate: Any,
    *,
    timeout_seconds: float,
    message: str,
    interval_seconds: float = 0.5,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_seconds)
    raise AcceptanceFailure(message)


def task_rows(runner: Runner, task_ids: list[str]) -> list[dict[str, Any]]:
    parsed = [str(uuid.UUID(value)) for value in task_ids]
    id_list = ",".join(f"'{value}'::uuid" for value in parsed)
    raw = runner.psql(
        "SELECT COALESCE(json_agg(json_build_object("
        "'task_id',t.id::text,'status',t.status,'queue_entered_at',t.queue_entered_at,"
        "'delivery_next_check_at',t.delivery_next_check_at,'delivery_retry_count',t.delivery_retry_count,"
        "'delivery_operation_count',t.delivery_operation_count,'delivery_wait_reason',t.delivery_wait_reason,"
        "'attempt_id',a.id::text,'claim_count',a.claim_count,'attempt_status',a.status,"
        "'first_claimed_at',a.first_claimed_at,'execution_started_at',e.started_at,"
        "'delivery_status',d.status,'generation',d.operation_generation"
        ") ORDER BY t.queue_entered_at,t.id), '[]'::json) "
        "FROM analysis_tasks t JOIN analysis_attempts a ON a.id=t.current_attempt_id "
        "JOIN task_deliveries d ON d.attempt_id=a.id "
        "LEFT JOIN analysis_executions e ON e.attempt_id=a.id "
        f"WHERE t.id IN ({id_list})"
    )
    value = json.loads(raw)
    if not isinstance(value, list):
        raise AcceptanceFailure("isolated task row snapshot was invalid")
    return value


def run_browser_queue_check(
    runner: Runner,
    *,
    api_url: str,
    username: str,
    password: str,
    video_id: str,
    output_dir: Path,
) -> None:
    browser_output = output_dir / f"playwright-{uuid.uuid4().hex}"
    environment = os.environ.copy()
    environment.update(
        {
            "MVP_WEB_MODE": "dev",
            "VIDEOCLIP_WEB_PORT": read_environment(runner.environment_file)["MVP_STAGE1_WEB_PORT"],
            "VIDEOCLIP_API_TARGET": api_url,
            "MVP_PLAYWRIGHT_OUTPUT_DIR": str(browser_output),
            "MVP_QUEUE_ACCEPTANCE_USERNAME": username,
            "MVP_QUEUE_ACCEPTANCE_PASSWORD": password,
            "MVP_QUEUE_ACCEPTANCE_VIDEO_ID": video_id,
        }
    )
    result = subprocess.run(
        ["npm.cmd", "run", "e2e", "--", "--grep", "durable execution-slot queue state"],
        cwd=ROOT / "frontend",
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    log_path = output_dir / f"playwright-{browser_output.name}.log"
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    runner.record("browser_queue_check", exit_code=result.returncode, output=str(log_path))
    if result.returncode != 0:
        raise AcceptanceFailure(
            "the real browser did not verify queued state and unavailable recovery action"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the isolated Stage 4 Beat-to-Worker queue path.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    arguments = parser.parse_args()
    environment_file = arguments.environment_file.resolve()
    output_dir = arguments.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    runner = Runner(arguments.project, environment_file, output_dir)
    environment = read_environment(environment_file)
    api_url = f"http://127.0.0.1:{environment['MVP_STAGE1_API_PORT']}"
    origin = environment["APP_ORIGIN"]
    before = runner.database_snapshot()
    if any(int(before[key]) != 0 for key in ("users", "videos", "tasks", "attempts", "executions")):
        raise AcceptanceFailure("Stage 4 acceptance requires its fresh, empty isolated database")
    if runner.analysis_queue_length() != 0:
        raise AcceptanceFailure("Stage 4 acceptance analysis queue must be empty before fixture creation")

    api_containers = runner.compose_call("ps", "-q", "api").stdout.splitlines()
    workers = runner.compose_call("ps", "-q", "worker").stdout.splitlines()
    maintenance_workers = runner.compose_call("ps", "-q", "maintenance-worker").stdout.splitlines()
    beats = runner.compose_call("ps", "-q", "beat").stdout.splitlines()
    if not all(len(items) == 1 for items in (api_containers, workers, maintenance_workers, beats)):
        raise AcceptanceFailure(
            "acceptance requires exactly one API, analysis Worker, maintenance Worker, and Beat"
        )

    data_directory = Path(environment["MVP_STAGE1_DATA_DIR"])
    fixture_path = data_directory / "app" / "stage4-acceptance-fixture.mp4"
    if fixture_path.exists():
        raise AcceptanceFailure("refusing to overwrite an existing Stage 4 fixture file")
    runner.compose_call(
        "exec",
        "-T",
        "api",
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "8",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        "-y",
        "/appdata/stage4-acceptance-fixture.mp4",
        timeout=60,
    )
    if not fixture_path.is_file():
        raise AcceptanceFailure("the controlled media fixture was not created in isolated Stage 4 data")
    fixture_sha256 = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    username = "stage4-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    task_ids: list[str] = []
    video_ids: list[str] = []
    worker_stopped = False
    max_observed_unconfirmed = 0
    runner.record("preflight", database=before, analysis_queue=0, project=arguments.project)

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
                "isolated fixture registration",
            )
            require(
                client.post(
                    "/auth/login",
                    json={"username": username, "password": password},
                    headers=headers,
                ),
                200,
                "isolated fixture login",
            )
            for index in range(3):
                upload_headers = csrf_headers(client, origin)
                uploaded = require(
                    client.post(
                        "/videos",
                        content=fixture_path.read_bytes(),
                        headers={
                            **upload_headers,
                            "Content-Type": "application/octet-stream",
                            "X-Video-Filename": urllib.parse.quote(f"stage4-queue-{index}.mp4", safe=""),
                        },
                    ),
                    201,
                    "isolated fixture upload",
                ).json()["video"]
                video_id = str(uuid.UUID(uploaded["id"]))
                video_ids.append(video_id)

            headers = csrf_headers(client, origin)
            first = require(
                client.post(f"/videos/{video_ids[0]}/tasks", headers=headers), 202, "task A submission"
            ).json()
            task_a = str(uuid.UUID(first["id"]))
            task_ids.append(task_a)
            wait_for(
                lambda: bool(runner.stub_control("__control/state").get("first_asr_entered")),
                timeout_seconds=60,
                message="analysis Worker did not reach the controlled first ASR request",
                interval_seconds=1,
            )
            task_a_status = api_task(client, video_ids[0], task_a, origin)
            if (
                task_a_status.get("status") != "running"
                or task_a_status.get("execution_state") != "stop_unconfirmed"
            ):
                raise AcceptanceFailure("task A did not hold a real Stage 2 Linux execution slot")
            if int(runner.database_snapshot()["unconfirmed"]) != 1:
                raise AcceptanceFailure("task A did not create exactly one unconfirmed execution")

            queued: list[dict[str, Any]] = []
            for index in (1, 2):
                headers = csrf_headers(client, origin)
                submitted = require(
                    client.post(f"/videos/{video_ids[index]}/tasks", headers=headers),
                    202,
                    f"task {index} queue submission",
                ).json()
                task_id = str(uuid.UUID(submitted["id"]))
                task_ids.append(task_id)
                status = api_task(client, video_ids[index], task_id, origin)
                if (
                    status.get("status") != "queued"
                    or status.get("delivery_wait_reason") != "execution_slot_occupied"
                    or status.get("delivery_retry_count") != 0
                    or status.get("delivery_operation_generation") != 0
                    or status.get("delivery_next_check_at") is None
                ):
                    raise AcceptanceFailure(
                        "queued task did not retain its execution-slot coordination state"
                    )
                queued.append(status)

            duplicate = require(
                client.post(f"/videos/{video_ids[1]}/tasks", headers=csrf_headers(client, origin)),
                202,
                "duplicate queued task submission",
            ).json()
            if duplicate.get("id") != task_ids[1]:
                raise AcceptanceFailure("duplicate video submission created or returned a different task")
            rows_while_a_running = task_rows(runner, task_ids)
            if len(rows_while_a_running) != 3 or any(
                row["claim_count"] != 0 for row in rows_while_a_running[1:]
            ):
                raise AcceptanceFailure("queued B/C attempts were claimed while task A held the slot")
            runner.record(
                "tasks_queued_behind_a",
                task_ids=task_ids,
                delivery_wait_reasons=[item.get("delivery_wait_reason") for item in queued],
                rows=rows_while_a_running,
            )
            injected_attempt_id = rows_while_a_running[2]["attempt_id"]
            runner.compose_call(
                "exec",
                "-T",
                "maintenance-worker",
                "python",
                "-c",
                "from videoclip.celery_app import celery_app; "
                "celery_app.send_task('videoclip.transcribe_analysis', "
                f"args=[{task_ids[2]!r}, {injected_attempt_id!r}], retry=False)",
            )
            runner.record(
                "out_of_order_message_injected",
                task_id=task_ids[2],
                attempt_id=injected_attempt_id,
            )
            run_browser_queue_check(
                runner,
                api_url=api_url,
                username=username,
                password=password,
                video_id=video_ids[1],
                output_dir=output_dir,
            )
            max_observed_unconfirmed = 1

            runner.stub_control("__control/release")
            deadline = time.monotonic() + arguments.timeout_seconds
            last_tasks: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                last_tasks = [
                    api_task(client, video_ids[index], task_ids[index], origin) for index in range(3)
                ]
                snapshot = runner.database_snapshot()
                max_observed_unconfirmed = max(max_observed_unconfirmed, int(snapshot["unconfirmed"]))
                if last_tasks[0].get("status") == "succeeded" and int(snapshot["unconfirmed"]) == 0:
                    break
                time.sleep(1)
            else:
                raise AcceptanceFailure("task A did not finish with a confirmed Stage 2 execution")
            if last_tasks[0].get("execution_stop_confirmed") is not True:
                raise AcceptanceFailure("task A result was not backed by trusted stop confirmation")

            worker_log = runner.compose_call("logs", "worker", timeout=15)
            out_of_order_acknowledged = (
                "ignored_not_eligible" in worker_log.stdout + worker_log.stderr
            )
            if not out_of_order_acknowledged:
                raise AcceptanceFailure(
                    "analysis Worker did not consume and ACK the injected later-task message"
                )
            runner.record(
                "out_of_order_message_acknowledged",
                task_id=task_ids[2],
                still_queued=True,
                claim_count=0,
            )

            if int(runner.database_snapshot()["unconfirmed"]) != 0:
                raise AcceptanceFailure(
                    "refusing to stop the isolated analysis Worker with an unconfirmed execution"
                )
            runner.compose_call("stop", "worker", timeout=180)
            worker_stopped = True
            runner.record("analysis_worker_stopped_idle", task_a=task_a)

            def deliveries_waiting_unclaimed() -> bool:
                rows = task_rows(runner, task_ids[1:])
                if len(rows) != 2:
                    return False
                first, later = rows
                return (
                    first["delivery_status"] == "sent"
                    and first["generation"] == 1
                    and first["claim_count"] == 0
                    and later["generation"] == 0
                    and later["claim_count"] == 0
                    and later["delivery_wait_reason"] == "queue_order_wait"
                    and runner.analysis_queue_length() == 1
                )

            wait_for(
                deliveries_waiting_unclaimed,
                timeout_seconds=180,
                message=(
                    "Beat and maintenance did not publish the oldest queued task "
                    "while holding later tasks behind its durable queue position"
                ),
                interval_seconds=1,
            )
            runner.record(
                "sent_but_unclaimed",
                rows=task_rows(runner, task_ids[1:]),
                analysis_queue=runner.analysis_queue_length(),
            )
            runner.compose_call("start", "worker", timeout=45)
            worker_stopped = False
            runner.record("analysis_worker_restarted_for_delivery", task_ids=task_ids[1:])

            deadline = time.monotonic() + arguments.timeout_seconds
            expected_claim_order = [row["task_id"] for row in task_rows(runner, task_ids[1:])]
            last_tasks = []
            while time.monotonic() < deadline:
                last_tasks = [
                    api_task(client, video_ids[index], task_ids[index], origin) for index in range(3)
                ]
                snapshot = runner.database_snapshot()
                max_observed_unconfirmed = max(max_observed_unconfirmed, int(snapshot["unconfirmed"]))
                if all(last_tasks[index].get("status") == "succeeded" for index in range(3)):
                    break
                time.sleep(1)
            else:
                raise AcceptanceFailure(
                    "queued tasks B/C did not both execute after the blocking execution stopped"
                )

        final_snapshot = runner.database_snapshot()
        final_rows = task_rows(runner, task_ids)
        traffic = runner.stub_control("__control/state")
        if max_observed_unconfirmed > 1 or int(final_snapshot["unconfirmed"]) != 0:
            raise AcceptanceFailure("the real Stage 2 execution slot exceeded its single-execution bound")
        if any(row["claim_count"] != 1 or row["attempt_status"] != "succeeded" for row in final_rows):
            raise AcceptanceFailure("the three tasks did not each finish through one current attempt claim")
        if any(row["delivery_retry_count"] != 0 or row["generation"] != 1 for row in final_rows):
            raise AcceptanceFailure(
                "normal queue progression consumed delivery retries or duplicated a generation"
            )
        if int(traffic["asr_requests"]) != 3 or int(traffic["llm_requests"]) != 3:
            raise AcceptanceFailure("analysis did not use only the controlled local model substitute")
        claim_order = [
            row["task_id"]
            for row in sorted(
                final_rows[1:], key=lambda row: datetime.fromisoformat(row["first_claimed_at"])
            )
        ]
        if claim_order != expected_claim_order:
            raise AcceptanceFailure("B/C claims did not follow persisted queue-entry order")
        result = {
            "result": "passed",
            "project": arguments.project,
            "fixture_sha256": fixture_sha256,
            "task_ids": task_ids,
            "video_ids": video_ids,
            "queue_order_by_postgres": expected_claim_order,
            "claim_order": claim_order,
            "max_observed_unconfirmed": max_observed_unconfirmed,
            "final_database": final_snapshot,
            "final_tasks": final_rows,
            "local_stub_requests": {"asr": traffic["asr_requests"], "llm": traffic["llm_requests"]},
            "history_rows_modified": 0,
            "captured_at": datetime.now(UTC).isoformat(),
        }
        (output_dir / "acceptance.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        runner.record("acceptance_passed", result=result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        runner.record("acceptance_failed", error_class=type(exc).__name__, message=str(exc))
        raise
    finally:
        try:
            runner.stub_control("__control/release")
        except Exception:
            pass
        if worker_stopped:
            try:
                if int(runner.database_snapshot()["unconfirmed"]) == 0:
                    runner.compose_call("start", "worker", timeout=45)
            except Exception:
                runner.record("worker_restart_after_failure_needs_attention")


if __name__ == "__main__":
    raise SystemExit(main())
