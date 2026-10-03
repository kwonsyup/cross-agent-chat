"""Regression coverage: a courier mid-accept must stay live to the control plane.

The field failure these tests name: while one sender's ``accept`` request was
being served, every other connection queued behind it until the caller's own
deadline died, so a perfectly live courier was reported to ``chat_peers`` and
to exact-token senders as "unavailable or changed".
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import (
    ChatError,
    Intent,
    IntentStore,
    Registry,
    Route,
    UnknownDeliveryError,
    session_key,
)
from cross_agent_chat.native_helper import (
    NATIVE_QUEUE_BINARY_ENV_VAR,
    NATIVE_QUEUE_ENV_VALUE,
    NATIVE_QUEUE_ENV_VAR,
    NativeDispatchStore,
    NativeHelperStore,
    native_helper_dispatch_hook_group,
)
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

    def counted(
        path: Path,
        payload: dict[str, object],
        *,
        timeout: float = runtime.SOCKET_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        if payload.get("operation") == "accept":
            attempts.append(str(payload["event_id"]))
        return real_request(path, payload, timeout=timeout)

    monkeypatch.setattr(runtime, "request_socket", counted)


def _wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert predicate()


def _assert_each_caller_body_delivered_once(
    routed: list[str], intents: list[Intent], caller_texts: dict[str, str]
) -> None:
    """Each caller's own text reaches the provider exactly once, digest-bound.

    ``routed`` is the list of bodies actually observed at the sendmessage
    boundary and ``caller_texts`` maps each source's session key to the exact
    message its caller passed in. The wrapped envelope puts the caller text
    last, so an exact suffix match identifies authorship; the stored intent's
    payload digest then binds that source's event to the bytes that were
    really delivered, and the event id appearing inside the same body rules
    out a body delivered under a different event's identity. A producer that
    duplicated one caller's body, dropped the other's, or wrapped content
    under the wrong event fails one of these three observations.
    """
    for source_key, text in caller_texts.items():
        rows = [row for row in intents if row.source_key == source_key]
        assert len(rows) == 1
        (row,) = rows
        matched = [body for body in routed if body.endswith(text)]
        assert len(matched) == 1
        (body,) = matched
        assert hashlib.sha256(body.encode()).hexdigest() == row.payload_digest
        assert row.event_id in body


def _probe_intent_row(source_key: str, event_id: str, payload_digest: str) -> Intent:
    return Intent(
        schema_version=1,
        event_id=event_id,
        source_key=source_key,
        source_generation=str(uuid4()),
        source_alias="codex@studio:probe:probe-a1",
        target_key="c" * 64,
        target_generation=str(uuid4()),
        payload_digest=payload_digest,
        status="TRANSPORT_ACCEPTED",
        timestamp="2026-10-03T00:00:00+00:00",
    )


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

    def authorize(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
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
    the held delivery finishes. The failure a ref-only ledger could not name:
    a producer that delivered source A's wrapped body twice while losing B's,
    or that wrapped B's content under A's event, still satisfies three
    ref-equal sends and two stored rows. The provider-boundary bodies
    themselves -- each ending in exactly one caller's own text and
    digest-bound to that source's stored event -- are the evidence.
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
    # The real provider boundary, held like _held_claude_provider but
    # recording the exact body handed to sendmessage -- not the body the
    # sender intended or the event the stored row describes.
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    monkeypatch.setattr(runtime, "discover_target_ref", lambda _name: "API A [ref]")
    accept_entered = threading.Event()
    release_accept = threading.Event()
    deliveries: list[tuple[str, str]] = []

    def held_sendmessage(ref: str, body: str, _executable: Path) -> None:
        deliveries.append((ref, body))
        accept_entered.set()
        release_accept.wait(timeout=30.0)

    monkeypatch.setattr(runtime, "sendmessage", held_sendmessage)
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
        # Three provider calls exactly: the held occupier's raw body first
        # (accept admission is serialized, so it can only be first), then one
        # wrapped body per caller -- no duplicate and no extra replay.
        assert len(deliveries) == 3
        assert [ref for ref, _body in deliveries] == ["API A [ref]"] * 3
        assert sum(body == "held delivery" for _ref, body in deliveries) == 1
        assert deliveries[0][1] == "held delivery"
        intents = IntentStore(root).intents()
        assert [row.status for row in intents] == [
            "TRANSPORT_ACCEPTED",
            "TRANSPORT_ACCEPTED",
        ]
        assert len({row.event_id for row in intents}) == 2
        assert len({row.source_key for row in intents}) == 2
        _assert_each_caller_body_delivered_once(
            [body for _ref, body in deliveries[1:]],
            intents,
            {
                session_key(source.provider, source.session_id): (f"concurrent delivery {index}")
                for index, source in enumerate(sources)
            },
        )
    finally:
        release_accept.set()
        _stop_courier(root, item, worker)


def test_a_replayed_body_cannot_pass_the_per_caller_delivery_ledger() -> None:
    """Probe: one caller's body replayed for the other's send is caught.

    The strengthened busy-target assertions only matter if the observation
    itself discriminates, so the matcher is exercised against a tampered
    delivery list -- A's wrapped body delivered twice while B's never
    reached the provider. The old ref-only ledger could not see that; the
    per-caller text and digest binding must refuse it, while the honest
    two-body list still passes so the probe is not a tautology.
    """
    key_a, key_b = "a" * 64, "b" * 64
    event_a, event_b = str(uuid4()), str(uuid4())
    body_a = f"envelope head {event_a}\n\nalpha caller text"
    body_b = f"envelope head {event_b}\n\nbeta caller text"
    intents = [
        _probe_intent_row(key_a, event_a, hashlib.sha256(body_a.encode()).hexdigest()),
        _probe_intent_row(key_b, event_b, hashlib.sha256(body_b.encode()).hexdigest()),
    ]
    callers = {key_a: "alpha caller text", key_b: "beta caller text"}
    _assert_each_caller_body_delivered_once([body_a, body_b], intents, callers)
    with pytest.raises(AssertionError):
        _assert_each_caller_body_delivered_once([body_a, body_a], intents, callers)


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
    monkeypatch.setenv(NATIVE_QUEUE_ENV_VAR, NATIVE_QUEUE_ENV_VALUE)
    monkeypatch.setenv(NATIVE_QUEUE_BINARY_ENV_VAR, sys.executable)
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


def _health(root: Path, item: Route, timeout: float) -> dict[str, object]:
    return request_socket(
        runtime.socket_path(root, item),
        {"schema_version": 1, "operation": "health", "generation": item.generation},
        timeout=timeout,
    )


def _held_claude_inventory(
    monkeypatch: pytest.MonkeyPatch, item: Route
) -> tuple[threading.Event, threading.Event, list[str]]:
    """Hold the first Claude inventory call open; every later call answers."""
    inventory_entered = threading.Event()
    release_inventory = threading.Event()
    calls: list[str] = []
    calls_lock = threading.Lock()

    def held_exact_agent(*_a: object, **_k: object) -> dict[str, object]:
        with calls_lock:
            calls.append(item.session_id)
            first = len(calls) == 1
        if first:
            inventory_entered.set()
            release_inventory.wait(timeout=30.0)
        return _agent_view(item)

    monkeypatch.setattr(runtime, "exact_agent", held_exact_agent)
    return inventory_entered, release_inventory, calls


def _codex_helper_route(tmp_path: Path, root: Path) -> Route:
    """One helper-lineage Codex route inside the fixture state root.

    ``is_helper_lineage`` binds the reserved directory name to the route's
    cwd, so a reserved binding is enough to place this courier on the helper
    dispatch path without faking an owner identity.
    """
    original_cwd = tmp_path / "helper-original"
    original_cwd.mkdir()
    profile = tmp_path / "helper-codex-home"
    profile.mkdir()
    original = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(original_cwd),
        pid=os.getpid(),
        owner_identity="a" * 64,
        profile_root=str(profile),
    )
    account = hashlib.sha256(b"fixture-account").hexdigest()
    binding, _nonce = NativeHelperStore(root).reserve(original, account)
    helper_cwd = tmp_path / binding.helper_directory
    helper_cwd.mkdir()
    helper = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(helper_cwd),
        pid=os.getpid(),
    )
    Registry(root).upsert(helper)
    return helper


def _registered_helper_courier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Route, Route]:
    """An original route with a registered helper on real guarded state.

    Everything ``runtime.native_dispatch`` checks is exercised for real: the
    binding comes from ``reserve``/``register``, the owner identity from the
    real kernel read of this process, the hook readiness from real
    trusted-hash profile files, and the dispatch claim from the durable
    store. Only the account-digest subprocess boundary is stubbed.
    """
    root = tmp_path / "state"
    profile = tmp_path / "dispatch-codex-home"
    profile.mkdir()
    owner, _binary = runtime.recipient_owner_identity("codex", os.getpid(), str(profile))
    original_cwd = tmp_path / "dispatch-original"
    original_cwd.mkdir()
    original = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(original_cwd),
        pid=os.getpid(),
        owner_identity=owner,
        profile_root=str(profile),
    )
    Registry(root).upsert(original)
    store = NativeHelperStore(root)
    account = hashlib.sha256(b"fixture-account").hexdigest()
    binding, nonce = store.reserve(original, account)
    helper_cwd = tmp_path / binding.helper_directory
    helper_cwd.mkdir()
    helper = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(helper_cwd),
        pid=os.getpid(),
        owner_identity=owner,
        profile_root=str(profile),
    )
    Registry(root).upsert(helper)
    store.register(helper, nonce, original, account)
    group = native_helper_dispatch_hook_group()
    hooks_path = profile / "hooks.json"
    hooks_path.write_text(json.dumps({"hooks": {"PostToolUse": [group]}}), encoding="utf-8")
    trusted = runtime._native_hook_hash("post_tool_use", group)
    (profile / "config.toml").write_text(
        "[features]\n"
        "hooks = true\n\n"
        "[hooks.state]\n"
        f'"{hooks_path.resolve()}:post_tool_use:0:0" = {{ trusted_hash = "{trusted}" }}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "_native_account_digest", lambda *_args: account)
    return root, original, helper


def test_fixture_boundary_blocks_real_provider_subprocesses() -> None:
    """The suite's own guard: no test may reach a real provider binary."""
    with pytest.raises(AssertionError):
        subprocess.run(["claude", "agents", "--json"], capture_output=True, timeout=5.0)
    with pytest.raises(AssertionError):
        subprocess.Popen(["codex", "app-server", "--listen", "stdio://"])


