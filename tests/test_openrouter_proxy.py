import json
import asyncio

import httpx
from fastapi.testclient import TestClient

from nanobot.proxy.openrouter import (
    OpenRouterProxy,
    create_openrouter_proxy_app,
    load_openrouter_key,
    run_openrouter_proxy,
)


def _response(
    status_code: int = 200,
    *,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.Response(
        status_code,
        json=json_body,
        headers=headers,
        request=request,
    )


def test_build_upstream_headers_replaces_authorization() -> None:
    proxy = OpenRouterProxy(api_key="real-key")

    headers = proxy._build_upstream_headers(
        {
            "Authorization": "Bearer dummy-key",
            "Content-Type": "application/json",
            "X-Title": "nanobot",
            "Host": "127.0.0.1:8088",
        }
    )

    assert headers["Authorization"] == "Bearer real-key"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Title"] == "nanobot"
    assert "Host" not in headers


def test_handle_forwards_chat_completions_with_injected_key(monkeypatch) -> None:
    proxy = OpenRouterProxy(api_key="real-key")
    captured: dict[str, object] = {}

    async def fake_send(method: str, url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return _response(
            json_body={"id": "chatcmpl_demo"},
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
                "transfer-encoding": "chunked",
            },
        )

    monkeypatch.setattr(proxy, "_send_upstream", fake_send)

    result = asyncio.run(
        proxy.handle(
            method="POST",
            raw_path="/v1/chat/completions",
            headers={
                "Authorization": "Bearer dummy-key",
                "Content-Type": "application/json",
                "X-Trace-Id": "abc123",
            },
            body=b'{"model":"anthropic/claude-sonnet-4-5","messages":[]}',
        )
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer real-key"
    assert captured["headers"]["X-Trace-Id"] == "abc123"
    assert result.status_code == 200
    assert result.headers["content-type"] == "application/json"
    assert "content-encoding" not in result.headers
    assert "transfer-encoding" not in result.headers
    assert json.loads(result.body) == {"id": "chatcmpl_demo"}


def test_handle_filters_server_header_from_upstream(monkeypatch) -> None:
    proxy = OpenRouterProxy(api_key="real-key")

    async def fake_send(method: str, url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        return _response(
            json_body={"id": "chatcmpl_demo"},
            headers={
                "content-type": "application/json",
                "server": "openrouter-edge",
            },
        )

    monkeypatch.setattr(proxy, "_send_upstream", fake_send)

    result = asyncio.run(
        proxy.handle(
            method="POST",
            raw_path="/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            body=b'{"model":"anthropic/claude-sonnet-4-5","messages":[]}',
        )
    )

    assert result.status_code == 200
    assert "server" not in {k.lower(): v for k, v in result.headers.items()}


def test_handle_rejects_unknown_route() -> None:
    proxy = OpenRouterProxy(api_key="real-key")

    result = asyncio.run(proxy.handle("POST", "/v1/responses", {"Content-Type": "application/json"}, b"{}"))

    assert result.status_code == 404
    assert json.loads(result.body)["error"] == "route_not_found"


def test_handle_rejects_oversized_body() -> None:
    proxy = OpenRouterProxy(api_key="real-key", max_body_bytes=4)

    result = asyncio.run(
        proxy.handle("POST", "/v1/chat/completions", {"Content-Type": "application/json"}, b"12345")
    )

    assert result.status_code == 413
    assert json.loads(result.body)["error"] == "body_too_large"


def test_fastapi_healthz() -> None:
    with TestClient(create_openrouter_proxy_app(api_key="real-key")) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "openrouter-proxy"}


def test_handle_allows_known_route_with_query(monkeypatch) -> None:
    proxy = OpenRouterProxy(api_key="real-key")

    async def fake_send(method: str, url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        return _response(json_body={"data": []}, headers={"content-type": "application/json"})

    monkeypatch.setattr(proxy, "_send_upstream", fake_send)

    result = asyncio.run(proxy.handle("GET", "/v1/models?page=1", {"Accept": "application/json"}))

    assert result.status_code == 200
    assert json.loads(result.body) == {"data": []}


def test_load_openrouter_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-key")

    assert load_openrouter_key() == "real-key"


def test_run_openrouter_proxy_requires_env(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    try:
        run_openrouter_proxy()
    except ValueError as exc:
        assert "OPENROUTER_API_KEY" in str(exc)
    else:
        raise AssertionError("run_openrouter_proxy() should require OPENROUTER_API_KEY")
