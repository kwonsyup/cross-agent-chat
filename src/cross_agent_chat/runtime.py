"""Provider process registration, Codex courier, discovery, and delivery."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    as_completed,
)
from concurrent.futures import (
    TimeoutError as FuturesTimeoutError,
)
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import BinaryIO, Final, Literal, cast
from uuid import uuid4

from cross_agent_chat.claude_runtime import (
    AGENTS_TIMEOUT_SECONDS,
    COURIER_ENV_KEYS,
    DISCOVERY_TIMEOUT_SECONDS,
    SEND_TIMEOUT_SECONDS,
    claude_alias,
    claude_binary,
    courier_environment,
    discover_target_ref,
    exact_agent,
    sendmessage,
)
from cross_agent_chat.codex import CodexCourier, deliver_at_stop
from cross_agent_chat.core import (
    SCHEMA_VERSION,
    ChatError,
    IntentStore,
    Provider,
    Registry,
    Route,
    UnknownDeliveryError,
    authenticate_sender,
    bounded_message,
    canonical_cwd,
    ensure_private_dir,
    session_key,
    state_lock,
    valid_device,
    valid_name,
    valid_uuid,
)
from cross_agent_chat.remote import parse_remote_envelope
from cross_agent_chat.tailnet import TAILNET_PORT, tailnet_nodes, valid_tailnet_address
from cross_agent_chat.transport import remote_envelope

MAX_FRAME_BYTES: Final = 64 * 1024
SOCKET_TIMEOUT_SECONDS: Final = 5.0
MCP_TOOL_TIMEOUT_SECONDS: Final = 270.0
OPERATION_TIMEOUT_SECONDS: Final = 260.0
HEALTH_TIMEOUT_SECONDS: Final = AGENTS_TIMEOUT_SECONDS + 2.0
REMOTE_DISCOVERY_TIMEOUT_SECONDS: Final = HEALTH_TIMEOUT_SECONDS + 5.0
ACCEPT_TIMEOUT_SECONDS: Final = (
    2 * AGENTS_TIMEOUT_SECONDS + DISCOVERY_TIMEOUT_SECONDS + SEND_TIMEOUT_SECONDS + 5.0
)
AUTHORIZE_TIMEOUT_SECONDS: Final = 20.0
COURIER_READY_SECONDS: Final = 3.0
MAX_COURIER_DIAGNOSTIC_BYTES: Final = 1024
BOOTSTRAP_FRAME_TIMEOUT_SECONDS: Final = 0.1
REMOTE_TIMEOUT_SECONDS: Final = (
    HEALTH_TIMEOUT_SECONDS + AUTHORIZE_TIMEOUT_SECONDS + ACCEPT_TIMEOUT_SECONDS + 5.0
)
LOCAL_DISCOVERY_WORKERS: Final = 32
LOCAL_DISCOVERY_TIMEOUT_SECONDS: Final = HEALTH_TIMEOUT_SECONDS
PRESENCE_ENV_VAR: Final = "CROSS_AGENT_CHAT_PRESENCE"
PROC_PIDTBSDINFO: Final = 3
PROC_BSDINFO_SIZE: Final = 136
DeliveryMode = Literal[
    "claude_native_cross_session",
    "codex_stop_bound",
    "codex_experimental_queue",
]


class RegistrationInterrupted(SystemExit):
    """Interrupt one in-flight courier registration without retrying its health probe."""

    def __init__(self) -> None:
        super().__init__(143)


def recipient_profile_root(provider: str) -> str:
    value = os.environ.get("CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR")
    root = Path(value) if value else Path.home() / (".codex" if provider == "codex" else ".claude")
    return str(root.expanduser().resolve(strict=False))


def recipient_owner_identity(
    provider: str, pid: int, profile_root: str | None = None
) -> tuple[str, Path]:
    """Bind a route to the provider executable, process birth, uid, and selected root."""
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    path_buffer = ctypes.create_string_buffer(4096)
    if libproc.proc_pidpath(pid, path_buffer, len(path_buffer)) <= 0:
        raise ChatError("provider process identity is unavailable")
    info = ctypes.create_string_buffer(PROC_BSDINFO_SIZE)
    if libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, info, len(info)) != PROC_BSDINFO_SIZE:
        raise ChatError("provider process identity is unavailable")
    uid = int.from_bytes(info.raw[20:24], sys.byteorder)
    if uid != os.getuid():
        raise ChatError("provider process identity is unavailable")
    start_seconds = int.from_bytes(info.raw[120:128], sys.byteorder)
    start_microseconds = int.from_bytes(info.raw[128:136], sys.byteorder)
    binary = Path(path_buffer.value.decode()).resolve(strict=True)
    root = Path(profile_root or recipient_profile_root(provider)).expanduser().resolve(strict=False)
    payload = f"{provider}\0{binary}\0{uid}\0{start_seconds}\0{start_microseconds}\0{root}".encode()
    return hashlib.sha256(payload).hexdigest(), binary


@dataclass(frozen=True, slots=True)
class Target:
    alias: str
    provider: Provider
    device: str
    project: str
    generation: str
    session_key: str
    remote: bool
    session_id: str | None = None
    cwd: str | None = None
    pid: int | None = None
    tailnet_address: str | None = None
    delivery_mode: DeliveryMode | None = None

    def public(self, *, include_delivery_mode: bool = False) -> dict[str, str]:
        result = {
            "alias": self.alias,
            "provider": self.provider,
            "device": self.device,
            "project": self.project,
            "status": "available",
        }
        if include_delivery_mode:
            result["delivery_mode"] = (
                "unknown" if self.delivery_mode is None else self.delivery_mode
            )
        return result


def state_root(value: str | None = None) -> Path:
    root = (
        Path(value).expanduser()
        if value is not None
        else Path.home() / ".local/state/cross-agent-chat"
    )
    if not root.is_absolute():
        raise ChatError("state root must be absolute")
    ensure_private_dir(root)
    return root


def presence_is_enabled() -> bool:
    value = os.environ.get(PRESENCE_ENV_VAR)
    if value in {None, ""}:
        return True
    if value == "off":
        return False
    raise ChatError(f"{PRESENCE_ENV_VAR} must be empty or 'off'")


def socket_path(root: Path, route: Route) -> Path:
    directory = Path("/tmp") / f"cross-agent-chat-{os.getuid()}"
    ensure_private_dir(directory)
    identity = f"{root.resolve()}:{route.provider}:{route.session_id}:{route.generation}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return directory / f"{digest[:32]}.sock"


def require_socket(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ChatError("session courier is unavailable") from error
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ChatError("session courier socket is unsafe")


def read_frame(connection: socket.socket, limit: int = MAX_FRAME_BYTES) -> bytes:
    payload = b""
    while b"\n" not in payload:
        chunk = connection.recv(min(65536, limit + 1 - len(payload)))
        if not chunk:
            break
        payload += chunk
        if len(payload) > limit:
            raise ChatError("courier frame exceeds the bounded limit")
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise ChatError("courier frame is malformed")
    return payload[:-1]


def emit_frame(connection: socket.socket, payload: dict[str, object]) -> None:
    frame = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    connection.sendall(frame.encode())


def emit_frame_safely(connection: socket.socket, payload: dict[str, object]) -> None:
    try:
        emit_frame(connection, payload)
    except OSError:
        return


def request_socket(
    path: Path, payload: dict[str, object], *, timeout: float = SOCKET_TIMEOUT_SECONDS
) -> dict[str, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    attempted_write = False
    try:
        require_socket(path)
        client.connect(str(path))
        attempted_write = True
        emit_frame(client, payload)
        raw = json.loads(read_frame(client))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ChatError) as error:
        if not attempted_write:
            raise ChatError("session courier is unavailable before delivery") from error
        raise UnknownDeliveryError("delivery state is unknown") from error
    finally:
        client.close()
    if not isinstance(raw, dict):
        raise UnknownDeliveryError("delivery state is unknown")
    return cast(dict[str, object], raw)


def request_tailnet(
    address: str,
    payload: dict[str, object],
    *,
    port: int = TAILNET_PORT,
    timeout: float = 2.0,
) -> dict[str, object]:
    """Exchange one bounded frame with a Tailnet broker."""
    attempted_write = False
    try:
        client = socket.create_connection((address, port), timeout=timeout)
        client.settimeout(timeout)
        with client:
            attempted_write = True
            emit_frame(client, payload)
            raw: object = json.loads(read_frame(client))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ChatError) as error:
        if not attempted_write:
            raise ChatError("Tailnet peer is unavailable before delivery") from error
        raise UnknownDeliveryError("Tailnet delivery state is unknown") from error
    if not isinstance(raw, dict):
        raise UnknownDeliveryError("Tailnet delivery state is unknown")
    return cast(dict[str, object], raw)


def hook_input(expected_event: str) -> dict[str, object]:
    text = sys.stdin.read(MAX_FRAME_BYTES + 1)
    if len(text.encode()) > MAX_FRAME_BYTES:
        raise ChatError("hook input exceeds the bounded limit")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ChatError("hook input is not JSON") from error
    if not isinstance(raw, dict) or raw.get("hook_event_name") != expected_event:
        raise ChatError("hook event is invalid")
    session_id = raw.get("session_id", raw.get("sessionId"))
    cwd = raw.get("cwd")
    if not isinstance(session_id, str) or not isinstance(cwd, str):
        raise ChatError("hook lacks session identity or cwd")
    raw["session_id"] = valid_uuid(session_id, "session id")
    raw["cwd"] = canonical_cwd(cwd)
    return cast(dict[str, object], raw)


def executable() -> Path:
    candidate = Path(sys.argv[0]).expanduser()
    try:
        return candidate.resolve(strict=True)
    except OSError as error:
        raise ChatError("runtime executable is unavailable") from error


def _bootstrap_response(response: dict[str, object], route: Route) -> bool:
    """Accept only the exact local listener bound to this route generation."""
    return response == {
        "schema_version": SCHEMA_VERSION,
        "status": "BOOTSTRAPPED",
        "generation": route.generation,
    }


def _owned_socket_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
        return None
    return metadata.st_dev, metadata.st_ino


def _drain_courier_stderr(stream: BinaryIO, captured: bytearray) -> None:
    try:
        descriptor = stream.fileno()
        os.set_blocking(descriptor, False)
    except OSError:
        return
    drained = 0
    while drained < MAX_COURIER_DIAGNOSTIC_BYTES:
        try:
            chunk = os.read(descriptor, min(4096, MAX_COURIER_DIAGNOSTIC_BYTES - drained))
        except BlockingIOError:
            return
        except OSError:
            return
        if not chunk:
            return
        drained += len(chunk)
        remaining = MAX_COURIER_DIAGNOSTIC_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])


def _courier_failure_phase(captured: bytearray) -> str:
    detail = bytes(captured).decode("utf-8", errors="replace").lower()
    if "courier route is not current" in detail:
        return "route"
    if "address already in use" in detail:
        return "socket-bind"
    if "native queue is unavailable" in detail:
        return "context"
    if "modulenotfounderror" in detail or "importerror" in detail:
        return "runtime"
    return "child"


def _reap_owned_courier(
    process: subprocess.Popen[bytes],
    root: Path,
    route: Route,
    path: Path,
    stderr: BinaryIO,
    captured: bytearray,
) -> None:
    """Stop and reap only the child and socket created for one failed registration."""
    socket_identity = _owned_socket_identity(path)
    _drain_courier_stderr(stderr, captured)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
    _drain_courier_stderr(stderr, captured)
    current_socket = _owned_socket_identity(path)
    if socket_identity is not None and current_socket == socket_identity:
        with suppress(FileNotFoundError):
            path.unlink()
    elif socket_identity is None and current_socket is not None and Registry(root).current(route):
        # The per-session registration lock still owns this exact route generation,
        # so a child that bound while termination began cannot leave a stale socket.
        with suppress(FileNotFoundError):
            path.unlink()
    with suppress(OSError):
        stderr.close()


def _registration_sigterm(_signum: int, _frame: FrameType | None) -> None:
    raise RegistrationInterrupted()


@contextmanager
def _registration_sigterm_scope() -> Iterator[None]:
    """Convert SIGTERM to exact registration cleanup for one main-thread transaction."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGTERM, _registration_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _spawn_courier(root: Path, route: Route) -> None:
    path = socket_path(root, route)
    owner_binary = recipient_owner_identity(route.provider, route.pid, route.profile_root)[1]
    if path.exists() or path.is_symlink():
        raise ChatError("session courier socket already exists")
    command = [
        str(executable()),
        "_courier",
        "--provider",
        route.provider,
        "--state-root",
        str(root),
        "--session-id",
        route.session_id,
        "--cwd",
        route.cwd,
        "--generation",
        route.generation,
        "--pid",
        str(route.pid),
    ]
    if route.provider == "claude":
        environment = courier_environment()
        environment["CROSS_AGENT_CHAT_CLAUDE_BINARY"] = str(owner_binary or claude_binary())
    else:
        environment = {
            key: os.environ[key]
            for key in (*COURIER_ENV_KEYS, "CODEX_HOME", "CROSS_AGENT_CHAT_CODEX_NATIVE_QUEUE")
            if key in os.environ
        }
        environment["CODEX_HOME"] = route.profile_root or recipient_profile_root("codex")
        codex = str(owner_binary) if owner_binary is not None else shutil.which("codex")
        if codex is not None:
            with suppress(OSError):
                environment["CROSS_AGENT_CHAT_CODEX_BINARY"] = str(Path(codex).resolve(strict=True))
    try:
        read_descriptor, write_descriptor = os.pipe()
    except OSError as error:
        raise ChatError("session courier diagnostics could not start") from error
    try:
        os.set_blocking(write_descriptor, False)
    except OSError as error:
        os.close(read_descriptor)
        os.close(write_descriptor)
        raise ChatError("session courier diagnostics could not start") from error
    stderr = os.fdopen(read_descriptor, "rb", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=write_descriptor,
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        stderr.close()
        raise ChatError("session courier could not start") from error
    finally:
        with suppress(OSError):
            os.close(write_descriptor)
    captured = bytearray()
    try:
        deadline = time.monotonic() + COURIER_READY_SECONDS
        while time.monotonic() < deadline:
            _drain_courier_stderr(stderr, captured)
            if process.poll() is not None:
                raise ChatError(
                    "session courier exited before local bootstrap"
                    f" ({_courier_failure_phase(captured)})"
                )
            try:
                remaining = max(0.1, deadline - time.monotonic())
                response = request_socket(
                    path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "operation": "bootstrap",
                        "generation": route.generation,
                    },
                    timeout=remaining,
                )
                if _bootstrap_response(response, route):
                    return
                time.sleep(0.02)
            except ChatError:
                time.sleep(0.02)
        raise ChatError("session courier did not complete local bootstrap")
    except BaseException:
        _reap_owned_courier(process, root, route, path, stderr, captured)
        raise
    finally:
        with suppress(OSError):
            stderr.close()


