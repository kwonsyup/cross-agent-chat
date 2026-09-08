from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
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
from cross_agent_chat.remote import parse_remote_envelope
from cross_agent_chat.runtime import (
    HEALTH_TIMEOUT_SECONDS,
    LOCAL_DISCOVERY_TIMEOUT_SECONDS,
    MAX_FRAME_BYTES,
    MCP_TOOL_TIMEOUT_SECONDS,
    OPERATION_TIMEOUT_SECONDS,
    REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    REMOTE_TIMEOUT_SECONDS,
    Target,
    _local_target,
    canonical_source_alias,
    courier_health,
    courier_server,
    event_status,
    local_targets,
    pre_effect_error,
    presence_is_enabled,
    request_socket,
    send,
    send_local,
    sender_readiness,
    socket_path,
    unregister,
    wrapped_message,
)
from cross_agent_chat.runtime import (
    resolve_target as resolve_live_target,
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


def test_target_handle_selects_one_duplicate_display_alias(tmp_path: Path) -> None:
    first_route = route(tmp_path, provider="claude", project="api", session_id=str(uuid4()))
    second_route = route(tmp_path, provider="claude", project="api", session_id=str(uuid4()))
    first = Target(
        alias=first_route.alias,
        provider=first_route.provider,
        device=first_route.device,
        project=first_route.project,
        generation=first_route.generation,
        session_key=session_key(first_route.provider, first_route.session_id),
        remote=False,
    )
    second = Target(
        alias=second_route.alias,
        provider=second_route.provider,
        device=second_route.device,
        project=second_route.project,
        generation=second_route.generation,
        session_key=session_key(second_route.provider, second_route.session_id),
        remote=False,
    )

    assert first.alias == second.alias
    assert first.public()["handle"] == first.session_key
    assert resolve_live_target([first, second], second.session_key) == second
    with pytest.raises(ChatError, match="ambiguous"):
        resolve_live_target([first, second], first.alias)


def test_reply_instruction_preserves_alias_hint_but_requires_exact_sender_handle(
    tmp_path: Path,
) -> None:
    source = route(tmp_path, provider="claude", session_id=str(uuid4()))
    duplicate = route(tmp_path, provider="claude", session_id=str(uuid4()))
    source_handle = session_key(source.provider, source.session_id)
    duplicate_handle = session_key(duplicate.provider, duplicate.session_id)

    assert source.alias == duplicate.alias
    body = wrapped_message(source.alias, source_handle, "reply when ready", str(uuid4()))

    assert source.alias in body
    assert source_handle in body
    assert "Do not use a display alias as a fallback." in body
    assert (
        resolve_live_target(
            [
                Target(
                    source.alias,
                    source.provider,
                    source.device,
                    source.project,
                    source.generation,
                    source_handle,
                    False,
                ),
                Target(
                    duplicate.alias,
                    duplicate.provider,
                    duplicate.device,
                    duplicate.project,
                    duplicate.generation,
                    duplicate_handle,
                    False,
                ),
            ],
            source_handle,
        ).session_key
        == source_handle
    )


def test_local_delivery_wraps_reply_with_authenticated_sender_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    root = tmp_path / "state"
    source = route(tmp_path, pid=os.getpid(), project="source")
    target = route(tmp_path, pid=os.getpid(), project="target")
    Registry(root).upsert(source)
    Registry(root).upsert(target)
    resolved = Target(
        target.alias,
        target.provider,
        target.device,
        target.project,
        target.generation,
        session_key(target.provider, target.session_id),
        False,
        session_id=target.session_id,
        cwd=target.cwd,
        pid=target.pid,
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(runtime, "local_targets", lambda _: [resolved])

    def accept(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        captured.update(payload)
        return {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": target.provider,
        }

    monkeypatch.setattr(runtime, "request_socket", accept)
    send_local(root, source, resolved.session_key, "reply when ready")

    assert session_key(source.provider, source.session_id) in str(captured["message"])


def test_remote_delivery_wraps_reply_with_authenticated_sender_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    root = tmp_path / "state"
    source = route(tmp_path, pid=os.getpid(), project="source")
    Registry(root).upsert(source)
    target = Target(
        "codex@remote:target:123456789abc",
        "codex",
        "remote",
        "target",
        str(uuid4()),
        "a" * 64,
        True,
        tailnet_address="100.64.0.2",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(runtime, "local_targets", lambda _: [])
    monkeypatch.setattr(runtime, "_remote_discovery", lambda: ([target], True))

    def accept(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        captured.update(payload)
        event_id = parse_remote_envelope(str(payload["envelope"]))[0]
        return {
            "schema_version": 1,
            "event_id": event_id,
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": target.provider,
        }

    monkeypatch.setattr(runtime, "request_tailnet", accept)
    runtime.send(root, source, target.session_key, "reply when ready")

    assert session_key(source.provider, source.session_id) in str(captured["envelope"])


def test_sender_readiness_is_bound_to_the_existing_sender_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, pid=os.getppid())
    Registry(root).upsert(source)
    monkeypatch.setattr("cross_agent_chat.runtime._route_current", lambda *_: True)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda *_args, **_kwargs: {"status": "READY", "generation": source.generation},
    )

    assert sender_readiness(root, "codex", os.getppid(), source.session_id) == {"status": "ready"}
    assert sender_readiness(root, "codex", os.getppid(), str(uuid4())) == {
        "status": "unavailable",
        "reason": "exact Codex sender is unavailable",
    }


def test_event_status_reads_only_an_exact_source_owned_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, project="source", pid=os.getpid())
    target = route(tmp_path, project="target", pid=os.getpid())
    store = IntentStore(root)
    event_id = store.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)
    before = store.path.read_bytes()
    monkeypatch.setattr("cross_agent_chat.runtime._route_current", lambda *_: True)

    status = event_status(root, source, event_id)

    assert status == {
        "schema_version": 1,
        "event_id": event_id,
        "status": "PENDING",
        "source_alias": source.alias,
        "target_handle": session_key(target.provider, target.session_id),
        "target_generation": target.generation,
        "timestamp": store.intents()[0].timestamp,
        "delivery_observation": "not_observed",
    }
    assert store.path.read_bytes() == before
    with pytest.raises(ChatError, match="event is unavailable"):
        event_status(root, target, event_id)


def test_stale_session_end_with_a_different_working_directory_keeps_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    root = tmp_path / "state"
    source = route(tmp_path, pid=os.getpid())
    stale_cwd = tmp_path / "stale"
    stale_cwd.mkdir()
    Registry(root).upsert(source)
    monkeypatch.setattr(runtime, "presence_is_enabled", lambda: True)
    monkeypatch.setattr(
        runtime,
        "hook_input",
        lambda *_args, **_kwargs: {
            "hook_event_name": "SessionEnd",
            "session_id": source.session_id,
            "cwd": str(stale_cwd),
        },
    )

    with pytest.raises(ChatError, match="exact session route is unavailable"):
        unregister(source.provider, source.pid, str(root))
    assert Registry(root).routes() == [source]


def test_session_end_ignores_untrusted_unavailable_cwd_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, pid=os.getpid())
    stale_cwd = tmp_path / "stale"
    stale_cwd.mkdir()
    Registry(root).upsert(source)
    monkeypatch.setattr("cross_agent_chat.runtime.presence_is_enabled", lambda: True)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": source.session_id,
                    "cwd": str(stale_cwd),
                    "_cwd_unavailable": True,
                }
            )
        ),
    )

    with pytest.raises(ChatError, match="exact session route is unavailable"):
        unregister(source.provider, source.pid, str(root))

    assert Registry(root).routes() == [source]


