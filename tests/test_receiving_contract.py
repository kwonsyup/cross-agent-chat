"""Custody results describe observed ingress without claiming original use."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.codex import CodexCourier
from cross_agent_chat.core import ChatError, IntentStatus, IntentStore, Registry, Route, session_key
from cross_agent_chat.devin import DevinCurrentBoundary
from cross_agent_chat.native_helper import NativeDispatchStore, NativeHelperStore
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
    monkeypatch.setattr(runtime, "_local_discovery", lambda *_a, **_k: (rows, True))
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
    monkeypatch.setattr(runtime, "_local_discovery", lambda *_a, **_k: ([local], True))
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
        "_local_discovery",
        lambda *_a, **_k: ([selected] + ([] if unknown_is_remote else [unknown]), True),
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


@pytest.mark.parametrize(
    ("mode", "mechanism", "active"),
    [
        # A v0.5.2-or-unnegotiated receiver presents only the legacy values.
        ("devin_stop_or_prompt_bound", "devin_prompt_bound", False),
        ("devin_tool_boundary", "devin_tool_boundary", True),
    ],
)
def test_devin_active_turn_input_is_reported_only_when_the_receiver_affirms_it(
    mode: runtime.DeliveryMode, mechanism: runtime.DeliveryMechanism, active: bool
) -> None:
    """Idle Devin never wakes; active-turn input needs the receiver's own affirmation."""
    target = runtime.Target(
        alias="devin@test:task",
        provider="devin",
        device="test",
        project="task",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
        delivery_mode=mode,
        delivery_mechanism=mechanism,
    )
    receiving = runtime.destination_receiving(target)
    assert receiving["parked_wake"] is False
    assert receiving["active_turn_input"] is active
    assert receiving["delivery_observation"] == "not_observed"


def _devin_route(tmp_path: Path) -> Route:
    return Route.create(
        provider="devin", session_id=str(uuid4()), device="m4", cwd=str(tmp_path), pid=os.getpid()
    )


def _target(
    *,
    provider: Literal["codex", "devin"] = "codex",
    mode: runtime.DeliveryMode | None = None,
    mechanism: runtime.DeliveryMechanism | None = None,
    boundary: DevinCurrentBoundary | None = None,
) -> runtime.Target:
    return runtime.Target(
        alias=f"{provider}@test:task" + (":123456789abc" if provider == "codex" else ""),
        provider=provider,
        device="test",
        project="task",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
        delivery_mode=mode,
        delivery_mechanism=mechanism,
        current_boundary=boundary,
    )


def test_one_observed_basis_grades_receiving_and_reply_identically() -> None:
    """The direct-queue contradiction's counterexample, on the shared basis.

    Before the fix, `reply_delivery` read only the mode and called every
    `codex_experimental_queue` "while_idle" while `destination_receiving`
    reported parked wake "unknown" for the same unqualified direct queue.
    Now both derive from one (mode, mechanism) qualification, so the only way
    to say "while_idle" is a mechanism that is actually qualified for it.
    """
    assert runtime._receiving_qualification("codex_experimental_queue", "direct_queue") == (
        "unknown",
        "unknown",
        "unknown",
    )
    assert runtime._receiving_qualification("codex_experimental_queue", "native_helper") == (
        "unknown",
        "unknown",
        "unknown",
    )
    assert runtime._receiving_qualification("claude_native_cross_session", "claude_native") == (
        "unknown",
        "unknown",
        "unknown",
    )
    assert runtime._receiving_qualification("codex_daemon_input", "owning_daemon") == (
        True,
        True,
        "while_idle",
    )
    assert runtime._receiving_qualification("codex_stop_bound", "stop_bound") == (
        False,
        False,
        "next_turn",
    )
    # A daemon-qualified mechanism plugs in only by reporting the pair: no
    # version string appears anywhere in the basis, so Lane A's qualification
    # needs no second codepath here.
    receiving = runtime.destination_receiving(
        _target(mode="codex_experimental_queue", mechanism="direct_queue")
    )
    assert receiving["parked_wake"] == "unknown"
    assert receiving["active_turn_input"] == "unknown"
    assert "current_boundary" not in receiving
    receiving = runtime.destination_receiving(
        _target(mode="codex_daemon_input", mechanism="owning_daemon")
    )
    assert receiving["parked_wake"] is True
    assert receiving["active_turn_input"] is True


@pytest.mark.parametrize(
    ("boundary", "active"),
    [
        ("next_prompt_custom_subagent", False),
        ("next_prompt_unobserved_launch", False),
        ("root_tools_only", "limited"),
        ("unrestricted", True),
    ],
)
def test_devin_current_restriction_narrows_active_input_and_is_disclosed(
    boundary: DevinCurrentBoundary, active: object
) -> None:
    """A held Devin can never be reported as accepting active-turn input."""
    receiving = runtime.destination_receiving(
        _target(
            provider="devin",
            mode="devin_tool_boundary",
            mechanism="devin_tool_boundary",
            boundary=boundary,
        )
    )
    assert receiving["parked_wake"] is False
    assert receiving["active_turn_input"] == active
    assert receiving["current_boundary"] == boundary


@pytest.mark.parametrize(
    ("mode", "mechanism", "active"),
    [
        ("devin_stop_or_prompt_bound", "devin_prompt_bound", False),
        (None, None, "unknown"),
    ],
)
def test_root_only_restriction_never_upgrades_a_deferred_or_unknown_route(
    mode: runtime.DeliveryMode | None,
    mechanism: runtime.DeliveryMechanism | None,
    active: object,
) -> None:
    """Custody evidence is a restriction, not proof the loaded receiver can
    take tool-boundary input: a retained legacy courier stays deferred."""
    receiving = runtime.destination_receiving(
        _target(provider="devin", mode=mode, mechanism=mechanism, boundary="root_tools_only")
    )
    assert receiving["active_turn_input"] == active
    assert receiving["current_boundary"] == "root_tools_only"


def test_restriction_refines_the_same_evidence_custody_enforces(
    tmp_path: Path,
) -> None:
    """Store-level mapping: each hold reason names its own boundary, and any
    remaining child bookkeeping without a hold is root-tools only."""
    from cross_agent_chat.devin import DevinSubagentStore

    root = tmp_path / "state"
    IntentStore(root)
    store = DevinSubagentStore(root)
    session = "devin-restriction"
    assert store.custody(session) is None
    assert store.restriction(session) == "unrestricted"
    store.mark_uncertain(session)
    assert store.custody(session) == "hold"
    assert store.restriction(session) == "next_prompt_unobserved_launch"
    store.clear(session)
    # An observed custom launch that may nest holds until the next prompt.
    store.launch(session, "tool-1", object())
    store.launched(session, "tool-1", "Background subagent started with agent_id=child-1")
    assert store.custody(session) == "hold"
    assert store.restriction(session) == "next_prompt_custom_subagent"
    # A finished non-nesting launch leaves bookkeeping without a hold: only
    # root-only tool boundaries remain provable.
    other = "devin-restriction-other"
    store.launch(other, "tool-2", "subagent_explore")
    store.launched(other, "tool-2", "Background subagent started with agent_id=child-2 finished")
    assert store.custody(other) == "root_tools"
    assert store.restriction(other) == "root_tools_only"


