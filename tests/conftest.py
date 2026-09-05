"""Fixture-only boundary that prevents tests from touching founder services."""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from os import PathLike
from pathlib import Path
from types import SimpleNamespace
from typing import Concatenate, ParamSpec, TypeVar, cast

import pytest

from cross_agent_chat import claude_runtime, runtime
from cross_agent_chat.core import Route

_SOCKET_ROOT_ENV = "CROSS_AGENT_CHAT_TEST_SOCKET_ROOT"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_BLOCKED_COMMANDS = {"claude", "codex", "launchctl", "lsof", "tailscale"}
CommandPart = str | bytes | PathLike[str] | PathLike[bytes]
Command = CommandPart | Sequence[CommandPart]
P = ParamSpec("P")
R = TypeVar("R")


def _command_parts(command: Command) -> tuple[str, ...]:
    if isinstance(command, (list, tuple)) and all(isinstance(item, str) for item in command):
        return tuple(command)
    if isinstance(command, str):
        return (command,)
    return ()


def _command_is_safe(command: Command, test_root: Path) -> bool:
    parts = _command_parts(command)
    if not parts:
        return False
    binary = Path(parts[0])
    if binary.name in _BLOCKED_COMMANDS:
        return False
    if binary.name in {"sh", "bash"}:
        return len(parts) >= 2 and Path(parts[1]).resolve() == (
            Path(__file__).parents[1] / "install.sh"
        )
    if binary == Path(sys.executable):
        return True
    return binary.is_absolute() and binary.is_relative_to(test_root)


def _guard_process_call(
    call: Callable[Concatenate[Command, P], R], test_root: Path
) -> Callable[Concatenate[Command, P], R]:
    def guarded(command: Command, /, *args: P.args, **kwargs: P.kwargs) -> R:
        if not _command_is_safe(command, test_root):
            raise AssertionError("test attempted a non-fixture subprocess")
        return call(command, *args, **kwargs)

    return guarded


def _guard_subprocess_run(
    call: Callable[Concatenate[Command, P], R], test_root: Path
) -> Callable[Concatenate[Command, P], R]:
    def guarded(command: Command, /, *args: P.args, **kwargs: P.kwargs) -> R:
        parts = _command_parts(command)
        if parts and Path(parts[0]).name in {"launchctl", "lsof", "tailscale"}:
            return cast(
                R,
                subprocess.CompletedProcess(parts, 113, "", "fixture service unavailable"),
            )
        if parts and Path(parts[0]).name == "ps":
            if str(os.getpid()) in parts:
                return cast(R, subprocess.CompletedProcess(parts, 0, "fixture process\n", ""))
            return cast(R, subprocess.CompletedProcess(parts, 1, "", "fixture process unavailable"))
        if not _command_is_safe(command, test_root):
            raise AssertionError("test attempted a non-fixture subprocess")
        return call(command, *args, **kwargs)

    return guarded


def _fixture_socket_root(request: pytest.FixtureRequest) -> Path:
    configured = os.environ.get(_SOCKET_ROOT_ENV)
    if configured is None:
        root = Path(tempfile.mkdtemp(prefix="cac-tests-"))
        root.chmod(0o700)
        request.addfinalizer(lambda: root.rmdir())
    else:
        root = Path(configured)
    metadata = root.stat()
    if (
        not root.is_absolute()
        or not root.is_dir()
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError("test courier socket root is not exclusively owned")
    return root


@pytest.fixture(autouse=True)
def _isolate_founder_surfaces(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Keep provider profiles, sockets, subprocesses, and network fixture-owned."""
    home = tmp_path / "home"
    codex_home = home / ".codex"
    temporary = tmp_path / "tmp"
    for directory in (temporary,):
        directory.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(temporary))

    socket_root = _fixture_socket_root(request)

    def socket_path(root: Path, route: Route) -> Path:
        identity = f"{root.resolve()}:{route.provider}:{route.session_id}:{route.generation}"
        return socket_root / f"{hashlib.sha256(identity.encode()).hexdigest()[:32]}.sock"

    monkeypatch.setattr(runtime, "socket_path", socket_path)
    core_tests = sys.modules.get("test_core")
    if core_tests is not None:
        monkeypatch.setattr(core_tests, "socket_path", socket_path)

    original_temporary_directory = tempfile.TemporaryDirectory

    def temporary_directory(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | PathLike[str] | None = None,
        ignore_cleanup_errors: bool = False,
    ) -> tempfile.TemporaryDirectory[str]:
        del dir
        return original_temporary_directory(
            suffix=suffix,
            prefix=prefix,
            dir=temporary,
            ignore_cleanup_errors=ignore_cleanup_errors,
        )

    monkeypatch.setattr(
        claude_runtime,
        "tempfile",
        SimpleNamespace(TemporaryDirectory=temporary_directory),
    )

    def process_identity(pid: int) -> str | None:
        if pid != os.getpid():
            return None
        return hashlib.sha256(f"fixture-process:{pid}".encode()).hexdigest()

    install_tests = sys.modules.get("test_install")
    if install_tests is not None:
        monkeypatch.setattr(install_tests, "_process_identity_digest", process_identity)

    original_create_connection = socket.create_connection

    def loopback_connection(
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        if address[0] not in _LOOPBACK_HOSTS:
            raise AssertionError("test attempted a non-loopback network connection")
        return original_create_connection(address, timeout, source_address)

    monkeypatch.setattr(socket, "create_connection", loopback_connection)

    monkeypatch.setattr(subprocess, "run", _guard_subprocess_run(subprocess.run, tmp_path))
    monkeypatch.setattr(subprocess, "Popen", _guard_process_call(subprocess.Popen, tmp_path))