def test_session_end_removes_exact_owned_route_after_its_cwd_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, pid=os.getpid())
    Registry(root).upsert(source)
    Path(source.cwd).rmdir()
    monkeypatch.setattr("cross_agent_chat.runtime.presence_is_enabled", lambda: True)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": source.session_id,
                    "cwd": source.cwd,
                }
            )
        ),
    )

    unregister(source.provider, source.pid, str(root))

    assert Registry(root).routes() == []


def test_session_end_removes_exact_owned_route_after_its_symlinked_cwd_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, pid=os.getpid())
    Registry(root).upsert(source)
    link = tmp_path / "linked-project"
    link.symlink_to(source.cwd, target_is_directory=True)
    link.unlink()
    monkeypatch.setattr("cross_agent_chat.runtime.presence_is_enabled", lambda: True)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": source.session_id,
                    "cwd": str(link),
                }
            )
        ),
    )

    unregister(source.provider, source.pid, str(root))

    assert Registry(root).routes() == []


def test_session_end_canonicalizes_an_existing_symlinked_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, pid=os.getpid())
    Registry(root).upsert(source)
    link = tmp_path / "linked-project"
    link.symlink_to(source.cwd, target_is_directory=True)
    monkeypatch.setattr("cross_agent_chat.runtime.presence_is_enabled", lambda: True)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": source.session_id,
                    "cwd": str(link),
                }
            )
        ),
    )

    unregister(source.provider, source.pid, str(root))

    assert Registry(root).routes() == []