def test_devin_hold_disclosed_on_a_legacy_mode_never_promises_active_input() -> None:
    """Custody can hold before the tool hook ever ran; the reader must see it."""
    receiving = runtime.destination_receiving(
        _target(
            provider="devin",
            mode="devin_stop_or_prompt_bound",
            mechanism="devin_prompt_bound",
            boundary="next_prompt_unobserved_launch",
        )
    )
    assert receiving["active_turn_input"] is False
    assert receiving["current_boundary"] == "next_prompt_unobserved_launch"


def test_current_boundary_stays_off_rows_a_reader_did_not_negotiate() -> None:
    """Old readers get byte-identical shapes; old couriers keep the peer."""
    held = _target(
        provider="devin",
        mode="devin_tool_boundary",
        mechanism="devin_tool_boundary",
        boundary="next_prompt_custom_subagent",
    )
    old_shape = held.public(
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        include_devin_tool_boundary=True,
        include_handle=False,
    )
    assert "current_boundary" not in old_shape
    row = held.public(
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        include_devin_tool_boundary=True,
        include_current_boundary=True,
        include_handle=False,
    )
    assert row["current_boundary"] == "next_prompt_custom_subagent"
    roster_row = dict(row)
    roster_row.update(generation=held.generation, session_key=held.session_key)
    roster = {"schema_version": 1, "peers": [roster_row]}

    def parse(raw: dict[str, object], *, negotiated: bool) -> list[runtime.Target]:
        return runtime._targets_from_tailnet(
            "100.64.0.2",
            raw,
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_devin=True,
            include_devin_tool_boundary=True,
            include_current_boundary=negotiated,
        )

    # A reader that never asked must reject the new key outright, not drop the
    # peer silently or half-apply it.
    with pytest.raises(ChatError, match="invalid discovery"):
        parse(roster, negotiated=False)
    [parsed] = parse(roster, negotiated=True)
    assert parsed.current_boundary == "next_prompt_custom_subagent"
    assert runtime.destination_receiving(parsed)["active_turn_input"] is False
    # An old courier simply omits the key: the peer is retained and the
    # boundary stays conservatively unobserved, never read as unrestricted.
    old_row = dict(old_shape)
    old_row.update(generation=held.generation, session_key=held.session_key)
    [legacy] = parse({"schema_version": 1, "peers": [old_row]}, negotiated=True)
    assert legacy.current_boundary is None
    assert "current_boundary" not in runtime.destination_receiving(legacy)
    assert runtime.destination_receiving(legacy)["active_turn_input"] is True
    # The restriction is Devin-only: another provider's row carrying it is
    # malformed rather than silently kept.
    foreign = _target(provider="codex", mode="codex_stop_bound", mechanism="stop_bound")
    foreign_row = foreign.public(
        include_delivery_mode=True, include_delivery_mechanism=True, include_handle=False
    )
    foreign_row.update(
        generation=foreign.generation,
        session_key=foreign.session_key,
        current_boundary="unrestricted",
    )
    with pytest.raises(ChatError, match="invalid discovery"):
        parse({"schema_version": 1, "peers": [foreign_row]}, negotiated=True)


def test_devin_health_publishes_current_boundary_only_when_asked(
    tmp_path: Path,
) -> None:
    route = _devin_route(tmp_path)
    asked = runtime.courier_health(
        route,
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        include_current_boundary=True,
        devin_current_boundary="root_tools_only",
    )
    assert asked["current_boundary"] == "root_tools_only"
    unasked = runtime.courier_health(
        route,
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        devin_current_boundary="root_tools_only",
    )
    assert "current_boundary" not in unasked
    # Never emitted for another provider even when the requester asks.
    codex = _route(tmp_path / "state", tmp_path)
    assert "current_boundary" not in runtime.courier_health(
        codex,
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        include_current_boundary=True,
        devin_current_boundary="root_tools_only",
    )


