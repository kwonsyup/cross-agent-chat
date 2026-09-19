"""Local broker reachable by Tailnet nodes permitted by the user's ACL policy."""

from __future__ import annotations

import errno
import json
import os
import select
import socket
import threading
import time
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from cross_agent_chat import __version__
from cross_agent_chat.core import SCHEMA_VERSION, ChatError
from cross_agent_chat.runtime import (
    BROKER_CAPACITY_REFUSAL,
    MAX_FRAME_BYTES,
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
MAX_BROKER_REFUSAL_WORKERS = 4
TAILNET_BIND_RETRY_SECONDS = 5.0
TAILNET_REFRESH_POLL_SECONDS = 0.1
REFUSAL_WRITE_TIMEOUT_SECONDS = 1.0


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
    tailnet_address = local_tailnet_address()
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
        return peers(root, include_remote=False, internal=True, include_devin=False)
    if operation == "peers" and set(request) == {
        "schema_version",
        "operation",
        "include_devin",
    }:
        if (
            request.get("schema_version") != SCHEMA_VERSION
            or request.get("include_devin") is not True
        ):
            raise ChatError("Tailnet broker request is invalid")
        valid_tailnet_address(peer_address)
        return peers(root, include_remote=False, internal=True, include_devin=True)
    if operation == "peers" and set(request) == {
        "schema_version",
        "operation",
        "include_delivery_mode",
    }:
        if (
            request.get("schema_version") != SCHEMA_VERSION
            or request.get("include_delivery_mode") is not True
        ):
            raise ChatError("Tailnet broker request is invalid")
        valid_tailnet_address(peer_address)
        return peers(
            root,
            include_remote=False,
            internal=True,
            include_delivery_mode=True,
            include_devin=False,
        )
    if operation == "peers" and set(request) == {
        "schema_version",
        "operation",
        "include_delivery_mode",
        "include_devin",
    }:
        if (
            request.get("schema_version") != SCHEMA_VERSION
            or request.get("include_delivery_mode") is not True
            or request.get("include_devin") is not True
        ):
            raise ChatError("Tailnet broker request is invalid")
        valid_tailnet_address(peer_address)
        return peers(
            root,
            include_remote=False,
            internal=True,
            include_delivery_mode=True,
            include_devin=True,
        )
    if operation == "peers" and set(request) == {
        "schema_version",
        "operation",
        "include_delivery_mode",
        "include_title",
    }:
        if (
            request.get("schema_version") != SCHEMA_VERSION
            or request.get("include_delivery_mode") is not True
            or request.get("include_title") is not True
        ):
            raise ChatError("Tailnet broker request is invalid")
        valid_tailnet_address(peer_address)
        return peers(
            root,
            include_remote=False,
            internal=True,
            include_delivery_mode=True,
            include_title=True,
            include_devin=False,
        )
    if operation == "peers" and set(request) == {
        "schema_version",
        "operation",
        "include_delivery_mode",
        "include_title",
        "include_devin",
    }:
        if (
            request.get("schema_version") != SCHEMA_VERSION
            or request.get("include_delivery_mode") is not True
            or request.get("include_title") is not True
            or request.get("include_devin") is not True
        ):
            raise ChatError("Tailnet broker request is invalid")
        valid_tailnet_address(peer_address)
        return peers(
            root,
            include_remote=False,
            internal=True,
            include_delivery_mode=True,
            include_title=True,
            include_devin=True,
        )
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


def _parse_peeked(buffered: bytes) -> dict[object, object] | None:
    if b"\n" not in buffered or len(buffered) > MAX_FRAME_BYTES:
        return None
    try:
        raw: object = json.loads(buffered.split(b"\n", 1)[0])
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return cast(dict[object, object], raw) if isinstance(raw, dict) else None


def _ready_request(connection: socket.socket) -> dict[object, object] | None:
    """A complete request already buffered, or None while anything is undecided."""
    try:
        buffered = connection.recv(65536, socket.MSG_PEEK | socket.MSG_DONTWAIT)
    except OSError:
        return None
    return _parse_peeked(buffered)


def _peeked_request(connection: socket.socket) -> dict[object, object] | None:
    """Copy the buffered request frame without consuming it, or None if undecided."""
    deadline = time.monotonic() + 5.0
    buffered = b""
    while b"\n" not in buffered and len(buffered) <= MAX_FRAME_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        connection.settimeout(remaining)
        try:
            buffered = connection.recv(65536, socket.MSG_PEEK)
        except OSError:
            return None
        if not buffered:
            return None
    return _parse_peeked(buffered)


def probe_broker_connection(root: Path, connection: socket.socket, peer_address: str) -> None:
    """Serve an overflowed authorize, refuse anything else before reading it."""
    request = _peeked_request(connection)
    if request is None:
        # Undecided without consuming a byte: drop silently, as before.
        return
    if request.get("operation") != "authorize":
        # The request was only peeked at, never consumed or dispatched, so the
        # capacity refusal is truthful and cannot be confused with a response
        # to a request that was read.
        connection.settimeout(REFUSAL_WRITE_TIMEOUT_SECONDS)
        emit_frame_safely(connection, BROKER_CAPACITY_REFUSAL)
        return
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


def _probe_and_close(
    root: Path,
    connection: socket.socket,
    peer_address: str,
    admission: BrokerAdmission,
) -> None:
    with connection:
        try:
            probe_broker_connection(root, connection, peer_address)
        except (ChatError, OSError):
            return
        finally:
            admission.release(peer_address)


def _refuse_and_close(connection: socket.socket, budget: threading.BoundedSemaphore) -> None:
    """Tell one overflowed connection it was refused, then close, bounded."""
    try:
        connection.settimeout(REFUSAL_WRITE_TIMEOUT_SECONDS)
        emit_frame_safely(connection, BROKER_CAPACITY_REFUSAL)
        # A bare close with unread request bytes can reset before the
        # refusal arrives; the half-close orders it ahead of the FIN.
        with suppress(OSError):
            connection.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    finally:
        connection.close()
        budget.release()


def _refuse_overflow(
    connection: socket.socket,
    refusals: tuple[Executor, threading.BoundedSemaphore] | None,
) -> bool:
    if refusals is None:
        return False
    refusal_workers, refusal_budget = refusals
    if not refusal_budget.acquire(blocking=False):
        return False
    refusal_workers.submit(_refuse_and_close, connection, refusal_budget)
    return True


def dispatch_broker_connection(
    workers: Executor,
    root: Path,
    connection: socket.socket,
    peer_address: str,
    admission: BrokerAdmission,
    callbacks: tuple[Executor, BrokerAdmission] | None = None,
    refusals: tuple[Executor, threading.BoundedSemaphore] | None = None,
) -> bool:
    """Give each accepted connection an independent bounded worker."""
    if admission.acquire(peer_address):
        workers.submit(_serve_and_close, root, connection, peer_address, admission)
        return True
    # A complete request already buffered names its lane without consuming a
    # byte, so an overflowed non-authorize request is refused outright and
    # never spends a callback-lane seat.
    ready = _ready_request(connection)
    if ready is not None and ready.get("operation") != "authorize":
        if _refuse_overflow(connection, refusals):
            return True
        connection.close()
        return False
    # A receive handler holds its admission while it waits on a reverse
    # authorization callback to the sender's broker, and that callback needs
    # admission of its own there. When reciprocal traffic fills a peer budget
    # with receive handlers, the callbacks they wait on are refused and every
    # admitted send dies as an unknown outcome. Overflow connections therefore
    # get one bounded reserve lane that serves only authorize requests, so a
    # completion callback always reaches a worker while every other operation
    # keeps the same truthful rejection it had before.
    if callbacks is not None:
        callback_workers, callback_admission = callbacks
        if callback_admission.acquire(peer_address):
            callback_workers.submit(
                _probe_and_close, root, connection, peer_address, callback_admission
            )
            return True
    # Nothing admitted this connection and no request byte was read, so no
    # effect is possible: answer with the definite capacity refusal instead of
    # an ambiguous close that freezes the send as unknown. The writer budget
    # keeps a flood of refused connections from owning unbounded workers.
    if _refuse_overflow(connection, refusals):
        return True
    connection.close()
    return False


def dispatch_ready_brokers(
    workers: Executor,
    root: Path,
    readable: list[socket.socket],
    admission: BrokerAdmission,
    callbacks: tuple[Executor, BrokerAdmission] | None = None,
    refusals: tuple[Executor, threading.BoundedSemaphore] | None = None,
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
            callbacks,
            refusals,
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
        callback_admission = BrokerAdmission()
        refusal_budget = threading.BoundedSemaphore(MAX_BROKER_CONNECTIONS)
        with (
            ThreadPoolExecutor(
                max_workers=MAX_BROKER_CONNECTIONS,
                thread_name_prefix="cross-agent-chat",
            ) as workers,
            ThreadPoolExecutor(
                max_workers=MAX_BROKER_CONNECTIONS,
                thread_name_prefix="cross-agent-chat-callback",
            ) as callback_workers,
            ThreadPoolExecutor(
                max_workers=MAX_BROKER_REFUSAL_WORKERS,
                thread_name_prefix="cross-agent-chat-refusal",
            ) as refusal_workers,
            ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="cross-agent-chat-refresh",
            ) as refresh_workers,
        ):
            refresh: Future[list[tuple[str, int]]] | None = refresh_workers.submit(broker_bindings)
            next_refresh_at: float | None = None
            while True:
                if refresh is not None and refresh.done():
                    current_bindings = refresh.result()
                    desired_tailnet = current_bindings[1] if len(current_bindings) == 2 else None
                    if tailnet_server is not None and desired_tailnet != tailnet_binding:
                        servers.remove(tailnet_server)
                        tailnet_server.close()
                        tailnet_server = None
                        tailnet_binding = None
                    if desired_tailnet is not None and desired_tailnet != tailnet_binding:
                        replacement = bind_broker_listener(desired_tailnet, allow_unavailable=True)
                        if replacement is not None:
                            servers.append(replacement)
                            tailnet_server = replacement
                            tailnet_binding = desired_tailnet
                    refresh = None
                    next_refresh_at = time.monotonic() + TAILNET_BIND_RETRY_SECONDS
                if (
                    refresh is None
                    and next_refresh_at is not None
                    and time.monotonic() >= next_refresh_at
                ):
                    refresh = refresh_workers.submit(broker_bindings)
                    next_refresh_at = None
                timeout = TAILNET_BIND_RETRY_SECONDS
                if refresh is not None:
                    timeout = min(timeout, TAILNET_REFRESH_POLL_SECONDS)
                elif next_refresh_at is not None:
                    timeout = max(0.0, next_refresh_at - time.monotonic())
                selectable_servers = [local_server] if refresh is not None else servers
                readable, _, _ = select.select(selectable_servers, [], [], timeout)
                dispatch_ready_brokers(
                    workers,
                    root,
                    readable,
                    admission,
                    (callback_workers, callback_admission),
                    (refusal_workers, refusal_budget),
                )
    finally:
        for server in servers:
            server.close()
