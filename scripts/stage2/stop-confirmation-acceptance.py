from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "data" / "mvp-stage-2" / ".env"
COMPOSE_FILE = ROOT / "compose.yaml"
PROJECT = "videoclip-mvp-stage2"
FIXTURE_MODULE = "videoclip.stop_confirmation_acceptance"


class AcceptanceFailure(RuntimeError):
    pass


class Runner:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.events_path = output_dir / "events.jsonl"
        self.compose = [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-p",
            PROJECT,
            "-f",
            str(COMPOSE_FILE),
        ]

    def record(self, event: str, **values: Any) -> None:
        document = {"event": event, **values}
        with self.events_path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")

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
        )
        self.record(
            "command",
            argv=argv,
            exit_code=result.returncode,
        )
        if result.returncode not in expected:
            raise AcceptanceFailure(f"command failed with exit code {result.returncode}: {argv[0]}")
        return result

    def compose_call(
        self, *args: str, timeout: float = 30, expected: tuple[int, ...] = (0,)
    ) -> subprocess.CompletedProcess[str]:
        return self.command(self.compose + list(args), timeout=timeout, expected=expected)

    def psql(self, sql: str) -> str:
        config = {}
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                if key in {"POSTGRES_DB", "POSTGRES_USER"}:
                    config[key] = value.strip().strip('"')
        database = config.get("POSTGRES_DB")
        user = config.get("POSTGRES_USER")
        if (
            not database
            or not user
            or not database.replace("_", "").isalnum()
            or not user.replace("_", "").isalnum()
        ):
            raise AcceptanceFailure("Stage 2 database identifiers are missing or invalid")
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

    def snapshot(self) -> dict[str, Any]:
        raw = self.psql(
            "SELECT json_build_object("
            "'users',(SELECT count(*) FROM users),"
            "'uploads',(SELECT count(*) FROM upload_handoffs),"
            "'videos',(SELECT count(*) FROM videos),"
            "'tasks_by_status',(SELECT coalesce(json_object_agg(status,n),'{}'::json) "
            "FROM (SELECT status,count(*) n FROM analysis_tasks GROUP BY status) s),"
            "'attempts',(SELECT count(*) FROM analysis_attempts),"
            "'executions',(SELECT count(*) FROM analysis_executions),"
            "'unconfirmed',(SELECT count(*) FROM analysis_executions WHERE stop_confirmed_at IS NULL),"
            "'deliveries',(SELECT count(*) FROM task_deliveries),"
            "'transcript_segments',(SELECT count(*) FROM transcript_segments),"
            "'summary_requests',(SELECT count(*) FROM summary_requests),"
            "'summaries',(SELECT count(*) FROM analysis_summaries)"
            ")::text"
        )
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise AcceptanceFailure("Stage 2 database snapshot was not a JSON object")
        return document

    def queue_length(self) -> int:
        result = self.compose_call("exec", "-T", "redis", "redis-cli", "LLEN", "analysis")
        return int(result.stdout.strip())

    def process_states(self, container_id: str, pids: list[int]) -> dict[int, str | None]:
        code = """
import json, pathlib, sys
out = {}
for value in sys.argv[1:]:
    pid = int(value)
    try:
        raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
        out[pid] = raw[raw.rfind(")") + 2 :].split()[0]
    except FileNotFoundError:
        out[pid] = None
print(json.dumps(out, sort_keys=True))
"""
        result = self.command(
            ["docker", "exec", container_id, "python", "-c", code, *[str(pid) for pid in pids]],
            timeout=10,
        )
        return {int(pid): state for pid, state in json.loads(result.stdout).items()}

    def fixture_call(self, service: str, *args: str) -> str:
        result = self.compose_call(
            "exec",
            "-T",
            "-e",
            "ANALYSIS_STOP_ACCEPTANCE=1",
            service,
            "python",
            "-m",
            FIXTURE_MODULE,
            *args,
            timeout=30,
        )
        return result.stdout.strip()

    def start_fixture(
        self, *, cleanup_sentinel: bool = False
    ) -> tuple[subprocess.Popen[str], dict[str, Any]]:
        argv = self.compose + [
            "exec",
            "-T",
            "-e",
            "ANALYSIS_STOP_ACCEPTANCE=1",
        ]
        if cleanup_sentinel:
            argv.extend(["-e", "ANALYSIS_STOP_ACCEPTANCE_CLEANUP_SENTINEL=1"])
        argv.extend(["worker", "sh", "-c", f"python -m {FIXTURE_MODULE} run & wait"])
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if process.stdout is None:
            raise AcceptanceFailure("could not capture the isolated process fixture")
        ready_line = process.stdout.readline()
        if not ready_line:
            _, stderr = process.communicate(timeout=10)
            error_class = "unknown"
            for line in reversed(stderr.splitlines()):
                match = re.match(r"^([A-Za-z][A-Za-z0-9_]*(?:Error|Failure|Exception))(?:\b|:)", line.strip())
                if match is not None:
                    error_class = match.group(1)
                    break
            self.record(
                "fixture_start_failed",
                exit_code=process.returncode,
                error_class=error_class,
            )
            raise AcceptanceFailure(f"isolated process fixture failed to start ({error_class})")
        try:
            ready = json.loads(ready_line)
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("isolated process fixture did not return its identity record") from exc
        if ready.get("event") != "ready":
            raise AcceptanceFailure("isolated process fixture returned an unexpected record")
        self.record("fixture_ready", **{key: value for key, value in ready.items() if key != "event"})
        return process, ready

    def terminate_fixture_parent(self, container_id: str, pid: int) -> None:
        code = "import os,signal,sys; os.kill(int(sys.argv[1]), signal.SIGKILL)"
        self.command(
            ["docker", "exec", container_id, "python", "-c", code, str(pid)],
            timeout=10,
            expected=(0, 137, -9),
        )

    def kill_execution_connection(self, backend_pid: int) -> None:
        if backend_pid <= 0:
            raise AcceptanceFailure("fixture returned an invalid PostgreSQL backend PID")
        result = self.psql(f"SELECT pg_terminate_backend({backend_pid})")
        if result.lower() not in {"t", "true"}:
            raise AcceptanceFailure("could not disconnect the isolated execution database session")

    def wait_postgres_healthy(self, timeout: float = 90) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.compose_call("ps", "--format", "json", expected=(0,))
            containers = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            postgres = [item for item in containers if item.get("Service") == "postgres"]
            if len(postgres) == 1 and postgres[0].get("Health") == "healthy":
                self.record("postgres_healthy", container_id=postgres[0].get("ID"))
                return
            time.sleep(1)
        raise AcceptanceFailure("Stage 2 PostgreSQL did not return to healthy status")


