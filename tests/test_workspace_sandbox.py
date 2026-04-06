"""Tests for the long-lived workspace Agent Sandbox integration."""

from __future__ import annotations

import shlex
from types import SimpleNamespace

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.workspace_sandbox import WorkspaceSandboxManager
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import WorkspaceSandboxConfig


class _FakeRunResult:
    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = ""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeSandboxClient:
    instances: list["_FakeSandboxClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {".", ""}
        self.entered = 0
        self.exited = 0
        self.requests: list[tuple[str, str]] = []
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []
        self.list_calls: list[str] = []
        self.exists_calls: list[str] = []
        self.run_calls: list[str] = []
        self.__class__.instances.append(self)

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited += 1
        return False

    def run(self, command: str, timeout: int = 60):
        self.run_calls.append(command)
        parts = shlex.split(command)
        if len(parts) >= 3 and parts[:2] == ["sh", "-lc"]:
            self._apply_script(parts[2])
        return _FakeRunResult()

    def write(self, path: str, content: bytes | str, timeout: int = 60):
        self.write_calls.append(path)
        raw = content.encode("utf-8") if isinstance(content, str) else content
        self._store_file(path, raw)

    def read(self, path: str, timeout: int = 60) -> bytes:
        self.read_calls.append(path)
        return self.files[path]

    def list(self, path: str, timeout: int = 60):
        self.list_calls.append(path)
        norm = "." if path in ("", ".") else path.strip("/")
        prefix = "" if norm == "." else f"{norm}/"
        entries: dict[str, tuple[str, int]] = {}

        for directory in sorted(self.dirs):
            if directory in ("", ".", norm):
                continue
            if not directory.startswith(prefix):
                continue
            remainder = directory[len(prefix):]
            if remainder and "/" not in remainder:
                entries[remainder] = ("directory", 0)

        for file_path, content in self.files.items():
            if not file_path.startswith(prefix):
                continue
            remainder = file_path[len(prefix):]
            if remainder and "/" not in remainder:
                entries[remainder] = ("file", len(content))

        return [
            SimpleNamespace(name=name, size=size, type=file_type, mod_time=0.0)
            for name, (file_type, size) in sorted(entries.items())
        ]

    def exists(self, path: str, timeout: int = 60) -> bool:
        self.exists_calls.append(path)
        norm = "." if path in ("", ".") else path.strip("/")
        return norm in self.files or norm in self.dirs

    def _request(self, method: str, endpoint: str, files=None, timeout: int = 60):
        self.requests.append((method, endpoint))
        if endpoint == "upload" and files:
            filename, content = files["file"]
            raw = content.encode("utf-8") if isinstance(content, str) else content
            self._store_file(filename, raw)
        return SimpleNamespace(status_code=200)

    def _store_file(self, path: str, content: bytes) -> None:
        norm = path.strip("/")
        parent = norm.rsplit("/", 1)[0] if "/" in norm else ""
        if parent:
            self._ensure_dir(parent)
        self.files[norm] = content

    def _ensure_dir(self, path: str) -> None:
        current = path.strip("/")
        while current not in ("", "."):
            self.dirs.add(current)
            if "/" not in current:
                break
            current = current.rsplit("/", 1)[0]

    def _apply_script(self, script: str) -> None:
        tokens = shlex.split(script)
        if tokens[:3] == ["mkdir", "-p", "--"] and len(tokens) >= 4:
            self._ensure_dir(tokens[3])
            return
        if len(tokens) >= 9 and tokens[:3] == ["mkdir", "-p", "--"] and tokens[4:7] == ["&&", "mv", "--"]:
            self._ensure_dir(tokens[3])
            src = tokens[7]
            dst = tokens[8]
            self._store_file(dst, self.files.pop(src))
            return


class _DummyProvider:
    def get_default_model(self) -> str:
        return "test-model"


@pytest.fixture(autouse=True)
def _reset_fake_client():
    _FakeSandboxClient.instances.clear()
    yield
    _FakeSandboxClient.instances.clear()


@pytest.fixture()
def sandboxed_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_sandbox._get_sandbox_client_class",
        lambda: _FakeSandboxClient,
    )
    return WorkspaceSandboxManager(tmp_path, WorkspaceSandboxConfig())


