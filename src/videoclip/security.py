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


# 用户名先按统一规则校验并规范化，数据库唯一性检查使用规范化后的值。
def validate_username(value: str) -> str:
    normalized = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(value) or len(normalized) > 32:
        raise ValueError("username must be 3-32 ASCII letters, digits, dots, dashes, or underscores")
    return normalized


# 密码使用专用密码哈希器保存，原始密码不会进入持久化记录。
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


# Cookie 返回随机令牌，数据库只保存其 SHA-256 摘要以便校验会话。
def new_session_token() -> str:
    # 用 32 字节安全随机数生成令牌，不把用户编号或密码编码进 Cookie。
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    # 会话表只存 SHA-256 摘要；收到 Cookie 后用相同算法查找会话。
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# 跨站请求令牌由随机数和服务端密钥签名，客户端无法自行伪造有效签名。
def new_csrf_token(secret: str) -> str:
    # 给随机值附上服务端签名，用于核验 CSRF Token 是否由本服务签发。
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(secret.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).digest()
    return f"{nonce}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


def verify_csrf_token(secret: str, token: str) -> bool:
    # 此处检查格式和签名；与 Cookie、登录会话的匹配由接口层检查。
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
