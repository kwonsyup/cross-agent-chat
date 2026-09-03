"""Local broker reachable by Tailnet nodes permitted by the user's ACL policy."""

from __future__ import annotations

import errno
import json
import os
import select
import socket
import threading
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from cross_agent_chat import __version__
from cross_agent_chat.core import SCHEMA_VERSION, ChatError
from cross_agent_chat.runtime import (
    authorize_remote,
    emit_frame_safely,
    peers,
    read_frame,
    receive_remote,
    state_root,
)
from cross_agent_chat.tailnet import (
    LOCAL_BROKER_HOST,
    LOCAL_BROKER_PORT,
    TAILNET_PORT,
    local_tailnet_address,
    valid_tailnet_address,
)

MAX_BROKER_CONNECTIONS = 16
MAX_BROKER_CONNECTIONS_PER_PEER = 2
TAILNET_BIND_RETRY_SECONDS = 5.0


@dataclass(slots=True)
class BrokerAdmission:
    """Bound global and per-peer work before executor submission."""

    capacity: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(MAX_BROKER_CONNECTIONS)
    )
    lock: threading.Lock = field(default_factory=threading.Lock)
    by_peer: dict[str, int] = field(default_factory=dict)

    def acquire(self, peer_address: str) -> bool:
        if not self.capacity.acquire(blocking=False):
            return False
        with self.lock:
            active = self.by_peer.get(peer_address, 0)
            if active >= MAX_BROKER_CONNECTIONS_PER_PEER:
                self.capacity.release()
                return False
            self.by_peer[peer_address] = active + 1
        return True

    def release(self, peer_address: str) -> None:
        with self.lock:
            active = self.by_peer.get(peer_address, 0)
            if active <= 1:
                self.by_peer.pop(peer_address, None)
            else:
                self.by_peer[peer_address] = active - 1
        self.capacity.release()


def broker_bindings() -> list[tuple[str, int]]:
    """Return the exact interfaces owned by this broker process."""
    bindings = [(LOCAL_BROKER_HOST, LOCAL_BROKER_PORT)]
    configured = os.environ.get("CROSS_AGENT_CHAT_TAILNET_ADDRESS")
    tailnet_address = (
        valid_tailnet_address(configured) if configured is not None else local_tailnet_address()
    )
    if tailnet_address is not None:
        bindings.append((tailnet_address, TAILNET_PORT))
    return bindings


