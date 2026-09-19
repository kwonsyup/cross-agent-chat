"""Isolated two-broker admission trials over loopback TCP.

Each ``_Machine`` owns a state root, registered Codex routes, real unix-socket
courier stubs, and a real broker listener running the production accept,
admission, and request-handling path. The two machines exchange traffic over
loopback TCP; every accepted connection is labelled with the peer's fixed
Tailnet address, exactly as a real broker sees one remote node.
"""

from __future__ import annotations

import inspect
import json
import os
import select
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

import cross_agent_chat.core as core_module
import cross_agent_chat.runtime as runtime
from cross_agent_chat.core import (
    SCHEMA_VERSION,
    ChatError,
    IntentStore,
    Registry,
    Route,
    UnknownDeliveryError,
    session_key,
)
from cross_agent_chat.runtime import (
    Target,
    emit_frame_safely,
    read_frame,
    send,
)
from cross_agent_chat.tailnet import TAILNET_PORT
from cross_agent_chat.tailnet_broker import (
    MAX_BROKER_CONNECTIONS,
    MAX_BROKER_CONNECTIONS_PER_PEER,
    BrokerAdmission,
    dispatch_broker_connection,
    dispatch_ready_brokers,
)

MAX_TARGET_COUNT = 12
_ADDRESS_A = "100.64.0.11"
_ADDRESS_B = "100.64.0.12"
_ADDRESS_C = "100.64.0.13"
_ADDRESS_D = "100.64.0.14"


class _PeerListener(socket.socket):
    """Label every accepted loopback connection with one fixed Tailnet peer."""

    def __init__(self, peer_label: str) -> None:
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self._peer_label = peer_label

    def accept(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, _peer = super().accept()
        return connection, (self._peer_label, 0)


def _courier_loop(path: Path, route: Route, stop: threading.Event, accept_delay: float) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o077)
    try:
        server.bind(str(path))
    finally:
        os.umask(old_umask)
    path.chmod(0o600)
    server.listen(8)
    server.settimeout(0.05)
    try:
        while not stop.is_set():
            try:
                connection, _peer = server.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(5.0)
                try:
                    raw: object = json.loads(read_frame(connection))
                except (ChatError, OSError, json.JSONDecodeError):
                    continue
                if not isinstance(raw, dict) or raw.get("generation") != route.generation:
                    continue
                operation = raw.get("operation")
                if operation == "health":
                    emit_frame_safely(
                        connection,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "READY",
                            "generation": route.generation,
                            "alias": route.alias,
                        },
                    )
                elif operation == "accept":
                    if accept_delay > 0:
                        time.sleep(accept_delay)
                    emit_frame_safely(
                        connection,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "event_id": raw.get("event_id"),
                            "status": "TRANSPORT_ACCEPTED",
                            "to": route.alias,
                            "provider": route.provider,
                        },
                    )
    finally:
        server.close()


def _broker_loop(
    listener: socket.socket,
    root: Path,
    stop: threading.Event,
) -> None:
    admission = BrokerAdmission()
    callback_admission = BrokerAdmission()
    # The harness also runs against the pre-reserve broker to reproduce the
    # starvation this file documents; older revisions take no reserve lane.
    reserve_supported = "callbacks" in inspect.signature(dispatch_ready_brokers).parameters
    with (
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as workers,
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as callback_workers,
    ):
        while not stop.is_set():
            readable, _, _ = select.select([listener], [], [], 0.05)
            if readable and reserve_supported:
                dispatch_ready_brokers(
                    workers,
                    root,
                    readable,
                    admission,
                    (callback_workers, callback_admission),
                )
            elif readable:
                dispatch_ready_brokers(workers, root, readable, admission)


@dataclass
class _Machine:
    name: str
    address: str
    root: Path
    source: Route
    targets: list[Route]
    listener: _PeerListener
    stop: threading.Event
    threads: list[threading.Thread] = field(default_factory=list)
    remote_targets: list[Target] = field(default_factory=list)

    @property
    def port(self) -> int:
        return int(self.listener.getsockname()[1])


def _remote_target(route: Route, address: str) -> Target:
    return Target(
        alias=route.alias,
        provider=route.provider,
        device=route.device,
        project=route.project,
        generation=route.generation,
        session_key=session_key(route.provider, route.session_id),
        remote=True,
        tailnet_address=address,
    )