def _is_live(state: str | None) -> bool:
    return state is not None and state not in {"Z", "X"}


def _load_worker_binding(runner: Runner) -> tuple[str, str]:
    result = runner.compose_call("ps", "--format", "json")
    containers = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    workers = [
        item
        for item in containers
        if item.get("Service") == "worker" and item.get("State") == "running"
    ]
    if len(workers) != 1:
        raise AcceptanceFailure("Stage 2 must have exactly one running analysis Worker")
    container_id = str(workers[0]["ID"])
    inspect = runner.command(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", container_id]
    )
    environment = json.loads(inspect.stdout)
    value = next(
        (entry.split("=", 1)[1] for entry in environment if entry.startswith("ANALYSIS_WORKER_INSTANCE_ID=")),
        "",
    )
    try:
        instance_id = str(uuid.UUID(value))
    except ValueError as exc:
        raise AcceptanceFailure("Stage 2 Worker has no inspectable instance generation") from exc
    return container_id, instance_id


def _assert_baseline(runner: Runner, snapshot: dict[str, Any]) -> None:
    if snapshot["unconfirmed"] != 0:
        raise AcceptanceFailure("an existing unconfirmed execution blocks this acceptance")
    if runner.psql("SELECT count(*) FROM analysis_tasks WHERE status='running'") != "0":
        raise AcceptanceFailure("a historical analysis task is running")
    if runner.queue_length() != 0:
        raise AcceptanceFailure("the analysis queue must be empty before fault acceptance")


def _assert_single_unconfirmed(runner: Runner, execution_id: str) -> None:
    exact = runner.psql(
        "SELECT count(*) FROM analysis_executions "
        f"WHERE id='{execution_id}' AND stop_confirmed_at IS NULL"
    )
    global_count = runner.psql(
        "SELECT count(*) FROM analysis_executions WHERE stop_confirmed_at IS NULL"
    )
    if exact != "1" or global_count != "1":
        raise AcceptanceFailure("the isolated execution no longer exclusively owns the global slot")