def register(provider: str, device: str, pid: int, state_root_value: str | None) -> Route | None:
    if not presence_is_enabled():
        return None
    if provider not in {"claude", "codex"}:
        raise ChatError("provider is invalid")
    if isinstance(pid, bool) or pid <= 0:
        raise ChatError("provider process is invalid")
    raw = hook_input("SessionStart")
    owner_identity, _owner_binary = recipient_owner_identity(provider, pid)
    route = Route.create(
        provider=provider,
        session_id=cast(str, raw["session_id"]),
        device=valid_device(device),
        cwd=cast(str, raw["cwd"]),
        pid=pid,
        owner_identity=owner_identity,
        profile_root=recipient_profile_root(provider),
    )
    root = state_root(state_root_value)
    try:
        with (
            _registration_sigterm_scope(),
            state_lock(root, "register-" + session_key(route.provider, route.session_id)),
        ):
            registry = Registry(root)
            registry.compact_dead()
            registered = registry.upsert_or_reuse_live_owner(route)
            if registered != route:
                try:
                    bootstrap = request_socket(
                        socket_path(root, registered),
                        {
                            "schema_version": SCHEMA_VERSION,
                            "operation": "bootstrap",
                            "generation": registered.generation,
                        },
                        timeout=0.5,
                    )
                except ChatError as error:
                    cause = error.__cause__
                    missing = not socket_path(root, registered).exists()
                    refused = isinstance(cause, OSError) and cause.errno == errno.ECONNREFUSED
                    if not missing and not refused:
                        # A busy courier may still own an accepted provider effect. A
                        # health timeout is not evidence authorizing queue replacement.
                        return registered
                    registry.upsert(route)
                else:
                    if _bootstrap_response(bootstrap, registered):
                        return registered
                    raise ChatError("existing courier ownership could not be verified")
            _spawn_courier(root, route)
            return route
    except BaseException:
        Registry(root).remove(
            route.provider, route.session_id, route.pid, generation=route.generation
        )
        raise