def _build_machine(
    name: str,
    address: str,
    peer_label: str,
    base: Path,
    target_count: int,
    accept_delay: float = 0.0,
) -> _Machine:
    root = base / f"{name}-state"
    workdir = base / f"{name}-work"
    workdir.mkdir(mode=0o700)
    registry = Registry(root)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device=name,
        cwd=str(workdir),
        pid=os.getpid(),
    )
    registry.upsert(source)
    targets = [
        Route.create(
            provider="codex",
            session_id=str(uuid4()),
            device=name,
            cwd=str(workdir),
            pid=os.getpid(),
        )
        for _ in range(target_count)
    ]
    stop = threading.Event()
    threads: list[threading.Thread] = []
    for route in targets:
        registry.upsert(route)
        thread = threading.Thread(
            target=_courier_loop,
            args=(runtime.socket_path(root, route), route, stop, accept_delay),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    listener = _PeerListener(peer_label)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.setblocking(False)
    machine = _Machine(
        name=name,
        address=address,
        root=root,
        source=source,
        targets=targets,
        listener=listener,
        stop=stop,
        threads=threads,
    )
    broker = threading.Thread(target=_broker_loop, args=(listener, root, stop), daemon=True)
    broker.start()
    machine.threads.append(broker)
    return machine


def _stop_machines(machines: list[_Machine]) -> None:
    for machine in machines:
        machine.stop.set()
    for machine in machines:
        for thread in machine.threads:
            thread.join(timeout=5.0)
        machine.listener.close()
        for route in machine.targets:
            runtime.socket_path(machine.root, route).unlink(missing_ok=True)


_caller = threading.local()


@dataclass
class _TrialResult:
    direction: str
    label: str
    seconds: float


def _run_send(
    machine: _Machine,
    target: Target,
    release: threading.Event,
    results: list[_TrialResult],
) -> None:
    _caller.machine = machine
    release.wait(timeout=10.0)
    started = time.monotonic()
    try:
        response = send(machine.root, machine.source, target.session_key, "admission trial")
        label = str(response.get("status"))
    except UnknownDeliveryError:
        label = "UNKNOWN_DELIVERY"
    except ChatError as error:
        label = f"ChatError:{error}"
    results.append(_TrialResult(machine.name, label, time.monotonic() - started))


_REAL_REQUEST_TAILNET = runtime.request_tailnet
_REAL_STATE_LOCK = core_module.state_lock


@contextmanager
def _wired_tailnet(
    monkeypatch: pytest.MonkeyPatch,
    ports: dict[str, int],
    authorize_gate: threading.Barrier | None = None,
) -> Iterator[list[float]]:
    """Route Tailnet addresses to loopback ports and time every state-lock wait."""

    def routed(
        address: str,
        payload: dict[str, object],
        *,
        port: int = TAILNET_PORT,
        timeout: float = 2.0,
    ) -> dict[str, object]:
        if (
            authorize_gate is not None
            and isinstance(payload, dict)
            and payload.get("operation") == "authorize"
        ):
            with suppress(threading.BrokenBarrierError):
                authorize_gate.wait(timeout=15.0)
        return _REAL_REQUEST_TAILNET("127.0.0.1", payload, port=ports[address], timeout=timeout)

    def discovered(
        *,
        include_delivery_mode: bool = False,
        include_title: bool = False,
        include_devin: bool = True,
    ) -> tuple[list[Target], bool]:
        machine = _caller.machine
        return list(machine.remote_targets), True

    lock_waits: list[float] = []

    @contextmanager
    def timed_state_lock(root: Path, name: str) -> Iterator[None]:
        started = time.monotonic()
        with _REAL_STATE_LOCK(root, name):
            lock_waits.append(time.monotonic() - started)
            yield

    monkeypatch.setattr(runtime, "request_tailnet", routed)
    monkeypatch.setattr(runtime, "_remote_discovery", discovered)
    monkeypatch.setattr(core_module, "state_lock", timed_state_lock)
    yield lock_waits


def _run_reciprocal_level(
    machine_a: _Machine,
    machine_b: _Machine,
    level: int,
) -> tuple[list[_TrialResult], float]:
    release = threading.Event()
    results: list[_TrialResult] = []
    started = time.monotonic()
    threads = [
        threading.Thread(
            target=_run_send,
            args=(machine, target, release, results),
            daemon=True,
        )
        for machine in (machine_a, machine_b)
        for target in machine.remote_targets[:level]
    ]
    for thread in threads:
        thread.start()
    release.set()
    for thread in threads:
        thread.join(timeout=30.0)
    wall = time.monotonic() - started
    assert not any(thread.is_alive() for thread in threads), "send thread outlived its bound"
    return results, wall


def _count_labels(results: list[_TrialResult], direction: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        if result.direction != direction:
            continue
        counts[result.label] = counts.get(result.label, 0) + 1
    return counts


def test_reciprocal_admission_levels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    machine_a = _build_machine("alpha", _ADDRESS_A, _ADDRESS_B, tmp_path, MAX_TARGET_COUNT)
    machine_b = _build_machine("bravo", _ADDRESS_B, _ADDRESS_A, tmp_path, MAX_TARGET_COUNT)
    machine_a.remote_targets = [_remote_target(route, _ADDRESS_B) for route in machine_b.targets]
    machine_b.remote_targets = [_remote_target(route, _ADDRESS_A) for route in machine_a.targets]
    ports = {machine.address: machine.port for machine in (machine_a, machine_b)}
    try:
        for level in (1, 3, 6, 12):
            seated = min(level, MAX_BROKER_CONNECTIONS_PER_PEER)
            gate = threading.Barrier(2 * seated)
            with _wired_tailnet(monkeypatch, ports, authorize_gate=gate) as lock_waits:
                results, wall = _run_reciprocal_level(machine_a, machine_b, level)
            for direction in (machine_a.name, machine_b.name):
                counts = _count_labels(results, direction)
                print(
                    f"level={level} direction={direction} wall={wall:.3f}s "
                    f"max_lock_wait={max(lock_waits, default=0.0):.4f}s {counts}"
                )
                admitted = counts.get("TRANSPORT_ACCEPTED", 0)
                assert admitted == seated, (
                    f"level {level} {direction}: expected {seated} admitted sends to "
                    f"complete, got {counts}"
                )
                rejected = level - admitted
                truthful = counts.get("UNKNOWN_DELIVERY", 0) + sum(
                    count for label, count in counts.items() if label.startswith("ChatError:")
                )
                assert truthful == rejected, (
                    f"level {level} {direction}: {rejected} sends were not admitted "
                    f"but {level - truthful} reported no outcome label"
                )
            assert wall < 20.0, f"level {level} did not finish in bounded time"
            for machine in (machine_a, machine_b):
                undecided = [
                    intent.status
                    for intent in IntentStore(machine.root).intents()
                    if intent.status in {"PENDING", "REMOTE_AUTHORIZED"}
                ]
                assert undecided == [], f"{machine.name} left in-flight intents: {undecided}"
    finally:
        _stop_machines([machine_a, machine_b])


def test_slow_and_offline_peers_amid_reciprocal_traffic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_a = _build_machine("alpha", _ADDRESS_A, _ADDRESS_B, tmp_path, 1)
    machine_b = _build_machine("bravo", _ADDRESS_B, _ADDRESS_A, tmp_path, 1)
    machine_c = _build_machine("charlie", _ADDRESS_C, _ADDRESS_A, tmp_path, 1, accept_delay=0.5)
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    dead_port = int(closed.getsockname()[1])
    closed.close()
    machine_a.remote_targets = [
        _remote_target(machine_b.targets[0], _ADDRESS_B),
        _remote_target(machine_c.targets[0], _ADDRESS_C),
        Target(
            alias="codex@delta:delta-work:delta",
            provider="codex",
            device="delta",
            project="delta-work",
            generation=str(uuid4()),
            session_key="d" * 64,
            remote=True,
            tailnet_address=_ADDRESS_D,
        ),
    ]
    machine_b.remote_targets = [_remote_target(machine_a.targets[0], _ADDRESS_A)]
    ports = {
        _ADDRESS_A: machine_a.port,
        _ADDRESS_B: machine_b.port,
        _ADDRESS_C: machine_c.port,
        _ADDRESS_D: dead_port,
    }
    release = threading.Event()
    results: list[_TrialResult] = []
    try:
        with _wired_tailnet(monkeypatch, ports):
            threads = [
                threading.Thread(
                    target=_run_send,
                    args=(machine_a, target, release, results),
                    daemon=True,
                )
                for target in machine_a.remote_targets
            ]
            threads.append(
                threading.Thread(
                    target=_run_send,
                    args=(machine_b, machine_b.remote_targets[0], release, results),
                    daemon=True,
                )
            )
            started = time.monotonic()
            for thread in threads:
                thread.start()
            release.set()
            for thread in threads:
                thread.join(timeout=30.0)
            wall = time.monotonic() - started
        assert not any(thread.is_alive() for thread in threads)
        labels = _count_labels(results, machine_a.name)
        assert labels["TRANSPORT_ACCEPTED"] == 2, labels
        refused = [
            result.label
            for result in results
            if result.direction == machine_a.name and result.label.startswith("ChatError:")
        ]
        assert refused == ["ChatError:Tailnet peer is unavailable before delivery"], labels
        assert _count_labels(results, machine_b.name) == {"TRANSPORT_ACCEPTED": 1}
        assert 0.5 <= wall < 20.0
        offline_events = [
            intent
            for intent in IntentStore(machine_a.root).intents()
            if intent.target_key == "d" * 64
        ]
        assert [intent.status for intent in offline_events] == ["PRE_EFFECT_REJECTED"]
    finally:
        _stop_machines([machine_a, machine_b, machine_c])


def test_authorize_callback_is_answered_when_peer_budget_is_full(tmp_path: Path) -> None:
    """The minimal reproduction: two in-flight receive handlers exhaust one peer's
    budget, and the reverse authorization they wait on must still be served."""
    peer = "100.64.0.11"
    admission = BrokerAdmission()
    callback_admission = BrokerAdmission()
    held: list[socket.socket] = []
    with (
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as workers,
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as callback_workers,
    ):
        callbacks = (callback_workers, callback_admission)
        for _ in range(MAX_BROKER_CONNECTIONS_PER_PEER):
            client, server_side = socket.socketpair()
            held.append(client)
            assert dispatch_broker_connection(
                workers, tmp_path, server_side, peer, admission, callbacks
            )
        client, server_side = socket.socketpair()
        try:
            assert dispatch_broker_connection(
                workers, tmp_path, server_side, peer, admission, callbacks
            )
            client.sendall(
                b'{"schema_version":1,"operation":"authorize",'
                b'"event_id":"00000000-0000-4000-8000-000000000001",'
                b'"source_alias":"codex@peer:proj:peer",'
                b'"source_generation":"00000000-0000-4000-8000-000000000002",'
                b'"target_key":"' + b"a" * 64 + b'",'
                b'"target_generation":"00000000-0000-4000-8000-000000000003",'
                b'"payload_digest":"' + b"b" * 64 + b'"}\n'
            )
            client.settimeout(5.0)
            response = json.loads(client.recv(4096))
            assert response["status"] == "REFUSED"
            assert response["event_id"] == "00000000-0000-4000-8000-000000000001"
        finally:
            client.close()
            for held_client in held:
                held_client.close()


def test_overflow_forward_connection_keeps_truthful_rejection(tmp_path: Path) -> None:
    """An overflowed non-authorize request is closed exactly as before the reserve lane."""
    peer = "100.64.0.11"
    admission = BrokerAdmission()
    callback_admission = BrokerAdmission()
    held: list[socket.socket] = []
    with (
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as workers,
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as callback_workers,
    ):
        callbacks = (callback_workers, callback_admission)
        for _ in range(MAX_BROKER_CONNECTIONS_PER_PEER):
            client, server_side = socket.socketpair()
            held.append(client)
            assert dispatch_broker_connection(
                workers, tmp_path, server_side, peer, admission, callbacks
            )
        client, server_side = socket.socketpair()
        try:
            assert dispatch_broker_connection(
                workers, tmp_path, server_side, peer, admission, callbacks
            )
            client.sendall(b'{"schema_version":1,"operation":"peers"}\n')
            client.settimeout(5.0)
            assert client.recv(4096) == b""
        finally:
            client.close()
            for held_client in held:
                held_client.close()


def test_callback_reserve_is_bounded(tmp_path: Path) -> None:
    """The reserve lane itself stays bounded: silent connections exhaust it too."""
    peer = "100.64.0.11"
    admission = BrokerAdmission()
    callback_admission = BrokerAdmission()
    held: list[socket.socket] = []
    with (
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as workers,
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as callback_workers,
    ):
        callbacks = (callback_workers, callback_admission)
        try:
            for _ in range(2 * MAX_BROKER_CONNECTIONS_PER_PEER):
                client, server_side = socket.socketpair()
                held.append(client)
                assert dispatch_broker_connection(
                    workers, tmp_path, server_side, peer, admission, callbacks
                )
            client, server_side = socket.socketpair()
            held.append(client)
            assert not dispatch_broker_connection(
                workers, tmp_path, server_side, peer, admission, callbacks
            )
        finally:
            for held_client in held:
                held_client.close()
