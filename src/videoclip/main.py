from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from videoclip.analysis import (
    continue_summary_attempt,
    recover_analysis_attempt,
    submit_analysis_task,
    summary_response,
    task_response,
    transcript_response,
)
from videoclip.analysis_delivery import DeliveryPolicy, coordinate_analysis_delivery
from videoclip.celery_app import celery_app
from videoclip.db import check_database, create_database_engine, create_session_factory
from videoclip.models import AnalysisAttempt, AnalysisEvent, AnalysisExecution, SessionRecord, User, Video
from videoclip.recovery import recover_interrupted_work
from videoclip.resumable_uploads import (
    CHUNK_SIZE,
    abandon_upload_session,
    complete_upload_session,
    create_upload_session,
    get_upload_status,
    write_upload_chunk,
)
from videoclip.schemas import (
    AnalysisEventResponse,
    AnalysisEventsResponse,
    AnalysisSummaryResponse,
    AnalysisTaskResponse,
    CleanupRequest,
    CleanupResponse,
    CsrfResponse,
    LoginRequest,
    RecoveryRequest,
    RegisterRequest,
    ResumableUploadCreateRequest,
    ResumableUploadCreateResponse,
    ResumableUploadStatusResponse,
    TranscriptResponse,
    UploadChunkResponse,
    UploadChunkSummary,
    UploadResponse,
    UserResponse,
    VideoDeletionProgress,
    VideoDetailResponse,
    VideoListResponse,
    VideoMediaDeletionProgress,
    VideoSummary,
)
from videoclip.security import (
    PASSWORDS,
    hash_password,
    new_csrf_token,
    new_session_token,
    token_hash,
    validate_username,
    verify_csrf_token,
    verify_password,
)
from videoclip.services import (
    _attempt_stop_is_confirmed,
    db_transaction,
    delete_video,
    delete_video_media,
    receive_video_upload,
    register_account,
    retry_attempt_cleanup,
    retry_video_media_cleanup,
)
from videoclip.settings import Settings, get_settings
from videoclip.storage import MediaStore

logger = logging.getLogger("videoclip.api")
SESSION_COOKIE = "videoclip_session"
CSRF_COOKIE = "videoclip_csrf"


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: Iterator[bytes],
        source: BinaryIO,
        *,
        status_code: int,
        media_type: str,
        headers: dict[str, str],
    ) -> None:
        super().__init__(content, status_code=status_code, media_type=media_type, headers=headers)
        self._source = source

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._source.close()


class JsonBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.paths = {"/auth/register", "/auth/login"}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await self._too_large(send)
            return
        if content_length > self.max_bytes:
            await self._too_large(send)
            return
        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._too_large(send)
                return
            if not message.get("more_body", False):
                break
        if disconnected:
            return
        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.request", "body": b"", "more_body": False}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _too_large(send: Send) -> None:
        body = b'{"detail":"request body too large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"connection", b"close")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _current_auth(
    request: Request, factory: sessionmaker[Session], required: bool = True
) -> tuple[User, SessionRecord] | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token or len(token) > 128:
        if required:
            raise HTTPException(status_code=401, detail="authentication required")
        return None
    with factory() as session:
        record = session.scalar(
            select(SessionRecord).where(
                SessionRecord.token_hash == token_hash(token),
                SessionRecord.revoked_at.is_(None),
                SessionRecord.expires_at > datetime.now(UTC),
            )
        )
        user = session.get(User, record.user_id) if record is not None else None
        if record is None or user is None:
            if required:
                raise HTTPException(status_code=401, detail="authentication required")
            return None
        return user, record


def _require_csrf(
    request: Request,
    settings: Settings,
    factory: sessionmaker[Session],
    current: tuple[User, SessionRecord] | None,
) -> None:
    origin = request.headers.get("origin")
    header_token = request.headers.get("x-csrf-token", "")
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if (
        origin != settings.app_origin
        or not header_token
        or len(header_token) > 256
        or not hmac.compare_digest(header_token, cookie_token)
        or not verify_csrf_token(settings.app_secret_key.get_secret_value(), header_token)
    ):
        raise HTTPException(status_code=403, detail="origin or csrf validation failed")
    if current is not None:
        _, record = current
        if not hmac.compare_digest(
            hashlib.sha256(header_token.encode("ascii")).hexdigest(), record.csrf_hash
        ):
            raise HTTPException(status_code=403, detail="origin or csrf validation failed")


