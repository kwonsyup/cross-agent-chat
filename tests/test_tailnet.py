from __future__ import annotations

import errno
import json
import os
import select
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import replace
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pytest

import cross_agent_chat.tailnet_broker as tailnet_broker_module
from cross_agent_chat import __version__, runtime
from cross_agent_chat.cli import parser
from cross_agent_chat.core import (
    MAX_MESSAGE_BYTES,
    ChatError,
    IntentStore,
    Registry,
    Route,
    UnknownDeliveryError,
    bounded_message,
    session_key,
)
from cross_agent_chat.recipient import remote_token
from cross_agent_chat.runtime import (
    ACCEPT_TIMEOUT_SECONDS,
    AUTHORIZE_TIMEOUT_SECONDS,
    MAX_FRAME_BYTES,
    REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    Target,
    authorize_remote,
    receive_remote,
    remote_targets,
    request_tailnet,
    send,
    wrapped_message,
)
from cross_agent_chat.tailnet import (
    TailnetIdentity,
    local_tailnet_address,
    parse_ifconfig_tailnet_address,
    parse_known_tailnet_address,
    parse_local_tailnet_address,
    parse_tailnet_identity,
)
from cross_agent_chat.tailnet_broker import (
    BrokerAdmission,
    bind_broker_listener,
    broker_bindings,
    broker_server,
    dispatch_broker_connection,
    dispatch_ready_brokers,
    handle_broker_request,
    serve_broker_connection,
)
from cross_agent_chat.transport import remote_envelope


def test_tailnet_discovery_returns_only_online_ipv4_nodes() -> None:
    payload = json.dumps(
        {
            "Peer": {
                "node-a": {
                    "ID": "nNodeA",
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.11", "fd7a:115c:a1e0::1"],
                },
                "node-b": {
                    "ID": "nNodeB",
                    "Online": False,
                    "TailscaleIPs": ["100.64.0.12"],
                },
                "node-c": {
                    "ID": "nNodeC",
                    "Online": True,
                    "TailscaleIPs": ["192.0.2.10", "fd7a:115c:a1e0::2"],
                },
            }
        }
    )

    identity = parse_tailnet_identity(payload)
    assert sorted(set(identity.peers.values())) == ["100.64.0.11"]


def test_tailnet_identity_maps_stable_node_ids_to_current_addresses() -> None:
    payload = json.dumps(
        {
            "Self": {"ID": "nSelfNode", "TailscaleIPs": ["100.64.0.10"]},
            "Peer": {
                "node-a": {
                    "ID": "nNodeA",
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.11"],
                },
                "node-b": {
                    "ID": "nNodeB",
                    "Online": False,
                    "TailscaleIPs": ["100.64.0.12"],
                },
            },
        }
    )

    identity = parse_tailnet_identity(payload)

    assert identity.self_node_id == "nSelfNode"
    assert identity.peers == {"nNodeA": "100.64.0.11"}


def test_tailnet_identity_fails_closed_on_a_duplicate_stable_node_id() -> None:
    payload = json.dumps(
        {
            "Peer": {
                "node-a": {
                    "ID": "nNodeA",
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.11"],
                },
                "node-b": {
                    "ID": "nNodeA",
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.12"],
                },
            }
        }
    )

    with pytest.raises(ChatError, match="Tailscale status"):
        parse_tailnet_identity(payload)


def test_tailnet_identity_fails_closed_on_a_shared_address() -> None:
    payload = json.dumps(
        {
            "Peer": {
                "node-a": {
                    "ID": "nNodeA",
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.11"],
                },
                "node-b": {
                    "ID": "nNodeB",
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.11"],
                },
            }
        }
    )

    with pytest.raises(ChatError, match="Tailscale status"):
        parse_tailnet_identity(payload)


def test_legacy_broker_peer_request_filters_new_devin_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[bool] = []

    def observed_peers(
        _root: Path,
        *,
        include_remote: bool,
        internal: bool,
        include_delivery_mode: bool,
        include_title: bool,
        include_devin: bool,
        handle: str | None,
    ) -> dict[str, object]:
        assert include_remote is False
        assert internal is True
        assert include_delivery_mode is False
        assert include_title is False
        assert handle is None
        calls.append(include_devin)
        return {"schema_version": 1, "peers": []}

    monkeypatch.setattr(tailnet_broker_module, "peers", observed_peers)

    result = handle_broker_request(
        tmp_path,
        {"schema_version": 1, "operation": "peers"},
        "100.64.0.2",
    )

    assert result == {"schema_version": 1, "peers": []}
    assert calls == [False]


def test_new_devin_discovery_falls_back_to_legacy_broker_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid4())
    peer = {
        "alias": "codex@remote:api:123456789abc",
        "provider": "codex",
        "device": "remote",
        "project": "api",
        "status": "available",
        "generation": str(uuid4()),
        "session_key": session_key("codex", session_id),
    }
    calls: list[dict[str, object]] = []

    def old_broker(
        _address: str, payload: dict[str, object], *, timeout: float
    ) -> dict[str, object]:
        del timeout
        calls.append(payload)
        if "include_devin" in payload:
            raise ChatError("legacy broker rejected unknown field")
        return {"schema_version": 1, "peers": [peer]}

    monkeypatch.setattr(runtime, "request_tailnet", old_broker)

    targets, complete = runtime._remote_node_targets(
        "100.64.0.2",
        deadline=time.monotonic() + 2,
        include_devin=True,
    )

    assert complete is True
    assert [target.provider for target in targets] == ["codex"]
    assert calls == [
        {"schema_version": 1, "operation": "peers", "include_devin": True},
        {"schema_version": 1, "operation": "peers"},
    ]


def test_negotiated_devin_roster_preserves_delivery_mode_and_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid4())
    peer = {
        "alias": "devin@remote:api",
        "provider": "devin",
        "device": "remote",
        "project": "api",
        "status": "available",
        "generation": str(uuid4()),
        "session_key": session_key("devin", session_id),
        "delivery_mode": "devin_stop_or_prompt_bound",
        "title": "Devin canary",
    }
    codex_peer = {
        "alias": "codex@remote:api:123456789abc",
        "provider": "codex",
        "device": "remote",
        "project": "api",
        "status": "available",
        "generation": str(uuid4()),
        "session_key": session_key("codex", str(uuid4())),
        "delivery_mode": "codex_stop_bound",
        "title": "Codex peer",
    }
    calls: list[dict[str, object]] = []

    def new_broker(
        _address: str, payload: dict[str, object], *, timeout: float
    ) -> dict[str, object]:
        del timeout
        calls.append(payload)
        items = (
            [peer, codex_peer]
            if "include_title" in payload
            else [
                {key: value for key, value in peer.items() if key != "title"},
                {key: value for key, value in codex_peer.items() if key != "title"},
            ]
        )
        return {"schema_version": 1, "peers": items}

    monkeypatch.setattr(runtime, "request_tailnet", new_broker)

    targets, complete = runtime._remote_node_targets(
        "100.64.0.2",
        deadline=time.monotonic() + 2,
        include_devin=True,
        include_delivery_mode=True,
        include_title=True,
    )

    assert complete is True
    assert {target.provider for target in targets} == {"devin", "codex"}
    devin = next(target for target in targets if target.provider == "devin")
    codex = next(target for target in targets if target.provider == "codex")
    assert devin.delivery_mode == "devin_stop_or_prompt_bound"
    assert devin.title == "Devin canary"
    assert codex.delivery_mode == "codex_stop_bound"
    assert codex.title == "Codex peer"
    assert calls == [
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
            "include_devin": True,
        },
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
            "include_title": True,
            "include_devin": True,
        },
    ]


def _remote_peer(*, provider: str = "codex", title: str | None = None) -> dict[str, object]:
    session = str(uuid4())
    peer: dict[str, object] = {
        "alias": f"{provider}@remote:api:123456789abc",
        "provider": provider,
        "device": "remote",
        "project": "api",
        "status": "available",
        "generation": str(uuid4()),
        "session_key": session_key("codex", session),
    }
    if title is not None:
        peer["title"] = title
    return peer


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


def test_remote_node_targets_base_then_rich_success_preserves_identity_and_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    base["delivery_mode"] = "codex_stop_bound"
    rich = {**base, "title": "Remote Canary"}
    clock = _FakeClock()
    calls: list[tuple[dict[str, object], float]] = []

    def request(_address: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        calls.append((payload, timeout))
        return {"schema_version": 1, "peers": [base if len(calls) == 1 else rich]}

    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runtime, "request_tailnet", request)

    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=22.0, include_delivery_mode=True, include_title=True
    )

    assert complete is True
    assert [payload for payload, _ in calls] == [
        {"schema_version": 1, "operation": "peers", "include_delivery_mode": True},
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
            "include_title": True,
        },
    ]
    assert calls[1][1] == pytest.approx(21.0)
    assert targets[0].alias == base["alias"]
    assert targets[0].generation == base["generation"]
    assert targets[0].session_key == base["session_key"]
    assert targets[0].delivery_mode == "codex_stop_bound"
    assert targets[0].title == "Remote Canary"


def test_remote_node_targets_slow_rich_timeout_retains_valid_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    base["delivery_mode"] = "codex_stop_bound"
    clock = _FakeClock()
    calls: list[float] = []

    def request(_address: str, _payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        calls.append(timeout)
        if len(calls) == 1:
            return {"schema_version": 1, "peers": [base]}
        clock.now += timeout
        raise ChatError("rich request timed out")

    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runtime, "request_tailnet", request)

    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=22.0, include_delivery_mode=True, include_title=True
    )

    assert complete is True
    assert targets[0].alias == base["alias"]
    assert targets[0].title is None
    assert targets[0].delivery_mode == "codex_stop_bound"
    assert calls[1] <= 21.0 + 1e-9
    assert clock.now <= 22.0


def test_remote_node_targets_mode_base_prompt_failure_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    calls: list[dict[str, object]] = []

    def request(_address: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        calls.append(payload)
        if len(calls) == 1:
            raise ChatError("old server")
        return {"schema_version": 1, "peers": [base]}

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=time.monotonic() + 22.0, include_delivery_mode=True
    )

    assert complete is True
    assert targets[0].alias == base["alias"]
    assert calls == [
        {"schema_version": 1, "operation": "peers", "include_delivery_mode": True},
        {"schema_version": 1, "operation": "peers"},
    ]


