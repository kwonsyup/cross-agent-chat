from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import SCHEMA_VERSION, ChatError, Registry, Route
from cross_agent_chat.native_helper import NativeDispatchStore, NativeHelperStore

ACCOUNT_DIGEST = "a" * 64


@dataclass(frozen=True)
class DispatchFixture:
    root: Path
    original: Route
    helper: Route
    event_id: str


def make_route(
    cwd: Path,
    *,
    session_id: str,
    profile_root: Path,
) -> Route:
    cwd.mkdir(parents=True, exist_ok=True)
    profile_root.mkdir(parents=True, exist_ok=True)
    return Route.create(
        provider="codex",
        session_id=session_id,
        device="studio",
        cwd=str(cwd),
        pid=1,
        owner_identity=ACCOUNT_DIGEST,
        profile_root=str(profile_root),
    )


def registered_fixture(tmp_path: Path) -> DispatchFixture:
    state_root = tmp_path / "state"
    profile_root = tmp_path / "profile"
    original = make_route(
        tmp_path / "original",
        session_id=str(uuid4()),
        profile_root=profile_root,
    )
    registry = Registry(state_root)
    registry.upsert(original)
    store = NativeHelperStore(state_root)
    binding, nonce = store.reserve(original, ACCOUNT_DIGEST)
    helper = make_route(
        tmp_path / binding.helper_directory,
        session_id=str(uuid4()),
        profile_root=profile_root,
    )
    registry.upsert(helper)
    store.register(helper, nonce, original, ACCOUNT_DIGEST)
    return DispatchFixture(state_root, original, helper, str(uuid4()))


def patch_dispatch_environment(
    monkeypatch: pytest.MonkeyPatch,
    fixture: DispatchFixture,
    *,
    request_socket: Callable[[Path, dict[str, object]], dict[str, object]],
) -> None:
    monkeypatch.setattr(runtime, "_native_hook_ready", lambda *_args: True)
    monkeypatch.setattr(runtime, "_native_account_digest", lambda *_args: ACCOUNT_DIGEST)
    monkeypatch.setattr(
        runtime,
        "recipient_owner_identity",
        lambda *_args: (ACCOUNT_DIGEST, Path("/usr/local/bin/codex")),
    )
    monkeypatch.setattr(runtime, "socket_path", lambda *_args: fixture.root / "helper.sock")
    monkeypatch.setattr(runtime, "request_socket", request_socket)


def dispatch_response(fixture: DispatchFixture, message: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "NATIVE_DISPATCH",
        "generation": fixture.helper.generation,
        "event_id": fixture.event_id,
        "message": message,
    }


def ack_response(fixture: DispatchFixture) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "NATIVE_DISPATCH_ACKED",
        "generation": fixture.helper.generation,
        "event_id": fixture.event_id,
    }


def test_dispatch_peeks_claims_unknown_before_ack_and_keeps_body_in_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = registered_fixture(tmp_path)
    body = "synthetic native body 🔒"
    operations: list[str] = []

    def request_socket(path: Path, payload: dict[str, object]) -> dict[str, object]:
        assert path == fixture.root / "helper.sock"
        operation = payload["operation"]
        assert isinstance(operation, str)
        operations.append(operation)
        if operation == "native_dispatch":
            assert NativeDispatchStore(fixture.root).dispatches() == []
            return dispatch_response(fixture, body)
        assert operation == "native_dispatch_ack"
        dispatches = NativeDispatchStore(fixture.root).dispatches()
        assert len(dispatches) == 1
        assert dispatches[0].state == "UNKNOWN"
        persisted = (fixture.root / "native-dispatches.json").read_text(encoding="utf-8")
        assert body not in persisted
        return ack_response(fixture)

    patch_dispatch_environment(monkeypatch, fixture, request_socket=request_socket)
    result = runtime.native_dispatch(fixture.root, fixture.helper, fixture.event_id)

    assert operations == ["native_dispatch", "native_dispatch_ack"]
    assert result == {
        "content": [{"type": "text", "text": "Native delivery was submitted."}],
        "_meta": {
            "native_args": {
                "threadId": fixture.original.session_id,
                "prompt": body,
            }
        },
    }
    assert body not in str(result["content"])
    dispatches = NativeDispatchStore(fixture.root).dispatches()
    assert len(dispatches) == 1
    assert dispatches[0].state == "UNKNOWN"
    assert dispatches[0].payload_sha256 == hashlib.sha256(body.encode()).hexdigest()