def test_local_target_reads_the_custody_store_and_validates_the_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local boundary comes from the shared store: exact even when the
    courier answering health cannot emit the negotiated field."""
    from cross_agent_chat.devin import DevinSubagentStore

    root = tmp_path / "state"
    route = _devin_route(tmp_path)
    IntentStore(root)  # creates the root with private modes before the store write
    DevinSubagentStore(root).mark_uncertain(route.session_id)
    SocketStub = Callable[[Path, dict[str, object]], dict[str, object]]

    def answer(boundary: object = "next_prompt_unobserved_launch") -> SocketStub:
        def socket(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
            assert payload["include_current_boundary"] is True
            response: dict[str, object] = {
                "schema_version": 1,
                "status": "READY",
                "generation": route.generation,
                "alias": route.alias,
                "delivery_mode": "devin_tool_boundary",
                "delivery_mechanism": "devin_tool_boundary",
            }
            if boundary is not None:
                response["current_boundary"] = boundary
            return response

        return socket

    monkeypatch.setattr(runtime, "request_socket", answer())
    parsed = runtime._local_target(root, route)
    assert parsed is not None
    assert parsed.current_boundary == "next_prompt_unobserved_launch"
    # An old courier ignores the flag and omits the key: keep the peer, and
    # the boundary still comes from the store the courier itself reads.
    monkeypatch.setattr(runtime, "request_socket", answer(boundary=None))
    legacy = runtime._local_target(root, route)
    assert legacy is not None
    assert legacy.current_boundary == "next_prompt_unobserved_launch"
    # An unrecognized restriction is an invalid shape, not a guess.
    monkeypatch.setattr(runtime, "request_socket", answer(boundary="probably_fine"))
    assert runtime._local_target(root, route) is None
    # And it never attaches to a non-Devin route.
    codex_route = _route(root, tmp_path)
    monkeypatch.setattr(
        runtime,
        "request_socket",
        lambda _p, _payload, **_k: {
            "schema_version": 1,
            "status": "READY",
            "generation": codex_route.generation,
            "alias": codex_route.alias,
            "delivery_mode": "codex_stop_bound",
            "delivery_mechanism": "stop_bound",
            "current_boundary": "unrestricted",
        },
    )
    assert runtime._local_target(root, codex_route) is None


def test_courier_inspect_reports_volatile_custody_without_bodies() -> None:
    """Pending, handed-off and unseen are distinguishable; no body leaks."""
    courier = CodexCourier(alias="codex@test:task:123456789abc", generation=str(uuid4()))
    pending, handed, unseen = str(uuid4()), str(uuid4()), str(uuid4())
    courier.accept(pending, "a secret body that must never appear in an observation")
    courier.accept(handed, "another opaque body")
    assert courier.inspect(pending)["event_state"] == "pending"
    assert courier.inspect(unseen)["event_state"] == "unseen"
    courier.acknowledge([handed])
    observed = courier.inspect(handed)
    assert observed["event_state"] == "handed_off"
    assert observed["pending_count"] == 1
    assert isinstance(observed["oldest_pending_age_seconds"], float)
    rendered = json.dumps(observed)
    assert "secret" not in rendered
    assert "body" not in rendered
    # A same-incarnation restart analogue: clear() forgets everything, and
    # the event degrades to unseen -- never still reported as handed off.
    courier.clear()
    assert courier.inspect(pending)["event_state"] == "unseen"


def test_owner_event_status_reads_courier_custody_without_touching_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source, target = _route(root, tmp_path), _route(root, tmp_path)
    store = IntentStore(root)
    event_id = store.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)
    store.mark(event_id, "TRANSPORT_ACCEPTED")
    before = store.path.read_bytes()
    requests: list[dict[str, object]] = []
    SocketStub = Callable[[Path, dict[str, object]], dict[str, object]]

    def courier_answer(state: str) -> dict[str, object]:
        base: dict[str, object] = {
            "schema_version": 1,
            "status": "INSPECTED",
            "generation": target.generation,
            "courier_version": "0.5.3",
            "courier_incarnation": "incarnation-1",
            "event_state": state,
        }
        if state in {"pending", "handed_off", "unseen"}:
            base["pending_count"] = 2
            base["oldest_pending_age_seconds"] = 4.25
        return base

    def socket(answer: dict[str, object]) -> SocketStub:
        def stub(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
            requests.append(payload)
            assert payload["operation"] == "inspect"
            assert payload["event_id"] == event_id
            assert set(payload) == {"schema_version", "operation", "generation", "event_id"}
            return answer

        return stub

    monkeypatch.setattr(runtime, "request_socket", socket(courier_answer("pending")))
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    assert observation["stage"] == "pending_in_courier"
    assert observation["pending_count"] == 2
    assert observation["oldest_pending_age_seconds"] == 4.25
    assert observation["courier_version"] == "0.5.3"
    assert observation["courier_incarnation"] == "incarnation-1"

    monkeypatch.setattr(runtime, "request_socket", socket(courier_answer("handed_off")))
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    # "Handed off" must never present as consumption: the only sentence about
    # consumption explicitly denies it.
    assert observation["stage"] == "handed_off"
    assert "never evidence the original session consumed" in cast(str, observation["detail"])

    monkeypatch.setattr(runtime, "request_socket", socket(courier_answer("unseen")))
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    assert observation["stage"] == "unknown"
    assert observation["reason"] == "no_courier_record"

    def unavailable(*_a: object, **_k: object) -> dict[str, object]:
        raise ChatError("session courier is unavailable")

    monkeypatch.setattr(runtime, "request_socket", unavailable)
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    assert observation["stage"] == "unknown"
    assert observation["reason"] == "courier_unavailable"

    # Inspect is a read-only operation: the only request it ever issues is
    # "inspect", the durable intent bytes never change, and no message field
    # appears in what the courier was asked or what the caller receives.
    assert {request["operation"] for request in requests} == {"inspect"}
    assert store.path.read_bytes() == before
    assert "message" not in json.dumps(observation)


def test_devin_receiver_affirms_tool_boundaries_only_when_asked_and_observed(
    tmp_path: Path,
) -> None:
    """New receiver: old senders keep the legacy shape; new senders get evidence-based values."""
    route = _devin_route(tmp_path)

    def health(*, asked: bool, observed: bool) -> tuple[object, object]:
        response = runtime.courier_health(
            route,
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_devin_tool_boundary=asked,
            devin_tool_boundary_observed=observed,
        )
        return response["delivery_mode"], response["delivery_mechanism"]

    legacy = ("devin_stop_or_prompt_bound", "devin_prompt_bound")
    assert health(asked=False, observed=True) == legacy
    # Installed but not yet loaded: the session never ran the tool hook.
    assert health(asked=True, observed=False) == legacy
    assert health(asked=True, observed=True) == ("devin_tool_boundary", "devin_tool_boundary")


def test_devin_tool_boundary_never_reaches_a_reader_that_did_not_negotiate_it() -> None:
    target = runtime.Target(
        alias="devin@m4:ws",
        provider="devin",
        device="m4",
        project="ws",
        generation=str(uuid4()),
        session_key="b" * 64,
        remote=False,
        delivery_mode="devin_tool_boundary",
        delivery_mechanism="devin_tool_boundary",
    )

    def roster(affirm: bool) -> dict[str, object]:
        row = target.public(
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_owning_daemon=True,
            include_devin_tool_boundary=affirm,
            include_handle=False,
        )
        row.update(generation=target.generation, session_key=target.session_key)
        return {"schema_version": 1, "peers": [row]}

    def parse(raw: dict[str, object], *, negotiated: bool) -> list[runtime.Target]:
        return runtime._targets_from_tailnet(
            "100.64.0.2",
            raw,
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_owning_daemon=True,
            include_devin=True,
            include_devin_tool_boundary=negotiated,
        )

    # New broker → old reader: the old reader never asks, so it gets legacy values.
    [old_view] = parse(roster(False), negotiated=False)
    assert old_view.delivery_mode == "devin_stop_or_prompt_bound"
    assert runtime.destination_receiving(old_view)["active_turn_input"] is False
    with pytest.raises(ChatError, match="invalid discovery"):
        parse(roster(True), negotiated=False)
    # New broker → new reader.
    [new_view] = parse(roster(True), negotiated=True)
    assert runtime.destination_receiving(new_view)["active_turn_input"] is True


@pytest.mark.parametrize(
    ("broker_knows_flag", "active"),
    [(False, False), (True, True)],
)
def test_new_reader_negotiates_devin_tool_boundaries_and_stays_conservative_with_old_brokers(
    monkeypatch: pytest.MonkeyPatch, broker_knows_flag: bool, active: bool
) -> None:
    """New reader → old (v0.5.2/83f3ce3) broker stays legacy; → new broker affirms."""
    generation, handle = str(uuid4()), "c" * 64
    requests: list[dict[str, object]] = []

    def broker(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        requests.append(payload)
        if "include_devin_tool_boundary" in payload and not broker_knows_flag:
            raise ChatError("Tailnet broker request is invalid")
        affirmed = "include_devin_tool_boundary" in payload
        row: dict[str, object] = {
            "alias": "devin@m4:ws",
            "provider": "devin",
            "device": "m4",
            "project": "ws",
            "status": "available",
            "generation": generation,
            "session_key": handle,
            "delivery_mode": "devin_tool_boundary" if affirmed else "devin_stop_or_prompt_bound",
            "delivery_mechanism": "devin_tool_boundary" if affirmed else "devin_prompt_bound",
        }
        return {"schema_version": 1, "peers": [row]}

    monkeypatch.setattr(runtime, "request_tailnet", broker)
    [target], complete = runtime._remote_node_targets(
        "100.64.0.2",
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        include_devin=True,
    )

    assert complete is True
    assert "include_devin_tool_boundary" in requests[0]
    assert runtime.destination_receiving(target)["active_turn_input"] is active


@pytest.mark.parametrize(
    ("response_mode", "active"),
    [
        # A receiver courier that predates the flag ignores it and answers legacy.
        (("devin_stop_or_prompt_bound", "devin_prompt_bound"), False),
        (("devin_tool_boundary", "devin_tool_boundary"), True),
    ],
)
def test_local_devin_capability_comes_from_the_receivers_own_health_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_mode: tuple[str, str],
    active: bool,
) -> None:
    route = _devin_route(tmp_path)
    asked: list[dict[str, object]] = []

    def courier(_path: object, payload: dict[str, object], **_: object) -> dict[str, object]:
        asked.append(payload)
        return {
            "schema_version": 1,
            "status": "READY",
            "generation": route.generation,
            "alias": route.alias,
            "delivery_mode": response_mode[0],
            "delivery_mechanism": response_mode[1],
        }

    monkeypatch.setattr(runtime, "request_socket", courier)
    target = runtime._local_target(tmp_path, route)

    assert target is not None
    assert asked[0]["include_devin_tool_boundary"] is True
    assert runtime.destination_receiving(target)["active_turn_input"] is active


@pytest.mark.parametrize("broker_knows_flag", [False, True])
def test_new_reader_negotiates_current_boundary_and_stays_conservative_with_old_brokers(
    monkeypatch: pytest.MonkeyPatch, broker_knows_flag: bool
) -> None:
    """A broker that predates the flag refuses it; the reader retries without
    it, keeps the peer, and reports the boundary as unobserved -- never
    unrestricted."""
    generation, handle = str(uuid4()), "d" * 64
    requests: list[dict[str, object]] = []

    def broker(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        requests.append(payload)
        if "include_current_boundary" in payload and not broker_knows_flag:
            raise ChatError("Tailnet broker request is invalid")
        row: dict[str, object] = {
            "alias": "devin@m4:ws",
            "provider": "devin",
            "device": "m4",
            "project": "ws",
            "status": "available",
            "generation": generation,
            "session_key": handle,
            "delivery_mode": "devin_tool_boundary",
            "delivery_mechanism": "devin_tool_boundary",
        }
        if "include_current_boundary" in payload:
            row["current_boundary"] = "next_prompt_custom_subagent"
        return {"schema_version": 1, "peers": [row]}

    monkeypatch.setattr(runtime, "request_tailnet", broker)
    [target], complete = runtime._remote_node_targets(
        "100.64.0.2",
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        include_devin=True,
        include_current_boundary=True,
    )

    assert complete is True
    assert requests[0]["include_current_boundary"] is True
    if broker_knows_flag:
        assert target.current_boundary == "next_prompt_custom_subagent"
        assert runtime.destination_receiving(target)["active_turn_input"] is False
    else:
        assert target.current_boundary is None
        assert "current_boundary" not in runtime.destination_receiving(target)
        assert len(requests) == 2


def test_devin_tool_hook_records_that_its_loaded_session_runs_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = _devin_route(tmp_path)
    monkeypatch.setattr(runtime, "presence_is_enabled", lambda: True)
    monkeypatch.setattr(runtime, "_devin_route", lambda *_args, **_kwargs: route)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    monkeypatch.setattr(runtime, "_devin_messages", lambda *_args: [])
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": route.session_id,
        "prompt_id": str(uuid4()),
        "tool_name": "exec",
        "tool_input": {},
        "tool_response": {"success": True, "output": "", "error": None},
    }
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    marker = runtime.devin_tool_boundary_path(tmp_path, route)
    assert not marker.exists()

    runtime.devin_post_tool(route.pid, str(tmp_path))

    assert marker.exists() and marker.stat().st_mode & 0o777 == 0o600


def test_broker_accepts_the_tool_boundary_flag_only_with_delivery_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import tailnet_broker

    seen: list[dict[str, object]] = []

    def peers(_root: Path, **kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        return {"schema_version": 1, "peers": []}

    monkeypatch.setattr(tailnet_broker, "peers", peers)
    base = {"schema_version": 1, "operation": "peers", "include_devin_tool_boundary": True}

    tailnet_broker.handle_broker_request(
        tmp_path, {**base, "include_delivery_mode": True}, "100.64.0.9"
    )
    assert seen[-1]["include_devin_tool_boundary"] is True
    with pytest.raises(ChatError, match="request is invalid"):
        tailnet_broker.handle_broker_request(tmp_path, base, "100.64.0.9")


def test_broker_accepts_the_current_boundary_flag_only_with_delivery_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import tailnet_broker

    seen: list[dict[str, object]] = []

    def peers(_root: Path, **kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        return {"schema_version": 1, "peers": []}

    monkeypatch.setattr(tailnet_broker, "peers", peers)
    base = {"schema_version": 1, "operation": "peers", "include_current_boundary": True}

    tailnet_broker.handle_broker_request(
        tmp_path, {**base, "include_delivery_mode": True}, "100.64.0.9"
    )
    assert seen[-1]["include_current_boundary"] is True
    with pytest.raises(ChatError, match="request is invalid"):
        tailnet_broker.handle_broker_request(tmp_path, base, "100.64.0.9")


def test_cli_status_prints_the_observation_and_stays_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cross_agent_chat import cli

    home = tmp_path / "home"
    home.mkdir()
    root = home / ".local/state/cross-agent-chat"
    monkeypatch.setenv("HOME", str(home))
    source, target = _route(root, tmp_path), _route(root, tmp_path)
    store = IntentStore(root)
    event_id = store.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)
    store.mark(event_id, "TRANSPORT_ACCEPTED")
    before = store.path.read_bytes()

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        assert payload["operation"] == "inspect"
        return {
            "schema_version": 1,
            "status": "INSPECTED",
            "generation": target.generation,
            "courier_version": "0.5.3",
            "courier_incarnation": "incarnation-1",
            "event_state": "pending",
            "pending_count": 1,
            "oldest_pending_age_seconds": 2.5,
        }

    monkeypatch.setattr(runtime, "request_socket", courier)
    assert cli.run(cli.parser().parse_args(["status", event_id])) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "stage: pending_in_courier" in lines
    assert "pending_count: 1" in lines
    assert "courier_incarnation: incarnation-1" in lines

    assert cli.run(cli.parser().parse_args(["status", event_id, "--json"])) == 0
    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    observation = cast(dict[str, object], payload["receiving_observation"])
    assert observation["stage"] == "pending_in_courier"
    assert "message" not in json.dumps(payload)
    assert store.path.read_bytes() == before


def _bound_helper_pair(root: Path, base: Path) -> tuple[Route, Route]:
    """Register one live native helper bound to an exact original Codex route."""
    profile = base / f"profile-{uuid4().hex[:12]}"
    profile.mkdir(parents=True, exist_ok=True)
    original = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="test",
        cwd=str(base),
        pid=os.getpid(),
        owner_identity="a" * 64,
        profile_root=str(profile),
    )
    registry = Registry(root)
    registry.upsert(original)
    store = NativeHelperStore(root)
    binding, nonce = store.reserve(original, "a" * 64)
    helper_root = base / binding.helper_directory
    helper_root.mkdir()
    helper = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="test",
        cwd=str(helper_root),
        pid=os.getpid(),
        owner_identity="a" * 64,
        profile_root=str(profile),
    )
    registry.upsert(helper)
    store.register(helper, nonce, original, "a" * 64)
    return original, helper


def _inspect_answer(generation: str, state: str, **extra: object) -> dict[str, object]:
    answer: dict[str, object] = {
        "schema_version": 1,
        "status": "INSPECTED",
        "generation": generation,
        "courier_version": "0.5.4",
        "courier_incarnation": "inc-1",
        "event_state": state,
    }
    answer.update(extra)
    return answer


def _accepted_intent(root: Path, source: Route, target: Route) -> tuple[IntentStore, str]:
    store = IntentStore(root)
    event_id = store.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)
    store.mark(event_id, "TRANSPORT_ACCEPTED")
    return store, event_id


def test_status_observes_the_bound_helper_courier_never_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The send addressed the bound helper's queue; the original courier's
    'unseen' must not be read as the event never arriving."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    original, helper = _bound_helper_pair(root, tmp_path)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    store, event_id = _accepted_intent(root, source, original)
    before = store.path.read_bytes()
    asked: list[str] = []

    def socket(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        generation = str(payload["generation"])
        asked.append(generation)
        assert payload["operation"] == "inspect"
        assert set(payload) == {"schema_version", "operation", "generation", "event_id"}
        if generation == helper.generation:
            return _inspect_answer(
                generation, "pending", pending_count=1, oldest_pending_age_seconds=2.0
            )
        # The original courier was never offered the event.
        return _inspect_answer(generation, "unseen")

    monkeypatch.setattr(runtime, "request_socket", socket)
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    assert observation["observer"] == "native_helper"
    assert observation["stage"] == "pending_in_courier"
    assert observation["pending_count"] == 1
    assert asked == [helper.generation]
    assert store.path.read_bytes() == before


def test_status_never_touches_an_unrelated_helper_with_the_same_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the exact bound helper may answer; another original's helper that
    happens to hold the event id is never inspected."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    original, helper = _bound_helper_pair(root, tmp_path)
    _other_original, unrelated = _bound_helper_pair(root, tmp_path)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    _store, event_id = _accepted_intent(root, source, original)
    asked: list[str] = []

    def socket(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        asked.append(str(payload["generation"]))
        # Even the unrelated helper claims the event pending; only the bound
        # helper's queue is this event's receiving owner.
        return _inspect_answer(
            str(payload["generation"]),
            "pending",
            pending_count=1,
            oldest_pending_age_seconds=1.0,
        )

    monkeypatch.setattr(runtime, "request_socket", socket)
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    assert observation["observer"] == "native_helper"
    assert observation["stage"] == "pending_in_courier"
    assert asked == [helper.generation]
    assert unrelated.generation not in asked


@pytest.mark.parametrize("variant", ["dead", "stale_generation", "not_current"])
def test_status_stays_unknown_when_the_bound_helper_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """A dead, generation-stale, or no-longer-owner-current bound helper cannot
    be inspected; falling back to the original courier would fabricate a
    misleading 'unseen' for a queue that never held the event."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    original, helper = _bound_helper_pair(root, tmp_path)
    _store, event_id = _accepted_intent(root, source, original)
    asked: list[str] = []

    def socket(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        asked.append(str(payload["generation"]))
        return _inspect_answer(str(payload["generation"]), "pending", pending_count=1)

    monkeypatch.setattr(runtime, "request_socket", socket)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    if variant == "dead":
        monkeypatch.setattr(Route, "process_is_live", lambda route: route != helper)
    elif variant == "stale_generation":
        restarted = Route.create(
            provider="codex",
            session_id=helper.session_id,
            device=helper.device,
            cwd=helper.cwd,
            pid=helper.pid,
            owner_identity=helper.owner_identity,
            profile_root=helper.profile_root,
        )
        Registry(root).upsert(restarted)
        monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    else:
        monkeypatch.setattr(runtime, "_route_current", lambda _root, route: route != helper)

    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    assert observation["stage"] == "unknown"
    assert observation["reason"] == "helper_unavailable"
    assert asked == []


def test_status_reports_a_bound_dispatch_claim_as_still_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A native dispatch row bound to the exact event/original/helper is a
    claim, not a success; it stays UNKNOWN in the observation."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    original, helper = _bound_helper_pair(root, tmp_path)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    _store, event_id = _accepted_intent(root, source, original)
    NativeDispatchStore(root).claim(helper, original, event_id, "an opaque body")
    claims_before = (root / "native-dispatches.json").read_bytes()

    monkeypatch.setattr(
        runtime,
        "request_socket",
        lambda _path, payload, **_k: _inspect_answer(
            str(payload["generation"]),
            "handed_off",
            pending_count=0,
            oldest_pending_age_seconds=None,
        ),
    )
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    assert observation["observer"] == "native_helper"
    assert observation["stage"] == "handed_off"
    assert observation["native_dispatch_claim"] == "UNKNOWN"
    assert "never evidence the original session consumed" in cast(str, observation["detail"])
    assert "original forwarding remains unobserved" in cast(str, observation["detail"])
    assert (root / "native-dispatches.json").read_bytes() == claims_before
    # An unrelated event's claim is never bound to this observation.
    other = _accepted_intent(root, source, original)[1]
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, other)["receiving_observation"]
    )
    assert "native_dispatch_claim" not in observation


