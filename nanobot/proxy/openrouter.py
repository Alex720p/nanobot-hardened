"""Local OpenRouter credential-injecting proxy."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_UPSTREAM_BASE = "https://openrouter.ai/api"
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024

_ALLOWED_ROUTES = frozenset(
    {
        ("POST", "/v1/chat/completions"),
        ("GET", "/v1/models"),
    }
)
_REQUEST_HEADER_SKIP = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_RESPONSE_HEADER_SKIP = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def _raw_path_with_query(path: str, query: str) -> str:
    return f"{path}?{query}" if query else path


def load_openrouter_key(api_key_env: str = "OPENROUTER_API_KEY") -> str:
    """Load the OpenRouter key from the configured environment variable."""
    api_key = os.environ.get(api_key_env, "").strip()
    if api_key:
        return api_key

    raise ValueError(f"Missing OpenRouter API key in env var: {api_key_env}")


@dataclass(slots=True)
class ProxyResult:
    """Represents one proxied response."""

    status_code: int
    headers: dict[str, str]
    body: bytes


class OpenRouterProxy:
    """Minimal local proxy that injects the real OpenRouter API key."""

    def __init__(
        self,
        api_key: str,
        upstream_base: str = DEFAULT_UPSTREAM_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        http_client: httpx.AsyncClient | None = None,
    ):
        if not api_key:
            raise ValueError("OpenRouter API key is required")

        self.api_key = api_key
        self.upstream_base = upstream_base.rstrip("/")
        self.timeout_s = timeout_s
        self.max_body_bytes = max_body_bytes
        self._http_client = http_client

    async def handle(
        self,
        method: str,
        raw_path: str,
        headers: dict[str, str],
        body: bytes = b"",
    ) -> ProxyResult:
        """Handle one incoming request."""
        parsed = urlsplit(raw_path)
        route = (method.upper(), parsed.path)

        if route == ("GET", "/healthz"):
            return self._json_response(200, {"ok": True, "service": "openrouter-proxy"})

        if route not in _ALLOWED_ROUTES:
            return self._json_response(404, {"error": "route_not_found", "path": parsed.path})

        if len(body) > self.max_body_bytes:
            return self._json_response(413, {"error": "body_too_large"})

        upstream_url = self._build_upstream_url(parsed.path, parsed.query)
        forward_headers = self._build_upstream_headers(headers)
        upstream = await self._send_upstream(method.upper(), upstream_url, forward_headers, body)
        return ProxyResult(
            status_code=upstream.status_code,
            headers=self._filter_response_headers(dict(upstream.headers)),
            body=upstream.content,
        )

    def _build_upstream_url(self, path: str, query: str = "") -> str:
        return _raw_path_with_query(f"{self.upstream_base}{path}", query)

    def _build_upstream_headers(self, headers: dict[str, str]) -> dict[str, str]:
        forwarded = {
            name: value
            for name, value in headers.items()
            if name.lower() not in _REQUEST_HEADER_SKIP
        }
        forwarded["Authorization"] = f"Bearer {self.api_key}"
        if "Content-Type" not in forwarded and "content-type" not in forwarded:
            forwarded["Content-Type"] = "application/json"
        return forwarded

    def _filter_response_headers(self, headers: dict[str, str]) -> dict[str, str]:
        return {
            name: value
            for name, value in headers.items()
            if name.lower() not in _RESPONSE_HEADER_SKIP
        }

    async def _send_upstream(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
    ) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.request(
                method=method,
                url=url,
                headers=headers,
                content=body,
            )

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            return await client.request(method=method, url=url, headers=headers, content=body)

    @staticmethod
    def _json_response(status_code: int, payload: dict[str, Any]) -> ProxyResult:
        return ProxyResult(
            status_code=status_code,
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )


def create_openrouter_proxy_app(
    *,
    api_key: str,
    upstream_base: str = DEFAULT_UPSTREAM_BASE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> FastAPI:
    """Build a FastAPI app for the local OpenRouter proxy."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(timeout=timeout_s) as http_client:
            app.state.proxy = OpenRouterProxy(
                api_key=api_key,
                upstream_base=upstream_base,
                timeout_s=timeout_s,
                max_body_bytes=max_body_bytes,
                http_client=http_client,
            )
            yield

    app = FastAPI(
        title="nanobot OpenRouter proxy",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "service": "openrouter-proxy"})

    @app.get("/v1/models")
    async def list_models(request: Request) -> Response:
        return await _handle_request(request)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _handle_request(request)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def fallback(path: str, request: Request) -> JSONResponse:
        del path
        body = await request.body()
        raw_path = _raw_path_with_query(request.url.path, request.url.query)
        result = await request.app.state.proxy.handle(
            request.method,
            raw_path,
            dict(request.headers),
            body,
        )
        return JSONResponse(
            content=json.loads(result.body),
            status_code=result.status_code,
            headers=result.headers,
        )

    return app


async def _handle_request(request: Request) -> Response:
    body = await request.body()
    raw_path = _raw_path_with_query(request.url.path, request.url.query)
    proxy: OpenRouterProxy = request.app.state.proxy
    try:
        result = await proxy.handle(
            method=request.method,
            raw_path=raw_path,
            headers=dict(request.headers),
            body=body,
        )
    except httpx.TimeoutException:
        result = OpenRouterProxy._json_response(504, {"error": "upstream_timeout"})
    except httpx.HTTPError as exc:
        result = OpenRouterProxy._json_response(
            502,
            {"error": "upstream_error", "detail": str(exc)},
        )
    except Exception:
        result = OpenRouterProxy._json_response(500, {"error": "proxy_internal_error"})

    return Response(
        content=result.body,
        status_code=result.status_code,
        headers=result.headers,
        media_type=result.headers.get("Content-Type"),
    )


def run_openrouter_proxy(
    api_key_env: str = "OPENROUTER_API_KEY",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    upstream_base: str = DEFAULT_UPSTREAM_BASE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> None:
    """Run the OpenRouter proxy until interrupted."""
    api_key = load_openrouter_key(api_key_env=api_key_env)

    try:
        import uvicorn
    except ImportError as exc:
        raise ValueError("uvicorn is required to run the FastAPI proxy") from exc

    app = create_openrouter_proxy_app(
        api_key=api_key,
        upstream_base=upstream_base,
        timeout_s=timeout_s,
        max_body_bytes=max_body_bytes,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