def _probe_claim(runner: Runner, ready: dict[str, Any]) -> None:
    response = json.loads(
        runner.fixture_call(
            "worker",
            "probe",
            "--task-id",
            ready["queued_task_id"],
            "--attempt-id",
            ready["queued_attempt_id"],
        )
    )
    if response.get("claim") != "blocked_by_unconfirmed_execution":
        raise AcceptanceFailure("the queued fixture was not blocked by the active stop confirmation")
    _assert_single_unconfirmed(runner, ready["execution_id"])


def _probe_cleanup(runner: Runner, ready: dict[str, Any]) -> None:
    response = json.loads(
        runner.fixture_call(
            "worker",
            "cleanup-probe",
            "--owner-id",
            ready["owner_id"],
            "--video-id",
            ready["video_id"],
            "--attempt-id",
            ready["attempt_id"],
        )
    )
    if response != {"cleanup": "blocked_by_stop_confirmation", "sentinel_preserved": True}:
        raise AcceptanceFailure("dangerous attempt cleanup was not blocked while execution was active")
    runner.record(
        "attempt_cleanup_blocked_before_stop_confirmation",
        execution_id=ready["execution_id"],
        attempt_id=ready["attempt_id"],
        sentinel_preserved=True,
    )
    _assert_single_unconfirmed(runner, ready["execution_id"])


def _sentinel_exists(runner: Runner, container_id: str, path: str) -> bool:
    sentinel = PurePosixPath(path)
    if (
        not sentinel.as_posix().startswith("/appdata/analysis/")
        or sentinel.name != "stop-proof-sentinel"
        or ".." in sentinel.parts
    ):
        raise AcceptanceFailure("refusing to inspect a path outside the isolated attempt fixture")
    code = "import pathlib,sys; print('present' if pathlib.Path(sys.argv[1]).is_file() else 'absent')"
    result = runner.command(["docker", "exec", container_id, "python", "-c", code, path], timeout=10)
    value = result.stdout.strip()
    if value not in {"present", "absent"}:
        raise AcceptanceFailure("the attempt cleanup sentinel returned an invalid state")
    return value == "present"


def _cleanup_process_fixture_files(runner: Runner, ready: dict[str, Any]) -> None:
    process_directory = PurePosixPath(str(ready["process_directory"]))
    if (
        process_directory.parent.as_posix() != "/tmp"
        or not process_directory.name.startswith("videoclip-stage3-processes-")
    ):
        raise AcceptanceFailure("refusing to remove a path outside the isolated process fixture")
    cleanup_code = """
import shutil, sys
from pathlib import Path
directory = Path(sys.argv[1])
if directory.parent != Path('/tmp') or not directory.name.startswith('videoclip-stage3-processes-'):
    raise SystemExit('refusing to remove an unscoped process fixture directory')
if directory.is_symlink():
    raise SystemExit('refusing to remove a symlinked process fixture directory')
shutil.rmtree(directory, ignore_errors=False)
print('isolated_process_files_removed')
"""
    result = runner.compose_call(
        "exec", "-T", "worker", "python", "-c", cleanup_code, process_directory.as_posix()
    )
    if result.stdout.strip() != "isolated_process_files_removed":
        raise AcceptanceFailure("the isolated process fixture files were not removed")


def _cleanup_fixture(runner: Runner, ready: dict[str, Any]) -> None:
    runner.fixture_call(
        "api",
        "cleanup",
        "--owner-id",
        ready["owner_id"],
        "--task-id",
        ready["task_id"],
        "--task-id",
        ready["queued_task_id"],
        "--attempt-id",
        ready["attempt_id"],
        "--attempt-id",
        ready["queued_attempt_id"],
        "--execution-id",
        ready["execution_id"],
    )