def unregister(provider: str, pid: int, state_root_value: str | None) -> None:
    if not presence_is_enabled():
        return
    if provider not in {"claude", "codex"}:
        raise ChatError("provider is invalid")
    raw = hook_input("SessionEnd")
    root = state_root(state_root_value)
    session_id = cast(str, raw["session_id"])
    routes = [
        route
        for route in Registry(root).routes()
        if route.provider == provider and route.session_id == session_id and route.pid == pid
    ]
    if len(routes) != 1:
        raise ChatError("exact session route is unavailable")
    route = routes[0]
    Registry(root).remove(route.provider, route.session_id, route.pid, generation=route.generation)
    try:
        request_socket(
            socket_path(root, route),
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "shutdown",
                "generation": route.generation,
            },
        )
    except ChatError:
        return


def _route_current(root: Path, expected: Route) -> bool:
    if expected.owner_identity is not None:
        profile = expected.profile_root or str(
            Path.home() / (".codex" if expected.provider == "codex" else ".claude")
        )
        try:
            if (
                recipient_owner_identity(expected.provider, expected.pid, profile)[0]
                != expected.owner_identity
            ):
                return False
        except (ChatError, OSError):
            return False
    return (
        Registry(root).current(expected)
        and expected.process_is_live()
        and expected.cwd_is_available()
    )


