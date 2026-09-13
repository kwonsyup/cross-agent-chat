from __future__ import annotations

from pathlib import Path

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import ChatError, Registry, Route
from cross_agent_chat.native_helper import NativeHelperStore


def route(
    root: Path, *, session: str, generation: str, pid: int, profile_root: Path | None = None
) -> Route:
    profile = root / "profile" if profile_root is None else profile_root
    profile.mkdir(exist_ok=True)
    return Route.create(
        provider="codex",
        session_id=session,
        device="studio",
        cwd=str(root),
        pid=pid,
        generation=generation,
        owner_identity="a" * 64,
        profile_root=str(profile),
    )


def test_unknown_create_blocks_only_its_original_route(tmp_path: Path) -> None:
    first = route(
        tmp_path,
        session="00000000-0000-4000-8000-000000000001",
        generation="00000000-0000-4000-8000-000000000011",
        pid=101,
    )
    second = route(
        tmp_path,
        session="00000000-0000-4000-8000-000000000002",
        generation="00000000-0000-4000-8000-000000000012",
        pid=102,
    )
    store = NativeHelperStore(tmp_path / "state")

    first_binding, _ = store.reserve(first, "a" * 64)
    with pytest.raises(ChatError, match="bootstrap"):
        store.reserve(first, "a" * 64)
    second_binding, _ = store.reserve(second, "a" * 64)

    assert first_binding.state == "UNKNOWN"
    assert second_binding.state == "UNKNOWN"
    assert len(store.bindings()) == 2


def test_register_requires_current_original_and_same_selected_profile(tmp_path: Path) -> None:
    original = route(
        tmp_path,
        session="00000000-0000-4000-8000-000000000001",
        generation="00000000-0000-4000-8000-000000000011",
        pid=101,
    )
    store = NativeHelperStore(tmp_path / "state")
    binding, nonce = store.reserve(original, "a" * 64)
    helper_root = tmp_path / binding.helper_directory
    helper_root.mkdir()
    helper = route(
        helper_root,
        session="00000000-0000-4000-8000-000000000002",
        generation="00000000-0000-4000-8000-000000000012",
        pid=102,
        profile_root=tmp_path / "profile",
    )

    registered = store.register(helper, nonce, original, "a" * 64)

    assert registered.state == "REGISTERED"
    assert registered.helper_session_id == helper.session_id
    with pytest.raises(ChatError, match="registration"):
        store.register(helper, nonce, original, "a" * 64)


def test_register_rejects_changed_original_generation(tmp_path: Path) -> None:
    original = route(
        tmp_path,
        session="00000000-0000-4000-8000-000000000001",
        generation="00000000-0000-4000-8000-000000000011",
        pid=101,
    )
    changed = route(
        tmp_path,
        session=original.session_id,
        generation="00000000-0000-4000-8000-000000000021",
        pid=101,
    )
    helper = route(
        tmp_path,
        session="00000000-0000-4000-8000-000000000002",
        generation="00000000-0000-4000-8000-000000000012",
        pid=102,
    )
    store = NativeHelperStore(tmp_path / "state")
    _, nonce = store.reserve(original, "a" * 64)

    with pytest.raises(ChatError, match="original route changed"):
        store.register(helper, nonce, changed, "a" * 64)
    assert store.bindings()[0].state == "UNKNOWN"


def test_register_rejects_nonce_holder_outside_reserved_helper_directory(tmp_path: Path) -> None:
    original = route(
        tmp_path,
        session="00000000-0000-4000-8000-000000000001",
        generation="00000000-0000-4000-8000-000000000011",
        pid=101,
    )
    store = NativeHelperStore(tmp_path / "state")
    _, nonce = store.reserve(original, "a" * 64)
    wrong_directory = tmp_path / "unrelated-projectless-task"
    wrong_directory.mkdir()
    helper = route(
        wrong_directory,
        session="00000000-0000-4000-8000-000000000002",
        generation="00000000-0000-4000-8000-000000000012",
        pid=102,
        profile_root=tmp_path / "profile",
    )

    with pytest.raises(ChatError, match="context"):
        store.register(helper, nonce, original, "a" * 64)

    assert store.bindings()[0].state == "UNKNOWN"


def test_helper_lineage_stays_ineligible_after_helper_generation_restarts(tmp_path: Path) -> None:
    original = route(
        tmp_path,
        session="00000000-0000-4000-8000-000000000001",
        generation="00000000-0000-4000-8000-000000000011",
        pid=101,
    )
    store = NativeHelperStore(tmp_path / "state")
    binding, _ = store.reserve(original, "a" * 64)
    helper_root = tmp_path / binding.helper_directory
    helper_root.mkdir()
    restarted = route(
        helper_root,
        session="00000000-0000-4000-8000-000000000002",
        generation="00000000-0000-4000-8000-000000000099",
        pid=102,
        profile_root=tmp_path / "profile",
    )

    assert store.is_helper_lineage(restarted)
    assert not store.needs_bootstrap(restarted)


def test_dead_registered_helper_is_replaced_before_recreation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = route(
        tmp_path,
        session="00000000-0000-4000-8000-000000000001",
        generation="00000000-0000-4000-8000-000000000011",
        pid=101,
    )
    store = NativeHelperStore(tmp_path / "state")
    binding, nonce = store.reserve(original, "a" * 64, [original])
    helper_root = tmp_path / binding.helper_directory
    helper_root.mkdir()
    helper = route(
        helper_root,
        session="00000000-0000-4000-8000-000000000002",
        generation="00000000-0000-4000-8000-000000000012",
        pid=102,
        profile_root=tmp_path / "profile",
    )
    store.register(helper, nonce, original, "a" * 64)
    dead_helper = Route.create(
        provider="codex",
        session_id=helper.session_id,
        device=helper.device,
        cwd=helper.cwd,
        pid=999999,
        generation=helper.generation,
        owner_identity=helper.owner_identity,
        profile_root=helper.profile_root,
    )
    registry = Registry(tmp_path / "state")
    registry.upsert(original)
    registry.upsert(dead_helper)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)

    assert store.helper_for_original(original, [original, dead_helper]) is None
    assert runtime.native_helper_tools(tmp_path / "state", original) == ("native_bootstrap",)
    replacement, replacement_nonce = store.reserve(original, "a" * 64, [original, dead_helper])

    assert replacement.state == "UNKNOWN"
    retired, pending = store.bindings()
    assert retired.state == "RETIRED"
    assert pending == replacement
    replacement_root = tmp_path / replacement.helper_directory
    replacement_root.mkdir()
    replacement_helper = route(
        replacement_root,
        session="00000000-0000-4000-8000-000000000003",
        generation="00000000-0000-4000-8000-000000000013",
        pid=103,
        profile_root=tmp_path / "profile",
    )
    registered = store.register(replacement_helper, replacement_nonce, original, "a" * 64)
    monkeypatch.setattr(Route, "process_is_live", lambda item: item.pid == replacement_helper.pid)

    assert registered.state == "REGISTERED"
    assert store.helper_for_original(original, [original, dead_helper, replacement_helper]) == replacement_helper
