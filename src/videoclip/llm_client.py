from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from videoclip.summarization import SUMMARY_RESPONSE_JSON_SCHEMA


class LlmCallError(RuntimeError):
    def __init__(
        self, code: str, *, retry_after: str | None = None, response_received_at: str | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.response_received_at = response_received_at


def _status_code(status: int) -> str:
    if status == 400:
        return "LLM_MODEL_INCOMPATIBLE"
    if status == 401:
        return "LLM_AUTHENTICATION_FAILED"
    if status == 403:
        return "LLM_ACCESS_FORBIDDEN"
    if status == 404:
        return "LLM_MODEL_UNAVAILABLE"
    if status == 408 or status == 504:
        return "LLM_UPSTREAM_TIMEOUT"
    if status == 429:
        return "LLM_RATE_LIMITED"
    if status == 503:
        return "LLM_MODEL_OVERLOADED"
    if status >= 500:
        return "LLM_UPSTREAM_ERROR"
    return "LLM_REQUEST_REJECTED"


def _retry_after_header(response: httpx.Response) -> str | None:
    value = response.headers.get("retry-after")
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
    ):
        return None
    return value.strip()


def _read_response(response: httpx.Response, maximum: int, deadline: float) -> bytes:
    length_header = response.headers.get("content-length")
    if length_header and length_header.isdecimal() and int(length_header) > maximum:
        raise LlmCallError("LLM_RESPONSE_TOO_LARGE")
    body = bytearray()
    for chunk in response.iter_bytes():
        if time.monotonic() >= deadline:
            raise LlmCallError("LLM_TOTAL_TIMEOUT")
        body.extend(chunk)
        if len(body) > maximum:
            raise LlmCallError("LLM_RESPONSE_TOO_LARGE")
    return bytes(body)


def _safe_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        item = value.get(key)
        if type(item) is int and item >= 0:
            result[key] = item
    return result


def _trace_id(response: httpx.Response) -> str | None:
    value = response.headers.get("x-siliconcloud-trace-id", "")[:128]
    safe = re.sub(r"[^A-Za-z0-9_-]", "", value)
    return safe or None


# 总请求期限覆盖完整响应读取，避免慢速分块绕过单次读取超时。
def request_completion(
    request: dict[str, Any],
    *,
    api_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    token = (api_key if api_key is not None else os.environ.get("SILICONFLOW_API_KEY", "")).strip()
    if not token:
        raise LlmCallError("LLM_SECRET_UNAVAILABLE")
    messages = request.get("messages")
    timeouts = request.get("timeouts")
    maximum = request.get("max_response_bytes")
    if (
        not isinstance(messages, list)
        or len(messages) != 2
        or not all(isinstance(item, dict) and isinstance(item.get("content"), str) for item in messages)
        or not isinstance(timeouts, dict)
        or type(maximum) is not int
        or maximum <= 0
    ):
        raise LlmCallError("LLM_CONFIGURATION_INVALID")
    try:
        total_timeout = float(timeouts["total"])
        timeout = httpx.Timeout(
            connect=float(timeouts["connect"]),
            read=float(timeouts["read"]),
            write=float(timeouts["write"]),
            pool=float(timeouts["pool"]),
        )
        model = str(request["model"])
        base_url = str(request["base_url"])
        max_tokens = int(request["max_output_tokens"])
        temperature = float(request["temperature"])
        enable_thinking = request["enable_thinking"]
    except (KeyError, TypeError, ValueError) as exc:
        raise LlmCallError("LLM_CONFIGURATION_INVALID") from exc
    if total_timeout <= 0 or max_tokens <= 0 or type(enable_thinking) is not bool:
        raise LlmCallError("LLM_CONFIGURATION_INVALID")
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "enable_thinking": enable_thinking,
        "response_format": {"type": "json_schema", "json_schema": SUMMARY_RESPONSE_JSON_SCHEMA},
        "stream": False,
    }
    started = time.monotonic()
    deadline = started + total_timeout
    try:
        with httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(max_connections=1),
            transport=transport,
        ) as client:
            with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    code = _status_code(response.status_code)
                    if response.status_code in {429, 503}:
                        raise LlmCallError(
                            code,
                            retry_after=_retry_after_header(response),
                            response_received_at=datetime.now(UTC).isoformat(),
                        )
                    raise LlmCallError(code)
                body = _read_response(response, maximum, deadline)
                try:
                    document = json.loads(body)
                    choice = document["choices"][0]
                    content = choice["message"]["content"]
                except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                    raise LlmCallError("LLM_INVALID_RESPONSE") from exc
                if not isinstance(document, dict) or not isinstance(content, str):
                    raise LlmCallError("LLM_INVALID_RESPONSE")
                response_model = document.get("model")
                if response_model not in (None, model):
                    raise LlmCallError("LLM_MODEL_MISMATCH")
                finish_reason = choice.get("finish_reason")
                if finish_reason is not None and not isinstance(finish_reason, str):
                    raise LlmCallError("LLM_INVALID_RESPONSE")
                return {
                    "content": content,
                    "metadata": {
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "model": response_model or model,
                        "http_status": response.status_code,
                        "trace_id": _trace_id(response),
                        "response_bytes": len(body),
                        "finish_reason": finish_reason[:64] if finish_reason else None,
                        "usage": _safe_usage(document.get("usage")),
                    },
                }
    except TimeoutError as exc:
        raise LlmCallError("LLM_TOTAL_TIMEOUT") from exc
    except httpx.TimeoutException as exc:
        raise LlmCallError("LLM_PHASE_TIMEOUT") from exc
    except httpx.RequestError as exc:
        raise LlmCallError("LLM_NETWORK_ERROR") from exc


def _write_output(path: Path, result: dict[str, Any], maximum_bytes: int) -> None:
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > maximum_bytes:
        encoded = b'{"error_code":"LLM_RESPONSE_TOO_LARGE"}'
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    maximum_output = 1_060_864
    try:
        request = json.loads(arguments.request.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise LlmCallError("LLM_CONFIGURATION_INVALID")
        maximum = request.get("max_response_bytes")
        if type(maximum) is not int or maximum <= 0:
            raise LlmCallError("LLM_CONFIGURATION_INVALID")
        maximum_output = maximum + 8192
        result = request_completion(request)
    except LlmCallError as exc:
        result = {"error_code": exc.code}
        if exc.code in {"LLM_RATE_LIMITED", "LLM_MODEL_OVERLOADED"}:
            if exc.retry_after is not None:
                result["retry_after"] = exc.retry_after
            if exc.response_received_at is not None:
                result["response_received_at"] = exc.response_received_at
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        result = {"error_code": "LLM_CONFIGURATION_INVALID"}
    try:
        _write_output(arguments.output, result, maximum_output)
    except OSError:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
