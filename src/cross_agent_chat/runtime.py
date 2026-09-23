"""Provider process registration, Codex courier, discovery, and delivery."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import tomllib
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
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType
from typing import BinaryIO, Final, Literal, cast
from uuid import uuid4

from cross_agent_chat import claude_runtime
from cross_agent_chat.claude_runtime import (
    AGENTS_TIMEOUT_SECONDS,
    COURIER_ENV_KEYS,
    DISCOVERY_TIMEOUT_SECONDS,
    SEND_TIMEOUT_SECONDS,
    ClaudeAgentsPreflightTimeout,
    ClaudeSendMessageUnknownDelivery,
    claude_alias,
    claude_binary,
    courier_environment,
    discover_target_ref,
    exact_agent,
    sendmessage,
)
from cross_agent_chat.codex import (
    CodexCourier,
    hook_context,
    native_account_digest,
    native_thread_titles,
)
from cross_agent_chat.core import (
    MAX_MESSAGE_BYTES,
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
    stored_cwd,
    valid_device,
    valid_name,
    valid_uuid,
)
from cross_agent_chat.devin import (
    DEVIN_CAPABILITY_FIELD,
    DevinCapabilityStore,
    DevinCapabilityTool,
    DevinHookEvent,
    build_pretool_callback,
    build_stop_callback_payload,
    build_user_prompt_callback_payload,
    devin_binary,
    devin_profile_root,
    parse_hook_input,
    parse_pretool_input,
)
from cross_agent_chat.native_helper import (
    NATIVE_HELPER_MODEL,
    NATIVE_QUEUE_BINARY_ENV_VAR,
    NATIVE_QUEUE_ENV_VALUE,
    NATIVE_QUEUE_ENV_VAR,
    NativeDispatchStore,
    NativeHelperStore,
    native_helper_create_hook_group,
    native_helper_dispatch_hook_group,
)
from cross_agent_chat.recipient import (
    RecipientToken,
    local_origin,
    local_token,
    parse_recipient_token,
    remote_token,
)
from cross_agent_chat.remote import parse_remote_envelope
from cross_agent_chat.tailnet import (
    TAILNET_PORT,
    TailnetIdentity,
    tailnet_identity,
    valid_tailnet_address,
)
from cross_agent_chat.transport import remote_envelope

MAX_FRAME_BYTES: Final = 64 * 1024
# The one answer a broker can give a connection it refuses to admit: no request
# byte was consumed and no effect is possible, so the refusal is decided rather
# than unknown. It is emitted only before any request byte is read, which keeps
# it impossible to confuse with a response to a request that was dispatched.
BROKER_CAPACITY_REFUSAL: Final[dict[str, object]] = {
    "schema_version": SCHEMA_VERSION,
    "status": "REFUSED",
    "detail": "recipient broker is at capacity",
}
SOCKET_TIMEOUT_SECONDS: Final = 5.0
MCP_TOOL_TIMEOUT_SECONDS: Final = 270.0
OPERATION_TIMEOUT_SECONDS: Final = 260.0
SOURCE_INVENTORY_RETRY_MINIMUM_SECONDS: Final = 1.0
HEALTH_TIMEOUT_SECONDS: Final = AGENTS_TIMEOUT_SECONDS + 2.0
REMOTE_DISCOVERY_TIMEOUT_SECONDS: Final = HEALTH_TIMEOUT_SECONDS + 5.0
ACCEPT_TIMEOUT_SECONDS: Final = (
    2 * AGENTS_TIMEOUT_SECONDS + DISCOVERY_TIMEOUT_SECONDS + SEND_TIMEOUT_SECONDS + 5.0
)
AUTHORIZE_TIMEOUT_SECONDS: Final = 20.0
COURIER_STARTUP_SECONDS: Final = 15.0
MAX_COURIER_DIAGNOSTIC_BYTES: Final = 1024
BOOTSTRAP_FRAME_TIMEOUT_SECONDS: Final = 0.1
REMOTE_TIMEOUT_SECONDS: Final = (
    HEALTH_TIMEOUT_SECONDS + AUTHORIZE_TIMEOUT_SECONDS + ACCEPT_TIMEOUT_SECONDS + 5.0
)
LOCAL_DISCOVERY_WORKERS: Final = 32
LOCAL_DISCOVERY_TIMEOUT_SECONDS: Final = HEALTH_TIMEOUT_SECONDS
NATIVE_TITLE_TIMEOUT_SECONDS: Final = 2.0
NATIVE_DESKTOP_APPLICATIONS: Final = Path("/Applications")
NATIVE_DESKTOP_BUNDLE_IDS: Final = ("com.openai.chat", "com.openai.codex")
PRESENCE_ENV_VAR: Final = "CROSS_AGENT_CHAT_PRESENCE"
PROC_PIDTBSDINFO: Final = 3
PROC_BSDINFO_SIZE: Final = 136
DeliveryMode = Literal[
    "claude_native_cross_session",
    "codex_stop_bound",
    "codex_experimental_queue",
    "devin_stop_or_prompt_bound",
]
DeliveryMechanism = Literal[
    "claude_native",
    "native_helper",
    "direct_queue",
    "stop_bound",
    "devin_prompt_bound",
]
DELIVERY_MECHANISMS: Final = frozenset(
    {"claude_native", "devin_prompt_bound", "direct_queue", "native_helper", "stop_bound"}
)


class RegistrationInterrupted(SystemExit):
    """Interrupt one in-flight courier registration without retrying its health probe."""

    def __init__(self) -> None:
        super().__init__(143)


def recipient_profile_root(provider: str) -> str:
    if provider == "devin":
        return str(devin_profile_root())
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
    if provider == "devin" and binary != devin_binary():
        raise ChatError("provider process identity is unavailable")
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
    tailnet_node_id: str | None = None
    delivery_mode: DeliveryMode | None = None
    delivery_mechanism: DeliveryMechanism | None = None
    title: str | None = None

    def public(
        self,
        *,
        include_delivery_mode: bool = False,
        include_delivery_mechanism: bool = False,
        include_handle: bool = True,
        handle: str | None = None,
        include_title: bool = True,
    ) -> dict[str, str]:
        result = {
            "alias": self.alias,
            "provider": self.provider,
            "device": self.device,
            "project": self.project,
            "status": "available",
        }
        if include_handle:
            result["handle"] = self.session_key if handle is None else handle
        if include_title and self.title is not None:
            result["title"] = self.title
        if include_delivery_mode:
            result["delivery_mode"] = (
                "unknown" if self.delivery_mode is None else self.delivery_mode
            )
            if include_delivery_mechanism:
                result["delivery_mechanism"] = (
                    "unknown" if self.delivery_mechanism is None else self.delivery_mechanism
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


def read_frame(
    connection: socket.socket,
    limit: int = MAX_FRAME_BYTES,
    *,
    deadline: float | None = None,
) -> bytes:
    # One absolute deadline bounds the whole frame. Callers that pass no
    # deadline get theirs from the socket's configured timeout, so a slow
    # trickle spends the same budget instead of renewing it on every recv.
    if deadline is None:
        configured = connection.gettimeout()
        if configured is not None:
            deadline = time.monotonic() + configured
    payload = b""
    while b"\n" not in payload:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("courier frame deadline expired")
            connection.settimeout(remaining)
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
    deadline = time.monotonic() + timeout
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    attempted_write = False
    try:
        require_socket(path)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("courier exchange deadline expired before connect")
        client.settimeout(remaining)
        client.connect(str(path))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("courier exchange deadline expired before write")
        client.settimeout(remaining)
        attempted_write = True
        emit_frame(client, payload)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("courier exchange deadline expired before read")
        client.settimeout(remaining)
        raw = json.loads(read_frame(client, deadline=deadline))
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
    deadline = time.monotonic() + timeout
    attempted_write = False
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Tailnet exchange deadline expired before connect")
        client = socket.create_connection((address, port), timeout=remaining)
        with client:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Tailnet exchange deadline expired before write")
            client.settimeout(remaining)
            attempted_write = True
            emit_frame(client, payload)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Tailnet exchange deadline expired before read")
            client.settimeout(remaining)
            raw: object = json.loads(read_frame(client, deadline=deadline))
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
    raw["_cwd_unavailable"] = False
    if expected_event != "SessionEnd":
        raw["cwd"] = canonical_cwd(cwd)
        return cast(dict[str, object], raw)
    try:
        raw["cwd"] = canonical_cwd(cwd)
    except ChatError as error:
        if not isinstance(error.__cause__, FileNotFoundError):
            raise error
        raw["cwd"] = stored_cwd(cwd)
        raw["_cwd_unavailable"] = True
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


def _native_claude_cwd(session_id: str) -> str | None:
    try:
        agents = claude_runtime.claude_agents(session_id, timeout=2.0)
    except TypeError as error:
        # Preserve compatibility with test/provider shims that predate the optional
        # timeout while keeping the real provider call bounded above.
        if "timeout" not in str(error):
            return None
        try:
            agents = claude_runtime.claude_agents(session_id)
        except (ChatError, TypeError):
            return None
    except ChatError:
        return None
    supported = [
        agent
        for agent in agents
        if agent["session_id"] == session_id and agent["kind"] in {"interactive", "background"}
    ]
    return supported[0]["cwd"] if len(supported) == 1 else None


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
    elif route.provider == "codex":
        environment = {
            key: os.environ[key]
            for key in (*COURIER_ENV_KEYS, "CODEX_HOME", NATIVE_QUEUE_ENV_VAR)
            if key in os.environ
        }
        environment["CODEX_HOME"] = route.profile_root or recipient_profile_root("codex")
        codex = str(owner_binary) if owner_binary is not None else shutil.which("codex")
        if codex is not None:
            with suppress(OSError):
                environment[NATIVE_QUEUE_BINARY_ENV_VAR] = str(Path(codex).resolve(strict=True))
    else:
        environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
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
        deadline = time.monotonic() + COURIER_STARTUP_SECONDS
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
    if provider not in {"claude", "codex", "devin"}:
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
            prior_cwd_drift = [
                item
                for item in registry.routes()
                if route.provider == "claude"
                and item.provider == route.provider
                and item.session_id == route.session_id
                and item.device == route.device
                and item.pid == route.pid
                and item.owner_identity == route.owner_identity
                and item.profile_root == route.profile_root
                and item.cwd != route.cwd
            ]
            prior_route = prior_cwd_drift[0] if len(prior_cwd_drift) == 1 else None
            registered = (
                prior_route
                if prior_route is not None
                else registry.upsert_or_reuse_live_owner(route)
            )
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
                        native_cwd = (
                            _native_claude_cwd(route.session_id)
                            if prior_route is not None
                            else None
                        )
                        if prior_route is not None and native_cwd == route.cwd:
                            registry.upsert(route)
                            _spawn_courier(root, route)
                            return route
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
    if provider not in {"claude", "codex", "devin"}:
        raise ChatError("provider is invalid")
    raw = hook_input("SessionEnd")
    root = state_root(state_root_value)
    session_id = cast(str, raw["session_id"])
    cwd = cast(str, raw["cwd"])
    cwd_unavailable = raw.get("_cwd_unavailable") is True
    session_routes = [
        route
        for route in Registry(root).routes()
        if route.provider == provider and route.session_id == session_id and route.pid == pid
    ]
    routes = [route for route in session_routes if cwd_unavailable or route.cwd == cwd]
    if not routes and not session_routes:
        return
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


def devin_hook_cwd() -> str:
    """Resolve the provider-supplied workspace root for a Devin hook."""

    value = os.environ.get("DEVIN_PROJECT_DIR")
    if not isinstance(value, str) or not value:
        raise ChatError("Devin hook workspace is unavailable")
    return canonical_cwd(value)


def _devin_route(
    root: Path, event_session_id: str, pid: int, *, require_cwd: bool = True
) -> Route | None:
    """Resolve Devin's exact route from hook session, trusted cwd, and process."""

    cwd: str | None = None
    if require_cwd:
        try:
            cwd = devin_hook_cwd()
        except (OSError, ChatError):
            return None
    matches = [
        route
        for route in Registry(root).routes()
        if route.provider == "devin"
        and route.session_id == event_session_id
        and route.pid == pid
        and (cwd is None or route.cwd == cwd)
    ]
    return matches[0] if len(matches) == 1 else None


