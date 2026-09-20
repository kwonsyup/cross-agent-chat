"""Deterministic trials for absolute exchange deadlines.

Each exchange -- a courier ``request_socket`` call, a Tailnet
``request_tailnet`` call, and broker/courier frame intake -- runs on one
monotonic deadline rather than a fresh inactivity bound per ``recv``.
Scripted sockets replay fragments against a fake clock so a slow trickle
can never reset the total elapsed budget, and contained real-socket
journeys pin the same classification on the wire.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

import cross_agent_chat.tailnet_broker as broker
from cross_agent_chat.core import ChatError, UnknownDeliveryError
from cross_agent_chat.runtime import (
    MAX_FRAME_BYTES,
    read_frame,
    request_socket,
    request_tailnet,
)
from cross_agent_chat.tailnet_broker import probe_broker_connection, serve_broker_connection

_PEER = "100.64.0.11"

_AUTHORIZE_FRAME = (
    b'{"schema_version":1,"operation":"authorize",'
    b'"event_id":"00000000-0000-4000-8000-000000000001",'
    b'"source_alias":"codex@peer:proj:peer",'
    b'"source_generation":"00000000-0000-4000-8000-000000000002",'
    b'"target_key":"' + b"a" * 64 + b'",'
    b'"target_generation":"00000000-0000-4000-8000-000000000003",'
    b'"payload_digest":"' + b"b" * 64 + b'"}'
)


class _FakeClock:
    """A monotonic clock advanced only by scripted socket operations."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedSocket(socket.socket):
    """A socket whose reads replay a script against a shared fake clock.

    Each script entry is ``(gap, payload)``: the recv waits ``gap`` fake
    seconds and returns ``payload`` -- or, when ``gap`` exceeds the armed
    socket timeout, waits only until that timeout and raises
    ``TimeoutError``, exactly what a real blocking socket would do.
    """

    def __init__(self, source: socket.socket, clock: _FakeClock) -> None:
        super().__init__(fileno=source.detach())
        self._clock = clock
        self._timeout: float | None = None
        self.script: list[tuple[float, bytes]] = []
        self.timeouts: list[float | None] = []
        self.recv_calls = 0

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)
        self._timeout = value

    def gettimeout(self) -> float | None:
        return self._timeout

    def recv(self, bufsize: int, flags: int = 0) -> bytes:
        self.recv_calls += 1
        assert self.script, "scripted recv ran out of results"
        gap, payload = self.script.pop(0)
        armed = self._timeout
        if armed is not None and gap > armed:
            self._clock.advance(armed)
            raise TimeoutError("scripted recv exceeded the armed timeout")
        self._clock.advance(gap)
        return payload


def _scripted_pair(clock: _FakeClock) -> tuple[socket.socket, _ScriptedSocket]:
    client, server = socket.socketpair()
    return client, _ScriptedSocket(server, clock)


def _socket_path(name: str) -> Path:
    """A courier socket path under the conftest's short /tmp socket root."""
    return Path(os.environ["CROSS_AGENT_CHAT_TEST_SOCKET_ROOT"]) / name