def test_remote_node_targets_title_only_uses_rich_wire_but_hides_mode_publicly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    rich = {**base, "delivery_mode": "codex_stop_bound", "title": "Title only"}
    calls: list[dict[str, object]] = []

    def request(_address: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        calls.append(payload)
        return {"schema_version": 1, "peers": [base if len(calls) == 1 else rich]}

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=time.monotonic() + 22.0, include_title=True
    )

    assert complete is True
    assert calls == [
        {"schema_version": 1, "operation": "peers"},
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
            "include_title": True,
        },
    ]
    assert targets[0].title == "Title only"
    assert targets[0].delivery_mode is None


def test_remote_node_targets_without_title_uses_one_base_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    calls: list[dict[str, object]] = []

    def request(_address: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        calls.append(payload)
        return {"schema_version": 1, "peers": [base]}

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=time.monotonic() + 22.0, include_delivery_mode=True
    )

    assert complete is True
    assert len(targets) == 1
    assert calls == [{"schema_version": 1, "operation": "peers", "include_delivery_mode": True}]


def test_remote_node_targets_little_remaining_budget_skips_optional_rich_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    clock = _FakeClock(21.5)
    calls: list[dict[str, object]] = []

    def request(_address: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        calls.append(payload)
        return {"schema_version": 1, "peers": [base]}

    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=22.0, include_title=True
    )

    assert complete is True
    assert len(targets) == 1
    assert calls == [{"schema_version": 1, "operation": "peers"}]


def test_remote_node_targets_no_base_response_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def request(_address: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        calls.append(payload)
        raise ChatError("no response")

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=time.monotonic() + 22.0, include_delivery_mode=True
    )

    assert targets == []
    assert complete is False
    assert calls == [
        {"schema_version": 1, "operation": "peers", "include_delivery_mode": True},
        {"schema_version": 1, "operation": "peers"},
    ]


@pytest.mark.parametrize(
    "field",
    ["alias", "generation", "session_key", "provider", "device", "project", "delivery_mode"],
)
def test_remote_node_targets_rich_identity_drift_retains_base(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    base = _remote_peer()
    base["delivery_mode"] = "codex_stop_bound"
    rich = {**base, "title": "Drifted"}
    if field == "alias":
        rich["alias"] = "codex@remote:other:123456789abc"
    elif field == "generation":
        rich["generation"] = str(uuid4())
    elif field == "session_key":
        rich["session_key"] = "b" * 64
    elif field == "provider":
        rich["provider"] = "claude"
    elif field == "device":
        rich["device"] = "other"
    elif field == "project":
        rich["project"] = "other"
    else:
        rich["delivery_mode"] = "codex_experimental_queue"
    calls = 0

    def request(_address: str, _payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"schema_version": 1, "peers": [base if calls == 1 else rich]}

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1",
        deadline=time.monotonic() + 22.0,
        include_delivery_mode=True,
        include_title=True,
    )

    assert complete is True
    assert targets[0].alias == base["alias"]
    assert targets[0].generation == base["generation"]
    assert targets[0].delivery_mode == "codex_stop_bound"
    assert targets[0].title is None


def test_remote_node_targets_rich_address_drift_retains_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    rich = {**base, "title": "Drifted"}
    calls = 0
    original = runtime._targets_from_tailnet

    def parse(
        address: str,
        raw: object,
        *,
        include_delivery_mode: bool = False,
        include_title: bool = False,
        include_devin: bool = False,
        node_id: str | None = None,
    ) -> list[Target]:
        nonlocal calls
        calls += 1
        targets = original(
            address,
            raw,
            include_delivery_mode=include_delivery_mode,
            include_title=include_title,
            include_devin=include_devin,
            node_id=node_id,
        )
        if calls == 2:
            targets[0] = replace(targets[0], tailnet_address="100.64.0.2")
        return targets

    def request(_address: str, _payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        return {"schema_version": 1, "peers": [base if calls == 0 else rich]}

    monkeypatch.setattr(runtime, "_targets_from_tailnet", parse)
    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=time.monotonic() + 22.0, include_title=True
    )

    assert complete is True
    assert targets[0].tailnet_address == "100.64.0.1"
    assert targets[0].title is None


def test_remote_node_targets_malformed_rich_response_retains_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    calls = 0

    def request(_address: str, _payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"schema_version": 1, "peers": [base]} if calls == 1 else {"schema_version": 1}

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=time.monotonic() + 22.0, include_title=True
    )

    assert complete is True
    assert targets[0].alias == base["alias"]
    assert targets[0].title is None


@pytest.mark.parametrize("field", ["provider", "delivery_mode"])
def test_remote_node_targets_malformed_rich_metadata_retains_base(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    base = _remote_peer()
    rich = {**base, "title": "Malformed"}
    rich[field] = []
    calls = 0

    def request(_address: str, _payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"schema_version": 1, "peers": [base if calls == 1 else rich]}

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=time.monotonic() + 22.0, include_title=True
    )

    assert complete is True
    assert targets[0].alias == base["alias"]
    assert targets[0].title is None


def test_remote_node_targets_duplicate_rich_response_retains_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    rich = {**base, "title": "Duplicate"}
    calls = 0

    def request(_address: str, _payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return (
            {"schema_version": 1, "peers": [base]}
            if calls == 1
            else {
                "schema_version": 1,
                "peers": [rich, rich],
            }
        )

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=time.monotonic() + 22.0, include_title=True
    )

    assert complete is True
    assert len(targets) == 1
    assert targets[0].title is None


def test_remote_node_targets_missing_rich_peer_snapshot_retains_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _remote_peer()
    calls = 0

    def request(_address: str, _payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return (
            {"schema_version": 1, "peers": [base]}
            if calls == 1
            else {
                "schema_version": 1,
                "peers": [],
            }
        )

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", deadline=time.monotonic() + 22.0, include_title=True
    )

    assert complete is True
    assert len(targets) == 1
    assert targets[0].alias == base["alias"]
    assert targets[0].title is None


def test_new_remote_title_negotiation_returns_title(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _remote_peer()
    peer = {**base, "title": "Remote Canary"}
    calls: list[dict[str, object]] = []

    def request(_address: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append(payload)
        return {"schema_version": 1, "peers": [base if len(calls) == 1 else peer]}

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", include_delivery_mode=True, include_title=True
    )

    assert complete is True
    assert targets[0].title == "Remote Canary"
    assert calls == [
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
        },
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
            "include_title": True,
        },
    ]


def test_new_remote_title_negotiation_retries_legacy_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    peer = _remote_peer()
    calls: list[dict[str, object]] = []

    def request(_address: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append(payload)
        if "include_title" in payload or "include_delivery_mode" in payload:
            raise ChatError("old peer")
        return {"schema_version": 1, "peers": [peer]}

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", include_delivery_mode=True, include_title=True
    )

    assert complete is True
    assert targets[0].title is None
    assert calls == [
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
        },
        {"schema_version": 1, "operation": "peers"},
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
            "include_title": True,
        },
    ]


def test_new_remote_title_negotiation_preserves_mode_from_an_older_mode_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = _remote_peer()
    peer["delivery_mode"] = "codex_stop_bound"
    calls: list[dict[str, object]] = []

    def request(_address: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        calls.append(payload)
        if "include_title" in payload:
            raise ChatError("peer does not support titles")
        return {"schema_version": 1, "peers": [peer]}

    monkeypatch.setattr(runtime, "request_tailnet", request)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.1", include_delivery_mode=True, include_title=True
    )

    assert complete is True
    assert targets[0].delivery_mode == "codex_stop_bound"
    assert targets[0].title is None
    assert calls == [
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
        },
        {
            "schema_version": 1,
            "operation": "peers",
            "include_delivery_mode": True,
            "include_title": True,
        },
    ]


def test_remote_title_must_be_a_valid_name() -> None:
    payload = {"schema_version": 1, "peers": [_remote_peer(title="bad\nname")]}

    with pytest.raises(ChatError, match="remote title"):
        runtime._targets_from_tailnet("100.64.0.1", payload, include_title=True)


def test_remote_title_and_delivery_mode_are_independent_negotiated_fields() -> None:
    claude = _remote_peer(provider="claude", title="Claude plan")
    codex = _remote_peer(title="Codex plan")
    codex["delivery_mode"] = "codex_stop_bound"

    targets = runtime._targets_from_tailnet(
        "100.64.0.1",
        {"schema_version": 1, "peers": [claude, codex]},
        include_delivery_mode=True,
        include_title=True,
    )

    assert [(target.provider, target.title, target.delivery_mode) for target in targets] == [
        ("claude", "Claude plan", None),
        ("codex", "Codex plan", "codex_stop_bound"),
    ]


def test_tailnet_discovery_rejects_malformed_status() -> None:
    with pytest.raises(ChatError, match="Tailscale status"):
        parse_tailnet_identity('{"Peer": []}')


def test_local_tailnet_address_uses_only_running_self_ipv4() -> None:
    payload = json.dumps(
        {
            "BackendState": "Running",
            "Self": {
                "TailscaleIPs": ["100.64.0.13", "fd7a:115c:a1e0::1"],
            },
        }
    )

    assert parse_local_tailnet_address(payload) == "100.64.0.13"
    assert (
        parse_local_tailnet_address(
            json.dumps({"BackendState": "Stopped", "Self": {"TailscaleIPs": ["100.1.2.3"]}})
        )
        is None
    )


def test_known_tailnet_address_survives_stopped_backend() -> None:
    payload = json.dumps(
        {
            "BackendState": "Stopped",
            "Self": {"TailscaleIPs": ["100.64.0.13", "fd7a:115c:a1e0::1"]},
        }
    )

    assert parse_known_tailnet_address(payload) == "100.64.0.13"


def test_ifconfig_tailnet_address_requires_one_utun_ipv4() -> None:
    payload = """en0: flags=8863<UP>
    inet 100.64.0.99 netmask 0xffffff00
utun4: flags=8051<UP>
    inet 100.64.0.13 --> 100.64.0.13 netmask 0xffffffff
"""

    assert parse_ifconfig_tailnet_address(payload) == "100.64.0.13"
    second_interface = payload + "utun5: flags=8051<UP>\n    inet 100.64.0.14\n"
    assert parse_ifconfig_tailnet_address(second_interface) is None


def test_unverified_cgnat_interface_never_becomes_a_tailnet_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cross_agent_chat.tailnet._status_output", lambda: None)
    monkeypatch.setattr(
        "cross_agent_chat.tailnet._ifconfig_tailnet_address", lambda: "100.90.10.11"
    )

    assert local_tailnet_address() is None