def test_session_end_canonicalizes_an_existing_non_normal_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, pid=os.getpid())
    Registry(root).upsert(source)
    monkeypatch.setattr("cross_agent_chat.runtime.presence_is_enabled", lambda: True)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": source.session_id,
                    "cwd": f"{source.cwd}/.",
                }
            )
        ),
    )

    unregister(source.provider, source.pid, str(root))

    assert Registry(root).routes() == []


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


def test_local_send_by_exact_handle_skips_remote_discovery_and_title_metadata(
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
        "cross_agent_chat.runtime._with_codex_titles",
        lambda *_args: pytest.fail("delivery must not invoke optional title metadata"),
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

    assert send(root, source, local.session_key, "hello")["status"] == "TRANSPORT_ACCEPTED"


def test_alias_send_rejects_incomplete_global_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    source = route(tmp_path, project="source", pid=os.getpid())
    target = route(tmp_path, project="target", pid=os.getpid())
    local = Target(
        alias=target.alias,
        provider=target.provider,
        device=target.device,
        project=target.project,
        generation=target.generation,
        session_key=session_key(target.provider, target.session_id),
        remote=False,
    )
    monkeypatch.setattr(runtime, "local_targets", lambda _: [local])
    monkeypatch.setattr(runtime, "_remote_discovery", lambda: ([], False))

    with pytest.raises(ChatError, match="discovery is incomplete"):
        send(tmp_path / "state", source, target.alias, "hello")


def test_duplicate_codex_aliases_are_ambiguous_after_complete_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    source = route(tmp_path, project="source", pid=os.getpid())
    aliases = [
        Target(
            alias="codex@studio:api:shared",
            provider="codex",
            device="studio",
            project="api",
            generation=str(uuid4()),
            session_key=session_key("codex", str(uuid4())),
            remote=False,
        )
        for _ in range(2)
    ]
    monkeypatch.setattr(runtime, "local_targets", lambda _: aliases)
    monkeypatch.setattr(runtime, "_remote_discovery", lambda: ([], True))

    with pytest.raises(ChatError, match="ambiguous"):
        send(tmp_path / "state", source, aliases[0].alias, "hello")


def test_unknown_opaque_handle_cannot_fall_back_to_fuzzy_target_matching() -> None:
    target = Target(
        alias="codex@studio:api:known",
        provider="codex",
        device="studio",
        project="api",
        generation=str(uuid4()),
        session_key="b" * 64,
        remote=False,
    )

    with pytest.raises(ChatError, match="target handle is unavailable"):
        resolve_live_target([target], "a" * 64)


def test_local_titles_use_the_registered_codex_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    root = tmp_path / "state"
    profile = tmp_path / "registered-profile"
    binary = tmp_path / "registered-profile" / "bin" / "codex"
    item = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
        profile_root=str(profile),
    )
    Registry(root).upsert(item)
    target = Target(
        alias=item.alias,
        provider="codex",
        device=item.device,
        project=item.project,
        generation=item.generation,
        session_key=session_key(item.provider, item.session_id),
        remote=False,
        session_id=item.session_id,
        cwd=item.cwd,
        pid=item.pid,
    )
    observed: dict[str, object] = {}

    def owner_identity(
        provider: str, pid: int, profile_root: str | None = None
    ) -> tuple[str, Path]:
        observed["owner"] = (provider, pid, profile_root)
        return "a" * 64, binary

    def titles(**kwargs: object) -> dict[str, str]:
        observed["titles"] = kwargs
        return {item.session_id: "Registered profile title"}

    monkeypatch.setattr(runtime, "recipient_owner_identity", owner_identity)
    monkeypatch.setattr(runtime, "native_thread_titles", titles)

    enriched = runtime._with_codex_titles(root, [target], time.monotonic() + 1)

    assert enriched[0].title == "Registered profile title"
    assert observed["owner"] == ("codex", os.getpid(), str(profile))
    title_call = observed["titles"]
    assert isinstance(title_call, dict)
    assert title_call["binary"] == binary
    assert title_call["environment"] == {"CODEX_HOME": str(profile)}
    assert title_call["thread_ids"] == [item.session_id]
    assert isinstance(title_call["deadline"], float)


