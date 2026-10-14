from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from videoclip.analysis_wait import wait_budget_error
from videoclip.model_cooling import (
    CooldownPolicy,
    extend_shared_cooldown,
    fallback_cooldown_seconds,
    model_identity,
    proposed_cooldown_deadline,
    record_model_success,
    retry_after_deadline,
)
from videoclip.models import AnalysisTask, SharedModelCooldown
from videoclip.queue_coordination_stub import StubState
from videoclip.recovery_rules import (
    automatic_recovery_budget_available,
    is_automatically_recoverable_failure,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def test_local_model_stub_default_responses_have_complete_http_control_fields(
    tmp_path: Path,
) -> None:
    state = StubState(tmp_path / "traffic.jsonl", hold_first_asr=False)

    assert state.next_response("asr") == {
        "status": 200,
        "delay_seconds": 0,
        "retry_after": None,
        "hold": False,
    }
    assert state.next_response("llm") == {
        "status": 200,
        "delay_seconds": 0,
        "retry_after": None,
        "hold": False,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120", datetime(2026, 10, 4, 12, 2, tzinfo=UTC)),
        ("Sun, 04 Oct 2026 12:03:00 GMT", datetime(2026, 10, 4, 12, 3, tzinfo=UTC)),
        ("Sunday, 04-Oct-26 12:03:00 GMT", datetime(2026, 10, 4, 12, 3, tzinfo=UTC)),
        ("Sun Oct  4 12:03:00 2026", datetime(2026, 10, 4, 12, 3, tzinfo=UTC)),
    ],
)
def test_retry_after_accepts_seconds_and_http_dates(value: str, expected: datetime) -> None:
    assert retry_after_deadline(value, now=NOW) == expected


@pytest.mark.parametrize("value", [None, "", "-1", "+3", "1.5", "tomorrow", "120\nsecret", "x" * 129])
def test_invalid_retry_after_uses_bounded_fallback(value: str | None) -> None:
    assert retry_after_deadline(value, now=NOW) is None
    deadline, accepted = proposed_cooldown_deadline(
        now=NOW,
        retry_after=value,
        consecutive_backpressure_responses=1,
        random_fraction=0,
        policy=CooldownPolicy(60, 900),
    )
    assert not accepted
    assert (deadline - NOW).total_seconds() == 30


def test_fallback_exponential_backoff_is_capped_and_jittered_at_both_edges() -> None:
    policy = CooldownPolicy(backoff_base_seconds=60, backoff_max_seconds=900)
    assert fallback_cooldown_seconds(1, policy=policy, random_fraction=0) == 30
    assert fallback_cooldown_seconds(1, policy=policy, random_fraction=1) == 60
    assert fallback_cooldown_seconds(2, policy=policy, random_fraction=1) == 120
    assert fallback_cooldown_seconds(20, policy=policy, random_fraction=1) == 900
    with pytest.raises(ValueError):
        fallback_cooldown_seconds(1, policy=policy, random_fraction=1.01)


def test_account_reference_is_stable_across_secret_rotation_and_rejects_secret_like_values() -> None:
    first = model_identity("siliconflow", "main-account", "Qwen/Qwen3.5-4B")
    after_key_rotation = model_identity("siliconflow", "main-account", "Qwen/Qwen3.5-4B")
    assert first == after_key_rotation
    with pytest.raises(ValueError):
        model_identity("siliconflow", "sk-secret-value", "Qwen/Qwen3.5-4B")


def test_recovery_whitelist_excludes_permanent_and_validation_errors() -> None:
    for code in ("ASR_RATE_LIMITED", "LLM_NETWORK_ERROR", "WORKER_EXECUTION_INTERRUPTED"):
        assert is_automatically_recoverable_failure(code)
    for code in (
        "LLM_AUTHENTICATION_FAILED",
        "LLM_MODEL_UNAVAILABLE",
        "ASR_EMPTY_TRANSCRIPT",
        "SUMMARY_OUTPUT_INVALID",
        "TRANSCRIPT_SOURCE_MISMATCH",
        "ANALYSIS_INTERNAL_ERROR",
    ):
        assert not is_automatically_recoverable_failure(code)


def test_automatic_recovery_budget_does_not_infer_count_from_attempt_number() -> None:
    assert automatic_recovery_budget_available(0, 2)
    assert automatic_recovery_budget_available(1, 2)
    assert not automatic_recovery_budget_available(2, 2)
    assert not automatic_recovery_budget_available(None, 2)