def test_held_claude_inventory_does_not_serialize_controls_or_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow provider inventory holds one worker seat, never the listener.

    While the first ``claude agents`` read is deterministically held, a second
    health request must spend its own real inventory call, a real accept must
    still reach the provider exactly once, and shutdown must still answer. A
    serialized listener would leave all three inside the provider timeout; a
    guessed READY would never spend the second inventory call.
    """
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    inventory_entered, release_inventory, inventory_calls = _held_claude_inventory(
        monkeypatch, item
    )
    monkeypatch.setattr(runtime, "discover_target_ref", lambda _name: "API A [ref]")
    send_calls: list[str] = []
    monkeypatch.setattr(runtime, "sendmessage", lambda _r, _m, _e: send_calls.append(_r))
    worker = _start_courier(root, item)
    first_health: list[dict[str, object]] = []
    held_probe = threading.Thread(
        target=lambda: first_health.append(_health(root, item, 30.0)),
        daemon=True,
    )
    try:
        _bootstrap(root, item)
        held_probe.start()
        assert inventory_entered.wait(10.0)
        # The inventory read is provably in flight, so no response can have
        # been answered from a cache or a guess.
        assert first_health == []
        second = _health(root, item, 5.0)
        assert second["status"] == "READY"
        # Health spent its own real inventory call while the first was held.
        assert len(inventory_calls) == 2
        accepted = _accept(root, item, str(uuid4()), "delivery beside held health", 10.0)
        assert accepted["status"] == "TRANSPORT_ACCEPTED"
        assert send_calls == ["API A [ref]"]
        # The accept re-read the exact agent twice at the effect boundary; a
        # cached identity would leave this count at 2.
        assert len(inventory_calls) == 4
        stopped = request_socket(
            runtime.socket_path(root, item),
            {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
            timeout=5.0,
        )
        assert stopped == {"schema_version": 1, "status": "STOPPED"}
        assert not release_inventory.is_set()
    finally:
        release_inventory.set()
        if held_probe.ident is not None:
            held_probe.join(timeout=15.0)
        worker.join(timeout=30.0)
    # Draining let the held read finish honestly: the first probe's READY
    # arrived only after its inventory call returned.
    assert first_health[0]["status"] == "READY"
    assert not worker.is_alive()


def test_a_partial_frame_does_not_serialize_the_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trickled request body occupies one seat, not the accept loop."""
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    worker = _start_courier(root, item)
    partial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        _bootstrap(root, item)
        partial.connect(str(runtime.socket_path(root, item)))
        partial.sendall(b'{"schema_version": 1, "operation": "heal')
        started = time.monotonic()
        health = _health(root, item, 5.0)
        assert health["status"] == "READY"
        stopped = request_socket(
            runtime.socket_path(root, item),
            {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
            timeout=5.0,
        )
        assert stopped == {"schema_version": 1, "status": "STOPPED"}
        # Both controls answered while the incomplete frame was still open,
        # well inside the per-connection frame deadline that reclaims its seat.
        assert time.monotonic() - started < 4.0
    finally:
        partial.close()
        worker.join(timeout=30.0)
    assert not worker.is_alive()


def _partial_accept(
    root: Path, item: Route, event_id: str, message: str
) -> tuple[socket.socket, bytes]:
    """Open a courier connection holding all but the tail of an accept frame."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(30.0)
    connection.connect(str(runtime.socket_path(root, item)))
    frame = (
        json.dumps(
            {
                "schema_version": 1,
                "operation": "accept",
                "generation": item.generation,
                "event_id": event_id,
                "message": message,
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    connection.sendall(frame[:-4])
    return connection, frame[-4:]


def _read_stopped_refusal(connection: socket.socket, event_id: str) -> dict[str, object]:
    raw: dict[str, object] = json.loads(runtime.read_frame(connection))
    assert raw == {
        "schema_version": 1,
        "event_id": event_id,
        "status": "PRE_EFFECT_REJECTED",
        "provider": "claude",
        "error": runtime.COURIER_STOPPED_PRE_EFFECT_REASON,
    }
    return raw


def test_an_incomplete_accept_cannot_start_an_effect_after_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frame completed after STOPPED stays pre-effect, never an old effect.

    The worker holding the unfinished frame drains during shutdown, but
    effect admission rechecks the stop first, so the finished accept is
    answered with a decided pre-effect refusal and the provider is never
    reached. Before the recheck this sequence ran ``sendmessage`` after the
    courier had already acknowledged its stop.
    """
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    monkeypatch.setattr(runtime, "discover_target_ref", lambda _name: "API A [ref]")
    send_calls: list[str] = []
    monkeypatch.setattr(runtime, "sendmessage", lambda _r, _m, _e: send_calls.append(_r))
    worker = _start_courier(root, item)
    event_id = str(uuid4())
    partial, tail = _partial_accept(root, item, event_id, "stale frame delivery")
    try:
        _bootstrap(root, item)
        stopped = request_socket(
            runtime.socket_path(root, item),
            {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
            timeout=5.0,
        )
        assert stopped == {"schema_version": 1, "status": "STOPPED"}
        # The frame read only now finishes, already after the acknowledged
        # stop; the decided refusal is honest because nothing was admitted.
        partial.sendall(tail)
        _read_stopped_refusal(partial, event_id)
        assert send_calls == []
    finally:
        partial.close()
        worker.join(timeout=30.0)
    assert not worker.is_alive()


def test_a_frame_completed_while_stopped_is_being_emitted_stays_pre_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observed STOPPED must never coexist with a still-admitting courier.

    The emitter is wrapped so that after the real STOPPED bytes are written
    it parks on a barrier before returning. On the old ordering the stop
    flag was published only after that emit returned, so a caller holding a
    STOPPED response while the emitter was held could race a held accept
    frame into ``sendmessage`` -- a new effect after acknowledgement, not
    admitted-before-stop drain. Publishing the flag first makes the decided
    refusal below unconditional; zero provider calls is the repair evidence.
    """
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    monkeypatch.setattr(runtime, "discover_target_ref", lambda _name: "API A [ref]")
    send_calls: list[str] = []
    monkeypatch.setattr(runtime, "sendmessage", lambda _r, _m, _e: send_calls.append(_r))
    emitter_held = threading.Event()
    release_emitter = threading.Event()
    real_emit_safely = runtime.emit_frame_safely

    def held_emit(connection: socket.socket, payload: dict[str, object]) -> None:
        if payload.get("status") == "STOPPED":
            real_emit_safely(connection, payload)
            emitter_held.set()
            release_emitter.wait(timeout=30.0)
        else:
            real_emit_safely(connection, payload)

    monkeypatch.setattr(runtime, "emit_frame_safely", held_emit)
    worker = _start_courier(root, item)
    event_id = str(uuid4())
    partial, tail = _partial_accept(root, item, event_id, "post-ack delivery")
    shutdown = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    shutdown.settimeout(30.0)
    shutdown.connect(str(runtime.socket_path(root, item)))
    try:
        _bootstrap(root, item)
        shutdown.sendall(
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "operation": "shutdown",
                        "generation": item.generation,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        )
        stopped: dict[str, object] = json.loads(runtime.read_frame(shutdown))
        assert stopped == {"schema_version": 1, "status": "STOPPED"}
        # The caller has observed STOPPED while the emitter is provably still
        # parked; under the old order the stop flag is still unpublished here.
        assert emitter_held.wait(timeout=5.0)
        partial.sendall(tail)
        _read_stopped_refusal(partial, event_id)
        assert send_calls == []
    finally:
        release_emitter.set()
        partial.close()
        shutdown.close()
        worker.join(timeout=30.0)
    assert not worker.is_alive()


def test_an_incomplete_accept_cannot_start_an_effect_after_route_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A frame completed after generation replacement stays pre-effect too.

    The request still names the old generation, so only the admission-time
    route recheck stands between it and a delivery into a rotated session;
    the refused response and the zero provider effects are the repair.
    """
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    monkeypatch.setattr(runtime, "exact_agent", lambda *_a, **_k: _agent_view(item))
    monkeypatch.setattr(runtime, "discover_target_ref", lambda _name: "API A [ref]")
    send_calls: list[str] = []
    monkeypatch.setattr(runtime, "sendmessage", lambda _r, _m, _e: send_calls.append(_r))
    worker = _start_courier(root, item)
    event_id = str(uuid4())
    partial, tail = _partial_accept(root, item, event_id, "stale frame delivery")
    try:
        _bootstrap(root, item)
        replacement = Route.create(
            provider="claude",
            session_id=item.session_id,
            device="studio",
            cwd=item.cwd,
            pid=item.pid,
        )
        Registry(root).upsert(replacement)
        partial.sendall(tail)
        _read_stopped_refusal(partial, event_id)
        assert send_calls == []
    finally:
        partial.close()
        worker.join(timeout=30.0)
    assert not worker.is_alive()


def test_a_guarded_dispatch_handoff_makes_a_late_notice_refusal_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimed body makes a definitive notice failure UNKNOWN, never replayable.

    The real ``runtime.native_dispatch`` producer -- hook, binding, owner,
    account, and durable-claim guards all live -- hands the staged body to the
    trusted helper path while the queue notice RPC is still held. When that
    notice then fails definitively, the body may already have reached the
    bound original thread, so the accept cannot claim no effect: a reported
    PRE_EFFECT_REJECTED here is the misclassification this test names.
    """
    root, original, item = _registered_helper_courier(tmp_path, monkeypatch)
    monkeypatch.setenv(NATIVE_QUEUE_ENV_VAR, NATIVE_QUEUE_ENV_VALUE)
    monkeypatch.setenv(NATIVE_QUEUE_BINARY_ENV_VAR, sys.executable)
    rpc_entered = threading.Event()
    release_rpc = threading.Event()
    rpc_calls: list[str] = []

    def held_queue_native_input(**kwargs: object) -> None:
        rpc_calls.append(str(kwargs["event_id"]))
        rpc_entered.set()
        release_rpc.wait(timeout=30.0)
        raise ChatError("Codex native queue rejected the message before acceptance")

    monkeypatch.setattr("cross_agent_chat.codex.queue_native_input", held_queue_native_input)
    worker = _start_courier(root, item)
    event_id = str(uuid4())
    responses: list[dict[str, object]] = []
    sender = threading.Thread(
        target=lambda: responses.append(
            _accept(root, item, event_id, "held helper delivery", 30.0)
        ),
        daemon=True,
    )
    try:
        _bootstrap(root, item)
        sender.start()
        assert rpc_entered.wait(10.0)
        # The whole guarded path runs while the notice is still in flight:
        # real socket dispatch, durable UNKNOWN claim, real socket ack.
        dispatched = runtime.native_dispatch(root, item, event_id)
        assert dispatched["_meta"] == {
            "native_args": {
                "threadId": original.session_id,
                "prompt": "held helper delivery",
            }
        }
        claims = NativeDispatchStore(root).dispatches()
        assert len(claims) == 1
        assert claims[0].event_id == event_id
        assert claims[0].state == "UNKNOWN"
        assert claims[0].payload_sha256 == hashlib.sha256(b"held helper delivery").hexdigest()
        assert "held helper delivery" not in (root / "native-dispatches.json").read_text(
            encoding="utf-8"
        )
        peeked = request_socket(
            runtime.socket_path(root, item),
            {"schema_version": 1, "operation": "peek", "generation": item.generation},
            timeout=5.0,
        )
        assert peeked["messages"] == []
        # The claim and ack completed while the provider notice was still held.
        assert not release_rpc.is_set()
        release_rpc.set()
        sender.join(timeout=15.0)
        # The body may already have effected through the bound native path, so
        # the notice's definitive refusal is uncertain -- never a retryable
        # no-effect answer, and no automatic second dispatch exists.
        assert responses == [
            {
                "schema_version": 1,
                "event_id": event_id,
                "status": "UNKNOWN_DELIVERY",
                "provider": "codex",
            }
        ]
        assert rpc_calls == [event_id]
        with pytest.raises(ChatError, match="native helper dispatch is unavailable"):
            runtime.native_dispatch(root, item, event_id)
    finally:
        release_rpc.set()
        if sender.ident is not None:
            sender.join(timeout=15.0)
        _stop_courier(root, item, worker)


def test_a_definitive_notice_refusal_without_handoff_stays_pre_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body that never left is the only safely retracted rollback.

    With no dispatch while the notice is held, the definitive refusal proves
    no effect: the accept stays PRE_EFFECT_REJECTED, the rollback removes the
    staged body atomically, and no later dispatch can claim it.
    """
    root = tmp_path / "state"
    item = _codex_helper_route(tmp_path, root)
    monkeypatch.setenv(NATIVE_QUEUE_ENV_VAR, NATIVE_QUEUE_ENV_VALUE)
    monkeypatch.setenv(NATIVE_QUEUE_BINARY_ENV_VAR, sys.executable)
    rpc_entered = threading.Event()
    release_rpc = threading.Event()
    rpc_calls: list[str] = []

    def held_queue_native_input(**kwargs: object) -> None:
        rpc_calls.append(str(kwargs["event_id"]))
        rpc_entered.set()
        release_rpc.wait(timeout=30.0)
        raise ChatError("Codex native queue rejected the message before acceptance")

    monkeypatch.setattr("cross_agent_chat.codex.queue_native_input", held_queue_native_input)
    worker = _start_courier(root, item)
    event_id = str(uuid4())
    responses: list[dict[str, object]] = []
    sender = threading.Thread(
        target=lambda: responses.append(
            _accept(root, item, event_id, "refused helper delivery", 30.0)
        ),
        daemon=True,
    )
    try:
        _bootstrap(root, item)
        sender.start()
        assert rpc_entered.wait(10.0)
        release_rpc.set()
        sender.join(timeout=15.0)
        assert responses == [
            {
                "schema_version": 1,
                "event_id": event_id,
                "status": "PRE_EFFECT_REJECTED",
                "provider": "codex",
                "error": "Codex native queue rejected the message before acceptance",
            }
        ]
        # The never-handed-off body was retracted: no peek, no later dispatch.
        assert (
            request_socket(
                runtime.socket_path(root, item),
                {"schema_version": 1, "operation": "peek", "generation": item.generation},
                timeout=5.0,
            )["messages"]
            == []
        )
        assert (
            request_socket(
                runtime.socket_path(root, item),
                {
                    "schema_version": 1,
                    "operation": "native_dispatch",
                    "generation": item.generation,
                    "event_id": event_id,
                },
                timeout=5.0,
            )["status"]
            == "UNAVAILABLE"
        )
        assert rpc_calls == [event_id]
    finally:
        release_rpc.set()
        if sender.ident is not None:
            sender.join(timeout=15.0)
        _stop_courier(root, item, worker)


def test_a_dispatch_racing_rollback_lands_on_one_coherent_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handoff check and retraction share one lock: no half-admitted end.

    If the guarded dispatch takes the queue lock first, the durable claim is
    possible-effect custody and the accept must end UNKNOWN; if the rollback
    runs first, the dispatch finds no body and the accept stays
    PRE_EFFECT_REJECTED. A non-atomic check could land a third, wrong state:
    a rejected accept beside a completed handoff, or a resurrected body.
    """
    root, original, item = _registered_helper_courier(tmp_path, monkeypatch)
    del original
    monkeypatch.setenv(NATIVE_QUEUE_ENV_VAR, NATIVE_QUEUE_ENV_VALUE)
    monkeypatch.setenv(NATIVE_QUEUE_BINARY_ENV_VAR, sys.executable)
    rpc_entered = threading.Event()
    release_rpc = threading.Event()

    def held_queue_native_input(**_kwargs: object) -> None:
        rpc_entered.set()
        release_rpc.wait(timeout=30.0)
        raise ChatError("Codex native queue rejected the message before acceptance")

    monkeypatch.setattr("cross_agent_chat.codex.queue_native_input", held_queue_native_input)
    worker = _start_courier(root, item)
    event_id = str(uuid4())
    responses: list[dict[str, object]] = []
    sender = threading.Thread(
        target=lambda: responses.append(
            _accept(root, item, event_id, "raced helper delivery", 30.0)
        ),
        daemon=True,
    )
    dispatch_result: list[dict[str, object]] = []
    dispatch_errors: list[BaseException] = []
    try:
        _bootstrap(root, item)
        sender.start()
        assert rpc_entered.wait(10.0)

        def dispatch() -> None:
            try:
                dispatch_result.append(runtime.native_dispatch(root, item, event_id))
            except BaseException as error:
                dispatch_errors.append(error)

        dispatcher = threading.Thread(target=dispatch, daemon=True)
        dispatcher.start()
        # Releasing the notice and starting the claim concurrently puts the
        # dispatch's lock acquisition and the rollback's lock acquisition in
        # real contention; whichever wins decides the whole outcome.
        release_rpc.set()
        dispatcher.join(timeout=15.0)
        sender.join(timeout=15.0)
        claims = NativeDispatchStore(root).dispatches()
        if dispatch_result:
            # The dispatch's handoff won: the claimed body may have effected,
            # so a PRE_EFFECT_REJECTED answer would have been a lie.
            assert responses[0]["status"] == "UNKNOWN_DELIVERY"
            assert len(claims) == 1
            assert claims[0].state == "UNKNOWN"
        else:
            # The rollback won: nothing ever left, so the decided refusal and
            # the empty queue are the honest, coherent end state.
            assert dispatch_errors
            assert responses[0]["status"] == "PRE_EFFECT_REJECTED"
            assert claims == []
            assert (
                request_socket(
                    runtime.socket_path(root, item),
                    {
                        "schema_version": 1,
                        "operation": "peek",
                        "generation": item.generation,
                    },
                    timeout=5.0,
                )["messages"]
                == []
            )
    finally:
        release_rpc.set()
        if sender.ident is not None:
            sender.join(timeout=15.0)
        _stop_courier(root, item, worker)


def test_unknown_native_notification_keeps_the_unclaimed_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncertain notification outcome preserves the pending body.

    The provider may have queued the notification, so the unacknowledged body
    stays claimable rather than being rolled back into disappearance.
    """
    root = tmp_path / "state"
    item = _codex_helper_route(tmp_path, root)
    monkeypatch.setenv(NATIVE_QUEUE_ENV_VAR, NATIVE_QUEUE_ENV_VALUE)
    monkeypatch.setenv(NATIVE_QUEUE_BINARY_ENV_VAR, sys.executable)
    rpc_entered = threading.Event()
    release_rpc = threading.Event()
    rpc_calls: list[str] = []

    def held_queue_native_input(**kwargs: object) -> None:
        rpc_calls.append(str(kwargs["event_id"]))
        rpc_entered.set()
        release_rpc.wait(timeout=30.0)
        raise UnknownDeliveryError("Codex native queue outcome is unknown")

    monkeypatch.setattr("cross_agent_chat.codex.queue_native_input", held_queue_native_input)
    worker = _start_courier(root, item)
    path = runtime.socket_path(root, item)
    event_id = str(uuid4())
    responses: list[dict[str, object]] = []
    sender = threading.Thread(
        target=lambda: responses.append(
            _accept(root, item, event_id, "uncertain helper delivery", 30.0)
        ),
        daemon=True,
    )
    try:
        _bootstrap(root, item)
        sender.start()
        assert rpc_entered.wait(10.0)
        release_rpc.set()
        sender.join(timeout=15.0)
        assert responses == [
            {
                "schema_version": 1,
                "event_id": event_id,
                "status": "UNKNOWN_DELIVERY",
                "provider": "codex",
            }
        ]
        peeked = request_socket(
            path,
            {"schema_version": 1, "operation": "peek", "generation": item.generation},
            timeout=5.0,
        )
        assert peeked["messages"] == [
            {"event_id": event_id, "message": "uncertain helper delivery"}
        ]
        # The retained body remains claimable through the normal dispatch path.
        dispatched = request_socket(
            path,
            {
                "schema_version": 1,
                "operation": "native_dispatch",
                "generation": item.generation,
                "event_id": event_id,
            },
            timeout=5.0,
        )
        assert dispatched["status"] == "NATIVE_DISPATCH"
        assert dispatched["message"] == "uncertain helper delivery"
        assert rpc_calls == [event_id]
    finally:
        release_rpc.set()
        if sender.ident is not None:
            sender.join(timeout=15.0)
        _stop_courier(root, item, worker)


def test_a_saturated_courier_refuses_an_overflow_accept_with_decided_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrency is bounded; an overflowed accept gets a no-effect busy.

    Every connection seat is provably held inside the provider inventory, so
    one more health probe is refused rather than serialized, and a well-formed
    accept is read and answered with the decided busy rejection its sender can
    safely retry -- never dropped into an unknown outcome and never given an
    effect.
    """
    item = _route(tmp_path)
    root = tmp_path / "state"
    Registry(root).upsert(item)
    gate = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def held_inventory(*_a: object, **_k: object) -> dict[str, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
        gate.wait(timeout=30.0)
        return _agent_view(item)

    monkeypatch.setattr(runtime, "exact_agent", held_inventory)
    send_calls: list[str] = []
    monkeypatch.setattr(runtime, "sendmessage", lambda _r, _m, _e: send_calls.append(_r))
    worker = _start_courier(root, item)
    probes = 20
    probe_results: list[dict[str, object]] = []
    probe_errors: list[ChatError] = []
    start = threading.Barrier(probes)

    def probe() -> None:
        start.wait(timeout=10.0)
        try:
            probe_results.append(_health(root, item, 30.0))
        except ChatError as error:
            probe_errors.append(error)

    probe_threads = [threading.Thread(target=probe, daemon=True) for _ in range(probes)]
    try:
        _bootstrap(root, item)
        for probe_thread in probe_threads:
            probe_thread.start()
        # Every worker seat is provably occupied inside the provider read;
        # the refused remainder never reaches the inventory at all, so the
        # count staying at the limit is the bound itself.
        _wait_until(lambda: calls >= runtime.COURIER_CONNECTION_LIMIT)
        assert calls == runtime.COURIER_CONNECTION_LIMIT
        event_id = str(uuid4())
        rejected = _accept(root, item, event_id, "overflow delivery", 10.0)
        assert rejected == {
            "schema_version": 1,
            "event_id": event_id,
            "status": "PRE_EFFECT_REJECTED",
            "provider": "claude",
            "error": runtime.COURIER_BUSY_PRE_EFFECT_REASON,
        }
        # No provider effect ran for the refused event.
        assert send_calls == []
    finally:
        gate.set()
        for probe_thread in probe_threads:
            if probe_thread.ident is not None:
                probe_thread.join(timeout=15.0)
        _stop_courier(root, item, worker)
    # The seated probes all answered READY; the overflow was refused, not
    # silently admitted beyond the bound.
    assert [probe.get("status") for probe in probe_results] == ["READY"] * (
        runtime.COURIER_CONNECTION_LIMIT
    )
    assert len(probe_errors) == probes - runtime.COURIER_CONNECTION_LIMIT