def _remote_intent(root: Path, source: Route, tmp_path: Path) -> tuple[IntentStore, Route, str]:
    """An intent whose recorded target lives on another node (no local route)."""
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="remote",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    store, event_id = _accepted_intent(root, source, target)
    return store, target, event_id


def test_status_recipient_asks_only_the_node_that_presented_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bounded inspect to the token's exact node; no discovery fanout."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    store, target, event_id = _remote_intent(root, source, tmp_path)
    before = store.path.read_bytes()
    token = remote_token(
        "nTarget", session_key(target.provider, target.session_id), target.generation
    )
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id="nSender", peers={"nTarget": "100.64.0.2", "nOther": "100.64.0.3"}
        ),
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def wire(address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        calls.append((address, payload))
        return {
            "schema_version": 1,
            "status": "INSPECTED",
            "event_id": event_id,
            "observer": "native_helper",
            "event_state": "handed_off",
            "courier_version": "0.5.4",
            "courier_incarnation": "inc-9",
            "dispatch_claim": "UNKNOWN",
        }

    monkeypatch.setattr(runtime, "request_tailnet", wire)
    observation = cast(
        dict[str, object],
        runtime.owner_event_status(root, event_id, recipient=token)["receiving_observation"],
    )
    assert calls == [
        (
            "100.64.0.2",
            {
                "schema_version": 1,
                "operation": "inspect",
                "event_id": event_id,
                "target_key": session_key(target.provider, target.session_id),
                "target_generation": target.generation,
            },
        )
    ]
    assert observation["stage"] == "handed_off"
    assert observation["observer"] == "native_helper"
    assert observation["native_dispatch_claim"] == "UNKNOWN"
    assert "never evidence the original session consumed" in cast(str, observation["detail"])
    assert store.path.read_bytes() == before


def test_status_recipient_mismatch_refuses_before_any_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token for another handle, generation, or scope is refused before the
    tailnet is even read."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    _store, target, event_id = _remote_intent(root, source, tmp_path)
    probed: list[str] = []

    def identity() -> None:
        probed.append("identity")

    def wire(*_args: object, **_kwargs: object) -> dict[str, object]:
        probed.append("wire")
        return {}

    monkeypatch.setattr(runtime, "tailnet_identity", identity)
    monkeypatch.setattr(runtime, "request_tailnet", wire)
    wrong_handle = remote_token("nTarget", "b" * 64, target.generation)
    with pytest.raises(ChatError, match="recorded target"):
        runtime.owner_event_status(root, event_id, recipient=wrong_handle)
    wrong_generation = remote_token(
        "nTarget", session_key(target.provider, target.session_id), str(uuid4())
    )
    with pytest.raises(ChatError, match="recorded target"):
        runtime.owner_event_status(root, event_id, recipient=wrong_generation)
    local = local_token(root, session_key(target.provider, target.session_id), target.generation)
    with pytest.raises(ChatError, match="remote recipient handle"):
        runtime.owner_event_status(root, event_id, recipient=local)
    with pytest.raises(ChatError):
        runtime.owner_event_status(root, event_id, recipient="not-a-token")
    assert probed == []


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("no_tailscale", "remote_unavailable"),
        ("node_offline", "remote_node_unavailable"),
        ("broker_error", "remote_broker_unavailable"),
        ("old_broker", "remote_broker_unavailable"),
        ("extra_field", "remote_observation_invalid"),
        ("no_receiver", "remote_no_receiver"),
    ],
)
def test_status_recipient_failures_stay_unknown_with_a_safe_next_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, reason: str
) -> None:
    """Stale node, offline broker, old broker, and hostile answers are all
    unknown -- never a guessed custody and never a replay instruction."""
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    _store, target, event_id = _remote_intent(root, source, tmp_path)
    token = remote_token(
        "nTarget", session_key(target.provider, target.session_id), target.generation
    )
    if mode == "no_tailscale":
        monkeypatch.setattr(runtime, "tailnet_identity", lambda: None)
    else:
        monkeypatch.setattr(
            runtime,
            "tailnet_identity",
            lambda: TailnetIdentity(
                self_node_id="nSender",
                peers={} if mode == "node_offline" else {"nTarget": "100.64.0.2"},
            ),
        )

    def wire(_address: str, _payload: dict[str, object], **_: object) -> dict[str, object]:
        if mode in {"broker_error", "old_broker"}:
            raise ChatError("Tailnet broker request is invalid")
        if mode == "extra_field":
            return {
                "schema_version": 1,
                "status": "INSPECTED",
                "event_id": event_id,
                "observer": "original_courier",
                "event_state": "handed_off",
                "courier_version": "0.5.4",
                "courier_incarnation": "inc-1",
                "message": "an injected body must be rejected",
            }
        return {
            "schema_version": 1,
            "status": "INSPECTED",
            "event_id": event_id,
            "observer": "none",
            "reason": "no_local_receiver",
        }

    monkeypatch.setattr(runtime, "request_tailnet", wire)
    observation = cast(
        dict[str, object],
        runtime.owner_event_status(root, event_id, recipient=token)["receiving_observation"],
    )
    assert observation["stage"] == "unknown"
    assert observation["reason"] == reason
    detail = cast(str, observation["detail"])
    assert "normal provider transcript" in detail
    assert "do not replay" in detail
    assert "run `cross-agent-chat status`" not in detail
    assert "message" not in detail and "injected" not in detail


