"""File system tools: read, write, edit, list."""

from __future__ import annotations

import asyncio
import difflib
import mimetypes
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.workspace_sandbox import WorkspaceSandboxManager
from nanobot.utils.helpers import build_image_content_blocks, detect_image_mime


def _resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
) -> Path:
    """Resolve path against workspace (if relative) and enforce directory restriction."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dir:
        all_dirs = [allowed_dir] + (extra_allowed_dirs or [])
        if not any(_is_under(resolved, d) for d in all_dirs):
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


class _FsTool(Tool):
    """Shared base for filesystem tools: path policy plus workspace-sandbox routing."""

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        extra_allowed_dirs: list[Path] | None = None,
        workspace_sandbox: WorkspaceSandboxManager | None = None,
    ):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._extra_allowed_dirs = extra_allowed_dirs
        self._workspace_sandbox = workspace_sandbox

    def _resolve(self, path: str) -> Path:
        resolved = _resolve_path(path, self._workspace, self._allowed_dir, self._extra_allowed_dirs)
        if not self._workspace:
            raise RuntimeError("Workspace is required for sandbox-backed filesystem tools.")
        if not _is_under(resolved, self._workspace):
            raise PermissionError(
                f"Sandbox-backed filesystem tools only support paths inside the workspace: {path}"
            )
        return resolved

    def _require_workspace_sandbox(self, path: Path) -> WorkspaceSandboxManager:
        if not self._workspace_sandbox:
            raise RuntimeError(f"Workspace sandbox manager is required for workspace path: {path}")
        return self._workspace_sandbox

    def _exists_sync(self, path: Path) -> bool:
        return self._require_workspace_sandbox(path).exists(path)

    def _entry_type_sync(self, path: Path) -> str | None:
        return self._require_workspace_sandbox(path).entry_type(path)

    def _read_bytes_sync(self, path: Path) -> bytes:
        return self._require_workspace_sandbox(path).read_bytes(path)

    def _write_bytes_sync(self, path: Path, content: bytes) -> None:
        self._require_workspace_sandbox(path).write_bytes(path, content)

    def _list_entries_sync(self, path: Path) -> list[dict[str, Any]]:
        return self._require_workspace_sandbox(path).list_entries(path)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class ReadFileTool(_FsTool):
    """Read file contents with optional line-based pagination."""

    _MAX_CHARS = 128_000
    _DEFAULT_LIMIT = 2000

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. Returns numbered lines. "
            "Use offset and limit to paginate through large files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to read"},
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed, default 1)",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (default 2000)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, offset: int = 1, limit: int | None = None, **kwargs: Any) -> Any:
        try:
            fp = self._resolve(path)
            entry_type = await asyncio.to_thread(self._entry_type_sync, fp)
            if entry_type is None:
                return f"Error: File not found: {path}"
            if entry_type != "file":
                return f"Error: Not a file: {path}"

            raw = await asyncio.to_thread(self._read_bytes_sync, fp)
            if not raw:
                return f"(Empty file: {path})"

            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if mime and mime.startswith("image/"):
                return build_image_content_blocks(raw, mime, str(fp), f"(Image file: {path})")

            try:
                text_content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return (
                    f"Error: Cannot read binary file {path} (MIME: {mime or 'unknown'}). "
                    "Only UTF-8 text and images are supported."
                )

            all_lines = text_content.splitlines()
            total = len(all_lines)

            if offset < 1:
                offset = 1
            if offset > total:
                return f"Error: offset {offset} is beyond end of file ({total} lines)"

            start = offset - 1
            end = min(start + (limit or self._DEFAULT_LIMIT), total)
            numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
            result = "\n".join(numbered)

            if len(result) > self._MAX_CHARS:
                trimmed, chars = [], 0
                for line in numbered:
                    chars += len(line) + 1
                    if chars > self._MAX_CHARS:
                        break
                    trimmed.append(line)
                end = start + len(trimmed)
                result = "\n".join(trimmed)

            if end < total:
                result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
            else:
                result += f"\n\n(End of file — {total} lines total)"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class WriteFileTool(_FsTool):
    """Write content to a file."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Creates parent directories if needed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to write to"},
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            fp = self._resolve(path)
            entry_type = await asyncio.to_thread(self._entry_type_sync, fp)
            if entry_type == "directory":
                return f"Error: Not a file: {path}"
            await asyncio.to_thread(self._write_bytes_sync, fp, content.encode("utf-8"))
            return f"Successfully wrote {len(content)} bytes to {fp}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def _find_match(content: str, old_text: str) -> tuple[str | None, int]:
    """Locate old_text in content: exact first, then line-trimmed sliding window.

    Both inputs should use LF line endings (caller normalises CRLF).
    Returns (matched_fragment, count) or (None, 0).
    """
    if old_text in content:
        return old_text, content.count(old_text)

    old_lines = old_text.splitlines()
    if not old_lines:
        return None, 0
    stripped_old = [l.strip() for l in old_lines]
    content_lines = content.splitlines()

    candidates = []
    for i in range(len(content_lines) - len(stripped_old) + 1):
        window = content_lines[i : i + len(stripped_old)]
        if [l.strip() for l in window] == stripped_old:
            candidates.append("\n".join(window))

    if candidates:
        return candidates[0], len(candidates)
    return None, 0


