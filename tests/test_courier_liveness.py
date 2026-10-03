"""Regression coverage: a courier mid-accept must stay live to the control plane.

The field failure these tests name: while one sender's ``accept`` request was
being served, every other connection queued behind it until the caller's own
deadline died, so a perfectly live courier was reported to ``chat_peers`` and
to exact-token senders as "unavailable or changed".
"""

from __future__ import annotations

import os
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import ChatError, IntentStore, Registry, Route, session_key
from cross_agent_chat.recipient import local_token
from cross_agent_chat.runtime import courier_server, request_socket
from cross_agent_chat.transport import remote_envelope


def _route(tmp_path: Path) -> Route:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(project),
        pid=os.getpid(),
    )


def _wait_for_courier_socket(path: Path, timeout: float = 10.0) -> None:
    """Wait until a courier socket is usable, not merely present."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            time.sleep(0.01)
            continue
        if (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600
        ):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.1)
            try:
                probe.connect(str(path))
            except OSError:
                pass
            else:
                return
            finally:
                probe.close()
        time.sleep(0.01)
    pytest.fail("courier socket did not become safely ready")


def _agent_view(item: Route) -> dict[str, object]:
    return {
        "session_id": item.session_id,
        "name": "API A",
        "kind": "interactive",
        "cwd": item.cwd,
    }


def _start_courier(root: Path, item: Route) -> threading.Thread:
    worker = threading.Thread(
        target=courier_server,
        kwargs={
            "provider": item.provider,
            "state_root_value": str(root),
            "session_id": item.session_id,
            "cwd": item.cwd,
            "generation": item.generation,
            "pid": item.pid,
        },
        daemon=True,
    )
    worker.start()
    _wait_for_courier_socket(runtime.socket_path(root, item))
    return worker


def _stop_courier(root: Path, item: Route, worker: threading.Thread) -> None:
    request_socket(
        runtime.socket_path(root, item),
        {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
        timeout=30.0,
    )
    worker.join(timeout=30.0)
    assert not worker.is_alive()


def _bootstrap(root: Path, item: Route) -> None:
    assert request_socket(
        runtime.socket_path(root, item),
        {"schema_version": 1, "operation": "bootstrap", "generation": item.generation},
        timeout=5.0,
    ) == {
        "schema_version": 1,
        "status": "BOOTSTRAPPED",
        "generation": item.generation,
    }


def _held_claude_provider(
    monkeypatch: pytest.MonkeyPatch, item: Route
) -> tuple[threading.Event, threading.Event, list[str]]:
    """Stub the Claude provider boundary so one accept can be held open."""
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    monkeypatch.setattr(runtime, "discover_target_ref", lambda _name: "API A [ref]")
    accept_entered = threading.Event()
    release_accept = threading.Event()
    send_calls: list[str] = []

    def held_sendmessage(_ref: str, _message: str, _executable: Path) -> None:
        send_calls.append(_ref)
        accept_entered.set()
        release_accept.wait(timeout=30.0)

    monkeypatch.setattr(runtime, "sendmessage", held_sendmessage)
    return accept_entered, release_accept, send_calls


def _accept(
    root: Path, item: Route, event_id: str, message: str, timeout: float
) -> dict[str, object]:
    return request_socket(
        runtime.socket_path(root, item),
        {
            "schema_version": 1,
            "operation": "accept",
            "generation": item.generation,
            "event_id": event_id,
            "message": message,
        },
        timeout=timeout,
    )


def _source_route(tmp_path: Path, name: str = "source-project") -> Route:
    project = tmp_path / name
    project.mkdir(exist_ok=True)
    return Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(project),
        pid=os.getpid(),
    )


def _counted_accept_frames(monkeypatch: pytest.MonkeyPatch, attempts: list[str]) -> None:
    """Record every accept frame sent, whether admitted or busy-rejected."""
    real_request = runtime.request_socket

    def counted(path: Path, payload: dict[str, object], **kwargs: object) -> dict[str, object]:
        if payload.get("operation") == "accept":
            attempts.append(str(payload["event_id"]))
        return real_request(path, payload, **kwargs)

    monkeypatch.setattr(runtime, "request_socket", counted)


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert predicate()


def test_idle_courier_health_returns_the_live_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: an idle courier answers the health probe."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        target = runtime._local_target(root, item, timeout=5.0)
        assert target is not None
        assert target.alias == "claude@studio:project:API A"
        assert target.generation == item.generation
    finally:
        _stop_courier(root, item, worker)


def test_dead_or_replaced_route_still_reports_no_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: a genuinely stale route must keep refusing."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    # A route with no courier behind it is dead to discovery.
    assert runtime._local_target(root, item, timeout=2.0) is None
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        replacement = Route.create(
            provider="claude",
            session_id=item.session_id,
            device="studio",
            cwd=item.cwd,
            pid=os.getpid(),
        )
        Registry(root).upsert(replacement)
        worker.join(timeout=30.0)
        assert not worker.is_alive()
        # Replaced-owner route: the old generation must keep refusing even
        # while a fresh registration exists for the same session.
        assert runtime._local_target(root, item, timeout=2.0) is None
        assert runtime._local_target(root, replacement, timeout=2.0) is None
    finally:
        if worker.is_alive():
            _stop_courier(root, item, worker)


def test_busy_courier_still_answers_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A courier held inside one provider accept is not 'unavailable or changed'."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    accept_entered, release_accept, _send_calls = _held_claude_provider(monkeypatch, item)
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        event_id = str(uuid4())
        responses: list[dict[str, object]] = []

        def deliver() -> None:
            responses.append(_accept(root, item, event_id, "held delivery", 30.0))

        sender = threading.Thread(target=deliver, daemon=True)
        sender.start()
        assert accept_entered.wait(10.0)
        # The provider effect is provably still in flight: a live courier that
        # cannot answer its health probe now is exactly the sender-facing
        # "recipient is unavailable or changed" this regression repairs.
        target = runtime._local_target(root, item, timeout=5.0)
        assert not release_accept.is_set()
        assert target is not None, "a busy courier is reported as unavailable/changed"
        release_accept.set()
        sender.join(timeout=10.0)
        assert responses == [
            {
                "schema_version": 1,
                "event_id": event_id,
                "status": "TRANSPORT_ACCEPTED",
                "to": "claude@studio:project:API A",
                "provider": "claude",
            }
        ]
    finally:
        release_accept.set()
        _stop_courier(root, item, worker)


def test_second_accept_is_rejected_while_one_delivery_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Effects stay serialized: a concurrent accept gets a decided rejection."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    accept_entered, release_accept, send_calls = _held_claude_provider(monkeypatch, item)
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        first_id = str(uuid4())
        responses: list[dict[str, object]] = []

        def deliver() -> None:
            responses.append(_accept(root, item, first_id, "held delivery", 30.0))

        sender = threading.Thread(target=deliver, daemon=True)
        sender.start()
        assert accept_entered.wait(10.0)
        second_id = str(uuid4())
        rejected = _accept(root, item, second_id, "concurrent delivery", 5.0)
        assert rejected == {
            "schema_version": 1,
            "event_id": second_id,
            "status": "PRE_EFFECT_REJECTED",
            "provider": "claude",
            "error": "session courier is busy with another delivery",
        }
        # The precise reason may cross the tailnet to a remote sender.
        assert (
            "session courier is busy with another delivery"
            in runtime.FORWARDABLE_PRE_EFFECT_REASONS
        )
        # Exactly one provider effect ran; the second send never reached it.
        assert send_calls == ["API A [ref]"]
        release_accept.set()
        sender.join(timeout=10.0)
        assert responses[0]["status"] == "TRANSPORT_ACCEPTED"
        # A resend once the courier frees up is accepted normally.
        third_id = str(uuid4())
        accepted = _accept(root, item, third_id, "retry after busy", 10.0)
        assert accepted["status"] == "TRANSPORT_ACCEPTED"
        assert send_calls == ["API A [ref]", "API A [ref]"]
    finally:
        release_accept.set()
        _stop_courier(root, item, worker)


def test_shutdown_still_answers_while_one_delivery_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown is a control-plane op: it must not wait behind an accept."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    accept_entered, release_accept, _send_calls = _held_claude_provider(monkeypatch, item)
    worker = _start_courier(root, item)
    path = runtime.socket_path(root, item)
    try:
        _bootstrap(root, item)
        responses: list[dict[str, object]] = []
        sender = threading.Thread(
            target=lambda: responses.append(
                _accept(root, item, str(uuid4()), "held delivery", 30.0)
            ),
            daemon=True,
        )
        sender.start()
        assert accept_entered.wait(10.0)
        stopped = request_socket(
            path,
            {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
            timeout=5.0,
        )
        assert stopped == {"schema_version": 1, "status": "STOPPED"}
    finally:
        release_accept.set()
        sender.join(timeout=10.0)
        worker.join(timeout=30.0)
    assert not worker.is_alive()
    with pytest.raises(FileNotFoundError):
        path.lstat()


def test_local_send_retries_busy_courier_until_the_held_delivery_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identical frame is retried inside the deadline; no manual resend."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    source = _source_route(tmp_path)
    Registry(root).upsert(source)
    accept_entered, release_accept, send_calls = _held_claude_provider(monkeypatch, item)
    attempts: list[str] = []
    _counted_accept_frames(monkeypatch, attempts)
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        held_responses: list[dict[str, object]] = []
        sender = threading.Thread(
            target=lambda: held_responses.append(
                _accept(root, item, str(uuid4()), "held delivery", 30.0)
            ),
            daemon=True,
        )
        sender.start()
        assert accept_entered.wait(10.0)
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def send() -> None:
            try:
                results.append(
                    runtime.send_local(
                        root,
                        source,
                        local_token(
                            root,
                            session_key(item.provider, item.session_id),
                            item.generation,
                        ),
                        "delivery behind a busy courier",
                    )
                )
            except BaseException as error:
                errors.append(error)

        send_thread = threading.Thread(target=send, daemon=True)
        send_thread.start()
        # A second accept frame while the held delivery is still in flight is
        # exactly the retry this regression requires; it is the identical
        # event, so every recorded attempt must share one event id.
        _wait_until(lambda: len(attempts) >= 2)
        release_accept.set()
        send_thread.join(timeout=15.0)
        sender.join(timeout=15.0)
        assert errors == []
        assert results[0]["status"] == "TRANSPORT_ACCEPTED"
        assert held_responses[0]["status"] == "TRANSPORT_ACCEPTED"
        assert set(attempts) == {attempts[0]}
        # One provider effect per accepted delivery; the busy rejections
        # provably never reached the provider.
        assert send_calls == ["API A [ref]", "API A [ref]"]
        assert [row.status for row in IntentStore(root).intents()] == ["TRANSPORT_ACCEPTED"]
    finally:
        release_accept.set()
        _stop_courier(root, item, worker)


def test_local_send_busy_until_the_deadline_fails_pre_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhausting the deadline while busy is a decided pre-effect failure."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    source = _source_route(tmp_path)
    Registry(root).upsert(source)
    accept_entered, release_accept, send_calls = _held_claude_provider(monkeypatch, item)
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        held_responses: list[dict[str, object]] = []
        sender = threading.Thread(
            target=lambda: held_responses.append(
                _accept(root, item, str(uuid4()), "held delivery", 30.0)
            ),
            daemon=True,
        )
        sender.start()
        assert accept_entered.wait(10.0)
        monkeypatch.setattr(runtime, "OPERATION_TIMEOUT_SECONDS", 4.0)
        with pytest.raises(ChatError) as caught:
            runtime.send_local(
                root,
                source,
                local_token(root, session_key(item.provider, item.session_id), item.generation),
                "delivery that stays queued behind a busy courier",
            )
        assert str(caught.value) == (
            "recipient stayed busy with another delivery until the send "
            "deadline; nothing was delivered; send again"
        )
        # The held delivery was the only provider effect, and the send never
        # degraded to UNKNOWN: resending the same recipient is safe.
        assert send_calls == ["API A [ref]"]
        assert [row.status for row in IntentStore(root).intents()] == ["PRE_EFFECT_REJECTED"]
    finally:
        release_accept.set()
        sender.join(timeout=15.0)
        _stop_courier(root, item, worker)


def test_local_send_does_not_retry_a_decided_non_busy_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the exact busy reason retried; other rejections stay one-shot."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    source = _source_route(tmp_path)
    Registry(root).upsert(source)
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    accept_calls: list[str] = []

    def reject_accept(
        _route: Route, _courier: object, event_id: str, _message: str
    ) -> dict[str, object]:
        accept_calls.append(event_id)
        return {
            "schema_version": 1,
            "event_id": event_id,
            "status": "PRE_EFFECT_REJECTED",
            "provider": "claude",
            "error": "Claude Code executable is unavailable",
        }

    monkeypatch.setattr(runtime, "courier_accept", reject_accept)
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        with pytest.raises(ChatError, match="Claude Code executable is unavailable"):
            runtime.send_local(
                root,
                source,
                local_token(root, session_key(item.provider, item.session_id), item.generation),
                "delivery to a courier that refuses pre-effect",
            )
        assert len(accept_calls) == 1
        assert [row.status for row in IntentStore(root).intents()] == ["PRE_EFFECT_REJECTED"]
    finally:
        _stop_courier(root, item, worker)


def test_remote_receive_retries_busy_courier_with_one_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The destination retries the local accept; authorize ran exactly once."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    accept_entered, release_accept, send_calls = _held_claude_provider(monkeypatch, item)
    attempts: list[str] = []
    _counted_accept_frames(monkeypatch, attempts)
    authorize_calls: list[dict[str, object]] = []

    def authorize(_address: str, payload: dict[str, object], **_: object) -> dict:
        authorize_calls.append(payload)
        return {k: v for k, v in payload.items() if k != "operation"} | {"status": "AUTHORIZED"}

    monkeypatch.setattr(runtime, "request_tailnet", authorize)
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        held_id = str(uuid4())
        held_responses: list[dict[str, object]] = []
        sender = threading.Thread(
            target=lambda: held_responses.append(
                _accept(root, item, held_id, "held delivery", 30.0)
            ),
            daemon=True,
        )
        sender.start()
        assert accept_entered.wait(10.0)
        event_id = str(uuid4())
        envelope = remote_envelope(
            event_id=event_id,
            source_alias="codex@source:api:source-a1",
            source_generation=str(uuid4()),
            target_alias="claude@studio:project:API A",
            generation=item.generation,
            message="hello",
        )
        responses: list[dict[str, object]] = []
        receiver = threading.Thread(
            target=lambda: responses.append(runtime.receive_remote(root, envelope, "100.64.0.11")),
            daemon=True,
        )
        receiver.start()
        # A second accept frame for the same remote event is the in-budget
        # retry; authorization is claimed once and never repeated.
        _wait_until(lambda: len(attempts) >= 2)
        release_accept.set()
        receiver.join(timeout=15.0)
        sender.join(timeout=15.0)
        assert responses[0]["status"] == "TRANSPORT_ACCEPTED"
        assert len(authorize_calls) == 1
        assert send_calls == ["API A [ref]", "API A [ref]"]
    finally:
        release_accept.set()
        _stop_courier(root, item, worker)


def test_idle_courier_answers_many_concurrent_health_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listener backlog admits a burst of concurrent listers."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        start = threading.Barrier(10)
        targets: list[object] = [None] * 10

        def probe(index: int) -> None:
            start.wait(timeout=10.0)
            targets[index] = runtime._local_target(root, item, timeout=10.0)

        probes = [threading.Thread(target=probe, args=(index,), daemon=True) for index in range(10)]
        for probe_thread in probes:
            probe_thread.start()
        for probe_thread in probes:
            probe_thread.join(timeout=15.0)
        assert all(target is not None for target in targets)
    finally:
        _stop_courier(root, item, worker)


def test_busy_courier_answers_many_concurrent_health_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A burst of health probes queues on the backlog, not behind the accept."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    accept_entered, release_accept, _send_calls = _held_claude_provider(monkeypatch, item)
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        held_responses: list[dict[str, object]] = []
        sender = threading.Thread(
            target=lambda: held_responses.append(
                _accept(root, item, str(uuid4()), "held delivery", 30.0)
            ),
            daemon=True,
        )
        sender.start()
        assert accept_entered.wait(10.0)
        start = threading.Barrier(10)
        targets: list[object] = [None] * 10

        def probe(index: int) -> None:
            start.wait(timeout=10.0)
            targets[index] = runtime._local_target(root, item, timeout=10.0)

        probes = [threading.Thread(target=probe, args=(index,), daemon=True) for index in range(10)]
        for probe_thread in probes:
            probe_thread.start()
        for probe_thread in probes:
            probe_thread.join(timeout=15.0)
        assert not release_accept.is_set()
        assert all(target is not None for target in targets)
    finally:
        release_accept.set()
        _stop_courier(root, item, worker)


def test_two_senders_to_one_busy_target_each_complete_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different sessions may send to one target; effects stay serialized.

    The per-(source, target) intent gate admits both sends as distinct events;
    the busy retries -- not the admission gate -- absorb the contention while
    the held delivery finishes.
    """
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    sources = [
        _source_route(tmp_path, "source-a"),
        _source_route(tmp_path, "source-b"),
    ]
    for source in sources:
        Registry(root).upsert(source)
    target = runtime.Target(
        alias="claude@studio:project:API A",
        provider="claude",
        device="studio",
        project="project",
        generation=item.generation,
        session_key=session_key(item.provider, item.session_id),
        remote=False,
        session_id=item.session_id,
        cwd=item.cwd,
        pid=item.pid,
    )
    accept_entered, release_accept, send_calls = _held_claude_provider(monkeypatch, item)
    attempts: list[str] = []
    _counted_accept_frames(monkeypatch, attempts)
    worker = _start_courier(root, item)
    try:
        _bootstrap(root, item)
        held_responses: list[dict[str, object]] = []
        sender = threading.Thread(
            target=lambda: held_responses.append(
                _accept(root, item, str(uuid4()), "held delivery", 30.0)
            ),
            daemon=True,
        )
        sender.start()
        assert accept_entered.wait(10.0)
        results: dict[int, dict[str, object]] = {}
        errors: list[BaseException] = []

        def send(index: int, source: Route) -> None:
            try:
                results[index] = runtime._send_local_target(
                    root,
                    source,
                    target,
                    f"concurrent delivery {index}",
                    deadline=time.monotonic() + 30.0,
                )
            except BaseException as error:
                errors.append(error)

        senders = [
            threading.Thread(target=send, args=(index, source), daemon=True)
            for index, source in enumerate(sources)
        ]
        for send_thread in senders:
            send_thread.start()
        # Both distinct events were busy-rejected and retried while the first
        # delivery still held the courier.
        _wait_until(lambda: len(set(attempts)) >= 2)
        release_accept.set()
        for send_thread in senders:
            send_thread.join(timeout=15.0)
        sender.join(timeout=15.0)
        assert errors == []
        assert [results[index]["status"] for index in results] == [
            "TRANSPORT_ACCEPTED",
            "TRANSPORT_ACCEPTED",
        ]
        assert held_responses[0]["status"] == "TRANSPORT_ACCEPTED"
        assert send_calls == ["API A [ref]"] * 3
        intents = IntentStore(root).intents()
        assert [row.status for row in intents] == [
            "TRANSPORT_ACCEPTED",
            "TRANSPORT_ACCEPTED",
        ]
        assert len({row.event_id for row in intents}) == 2
        assert len({row.source_key for row in intents}) == 2
    finally:
        release_accept.set()
        _stop_courier(root, item, worker)