def bind_broker_listener(
    binding: tuple[str, int], *, allow_unavailable: bool = False
) -> socket.socket | None:
    """Bind one listener, deferring only a Tailnet address that is not ready yet."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(binding)
        server.listen(16)
        server.setblocking(False)
    except OSError as error:
        server.close()
        if allow_unavailable and error.errno == errno.EADDRNOTAVAIL:
            return None
        raise
    return server


def handle_broker_request(root: Path, raw: object, peer_address: str) -> dict[str, object]:
    """Handle one validated Tailnet broker request without recursive discovery."""
    if not isinstance(raw, dict):
        raise ChatError("Tailnet broker request is invalid")
    request = cast(dict[object, object], raw)
    operation = request.get("operation")
    if operation == "health" and set(request) == {"schema_version", "operation"}:
        if request.get("schema_version") != SCHEMA_VERSION or peer_address != LOCAL_BROKER_HOST:
            raise ChatError("Tailnet broker request is invalid")
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "READY",
            "pid": os.getpid(),
            "version": __version__,
            "module_path": str(Path(__file__).resolve()),
        }
    if operation == "peers" and set(request) == {"schema_version", "operation"}:
        if request.get("schema_version") != SCHEMA_VERSION:
            raise ChatError("Tailnet broker request is invalid")
        valid_tailnet_address(peer_address)
        return peers(root, include_remote=False, internal=True)
    authorization_fields = {
        "schema_version",
        "operation",
        "event_id",
        "source_alias",
        "source_generation",
        "target_key",
        "target_generation",
        "payload_digest",
    }
    if operation == "authorize" and set(request) == authorization_fields:
        valid_tailnet_address(peer_address)
        values = cast(dict[str, object], request)
        string_fields = authorization_fields - {"schema_version", "operation"}
        if request.get("schema_version") != SCHEMA_VERSION or not all(
            isinstance(values[field], str) for field in string_fields
        ):
            raise ChatError("Tailnet broker request is invalid")
        return authorize_remote(
            root,
            event_id=cast(str, values["event_id"]),
            source_alias=cast(str, values["source_alias"]),
            source_generation=cast(str, values["source_generation"]),
            target_key=cast(str, values["target_key"]),
            target_generation=cast(str, values["target_generation"]),
            payload_digest=cast(str, values["payload_digest"]),
        )
    if operation == "receive" and set(request) == {
        "schema_version",
        "operation",
        "envelope",
    }:
        envelope = request.get("envelope")
        if request.get("schema_version") != SCHEMA_VERSION or not isinstance(envelope, str):
            raise ChatError("Tailnet broker request is invalid")
        return receive_remote(root, envelope, valid_tailnet_address(peer_address))
    raise ChatError("Tailnet broker request is invalid")


def serve_broker_connection(root: Path, connection: socket.socket, peer_address: str) -> None:
    """Serve one bounded request on an accepted localhost connection."""
    connection.settimeout(5.0)
    try:
        raw: object = json.loads(read_frame(connection))
    except json.JSONDecodeError as error:
        raise ChatError("Tailnet broker request is invalid") from error
    emit_frame_safely(connection, handle_broker_request(root, raw, peer_address))


def _serve_and_close(
    root: Path,
    connection: socket.socket,
    peer_address: str,
    admission: BrokerAdmission,
) -> None:
    with connection:
        try:
            serve_broker_connection(root, connection, peer_address)
        except (ChatError, OSError):
            return
        finally:
            admission.release(peer_address)


def dispatch_broker_connection(
    workers: Executor,
    root: Path,
    connection: socket.socket,
    peer_address: str,
    admission: BrokerAdmission,
) -> bool:
    """Give each accepted connection an independent bounded worker."""
    if not admission.acquire(peer_address):
        connection.close()
        return False
    workers.submit(_serve_and_close, root, connection, peer_address, admission)
    return True


def dispatch_ready_brokers(
    workers: Executor,
    root: Path,
    readable: list[socket.socket],
    admission: BrokerAdmission,
) -> int:
    """Accept each ready listener without letting one vanished connection stall the broker."""
    dispatched = 0
    for server in readable:
        try:
            connection, peer = server.accept()
        except (BlockingIOError, ConnectionAbortedError, InterruptedError):
            continue
        if dispatch_broker_connection(
            workers,
            root,
            connection,
            cast(tuple[str, int], peer)[0],
            admission,
        ):
            dispatched += 1
    return dispatched


def broker_server(state_root_value: str | None) -> None:
    """Serve localhost and, when available, this Mac's private Tailnet address."""
    root = state_root(state_root_value)
    servers: list[socket.socket] = []
    try:
        local_server = bind_broker_listener((LOCAL_BROKER_HOST, LOCAL_BROKER_PORT))
        if local_server is None:  # Defensive: the required local bind never permits deferral.
            raise RuntimeError("local broker listener could not be created")
        servers.append(local_server)
        tailnet_binding: tuple[str, int] | None = None
        tailnet_server: socket.socket | None = None
        admission = BrokerAdmission()
        with ThreadPoolExecutor(
            max_workers=MAX_BROKER_CONNECTIONS,
            thread_name_prefix="cross-agent-chat",
        ) as workers:
            while True:
                if tailnet_server is None:
                    current_bindings = broker_bindings()
                    tailnet_binding = current_bindings[1] if len(current_bindings) == 2 else None
                if tailnet_binding is not None and tailnet_server is None:
                    tailnet_server = bind_broker_listener(tailnet_binding, allow_unavailable=True)
                    if tailnet_server is not None:
                        servers.append(tailnet_server)
                retry_timeout = TAILNET_BIND_RETRY_SECONDS if tailnet_server is None else None
                readable, _, _ = select.select(servers, [], [], retry_timeout)
                dispatch_ready_brokers(workers, root, readable, admission)
    finally:
        for server in servers:
            server.close()
