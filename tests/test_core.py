from __future__ import annotations

import io
import json
import os
import threading
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat.core import (
    ChatError,
    IntentStore,
    Registry,
    Route,
    UnknownDeliveryError,
    atomic_json,
    authenticate_sender,
    bounded_message,
    resolve_target,
    session_key,
)
from cross_agent_chat.runtime import (
    HEALTH_TIMEOUT_SECONDS,
    LOCAL_DISCOVERY_TIMEOUT_SECONDS,
    MCP_TOOL_TIMEOUT_SECONDS,
    OPERATION_TIMEOUT_SECONDS,
    REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    REMOTE_TIMEOUT_SECONDS,
    Target,
    _local_target,
    canonical_source_alias,
    courier_health,
    courier_server,
    local_targets,
    pre_effect_error,
    presence_is_enabled,
    request_socket,
    send,
    send_local,
    socket_path,
)


def route(
    tmp_path: Path,
    *,
    provider: str = "codex",
    pid: int = 1200,
    project: str = "project",
    device: str = "studio",
    session_id: str | None = None,
) -> Route:
    cwd = tmp_path / project
    cwd.mkdir(exist_ok=True)
    return Route.create(
        provider=provider,
        session_id=session_id or str(uuid4()),
        device=device,
        cwd=str(cwd),
        pid=pid,
    )


def test_codex_alias_distinguishes_sessions_in_one_project(tmp_path: Path) -> None:
    first = route(tmp_path)
    second = route(tmp_path)

    assert first.alias != second.alias
    assert first.alias.startswith("codex@studio:project:")


def test_claude_alias_uses_provider_device_and_project(tmp_path: Path) -> None:
    item = route(tmp_path, provider="claude")

    assert item.alias == "claude@studio:project"


def test_live_claude_sender_alias_includes_exact_agent_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    item = route(tmp_path, provider="claude", pid=os.getpid())
    Registry(root).upsert(item)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.exact_agent",
        lambda *_: {
            "session_id": item.session_id,
            "name": "API A",
            "kind": "interactive",
            "cwd": item.cwd,
        },
    )

    assert canonical_source_alias(root, item) == "claude@studio:project:API A"