def _unix_listener(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(4)
    return listener


def test_fragments_inside_one_deadline_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fragments that arrive within the total budget still form the frame."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client, scripted = _scripted_pair(clock)
    scripted.script.extend([(0.2, b'{"a":'), (0.2, b'1}\n')])
    try:
        assert read_frame(scripted, deadline=clock.monotonic() + 1.0) == b'{"a":1}'
    finally:
        client.close()
        scripted.close()


def test_frame_split_at_the_newline_still_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newline delivered in its own recv still terminates the frame."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client, scripted = _scripted_pair(clock)
    scripted.script.extend([(0.1, b'{"a":1}'), (0.1, b"\n")])
    try:
        assert read_frame(scripted, deadline=clock.monotonic() + 5.0) == b'{"a":1}'
    finally:
        client.close()
        scripted.close()


def test_trickle_below_the_inactivity_bound_still_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gaps under the socket timeout must not reset the total frame budget.

    Every gap (0.4 s) is below the configured socket timeout (0.5 s), so an
    inactivity-only read would consume the whole script; an absolute
    deadline fails the frame as soon as the cumulative budget is spent.
    """
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client, scripted = _scripted_pair(clock)
    scripted.settimeout(0.5)
    scripted.script.extend([(0.4, b"{"), (0.4, b'"'), (0.4, b'a":1}\n')])
    try:
        with pytest.raises(TimeoutError):
            read_frame(scripted)
        assert scripted.recv_calls == 2
        assert scripted.timeouts[1:] == [0.5, pytest.approx(0.1)]
        assert clock.monotonic() == pytest.approx(1_000.5)
    finally:
        client.close()
        scripted.close()


def test_expired_frame_deadline_fails_before_recv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-spent deadline refuses to recv instead of blocking."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client, scripted = _scripted_pair(clock)
    scripted.script.append((0.0, b'{"a":1}\n'))
    try:
        with pytest.raises(TimeoutError):
            read_frame(scripted, deadline=clock.monotonic() - 1.0)
        assert scripted.recv_calls == 0
    finally:
        client.close()
        scripted.close()


def test_partial_frame_then_fin_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer that closes mid-frame still produces a malformed-frame error."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client, scripted = _scripted_pair(clock)
    scripted.script.extend([(0.1, b'{"a":1}'), (0.0, b"")])
    try:
        with pytest.raises(ChatError, match="malformed"):
            read_frame(scripted, deadline=clock.monotonic() + 5.0)
    finally:
        client.close()
        scripted.close()


def test_frame_over_the_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A frame that grows past the byte bound is refused, deadline or not."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client, scripted = _scripted_pair(clock)
    scripted.script.append((0.0, b"x" * 9))
    try:
        with pytest.raises(ChatError, match="bounded limit"):
            read_frame(scripted, limit=8, deadline=clock.monotonic() + 5.0)
    finally:
        client.close()
        scripted.close()


def test_broker_intake_accepts_fragments_inside_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker's single intake deadline still serves a complete request."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client, scripted = _scripted_pair(clock)
    scripted.script.extend(
        [(0.5, b'{"schema_version":1,'), (0.5, b'"operation":"peers"}\n')]
    )
    try:
        serve_broker_connection(tmp_path, scripted, _PEER)
        client.settimeout(5.0)
        assert json.loads(client.recv(65536)) == {"schema_version": 1, "peers": []}
    finally:
        client.close()
        scripted.close()


def test_broker_intake_runs_on_one_total_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trickled broker request dies at the intake deadline, not per-recv."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client, scripted = _scripted_pair(clock)
    scripted.script.extend(
        [(4.0, b'{"schema_version":1,'), (4.0, b'"operation":"peers"}\n')]
    )
    try:
        with pytest.raises(TimeoutError):
            serve_broker_connection(tmp_path, scripted, _PEER)
        assert scripted.recv_calls == 2
        assert scripted.timeouts == [5.0, 5.0, 1.0]
        assert clock.monotonic() == pytest.approx(1_005.0)
    finally:
        client.close()
        scripted.close()


def test_probe_consumes_inside_the_original_peek_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reserve lane's consume read shares the peek's original deadline.

    A fresh timeout after the peek would let one overflowed connection hold
    a reserve worker for nearly two deadline lengths; the consume must run
    on what is left of the same budget the probe started with.
    """
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client, scripted = _scripted_pair(clock)
    frame = _AUTHORIZE_FRAME + b"\n"
    scripted.script.extend([(0.0, frame), (0.0, frame)])
    recorded: list[float | None] = []
    original_read_frame = read_frame

    def spy(
        connection: socket.socket,
        limit: int = MAX_FRAME_BYTES,
        *,
        deadline: float | None = None,
    ) -> bytes:
        recorded.append(deadline)
        return original_read_frame(connection, limit, deadline=deadline)

    monkeypatch.setattr(broker, "read_frame", spy)
    try:
        probe_broker_connection(tmp_path, scripted, _PEER)
    finally:
        client.close()
        scripted.close()
    assert recorded == [1_000.0 + broker.PEEK_REQUEST_DEADLINE_SECONDS]


def test_fragmented_reply_within_budget_succeeds() -> None:
    """Real journey: a reply delivered in fragments inside the budget works."""
    path = _socket_path("courier-a.sock")
    listener = _unix_listener(path)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(b'{"schema_version":1,')
            time.sleep(0.05)
            connection.sendall(b'"status":"READY"}\n')

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        response = request_socket(path, {"operation": "health"}, timeout=5.0)
        assert response == {"schema_version": 1, "status": "READY"}
    finally:
        thread.join(timeout=5.0)
        listener.close()
        path.unlink(missing_ok=True)
    assert not thread.is_alive()


def test_slow_trickle_cannot_extend_the_exchange() -> None:
    """Real journey: gaps under the old inactivity bound still hit the deadline.

    Each 0.25 s gap is below the 1 s socket timeout, so per-recv inactivity
    accounting would ride the trickle for the full 3 s script; the absolute
    deadline must end the exchange near 1 s.
    """
    path = _socket_path("courier-b.sock")
    listener = _unix_listener(path)
    stop = threading.Event()

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            for _ in range(12):
                if stop.is_set():
                    return
                time.sleep(0.25)
                try:
                    connection.sendall(b'"')
                except OSError:
                    return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(UnknownDeliveryError):
            request_socket(path, {"operation": "health"}, timeout=1.0)
        assert time.monotonic() - started < 2.0
    finally:
        stop.set()
        thread.join(timeout=5.0)
        listener.close()
        path.unlink(missing_ok=True)


def test_reply_timeout_after_write_is_unknown() -> None:
    """Real journey: silence after a sent request stays an unknown outcome."""
    path = _socket_path("courier-c.sock")
    listener = _unix_listener(path)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            time.sleep(1.0)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(UnknownDeliveryError):
            request_socket(path, {"operation": "health"}, timeout=0.3)
        assert time.monotonic() - started < 2.0
    finally:
        thread.join(timeout=5.0)
        listener.close()
        path.unlink(missing_ok=True)


def test_invalid_utf8_reply_is_unknown() -> None:
    """Real journey: an undecodable reply after the write stays unknown."""
    path = _socket_path("courier-d.sock")
    listener = _unix_listener(path)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(b"\xff\xfe\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with pytest.raises(UnknownDeliveryError):
            request_socket(path, {"operation": "health"}, timeout=5.0)
    finally:
        thread.join(timeout=5.0)
        listener.close()
        path.unlink(missing_ok=True)


def test_exhausted_budget_before_connect_is_pre_effect() -> None:
    """A deadline already spent before connect is a decided pre-effect error."""
    path = _socket_path("courier-e.sock")
    listener = _unix_listener(path)
    listener.settimeout(0.2)
    try:
        with pytest.raises(ChatError, match="before delivery") as error:
            request_socket(path, {"operation": "health"}, timeout=0.0)
        assert not isinstance(error.value, UnknownDeliveryError)
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()
        path.unlink(missing_ok=True)


def test_connect_consumes_the_shared_exchange_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connect time is spent from the same deadline the write and read share."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client_end, scripted = _scripted_pair(clock)
    scripted.script.append((0.5, b""))
    connect_timeouts: list[float | None] = []

    def create_connection(
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        connect_timeouts.append(timeout)
        clock.advance(0.9)
        return scripted

    monkeypatch.setattr(socket, "create_connection", create_connection)
    try:
        with pytest.raises(UnknownDeliveryError):
            request_tailnet(
                "100.64.0.11",
                {"schema_version": 1, "operation": "peers"},
                timeout=1.0,
            )
    finally:
        client_end.close()
        scripted.close()
    assert connect_timeouts == [1.0]
    # Write and read stages arm only the ~0.1 s remainder, never a fresh budget.
    assert len(scripted.timeouts) >= 2
    assert all(
        isinstance(value, float) and 0.0 < value < 0.5 for value in scripted.timeouts
    )


def test_tailnet_post_write_timeout_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline hit while awaiting the broker's reply stays unknown."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    client_end, scripted = _scripted_pair(clock)
    scripted.script.append((2.0, b""))

    def create_connection(
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        return scripted

    monkeypatch.setattr(socket, "create_connection", create_connection)
    try:
        with pytest.raises(UnknownDeliveryError):
            request_tailnet(
                "100.64.0.11",
                {"schema_version": 1, "operation": "peers"},
                timeout=1.0,
            )
    finally:
        client_end.close()
        scripted.close()
    assert clock.monotonic() == pytest.approx(1_001.0)


def test_tailnet_preconnect_expiry_is_pre_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline spent before dialing never attempts the connection."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    def forbidden(
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        raise AssertionError("connect attempted after the deadline")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(ChatError, match="before delivery") as error:
        request_tailnet(
            "100.64.0.11",
            {"schema_version": 1, "operation": "peers"},
            timeout=0.0,
        )
    assert not isinstance(error.value, UnknownDeliveryError)


def test_tailnet_connect_timeout_is_pre_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect that itself times out is a decided pre-effect failure."""
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    def stalled(
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        clock.advance(5.0)
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(socket, "create_connection", stalled)
    with pytest.raises(ChatError, match="before delivery") as error:
        request_tailnet(
            "100.64.0.11",
            {"schema_version": 1, "operation": "peers"},
            timeout=1.0,
        )
    assert not isinstance(error.value, UnknownDeliveryError)
