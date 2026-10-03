from __future__ import annotations

import json
import logging
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from videoclip.db import create_database_engine, create_session_factory
from videoclip.models import AnalysisExecution
from videoclip.services import cleanup_confirmed_attempt_files
from videoclip.settings import get_settings
from videoclip.stop_confirmation import (
    PROCESS_SCOPE,
    ExecutionIdentity,
    confirm_stopped_execution,
    identity_from_execution,
    worker_stop_evidence,
)
from videoclip.storage import MediaStore

logger = logging.getLogger("videoclip.analysis_supervisor")
SOCKET_PATH = Path(os.environ.get("ANALYSIS_SUPERVISOR_SOCKET", "/tmp/videoclip-analysis-supervisor.sock"))
TERM_GRACE_SECONDS = 2.0
KILL_GRACE_SECONDS = 5.0
CONFIRM_RETRY_DELAYS = (0.0, 0.5, 1.5)


@dataclass(frozen=True)
class ProcessStat:
    pid: int
    state: str
    process_group_id: int
    session_id: int
    start_ticks: int


@dataclass(frozen=True)
class WorkerBoundary:
    worker_instance_id: uuid.UUID
    worker_container_id: str
    process_scope: str
    boundary_pid: int
    boundary_start_ticks: int
    boundary_session_id: int
    boundary_boot_id: str


@dataclass
class TrackedExecution:
    identity: ExecutionIdentity
    retry_index: int = 0
    next_retry_at: float = 0.0
    abandoned: bool = False


class BoundaryUnknown(RuntimeError):
    pass


def read_process_stat(pid: int) -> ProcessStat | None:
    path = Path(f"/proc/{pid}/stat")
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BoundaryUnknown("cannot read the bound process identity") from exc
    end = raw.rfind(")")
    if end < 0:
        raise BoundaryUnknown("malformed /proc process record")
    fields = raw[end + 2 :].split()
    if len(fields) < 20:
        raise BoundaryUnknown("incomplete /proc process record")
    try:
        return ProcessStat(
            pid=pid,
            state=fields[0],
            process_group_id=int(fields[2]),
            session_id=int(fields[3]),
            start_ticks=int(fields[19]),
        )
    except ValueError as exc:
        raise BoundaryUnknown("invalid /proc process identity") from exc


def read_process_start_ticks(pid: int) -> int:
    stat = read_process_stat(pid)
    if stat is None:
        raise BoundaryUnknown("execution process disappeared before it could be bound")
    return stat.start_ticks


def read_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise BoundaryUnknown("Linux boot identity is unavailable") from exc
    if not value:
        raise BoundaryUnknown("Linux boot identity is empty")
    return value


def establish_execution_boundary() -> WorkerBoundary:
    if not sys.platform.startswith("linux") or not hasattr(os, "setsid"):
        raise BoundaryUnknown("trusted analysis execution requires a Linux session boundary")
    try:
        os.setsid()
    except OSError as exc:
        raise BoundaryUnknown("could not isolate this analysis execution in a new session") from exc
    pid = os.getpid()
    stat = read_process_stat(pid)
    if stat is None or stat.session_id != pid:
        raise BoundaryUnknown("the execution session could not be identified")
    container_id = os.environ.get("HOSTNAME", "").lower()
    instance_value = os.environ.get("ANALYSIS_WORKER_INSTANCE_ID", "")
    try:
        instance_id = uuid.UUID(instance_value)
    except ValueError as exc:
        raise BoundaryUnknown("the Worker instance generation is unavailable") from exc
    if len(container_id) < 12 or any(character not in "0123456789abcdef" for character in container_id):
        raise BoundaryUnknown("the Linux container identity is unavailable")
    return WorkerBoundary(
        worker_instance_id=instance_id,
        worker_container_id=container_id,
        process_scope=PROCESS_SCOPE,
        boundary_pid=pid,
        boundary_start_ticks=stat.start_ticks,
        boundary_session_id=stat.session_id,
        boundary_boot_id=read_boot_id(),
    )