def test_registry_generation_replacement_invalidates_old_route(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state")
    first = route(tmp_path)
    replacement = Route.create(
        provider=first.provider,
        session_id=first.session_id,
        device=first.device,
        cwd=first.cwd,
        pid=first.pid,
    )

    registry.upsert(first)
    registry.upsert(replacement)

    assert registry.current(first) is False
    assert registry.current(replacement) is True
    assert registry.routes() == [replacement]


def test_registry_rejects_non_private_state_file(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state")
    item = route(tmp_path)
    registry.upsert(item)
    registry.path.chmod(0o644)

    with pytest.raises(ChatError, match="private"):
        registry.routes()


def test_registry_schema_is_strict(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "state")
    item = route(tmp_path)
    registry.upsert(item)
    raw = json.loads(registry.path.read_text())
    raw[0]["message"] = "must never persist"
    registry.path.write_text(json.dumps(raw))
    registry.path.chmod(0o600)

    with pytest.raises(ChatError, match="schema"):
        registry.routes()


def test_route_liveness_compacts_only_definitively_missing_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = route(tmp_path)
    monkeypatch.setattr("cross_agent_chat.core.os.kill", lambda *_args: None)
    assert item.process_is_live()
    monkeypatch.setattr(
        "cross_agent_chat.core.os.kill",
        lambda *_args: (_ for _ in ()).throw(PermissionError()),
    )
    assert item.process_is_live()
    monkeypatch.setattr(
        "cross_agent_chat.core.os.kill",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert not item.process_is_live()


def test_removed_live_workspace_is_excluded_without_poisoning_a_healthy_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    removed = route(tmp_path, pid=os.getpid(), project="removed")
    healthy = route(tmp_path, pid=os.getpid(), project="healthy")
    Registry(root).upsert(removed)
    Registry(root).upsert(healthy)
    Path(removed.cwd).rmdir()
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "status": "READY",
            "generation": healthy.generation,
            "alias": healthy.alias,
        },
    )

    assert Registry(root).routes() == [removed, healthy]
    assert [target.alias for target in local_targets(root)] == [healthy.alias]


def test_authenticate_claude_sender_requires_unique_parent_pid(tmp_path: Path) -> None:
    first = route(tmp_path, provider="claude", pid=1400, project="one")
    second = route(tmp_path, provider="claude", pid=1400, project="two")

    with pytest.raises(ChatError, match="exact Claude sender"):
        authenticate_sender([first, second], "claude", 1400, None)


def test_authenticate_codex_sender_requires_host_thread_identity(tmp_path: Path) -> None:
    item = route(tmp_path, provider="codex", pid=1500)

    with pytest.raises(ChatError, match="host thread identity"):
        authenticate_sender([item], "codex", 1500, None)


def test_authenticate_codex_sender_binds_thread_and_parent_pid(tmp_path: Path) -> None:
    item = route(tmp_path, provider="codex", pid=1500)

    assert authenticate_sender([item], "codex", 1500, item.session_id) == item
    with pytest.raises(ChatError, match="exact Codex sender"):
        authenticate_sender([item], "codex", 1501, item.session_id)


def test_target_resolution_rejects_ambiguity(tmp_path: Path) -> None:
    first = route(tmp_path, project="api")
    second = route(tmp_path, project="api", device="laptop")

    with pytest.raises(ChatError, match="ambiguous"):
        resolve_target([first, second], "api")


def test_target_resolution_accepts_exact_alias(tmp_path: Path) -> None:
    first = route(tmp_path, project="api")
    second = route(tmp_path, project="web")

    assert resolve_target([first, second], second.alias) == second


def test_unicode_project_aliases_preserve_identity_without_ascii_filtering(tmp_path: Path) -> None:
    first = route(tmp_path, project="클루로")
    second = route(tmp_path, project="클루로\u0301")

    assert first.project != second.project
    assert first.alias != second.alias
    assert resolve_target([first], "클루로") == first


def test_long_project_label_is_bounded_without_changing_route_identity(tmp_path: Path) -> None:
    item = route(tmp_path, project="p" * 110, device="device-with-a-long-name")

    assert item.project == "p" * 110
    assert len(item.alias) <= 128
    assert "~" in item.alias


def test_bounded_message_rejects_empty_and_oversized() -> None:
    with pytest.raises(ChatError, match="empty"):
        bounded_message("")
    with pytest.raises(ChatError, match="16 KiB"):
        bounded_message("x" * 16385)


def test_intent_store_never_persists_message_body(tmp_path: Path) -> None:
    store = IntentStore(tmp_path / "state")
    source = route(tmp_path, project="source")
    target = route(tmp_path)
    event_id = store.begin(
        source,
        target,
        source_alias=source.alias,
        payload_digest="a" * 64,
    )
    store.mark(event_id, "TRANSPORT_ACCEPTED")

    persisted = store.path.read_text()
    assert event_id in persisted
    assert "TRANSPORT_ACCEPTED" in persisted
    assert "message" not in persisted
    assert "body" not in persisted


def test_pending_or_unknown_intent_blocks_duplicate_send(tmp_path: Path) -> None:
    source = route(tmp_path, project="source")
    target = route(tmp_path)
    store = IntentStore(tmp_path / "state")
    store.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)

    with pytest.raises(ChatError, match="unresolved"):
        store.begin(source, target, source_alias=source.alias, payload_digest="b" * 64)

    other = IntentStore(tmp_path / "other-state")
    event_id = other.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)
    other.mark(event_id, "UNKNOWN_DELIVERY")
    with pytest.raises(ChatError, match="unresolved"):
        other.begin(source, target, source_alias=source.alias, payload_digest="b" * 64)

    other.mark(event_id, "RESOLVED_BY_OWNER")
    assert (
        other.begin(source, target, source_alias=source.alias, payload_digest="b" * 64) != event_id
    )


def test_pre_effect_rejection_does_not_block_fresh_intent(tmp_path: Path) -> None:
    source = route(tmp_path, project="source")
    target = route(tmp_path)
    store = IntentStore(tmp_path / "state")
    event_id = store.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)

    store.mark(event_id, "PRE_EFFECT_REJECTED")

    assert store.begin(source, target, source_alias=source.alias, payload_digest="b" * 64)