def _automatic_stop_scenario(runner: Runner, container_id: str) -> dict[str, Any]:
    child, ready = runner.start_fixture(cleanup_sentinel=True)
    initial_states = runner.process_states(
        container_id,
        [ready["boundary_pid"], *[entry["pid"] for entry in ready["children"]]],
    )
    if not all(_is_live(state) for state in initial_states.values()):
        raise AcceptanceFailure("the supervised execution fixture did not start all real child processes")
    _assert_single_unconfirmed(runner, ready["execution_id"])

    runner.kill_execution_connection(ready["database_backend_pid"])
    alive_states = runner.process_states(
        container_id,
        [ready["boundary_pid"], *[entry["pid"] for entry in ready["children"]]],
    )
    _assert_single_unconfirmed(runner, ready["execution_id"])
    if not all(_is_live(state) for state in alive_states.values()):
        raise AcceptanceFailure(
            "disconnecting the execution DB connection changed a controlled local process"
        )
    runner.record(
        "execution_connection_disconnected_processes_alive",
        execution_id=ready["execution_id"],
        worker_instance_id=ready["worker_instance_id"],
        database_backend_pid=ready["database_backend_pid"],
        processes={str(pid): state for pid, state in alive_states.items()},
        unconfirmed_execution_count=1,
    )
    _probe_claim(runner, ready)
    _probe_cleanup(runner, ready)

    runner.terminate_fixture_parent(container_id, ready["boundary_pid"])
    try:
        parent_exit_code = child.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        child.kill()
        raise AcceptanceFailure("docker exec did not report the forced executor exit") from exc

    deadline = time.monotonic() + 20
    saw_unconfirmed_residual = False
    children = [entry["pid"] for entry in ready["children"]]
    while time.monotonic() < deadline:
        states = runner.process_states(container_id, children)
        unconfirmed = runner.psql(
            "SELECT count(*) FROM analysis_executions "
            f"WHERE id='{ready['execution_id']}' AND stop_confirmed_at IS NULL"
        )
        live_roles = [
            entry["role"] for entry in ready["children"] if _is_live(states.get(entry["pid"]))
        ]
        if live_roles and unconfirmed == "1":
            saw_unconfirmed_residual = True
        if live_roles and unconfirmed == "0":
            raise AcceptanceFailure("execution was confirmed while controlled subprocesses were still alive")
        if not live_roles and unconfirmed == "0":
            break
        time.sleep(0.1)
    else:
        raise AcceptanceFailure("the production supervisor left a controlled subprocess alive")

    confirmation = runner.psql(
        "SELECT stop_confirmation_source || '|' || stop_evidence_sha256 "
        f"FROM analysis_executions WHERE id='{ready['execution_id']}' AND stop_confirmed_at IS NOT NULL"
    )
    if not saw_unconfirmed_residual or not confirmation.startswith("worker_supervisor|"):
        raise AcceptanceFailure(
            "real child exit evidence was not followed by the expected supervisor confirmation"
        )
    sentinel_path = ready.get("cleanup_sentinel")
    if not isinstance(sentinel_path, str):
        raise AcceptanceFailure("the isolated active attempt did not create a cleanup sentinel")
    cleanup_deadline = time.monotonic() + 10
    while time.monotonic() < cleanup_deadline and _sentinel_exists(runner, container_id, sentinel_path):
        time.sleep(0.1)
    if _sentinel_exists(runner, container_id, sentinel_path):
        raise AcceptanceFailure("confirmed attempt cleanup did not remove its isolated sentinel")
    runner.record(
        "automatic_stop_verified",
        execution_id=ready["execution_id"],
        worker_instance_id=ready["worker_instance_id"],
        boundary_pid=ready["boundary_pid"],
        child_processes=ready["children"],
        fixture_parent_exit_code=parent_exit_code,
        residual_seen_before_confirmation=saw_unconfirmed_residual,
        confirmation=confirmation,
    )
    _cleanup_process_fixture_files(runner, ready)
    _cleanup_fixture(runner, ready)
    return ready