def test_local_titles_reject_an_owner_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    root = tmp_path / "state"
    item = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
        profile_root=str(tmp_path / "registered-profile"),
    )
    Registry(root).upsert(item)
    target = Target(
        alias=item.alias,
        provider="codex",
        device=item.device,
        project=item.project,
        generation=item.generation,
        session_key=session_key(item.provider, item.session_id),
        remote=False,
        session_id=item.session_id,
        cwd=item.cwd,
        pid=item.pid,
    )
    monkeypatch.setattr(
        runtime,
        "recipient_owner_identity",
        lambda *_args: ("b" * 64, tmp_path / "wrong-codex"),
    )
    monkeypatch.setattr(
        runtime,
        "native_thread_titles",
        lambda **_kwargs: pytest.fail("unverified owner must not receive title metadata"),
    )

    assert runtime._with_codex_titles(root, [target], time.monotonic() + 1) == [target]


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
    ready = False
    while time.monotonic() < deadline:
        try:
            bootstrap = request_socket(
                path,
                {"schema_version": 1, "operation": "bootstrap", "generation": first.generation},
                timeout=0.1,
            )
            assert bootstrap == {
                "schema_version": 1,
                "status": "BOOTSTRAPPED",
                "generation": first.generation,
            }
            health = request_socket(
                path,
                {"schema_version": 1, "operation": "health", "generation": first.generation},
                timeout=0.1,
            )
            ready = health.get("status") == "READY"
        except ChatError:
            pass
        if ready:
            break
        time.sleep(0.01)
    assert ready
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


def test_courier_bootstrap_does_not_wait_for_claude_native_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    item = route(tmp_path, provider="claude", pid=os.getpid())

    class OwnedProcess:
        stderr: None = None
        terminated = False
        waited = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            self.waited = True
            return 0

    process = OwnedProcess()

    def spawn(*_args: object, **_kwargs: object) -> OwnedProcess:
        return process

    def bootstrap(_path: Path, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        assert timeout > 0
        assert payload == {
            "schema_version": 1,
            "operation": "bootstrap",
            "generation": item.generation,
        }
        return {
            "schema_version": 1,
            "status": "BOOTSTRAPPED",
            "generation": item.generation,
        }

    monkeypatch.setattr(runtime, "executable", lambda: Path("/bin/echo"))
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_: ("a" * 64, Path("/bin/echo"))
    )
    monkeypatch.setattr("cross_agent_chat.runtime.subprocess.Popen", spawn)
    monkeypatch.setattr(runtime, "request_socket", bootstrap)

    runtime._spawn_courier(tmp_path / "state", item)

    assert not process.terminated
    assert not process.waited