def test_broker_inspect_answers_the_bound_courier_without_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ACL-permitted inspect reads only volatile courier state: no
    authorize, receive, peek, ack, or durable write."""
    root = tmp_path / "receiver"
    original, helper = _bound_helper_pair(root, tmp_path)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    event_id = str(uuid4())
    # Inbound remote custody lives at the sender. The receiving courier can
    # answer inspection even though owner status has no local intent to read.
    with pytest.raises(ChatError, match="event is unavailable"):
        runtime.owner_event_status(root, event_id)
    asked: list[dict[str, object]] = []

    def socket(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        asked.append(payload)
        return _inspect_answer(
            str(payload["generation"]),
            "pending",
            pending_count=1,
            oldest_pending_age_seconds=0.5,
        )

    monkeypatch.setattr(runtime, "request_socket", socket)
    request = {
        "schema_version": 1,
        "operation": "inspect",
        "event_id": event_id,
        "target_key": session_key(original.provider, original.session_id),
        "target_generation": original.generation,
    }
    response = handle_broker_request(root, request, "100.64.0.9")
    assert response == {
        "schema_version": 1,
        "status": "INSPECTED",
        "event_id": event_id,
        "observer": "native_helper",
        "event_state": "pending",
        "courier_version": "0.5.4",
        "courier_incarnation": "inc-1",
        "pending_count": 1,
        "oldest_pending_age_seconds": 0.5,
    }
    assert [item["generation"] for item in asked] == [helper.generation]
    assert {item["operation"] for item in asked} == {"inspect"}
    # No receiver or an unreadable answer reports "none" with a closed reason.
    asked.clear()
    response = handle_broker_request(root, {**request, "event_id": str(uuid4())}, "100.64.0.9")
    assert response["observer"] == "native_helper"
    response = handle_broker_request(
        root, {**request, "target_generation": str(uuid4())}, "100.64.0.9"
    )
    assert response == {
        "schema_version": 1,
        "status": "INSPECTED",
        "event_id": event_id,
        "observer": "none",
        "reason": "no_local_receiver",
    }
    # A non-tailnet peer and malformed shapes are refused outright.
    with pytest.raises(ChatError, match="invalid"):
        handle_broker_request(root, request, "127.0.0.1")
    with pytest.raises(ChatError, match="invalid"):
        handle_broker_request(root, {**request, "message": "x"}, "100.64.0.9")
    with pytest.raises(ChatError, match="invalid"):
        handle_broker_request(root, {**request, "target_key": "z" * 64}, "100.64.0.9")


def test_chat_status_schema_and_callers_carry_the_optional_recipient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The advertised schema names the optional recipient, and both the native
    and external caller paths pass it through unchanged."""
    from cross_agent_chat import cli

    tools = cli._mcp_tools("claude", True)
    status_tool = next(tool for tool in tools if tool["name"] == "chat_status")
    schema = cast(dict[str, object], status_tool["inputSchema"])
    assert set(cast(dict[str, object], schema["properties"])) == {"event_id", "recipient"}
    assert schema["required"] == ["event_id"]

    root = tmp_path / "state"
    source = _route(root, tmp_path)
    _target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="remote",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    store = IntentStore(root)
    event_id = store.begin(source, _target, source_alias=source.alias, payload_digest="a" * 64)
    seen: list[str | None] = []

    def fake_status(
        _root: Path, _source: object, _event: str, *, recipient: str | None = None
    ) -> dict[str, object]:
        seen.append(recipient)
        return {"schema_version": 1, "event_id": _event, "status": "PENDING"}

    monkeypatch.setattr(cli, "event_status", fake_status)
    monkeypatch.setattr(cli, "authenticate_mcp_sender", lambda *_args: source)
    result = cli._mcp_call_tool(
        "claude",
        root,
        {
            "name": "chat_status",
            "arguments": {"event_id": event_id, "recipient": "cac2.fixture"},
        },
    )
    assert seen == ["cac2.fixture"]
    assert not result.get("isError")
    cli._mcp_call_tool("claude", root, {"name": "chat_status", "arguments": {"event_id": event_id}})
    assert seen == ["cac2.fixture", None]
    with pytest.raises(ChatError, match="event id is invalid"):
        cli._mcp_call_tool(
            "claude",
            root,
            {"name": "chat_status", "arguments": {"event_id": event_id, "recipient": 7}},
        )
    with pytest.raises(ChatError, match="event id is invalid"):
        cli._mcp_call_tool(
            "claude",
            root,
            {"name": "chat_status", "arguments": {"event_id": event_id, "extra": "x"}},
        )
    # The external endpoint caller takes the same optional argument.
    _endpoint, credential = _external_endpoint(root)
    seen.clear()
    result = cli._external_call_tool(
        root,
        credential,
        {
            "name": "chat_status",
            "arguments": {"event_id": event_id, "recipient": "cac2.fixture"},
        },
    )
    assert seen == ["cac2.fixture"]
    assert not result.get("isError")


