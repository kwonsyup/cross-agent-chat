"""Custody results describe observed ingress without claiming original use."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.codex import CodexCourier
from cross_agent_chat.core import ChatError, Registry, Route, session_key
from cross_agent_chat.recipient import local_token, remote_token
from cross_agent_chat.tailnet import TailnetIdentity
from cross_agent_chat.tailnet_broker import handle_broker_request


def _route(root: Path, cwd: Path) -> Route:
    route = Route.create(
        provider="codex", session_id=str(uuid4()), device="test", cwd=str(cwd), pid=os.getpid()
    )
    Registry(root).upsert(route)
    return route


def test_stop_custody_result_does_not_promise_parked_or_active_original_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real Stop queue accepts a body while the original has not consumed it."""
    root = tmp_path / "state"
    source, target = _route(root, tmp_path), _route(root, tmp_path)
    courier = CodexCourier(alias=target.alias, generation=target.generation)
    accepted = False
    admissions = 0

    def socket(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        nonlocal accepted, admissions
        if payload["operation"] == "health":
            assert not accepted, "optional metadata must not probe after accepted delivery"
            return runtime.courier_health(
                target, courier, include_delivery_mode=True, include_delivery_mechanism=True
            )
        assert "destination_receiving" not in payload
        assert payload["operation"] == "accept"
        accepted = True
        admissions += 1
        return runtime.courier_accept(
            target, courier, str(payload["event_id"]), str(payload["message"])
        )

    monkeypatch.setattr(runtime, "request_socket", socket)
    handle = local_token(root, session_key(target.provider, target.session_id), target.generation)
    event = str(uuid4())
    result = runtime.send(root, source, handle, "independent task", event_id=event)
    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["destination_receiving"] == {
        "mode": "codex_stop_bound",
        "mechanism": "stop_bound",
        "parked_wake": False,
        "active_turn_input": False,
        "delivery_observation": "not_observed",
    }
    assert [item["event_id"] for item in courier.peek()] == [event]
    # A retained event is not a fresh dispatch or a current capability observation.
    accepted = False
    with pytest.raises(ChatError, match="event id is unavailable"):
        runtime.send(root, source, handle, "independent task", event_id=event)
    assert admissions == 1
    assert len(courier.peek()) == 1


def test_remote_sender_receives_negotiated_mechanism_without_extending_custody_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health -> strict broker roster -> sender result preserves the observed mechanism."""
    sender_root, broker_root = tmp_path / "sender", tmp_path / "broker"
    source, target = _route(sender_root, tmp_path), _route(broker_root, tmp_path)
    courier = CodexCourier(alias=target.alias, generation=target.generation)
    frames: list[dict[str, object]] = []
    monkeypatch.setattr(
        runtime,
        "request_socket",
        lambda *_a, **_k: runtime.courier_health(
            target, courier, include_delivery_mode=True, include_delivery_mechanism=True
        ),
    )
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSender", peers={"nTarget": "100.64.0.2"}),
    )

    def wire(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        frames.append(payload)
        if payload["operation"] == "peers":
            return handle_broker_request(broker_root, payload, "100.64.0.1")
        assert set(payload) == {"schema_version", "operation", "envelope"}
        envelope = json.loads(str(payload["envelope"]))
        return runtime.courier_accept(target, courier, envelope["event_id"], envelope["message"])

    monkeypatch.setattr(runtime, "request_tailnet", wire)
    result = runtime.send(
        sender_root,
        source,
        remote_token("nTarget", session_key(target.provider, target.session_id), target.generation),
        "independent remote task",
    )
    assert result["status"] == "TRANSPORT_ACCEPTED"
    receiving = cast(dict[str, object], result["destination_receiving"])
    assert receiving["mechanism"] == "stop_bound"
    assert receiving["parked_wake"] is False
    assert frames[0]["include_delivery_mechanism"] is True
    # A legacy strict reader sees exactly the old response until it asks for metadata.
    legacy = handle_broker_request(
        broker_root, {"schema_version": 1, "operation": "peers"}, "100.64.0.1"
    )
    legacy_peers = cast(list[dict[str, object]], legacy["peers"])
    assert "delivery_mechanism" not in legacy_peers[0]


def test_old_remote_broker_keeps_send_usable_and_receiving_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained broker rejecting new metadata flags still accepts its old custody frame."""
    root = tmp_path / "sender"
    source = _route(root, tmp_path)
    target = runtime.Target(
        alias="codex@remote:task:123456789abc",
        provider="codex",
        device="remote",
        project="task",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=True,
        tailnet_address="100.64.0.2",
        tailnet_node_id="nTarget",
    )
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSender", peers={"nTarget": "100.64.0.2"}),
    )
    calls: list[dict[str, object]] = []

    def old(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        calls.append(payload)
        if payload["operation"] == "receive":
            envelope = json.loads(str(payload["envelope"]))
            return {
                "schema_version": 1,
                "event_id": envelope["event_id"],
                "status": "TRANSPORT_ACCEPTED",
                "to": target.alias,
                "provider": "codex",
            }
        if set(payload) != {"schema_version", "operation"}:
            raise ChatError("old broker rejected optional fields")
        row = target.public(include_handle=False)
        row.update(generation=target.generation, session_key=target.session_key)
        return {"schema_version": 1, "peers": [row]}

    monkeypatch.setattr(runtime, "request_tailnet", old)
    result = runtime.send(
        root, source, remote_token("nTarget", target.session_key, target.generation), "old task"
    )
    assert result["status"] == "TRANSPORT_ACCEPTED"
    receiving = cast(dict[str, object], result["destination_receiving"])
    assert receiving["mode"] == "unknown"
    assert receiving["mechanism"] == "unknown"
    assert receiving["parked_wake"] == "unknown"
    assert sum(call["operation"] == "receive" for call in calls) == 1


def test_exact_title_selects_one_original_and_duplicate_title_refuses_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Display text must resolve one live identity; duplicates cannot choose a successor."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    first, second = _route(root, tmp_path), _route(root, tmp_path)

    def target(route: Route) -> runtime.Target:
        return runtime.Target(
            alias=route.alias,
            provider=route.provider,
            device=route.device,
            project=route.project,
            generation=route.generation,
            session_key=session_key(route.provider, route.session_id),
            remote=False,
            session_id=route.session_id,
            cwd=route.cwd,
            pid=route.pid,
            title="Review task",
        )

    rows = [target(first)]
    monkeypatch.setattr(runtime, "local_targets", lambda *_a, **_k: rows)
    monkeypatch.setattr(runtime, "_with_codex_titles", lambda _root, targets, _deadline: targets)
    monkeypatch.setattr(runtime, "_remote_discovery", lambda **_: ([], True))
    selected: list[runtime.Target] = []

    def capture(
        _root: object,
        _source: object,
        target: runtime.Target,
        _message: object,
        **_: object,
    ) -> dict[str, object]:
        selected.append(target)
        return {}

    monkeypatch.setattr(runtime, "_send_local_target", capture)
    runtime.send(root, source, "review TASK", "one task")
    assert selected == [rows[0]]
    rows.append(target(second))
    with pytest.raises(ChatError, match="ambiguous"):
        runtime.send(root, source, "Review task", "must refuse")
    assert selected == [rows[0]]
    # Near titles are never used as a hidden fallback selector.
    with pytest.raises(ChatError, match="exact title"):
        runtime.send(root, source, "Review", "must refuse")
    assert selected == [rows[0]]


@pytest.mark.parametrize("mechanism", ["direct_queue", "native_helper"])
def test_queue_health_alone_does_not_qualify_parked_wake_or_active_input(
    mechanism: runtime.DeliveryMechanism,
) -> None:
    """A queue route can be healthy while the original waits behind an active turn."""
    target = runtime.Target(
        alias="codex@test:task:123456789abc",
        provider="codex",
        device="test",
        project="task",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
        delivery_mode="codex_experimental_queue",
        delivery_mechanism=mechanism,
    )
    receiving = runtime.destination_receiving(target)
    assert receiving["parked_wake"] == "unknown"
    assert receiving["active_turn_input"] == "unknown"
    assert receiving["delivery_observation"] == "not_observed"


def test_title_dispatch_refuses_a_hidden_remote_duplicate_when_title_enrichment_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity discovery can be complete while another actor's matching title is unavailable."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    local = runtime.Target(
        alias="codex@test:local:123456789abc",
        provider="codex",
        device="test",
        project="local",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
        title="Review task",
    )
    # Its real provider title is also Review task, but the optional title query
    # times out. The base identity row remains live and identity-complete.
    remote = runtime.Target(
        alias="codex@remote:review:987654321abc",
        provider="codex",
        device="remote",
        project="review",
        generation=str(uuid4()),
        session_key="b" * 64,
        remote=True,
    )
    row = remote.public(include_handle=False)
    row.update(generation=remote.generation, session_key=remote.session_key)
    probes: list[dict[str, object]] = []
    effects: list[runtime.Target] = []

    def wire(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        probes.append(payload)
        if payload.get("include_title"):
            raise runtime.UnknownDeliveryError("optional title response timed out")
        return {"schema_version": 1, "peers": [row]}

    monkeypatch.setattr(runtime, "request_tailnet", wire)
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nLocal", peers={"nRemote": "100.64.0.2"}),
    )
    monkeypatch.setattr(runtime, "local_targets", lambda *_a, **_k: [local])
    monkeypatch.setattr(runtime, "_with_codex_titles", lambda _r, rows, _d: rows)

    def capture(
        _r: object, _s: object, target: runtime.Target, _m: object, **_: object
    ) -> dict[str, object]:
        effects.append(target)
        return {}

    monkeypatch.setattr(runtime, "_send_local_target", capture)
    # This exercises the real optional-negotiation failure, rather than supplying
    # two already complete duplicate-title rows to the selector.
    with pytest.raises(ChatError, match="title metadata is incomplete"):
        runtime.send(root, source, "Review task", "must not pick the visible twin")
    assert any(probe.get("include_title") for probe in probes)
    assert effects == []