def _identity_json(identity: ExecutionIdentity) -> dict[str, object]:
    return {
        "execution_id": str(identity.execution_id),
        "task_id": str(identity.task_id),
        "attempt_id": str(identity.attempt_id),
        "worker_instance_id": str(identity.worker_instance_id),
        "worker_container_id": identity.worker_container_id,
        "process_scope": identity.process_scope,
        "boundary_pid": identity.boundary_pid,
        "boundary_start_ticks": identity.boundary_start_ticks,
        "boundary_session_id": identity.boundary_session_id,
        "boundary_boot_id": identity.boundary_boot_id,
    }


def register_execution_with_supervisor(identity: ExecutionIdentity, socket_path: Path = SOCKET_PATH) -> None:
    address_family = getattr(socket, "AF_UNIX", None)
    if address_family is None:
        raise BoundaryUnknown("Unix domain sockets are unavailable")
    request_document = {"operation": "register", "identity": _identity_json(identity)}
    request = json.dumps(request_document).encode("utf-8") + b"\n"
    try:
        with socket.socket(address_family, socket.SOCK_STREAM) as connection:
            connection.settimeout(3.0)
            connection.connect(str(socket_path))
            connection.sendall(request)
            response = connection.makefile("rb").readline(4097)
    except (OSError, TimeoutError) as exc:
        raise BoundaryUnknown("analysis supervisor registration is unavailable") from exc
    if len(response) > 4096:
        raise BoundaryUnknown("analysis supervisor returned an invalid response")
    try:
        payload = json.loads(response)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BoundaryUnknown("analysis supervisor returned an invalid response") from exc
    if not isinstance(payload, dict) or payload.get("status") != "registered":
        raise BoundaryUnknown("analysis supervisor did not accept the execution binding")


def _identity_from_payload(value: Any) -> ExecutionIdentity:
    if not isinstance(value, dict):
        raise ValueError("identity must be an object")
    return ExecutionIdentity(
        execution_id=uuid.UUID(str(value["execution_id"])),
        task_id=uuid.UUID(str(value["task_id"])),
        attempt_id=uuid.UUID(str(value["attempt_id"])),
        worker_instance_id=uuid.UUID(str(value["worker_instance_id"])),
        worker_container_id=str(value["worker_container_id"]),
        process_scope=str(value["process_scope"]),
        boundary_pid=int(value["boundary_pid"]),
        boundary_start_ticks=int(value["boundary_start_ticks"]),
        boundary_session_id=int(value["boundary_session_id"]),
        boundary_boot_id=str(value["boundary_boot_id"]),
    )


def _processes_in_session(session_id: int) -> list[ProcessStat]:
    proc = Path("/proc")
    if not proc.is_dir():
        raise BoundaryUnknown("/proc is unavailable")
    members: list[ProcessStat] = []
    try:
        entries = list(proc.iterdir())
    except OSError as exc:
        raise BoundaryUnknown("cannot enumerate /proc") from exc
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        stat = read_process_stat(int(entry.name))
        if stat is not None and stat.session_id == session_id and stat.state not in {"Z", "X"}:
            members.append(stat)
    return members


def _send_to_groups(members: list[ProcessStat], signum: int) -> None:
    kill_group = getattr(os, "killpg", None)
    if kill_group is None:
        raise BoundaryUnknown("process group signaling is unavailable")
    group_ids = {member.process_group_id for member in members if member.process_group_id > 0}
    for process_group_id in sorted(group_ids):
        try:
            kill_group(process_group_id, signum)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise BoundaryUnknown("cannot signal a process inside the controlled execution session") from exc


def _leader_state(identity: ExecutionIdentity) -> Literal["alive", "exited", "mismatch"]:
    leader = read_process_stat(identity.boundary_pid)
    if leader is None:
        return "exited"
    if (
        leader.start_ticks != identity.boundary_start_ticks
        or leader.session_id != identity.boundary_session_id
        or identity.boundary_session_id != identity.boundary_pid
    ):
        return "mismatch"
    return "alive"