def courier_accept(
    route: Route,
    courier: CodexCourier | None,
    event_id: str,
    message: str,
) -> dict[str, object]:
    """Attempt one provider delivery and report its exact effect boundary."""
    try:
        identifier = valid_uuid(event_id, "event id")
        body = bounded_message(message)
        if route.provider == "codex":
            if courier is None:
                raise ChatError("Codex courier is unavailable")
            return courier.accept(identifier, body)
        agent = exact_agent(route.session_id, route.cwd)
        target_ref = discover_target_ref(agent["name"])
        if exact_agent(route.session_id, route.cwd) != agent:
            raise ChatError("Claude target changed during discovery")
        target_alias = claude_alias(route.device, route.project, agent)
        sendmessage(target_ref, body, executable())
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": identifier,
            "status": "TRANSPORT_ACCEPTED",
            "to": target_alias,
            "provider": "claude",
        }
    except UnknownDeliveryError:
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "status": "UNKNOWN_DELIVERY",
            "provider": route.provider,
        }
    except ChatError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "status": "PRE_EFFECT_REJECTED",
            "provider": route.provider,
            "error": str(error),
        }


def pre_effect_error(response: dict[str, object], event_id: str, provider: Provider) -> str | None:
    if set(response) != {"schema_version", "event_id", "status", "provider", "error"}:
        return None
    error = response.get("error")
    if (
        response.get("schema_version") != SCHEMA_VERSION
        or response.get("event_id") != event_id
        or response.get("status") != "PRE_EFFECT_REJECTED"
        or response.get("provider") != provider
        or not isinstance(error, str)
        or not error
        or any(character in error for character in "\r\n\0")
    ):
        return None
    try:
        if len(error.encode()) > 256:
            return None
    except UnicodeEncodeError:
        return None
    return error


def _delivery_mode(route: Route, courier: CodexCourier | None) -> DeliveryMode:
    if route.provider == "claude":
        return "claude_native_cross_session"
    return (
        "codex_experimental_queue"
        if courier is not None and courier.native_queue
        else "codex_stop_bound"
    )


def courier_health(
    route: Route, courier: CodexCourier | None = None, *, include_delivery_mode: bool = False
) -> dict[str, object]:
    alias = route.alias
    if route.provider == "claude":
        try:
            alias = claude_alias(
                route.device,
                route.project,
                exact_agent(route.session_id, route.cwd),
            )
        except ChatError:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "UNAVAILABLE",
                "generation": route.generation,
            }
    response: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "READY",
        "generation": route.generation,
        "alias": alias,
    }
    if include_delivery_mode:
        response["delivery_mode"] = _delivery_mode(route, courier)
    return response


def courier_server(
    *,
    provider: str,
    state_root_value: str,
    session_id: str,
    cwd: str,
    generation: str,
    pid: int,
) -> None:
    root = state_root(state_root_value)
    candidates = [
        route
        for route in Registry(root).routes()
        if route.provider == provider
        and route.session_id == session_id
        and route.cwd == canonical_cwd(cwd)
        and route.generation == valid_uuid(generation, "route generation")
        and route.pid == pid
    ]
    if len(candidates) != 1:
        raise ChatError("courier route is not current")
    route = candidates[0]
    path = socket_path(root, route)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o077)
    try:
        server.bind(str(path))
    finally:
        os.umask(old_umask)
    native_queue: tuple[Path, dict[str, str], str] | None = None
    native_enabled = os.environ.get("CROSS_AGENT_CHAT_CODEX_NATIVE_QUEUE") == "experimental"
    if provider == "codex" and native_enabled:
        bound_binary = os.environ.get("CROSS_AGENT_CHAT_CODEX_BINARY")
        if bound_binary is not None:
            candidate = Path(bound_binary)
            try:
                binary = candidate.resolve(strict=True)
            except OSError as error:
                raise ChatError("Codex native queue is unavailable") from error
            native_queue = (
                binary,
                {
                    key: os.environ[key]
                    for key in (*COURIER_ENV_KEYS, "CODEX_HOME")
                    if key in os.environ
                },
                route.session_id,
            )
    courier = (
        CodexCourier(alias=route.alias, generation=route.generation, native_queue=native_queue)
        if provider == "codex"
        else None
    )
    bound = path.lstat()
    path.chmod(0o600)
    server.listen(4)
    server.settimeout(1.0)
    stopping = False
    bootstrapped = False
    try:
        while not stopping and _route_current(root, route):
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(
                    SOCKET_TIMEOUT_SECONDS if bootstrapped else BOOTSTRAP_FRAME_TIMEOUT_SECONDS
                )
                try:
                    raw = json.loads(read_frame(connection))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ChatError):
                    continue
                if not isinstance(raw, dict):
                    continue
                request = cast(dict[str, object], raw)
                if (
                    request.get("schema_version") != SCHEMA_VERSION
                    or request.get("generation") != route.generation
                ):
                    continue
                operation = request.get("operation")
                if operation == "bootstrap":
                    try:
                        emit_frame(
                            connection,
                            {
                                "schema_version": SCHEMA_VERSION,
                                "status": "BOOTSTRAPPED",
                                "generation": route.generation,
                            },
                        )
                    except OSError:
                        continue
                    bootstrapped = True
                elif operation == "health":
                    if not bootstrapped:
                        emit_frame_safely(
                            connection,
                            {
                                "schema_version": SCHEMA_VERSION,
                                "status": "UNAVAILABLE",
                                "generation": route.generation,
                            },
                        )
                    else:
                        emit_frame_safely(
                            connection,
                            courier_health(
                                route,
                                courier,
                                include_delivery_mode=request.get("include_delivery_mode") is True,
                            ),
                        )
                elif operation == "shutdown":
                    emit_frame_safely(connection, {"schema_version": 1, "status": "STOPPED"})
                    stopping = True
                elif operation == "accept":
                    event_id = request.get("event_id")
                    message = request.get("message")
                    if not isinstance(event_id, str) or not isinstance(message, str):
                        continue
                    if not bootstrapped:
                        try:
                            identifier = valid_uuid(event_id, "event id")
                        except ChatError:
                            continue
                        emit_frame_safely(
                            connection,
                            {
                                "schema_version": SCHEMA_VERSION,
                                "event_id": identifier,
                                "status": "PRE_EFFECT_REJECTED",
                                "provider": route.provider,
                                "error": "session courier is still bootstrapping",
                            },
                        )
                    else:
                        emit_frame_safely(
                            connection, courier_accept(route, courier, event_id, message)
                        )
                elif operation == "peek":
                    if courier is None:
                        continue
                    emit_frame_safely(
                        connection,
                        {
                            "schema_version": 1,
                            "status": "PEEKED",
                            "generation": route.generation,
                            "messages": courier.peek(),
                        },
                    )
                elif operation == "ack":
                    if courier is None:
                        continue
                    identifiers = request.get("event_ids")
                    if not isinstance(identifiers, list) or not all(
                        isinstance(item, str) for item in identifiers
                    ):
                        continue
                    try:
                        typed_ids = [cast(str, item) for item in identifiers]
                        courier.acknowledge(typed_ids)
                        emit_frame_safely(
                            connection,
                            {"schema_version": 1, "status": "ACKNOWLEDGED", "event_ids": typed_ids},
                        )
                    except ChatError:
                        continue
    finally:
        if courier is not None:
            courier.clear()
        server.close()
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass


def shutdown_couriers(root: Path) -> None:
    """Stop every exact live courier owned by one runtime state root."""
    for route in Registry(root).routes():
        if not route.process_is_live():
            continue
        path = socket_path(root, route)
        if not path.exists():
            continue
        response = request_socket(
            path,
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "shutdown",
                "generation": route.generation,
            },
            timeout=ACCEPT_TIMEOUT_SECONDS + 1.0,
        )
        if response != {"schema_version": SCHEMA_VERSION, "status": "STOPPED"}:
            raise ChatError("courier shutdown failed")


def _local_target(
    root: Path, route: Route, *, timeout: float = HEALTH_TIMEOUT_SECONDS
) -> Target | None:
    if not route.cwd_is_available():
        return None
    try:
        response = request_socket(
            socket_path(root, route),
            {
                "schema_version": 1,
                "operation": "health",
                "generation": route.generation,
                "include_delivery_mode": True,
            },
            timeout=timeout,
        )
    except ChatError:
        return None
    alias = response.get("alias")
    expected = {"schema_version", "status", "generation", "alias"}
    observed_mode = response.get("delivery_mode")
    if (
        set(response) not in (expected, expected | {"delivery_mode"})
        or response.get("schema_version") != SCHEMA_VERSION
        or response.get("status") != "READY"
        or response.get("generation") != route.generation
        or not isinstance(alias, str)
        or (
            observed_mode is not None
            and observed_mode
            not in {"claude_native_cross_session", "codex_stop_bound", "codex_experimental_queue"}
        )
    ):
        return None
    if route.provider == "codex":
        if alias != route.alias:
            return None
    else:
        prefix = f"claude@{route.device}:{route.project}:"
        shortened = prefix[:115] + "~" + session_key("claude", route.session_id)[:12]
        if not alias.startswith(prefix) and not (len(prefix) > 115 and alias == shortened):
            return None
    try:
        return Target(
            alias=valid_name(alias, "route alias"),
            provider=route.provider,
            device=route.device,
            project=route.project,
            generation=route.generation,
            session_key=session_key(route.provider, route.session_id),
            remote=False,
            session_id=route.session_id,
            cwd=route.cwd,
            pid=route.pid,
            delivery_mode=observed_mode if observed_mode is not None else None,
        )
    except ChatError:
        return None


def _local_target_before_deadline(root: Path, route: Route, deadline: float) -> Target | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return _local_target(root, route, timeout=min(HEALTH_TIMEOUT_SECONDS, remaining))


def local_targets(root: Path) -> list[Target]:
    routes = [
        route
        for route in Registry(root).routes()
        if route.process_is_live() and route.cwd_is_available()
    ]
    if not routes:
        return []
    deadline = time.monotonic() + LOCAL_DISCOVERY_TIMEOUT_SECONDS
    workers = ThreadPoolExecutor(max_workers=min(LOCAL_DISCOVERY_WORKERS, len(routes)))
    futures: list[Future[Target | None]] = []
    results: list[Target | None] = [None] * len(routes)
    future_indexes: dict[Future[Target | None], int] = {}
    try:
        for index, route in enumerate(routes):
            if time.monotonic() >= deadline:
                break
            future = workers.submit(_local_target_before_deadline, root, route, deadline)
            futures.append(future)
            future_indexes[future] = index
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                for future in as_completed(futures, timeout=remaining):
                    results[future_indexes[future]] = future.result()
            except FuturesTimeoutError:
                pass
    finally:
        for future in futures:
            future.cancel()
        workers.shutdown(wait=False, cancel_futures=True)
    return [target for target in results if target is not None]