def test_configured_tool_deadline_covers_one_remote_discovery_and_delivery() -> None:
    assert OPERATION_TIMEOUT_SECONDS >= (
        LOCAL_DISCOVERY_TIMEOUT_SECONDS
        + REMOTE_DISCOVERY_TIMEOUT_SECONDS
        + REMOTE_TIMEOUT_SECONDS
        + 5
        + 15
    )
    assert MCP_TOOL_TIMEOUT_SECONDS >= OPERATION_TIMEOUT_SECONDS + 10


def test_local_send_does_not_wait_for_remote_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, project="source", pid=os.getpid())
    target = route(tmp_path, project="target", pid=os.getpid())
    Registry(root).upsert(source)
    Registry(root).upsert(target)
    local = Target(
        alias=target.alias,
        provider=target.provider,
        device=target.device,
        project=target.project,
        generation=target.generation,
        session_key="b" * 64,
        remote=False,
        session_id=target.session_id,
        cwd=target.cwd,
        pid=target.pid,
    )
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda _: [local])
    monkeypatch.setattr(
        "cross_agent_chat.runtime.remote_targets",
        lambda _: pytest.fail("local delivery must not wait for Tailnet discovery"),
    )
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda _path, payload, **_: {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": "codex",
        },
    )

    assert send(root, source, target.alias, "hello")["status"] == "TRANSPORT_ACCEPTED"


@pytest.mark.parametrize("health_timeout", [False, True])
def test_healthy_duplicate_registration_keeps_generation_and_pending_courier_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, health_timeout: bool
) -> None:
    from cross_agent_chat import runtime

    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "state"
    session_id = str(uuid4())
    identity, _ = runtime.recipient_owner_identity("codex", os.getpid())
    first = Route.create(
        provider="codex",
        session_id=session_id,
        device="studio",
        cwd=str(project),
        pid=os.getpid(),
        owner_identity=identity,
    )
    Registry(root).upsert(first)
    worker = threading.Thread(
        target=courier_server,
        kwargs={
            "provider": "codex",
            "state_root_value": str(root),
            "session_id": session_id,
            "cwd": str(project),
            "generation": first.generation,
            "pid": os.getpid(),
        },
        daemon=True,
    )
    worker.start()
    path = socket_path(root, first)
    deadline = time.monotonic() + 2
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()
    event_id = str(uuid4())
    request_socket(
        path,
        {
            "schema_version": 1,
            "operation": "accept",
            "generation": first.generation,
            "event_id": event_id,
            "message": "pending",
        },
    )
    hook = {"hook_event_name": "SessionStart", "session_id": session_id, "cwd": str(project)}
    monkeypatch.setattr(runtime, "hook_input", lambda _: hook)
    monkeypatch.setattr(
        runtime, "_spawn_courier", lambda *_: pytest.fail("duplicate spawned courier")
    )

    if health_timeout:

        def busy_health(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise UnknownDeliveryError("busy courier") from TimeoutError()

        monkeypatch.setattr(runtime, "request_socket", busy_health)
    try:
        repeated = runtime.register("codex", "studio", os.getpid(), str(root))
        assert repeated is not None and repeated.generation == first.generation
        peek = request_socket(
            path, {"schema_version": 1, "operation": "peek", "generation": first.generation}
        )
        assert peek["messages"] == [{"event_id": event_id, "message": "pending"}]
    finally:
        request_socket(
            path, {"schema_version": 1, "operation": "shutdown", "generation": first.generation}
        )
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_missing_courier_duplicate_registration_replaces_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "state"
    session_id = str(uuid4())
    identity, _ = runtime.recipient_owner_identity("codex", os.getpid())
    first = Route.create(
        provider="codex",
        session_id=session_id,
        device="studio",
        cwd=str(project),
        pid=os.getpid(),
        owner_identity=identity,
    )
    Registry(root).upsert(first)
    hook = {"hook_event_name": "SessionStart", "session_id": session_id, "cwd": str(project)}
    monkeypatch.setattr(runtime, "hook_input", lambda _: hook)
    spawned: list[Route] = []
    monkeypatch.setattr(runtime, "_spawn_courier", lambda _root, route: spawned.append(route))

    replacement = runtime.register("codex", "studio", os.getpid(), str(root))

    assert replacement is not None and replacement.generation != first.generation
    assert spawned == [replacement]


@pytest.mark.parametrize("owner_identity", ["a" * 64, "b" * 64])
def test_owner_identity_change_never_reuses_a_route(tmp_path: Path, owner_identity: str) -> None:
    registry = Registry(tmp_path / "state")
    first = route(tmp_path, pid=os.getpid())
    first = replace(first, owner_identity="0" * 64)
    replacement = Route.create(
        provider=first.provider,
        session_id=first.session_id,
        device=first.device,
        cwd=first.cwd,
        pid=first.pid,
        owner_identity=owner_identity,
    )
    registry.upsert(first)

    assert registry.upsert_or_reuse_live_owner(replacement) == replacement


def test_live_route_discovery_does_not_apply_an_age_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    live = replace(
        route(tmp_path, pid=os.getpid()),
        last_seen=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    Registry(root).upsert(live)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "status": "READY",
            "generation": live.generation,
            "alias": live.alias,
        },
    )

    assert [target.alias for target in local_targets(root)] == [live.alias]


