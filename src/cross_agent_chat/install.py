"""Ownership-aware provider setup, background service, and uninstall."""

from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, MutableMapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

import tomlkit
from tomlkit.exceptions import TOMLKitError

from cross_agent_chat import __version__
from cross_agent_chat.core import ChatError, atomic_json, ensure_private_dir, valid_device
from cross_agent_chat.tailnet import LOCAL_BROKER_HOST, LOCAL_BROKER_PORT, valid_tailnet_address

SERVER_NAME: Final = "cross-agent-chat"
LAUNCH_AGENT_LABEL: Final = "io.github.kwonsyup.cross-agent-chat"
BROKER_HEALTH_ATTEMPTS: Final = 300
BROKER_HEALTH_INTERVAL_SECONDS: Final = 0.25
BROKER_HEALTH_WAIT_SECONDS: Final = 75.0
BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS: Final = 10.0
OWNED_TOML_START: Final = "# cross-agent-chat:start"
OWNED_TOML_END: Final = "# cross-agent-chat:end"
RELEASE_MARKER: Final = ".cross-agent-chat-release"
COMMITTED_RELEASE_MARKER: Final = "cross-agent-chat-runtime-v1:committed\n"
STAGED_RELEASE_MARKER_RE: Final = re.compile(
    r"cross-agent-chat-runtime-v1:staged:(?P<pid>[1-9][0-9]*):(?P<identity>[0-9a-f]{64})\n"
)
TRANSACTION_RELEASE_MARKER_RE: Final = re.compile(
    r"cross-agent-chat-runtime-v1:transaction:(?P<transaction>[0-9a-f]{32})\n"
)
OWNED_TOML_RE: Final = re.compile(
    rf"\n?{re.escape(OWNED_TOML_START)}.*?{re.escape(OWNED_TOML_END)}\n?", re.DOTALL
)


class SettingsError(RuntimeError):
    """A safe setup or configuration failure."""


class SetupRollbackError(SettingsError):
    """Setup failed and its provider-file rollback also failed."""

    def __init__(self, failure: Exception, rollback: Exception, backup: Path) -> None:
        super().__init__(f"setup failed: {failure}; setup rollback failed: {rollback}")
        self.failure = failure
        self.rollback = rollback
        self.backup = backup


@dataclass(frozen=True, slots=True)
class InstallReport:
    changed_paths: tuple[Path, ...]
    backup: Path


@dataclass(frozen=True, slots=True)
class PreparedSetup:
    payloads: dict[Path, bytes]
    destinations: dict[Path, Path]
    originals: dict[Path, PathSnapshot]
    backup: Path


@dataclass(frozen=True, slots=True)
class PathSnapshot:
    path: Path
    kind: Literal["absent", "file", "symlink"]
    payload: bytes | None = None
    mode: int | None = None
    target: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerService:
    pid: int
    program: Path


@dataclass(frozen=True, slots=True)
class RecoveryTransaction:
    phase: str
    candidate: Path
    stable_entrypoint: Path
    current_snapshot: PathSnapshot
    entrypoint_snapshot: PathSnapshot
    config_backup: Path
    previous_broker_loaded: bool
    previous_broker_healthy: bool
    package_tree_sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeRemovalPlan:
    current: Path
    owned_releases: tuple[Path, ...]
    entrypoints: tuple[Path, ...]


def default_device() -> str:
    candidate = os.uname().nodename.split(".", maxsplit=1)[0].lower()
    try:
        return valid_device(candidate)
    except RuntimeError as error:
        raise SettingsError("hostname cannot be used as a device name; pass --device") from error


def _json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SettingsError(f"invalid JSON: {path}") from error
    if not isinstance(raw, dict):
        raise SettingsError(f"expected a JSON object: {path}")
    return cast(dict[str, object], raw)


def _json_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _snapshot_path(path: Path) -> PathSnapshot:
    if path.is_symlink():
        return PathSnapshot(path=path, kind="symlink", target=os.readlink(path))
    if not path.exists():
        return PathSnapshot(path=path, kind="absent")
    if not path.is_file():
        raise SettingsError(f"expected a file or symlink: {path}")
    return PathSnapshot(
        path=path,
        kind="file",
        payload=path.read_bytes(),
        mode=stat.S_IMODE(path.stat().st_mode),
    )


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise SettingsError(f"refusing to replace a directory: {path}")


