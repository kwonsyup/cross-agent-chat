"""Deterministic trials for the broker reserve-lane probe.

``probe_broker_connection`` peeks at an overflowed connection's buffered
request without consuming it. These trials drive the real probe and dispatch
path over owned socketpairs while request fragments arrive on a controlled
schedule, and count every peek so an unchanged partial frame can never spin
the CPU again.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import cross_agent_chat.tailnet_broker as broker
from cross_agent_chat.runtime import BROKER_CAPACITY_REFUSAL, read_frame
from cross_agent_chat.tailnet_broker import (
    MAX_BROKER_CONNECTIONS,
    MAX_BROKER_CONNECTIONS_PER_PEER,
    MAX_BROKER_REFUSAL_WORKERS,
    BrokerAdmission,
    dispatch_broker_connection,
    probe_broker_connection,
)

_PEER = "100.64.0.11"
_MAX_STALL_PEEKS = 64

_AUTHORIZE_FRAME = (
    b'{"schema_version":1,"operation":"authorize",'
    b'"event_id":"00000000-0000-4000-8000-000000000001",'
    b'"source_alias":"codex@peer:proj:peer",'
    b'"source_generation":"00000000-0000-4000-8000-000000000002",'
    b'"target_key":"' + b"a" * 64 + b'",'
    b'"target_generation":"00000000-0000-4000-8000-000000000003",'
    b'"payload_digest":"' + b"b" * 64 + b'"}'
)


class _CountingSocket(socket.socket):
    """A socket that counts how often the broker peeks at it."""

    def __init__(self, source: socket.socket) -> None:
        super().__init__(fileno=source.detach())
        self.peeks = 0

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        if flags & socket.MSG_PEEK:
            self.peeks += 1
        return super().recv(bufsize, flags)


def _feed(client: socket.socket, schedule: list[tuple[float, bytes | None]]) -> threading.Thread:
    """Send each fragment at its offset in seconds; None closes the client."""

    def run() -> None:
        started = time.monotonic()
        for offset, fragment in schedule:
            delay = offset - (time.monotonic() - started)
            if delay > 0:
                time.sleep(delay)
            if fragment is None:
                client.close()
                return
            client.sendall(fragment)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@contextmanager
def _lanes() -> Iterator[
    tuple[
        ThreadPoolExecutor,
        tuple[ThreadPoolExecutor, BrokerAdmission],
        tuple[ThreadPoolExecutor, threading.BoundedSemaphore],
    ]
]:
    with (
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as workers,
        ThreadPoolExecutor(max_workers=MAX_BROKER_CONNECTIONS) as callback_workers,
        ThreadPoolExecutor(max_workers=MAX_BROKER_REFUSAL_WORKERS) as refusal_workers,
    ):
        yield (
            workers,
            (callback_workers, BrokerAdmission()),
            (refusal_workers, threading.BoundedSemaphore(MAX_BROKER_CONNECTIONS)),
        )


def _saturate_peer(
    workers: ThreadPoolExecutor,
    callbacks: tuple[ThreadPoolExecutor, BrokerAdmission],
    refusals: tuple[ThreadPoolExecutor, threading.BoundedSemaphore],
    admission: BrokerAdmission,
    root: Path,
    held: list[socket.socket],
) -> None:
    """Fill one peer's admission budget with silent primary-lane handlers."""
    for _ in range(MAX_BROKER_CONNECTIONS_PER_PEER):
        client, server_side = socket.socketpair()
        held.append(client)
        assert dispatch_broker_connection(
            workers, root, server_side, _PEER, admission, callbacks, refusals
        )


