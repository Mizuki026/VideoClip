from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from argon2 import extract_parameters
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.helpers import csrf, login, mutate_headers, register
from videoclip.main import CSRF_COOKIE, SESSION_COOKIE, create_app
from videoclip.models import SessionRecord, User
from videoclip.security import hash_password, new_csrf_token, token_hash, verify_csrf_token, verify_password
from videoclip.settings import Settings
from videoclip.storage import MediaStore


def test_password_hash_is_argon2id_and_session_hash_is_one_way() -> None:
    value = hash_password("correct horse battery staple")
    assert value.startswith("$argon2id$")
    assert extract_parameters(value).type.name == "ID"
    assert verify_password(value, "correct horse battery staple")
    assert not verify_password(value, "wrong password")
    raw_token = "sample-token-which-is-not-a-cookie-secret"
    assert token_hash(raw_token) == hashlib.sha256(raw_token.encode()).hexdigest()
    assert token_hash(raw_token) != raw_token


def test_csrf_token_signature_is_bound_to_secret() -> None:
    token = new_csrf_token("s" * 32)
    assert verify_csrf_token("s" * 32, token)
    assert not verify_csrf_token("x" * 32, token)
    assert not verify_csrf_token("s" * 32, token + ".extra")


def test_two_users_register_login_and_logout_invalidates_session(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path, caplog
) -> None:
    app = create_app(test_settings, session_factory, MediaStore(tmp_path / "data"))
    with TestClient(app) as first, TestClient(app) as second:
        first_registration = register(first, "Alice")
        second_registration = register(second, "bob")
        assert first_registration.status_code == second_registration.status_code == 201
        assert first_registration.json()["username"] == "alice"

        first_login = login(first, "alice")
        second_login = login(second, "bob")
        assert first_login.status_code == second_login.status_code == 200
        password = "correct horse battery staple 42"
        assert first.get("/auth/me").json()["username"] == "alice"
        assert second.get("/auth/me").json()["username"] == "bob"

        raw_session = first.cookies.get(SESSION_COOKIE)
        assert raw_session and raw_session not in first_login.text
        session_cookie = next(
            value for value in first_login.headers.get_list("set-cookie") if SESSION_COOKIE in value
        )
        assert (
            "HttpOnly" in session_cookie
            and "SameSite=lax" in session_cookie
            and "Max-Age=86400" in session_cookie
        )
        assert "Secure" not in session_cookie
        assert raw_session not in caplog.text
        assert password not in caplog.text
        assert test_settings.app_secret_key.get_secret_value() not in caplog.text
        assert str(test_settings.app_data_root) not in caplog.text

        with session_factory() as session:
            stored = session.scalar(
                select(SessionRecord).where(SessionRecord.token_hash == token_hash(raw_session))
            )
            user = session.scalar(select(User).where(User.username == "alice"))
            assert stored is not None and stored.revoked_at is None
            assert stored.token_hash != raw_session
            assert user is not None and user.password_hash.startswith("$argon2id$")

        token = csrf(first)
        logout = first.post("/auth/logout", headers=mutate_headers(token))
        assert logout.status_code == 204
        assert SESSION_COOKIE not in first.cookies
        first.cookies.set(SESSION_COOKIE, raw_session)
        assert first.get("/auth/me").status_code == 401
        with session_factory() as session:
            stored = session.scalar(
                select(SessionRecord).where(SessionRecord.token_hash == token_hash(raw_session))
            )
            assert stored is not None and stored.revoked_at is not None
        assert CSRF_COOKIE not in first.cookies


def test_active_session_limit_revokes_oldest_session(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path
) -> None:
    limited_settings = test_settings.model_copy(update={"max_sessions_per_user": 1})
    app = create_app(limited_settings, session_factory, MediaStore(tmp_path / "data"))
    with TestClient(app) as client:
        assert register(client, "session-limit").status_code == 201
        assert login(client, "session-limit").status_code == 200
        old_token = client.cookies.get(SESSION_COOKIE)
        assert old_token
        assert login(client, "session-limit").status_code == 200
        current_token = client.cookies.get(SESSION_COOKIE)
        assert current_token and current_token != old_token
        client.cookies.set(SESSION_COOKIE, old_token)
        assert client.get("/auth/me").status_code == 401
        with session_factory() as session:
            user = session.scalar(select(User).where(User.username == "session-limit"))
            assert user is not None
            records = list(session.scalars(select(SessionRecord).where(SessionRecord.user_id == user.id)))
            assert sum(record.revoked_at is None for record in records) == 1


def test_secure_cookie_defaults_to_https_and_production_rejects_insecure_override(
    test_settings: Settings, tmp_path: Path
) -> None:
    https_settings = Settings(
        _env_file=None,
        APP_ENV="production",
        APP_ORIGIN="https://localhost:58001",
        APP_SECRET_KEY=test_settings.app_secret_key.get_secret_value(),
        APP_DATA_ROOT=tmp_path / "https-data",
        DB_HOST=test_settings.db_host,
        DB_PORT=test_settings.db_port,
        DB_NAME=test_settings.db_name,
        DB_USER=test_settings.db_user,
        DB_PASSWORD_FILE=test_settings.db_password_file,
    )
    assert https_settings.secure_cookie is True
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            APP_ENV="production",
            APP_ORIGIN="https://localhost:58001",
            APP_SECRET_KEY=test_settings.app_secret_key.get_secret_value(),
            APP_DATA_ROOT=tmp_path / "insecure-data",
            COOKIE_SECURE=False,
            DB_HOST=test_settings.db_host,
            DB_PORT=test_settings.db_port,
            DB_NAME=test_settings.db_name,
            DB_USER=test_settings.db_user,
            DB_PASSWORD_FILE=test_settings.db_password_file,
        )


def test_state_changes_require_exact_origin_and_csrf(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path
) -> None:
    app = create_app(test_settings, session_factory, MediaStore(tmp_path / "data"))
    with TestClient(app) as client:
        token = csrf(client)
        payload = {"username": "case-user", "password": "correct horse battery staple 42"}
        assert client.post("/auth/register", json=payload, headers={"X-CSRF-Token": token}).status_code == 403
        assert (
            client.post(
                "/auth/register", json=payload, headers=mutate_headers(token, origin="https://evil.example")
            ).status_code
            == 403
        )
        assert (
            client.post("/auth/register", json=payload, headers=mutate_headers("invalid")).status_code == 403
        )
        assert client.post("/auth/register", json=payload, headers=mutate_headers(token)).status_code == 201


def test_input_size_is_bounded_and_validation_does_not_echo_password(
    session_factory: sessionmaker[Session], test_settings: Settings, tmp_path: Path
) -> None:
    app = create_app(test_settings, session_factory, MediaStore(tmp_path / "data"))
    with TestClient(app) as client:
        too_large = b"{" + (b" " * (test_settings.max_json_body_bytes + 20))
        response = client.post("/auth/register", content=too_large)
        assert response.status_code == 413
        bad_password = "bad"
        response = client.post(
            "/auth/register",
            json={"username": "bounded", "password": bad_password},
            headers=mutate_headers(csrf(client)),
        )
        assert response.status_code == 422
        assert bad_password not in response.text
