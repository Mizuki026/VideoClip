from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
VIDEO = ROOT / "video" / "agent-skill介绍.mp4"
API_ORIGIN = "http://127.0.0.1:58002"
WEB_ORIGIN = "http://127.0.0.1:5174"
EXPECTED_BYTES = 3_221_243_133
EXPECTED_SHA256 = "e08fdf8772a508ce8811f46f28f330ec493ba50d000977c8f10e273fb54ff8b3"
CHUNK_SIZE = 8 * 1024 * 1024
POSTGRES_CONTAINER = "videoclip-mvp-stage2-postgres-1"


def require(response: httpx.Response, status_code: int, code: str | None = None) -> dict[str, Any]:
    if response.status_code != status_code:
        detail = response.json().get("detail")
        error_code = detail.get("code") if isinstance(detail, dict) else None
        raise RuntimeError(f"HTTP status {response.status_code}; error_code={error_code}")
    payload: dict[str, Any] = {}
    if response.content:
        payload = response.json()
    if code is not None:
        detail = payload.get("detail")
        actual = detail.get("code") if isinstance(detail, dict) else None
        if actual != code:
            raise RuntimeError(f"unexpected stable error code: {actual}")
    return payload


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def remove_test_user(user_id: str) -> None:
    parsed = uuid.UUID(user_id)
    if str(parsed) != user_id.lower():
        raise RuntimeError("test account identifier was not a UUID")
    sql = (
        "DELETE FROM users AS u "
        f"WHERE u.id = '{parsed}' "
        "AND NOT EXISTS (SELECT 1 FROM videos WHERE owner_id = u.id) "
        "AND NOT EXISTS (SELECT 1 FROM upload_handoffs WHERE owner_id = u.id)"
    )
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"FINAL_STAGE1_CLEANUP_SQL={sql}",
            POSTGRES_CONTAINER,
            "sh",
            "-lc",
            'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "$FINAL_STAGE1_CLEANUP_SQL"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("test account cleanup failed")
    verify_sql = f"SELECT count(*) FROM users WHERE id = '{parsed}'"
    verify = subprocess.run(
        [
            "docker",
            "exec",
            POSTGRES_CONTAINER,
            "sh",
            "-lc",
            f'psql -Atq -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "{verify_sql}"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0 or verify.stdout.strip() != "0":
        raise RuntimeError("test account cleanup could not be verified")


def remove_test_upload(upload_id: str, user_id: str) -> None:
    upload_uuid = uuid.UUID(upload_id)
    owner_uuid = uuid.UUID(user_id)
    if str(upload_uuid) != upload_id.lower() or str(owner_uuid) != user_id.lower():
        raise RuntimeError("test upload cleanup identifiers were not UUIDs")
    sql = (
        "DELETE FROM upload_handoffs AS u "
        f"WHERE u.id = '{upload_uuid}' AND u.owner_id = '{owner_uuid}' "
        "AND u.protocol_type = 'resumable' AND u.state = 'complete' "
        "AND NOT EXISTS (SELECT 1 FROM videos WHERE upload_id = u.id)"
    )
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-e",
            f"FINAL_STAGE1_CLEANUP_SQL={sql}",
            POSTGRES_CONTAINER,
            "sh",
            "-lc",
            'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "$FINAL_STAGE1_CLEANUP_SQL"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("test upload cleanup failed")
    verify_sql = f"SELECT count(*) FROM upload_handoffs WHERE id = '{upload_uuid}'"
    verify = subprocess.run(
        [
            "docker",
            "exec",
            POSTGRES_CONTAINER,
            "sh",
            "-lc",
            f'psql -Atq -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "{verify_sql}"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0 or verify.stdout.strip() != "0":
        raise RuntimeError("test upload cleanup could not be verified")


def remove_video(client: httpx.Client, video_id: str, headers: dict[str, str]) -> None:
    for _ in range(8):
        response = client.delete(f"/videos/{video_id}", headers=headers)
        if response.status_code in {204, 404}:
            return
        if response.status_code != 202:
            detail = response.json().get("detail")
            code = detail.get("code") if isinstance(detail, dict) else None
            raise RuntimeError(f"video cleanup returned HTTP {response.status_code}; error_code={code}")
        time.sleep(0.5)
    raise RuntimeError("video cleanup remained pending")


def main() -> None:
    if VIDEO.stat().st_size != EXPECTED_BYTES or sha256_file(VIDEO) != EXPECTED_SHA256:
        raise RuntimeError("authorized sample size or SHA-256 differs from Stage 0 record")

    username = f"s1{uuid.uuid4().hex[:16]}"
    password = f"S1-{uuid.uuid4().hex}-{uuid.uuid4().hex}!"
    user_id: str | None = None
    upload_id: str | None = None
    video_id: str | None = None
    video_removed = False
    upload_removed = False
    user_removed = False
    chunks_uploaded = 0
    confirmed_after_interrupt = 0
    client = httpx.Client(
        base_url=API_ORIGIN,
        timeout=httpx.Timeout(120.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
    )

    def mutation_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Origin": WEB_ORIGIN, "X-CSRF-Token": csrf_token}
        headers.update(extra or {})
        return headers

    try:
        csrf_response = require(client.get("/auth/csrf"), 200)
        csrf_token = csrf_response["csrf_token"]
        created_user = require(
            client.post(
                "/auth/register",
                json={"username": username, "password": password},
                headers=mutation_headers(),
            ),
            201,
        )
        user_id = created_user["id"]
        require(
            client.post(
                "/auth/login",
                json={"username": username, "password": password},
                headers=mutation_headers(),
            ),
            200,
        )
        csrf_token = require(client.get("/auth/csrf"), 200)["csrf_token"]

        created_upload = require(
            client.post(
                "/uploads",
                json={
                    "file_name": VIDEO.name,
                    "total_bytes": EXPECTED_BYTES,
                    "sha256": EXPECTED_SHA256,
                },
                headers=mutation_headers({"Idempotency-Key": uuid.uuid4().hex}),
            ),
            201,
        )
        upload_id = created_upload["upload_id"]
        if created_upload["chunk_size"] != CHUNK_SIZE:
            raise RuntimeError("server returned an unexpected chunk size")

        with VIDEO.open("rb") as source:
            first_chunk = source.read(CHUNK_SIZE)
            first_digest = hashlib.sha256(first_chunk).hexdigest()
            first_response = require(
                client.put(
                    f"/uploads/{upload_id}/chunks/0",
                    content=first_chunk,
                    headers=mutation_headers(
                        {
                            "Content-Type": "application/octet-stream",
                            "X-Chunk-SHA256": first_digest,
                        }
                    ),
                ),
                200,
            )
            chunks_uploaded = 1
            if first_response["confirmed_offset"] != CHUNK_SIZE:
                raise RuntimeError("first server-confirmed offset did not match one full chunk")

            second_expected = min(CHUNK_SIZE, EXPECTED_BYTES - CHUNK_SIZE)
            interrupted_payload = source.read(min(1024 * 1024, second_expected))
            cookie_header = "; ".join(
                f"{cookie.name}={cookie.value}" for cookie in client.cookies.jar
            )
            connection = http.client.HTTPConnection("127.0.0.1", 58002, timeout=15)
            try:
                connection.putrequest("PUT", f"/uploads/{upload_id}/chunks/1")
                connection.putheader("Origin", WEB_ORIGIN)
                connection.putheader("X-CSRF-Token", csrf_token)
                connection.putheader("Cookie", cookie_header)
                connection.putheader("Content-Type", "application/octet-stream")
                connection.putheader(
                    "X-Chunk-SHA256", hashlib.sha256(interrupted_payload).hexdigest()
                )
                connection.putheader("Content-Length", str(second_expected))
                connection.putheader("Connection", "close")
                connection.endheaders()
                connection.send(interrupted_payload)
            finally:
                connection.close()

            status: dict[str, Any] | None = None
            for _ in range(60):
                response = client.get(f"/uploads/{upload_id}")
                if response.status_code == 200:
                    status = response.json()
                    break
                if response.status_code != 409:
                    require(response, 200)
                time.sleep(0.25)
            if status is None:
                raise RuntimeError("upload lease did not clear after the injected interruption")
            confirmed_after_interrupt = status["confirmed_offset"]
            if confirmed_after_interrupt != CHUNK_SIZE or status["next_chunk_index"] != 1:
                raise RuntimeError("server confirmation advanced or regressed across interruption")

            source.seek(confirmed_after_interrupt)
            offset = confirmed_after_interrupt
            index = status["next_chunk_index"]
            while offset < EXPECTED_BYTES:
                chunk = source.read(min(CHUNK_SIZE, EXPECTED_BYTES - offset))
                if not chunk:
                    raise RuntimeError("sample ended before declared byte count")
                response_data = require(
                    client.put(
                        f"/uploads/{upload_id}/chunks/{index}",
                        content=chunk,
                        headers=mutation_headers(
                            {
                                "Content-Type": "application/octet-stream",
                                "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                            }
                        ),
                    ),
                    200,
                )
                offset += len(chunk)
                if response_data["confirmed_offset"] != offset:
                    raise RuntimeError("server-confirmed offset differs from uploaded prefix")
                index += 1
                chunks_uploaded += 1
                if chunks_uploaded % 64 == 0:
                    print(f"confirmed_chunks={chunks_uploaded}")

        completion = require(
            client.post(f"/uploads/{upload_id}/complete", headers=mutation_headers()), 200
        )
        video = completion["video"]
        video_id = video["id"]
        if video["byte_count"] != EXPECTED_BYTES or video["content_sha256"] != EXPECTED_SHA256:
            raise RuntimeError("registered video metadata differs from the source sample")
        repeated = require(
            client.post(f"/uploads/{upload_id}/complete", headers=mutation_headers()), 200
        )
        if repeated["video"]["id"] != video_id:
            raise RuntimeError("repeated completion returned a different video")

        detail = require(client.get(f"/videos/{video_id}"), 200)
        if detail["id"] != video_id or detail["byte_count"] != EXPECTED_BYTES:
            raise RuntimeError("video query did not return the completed upload")
        head = client.head(f"/videos/{video_id}/content")
        if head.status_code != 200 or head.headers.get("content-length") != str(EXPECTED_BYTES):
            raise RuntimeError("authenticated media HEAD did not match registered byte count")
        for start in (0, EXPECTED_BYTES // 2, EXPECTED_BYTES - 64):
            end = min(start + 63, EXPECTED_BYTES - 1)
            ranged = client.get(
                f"/videos/{video_id}/content", headers={"Range": f"bytes={start}-{end}"}
            )
            if ranged.status_code != 206:
                raise RuntimeError("authenticated media range read failed")
            with VIDEO.open("rb") as source:
                source.seek(start)
                expected = source.read(end - start + 1)
            if ranged.content != expected:
                raise RuntimeError("authenticated media range differed from source bytes")

        remove_video(client, video_id, mutation_headers())
        video_removed = True
        if client.get(f"/videos/{video_id}").status_code != 404:
            raise RuntimeError("new video remained queryable after cleanup")
        if client.get(f"/videos/{video_id}/content").status_code != 404:
            raise RuntimeError("new video media remained readable after cleanup")
        remove_test_upload(upload_id, user_id)
        upload_removed = True
        remove_test_user(user_id)
        user_removed = True
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "source_bytes": EXPECTED_BYTES,
                    "source_sha256_matches_stage0": True,
                    "chunk_size": CHUNK_SIZE,
                    "confirmed_chunks": chunks_uploaded,
                    "confirmed_offset_after_interrupt": confirmed_after_interrupt,
                    "duplicate_completion_same_video": True,
                    "video_query_and_authenticated_ranges": "PASS",
                    "video_removed": video_removed,
                    "test_account_removed": user_removed,
                    "analysis_submitted": False,
                },
                ensure_ascii=False,
            )
        )
    finally:
        if video_id is not None and not video_removed:
            try:
                remove_video(client, video_id, mutation_headers())
                video_removed = True
            except Exception:
                pass
        elif upload_id is not None and video_id is None:
            try:
                result = client.delete(f"/uploads/{upload_id}", headers=mutation_headers())
                if result.status_code == 409:
                    status = client.get(f"/uploads/{upload_id}")
                    if status.status_code == 200 and status.json().get("video_id"):
                        video_id = str(status.json()["video_id"])
                        remove_video(client, video_id, mutation_headers())
                        video_removed = True
            except Exception:
                pass
        if upload_id is not None and user_id is not None and video_removed and not upload_removed:
            try:
                remove_test_upload(upload_id, user_id)
                upload_removed = True
            except Exception:
                pass
        if user_id is not None and not user_removed:
            try:
                remove_test_user(user_id)
            except Exception:
                pass
        client.close()


if __name__ == "__main__":
    main()
