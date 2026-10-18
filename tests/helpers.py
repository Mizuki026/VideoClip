from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from videoclip.main import CSRF_COOKIE

ORIGIN = "http://127.0.0.1:58001"

# 封装重复的接口操作，csrf() 获取 CSRF Token
def csrf(client: TestClient) -> str:
    response = client.get("/auth/csrf")
    assert response.status_code == 200
    return response.json()["csrf_token"]

# mutate_headers() 生成包含 Origin 和 CSRF Token 的请求头
def mutate_headers(token: str, *, origin: str | None = ORIGIN) -> dict[str, str]:
    headers = {"X-CSRF-Token": token}
    if origin is not None:
        headers["Origin"] = origin
    return headers

# register() 和 login() 分别发送注册、登录请求
def register(client: TestClient, username: str, password: str = "correct horse battery staple 42") -> Any:
    return client.post(
        "/auth/register",
        json={"username": username, "password": password},
        headers=mutate_headers(csrf(client)),
    )


def login(client: TestClient, username: str, password: str = "correct horse battery staple 42") -> Any:
    response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
        headers=mutate_headers(csrf(client)),
    )
    if response.status_code == 200:
        assert client.get("/auth/csrf").status_code == 200
        assert client.cookies.get(CSRF_COOKIE)
    return response
