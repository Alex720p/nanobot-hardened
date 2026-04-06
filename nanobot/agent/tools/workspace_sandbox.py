"""Long-lived Agent Sandbox workspace manager for filesystem tools."""

from __future__ import annotations

import shlex
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from nanobot.config.schema import WorkspaceSandboxConfig


def _get_sandbox_client_class():
    from k8s_agent_sandbox import SandboxClient

    return SandboxClient
class WorkspaceSandboxManager:
    """Owns a single long-lived sandbox for workspace filesystem operations."""

    def __init__(self, workspace: Path, config: "WorkspaceSandboxConfig"):
        self.workspace = workspace.resolve()
        self.config = config
        self._lock = threading.RLock()
        self._client: Any | None = None
        self._sandbox: Any | None = None
        self._closed = False

    def close(self) -> None:
        """Release the long-lived sandbox claim."""
        with self._lock:
            self._closed = True
            client = self._client
            self._client = None
            self._sandbox = None

            if client and hasattr(client, "__exit__"):
                try:
                    client.__exit__(None, None, None)
                except Exception as exc:
                    logger.warning("Failed to close workspace sandbox cleanly: {}", exc)

    def read_bytes(self, path: Path) -> bytes:
        """Read a file from the sandbox workspace."""
        sandbox = self._get_sandbox()
        with self._lock:
            return sandbox.read(self._remote_path(path), timeout=self.config.request_timeout)

    def write_bytes(self, path: Path, content: bytes) -> None:
        """Write a file into the sandbox workspace."""
        sandbox = self._get_sandbox()
        with self._lock:
            entry_type = self._entry_type_locked(path, sandbox)
            if entry_type == "directory":
                raise IsADirectoryError(str(path))
            self._upload_bytes_locked(self._remote_path(path), content, sandbox)

    def exists(self, path: Path) -> bool:
        """Check whether a path exists in the sandbox workspace."""
        sandbox = self._get_sandbox()
        with self._lock:
            remote = self._remote_path(path)
            if remote == ".":
                return True
            return sandbox.exists(remote, timeout=self.config.request_timeout)

    def entry_type(self, path: Path) -> str | None:
        """Return the sandbox entry type for a path: file, directory, or None."""
        sandbox = self._get_sandbox()
        with self._lock:
            return self._entry_type_locked(path, sandbox)

    def list_entries(self, path: Path) -> list[dict[str, Any]]:
        """List a directory directly from the sandbox."""
        sandbox = self._get_sandbox()
        with self._lock:
            entries = sandbox.list(self._remote_dir_path(path), timeout=self.config.request_timeout)
            return [
                {
                    "name": entry.name,
                    "size": entry.size,
                    "type": entry.type,
                    "mod_time": entry.mod_time,
                }
                for entry in entries
            ]

    def run_command(
        self,
        command: str,
        *,
        timeout: int,
        working_dir: Path | None = None,
        path_append: str = "",
    ) -> Any:
        """Run a shell command inside the long-lived sandbox workspace."""
        sandbox = self._get_sandbox()
        script_parts: list[str] = []

        with self._lock:
            if path_append:
                script_parts.append(f'export PATH="$PATH":{shlex.quote(path_append)}')

            if working_dir is not None:
                entry_type = self._entry_type_locked(working_dir, sandbox)
                if entry_type != "directory":
                    raise NotADirectoryError(str(working_dir))
                script_parts.append(f"cd -- {shlex.quote(self._remote_dir_path(working_dir))}")

            script_parts.append(command)
            script = " && ".join(script_parts)
            return sandbox.run(f"sh -lc {shlex.quote(script)}", timeout=timeout)

    def _get_sandbox(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Workspace sandbox manager is already closed.")
            if self._sandbox is not None:
                return self._sandbox
            if not self.config.template_name:
                raise RuntimeError(
                    "Workspace sandbox is required but not configured. "
                    "Set tools.workspace.templateName in config.json."
                )

            try:
                sandbox_client_cls = _get_sandbox_client_class()
                client = sandbox_client_cls(
                    template_name=self.config.template_name,
                    namespace=self.config.namespace,
                    gateway_name=self.config.gateway_name,
                    gateway_namespace=self.config.gateway_namespace,
                    api_url=self.config.api_url,
                    server_port=self.config.server_port,
                )
                sandbox = client.__enter__() if hasattr(client, "__enter__") else client
                self._client = client
                self._sandbox = sandbox
                logger.info(
                    "Workspace sandbox ready with template '{}' in namespace '{}'",
                    self.config.template_name,
                    self.config.namespace,
                )
                return sandbox
            except Exception as exc:
                logger.error("Workspace sandbox unavailable: {}", exc)
                raise RuntimeError(f"Workspace sandbox unavailable: {exc}") from exc

    def _entry_type_locked(self, path: Path, sandbox: Any) -> str | None:
        remote = self._remote_path(path)
        if remote == ".":
            return "directory"

        parent = self._remote_dir_path(path.parent)
        name = path.name
        try:
            entries = sandbox.list(parent, timeout=self.config.request_timeout)
        except Exception:
            return None

        for entry in entries:
            if entry.name == name:
                return str(entry.type)
        return None

    def _upload_bytes_locked(self, rel_path: str, content: bytes, sandbox: Any) -> None:
        if "/" not in rel_path:
            sandbox.write(rel_path, content, timeout=self.config.request_timeout)
            return

        parent = str(Path(rel_path).parent).replace("\\", "/")
        temp_name = f".nanobot-upload-{uuid.uuid4().hex}"
        sandbox.write(temp_name, content, timeout=self.config.request_timeout)
        self._run_shell_locked(
            sandbox,
            f"mkdir -p -- {shlex.quote(parent)} && mv -- {shlex.quote(temp_name)} {shlex.quote(rel_path)}",
        )

    def _run_shell_locked(self, sandbox: Any, script: str) -> None:
        result = sandbox.run(f"sh -lc {shlex.quote(script)}", timeout=self.config.request_timeout)
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or "").strip() or "unknown sandbox error"
            raise RuntimeError(f"Workspace sandbox command failed: {detail}")

    def _remote_path(self, path: Path) -> str:
        rel = path.resolve().relative_to(self.workspace).as_posix()
        return rel or "."

    def _remote_dir_path(self, path: Path) -> str:
        rel = self._remote_path(path)
        return "." if rel in ("", ".") else rel