def _register_devin_prompt(device: str, pid: int, root: Path, event: DevinHookEvent) -> Route:
    """Register the first trusted prompt for one exact local Devin session."""

    cwd = devin_hook_cwd()
    owner_identity, _ = recipient_owner_identity("devin", pid)
    route = Route.create(
        provider="devin",
        session_id=event.session_id,
        device=valid_device(device),
        cwd=cwd,
        pid=pid,
        owner_identity=owner_identity,
        profile_root=recipient_profile_root("devin"),
    )
    try:
        with (
            _registration_sigterm_scope(),
            state_lock(root, "register-" + session_key(route.provider, route.session_id)),
        ):
            registry = Registry(root)
            registry.compact_dead()
            existing_session = [
                item
                for item in registry.routes()
                if item.provider == "devin" and item.session_id == route.session_id
            ]
            if existing_session and any(
                item.pid != route.pid
                or item.cwd != route.cwd
                or item.profile_root != route.profile_root
                or item.owner_identity != route.owner_identity
                for item in existing_session
            ):
                raise ChatError("exact Devin session route is unavailable")
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


def register_devin(device: str, pid: int, state_root_value: str | None) -> Route | None:
    """Validate a Devin SessionStart without publishing an inactive inbox."""

    if not presence_is_enabled():
        return None
    if isinstance(pid, bool) or pid <= 0:
        raise ChatError("provider process is invalid")
    parse_hook_input(sys.stdin.read(MAX_FRAME_BYTES + 1), expected_event="SessionStart")
    devin_hook_cwd()
    recipient_owner_identity("devin", pid)
    valid_device(device)
    return None


def unregister_devin(pid: int, state_root_value: str | None) -> None:
    if not presence_is_enabled():
        return
    event = parse_hook_input(sys.stdin.read(MAX_FRAME_BYTES + 1), expected_event="SessionEnd")
    root = state_root(state_root_value)
    DevinCapabilityStore(root).revoke_session(event.session_id)
    session_routes = [
        item
        for item in Registry(root).routes()
        if item.provider == "devin" and item.session_id == event.session_id
    ]
    route = _devin_route(root, event.session_id, pid)
    if route is None:
        if not session_routes:
            return
        raise ChatError("exact Devin session route is unavailable")
    Registry(root).remove(route.provider, route.session_id, route.pid, generation=route.generation)
    with suppress(ChatError):
        request_socket(
            socket_path(root, route),
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "shutdown",
                "generation": route.generation,
            },
        )


def devin_pretool(state_root_value: str | None) -> None:
    """Bind one exact Devin MCP call to the hook session that authorized it."""

    if not presence_is_enabled():
        return
    event = parse_pretool_input(sys.stdin.read(MAX_FRAME_BYTES + 1))
    prefix = "mcp__cross-agent-chat__"
    if not event.tool_name.startswith(prefix):
        return
    tool_name = event.tool_name.removeprefix(prefix)
    if tool_name not in {"chat_peers", "chat_send", "chat_status"}:
        return
    root = state_root(state_root_value)
    route = _devin_route(root, event.session_id, os.getppid())
    if route is None or not _route_current(root, route):
        DevinCapabilityStore(root).revoke_session(event.session_id)
        return
    arguments = {
        key: value for key, value in event.tool_input.items() if key != DEVIN_CAPABILITY_FIELD
    }
    typed_tool = cast(DevinCapabilityTool, tool_name)
    token = DevinCapabilityStore(root).issue(
        route,
        prompt_id=event.prompt_id,
        tool_name=typed_tool,
        arguments=arguments,
    )
    payload = build_pretool_callback(replace(event, tool_input=arguments), token)
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), flush=True)


def authenticate_devin_capability(
    root: Path,
    *,
    parent_pid: int,
    tool_name: str,
    arguments: dict[str, object],
) -> Route:
    token = arguments.get(DEVIN_CAPABILITY_FIELD)
    if not isinstance(token, str):
        raise ChatError("Devin sender capability is required")
    original_arguments = {
        key: value for key, value in arguments.items() if key != DEVIN_CAPABILITY_FIELD
    }
    if tool_name not in {"chat_peers", "chat_send", "chat_status"}:
        raise ChatError("Devin sender capability tool is invalid")
    capability = DevinCapabilityStore(root).consume(
        token,
        tool_name=cast(DevinCapabilityTool, tool_name),
        arguments=original_arguments,
    )
    route = _devin_route(root, capability.session_id, parent_pid, require_cwd=False)
    if (
        route is None
        or route.generation != capability.generation
        or not _route_current(root, route)
    ):
        DevinCapabilityStore(root).revoke_session(capability.session_id)
        raise ChatError("Devin sender capability route is unavailable")
    return route


def _devin_messages(root: Path, route: Route) -> list[dict[str, str]]:
    response = request_socket(
        socket_path(root, route),
        {"schema_version": SCHEMA_VERSION, "operation": "peek", "generation": route.generation},
    )
    raw_messages = response.get("messages")
    if not isinstance(raw_messages, list):
        raise ChatError("Devin courier response is invalid")
    messages: list[dict[str, str]] = []
    for item in raw_messages:
        if not isinstance(item, dict) or set(item) != {"event_id", "message"}:
            raise ChatError("Devin courier response is invalid")
        event_id = item.get("event_id")
        message = item.get("message")
        if not isinstance(event_id, str) or not isinstance(message, str):
            raise ChatError("Devin courier response is invalid")
        messages.append(
            {
                "event_id": valid_uuid(event_id, "event id"),
                "message": bounded_message(message),
            }
        )
    return messages


def _ack_devin(root: Path, route: Route, messages: list[dict[str, str]]) -> None:
    request_socket(
        socket_path(root, route),
        {
            "schema_version": SCHEMA_VERSION,
            "operation": "ack",
            "generation": route.generation,
            "event_ids": [item["event_id"] for item in messages],
        },
    )


def devin_stop(pid: int, state_root_value: str | None) -> None:
    if not presence_is_enabled():
        return
    event = parse_hook_input(sys.stdin.read(MAX_FRAME_BYTES + 1), expected_event="Stop")
    root = state_root(state_root_value)
    DevinCapabilityStore(root).revoke_session(event.session_id)
    if event.stop_hook_active is True:
        print("{}", flush=True)
        return
    route = _devin_route(root, event.session_id, pid)
    if route is None or not _route_current(root, route):
        print("{}", flush=True)
        return
    messages = _devin_messages(root, route)
    if not messages:
        print("{}", flush=True)
        return
    selected = messages[0]
    payload = build_stop_callback_payload(selected["event_id"], selected["message"])
    _ack_devin(root, route, [selected])
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), flush=True)