def _external_endpoint(root: Path) -> tuple[object, str]:
    from cross_agent_chat.external import ExternalEndpointStore

    return ExternalEndpointStore(root).enroll(
        device="test", name="status", context="status fixture"
    )


def test_cli_status_recipient_flag_reaches_the_owner_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cross_agent_chat import cli

    home = tmp_path / "home"
    home.mkdir()
    root = home / ".local/state/cross-agent-chat"
    monkeypatch.setenv("HOME", str(home))
    source = _route(root, tmp_path)
    _store, target, event_id = _remote_intent(root, source, tmp_path)
    token = remote_token(
        "nTarget", session_key(target.provider, target.session_id), target.generation
    )
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSender", peers={"nTarget": "100.64.0.2"}),
    )
    monkeypatch.setattr(
        runtime,
        "request_tailnet",
        lambda _address, _payload, **_k: {
            "schema_version": 1,
            "status": "INSPECTED",
            "event_id": event_id,
            "observer": "original_courier",
            "event_state": "pending",
            "courier_version": "0.5.4",
            "courier_incarnation": "inc-1",
            "pending_count": 1,
            "oldest_pending_age_seconds": 1.5,
        },
    )
    assert (
        cli.run(cli.parser().parse_args(["status", event_id, "--recipient", token, "--json"])) == 0
    )
    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    observation = cast(dict[str, object], payload["receiving_observation"])
    assert observation["stage"] == "pending_in_courier"
    assert observation["observer"] == "original_courier"


