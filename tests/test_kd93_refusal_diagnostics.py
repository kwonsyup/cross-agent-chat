from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import ChatError, Registry, Route, UnknownDeliveryError, session_key
from cross_agent_chat.recipient import RecipientToken
from cross_agent_chat.transport import remote_envelope

Phase = Literal[
    "target",
    "authorization",
    "reply_header",
    "reply_binding",
    "revalidation",
    "courier",
    "external",
]
SECRET = "fixture-secret /private/owner/path token=private-token alias=private-peer\nsecond line"


@dataclass
class Receiver:
    root: Path
    target: runtime.Target
    event_id: str
    envelope: str
    fail_at: Phase | None = None
    failure: ChatError | None = None
    calls: list[Phase] = field(default_factory=list)

    def visit(self, phase: Phase) -> None:
        self.calls.append(phase)
        if phase == self.fail_at:
            assert self.failure is not None
            raise self.failure

    def receive(self) -> dict[str, object]:
        return runtime.receive_remote(self.root, self.envelope, "100.64.0.11")


def receiver(root: Path, monkeypatch: pytest.MonkeyPatch, *, external: bool = False) -> Receiver:
    route = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="fixture",
        cwd=str(root),
        pid=os.getpid(),
    )
    Registry(root).upsert(route)
    target = runtime.Target(
        alias="external@fixture:endpoint:fixture" if external else route.alias,
        provider="external" if external else "claude",
        device=route.device,
        project=route.project,
        generation=route.generation,
        session_key=session_key(route.provider, route.session_id),
        remote=False,
        session_id=route.session_id,
        cwd=route.cwd,
        pid=route.pid,
    )
    event_id = str(uuid4())
    source_generation = str(uuid4())
    state = Receiver(
        root,
        target,
        event_id,
        remote_envelope(
            event_id=event_id,
            source_alias="codex@fixture:source:fixture",
            source_generation=source_generation,
            target_alias=target.alias,
            generation=target.generation,
            message="fixture generic request",
        ),
    )

    def targets(_root: Path, **_kwargs: object) -> list[runtime.Target]:
        state.visit("target")
        return [target]

    def authorize(
        _address: str, payload: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        state.visit("authorization")
        return {key: value for key, value in payload.items() if key != "operation"} | {
            "status": "AUTHORIZED"
        }

    def reply_token(*_args: object) -> RecipientToken:
        state.visit("reply_header")
        return RecipientToken(
            scope="remote", handle="a" * 64, generation=source_generation, node_id="nFixture"
        )

    def verify_reply(*_args: object) -> None:
        state.visit("reply_binding")

    original_routes = Registry.routes
    route_reads = 0

    def routes(registry: Registry) -> list[Route]:
        nonlocal route_reads
        route_reads += 1
        if route_reads == 2:
            state.visit("revalidation")
        return original_routes(registry)

    def accepted() -> dict[str, object]:
        return {
            "schema_version": 1,
            "event_id": event_id,
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": target.provider,
        }

    def courier(_path: Path, _payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        state.visit("courier")
        return accepted()

    def endpoint(*_args: object, **_kwargs: object) -> dict[str, object]:
        state.visit("external")
        return accepted()

    monkeypatch.setattr(runtime, "local_targets", targets)
    monkeypatch.setattr(runtime, "external_targets", targets)
    monkeypatch.setattr(runtime, "request_tailnet", authorize)
    monkeypatch.setattr(runtime, "_envelope_reply_token", reply_token)
    monkeypatch.setattr(runtime, "_verify_remote_reply_token", verify_reply)
    monkeypatch.setattr(Registry, "routes", routes)
    monkeypatch.setattr(runtime, "request_socket", courier)
    monkeypatch.setattr(runtime, "_accept_external", endpoint)
    return state


def legacy_pre_effect_error(
    response: dict[str, object], event_id: str, provider: str
) -> str | None:
    """Frozen old strict sender contract; no stage field or negotiation exists."""
    if set(response) != {"schema_version", "event_id", "status", "provider", "error"}:
        return None
    error = response.get("error")
    if (
        response.get("schema_version") != 1
        or response.get("event_id") != event_id
        or response.get("status") != "PRE_EFFECT_REJECTED"
        or response.get("provider") != provider
        or not isinstance(error, str)
        or not error
        or any(character in error for character in "\r\n\0")
    ):
        return None
    try:
        if len(error.encode()) > 256:
            return None
    except UnicodeEncodeError:
        return None
    return error


@pytest.mark.parametrize(
    ("phase", "expected", "external"),
    [
        ("target", runtime.REMOTE_TARGET_VALIDATION_FAILURE, False),
        ("authorization", runtime.REMOTE_SOURCE_AUTHORIZATION_FAILURE, False),
        ("reply_header", "reply token is invalid", False),
        ("reply_binding", runtime.REMOTE_REPLY_BINDING_FAILURE, False),
        ("revalidation", runtime.REMOTE_TARGET_VALIDATION_FAILURE, False),
        ("courier", runtime.REMOTE_COURIER_ADMISSION_FAILURE, False),
        ("external", runtime.REMOTE_EXTERNAL_ADMISSION_FAILURE, True),
    ],
)
def test_private_error_is_replaced_at_its_actual_callsite_without_later_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: Phase,
    expected: str,
    external: bool,
) -> None:
    state = receiver(tmp_path, monkeypatch, external=external)
    state.fail_at = phase
    state.failure = ChatError(SECRET)

    response = state.receive()

    assert response == {
        "schema_version": 1,
        "event_id": state.event_id,
        "status": "PRE_EFFECT_REJECTED",
        "provider": state.target.provider,
        "error": expected,
    }
    assert state.calls[-1] == phase
    assert state.calls.count(phase) == 1
    if phase not in {"courier", "external"}:
        assert "courier" not in state.calls and "external" not in state.calls
    assert SECRET not in str(response)
    assert "private-token" not in str(response) and "/private/owner" not in str(response)
    assert legacy_pre_effect_error(response, state.event_id, state.target.provider) == expected
    assert runtime.pre_effect_error(response, state.event_id, state.target.provider) == expected
    assert legacy_pre_effect_error({**response, "stage": phase}, state.event_id, "claude") is None


@pytest.mark.parametrize(
    "phase",
    [
        "target",
        "authorization",
        "reply_header",
        "reply_binding",
        "revalidation",
        "courier",
        "external",
    ],
)
def test_unknown_outcome_is_never_reclassified_by_the_current_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: Phase
) -> None:
    state = receiver(tmp_path, monkeypatch, external=phase == "external")
    state.fail_at = phase
    unknown = UnknownDeliveryError(SECRET)
    state.failure = unknown

    with pytest.raises(UnknownDeliveryError) as caught:
        state.receive()

    assert caught.value is unknown
    assert state.calls[-1] == phase and state.calls.count(phase) == 1