def test_running_tailscale_identity_requires_the_current_interface_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cross_agent_chat.tailnet._status_output",
        lambda: json.dumps({"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.13"]}}),
    )
    monkeypatch.setattr(
        "cross_agent_chat.tailnet._ifconfig_output", lambda: "utun4:\n inet 100.64.0.13"
    )

    assert local_tailnet_address() == "100.64.0.13"


def test_tailnet_broker_exposes_only_local_live_peers(tmp_path: Path) -> None:
    assert handle_broker_request(
        tmp_path,
        {"schema_version": 1, "operation": "peers"},
        "100.64.0.10",
    ) == {"schema_version": 1, "peers": []}


def test_broker_keeps_only_localhost_when_tailscale_identity_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CROSS_AGENT_CHAT_TAILNET_ADDRESS", "100.64.0.13")
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.local_tailnet_address", lambda: None)

    assert broker_bindings() == [("127.0.0.1", 47072)]


def test_broker_prefers_current_tailnet_address_over_persisted_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CROSS_AGENT_CHAT_TAILNET_ADDRESS", "100.64.0.13")
    monkeypatch.setattr(
        "cross_agent_chat.tailnet_broker.local_tailnet_address", lambda: "100.64.0.14"
    )

    assert broker_bindings() == [("127.0.0.1", 47072), ("100.64.0.14", 47071)]


def test_tailnet_listener_defers_only_until_address_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = mock.Mock()
    listener.bind.side_effect = OSError(errno.EADDRNOTAVAIL, "fixture")
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.socket.socket", lambda *_args: listener)

    assert bind_broker_listener(("100.64.0.13", 47071), allow_unavailable=True) is None
    listener.close.assert_called_once_with()


def test_local_listener_does_not_suppress_unavailable_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = mock.Mock()
    listener.bind.side_effect = OSError(errno.EADDRNOTAVAIL, "fixture")
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.socket.socket", lambda *_args: listener)

    with pytest.raises(OSError) as error:
        bind_broker_listener(("127.0.0.1", 47072))

    assert error.value.errno == errno.EADDRNOTAVAIL
    listener.close.assert_called_once_with()


def test_tailnet_listener_does_not_suppress_address_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = mock.Mock()
    listener.bind.side_effect = OSError(errno.EADDRINUSE, "fixture")
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.socket.socket", lambda *_args: listener)

    with pytest.raises(OSError) as error:
        bind_broker_listener(("100.64.0.13", 47071), allow_unavailable=True)

    assert error.value.errno == errno.EADDRINUSE
    listener.close.assert_called_once_with()


def test_broker_keeps_local_listener_and_discovers_tailnet_after_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_listener = mock.Mock()
    tailnet_listener = mock.Mock()
    bindings: list[tuple[tuple[str, int], bool]] = []

    def bind(binding: tuple[str, int], *, allow_unavailable: bool = False) -> object:
        bindings.append((binding, allow_unavailable))
        if allow_unavailable:
            return tailnet_listener
        return local_listener

    select_timeouts: list[float | None] = []
    refresh_calls = 0
    refresh_ready = threading.Event()
    second_refresh_ready = threading.Event()
    clock = _FakeClock()

    def refresh_bindings() -> list[tuple[str, int]]:
        nonlocal refresh_calls
        refresh_calls += 1
        refresh_ready.set()
        if refresh_calls == 2:
            second_refresh_ready.set()
        return next(discovered)

    def select_once_then_stop(
        _readers: object, _writers: object, _errors: object, timeout: float | None
    ) -> tuple[list[object], list[object], list[object]]:
        select_timeouts.append(timeout)
        if len(select_timeouts) == 1:
            assert refresh_ready.wait(timeout=1.0)
        elif len(select_timeouts) == 2:
            clock.now = tailnet_broker_module.TAILNET_BIND_RETRY_SECONDS
        elif len(select_timeouts) == 3:
            assert second_refresh_ready.wait(timeout=1.0)
        elif len(select_timeouts) == 4:
            raise RuntimeError("stop fixture")
        return [], [], []

    monkeypatch.setattr("cross_agent_chat.tailnet_broker.bind_broker_listener", bind)
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.select.select", select_once_then_stop)
    discovered = iter(
        (
            [("127.0.0.1", 47072)],
            [("127.0.0.1", 47072), ("100.64.0.13", 47071)],
        )
    )
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.broker_bindings", refresh_bindings)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    with pytest.raises(RuntimeError, match="stop fixture"):
        broker_server(str(tmp_path))

    assert bindings == [
        (("127.0.0.1", 47072), False),
        (("100.64.0.13", 47071), True),
    ]
    assert select_timeouts[-2:] == [0.1, 5.0]
    local_listener.close.assert_called_once_with()
    tailnet_listener.close.assert_called_once_with()


def test_broker_admits_completed_refresh_on_the_next_pending_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_listener = mock.Mock()
    tailnet_listener = mock.Mock()
    select_timeouts: list[float | None] = []
    refresh: Future[list[tuple[str, int]]] = Future()

    class ControlledExecutor:
        def __init__(self, *, max_workers: int, **_kwargs: object) -> None:
            self.max_workers = max_workers

        def __enter__(self) -> ControlledExecutor:
            return self

        def __exit__(
            self,
            _type: type[BaseException] | None,
            _value: BaseException | None,
            _traceback: object,
        ) -> None:
            return None

        def submit(self, _function: object) -> Future[list[tuple[str, int]]]:
            assert self.max_workers == 1
            return refresh

    def bind(binding: tuple[str, int], *, allow_unavailable: bool = False) -> object:
        if allow_unavailable:
            assert binding == ("100.64.0.13", 47071)
            return tailnet_listener
        assert binding == ("127.0.0.1", 47072)
        return local_listener

    def select_after_refresh(
        readers: list[object], _writers: object, _errors: object, timeout: float | None
    ) -> tuple[list[object], list[object], list[object]]:
        select_timeouts.append(timeout)
        if len(select_timeouts) == 1:
            assert readers == [local_listener]
            assert timeout == tailnet_broker_module.TAILNET_REFRESH_POLL_SECONDS
            refresh.set_result([("127.0.0.1", 47072), ("100.64.0.13", 47071)])
            return [], [], []
        assert readers == [local_listener, tailnet_listener]
        assert timeout is not None
        assert 0.0 < timeout <= tailnet_broker_module.TAILNET_BIND_RETRY_SECONDS
        raise RuntimeError("stop fixture")

    monkeypatch.setattr(tailnet_broker_module, "bind_broker_listener", bind)
    monkeypatch.setattr(tailnet_broker_module, "ThreadPoolExecutor", ControlledExecutor)
    monkeypatch.setattr(select, "select", select_after_refresh)

    with pytest.raises(RuntimeError, match="stop fixture"):
        broker_server(str(tmp_path))

    assert select_timeouts[0] == tailnet_broker_module.TAILNET_REFRESH_POLL_SECONDS
    local_listener.close.assert_called_once_with()
    tailnet_listener.close.assert_called_once_with()