def test_duplicate_dispatch_rejects_before_peek(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = registered_fixture(tmp_path)
    operations: list[str] = []

    def request_socket(_path: Path, payload: dict[str, object]) -> dict[str, object]:
        operation = payload["operation"]
        assert isinstance(operation, str)
        operations.append(operation)
        if operation == "native_dispatch":
            return dispatch_response(fixture, "first body")
        return ack_response(fixture)

    patch_dispatch_environment(monkeypatch, fixture, request_socket=request_socket)
    runtime.native_dispatch(fixture.root, fixture.helper, fixture.event_id)
    operations.clear()

    with pytest.raises(ChatError, match="native helper dispatch is unavailable"):
        runtime.native_dispatch(fixture.root, fixture.helper, fixture.event_id)
    assert operations == []


def test_ack_failure_keeps_durable_unknown_claim_without_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = registered_fixture(tmp_path)
    body = "ack-failure body"
    operations: list[str] = []

    def request_socket(_path: Path, payload: dict[str, object]) -> dict[str, object]:
        operation = payload["operation"]
        assert isinstance(operation, str)
        operations.append(operation)
        if operation == "native_dispatch":
            return dispatch_response(fixture, body)
        raise ChatError("ack socket failed")

    patch_dispatch_environment(monkeypatch, fixture, request_socket=request_socket)
    with pytest.raises(ChatError, match="ack socket failed"):
        runtime.native_dispatch(fixture.root, fixture.helper, fixture.event_id)

    assert operations == ["native_dispatch", "native_dispatch_ack"]
    dispatches = NativeDispatchStore(fixture.root).dispatches()
    assert len(dispatches) == 1
    assert dispatches[0].state == "UNKNOWN"
    assert dispatches[0].payload_sha256 == hashlib.sha256(body.encode()).hexdigest()
    assert body not in (fixture.root / "native-dispatches.json").read_text(encoding="utf-8")


def test_mark_failure_stops_before_ack_and_leaves_no_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = registered_fixture(tmp_path)
    operations: list[str] = []

    def request_socket(_path: Path, payload: dict[str, object]) -> dict[str, object]:
        operation = payload["operation"]
        assert isinstance(operation, str)
        operations.append(operation)
        return dispatch_response(fixture, "mark-failure body")

    def fail_claim(*_args: object, **_kwargs: object) -> None:
        raise ChatError("dispatch mark failed")

    patch_dispatch_environment(monkeypatch, fixture, request_socket=request_socket)
    monkeypatch.setattr(NativeDispatchStore, "claim", fail_claim)
    with pytest.raises(ChatError, match="dispatch mark failed"):
        runtime.native_dispatch(fixture.root, fixture.helper, fixture.event_id)

    assert operations == ["native_dispatch"]
    assert not (fixture.root / "native-dispatches.json").exists()


def test_invalid_event_and_helper_identity_are_rejected_before_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = registered_fixture(tmp_path)
    operations: list[str] = []

    def request_socket(_path: Path, _payload: dict[str, object]) -> dict[str, object]:
        operations.append("unexpected")
        raise AssertionError("invalid dispatch reached helper socket")

    patch_dispatch_environment(monkeypatch, fixture, request_socket=request_socket)
    with pytest.raises(ChatError, match="event id is invalid"):
        runtime.native_dispatch(fixture.root, fixture.helper, "not-a-uuid")

    wrong_helper = make_route(
        tmp_path / "wrong-helper",
        session_id=str(uuid4()),
        profile_root=Path(fixture.original.profile_root or tmp_path / "profile"),
    )
    with pytest.raises(ChatError, match="native helper dispatch is unavailable"):
        runtime.native_dispatch(fixture.root, wrong_helper, fixture.event_id)
    assert operations == []


@pytest.mark.parametrize("variant", ["generation", "profile"])
def test_stale_helper_generation_or_profile_is_rejected_before_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    fixture = registered_fixture(tmp_path)
    operations: list[str] = []

    def request_socket(_path: Path, _payload: dict[str, object]) -> dict[str, object]:
        operations.append("unexpected")
        raise AssertionError("stale helper reached helper socket")

    patch_dispatch_environment(monkeypatch, fixture, request_socket=request_socket)
    if variant == "generation":
        stale_helper = replace(fixture.helper, generation=str(uuid4()))
    else:
        stale_helper = replace(fixture.helper, profile_root=str(tmp_path / "other-profile"))
    with pytest.raises(ChatError, match="native helper dispatch is unavailable"):
        runtime.native_dispatch(fixture.root, stale_helper, fixture.event_id)
    assert operations == []


def test_account_digest_mismatch_is_rejected_before_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = registered_fixture(tmp_path)
    operations: list[str] = []

    def request_socket(_path: Path, _payload: dict[str, object]) -> dict[str, object]:
        operations.append("unexpected")
        raise AssertionError("wrong-account helper reached helper socket")

    def digest(route: Route) -> str:
        return "b" * 64 if route.session_id == fixture.helper.session_id else ACCOUNT_DIGEST

    patch_dispatch_environment(monkeypatch, fixture, request_socket=request_socket)
    monkeypatch.setattr(runtime, "_native_account_digest", digest)
    with pytest.raises(ChatError, match="native helper dispatch is unavailable"):
        runtime.native_dispatch(fixture.root, fixture.helper, fixture.event_id)
    assert operations == []