def test_failed_courier_bootstrap_reaps_the_exact_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    item = route(tmp_path, provider="claude", pid=os.getpid())

    class OwnedProcess:
        stderr: None = None
        terminated = False
        waited = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            self.waited = True
            return 0

    process = OwnedProcess()

    def spawn(*_args: object, **_kwargs: object) -> OwnedProcess:
        return process

    def unavailable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ChatError("local listener is unavailable")

    monkeypatch.setattr(runtime, "executable", lambda: Path("/bin/echo"))
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_: ("a" * 64, Path("/bin/echo"))
    )
    monkeypatch.setattr("cross_agent_chat.runtime.subprocess.Popen", spawn)
    monkeypatch.setattr(runtime, "request_socket", unavailable)
    monkeypatch.setattr(runtime, "COURIER_READY_SECONDS", 0.0)

    with pytest.raises(ChatError, match="local bootstrap"):
        runtime._spawn_courier(tmp_path / "state", item)

    assert process.terminated
    assert process.waited


def test_failed_bootstrap_reaps_a_socket_bound_during_exact_child_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    root = tmp_path / "state"
    item = route(tmp_path, provider="claude", pid=os.getpid())
    Registry(root).upsert(item)
    path = socket_path(root, item)
    sockets: list[socket.socket] = []

    class OwnedProcess:
        terminated = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen()
            sockets.append(server)

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            return 0

    process = OwnedProcess()

    def spawn(*_args: object, **_kwargs: object) -> OwnedProcess:
        return process

    def unavailable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ChatError("local listener is unavailable")

    monkeypatch.setattr(runtime, "executable", lambda: Path("/bin/echo"))
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_: ("a" * 64, Path("/bin/echo"))
    )
    monkeypatch.setattr("cross_agent_chat.runtime.subprocess.Popen", spawn)
    monkeypatch.setattr(runtime, "request_socket", unavailable)
    monkeypatch.setattr(runtime, "COURIER_READY_SECONDS", 0.0)

    try:
        with pytest.raises(ChatError, match="local bootstrap"):
            runtime._spawn_courier(root, item)
        assert process.terminated
        assert not path.exists()
    finally:
        for server in sockets:
            server.close()


def test_cancelled_courier_bootstrap_reaps_the_exact_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    item = route(tmp_path, provider="claude", pid=os.getpid())

    class OwnedProcess:
        terminated = False
        waited = False

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout: float) -> int:
            self.waited = True
            return 0

    process = OwnedProcess()

    def spawn(*_args: object, **_kwargs: object) -> OwnedProcess:
        return process

    def cancelled(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "executable", lambda: Path("/bin/echo"))
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_: ("a" * 64, Path("/bin/echo"))
    )
    monkeypatch.setattr("cross_agent_chat.runtime.subprocess.Popen", spawn)
    monkeypatch.setattr(runtime, "request_socket", cancelled)

    with pytest.raises(KeyboardInterrupt):
        runtime._spawn_courier(tmp_path / "state", item)

    assert process.terminated
    assert process.waited


def test_cancelled_registration_removes_its_exact_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "state"
    session_id = str(uuid4())
    hook = {"hook_event_name": "SessionStart", "session_id": session_id, "cwd": str(project)}
    monkeypatch.setattr(runtime, "hook_input", lambda _: hook)
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_: ("a" * 64, Path("/bin/echo"))
    )
    monkeypatch.setattr(
        runtime, "_spawn_courier", lambda *_: (_ for _ in ()).throw(KeyboardInterrupt)
    )

    with pytest.raises(KeyboardInterrupt):
        runtime.register("claude", "studio", os.getpid(), str(root))

    assert Registry(root).routes() == []