def test_broker_serves_local_health_while_tailnet_refresh_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled optional refresh must not delay the required local health endpoint."""
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    stop = threading.Event()
    bound = threading.Event()
    listeners: list[socket.socket] = []
    failures: list[BaseException] = []
    original_select = select.select

    def bind(binding: tuple[str, int], *, allow_unavailable: bool = False) -> socket.socket | None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(binding)
        listener.listen(16)
        listener.setblocking(False)
        listeners.append(listener)
        if not allow_unavailable:
            bound.set()
        return listener

    def blocking_bindings() -> list[tuple[str, int]]:
        refresh_started.set()
        assert release_refresh.wait(timeout=2.0)
        return [("127.0.0.1", 0)]

    def selectable(
        readers: list[socket.socket],
        writers: list[socket.socket],
        errors: list[socket.socket],
        timeout: float,
    ) -> tuple[list[socket.socket], list[socket.socket], list[socket.socket]]:
        if stop.is_set():
            raise RuntimeError("stop fixture")
        return original_select(readers, writers, errors, min(timeout, 0.01))

    def run_server() -> None:
        try:
            broker_server(str(tmp_path))
        except RuntimeError as error:
            if str(error) != "stop fixture":
                failures.append(error)

    monkeypatch.setattr(tailnet_broker_module, "LOCAL_BROKER_PORT", 0)
    monkeypatch.setattr(tailnet_broker_module, "bind_broker_listener", bind)
    monkeypatch.setattr(tailnet_broker_module, "broker_bindings", blocking_bindings)
    monkeypatch.setattr(select, "select", selectable)

    server_thread = threading.Thread(target=run_server)
    server_thread.start()
    try:
        assert bound.wait(timeout=1.0)
        assert refresh_started.wait(timeout=1.0)
        with socket.create_connection(listeners[0].getsockname(), timeout=0.2) as connection:
            connection.settimeout(0.4)
            connection.sendall(b'{"schema_version":1,"operation":"health"}\n')
            response = json.loads(connection.recv(4096))
        assert response["status"] == "READY"
    finally:
        release_refresh.set()
        stop.set()
        server_thread.join(timeout=2.0)

    assert not server_thread.is_alive()
    assert failures == []


def test_broker_excludes_remote_listener_while_refresh_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_listener = mock.Mock()
    remote_listener = mock.Mock()
    listeners = iter((local_listener, remote_listener))
    clock = _FakeClock()
    first_refresh_ready = threading.Event()
    second_refresh_started = threading.Event()
    release_second_refresh = threading.Event()
    bindings = iter(
        (
            [("127.0.0.1", 47072), ("100.64.0.13", 47071)],
            [("127.0.0.1", 47072), ("100.64.0.13", 47071)],
        )
    )
    dispatched: list[list[object]] = []

    def refresh_bindings() -> list[tuple[str, int]]:
        result = next(bindings)
        if not first_refresh_ready.is_set():
            first_refresh_ready.set()
            return result
        second_refresh_started.set()
        assert release_second_refresh.wait(timeout=1.0)
        return result

    def select_until_pending(
        readers: list[object], *_args: object
    ) -> tuple[list[object], list[object], list[object]]:
        if remote_listener in readers:
            clock.now = tailnet_broker_module.TAILNET_BIND_RETRY_SECONDS
            return [], [], []
        if not first_refresh_ready.is_set():
            assert first_refresh_ready.wait(timeout=1.0)
            return [], [], []
        assert second_refresh_started.wait(timeout=1.0)
        assert readers == [local_listener]
        release_second_refresh.set()
        raise RuntimeError("stop fixture")

    def record_dispatch(
        _workers: object,
        _root: Path,
        readable: list[object],
        _admission: BrokerAdmission,
        _callbacks: object = None,
        _refusals: object = None,
    ) -> int:
        dispatched.append(readable)
        return 0

    monkeypatch.setattr(
        tailnet_broker_module, "bind_broker_listener", lambda *_args, **_kwargs: next(listeners)
    )
    monkeypatch.setattr(tailnet_broker_module, "broker_bindings", refresh_bindings)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(select, "select", select_until_pending)
    monkeypatch.setattr(
        tailnet_broker_module,
        "dispatch_ready_brokers",
        record_dispatch,
    )

    with pytest.raises(RuntimeError, match="stop fixture"):
        broker_server(str(tmp_path))

    assert dispatched
    assert all(readable == [] for readable in dispatched)
    remote_listener.close.assert_called_once_with()
    local_listener.close.assert_called_once_with()


def test_broker_keeps_verified_listener_when_refresh_confirms_same_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_listener = mock.Mock()
    remote_listener = mock.Mock()
    listeners = iter((local_listener, remote_listener))
    clock = _FakeClock()
    refresh_calls = 0
    first_refresh_ready = threading.Event()
    second_refresh_ready = threading.Event()

    def refresh_bindings() -> list[tuple[str, int]]:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            first_refresh_ready.set()
        else:
            second_refresh_ready.set()
        return [("127.0.0.1", 47072), ("100.64.0.13", 47071)]

    def select_until_confirmed(
        readers: list[object], *_args: object
    ) -> tuple[list[object], list[object], list[object]]:
        if not first_refresh_ready.is_set():
            assert first_refresh_ready.wait(timeout=1.0)
            return [], [], []
        if refresh_calls == 1:
            if remote_listener in readers:
                clock.now = tailnet_broker_module.TAILNET_BIND_RETRY_SECONDS
            return [], [], []
        if refresh_calls == 2 and remote_listener not in readers:
            assert second_refresh_ready.wait(timeout=1.0)
            return [], [], []
        assert readers == [local_listener, remote_listener]
        raise RuntimeError("stop fixture")

    monkeypatch.setattr(
        tailnet_broker_module, "bind_broker_listener", lambda *_args, **_kwargs: next(listeners)
    )
    monkeypatch.setattr(tailnet_broker_module, "broker_bindings", refresh_bindings)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(select, "select", select_until_confirmed)

    with pytest.raises(RuntimeError, match="stop fixture"):
        broker_server(str(tmp_path))

    assert refresh_calls == 2
    remote_listener.close.assert_called_once_with()
    local_listener.close.assert_called_once_with()


def test_broker_replaces_listener_when_tailnet_address_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_listener = mock.Mock()
    first_tailnet = mock.Mock()
    second_tailnet = mock.Mock()
    listeners = iter((local_listener, first_tailnet, second_tailnet))
    discovered = iter(
        (
            [("127.0.0.1", 47072), ("100.64.0.13", 47071)],
            [("127.0.0.1", 47072), ("100.64.0.14", 47071)],
        )
    )

    monkeypatch.setattr(
        "cross_agent_chat.tailnet_broker.bind_broker_listener",
        lambda _binding, **_kwargs: next(listeners),
    )
    refresh_calls = 0
    refresh_ready = threading.Event()
    second_refresh_ready = threading.Event()
    clock = _FakeClock()

    def refresh_bindings() -> list[tuple[str, int]]:
        nonlocal refresh_calls
        refresh_calls += 1
        refresh_ready.set()
        if refresh_calls == 2:
            second_refresh_ready.set()
        return next(discovered)

    monkeypatch.setattr("cross_agent_chat.tailnet_broker.broker_bindings", refresh_bindings)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    calls = 0

    def select_once_then_stop(*_args: object) -> tuple[list[object], list[object], list[object]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert refresh_ready.wait(timeout=1.0)
        elif calls == 2:
            clock.now = tailnet_broker_module.TAILNET_BIND_RETRY_SECONDS
        elif calls == 3:
            assert second_refresh_ready.wait(timeout=1.0)
        elif calls == 4:
            raise RuntimeError("stop fixture")
        return [], [], []

    monkeypatch.setattr("cross_agent_chat.tailnet_broker.select.select", select_once_then_stop)

    with pytest.raises(RuntimeError, match="stop fixture"):
        broker_server(str(tmp_path))

    first_tailnet.close.assert_called_once_with()
    local_listener.close.assert_called_once_with()
    second_tailnet.close.assert_called_once_with()


def test_tailnet_broker_rejects_extra_request_fields(tmp_path: Path) -> None:
    with pytest.raises(ChatError, match="broker request"):
        handle_broker_request(
            tmp_path,
            {"schema_version": 1, "operation": "peers", "extra": True},
            "100.64.0.10",
        )


def test_tailnet_broker_serves_one_framed_request(tmp_path: Path) -> None:
    client, server = socket.socketpair()
    try:
        client.sendall(b'{"schema_version":1,"operation":"peers"}\n')
        client.shutdown(socket.SHUT_WR)

        serve_broker_connection(tmp_path, server, "100.64.0.10")

        response = json.loads(client.recv(4096))
        assert response == {"schema_version": 1, "peers": []}
    finally:
        client.close()
        server.close()


def test_tailnet_client_crosses_real_tcp_boundary(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            serve_broker_connection(tmp_path, connection, "127.0.0.1")

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        response = request_tailnet(
            "127.0.0.1",
            {"schema_version": 1, "operation": "health"},
            port=port,
        )
        assert response == {
            "schema_version": 1,
            "status": "READY",
            "pid": os.getpid(),
            "version": __version__,
            "module_path": str(Path(tailnet_broker_module.__file__).resolve()),
        }
    finally:
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive()


def test_silent_connection_does_not_block_another_broker_request(tmp_path: Path) -> None:
    silent_client, silent_server = socket.socketpair()
    active_client, active_server = socket.socketpair()
    admission = BrokerAdmission()
    with ThreadPoolExecutor(max_workers=2) as workers:
        dispatch_broker_connection(workers, tmp_path, silent_server, "100.64.0.11", admission)
        dispatch_broker_connection(workers, tmp_path, active_server, "100.64.0.10", admission)
        active_client.sendall(b'{"schema_version":1,"operation":"peers"}\n')
        active_client.settimeout(0.5)

        assert json.loads(active_client.recv(4096)) == {
            "schema_version": 1,
            "peers": [],
        }

        silent_client.close()
        active_client.close()


def test_accepted_remote_connection_survives_listener_close(tmp_path: Path) -> None:
    class RemoteListener(socket.socket):
        def accept(self) -> tuple[socket.socket, tuple[str, int]]:
            connection, _ = super().accept()
            return connection, ("100.64.0.11", 47071)

    listener = RemoteListener(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    try:
        with (
            socket.create_connection(listener.getsockname(), timeout=0.5) as client,
            ThreadPoolExecutor(max_workers=1) as workers,
        ):
            assert (
                dispatch_ready_brokers(
                    workers,
                    tmp_path,
                    [listener],
                    BrokerAdmission(),
                )
                == 1
            )
            listener.close()
            client.sendall(b'{"schema_version":1,"operation":"peers"}\n')
            client.settimeout(0.5)
            assert json.loads(client.recv(4096)) == {
                "schema_version": 1,
                "peers": [],
            }
    finally:
        listener.close()


def test_vanished_ready_connection_does_not_block_broker_loop(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert (
                dispatch_ready_brokers(
                    workers,
                    tmp_path,
                    [listener],
                    BrokerAdmission(),
                )
                == 0
            )
    finally:
        listener.close()


def test_aborted_ready_connection_does_not_block_another_listener(tmp_path: Path) -> None:
    class AbortedListener(socket.socket):
        def accept(self) -> tuple[socket.socket, tuple[str, int]]:
            raise ConnectionAbortedError

    aborted = AbortedListener(socket.AF_INET, socket.SOCK_STREAM)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    client = socket.create_connection(listener.getsockname(), timeout=2)
    client.sendall(b'{"schema_version":1,"operation":"health"}\n')
    client.shutdown(socket.SHUT_WR)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert (
                dispatch_ready_brokers(
                    workers,
                    tmp_path,
                    [aborted, listener],
                    BrokerAdmission(),
                )
                == 1
            )
        client.settimeout(2)
        response = json.loads(client.recv(4096))
        assert response["status"] == "READY"
    finally:
        client.close()
        listener.close()
        aborted.close()


def test_broker_admission_rejects_third_connection_from_one_peer() -> None:
    admission = BrokerAdmission()

    assert admission.acquire("100.64.0.10")
    assert admission.acquire("100.64.0.10")
    assert not admission.acquire("100.64.0.10")

    admission.release("100.64.0.10")
    admission.release("100.64.0.10")


def test_remote_authorization_binds_source_target_and_payload(tmp_path: Path) -> None:
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="source",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    event_id = str(uuid4())
    target_key = "b" * 64
    target_generation = str(uuid4())
    IntentStore(tmp_path).begin_identity(
        source_key=session_key(source.provider, source.session_id),
        source_generation=source.generation,
        source_alias=source.alias,
        target_key=target_key,
        target_generation=target_generation,
        payload_digest="a" * 64,
        event_id=event_id,
    )

    assert (
        authorize_remote(
            tmp_path,
            event_id=event_id,
            source_alias=source.alias,
            source_generation=source.generation,
            target_key=target_key,
            target_generation=target_generation,
            payload_digest="a" * 64,
        )["status"]
        == "AUTHORIZED"
    )

    # A second claim is refused, and the refusal is ANSWERED rather than raised.
    # Raising closed the connection with no frame, so the peer read EOF as an
    # unknown outcome and this sender recorded its own decided refusal as
    # UNKNOWN_DELIVERY, freezing an event that provably produced no effect.
    refusal = authorize_remote(
        tmp_path,
        event_id=event_id,
        source_alias=source.alias,
        source_generation=source.generation,
        target_key=target_key,
        target_generation=target_generation,
        payload_digest="a" * 64,
    )
    assert refusal["status"] == "REFUSED"
    assert refusal["event_id"] == event_id

    assert IntentStore(tmp_path).intents()[0].status == "REMOTE_AUTHORIZED"


def test_remote_receive_requires_source_authorization_before_provider_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    envelope = remote_envelope(
        event_id=str(uuid4()),
        source_alias="codex@source:api:source-a1",
        source_generation=str(uuid4()),
        target_alias=target.alias,
        generation=target.generation,
        message="hello",
    )

    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda *_args, **_kwargs: {"schema_version": 1, "status": "DENIED"},
    )

    def provider_boundary(
        _path: Path, payload: dict[str, object], **_: object
    ) -> dict[str, object]:
        if payload.get("operation") == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "generation": target.generation,
                "alias": target.alias,
            }
        pytest.fail("provider delivery ran before source authorization")

    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", provider_boundary)

    response = receive_remote(tmp_path, envelope, "100.64.0.11")
    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["provider"] == "codex"


def test_remote_receive_routes_registered_codex_original_to_its_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    original = Route.create(
        provider="codex",
        session_id="00000000-0000-4000-8000-000000000001",
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
        generation="00000000-0000-4000-8000-000000000011",
        owner_identity="a" * 64,
        profile_root=str(profile),
    )
    from cross_agent_chat.native_helper import NativeHelperStore

    store = NativeHelperStore(tmp_path)
    binding, nonce = store.reserve(original, "a" * 64)
    helper_root = tmp_path / binding.helper_directory
    helper_root.mkdir()
    helper = Route.create(
        provider="codex",
        session_id="00000000-0000-4000-8000-000000000002",
        device="target",
        cwd=str(helper_root),
        pid=os.getpid(),
        generation="00000000-0000-4000-8000-000000000012",
        owner_identity="a" * 64,
        profile_root=str(profile),
    )
    store.register(helper, nonce, original, "a" * 64)
    Registry(tmp_path).upsert(original)
    Registry(tmp_path).upsert(helper)
    public_target = Target(
        alias=original.alias,
        provider="codex",
        device=original.device,
        project=original.project,
        generation=original.generation,
        session_key=session_key("codex", original.session_id),
        remote=False,
        session_id=original.session_id,
        cwd=original.cwd,
        pid=original.pid,
    )
    event_id = str(uuid4())
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=str(uuid4()),
        target_alias=original.alias,
        generation=original.generation,
        message="hello",
    )
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda _: [public_target])
    monkeypatch.setattr("cross_agent_chat.runtime._route_current", lambda *_args: True)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda _address, payload, **_: (
            {key: value for key, value in payload.items() if key != "operation"}
            | {"status": "AUTHORIZED"}
        ),
    )

    def accept(path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        assert path == runtime.socket_path(tmp_path, helper)
        assert payload["generation"] == helper.generation
        return {
            "schema_version": 1,
            "event_id": event_id,
            "status": "TRANSPORT_ACCEPTED",
            "to": helper.alias,
            "provider": "codex",
        }

    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", accept)

    assert receive_remote(tmp_path, envelope, "100.64.0.11") == {
        "schema_version": 1,
        "event_id": event_id,
        "status": "TRANSPORT_ACCEPTED",
        "to": original.alias,
        "provider": "codex",
    }


def _remote_receive_target(tmp_path: Path) -> tuple[Route, Target]:
    route = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(route)
    return route, Target(
        alias=route.alias,
        provider="codex",
        device=route.device,
        project=route.project,
        generation=route.generation,
        session_key=session_key("codex", route.session_id),
        remote=False,
        session_id=route.session_id,
        cwd=route.cwd,
        pid=route.pid,
    )


def test_remote_receive_authorization_spend_shrinks_the_accept_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow authorization answer spends from the same budget the accept uses.

    The sender-broker callback and the provider accept draw from one
    absolute deadline, so time the callback consumed is no longer
    available to the accept.
    """
    route, public_target = _remote_receive_target(tmp_path)
    event_id = str(uuid4())
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=str(uuid4()),
        target_alias=route.alias,
        generation=route.generation,
        message="hello",
    )
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda _: [public_target])
    now = [1_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    authorize_timeouts: list[float] = []
    accept_timeouts: list[float] = []

    def authorize(
        _address: str, payload: dict[str, object], *, timeout: float, **_kwargs: object
    ) -> dict[str, object]:
        authorize_timeouts.append(timeout)
        now[0] += 30.0
        return {key: value for key, value in payload.items() if key != "operation"} | {
            "status": "AUTHORIZED"
        }

    def accept(
        _path: Path, payload: dict[str, object], *, timeout: float, **_kwargs: object
    ) -> dict[str, object]:
        accept_timeouts.append(timeout)
        return {
            "schema_version": 1,
            "event_id": event_id,
            "status": "TRANSPORT_ACCEPTED",
            "to": route.alias,
            "provider": "codex",
        }

    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", authorize)
    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", accept)

    assert receive_remote(tmp_path, envelope, "100.64.0.11")["status"] == "TRANSPORT_ACCEPTED"
    assert authorize_timeouts == [AUTHORIZE_TIMEOUT_SECONDS]
    assert accept_timeouts == [
        pytest.approx(AUTHORIZE_TIMEOUT_SECONDS + ACCEPT_TIMEOUT_SECONDS - 30.0)
    ]


def test_remote_receive_spent_budget_refuses_before_any_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget already spent by authorization leaves nothing for the accept.

    The accept is armed with the shared remainder -- here nothing -- rather
    than a fresh provider budget, so the receive stays a decided
    pre-effect refusal.
    """
    route, public_target = _remote_receive_target(tmp_path)
    event_id = str(uuid4())
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=str(uuid4()),
        target_alias=route.alias,
        generation=route.generation,
        message="hello",
    )
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda _: [public_target])
    now = [1_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    accept_timeouts: list[float] = []

    def authorize(
        _address: str, payload: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        now[0] += AUTHORIZE_TIMEOUT_SECONDS + ACCEPT_TIMEOUT_SECONDS + 1.0
        return {key: value for key, value in payload.items() if key != "operation"} | {
            "status": "AUTHORIZED"
        }

    def accept(
        _path: Path, payload: dict[str, object], *, timeout: float, **_kwargs: object
    ) -> dict[str, object]:
        accept_timeouts.append(timeout)
        raise ChatError("session courier is unavailable before delivery")

    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", authorize)
    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", accept)

    assert receive_remote(tmp_path, envelope, "100.64.0.11")["status"] == "PRE_EFFECT_REJECTED"
    assert accept_timeouts and accept_timeouts[0] <= 0.0


def test_remote_claude_receipt_uses_fresh_discovery_alias_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
        generation=str(uuid4()),
    )
    Registry(tmp_path).upsert(target)
    fresh_alias = target.alias + " Fresh"
    public_target = Target(
        alias=fresh_alias,
        provider="claude",
        device=target.device,
        project=target.project,
        generation=target.generation,
        session_key=session_key("claude", target.session_id),
        remote=False,
        session_id=target.session_id,
        cwd=target.cwd,
        pid=target.pid,
    )
    event_id = str(uuid4())
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=str(uuid4()),
        target_alias=fresh_alias,
        generation=target.generation,
        message="fresh independent work",
    )
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda _: [public_target])
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda _address, payload, **_: (
            {key: value for key, value in payload.items() if key != "operation"}
            | {"status": "AUTHORIZED"}
        ),
    )
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda _path, payload, **_: {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": fresh_alias,
            "provider": "claude",
        },
    )

    assert receive_remote(tmp_path, envelope, "100.64.0.11")["status"] == "TRANSPORT_ACCEPTED"


def test_tailnet_client_keeps_write_side_open_for_serve_proxy() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve_like_tailscale() -> None:
        connection, _ = listener.accept()
        with connection:
            request = b""
            while not request.endswith(b"\n"):
                request += connection.recv(4096)
            readable, _, _ = select.select([connection], [], [], 0.05)
            if readable and connection.recv(1) == b"":
                return
            connection.sendall(b'{"schema_version":1,"peers":[]}\n')

    thread = threading.Thread(target=serve_like_tailscale)
    thread.start()
    try:
        assert request_tailnet(
            "127.0.0.1",
            {"schema_version": 1, "operation": "peers"},
            port=port,
        ) == {"schema_version": 1, "peers": []}
    finally:
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive()


def test_remote_targets_are_discovered_without_peer_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def identity() -> TailnetIdentity:
        return TailnetIdentity(self_node_id="nSelf", peers={"nNodeA": "100.64.0.11"})

    def request(address: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        assert address == "100.64.0.11"
        assert payload == {"schema_version": 1, "operation": "peers"}
        assert 0 < timeout <= REMOTE_DISCOVERY_TIMEOUT_SECONDS
        return {
            "schema_version": 1,
            "peers": [
                {
                    "alias": "claude@studio:api:api-a1",
                    "provider": "claude",
                    "device": "studio",
                    "project": "api",
                    "status": "available",
                    "generation": "7d9ae03f-f86c-4c96-a40d-69f37f0a7189",
                    "session_key": "a" * 64,
                }
            ],
        }

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_identity", identity)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        request,
    )

    targets = remote_targets(tmp_path)

    assert len(targets) == 1
    assert targets[0].alias == "claude@studio:api:api-a1"
    assert targets[0].tailnet_address == "100.64.0.11"
    assert targets[0].tailnet_node_id == "nNodeA"


def test_remote_send_uses_tailnet_broker_without_ssh_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "100.64.0.11"
    target_alias = "claude@studio:api:api-a1"

    def identity() -> TailnetIdentity:
        return TailnetIdentity(self_node_id="nSelf", peers={"nNodeA": address})

    def request(
        actual_address: str,
        payload: dict[str, object],
        *,
        port: int = 47071,
        timeout: float = 2.0,
    ) -> dict[str, object]:
        assert actual_address == address
        assert port == 47071
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [
                    {
                        "alias": target_alias,
                        "provider": "claude",
                        "device": "studio",
                        "project": "api",
                        "status": "available",
                        "generation": "7d9ae03f-f86c-4c96-a40d-69f37f0a7189",
                        "session_key": "a" * 64,
                    }
                ],
            }
        assert timeout > 90
        envelope = json.loads(str(payload["envelope"]))
        return {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target_alias,
            "provider": "claude",
        }

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_identity", identity)
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="imac",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    result = send(tmp_path, source, target_alias, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert "hello" not in (tmp_path / "intents.json").read_text()


def test_exact_token_send_never_waits_on_a_silent_unrelated_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scoped token asks only its pinned node; a silent neighbor is never
    contacted, so it cannot hold the send hostage the way the 19 Sep roster
    collector did."""
    owner = "100.64.0.11"
    silent = "100.64.0.12"
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    peer = {
        "alias": "claude@studio:api:api-a1",
        "provider": "claude",
        "device": "studio",
        "project": "api",
        "status": "available",
        "generation": generation,
        "session_key": handle,
    }
    release = threading.Event()
    contacted: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        contacted.append(address)
        if address == silent:
            # The neighbor would accept the connection and never answer inside
            # its request budget; the send must never ask it.
            release.wait(timeout)
            raise ChatError("silent neighbor never answered")
        if payload.get("operation") == "peers":
            return {"schema_version": 1, "peers": [peer]}
        envelope = json.loads(str(payload["envelope"]))
        return {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": peer["alias"],
            "provider": "claude",
        }

    monkeypatch.setattr(
        "cross_agent_chat.runtime.tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nSilent": silent, "nOwner": owner}),
    )
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    monkeypatch.setattr("cross_agent_chat.runtime.REMOTE_DISCOVERY_TIMEOUT_SECONDS", 8.0)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="imac",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    started = time.monotonic()
    try:
        result = send(tmp_path, source, remote_token("nOwner", handle, generation), "hello")
    finally:
        release.set()

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["to"] == peer["alias"]
    assert time.monotonic() - started < 4.0
    assert contacted == [owner, owner]