def test_lost_receipt_after_provider_invocation_stays_unknown_without_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = receiver(tmp_path, monkeypatch)
    provider_calls = 0

    def provider_then_lost_receipt(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        raise UnknownDeliveryError("fixture provider invoked, then receipt lost")

    monkeypatch.setattr(runtime, "request_socket", provider_then_lost_receipt)

    with pytest.raises(UnknownDeliveryError):
        state.receive()

    assert provider_calls == 1


@pytest.mark.parametrize("external", [False, True])
def test_accepted_result_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external: bool
) -> None:
    state = receiver(tmp_path, monkeypatch, external=external)

    assert state.receive() == {
        "schema_version": 1,
        "event_id": state.event_id,
        "status": "TRANSPORT_ACCEPTED",
        "to": state.target.alias,
        "provider": state.target.provider,
    }
    assert state.calls.count("external" if external else "courier") == 1


def test_unexpected_exception_type_is_not_newly_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = receiver(tmp_path, monkeypatch)

    def unrelated(*_args: object, **_kwargs: object) -> list[runtime.Target]:
        raise RuntimeError("fixture non-ChatError")

    monkeypatch.setattr(runtime, "local_targets", unrelated)
    with pytest.raises(RuntimeError, match="fixture non-ChatError"):
        state.receive()


@pytest.mark.parametrize(
    "reason",
    [
        runtime.COURIER_BUSY_PRE_EFFECT_REASON,
        runtime.COURIER_STOPPED_PRE_EFFECT_REASON,
        "Claude SendMessage was refused before delivery",
    ],
)
def test_trusted_courier_reasons_keep_their_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    state = receiver(tmp_path, monkeypatch)

    def refused(*_args: object, **_kwargs: object) -> dict[str, object]:
        state.visit("courier")
        return {
            "schema_version": 1,
            "event_id": state.event_id,
            "status": "PRE_EFFECT_REJECTED",
            "provider": "claude",
            "error": reason,
        }

    monkeypatch.setattr(runtime, "request_socket", refused)
    monkeypatch.setattr(runtime, "_pre_effect_retry_pause", lambda *_args: None)

    assert state.receive()["error"] == reason
    assert state.calls.count("courier") == 1