def devin_user_prompt(pid: int, state_root_value: str | None, device: str | None = None) -> None:
    if not presence_is_enabled():
        return
    event = parse_hook_input(sys.stdin.read(MAX_FRAME_BYTES + 1), expected_event="UserPromptSubmit")
    root = state_root(state_root_value)
    route = (
        _devin_route(root, event.session_id, pid)
        if device is None
        else _register_devin_prompt(valid_device(device), pid, root, event)
    )
    DevinCapabilityStore(root).revoke_session(event.session_id)
    if route is None or not _route_current(root, route):
        return
    messages = _devin_messages(root, route)
    if not messages:
        return
    selected = messages[0]
    payload = build_user_prompt_callback_payload(selected["message"])
    _ack_devin(root, route, [selected])
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), flush=True)


def _route_current(root: Path, expected: Route) -> bool:
    if expected.owner_identity is not None:
        profile = expected.profile_root or recipient_profile_root(expected.provider)
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
        if route.provider == "devin":
            if courier is None:
                raise ChatError("Devin inbox is unavailable")
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
    except ClaudeSendMessageUnknownDelivery as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "status": "UNKNOWN_DELIVERY",
            "provider": route.provider,
            "diagnostic": f"claude_{error.phase}",
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


FORWARDABLE_PRE_EFFECT_REASONS: Final = frozenset(
    {
        "Claude ListAgents discovery timed out",
        "Claude ListAgents discovery failed",
        "Claude target discovery is not one exact supported match",
        "Claude target is not one exact live supported session",
        "Claude target changed during discovery",
        "Claude agents preflight timed out",
        "Claude Code executable is unavailable",
        "Claude agents response is invalid",
        "Claude target reference is invalid",
        "Claude SendMessage courier setup failed",
        "Claude courier finished without a SendMessage call",
        "session courier is still bootstrapping",
        "Codex courier is unavailable",
        "Codex courier queue is full",
        "Codex native queue is unavailable",
        "Codex native queue response exceeds the bounded limit",
        "Codex native queue profile changed",
        "Codex native queue preflight failed",
        "Codex native queue rejected the message before acceptance",
        "Devin inbox is unavailable",
        "message is invalid",
        "message must not be empty",
        "message exceeds the 16 KiB limit",
        "message exceeds the encoded frame budget",
    }
)


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


def unknown_delivery_diagnostic(
    response: dict[str, object], event_id: str, provider: Provider
) -> str | None:
    diagnostic = response.get("diagnostic")
    if (
        set(response) != {"schema_version", "event_id", "status", "provider", "diagnostic"}
        or response.get("schema_version") != SCHEMA_VERSION
        or response.get("event_id") != event_id
        or response.get("status") != "UNKNOWN_DELIVERY"
        or response.get("provider") != provider
        or provider != "claude"
        or not isinstance(diagnostic, str)
        or diagnostic
        not in {
            "claude_pretool_gate_unobserved",
            "claude_pretool_gate_unreadable",
            "claude_pretool_gate_denied",
            "claude_pretool_gate_conflict",
            "claude_no_sendmessage_tool_use",
            "claude_multiple_sendmessage_tool_use",
            "claude_sendmessage_payload_mismatch",
            "claude_sendmessage_target_mismatch",
            "claude_sendmessage_message_mismatch",
            "claude_sendmessage_control_mismatch",
            "claude_sendmessage_type_mismatch",
            "claude_sendmessage_summary_mismatch",
            "claude_helper_stream_invalid",
            "claude_helper_timeout",
            "claude_helper_execution_failed",
            "claude_helper_exit_nonzero",
            "claude_receipt_invalid",
        }
    ):
        return None
    return diagnostic


def _delivery_mode(
    route: Route, courier: CodexCourier | None, *, native_helper: bool = False
) -> DeliveryMode:
    if route.provider == "claude":
        return "claude_native_cross_session"
    if route.provider == "devin":
        return "devin_stop_or_prompt_bound"
    return (
        "codex_experimental_queue"
        if native_helper or (courier is not None and courier.native_queue)
        else "codex_stop_bound"
    )


def _delivery_mechanism(
    route: Route, courier: CodexCourier | None, *, native_helper: bool = False
) -> DeliveryMechanism:
    """Name the exact delivery path behind one coarse delivery mode."""

    if route.provider == "claude":
        return "claude_native"
    if route.provider == "devin":
        return "devin_prompt_bound"
    if native_helper:
        return "native_helper"
    return "direct_queue" if courier is not None and courier.native_queue else "stop_bound"


def courier_health(
    route: Route,
    courier: CodexCourier | None = None,
    *,
    include_delivery_mode: bool = False,
    include_direct_delivery_mode: bool = False,
    include_delivery_mechanism: bool = False,
    native_helper: bool = False,
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
        response["delivery_mode"] = _delivery_mode(route, courier, native_helper=native_helper)
    if include_delivery_mechanism:
        response["delivery_mechanism"] = _delivery_mechanism(
            route, courier, native_helper=native_helper
        )
    if include_direct_delivery_mode:
        response["direct_delivery_mode"] = _delivery_mode(route, courier)
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
    helper_lineage = provider == "codex" and NativeHelperStore(root).is_helper_lineage(route)
    helper_queue = False
    if helper_lineage:
        try:
            identity, _ = recipient_owner_identity("codex", route.pid, route.profile_root)
        except (ChatError, OSError):
            identity = ""
        helper_queue = (
            identity == route.owner_identity and native_desktop_bundle(route.pid) is not None
        )
    native_enabled = os.environ.get(NATIVE_QUEUE_ENV_VAR) == NATIVE_QUEUE_ENV_VALUE or helper_queue
    if provider == "codex" and native_enabled:
        bound_binary = os.environ.get(NATIVE_QUEUE_BINARY_ENV_VAR)
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
        CodexCourier(
            alias=route.alias,
            generation=route.generation,
            native_queue=native_queue,
            native_helper=helper_lineage,
            provider=cast(Provider, provider),
        )
        if provider in {"codex", "devin"}
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
                                include_direct_delivery_mode=(
                                    request.get("include_direct_delivery_mode") is True
                                ),
                                include_delivery_mechanism=(
                                    request.get("include_delivery_mechanism") is True
                                ),
                                native_helper=(
                                    NativeHelperStore(root).helper_for_original(
                                        route, Registry(root).routes()
                                    )
                                    is not None
                                ),
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
                elif operation == "native_dispatch":
                    event_id = request.get("event_id")
                    if (
                        route.provider != "codex"
                        or courier is None
                        or not isinstance(event_id, str)
                    ):
                        continue
                    try:
                        message = courier.native_dispatch_message(event_id)
                    except ChatError as error:
                        emit_frame_safely(
                            connection,
                            {
                                "schema_version": SCHEMA_VERSION,
                                "status": "UNAVAILABLE",
                                "generation": route.generation,
                                "error": str(error),
                            },
                        )
                    else:
                        emit_frame_safely(
                            connection,
                            {
                                "schema_version": SCHEMA_VERSION,
                                "status": "NATIVE_DISPATCH",
                                "generation": route.generation,
                                "event_id": event_id,
                                "message": message,
                            },
                        )
                elif operation == "native_dispatch_ack":
                    event_id = request.get("event_id")
                    if (
                        route.provider != "codex"
                        or courier is None
                        or not isinstance(event_id, str)
                    ):
                        continue
                    try:
                        courier.acknowledge([event_id])
                    except ChatError:
                        continue
                    emit_frame_safely(
                        connection,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "NATIVE_DISPATCH_ACKED",
                            "generation": route.generation,
                            "event_id": event_id,
                        },
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
                "include_delivery_mechanism": True,
            },
            timeout=timeout,
        )
    except ChatError:
        return None
    alias = response.get("alias")
    expected = {"schema_version", "status", "generation", "alias"}
    observed_mode = response.get("delivery_mode")
    observed_mechanism = response.get("delivery_mechanism")
    if (
        set(response)
        not in (
            expected,
            expected | {"delivery_mode"},
            expected | {"delivery_mode", "delivery_mechanism"},
        )
        or response.get("schema_version") != SCHEMA_VERSION
        or response.get("status") != "READY"
        or response.get("generation") != route.generation
        or not isinstance(alias, str)
        or (
            observed_mode is not None
            and observed_mode
            not in {
                "claude_native_cross_session",
                "codex_stop_bound",
                "codex_experimental_queue",
                "devin_stop_or_prompt_bound",
            }
        )
        or (observed_mechanism is not None and observed_mechanism not in DELIVERY_MECHANISMS)
    ):
        return None
    if route.provider == "codex":
        if alias != route.alias:
            return None
    elif route.provider == "claude":
        prefix = f"claude@{route.device}:{route.project}:"
        shortened = prefix[:115] + "~" + session_key("claude", route.session_id)[:12]
        if not alias.startswith(prefix) and not (len(prefix) > 115 and alias == shortened):
            return None
    elif alias != route.alias:
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
            delivery_mechanism=(
                cast(DeliveryMechanism, observed_mechanism)
                if observed_mechanism is not None
                else None
            ),
        )
    except ChatError:
        return None


def _local_target_before_deadline(root: Path, route: Route, deadline: float) -> Target | None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return _local_target(root, route, timeout=min(HEALTH_TIMEOUT_SECONDS, remaining))