def test_exact_token_send_ignores_a_clone_claimant_on_another_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forged clone attesting the same handle elsewhere is never consulted:
    the token pins one stable node and only that node is asked."""
    first = "100.64.0.11"
    clone = "100.64.0.12"
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    deliveries: list[tuple[str, dict[str, object]]] = []
    contacted: list[str] = []

    def peer(alias: str, device: str) -> dict[str, object]:
        return {
            "alias": alias,
            "provider": "claude",
            "device": device,
            "project": "api",
            "status": "available",
            "generation": generation,
            "session_key": handle,
        }

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        contacted.append(address)
        if payload.get("operation") == "peers":
            if address == clone:
                return {
                    "schema_version": 1,
                    "peers": [peer("claude@laptop:api:api-a1", "laptop")],
                }
            return {
                "schema_version": 1,
                "peers": [peer("claude@studio:api:api-a1", "studio")],
            }
        deliveries.append((address, payload))
        envelope = json.loads(str(payload["envelope"]))
        return {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": "claude@studio:api:api-a1",
            "provider": "claude",
        }

    monkeypatch.setattr(
        "cross_agent_chat.runtime.tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nOwner": first, "nClone": clone}),
    )
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    monkeypatch.setattr("cross_agent_chat.runtime.REMOTE_DISCOVERY_TIMEOUT_SECONDS", 8.0)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="imac",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    result = send(tmp_path, source, remote_token("nOwner", handle, generation), "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert contacted == [first, first]
    assert [address for address, _ in deliveries] == [first]


def test_a_node_that_never_answers_marks_discovery_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanswered node reports incomplete: discovery never claims a roster
    it did not finish reading, and nothing records a selection for it."""
    owner = "100.64.0.11"
    laggard = "100.64.0.12"
    handle = session_key("claude", str(uuid4()))
    peer = {
        "alias": "claude@studio:api:api-a1",
        "provider": "claude",
        "device": "studio",
        "project": "api",
        "status": "available",
        "generation": str(uuid4()),
        "session_key": handle,
    }
    release = threading.Event()

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        if address == laggard:
            release.wait(timeout)
            raise ChatError("laggard never answered")
        return {"schema_version": 1, "peers": [peer]}

    monkeypatch.setattr(
        "cross_agent_chat.runtime.tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nOwner": owner, "nLaggard": laggard}),
    )
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    monkeypatch.setattr("cross_agent_chat.runtime.REMOTE_DISCOVERY_TIMEOUT_SECONDS", 4.0)

    try:
        targets, complete = runtime._remote_discovery()
    finally:
        release.set()

    assert complete is False
    assert [target.session_key for target in targets] == [handle]
    assert targets[0].tailnet_node_id == "nOwner"
    assert not (tmp_path / "recipients.json").exists()


