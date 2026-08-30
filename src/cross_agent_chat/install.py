"""Ownership-aware provider setup, background service, and uninstall."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

from cross_agent_chat.core import ChatError, atomic_json, ensure_private_dir, valid_device
from cross_agent_chat.tailnet import LOCAL_BROKER_HOST, LOCAL_BROKER_PORT, valid_tailnet_address

SERVER_NAME: Final = "cross-agent-chat"
LAUNCH_AGENT_LABEL: Final = "io.github.kwonsyup.cross-agent-chat"
OWNED_TOML_START: Final = "# cross-agent-chat:start"
OWNED_TOML_END: Final = "# cross-agent-chat:end"
OWNED_TOML_RE: Final = re.compile(
    rf"\n?{re.escape(OWNED_TOML_START)}.*?{re.escape(OWNED_TOML_END)}\n?", re.DOTALL
)


class SettingsError(RuntimeError):
    """A safe setup or configuration failure."""


@dataclass(frozen=True, slots=True)
class InstallReport:
    changed_paths: tuple[Path, ...]
    backup: Path


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
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


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
    hook_map[event] = [item for item in existing if not _owned_hook(item)] + [owned]


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
    lines = text.splitlines(keepends=True)

    def heading(line: str) -> str | None:
        stripped = line.strip()
        if not stripped.startswith("[") or not stripped.endswith("]") or stripped.startswith("[["):
            return None
        return stripped[1:-1]

    index = next((offset for offset, line in enumerate(lines) if heading(line) == "features"), None)
    if index is None:
        suffix = "" if not text or text.endswith("\n") else "\n"
        return text + suffix + "\n[features]\nhooks = true\n"
    end = next(
        (offset for offset in range(index + 1, len(lines)) if heading(lines[offset]) is not None),
        len(lines),
    )
    section = [line for line in lines[index + 1 : end] if re.match(r"^\s*hooks\s*=", line) is None]
    return "".join([*lines[: index + 1], "hooks = true\n", *section, *lines[end:]])


def _codex_owned_toml(
    executable: Path,
    device: str,
    hooks_path: Path,
    hook_indices: dict[str, int],
) -> str:
    command = json.dumps(str(executable))
    args = json.dumps(["_mcp", "--provider", "codex", "--device", device])
    text = (
        f'{OWNED_TOML_START}\n[mcp_servers."{SERVER_NAME}"]\ncommand = {command}\n'
        f"args = {args}\ntool_timeout_sec = 120\n"
    )
    for event, timeout in (("SessionStart", 5), ("SessionEnd", 3), ("Stop", 3)):
        hook_command = _hook_command(executable, "codex", device, event)
        key = f"{hooks_path}:{_hook_event_name(event)}:{hook_indices[event]}:0"
        text += (
            f"\n[hooks.state.{json.dumps(key)}]\n"
            f"trusted_hash = {json.dumps(_hook_trust_hash(hook_command, event, timeout))}\n"
        )
    return text + f"{OWNED_TOML_END}\n"


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

    def _install_metadata(self, settings: dict[str, object]) -> dict[str, object]:
        if self.install_state.exists():
            metadata = _json_object(self.install_state)
            previous = metadata.get("claude_cross_session_inbound")
            if (
                set(metadata) != {"schema_version", "claude_cross_session_inbound"}
                or metadata.get("schema_version") != 1
                or not isinstance(previous, dict)
                or set(previous) != {"present", "value"}
                or not isinstance(previous.get("present"), bool)
                or (previous.get("present") is False and previous.get("value") is not None)
            ):
                raise SettingsError("Cross Agent Chat install state is invalid")
            return metadata
        present = "crossSessionInbound" in settings
        return {
            "schema_version": 1,
            "claude_cross_session_inbound": {
                "present": present,
                "value": settings.get("crossSessionInbound") if present else None,
            },
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

    def _payloads(self) -> dict[Path, bytes]:
        claude_settings = _json_object(self.claude_settings)
        install_metadata = self._install_metadata(claude_settings)
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

        codex_text = self.codex_config.read_text() if self.codex_config.exists() else ""
        codex_text = OWNED_TOML_RE.sub("\n", codex_text).rstrip()
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

    def _backup(self, originals: dict[Path, bytes | None]) -> Path:
        root = self.home / ".cache" / SERVER_NAME / "backups"
        ensure_private_dir(root)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = root / f"{stamp}-{uuid4().hex[:8]}"
        ensure_private_dir(destination)
        manifest = {
            str(path.relative_to(self.home)): None
            if payload is None
            else base64.b64encode(payload).decode("ascii")
            for path, payload in originals.items()
        }
        atomic_json(destination / "manifest.json", manifest)
        return destination

    def setup(self, verify: Callable[[], bool] | None = None) -> InstallReport:
        payloads = self._payloads()
        originals = {path: path.read_bytes() if path.exists() else None for path in payloads}
        if self.legacy_peers.exists():
            originals[self.legacy_peers] = self.legacy_peers.read_bytes()
        backup = self._backup(originals)
        written: list[Path] = []
        try:
            for path, payload in payloads.items():
                _atomic_write(path, payload)
                written.append(path)
            if self.legacy_peers.exists():
                self.legacy_peers.unlink()
                written.append(self.legacy_peers)
            healthy = self.verify_configuration() if verify is None else verify()
            if not healthy:
                raise SettingsError("verification failed after setup")
        except (OSError, SettingsError):
            for path in reversed(written):
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, original)
            raise
        return InstallReport(tuple(payloads), backup)

    def _restore(self, backup: Path) -> None:
        manifest = _json_object(backup / "manifest.json")
        for relative, encoded in manifest.items():
            if not isinstance(relative, str):
                raise SettingsError("backup manifest is invalid")
            destination = (self.home / relative).resolve()
            if not destination.is_relative_to(self.home):
                raise SettingsError("backup manifest is invalid")
            if encoded is None:
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
        previous_broker_loaded = self.broker_is_loaded()
        previous_broker_healthy = previous_broker_loaded and self.broker_is_healthy()
        report = self.setup()
        service_transition_started = False
        try:
            self._stop_couriers()
            self._remove_runtime_state()
            service_transition_started = True
            self.activate()
            for attempt in range(60):
                if self.verify():
                    break
                if attempt < 59:
                    time.sleep(0.25)
            else:
                raise SettingsError("background broker did not become healthy")
        except (OSError, subprocess.SubprocessError, ChatError, SettingsError) as failure:
            try:
                if service_transition_started:
                    self._bootout()
                self._restore(report.backup)
                if service_transition_started and previous_broker_loaded:
                    self.activate()
                if previous_broker_healthy and not self._wait_for_broker_health():
                    raise SettingsError("previous broker did not become healthy")
            except (OSError, subprocess.SubprocessError, ChatError, SettingsError) as rollback:
                raise SettingsError(
                    f"installation failed: {failure}; rollback failed: {rollback}"
                ) from failure
            raise
        return report

    def verify_configuration(self) -> bool:
        try:
            claude = _json_object(self.claude_config)
            servers = claude.get("mcpServers")
            settings = _json_object(self.claude_settings)
            codex_hooks = _json_object(self.codex_hooks)
            codex_text = self.codex_config.read_text()
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
        except (OSError, plistlib.InvalidFileException, SettingsError, RuntimeError):
            return False

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

    def broker_is_healthy(self) -> bool:
        try:
            loaded = self.broker_is_loaded()
        except SettingsError:
            return False
        if not loaded:
            return False
        try:
            from cross_agent_chat.runtime import request_tailnet

            response = request_tailnet(
                LOCAL_BROKER_HOST,
                {"schema_version": 1, "operation": "health"},
                port=LOCAL_BROKER_PORT,
                timeout=1.0,
            )
        except RuntimeError:
            return False
        return response == {"schema_version": 1, "status": "READY"}

    def _wait_for_broker_health(self) -> bool:
        for attempt in range(60):
            if self.broker_is_healthy():
                return True
            if attempt < 59:
                time.sleep(0.25)
        return False

    def verify(self) -> bool:
        return self.verify_configuration() and self.broker_is_healthy()

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
            stopped = subprocess.run(
                ["launchctl", "bootout", service],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            if stopped.returncode != 0:
                raise SettingsError("could not reload the background broker")
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
                    break
                if attempt < 19:
                    time.sleep(0.1)
            else:
                raise SettingsError("could not stop the background broker")
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
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{LAUNCH_AGENT_LABEL}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10.0,
            check=False,
        )

    def _stop_couriers(self) -> None:
        if not self.state.exists():
            return
        from cross_agent_chat.runtime import shutdown_couriers

        shutdown_couriers(self.state)

    def _remove_runtime_state(self) -> None:
        if self.state.exists():
            shutil.rmtree(self.state)

    def uninstall(self) -> None:
        metadata = self._install_metadata(_json_object(self.claude_settings))
        self._bootout()
        self._stop_couriers()
        self._remove_runtime_state()
        claude_settings = _json_object(self.claude_settings)
        _remove_hooks(claude_settings)
        previous = cast(dict[str, object], metadata["claude_cross_session_inbound"])
        if claude_settings.get("crossSessionInbound") == "accept":
            if cast(bool, previous["present"]):
                claude_settings["crossSessionInbound"] = previous["value"]
            else:
                claude_settings.pop("crossSessionInbound", None)
        _atomic_write(self.claude_settings, _json_bytes(claude_settings))

        claude = _json_object(self.claude_config)
        servers = claude.get("mcpServers")
        if isinstance(servers, dict):
            cast(dict[str, object], servers).pop(SERVER_NAME, None)
        _atomic_write(self.claude_config, _json_bytes(claude))

        if self.codex_config.exists():
            cleaned = OWNED_TOML_RE.sub("\n", self.codex_config.read_text()).lstrip("\n")
            _atomic_write(self.codex_config, cleaned.encode())

        codex_hooks = _json_object(self.codex_hooks)
        _remove_hooks(codex_hooks)
        _atomic_write(self.codex_hooks, _json_bytes(codex_hooks))
        self.launch_agent.unlink(missing_ok=True)
        self.legacy_peers.unlink(missing_ok=True)
        self.install_state.unlink(missing_ok=True)
        with suppress(OSError):
            self.install_state.parent.rmdir()
        if self.cache.exists():
            shutil.rmtree(self.cache)


def discover_executable(invoked_as: Path | None = None) -> Path:
    candidate = invoked_as if invoked_as is not None else None
    if candidate is not None and (candidate.is_absolute() or candidate.parent != Path(".")):
        try:
            return candidate.expanduser().resolve(strict=True)
        except OSError as error:
            raise SettingsError("cross-agent-chat executable is unavailable") from error
    discovered = shutil.which(SERVER_NAME)
    if discovered is None:
        raise SettingsError("cross-agent-chat executable is not on PATH")
    return Path(discovered).resolve(strict=True)
