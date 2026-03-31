"""Tests for Agent Sandbox-backed web_fetch orchestration."""

from __future__ import annotations

import json
import socket

import pytest

from nanobot.config.schema import WebSandboxConfig
from nanobot.agent.tools.web import WebFetchTool


def _fake_resolve_public(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


class _FakeRunResult:
    def __init__(self, stdout: str, stderr: str = "", exit_code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


@pytest.mark.asyncio
async def test_web_fetch_uses_agent_sandbox_and_reads_output(monkeypatch):
    calls: list[tuple[str, str]] = []

    class FakeSandboxClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            calls.append(("enter", json.dumps(self.kwargs, sort_keys=True)))
            return self

        def __exit__(self, exc_type, exc, tb):
            calls.append(("exit", ""))
            return False

        def run(self, command, timeout=60):
            calls.append(("run", command))
            return _FakeRunResult(
                json.dumps(
                    {
                        "status": "ok",
                        "output_file": "web_fetch_result.json",
                        "file_size": 12,
                        "result_type": "json",
                    }
                )
            )

        def read(self, path, timeout=60):
            calls.append(("read", path))
            return json.dumps({"untrusted": True, "text": "hello"}).encode("utf-8")

    monkeypatch.setattr(
        "nanobot.agent.tools.web_sandbox._get_sandbox_client_class",
        lambda: FakeSandboxClient,
    )
    monkeypatch.setattr("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public)

    tool = WebFetchTool(sandbox_config=WebSandboxConfig(template_name="python-sandbox-template"))
    result = await tool.execute(url="https://example.com/page")

    assert json.loads(result)["text"] == "hello"
    assert ("enter", json.dumps({"namespace": "default", "template_name": "python-sandbox-template"}, sort_keys=True)) in calls
    assert ("read", "web_fetch_result.json") in calls
    assert any(name == "run" and "python3 web_fetch.py" in value for name, value in calls)


@pytest.mark.asyncio
async def test_web_fetch_sandbox_image_result_returns_native_blocks(monkeypatch):
    png = b"\x89PNG\r\n\x1a\nrest"

    class FakeSandboxClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, command, timeout=60):
            return _FakeRunResult(
                json.dumps(
                    {
                        "status": "ok",
                        "output_file": "image.bin",
                        "file_size": len(png),
                        "result_type": "image",
                        "content_type": "image/png",
                    }
                )
            )

        def read(self, path, timeout=60):
            return png

    monkeypatch.setattr(
        "nanobot.agent.tools.web_sandbox._get_sandbox_client_class",
        lambda: FakeSandboxClient,
    )
    monkeypatch.setattr("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public)

    tool = WebFetchTool(sandbox_config=WebSandboxConfig(template_name="python-sandbox-template"))
    result = await tool.execute(url="https://example.com/image.png")

    assert isinstance(result, list)
    assert result[0]["type"] == "image_url"
    assert result[1]["text"] == "(Image fetched from: https://example.com/image.png)"