def test_alias_send_still_waits_on_silent_unrelated_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = "100.64.0.11"
    silent = "100.64.0.12"
    target_alias = "claude@studio:api:api-a1"
    release = threading.Event()

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        if payload.get("operation") == "peers":
            if address == silent:
                release.wait(timeout)
                raise ChatError("silent neighbor never answered")
            return {
                "schema_version": 1,
                "peers": [
                    {
                        "alias": target_alias,
                        "provider": "claude",
                        "device": "studio",
                        "project": "api",
                        "status": "available",
                        "generation": str(uuid4()),
                        "session_key": "a" * 64,
                    }
                ],
            }
        envelope = json.loads(str(payload["envelope"]))
        return {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target_alias,
            "provider": "claude",
        }

    monkeypatch.setattr(
        "cross_agent_chat.runtime.tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nSilent": silent, "nOwner": owner}),
    )
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    monkeypatch.setattr("cross_agent_chat.runtime.REMOTE_DISCOVERY_TIMEOUT_SECONDS", 4.0)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="imac",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    started = time.monotonic()
    try:
        with pytest.raises(ChatError, match="remote peer discovery is incomplete"):
            send(tmp_path, source, target_alias, "hello")
    finally:
        release.set()

    # Alias queries keep the old contract: a known alias match does not shorten
    # the wait, so the silent neighbor still holds the send for the deadline.
    assert time.monotonic() - started >= 3.5


