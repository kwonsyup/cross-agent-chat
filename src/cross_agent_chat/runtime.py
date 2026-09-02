"""Provider process registration, Codex courier, discovery, and delivery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

from cross_agent_chat.claude_runtime import (
    AGENTS_TIMEOUT_SECONDS,
    COURIER_ENV_KEYS,
    DISCOVERY_TIMEOUT_SECONDS,
    SEND_TIMEOUT_SECONDS,
    claude_alias,
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
    valid_device,
    valid_name,
    valid_uuid,
)
from cross_agent_chat.remote import parse_remote_envelope
from cross_agent_chat.tailnet import TAILNET_PORT, tailnet_nodes, valid_tailnet_address
from cross_agent_chat.transport import remote_envelope

MAX_FRAME_BYTES: Final = 64 * 1024
SOCKET_TIMEOUT_SECONDS: Final = 5.0
HEALTH_TIMEOUT_SECONDS: Final = AGENTS_TIMEOUT_SECONDS + 2.0
REMOTE_DISCOVERY_TIMEOUT_SECONDS: Final = HEALTH_TIMEOUT_SECONDS + 5.0
ACCEPT_TIMEOUT_SECONDS: Final = (
    2 * AGENTS_TIMEOUT_SECONDS + DISCOVERY_TIMEOUT_SECONDS + SEND_TIMEOUT_SECONDS + 5.0
)
AUTHORIZE_TIMEOUT_SECONDS: Final = 20.0
COURIER_READY_SECONDS: Final = 3.0
REMOTE_TIMEOUT_SECONDS: Final = (
    HEALTH_TIMEOUT_SECONDS + AUTHORIZE_TIMEOUT_SECONDS + ACCEPT_TIMEOUT_SECONDS + 5.0
)
LOCAL_DISCOVERY_WORKERS: Final = 32
PRESENCE_ENV_VAR: Final = "CROSS_AGENT_CHAT_PRESENCE"


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

    def public(self) -> dict[str, str]:
        return {
            "alias": self.alias,
            "provider": self.provider,
            "device": self.device,
            "project": self.project,
            "status": "available",
        }


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
    connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())


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
    try:
        require_socket(path)
        client.connect(str(path))
        emit_frame(client, payload)
        client.shutdown(socket.SHUT_WR)
        raw = json.loads(read_frame(client))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ChatError) as error:
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
    try:
        client = socket.create_connection((address, port), timeout=timeout)
        client.settimeout(timeout)
        with client:
            emit_frame(client, payload)
            raw: object = json.loads(read_frame(client))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ChatError) as error:
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


def _spawn_courier(root: Path, route: Route) -> None:
    path = socket_path(root, route)
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
    environment = (
        courier_environment()
        if route.provider == "claude"
        else {key: os.environ[key] for key in COURIER_ENV_KEYS if key in os.environ}
    )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        raise ChatError("session courier could not start") from error
    deadline = time.monotonic() + COURIER_READY_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ChatError("session courier exited before becoming ready")
        try:
            remaining = max(0.1, deadline - time.monotonic())
            response = request_socket(
                path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "operation": "health",
                    "generation": route.generation,
                },
                timeout=remaining,
            )
            if response.get("status") == "READY":
                return
            time.sleep(0.02)
        except UnknownDeliveryError:
            time.sleep(0.02)
    process.terminate()
    raise ChatError("session courier did not become ready")


def register(provider: str, device: str, pid: int, state_root_value: str | None) -> Route | None:
    if not presence_is_enabled():
        return None
    if provider not in {"claude", "codex"}:
        raise ChatError("provider is invalid")
    if isinstance(pid, bool) or pid <= 0:
        raise ChatError("provider process is invalid")
    raw = hook_input("SessionStart")
    route = Route.create(
        provider=provider,
        session_id=cast(str, raw["session_id"]),
        device=valid_device(device),
        cwd=cast(str, raw["cwd"]),
        pid=pid,
    )
    root = state_root(state_root_value)
    registry = Registry(root)
    registry.compact_dead()
    registry.upsert(route)
    try:
        _spawn_courier(root, route)
    except ChatError:
        Registry(root).remove(route.provider, route.session_id, route.pid)
        raise
    return route


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
    Registry(root).remove(route.provider, route.session_id, route.pid)
    try:
        request_socket(
            socket_path(root, route),
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "shutdown",
                "generation": route.generation,
            },
        )
    except UnknownDeliveryError:
        return


def _route_current(root: Path, expected: Route) -> bool:
    return Registry(root).current(expected) and expected.process_is_live()


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


def courier_health(route: Route) -> dict[str, object]:
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
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "READY",
        "generation": route.generation,
        "alias": alias,
    }


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
    courier = (
        CodexCourier(alias=route.alias, generation=route.generation)
        if provider == "codex"
        else None
    )
    bound = path.lstat()
    path.chmod(0o600)
    server.listen(4)
    server.settimeout(1.0)
    stopping = False
    try:
        while not stopping and _route_current(root, route):
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(SOCKET_TIMEOUT_SECONDS)
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
                if operation == "health":
                    emit_frame_safely(connection, courier_health(route))
                elif operation == "shutdown":
                    emit_frame_safely(connection, {"schema_version": 1, "status": "STOPPED"})
                    stopping = True
                elif operation == "accept":
                    event_id = request.get("event_id")
                    message = request.get("message")
                    if not isinstance(event_id, str) or not isinstance(message, str):
                        continue
                    emit_frame_safely(connection, courier_accept(route, courier, event_id, message))
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


def _local_target(root: Path, route: Route) -> Target | None:
    try:
        response = request_socket(
            socket_path(root, route),
            {"schema_version": 1, "operation": "health", "generation": route.generation},
            timeout=HEALTH_TIMEOUT_SECONDS,
        )
    except UnknownDeliveryError:
        return None
    alias = response.get("alias")
    if (
        set(response) != {"schema_version", "status", "generation", "alias"}
        or response.get("schema_version") != SCHEMA_VERSION
        or response.get("status") != "READY"
        or response.get("generation") != route.generation
        or not isinstance(alias, str)
    ):
        return None
    if route.provider == "codex":
        if alias != route.alias:
            return None
    elif not alias.startswith(f"claude@{route.device}:{route.project}:"):
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
        )
    except ChatError:
        return None


def local_targets(root: Path) -> list[Target]:
    routes = [route for route in Registry(root).routes() if route.process_is_live()]
    if not routes:
        return []
    with ThreadPoolExecutor(max_workers=min(LOCAL_DISCOVERY_WORKERS, len(routes))) as workers:
        targets = workers.map(lambda route: _local_target(root, route), routes)
        return [target for target in targets if target is not None]


def _targets_from_tailnet(address: str, raw: object) -> list[Target]:
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
        if not isinstance(raw_item, dict) or set(raw_item) != required:
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
        ):
            raise ChatError("Tailnet peer returned invalid discovery")
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
            )
        )
    return targets


def _remote_node_targets(address: str) -> list[Target]:
    try:
        raw = request_tailnet(
            address,
            {"schema_version": SCHEMA_VERSION, "operation": "peers"},
            # The remote broker may spend HEALTH_TIMEOUT_SECONDS validating local routes.
            timeout=REMOTE_DISCOVERY_TIMEOUT_SECONDS,
        )
        return _targets_from_tailnet(address, raw)
    except (ChatError, UnknownDeliveryError):
        return []


def remote_targets(_root: Path) -> list[Target]:
    addresses = tailnet_nodes()
    if not addresses:
        return []
    targets: list[Target] = []
    with ThreadPoolExecutor(max_workers=min(16, len(addresses))) as workers:
        results = workers.map(_remote_node_targets, addresses)
        for result in results:
            targets.extend(result)
    return targets


def all_targets(root: Path, *, include_remote: bool = True) -> list[Target]:
    if include_remote:
        with ThreadPoolExecutor(max_workers=2) as workers:
            local = workers.submit(local_targets, root)
            remote = workers.submit(remote_targets, root)
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
    wanted = [token for token in re.split(r"[^A-Za-z0-9]+", query.casefold()) if token]
    available = {token for token in re.split(r"[^A-Za-z0-9]+", target.alias.casefold()) if token}
    return bool(wanted) and all(token in available for token in wanted)


def resolve_target(targets: list[Target], query: str) -> Target:
    if not query.strip() or len(query) > 160:
        raise ChatError("target query is invalid")
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
    if not Registry(root).current(source) or not source.process_is_live():
        raise ChatError("sender route changed before transport acceptance")
    if source.provider == "codex":
        return source.alias
    agent = exact_agent(source.session_id, source.cwd)
    return claude_alias(source.device, source.project, agent)


def send_local(root: Path, source: Route, target_query: str, message: str) -> dict[str, object]:
    targets = local_targets(root)
    target = resolve_target(targets, target_query)
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
            timeout=ACCEPT_TIMEOUT_SECONDS,
        )
    except UnknownDeliveryError:
        store.mark(event_id, "UNKNOWN_DELIVERY")
        raise UnknownDeliveryError(
            f"delivery state is unknown for event {event_id}; do not retry automatically"
        ) from None
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


def send(root: Path, source: Route, target_query: str, message: str) -> dict[str, object]:
    target = resolve_target(all_targets(root), target_query)
    if not target.remote:
        return send_local(root, source, target.alias, message)
    if target.tailnet_address is None:
        raise ChatError("remote target route is incomplete")
    source_alias = canonical_source_alias(root, source)
    event_id = str(uuid4())
    body = wrapped_message(source_alias, bounded_message(message), event_id)
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
            timeout=REMOTE_TIMEOUT_SECONDS,
        )
    except UnknownDeliveryError as error:
        store.mark(event_id, "UNKNOWN_DELIVERY")
        raise UnknownDeliveryError(
            f"remote delivery state is unknown for event {event_id}; do not retry automatically"
        ) from error
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


def peers(root: Path, *, include_remote: bool = True, internal: bool = False) -> dict[str, object]:
    targets = all_targets(root, include_remote=include_remote)
    items: list[dict[str, str]] = []
    for target in targets:
        item = target.public()
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
        print(json.dumps(payload, separators=(",", ":")), flush=True)

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
    except UnknownDeliveryError:
        return


def authenticate_mcp_sender(
    root: Path, provider: str, parent_pid: int, thread_id: str | None
) -> Route:
    return authenticate_sender(Registry(root).routes(), provider, parent_pid, thread_id)