class EditFileTool(_FsTool):
    """Edit a file by replacing text with fallback matching."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing old_text with new_text. "
            "Supports minor whitespace/line-ending differences. "
            "Set replace_all=true to replace every occurrence."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to edit"},
                "old_text": {"type": "string", "description": "The text to find and replace"},
                "new_text": {"type": "string", "description": "The text to replace with"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false)",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            fp = self._resolve(path)
            entry_type = await asyncio.to_thread(self._entry_type_sync, fp)
            if entry_type is None:
                return f"Error: File not found: {path}"
            if entry_type != "file":
                return f"Error: Not a file: {path}"

            raw = await asyncio.to_thread(self._read_bytes_sync, fp)
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")
            match, count = _find_match(content, old_text.replace("\r\n", "\n"))

            if match is None:
                return self._not_found_msg(old_text, content, path)
            if count > 1 and not replace_all:
                return (
                    f"Warning: old_text appears {count} times. "
                    "Provide more context to make it unique, or set replace_all=true."
                )

            norm_new = new_text.replace("\r\n", "\n")
            new_content = content.replace(match, norm_new) if replace_all else content.replace(match, norm_new, 1)
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")

            await asyncio.to_thread(self._write_bytes_sync, fp, new_content.encode("utf-8"))
            return f"Successfully edited {fp}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {e}"

    @staticmethod
    def _not_found_msg(old_text: str, content: str, path: str) -> str:
        lines = content.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        window = len(old_lines)

        best_ratio, best_start = 0.0, 0
        for i in range(max(1, len(lines) - window + 1)):
            ratio = difflib.SequenceMatcher(None, old_lines, lines[i : i + window]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, i

        if best_ratio > 0.5:
            diff = "\n".join(difflib.unified_diff(
                old_lines,
                lines[best_start : best_start + window],
                fromfile="old_text (provided)",
                tofile=f"{path} (actual, line {best_start + 1})",
                lineterm="",
            ))
            return (
                f"Error: old_text not found in {path}.\n"
                f"Best match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
            )
        return f"Error: old_text not found in {path}. No similar text found. Verify the file content."


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


class ListDirTool(_FsTool):
    """List directory contents with optional recursion."""

    _DEFAULT_MAX = 200
    _IGNORE_DIRS = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".coverage", "htmlcov",
    }

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. "
            "Set recursive=true to explore nested structure. "
            "Common noise directories (.git, node_modules, __pycache__, etc.) are auto-ignored."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The directory path to list"},
                "recursive": {
                    "type": "boolean",
                    "description": "Recursively list all files (default false)",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum entries to return (default 200)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        recursive: bool = False,
        max_entries: int | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            dp = self._resolve(path)
            entry_type = await asyncio.to_thread(self._entry_type_sync, dp)
            if entry_type is None:
                return f"Error: Directory not found: {path}"
            if entry_type != "directory":
                return f"Error: Not a directory: {path}"

            cap = max_entries or self._DEFAULT_MAX

            if recursive:
                items, total = await asyncio.to_thread(self._collect_recursive_entries, dp, cap)
            else:
                items, total = await asyncio.to_thread(self._collect_shallow_entries, dp, cap)

            if not items and total == 0:
                return f"Directory {path} is empty"

            result = "\n".join(items)
            if total > cap:
                result += f"\n\n(truncated, showing first {cap} of {total} entries)"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {e}"

    def _collect_shallow_entries(self, directory: Path, cap: int) -> tuple[list[str], int]:
        items: list[str] = []
        total = 0
        for entry in sorted(self._list_entries_sync(directory), key=lambda item: item["name"]):
            if entry["name"] in self._IGNORE_DIRS:
                continue
            total += 1
            if len(items) < cap:
                prefix = "📁 " if entry["type"] == "directory" else "📄 "
                items.append(f"{prefix}{entry['name']}")
        return items, total

    def _collect_recursive_entries(self, root: Path, cap: int) -> tuple[list[str], int]:
        items: list[str] = []
        total = 0

        def walk(directory: Path) -> None:
            nonlocal total
            for entry in sorted(self._list_entries_sync(directory), key=lambda item: item["name"]):
                if entry["name"] in self._IGNORE_DIRS:
                    continue
                child = directory / entry["name"]
                total += 1
                if len(items) < cap:
                    rel = child.relative_to(root).as_posix()
                    items.append(f"{rel}/" if entry["type"] == "directory" else rel)
                if entry["type"] == "directory":
                    walk(child)

        walk(root)
        return items, total