def _set_csrf_cookie(response: Response, token: str, settings: Settings, max_age: int) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=max_age,
        httponly=False,
        secure=settings.secure_cookie,
        samesite="strict",
        path="/",
    )


def _video_summary(video: Video) -> VideoSummary:
    return VideoSummary(
        id=video.id,
        display_name=video.display_name,
        byte_count=video.byte_count,
        content_sha256=video.content_sha256,
        duration_seconds=video.duration_seconds,
        media_info=video.media_info,
        status=video.status,
        deletion_error=video.deletion_error,
        media_status=video.media_status,
        media_error_code=video.media_error_code,
        created_at=video.created_at,
    )


def _video_detail(video: Video) -> VideoDetailResponse:
    return VideoDetailResponse(
        **_video_summary(video).model_dump(),
        analysis_task_exists=video.analysis_task_created_at is not None,
        analysis_task_id=video.current_analysis_task_id,
    )


def _parse_range(value: str, byte_count: int) -> tuple[int, int] | None:
    if byte_count <= 0 or not value.startswith("bytes=") or "," in value:
        return None
    item = value[6:]
    if item.count("-") != 1:
        return None
    first, last = item.split("-", 1)
    try:
        if not first:
            suffix_length = int(last)
            if suffix_length <= 0:
                return None
            start = max(0, byte_count - suffix_length)
            return start, byte_count - 1
        start = int(first)
        if start < 0 or start >= byte_count:
            return None
        end = byte_count - 1 if not last else int(last)
        if end < start:
            return None
        return start, min(end, byte_count - 1)
    except ValueError:
        return None


def _media_deletion_progress(session: Session, video: Video) -> VideoMediaDeletionProgress:
    if video.status != "ready":
        raise HTTPException(
            status_code=409,
            detail={"code": "VIDEO_DELETION_IN_PROGRESS", "deletion_eligible": False},
        )
    attempts: list[AnalysisAttempt] = []
    if video.current_analysis_task_id is not None:
        attempts = list(
            session.scalars(
                select(AnalysisAttempt)
                .where(AnalysisAttempt.task_id == video.current_analysis_task_id)
                .order_by(AnalysisAttempt.attempt_number)
            )
        )
    pending_attempt_ids: list[uuid.UUID] = []
    stop_confirmation_required = False
    for attempt in attempts:
        stopped = _attempt_stop_is_confirmed(session, attempt)
        if not stopped:
            stop_confirmation_required = True
        if not stopped or attempt.cleanup_state != "cleaned":
            pending_attempt_ids.append(attempt.id)
    return VideoMediaDeletionProgress(
        video_id=video.id,
        media_status=video.media_status,
        error_code=video.media_error_code,
        pending_attempt_ids=pending_attempt_ids,
        stop_confirmation_required=stop_confirmation_required,
    )