def _terminate_session(identity: ExecutionIdentity, *, include_leader: bool) -> bool:
    leader_state = _leader_state(identity)
    if leader_state == "mismatch" or (leader_state == "alive" and not include_leader):
        return False
    members = _processes_in_session(identity.boundary_session_id)
    if not members:
        return True
    term_signal = getattr(signal, "SIGTERM", None)
    kill_signal = getattr(signal, "SIGKILL", None)
    if term_signal is None or kill_signal is None:
        raise BoundaryUnknown("required process termination signals are unavailable")
    _send_to_groups(members, term_signal)
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _processes_in_session(identity.boundary_session_id):
            return True
        time.sleep(0.05)
    members = _processes_in_session(identity.boundary_session_id)
    if members:
        _send_to_groups(members, kill_signal)
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not _processes_in_session(identity.boundary_session_id):
            return True
        time.sleep(0.05)
    return not _processes_in_session(identity.boundary_session_id)


class AnalysisSupervisor:
    def __init__(
        self,
        factory: Any,
        instance_id: uuid.UUID,
        worker_container_id: str,
        store: MediaStore,
    ) -> None:
        self.factory = factory
        self.instance_id = instance_id
        self.worker_container_id = worker_container_id
        self.store = store
        self.tracked: dict[uuid.UUID, TrackedExecution] = {}
        self.lock = threading.RLock()
        self.stop_requested = False
        self.celery_pid: int | None = None
        self.celery_return_code: int | None = None

    def register(self, identity: ExecutionIdentity) -> None:
        if (
            identity.worker_instance_id != self.instance_id
            or identity.worker_container_id[:12] != self.worker_container_id[:12]
            or identity.process_scope != PROCESS_SCOPE
            or identity.boundary_session_id != identity.boundary_pid
            or identity.boundary_boot_id != read_boot_id()
        ):
            raise ValueError("Worker instance or execution session does not match this supervisor")
        stat = read_process_stat(identity.boundary_pid)
        if (
            stat is None
            or stat.start_ticks != identity.boundary_start_ticks
            or stat.session_id != identity.boundary_session_id
        ):
            raise ValueError("execution process identity cannot be verified")
        with self.factory.begin() as session:
            execution = session.scalar(
                select(AnalysisExecution)
                .where(
                    AnalysisExecution.id == identity.execution_id,
                    AnalysisExecution.task_id == identity.task_id,
                    AnalysisExecution.attempt_id == identity.attempt_id,
                )
                .with_for_update()
            )
            if execution is None or identity_from_execution(execution) != identity:
                raise ValueError("execution binding does not match the database record")
            if execution.stop_confirmed_at is not None:
                raise ValueError("execution is already confirmed stopped")
            execution.supervisor_registered_at = execution.supervisor_registered_at or datetime.now(UTC)
        with self.lock:
            previous = self.tracked.get(identity.execution_id)
            if previous is not None and previous.identity != identity:
                raise ValueError("execution is already registered with another boundary")
            self.tracked[identity.execution_id] = previous or TrackedExecution(identity)

    def _confirm(self, tracked: TrackedExecution) -> None:
        identity = tracked.identity
        evidence = worker_stop_evidence(identity, session_stopped=True)
        try:
            result = confirm_stopped_execution(self.factory, evidence)
        except Exception:
            logger.exception("stop_confirmation_write_failed execution_id=%s", identity.execution_id)
            if tracked.retry_index + 1 >= len(CONFIRM_RETRY_DELAYS):
                tracked.abandoned = True
                logger.error("stop_confirmation_required execution_id=%s", identity.execution_id)
                return
            tracked.retry_index += 1
            tracked.next_retry_at = time.monotonic() + CONFIRM_RETRY_DELAYS[tracked.retry_index]
            return
        if result in {"confirmed", "already_confirmed"}:
            logger.info(
                "analysis_stop_confirmed execution_id=%s worker_instance_id=%s boundary_pid=%s "
                "session_id=%s evidence_sha256=%s result=%s",
                identity.execution_id,
                identity.worker_instance_id,
                identity.boundary_pid,
                identity.boundary_session_id,
                evidence.sha256,
                result,
            )
            try:
                cleaned = cleanup_confirmed_attempt_files(self.factory, self.store, identity.attempt_id)
                logger.info(
                    "analysis_attempt_cleanup execution_id=%s attempt_id=%s cleaned=%s",
                    identity.execution_id,
                    identity.attempt_id,
                    cleaned,
                )
            except Exception:
                logger.exception("analysis_attempt_cleanup_failed execution_id=%s", identity.execution_id)
            tracked.abandoned = True
        else:
            logger.error(
                "stop_confirmation_required execution_id=%s reason=identity_mismatch",
                identity.execution_id,
            )
            tracked.abandoned = True

    def monitor(self, *, force_stop: bool = False) -> None:
        now = time.monotonic()
        with self.lock:
            items = list(self.tracked.values())
        for tracked in items:
            if tracked.abandoned or now < tracked.next_retry_at:
                continue
            identity = tracked.identity
            try:
                state = _leader_state(identity)
                if state == "mismatch":
                    tracked.abandoned = True
                    logger.error(
                        "stop_confirmation_required execution_id=%s reason=boundary_mismatch",
                        identity.execution_id,
                    )
                    continue
                if state == "alive" and not force_stop:
                    continue
                if not _terminate_session(identity, include_leader=force_stop):
                    tracked.abandoned = True
                    logger.error(
                        "stop_confirmation_required execution_id=%s reason=session_not_empty",
                        identity.execution_id,
                    )
                    continue
                self._confirm(tracked)
            except BoundaryUnknown:
                tracked.abandoned = True
                logger.exception(
                    "stop_confirmation_required execution_id=%s reason=boundary_unreadable",
                    identity.execution_id,
                )
            except Exception:
                tracked.abandoned = True
                logger.exception(
                    "stop_confirmation_required execution_id=%s reason=supervisor_error",
                    identity.execution_id,
                )

    def active_count(self) -> int:
        with self.lock:
            return sum(not item.abandoned for item in self.tracked.values())


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(4097)
        if not raw or len(raw) > 4096:
            self._reply({"status": "rejected", "reason": "invalid_request"})
            return
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("operation") != "register":
                raise ValueError("unsupported supervisor operation")
            identity = _identity_from_payload(request.get("identity"))
            self.server.supervisor.register(identity)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("supervisor_registration_rejected")
            self._reply({"status": "rejected", "reason": "identity_or_registration_invalid"})
            return
        self._reply({"status": "registered"})

    def _reply(self, payload: dict[str, str]) -> None:
        self.wfile.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        self.wfile.flush()