def _targets_from_tailnet(
    address: str, raw: object, *, include_delivery_mode: bool = False
) -> list[Target]:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "peers"}:
        raise ChatError("Tailnet peer returned invalid discovery")
    response = cast(dict[object, object], raw)
    items = response.get("peers")
    if response.get("schema_version") != SCHEMA_VERSION or not isinstance(items, list):
        raise ChatError("Tailnet peer returned invalid discovery")
    targets: list[Target] = []
    for raw_item in cast(list[object], items):
        required = {
            "alias",
            "provider",
            "device",
            "project",
            "status",
            "generation",
            "session_key",
        }
        allowed = required | ({"delivery_mode"} if include_delivery_mode else set())
        if not isinstance(raw_item, dict) or set(raw_item) not in (required, allowed):
            raise ChatError("Tailnet peer returned invalid discovery")
        item = cast(dict[object, object], raw_item)
        provider = item.get("provider")
        values = (
            item.get("alias"),
            item.get("device"),
            item.get("project"),
            item.get("generation"),
            item.get("session_key"),
        )
        if (
            provider not in {"claude", "codex"}
            or not all(isinstance(value, str) for value in values)
            or item.get("status") != "available"
            or not re.fullmatch(r"[0-9a-f]{64}", cast(str, item.get("session_key")))
            or (
                "delivery_mode" in item
                and item["delivery_mode"]
                not in {
                    "claude_native_cross_session",
                    "codex_stop_bound",
                    "codex_experimental_queue",
                    "unknown",
                }
            )
        ):
            raise ChatError("Tailnet peer returned invalid discovery")
        observed_mode = item.get("delivery_mode")
        targets.append(
            Target(
                alias=valid_name(cast(str, item["alias"]), "remote alias"),
                provider=provider,
                device=valid_device(cast(str, item["device"])),
                project=valid_name(cast(str, item["project"]), "remote project"),
                generation=valid_uuid(cast(str, item["generation"]), "route generation"),
                session_key=cast(str, item["session_key"]),
                remote=True,
                tailnet_address=address,
                delivery_mode=(
                    cast(DeliveryMode, observed_mode)
                    if isinstance(observed_mode, str) and observed_mode != "unknown"
                    else None
                ),
            )
        )
    return targets


def _remote_node_targets(
    address: str, deadline: float | None = None, *, include_delivery_mode: bool = False
) -> tuple[list[Target], bool]:
    remaining = (
        REMOTE_DISCOVERY_TIMEOUT_SECONDS if deadline is None else deadline - time.monotonic()
    )
    if remaining <= 0:
        return [], False
    request: dict[str, object] = {"schema_version": SCHEMA_VERSION, "operation": "peers"}
    if include_delivery_mode:
        request["include_delivery_mode"] = True
    try:
        raw = request_tailnet(
            address,
            request,
            # The remote broker may spend HEALTH_TIMEOUT_SECONDS validating local routes.
            timeout=min(REMOTE_DISCOVERY_TIMEOUT_SECONDS, remaining),
        )
        return (
            _targets_from_tailnet(address, raw, include_delivery_mode=include_delivery_mode),
            True,
        )
    except (ChatError, UnknownDeliveryError):
        if not include_delivery_mode:
            return [], False
        remaining = deadline - time.monotonic() if deadline is not None else 0.0
        if remaining <= 0:
            return [], False
        try:
            raw = request_tailnet(
                address,
                {"schema_version": SCHEMA_VERSION, "operation": "peers"},
                timeout=min(REMOTE_DISCOVERY_TIMEOUT_SECONDS, remaining),
            )
            return _targets_from_tailnet(address, raw), True
        except (ChatError, UnknownDeliveryError):
            return [], False


def _remote_discovery(*, include_delivery_mode: bool = False) -> tuple[list[Target], bool]:
    addresses = tailnet_nodes()
    if not addresses:
        return [], False
    targets: list[Target] = []
    complete = True
    deadline = time.monotonic() + REMOTE_DISCOVERY_TIMEOUT_SECONDS
    workers = ThreadPoolExecutor(max_workers=min(16, len(addresses)))
    futures = [
        (
            workers.submit(
                _remote_node_targets,
                address,
                deadline,
                include_delivery_mode=True,
            )
            if include_delivery_mode
            else workers.submit(_remote_node_targets, address, deadline)
        )
        for address in addresses
    ]
    try:
        for future in as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
            outcome = future.result()
            discovered, node_complete = outcome
            targets.extend(discovered)
            complete = complete and node_complete
    except FuturesTimeoutError:
        complete = False
    finally:
        for future in futures:
            future.cancel()
        workers.shutdown(wait=False, cancel_futures=True)
    return targets, complete


def remote_targets(_root: Path, *, include_delivery_mode: bool = False) -> list[Target]:
    return _remote_discovery(include_delivery_mode=include_delivery_mode)[0]


def all_targets(
    root: Path, *, include_remote: bool = True, include_delivery_mode: bool = False
) -> list[Target]:
    if include_remote:
        with ThreadPoolExecutor(max_workers=2) as workers:
            local = workers.submit(local_targets, root)
            remote = workers.submit(
                remote_targets, root, include_delivery_mode=include_delivery_mode
            )
            targets = [*local.result(), *remote.result()]
    else:
        targets = local_targets(root)
    aliases = [target.alias.casefold() for target in targets]
    if len(set(aliases)) != len(aliases):
        raise ChatError("peer discovery returned duplicate aliases")
    return sorted(targets, key=lambda target: target.alias.casefold())


def _target_matches(target: Target, query: str) -> bool:
    if target.alias.casefold() == query.casefold():
        return True
    wanted = re.findall(r"[^\W_]+", query.casefold())
    available = set(re.findall(r"[^\W_]+", target.alias.casefold()))
    return bool(wanted) and all(token in available for token in wanted)


def resolve_target(targets: list[Target], query: str) -> Target:
    if not query.strip() or len(query) > 160:
        raise ChatError("target query is invalid")
    exact_matches = [target for target in targets if target.alias.casefold() == query.casefold()]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        candidates = ", ".join(target.alias for target in exact_matches)
        raise ChatError(f"target is ambiguous or unavailable: {candidates}")
    matches = [target for target in targets if _target_matches(target, query)]
    if len(matches) != 1:
        candidates = ", ".join(target.alias for target in matches)
        raise ChatError(
            "target is ambiguous or unavailable" + (f": {candidates}" if candidates else "")
        )
    return matches[0]


def wrapped_message(source_alias: str, message: str, event_id: str) -> str:
    body = (
        f"From {source_alias} (Cross Agent Chat event {event_id}): {message}\n\n"
        f"Reply with chat_send to {source_alias} only if the sender explicitly asks for a reply."
    )
    return bounded_message(body)