def _database_outage_and_manual_stop_scenario(
    runner: Runner, container_id: str
) -> dict[str, Any]:
    child, ready = runner.start_fixture()
    states = runner.process_states(
        container_id,
        [ready["boundary_pid"], *[entry["pid"] for entry in ready["children"]]],
    )
    if not all(_is_live(state) for state in states.values()):
        raise AcceptanceFailure("the manual stop fixture did not start all real child processes")
    _assert_single_unconfirmed(runner, ready["execution_id"])
    runner.kill_execution_connection(ready["database_backend_pid"])
    _assert_single_unconfirmed(runner, ready["execution_id"])
    _probe_claim(runner, ready)

    runner.terminate_fixture_parent(container_id, ready["boundary_pid"])
    try:
        parent_exit_code = child.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        child.kill()
        raise AcceptanceFailure("docker exec did not report the forced executor exit") from exc

    runner.compose_call("stop", "--timeout", "0", "postgres", timeout=20)
    child_pids = [entry["pid"] for entry in ready["children"]]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        states = runner.process_states(container_id, child_pids)
        if not any(_is_live(state) for state in states.values()):
            break
        time.sleep(0.1)
    else:
        raise AcceptanceFailure("the supervisor did not stop subprocesses while PostgreSQL was unavailable")

    required_log = f"stop_confirmation_required execution_id={ready['execution_id']}"
    deadline = time.monotonic() + 20
    saw_db_write_failure = False
    saw_manual_required = False
    while time.monotonic() < deadline:
        logs = runner.compose_call("logs", "--tail=500", "worker", timeout=10).stdout
        saw_db_write_failure = saw_db_write_failure or (
            f"stop_confirmation_write_failed execution_id={ready['execution_id']}" in logs
        )
        saw_manual_required = saw_manual_required or required_log in logs
        if saw_manual_required:
            break
        time.sleep(0.25)
    if not saw_db_write_failure or not saw_manual_required:
        raise AcceptanceFailure(
            "the supervisor did not exhaust same-evidence DB retries and request manual handling"
        )

    runner.compose_call("up", "-d", "postgres", timeout=60)
    runner.wait_postgres_healthy()
    _assert_single_unconfirmed(runner, ready["execution_id"])
    runner.record(
        "db_outage_stop_unconfirmed",
        execution_id=ready["execution_id"],
        fixture_parent_exit_code=parent_exit_code,
        db_connection_terminated=True,
        child_processes_exited=True,
        supervisor_database_write_failed=saw_db_write_failure,
        manual_handling_required=saw_manual_required,
    )

    _cleanup_process_fixture_files(runner, ready)
    _operator_confirm(runner, ready, container_id, "db_outage")
    _cleanup_fixture(runner, ready)

    _restart_stage2(runner)
    return ready


def _operator_confirm(
    runner: Runner, ready: dict[str, Any], container_id: str, scenario: str
) -> str:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        raise AcceptanceFailure("PowerShell is required to run the existing operator stop script")
    runner.command(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(ROOT / "scripts" / "stage2" / "stop-and-confirm-worker.ps1"),
            "-ExecutionId",
            ready["execution_id"],
        ],
        timeout=45,
    )
    confirmation = runner.psql(
        "SELECT stop_confirmation_source || '|' || stop_evidence_sha256 "
        f"FROM analysis_executions WHERE id='{ready['execution_id']}' AND stop_confirmed_at IS NOT NULL"
    )
    if not confirmation.startswith("operator_container_exit|"):
        raise AcceptanceFailure("the manual script did not confirm the exact stopped Worker container")
    runner.record(
        "operator_stop_verified",
        scenario=scenario,
        execution_id=ready["execution_id"],
        worker_instance_id=ready["worker_instance_id"],
        boundary_pid=ready["boundary_pid"],
        exited_container_id=container_id,
        confirmation=confirmation,
    )
    return confirmation