def _enable_child_subreaper() -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error_number = ctypes.get_errno()
        raise OSError(error_number, "cannot enable Linux child subreaper")


def _make_socket_server(supervisor: AnalysisSupervisor) -> Any:
    server_type = getattr(socketserver, "ThreadingUnixStreamServer", None)
    if server_type is None:
        raise RuntimeError("Unix domain socket server is unavailable")
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        if not SOCKET_PATH.is_socket():
            raise RuntimeError("supervisor socket path is occupied by a non-socket file")
        SOCKET_PATH.unlink()
    server = server_type(str(SOCKET_PATH), _ControlHandler)
    server.daemon_threads = True
    server.supervisor = supervisor
    os.chmod(SOCKET_PATH, 0o600)
    thread = threading.Thread(target=server.serve_forever, name="analysis-supervisor-socket", daemon=True)
    thread.start()
    return server


def _wait_child_status(child_pid: int) -> int | None:
    wait_id = getattr(os, "waitid", None)
    p_all = getattr(os, "P_ALL", None)
    exited = getattr(os, "WEXITED", None)
    nohang = getattr(os, "WNOHANG", None)
    nowait = getattr(os, "WNOWAIT", None)
    nohang_wait = getattr(os, "WNOHANG", None)
    if (
        wait_id is None
        or p_all is None
        or exited is None
        or nohang is None
        or nowait is None
        or nohang_wait is None
    ):
        raise RuntimeError("Linux child reaping APIs are unavailable")
    try:
        result = wait_id(p_all, 0, exited | nohang | nowait)
    except ChildProcessError:
        return None
    if result is None or result.si_pid == 0:
        return None
    if result.si_pid != child_pid:
        try:
            os.waitpid(result.si_pid, nohang_wait)
        except ChildProcessError:
            pass
        return None
    _, status = os.waitpid(child_pid, 0)
    return os.waitstatus_to_exitcode(status)


