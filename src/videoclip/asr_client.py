from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


class AsrCallError(RuntimeError):
    def __init__(
        self, code: str, *, retry_after: str | None = None, response_received_at: str | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.response_received_at = response_received_at


def _read_response(response: httpx.Response, maximum: int, deadline: float) -> bytes:
    length_header = response.headers.get("content-length")
    if length_header and length_header.isdecimal() and int(length_header) > maximum:
        raise AsrCallError("ASR_RESPONSE_TOO_LARGE")
    body = bytearray()
    for chunk in response.iter_bytes():
        if time.monotonic() >= deadline:
            raise AsrCallError("ASR_TIMEOUT")
        body.extend(chunk)
        if len(body) > maximum:
            raise AsrCallError("ASR_RESPONSE_TOO_LARGE")
    return bytes(body)


def _status_code(status: int) -> str:
    if status >= 500:
        return {
            503: "ASR_MODEL_OVERLOADED",
            504: "ASR_UPSTREAM_TIMEOUT",
        }.get(status, "ASR_UPSTREAM_ERROR")
    return {
        400: "ASR_REQUEST_REJECTED",
        401: "ASR_AUTHENTICATION_FAILED",
        403: "ASR_ACCESS_FORBIDDEN",
        404: "ASR_MODEL_UNAVAILABLE",
        408: "ASR_UPSTREAM_TIMEOUT",
        429: "ASR_RATE_LIMITED",
    }.get(status, "ASR_REQUEST_REJECTED")


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


# 音频请求受阶段超时和总期限约束，响应体也按字节上限流式读取。
def transcribe(
    audio_path: Path,
    *,
    base_url: str,
    model: str,
    total_timeout: float,
    connect_timeout: float,
    read_timeout: float,
    maximum_response_bytes: int,
    parameters: dict[str, str],
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, dict[str, Any]]:
    token = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not token:
        raise AsrCallError("ASR_SECRET_UNAVAILABLE")
    started = time.monotonic()
    deadline = started + total_timeout
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=read_timeout,
        pool=connect_timeout,
    )
    try:
        with httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(max_connections=1),
            transport=transport,
        ) as client:
            with audio_path.open("rb") as audio_file:
                with client.stream(
                    "POST",
                    f"{base_url}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"model": model, **parameters},
                    files={"file": (audio_path.name, audio_file, "audio/wav")},
                ) as response:
                    if response.status_code >= 400:
                        code = _status_code(response.status_code)
                        if response.status_code in {429, 503}:
                            raise AsrCallError(
                                code,
                                retry_after=_retry_after_header(response),
                                response_received_at=datetime.now(UTC).isoformat(),
                            )
                        raise AsrCallError(code)
                    body = _read_response(response, maximum_response_bytes, deadline)
                    try:
                        document = json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise AsrCallError("ASR_INVALID_RESPONSE") from exc
                    text = document.get("text") if isinstance(document, dict) else None
                    if not isinstance(text, str) or not text.strip():
                        raise AsrCallError("ASR_EMPTY_TRANSCRIPT")
                    return text, {
                        "model": model,
                        "http_status": response.status_code,
                        "trace_id": response.headers.get("x-siliconcloud-trace-id", "")[:128],
                        "response_bytes": len(body),
                        "text_characters": len(text),
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    }
    except httpx.TimeoutException as exc:
        raise AsrCallError("ASR_TIMEOUT") from exc
    except httpx.RequestError as exc:
        raise AsrCallError("ASR_TRANSPORT_ERROR") from exc


def _write_result(path: Path, result: dict[str, Any], maximum: int) -> None:
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > maximum:
        raise AsrCallError("ASR_RESPONSE_TOO_LARGE")
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--total-timeout", type=float, required=True)
    parser.add_argument("--connect-timeout", type=float, required=True)
    parser.add_argument("--read-timeout", type=float, required=True)
    parser.add_argument("--maximum-response-bytes", type=int, required=True)
    parser.add_argument("--parameters-json", default="{}")
    arguments = parser.parse_args()
    try:
        parameters = json.loads(arguments.parameters_json)
        if not isinstance(parameters, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in parameters.items()
        ):
            raise AsrCallError("ASR_CONFIGURATION_INVALID")
        text, metadata = transcribe(
            arguments.audio,
            base_url=arguments.base_url,
            model=arguments.model,
            total_timeout=arguments.total_timeout,
            connect_timeout=arguments.connect_timeout,
            read_timeout=arguments.read_timeout,
            maximum_response_bytes=arguments.maximum_response_bytes,
            parameters=parameters,
        )
        _write_result(
            arguments.output,
            {"text": text, "metadata": metadata},
            arguments.maximum_response_bytes,
        )
        return 0
    except AsrCallError as exc:
        try:
            error_payload: dict[str, Any] = {"error_code": exc.code}
            if exc.code in {"ASR_RATE_LIMITED", "ASR_MODEL_OVERLOADED"}:
                if exc.retry_after is not None:
                    error_payload["retry_after"] = exc.retry_after
                if exc.response_received_at is not None:
                    error_payload["response_received_at"] = exc.response_received_at
            _write_result(arguments.output, error_payload, arguments.maximum_response_bytes)
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
