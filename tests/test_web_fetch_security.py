"""Tests for sandbox-only web_fetch guards and configuration errors."""

from __future__ import annotations

import json
import socket
from unittest.mock import patch

import pytest

from nanobot.config.schema import WebSandboxConfig
from nanobot.agent.tools.web import WebFetchTool


def _fake_resolve_private(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0))]


def _fake_resolve_public(hostname, port, family=0, type_=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]


@pytest.mark.asyncio
async def test_web_fetch_blocks_private_ip():
    tool = WebFetchTool()
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_private):
        result = await tool.execute(url="http://169.254.169.254/computeMetadata/v1/")
    data = json.loads(result)
    assert "error" in data
    assert "private" in data["error"].lower() or "blocked" in data["error"].lower()


@pytest.mark.asyncio
async def test_web_fetch_blocks_localhost():
    tool = WebFetchTool()
    def _resolve_localhost(hostname, port, family=0, type_=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]
    with patch("nanobot.security.network.socket.getaddrinfo", _resolve_localhost):
        result = await tool.execute(url="http://localhost/admin")
    data = json.loads(result)
    assert "error" in data


@pytest.mark.asyncio
async def test_web_fetch_requires_sandbox_configuration(monkeypatch):
    tool = WebFetchTool()

    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await tool.execute(url="https://example.com/page")
    data = json.loads(result)
    assert "error" in data
    assert "sandbox web_fetch is required" in data["error"].lower()


@pytest.mark.asyncio
async def test_web_fetch_surfaces_sandbox_unavailable_error(monkeypatch):
    monkeypatch.setattr(
        "nanobot.agent.tools.web_sandbox._get_sandbox_client_class",
        lambda: (_ for _ in ()).throw(ModuleNotFoundError("k8s_agent_sandbox")),
    )
    tool = WebFetchTool(sandbox_config=WebSandboxConfig(template_name="python-sandbox-template"))

    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve_public):
        result = await tool.execute(url="https://example.com/page")

    data = json.loads(result)
    assert "error" in data
    assert "sandbox web_fetch unavailable" in data["error"].lower()