def canonical_source_alias(root: Path, source: Route) -> str:
    """Return the exact currently live public alias for an authenticated sender."""
    if not _route_current(root, source):
        raise ChatError("sender route changed before transport acceptance")
    if source.provider == "codex":
        return source.alias
    agent = exact_agent(source.session_id, source.cwd)
    return claude_alias(source.device, source.project, agent)


def _remaining_operation_timeout(deadline: float, maximum: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ChatError("delivery timed out before provider acceptance")
    return min(maximum, remaining)


def _send_local_target(
    root: Path,
    source: Route,
    target: Target,
    message: str,
    *,
    deadline: float,
) -> dict[str, object]:
    if target.session_id is None or target.pid is None or target.cwd is None:
        raise ChatError("target route is incomplete")
    current_routes = Registry(root).routes()
    route_matches = [
        route
        for route in current_routes
        if route.session_id == target.session_id
        and route.generation == target.generation
        and route.pid == target.pid
        and route.cwd == target.cwd
    ]
    if len(route_matches) != 1:
        raise ChatError("target changed before transport acceptance")
    target_route = route_matches[0]
    source_alias = canonical_source_alias(root, source)
    event_id = str(uuid4())
    body = wrapped_message(source_alias, bounded_message(message), event_id)
    timeout = _remaining_operation_timeout(deadline, ACCEPT_TIMEOUT_SECONDS)
    store = IntentStore(root)
    store.begin(
        source,
        target_route,
        source_alias=source_alias,
        payload_digest=hashlib.sha256(body.encode()).hexdigest(),
        event_id=event_id,
    )
    try:
        response = request_socket(
            socket_path(root, target_route),
            {
                "schema_version": 1,
                "operation": "accept",
                "generation": target_route.generation,
                "event_id": event_id,
                "message": body,
            },
            timeout=timeout,
        )
    except UnknownDeliveryError:
        store.mark(event_id, "UNKNOWN_DELIVERY")
        raise UnknownDeliveryError(
            f"delivery state is unknown for event {event_id}; do not retry automatically"
        ) from None
    except ChatError:
        store.mark(event_id, "PRE_EFFECT_REJECTED")
        raise
    expected = {
        "schema_version": 1,
        "event_id": event_id,
        "status": "TRANSPORT_ACCEPTED",
        "to": target.alias,
        "provider": target.provider,
    }
    rejection = pre_effect_error(response, event_id, target.provider)
    if rejection is not None:
        store.mark(event_id, "PRE_EFFECT_REJECTED")
        # Local courier errors originate in a same-uid process, not a remote peer.
        raise ChatError(rejection)
    if response != expected:
        store.mark(event_id, "UNKNOWN_DELIVERY")
        raise UnknownDeliveryError("delivery state is unknown")
    store.mark(event_id, "TRANSPORT_ACCEPTED")
    return expected


def send_local(root: Path, source: Route, target_query: str, message: str) -> dict[str, object]:
    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    target = resolve_target(local_targets(root), target_query)
    return _send_local_target(root, source, target, message, deadline=deadline)


def send(root: Path, source: Route, target_query: str, message: str) -> dict[str, object]:
    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    local = local_targets(root)
    exact_local_matches = [
        target for target in local if target.alias.casefold() == target_query.casefold()
    ]
    if len(exact_local_matches) == 1:
        target = exact_local_matches[0]
        return _send_local_target(root, source, target, message, deadline=deadline)
    remote, remote_complete = _remote_discovery()
    exact_remote_matches = [
        target for target in remote if target.alias.casefold() == target_query.casefold()
    ]
    if len(exact_remote_matches) == 1:
        target = exact_remote_matches[0]
    else:
        if not remote_complete:
            raise ChatError("remote peer discovery is incomplete; use an exact available recipient")
        target = resolve_target([*local, *remote], target_query)
    if not target.remote:
        return _send_local_target(root, source, target, message, deadline=deadline)
    if target.tailnet_address is None:
        raise ChatError("remote target route is incomplete")
    source_alias = canonical_source_alias(root, source)
    event_id = str(uuid4())
    body = wrapped_message(source_alias, bounded_message(message), event_id)
    timeout = _remaining_operation_timeout(deadline, REMOTE_TIMEOUT_SECONDS)
    store = IntentStore(root)
    store.begin_identity(
        source_key=session_key(source.provider, source.session_id),
        source_generation=source.generation,
        source_alias=source_alias,
        target_key=target.session_key,
        target_generation=target.generation,
        payload_digest=hashlib.sha256(body.encode()).hexdigest(),
        event_id=event_id,
    )
    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source_alias,
        source_generation=source.generation,
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )
    expected: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "status": "TRANSPORT_ACCEPTED",
        "to": target.alias,
        "provider": target.provider,
    }
    try:
        response = request_tailnet(
            target.tailnet_address,
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "receive",
                "envelope": envelope,
            },
            timeout=timeout,
        )
    except UnknownDeliveryError as error:
        store.mark(event_id, "UNKNOWN_DELIVERY")
        raise UnknownDeliveryError(
            f"remote delivery state is unknown for event {event_id}; do not retry automatically"
        ) from error
    except ChatError:
        store.mark(event_id, "PRE_EFFECT_REJECTED")
        raise
    rejection = pre_effect_error(response, event_id, target.provider)
    if rejection is not None:
        store.mark(event_id, "PRE_EFFECT_REJECTED")
        raise ChatError("remote target rejected the message before provider effect")
    if response != expected:
        store.mark(event_id, "UNKNOWN_DELIVERY")
        raise UnknownDeliveryError("remote delivery state is unknown")
    store.mark(event_id, "TRANSPORT_ACCEPTED")
    return expected


