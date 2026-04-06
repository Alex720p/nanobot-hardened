"""Shell execution tool."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.workspace_sandbox import WorkspaceSandboxManager


class ExecTool(Tool):
    """Tool to execute shell commands inside the workspace sandbox."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        workspace_sandbox: WorkspaceSandboxManager | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self._workspace_sandbox = workspace_sandbox

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_OUTPUT = 10_000

    @property
    def description(self) -> str:
        return "Execute a shell command in the workspace sandbox and return its output. The workspace sandbox has no access to internet."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
                "timeout": {
                    "type": "integer",
                    "description": (
                        "Timeout in seconds. Increase for long-running commands "
                        "like compilation or installation (default 60, max 600)."
                    ),
                    "minimum": 1,
                    "maximum": 600,
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        if not self._workspace_sandbox:
            return "Error: Workspace sandbox manager is required for exec"

        cwd = self._resolve_working_dir(working_dir)
        guard_error = self._guard_command(command, str(cwd))
        if guard_error:
            return guard_error

        effective_timeout = min(timeout or self.timeout, self._MAX_TIMEOUT)

        try:
            result = await asyncio.to_thread(
                self._workspace_sandbox.run_command,
                command,
                timeout=effective_timeout,
                working_dir=cwd,
                path_append=self.path_append,
            )
            return self._format_result(
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exit_code=result.exit_code,
            )
        except NotADirectoryError:
            return f"Error: Working directory not found: {cwd}"
        except Exception as e:
            if "timed out" in str(e).lower():
                return f"Error: Command timed out after {effective_timeout} seconds"
            return f"Error executing command: {str(e)}"

    def _resolve_working_dir(self, working_dir: str | None) -> Path:
        base = Path(self.working_dir).expanduser() if self.working_dir else (
            self._workspace_sandbox.workspace if self._workspace_sandbox else Path.cwd()
        )
        target = Path(working_dir).expanduser() if working_dir else base
        if not target.is_absolute():
            target = base / target
        return target.resolve()

    def _format_result(self, *, stdout: str, stderr: str, exit_code: int) -> str:
        output_parts = []

        if stdout:
            output_parts.append(stdout)

        if stderr and stderr.strip():
            output_parts.append(f"STDERR:\n{stderr}")

        output_parts.append(f"\nExit code: {exit_code}")

        result = "\n".join(output_parts) if output_parts else "(no output)"

        if len(result) > self._MAX_OUTPUT:
            half = self._MAX_OUTPUT // 2
            result = (
                result[:half]
                + f"\n\n... ({len(result) - self._MAX_OUTPUT:,} chars truncated) ...\n\n"
                + result[-half:]
            )

        return result

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        from nanobot.security.network import contains_internal_url
        if contains_internal_url(cmd):
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"[A-Za-z]:\\[^\s\"'|><;]+", command)
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command)
        home_paths = re.findall(r"(?:^|[\s|>'\"])(~[^\s\"'>;|<]*)", command)
        return win_paths + posix_paths + home_paths
