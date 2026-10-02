from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=2, hash_len=32, salt_len=16)
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")


def validate_username(value: str) -> str:
    normalized = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(value) or len(normalized) > 32:
        raise ValueError("username must be 3-32 ASCII letters, digits, dots, dashes, or underscores")
    return normalized


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("password must contain 12-128 characters")
    return PASSWORDS.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if len(password) > 128:
        return False
    try:
        return PASSWORDS.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token(secret: str) -> str:
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(secret.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).digest()
    return f"{nonce}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


def verify_csrf_token(secret: str, token: str) -> bool:
    if len(token) > 256 or token.count(".") != 1:
        return False
    nonce, provided = token.split(".", 1)
    if not nonce or len(nonce) > 128 or not provided:
        return False
    try:
        expected = new_signature(secret, nonce)
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(expected, provided)


def new_signature(secret: str, nonce: str) -> str:
    signature = hmac.new(secret.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