def test_sigterm_registration_cleanup_removes_the_exact_generation_in_an_owned_process(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    session_id = str(uuid4())
    code = f"""
import hashlib
import os
import time
from pathlib import Path
from cross_agent_chat import runtime
from cross_agent_chat.core import ChatError, Registry

root = Path({str(root)!r})
project = Path({str(project)!r})
session_id = {session_id!r}
socket_root = Path(os.environ["CROSS_AGENT_CHAT_TEST_SOCKET_ROOT"])
def fixture_socket_path(state_root, route):
    identity = (
        f"{{state_root.resolve()}}:{{route.provider}}:"
        f"{{route.session_id}}:{{route.generation}}"
    )
    return socket_root / f"{{hashlib.sha256(identity.encode()).hexdigest()[:32]}}.sock"
runtime.socket_path = fixture_socket_path
runtime.hook_input = lambda _event: {{
    "hook_event_name": "SessionStart", "session_id": session_id, "cwd": str(project)
}}
runtime.recipient_owner_identity = lambda *_args: ("a" * 64, Path("/bin/echo"))
runtime.recipient_profile_root = lambda _provider: str(root / "profile")
runtime.executable = lambda: Path("/bin/echo")
class Process:
    terminated = False
    def poll(self):
        return None
    def terminate(self):
        self.terminated = True
        (root / "terminated").write_text("yes")
    def kill(self):
        self.terminated = True
    def wait(self, timeout):
        return 0
process = Process()
runtime.subprocess.Popen = lambda *_args, **_kwargs: process
def wait_for_bootstrap(*_args, **_kwargs):
    print("WAITING", flush=True)
    while True:
        time.sleep(1)
runtime.request_socket = wait_for_bootstrap
runtime.register("claude", "studio", os.getpid(), str(root))
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    try:
        assert process.stdout.readline() == "WAITING\n"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=2)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
    assert process.returncode == 143
    assert stdout == ""
    assert stderr == ""
    assert Registry(root).routes() == []
    assert (root / "terminated").read_text() == "yes"


def test_sigterm_scope_begins_before_route_publication_in_an_owned_process(tmp_path: Path) -> None:
    root = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    session_id = str(uuid4())
    code = f"""
import hashlib
import os
import time
from pathlib import Path
from cross_agent_chat import runtime

root = Path({str(root)!r})
project = Path({str(project)!r})
socket_root = Path(os.environ["CROSS_AGENT_CHAT_TEST_SOCKET_ROOT"])
def fixture_socket_path(state_root, route):
    identity = (
        f"{{state_root.resolve()}}:{{route.provider}}:"
        f"{{route.session_id}}:{{route.generation}}"
    )
    return socket_root / f"{{hashlib.sha256(identity.encode()).hexdigest()[:32]}}.sock"
runtime.socket_path = fixture_socket_path
runtime.hook_input = lambda _event: {{
    "hook_event_name": "SessionStart", "session_id": {session_id!r}, "cwd": str(project)
}}
runtime.recipient_owner_identity = lambda *_args: ("a" * 64, Path("/bin/echo"))
runtime.recipient_profile_root = lambda _provider: str(root / "profile")
def wait_before_publish(self, _route):
    print("BEFORE_PUBLISH", flush=True)
    while True:
        time.sleep(1)
runtime.Registry.upsert_or_reuse_live_owner = wait_before_publish
runtime.register("claude", "studio", os.getpid(), str(root))
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    try:
        assert process.stdout.readline() == "BEFORE_PUBLISH\n"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=2)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
    assert process.returncode == 143
    assert stdout == ""
    assert stderr == ""
    assert Registry(root).routes() == []


def test_registration_sigterm_scope_restores_the_previous_handler() -> None:
    from cross_agent_chat import runtime

    previous = signal.getsignal(signal.SIGTERM)

    with runtime._registration_sigterm_scope():
        assert signal.getsignal(signal.SIGTERM) is runtime._registration_sigterm

    assert signal.getsignal(signal.SIGTERM) is previous