def test_exact_token_send_is_refused_when_attested_generation_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned node re-attests handle+generation at dispatch; a stale
    generation refuses before an intent exists or a receive byte is sent."""
    owner = "100.64.0.11"
    stale = str(uuid4())
    live = str(uuid4())
    handle = session_key("claude", str(uuid4()))
    peer = {
        "alias": "claude@studio:api:api-a1",
        "provider": "claude",
        "device": "studio",
        "project": "api",
        "status": "available",
        "generation": live,
        "session_key": handle,
    }
    contacted: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        contacted.append(str(payload.get("operation")))
        if payload.get("operation") == "peers":
            return {"schema_version": 1, "peers": [peer]}
        pytest.fail("a stale selection must not reach the receive boundary")

    monkeypatch.setattr(
        "cross_agent_chat.runtime.tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nOwner": owner}),
    )
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="imac",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    with pytest.raises(ChatError, match="unavailable or changed"):
        send(tmp_path, source, remote_token("nOwner", handle, stale), "hello")

    assert contacted == ["peers"]
    assert IntentStore(tmp_path).intents() == []


def test_remote_claude_diagnostic_is_body_free_and_marks_one_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "100.64.0.11"
    target_alias = "claude@studio:api:api-a1"
    generation = str(uuid4())

    def request(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [
                    {
                        "alias": target_alias,
                        "provider": "claude",
                        "device": "studio",
                        "project": "api",
                        "status": "available",
                        "generation": generation,
                        "session_key": "a" * 64,
                    }
                ],
            }
        envelope = json.loads(str(payload["envelope"]))
        return {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "UNKNOWN_DELIVERY",
            "provider": "claude",
            "diagnostic": "claude_helper_timeout",
        }

    monkeypatch.setattr(
        "cross_agent_chat.runtime.tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nNodeA": address}),
    )
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="source",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    with pytest.raises(UnknownDeliveryError, match="claude_helper_timeout") as error:
        send(tmp_path, source, target_alias, "private message body")

    assert "private message body" not in str(error.value)
    assert IntentStore(tmp_path).intents()[0].status == "UNKNOWN_DELIVERY"


def test_remote_receiver_forwards_only_exact_claude_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    public_target = Target(
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
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=str(uuid4()),
        target_alias=target.alias,
        generation=target.generation,
        message="private message body",
    )
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda _: [public_target])
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda _address, payload, **_: (
            {key: value for key, value in payload.items() if key != "operation"}
            | {"status": "AUTHORIZED"}
        ),
    )
    valid_response = {
        "schema_version": 1,
        "event_id": event_id,
        "status": "UNKNOWN_DELIVERY",
        "provider": "claude",
        "diagnostic": "claude_helper_timeout",
    }
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket", lambda *_args, **_kwargs: valid_response
    )

    assert receive_remote(tmp_path, envelope, "100.64.0.11") == valid_response

    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda *_args, **_kwargs: {**valid_response, "extra": "reject"},
    )
    with pytest.raises(UnknownDeliveryError, match="remote delivery state is unknown"):
        receive_remote(tmp_path, envelope, "100.64.0.11")


def test_remote_pre_effect_rejection_is_safe_and_does_not_block_fresh_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "100.64.0.11"
    target_alias = "claude@studio:api:api-a1"
    generation = str(uuid4())

    def request(
        _address: str,
        payload: dict[str, object],
        **_: object,
    ) -> dict[str, object]:
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [
                    {
                        "alias": target_alias,
                        "provider": "claude",
                        "device": "studio",
                        "project": "api",
                        "status": "available",
                        "generation": generation,
                        "session_key": "a" * 64,
                    }
                ],
            }
        envelope = json.loads(str(payload["envelope"]))
        return {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "PRE_EFFECT_REJECTED",
            "provider": "claude",
            "error": "peer-controlled wording must not escape",
        }

    monkeypatch.setattr(
        "cross_agent_chat.runtime.tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nNodeA": address}),
    )
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="source",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    with pytest.raises(ChatError, match="remote target rejected") as caught:
        send(tmp_path, source, target_alias, "hello")

    assert "peer-controlled" not in str(caught.value)
    intent = IntentStore(tmp_path).intents()[0]
    assert intent.status == "PRE_EFFECT_REJECTED"
    assert IntentStore(tmp_path).begin_identity(
        source_key=session_key(source.provider, source.session_id),
        source_generation=source.generation,
        source_alias=source.alias,
        target_key="a" * 64,
        target_generation=generation,
        payload_digest="b" * 64,
    )


def test_remote_receiver_sanitizes_pre_effect_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    public_target = Target(
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
    source_generation = str(uuid4())
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message="hello",
    )
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda _: [public_target])

    def authorize(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        return {key: value for key, value in payload.items() if key != "operation"} | {
            "status": "AUTHORIZED"
        }

    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", authorize)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "event_id": event_id,
            "status": "PRE_EFFECT_REJECTED",
            "provider": "codex",
            "error": "internal path and peer-shaped text",
        },
    )

    response = receive_remote(tmp_path, envelope, "100.64.0.11")

    assert response == {
        "schema_version": 1,
        "event_id": event_id,
        "status": "PRE_EFFECT_REJECTED",
        "provider": "codex",
        "error": "remote destination rejected before provider effect",
    }


def test_wrapped_message_limit_rejects_before_intent_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "100.64.0.10"
    target_alias = "claude@studio:api:api-a1"
    generation = str(uuid4())

    monkeypatch.setattr(
        "cross_agent_chat.runtime.tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nNodeA": address}),
    )
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "peers": [
                {
                    "alias": target_alias,
                    "provider": "claude",
                    "device": "studio",
                    "project": "api",
                    "status": "available",
                    "generation": generation,
                    "session_key": "a" * 64,
                }
            ],
        },
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="source",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    with pytest.raises(ChatError, match="16 KiB"):
        send(tmp_path, source, target_alias, "x" * (16 * 1024))

    assert not (tmp_path / "intents.json").exists()


def test_cli_has_private_tailnet_broker_entrypoint(tmp_path: Path) -> None:
    arguments = parser().parse_args(["_broker", "--state-root", str(tmp_path)])

    assert arguments.command == "_broker"
    assert arguments.state_root == str(tmp_path)


def test_launchd_path_prefers_installed_standalone_tailscale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import tailnet

    standalone, gui = tmp_path / "standalone", tmp_path / "gui"
    for binary in (standalone, gui):
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
    monkeypatch.setattr("cross_agent_chat.tailnet.shutil.which", lambda _: None)
    monkeypatch.setattr(tailnet, "TAILSCALE_STANDALONE_BINARIES", (standalone,))
    monkeypatch.setattr(tailnet, "TAILSCALE_APP_BINARY", gui)
    assert tailnet.tailscale_binary() == standalone


def test_unavailable_status_uses_only_exact_previously_validated_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cross_agent_chat import tailnet

    monkeypatch.setattr(tailnet, "_status_output", lambda: None)
    monkeypatch.setenv("CROSS_AGENT_CHAT_TAILNET_ADDRESS", "100.64.0.13")
    monkeypatch.setattr(
        tailnet, "_ifconfig_output", lambda: "utun8: flags\n  inet 100.64.0.14 netmask 0xffffffff\n"
    )
    assert tailnet.local_tailnet_address() is None
    monkeypatch.setattr(
        tailnet, "_ifconfig_output", lambda: "utun8: flags\n  inet 100.64.0.13 netmask 0xffffffff\n"
    )
    assert tailnet.local_tailnet_address() == "100.64.0.13"
    monkeypatch.setattr(tailnet, "_status_output", lambda: json.dumps({"BackendState": "Stopped"}))
    assert tailnet.local_tailnet_address() is None


def test_broker_revokes_listener_when_verified_identity_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local, private = mock.Mock(), mock.Mock()
    listeners = iter((local, private))
    bindings = iter(([("127.0.0.1", 47072), ("100.64.0.13", 47071)], [("127.0.0.1", 47072)]))
    observed: list[list[object]] = []
    monkeypatch.setattr(
        tailnet_broker_module, "bind_broker_listener", lambda *args, **kwargs: next(listeners)
    )
    refresh_calls = 0
    refresh_ready = threading.Event()
    second_refresh_ready = threading.Event()
    clock = _FakeClock()

    def refresh_bindings() -> list[tuple[str, int]]:
        nonlocal refresh_calls
        refresh_calls += 1
        refresh_ready.set()
        if refresh_calls == 2:
            second_refresh_ready.set()
        return next(bindings)

    monkeypatch.setattr(tailnet_broker_module, "broker_bindings", refresh_bindings)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)

    def ready(
        servers: list[object], *_args: object
    ) -> tuple[list[object], list[object], list[object]]:
        observed.append(list(servers))
        if len(observed) == 1:
            assert refresh_ready.wait(timeout=1.0)
        elif len(observed) == 2:
            clock.now = tailnet_broker_module.TAILNET_BIND_RETRY_SECONDS
        elif len(observed) == 3:
            assert second_refresh_ready.wait(timeout=1.0)
        elif len(observed) == 4:
            private.close.assert_called_once_with()
            raise RuntimeError("stop fixture")
        return [], [], []

    monkeypatch.setattr(select, "select", ready)
    with pytest.raises(RuntimeError, match="stop fixture"):
        broker_server(str(tmp_path))
    assert any(servers == [local, private] for servers in observed)
    assert observed[-2:] == [[local], [local]]
    private.close.assert_called_once_with()
    local.close.assert_called_once_with()


@pytest.mark.parametrize(
    "stage", ["authorize_pre", "accept_pre", "authorize_unknown", "accept_unknown"]
)
def test_broker_receive_preserves_proven_rejection_and_post_write_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from cross_agent_chat.core import UnknownDeliveryError

    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    event_id = str(uuid4())
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:project:sender",
        source_generation=str(uuid4()),
        target_alias=target.alias,
        generation=target.generation,
        message="controlled body",
    )
    effects: list[str] = []

    def authorize(
        _address: str, payload: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        if stage == "authorize_pre":
            raise ChatError("authorization connection unavailable")
        if stage == "authorize_unknown":
            raise UnknownDeliveryError("authorization receipt missing")
        return {**{k: v for k, v in payload.items() if k != "operation"}, "status": "AUTHORIZED"}

    def courier(_path: Path, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        if payload["operation"] == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "alias": target.alias,
                "generation": target.generation,
            }
        if stage == "accept_pre":
            raise ChatError("destination disappeared before connect")
        effects.append("possible provider write")
        raise UnknownDeliveryError("destination receipt missing")

    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", authorize)
    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", courier)
    monkeypatch.setattr(
        tailnet_broker_module,
        "read_frame",
        lambda _connection, _limit=MAX_FRAME_BYTES, **_kwargs: json.dumps(
            {"schema_version": 1, "operation": "receive", "envelope": envelope}
        ).encode(),
    )
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        tailnet_broker_module,
        "emit_frame_safely",
        lambda _connection, response: emitted.append(response),
    )
    connection = mock.Mock()
    if stage.endswith("unknown"):
        with pytest.raises(UnknownDeliveryError):
            tailnet_broker_module.serve_broker_connection(tmp_path, connection, "100.64.0.11")
        assert emitted == []
    else:
        tailnet_broker_module.serve_broker_connection(tmp_path, connection, "100.64.0.11")
        assert emitted == [
            {
                "schema_version": 1,
                "event_id": event_id,
                "status": "PRE_EFFECT_REJECTED",
                "provider": "codex",
                "error": "remote destination rejected before provider effect",
            }
        ]
        assert effects == []


def test_a_real_authorization_refusal_reaches_the_receiver_as_a_decided_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sender's own refusal must not come back to it as UNKNOWN_DELIVERY.

    Uses the frame `authorize_remote` actually produces rather than a hand-built
    one, so the two halves are bound: refusing by raising closed the connection
    with no frame, the receiver read EOF as uncertain, and the sender recorded a
    refusal it had itself decided as an unknown outcome -- freezing an event that
    provably produced no effect.
    """
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="source",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    event_id = str(uuid4())
    target_key = session_key(target.provider, target.session_id)
    IntentStore(tmp_path).begin_identity(
        source_key=session_key(source.provider, source.session_id),
        source_generation=source.generation,
        source_alias=source.alias,
        target_key=target_key,
        target_generation=target.generation,
        payload_digest="a" * 64,
        event_id=event_id,
    )
    claim = {
        "event_id": event_id,
        "source_alias": source.alias,
        "source_generation": source.generation,
        "target_key": target_key,
        "target_generation": target.generation,
        "payload_digest": "a" * 64,
    }
    assert authorize_remote(tmp_path, **claim)["status"] == "AUTHORIZED"
    refusal = authorize_remote(tmp_path, **claim)
    assert refusal["status"] == "REFUSED"

    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source.alias,
        source_generation=source.generation,
        target_alias=target.alias,
        generation=target.generation,
        message="hello",
    )
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", lambda *_a, **_k: refusal)

    def provider_boundary(
        _path: Path, payload: dict[str, object], **_: object
    ) -> dict[str, object]:
        if payload.get("operation") == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "generation": target.generation,
                "alias": target.alias,
            }
        pytest.fail("provider delivery ran after a decided authorization refusal")

    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", provider_boundary)

    response = receive_remote(tmp_path, envelope, "100.64.0.11")

    assert response["status"] == "PRE_EFFECT_REJECTED"


def test_wrapped_message_names_the_exact_budget_at_its_real_boundary() -> None:
    """Pin the true maximum and the first rejection, not the nominal 16 KiB.

    The cap applies to the WRAPPED body, so the largest sendable message is
    smaller than the limit by however much the envelope costs. Nothing pinned
    that number, so envelope edits moved it silently.
    """
    alias = "claude@studio:api:api-a1"
    handle = "b7" * 32
    event_id = str(uuid4())
    overhead = len(wrapped_message(alias, handle, "", event_id, "claude").encode())
    largest = MAX_MESSAGE_BYTES - overhead

    accepted = wrapped_message(alias, handle, "x" * largest, event_id, "claude")
    assert len(accepted.encode()) == MAX_MESSAGE_BYTES

    with pytest.raises(ChatError) as caught:
        wrapped_message(alias, handle, "x" * (largest + 1), event_id, "claude")

    reason = str(caught.value)
    # The sender can see their message is under 16 KiB, so the raw limit alone
    # is not an actionable error: it must name the overhead and the real budget.
    assert str(overhead) in reason
    assert str(largest) in reason
    assert reason != "message exceeds the 16 KiB limit"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param("has a \x00 byte", "16 KiB limit", id="nul-byte"),
        pytest.param("\x01" * 6000, "encoded frame budget", id="frame-budget"),
    ],
)
def test_wrapped_message_does_not_restate_other_failures_as_a_size_problem(
    message: str, expected: str
) -> None:
    """Only the size cap may be restated, and only when the body crossed it.

    `bounded_message` also rejects emptiness, NUL bytes, unencodable strings and
    the encoded-frame budget. Rewriting all of those as "your message is N bytes
    and the envelope adds M" states a byte budget as the cause of a failure that
    has nothing to do with size.
    """
    with pytest.raises(ChatError) as caught:
        wrapped_message(
            "claude@studio:api:api-a1",
            "b7" * 32,
            message,
            str(uuid4()),
            "claude",
        )

    reason = str(caught.value)
    assert expected in reason
    assert "envelope adds" not in reason
    assert "send at most" not in reason


def test_wrapped_message_keeps_the_frame_budget_reason_when_the_envelope_causes_it() -> None:
    """The one window where WRAPPING causes a non-size failure.

    A control-character body can pass `bounded_message` on its own and still push
    the wrapped body past the encoded-frame budget once the envelope is added.
    That is the only reachable trigger for the re-raise branch where the envelope
    is genuinely at fault, and it must not be restated as a size problem: the
    body is well inside 16 KiB, so a byte budget would describe the wrong cause.
    """
    message = "\x01" * 5392
    # Precondition: the message alone is acceptable. Only wrapping breaks it.
    assert bounded_message(message) == message

    with pytest.raises(ChatError) as caught:
        wrapped_message(
            "claude@studio:api:api-a1",
            "b7" * 32,
            message,
            str(uuid4()),
            "claude",
        )

    reason = str(caught.value)
    assert "encoded frame budget" in reason
    assert "envelope adds" not in reason
    assert "16 KiB" not in reason