def test_stalled_fragment_does_not_spin_the_probe(tmp_path: Path) -> None:
    """A buffered fragment that does not change must wait, not busy-peek.

    Admission is saturated so the connection lands on the authorize reserve
    lane; the rest of the frame arrives after a 0.5 s stall. The probe must
    still serve the authorize, and the peeks against the unchanged buffer stay
    bounded instead of scaling with wall time.
    """
    admission = BrokerAdmission()
    held: list[socket.socket] = []
    with _lanes() as (workers, callbacks, refusals):
        _saturate_peer(workers, callbacks, refusals, admission, tmp_path, held)
        client, raw_server = socket.socketpair()
        server_side = _CountingSocket(raw_server)
        try:
            feeder = _feed(
                client,
                [(0.0, _AUTHORIZE_FRAME[:32]), (0.5, _AUTHORIZE_FRAME[32:] + b"\n")],
            )
            cpu_started = time.process_time()
            wall_started = time.monotonic()
            assert dispatch_broker_connection(
                workers, tmp_path, server_side, _PEER, admission, callbacks, refusals
            )
            client.settimeout(10.0)
            response = json.loads(read_frame(client))
            feeder.join(timeout=10.0)
            print(
                f"\nstalled-fragment probe: peeks={server_side.peeks} "
                f"cpu={time.process_time() - cpu_started:.3f}s "
                f"wall={time.monotonic() - wall_started:.3f}s"
            )
            assert response != BROKER_CAPACITY_REFUSAL
            assert response["status"] == "REFUSED"
            assert response["event_id"] == "00000000-0000-4000-8000-000000000001"
            assert server_side.peeks <= _MAX_STALL_PEEKS, (
                f"probe peeked {server_side.peeks} times during a 0.5 s stall"
            )
        finally:
            client.close()
            server_side.close()
            for held_client in held:
                held_client.close()


def test_fragmented_forward_request_gets_capacity_refusal(tmp_path: Path) -> None:
    """A fragmented non-authorize overflow request is refused once it completes."""
    admission = BrokerAdmission()
    held: list[socket.socket] = []
    with _lanes() as (workers, callbacks, refusals):
        _saturate_peer(workers, callbacks, refusals, admission, tmp_path, held)
        client, raw_server = socket.socketpair()
        server_side = _CountingSocket(raw_server)
        try:
            frame = b'{"schema_version":1,"operation":"peers"}'
            _feed(client, [(0.0, frame[:16]), (0.3, frame[16:] + b"\n")])
            assert dispatch_broker_connection(
                workers, tmp_path, server_side, _PEER, admission, callbacks, refusals
            )
            client.settimeout(10.0)
            assert json.loads(read_frame(client)) == BROKER_CAPACITY_REFUSAL
            assert server_side.peeks <= _MAX_STALL_PEEKS, (
                f"probe peeked {server_side.peeks} times during a 0.3 s stall"
            )
        finally:
            client.close()
            server_side.close()
            for held_client in held:
                held_client.close()


def test_peeked_request_leaves_frame_buffered() -> None:
    """The probe copies the frame; the bytes stay buffered for the real read."""
    client, raw_server = socket.socketpair()
    server_side = _CountingSocket(raw_server)
    try:
        client.sendall(_AUTHORIZE_FRAME + b"\n")
        request = broker._peeked_request(server_side)
        assert request is not None
        assert request.get("operation") == "authorize"
        server_side.settimeout(5.0)
        assert read_frame(server_side) == _AUTHORIZE_FRAME
    finally:
        client.close()
        server_side.close()


def test_peeked_request_waits_for_first_bytes() -> None:
    """With nothing buffered yet, the probe blocks instead of polling."""
    client, raw_server = socket.socketpair()
    server_side = _CountingSocket(raw_server)
    try:
        _feed(client, [(0.3, _AUTHORIZE_FRAME + b"\n")])
        started = time.monotonic()
        request = broker._peeked_request(server_side)
        elapsed = time.monotonic() - started
        assert request is not None
        assert request.get("operation") == "authorize"
        assert elapsed >= 0.2
        assert server_side.peeks <= 8
    finally:
        client.close()
        server_side.close()


def test_peeked_request_returns_none_when_sender_closes_mid_frame() -> None:
    """EOF before the frame completes is undecided, and the wait stays bounded."""
    client, raw_server = socket.socketpair()
    server_side = _CountingSocket(raw_server)
    try:
        _feed(client, [(0.0, _AUTHORIZE_FRAME[:24]), (0.3, None)])
        started = time.monotonic()
        assert broker._peeked_request(server_side) is None
        assert time.monotonic() - started < 4.0
        assert server_side.peeks <= _MAX_STALL_PEEKS, (
            f"probe peeked {server_side.peeks} times before a mid-frame EOF"
        )
    finally:
        client.close()
        server_side.close()


def test_probe_drops_malformed_frame_silently(tmp_path: Path) -> None:
    """A complete but unparseable frame is undecided: the connection closes."""
    client, server_side = socket.socketpair()
    try:
        client.sendall(b"this is not json\n")
        probe_broker_connection(tmp_path, server_side, _PEER)
        server_side.close()
        client.settimeout(5.0)
        assert client.recv(4096) == b""
    finally:
        client.close()
        server_side.close()
