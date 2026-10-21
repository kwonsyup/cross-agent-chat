"""Negotiated reporting of the ACP-hosted Devin receiving mode."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import ChatError, IntentStore, Route
from cross_agent_chat.devin import DevinCurrentBoundary


def _devin_route(cwd: Path) -> Route:
    return Route.create(
        provider="devin", session_id=str(uuid4()), device="m4", cwd=str(cwd), pid=os.getpid()
    )


def _acp_target(
    *,
    boundary: DevinCurrentBoundary | None = None,
    tool_hook: bool = True,
) -> runtime.Target:
    return runtime.Target(
        alias="devin@m4:ws",
        provider="devin",
        device="m4",
        project="ws",
        generation=str(uuid4()),
        session_key="b" * 64,
        remote=False,
        delivery_mode="devin_acp_hosted",
        delivery_mechanism="devin_tool_boundary" if tool_hook else "devin_acp_host",
        current_boundary=boundary,
    )


def test_courier_health_reports_acp_host_only_when_asked_and_observed(
    tmp_path: Path,
) -> None:
    """The new pair needs the negotiated flag AND a live attested adapter."""
    route = _devin_route(tmp_path)
    acp = ("devin_acp_hosted", "devin_tool_boundary")
    acp_without_tool_hook = ("devin_acp_hosted", "devin_acp_host")
    tool_boundary = ("devin_tool_boundary", "devin_tool_boundary")
    legacy = ("devin_stop_or_prompt_bound", "devin_prompt_bound")

    def health(*, asked: bool, observed: bool, tool_observed: bool = True) -> tuple[object, object]:
        response = runtime.courier_health(
            route,
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_devin_tool_boundary=True,
            devin_tool_boundary_observed=tool_observed,
            include_devin_acp_host=asked,
            devin_acp_host_observed=observed,
        )
        return response["delivery_mode"], response["delivery_mechanism"]

    # Observed + asked: ACP mode; the mechanism keeps the tool-hook evidence.
    assert health(asked=True, observed=True) == acp
    assert health(asked=True, observed=True, tool_observed=False) == acp_without_tool_hook
    # Adapter exited between probes: fall back to today's tool-boundary truth.
    assert health(asked=True, observed=False) == tool_boundary
    assert health(asked=True, observed=False, tool_observed=False) == legacy
    # An old reader never sends the flag: identical response to today's.
    assert health(asked=False, observed=True) == tool_boundary
    assert health(asked=False, observed=True, tool_observed=False) == legacy


def test_acp_hosted_values_never_reach_a_reader_that_did_not_negotiate() -> None:
    """The closed-set regression guard: unnegotiated readers get at most the
    tool-boundary layer they already understand, never the new values."""
    target = _acp_target()

    def row(*, tool_boundary: bool, acp_host: bool = False) -> dict[str, str]:
        item = target.public(
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_handle=False,
            include_devin_tool_boundary=tool_boundary,
            include_devin_acp_host=acp_host,
        )
        item.update(generation=target.generation, session_key=target.session_key)
        return item

    def parse(roster_row: dict[str, str], *, negotiated: bool) -> runtime.Target:
        [parsed] = runtime._targets_from_tailnet(
            "100.64.0.2",
            {"schema_version": 1, "peers": [roster_row]},
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_devin=True,
            include_devin_tool_boundary=True,
            include_devin_acp_host=negotiated,
        )
        return parsed

    # A reader that negotiated tool-boundary but not acp-host sees exactly
    # today's strongest shape; one that negotiated neither sees legacy.
    downgraded = row(tool_boundary=True)
    assert downgraded["delivery_mode"] == "devin_tool_boundary"
    assert downgraded["delivery_mechanism"] == "devin_tool_boundary"
    # Without observed tool-hook evidence the downgrade never claims it.
    unhooked = _acp_target(tool_hook=False).public(
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        include_handle=False,
        include_devin_tool_boundary=True,
    )
    assert unhooked["delivery_mode"] == "devin_stop_or_prompt_bound"
    legacy = row(tool_boundary=False)
    assert legacy["delivery_mode"] == "devin_stop_or_prompt_bound"
    assert legacy["delivery_mechanism"] == "devin_prompt_bound"
    # A rogue reply that emits the new pair anyway is rejected, not read.
    affirmed = row(tool_boundary=True, acp_host=True)
    assert affirmed["delivery_mode"] == "devin_acp_hosted"
    with pytest.raises(ChatError, match="invalid discovery"):
        parse(affirmed, negotiated=False)
    parsed = parse(affirmed, negotiated=True)
    assert parsed.delivery_mode == "devin_acp_hosted"
    assert parsed.delivery_mechanism == "devin_tool_boundary"
    unhooked_row = _acp_target(tool_hook=False).public(
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        include_handle=False,
        include_devin_tool_boundary=True,
        include_devin_acp_host=True,
    )
    unhooked_row.update(generation=target.generation, session_key=target.session_key)
    with pytest.raises(ChatError, match="invalid discovery"):
        parse(unhooked_row, negotiated=False)


def test_acp_hosted_qualifies_parked_wake_active_input_and_while_idle_reply() -> None:
    """The adapter prompts the idle engine; active input needs observed hook evidence."""
    qualify = runtime._receiving_qualification
    assert qualify("devin_acp_hosted", "devin_tool_boundary") == (True, True, "while_idle")
    assert qualify("devin_acp_hosted", "devin_acp_host") == (True, False, "while_idle")
    receiving = runtime.destination_receiving(_acp_target())
    assert receiving["mode"] == "devin_acp_hosted"
    assert receiving["parked_wake"] is True
    assert receiving["active_turn_input"] is True
    unhooked = runtime.destination_receiving(_acp_target(tool_hook=False))
    assert unhooked["parked_wake"] is True
    assert unhooked["active_turn_input"] is False


def test_a_hold_still_narrows_acp_hosted_input_and_never_upgrades_other_modes() -> None:
    """current_boundary narrowing applies unchanged to the new pair, and a
    restriction can never raise a weaker mode's promises."""
    held = runtime.destination_receiving(_acp_target(boundary="next_prompt_custom_subagent"))
    assert held["parked_wake"] is True
    assert held["active_turn_input"] is False
    limited = runtime.destination_receiving(_acp_target(boundary="root_tools_only"))
    assert limited["active_turn_input"] == "limited"
    deferred = runtime.destination_receiving(
        runtime.Target(
            alias="devin@m4:ws",
            provider="devin",
            device="m4",
            project="ws",
            generation=str(uuid4()),
            session_key="b" * 64,
            remote=False,
            delivery_mode="devin_stop_or_prompt_bound",
            delivery_mechanism="devin_prompt_bound",
            current_boundary="next_prompt_custom_subagent",
        )
    )
    assert deferred["parked_wake"] is False
    assert deferred["active_turn_input"] is False