def _loaded_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    route_count: int = 30,
    slow_probe_seconds: float = 1.0,
    requester_budget_seconds: float = 0.4,
) -> tuple[Path, Route, Route, str, str, list[dict[str, object]]]:
    """A broker with many routes whose non-target couriers answer slowly.

    The requester keeps a shrunken per-node budget the way the incident's real
    budget expired: the broker's full-roster answer outlives the requester's
    socket timeout, so the answer is computed but lost. A requester-side
    timeout or a broker-side invalid-request close both surface as
    UnknownDeliveryError, matching request_tailnet's wire semantics.
    """
    broker_root = tmp_path / "broker"
    requester_root = tmp_path / "requester"
    routes = [
        Route.create(
            provider="codex",
            session_id=str(uuid4()),
            device="m1",
            cwd=str(tmp_path),
            pid=os.getpid(),
        )
        for _ in range(route_count)
    ]
    registry = Registry(broker_root)
    for route in routes:
        registry.upsert(route)
    target = routes[0]
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="imac",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(requester_root).upsert(source)
    by_generation = {route.generation: route for route in [*routes, source]}

    def couriers(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        route = by_generation[str(payload["generation"])]
        if route is not target and route is not source:
            time.sleep(slow_probe_seconds)
        return {
            "schema_version": 1,
            "status": "READY",
            "generation": route.generation,
            "alias": route.alias,
        }

    calls: list[dict[str, object]] = []
    broker_workers = ThreadPoolExecutor(max_workers=4)

    def wire(
        address: str,
        payload: dict[str, object],
        *,
        port: int = 47071,
        timeout: float = 2.0,
    ) -> dict[str, object]:
        del port
        calls.append(payload)
        if payload.get("operation") == "receive":
            envelope = json.loads(str(payload["envelope"]))
            return {
                "schema_version": 1,
                "event_id": envelope["event_id"],
                "status": "TRANSPORT_ACCEPTED",
                "to": envelope["target_alias"],
                "provider": "codex",
            }
        future = broker_workers.submit(handle_broker_request, broker_root, payload, address)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            raise UnknownDeliveryError("Tailnet delivery state is unknown") from None
        except ChatError as error:
            raise UnknownDeliveryError("Tailnet delivery state is unknown") from error

    monkeypatch.setattr(runtime, "request_socket", couriers)
    monkeypatch.setattr(runtime, "request_tailnet", wire)
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nBroker": "100.64.0.11"}),
    )
    monkeypatch.setattr(runtime, "REMOTE_DISCOVERY_TIMEOUT_SECONDS", requester_budget_seconds)
    handle = session_key(target.provider, target.session_id)
    token = remote_token("nBroker", handle, target.generation)
    return requester_root, source, target, handle, token, calls


def test_send_to_an_exact_handle_does_not_wait_for_a_full_loaded_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact-handle send must not need a loaded node's complete roster.

    Reproduces the 19 Sep incident: ~30 routes with slow couriers made the
    broker's full-roster answer outlive the requester's per-node budget, so an
    exact-handle send failed pre-effect with "remote peer discovery is
    incomplete" while the target's own courier was healthy. The handle-bound
    variant lets the broker answer after validating only the owning route.
    """
    root, source, _target, handle, token, calls = _loaded_broker(tmp_path, monkeypatch)

    result = send(root, source, token, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    peers_calls = [payload for payload in calls if payload.get("operation") == "peers"]
    assert peers_calls == [
        {
            "schema_version": 1,
            "operation": "peers",
            "handle": handle,
            "include_devin": True,
        }
    ]
    # Counterexample on the same fixture: the un-bound roster query still
    # outlives the requester budget and reports incomplete.
    discovered, complete = runtime._remote_discovery()
    assert discovered == []
    assert complete is False


def test_exact_token_send_falls_back_to_a_full_roster_on_an_old_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broker that rejects the unknown variant costs one extra round-trip."""
    handle = session_key("codex", str(uuid4()))
    generation = str(uuid4())
    peer = {
        "alias": "codex@remote:api:123456789abc",
        "provider": "codex",
        "device": "remote",
        "project": "api",
        "status": "available",
        "generation": generation,
        "session_key": handle,
    }
    calls: list[dict[str, object]] = []

    def old_broker(
        _address: str,
        payload: dict[str, object],
        *,
        port: int = 47071,
        timeout: float = 2.0,
    ) -> dict[str, object]:
        del port, timeout
        calls.append(payload)
        if payload.get("operation") == "receive":
            envelope = json.loads(str(payload["envelope"]))
            return {
                "schema_version": 1,
                "event_id": envelope["event_id"],
                "status": "TRANSPORT_ACCEPTED",
                "to": envelope["target_alias"],
                "provider": "codex",
            }
        if "handle" in payload:
            raise ChatError("legacy broker rejected unknown field")
        return {"schema_version": 1, "peers": [peer]}

    monkeypatch.setattr(runtime, "request_tailnet", old_broker)
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nOld": "100.64.0.2"}),
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="imac",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    result = send(tmp_path, source, remote_token("nOld", handle, generation), "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert calls[:2] == [
        {
            "schema_version": 1,
            "operation": "peers",
            "handle": handle,
            "include_devin": True,
        },
        {"schema_version": 1, "operation": "peers", "include_devin": True},
    ]
    assert calls[2].get("operation") == "receive"


def test_handle_bound_roster_query_is_empty_and_complete_for_an_unowned_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="m1",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(route)

    def forbidden(_path: Path, _payload: dict[str, object], **_: object) -> dict[str, object]:
        pytest.fail("an unowned handle must not trigger any courier probe")

    monkeypatch.setattr(runtime, "request_socket", forbidden)

    answer = handle_broker_request(
        tmp_path,
        {"schema_version": 1, "operation": "peers", "handle": "f" * 64},
        "100.64.0.10",
    )

    assert answer == {"schema_version": 1, "peers": []}

    monkeypatch.setattr(runtime, "request_tailnet", lambda *_a, **_k: answer)
    targets, complete = runtime._remote_node_targets("100.64.0.10", handle="f" * 64)
    assert targets == []
    assert complete is True


def test_handle_bound_roster_query_still_drops_a_route_that_fails_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound answer runs the same courier validation as the full roster."""
    route = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="m1",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(route)
    handle = session_key(route.provider, route.session_id)
    probes: list[dict[str, object]] = []

    def stale_generation(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        probes.append(payload)
        return {
            "schema_version": 1,
            "status": "READY",
            "generation": str(uuid4()),
            "alias": route.alias,
        }

    monkeypatch.setattr(runtime, "request_socket", stale_generation)

    answer = handle_broker_request(
        tmp_path,
        {"schema_version": 1, "operation": "peers", "handle": handle},
        "100.64.0.10",
    )

    assert answer == {"schema_version": 1, "peers": []}
    assert len(probes) == 1


def test_handle_bound_roster_variants_carry_the_same_include_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[tuple[bool, bool, bool, str | None]] = []

    def observed_peers(
        _root: Path,
        *,
        include_remote: bool,
        internal: bool,
        include_delivery_mode: bool,
        include_title: bool,
        include_devin: bool,
        handle: str | None,
    ) -> dict[str, object]:
        assert include_remote is False
        assert internal is True
        observed.append((include_delivery_mode, include_title, include_devin, handle))
        return {"schema_version": 1, "peers": []}

    monkeypatch.setattr(tailnet_broker_module, "peers", observed_peers)
    handle = "e" * 64
    for extra in (
        {"handle": handle},
        {"handle": handle, "include_devin": True},
        {"handle": handle, "include_delivery_mode": True},
        {"handle": handle, "include_delivery_mode": True, "include_devin": True},
        {"handle": handle, "include_delivery_mode": True, "include_title": True},
        {
            "handle": handle,
            "include_delivery_mode": True,
            "include_title": True,
            "include_devin": True,
        },
    ):
        request = {"schema_version": 1, "operation": "peers", **extra}
        assert handle_broker_request(tmp_path, request, "100.64.0.10") == {
            "schema_version": 1,
            "peers": [],
        }

    assert observed == [
        (False, False, False, handle),
        (False, False, True, handle),
        (True, False, False, handle),
        (True, False, True, handle),
        (True, True, False, handle),
        (True, True, True, handle),
    ]


def test_handle_bound_variant_is_rejected_with_any_extra_or_invalid_field(
    tmp_path: Path,
) -> None:
    """Strictness matches the other variants: exact key set, exact values."""
    handle = "f" * 64
    for request in (
        {"schema_version": 1, "operation": "peers", "handle": handle, "extra": True},
        {"schema_version": 1, "operation": "peers", "handle": "not-a-handle"},
        {"schema_version": 1, "operation": "peers", "handle": "F" * 64},
        {"schema_version": 1, "operation": "peers", "handle": handle[:-1]},
        {"schema_version": 1, "operation": "peers", "handle": 5},
        {"schema_version": 1, "operation": "peers", "handle": handle, "include_devin": False},
        {
            "schema_version": 1,
            "operation": "peers",
            "handle": handle,
            "include_title": True,
        },
        {"schema_version": 2, "operation": "peers", "handle": handle},
    ):
        with pytest.raises(ChatError, match="broker request"):
            handle_broker_request(tmp_path, request, "100.64.0.10")


def test_exact_token_send_refuses_when_the_handle_moved_to_another_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handle reappearing on a different stable node does not retarget:
    the pinned node stopped claiming it, so the send refuses pre-effect."""
    handle = "a" * 64
    generation = str(uuid4())
    peer = {
        "alias": "codex@remote:api:123456789abc",
        "provider": "codex",
        "device": "remote",
        "project": "api",
        "status": "available",
        "generation": generation,
        "session_key": handle,
    }
    contacted: list[str] = []

    def broker(address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        contacted.append(address)
        if payload.get("operation") == "peers":
            # The old node no longer claims the handle; the new one is never asked.
            return {"schema_version": 1, "peers": [] if address == "100.64.0.11" else [peer]}
        pytest.fail("a refused send must not reach the receive boundary")

    monkeypatch.setattr(runtime, "request_tailnet", broker)
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id="nSelf",
            peers={"nOwner": "100.64.0.11", "nNew": "100.64.0.12"},
        ),
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="imac",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    with pytest.raises(ChatError, match="unavailable or changed"):
        send(tmp_path, source, remote_token("nOwner", handle, generation), "hello")

    assert contacted == ["100.64.0.11"]