def test_presence_parser_accepts_only_absent_empty_or_exact_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    assert presence_is_enabled()
    monkeypatch.setenv("CROSS_AGENT_CHAT_PRESENCE", "")
    assert presence_is_enabled()
    monkeypatch.setenv("CROSS_AGENT_CHAT_PRESENCE", "off")
    assert not presence_is_enabled()
    for value in ("OFF", " off", "on"):
        monkeypatch.setenv("CROSS_AGENT_CHAT_PRESENCE", value)
        with pytest.raises(ChatError, match="CROSS_AGENT_CHAT_PRESENCE must be empty or 'off'"):
            presence_is_enabled()


def test_presence_off_does_not_change_visible_sibling_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat.runtime import register

    root = tmp_path / "state"
    visible = route(tmp_path, pid=os.getpid(), project="visible")
    Registry(root).upsert(visible)
    monkeypatch.setenv("CROSS_AGENT_CHAT_PRESENCE", "off")

    assert register("codex", "studio", os.getpid(), str(root)) is None
    assert Registry(root).routes() == [visible]


def test_registration_compacts_dead_routes_and_preserves_live_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat.runtime import register

    root = tmp_path / "state"
    registry = Registry(root)
    live = route(tmp_path, pid=os.getpid(), project="native-app-server")
    stale = [
        route(tmp_path, pid=1002 + index, project=f"dead-worker-{index}") for index in range(7)
    ]
    atomic_json(registry.path, [live.to_dict(), *(item.to_dict() for item in stale)])
    monkeypatch.setattr(Route, "process_is_live", lambda item: item.pid == live.pid)
    monkeypatch.setattr("cross_agent_chat.runtime._spawn_courier", lambda *_args: None)
    hook = {
        "hook_event_name": "SessionStart",
        "session_id": str(uuid4()),
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hook)))

    registered = register("codex", "studio", os.getpid(), str(root))

    assert registered is not None
    assert registry.routes() == [live, registered]


def test_local_discovery_filters_dead_routes_without_state_lock_or_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    registry = Registry(root)
    live = route(tmp_path, pid=os.getpid(), project="native-app-server")
    stale = route(tmp_path, pid=1002, project="dead-worker")
    atomic_json(registry.path, [live.to_dict(), stale.to_dict()])
    before = registry.path.read_bytes()
    monkeypatch.setattr(Route, "process_is_live", lambda item: item.pid == live.pid)
    monkeypatch.setattr(
        "cross_agent_chat.core.state_lock",
        lambda *_args: (_ for _ in ()).throw(AssertionError("discovery must not lock state")),
    )
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "status": "READY",
            "generation": live.generation,
            "alias": live.alias,
        },
    )

    assert [target.alias for target in local_targets(root)] == [live.alias]
    assert registry.path.read_bytes() == before