def create_app(
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    storage: MediaStore | None = None,
    task_publisher: Callable[[str, str], object] | None = None,
    *,
    run_recovery: bool = True,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    engine = None
    if session_factory is None:
        engine = create_database_engine(resolved_settings)
        resolved_factory = create_session_factory(engine)
    else:
        resolved_factory = session_factory
    resolved_store = storage or MediaStore(resolved_settings.app_data_root)

    def publish_task(task_id: str, attempt_id: str) -> object:
        if task_publisher is not None:
            return task_publisher(task_id, attempt_id)
        return celery_app.send_task("videoclip.transcribe_analysis", args=[task_id, attempt_id], retry=False)

    delivery_policy = DeliveryPolicy.from_settings(resolved_settings)

    def coordinate_delivery(task_id: uuid.UUID, attempt_id: uuid.UUID) -> None:
        coordinate_analysis_delivery(
            resolved_factory,
            task_id,
            attempt_id,
            publish_task,
            policy=delivery_policy,
        )

    async def finish_media_cleanup_request(
        owner_id: uuid.UUID,
        video_id: uuid.UUID,
        operation: Callable[[], bool],
        response: Response,
    ) -> VideoMediaDeletionProgress:
        cleanup_tasks: set[asyncio.Task[Any]] = request_cleanup_tasks
        cleanup_task = asyncio.create_task(asyncio.to_thread(operation))
        cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(cleanup_tasks.discard)
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup_task), timeout=resolved_settings.cleanup_timeout_seconds
            )
        except TimeoutError:
            logger.warning("video_media_cleanup_timeout video_id=%s", video_id.hex)
        with resolved_factory() as session:
            video = session.scalar(
                select(Video).where(Video.id == video_id, Video.owner_id == owner_id)
            )
            if video is None:
                raise HTTPException(status_code=404, detail="video not found")
            progress = _media_deletion_progress(session, video)
        response.status_code = 200 if progress.media_status == "deleted" else status.HTTP_202_ACCEPTED
        response.headers["Cache-Control"] = "no-store"
        return progress

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        resolved_store.prepare()
        check_database(resolved_factory.kw["bind"])
        if run_recovery:
            recover_interrupted_work(resolved_factory, resolved_settings, resolved_store)
        yield
        if engine is not None:
            engine.dispose()

    app = FastAPI(title="VideoClip MVP API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.session_factory = resolved_factory
    app.state.store = resolved_store
    request_cleanup_tasks: set[asyncio.Task[Any]] = set()
    app.state.cleanup_tasks = request_cleanup_tasks
    app.add_middleware(JsonBodyLimitMiddleware, max_bytes=resolved_settings.max_json_body_bytes)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, __: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/uploads"):
            return JSONResponse({"detail": {"code": "INVALID_UPLOAD_METADATA"}}, status_code=422)
        return JSONResponse({"detail": "invalid request"}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, __: Exception) -> JSONResponse:
        logger.error("request_failed")
        return JSONResponse({"detail": "request failed"}, status_code=500)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/auth/csrf", response_model=CsrfResponse)
    def csrf_token(response: Response, request: Request) -> CsrfResponse:
        current = _current_auth(request, resolved_factory, required=False)
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        secret = resolved_settings.app_secret_key.get_secret_value()
        valid = verify_csrf_token(secret, cookie_token)
        if current is not None:
            _, record = current
            valid = valid and hmac.compare_digest(
                hashlib.sha256(cookie_token.encode("ascii")).hexdigest(), record.csrf_hash
            )
        token = cookie_token if valid else new_csrf_token(secret)
        if current is not None and not valid:
            _, record = current
            with db_transaction(resolved_factory) as session:
                stored = session.get(SessionRecord, record.id, with_for_update=True)
                if stored is not None and stored.revoked_at is None:
                    stored.csrf_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        max_age = resolved_settings.session_ttl_hours * 3600 if current is not None else 3600
        _set_csrf_cookie(response, token, resolved_settings, max_age)
        response.headers["Cache-Control"] = "no-store"
        return CsrfResponse(csrf_token=token)

    @app.post("/auth/register", status_code=201, response_model=UserResponse)
    def register(payload: RegisterRequest, request: Request, response: Response) -> UserResponse:
        current = _current_auth(request, resolved_factory, required=False)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        user = register_account(resolved_factory, payload.username, payload.password)
        response.headers["Cache-Control"] = "no-store"
        return UserResponse(id=user.id, username=user.username)

    @app.post("/auth/login", response_model=UserResponse)
    def login(payload: LoginRequest, request: Request, response: Response) -> UserResponse:
        current = _current_auth(request, resolved_factory, required=False)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        try:
            username = validate_username(payload.username)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="invalid username or password") from exc
        with resolved_factory() as session:
            user = session.scalar(select(User).where(User.username == username))
            if user is None or not verify_password(user.password_hash, payload.password):
                raise HTTPException(status_code=401, detail="invalid username or password")
            user_id = user.id
            password_hash = user.password_hash
        if PASSWORDS.check_needs_rehash(password_hash):
            with db_transaction(resolved_factory) as session:
                user = session.get(User, user_id, with_for_update=True)
                if user is not None:
                    user.password_hash = hash_password(payload.password)

        raw_session = new_session_token()
        raw_csrf = new_csrf_token(resolved_settings.app_secret_key.get_secret_value())
        now = datetime.now(UTC)
        expires = now + timedelta(hours=resolved_settings.session_ttl_hours)
        with db_transaction(resolved_factory) as session:
            user = session.get(User, user_id, with_for_update=True)
            if user is None:
                raise HTTPException(status_code=401, detail="invalid username or password")
            session.query(SessionRecord).filter(
                SessionRecord.user_id == user_id,
                (SessionRecord.revoked_at.is_not(None)) | (SessionRecord.expires_at <= now),
            ).delete(synchronize_session=False)
            active = list(
                session.scalars(
                    select(SessionRecord)
                    .where(SessionRecord.user_id == user_id, SessionRecord.revoked_at.is_(None))
                    .order_by(SessionRecord.created_at.asc())
                    .with_for_update()
                )
            )
            for old in active:
                if old.expires_at <= now:
                    old.revoked_at = now
            active = [old for old in active if old.expires_at > now]
            while len(active) >= resolved_settings.max_sessions_per_user:
                active.pop(0).revoked_at = now
            session.add(
                SessionRecord(
                    user_id=user_id,
                    token_hash=token_hash(raw_session),
                    csrf_hash=hashlib.sha256(raw_csrf.encode("ascii")).hexdigest(),
                    expires_at=expires,
                )
            )
        response.set_cookie(
            SESSION_COOKIE,
            raw_session,
            max_age=resolved_settings.session_ttl_hours * 3600,
            httponly=True,
            secure=resolved_settings.secure_cookie,
            samesite=resolved_settings.cookie_samesite,
            path="/",
        )
        _set_csrf_cookie(response, raw_csrf, resolved_settings, resolved_settings.session_ttl_hours * 3600)
        response.headers["Cache-Control"] = "no-store"
        return UserResponse(id=user_id, username=username)

    @app.get("/auth/me", response_model=UserResponse)
    def me(request: Request) -> UserResponse:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        return UserResponse(id=user.id, username=user.username)

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request, response: Response) -> Response:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        _, record = current
        with db_transaction(resolved_factory) as session:
            stored = session.get(SessionRecord, record.id, with_for_update=True)
            if stored is not None and stored.revoked_at is None:
                stored.revoked_at = datetime.now(UTC)
        response.delete_cookie(
            SESSION_COOKIE, path="/", secure=resolved_settings.secure_cookie, httponly=True
        )
        response.delete_cookie(CSRF_COOKIE, path="/", secure=resolved_settings.secure_cookie)
        response.status_code = status.HTTP_204_NO_CONTENT
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/videos", status_code=201, response_model=UploadResponse)
    async def upload_video(request: Request) -> UploadResponse:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        video = await receive_video_upload(
            request, resolved_factory, resolved_settings, resolved_store, user.id
        )
        return UploadResponse(video=_video_detail(video))

    @app.post("/uploads", status_code=201, response_model=ResumableUploadCreateResponse)
    def create_resumable_upload(
        payload: ResumableUploadCreateRequest,
        request: Request,
        response: Response,
    ) -> ResumableUploadCreateResponse:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        result = create_upload_session(
            resolved_factory,
            user.id,
            payload.file_name,
            payload.total_bytes,
            payload.sha256,
            request.headers.get("idempotency-key", ""),
        )
        upload_id, state, total_bytes, confirmed_offset, expires_at, replayed = result
        response.status_code = 200 if replayed else 201
        response.headers["Cache-Control"] = "no-store"
        return ResumableUploadCreateResponse(
            upload_id=upload_id,
            state=state,
            chunk_size=CHUNK_SIZE,
            total_bytes=total_bytes,
            confirmed_offset=confirmed_offset,
            expires_at=expires_at,
            replayed=replayed,
        )

    @app.get("/uploads/{upload_id}", response_model=ResumableUploadStatusResponse)
    def resumable_upload_status(upload_id: uuid.UUID, request: Request) -> ResumableUploadStatusResponse:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        data = get_upload_status(resolved_factory, resolved_store, user.id, upload_id)
        return ResumableUploadStatusResponse(
            upload_id=data["upload_id"],
            state=data["state"],
            chunk_size=data["chunk_size"],
            total_bytes=data["total_bytes"],
            confirmed_offset=data["confirmed_offset"],
            next_chunk_index=data["next_chunk_index"],
            expires_at=data["expires_at"],
            chunks=[UploadChunkSummary(**chunk) for chunk in data["chunks"]],
            error_code=data["error_code"],
            video_id=data["video_id"],
        )

    @app.put("/uploads/{upload_id}/chunks/{chunk_index}", response_model=UploadChunkResponse)
    async def resumable_upload_chunk(
        upload_id: uuid.UUID, chunk_index: int, request: Request
    ) -> UploadChunkResponse:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        result = await write_upload_chunk(
            request,
            resolved_factory,
            resolved_store,
            user.id,
            upload_id,
            chunk_index,
            request.headers.get("x-chunk-sha256", ""),
        )
        return UploadChunkResponse(**result)

    @app.post("/uploads/{upload_id}/complete", response_model=UploadResponse)
    async def complete_resumable_upload(upload_id: uuid.UUID, request: Request) -> UploadResponse:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        video = await complete_upload_session(
            resolved_factory, resolved_settings, resolved_store, user.id, upload_id
        )
        return UploadResponse(video=_video_detail(video))

    @app.delete("/uploads/{upload_id}")
    def abandon_resumable_upload(upload_id: uuid.UUID, request: Request, response: Response) -> Response:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        response_status, code = abandon_upload_session(resolved_factory, resolved_store, user.id, upload_id)
        response.status_code = response_status
        response.headers["Cache-Control"] = "no-store"
        if code is not None:
            return JSONResponse(
                {"detail": {"code": code}}, status_code=response_status, headers={"Cache-Control": "no-store"}
            )
        return response

    @app.get("/videos", response_model=VideoListResponse)
    def list_videos(request: Request) -> VideoListResponse:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        with resolved_factory() as session:
            videos = list(
                session.scalars(
                    select(Video).where(Video.owner_id == user.id).order_by(Video.created_at.desc(), Video.id)
                )
            )
            return VideoListResponse(items=[_video_summary(video) for video in videos])

    @app.get("/videos/{video_id}", response_model=VideoDetailResponse)
    def get_video(video_id: uuid.UUID, request: Request) -> VideoDetailResponse:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        with resolved_factory() as session:
            video = session.scalar(select(Video).where(Video.id == video_id, Video.owner_id == user.id))
            if video is None:
                raise HTTPException(status_code=404, detail="video not found")
            if video.status != "ready":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "VIDEO_DELETION_IN_PROGRESS", "progress_endpoint": "deletion"},
                )
            return _video_detail(video)

    @app.get("/videos/{video_id}/events", response_model=AnalysisEventsResponse)
    def get_video_events(
        video_id: uuid.UUID,
        request: Request,
        response: Response,
        before_id: int | None = Query(default=None, ge=1),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> AnalysisEventsResponse:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        with resolved_factory() as session:
            video = session.scalar(select(Video.id).where(Video.id == video_id, Video.owner_id == user.id))
            if video is None:
                raise HTTPException(status_code=404, detail="video not found")
            statement = select(AnalysisEvent).where(
                AnalysisEvent.video_id == video_id,
                AnalysisEvent.owner_id == user.id,
            )
            if before_id is not None:
                statement = statement.where(AnalysisEvent.id < before_id)
            rows = list(session.scalars(statement.order_by(AnalysisEvent.id.desc()).limit(limit + 1)))
            has_more = len(rows) > limit
            page = list(reversed(rows[:limit]))
            response.headers["Cache-Control"] = "private, no-store"
            return AnalysisEventsResponse(
                items=[AnalysisEventResponse.model_validate(row) for row in page],
                has_more=has_more,
                next_before_id=page[0].id if has_more and page else None,
            )

    @app.get("/videos/{video_id}/deletion", response_model=VideoDeletionProgress)
    def get_video_deletion(video_id: uuid.UUID, request: Request) -> VideoDeletionProgress:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        with resolved_factory() as session:
            video = session.scalar(select(Video).where(Video.id == video_id, Video.owner_id == user.id))
            if video is None:
                raise HTTPException(status_code=404, detail="video not found")
            pending_attempt_ids: list[uuid.UUID] = []
            stop_required = False
            if video.current_analysis_task_id is not None:
                attempts = list(
                    session.scalars(
                        select(AnalysisAttempt).where(
                            AnalysisAttempt.task_id == video.current_analysis_task_id
                        )
                    )
                )
                for attempt in attempts:
                    execution = session.scalar(
                        select(AnalysisExecution).where(AnalysisExecution.attempt_id == attempt.id)
                    )
                    stopped = (attempt.claim_count == 0 and execution is None) or (
                        attempt.claim_count == 1
                        and attempt.stop_confirmed_at is not None
                        and execution is not None
                        and execution.stop_confirmed_at is not None
                    )
                    if not stopped:
                        stop_required = True
                    if not stopped or attempt.cleanup_state != "cleaned":
                        pending_attempt_ids.append(attempt.id)
            return VideoDeletionProgress(
                video_id=video.id,
                state=video.status,
                error_code=video.deletion_error,
                pending_attempt_ids=pending_attempt_ids,
                stop_confirmation_required=stop_required,
            )

    @app.get("/videos/{video_id}/media-deletion", response_model=VideoMediaDeletionProgress)
    def get_video_media_deletion(
        video_id: uuid.UUID, request: Request, response: Response
    ) -> VideoMediaDeletionProgress:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        with resolved_factory() as session:
            video = session.scalar(select(Video).where(Video.id == video_id, Video.owner_id == user.id))
            if video is None:
                raise HTTPException(status_code=404, detail="video not found")
            response.headers["Cache-Control"] = "private, no-store"
            return _media_deletion_progress(session, video)

    @app.delete("/videos/{video_id}/media", response_model=VideoMediaDeletionProgress)
    async def remove_video_media(
        video_id: uuid.UUID, request: Request, response: Response
    ) -> VideoMediaDeletionProgress:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        return await finish_media_cleanup_request(
            user.id,
            video_id,
            lambda: delete_video_media(resolved_factory, resolved_store, user.id, video_id),
            response,
        )

    @app.post(
        "/videos/{video_id}/media-deletion/retry",
        response_model=VideoMediaDeletionProgress,
    )
    async def retry_video_media_deletion(
        video_id: uuid.UUID, request: Request, response: Response
    ) -> VideoMediaDeletionProgress:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        return await finish_media_cleanup_request(
            user.id,
            video_id,
            lambda: retry_video_media_cleanup(resolved_factory, resolved_store, user.id, video_id),
            response,
        )

    @app.post(
        "/videos/{video_id}/tasks",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AnalysisTaskResponse,
    )
    def create_analysis(video_id: uuid.UUID, request: Request) -> AnalysisTaskResponse:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        submitted = submit_analysis_task(resolved_factory, user.id, video_id, resolved_settings)
        coordinate_delivery(submitted.task_id, submitted.attempt_id)
        response = task_response(resolved_factory, user.id, video_id, submitted.task_id)
        return response

    @app.post(
        "/videos/{video_id}/tasks/{task_id}/recover",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AnalysisTaskResponse,
    )
    def recover_analysis(
        video_id: uuid.UUID,
        task_id: uuid.UUID,
        payload: RecoveryRequest,
        request: Request,
    ) -> AnalysisTaskResponse:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        recovered = recover_analysis_attempt(
            resolved_factory,
            user.id,
            video_id,
            task_id,
            payload.attempt_id,
            policy=delivery_policy,
            settings=resolved_settings,
        )
        coordinate_delivery(recovered.task_id, recovered.attempt_id)
        return task_response(
            resolved_factory,
            user.id,
            video_id,
            recovered.task_id,
            recovered.attempt_id,
        )

    @app.post(
        "/videos/{video_id}/tasks/{task_id}/summarize",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=AnalysisTaskResponse,
    )
    def continue_summary(
        video_id: uuid.UUID,
        task_id: uuid.UUID,
        payload: RecoveryRequest,
        request: Request,
    ) -> AnalysisTaskResponse:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        continued = continue_summary_attempt(
            resolved_factory,
            user.id,
            video_id,
            task_id,
            payload.attempt_id,
            policy=delivery_policy,
            settings=resolved_settings,
        )
        coordinate_delivery(continued.task_id, continued.attempt_id)
        return task_response(
            resolved_factory,
            user.id,
            video_id,
            continued.task_id,
            continued.attempt_id,
        )

    @app.post("/videos/{video_id}/cleanup", response_model=CleanupResponse)
    def cleanup_attempt(
        video_id: uuid.UUID,
        payload: CleanupRequest,
        request: Request,
    ) -> CleanupResponse:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        state, error_code = retry_attempt_cleanup(
            resolved_factory, resolved_store, user.id, video_id, payload.attempt_id
        )
        return CleanupResponse(
            video_id=video_id,
            attempt_id=payload.attempt_id,
            cleanup_state=state,
            cleanup_error_code=error_code,
        )

    @app.get(
        "/videos/{video_id}/tasks/{task_id}",
        response_model=AnalysisTaskResponse,
    )
    def get_analysis(video_id: uuid.UUID, task_id: uuid.UUID, request: Request) -> AnalysisTaskResponse:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        return task_response(resolved_factory, user.id, video_id, task_id)

    @app.get("/videos/{video_id}/transcript", response_model=TranscriptResponse)
    def get_transcript(video_id: uuid.UUID, request: Request) -> TranscriptResponse:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        return transcript_response(resolved_factory, user.id, video_id)

    @app.get("/videos/{video_id}/summary", response_model=AnalysisSummaryResponse)
    def get_summary(video_id: uuid.UUID, request: Request, response: Response) -> AnalysisSummaryResponse:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        response.headers["Cache-Control"] = "private, no-store"
        return summary_response(resolved_factory, user.id, video_id)

    @app.api_route("/videos/{video_id}/content", methods=["GET", "HEAD"])
    def video_content(
        video_id: uuid.UUID,
        request: Request,
        range_header: str | None = Header(default=None, alias="Range"),
    ) -> Response:
        user, _ = _current_auth(request, resolved_factory) or _raise_auth()
        with resolved_factory() as session:
            video = session.scalar(
                select(Video)
                .where(
                    Video.id == video_id,
                    Video.owner_id == user.id,
                    Video.status == "ready",
                )
                .with_for_update()
            )
            if video is None:
                raise HTTPException(status_code=404, detail="video not found")
            if video.media_status != "available":
                raise HTTPException(
                    status_code=410,
                    detail={"code": "VIDEO_MEDIA_UNAVAILABLE"},
                    headers={"Cache-Control": "private, no-store"},
                )
            storage_name = video.storage_name
            byte_count = video.byte_count
            path = resolved_store.target_path(storage_name)
            try:
                source = path.open("rb")
            except OSError as exc:
                raise HTTPException(status_code=404, detail="video content is unavailable") from exc
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"}
        if request.method == "HEAD":
            source.close()
            headers["Content-Length"] = str(byte_count)
            return Response(status_code=200, media_type="video/mp4", headers=headers)
        start, end, response_status = 0, byte_count - 1, 200
        if range_header is not None:
            parsed = _parse_range(range_header, byte_count)
            if parsed is None:
                source.close()
                raise HTTPException(
                    status_code=416,
                    detail="range not satisfiable",
                    headers={"Content-Range": f"bytes */{byte_count}", "Accept-Ranges": "bytes"},
                )
            start, end = parsed
            response_status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{byte_count}"
        content_length = end - start + 1
        source.seek(start)

        def file_chunks() -> Iterator[bytes]:
            remaining = content_length
            try:
                while remaining:
                    block = source.read(min(64 * 1024, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block
            finally:
                source.close()

        headers["Content-Length"] = str(content_length)
        return _ClosingStreamingResponse(
            file_chunks(),
            source,
            status_code=response_status,
            media_type="video/mp4",
            headers=headers,
        )

    @app.delete("/videos/{video_id}")
    async def remove_video(video_id: uuid.UUID, request: Request, response: Response) -> Response:
        current = _current_auth(request, resolved_factory)
        _require_csrf(request, resolved_settings, resolved_factory, current)
        assert current is not None
        user, _ = current
        cleanup_tasks: set[asyncio.Task[int]] = request.app.state.cleanup_tasks
        cleanup_task = asyncio.create_task(
            asyncio.to_thread(delete_video, resolved_factory, resolved_store, user.id, video_id)
        )
        cleanup_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(cleanup_tasks.discard)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(cleanup_task), timeout=resolved_settings.cleanup_timeout_seconds
            )
        except TimeoutError:
            logger.warning("video_cleanup_timeout video_id=%s", video_id.hex)
            result = status.HTTP_202_ACCEPTED
        response.status_code = result
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


def _raise_auth() -> tuple[User, SessionRecord]:
    raise HTTPException(status_code=401, detail="authentication required")


app = create_app()