def test_duplicate_claude_start_reuses_bootstrapped_courier_before_native_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "state"
    session_id = str(uuid4())
    profile = str(tmp_path / "claude-profile")
    first = Route.create(
        provider="claude",
        session_id=session_id,
        device="studio",
        cwd=str(project),
        pid=os.getpid(),
        owner_identity="a" * 64,
        profile_root=profile,
    )
    Registry(root).upsert(first)
    hook = {"hook_event_name": "SessionStart", "session_id": session_id, "cwd": str(project)}
    monkeypatch.setattr(runtime, "hook_input", lambda _: hook)
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_: ("a" * 64, Path("/bin/echo"))
    )
    monkeypatch.setattr(runtime, "recipient_profile_root", lambda _: profile)
    monkeypatch.setattr(
        runtime, "_spawn_courier", lambda *_: pytest.fail("duplicate spawned a courier")
    )

    def bootstrap(_path: Path, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        assert timeout == 0.5
        assert payload == {
            "schema_version": 1,
            "operation": "bootstrap",
            "generation": first.generation,
        }
        return {
            "schema_version": 1,
            "status": "BOOTSTRAPPED",
            "generation": first.generation,
        }

    monkeypatch.setattr(runtime, "request_socket", bootstrap)

    assert runtime.register("claude", "studio", os.getpid(), str(root)) == first


def test_claude_bootstrap_survives_delayed_native_health_without_claiming_a_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = route(tmp_path, provider="claude", pid=os.getpid())
    root = tmp_path / "state"
    Registry(root).upsert(item)
    native_lookups = 0

    def unavailable_native(*_args: object) -> dict[str, str]:
        nonlocal native_lookups
        native_lookups += 1
        time.sleep(0.15)
        raise ChatError("native listing is unavailable")

    monkeypatch.setattr("cross_agent_chat.runtime.exact_agent", unavailable_native)
    worker = threading.Thread(
        target=courier_server,
        kwargs={
            "provider": "claude",
            "state_root_value": str(root),
            "session_id": item.session_id,
            "cwd": item.cwd,
            "generation": item.generation,
            "pid": os.getpid(),
        },
        daemon=True,
    )
    worker.start()
    path = socket_path(root, item)
    deadline = time.monotonic() + 2.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()
    try:
        bootstrap = request_socket(
            path,
            {"schema_version": 1, "operation": "bootstrap", "generation": item.generation},
            timeout=0.1,
        )
        assert bootstrap == {
            "schema_version": 1,
            "status": "BOOTSTRAPPED",
            "generation": item.generation,
        }
        assert native_lookups == 0
        health = request_socket(
            path,
            {"schema_version": 1, "operation": "health", "generation": item.generation},
            timeout=0.5,
        )
        assert health == {
            "schema_version": 1,
            "status": "UNAVAILABLE",
            "generation": item.generation,
        }
        assert native_lookups == 1
        assert (
            request_socket(
                path,
                {"schema_version": 1, "operation": "bootstrap", "generation": item.generation},
                timeout=0.1,
            )
            == bootstrap
        )
    finally:
        request_socket(
            path,
            {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
        )
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_initial_bootstrap_follows_prebootstrap_health_without_native_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = route(tmp_path, provider="claude", pid=os.getpid())
    root = tmp_path / "state"
    Registry(root).upsert(item)
    health_done = threading.Event()
    health_received = threading.Event()
    native_started = threading.Event()
    release_native = threading.Event()
    health_responses: list[dict[str, object]] = []

    def slow_native(*_args: object) -> dict[str, str]:
        native_started.set()
        assert release_native.wait(2)
        raise ChatError("native listing is unavailable")

    def health_before_bootstrap() -> None:
        health_responses.append(
            request_socket(
                socket_path(root, item),
                {"schema_version": 1, "operation": "health", "generation": item.generation},
                timeout=1.0,
            )
        )
        health_done.set()

    from cross_agent_chat import runtime

    original_read_frame = runtime.read_frame

    def track_health_frame(connection: socket.socket, limit: int = MAX_FRAME_BYTES) -> bytes:
        frame = original_read_frame(connection, limit)
        request = json.loads(frame)
        if isinstance(request, dict) and request.get("operation") == "health":
            health_received.set()
        return frame

    monkeypatch.setattr("cross_agent_chat.runtime.exact_agent", slow_native)
    monkeypatch.setattr(runtime, "read_frame", track_health_frame)
    worker = threading.Thread(
        target=courier_server,
        kwargs={
            "provider": "claude",
            "state_root_value": str(root),
            "session_id": item.session_id,
            "cwd": item.cwd,
            "generation": item.generation,
            "pid": os.getpid(),
        },
        daemon=True,
    )
    worker.start()
    path = socket_path(root, item)
    deadline = time.monotonic() + 2.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()
    health = threading.Thread(target=health_before_bootstrap, daemon=True)
    health.start()
    try:
        assert health_received.wait(1.0)
        assert request_socket(
            path,
            {"schema_version": 1, "operation": "bootstrap", "generation": item.generation},
            timeout=1.0,
        ) == {
            "schema_version": 1,
            "status": "BOOTSTRAPPED",
            "generation": item.generation,
        }
        assert not release_native.is_set()
        assert health_done.wait(1.0)
        assert health_responses == [
            {
                "schema_version": 1,
                "status": "UNAVAILABLE",
                "generation": item.generation,
            }
        ]
        assert not native_started.is_set()
    finally:
        release_native.set()
        health.join(timeout=2)
        request_socket(
            path,
            {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
        )
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_prebootstrap_accept_is_rejected_before_native_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = route(tmp_path, provider="claude", pid=os.getpid())
    root = tmp_path / "state"
    Registry(root).upsert(item)

    monkeypatch.setattr(
        "cross_agent_chat.runtime.exact_agent",
        lambda *_args: pytest.fail("native delivery ran before bootstrap"),
    )
    worker = threading.Thread(
        target=courier_server,
        kwargs={
            "provider": "claude",
            "state_root_value": str(root),
            "session_id": item.session_id,
            "cwd": item.cwd,
            "generation": item.generation,
            "pid": os.getpid(),
        },
        daemon=True,
    )
    worker.start()
    path = socket_path(root, item)
    deadline = time.monotonic() + 2.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()
    event_id = str(uuid4())
    try:
        assert request_socket(
            path,
            {
                "schema_version": 1,
                "operation": "accept",
                "generation": item.generation,
                "event_id": event_id,
                "message": "must not invoke native delivery",
            },
            timeout=1.0,
        ) == {
            "schema_version": 1,
            "event_id": event_id,
            "status": "PRE_EFFECT_REJECTED",
            "provider": "claude",
            "error": "session courier is still bootstrapping",
        }
    finally:
        request_socket(
            path,
            {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
        )
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_incomplete_prebootstrap_frame_cannot_delay_initial_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = route(tmp_path, pid=os.getpid())
    root = tmp_path / "state"
    Registry(root).upsert(item)
    frame_started = threading.Event()
    from cross_agent_chat import runtime

    original_read_frame = runtime.read_frame

    def tracked_read_frame(connection: socket.socket, limit: int = MAX_FRAME_BYTES) -> bytes:
        frame_started.set()
        return original_read_frame(connection, limit)

    monkeypatch.setattr(runtime, "read_frame", tracked_read_frame)
    worker = threading.Thread(
        target=courier_server,
        kwargs={
            "provider": "codex",
            "state_root_value": str(root),
            "session_id": item.session_id,
            "cwd": item.cwd,
            "generation": item.generation,
            "pid": os.getpid(),
        },
        daemon=True,
    )
    worker.start()
    path = socket_path(root, item)
    deadline = time.monotonic() + 2.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()
    partial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    while True:
        try:
            partial.connect(str(path))
            break
        except (ConnectionRefusedError, FileNotFoundError):
            partial.close()
            if time.monotonic() >= deadline:
                pytest.fail("courier listener did not become ready")
            time.sleep(0.01)
            partial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    partial.sendall(b'{"schema_version":1')
    assert frame_started.wait(1.0)
    try:
        assert request_socket(
            path,
            {"schema_version": 1, "operation": "bootstrap", "generation": item.generation},
            timeout=1.0,
        ) == {
            "schema_version": 1,
            "status": "BOOTSTRAPPED",
            "generation": item.generation,
        }
    finally:
        partial.close()
        request_socket(
            path,
            {"schema_version": 1, "operation": "shutdown", "generation": item.generation},
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

    def wait_for_peer(address: str, deadline: float | None = None) -> tuple[list[Target], bool]:
        assert deadline is not None
        started.append(address)
        release.wait(1)
        return [], True

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
        runtime.send(root, source, target.session_key, "never sent")
    assert not isinstance(error.value, UnknownDeliveryError)
    store = IntentStore(root)
    assert [item.status for item in store.intents()] == ["PRE_EFFECT_REJECTED"]
    assert store.begin(source, destination, source_alias=source.alias, payload_digest="b" * 64)
