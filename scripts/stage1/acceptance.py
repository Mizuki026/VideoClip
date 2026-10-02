from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "data" / "mvp-stage-1" / ".env"
COMPOSE = ROOT / "compose.yaml"
DEFAULT_ORIGIN = "http://127.0.0.1:58001"
EXPECTED_BYTES = 1_116_803_236


class Digest(Protocol):
    def update(self, data: bytes, /) -> None: ...


def chunks(path: Path, digest: Digest) -> Iterator[bytes]:
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
            yield block


def check(response: httpx.Response, expected: int, action: str) -> httpx.Response:
    if response.status_code != expected:
        raise RuntimeError(f"{action} returned HTTP {response.status_code}")
    return response


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Stage 1 authorized large-video HTTP acceptance.")
    parser.add_argument("sample", type=Path)
    args = parser.parse_args()
    sample = args.sample.resolve(strict=True)
    actual_bytes = sample.stat().st_size
    if actual_bytes != EXPECTED_BYTES:
        raise SystemExit(f"authorized sample size mismatch: expected {EXPECTED_BYTES}, got {actual_bytes}")
    if not ENV_FILE.is_file():
        raise SystemExit("Run scripts/stage1/prepare-private-data.ps1 before acceptance.")
    origin = str(dotenv_values(ENV_FILE).get("APP_ORIGIN") or DEFAULT_ORIGIN)

    username = "stage1-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    digest = hashlib.sha256()
    with httpx.Client(base_url=origin, timeout=httpx.Timeout(1800.0), follow_redirects=False) as client:
        csrf = check(client.get("/auth/csrf"), 200, "anonymous CSRF token").json()["csrf_token"]
        headers = {"Origin": origin, "X-CSRF-Token": csrf}
        check(
            client.post("/auth/register", json={"username": username, "password": password}, headers=headers),
            201,
            "registration",
        )
        check(
            client.post("/auth/login", json={"username": username, "password": password}, headers=headers),
            200,
            "login",
        )
        csrf = check(client.get("/auth/csrf"), 200, "authenticated CSRF token").json()["csrf_token"]
        upload_headers = {
            "Origin": origin,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/octet-stream",
            "X-Video-Filename": urllib.parse.quote(sample.name, safe=""),
        }
        upload_started = time.monotonic()
        uploaded = check(
            client.post("/videos", content=chunks(sample, digest), headers=upload_headers),
            201,
            "large raw upload",
        ).json()["video"]
        upload_seconds = round(time.monotonic() - upload_started, 2)
        video_id = uploaded["id"]
        if uploaded["byte_count"] != actual_bytes or uploaded["content_sha256"] != digest.hexdigest():
            raise RuntimeError("persisted upload size or SHA-256 does not match the source")
        if not 0 < uploaded["duration_seconds"] <= 1200:
            raise RuntimeError("uploaded media duration is outside the accepted interval")
        check(client.head(f"/videos/{video_id}/content"), 200, "authenticated HEAD")
        check(
            client.get(f"/videos/{video_id}/content", headers={"Range": "bytes=0-31"}),
            206,
            "authenticated range",
        )
        check(
            client.get(f"/videos/{video_id}/content", headers={"Range": "bytes=999999999999-"}),
            416,
            "invalid range",
        )
        check(client.get(f"/videos/{video_id}"), 200, "pre-restart detail")

        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ENV_FILE),
                "-f",
                str(COMPOSE),
                "restart",
                "api",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        _ = result
        deadline = time.monotonic() + 60
        while True:
            try:
                check(client.get("/health", timeout=3), 200, "API restart health")
                break
            except (httpx.HTTPError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("API did not return after restart") from None
                time.sleep(1)
        listing = check(client.get("/videos"), 200, "post-restart list").json()["items"]
        if not any(item["id"] == video_id for item in listing):
            raise RuntimeError("uploaded video was not listed after API restart")
        check(client.get(f"/videos/{video_id}"), 200, "post-restart detail")
        csrf = check(client.get("/auth/csrf"), 200, "delete CSRF token").json()["csrf_token"]
        check(
            client.delete(f"/videos/{video_id}", headers={"Origin": origin, "X-CSRF-Token": csrf}),
            204,
            "unprocessed video deletion",
        )
        check(client.get(f"/videos/{video_id}/content"), 404, "post-delete content denial")
        check(client.get(f"/videos/{video_id}"), 404, "post-delete detail denial")
        csrf = check(client.get("/auth/csrf"), 200, "logout CSRF token").json()["csrf_token"]
        check(client.post("/auth/logout", headers={"Origin": origin, "X-CSRF-Token": csrf}), 204, "logout")

    print(
        json.dumps(
            {
                "result": "passed",
                "bytes": actual_bytes,
                "sha256": digest.hexdigest(),
                "upload_seconds": upload_seconds,
                "api_restart_and_persistence": "passed",
                "head_range_and_delete": "passed",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