def _atomic_symlink(target: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        os.symlink(target, temporary)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    for item in sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if item.is_symlink():
            continue
        if item.is_dir():
            _fsync_directory(item)
            continue
        descriptor = os.open(item, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _fsync_directory(root)


def _restore_path(snapshot: PathSnapshot) -> None:
    existed = snapshot.path.is_symlink() or snapshot.path.exists()
    _remove_path(snapshot.path)
    if snapshot.kind == "absent":
        if existed:
            _fsync_directory(snapshot.path.parent)
        return
    if snapshot.kind == "symlink":
        if snapshot.target is None:
            raise SettingsError("symlink snapshot is incomplete")
        _atomic_symlink(snapshot.target, snapshot.path)
        return
    if snapshot.payload is None or snapshot.mode is None:
        raise SettingsError("file snapshot is incomplete")
    _atomic_write(snapshot.path, snapshot.payload, snapshot.mode)


def _snapshot_json(snapshot: PathSnapshot) -> dict[str, object]:
    return {
        "kind": snapshot.kind,
        "payload": None
        if snapshot.payload is None
        else base64.b64encode(snapshot.payload).decode("ascii"),
        "mode": snapshot.mode,
        "target": snapshot.target,
    }


def _snapshot_from_json(path: Path, raw: object) -> PathSnapshot:
    if not isinstance(raw, dict):
        raise SettingsError("transaction snapshot is invalid")
    value = cast(dict[object, object], raw)
    if set(value) != {"kind", "payload", "mode", "target"}:
        raise SettingsError("transaction snapshot is invalid")
    kind = value["kind"]
    payload = value["payload"]
    mode = value["mode"]
    target = value["target"]
    if kind == "absent" and payload is None and mode is None and target is None:
        return PathSnapshot(path=path, kind="absent")
    if kind == "symlink" and payload is None and mode is None and isinstance(target, str):
        return PathSnapshot(path=path, kind="symlink", target=target)
    if (
        kind == "file"
        and isinstance(payload, str)
        and isinstance(mode, int)
        and not isinstance(mode, bool)
        and 0 <= mode <= 0o777
        and target is None
    ):
        try:
            decoded = base64.b64decode(payload, validate=True)
        except ValueError as error:
            raise SettingsError("transaction snapshot is invalid") from error
        return PathSnapshot(path=path, kind="file", payload=decoded, mode=mode)
    raise SettingsError("transaction snapshot is invalid")


def _package_tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda candidate: candidate.as_posix()):
        if item.name == RELEASE_MARKER:
            continue
        relative = item.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        if item.is_symlink():
            target = os.readlink(item).encode()
            digest.update(b"L")
            digest.update(len(target).to_bytes(4, "big"))
            digest.update(target)
        elif item.is_file():
            digest.update(b"F")
            digest.update(item.read_bytes())
        elif item.is_dir():
            digest.update(b"D")
    return digest.hexdigest()


def _staged_release_owner(marker: Path) -> tuple[int, str] | None:
    if marker.is_symlink() or not marker.is_file():
        return None
    match = STAGED_RELEASE_MARKER_RE.fullmatch(marker.read_text(encoding="utf-8"))
    return None if match is None else (int(match.group("pid")), match.group("identity"))


def _process_identity_digest(pid: int) -> str | None:
    result = subprocess.run(
        ["/bin/ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5.0,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    payload = result.stdout.encode() if isinstance(result.stdout, str) else result.stdout
    return hashlib.sha256(payload).hexdigest()


def _hook_command(executable: Path, provider: str, device: str, event: str) -> str:
    binary = shlex.quote(str(executable))
    if event == "SessionStart":
        return (
            f"{binary} _register --provider {provider} --device {shlex.quote(device)} "
            '--pid "$PPID" >/dev/null'
        )
    if event == "SessionEnd":
        return f'{binary} _unregister --provider {provider} --pid "$PPID" >/dev/null'
    if provider == "codex" and event == "Stop":
        return f'{binary} _codex-stop --pid "$PPID"'
    raise SettingsError("unsupported provider hook")


def _owned_hook(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    hooks = value.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
        return False
    command = hooks[0].get("command")
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return (
        len(tokens) >= 2
        and Path(tokens[0]).name == SERVER_NAME
        and tokens[1] in {"_register", "_unregister", "_codex-stop"}
    )


def _hook_group(executable: Path, provider: str, device: str, event: str) -> dict[str, object]:
    timeout = 10 if (provider, event) == ("claude", "SessionStart") else 5
    if event in {"SessionEnd", "Stop"}:
        timeout = 3
    return {
        "hooks": [
            {
                "type": "command",
                "command": _hook_command(executable, provider, device, event),
                "timeout": timeout,
            }
        ]
    }


def _merge_hook(config: dict[str, object], event: str, owned: dict[str, object]) -> None:
    hooks = config.get("hooks")
    if hooks is None:
        hook_map: dict[str, object] = {}
        config["hooks"] = hook_map
    elif isinstance(hooks, dict):
        hook_map = cast(dict[str, object], hooks)
    else:
        raise SettingsError("provider hooks must be an object")
    existing = hook_map.get(event, [])
    if not isinstance(existing, list):
        raise SettingsError(f"provider hook {event} must be a list")
    owned_indices = [index for index, item in enumerate(existing) if _owned_hook(item)]
    if len(owned_indices) > 1:
        raise SettingsError(f"provider hook {event} ownership is ambiguous")
    merged: list[object] = []
    replaced = False
    for item in existing:
        if not _owned_hook(item):
            merged.append(item)
        elif not replaced:
            merged.append(owned)
            replaced = True
    if not replaced:
        merged.append(owned)
    hook_map[event] = merged


def _remove_hooks(config: dict[str, object]) -> None:
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return
    hook_map = cast(dict[str, object], hooks)
    for event in list(hook_map):
        values = hook_map[event]
        if not isinstance(values, list):
            continue
        retained = [item for item in values if not _owned_hook(item)]
        if retained:
            hook_map[event] = retained
        else:
            del hook_map[event]
    if not hook_map:
        del config["hooks"]


def _mcp_route(executable: Path, provider: str, device: str) -> dict[str, object]:
    return {
        "type": "stdio",
        "command": str(executable),
        "args": ["_mcp", "--provider", provider, "--device", device],
        "env": {},
    }


def _hook_event_name(event: str) -> str:
    name = {
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "Stop": "stop",
    }.get(event)
    if name is None:
        raise SettingsError("unsupported Codex hook event")
    return name


def _hook_trust_hash(command: str, event: str, timeout: int) -> str:
    identity = {
        "event_name": _hook_event_name(event),
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": timeout,
                "async": False,
            }
        ],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _enable_hooks_feature(text: str) -> str:
    try:
        document = tomlkit.parse(text)
    except TOMLKitError as error:
        raise SettingsError("Codex config.toml is invalid") from error
    features = document.get("features")
    if not isinstance(features, MutableMapping):
        features = tomlkit.table()
        document["features"] = features
    features["hooks"] = True
    return tomlkit.dumps(document)


def _toml_table_path(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    marker = "__cross_agent_chat_table_path__"
    try:
        parsed = tomllib.loads(f"{line.rstrip()}\n{marker} = true\n")
    except tomllib.TOMLDecodeError:
        return None

    def find(value: object, path: tuple[str, ...]) -> tuple[str, ...] | None:
        if not isinstance(value, dict):
            return None
        if value.get(marker) is True:
            return path
        for key, child in value.items():
            if key == marker:
                continue
            found = find(child, (*path, key))
            if found is not None:
                return found
        return None

    return find(parsed, ())


def _toml_string(value: str) -> str:
    short_escapes = {
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
        '"': '\\"',
        "\\": "\\\\",
    }
    encoded: list[str] = []
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise SettingsError("Codex config.toml contains an invalid Unicode value")
        if character in short_escapes:
            encoded.append(short_escapes[character])
        elif codepoint < 0x20 or codepoint == 0x7F:
            encoded.append(f"\\u{codepoint:04X}")
        else:
            encoded.append(character)
    return '"' + "".join(encoded) + '"'


def _remove_owned_codex_tool_approval_overrides(text: str) -> str:
    try:
        document = tomlkit.parse(text)
    except TOMLKitError as error:
        raise SettingsError("Codex config.toml is invalid") from error
    preserved_tools: MutableMapping[str, object] | None = None
    servers = document.get("mcp_servers")
    if isinstance(servers, MutableMapping):
        server = servers.pop(SERVER_NAME, None)
        if isinstance(server, MutableMapping):
            tools = server.get("tools")
            if isinstance(tools, MutableMapping):
                preserved_tools = copy.deepcopy(tools)
                for name in ("chat_peers", "chat_send"):
                    tool = preserved_tools.get(name)
                    if isinstance(tool, MutableMapping):
                        tool.pop("approval_mode", None)
    elif servers is not None:
        raise SettingsError("Codex mcp_servers must be a table")
    cleaned = tomlkit.dumps(document).rstrip()
    if preserved_tools:
        preserved_document = tomlkit.document()
        preserved_servers = tomlkit.table()
        preserved_server = tomlkit.table()
        preserved_tool_table = tomlkit.table()
        for name, value in preserved_tools.items():
            preserved_tool_table.add(name, copy.deepcopy(value))
        preserved_server.add("tools", preserved_tool_table)
        preserved_servers.add(SERVER_NAME, preserved_server)
        preserved_document.add("mcp_servers", preserved_servers)
        preserved = tomlkit.dumps(preserved_document).rstrip()
        cleaned = f"{cleaned}\n\n{preserved}" if cleaned else preserved
    return cleaned + ("\n" if cleaned else "")


def _codex_owned_toml(
    executable: Path,
    device: str,
    hooks_path: Path,
    hook_indices: dict[str, int],
) -> str:
    command = _toml_string(str(executable))
    args = (
        "["
        + ", ".join(
            _toml_string(item) for item in ("_mcp", "--provider", "codex", "--device", device)
        )
        + "]"
    )
    text = (
        f'{OWNED_TOML_START}\n[mcp_servers."{SERVER_NAME}"]\ncommand = {command}\n'
        f'args = {args}\ntool_timeout_sec = 120\ndefault_tools_approval_mode = "approve"\n'
    )
    for event, timeout in (("SessionStart", 5), ("SessionEnd", 3), ("Stop", 3)):
        hook_command = _hook_command(executable, "codex", device, event)
        key = f"{hooks_path}:{_hook_event_name(event)}:{hook_indices[event]}:0"
        text += (
            f"\n[hooks.state.{_toml_string(key)}]\n"
            f"trusted_hash = {_toml_string(_hook_trust_hash(hook_command, event, timeout))}\n"
        )
    return text + f"{OWNED_TOML_END}\n"


def _owned_hook_trust_keys(config: dict[str, object], hooks_path: Path) -> set[str]:
    raw_hooks = config.get("hooks")
    if not isinstance(raw_hooks, dict):
        return set()
    hook_map = cast(dict[str, object], raw_hooks)
    keys: set[str] = set()
    for event in ("SessionStart", "SessionEnd", "Stop"):
        groups = hook_map.get(event)
        if not isinstance(groups, list):
            continue
        for index, item in enumerate(groups):
            if _owned_hook(item):
                keys.add(f"{hooks_path}:{_hook_event_name(event)}:{index}:0")
    return keys


def _remove_owned_hook_trust(text: str, owned_keys: set[str]) -> str:
    removing = False
    retained: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("["):
            path = _toml_table_path(line)
            removing = (
                path is not None
                and len(path) == 3
                and path[:2] == ("hooks", "state")
                and path[2] in owned_keys
            )
        if not removing:
            retained.append(line)
    return "".join(retained)


class Installer:
    """Transactional owner of the Cross Agent Chat config surface."""

    def __init__(
        self,
        *,
        home: Path,
        executable: Path,
        device: str,
        tailnet_address: str | None = None,
    ) -> None:
        self.home = home.resolve()
        self.executable = executable if executable.is_absolute() else executable.absolute()
        self.device = valid_device(device)
        self.tailnet_address = (
            None if tailnet_address is None else valid_tailnet_address(tailnet_address)
        )
        self.state = self.home / ".local" / "state" / SERVER_NAME
        self.install_state = self.home / ".config" / SERVER_NAME / "install.json"
        self.cache = self.home / ".cache" / SERVER_NAME
        self.legacy_peers = self.state / "peers.json"
        self.claude_settings = self.home / ".claude" / "settings.json"
        self.claude_config = self.home / ".claude.json"
        self.codex_config = self.home / ".codex" / "config.toml"
        self.codex_hooks = self.home / ".codex" / "hooks.json"
        self.launch_agent = self.home / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
        self.runtime_root = self.home / ".local" / "share" / f"{SERVER_NAME}-runtime"
        self.releases = self.runtime_root / "releases"
        self.current_runtime = self.runtime_root / "current"
        self.transactions = self.runtime_root / "transactions"
        self.install_lock = self.home / ".local" / "share" / f".{SERVER_NAME}-install.lock"
        self._lock_depth = 0

    @property
    def config_paths(self) -> tuple[Path, ...]:
        return (
            self.claude_settings,
            self.claude_config,
            self.codex_config,
            self.codex_hooks,
            self.launch_agent,
            self.install_state,
        )

    def _validate_runtime_roots(self) -> None:
        for root in (self.runtime_root, self.releases, self.transactions):
            if root.is_symlink():
                raise SettingsError("runtime ownership is invalid")
            if root.exists() and not root.resolve(strict=True).is_relative_to(self.home):
                raise SettingsError("runtime ownership is invalid")

    def _ensure_durable_parent(self, parent: Path) -> None:
        if not self.home.exists():
            self.home.mkdir(mode=0o700, exist_ok=True)
        resolved = parent.resolve(strict=False)
        if not resolved.is_relative_to(self.home):
            raise SettingsError("managed path parent escapes home")
        current = self.home
        for part in resolved.relative_to(self.home).parts:
            current /= part
            if current.exists():
                if not current.is_dir():
                    raise SettingsError("managed path parent is invalid")
            else:
                current.mkdir(mode=0o700, exist_ok=True)
                if not current.is_dir():
                    raise SettingsError("managed path parent is invalid")
                _fsync_directory(current.parent)
        current = resolved
        while True:
            _fsync_directory(current)
            if current == self.home:
                return
            current = current.parent

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        if self._lock_depth > 0:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        local_root = self.home / ".local"
        share_root = local_root / "share"
        try:
            self._ensure_durable_parent(share_root)
        except SettingsError as error:
            raise SettingsError("install lock ownership is invalid") from error
        if not share_root.resolve(strict=True).is_relative_to(self.home.resolve(strict=True)):
            raise SettingsError("install lock ownership is invalid")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.install_lock, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _install_metadata(
        self,
        settings: dict[str, object],
        stable_entrypoint: Path | None = None,
    ) -> dict[str, object]:
        if self.install_state.exists():
            metadata = _json_object(self.install_state)
            previous = metadata.get("claude_cross_session_inbound")
            stable_relative = metadata.get("stable_entrypoint")
            if (
                metadata.get("schema_version") not in {1, 2}
                or not isinstance(previous, dict)
                or set(previous) != {"present", "value"}
                or not isinstance(previous.get("present"), bool)
                or (previous.get("present") is False and previous.get("value") is not None)
            ):
                raise SettingsError("Cross Agent Chat install state is invalid")
            if metadata["schema_version"] == 1:
                if set(metadata) != {"schema_version", "claude_cross_session_inbound"}:
                    raise SettingsError("Cross Agent Chat install state is invalid")
                try:
                    stable = self._validate_stable_entrypoint(self.executable)
                except SettingsError:
                    stable = self._validate_stable_entrypoint(
                        self.home / ".local" / "bin" / SERVER_NAME
                    )
            else:
                if (
                    set(metadata)
                    != {"schema_version", "claude_cross_session_inbound", "stable_entrypoint"}
                    or not isinstance(stable_relative, str)
                    or Path(stable_relative).is_absolute()
                    or ".." in Path(stable_relative).parts
                    or Path(stable_relative).name != SERVER_NAME
                ):
                    raise SettingsError("Cross Agent Chat install state is invalid")
                try:
                    stable = self._validate_stable_entrypoint(self.home / stable_relative)
                except SettingsError as error:
                    raise SettingsError("Cross Agent Chat install state is invalid") from error
            if stable_entrypoint is not None:
                stable = self._validate_stable_entrypoint(stable_entrypoint)
            return {
                "schema_version": 2,
                "claude_cross_session_inbound": previous,
                "stable_entrypoint": str(stable.relative_to(self.home)),
            }
        present = "crossSessionInbound" in settings
        try:
            stable = self._validate_stable_entrypoint(self.executable)
        except SettingsError:
            stable = self._validate_stable_entrypoint(self.home / ".local" / "bin" / SERVER_NAME)
        return {
            "schema_version": 2,
            "claude_cross_session_inbound": {
                "present": present,
                "value": settings.get("crossSessionInbound") if present else None,
            },
            "stable_entrypoint": str(stable.relative_to(self.home)),
        }

    def _launch_agent_payload(self) -> bytes:
        payload: dict[str, object] = {
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": [str(self.executable), "_broker"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
        }
        if self.tailnet_address is not None:
            payload["EnvironmentVariables"] = {
                "CROSS_AGENT_CHAT_TAILNET_ADDRESS": self.tailnet_address
            }
        return plistlib.dumps(payload, sort_keys=True)

    def _codex_config_text(self) -> str | None:
        if not self.codex_config.exists():
            return None
        try:
            return self.codex_config.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise SettingsError(f"Codex config.toml is invalid: {self.codex_config}") from error

    def _payloads(self, stable_entrypoint: Path | None = None) -> dict[Path, bytes]:
        claude_settings = _json_object(self.claude_settings)
        install_metadata = self._install_metadata(claude_settings, stable_entrypoint)
        claude_settings["crossSessionInbound"] = "accept"
        for event in ("SessionStart", "SessionEnd"):
            _merge_hook(
                claude_settings,
                event,
                _hook_group(self.executable, "claude", self.device, event),
            )

        claude_config = _json_object(self.claude_config)
        raw_servers = claude_config.get("mcpServers")
        if raw_servers is None:
            servers: dict[str, object] = {}
            claude_config["mcpServers"] = servers
        elif isinstance(raw_servers, dict):
            servers = cast(dict[str, object], raw_servers)
        else:
            raise SettingsError("Claude mcpServers must be an object")
        servers[SERVER_NAME] = _mcp_route(self.executable, "claude", self.device)

        codex_hooks = _json_object(self.codex_hooks)
        for event in ("SessionStart", "SessionEnd", "Stop"):
            _merge_hook(
                codex_hooks,
                event,
                _hook_group(self.executable, "codex", self.device, event),
            )
        raw_hook_map = codex_hooks.get("hooks")
        if not isinstance(raw_hook_map, dict):
            raise SettingsError("Codex hooks must be an object")
        hook_map = cast(dict[str, object], raw_hook_map)
        hook_indices: dict[str, int] = {}
        for event in ("SessionStart", "SessionEnd", "Stop"):
            groups = hook_map.get(event)
            if not isinstance(groups, list):
                raise SettingsError(f"Codex hook {event} must be a list")
            indices = [index for index, item in enumerate(groups) if _owned_hook(item)]
            if len(indices) != 1:
                raise SettingsError(f"Codex hook {event} ownership is ambiguous")
            hook_indices[event] = indices[0]

        codex_text = self._codex_config_text() or ""
        codex_text = OWNED_TOML_RE.sub("\n", codex_text)
        codex_text = _remove_owned_hook_trust(
            codex_text,
            _owned_hook_trust_keys(codex_hooks, self.codex_hooks),
        ).rstrip()
        codex_text = _remove_owned_codex_tool_approval_overrides(codex_text).rstrip()
        codex_text = _enable_hooks_feature(codex_text).rstrip() + "\n\n"
        codex_text += _codex_owned_toml(
            self.executable,
            self.device,
            self.codex_hooks,
            hook_indices,
        )

        return {
            self.claude_settings: _json_bytes(claude_settings),
            self.claude_config: _json_bytes(claude_config),
            self.codex_config: codex_text.encode(),
            self.codex_hooks: _json_bytes(codex_hooks),
            self.launch_agent: self._launch_agent_payload(),
            self.install_state: _json_bytes(install_metadata),
        }

    def _backup(self, originals: dict[Path, PathSnapshot]) -> Path:
        root = self.home / ".cache" / SERVER_NAME / "backups"
        ensure_private_dir(root)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = root / f"{stamp}-{uuid4().hex[:8]}"
        ensure_private_dir(destination)
        manifest: dict[str, object] = {
            str(path.relative_to(self.home)): _snapshot_json(snapshot)
            for path, snapshot in originals.items()
        }
        _atomic_write(destination / "manifest.json", _json_bytes(manifest))
        _fsync_directory(root)
        _fsync_directory(root.parent)
        _fsync_directory(root.parent.parent)
        _fsync_directory(self.home)
        return destination

    def _configuration_destinations(self) -> dict[Path, Path]:
        destinations: dict[Path, Path] = {}
        for path in self.config_paths:
            try:
                destination = path.resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise SettingsError(f"managed configuration path is invalid: {path}") from error
            if not destination.is_relative_to(self.home):
                raise SettingsError(f"managed configuration path escapes home: {path}")
            destinations[path] = destination
        return destinations

    def _prepare_setup(self, stable_entrypoint: Path | None = None) -> PreparedSetup:
        destinations = self._configuration_destinations()
        payloads = self._payloads(stable_entrypoint)
        originals: dict[Path, PathSnapshot] = {}
        for path, destination in destinations.items():
            if path.is_symlink():
                originals[path] = _snapshot_path(path)
            originals[destination] = _snapshot_path(destination)
        if self.legacy_peers.exists():
            originals[self.legacy_peers] = _snapshot_path(self.legacy_peers)
        backup = self._backup(originals)
        return PreparedSetup(
            payloads=payloads,
            destinations=destinations,
            originals=originals,
            backup=backup,
        )

    def setup(
        self,
        verify: Callable[[], bool] | None = None,
        *,
        prepared: PreparedSetup | None = None,
    ) -> InstallReport:
        with self._exclusive_lock():
            return self._setup(verify, prepared=prepared)

    def _setup(
        self,
        verify: Callable[[], bool] | None = None,
        *,
        prepared: PreparedSetup | None = None,
    ) -> InstallReport:
        transaction = self._prepare_setup() if prepared is None else prepared
        written: list[Path] = []
        try:
            for path, payload in transaction.payloads.items():
                destination = transaction.destinations[path]
                _atomic_write(destination, payload)
                written.append(destination)
            if self.legacy_peers.exists():
                self.legacy_peers.unlink()
                written.append(self.legacy_peers)
            healthy = self.verify_configuration() if verify is None else verify()
            if not healthy:
                raise SettingsError("verification failed after setup")
        except (OSError, SettingsError) as failure:
            try:
                for path in reversed(written):
                    _restore_path(transaction.originals[path])
            except (OSError, SettingsError) as rollback:
                raise SetupRollbackError(failure, rollback, transaction.backup) from failure
            raise
        return InstallReport(tuple(transaction.payloads), transaction.backup)

    def _restore(self, backup: Path) -> None:
        manifest = _json_object(backup / "manifest.json")
        for relative, encoded in manifest.items():
            if not isinstance(relative, str):
                raise SettingsError("backup manifest is invalid")
            relative_path = Path(relative)
            destination = self.home / relative_path
            if (
                relative_path.is_absolute()
                or ".." in relative_path.parts
                or not destination.parent.resolve(strict=False).is_relative_to(self.home)
            ):
                raise SettingsError("backup manifest is invalid")
            if isinstance(encoded, dict):
                _restore_path(_snapshot_from_json(destination, encoded))
            elif encoded is None:
                destination.unlink(missing_ok=True)
            elif isinstance(encoded, str):
                try:
                    payload = base64.b64decode(encoded, validate=True)
                except ValueError as error:
                    raise SettingsError("backup manifest is invalid") from error
                _atomic_write(destination, payload)
            else:
                raise SettingsError("backup manifest is invalid")

    def install(self) -> InstallReport:
        with self._exclusive_lock():
            return self._install()

    def _install(self) -> InstallReport:
        previous_broker_loaded = self.broker_is_loaded()
        previous_broker_healthy = previous_broker_loaded and self._wait_for_previous_broker_health()
        report = self.setup()
        service_transition_started = False
        try:
            self._stop_couriers()
            self._remove_runtime_state()
            service_transition_started = True
            self.activate()
            if not self._wait_for_broker_health(lambda timeout: self.verify(timeout=timeout)):
                raise SettingsError("background broker did not become healthy")
        except (OSError, subprocess.SubprocessError, ChatError, SettingsError) as failure:
            rollback_failures: list[str] = []
            service_stopped = not service_transition_started
            if service_transition_started:
                try:
                    self._stop_broker()
                    service_stopped = True
                except (OSError, subprocess.SubprocessError, SettingsError) as error:
                    rollback_failures.append(f"broker stop failed: {error}")
            try:
                self._restore(report.backup)
            except (OSError, SettingsError) as error:
                rollback_failures.append(f"configuration restore failed: {error}")
            if previous_broker_loaded and (not service_transition_started or service_stopped):
                try:
                    if service_transition_started:
                        self.activate()
                    if previous_broker_healthy:
                        if not self._wait_for_previous_broker_health():
                            raise SettingsError("previous healthy broker did not become healthy")
                    elif not self.broker_is_loaded():
                        raise SettingsError("previous broker did not become loaded")
                except (OSError, subprocess.SubprocessError, ChatError, SettingsError) as error:
                    rollback_failures.append(f"predecessor broker restore failed: {error}")
            if rollback_failures:
                raise SettingsError(
                    f"installation failed: {failure}; rollback failed: "
                    + "; ".join(rollback_failures)
                ) from failure
            raise
        return report

    def _validate_staged_runtime(self, staged_runtime: Path) -> Path:
        self._validate_runtime_roots()
        if staged_runtime.is_symlink() or not staged_runtime.is_dir():
            raise SettingsError("candidate staging failed: staged runtime is unavailable")
        try:
            staged = staged_runtime.resolve(strict=True)
            releases = self.releases.resolve(strict=True)
        except OSError as error:
            raise SettingsError(
                "candidate staging failed: staged runtime is unavailable"
            ) from error
        marker = staged / RELEASE_MARKER
        if (
            not releases.is_relative_to(self.home)
            or staged.parent != releases
            or not staged.name.startswith("release-")
            or _staged_release_owner(marker) is None
        ):
            raise SettingsError(
                "candidate staging failed: staged runtime is outside owned releases"
            )
        candidate = staged / "bin" / SERVER_NAME
        candidate_python = staged / "bin" / "python"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise SettingsError("candidate staging failed: executable is unavailable")
        if not candidate_python.is_file() or not os.access(candidate_python, os.X_OK):
            raise SettingsError("candidate staging failed: Python runtime is unavailable")
        version = subprocess.run(
            [str(candidate), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if version.returncode != 0 or version.stdout.strip() != f"{SERVER_NAME} {__version__}":
            raise SettingsError("candidate staging failed: version verification failed")
        imported = subprocess.run(
            [str(candidate_python), "-c", "import cross_agent_chat"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if imported.returncode != 0:
            raise SettingsError("candidate staging failed: import verification failed")
        broker_smoke = subprocess.run(
            [str(candidate), "_broker", "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if broker_smoke.returncode != 0:
            raise SettingsError("candidate staging failed: broker executable verification failed")
        self._configuration_destinations()
        self._payloads()
        _package_tree_digest(staged)
        return staged

    def _validate_runtime_pointer(self) -> PathSnapshot:
        snapshot = _snapshot_path(self.current_runtime)
        if snapshot.kind == "file":
            raise SettingsError("predecessor state is unknown: current runtime is not a symlink")
        if snapshot.kind == "symlink":
            if snapshot.target is None:
                raise SettingsError("predecessor state is unknown: current runtime is invalid")
            try:
                target = (self.current_runtime.parent / snapshot.target).resolve(strict=True)
                releases = self.releases.resolve(strict=True)
            except OSError as error:
                raise SettingsError(
                    "predecessor state is unknown: current runtime is unavailable"
                ) from error
            marker = target / RELEASE_MARKER
            if (
                target.parent != releases
                or not target.name.startswith("release-")
                or marker.is_symlink()
                or not marker.is_file()
                or marker.read_text(encoding="utf-8") != COMMITTED_RELEASE_MARKER
            ):
                raise SettingsError("predecessor state is unknown: current runtime is not owned")
        return snapshot

    def _validate_stable_entrypoint(self, stable_entrypoint: Path) -> Path:
        if not stable_entrypoint.is_absolute() or stable_entrypoint.name != SERVER_NAME:
            raise SettingsError("stable entrypoint is invalid")
        parent = stable_entrypoint.parent.resolve(strict=False)
        runtime_root = self.runtime_root.resolve(strict=False)
        if not parent.is_relative_to(self.home) or parent.is_relative_to(runtime_root):
            raise SettingsError("stable entrypoint must be owner-local")
        canonical = parent / SERVER_NAME
        _snapshot_path(canonical)
        return canonical

    def _record_transaction(
        self,
        transaction: Path,
        *,
        phase: str,
        staged: Path,
        stable_entrypoint: Path,
        current_snapshot: PathSnapshot,
        entrypoint_snapshot: PathSnapshot,
        config_backup: Path,
        previous_broker_loaded: bool,
        previous_broker_healthy: bool,
        package_tree_sha256: str,
    ) -> None:
        atomic_json(
            transaction / "metadata.json",
            {
                "schema_version": 1,
                "phase": phase,
                "candidate_name": staged.name,
                "stable_entrypoint": str(stable_entrypoint.relative_to(self.home)),
                "current_snapshot": _snapshot_json(current_snapshot),
                "entrypoint_snapshot": _snapshot_json(entrypoint_snapshot),
                "config_backup": str(config_backup.relative_to(self.home)),
                "previous_broker_loaded": previous_broker_loaded,
                "previous_broker_healthy": previous_broker_healthy,
                "package_tree_sha256": package_tree_sha256,
            },
        )

    def _read_transaction(self, transaction: Path) -> RecoveryTransaction:
        metadata = _json_object(transaction / "metadata.json")
        expected_keys = {
            "schema_version",
            "phase",
            "candidate_name",
            "stable_entrypoint",
            "current_snapshot",
            "entrypoint_snapshot",
            "config_backup",
            "previous_broker_loaded",
            "previous_broker_healthy",
            "package_tree_sha256",
        }
        if set(metadata) != expected_keys or metadata.get("schema_version") != 1:
            raise SettingsError("transaction metadata is invalid")
        phase = metadata["phase"]
        candidate_name = metadata["candidate_name"]
        stable_relative = metadata["stable_entrypoint"]
        backup_relative = metadata["config_backup"]
        previous_loaded = metadata["previous_broker_loaded"]
        previous_healthy = metadata["previous_broker_healthy"]
        package_digest = metadata["package_tree_sha256"]
        if (
            not isinstance(phase, str)
            or phase
            not in {
                "prepared",
                "runtime_switching",
                "runtime_switched",
                "config_writing",
                "config_written",
                "service_starting",
                "service_started",
                "committing",
                "committed",
            }
            or not isinstance(candidate_name, str)
            or not candidate_name.startswith("release-")
            or Path(candidate_name).name != candidate_name
            or not isinstance(stable_relative, str)
            or not isinstance(backup_relative, str)
            or not isinstance(previous_loaded, bool)
            or not isinstance(previous_healthy, bool)
            or not isinstance(package_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", package_digest) is None
        ):
            raise SettingsError("transaction metadata is invalid")
        stable_path = Path(stable_relative)
        backup_path = Path(backup_relative)
        if (
            stable_path.is_absolute()
            or backup_path.is_absolute()
            or ".." in stable_path.parts
            or ".." in backup_path.parts
            or stable_path.name != SERVER_NAME
        ):
            raise SettingsError("transaction metadata is invalid")
        stable = self._validate_stable_entrypoint(self.home / stable_path)
        backup = self.home / backup_path
        candidate = self.releases / candidate_name
        if backup.resolve(strict=True).parent != (self.cache / "backups").resolve(strict=True):
            raise SettingsError("transaction metadata is invalid")
        marker = candidate / RELEASE_MARKER
        marker_text = (
            marker.read_text(encoding="utf-8")
            if marker.is_file() and not marker.is_symlink()
            else ""
        )
        if phase == "committed":
            marker_matches = marker_text == COMMITTED_RELEASE_MARKER
        elif phase == "committing":
            marker_matches = marker_text in {
                COMMITTED_RELEASE_MARKER,
                f"cross-agent-chat-runtime-v1:transaction:{transaction.name}\n",
            }
        else:
            marker_matches = marker_text == (
                f"cross-agent-chat-runtime-v1:transaction:{transaction.name}\n"
            )
        if re.fullmatch(r"[0-9a-f]{32}", transaction.name) is None or not marker_matches:
            raise SettingsError("transaction metadata is invalid")
        return RecoveryTransaction(
            phase=phase,
            candidate=candidate,
            stable_entrypoint=stable,
            current_snapshot=_snapshot_from_json(
                self.current_runtime, metadata["current_snapshot"]
            ),
            entrypoint_snapshot=_snapshot_from_json(stable, metadata["entrypoint_snapshot"]),
            config_backup=backup,
            previous_broker_loaded=previous_loaded,
            previous_broker_healthy=previous_healthy,
            package_tree_sha256=package_digest,
        )

    def _rollback_transition(
        self,
        *,
        transaction_path: Path,
        config_backup: Path,
        current_snapshot: PathSnapshot,
        entrypoint_snapshot: PathSnapshot,
        candidate: Path,
        runtime_transition_started: bool,
        config_transition_started: bool,
        service_transition_started: bool,
        previous_broker_loaded: bool,
        previous_broker_healthy: bool,
        cleanup_on_success: bool = True,
    ) -> None:
        failures: list[str] = []
        service_stopped = not service_transition_started
        if service_transition_started:
            try:
                self._stop_broker()
                service_stopped = True
            except (OSError, subprocess.SubprocessError, SettingsError) as error:
                failures.append(f"broker stop failed: {error}")
        if config_transition_started:
            try:
                self._restore(config_backup)
            except (OSError, SettingsError) as error:
                failures.append(f"configuration restore failed: {error}")
        if runtime_transition_started:
            for label, snapshot in (
                ("runtime pointer", current_snapshot),
                ("public entrypoint", entrypoint_snapshot),
            ):
                try:
                    _restore_path(snapshot)
                except (OSError, SettingsError) as error:
                    failures.append(f"{label} restore failed: {error}")
        if service_transition_started and previous_broker_loaded and service_stopped:
            try:
                self.activate()
                if previous_broker_healthy:
                    if not self._wait_for_previous_broker_health():
                        raise SettingsError("previous broker did not become healthy")
                elif not self.broker_is_loaded():
                    raise SettingsError("previous broker did not become loaded")
            except (OSError, subprocess.SubprocessError, ChatError, SettingsError) as error:
                failures.append(f"predecessor broker restore failed: {error}")
        if failures:
            raise SettingsError("; ".join(failures))
        if cleanup_on_success:
            shutil.rmtree(transaction_path)
            _fsync_directory(transaction_path.parent)
            if candidate.exists():
                self._remove_release(candidate)

    def _recover_unfinished_transaction(self) -> None:
        self._validate_runtime_roots()
        if not self.transactions.exists():
            return
        for preparing in self.transactions.glob(".preparing-*"):
            if preparing.is_dir() and not preparing.is_symlink():
                shutil.rmtree(preparing)
        for residue in self.transactions.iterdir():
            if residue.is_file() and not residue.is_symlink():
                residue.unlink()
        entries = list(self.transactions.iterdir())
        if not entries:
            return
        if len(entries) != 1 or entries[0].is_symlink() or not entries[0].is_dir():
            paths = ", ".join(str(entry) for entry in entries)
            raise SettingsError(
                f"predecessor state is unknown: transaction state is ambiguous: {paths}"
            )
        transaction_path = entries[0]
        try:
            transaction = self._read_transaction(transaction_path)
        except SettingsError as error:
            raise SettingsError(
                f"transaction state at {transaction_path} is invalid: {error}"
            ) from error
        if transaction.phase == "committed":
            try:
                if (
                    self.current_runtime.resolve(strict=True)
                    != transaction.candidate.resolve(strict=True)
                    or transaction.stable_entrypoint.resolve(strict=True)
                    != transaction.candidate / "bin" / SERVER_NAME
                    or _package_tree_digest(transaction.candidate)
                    != transaction.package_tree_sha256
                ):
                    raise SettingsError("committed transaction state is invalid")
            except OSError as error:
                raise SettingsError("committed transaction state is invalid") from error
            shutil.rmtree(transaction_path)
            _fsync_directory(transaction_path.parent)
            return
        if transaction.phase == "prepared":
            shutil.rmtree(transaction_path)
            _fsync_directory(transaction_path.parent)
            if transaction.candidate.exists():
                self._remove_release(transaction.candidate)
            return
        try:
            self._rollback_transition(
                transaction_path=transaction_path,
                config_backup=transaction.config_backup,
                current_snapshot=transaction.current_snapshot,
                entrypoint_snapshot=transaction.entrypoint_snapshot,
                candidate=transaction.candidate,
                runtime_transition_started=True,
                config_transition_started=transaction.phase
                in {
                    "config_writing",
                    "config_written",
                    "service_starting",
                    "service_started",
                    "committing",
                },
                service_transition_started=transaction.phase
                in {"service_starting", "service_started", "committing"},
                previous_broker_loaded=transaction.previous_broker_loaded,
                previous_broker_healthy=transaction.previous_broker_healthy,
            )
        except (OSError, subprocess.SubprocessError, ChatError, SettingsError) as error:
            raise SettingsError(f"unfinished transaction recovery failed: {error}") from error

    def _remove_release(self, release: Path) -> None:
        self._validate_runtime_roots()
        if release.is_symlink() or not release.is_dir() or not release.name.startswith("release-"):
            raise SettingsError("refusing to remove an unowned runtime")
        releases = self.releases.resolve(strict=True)
        resolved = release.resolve(strict=True)
        marker = resolved / RELEASE_MARKER
        marker_text = marker.read_text(encoding="utf-8") if marker.is_file() else ""
        if (
            not releases.is_relative_to(self.home)
            or release.parent.resolve(strict=True) != releases
            or resolved.parent != releases
            or marker.is_symlink()
            or not marker.is_file()
            or (
                marker_text != COMMITTED_RELEASE_MARKER
                and STAGED_RELEASE_MARKER_RE.fullmatch(marker_text) is None
                and TRANSACTION_RELEASE_MARKER_RE.fullmatch(marker_text) is None
            )
        ):
            raise SettingsError("refusing to remove an unowned runtime")
        shutil.rmtree(resolved)

    def _prune_committed_releases(self, current: Path, previous: PathSnapshot) -> None:
        retained = {current.resolve()}
        if previous.kind == "symlink" and previous.target is not None:
            retained.add((self.current_runtime.parent / previous.target).resolve())
        for release in self.releases.iterdir():
            marker = release / RELEASE_MARKER
            if (
                release.is_dir()
                and not release.is_symlink()
                and release.resolve() not in retained
                and marker.is_file()
                and not marker.is_symlink()
                and marker.read_text(encoding="utf-8") == COMMITTED_RELEASE_MARKER
            ):
                self._remove_release(release)

    def _prune_abandoned_staged_releases(self, retained: set[Path] | None = None) -> None:
        self._validate_runtime_roots()
        if not self.releases.exists():
            return
        keep = set() if retained is None else {path.resolve() for path in retained}
        for release in self.releases.iterdir():
            if release.is_symlink() or not release.is_dir() or release.resolve() in keep:
                continue
            marker = release / RELEASE_MARKER
            owner = _staged_release_owner(marker)
            marker_text = (
                marker.read_text(encoding="utf-8")
                if marker.is_file() and not marker.is_symlink()
                else ""
            )
            transaction_owner = TRANSACTION_RELEASE_MARKER_RE.fullmatch(marker_text)
            if (owner is not None and _process_identity_digest(owner[0]) != owner[1]) or (
                transaction_owner is not None
                and not (self.transactions / transaction_owner.group("transaction")).exists()
            ):
                self._remove_release(release)

    def install_staged(self, staged_runtime: Path, stable_entrypoint: Path) -> InstallReport:
        with self._exclusive_lock():
            return self._install_staged(staged_runtime, stable_entrypoint)

    def _install_staged(self, staged_runtime: Path, stable_entrypoint: Path) -> InstallReport:
        staged = self._validate_staged_runtime(staged_runtime)
        self._recover_unfinished_transaction()
        self._prune_abandoned_staged_releases({staged})
        stable = self._validate_stable_entrypoint(stable_entrypoint)
        self.executable = stable
        current_snapshot = self._validate_runtime_pointer()
        entrypoint_snapshot = _snapshot_path(stable)
        previous_broker_loaded = self.broker_is_loaded()
        if not previous_broker_loaded and not self._broker_port_is_available():
            raise SettingsError("predecessor broker state is unmanaged; no changes were made")
        previous_broker_healthy = previous_broker_loaded and self._wait_for_previous_broker_health()
        prepared_setup = self._prepare_setup(stable)
        for parent in {
            stable.parent,
            *(destination.parent for destination in prepared_setup.destinations.values()),
        }:
            self._ensure_durable_parent(parent)
        _fsync_tree(staged)
        package_digest = _package_tree_digest(staged)

        ensure_private_dir(self.transactions)
        _fsync_directory(self.runtime_root)
        _fsync_directory(self.runtime_root.parent)
        _fsync_directory(self.runtime_root.parent.parent)
        _fsync_directory(self.home)
        transaction_id = uuid4().hex
        preparing_transaction = self.transactions / f".preparing-{transaction_id}"
        transaction = self.transactions / transaction_id
        ensure_private_dir(preparing_transaction)
        final_runtime = staged

        def record(phase: str) -> None:
            self._record_transaction(
                transaction,
                phase=phase,
                staged=final_runtime,
                stable_entrypoint=stable,
                current_snapshot=current_snapshot,
                entrypoint_snapshot=entrypoint_snapshot,
                config_backup=prepared_setup.backup,
                previous_broker_loaded=previous_broker_loaded,
                previous_broker_healthy=previous_broker_healthy,
                package_tree_sha256=package_digest,
            )

        self._record_transaction(
            preparing_transaction,
            phase="prepared",
            staged=final_runtime,
            stable_entrypoint=stable,
            current_snapshot=current_snapshot,
            entrypoint_snapshot=entrypoint_snapshot,
            config_backup=prepared_setup.backup,
            previous_broker_loaded=previous_broker_loaded,
            previous_broker_healthy=previous_broker_healthy,
            package_tree_sha256=package_digest,
        )
        _atomic_write(
            final_runtime / RELEASE_MARKER,
            f"cross-agent-chat-runtime-v1:transaction:{transaction_id}\n".encode(),
        )
        _fsync_directory(self.releases)
        os.replace(preparing_transaction, transaction)
        _fsync_directory(self.transactions)
        report: InstallReport | None = None
        service_transition_started = False
        runtime_transition_started = False
        config_transition_started = False
        try:
            self._stop_couriers()
            record("runtime_switching")
            runtime_transition_started = True
            _atomic_symlink(f"releases/{final_runtime.name}", self.current_runtime)
            _atomic_symlink(str(self.current_runtime / "bin" / SERVER_NAME), stable)
            record("runtime_switched")
            record("config_writing")
            config_transition_started = True
            report = self.setup(prepared=prepared_setup)
            record("config_written")
            self._remove_runtime_state()
            record("service_starting")
            service_transition_started = True
            self.activate()
            record("service_started")
            if not self._wait_for_broker_health(
                lambda timeout: (
                    self.verify(timeout=timeout)
                    and _package_tree_digest(final_runtime) == package_digest
                )
            ):
                raise SettingsError("background broker did not become healthy")
            record("committing")
            _atomic_write(final_runtime / RELEASE_MARKER, COMMITTED_RELEASE_MARKER.encode())
        except (OSError, subprocess.SubprocessError, ChatError, SettingsError) as failure:
            setup_rollback_failed = isinstance(failure, SetupRollbackError)
            try:
                self._rollback_transition(
                    transaction_path=transaction,
                    config_backup=prepared_setup.backup,
                    current_snapshot=current_snapshot,
                    entrypoint_snapshot=entrypoint_snapshot,
                    candidate=final_runtime,
                    runtime_transition_started=runtime_transition_started,
                    config_transition_started=config_transition_started,
                    service_transition_started=service_transition_started,
                    previous_broker_loaded=previous_broker_loaded,
                    previous_broker_healthy=previous_broker_healthy,
                    cleanup_on_success=not setup_rollback_failed,
                )
                if setup_rollback_failed:
                    raise SettingsError(str(failure))
            except (OSError, subprocess.SubprocessError, ChatError, SettingsError) as rollback:
                raise SettingsError(
                    f"transition and rollback failed: {failure}; rollback failure: {rollback}"
                ) from failure
            raise SettingsError(f"transition failed but rollback succeeded: {failure}") from failure

        if report is None:
            raise SettingsError("installation transaction did not produce a report")
        record("committed")
        self._prune_committed_releases(final_runtime, current_snapshot)
        shutil.rmtree(transaction)
        _fsync_directory(self.transactions)
        return report

    def verify_configuration(self) -> bool:
        try:
            claude = _json_object(self.claude_config)
            servers = claude.get("mcpServers")
            settings = _json_object(self.claude_settings)
            codex_hooks = _json_object(self.codex_hooks)
            codex_text = self._codex_config_text()
            if codex_text is None:
                return False
            parsed_codex: object = tomllib.loads(codex_text)
            if not isinstance(parsed_codex, dict):
                return False
            features = parsed_codex.get("features")
            if not isinstance(features, dict) or features.get("hooks") is not True:
                return False
            codex_servers = parsed_codex.get("mcp_servers")
            if not isinstance(codex_servers, dict):
                return False
            codex_server = codex_servers.get(SERVER_NAME)
            if not isinstance(codex_server, dict):
                return False
            if codex_server.get("default_tools_approval_mode") != "approve":
                return False
            codex_tools = codex_server.get("tools")
            if codex_tools is not None:
                if not isinstance(codex_tools, dict):
                    return False
                for name in ("chat_peers", "chat_send"):
                    tool = codex_tools.get(name)
                    if not isinstance(tool, dict):
                        continue
                    if tool.get("approval_mode", "approve") != "approve":
                        return False
            if not isinstance(servers, dict) or SERVER_NAME not in servers:
                return False
            if settings.get("crossSessionInbound") != "accept":
                return False
            if OWNED_TOML_START not in codex_text or OWNED_TOML_END not in codex_text:
                return False
            launch_agent = plistlib.loads(self.launch_agent.read_bytes())
            if launch_agent != plistlib.loads(self._launch_agent_payload()):
                return False
            self._install_metadata(settings)
            for config, events in (
                (settings, ("SessionStart", "SessionEnd")),
                (codex_hooks, ("SessionStart", "SessionEnd", "Stop")),
            ):
                raw_hooks = config.get("hooks")
                if not isinstance(raw_hooks, dict):
                    return False
                for event in events:
                    groups = raw_hooks.get(event)
                    if not isinstance(groups, list):
                        return False
                    if len([item for item in groups if _owned_hook(item)]) != 1:
                        return False
            return True
        except (
            OSError,
            plistlib.InvalidFileException,
            SettingsError,
            RuntimeError,
            tomllib.TOMLDecodeError,
        ):
            return False

    def _broker_service(self, *, timeout: float = 5.0) -> BrokerService | None:
        service = f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"
        try:
            result = subprocess.run(
                ["launchctl", "print", service],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SettingsError("could not determine previous broker state") from error
        if result.returncode != 0:
            return None
        state = re.search(r"^\s*state = (?P<state>\S+)\s*$", result.stdout, re.MULTILINE)
        pid = re.search(r"^\s*pid = (?P<pid>[0-9]+)\s*$", result.stdout, re.MULTILINE)
        program = re.search(r"^\s*program = (?P<program>.+?)\s*$", result.stdout, re.MULTILINE)
        if state is None or state.group("state") != "running" or pid is None or program is None:
            return None
        return BrokerService(pid=int(pid.group("pid")), program=Path(program.group("program")))

    def broker_is_loaded(self) -> bool:
        service = f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"
        try:
            return (
                subprocess.run(
                    ["launchctl", "print", service],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                ).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SettingsError("could not determine previous broker state") from error

    def broker_is_healthy(self, *, timeout: float = BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS) -> bool:
        deadline = time.monotonic() + timeout
        try:
            service = self._broker_service(timeout=min(5.0, timeout))
        except SettingsError:
            return False
        if service is None or service.program != self.executable:
            return False
        try:
            from cross_agent_chat.runtime import request_tailnet

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            response = request_tailnet(
                LOCAL_BROKER_HOST,
                {"schema_version": 1, "operation": "health"},
                port=LOCAL_BROKER_PORT,
                timeout=min(BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS, remaining),
            )
        except RuntimeError:
            return False
        pid = response.get("pid")
        version = response.get("version")
        module_path = response.get("module_path")
        if (
            response.get("schema_version") != 1
            or response.get("status") != "READY"
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid != service.pid
            or version != __version__
            or not isinstance(module_path, str)
        ):
            return False
        try:
            runtime = self.executable.resolve(strict=True).parent.parent
            module = Path(module_path).resolve(strict=True)
        except OSError:
            return False
        return module.is_relative_to(runtime)

    def _local_broker_listener_pid(self, *, timeout: float = 5.0) -> int | None:
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{LOCAL_BROKER_PORT}", "-sTCP:LISTEN"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        raw_pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or len(raw_pids) != 1 or not raw_pids[0].isdigit():
            return None
        return int(raw_pids[0])

    def _previous_broker_is_healthy(self, *, deadline: float | None = None) -> bool:
        health_deadline = (
            deadline if deadline is not None else time.monotonic() + BROKER_HEALTH_WAIT_SECONDS
        )
        try:
            remaining = health_deadline - time.monotonic()
            if remaining <= 0:
                return False
            service = self._broker_service(timeout=min(5.0, remaining))
            remaining = health_deadline - time.monotonic()
            if (
                service is None
                or remaining <= 0
                or self._local_broker_listener_pid(timeout=min(5.0, remaining)) != service.pid
            ):
                return False
            from cross_agent_chat.runtime import request_tailnet

            remaining = health_deadline - time.monotonic()
            if remaining <= 0:
                return False
            response = request_tailnet(
                LOCAL_BROKER_HOST,
                {"schema_version": 1, "operation": "health"},
                port=LOCAL_BROKER_PORT,
                timeout=min(BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS, remaining),
            )
        except (OSError, RuntimeError, SettingsError, subprocess.SubprocessError):
            return False
        if response.get("schema_version") != 1 or response.get("status") != "READY":
            return False
        identity_fields = ("pid", "version", "module_path")
        if not any(field in response for field in identity_fields):
            return True
        pid = response.get("pid")
        version = response.get("version")
        module_path = response.get("module_path")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid != service.pid
            or not isinstance(version, str)
            or not version
            or not isinstance(module_path, str)
        ):
            return False
        try:
            runtime = service.program.resolve(strict=True).parent.parent
            module = Path(module_path).resolve(strict=True)
            remaining = health_deadline - time.monotonic()
            if remaining <= 0:
                return False
            reported = subprocess.run(
                [str(service.program), "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=min(5.0, remaining),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return (
            module.is_relative_to(runtime)
            and reported.returncode == 0
            and reported.stdout.strip() == f"{SERVER_NAME} {version}"
        )

    def _wait_for_previous_broker_health(self) -> bool:
        deadline = time.monotonic() + BROKER_HEALTH_WAIT_SECONDS
        for attempt in range(BROKER_HEALTH_ATTEMPTS):
            if self._previous_broker_is_healthy(deadline=deadline):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if attempt < BROKER_HEALTH_ATTEMPTS - 1:
                time.sleep(min(BROKER_HEALTH_INTERVAL_SECONDS, remaining))
        return False

    def _wait_for_broker_health(self, check: Callable[[float], bool] | None = None) -> bool:
        health_check = check or (lambda timeout: self.broker_is_healthy(timeout=timeout))
        deadline = time.monotonic() + BROKER_HEALTH_WAIT_SECONDS
        for attempt in range(BROKER_HEALTH_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if health_check(min(BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS, remaining)):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if attempt < BROKER_HEALTH_ATTEMPTS - 1:
                time.sleep(min(BROKER_HEALTH_INTERVAL_SECONDS, remaining))
        return False

    def verify(self, *, timeout: float = BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS) -> bool:
        return self.verify_configuration() and self.broker_is_healthy(timeout=timeout)

    def _broker_port_is_available(self) -> bool:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((LOCAL_BROKER_HOST, LOCAL_BROKER_PORT))
            listener.listen(1)
            readback = subprocess.run(
                [
                    "/usr/sbin/lsof",
                    "-nP",
                    f"-iTCP:{LOCAL_BROKER_PORT}",
                    "-sTCP:LISTEN",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            rows = [line.split() for line in readback.stdout.splitlines()[1:] if line.split()]
            return (
                readback.returncode == 0
                and len(rows) == 1
                and len(rows[0]) > 1
                and rows[0][1] == str(os.getpid())
            )
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            listener.close()

    def _wait_for_broker_port_release(self) -> bool:
        for attempt in range(20):
            if self._broker_port_is_available():
                return True
            if attempt < 19:
                time.sleep(0.1)
        return False

    def _require_broker_released(self, message: str) -> None:
        if self._broker_port_is_available():
            return
        raise SettingsError(message)

    def _stop_owned_orphan_broker(self) -> None:
        try:
            self._stop_owned_orphan_broker_checked()
        except (OSError, subprocess.SubprocessError):
            self._require_broker_released("local broker port is occupied by an unverified process")

    def _stop_owned_orphan_broker_checked(self) -> None:
        if self._broker_port_is_available():
            return
        listener = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{LOCAL_BROKER_PORT}", "-sTCP:LISTEN"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        raw_pids = [line.strip() for line in listener.stdout.splitlines() if line.strip()]
        if listener.returncode != 0 or len(raw_pids) != 1 or not raw_pids[0].isdigit():
            self._require_broker_released("local broker port is occupied by an unverified process")
            return
        pid = int(raw_pids[0])
        process_identity = _process_identity_digest(pid)
        if process_identity is None:
            self._require_broker_released("local broker port is occupied by an unverified process")
            return
        uid = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "uid="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        command = subprocess.run(
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        try:
            tokens = shlex.split(command.stdout.strip())
            broker_index = tokens.index("_broker")
            broker_executable = Path(tokens[broker_index - 1])
        except (ValueError, IndexError):
            self._require_broker_released("local broker port is occupied by an unverified process")
            return
        try:
            resolved_executable = broker_executable.resolve(strict=True)
        except OSError:
            self._require_broker_released("local broker port is occupied by an unverified process")
            return
        if (
            uid.returncode != 0
            or uid.stdout.strip() != str(os.getuid())
            or command.returncode != 0
            or broker_index == 0
            or broker_executable.name != SERVER_NAME
            or not broker_executable.is_absolute()
            or not resolved_executable.is_relative_to(self.home)
        ):
            self._require_broker_released("local broker port is occupied by an unverified process")
            return
        version = subprocess.run(
            [str(resolved_executable), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if (
            version.returncode != 0
            or re.fullmatch(rf"{re.escape(SERVER_NAME)} [0-9]+\.[0-9]+\.[0-9]+\s*", version.stdout)
            is None
        ):
            self._require_broker_released("local broker port is occupied by an unverified process")
            return
        if (
            self._local_broker_listener_pid() != pid
            or _process_identity_digest(pid) != process_identity
        ):
            self._require_broker_released("local broker process changed before termination")
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._require_broker_released("local broker process changed before termination")
            return
        if not self._wait_for_broker_port_release():
            raise SettingsError("owned predecessor broker did not stop")

    def _stop_broker(self) -> None:
        self._bootout()
        if not self._wait_for_broker_port_release():
            self._stop_owned_orphan_broker()

    def activate(self) -> None:
        domain = f"gui/{os.getuid()}"
        service = f"{domain}/{LAUNCH_AGENT_LABEL}"
        loaded = (
            subprocess.run(
                ["launchctl", "print", service],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            ).returncode
            == 0
        )
        if loaded:
            self._stop_broker()
        elif not self._wait_for_broker_port_release():
            self._stop_owned_orphan_broker()
        started = subprocess.run(
            ["launchctl", "bootstrap", domain, str(self.launch_agent)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if started.returncode != 0:
            time.sleep(0.25)
            started = subprocess.run(
                ["launchctl", "bootstrap", domain, str(self.launch_agent)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
        if started.returncode != 0:
            raise SettingsError("could not start the background broker")

    def _bootout(self) -> None:
        domain = f"gui/{os.getuid()}"
        service = f"{domain}/{LAUNCH_AGENT_LABEL}"
        existing = subprocess.run(
            ["launchctl", "print", service],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if existing.returncode != 0:
            return
        stopped = subprocess.run(
            ["launchctl", "bootout", f"{domain}/{LAUNCH_AGENT_LABEL}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if stopped.returncode != 0:
            raise SettingsError("could not stop the background broker")
        for attempt in range(20):
            removed = (
                subprocess.run(
                    ["launchctl", "print", service],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                ).returncode
                != 0
            )
            if removed:
                return
            if attempt < 19:
                time.sleep(0.1)
        raise SettingsError("could not stop the background broker")

    def _stop_couriers(self) -> None:
        if not self.state.exists():
            return
        from cross_agent_chat.runtime import shutdown_couriers

        shutdown_couriers(self.state)

    def _remove_runtime_state(self) -> None:
        if self.state.exists():
            shutil.rmtree(self.state)

    def _prepare_runtime_removal(self, stable_entrypoint: Path | None) -> RuntimeRemovalPlan | None:
        self._validate_runtime_roots()
        self._prune_abandoned_staged_releases()
        if not self.current_runtime.is_symlink():
            owned_residue = False
            if self.releases.exists():
                for release in self.releases.iterdir():
                    marker = release / RELEASE_MARKER
                    marker_text = (
                        marker.read_text(encoding="utf-8")
                        if marker.is_file() and not marker.is_symlink()
                        else ""
                    )
                    if (
                        marker_text == COMMITTED_RELEASE_MARKER
                        or STAGED_RELEASE_MARKER_RE.fullmatch(marker_text) is not None
                        or TRANSACTION_RELEASE_MARKER_RE.fullmatch(marker_text) is not None
                    ):
                        owned_residue = True
                        break
            if self.current_runtime.exists() or owned_residue:
                raise SettingsError("installed runtime ownership is invalid")
            return None
        try:
            current = self.current_runtime.resolve(strict=True)
            releases = self.releases.resolve(strict=True)
            executable = self.executable.resolve(strict=True)
        except OSError as error:
            raise SettingsError("installed runtime ownership is invalid") from error
        expected = current / "bin" / SERVER_NAME
        current_marker = current / RELEASE_MARKER
        if (
            current.parent != releases
            or not releases.is_relative_to(self.home)
            or not current.name.startswith("release-")
            or executable != expected
            or current_marker.is_symlink()
            or not current_marker.is_file()
            or current_marker.read_text(encoding="utf-8") != COMMITTED_RELEASE_MARKER
        ):
            raise SettingsError("installed runtime ownership is invalid")
        owned_releases = tuple(
            release
            for release in self.releases.iterdir()
            if release.is_dir()
            and not release.is_symlink()
            and release.name.startswith("release-")
            and (release / RELEASE_MARKER).is_file()
            and not (release / RELEASE_MARKER).is_symlink()
            and (release / RELEASE_MARKER).read_text(encoding="utf-8") == COMMITTED_RELEASE_MARKER
        )
        entrypoints: list[Path] = []
        if stable_entrypoint is not None and stable_entrypoint.is_symlink():
            try:
                if stable_entrypoint.resolve(strict=True) == expected:
                    entrypoints.append(stable_entrypoint)
            except OSError as error:
                raise SettingsError("installed runtime ownership is invalid") from error
        if self.executable.is_symlink() and self.executable not in entrypoints:
            try:
                if self.executable.resolve(strict=True) == expected:
                    entrypoints.append(self.executable)
            except OSError as error:
                raise SettingsError("installed runtime ownership is invalid") from error
        return RuntimeRemovalPlan(
            current=current,
            owned_releases=owned_releases,
            entrypoints=tuple(entrypoints),
        )

    def _remove_installed_runtime(self, plan: RuntimeRemovalPlan | None) -> None:
        if plan is None:
            self._prune_abandoned_staged_releases()
            return
        expected = plan.current / "bin" / SERVER_NAME
        try:
            if self.current_runtime.resolve(strict=True) != plan.current:
                raise SettingsError("installed runtime ownership changed during uninstall")
            for entrypoint in plan.entrypoints:
                if not entrypoint.is_symlink() or entrypoint.resolve(strict=True) != expected:
                    raise SettingsError("installed runtime ownership changed during uninstall")
        except OSError as error:
            raise SettingsError("installed runtime ownership changed during uninstall") from error
        self._prune_abandoned_staged_releases()
        for public_entrypoint in plan.entrypoints:
            public_entrypoint.unlink()
        self.current_runtime.unlink()
        for release in plan.owned_releases:
            self._remove_release(release)
        with suppress(OSError):
            self.releases.rmdir()
        if self.transactions.exists():
            shutil.rmtree(self.transactions)
        with suppress(OSError):
            self.runtime_root.rmdir()

    def uninstall(self) -> None:
        with self._exclusive_lock():
            self._uninstall()

    def _uninstall(self) -> None:
        self._codex_config_text()
        self._recover_unfinished_transaction()
        destinations = self._configuration_destinations()
        metadata = self._install_metadata(_json_object(self.claude_settings))
        stable_entrypoint = self.home / cast(str, metadata["stable_entrypoint"])
        runtime_removal = self._prepare_runtime_removal(stable_entrypoint)
        self._stop_broker()
        self._stop_couriers()
        self._remove_runtime_state()
        codex_hooks = _json_object(self.codex_hooks)
        owned_trust_keys = _owned_hook_trust_keys(codex_hooks, self.codex_hooks)
        codex_text = self._codex_config_text()
        if codex_text is not None:
            stripped = _remove_owned_hook_trust(
                OWNED_TOML_RE.sub("\n", codex_text), owned_trust_keys
            )
            if stripped != codex_text:
                codex_destination = self._configuration_destinations()[self.codex_config]
                _atomic_write(codex_destination, stripped.lstrip("\n").encode())
        claude_settings = _json_object(self.claude_settings)
        _remove_hooks(claude_settings)
        previous = cast(dict[str, object], metadata["claude_cross_session_inbound"])
        if claude_settings.get("crossSessionInbound") == "accept":
            if cast(bool, previous["present"]):
                claude_settings["crossSessionInbound"] = previous["value"]
            else:
                claude_settings.pop("crossSessionInbound", None)
        _atomic_write(destinations[self.claude_settings], _json_bytes(claude_settings))

        claude = _json_object(self.claude_config)
        servers = claude.get("mcpServers")
        if isinstance(servers, dict):
            cast(dict[str, object], servers).pop(SERVER_NAME, None)
        _atomic_write(destinations[self.claude_config], _json_bytes(claude))

        _remove_hooks(codex_hooks)
        _atomic_write(destinations[self.codex_hooks], _json_bytes(codex_hooks))
        destinations[self.launch_agent].unlink(missing_ok=True)
        self.legacy_peers.unlink(missing_ok=True)
        destinations[self.install_state].unlink(missing_ok=True)
        with suppress(OSError):
            self.install_state.parent.rmdir()
        if self.cache.exists():
            shutil.rmtree(self.cache)
        self._remove_installed_runtime(runtime_removal)


def discover_executable(invoked_as: Path | None = None) -> Path:
    candidate = invoked_as if invoked_as is not None else None
    if candidate is not None and (candidate.is_absolute() or candidate.parent != Path(".")):
        try:
            expanded = candidate.expanduser().absolute()
            expanded.resolve(strict=True)
            return expanded
        except OSError as error:
            raise SettingsError("cross-agent-chat executable is unavailable") from error
    discovered = shutil.which(SERVER_NAME)
    if discovered is None:
        raise SettingsError("cross-agent-chat executable is not on PATH")
    path = Path(discovered).absolute()
    try:
        path.resolve(strict=True)
    except OSError as error:
        raise SettingsError("cross-agent-chat executable is unavailable") from error
    return path
