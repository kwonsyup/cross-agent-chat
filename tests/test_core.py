from __future__ import annotations

import json
import os
import threading
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
    authenticate_sender,
    bounded_message,
    resolve_target,
)
from cross_agent_chat.runtime import (
    HEALTH_TIMEOUT_SECONDS,
    Target,
    _local_target,
    canonical_source_alias,
    courier_health,
    local_targets,
    pre_effect_error,
    send_local,
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
        assert timeout == HEALTH_TIMEOUT_SECONDS
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