@pytest.mark.parametrize("malformed", [[], {}])
def test_status_malformed_local_inspect_state_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: object
) -> None:
    root = tmp_path / "state"
    source, target = _route(root, tmp_path), _route(root, tmp_path)
    store, event_id = _accepted_intent(root, source, target)
    before = store.path.read_bytes()
    response = _inspect_answer(target.generation, "unseen")
    response["event_state"] = malformed
    monkeypatch.setattr(runtime, "request_socket", lambda *_a, **_k: response)
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    assert observation["stage"] == "unknown"
    assert observation["reason"] == "observation_invalid"
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("field", ["observer", "event_state", "reason"])
@pytest.mark.parametrize("malformed", [[], {}])
def test_status_malformed_remote_inspect_fields_stay_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, malformed: object
) -> None:
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    store, target, event_id = _remote_intent(root, source, tmp_path)
    before = store.path.read_bytes()
    recipient = remote_token(
        "nTarget", session_key(target.provider, target.session_id), target.generation
    )
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSender", peers={"nTarget": "100.64.0.2"}),
    )
    response: dict[str, object] = {
        "schema_version": 1,
        "status": "INSPECTED",
        "event_id": event_id,
        "observer": "original_courier",
        "event_state": "handed_off",
        "courier_version": "0.5.5",
        "courier_incarnation": "inc-1",
    }
    if field == "reason":
        response = {
            "schema_version": 1,
            "status": "INSPECTED",
            "event_id": event_id,
            "observer": "none",
            "reason": "courier_unavailable",
        }
    response[field] = malformed
    monkeypatch.setattr(runtime, "request_tailnet", lambda *_a, **_k: response)
    observation = cast(
        dict[str, object],
        runtime.owner_event_status(root, event_id, recipient=recipient)["receiving_observation"],
    )
    assert observation["stage"] == "unknown"
    assert observation["reason"] == "remote_observation_invalid"
    assert store.path.read_bytes() == before