def local_targets(root: Path, *, handle: str | None = None) -> list[Target]:
    routes = [
        route
        for route in Registry(root).routes()
        if (handle is None or session_key(route.provider, route.session_id) == handle)
        and route.process_is_live()
        and route.cwd_is_available()
        and not NativeHelperStore(root).is_helper_lineage(route)
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


def _with_codex_titles(root: Path, targets: list[Target], deadline: float) -> list[Target]:
    """Add optional title hints after courier health, never as route authority."""
    groups: dict[tuple[Path, str], list[Target]] = {}
    routes = Registry(root).routes()
    for target in targets:
        if target.provider != "codex" or target.pid is None or target.session_id is None:
            continue
        route_matches = [
            route
            for route in routes
            if route.provider == "codex"
            and route.pid == target.pid
            and route.session_id == target.session_id
            and route.generation == target.generation
        ]
        if len(route_matches) != 1:
            continue
        profile = route_matches[0].profile_root or recipient_profile_root("codex")
        try:
            identity, binary = recipient_owner_identity("codex", target.pid, profile)
        except (ChatError, OSError):
            continue
        if route_matches[0].owner_identity != identity:
            continue
        groups.setdefault((binary, profile), []).append(target)
    titles: dict[str, str] = {}
    for (binary, profile), group in groups.items():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        titles.update(
            native_thread_titles(
                binary=binary,
                environment={"CODEX_HOME": profile},
                thread_ids=[target.session_id for target in group if target.session_id is not None],
                deadline=time.monotonic() + min(remaining, NATIVE_TITLE_TIMEOUT_SECONDS),
            )
        )
    return [
        target if target.remote else replace(target, title=titles.get(target.session_id or ""))
        for target in targets
    ]


def _targets_from_tailnet(
    address: str,
    raw: object,
    *,
    include_delivery_mode: bool = False,
    include_title: bool = False,
    include_devin: bool = False,
    node_id: str | None = None,
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
        allowed = (
            required
            | ({"delivery_mode"} if include_delivery_mode else set())
            | ({"title"} if include_title else set())
        )
        if not isinstance(raw_item, dict) or not required <= set(raw_item) <= allowed:
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
            not isinstance(provider, str)
            or provider not in {"claude", "codex", "devin"}
            or (provider == "devin" and not include_devin)
            or not all(isinstance(value, str) for value in values)
            or item.get("status") != "available"
            or not re.fullmatch(r"[0-9a-f]{64}", cast(str, item.get("session_key")))
            or (
                "delivery_mode" in item
                and (
                    not isinstance(item["delivery_mode"], str)
                    or item["delivery_mode"]
                    not in {
                        "claude_native_cross_session",
                        "codex_stop_bound",
                        "codex_experimental_queue",
                        "devin_stop_or_prompt_bound",
                        "unknown",
                    }
                )
            )
        ):
            raise ChatError("Tailnet peer returned invalid discovery")
        observed_mode = item.get("delivery_mode")
        title = item.get("title")
        if title is not None and (not include_title or not isinstance(title, str)):
            raise ChatError("Tailnet peer returned invalid discovery")
        targets.append(
            Target(
                alias=valid_name(cast(str, item["alias"]), "remote alias"),
                provider=cast(Provider, provider),
                device=valid_device(cast(str, item["device"])),
                project=valid_name(cast(str, item["project"]), "remote project"),
                generation=valid_uuid(cast(str, item["generation"]), "route generation"),
                session_key=cast(str, item["session_key"]),
                remote=True,
                tailnet_address=address,
                tailnet_node_id=node_id,
                delivery_mode=(
                    cast(DeliveryMode, observed_mode)
                    if isinstance(observed_mode, str) and observed_mode != "unknown"
                    else None
                ),
                title=valid_name(title, "remote title") if isinstance(title, str) else None,
            )
        )
    return targets


def _remote_node_targets(
    address: str,
    deadline: float | None = None,
    *,
    include_delivery_mode: bool = False,
    include_title: bool = False,
    include_devin: bool = False,
    handle: str | None = None,
    node_id: str | None = None,
) -> tuple[list[Target], bool]:
    if deadline is None:
        deadline = time.monotonic() + REMOTE_DISCOVERY_TIMEOUT_SECONDS
    legacy: dict[str, object] = {"schema_version": SCHEMA_VERSION, "operation": "peers"}
    variants: list[tuple[dict[str, object], bool]] = []
    if handle is not None:
        bound: dict[str, object] = {**legacy, "handle": handle}
        if include_delivery_mode:
            bound["include_delivery_mode"] = True
        if include_devin:
            bound["include_devin"] = True
        variants.append((bound, include_delivery_mode))
    if include_delivery_mode and include_devin:
        variants.append(({**legacy, "include_delivery_mode": True, "include_devin": True}, True))
    if include_delivery_mode:
        variants.append(({**legacy, "include_delivery_mode": True}, True))
    if include_devin:
        variants.append(({**legacy, "include_devin": True}, False))
    variants.append((legacy, False))
    base: list[Target] | None = None
    for payload, mode_requested in variants:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return [], False
        try:
            raw = request_tailnet(
                address, payload, timeout=min(REMOTE_DISCOVERY_TIMEOUT_SECONDS, remaining)
            )
            base = _targets_from_tailnet(
                address,
                raw,
                include_delivery_mode=mode_requested,
                include_devin=include_devin,
                node_id=node_id,
            )
            break
        except (ChatError, UnknownDeliveryError):
            continue
    if base is None:
        return [], False
    remaining = deadline - time.monotonic()
    # Return validated identities even when optional title work cannot finish.
    # Leave time for the outer discovery collector to receive the retained result.
    if not include_title or remaining <= 1.0:
        return base, True
    try:
        rich_payload: dict[str, object] = {
            **legacy,
            "include_delivery_mode": True,
            "include_title": True,
        }
        if include_devin:
            rich_payload["include_devin"] = True
        if handle is not None:
            rich_payload["handle"] = handle
        raw = request_tailnet(
            address,
            rich_payload,
            timeout=min(REMOTE_DISCOVERY_TIMEOUT_SECONDS, remaining - 1.0),
        )
        enriched = _targets_from_tailnet(
            address,
            raw,
            include_delivery_mode=True,
            include_title=True,
            include_devin=include_devin,
            node_id=node_id,
        )
    except (ChatError, UnknownDeliveryError):
        return base, True
    if not include_delivery_mode:
        enriched = [replace(target, delivery_mode=None) for target in enriched]
    base_authority = {target.session_key: replace(target, title=None) for target in base}
    rich_authority = {target.session_key: replace(target, title=None) for target in enriched}
    if (
        len(base_authority) == len(base)
        and len(rich_authority) == len(enriched)
        and rich_authority == base_authority
    ):
        return enriched, True
    return base, True


def _remote_discovery(
    *,
    include_delivery_mode: bool = False,
    include_title: bool = False,
    include_devin: bool = True,
    identity: TailnetIdentity | None = None,
) -> tuple[list[Target], bool]:
    if identity is None:
        identity = tailnet_identity()
    if identity is None or not identity.peers:
        return [], False
    targets: list[Target] = []
    complete = True
    deadline = time.monotonic() + REMOTE_DISCOVERY_TIMEOUT_SECONDS
    workers = ThreadPoolExecutor(max_workers=min(16, len(identity.peers)))
    futures = [
        workers.submit(
            _remote_node_targets,
            address,
            deadline,
            include_delivery_mode=include_delivery_mode,
            include_title=include_title,
            include_devin=include_devin,
            node_id=node_id,
        )
        for node_id, address in identity.peers.items()
    ]
    try:
        try:
            for future in as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
                discovered, node_complete = future.result()
                targets.extend(discovered)
                complete = complete and node_complete
        except FuturesTimeoutError:
            complete = False
    finally:
        for future in futures:
            future.cancel()
        workers.shutdown(wait=False, cancel_futures=True)
    return targets, complete


def remote_targets(
    root: Path,
    *,
    include_delivery_mode: bool = False,
    include_title: bool = False,
    include_devin: bool = False,
) -> list[Target]:
    return _remote_discovery(
        include_delivery_mode=include_delivery_mode,
        include_title=include_title,
        include_devin=include_devin,
    )[0]


def all_targets(
    root: Path,
    *,
    include_remote: bool = True,
    include_delivery_mode: bool = False,
    include_title: bool = False,
) -> list[Target]:
    if include_remote:
        with ThreadPoolExecutor(max_workers=2) as workers:
            local = workers.submit(local_targets, root)
            remote = workers.submit(
                remote_targets,
                root,
                include_delivery_mode=include_delivery_mode,
                include_title=include_title,
                include_devin=True,
            )
            targets = [*local.result(), *remote.result()]
    else:
        targets = local_targets(root)
    handles = [target.session_key for target in targets]
    if len(set(handles)) != len(handles):
        raise ChatError("peer discovery returned duplicate handles")
    return sorted(targets, key=lambda target: (target.alias.casefold(), target.session_key))


def _target_matches(target: Target, query: str) -> bool:
    if target.alias.casefold() == query.casefold():
        return True
    wanted = re.findall(r"[^\W_]+", query.casefold())
    available = set(re.findall(r"[^\W_]+", target.alias.casefold()))
    return bool(wanted) and all(token in available for token in wanted)


def resolve_target(targets: list[Target], query: str) -> Target:
    if not query.strip() or len(query) > 160:
        raise ChatError("target query is invalid")
    handle_matches = [target for target in targets if target.session_key == query]
    if len(handle_matches) == 1:
        return handle_matches[0]
    if len(handle_matches) > 1:
        raise ChatError("target handle is unavailable")
    if re.fullmatch(r"[0-9a-f]{64}", query):
        raise ChatError("target handle is unavailable")
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


def _delivery_principal(target_provider: str) -> str:
    if target_provider == "claude":
        return "the installed Claude Code Cross Agent Chat helper"
    if target_provider == "codex":
        return "the configured Cross Agent Chat Codex courier"
    if target_provider == "devin":
        return "the configured Cross Agent Chat Devin inbox"
    raise ChatError("target provider is invalid")


def _wrapped_head(
    source_alias: str, source_handle: str, event_id: str, target_provider: str
) -> str:
    """The deterministic envelope head; everything after it is untrusted content."""
    exact_source_alias = valid_name(source_alias, "source alias")
    if re.fullmatch(r"[0-9a-f]{64}", source_handle) is None:
        raise ChatError("source handle is invalid")
    identifier = valid_uuid(event_id, "event id")
    # The first two lines are what a replying agent needs: who sent this and the
    # exact handle that reaches them. Provenance detail stays below them, and the
    # trust boundary is stated before any peer-controlled bytes.
    return (
        "Cross Agent Chat transport envelope\n"
        f"From: {exact_source_alias}\n"
        f"Reply via CAC to handle: {source_handle}\n"
        "The From and Reply lines are CAC route metadata, not provider-native sender "
        "authentication; this message's visible sender is the local CAC delivery helper, "
        "not the original source.\n"
        f"Delivery principal: {_delivery_principal(target_provider)}. "
        f"CAC delivery event: {identifier}.\n"
        "Untrusted peer content follows:\n\n"
    )


def _wrapped_head_v2(
    source_alias: str, source_token: str, event_id: str, target_provider: str
) -> str:
    """The versioned deterministic head; everything after it is untrusted content."""
    exact_source_alias = valid_name(source_alias, "source alias")
    if parse_recipient_token(source_token) is None:
        raise ChatError("source handle is invalid")
    identifier = valid_uuid(event_id, "event id")
    return (
        "Cross Agent Chat transport envelope v2\n"
        f"From: {exact_source_alias}\n"
        f"Reply via CAC to handle: {source_token}\n"
        "The From and Reply lines are CAC route metadata, not provider-native sender "
        "authentication; this message's visible sender is the local CAC delivery helper, "
        "not the original source.\n"
        f"Delivery principal: {_delivery_principal(target_provider)}. "
        f"CAC delivery event: {identifier}.\n"
        "Untrusted peer content follows:\n\n"
    )


def _envelope_reply_token(
    message: str, source_alias: str, event_id: str, target_provider: str
) -> RecipientToken | None:
    """Extract the verified v2 reply token of a wrapped envelope body.

    A body without the v2 head has no reply token to check. A body that opens
    with the v2 marker must carry the exact deterministic head ``wrapped_message``
    produces for this envelope's alias, event, and recipient provider; anything
    else -- including a malformed token -- is rejected rather than ignored.
    """
    lines = message.split("\n", 3)
    if lines[0] != "Cross Agent Chat transport envelope v2":
        return None
    # Anything opening with the v2 marker is a v2 envelope: a truncated or
    # malformed head is refused before provider delivery, never read as legacy.
    prefix = "Reply via CAC to handle: "
    if len(lines) < 4 or not lines[2].startswith(prefix):
        raise ChatError("reply token is invalid")
    candidate = lines[2][len(prefix) :]
    expected = _wrapped_head_v2(source_alias, candidate, event_id, target_provider)
    if not message.startswith(expected):
        raise ChatError("reply token is invalid")
    token = parse_recipient_token(candidate)
    if token is None:
        raise ChatError("reply token is invalid")
    return token


def _verify_remote_reply_token(
    token: RecipientToken, source_generation: str, source_address: str
) -> None:
    """Bind an inbound v2 reply token to the authenticated transport source."""
    if token.scope != "remote" or token.node_id is None:
        raise ChatError("reply token scope is invalid")
    if token.generation != source_generation:
        raise ChatError("reply token does not match the envelope generation")
    identity = tailnet_identity()
    if identity is None or identity.peers.get(token.node_id) != source_address:
        raise ChatError("reply token does not match the authenticated sender")


def wrapped_message(
    source_alias: str,
    source_handle: str,
    message: str,
    event_id: str,
    target_provider: str,
) -> str:
    if parse_recipient_token(source_handle) is not None:
        head = _wrapped_head_v2(source_alias, source_handle, event_id, target_provider)
    else:
        head = _wrapped_head(source_alias, source_handle, event_id, target_provider)
    body = head + message
    try:
        return bounded_message(body)
    except ChatError as error:
        # `bounded_message` rejects emptiness, NUL bytes, an unencodable string
        # and the encoded-frame budget as well as the size cap. Only the size
        # cap is worth restating, and only when the WRAPPED body is what crossed
        # it: a message comfortably under 16 KiB can still fail once the envelope
        # is added, and reporting the raw limit then tells the sender their
        # message is too long when they can see that it is not. Every other
        # reason keeps its own accurate message.
        if len(body.encode()) <= MAX_MESSAGE_BYTES:
            raise
        overhead = len(body.encode()) - len(message.encode())
        budget = MAX_MESSAGE_BYTES - overhead
        raise ChatError(
            f"message is {len(message.encode())} bytes and the Cross Agent Chat "
            f"envelope adds {overhead}, which exceeds the 16 KiB limit; "
            f"send at most {budget} bytes to this recipient"
        ) from error


def canonical_source_alias(root: Path, source: Route, *, deadline: float | None = None) -> str:
    """Return the exact currently live public alias for an authenticated sender."""
    if not _route_current(root, source):
        raise ChatError("sender route changed before transport acceptance")
    if source.provider in {"codex", "devin"}:
        return source.alias
    attempt_timeout = (
        AGENTS_TIMEOUT_SECONDS
        if deadline is None
        else _remaining_operation_timeout(deadline, AGENTS_TIMEOUT_SECONDS)
    )
    try:
        agent = exact_agent(source.session_id, source.cwd, attempt_timeout)
    except ClaudeAgentsPreflightTimeout:
        if deadline is None:
            raise
        remaining = deadline - time.monotonic()
        if remaining < SOURCE_INVENTORY_RETRY_MINIMUM_SECONDS:
            raise
        agent = exact_agent(source.session_id, source.cwd, min(AGENTS_TIMEOUT_SECONDS, remaining))
    if not _route_current(root, source):
        raise ChatError("sender route changed before transport acceptance")
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
    source_alias = canonical_source_alias(root, source, deadline=deadline)
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
    delivery_route = target_route
    if target_route.provider == "codex":
        helper = NativeHelperStore(root).helper_for_original(target_route, current_routes)
        if helper is not None:
            if not _route_current(root, helper):
                raise ChatError("native helper is unavailable")
            delivery_route = helper
    event_id = str(uuid4())
    source_token = local_token(
        root, session_key(source.provider, source.session_id), source.generation
    )
    body = wrapped_message(
        source_alias,
        source_token,
        bounded_message(message),
        event_id,
        target.provider,
    )
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
            socket_path(root, delivery_route),
            {
                "schema_version": 1,
                "operation": "accept",
                "generation": delivery_route.generation,
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
    delivery_expected = {
        "schema_version": 1,
        "event_id": event_id,
        "status": "TRANSPORT_ACCEPTED",
        "to": target.alias if delivery_route is target_route else delivery_route.alias,
        "provider": delivery_route.provider,
    }
    rejection = pre_effect_error(response, event_id, target.provider)
    if rejection is not None:
        store.mark(event_id, "PRE_EFFECT_REJECTED")
        # Local courier errors originate in a same-uid process, not a remote peer.
        raise ChatError(rejection)
    diagnostic = unknown_delivery_diagnostic(response, event_id, target.provider)
    if diagnostic is not None:
        store.mark(event_id, "UNKNOWN_DELIVERY")
        raise UnknownDeliveryError(
            f"delivery state is unknown for event {event_id}; diagnostic {diagnostic}; "
            "do not retry automatically"
        )
    if response != delivery_expected:
        store.mark(event_id, "UNKNOWN_DELIVERY")
        raise UnknownDeliveryError(
            f"delivery state is unknown for event {event_id}; do not retry automatically"
        )
    store.mark(event_id, "TRANSPORT_ACCEPTED")
    return {
        "schema_version": 1,
        "event_id": event_id,
        "status": "TRANSPORT_ACCEPTED",
        "to": target.alias,
        "provider": target.provider,
    }


def _send_local_token_target(
    root: Path,
    source: Route,
    token: RecipientToken,
    message: str,
    *,
    deadline: float,
) -> dict[str, object]:
    if token.origin != local_origin(root):
        raise ChatError(
            "recipient token was issued for a different state; "
            "call chat_peers and choose the recipient again"
        )
    matches = [
        target
        for target in local_targets(root)
        if target.session_key == token.handle and target.generation == token.generation
    ]
    if len(matches) != 1:
        raise ChatError(
            "recipient is unavailable or changed; call chat_peers and choose the recipient again"
        )
    return _send_local_target(root, source, matches[0], message, deadline=deadline)


def _send_remote_token_target(
    root: Path,
    source: Route,
    token: RecipientToken,
    message: str,
    *,
    deadline: float,
) -> dict[str, object]:
    if token.node_id is None:
        raise ChatError("recipient token is invalid")
    identity = tailnet_identity()
    if identity is None:
        raise ChatError("Tailscale is unavailable, so the selected recipient cannot be verified")
    address = identity.peers.get(token.node_id)
    if address is None:
        raise ChatError(
            "the selected recipient's device is not on the tailnet; "
            "call chat_peers and choose the recipient again"
        )
    self_node_id = identity.self_node_id
    if self_node_id is None:
        raise ChatError("local Tailscale node identity is unavailable")
    # The token asks only the node that verifiably presented its handle; an
    # unrelated node is never fanned out to and cannot hold the send hostage.
    attested, _node_complete = _remote_node_targets(
        address,
        min(time.monotonic() + REMOTE_DISCOVERY_TIMEOUT_SECONDS, deadline),
        include_devin=True,
        handle=token.handle,
        node_id=token.node_id,
    )
    claimants = [
        target
        for target in attested
        if target.session_key == token.handle and target.generation == token.generation
    ]
    if len(claimants) != 1:
        raise ChatError(
            "recipient is unavailable or changed; call chat_peers and choose the recipient again"
        )
    return _send_remote_target(
        root, source, claimants[0], message, deadline=deadline, self_node_id=self_node_id
    )


def send_local(root: Path, source: Route, target_query: str, message: str) -> dict[str, object]:
    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    token = parse_recipient_token(target_query)
    if token is not None:
        if token.scope != "local":
            raise ChatError("recipient token selects a remote device, not a local session")
        return _send_local_token_target(root, source, token, message, deadline=deadline)
    if re.fullmatch(r"[0-9a-f]{64}", target_query) is not None:
        raise ChatError(
            "recipient handles are now opaque tokens; call chat_peers and send to the fresh handle"
        )
    target = resolve_target(local_targets(root), target_query)
    return _send_local_target(root, source, target, message, deadline=deadline)


def send(root: Path, source: Route, target_query: str, message: str) -> dict[str, object]:
    deadline = time.monotonic() + OPERATION_TIMEOUT_SECONDS
    token = parse_recipient_token(target_query)
    if token is not None:
        if token.scope == "local":
            return _send_local_token_target(root, source, token, message, deadline=deadline)
        return _send_remote_token_target(root, source, token, message, deadline=deadline)
    if re.fullmatch(r"[0-9a-f]{64}", target_query) is not None:
        raise ChatError(
            "recipient handles are now opaque tokens; call chat_peers and send to the fresh handle"
        )
    local = local_targets(root)
    remote, remote_complete = _remote_discovery()
    if not remote_complete:
        raise ChatError(
            "remote peer discovery is incomplete; use an exact available recipient handle"
        )
    exact_aliases = [
        target for target in [*local, *remote] if target.alias.casefold() == target_query.casefold()
    ]
    if len(exact_aliases) == 1:
        target = exact_aliases[0]
    elif len(exact_aliases) > 1:
        raise ChatError("target is ambiguous or unavailable")
    else:
        target = resolve_target([*local, *remote], target_query)
    if not target.remote:
        return _send_local_target(root, source, target, message, deadline=deadline)
    if target.tailnet_node_id is None:
        raise ChatError("remote target route is incomplete")
    # An alias is only a display-time selector: dispatch revalidates the same
    # StableNodeID+handle+generation contract a token send does.
    return _send_remote_token_target(
        root,
        source,
        RecipientToken(
            scope="remote",
            handle=target.session_key,
            generation=target.generation,
            node_id=target.tailnet_node_id,
        ),
        message,
        deadline=deadline,
    )


def _send_remote_target(
    root: Path,
    source: Route,
    target: Target,
    message: str,
    *,
    deadline: float,
    self_node_id: str,
) -> dict[str, object]:
    if target.tailnet_address is None:
        raise ChatError("remote target route is incomplete")
    source_alias = canonical_source_alias(root, source, deadline=deadline)
    event_id = str(uuid4())
    source_token = remote_token(
        self_node_id, session_key(source.provider, source.session_id), source.generation
    )
    body = wrapped_message(
        source_alias,
        source_token,
        bounded_message(message),
        event_id,
        target.provider,
    )
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
    if response == BROKER_CAPACITY_REFUSAL:
        # The broker refused admission before reading a byte, so nothing could
        # have happened. Older brokers close instead, which stays unknown.
        store.mark(event_id, "PRE_EFFECT_REJECTED")
        raise ChatError("recipient broker is at capacity; nothing was delivered; send again")
    rejection = pre_effect_error(response, event_id, target.provider)
    if rejection is not None:
        store.mark(event_id, "PRE_EFFECT_REJECTED")
        raise ChatError(
            "remote target rejected the message before provider effect: "
            f"{rejection}; nothing was delivered"
        )
    diagnostic = unknown_delivery_diagnostic(response, event_id, target.provider)
    if diagnostic is not None:
        store.mark(event_id, "UNKNOWN_DELIVERY")
        raise UnknownDeliveryError(
            f"remote delivery state is unknown for event {event_id}; diagnostic {diagnostic}; "
            "do not retry automatically"
        )
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
        # A refusal here is decided, and it is decided before the receiver has
        # touched its provider: the receiver checks this answer before it calls
        # the delivery socket. Raising instead would close the connection with no
        # frame, the receiver would read EOF as an unknown outcome, and this
        # sender would end up recording its own refusal as UNKNOWN_DELIVERY --
        # freezing an event that provably produced no effect. Answer definitively.
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": identifier,
            "status": "REFUSED",
            "source_alias": exact_alias,
            "source_generation": exact_source_generation,
            "target_key": target_key,
            "target_generation": exact_target_generation,
            "payload_digest": payload_digest,
        }
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
    if target_provider not in {"claude", "codex", "devin"}:
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
        # The authorization callback and the provider accept draw from one
        # absolute budget: a slow answer from the sender's broker spends time
        # the accept can no longer use, and the pair can never hold the
        # receive longer than their combined configured bound.
        deadline = time.monotonic() + AUTHORIZE_TIMEOUT_SECONDS + ACCEPT_TIMEOUT_SECONDS
        authorization = request_tailnet(
            source_address,
            authorization_request,
            timeout=min(AUTHORIZE_TIMEOUT_SECONDS, deadline - time.monotonic()),
        )
        expected_authorization = {
            key: value for key, value in authorization_request.items() if key != "operation"
        }
        expected_authorization["status"] = "AUTHORIZED"
        if authorization != expected_authorization:
            raise ChatError("remote envelope is not authorized")
        # The sender's own broker authorized this exact envelope. A v2 reply
        # token inside it must bind to that same authenticated source before
        # the provider is touched; a malformed head is refused, not ignored.
        source_token = _envelope_reply_token(message, source_alias, event_id, target_provider)
        if source_token is not None:
            _verify_remote_reply_token(source_token, source_generation, source_address)
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
        delivery_route = routes[0]
        if delivery_route.provider == "codex":
            helper = NativeHelperStore(root).helper_for_original(
                delivery_route, Registry(root).routes()
            )
            if helper is not None:
                if not _route_current(root, helper):
                    raise ChatError("native helper is unavailable")
                delivery_route = helper
        response = request_socket(
            socket_path(root, delivery_route),
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "accept",
                "generation": delivery_route.generation,
                "event_id": event_id,
                "message": message,
            },
            timeout=min(ACCEPT_TIMEOUT_SECONDS, deadline - time.monotonic()),
        )
        delivery_expected: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias if delivery_route is routes[0] else delivery_route.alias,
            "provider": delivery_route.provider,
        }
        courier_rejection = pre_effect_error(response, event_id, target.provider)
        if courier_rejection is not None:
            return {
                "schema_version": SCHEMA_VERSION,
                "event_id": event_id,
                "status": "PRE_EFFECT_REJECTED",
                "provider": target.provider,
                "error": (
                    courier_rejection
                    if courier_rejection in FORWARDABLE_PRE_EFFECT_REASONS
                    else "remote destination rejected before provider effect"
                ),
            }
        if unknown_delivery_diagnostic(response, event_id, target.provider) is not None:
            return response
        if response != delivery_expected:
            raise UnknownDeliveryError("remote delivery state is unknown")
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id,
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": target.provider,
        }
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


