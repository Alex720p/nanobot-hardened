"""Agent Sandbox orchestration for web_fetch."""

from __future__ import annotations

import json
import os
import shlex
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.utils.helpers import build_image_content_blocks

if TYPE_CHECKING:
    from nanobot.config.schema import WebSandboxConfig

_RUNNER_NAME = "web_fetch.py"


def _get_sandbox_client_class():
    from k8s_agent_sandbox import SandboxClient

    return SandboxClient


def _parse_run_stdout(stdout: str) -> dict[str, Any]:
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("Sandbox runner did not emit a JSON status line")

class WebFetchSandboxOrchestrator:
    """Runs the standalone web_fetch runner in Agent Sandbox and reads the result file back."""

    def __init__(self, config: "WebSandboxConfig", proxy: str | None = None):
        self.config = config
        self.proxy = proxy

    async def fetch(self, url: str, extract_mode: str, max_chars: int) -> Any:
        import asyncio

        return await asyncio.to_thread(self._fetch_sync, url, extract_mode, max_chars)

    def _fetch_sync(self, url: str, extract_mode: str, max_chars: int) -> Any:
        try:
            sandbox_client_cls = _get_sandbox_client_class()
        except Exception as exc:
            logger.error("Agent Sandbox client unavailable for web_fetch: {}", exc)
            return json.dumps(
                {"error": f"Sandbox web_fetch unavailable: {exc}", "url": url},
                ensure_ascii=False,
            )

        try:
            with sandbox_client_cls(
                template_name=self.config.template_name,
                namespace=self.config.namespace,
            ) as sandbox:
                result = sandbox.run(
                    self._build_command(url, extract_mode, max_chars),
                    timeout=self.config.run_timeout,
                )
                if result.exit_code != 0:
                    detail = (result.stderr or result.stdout or "").strip() or "unknown sandbox error"
                    return json.dumps(
                        {"error": f"Sandbox web_fetch failed: {detail}", "url": url},
                        ensure_ascii=False,
                    )

                meta = _parse_run_stdout(result.stdout or "")
                if meta.get("status") != "ok":
                    return json.dumps(
                        {"error": meta.get("error", "Sandbox web_fetch returned an error"), "url": url},
                        ensure_ascii=False,
                    )

                output_file = str(meta.get("output_file", "")).strip()
                if not output_file:
                    return json.dumps(
                        {"error": "Sandbox web_fetch did not return an output_file", "url": url},
                        ensure_ascii=False,
                    )

                raw = sandbox.read(output_file, timeout=self.config.run_timeout)
                if meta.get("result_type") == "image":
                    content_type = str(meta.get("content_type") or "application/octet-stream")
                    return build_image_content_blocks(
                        raw,
                        content_type,
                        url,
                        f"(Image fetched from: {url})",
                    )

                return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.error("Agent Sandbox web_fetch failed: {}", exc)
            return json.dumps({"error": f"Sandbox web_fetch failed: {exc}", "url": url}, ensure_ascii=False)

    def _build_command(self, url: str, extract_mode: str, max_chars: int) -> str:
        parts = [
            "python3",
            _RUNNER_NAME,
            "--url",
            url,
            "--extract-mode",
            extract_mode,
            "--max-chars",
            str(max_chars),
        ]
        if self.proxy:
            parts.extend(["--proxy", self.proxy])
        jina_key = os.environ.get("JINA_API_KEY", "").strip()
        if jina_key:
            parts.extend(["--jina-api-key", jina_key])
        return " ".join(shlex.quote(part) for part in parts)