def test_reply_delivery_is_while_idle_only_while_the_adapter_is_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Devin source whose adapter exited flips back to next_turn."""
    source = _devin_route(tmp_path)
    monkeypatch.setattr(runtime, "acp_host_observed", lambda *_args: True)
    assert runtime.reply_delivery(tmp_path, source) == "while_idle"
    monkeypatch.setattr(runtime, "acp_host_observed", lambda *_args: False)
    assert runtime.reply_delivery(tmp_path, source) == "next_turn"


def test_local_health_probe_negotiates_the_acp_host_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This reader always asks; the courier answers the new pair only when observed."""
    root = tmp_path / "state"
    IntentStore(root)
    route = _devin_route(tmp_path)
    asked: list[dict[str, object]] = []

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        asked.append(payload)
        return {
            "schema_version": 1,
            "status": "READY",
            "generation": route.generation,
            "alias": route.alias,
            "delivery_mode": "devin_acp_hosted",
            "delivery_mechanism": "devin_tool_boundary",
        }

    monkeypatch.setattr(runtime, "request_socket", courier)
    target = runtime._local_target(root, route)

    assert target is not None
    assert asked[0]["include_devin_acp_host"] is True
    assert target.delivery_mode == "devin_acp_hosted"
    assert runtime.destination_receiving(target)["parked_wake"] is True