@pytest.mark.parametrize("unknown_is_remote", [False, True])
def test_title_metadata_failure_preserves_exact_alias_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unknown_is_remote: bool,
) -> None:
    """Unknown title metadata cannot prevent a uniquely authenticated full alias send."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    selected = runtime.Target(
        alias="codex@test:local:123456789abc",
        provider="codex",
        device="test",
        project="local",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
        title="Review task",
    )
    unknown = runtime.Target(
        alias="codex@remote:review:987654321abc",
        provider="codex",
        device="remote",
        project="review",
        generation=str(uuid4()),
        session_key="b" * 64,
        remote=unknown_is_remote,
    )
    effects: list[runtime.Target] = []
    monkeypatch.setattr(
        runtime,
        "local_targets",
        lambda *_a, **_k: [selected] + ([] if unknown_is_remote else [unknown]),
    )
    monkeypatch.setattr(
        runtime, "_remote_discovery", lambda **_: ([unknown] if unknown_is_remote else [], True)
    )
    monkeypatch.setattr(runtime, "_with_codex_titles", lambda _r, rows, _d: rows)

    def capture(
        _r: object, _s: object, target: runtime.Target, _m: object, **_: object
    ) -> dict[str, object]:
        effects.append(target)
        return {}

    monkeypatch.setattr(runtime, "_send_local_target", capture)
    runtime.send(root, source, selected.alias, "exact alias task")
    assert effects == [selected]
    with pytest.raises(ChatError, match="title metadata is incomplete"):
        runtime.send(root, source, "Review task", "must not guess")
    assert effects == [selected]


def test_devin_route_reports_active_turn_input_without_parked_wake() -> None:
    """Devin receives at the next root tool boundary but cannot wake while idle."""
    target = runtime.Target(
        alias="devin@test:task",
        provider="devin",
        device="test",
        project="task",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
        delivery_mode="devin_stop_or_prompt_bound",
        delivery_mechanism="devin_prompt_bound",
    )
    receiving = runtime.destination_receiving(target)
    assert receiving["parked_wake"] is False
    assert receiving["active_turn_input"] is True
    assert receiving["delivery_observation"] == "not_observed"
