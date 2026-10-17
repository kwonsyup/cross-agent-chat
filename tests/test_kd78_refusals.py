from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.claude_runtime import ClaudeSendMessageRefused
from cross_agent_chat.core import ChatError, Registry, Route, UnknownDeliveryError, session_key
from cross_agent_chat.transport import remote_envelope


def _remote_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accept_response: object,
) -> dict[str, object]:
    target = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    runtime_target = runtime.Target(
        alias=target.alias,
        provider=target.provider,
        device=target.device,
        project=target.project,
        generation=target.generation,
        session_key=session_key(target.provider, target.session_id),
        remote=False,
        session_id=target.session_id,
        cwd=target.cwd,
        pid=target.pid,
    )
    event_id = str(uuid4())
    source_alias = "codex@source:project:sender"
    source_generation = str(uuid4())
    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source_alias,
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message="controlled message",
    )

    def authorize(_address: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        return {
            **{key: value for key, value in payload.items() if key != "operation"},
            "status": "AUTHORIZED",
        }

    def socket(_path: Path, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if payload["operation"] == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "alias": target.alias,
                "generation": target.generation,
            }
        if isinstance(accept_response, BaseException):
            raise accept_response
        if callable(accept_response):
            return accept_response(target, payload)
        return accept_response  # type: ignore[return-value]

    monkeypatch.setattr(runtime, "request_tailnet", authorize)
    monkeypatch.setattr(runtime, "request_socket", socket)
    monkeypatch.setattr(runtime, "local_targets", lambda *_args, **_kwargs: [runtime_target])
    return runtime.receive_remote(tmp_path, envelope, "100.64.0.11")


def test_provider_refusal_keeps_safe_reason_through_remote_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def provider_refuses(_target: Route, payload: dict[str, object]) -> dict[str, object]:
        route = Registry(tmp_path).routes()[0]
        monkeypatch.setattr(runtime, "exact_agent", lambda *_args: {"name": "fixture"})
        monkeypatch.setattr(runtime, "discover_target_ref", lambda _name: "fixture-ref")
        monkeypatch.setattr(runtime, "claude_alias", lambda *_args: route.alias)
        monkeypatch.setattr(runtime, "executable", lambda: "fixture-claude")

        def refused(*_args: object) -> None:
            raise ClaudeSendMessageRefused("provider detail contains private text")

        monkeypatch.setattr(runtime, "sendmessage", refused)
        return runtime.courier_accept(
            route,
            None,
            str(payload["event_id"]),
            str(payload["message"]),
        )

    response = _remote_delivery(tmp_path, monkeypatch, provider_refuses)

    assert isinstance(response["event_id"], str) and response["event_id"]
    assert {key: value for key, value in response.items() if key != "event_id"} == {
        "schema_version": 1,
        "status": "PRE_EFFECT_REJECTED",
        "provider": "claude",
        "error": "Claude SendMessage was refused before delivery",
    }
    assert "private" not in str(response)


def test_remote_consumer_preserves_unknown_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(UnknownDeliveryError):
        _remote_delivery(
            tmp_path,
            monkeypatch,
            UnknownDeliveryError("receipt missing after possible provider effect"),
        )


def test_remote_consumer_suppresses_unrecognized_secret_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _remote_delivery(
        tmp_path,
        monkeypatch,
        lambda _target, payload: {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "PRE_EFFECT_REJECTED",
            "provider": "claude",
            "error": "fixture-secret arbitrary provider payload",
        },
    )

    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["error"] == "remote destination rejected before provider effect"
    assert "fixture-secret" not in str(response)


def test_remote_consumer_exposes_exact_cac_route_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _remote_delivery(
        tmp_path,
        monkeypatch,
        ChatError("remote target changed before transport acceptance"),
    )

    assert response["error"] == "remote target changed before transport acceptance"
