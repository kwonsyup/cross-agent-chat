from __future__ import annotations

import errno
import json
import os
import select
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pytest

import cross_agent_chat.tailnet_broker as tailnet_broker_module
from cross_agent_chat import __version__, runtime
from cross_agent_chat.cli import parser
from cross_agent_chat.core import ChatError, IntentStore, Registry, Route, session_key
from cross_agent_chat.runtime import (
    REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    Target,
    authorize_remote,
    receive_remote,
    remote_targets,
    request_tailnet,
    send,
)
from cross_agent_chat.tailnet import (
    local_tailnet_address,
    parse_ifconfig_tailnet_address,
    parse_known_tailnet_address,
    parse_local_tailnet_address,
    parse_tailnet_nodes,
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
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.11", "fd7a:115c:a1e0::1"],
                },
                "node-b": {
                    "Online": False,
                    "TailscaleIPs": ["100.64.0.12"],
                },
                "node-c": {
                    "Online": True,
                    "TailscaleIPs": ["192.0.2.10", "fd7a:115c:a1e0::2"],
                },
            }
        }
    )

    assert parse_tailnet_nodes(payload) == ["100.64.0.11"]


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

    def parse(address: str, raw: object, **kwargs: bool) -> list[Target]:
        nonlocal calls
        calls += 1
        targets = original(address, raw, **kwargs)
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
        parse_tailnet_nodes('{"Peer": []}')


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
    assert select_timeouts == [5.0, 5.0, 5.0, 5.0]
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
        _workers: object, _root: Path, readable: list[object], _admission: BrokerAdmission
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

    with pytest.raises(ChatError, match="not authorized"):
        authorize_remote(
            tmp_path,
            event_id=event_id,
            source_alias=source.alias,
            source_generation=source.generation,
            target_key=target_key,
            target_generation=target_generation,
            payload_digest="a" * 64,
        )

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
    def nodes() -> list[str]:
        return ["100.64.0.11"]

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

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_nodes", nodes)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        request,
    )

    targets = remote_targets(tmp_path)

    assert len(targets) == 1
    assert targets[0].alias == "claude@studio:api:api-a1"
    assert targets[0].tailnet_address == "100.64.0.11"


def test_remote_send_uses_tailnet_broker_without_ssh_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "100.64.0.11"
    target_alias = "claude@studio:api:api-a1"

    def nodes() -> list[str]:
        return [address]

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

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_nodes", nodes)
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

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_nodes", lambda: [address])
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

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_nodes", lambda: [address])
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
        lambda _: json.dumps(
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
