from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
COMPOSE_OVERRIDE = ROOT / "scripts" / "stage2" / "final-stage5-acceptance.override.yaml"
PROJECT_PREFIX = "videoclip-mvp-stage5-"


class AcceptanceFailure(RuntimeError):
    pass


class Runner:
    def __init__(self, project: str, environment_file: Path, output_dir: Path) -> None:
        if not project.startswith(PROJECT_PREFIX):
            raise AcceptanceFailure("Stage 5 Compose project name is outside its dedicated prefix")
        self.project = project
        self.environment_file = environment_file.resolve()
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.output_dir / "events.jsonl"
        self.compose = [
            "docker",
            "compose",
            "--env-file",
            str(self.environment_file),
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
            raise AcceptanceFailure(f"isolated Stage 5 command failed: {argv[0]}")
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
            raise AcceptanceFailure("isolated Stage 5 database identifiers are invalid")
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

    def database_snapshot(self, task_ids: list[str] | None = None) -> dict[str, Any]:
        if task_ids:
            parsed = [str(uuid.UUID(value)) for value in task_ids]
            id_list = ",".join(f"'{value}'::uuid" for value in parsed)
            task_filter = f"WHERE t.id IN ({id_list})"
        else:
            task_filter = ""
        raw = self.psql(
            "SELECT json_build_object("
            "'users',(SELECT count(*) FROM users),"
            "'videos',(SELECT count(*) FROM videos),"
            "'tasks',(SELECT count(*) FROM analysis_tasks),"
            "'attempts',(SELECT count(*) FROM analysis_attempts),"
            "'executions',(SELECT count(*) FROM analysis_executions),"
            "'unconfirmed',(SELECT count(*) FROM analysis_executions WHERE stop_confirmed_at IS NULL),"
            "'summary_rows',(SELECT count(*) FROM analysis_summaries),"
            "'cooldowns',(SELECT count(*) FROM shared_model_cooldowns),"
            "'task_rows',(SELECT COALESCE(json_agg(json_build_object("
            "'task_id',t.id::text,'status',t.status,'stage',t.stage,'failure_code',t.failure_code,"
            "'current_attempt_id',t.current_attempt_id::text,'auto_recovery_count',t.auto_recovery_count,"
            "'auto_recovery_limit',t.auto_recovery_limit,'summary_status',t.summary_status,"
            "'summary_requests',(SELECT count(*) FROM summary_requests sr WHERE sr.task_id=t.id),"
            "'summary_publications',(SELECT count(*) FROM analysis_summaries s WHERE s.task_id=t.id),"
            "'model_wait_origin',t.model_wait_origin,'model_wait_stage',t.model_wait_stage,"
            "'model_wait_reason',t.model_wait_reason,'model_wait_total_seconds',t.model_wait_total_seconds,"
            "'delivery_retry_count',t.delivery_retry_count,'delivery_operation_count',t.delivery_operation_count,"
            "'delivery_deadline_at',t.delivery_deadline_at,'delivery_blocked_since',t.delivery_blocked_since,"
            "'queue_entered_at',t.queue_entered_at,'delivery_wait_reason',t.delivery_wait_reason,"
            "'attempts',(SELECT COALESCE(json_agg("
            "json_build_object("
            "'id',a.id::text,'attempt_number',a.attempt_number,'parent_attempt_id',a.parent_attempt_id::text,"
            "'status',a.status,'execution_mode',a.execution_mode,'claim_count',a.claim_count,"
            "'failure_code',a.failure_code,'stop_confirmed_at',a.stop_confirmed_at,"
            "'execution_stop_source',e.stop_confirmation_source,'request_count',"
            "(SELECT COALESCE(sum(ts.request_count),0) FROM transcript_segments ts WHERE ts.attempt_id=a.id),"
            "'completed_segments',(SELECT count(*) FROM transcript_segments ts "
            "WHERE ts.attempt_id=a.id AND ts.status='succeeded')"
            ") ORDER BY a.attempt_number),'[]'::json) FROM analysis_attempts a "
            "LEFT JOIN analysis_executions e ON e.attempt_id=a.id WHERE a.task_id=t.id)"
            ") ORDER BY t.id),'[]'::json) FROM analysis_tasks t "
            f"{task_filter})"
            ")::text"
        )
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise AcceptanceFailure("isolated Stage 5 database snapshot was invalid")
        return value

    def stub_control(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        document = json.dumps(payload or {}, separators=(",", ":"))
        code = (
            "import json,ssl,urllib.request; "
            f"body={document!r}.encode(); request=urllib.request.Request("
            f"'https://127.0.0.1:443/{path}', data=body if {payload is not None!r} else None, "
            f"method={'POST' if payload is not None or path == '__control/release' else 'GET'!r}, "
            "headers={'Content-Type':'application/json'}); "
            "r=urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=5); "
            "print(r.read().decode())"
        )
        result = self.compose_call("exec", "-T", "model-stub", "python", "-c", code, timeout=10)
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise AcceptanceFailure("local Stage 5 substitute returned invalid control state")
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


def task_status(client: httpx.Client, video_id: str, task_id: str) -> dict[str, Any]:
    value = require(client.get(f"/videos/{video_id}/tasks/{task_id}"), 200, "task status").json()
    if not isinstance(value, dict):
        raise AcceptanceFailure("task status response was not an object")
    return value


def stopped_failure_status(
    client: httpx.Client, video_id: str, task_id: str, failure_codes: set[str]
) -> dict[str, Any] | None:
    value = task_status(client, video_id, task_id)
    if (
        value.get("status") == "failed"
        and value.get("execution_stop_confirmed") is True
        and value.get("failure_code") in failure_codes
    ):
        return value
    return None


def play_waiting_task_in_browser(
    runner: Runner,
    *,
    environment: dict[str, str],
    username: str,
    password: str,
    video_id: str,
) -> None:
    browser_output = runner.output_dir / f"playwright-{uuid.uuid4().hex}"
    browser_output.mkdir(parents=True, exist_ok=False)
    process_environment = os.environ.copy()
    process_environment.update(
        {
            "MVP_WEB_MODE": "dev",
            "VIDEOCLIP_WEB_PORT": environment["MVP_STAGE1_WEB_PORT"],
            "VIDEOCLIP_API_TARGET": f"http://127.0.0.1:{environment['MVP_STAGE1_API_PORT']}",
            "MVP_PLAYWRIGHT_OUTPUT_DIR": str(browser_output),
            "MVP_STAGE5_EXPECTED_ORIGIN": environment["APP_ORIGIN"],
            "MVP_STAGE5_USERNAME": username,
            "MVP_STAGE5_PASSWORD": password,
            "MVP_STAGE5_WAITING_VIDEO_ID": video_id,
        }
    )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    result = subprocess.run(
        [npm, "run", "e2e", "--", "--grep", "Stage 5 displays a persisted model wait"],
        cwd=ROOT / "frontend",
        env=process_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    log_path = runner.output_dir / f"{browser_output.name}.log"
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    runner.record("browser_model_wait", exit_code=result.returncode, output=str(log_path))
    if result.returncode != 0:
        raise AcceptanceFailure("the real browser did not verify persisted Stage 5 model-wait state")


def play_recovery_states_in_browser(
    runner: Runner,
    *,
    environment: dict[str, str],
    username: str,
    password: str,
    recovered_video_id: str,
    permanent_failure_video_id: str,
) -> None:
    browser_output = runner.output_dir / f"playwright-{uuid.uuid4().hex}"
    browser_output.mkdir(parents=True, exist_ok=False)
    process_environment = os.environ.copy()
    process_environment.update(
        {
            "MVP_WEB_MODE": "dev",
            "VIDEOCLIP_WEB_PORT": environment["MVP_STAGE1_WEB_PORT"],
            "VIDEOCLIP_API_TARGET": f"http://127.0.0.1:{environment['MVP_STAGE1_API_PORT']}",
            "MVP_PLAYWRIGHT_OUTPUT_DIR": str(browser_output),
            "MVP_STAGE5_EXPECTED_ORIGIN": environment["APP_ORIGIN"],
            "MVP_STAGE5_USERNAME": username,
            "MVP_STAGE5_PASSWORD": password,
            "MVP_STAGE5_RECOVERED_VIDEO_ID": recovered_video_id,
            "MVP_STAGE5_PERMANENT_VIDEO_ID": permanent_failure_video_id,
        }
    )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    result = subprocess.run(
        [npm, "run", "e2e", "--", "--grep", "Stage 5 displays automatic recovery budgets"],
        cwd=ROOT / "frontend",
        env=process_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    log_path = runner.output_dir / f"{browser_output.name}.log"
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    runner.record("browser_recovery_state", exit_code=result.returncode, output=str(log_path))
    if result.returncode != 0:
        raise AcceptanceFailure("the real browser did not verify Stage 5 recovery eligibility and budgets")


def play_stop_confirmation_in_browser(
    runner: Runner,
    *,
    environment: dict[str, str],
    username: str,
    password: str,
    video_id: str,
) -> None:
    browser_output = runner.output_dir / f"playwright-{uuid.uuid4().hex}"
    browser_output.mkdir(parents=True, exist_ok=False)
    process_environment = os.environ.copy()
    process_environment.update(
        {
            "MVP_WEB_MODE": "dev",
            "VIDEOCLIP_WEB_PORT": environment["MVP_STAGE1_WEB_PORT"],
            "VIDEOCLIP_API_TARGET": f"http://127.0.0.1:{environment['MVP_STAGE1_API_PORT']}",
            "MVP_PLAYWRIGHT_OUTPUT_DIR": str(browser_output),
            "MVP_STAGE5_EXPECTED_ORIGIN": environment["APP_ORIGIN"],
            "MVP_STAGE5_USERNAME": username,
            "MVP_STAGE5_PASSWORD": password,
            "MVP_STAGE5_STOP_CONFIRMATION_VIDEO_ID": video_id,
        }
    )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    result = subprocess.run(
        [npm, "run", "e2e", "--", "--grep", "Stage 5 explains the unconfirmed stop gate"],
        cwd=ROOT / "frontend",
        env=process_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    log_path = runner.output_dir / f"{browser_output.name}.log"
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    runner.record("browser_stop_confirmation", exit_code=result.returncode, output=str(log_path))
    if result.returncode != 0:
        raise AcceptanceFailure("the real browser did not display the unconfirmed stop reason")


def play_exhausted_budgets_in_browser(
    runner: Runner,
    *,
    environment: dict[str, str],
    username: str,
    password: str,
    video_id: str,
) -> None:
    browser_output = runner.output_dir / f"playwright-{uuid.uuid4().hex}"
    browser_output.mkdir(parents=True, exist_ok=False)
    process_environment = os.environ.copy()
    process_environment.update(
        {
            "MVP_WEB_MODE": "dev",
            "VIDEOCLIP_WEB_PORT": environment["MVP_STAGE1_WEB_PORT"],
            "VIDEOCLIP_API_TARGET": f"http://127.0.0.1:{environment['MVP_STAGE1_API_PORT']}",
            "MVP_PLAYWRIGHT_OUTPUT_DIR": str(browser_output),
            "MVP_STAGE5_EXPECTED_ORIGIN": environment["APP_ORIGIN"],
            "MVP_STAGE5_USERNAME": username,
            "MVP_STAGE5_PASSWORD": password,
            "MVP_STAGE5_EXHAUSTED_VIDEO_ID": video_id,
        }
    )
    npm = "npm.cmd" if os.name == "nt" else "npm"
    result = subprocess.run(
        [npm, "run", "e2e", "--", "--grep", "Stage 5 displays both exhausted budgets"],
        cwd=ROOT / "frontend",
        env=process_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    log_path = runner.output_dir / f"{browser_output.name}.log"
    log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    runner.record("browser_budget_exhaustion", exit_code=result.returncode, output=str(log_path))
    if result.returncode != 0:
        raise AcceptanceFailure("the real browser did not display exhausted Stage 5 budgets")


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the isolated Stage 5 wait and recovery chain.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=420)
    parser.add_argument("--worker-instance-id", type=uuid.UUID, required=True)
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
        raise AcceptanceFailure("Stage 5 chain acceptance requires its empty isolated application database")
    runner.stub_control(
        "__control/plan",
        {"asr": [], "llm": [{"status": 429, "retry_after": "120"}, 200, 200]},
    )
    runner.record("preflight", project=arguments.project, database=before, real_supplier_used=False)

    api_containers = runner.compose_call("ps", "-q", "api").stdout.splitlines()
    workers = runner.compose_call("ps", "-q", "worker").stdout.splitlines()
    maintenance_workers = runner.compose_call("ps", "-q", "maintenance-worker").stdout.splitlines()
    beats = runner.compose_call("ps", "-q", "beat").stdout.splitlines()
    if not all(len(items) == 1 for items in (api_containers, workers, maintenance_workers, beats)):
        raise AcceptanceFailure(
            "acceptance requires exactly one API, analysis Worker, maintenance Worker, and Beat"
        )

    data_directory = Path(environment["MVP_STAGE1_DATA_DIR"])
    fixture_path = data_directory / "app" / "stage5-acceptance-fixture.mp4"
    if fixture_path.exists():
        raise AcceptanceFailure("refusing to overwrite an existing Stage 5 fixture")
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
        "/appdata/stage5-acceptance-fixture.mp4",
        timeout=60,
    )
    if not fixture_path.is_file():
        raise AcceptanceFailure("the isolated Stage 5 media fixture was not created")
    fixture_sha256 = hashlib.sha256(fixture_path.read_bytes()).hexdigest()

    username = "stage5-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    task_ids: list[str] = []
    video_ids: list[str] = []
    maximum_unconfirmed = 0
    browser_checked = False
    browser_recovery_checked = False
    browser_stop_confirmation_checked = False
    browser_budget_exhaustion_checked = False
    worker_fault_checked = False
    maintenance_paused = False

    try:
        timeout = httpx.Timeout(60.0, connect=10.0)
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
                    headers=csrf_headers(client, origin),
                ),
                200,
                "isolated fixture login",
            )
            for index in range(7):
                uploaded = require(
                    client.post(
                        "/videos",
                        content=fixture_path.read_bytes(),
                        headers={
                            **csrf_headers(client, origin),
                            "Content-Type": "application/octet-stream",
                            "X-Video-Filename": urllib.parse.quote(f"stage5-{index}.mp4", safe=""),
                        },
                    ),
                    201,
                    "isolated Stage 5 fixture upload",
                ).json()["video"]
                video_ids.append(str(uuid.UUID(uploaded["id"])))

            runner.compose_call("pause", "maintenance-worker")
            maintenance_paused = True
            runner.record("maintenance_paused_for_deterministic_429_observation")
            first = require(
                client.post(
                    f"/videos/{video_ids[0]}/tasks",
                    headers=csrf_headers(client, origin),
                ),
                202,
                "task A submission",
            ).json()
            task_a = str(uuid.UUID(first["id"]))
            task_ids.append(task_a)
            wait_for(
                lambda: (
                    (state := runner.stub_control("__control/state")).get("llm_requests") == 1
                    and state.get("asr_requests") == 1
                ),
                timeout_seconds=60,
                message="task A did not reach its first controlled LLM request",
                interval_seconds=1,
            )
            wait_for(
                lambda: (
                    (status := task_status(client, video_ids[0], task_a)).get("failure_code")
                    == "LLM_RATE_LIMITED"
                    and status.get("execution_stop_confirmed") is True
                ),
                timeout_seconds=30,
                message="task A's 429 did not become a stopped, persisted failure",
            )

            second = require(
                client.post(
                    f"/videos/{video_ids[1]}/tasks",
                    headers=csrf_headers(client, origin),
                ),
                202,
                "task B submission during shared cooldown",
            ).json()
            task_b = str(uuid.UUID(second["id"]))
            task_ids.append(task_b)
            wait_for(
                lambda: (
                    (status := task_status(client, video_ids[1], task_b)).get("status") == "waiting_model"
                    and status.get("model_wait_stage") == "summarizing"
                    and status.get("execution_stop_confirmed") is True
                ),
                timeout_seconds=180,
                message="task B did not finish ASR and enter a persisted LLM cooldown wait",
            )
            cooling_state = runner.stub_control("__control/state")
            waiting_snapshot = runner.database_snapshot(task_ids)
            maximum_unconfirmed = max(maximum_unconfirmed, int(waiting_snapshot["unconfirmed"]))
            rows_by_id = {row["task_id"]: row for row in waiting_snapshot["task_rows"]}
            waiting_deadlines = {task_id: rows_by_id[task_id]["delivery_deadline_at"] for task_id in task_ids}
            if cooling_state["llm_requests"] != 1 or cooling_state["asr_requests"] != 2:
                raise AcceptanceFailure("the task behind the LLM cooldown did not stop after ASR")
            if rows_by_id[task_a]["auto_recovery_count"] != 0:
                raise AcceptanceFailure("normal 429 cooldown waiting consumed an automatic recovery")
            if rows_by_id[task_b]["summary_requests"] != 0:
                raise AcceptanceFailure("preflight cooldown consumed a summary request reservation")
            if rows_by_id[task_b]["model_wait_stage"] != "summarizing":
                raise AcceptanceFailure("task B persisted an incorrect resume stage")
            waiting_task = rows_by_id[task_b]
            if (
                waiting_task["delivery_deadline_at"] is None
                or waiting_task["delivery_blocked_since"] is None
                or waiting_task["queue_entered_at"] is not None
            ):
                raise AcceptanceFailure(
                    "preflight model wait did not pause the Stage 4 active delivery deadline"
                )
            b_first_attempt = rows_by_id[task_b]["attempts"][0]
            if b_first_attempt["completed_segments"] != 1 or b_first_attempt["request_count"] != 1:
                raise AcceptanceFailure("task B did not persist its completed ASR checkpoint before waiting")
            if int(waiting_snapshot["cooldowns"]) != 1 or int(waiting_snapshot["unconfirmed"]) != 0:
                raise AcceptanceFailure("shared cooldown or trusted stop state was not persisted safely")
            runner.record(
                "two_tasks_share_llm_cooldown",
                task_ids=task_ids,
                stub_requests=cooling_state,
                database=waiting_snapshot,
            )

            play_waiting_task_in_browser(
                runner,
                environment=environment,
                username=username,
                password=password,
                video_id=video_ids[1],
            )
            browser_checked = True
            runner.compose_call("unpause", "maintenance-worker")
            maintenance_paused = False
            runner.record("maintenance_resumed_after_cooldown_observation")

            deadline = time.monotonic() + arguments.timeout_seconds
            last_snapshot: dict[str, Any] = {}
            while time.monotonic() < deadline:
                statuses = [task_status(client, video_ids[index], task_ids[index]) for index in range(2)]
                last_snapshot = runner.database_snapshot(task_ids)
                maximum_unconfirmed = max(maximum_unconfirmed, int(last_snapshot["unconfirmed"]))
                if all(status.get("status") == "succeeded" for status in statuses):
                    break
                time.sleep(1)
            else:
                raise AcceptanceFailure("Beat and maintenance Worker did not resume both model-wait tasks")

            rows_by_id = {row["task_id"]: row for row in last_snapshot["task_rows"]}
            row_a, row_b = rows_by_id[task_a], rows_by_id[task_b]
            if row_a["auto_recovery_count"] != 1 or row_b["auto_recovery_count"] != 0:
                raise AcceptanceFailure(
                    "automatic recovery counts do not distinguish failed and normal wait resume"
                )
            if row_a["summary_requests"] != 2 or row_b["summary_requests"] != 1:
                raise AcceptanceFailure("summary requests were not counted across attempts")
            if any(
                len(row["attempts"]) != 2 or row["attempts"][-1]["execution_mode"] != "summary_only"
                for row in (row_a, row_b)
            ):
                raise AcceptanceFailure("complete transcripts were not resumed through summary-only attempts")
            if any(
                row["delivery_retry_count"] != 1 or row["delivery_operation_count"] != 2
                for row in (row_a, row_b)
            ):
                raise AcceptanceFailure(
                    "Stage 4 delivery budgets were not preserved across accepted successor delivery"
                )
            if any(
                row["delivery_deadline_at"] is None or row["queue_entered_at"] is None
                for row in (row_a, row_b)
            ):
                raise AcceptanceFailure(
                    "resumed tasks did not retain their delivery deadline and re-enter queue order"
                )
            if any(
                datetime.fromisoformat(after["delivery_deadline_at"])
                <= datetime.fromisoformat(waiting_deadlines[task_id])
                for task_id, after in ((task_a, row_a), (task_b, row_b))
            ):
                raise AcceptanceFailure("the active delivery deadline was not extended by model wait")
            traffic = runner.stub_control("__control/state")
            if traffic["asr_requests"] != 2 or traffic["llm_requests"] != 3:
                raise AcceptanceFailure("cooldown recovery repeated ASR or made unexpected model calls")
            if maximum_unconfirmed > 1 or int(last_snapshot["unconfirmed"]) != 0:
                raise AcceptanceFailure("shared cooldown chain exceeded the single-execution proof bound")
            runner.record(
                "shared_cooldown_tasks_resumed",
                database=last_snapshot,
                stub_requests=traffic,
                maximum_unconfirmed=maximum_unconfirmed,
            )

            # A controlled Celery child failure while the supervised local ASR request is live.
            runner.stub_control(
                "__control/plan",
                {"asr": [{"status": 200, "hold": True}], "llm": []},
            )
            third = require(
                client.post(
                    f"/videos/{video_ids[2]}/tasks",
                    headers=csrf_headers(client, origin),
                ),
                202,
                "controlled worker-fault task submission",
            ).json()
            task_c = str(uuid.UUID(third["id"]))
            task_ids.append(task_c)
            wait_for(
                lambda: runner.stub_control("__control/state").get("blocked_asr", 0) >= 1,
                timeout_seconds=60,
                message="task C did not reach the held local ASR substitute request",
            )
            active = runner.database_snapshot([task_c])
            if int(active["unconfirmed"]) != 1:
                raise AcceptanceFailure(
                    "task C did not hold the single execution slot before fault injection"
                )
            active_status = task_status(client, video_ids[2], task_c)
            if active_status.get("recovery_error_code") != "STOP_CONFIRMATION_REQUIRED":
                raise AcceptanceFailure("an unconfirmed live execution did not block recovery")
            play_stop_confirmation_in_browser(
                runner,
                environment=environment,
                username=username,
                password=password,
                video_id=video_ids[2],
            )
            browser_stop_confirmation_checked = True
            worker_id = runner.compose_call("ps", "-q", "worker").stdout.strip()
            if not worker_id:
                raise AcceptanceFailure("isolated analysis Worker container was not found")
            worker_log = runner.compose_call("logs", "worker", timeout=15).stdout
            celery_pids = re.findall(r"analysis_supervisor_started .*celery_pid=(\d+)", worker_log)
            if not celery_pids:
                raise AcceptanceFailure("supervisor log did not expose its controlled Celery child PID")
            celery_pid = int(celery_pids[-1])
            runner.compose_call(
                "exec",
                "-T",
                "worker",
                "python",
                "-c",
                f"import os,signal; os.kill({celery_pid}, signal.SIGKILL)",
                timeout=10,
            )
            stopped_deadline = time.monotonic() + 30
            stopped_status: dict[str, Any] = {}
            while time.monotonic() < stopped_deadline:
                stopped_status = task_status(client, video_ids[2], task_c)
                worker_state = runner.command(
                    ["docker", "inspect", "--format", "{{.State.Status}}", worker_id], timeout=10
                ).stdout.strip()
                if stopped_status.get("execution_stop_confirmed") is True and worker_state == "exited":
                    break
                time.sleep(0.5)
            else:
                raise AcceptanceFailure("supervisor did not confirm the stopped controlled execution")
            failed_snapshot = runner.database_snapshot([task_c])
            parent = failed_snapshot["task_rows"][0]["attempts"][0]
            if parent["failure_code"] != "WORKER_EXECUTION_INTERRUPTED":
                raise AcceptanceFailure(
                    "controlled worker fault was not classified as a recoverable interruption"
                )
            if parent["execution_stop_source"] != "worker_supervisor":
                raise AcceptanceFailure("controlled worker fault lacked trusted supervisor stop evidence")

            new_worker_instance = uuid.uuid4()
            os.environ["ANALYSIS_WORKER_INSTANCE_ID"] = str(new_worker_instance)
            runner.compose_call("up", "-d", "--no-deps", "worker", timeout=60)
            worker_fault_checked = True
            runner.record(
                "controlled_worker_fault_confirmed",
                task_id=task_c,
                parent_attempt_id=parent["id"],
                celery_pid=celery_pid,
                stop_source=parent["execution_stop_source"],
                next_worker_instance_id=str(new_worker_instance),
            )

            deadline = time.monotonic() + arguments.timeout_seconds
            final_status: dict[str, Any] = {}
            final_snapshot: dict[str, Any] = {}
            while time.monotonic() < deadline:
                final_status = task_status(client, video_ids[2], task_c)
                final_snapshot = runner.database_snapshot([task_c])
                maximum_unconfirmed = max(maximum_unconfirmed, int(final_snapshot["unconfirmed"]))
                if final_status.get("status") == "succeeded":
                    break
                time.sleep(1)
            else:
                raise AcceptanceFailure("the confirmed controlled process fault did not auto-recover")
            row_c = final_snapshot["task_rows"][0]
            if row_c["auto_recovery_count"] != 1 or len(row_c["attempts"]) != 2:
                raise AcceptanceFailure(
                    "controlled process fault did not create exactly one bounded recovery"
                )
            if int(final_snapshot["unconfirmed"]) != 0 or maximum_unconfirmed > 1:
                raise AcceptanceFailure(
                    "controlled process recovery started before trusted stop confirmation"
                )

            fault_cases = (
                (
                    "llm_5xx",
                    3,
                    {"status": 503},
                    {"LLM_MODEL_OVERLOADED"},
                ),
                (
                    "llm_timeout",
                    4,
                    {"status": 200, "delay_seconds": 5},
                    {"LLM_PHASE_TIMEOUT", "LLM_TOTAL_TIMEOUT"},
                ),
            )
            for case_name, video_index, first_response, expected_errors in fault_cases:
                runner.stub_control(
                    "__control/plan",
                    {"asr": [], "llm": [first_response, {"status": 200}]},
                )
                created = require(
                    client.post(
                        f"/videos/{video_ids[video_index]}/tasks",
                        headers=csrf_headers(client, origin),
                    ),
                    202,
                    f"{case_name} task submission",
                ).json()
                case_task_id = str(uuid.UUID(created["id"]))
                task_ids.append(case_task_id)
                failed_status = wait_for(
                    lambda current_video=video_ids[video_index],
                    current_task=case_task_id,
                    current_errors=expected_errors: stopped_failure_status(
                        client, current_video, current_task, current_errors
                    ),
                    timeout_seconds=180,
                    message=f"{case_name} did not persist its classified, stopped failure",
                    interval_seconds=1,
                )
                failed = runner.database_snapshot([case_task_id])
                failed_row = failed["task_rows"][0]
                if failed_row["summary_requests"] != 1 or len(failed_row["attempts"]) != 1:
                    raise AcceptanceFailure(f"{case_name} did not reserve exactly one first summary request")
                runner.record(
                    "recoverable_model_failure_classified",
                    case=case_name,
                    task_id=case_task_id,
                    failure_code=failed_status["failure_code"],
                    database=failed,
                )

                deadline = time.monotonic() + arguments.timeout_seconds
                resumed_snapshot: dict[str, Any] = {}
                while time.monotonic() < deadline:
                    status = task_status(client, video_ids[video_index], case_task_id)
                    resumed_snapshot = runner.database_snapshot([case_task_id])
                    maximum_unconfirmed = max(maximum_unconfirmed, int(resumed_snapshot["unconfirmed"]))
                    if status.get("status") == "succeeded":
                        break
                    time.sleep(1)
                else:
                    raise AcceptanceFailure(f"Beat did not resume {case_name} through automatic recovery")
                resumed_row = resumed_snapshot["task_rows"][0]
                if (
                    resumed_row["auto_recovery_count"] != 1
                    or resumed_row["summary_requests"] != 2
                    or len(resumed_row["attempts"]) != 2
                    or resumed_row["attempts"][-1]["execution_mode"] != "summary_only"
                ):
                    raise AcceptanceFailure(f"{case_name} did not use one bounded summary-only recovery")
                runner.record(
                    "recoverable_model_failure_resumed",
                    case=case_name,
                    task_id=case_task_id,
                    database=resumed_snapshot,
                )

            runner.stub_control("__control/plan", {"asr": [], "llm": [{"status": 401}]})
            permanent_created = require(
                client.post(
                    f"/videos/{video_ids[5]}/tasks",
                    headers=csrf_headers(client, origin),
                ),
                202,
                "permanent authentication-error task submission",
            ).json()
            permanent_task_id = str(uuid.UUID(permanent_created["id"]))
            task_ids.append(permanent_task_id)
            permanent_status = wait_for(
                lambda: stopped_failure_status(
                    client,
                    video_ids[5],
                    permanent_task_id,
                    {"LLM_AUTHENTICATION_FAILED"},
                ),
                timeout_seconds=180,
                message="permanent authentication failure was not surfaced for operator action",
                interval_seconds=1,
            )
            time.sleep(65)
            permanent_snapshot = runner.database_snapshot([permanent_task_id])
            permanent_row = permanent_snapshot["task_rows"][0]
            if permanent_row["auto_recovery_count"] != 0 or len(permanent_row["attempts"]) != 1:
                raise AcceptanceFailure("permanent authentication failure was automatically retried")
            recovery_response = client.post(
                f"/videos/{video_ids[5]}/tasks/{permanent_task_id}/recover",
                json={"attempt_id": permanent_row["attempts"][0]["id"]},
                headers=csrf_headers(client, origin),
            )
            if recovery_response.status_code != 409:
                raise AcceptanceFailure("permanent authentication failure exposed a manual retry")
            if recovery_response.json().get("detail", {}).get("code") != permanent_status["failure_code"]:
                raise AcceptanceFailure("permanent failure did not retain its stable operator reason")
            runner.record(
                "permanent_model_failure_not_retried",
                task_id=permanent_task_id,
                failure_code=permanent_status["failure_code"],
                database=permanent_snapshot,
            )

            runner.stub_control(
                "__control/plan",
                {"asr": [], "llm": [{"status": 503}, {"status": 503}, {"status": 503}]},
            )
            exhausted_created = require(
                client.post(
                    f"/videos/{video_ids[6]}/tasks",
                    headers=csrf_headers(client, origin),
                ),
                202,
                "three-request summary-budget exhaustion task submission",
            ).json()
            exhausted_task_id = str(uuid.UUID(exhausted_created["id"]))
            task_ids.append(exhausted_task_id)
            exhausted_snapshot: dict[str, Any] = {}
            exhausted_row: dict[str, Any] = {}
            exhaustion_deadline = time.monotonic() + arguments.timeout_seconds
            while time.monotonic() < exhaustion_deadline:
                exhausted_snapshot = runner.database_snapshot([exhausted_task_id])
                exhausted_row = exhausted_snapshot["task_rows"][0]
                if (
                    exhausted_row["status"] == "failed"
                    and exhausted_row["auto_recovery_count"] == 2
                    and exhausted_row["summary_requests"] == 3
                    and exhausted_row["delivery_wait_reason"] == "automatic_recovery_exhausted"
                ):
                    break
                time.sleep(1)
            else:
                raise AcceptanceFailure(
                    "automatic recovery did not stop at two attempts and three summary requests"
                )
            if len(exhausted_row["attempts"]) != 3 or any(
                attempt["execution_mode"] != "summary_only" for attempt in exhausted_row["attempts"][1:]
            ):
                raise AcceptanceFailure("summary-only automatic attempts were duplicated or re-ran ASR")
            transcript = require(
                client.get(f"/videos/{video_ids[6]}/transcript"), 200, "exhausted-task transcript"
            ).json()
            if transcript.get("complete") is not True or exhausted_row["summary_publications"] != 0:
                raise AcceptanceFailure("budget exhaustion did not preserve the completed transcript")
            runner.record(
                "automatic_and_summary_budgets_exhausted_independently",
                task_id=exhausted_task_id,
                database=exhausted_snapshot,
            )
            play_exhausted_budgets_in_browser(
                runner,
                environment=environment,
                username=username,
                password=password,
                video_id=video_ids[6],
            )
            browser_budget_exhaustion_checked = True
            play_recovery_states_in_browser(
                runner,
                environment=environment,
                username=username,
                password=password,
                recovered_video_id=video_ids[0],
                permanent_failure_video_id=video_ids[5],
            )
            browser_recovery_checked = True

        final = runner.database_snapshot(task_ids)
        traffic = runner.stub_control("__control/state")
        runner_logs = runner.compose_call("logs", "worker", timeout=15).stdout
        maintenance_logs = runner.compose_call("logs", "maintenance-worker", timeout=15).stdout
        beat_logs = runner.compose_call("logs", "beat", timeout=15).stdout
        if "recovery_resumed" not in maintenance_logs:
            raise AcceptanceFailure(
                "the real maintenance Worker did not report bounded recovery or wait resumption"
            )
        if "coordinate-analysis-deliveries" not in beat_logs:
            raise AcceptanceFailure("Beat did not schedule the Stage 5 maintenance task")
        if "analysis_stop_confirmed" not in runner_logs:
            raise AcceptanceFailure("the analysis supervisor did not log trusted stop confirmation")
        if any(
            row["summary_publications"] > 1
            or (row["status"] == "succeeded" and row["summary_publications"] != 1)
            for row in final["task_rows"]
        ):
            raise AcceptanceFailure("a successful task did not have exactly one published summary")
        if traffic["asr_requests"] != 8 or traffic["llm_requests"] != 12:
            raise AcceptanceFailure("the local failure matrix made unexpected or duplicate model requests")
        result = {
            "result": "passed",
            "project": arguments.project,
            "fixture_sha256": fixture_sha256,
            "task_ids": task_ids,
            "video_ids": video_ids,
            "chain": "Beat -> Redis -> maintenance Worker -> analysis Worker",
            "local_stub_requests": {"asr": traffic["asr_requests"], "llm": traffic["llm_requests"]},
            "shared_account_reference": environment["SILICONFLOW_ACCOUNT_REFERENCE"],
            "retry_after_seconds": 120,
            "recovery_error_classes": [
                "429",
                "timeout",
                "5xx",
                "authentication_permanent",
                "controlled_process_fault",
            ],
            "maximum_unconfirmed_executions": maximum_unconfirmed,
            "browser_wait_verified": browser_checked,
            "browser_recovery_verified": browser_recovery_checked,
            "browser_stop_confirmation_verified": browser_stop_confirmation_checked,
            "browser_budget_exhaustion_verified": browser_budget_exhaustion_checked,
            "controlled_worker_fault_verified": worker_fault_checked,
            "final_database": final,
            "history_rows_modified": 0,
            "real_supplier_calls": 0,
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
        if maintenance_paused:
            try:
                runner.compose_call("unpause", "maintenance-worker")
                runner.record("maintenance_resumed_in_finally")
            except Exception:
                runner.record("maintenance_resume_in_finally_failed")
        os.environ.pop("ANALYSIS_WORKER_INSTANCE_ID", None)


if __name__ == "__main__":
    raise SystemExit(main())