def _target_handle_token(root: Path, target: Target) -> str | None:
    """Mint the public endpoint token for one discovered target, or none.

    A remote peer can only be offered when the local Tailscale read verified
    the stable node identity that presented it; without that identity the peer
    is not selectable and is left out of the public listing.
    """
    if target.remote:
        if target.tailnet_node_id is None:
            return None
        return remote_token(target.tailnet_node_id, target.session_key, target.generation)
    return local_token(root, target.session_key, target.generation)


def peers(
    root: Path,
    *,
    include_remote: bool = True,
    internal: bool = False,
    include_delivery_mode: bool = False,
    include_delivery_mechanism: bool = False,
    include_title: bool = False,
    include_devin: bool = True,
    handle: str | None = None,
) -> dict[str, object]:
    display_titles = not internal or include_title
    deadline = time.monotonic() + (
        REMOTE_DISCOVERY_TIMEOUT_SECONDS if include_remote else LOCAL_DISCOVERY_TIMEOUT_SECONDS
    )
    if include_remote:
        identity = tailnet_identity()
        with ThreadPoolExecutor(max_workers=2) as workers:
            local = workers.submit(local_targets, root, handle=handle)
            remote = workers.submit(
                _remote_discovery,
                include_delivery_mode=include_delivery_mode,
                include_title=display_titles,
                identity=identity,
            )
            remote_targets, remote_complete = remote.result()
            targets = [*local.result(), *remote_targets]
        # Without a verified local StableNodeID this node cannot mint the source
        # reply token a remote send needs, so remote rows are unselectable.
        if not internal and (identity is None or identity.self_node_id is None):
            targets = [target for target in targets if not target.remote]
        remote_discovery = "complete" if remote_complete else "incomplete"
    else:
        targets = local_targets(root, handle=handle)
        remote_discovery = "not_requested"
    if not include_devin:
        targets = [target for target in targets if target.provider != "devin"]
    handles = [target.session_key for target in targets]
    if len(set(handles)) != len(handles):
        raise ChatError("peer discovery returned duplicate handles")
    targets = sorted(targets, key=lambda target: (target.alias.casefold(), target.session_key))
    if display_titles:
        targets = _with_codex_titles(root, targets, deadline)
    items: list[dict[str, str]] = []
    for target in targets:
        token = None if internal else _target_handle_token(root, target)
        if not internal and token is None:
            continue
        item = target.public(
            include_delivery_mode=include_delivery_mode,
            include_delivery_mechanism=include_delivery_mechanism,
            include_handle=not internal,
            handle=token,
            include_title=not internal or include_title,
        )
        if internal:
            item["generation"] = target.generation
            item["session_key"] = target.session_key
        items.append(item)
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "peers": items,
    }
    if not internal:
        result["remote_discovery"] = remote_discovery
    return result