@pytest.mark.parametrize("broker_knows_flag", [False, True])
def test_new_reader_negotiates_acp_host_and_stays_conservative_with_old_brokers(
    monkeypatch: pytest.MonkeyPatch, broker_knows_flag: bool
) -> None:
    """A broker that predates the flag refuses it; the reader retries without
    it and keeps the strongest shape the older broker can still affirm."""
    generation, handle = str(uuid4()), "c" * 64
    requests: list[dict[str, object]] = []

    def broker(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        requests.append(payload)
        if "include_devin_acp_host" in payload and not broker_knows_flag:
            raise ChatError("Tailnet broker request is invalid")
        if "include_devin_acp_host" in payload:
            mode, mechanism = "devin_acp_hosted", "devin_tool_boundary"
        elif "include_devin_tool_boundary" in payload:
            mode, mechanism = "devin_tool_boundary", "devin_tool_boundary"
        else:
            mode, mechanism = "devin_stop_or_prompt_bound", "devin_prompt_bound"
        row: dict[str, object] = {
            "alias": "devin@m4:ws",
            "provider": "devin",
            "device": "m4",
            "project": "ws",
            "status": "available",
            "generation": generation,
            "session_key": handle,
            "delivery_mode": mode,
            "delivery_mechanism": mechanism,
        }
        return {"schema_version": 1, "peers": [row]}

    monkeypatch.setattr(runtime, "request_tailnet", broker)
    [target], complete = runtime._remote_node_targets(
        "100.64.0.2",
        include_delivery_mode=True,
        include_delivery_mechanism=True,
        include_devin=True,
    )

    assert complete is False  # Legacy coverage cannot establish alias uniqueness.
    assert requests[0]["include_devin_acp_host"] is True
    receiving = runtime.destination_receiving(target)
    if broker_knows_flag:
        assert receiving["parked_wake"] is True
        assert receiving["mode"] == "devin_acp_hosted"
    else:
        # Fell back to the tool-boundary shape this broker still understands.
        assert len(requests) == 3
        assert "include_devin_acp_host" not in requests[2]
        assert receiving["mode"] == "devin_tool_boundary"
        assert receiving["parked_wake"] is False
        assert receiving["active_turn_input"] is True


def test_broker_accepts_the_acp_host_flag_only_with_delivery_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker forwards the negotiated flag, and it cannot ride a request
    that never asked for delivery_mode."""
    from cross_agent_chat import tailnet_broker

    seen: list[dict[str, object]] = []

    def peers(_root: Path, **kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        return {"schema_version": 1, "peers": []}

    monkeypatch.setattr(tailnet_broker, "peers", peers)
    base = {"schema_version": 1, "operation": "peers", "include_devin_acp_host": True}

    tailnet_broker.handle_broker_request(
        tmp_path, {**base, "include_delivery_mode": True}, "100.64.0.9"
    )
    assert seen[-1]["include_devin_acp_host"] is True
    with pytest.raises(ChatError, match="request is invalid"):
        tailnet_broker.handle_broker_request(tmp_path, base, "100.64.0.9")


def test_broker_listing_emits_only_the_layer_the_remote_reader_negotiated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broker path is internal: its output must honor the remote reader's
    own flag, or an old reader's closed set drops the whole route."""
    monkeypatch.setattr(runtime, "_local_discovery", lambda *_a, **_k: ([_acp_target()], True))

    def row(*, tool_boundary: bool, acp_host: bool = False) -> dict[str, object]:
        listing = runtime.peers(
            tmp_path,
            include_remote=False,
            internal=True,
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_devin_tool_boundary=tool_boundary,
            include_devin_acp_host=acp_host,
        )
        [item] = cast(list[dict[str, object]], listing["peers"])
        return item

    assert row(tool_boundary=True, acp_host=True)["delivery_mode"] == "devin_acp_hosted"
    assert row(tool_boundary=True)["delivery_mode"] == "devin_tool_boundary"
    assert row(tool_boundary=False)["delivery_mode"] == "devin_stop_or_prompt_bound"