def test_codex_native_accept_does_not_block_queue_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held native-queue RPC must not freeze peek, health, or shutdown."""
    project = tmp_path / "codex-project"
    project.mkdir()
    item = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(project),
        pid=os.getpid(),
    )
    root = tmp_path / "state"
    Registry(root).upsert(item)
    monkeypatch.setenv(runtime.NATIVE_QUEUE_ENV_VAR, runtime.NATIVE_QUEUE_ENV_VALUE)
    monkeypatch.setenv(runtime.NATIVE_QUEUE_BINARY_ENV_VAR, sys.executable)
    rpc_entered = threading.Event()
    release_rpc = threading.Event()
    rpc_calls: list[str] = []

    def held_queue_native_input(**kwargs: object) -> None:
        rpc_calls.append(str(kwargs["event_id"]))
        rpc_entered.set()
        release_rpc.wait(timeout=30.0)

    monkeypatch.setattr("cross_agent_chat.codex.queue_native_input", held_queue_native_input)
    worker = _start_courier(root, item)
    path = runtime.socket_path(root, item)
    event_id = str(uuid4())
    responses: list[dict[str, object]] = []
    sender = threading.Thread(
        target=lambda: responses.append(
            _accept(root, item, event_id, "held native delivery", 30.0)
        ),
        daemon=True,
    )
    try:
        _bootstrap(root, item)
        sender.start()
        assert rpc_entered.wait(10.0)
        started = time.monotonic()
        peeked = request_socket(
            path,
            {"schema_version": 1, "operation": "peek", "generation": item.generation},
            timeout=5.0,
        )
        assert peeked["status"] == "PEEKED"
        # A plain native delivery rides the provider queue, not the courier's
        # pending list: the held event must never appear here, even once.
        assert peeked["messages"] == []
        health = request_socket(
            path,
            {"schema_version": 1, "operation": "health", "generation": item.generation},
            timeout=5.0,
        )
        assert health["status"] == "READY"
        stopped = request_socket(
            path,
            {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
            timeout=5.0,
        )
        assert stopped == {"schema_version": 1, "status": "STOPPED"}
        # All three control-plane answers arrive while the RPC is still held.
        assert time.monotonic() - started < 1.0
        assert not release_rpc.is_set()
    finally:
        release_rpc.set()
        sender.join(timeout=15.0)
        worker.join(timeout=30.0)
    assert not worker.is_alive()
    # Exactly one native effect ran, for exactly the held event.
    assert rpc_calls == [event_id]
    assert responses == [
        {
            "schema_version": 1,
            "event_id": event_id,
            "status": "TRANSPORT_ACCEPTED",
            "to": item.alias,
            "provider": "codex",
        }
    ]


def test_local_send_deadline_dying_in_the_retry_pause_ends_decided(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recompute that finds no room after the pause still marks the row."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    source = _source_route(tmp_path)
    Registry(root).upsert(source)
    target = runtime.Target(
        alias="claude@studio:project:API A",
        provider="claude",
        device="studio",
        project="project",
        generation=item.generation,
        session_key=session_key(item.provider, item.session_id),
        remote=False,
        session_id=item.session_id,
        cwd=item.cwd,
        pid=item.pid,
    )
    calls: list[dict[str, object]] = []

    def busy(_path: Path, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append(payload)
        return {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "PRE_EFFECT_REJECTED",
            "provider": "claude",
            "error": runtime.COURIER_BUSY_PRE_EFFECT_REASON,
        }

    monkeypatch.setattr(runtime, "request_socket", busy)

    def burning_pause(_deadline: float, delay: float) -> float | None:
        # The sleep itself spends the rest of this send's deadline, so the
        # next attempt's budget recompute raises after the pause returned.
        time.sleep(0.6)
        return delay

    monkeypatch.setattr(runtime, "_pre_effect_retry_pause", burning_pause)

    with pytest.raises(ChatError) as caught:
        runtime._send_local_target(
            root,
            source,
            target,
            "retry with a dying deadline",
            deadline=time.monotonic() + 0.5,
        )

    assert str(caught.value) == (
        "recipient stayed busy with another delivery until the send "
        "deadline; nothing was delivered; send again"
    )
    # The deadlined retry never emitted a second frame, and the intent ends
    # decided rather than parked PENDING.
    assert len(calls) == 1
    assert [row.status for row in IntentStore(root).intents()] == ["PRE_EFFECT_REJECTED"]