def sender_readiness(
    root: Path, provider: str, parent_pid: int, thread_id: str | None
) -> dict[str, str]:
    """Report whether this MCP caller can pass the existing send identity gate."""
    try:
        source = authenticate_mcp_sender(root, provider, parent_pid, thread_id)
    except ChatError as error:
        return {"status": "unavailable", "reason": str(error)}
    return sender_readiness_for_route(root, source)


def sender_readiness_for_route(root: Path, source: Route) -> dict[str, str]:
    """Check one already-authenticated source route without PID re-resolution."""

    if not _route_current(root, source):
        return {"status": "unavailable", "reason": "registered sender route is not current"}
    try:
        response = request_socket(
            socket_path(root, source),
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "health",
                "generation": source.generation,
            },
            timeout=HEALTH_TIMEOUT_SECONDS,
        )
    except ChatError:
        return {"status": "unavailable", "reason": "sender courier is unavailable"}
    if response.get("status") != "READY" or response.get("generation") != source.generation:
        return {"status": "unavailable", "reason": "sender courier is not ready"}
    return {"status": "ready"}


ReplyDelivery = Literal["while_idle", "next_turn", "unknown"]
# The caller asks before sending, so this bound adds at most this much before the send's own budget.
REPLY_DELIVERY_TIMEOUT_SECONDS: Final = 1.0