def _worker_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "videoclip.celery_app:celery_app",
        "worker",
        "--queues=analysis",
        "--loglevel=INFO",
        "--concurrency=1",
        "--prefetch-multiplier=1",
        "--max-tasks-per-child=1",
        "--hostname=stage2-analysis@%h",
    ]


def run_supervisor() -> int:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("trusted analysis supervision is supported only in the Linux Worker container")
    _enable_child_subreaper()
    settings = get_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        instance_id_value = os.environ.get("ANALYSIS_WORKER_INSTANCE_ID", "")
        try:
            instance_id = uuid.UUID(instance_id_value)
        except ValueError:
            # Direct Compose starts remain fail-safe; the host manual stop entry point will reject
            # confirmations for this generated value unless run-stage2 supplied it in container env.
            instance_id = uuid.uuid4()
        os.environ["ANALYSIS_WORKER_INSTANCE_ID"] = str(instance_id)
        container_id = os.environ.get("HOSTNAME", "").lower()
        if len(container_id) < 12 or any(char not in "0123456789abcdef" for char in container_id):
            raise RuntimeError("Docker Worker container identity is unavailable")
        supervisor = AnalysisSupervisor(
            factory,
            instance_id,
            container_id,
            MediaStore(settings.app_data_root),
        )
        server = _make_socket_server(supervisor)
        child = subprocess.Popen(
            _worker_command(), close_fds=True, start_new_session=False, process_group=0
        )
        supervisor.celery_pid = child.pid
        shutdown = threading.Event()

        def request_shutdown(signum: int, _frame: Any) -> None:
            logger.info("analysis_supervisor_shutdown_requested signal=%s", signum)
            shutdown.set()
            try:
                os.kill(child.pid, signum)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGTERM, request_shutdown)
        signal.signal(signal.SIGINT, request_shutdown)
        logger.info(
            "analysis_supervisor_started worker_instance_id=%s worker_container_id=%s celery_pid=%s",
            instance_id,
            container_id,
            child.pid,
        )
        child_status: int | None = None
        try:
            while child_status is None:
                supervisor.monitor()
                child_status = _wait_child_status(child.pid)
                if child_status is None:
                    time.sleep(0.1)
            child.returncode = child_status
            supervisor.celery_return_code = child_status
            # An unexpected Celery parent exit must not leave a pool child or local request/media
            # descendant running. It is still confirmed only after the complete bound session ends.
            supervisor.monitor(force_stop=True)
            deadline = time.monotonic() + KILL_GRACE_SECONDS + TERM_GRACE_SECONDS + 1
            while supervisor.active_count() and time.monotonic() < deadline:
                supervisor.monitor(force_stop=True)
                time.sleep(0.1)
            return child_status
        finally:
            server.shutdown()
            server.server_close()
            try:
                SOCKET_PATH.unlink()
            except FileNotFoundError:
                pass
    finally:
        engine.dispose()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return run_supervisor()


if __name__ == "__main__":
    raise SystemExit(main())
