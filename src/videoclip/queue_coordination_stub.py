from __future__ import annotations

import argparse
import json
import ssl
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

TRANSCRIPT = "课程介绍说明目标。模块设计解释职责。安全执行保证单分析执行门禁。"
SUMMARY = {
    "chapters": [
        {"title": "课程介绍", "evidence": [{"segment_id": 0}]},
        {"title": "模块设计", "evidence": [{"segment_id": 0}]},
        {"title": "安全执行", "evidence": [{"segment_id": 0}]},
    ],
    "key_points": [
        {"text": "课程介绍", "evidence": [{"segment_id": 0}]},
        {"text": "模块设计", "evidence": [{"segment_id": 0}]},
        {"text": "安全执行", "evidence": [{"segment_id": 0}]},
    ],
}


class StubState:
    def __init__(self, event_path: Path, hold_first_asr: bool) -> None:
        self.event_path = event_path
        self.hold_first_asr = hold_first_asr
        self.release = threading.Event()
        self.first_asr_entered = threading.Event()
        self.lock = threading.Lock()
        self.asr_requests = 0
        self.llm_requests = 0
        self.blocked_asr = 0
        self.plans: dict[str, list[dict[str, Any]]] = {"asr": [], "llm": []}

    def record(self, event: str, **values: Any) -> None:
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_path.open("a", encoding="utf-8") as output:
            output.write(
                json.dumps(
                    {"at": datetime.now(UTC).isoformat(), "event": event, **values},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "asr_requests": self.asr_requests,
                "llm_requests": self.llm_requests,
                "blocked_asr": self.blocked_asr,
                "first_asr_entered": self.first_asr_entered.is_set(),
                "released": self.release.is_set(),
            }

    def next_response(self, kind: str) -> dict[str, Any]:
        with self.lock:
            queue = self.plans[kind]
            return (
                queue.pop(0)
                if queue
                else {"status": 200, "delay_seconds": 0, "retry_after": None, "hold": False}
            )

    def set_plan(self, value: object) -> bool:
        if not isinstance(value, dict):
            return False
        plan: dict[str, list[dict[str, Any]]] = {}
        for kind in ("asr", "llm"):
            entries = value.get(kind, [])
            if not isinstance(entries, list):
                return False
            normalized: list[dict[str, Any]] = []
            for entry in entries:
                item = {"status": entry} if type(entry) is int else entry
                if (
                    not isinstance(item, dict)
                    or type(item.get("status")) is not int
                    or not 100 <= item["status"] <= 599
                ):
                    return False
                delay = item.get("delay_seconds", 0)
                retry_after = item.get("retry_after")
                hold = item.get("hold", False)
                if not isinstance(delay, int | float) or isinstance(delay, bool) or delay < 0 or delay > 600:
                    return False
                if type(hold) is not bool:
                    return False
                if retry_after is not None and (
                    not isinstance(retry_after, str)
                    or len(retry_after) > 128
                    or any(ord(character) < 32 or ord(character) > 126 for character in retry_after)
                ):
                    return False
                normalized.append(
                    {
                        "status": item["status"],
                        "delay_seconds": delay,
                        "retry_after": retry_after,
                        "hold": hold,
                    }
                )
            plan[kind] = normalized
        with self.lock:
            self.plans = plan
        return True


def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _handler_for(state: StubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: object) -> None:
            del args

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/health":
                _send_json(self, {"status": "ok"})
            elif path == "/__control/state":
                _send_json(self, state.snapshot())
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == "/__control/release":
                state.release.set()
                state.record("asr_gate_released")
                _send_json(self, {"released": True})
                return
            if path == "/__control/plan":
                content_length = self.headers.get("Content-Length", "0")
                if not content_length.isdecimal() or int(content_length) > 65_536:
                    self.send_error(413)
                    return
                try:
                    plan = json.loads(self.rfile.read(int(content_length)))
                except json.JSONDecodeError:
                    self.send_error(400)
                    return
                accepted = state.set_plan(plan)
                _send_json(self, {"accepted": accepted})
                return
            content_length = self.headers.get("Content-Length", "0")
            if not content_length.isdecimal() or int(content_length) > 8_388_608:
                self.send_error(413)
                return
            body = self.rfile.read(int(content_length))
            if path.endswith("/audio/transcriptions"):
                with state.lock:
                    state.asr_requests += 1
                    request_number = state.asr_requests
                    should_hold = state.hold_first_asr and request_number == 1
                    if should_hold:
                        state.blocked_asr += 1
                state.record("asr_request", request=request_number, bytes=len(body))
                if should_hold:
                    state.first_asr_entered.set()
                    state.release.wait(timeout=600)
                response = state.next_response("asr")
                if response.get("hold") is True:
                    state.first_asr_entered.set()
                    state.blocked_asr += 1
                    state.record("asr_gate_held", request=request_number)
                    state.release.wait(timeout=600)
                time.sleep(float(response["delay_seconds"]))
                status = int(response["status"])
                state.record("asr_response", request=request_number, status=status)
                if status >= 400:
                    self._send(
                        status, {"error": {"message": "controlled local substitute failure"}}, response
                    )
                else:
                    self._send(status, {"text": TRANSCRIPT}, response)
                return
            if path.endswith("/chat/completions"):
                try:
                    request = json.loads(body)
                except json.JSONDecodeError:
                    self.send_error(400)
                    return
                if not isinstance(request, dict) or not isinstance(request.get("model"), str):
                    self.send_error(400)
                    return
                with state.lock:
                    state.llm_requests += 1
                    request_number = state.llm_requests
                state.record("llm_request", request=request_number, bytes=len(body))
                response = state.next_response("llm")
                time.sleep(float(response["delay_seconds"]))
                status = int(response["status"])
                state.record("llm_response", request=request_number, status=status)
                if status >= 400:
                    self._send(
                        status, {"error": {"message": "controlled local substitute failure"}}, response
                    )
                    return
                payload = {
                    "model": request["model"],
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(SUMMARY, ensure_ascii=False, separators=(",", ":")),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
                self._send(status, payload, response)
                return
            self.send_error(404)

        def _send(self, status: int, payload: dict[str, Any], plan: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            retry_after = plan.get("retry_after")
            if status == 429 and isinstance(retry_after, str):
                self.send_header("Retry-After", retry_after)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Local TLS-only model substitute for Stage 4 acceptance.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9443)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--hold-first-asr", action="store_true")
    arguments = parser.parse_args()

    state = StubState(arguments.event_log, arguments.hold_first_asr)
    server = ThreadingHTTPServer((arguments.host, arguments.port), _handler_for(state))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(arguments.certificate), str(arguments.private_key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    state.record(
        "stub_started", host=arguments.host, port=arguments.port, hold_first_asr=arguments.hold_first_asr
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