def reply_delivery(root: Path, source: Route) -> ReplyDelivery:
    """Say how an answer sent to this authenticated source session reaches it.

    Claude and a queued Codex route receive a new message while idle. A Stop-bound
    Codex route or a Devin route is handed a queued message only at its next turn
    boundary: the end of the current turn if it has already arrived, otherwise the
    user's next prompt. An unreachable, busy, or unrecognized courier reports unknown.
    Callers ask before sending: a slow or failing check must never turn an accepted
    send into an apparent failure that invites a resend.
    """
    if source.provider == "claude":
        mode: str | None = "claude_native_cross_session"
    elif source.provider == "devin":
        mode = "devin_stop_or_prompt_bound"
    else:
        try:
            response = request_socket(
                socket_path(root, source),
                {
                    "schema_version": SCHEMA_VERSION,
                    "operation": "health",
                    "generation": source.generation,
                    "include_delivery_mode": True,
                },
                timeout=REPLY_DELIVERY_TIMEOUT_SECONDS,
            )
        except (ChatError, OSError):
            return "unknown"
        raw = response.get("delivery_mode")
        mode = (
            raw
            if response.get("status") == "READY"
            and response.get("generation") == source.generation
            and isinstance(raw, str)
            else None
        )
    if mode in {"claude_native_cross_session", "codex_experimental_queue"}:
        return "while_idle"
    if mode in {"codex_stop_bound", "devin_stop_or_prompt_bound"}:
        return "next_turn"
    return "unknown"


def native_helper_tools(root: Path, source: Route) -> tuple[str, ...]:
    """Return lifecycle-only tools for an exact eligible Codex app route."""

    if (
        source.provider != "codex"
        or source.profile_root is None
        or not _route_current(root, source)
    ):
        return ()
    store = NativeHelperStore(root)
    if store.pending_for_helper(source):
        return ("native_register",)
    try:
        store.original_for_helper(source, Registry(root).routes())
    except ChatError:
        pass
    else:
        return ("native_dispatch",)
    original_bindings = [
        item
        for item in store.bindings()
        if (
            item.original_session_id == source.session_id
            and item.original_generation == source.generation
        )
    ]
    if original_bindings and any(item.state == "UNKNOWN" for item in original_bindings):
        return ()
    if original_bindings and store.helper_for_original(source, Registry(root).routes()) is None:
        return ("native_bootstrap",)
    if store.needs_bootstrap(source):
        return ("native_bootstrap",)
    return ()


def native_startup(root: Path, device: str, pid: int) -> dict[str, object]:
    """Offer one original Desktop task the internal bootstrap tool on its first prompt."""

    raw = hook_input("UserPromptSubmit")
    candidates = [
        route
        for route in Registry(root).routes()
        if route.provider == "codex"
        and route.device == valid_device(device)
        and route.session_id == raw["session_id"]
        and route.pid == pid
        and route.cwd == raw["cwd"]
    ]
    if len(candidates) != 1 or not _route_current(root, candidates[0]):
        return {}
    if not _native_desktop_route(candidates[0]):
        return {}
    return native_bootstrap_context(root, candidates[0], "UserPromptSubmit")


def native_bootstrap_context(root: Path, source: Route, event_name: str) -> dict[str, object]:
    """Emit the provider-shaped one-time bootstrap instruction for an eligible Desktop route."""

    if (
        not _native_desktop_route(source)
        or event_name not in {"SessionStart", "UserPromptSubmit"}
        or native_helper_tools(root, source) != ("native_bootstrap",)
        or not _native_hook_ready(source, _native_create_hook_group())
    ):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": (
                "Cross Agent Chat needs its native delivery helper. Call native_bootstrap once now."
            ),
        }
    }


def _native_desktop_route(route: Route) -> bool:
    """Recognize a route owned by the bundled Native Codex process."""

    try:
        identity, _ = recipient_owner_identity("codex", route.pid, route.profile_root)
    except (ChatError, OSError):
        return False
    return (
        route.provider == "codex"
        and identity == route.owner_identity
        and native_desktop_bundle(route.pid) is not None
    )


def _native_bundle_root(executable: Path, inner: tuple[str, str]) -> Path | None:
    """Return the ChatGPT.app root containing one bundled executable path."""

    parents = executable.parents
    if (
        len(parents) >= 3
        and parents[2].name == "ChatGPT.app"
        and parents[1].name == "Contents"
        and parents[0].name == inner[0]
        and executable.name == inner[1]
    ):
        return parents[2]
    return None


def _supported_native_bundle(bundle: Path) -> Path | None:
    """Resolve one ChatGPT.app root only inside a supported install location."""

    try:
        resolved = bundle.resolve(strict=True)
    except OSError:
        return None
    applications = NATIVE_DESKTOP_APPLICATIONS.resolve()
    if resolved.name != "ChatGPT.app" or (
        resolved.parent != applications
        and resolved.parent.parent != applications
        and resolved.parent != (Path.home() / "Applications").resolve()
    ):
        return None
    try:
        info = plistlib.loads((resolved / "Contents" / "Info.plist").read_bytes())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(info, dict)
        or info.get("CFBundleIdentifier") not in NATIVE_DESKTOP_BUNDLE_IDS
    ):
        return None
    return resolved


def native_desktop_bundle(pid: int) -> Path | None:
    """Resolve the supported ChatGPT.app bundle owning one exact Codex child."""

    if pid <= 0:
        return None
    try:
        _, executable = recipient_owner_identity("codex", pid)
    except (ChatError, OSError):
        return None
    bundle = _native_bundle_root(executable, ("Resources", "codex"))
    resolved = _supported_native_bundle(bundle) if bundle is not None else None
    if resolved is None or not native_desktop_process(pid, resolved):
        return None
    return resolved


