"""A repeated Claude SessionStart hook reporting a drifted cwd keeps live custody.

Claude Code can fire SessionStart again for the same live session -- same
session id, process, owner, and profile -- while reporting a different cwd than
the registered route. Registry matching requires cwd equality, so an unguarded
re-registration replaces the live generation and spawns a second courier,
orphaning reply selectors and delivery intents bound to the first generation.

These tests exercise the actual registration and registry boundary. Claude's
courier has no Python-side body queue to peek at, so custody is represented by
the registry record, spawn count, intent bytes, and reply-selector resolution.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import claude_runtime, runtime
from cross_agent_chat.core import (
    ChatError,
    IntentStore,
    Registry,
    Route,
    UnknownDeliveryError,
    session_key,
)
from cross_agent_chat.recipient import local_token, parse_recipient_token

OWNER = "a" * 64
OTHER_OWNER = "b" * 64


def _claude_route(
    *,
    session_id: str,
    cwd: Path,
    pid: int,
    owner_identity: str = OWNER,
    profile_root: str,
) -> Route:
    return Route.create(
        provider="claude",
        session_id=session_id,
        device="studio",
        cwd=str(cwd),
        pid=pid,
        owner_identity=owner_identity,
        profile_root=profile_root,
    )


def _bind_hook(
    monkeypatch: pytest.MonkeyPatch, session_id: str, cwd: Path, profile_root: str
) -> None:
    hook = {"hook_event_name": "SessionStart", "session_id": session_id, "cwd": str(cwd)}
    monkeypatch.setattr(runtime, "hook_input", lambda _event: hook)
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_args: (OWNER, Path("/bin/echo"))
    )
    monkeypatch.setattr(runtime, "recipient_profile_root", lambda _provider: profile_root)


def _record_spawns(monkeypatch: pytest.MonkeyPatch) -> list[Route]:
    spawned: list[Route] = []
    monkeypatch.setattr(runtime, "_spawn_courier", lambda _root, route: spawned.append(route))
    return spawned


def _bootstrap_stub(
    monkeypatch: pytest.MonkeyPatch, root: Path, route: Route
) -> list[tuple[Path, dict[str, object], float]]:
    calls: list[tuple[Path, dict[str, object], float]] = []

    def bootstrap(path: Path, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        calls.append((path, payload, timeout))
        return {
            "schema_version": 1,
            "status": "BOOTSTRAPPED",
            "generation": route.generation,
        }

    monkeypatch.setattr(runtime, "request_socket", bootstrap)
    return calls


def _selector_still_resolves(root: Path, selector: str, route: Route) -> bool:
    parsed = parse_recipient_token(selector)
    assert parsed is not None
    return [
        item
        for item in Registry(root).routes()
        if session_key(item.provider, item.session_id) == parsed.handle
        and item.generation == parsed.generation
    ] == [route]


def test_drifted_claude_start_keeps_live_generation_route_and_custody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    root = tmp_path / "state"
    profile = str(tmp_path / "claude-profile")
    session_id = str(uuid4())
    first = _claude_route(
        session_id=session_id, cwd=project_a, pid=os.getpid(), profile_root=profile
    )
    Registry(root).upsert(first)

    store = IntentStore(root)
    event_id = store.begin_identity(
        source_key=session_key("claude", session_id),
        source_generation=first.generation,
        source_alias=first.alias,
        target_key=session_key("claude", str(uuid4())),
        target_generation=str(uuid4()),
        payload_digest=hashlib.sha256(b"held body").hexdigest(),
    )
    store.mark(event_id, "TRANSPORT_ACCEPTED")
    intents_before = (root / "intents.json").read_bytes()
    reply_selector = local_token(root, session_key("claude", session_id), first.generation)

    _bind_hook(monkeypatch, session_id, project_b, profile)
    native_timeouts: list[float] = []

    def native_agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        native_timeouts.append(timeout)
        return [
            {
                "session_id": session_id,
                "name": "Probe",
                "kind": "interactive",
                "cwd": first.cwd,
            }
        ]

    monkeypatch.setattr(claude_runtime, "claude_agents", native_agents)
    calls = _bootstrap_stub(monkeypatch, root, first)
    spawned = _record_spawns(monkeypatch)

    repeated = runtime.register("claude", "studio", os.getpid(), str(root))

    assert repeated == first
    assert Registry(root).routes() == [first]
    assert spawned == []
    assert native_timeouts == [2.0]
    assert (root / "intents.json").read_bytes() == intents_before
    assert _selector_still_resolves(root, reply_selector, first)
    assert calls == [
        (
            runtime.socket_path(root, first),
            {
                "schema_version": 1,
                "operation": "bootstrap",
                "generation": first.generation,
            },
            0.5,
        )
    ]


def test_drifted_claude_start_keeps_generation_while_courier_state_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    root = tmp_path / "state"
    profile = str(tmp_path / "claude-profile")
    session_id = str(uuid4())
    first = _claude_route(
        session_id=session_id, cwd=project_a, pid=os.getpid(), profile_root=profile
    )
    Registry(root).upsert(first)
    socket_marker = runtime.socket_path(root, first)
    socket_marker.touch(mode=0o600)
    try:
        _bind_hook(monkeypatch, session_id, project_b, profile)
        native_calls: list[str] = []

        def native_agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
            native_calls.append(str(timeout))
            return []

        monkeypatch.setattr(claude_runtime, "claude_agents", native_agents)

        def busy_courier(
            _path: Path, _payload: dict[str, object], *, timeout: float
        ) -> dict[str, object]:
            del timeout
            raise UnknownDeliveryError("courier state is unknown") from TimeoutError()

        monkeypatch.setattr(runtime, "request_socket", busy_courier)
        spawned = _record_spawns(monkeypatch)

        repeated = runtime.register("claude", "studio", os.getpid(), str(root))

        assert repeated == first
        assert Registry(root).routes() == [first]
        assert spawned == []
        assert native_calls == []
    finally:
        socket_marker.unlink(missing_ok=True)


def test_drifted_claude_start_replaces_route_only_when_courier_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    root = tmp_path / "state"
    profile = str(tmp_path / "claude-profile")
    session_id = str(uuid4())
    first = _claude_route(
        session_id=session_id, cwd=project_a, pid=os.getpid(), profile_root=profile
    )
    Registry(root).upsert(first)
    _bind_hook(monkeypatch, session_id, project_b, profile)
    native_calls: list[str] = []

    def native_agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        native_calls.append(str(timeout))
        return []

    monkeypatch.setattr(claude_runtime, "claude_agents", native_agents)
    spawned = _record_spawns(monkeypatch)

    repeated = runtime.register("claude", "studio", os.getpid(), str(root))

    assert repeated is not None
    assert repeated != first
    assert repeated.cwd == str(project_b.resolve())
    assert repeated.generation != first.generation
    assert Registry(root).routes() == [repeated]
    assert spawned == [repeated]
    assert native_calls == []


def test_drifted_claude_start_recovers_when_native_cwd_is_incoming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    root = tmp_path / "state"
    profile = str(tmp_path / "claude-profile")
    session_id = str(uuid4())
    first = _claude_route(
        session_id=session_id, cwd=project_a, pid=os.getpid(), profile_root=profile
    )
    Registry(root).upsert(first)
    _bind_hook(monkeypatch, session_id, project_b, profile)
    native_timeouts: list[float] = []

    def native_agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        native_timeouts.append(timeout)
        return [
            {
                "session_id": session_id,
                "name": "Probe",
                "kind": "interactive",
                "cwd": str(project_b.resolve()),
            }
        ]

    monkeypatch.setattr(claude_runtime, "claude_agents", native_agents)
    _bootstrap_stub(monkeypatch, root, first)
    spawned = _record_spawns(monkeypatch)

    repeated = runtime.register("claude", "studio", os.getpid(), str(root))

    assert repeated is not None
    assert repeated.cwd == str(project_b.resolve())
    assert repeated.generation != first.generation
    assert Registry(root).routes() == [repeated]
    assert spawned == [repeated]
    assert native_timeouts == [2.0]


@pytest.mark.parametrize("lookup_result", ["timeout", "duplicate"])
def test_drifted_claude_start_keeps_prior_when_native_lookup_is_not_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lookup_result: str
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    root = tmp_path / "state"
    profile = str(tmp_path / "claude-profile")
    session_id = str(uuid4())
    first = _claude_route(
        session_id=session_id, cwd=project_a, pid=os.getpid(), profile_root=profile
    )
    Registry(root).upsert(first)
    _bind_hook(monkeypatch, session_id, project_b, profile)
    native_timeouts: list[float] = []

    def native_agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        native_timeouts.append(timeout)
        row = {
            "session_id": session_id,
            "name": "Probe",
            "kind": "interactive",
            "cwd": str(project_b.resolve()),
        }
        if lookup_result == "timeout":
            raise ChatError("Claude agents preflight failed")
        return [row, row]

    monkeypatch.setattr(claude_runtime, "claude_agents", native_agents)
    _bootstrap_stub(monkeypatch, root, first)
    spawned = _record_spawns(monkeypatch)

    repeated = runtime.register("claude", "studio", os.getpid(), str(root))

    assert repeated == first
    assert Registry(root).routes() == [first]
    assert spawned == []
    assert native_timeouts == [2.0]


@pytest.mark.parametrize("difference", ["pid", "owner_identity", "profile_root"])
def test_drifted_claude_start_without_same_session_identity_replaces_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, difference: str
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    root = tmp_path / "state"
    profile = str(tmp_path / "claude-profile")
    session_id = str(uuid4())
    first = _claude_route(
        session_id=session_id,
        cwd=project_a,
        pid=os.getppid() if difference == "pid" else os.getpid(),
        owner_identity=OTHER_OWNER if difference == "owner_identity" else OWNER,
        profile_root=(str(tmp_path / "other-profile") if difference == "profile_root" else profile),
    )
    Registry(root).upsert(first)
    _bind_hook(monkeypatch, session_id, project_b, profile)
    spawned = _record_spawns(monkeypatch)

    repeated = runtime.register("claude", "studio", os.getpid(), str(root))

    assert repeated is not None
    assert repeated.generation != first.generation
    assert Registry(root).routes() == [repeated]
    assert spawned == [repeated]


def test_drifted_codex_start_replaces_route_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    root = tmp_path / "state"
    profile = str(tmp_path / "codex-profile")
    session_id = str(uuid4())
    first = Route.create(
        provider="codex",
        session_id=session_id,
        device="studio",
        cwd=str(project_a),
        pid=os.getpid(),
        owner_identity=OWNER,
        profile_root=profile,
    )
    Registry(root).upsert(first)
    _bind_hook(monkeypatch, session_id, project_b, profile)
    native_calls: list[str] = []

    def native_agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        native_calls.append(str(timeout))
        return []

    monkeypatch.setattr(claude_runtime, "claude_agents", native_agents)
    spawned = _record_spawns(monkeypatch)

    repeated = runtime.register("codex", "studio", os.getpid(), str(root))

    assert repeated is not None
    assert repeated != first
    assert repeated.cwd == str(project_b.resolve())
    assert repeated.generation != first.generation
    assert Registry(root).routes() == [repeated]
    assert spawned == [repeated]
    assert native_calls == []


def test_repeated_claude_start_same_cwd_reuses_live_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "state"
    profile = str(tmp_path / "claude-profile")
    session_id = str(uuid4())
    first = _claude_route(session_id=session_id, cwd=project, pid=os.getpid(), profile_root=profile)
    Registry(root).upsert(first)
    _bind_hook(monkeypatch, session_id, project, profile)
    native_calls: list[str] = []

    def native_agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        native_calls.append(str(timeout))
        return []

    monkeypatch.setattr(claude_runtime, "claude_agents", native_agents)
    _bootstrap_stub(monkeypatch, root, first)
    spawned = _record_spawns(monkeypatch)

    repeated = runtime.register("claude", "studio", os.getpid(), str(root))

    assert repeated == first
    assert Registry(root).routes() == [first]
    assert spawned == []
    assert native_calls == []


def test_first_claude_start_registers_and_spawns_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "state"
    profile = str(tmp_path / "claude-profile")
    session_id = str(uuid4())
    _bind_hook(monkeypatch, session_id, project, profile)
    spawned = _record_spawns(monkeypatch)

    registered = runtime.register("claude", "studio", os.getpid(), str(root))

    assert registered is not None
    assert registered.cwd == str(project.resolve())
    assert registered.profile_root == profile
    assert registered.owner_identity == OWNER
    assert Registry(root).routes() == [registered]
    assert spawned == [registered]