def test_local_route_health_checks_run_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    routes = [
        route(tmp_path, pid=os.getpid(), project="one"),
        route(tmp_path, pid=os.getpid(), project="two"),
    ]
    for item in routes:
        Registry(root).upsert(item)
    by_generation = {item.generation: item for item in routes}
    rendezvous = threading.Barrier(2)

    def health(_path: Path, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        assert 0 < timeout <= HEALTH_TIMEOUT_SECONDS
        rendezvous.wait(timeout=5)
        item = by_generation[str(payload["generation"])]
        return {
            "schema_version": 1,
            "status": "READY",
            "generation": item.generation,
            "alias": item.alias,
        }

    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", health)

    assert [target.alias for target in local_targets(root)] == [item.alias for item in routes]


def test_local_route_health_checks_are_capped_at_thirty_two_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    routes = [route(tmp_path, pid=os.getpid(), project=f"route-{index}") for index in range(33)]
    for item in routes:
        Registry(root).upsert(item)
    observed_workers: list[int] = []

    def worker_pool(*, max_workers: int) -> ThreadPoolExecutor:
        observed_workers.append(max_workers)
        return ThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr("cross_agent_chat.runtime.ThreadPoolExecutor", worker_pool)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda _path, payload, **_kwargs: {
            "schema_version": 1,
            "status": "READY",
            "generation": payload["generation"],
            "alias": next(
                item.alias for item in routes if item.generation == payload["generation"]
            ),
        },
    )

    assert [target.alias for target in local_targets(root)] == [item.alias for item in routes]
    assert observed_workers == [32]


def test_local_discovery_deadline_matches_remote_broker_budget() -> None:
    assert REMOTE_DISCOVERY_TIMEOUT_SECONDS == LOCAL_DISCOVERY_TIMEOUT_SECONDS + 5.0


def test_local_discovery_deadline_cancels_queued_work_without_waiting_for_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    routes = [route(tmp_path, pid=os.getpid(), project=f"route-{index}") for index in range(33)]
    for item in routes:
        Registry(root).upsert(item)

    class TimedOutWorkers:
        def __init__(self, *, max_workers: int) -> None:
            assert max_workers == 32
            self.futures: list[Future[Target | None]] = []
            self.shutdown_calls: list[tuple[bool, bool]] = []
            workers.append(self)

        def submit(self, _fn: object, *_args: object) -> Future[Target | None]:
            future: Future[Target | None] = Future()
            self.futures.append(future)
            return future

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.shutdown_calls.append((wait, cancel_futures))

    workers: list[TimedOutWorkers] = []

    def expired(
        _futures: Iterable[Future[Target | None]], *, timeout: float | None = None
    ) -> Iterator[Future[Target | None]]:
        assert timeout is not None and 0 < timeout <= LOCAL_DISCOVERY_TIMEOUT_SECONDS
        raise FuturesTimeoutError()

    monkeypatch.setattr("cross_agent_chat.runtime.ThreadPoolExecutor", TimedOutWorkers)
    monkeypatch.setattr("cross_agent_chat.runtime.as_completed", expired)

    assert local_targets(root) == []
    assert len(workers) == 1
    assert len(workers[0].futures) == 33
    assert all(future.cancelled() for future in workers[0].futures)
    assert workers[0].shutdown_calls == [(False, True)]


def test_local_discovery_returns_completed_targets_before_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    routes = [route(tmp_path, pid=os.getpid(), project=f"route-{index}") for index in range(33)]
    for item in routes:
        Registry(root).upsert(item)
    first = routes[0]
    expected = Target(
        alias=first.alias,
        provider=first.provider,
        device=first.device,
        project=first.project,
        generation=first.generation,
        session_key="a" * 64,
        remote=False,
        session_id=first.session_id,
        cwd=first.cwd,
        pid=first.pid,
    )

    class PartialWorkers:
        def __init__(self, *, max_workers: int) -> None:
            assert max_workers == 32
            self.futures: list[Future[Target | None]] = []
            self.shutdown_calls: list[tuple[bool, bool]] = []
            workers.append(self)

        def submit(self, _fn: object, *_args: object) -> Future[Target | None]:
            future: Future[Target | None] = Future()
            if not self.futures:
                future.set_result(expected)
            self.futures.append(future)
            return future

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.shutdown_calls.append((wait, cancel_futures))

    workers: list[PartialWorkers] = []

    def completed_then_expired(
        futures: Iterable[Future[Target | None]], *, timeout: float | None = None
    ) -> Iterator[Future[Target | None]]:
        assert timeout is not None and 0 < timeout <= LOCAL_DISCOVERY_TIMEOUT_SECONDS
        first_future, *_ = futures
        yield first_future
        raise FuturesTimeoutError()

    monkeypatch.setattr("cross_agent_chat.runtime.ThreadPoolExecutor", PartialWorkers)
    monkeypatch.setattr("cross_agent_chat.runtime.as_completed", completed_then_expired)

    assert local_targets(root) == [expected]
    assert len(workers) == 1
    assert workers[0].shutdown_calls == [(False, True)]
    assert all(future.cancelled() for future in workers[0].futures[1:])