def test_status_bound_helper_with_stale_original_cannot_claim_a_current_receiving_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    original, _helper = _bound_helper_pair(root, tmp_path)
    store, event_id = _accepted_intent(root, source, original)
    before = store.path.read_bytes()
    monkeypatch.setattr(runtime, "_route_current", lambda _root, route: route != original)
    calls: list[object] = []
    monkeypatch.setattr(runtime, "request_socket", lambda *args, **_kw: calls.append(args))
    observation = cast(
        dict[str, object], runtime.owner_event_status(root, event_id)["receiving_observation"]
    )
    assert observation["stage"] == "unknown"
    assert observation["reason"] == "original_unavailable"
    assert calls == []
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("status", ["TRANSPORT_ACCEPTED", "UNKNOWN_DELIVERY"])
def test_status_no_courier_queue_preserves_the_sender_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: bool, status: IntentStatus
) -> None:
    """The same qualified receiver answer must not turn an unknown send into handoff."""
    root, receiver_root = tmp_path / "sender", tmp_path / "receiver"
    source = _route(root, tmp_path)
    target = _route(receiver_root if remote else root, tmp_path)
    store, event_id = _accepted_intent(root, source, target)
    store.mark(event_id, status)
    before = store.path.read_bytes()
    requests: list[dict[str, object]] = []

    def socket(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        requests.append(payload)
        assert payload == {
            "schema_version": 1,
            "operation": "inspect",
            "generation": target.generation,
            "event_id": event_id,
        }
        return _inspect_answer(target.generation, "no_courier_queue")

    monkeypatch.setattr(runtime, "request_socket", socket)
    recipient: str | None = None
    if remote:
        recipient = remote_token(
            "nTarget", session_key(target.provider, target.session_id), target.generation
        )
        monkeypatch.setattr(
            runtime,
            "tailnet_identity",
            lambda: TailnetIdentity(self_node_id="nSender", peers={"nTarget": "100.64.0.2"}),
        )

        def wire(address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
            assert address == "100.64.0.2"
            assert payload["operation"] == "inspect"
            return handle_broker_request(receiver_root, payload, "100.64.0.1")

        monkeypatch.setattr(runtime, "request_tailnet", wire)

    payload = runtime.owner_event_status(root, event_id, recipient=recipient)
    observation = cast(dict[str, object], payload["receiving_observation"])
    assert payload["status"] == status
    assert payload["delivery_observation"] == "not_observed"
    assert observation["observer"] == "original_courier"
    assert observation["courier_version"] == "0.5.4"
    if status == "TRANSPORT_ACCEPTED":
        assert observation["stage"] == "handed_off"
    else:
        assert observation["stage"] == "unknown"
        assert observation["reason"] == "no_courier_queue"
        detail = cast(str, observation["detail"])
        assert "no CAC-held queue to observe" in detail
        assert "does not establish whether the provider took custody" in detail
        assert "normal provider transcript" in detail
        assert "next natural turn" in detail
        assert "do not replay this event" in detail
    assert len(requests) == 1
    assert store.path.read_bytes() == before
    assert "message" not in json.dumps(payload)


@pytest.mark.parametrize("status", ["TRANSPORT_ACCEPTED", "UNKNOWN_DELIVERY"])
def test_status_no_helper_queue_never_establishes_original_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: IntentStatus
) -> None:
    root = tmp_path / "state"
    source = _route(root, tmp_path)
    original, helper = _bound_helper_pair(root, tmp_path)
    store, event_id = _accepted_intent(root, source, original)
    store.mark(event_id, status)
    before = store.path.read_bytes()
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)

    def socket(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        assert payload["operation"] == "inspect"
        assert payload["generation"] == helper.generation
        return _inspect_answer(helper.generation, "no_courier_queue")

    monkeypatch.setattr(runtime, "request_socket", socket)
    payload = runtime.owner_event_status(root, event_id)
    observation = cast(dict[str, object], payload["receiving_observation"])
    assert payload["status"] == status
    assert observation["observer"] == "native_helper"
    assert observation["stage"] == "unknown"
    assert observation["reason"] == "no_courier_queue"
    assert "the bound native helper's courier" in cast(str, observation["detail"])
    assert "do not replay this event" in cast(str, observation["detail"])
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("mode", ["schema_version", "generation", "event_state", "timeout"])
def test_status_no_queue_does_not_bypass_inspect_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    root = tmp_path / "state"
    source, target = _route(root, tmp_path), _route(root, tmp_path)
    store, event_id = _accepted_intent(root, source, target)
    store.mark(event_id, "UNKNOWN_DELIVERY")
    before = store.path.read_bytes()
    response = _inspect_answer(target.generation, "no_courier_queue")
    if mode == "schema_version":
        response[mode] = 0
    elif mode == "generation":
        response[mode] = str(uuid4())
    elif mode == "event_state":
        response[mode] = "unsupported_state"

    def socket(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        assert payload["operation"] == "inspect"
        if mode == "timeout":
            raise OSError("inspect timed out")
        return response

    monkeypatch.setattr(runtime, "request_socket", socket)
    payload = runtime.owner_event_status(root, event_id)
    observation = cast(dict[str, object], payload["receiving_observation"])
    assert payload["status"] == "UNKNOWN_DELIVERY"
    assert observation["stage"] == "unknown"
    assert observation["reason"] == (
        "courier_unavailable" if mode == "timeout" else "observation_invalid"
    )
    assert "courier_version" not in observation
    assert store.path.read_bytes() == before