def _restart_stage2(runner: Runner) -> None:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        raise AcceptanceFailure("PowerShell is required to restart Stage 2 safely")
    restart = subprocess.run(
        [powershell, "-NoProfile", "-File", str(ROOT / "scripts" / "stage2" / "run-stage2.ps1")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    runner.record(
        "stage2_restart",
        exit_code=restart.returncode,
    )
    if restart.returncode != 0:
        raise AcceptanceFailure("Stage 2 run script failed to restore the project after manual confirmation")


def _supervisor_and_container_death_scenario(runner: Runner, container_id: str) -> dict[str, Any]:
    child, ready = runner.start_fixture()
    states = runner.process_states(
        container_id,
        [ready["boundary_pid"], *[entry["pid"] for entry in ready["children"]]],
    )
    if not all(_is_live(state) for state in states.values()):
        raise AcceptanceFailure("the hard-stop fixture did not start all real child processes")
    _assert_single_unconfirmed(runner, ready["execution_id"])
    runner.kill_execution_connection(ready["database_backend_pid"])
    _assert_single_unconfirmed(runner, ready["execution_id"])
    _probe_claim(runner, ready)

    runner.command(["docker", "kill", "--signal=KILL", container_id], timeout=20)
    try:
        fixture_exit_code = child.wait(timeout=20)
    except subprocess.TimeoutExpired as exc:
        child.kill()
        raise AcceptanceFailure("the forced Worker container stop did not terminate the fixture") from exc
    inspect = runner.command(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}|{{.State.ExitCode}}|{{.State.FinishedAt}}",
            container_id,
        ]
    )
    status, container_exit_code, finished_at = inspect.stdout.strip().split("|", 2)
    if status != "exited" or int(container_exit_code) == 0:
        raise AcceptanceFailure("the exact Worker supervisor container did not hard-exit")
    _assert_single_unconfirmed(runner, ready["execution_id"])
    runner.record(
        "supervisor_container_hard_stop",
        execution_id=ready["execution_id"],
        worker_instance_id=ready["worker_instance_id"],
        boundary_pid=ready["boundary_pid"],
        child_processes=ready["children"],
        fixture_parent_exit_code=fixture_exit_code,
        container_id=container_id,
        container_status=status,
        container_exit_code=int(container_exit_code),
        container_finished_at=finished_at,
        stop_confirmation_written=False,
    )

    _operator_confirm(runner, ready, container_id, "supervisor_container_hard_stop")
    _cleanup_fixture(runner, ready)
    _restart_stage2(runner)
    return ready


def main() -> int:
    if not ENV_FILE.is_file():
        raise AcceptanceFailure("Stage 2 private environment file is missing")
    output_dir = ROOT / "data" / "mvp-stage-3" / "acceptance" / f"run-{uuid.uuid4().hex}"
    output_dir.mkdir(parents=True, exist_ok=False)
    runner = Runner(output_dir)
    try:
        baseline = runner.snapshot()
        _assert_baseline(runner, baseline)
        container_id, instance_id = _load_worker_binding(runner)
        runtime_code = """
import json, pathlib
def command_line(path):
    try:
        return path.read_bytes().replace(bytes([0]), b" ").decode()
    except FileNotFoundError:
        return ""
pid1 = command_line(pathlib.Path("/proc/1/cmdline"))
cmdlines = [
    value
    for path in pathlib.Path("/proc").glob("[0-9]*/cmdline")
    if "celery" in (value := command_line(path))
]
print(json.dumps({"pid1": pid1, "cmdlines": cmdlines}))
"""
        runtime = runner.command(
            [
                "docker",
                "exec",
                container_id,
                "python",
                "-c",
                runtime_code,
            ],
            timeout=15,
        )
        runtime_info = json.loads(runtime.stdout)
        if "analysis_supervisor" not in runtime_info["pid1"]:
            raise AcceptanceFailure("Stage 2 Worker PID 1 is not the production supervisor entry point")
        if not any(
            "--queues=analysis" in cmd and "--concurrency=1" in cmd for cmd in runtime_info["cmdlines"]
        ):
            raise AcceptanceFailure(
                "Stage 2 supervisor did not launch the single analysis-only Celery Worker"
            )
        runner.record("baseline", database=baseline, analysis_queue=runner.queue_length())
        runner.record(
            "runtime_identity",
            worker_container_id=container_id,
            worker_instance_id=instance_id,
            pid1=runtime_info["pid1"],
        )

        _automatic_stop_scenario(runner, container_id)
        _database_outage_and_manual_stop_scenario(runner, container_id)
        container_id, _ = _load_worker_binding(runner)
        _supervisor_and_container_death_scenario(runner, container_id)
        final_snapshot = runner.snapshot()
        if final_snapshot != baseline:
            raise AcceptanceFailure("Stage 2 database counts did not return to their pre-acceptance values")
        if runner.queue_length() != 0:
            raise AcceptanceFailure("the analysis queue is not empty after acceptance")
        runner.record("final", database=final_snapshot, analysis_queue=runner.queue_length())
        print(f"PASS: isolated trusted-stop fault evidence is in {output_dir}")
        return 0
    except Exception as exc:
        runner.record("failure", reason=type(exc).__name__, message=str(exc))
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"Evidence directory: {output_dir}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