def test_courier_health_emits_exact_ready_contract(tmp_path: Path) -> None:
    item = route(tmp_path, pid=os.getpid())

    assert courier_health(item) == {
        "schema_version": 1,
        "status": "READY",
        "generation": item.generation,
        "alias": item.alias,
    }


def test_claude_courier_health_reports_current_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = route(tmp_path, provider="claude", pid=os.getpid())
    agent = {
        "session_id": item.session_id,
        "name": "Gate Health",
        "kind": "interactive",
        "cwd": item.cwd,
    }
    monkeypatch.setattr("cross_agent_chat.runtime.exact_agent", lambda *_: agent)

    assert courier_health(item) == {
        "schema_version": 1,
        "status": "READY",
        "generation": item.generation,
        "alias": f"claude@{item.device}:{item.project}:Gate Health",
    }


def test_claude_courier_health_reports_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = route(tmp_path, provider="claude", pid=os.getpid())
    monkeypatch.setattr(
        "cross_agent_chat.runtime.exact_agent",
        lambda *_: (_ for _ in ()).throw(ChatError("gone")),
    )

    assert courier_health(item) == {
        "schema_version": 1,
        "status": "UNAVAILABLE",
        "generation": item.generation,
    }


@pytest.mark.parametrize(
    "changed",
    [
        {"generation": str(uuid4())},
        {"schema_version": 2},
        {"status": "UNAVAILABLE"},
        {"alias": "invalid/alias"},
        {"alias": "codex@other:project:123456789abc"},
    ],
)
def test_invalid_local_health_response_drops_only_that_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: dict[str, object]
) -> None:
    root = tmp_path / "state"
    item = route(tmp_path, pid=os.getpid(), project="bad")
    healthy = route(tmp_path, pid=os.getpid(), project="healthy")
    Registry(root).upsert(item)
    Registry(root).upsert(healthy)
    response: dict[str, object] = {
        "schema_version": 1,
        "status": "READY",
        "generation": item.generation,
        "alias": item.alias,
    }
    response.update(changed)

    def health(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        if payload["generation"] == item.generation:
            return response
        return {
            "schema_version": 1,
            "status": "READY",
            "generation": healthy.generation,
            "alias": healthy.alias,
        }

    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", health)

    assert _local_target(root, item) is None
    assert [target.alias for target in local_targets(root)] == [healthy.alias]


def test_claude_health_alias_must_match_route_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    item = route(tmp_path, provider="claude", pid=os.getpid())
    Registry(root).upsert(item)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "status": "READY",
            "generation": item.generation,
            "alias": "claude@other-device:other-project:Gate Health",
        },
    )

    assert local_targets(root) == []


def test_local_pre_effect_response_closes_intent_and_allows_fresh_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, project="source", pid=os.getpid())
    target = route(tmp_path, project="target", pid=os.getpid())
    Registry(root).upsert(source)
    Registry(root).upsert(target)
    resolved = Target(
        alias=target.alias,
        provider=target.provider,
        device=target.device,
        project=target.project,
        generation=target.generation,
        session_key="b" * 64,
        remote=False,
        session_id=target.session_id,
        cwd=target.cwd,
        pid=target.pid,
    )
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda _: [resolved])

    def reject(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "PRE_EFFECT_REJECTED",
            "provider": "codex",
            "error": "target changed before SendMessage",
        }

    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", reject)

    with pytest.raises(ChatError, match="target changed"):
        send_local(root, source, target.alias, "hello")

    rejected = IntentStore(root).intents()[0]
    assert rejected.status == "PRE_EFFECT_REJECTED"
    assert IntentStore(root).begin(
        source,
        target,
        source_alias=source.alias,
        payload_digest="c" * 64,
    )


def test_pre_effect_response_parser_rejects_untrusted_variants() -> None:
    event_id = str(uuid4())
    response: dict[str, object] = {
        "schema_version": 1,
        "event_id": event_id,
        "status": "PRE_EFFECT_REJECTED",
        "provider": "claude",
        "error": "target changed before SendMessage",
    }

    assert pre_effect_error(response, event_id, "claude") == response["error"]
    for changed in (
        response | {"provider": "codex"},
        response | {"error": "line one\nline two"},
        response | {"error": "x" * 257},
        response | {"extra": True},
    ):
        assert pre_effect_error(changed, event_id, "claude") is None