def authorize_remote(
    root: Path,
    *,
    event_id: str,
    source_alias: str,
    source_generation: str,
    target_key: str,
    target_generation: str,
    payload_digest: str,
) -> dict[str, object]:
    """Authorize one envelope against an authenticated local sender intent."""
    identifier = valid_uuid(event_id, "event id")
    exact_alias = valid_name(source_alias, "source alias")
    exact_source_generation = valid_uuid(source_generation, "source generation")
    exact_target_generation = valid_uuid(target_generation, "target generation")
    if not IntentStore(root).claim_remote_authorization(
        event_id=event_id,
        source_generation=exact_source_generation,
        source_alias=exact_alias,
        target_key=target_key,
        target_generation=exact_target_generation,
        payload_digest=payload_digest,
    ):
        raise ChatError("remote envelope is not authorized")
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": identifier,
        "status": "AUTHORIZED",
        "source_alias": exact_alias,
        "source_generation": exact_source_generation,
        "target_key": target_key,
        "target_generation": exact_target_generation,
        "payload_digest": payload_digest,
    }


def receive_remote(root: Path, text: str, source_address: str) -> dict[str, object]:
    source_address = valid_tailnet_address(source_address)
    event_id, source_alias, source_generation, target_alias, generation, message = (
        parse_remote_envelope(text)
    )
    target_provider = target_alias.split("@", 1)[0]
    if target_provider not in {"claude", "codex"}:
        raise ChatError("remote target provider is invalid")
    try:
        matches = [
            target
            for target in local_targets(root)
            if target.alias == target_alias and target.generation == generation
        ]
        if len(matches) != 1:
            raise ChatError("remote target changed before transport acceptance")
        target = matches[0]
        if target.session_id is None or target.pid is None or target.cwd is None:
            raise ChatError("remote target route is incomplete")
        target_key = session_key(target.provider, target.session_id)
        payload_digest = hashlib.sha256(message.encode()).hexdigest()
        authorization_request: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "operation": "authorize",
            "event_id": event_id,
            "source_alias": source_alias,
            "source_generation": source_generation,
            "target_key": target_key,
            "target_generation": generation,
            "payload_digest": payload_digest,
        }
        authorization = request_tailnet(
            source_address,
            authorization_request,
            timeout=AUTHORIZE_TIMEOUT_SECONDS,
        )
        expected_authorization = {
            key: value for key, value in authorization_request.items() if key != "operation"
        }
        expected_authorization["status"] = "AUTHORIZED"
        if authorization != expected_authorization:
            raise ChatError("remote envelope is not authorized")
        routes = [
            route
            for route in Registry(root).routes()
            if route.session_id == target.session_id
            and route.pid == target.pid
            and route.generation == target.generation
            and route.cwd == target.cwd
        ]
        if len(routes) != 1:
            raise ChatError("remote target changed before transport acceptance")
        response = request_socket(
            socket_path(root, routes[0]),
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "accept",
                "generation": generation,
                "event_id": event_id,
                "message": message,
            },
            timeout=ACCEPT_TIMEOUT_SECONDS,
        )
        expected: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": target.provider,
        }
        if pre_effect_error(response, event_id, target.provider) is not None:
            return {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id,
                "status": "PRE_EFFECT_REJECTED",
                "provider": target.provider,
                "error": "remote destination rejected before provider effect",
            }
        if response != expected:
            raise UnknownDeliveryError("remote delivery state is unknown")
        return expected
    except UnknownDeliveryError:
        raise
    except ChatError:
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "status": "PRE_EFFECT_REJECTED",
            "provider": target_provider,
            "error": "remote destination rejected before provider effect",
        }


def peers(
    root: Path,
    *,
    include_remote: bool = True,
    internal: bool = False,
    include_delivery_mode: bool = False,
) -> dict[str, object]:
    targets = all_targets(
        root,
        include_remote=include_remote,
        include_delivery_mode=include_delivery_mode,
    )
    items: list[dict[str, str]] = []
    for target in targets:
        item = target.public(include_delivery_mode=include_delivery_mode)
        if internal:
            item["generation"] = target.generation
            item["session_key"] = target.session_key
        items.append(item)
    return {"schema_version": SCHEMA_VERSION, "peers": items}


def codex_stop(pid: int, state_root_value: str | None) -> None:
    if not presence_is_enabled():
        return
    raw = hook_input("Stop")
    if raw.get("stop_hook_active") is True:
        print("{}", flush=True)
        return
    root = state_root(state_root_value)
    session_id = cast(str, raw["session_id"])
    cwd = cast(str, raw["cwd"])
    routes = [
        route
        for route in Registry(root).routes()
        if route.provider == "codex"
        and route.session_id == session_id
        and route.cwd == cwd
        and route.pid == pid
    ]
    if len(routes) != 1 or not _route_current(root, routes[0]):
        print("{}", flush=True)
        return
    route = routes[0]
    peek = request_socket(
        socket_path(root, route),
        {"schema_version": 1, "operation": "peek", "generation": route.generation},
    )
    raw_messages = peek.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        print("{}", flush=True)
        return
    messages: list[dict[str, str]] = []
    for item in raw_messages:
        if not isinstance(item, dict) or set(item) != {"event_id", "message"}:
            raise ChatError("Codex courier response is invalid")
        event_id = item.get("event_id")
        message = item.get("message")
        if not isinstance(event_id, str) or not isinstance(message, str):
            raise ChatError("Codex courier response is invalid")
        messages.append({"event_id": valid_uuid(event_id, "event id"), "message": message})

    snapshot = CodexCourier(alias=route.alias, generation=route.generation)
    for item in messages:
        snapshot.accept(item["event_id"], item["message"])

    def emit(payload: dict[str, object]) -> None:
        print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), flush=True)

    deliver_at_stop(snapshot, stop_hook_active=False, emit=emit)
    try:
        request_socket(
            socket_path(root, route),
            {
                "schema_version": 1,
                "operation": "ack",
                "generation": route.generation,
                "event_ids": [item["event_id"] for item in messages],
            },
        )
    except ChatError:
        return


def authenticate_mcp_sender(
    root: Path, provider: str, parent_pid: int, thread_id: str | None
) -> Route:
    return authenticate_sender(Registry(root).routes(), provider, parent_pid, thread_id)