def test_cooldown_transactions_serialize_and_never_shorten_the_shared_deadline(
    session_factory: sessionmaker[Session],
) -> None:
    identity = model_identity("siliconflow", "shared-account", "Qwen/Qwen3.5-4B")
    policy = CooldownPolicy(60, 900)

    def extend(retry_after: str) -> datetime:
        update = extend_shared_cooldown(
            session_factory,
            identity,
            now=NOW,
            response_received_at=NOW,
            retry_after=retry_after,
            random_fraction=0,
            policy=policy,
        )
        return update.cooldown_until

    with ThreadPoolExecutor(max_workers=2) as pool:
        deadlines = list(pool.map(extend, ("120", "5")))

    with session_factory() as session:
        row = session.scalar(
            select(SharedModelCooldown).where(
                SharedModelCooldown.provider == identity.provider,
                SharedModelCooldown.account_reference == identity.account_reference,
                SharedModelCooldown.model == identity.model,
            )
        )
        assert row is not None
        assert row.cooldown_until == NOW + timedelta(seconds=120)
        assert row.consecutive_backpressure_responses == 2
        session.commit()
    assert max(deadlines) == NOW + timedelta(seconds=120)

    later = NOW + timedelta(seconds=121)
    record_model_success(session_factory, identity, request_started_at=later)
    with session_factory() as session:
        row = session.scalar(select(SharedModelCooldown))
        assert row is not None
        assert row.cooldown_until == NOW + timedelta(seconds=120)
        assert row.consecutive_backpressure_responses == 0
        session.commit()
    assert model_identity("siliconflow", "another-account", "Qwen/Qwen3.5-4B") != identity


def test_persisted_wait_deadlines_keep_single_and_cumulative_budgets_separate() -> None:
    started = NOW - timedelta(seconds=60)
    task = AnalysisTask(
        model_wait_started_at=started,
        model_wait_single_deadline_at=started + timedelta(seconds=100),
        model_wait_total_seconds=30,
    )
    assert (
        wait_budget_error(
            task=task,
            now=NOW,
            cooldown_until=NOW + timedelta(seconds=10),
            single_limit_seconds=100,
            total_limit_seconds=120,
        )
        is None
    )
    assert (
        wait_budget_error(
            task=task,
            now=NOW,
            cooldown_until=started + timedelta(seconds=101),
            single_limit_seconds=100,
            total_limit_seconds=120,
        )
        == "MODEL_WAIT_SINGLE_LIMIT_EXCEEDED"
    )
    assert (
        wait_budget_error(
            task=task,
            now=NOW + timedelta(seconds=1),
            cooldown_until=NOW + timedelta(seconds=10),
            single_limit_seconds=100,
            total_limit_seconds=99,
        )
        == "MODEL_WAIT_TOTAL_LIMIT_EXCEEDED"
    )


@pytest.mark.parametrize(
    ("status", "asr_code", "llm_code"),
    [
        (429, "ASR_RATE_LIMITED", "LLM_RATE_LIMITED"),
        (503, "ASR_MODEL_OVERLOADED", "LLM_MODEL_OVERLOADED"),
    ],
)
def test_asr_and_llm_subprocesses_preserve_only_sanitized_backpressure_metadata(
    tmp_path: Path, status: int, asr_code: str, llm_code: str
) -> None:
    response_body = b"private supplier response must not cross this boundary"

    class RateLimitHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(content_length)
            self.send_response(status)
            self.send_header("Retry-After", "7")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, _format: str, *args: object) -> None:
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), RateLimitHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    environment["SILICONFLOW_API_KEY"] = "private-test-key-must-not-cross-boundary"
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"test wav bytes")
    asr_output = tmp_path / "asr-result.json"
    llm_request = tmp_path / "llm-request.json"
    llm_output = tmp_path / "llm-result.json"
    llm_request.write_text(
        json.dumps(
            {
                "base_url": base_url,
                "model": "Qwen/Qwen3.5-4B",
                "messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "user"}],
                "max_output_tokens": 128,
                "temperature": 0.2,
                "enable_thinking": False,
                "max_response_bytes": 4096,
                "timeouts": {"total": 3, "connect": 1, "read": 1, "write": 1, "pool": 1},
            }
        ),
        encoding="utf-8",
    )
    try:
        asr = subprocess.run(
            [
                sys.executable,
                "-m",
                "videoclip.asr_client",
                "--audio",
                str(audio),
                "--output",
                str(asr_output),
                "--base-url",
                base_url,
                "--model",
                "FunAudioLLM/SenseVoiceSmall",
                "--total-timeout",
                "3",
                "--connect-timeout",
                "1",
                "--read-timeout",
                "1",
                "--maximum-response-bytes",
                "4096",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        llm = subprocess.run(
            [
                sys.executable,
                "-m",
                "videoclip.llm_client",
                "--request",
                str(llm_request),
                "--output",
                str(llm_output),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert asr.returncode == llm.returncode == 0
    asr_result = json.loads(asr_output.read_text(encoding="utf-8"))
    llm_result = json.loads(llm_output.read_text(encoding="utf-8"))
    for result, error_code in ((asr_result, asr_code), (llm_result, llm_code)):
        assert set(result) == {"error_code", "retry_after", "response_received_at"}
        assert result["error_code"] == error_code
        assert result["retry_after"] == "7"
        assert datetime.fromisoformat(result["response_received_at"]).tzinfo is not None
        assert "private-test-key" not in json.dumps(result)
        assert response_body.decode() not in json.dumps(result)
    assert not asr.stdout and not asr.stderr and not llm.stdout and not llm.stderr