def test_route_owner_check_uses_recipient_profile_not_broker_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    profile_a = str(tmp_path / "profile-a")
    monkeypatch.setenv("CODEX_HOME", profile_a)
    identity, _ = runtime.recipient_owner_identity("codex", os.getpid())
    owned = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity=identity,
        profile_root=profile_a,
    )
    root = tmp_path / "state"
    Registry(root).upsert(owned)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "broker-profile"))
    assert runtime._route_current(root, owned)
    assert Registry(root).routes()[0].profile_root == profile_a
    changed = replace(owned, profile_root=str(tmp_path / "different-recipient"))
    Registry(root).upsert(changed)
    assert not runtime._route_current(root, changed)


def test_remote_discovery_uses_one_deadline_for_queued_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    release = threading.Event()
    started: list[str] = []

    def wait_for_peer(address: str, deadline: float | None = None) -> list[Target]:
        assert deadline is not None
        started.append(address)
        release.wait(1)
        return []

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [str(i) for i in range(48)])
    monkeypatch.setattr(runtime, "_remote_node_targets", wait_for_peer)
    monkeypatch.setattr(runtime, "REMOTE_DISCOVERY_TIMEOUT_SECONDS", 0.05)
    start = time.monotonic()
    try:
        assert runtime.remote_targets(tmp_path) == []
        assert time.monotonic() - start < 0.5
        assert len(started) <= 16
    finally:
        release.set()


def test_stale_generation_cleanup_never_removes_new_owner(tmp_path: Path) -> None:
    old = route(tmp_path, pid=os.getpid())
    newer = replace(old, generation=str(uuid4()))
    registry = Registry(tmp_path / "state")
    registry.upsert(newer)
    registry.remove(old.provider, old.session_id, old.pid, generation=old.generation)
    assert registry.routes() == [newer]


def test_concurrent_registration_failure_preserves_successful_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    hook = {"hook_event_name": "SessionStart", "session_id": str(uuid4()), "cwd": str(tmp_path)}
    root = tmp_path / "state"
    entered, release = threading.Event(), threading.Event()
    spawned: list[Route] = []

    def spawn(_root: Path, owned: Route) -> None:
        spawned.append(owned)
        if len(spawned) == 1:
            entered.set()
            assert release.wait(2)
            raise ChatError("first launch failed")

    monkeypatch.setattr(runtime, "hook_input", lambda _: hook)
    monkeypatch.setattr(runtime, "_spawn_courier", spawn)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(runtime.register, "codex", "studio", os.getpid(), str(root))
        assert entered.wait(2)
        second = workers.submit(runtime.register, "codex", "studio", os.getpid(), str(root))
        try:
            time.sleep(0.05)
            assert len(spawned) == 1
        finally:
            release.set()
        with pytest.raises(ChatError, match="first launch failed"):
            first.result(timeout=2)
        replacement = second.result(timeout=2)
    assert replacement is not None
    assert Registry(root).routes() == [replacement]


def test_disappeared_courier_closes_intent_as_pre_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    root = tmp_path / "state"
    source = route(tmp_path, project="source", pid=os.getpid())
    destination = route(tmp_path, project="target", pid=os.getpid())
    for owned in (source, destination):
        Registry(root).upsert(owned)
    target = Target(
        alias=destination.alias,
        provider=destination.provider,
        device=destination.device,
        project=destination.project,
        generation=destination.generation,
        session_key=session_key(destination.provider, destination.session_id),
        remote=False,
        session_id=destination.session_id,
        cwd=destination.cwd,
        pid=destination.pid,
    )
    monkeypatch.setattr(runtime, "local_targets", lambda _: [target])
    with pytest.raises(ChatError) as error:
        runtime.send(root, source, target.alias, "never sent")
    assert not isinstance(error.value, UnknownDeliveryError)
    store = IntentStore(root)
    assert [item.status for item in store.intents()] == ["PRE_EFFECT_REJECTED"]
    assert store.begin(source, destination, source_alias=source.alias, payload_digest="b" * 64)