def native_desktop_process(pid: int, bundle: Path | None = None) -> bool:
    """Recognize an app-owned Codex child by its bounded Desktop ancestor chain."""

    if pid <= 0:
        return False
    for _ in range(8):
        try:
            _, executable = recipient_owner_identity("codex", pid)
            parent = subprocess.run(
                ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
        except (ChatError, OSError, subprocess.SubprocessError):
            return False
        ancestor = _native_bundle_root(executable, ("MacOS", "ChatGPT"))
        found = _supported_native_bundle(ancestor) if ancestor is not None else None
        if found is not None and (bundle is None or found == bundle):
            return True
        raw_parent = parent.stdout.strip()
        if parent.returncode != 0 or not raw_parent.isdigit() or int(raw_parent) == pid:
            return False
        pid = int(raw_parent)
    return False


def native_desktop_mcp_host() -> bool:
    """Recognize a Desktop-hosted MCP process without trusting a model field."""

    return native_desktop_process(os.getppid())


def _native_account_binary(route: Route) -> Path:
    """Select only the bundled app-server client of the bound Desktop process."""

    if route.profile_root is None:
        raise ChatError("Codex account identity is unavailable")
    try:
        identity, _ = recipient_owner_identity("codex", route.pid, route.profile_root)
    except (ChatError, OSError) as error:
        raise ChatError("Codex account identity is unavailable") from error
    bundle = native_desktop_bundle(route.pid)
    if identity != route.owner_identity or bundle is None:
        raise ChatError("Codex account identity is unavailable")
    try:
        return (bundle / "Contents" / "Resources" / "codex").resolve(strict=True)
    except OSError as error:
        raise ChatError("Codex account identity is unavailable") from error


def _native_account_digest(route: Route) -> str:
    """Bind lifecycle operations to the selected provider account without refresh."""

    assert route.profile_root is not None
    return native_account_digest(
        binary=_native_account_binary(route), environment={"CODEX_HOME": route.profile_root}
    )


def _native_hook_hash(event_name: str, group: dict[str, object]) -> str:
    """Match Codex's trusted-hook identity for one exact group."""

    identity = {"event_name": event_name, **group}
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _native_create_hook_group() -> dict[str, object]:
    return native_helper_create_hook_group()


def _native_dispatch_hook_group() -> dict[str, object]:
    return native_helper_dispatch_hook_group()


def _native_hook_ready(source: Route, expected: dict[str, object]) -> bool:
    """Require the exact enabled hook and the provider-written trust hash."""

    if source.profile_root is None:
        return False
    profile = Path(source.profile_root)
    hooks_path = profile / "hooks.json"
    config_path = profile / "config.toml"
    try:
        hooks_raw = json.loads(hooks_path.read_text(encoding="utf-8"))
        config_raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(hooks_raw, dict) or not isinstance(config_raw, dict):
        return False
    features = config_raw.get("features")
    state_root = config_raw.get("hooks")
    if (
        not isinstance(features, dict)
        or features.get("hooks") is not True
        or not isinstance(state_root, dict)
        or not isinstance(state_root.get("state"), dict)
    ):
        return False
    hooks = hooks_raw.get("hooks")
    if not isinstance(hooks, dict):
        return False
    groups = hooks.get("PostToolUse")
    if not isinstance(groups, list):
        return False
    matches = [index for index, group in enumerate(groups) if group == expected]
    if len(matches) != 1:
        return False
    key = f"{hooks_path.resolve(strict=False)}:post_tool_use:{matches[0]}:0"
    trusted = state_root["state"].get(key)
    return isinstance(trusted, dict) and trusted.get("trusted_hash") == _native_hook_hash(
        "post_tool_use", expected
    )


def native_bootstrap(root: Path, source: Route) -> dict[str, object]:
    """Reserve one helper create and return only trusted-hook template arguments."""

    if "native_bootstrap" not in native_helper_tools(root, source) or not _native_hook_ready(
        source, _native_create_hook_group()
    ):
        raise ChatError("native helper bootstrap is unavailable")
    binding, nonce = NativeHelperStore(root).reserve(
        source, _native_account_digest(source), Registry(root).routes()
    )
    return {
        "content": [{"type": "text", "text": "Native delivery helper setup was submitted."}],
        "_meta": {
            "create_thread": {
                "prompt": (
                    "You are the Cross Agent Chat native delivery helper. "
                    f"Call native_register once with token {nonce}, then wait for inbound work. "
                    "Do not inspect memory, source files, or unrelated tasks."
                ),
                "target": {"type": "projectless", "directoryName": binding.helper_directory},
                "model": NATIVE_HELPER_MODEL,
                "thinking": "high",
                "title": "Cross Agent Chat helper",
            }
        },
    }


def native_register(root: Path, helper: Route, token: str) -> dict[str, object]:
    """Bind one app-created helper to its exact current original route."""

    store = NativeHelperStore(root)
    original = store.original_for_nonce(token, Registry(root).routes())
    if not _route_current(root, original) or not _route_current(root, helper):
        raise ChatError("native helper registration is unavailable")
    store.register(helper, token, original, _native_account_digest(helper))
    return {"content": [{"type": "text", "text": "Native helper is ready."}]}


def native_dispatch(root: Path, helper: Route, event_id: str) -> dict[str, object]:
    """Claim one helper-originated native send to its bound original task."""

    identifier = valid_uuid(event_id, "event id")
    if not _native_hook_ready(helper, _native_dispatch_hook_group()):
        raise ChatError("native helper dispatch is unavailable")
    store = NativeHelperStore(root)
    original = store.original_for_helper(helper, Registry(root).routes())
    if not _route_current(root, helper) or not _route_current(root, original):
        raise ChatError("native helper dispatch is unavailable")
    bound_account = next(
        item.account_sha256
        for item in store.bindings()
        if (
            item.helper_session_id == helper.session_id
            and item.helper_generation == helper.generation
        )
    )
    if (
        _native_account_digest(helper) != bound_account
        or _native_account_digest(original) != bound_account
    ):
        raise ChatError("native helper dispatch is unavailable")
    dispatch_store = NativeDispatchStore(root)
    if dispatch_store.has_event(identifier):
        raise ChatError("native helper dispatch is unavailable")
    response = request_socket(
        socket_path(root, helper),
        {
            "schema_version": SCHEMA_VERSION,
            "operation": "native_dispatch",
            "generation": helper.generation,
            "event_id": identifier,
        },
    )
    if (
        set(response) != {"schema_version", "status", "generation", "event_id", "message"}
        or response.get("schema_version") != SCHEMA_VERSION
        or response.get("status") != "NATIVE_DISPATCH"
        or response.get("generation") != helper.generation
        or response.get("event_id") != identifier
        or not isinstance(response.get("message"), str)
    ):
        raise ChatError("native helper dispatch is unavailable")
    body = bounded_message(cast(str, response["message"]))
    dispatch_store.claim(helper, original, identifier, body)
    acknowledgement = request_socket(
        socket_path(root, helper),
        {
            "schema_version": SCHEMA_VERSION,
            "operation": "native_dispatch_ack",
            "generation": helper.generation,
            "event_id": identifier,
        },
    )
    if acknowledgement != {
        "schema_version": SCHEMA_VERSION,
        "status": "NATIVE_DISPATCH_ACKED",
        "generation": helper.generation,
        "event_id": identifier,
    }:
        raise ChatError("native helper dispatch is unavailable")
    return {
        "content": [{"type": "text", "text": "Native delivery was submitted."}],
        "_meta": {"native_args": {"threadId": original.session_id, "prompt": body}},
    }


def event_status(root: Path, source: Route, event_id: str) -> dict[str, object]:
    """Return body-free custody state for one exact authenticated source event."""
    if not _route_current(root, source):
        raise ChatError("event is unavailable")
    intent = IntentStore(root).intent_for_source(
        event_id=event_id,
        source_key=session_key(source.provider, source.session_id),
        source_generation=source.generation,
    )
    if intent is None:
        raise ChatError("event is unavailable")
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": intent.event_id,
        "status": intent.status,
        "source_alias": intent.source_alias,
        "target_handle": intent.target_key,
        "target_generation": intent.target_generation,
        "timestamp": intent.timestamp,
        "delivery_observation": "not_observed",
    }


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
    if NativeHelperStore(root).is_helper_lineage(route):
        print("{}", flush=True)
        return
    try:
        health = request_socket(
            socket_path(root, route),
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "health",
                "generation": route.generation,
                "include_delivery_mode": True,
                "include_direct_delivery_mode": True,
            },
        )
    except ChatError:
        print("{}", flush=True)
        return
    base_health = {
        "schema_version": SCHEMA_VERSION,
        "status": "READY",
        "generation": route.generation,
        "alias": route.alias,
    }
    allowed = {
        frozenset(base_health),
        frozenset((*base_health, "delivery_mode")),
        frozenset((*base_health, "delivery_mode", "direct_delivery_mode")),
    }
    if frozenset(health) not in allowed or any(
        health.get(key) != value for key, value in base_health.items()
    ):
        print("{}", flush=True)
        return
    direct_mode = health.get("direct_delivery_mode", health.get("delivery_mode"))
    if direct_mode != "codex_stop_bound":
        print("{}", flush=True)
        return
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

    try:
        acknowledged = request_socket(
            socket_path(root, route),
            {
                "schema_version": 1,
                "operation": "ack",
                "generation": route.generation,
                "event_ids": [item["event_id"] for item in messages],
            },
        )
    except ChatError:
        print("{}", flush=True)
        return
    if acknowledged != {
        "schema_version": SCHEMA_VERSION,
        "status": "ACKNOWLEDGED",
        "event_ids": [item["event_id"] for item in messages],
    }:
        print("{}", flush=True)
        return
    print(
        json.dumps(
            {"decision": "block", "reason": hook_context(messages)},
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        flush=True,
    )


def authenticate_mcp_sender(
    root: Path, provider: str, parent_pid: int, thread_id: str | None
) -> Route:
    return authenticate_sender(Registry(root).routes(), provider, parent_pid, thread_id)