@pytest.mark.asyncio
async def test_workspace_tools_reuse_single_long_lived_sandbox(sandboxed_manager, tmp_path):
    read_tool = ReadFileTool(workspace=tmp_path, workspace_sandbox=sandboxed_manager)
    write_tool = WriteFileTool(workspace=tmp_path, workspace_sandbox=sandboxed_manager)

    file_path = tmp_path / "src" / "main.py"
    await write_tool.execute(path=str(file_path), content="print('hi')\n")
    first = await read_tool.execute(path=str(file_path))
    second = await read_tool.execute(path=str(file_path))

    assert "1| print('hi')" in first
    assert "1| print('hi')" in second
    assert not file_path.exists()
    assert len(_FakeSandboxClient.instances) == 1
    client = _FakeSandboxClient.instances[0]
    assert client.entered == 1
    assert client.exited == 0

    sandboxed_manager.close()
    assert client.exited == 1


@pytest.mark.asyncio
async def test_workspace_tools_use_sandbox_only_for_nested_paths(sandboxed_manager, tmp_path):
    read_tool = ReadFileTool(workspace=tmp_path, workspace_sandbox=sandboxed_manager)
    write_tool = WriteFileTool(workspace=tmp_path, workspace_sandbox=sandboxed_manager)
    edit_tool = EditFileTool(workspace=tmp_path, workspace_sandbox=sandboxed_manager)
    list_tool = ListDirTool(workspace=tmp_path, workspace_sandbox=sandboxed_manager)

    source = tmp_path / "pkg" / "module.py"
    result = await read_tool.execute(path=str(source))
    assert "not found" in result.lower()

    written = await write_tool.execute(path=str(source), content="value = 1\n")
    assert "Successfully" in written
    assert not source.exists()

    edited = await edit_tool.execute(path=str(source), old_text="value = 1", new_text="value = 2")
    assert "Successfully" in edited

    listed = await list_tool.execute(path=str(tmp_path), recursive=True)
    assert "pkg/module.py" in listed

    reread = await read_tool.execute(path=str(source))
    assert "1| value = 2" in reread

    client = _FakeSandboxClient.instances[0]
    assert client.files["pkg/module.py"] == b"value = 2\n"
    assert any(path.startswith(".nanobot-upload-") for path in client.write_calls)


@pytest.mark.asyncio
async def test_read_file_extra_allowed_dir_is_rejected_for_sandbox_only_tools(
    monkeypatch,
    sandboxed_manager,
    tmp_path,
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_file = skills_dir / "weather" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# Weather\nLocal only.\n", encoding="utf-8")

    tool = ReadFileTool(
        workspace=workspace,
        allowed_dir=workspace,
        extra_allowed_dirs=[skills_dir],
        workspace_sandbox=sandboxed_manager,
    )
    result = await tool.execute(path=str(skill_file))

    assert "Error" in result
    assert "workspace" in result.lower()
    assert not _FakeSandboxClient.instances


@pytest.mark.asyncio
async def test_agent_loop_close_mcp_closes_workspace_sandbox(monkeypatch, tmp_path):
    closed: list[str] = []

    class _FakeManager:
        def __init__(self, workspace, config):
            self.workspace = workspace
            self.config = config

        def close(self):
            closed.append("closed")

    monkeypatch.setattr("nanobot.agent.loop.WorkspaceSandboxManager", _FakeManager)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
    )
    await loop.close_mcp()

    assert closed == ["closed"]


def test_agent_loop_registers_exec_with_shared_workspace_sandbox(monkeypatch, tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_sandbox._get_sandbox_client_class",
        lambda: _FakeSandboxClient,
    )

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_DummyProvider(),
        workspace=tmp_path,
    )

    exec_tool = loop.tools.get("exec")
    read_tool = loop.tools.get("read_file")

    assert exec_tool is not None
    assert read_tool is not None
    assert exec_tool._workspace_sandbox is loop.workspace_sandbox
    assert read_tool._workspace_sandbox is loop.workspace_sandbox
