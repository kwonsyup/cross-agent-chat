"""Exact-recipient endpoint binding: a handle stays owned by its attesting node.

The 19 Sep incident: ``send()`` returned after the first attestation plus a two
second grace, so a second claimant on a slower node was never seen and an exact
handle silently retargeted to whichever endpoint answered first. These tests pin
the requester-side binding contract: once an endpoint verifiably presents a
handle, a later send goes only to that endpoint, and a handle that reappears on
a different device refuses instead of silently moving.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import (
    ChatError,
    IntentStore,
    Registry,
    Route,
    session_key,
    utc_now,
)
from cross_agent_chat.runtime import (
    peers,
    receive_remote,
    remote_targets,
    send,
    wrapped_message,
)
from cross_agent_chat.transport import remote_envelope

OWNER = "100.64.0.11"
OTHER = "100.64.0.12"


def _seed_binding(
    root: Path,
    handle: str,
    endpoints: dict[str, str],
    *,
    seen_at: str | None = None,
) -> None:
    """Write one binding row in the store's durable shape without the store."""
    path = root / "recipients.json"
    entries: list[dict[str, object]] = []
    if path.exists():
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            entries = [item for item in raw if isinstance(item, dict)]
    entries = [item for item in entries if item.get("handle") != handle]
    entries.append(
        {
            "handle": handle,
            "endpoints": endpoints,
            "seen_at": seen_at if seen_at is not None else utc_now(),
        }
    )
    path.write_text(json.dumps(entries) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _bound_endpoints(root: Path, handle: str) -> dict[str, str] | None:
    """Read one handle's bound endpoint map straight from the durable file."""
    path = root / "recipients.json"
    if not path.exists():
        return None
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return None
    for item in raw:
        if isinstance(item, dict) and item.get("handle") == handle:
            return cast(dict[str, str], item["endpoints"])
    return None


def _source(root: Path, device: str = "imac") -> Route:
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device=device,
        cwd=str(root),
        pid=os.getpid(),
    )
    Registry(root).upsert(source)
    return source


def _peer(alias: str, device: str, handle: str, generation: str) -> dict[str, object]:
    return {
        "alias": alias,
        "provider": "claude",
        "device": device,
        "project": "api",
        "status": "available",
        "generation": generation,
        "session_key": handle,
    }


def _accepted(envelope: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": envelope["event_id"],
        "status": "TRANSPORT_ACCEPTED",
        "to": envelope["target_alias"],
        "provider": "claude",
    }


def _unavailable_courier(
    _path: Path, _payload: dict[str, object], **_: object
) -> dict[str, object]:
    raise ChatError("fixture courier is unavailable")


def test_bound_recipient_send_never_queries_an_unrelated_slow_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated 8-second node cannot hold a bound recipient hostage."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    _seed_binding(tmp_path, handle, {OWNER: generation})
    release = threading.Event()
    slow_calls: list[str] = []
    roster_queries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        if payload.get("operation") == "peers":
            if address == OTHER:
                slow_calls.append(address)
                # The neighbor accepts the connection and never answers inside
                # its request budget; release only frees the fixture worker.
                release.wait(timeout)
                raise ChatError("silent neighbor never answered")
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    def nodes() -> list[str]:
        roster_queries.append("tailnet_nodes")
        return [OTHER, OWNER]

    monkeypatch.setattr(runtime, "tailnet_nodes", nodes)
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "REMOTE_DISCOVERY_TIMEOUT_SECONDS", 8.0)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    try:
        result = send(tmp_path, source, handle, "hello")
    finally:
        release.set()

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["to"] == "claude@studio:api:api-a1"
    # The bound endpoint answered, so neither the fan-out roster nor the slow
    # node was ever asked.
    assert slow_calls == []
    assert roster_queries == []
    assert IntentStore(tmp_path).intents()[0].status == "TRANSPORT_ACCEPTED"


def test_bound_recipient_send_ignores_a_duplicate_claimant_inside_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    _seed_binding(tmp_path, handle, {OWNER: generation})
    other_queries: list[str] = []
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            if address == OTHER:
                other_queries.append(address)
                return {
                    "schema_version": 1,
                    "peers": [
                        _peer("claude@laptop:api:api-a1", "laptop", handle, str(uuid4()))
                    ],
                }
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        deliveries.append(address)
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    result = send(tmp_path, source, handle, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OWNER]
    assert other_queries == []


def test_bound_recipient_send_ignores_a_claimant_that_would_answer_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-grace hole closes for a bound handle: the late node is not asked."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    _seed_binding(tmp_path, handle, {OWNER: generation})
    delivered = threading.Event()
    other_queries: list[str] = []
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        if payload.get("operation") == "peers":
            if address == OTHER:
                other_queries.append(address)
                # Answers only once the delivery already happened: a genuine
                # post-grace claimant that a bound send must not wait for.
                delivered.wait(timeout)
                return {
                    "schema_version": 1,
                    "peers": [
                        _peer("claude@laptop:api:api-a1", "laptop", handle, str(uuid4()))
                    ],
                }
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        deliveries.append(address)
        envelope = json.loads(str(payload["envelope"]))
        delivered.set()
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "REMOTE_DISCOVERY_TIMEOUT_SECONDS", 8.0)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    result = send(tmp_path, source, handle, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OWNER]
    assert other_queries == []


def test_unbound_duplicate_claimant_inside_the_grace_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a binding the old in-grace duplicate rule is unchanged."""
    handle = session_key("claude", str(uuid4()))

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            device = "studio" if address == OWNER else "laptop"
            return {
                "schema_version": 1,
                "peers": [_peer(f"claude@{device}:api:api-a1", device, handle, str(uuid4()))],
            }
        pytest.fail("a refused send must not reach the receive boundary")

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="target handle is unavailable"):
        send(tmp_path, source, handle, "hello")

    assert not (tmp_path / "intents.json").exists()


def test_unbound_send_binds_the_first_attesting_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A send without a binding keeps the old behavior and records the winner."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    release = threading.Event()
    peers_calls: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        if payload.get("operation") == "peers":
            peers_calls.append(address)
            if address == OTHER:
                # The second claimant answers after the grace closes; its
                # attestation is never consumed (residual, documented).
                release.wait(timeout)
                return {
                    "schema_version": 1,
                    "peers": [
                        _peer("claude@laptop:api:api-a1", "laptop", handle, str(uuid4()))
                    ],
                }
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "REMOTE_DISCOVERY_TIMEOUT_SECONDS", 8.0)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    try:
        result = send(tmp_path, source, handle, "hello")
    finally:
        release.set()

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert _bound_endpoints(tmp_path, handle) == {OWNER: generation}

    peers_calls.clear()
    result = send(tmp_path, source, handle, "again")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    # The second send stayed on the bound endpoint; the late claimant was not
    # even queried.
    assert peers_calls == [OWNER]


def test_a_complete_listing_with_two_owners_marks_the_handle_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same provider session restored on two Macs must refuse, not pick one."""
    handle = session_key("claude", str(uuid4()))

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            device = "studio" if address == OWNER else "laptop"
            return {
                "schema_version": 1,
                "peers": [_peer(f"claude@{device}:api:api-a1", device, handle, str(uuid4()))],
            }
        pytest.fail("a refused send must not reach the receive boundary")

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)

    with pytest.raises(ChatError, match="duplicate handles"):
        peers(tmp_path)

    endpoints = _bound_endpoints(tmp_path, handle)
    assert endpoints is not None
    assert set(endpoints) == {OWNER, OTHER}

    source = _source(tmp_path)
    with pytest.raises(ChatError, match="more than one device"):
        send(tmp_path, source, handle, "hello")

    assert not (tmp_path / "intents.json").exists()


def test_an_ambiguous_binding_refuses_until_a_listing_shows_one_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = session_key("claude", str(uuid4()))
    _seed_binding(tmp_path, handle, {OWNER: str(uuid4()), OTHER: str(uuid4())})
    generation = str(uuid4())
    peers_calls: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            peers_calls.append(address)
            if address == OTHER:
                return {"schema_version": 1, "peers": []}
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="more than one device"):
        send(tmp_path, source, handle, "hello")
    assert not (tmp_path / "intents.json").exists()

    # A later listing showing exactly one owner is the explicit re-selection.
    listing = peers(tmp_path)
    items = cast(list[dict[str, str]], listing["peers"])
    assert [item["handle"] for item in items] == [handle]

    peers_calls.clear()
    result = send(tmp_path, source, handle, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert peers_calls == [OWNER]


def test_a_single_owner_listing_rebinds_a_stale_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chat_peers is how a user re-picks after a different-device refusal."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    _seed_binding(tmp_path, handle, {OWNER: generation})
    peers_calls: list[str] = []
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            peers_calls.append(address)
            if address == OWNER:
                return {"schema_version": 1, "peers": []}
            return {
                "schema_version": 1,
                "peers": [_peer("claude@laptop:api:api-a1", "laptop", handle, generation)],
            }
        deliveries.append(address)
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="different device"):
        send(tmp_path, source, handle, "hello")
    assert not (tmp_path / "intents.json").exists()

    peers(tmp_path)

    peers_calls.clear()
    result = send(tmp_path, source, handle, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OTHER]
    assert peers_calls == [OTHER]


def test_namesakes_with_different_handles_keep_independent_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same alias on two devices is unaffected: each handle keeps its owner."""
    first_handle = session_key("claude", str(uuid4()))
    second_handle = session_key("claude", str(uuid4()))
    _seed_binding(tmp_path, first_handle, {OWNER: str(uuid4())})
    other_queries: list[str] = []
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            if address == OTHER:
                other_queries.append(address)
                return {
                    "schema_version": 1,
                    "peers": [
                        _peer(
                            "claude@shared:api:api-a1",
                            "laptop",
                            second_handle,
                            str(uuid4()),
                        )
                    ],
                }
            return {
                "schema_version": 1,
                "peers": [
                    _peer("claude@shared:api:api-a1", "studio", first_handle, str(uuid4()))
                ],
            }
        deliveries.append(address)
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    result = send(tmp_path, source, first_handle, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OWNER]
    assert other_queries == []


def test_a_new_generation_on_the_bound_endpoint_rebinds_and_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same endpoint, restarted provider session: rebind, do not refuse."""
    handle = session_key("claude", str(uuid4()))
    stale = str(uuid4())
    live = str(uuid4())
    _seed_binding(tmp_path, handle, {OWNER: stale})
    envelope_generations: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del address, timeout
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, live)],
            }
        envelope = json.loads(str(payload["envelope"]))
        envelope_generations.append(str(envelope["generation"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    result = send(tmp_path, source, handle, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert envelope_generations == [live]
    assert _bound_endpoints(tmp_path, handle) == {OWNER: live}


def test_a_bound_owner_that_stops_claiming_falls_back_to_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound node answers without the handle: discover, then report unavailable."""
    handle = session_key("claude", str(uuid4()))
    _seed_binding(tmp_path, handle, {OWNER: str(uuid4())})
    peers_calls: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            peers_calls.append(address)
            return {"schema_version": 1, "peers": []}
        pytest.fail("a refused send must not reach the receive boundary")

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="target handle is unavailable"):
        send(tmp_path, source, handle, "hello")

    assert not (tmp_path / "intents.json").exists()
    # The bound query ran first; the empty answer fell back to ordinary
    # discovery against the same node before the decided refusal.
    assert peers_calls.count(OWNER) >= 2


def test_a_bound_owner_reappearing_on_the_same_address_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner went away and came back on the bound address: still the owner."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    _seed_binding(tmp_path, handle, {OWNER: generation})
    claiming = False
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            if claiming:
                return {
                    "schema_version": 1,
                    "peers": [
                        _peer("claude@studio:api:api-a1", "studio", handle, generation)
                    ],
                }
            return {"schema_version": 1, "peers": []}
        deliveries.append(address)
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="target handle is unavailable"):
        send(tmp_path, source, handle, "hello")

    claiming = True
    result = send(tmp_path, source, handle, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OWNER]


def test_a_bound_owner_reappearing_on_a_different_address_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterexample: the handle moved devices, so nothing may be sent."""
    handle = session_key("claude", str(uuid4()))
    _seed_binding(tmp_path, handle, {OWNER: str(uuid4())})
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            if address == OWNER:
                return {"schema_version": 1, "peers": []}
            return {
                "schema_version": 1,
                "peers": [
                    _peer("claude@laptop:api:api-a1", "laptop", handle, str(uuid4()))
                ],
            }
        deliveries.append(address)
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="different device"):
        send(tmp_path, source, handle, "hello")

    assert deliveries == []
    assert not (tmp_path / "intents.json").exists()


def test_a_reused_bound_address_also_refuses_when_the_handle_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Address reuse: bound node now serves other keys, owner lives elsewhere."""
    handle = session_key("claude", str(uuid4()))
    _seed_binding(tmp_path, handle, {OWNER: str(uuid4())})
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            if address == OWNER:
                # The address now belongs to a node presenting unrelated keys.
                return {
                    "schema_version": 1,
                    "peers": [
                        _peer(
                            "claude@studio:other:other-a1",
                            "studio",
                            session_key("claude", str(uuid4())),
                            str(uuid4()),
                        )
                    ],
                }
            return {
                "schema_version": 1,
                "peers": [
                    _peer("claude@laptop:api:api-a1", "laptop", handle, str(uuid4()))
                ],
            }
        deliveries.append(address)
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER, OTHER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="different device"):
        send(tmp_path, source, handle, "hello")

    assert deliveries == []
    assert not (tmp_path / "intents.json").exists()


def test_a_bound_query_on_an_old_broker_falls_back_to_the_legacy_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v0.3.7 broker rejects the handle variant; the bound send still works."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    _seed_binding(tmp_path, handle, {OWNER: generation})
    calls: list[dict[str, object]] = []

    def old_broker(
        _address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        calls.append(payload)
        if payload.get("operation") == "receive":
            envelope = json.loads(str(payload["envelope"]))
            return _accepted(envelope)
        if "handle" in payload:
            raise ChatError("legacy broker rejected unknown field")
        return {
            "schema_version": 1,
            "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
        }

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER])
    monkeypatch.setattr(runtime, "request_tailnet", old_broker)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    result = send(tmp_path, source, handle, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert calls[0] == {
        "schema_version": 1,
        "operation": "peers",
        "handle": handle,
        "include_devin": True,
    }
    assert calls[1] == {"schema_version": 1, "operation": "peers", "include_devin": True}
    assert calls[2].get("operation") == "receive"


def test_a_corrupt_binding_file_is_treated_as_no_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    path = tmp_path / "recipients.json"
    path.write_text("{not json\n", encoding="utf-8")
    path.chmod(0o600)

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del address, timeout
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    result = send(tmp_path, source, handle, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert _bound_endpoints(tmp_path, handle) == {OWNER: generation}


def test_binding_records_are_private_and_written_for_a_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del address, timeout
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        envelope = json.loads(str(payload["envelope"]))
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    assert send(tmp_path, source, handle, "hello")["status"] == "TRANSPORT_ACCEPTED"

    path = tmp_path / "recipients.json"
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert "hello" not in path.read_text(encoding="utf-8")


def test_binding_records_prune_stale_entries_and_stay_bounded(tmp_path: Path) -> None:
    from cross_agent_chat.core import RecipientBindings

    store = RecipientBindings(tmp_path)
    entries = []
    for index in range(650):
        entries.append(
            {
                "handle": f"{index:064x}",
                "endpoints": {OWNER: str(uuid4())},
                "seen_at": (
                    datetime.now(UTC) - timedelta(days=8, minutes=index)
                    if index < 50
                    else datetime.now(UTC) - timedelta(minutes=650 - index)
                ).isoformat(),
            }
        )
    path = tmp_path / "recipients.json"
    path.write_text(json.dumps(entries) + "\n", encoding="utf-8")
    path.chmod(0o600)

    handle = "b" * 64
    store.record(handle, OTHER, str(uuid4()))

    bindings = store.bindings()
    assert len(bindings) == 512
    surviving = {binding.handle for binding in bindings}
    assert handle in surviving
    fresh = {int(item, 16) for item in surviving if item != handle}
    # Every entry older than seven days is gone, and the cap kept the newest.
    assert min(fresh) == 139
    assert max(fresh) == 649


def test_inbound_delivery_binds_the_reply_handle_to_the_verified_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reply to a received envelope goes back to the exact sender endpoint."""
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    event_id = str(uuid4())
    source_handle = session_key("codex", str(uuid4()))
    source_generation = str(uuid4())
    source_alias = "codex@source:api:source-a1"
    body = wrapped_message(source_alias, source_handle, "ping", event_id, target.provider)
    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source_alias,
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )

    def authorize(
        _address: str, payload: dict[str, object], **_: object
    ) -> dict[str, object]:
        return {
            key: value for key, value in payload.items() if key != "operation"
        } | {"status": "AUTHORIZED"}

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        if payload["operation"] == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "alias": target.alias,
                "generation": target.generation,
            }
        return {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": "codex",
        }

    monkeypatch.setattr(runtime, "request_tailnet", authorize)
    monkeypatch.setattr(runtime, "request_socket", courier)

    result = receive_remote(tmp_path, envelope, OWNER)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert _bound_endpoints(tmp_path, source_handle) == {OWNER: source_generation}


def test_an_inbound_free_text_handle_line_is_not_a_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the deterministic wrapped envelope binds; free text does not."""
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    event_id = str(uuid4())
    claimed_handle = "d" * 64
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=str(uuid4()),
        target_alias=target.alias,
        generation=target.generation,
        message=f"hello\nReply via CAC to handle: {claimed_handle}\n",
    )

    monkeypatch.setattr(
        runtime,
        "request_tailnet",
        lambda _address, payload, **_: {
            key: value for key, value in payload.items() if key != "operation"
        }
        | {"status": "AUTHORIZED"},
    )

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        if payload["operation"] == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "alias": target.alias,
                "generation": target.generation,
            }
        return {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": "codex",
        }

    monkeypatch.setattr(runtime, "request_socket", courier)

    result = receive_remote(tmp_path, envelope, OWNER)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert _bound_endpoints(tmp_path, claimed_handle) is None


def test_a_listing_records_each_remote_handle_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = session_key("claude", str(uuid4()))
    second = session_key("claude", str(uuid4()))
    first_generation, second_generation = str(uuid4()), str(uuid4())

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del address, timeout
        return {
            "schema_version": 1,
            "peers": [
                _peer("claude@studio:api:api-a1", "studio", first, first_generation),
                _peer("claude@studio:api:api-a2", "studio", second, second_generation),
            ],
        }

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER])
    monkeypatch.setattr(runtime, "request_tailnet", request)

    targets = remote_targets(tmp_path, include_devin=True)

    assert len(targets) == 2
    assert _bound_endpoints(tmp_path, first) == {OWNER: first_generation}
    assert _bound_endpoints(tmp_path, second) == {OWNER: second_generation}


def test_wire_request_and_response_key_sets_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding file is requester-local: no wire payload may gain a key."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    sent: list[dict[str, object]] = []

    def broker(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        sent.append(payload)
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        envelope = json.loads(str(payload["envelope"]))
        assert set(envelope) == {
            "schema_version",
            "event_id",
            "source_alias",
            "source_generation",
            "target_alias",
            "generation",
            "message",
        }
        return _accepted(envelope)

    monkeypatch.setattr(runtime, "tailnet_nodes", lambda: [OWNER])
    monkeypatch.setattr(runtime, "request_tailnet", broker)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    assert send(tmp_path, source, handle, "hello")["status"] == "TRANSPORT_ACCEPTED"

    peers_requests = [payload for payload in sent if payload.get("operation") == "peers"]
    assert peers_requests
    assert all(
        set(payload)
        in (
            {"schema_version", "operation", "handle", "include_devin"},
            {"schema_version", "operation", "include_devin"},
            {"schema_version", "operation"},
        )
        for payload in peers_requests
    )
    receive_requests = [payload for payload in sent if payload.get("operation") == "receive"]
    assert [set(payload) for payload in receive_requests] == [
        {"schema_version", "operation", "envelope"}
    ]

    # The receiver side asks exactly the v0.3.8 authorize fields and accepts
    # the same decided refusal keys an old sender validates.
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    event_id = str(uuid4())
    authorize_payloads: list[dict[str, object]] = []

    def authorize(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        authorize_payloads.append(payload)
        assert set(payload) == {
            "schema_version",
            "operation",
            "event_id",
            "source_alias",
            "source_generation",
            "target_key",
            "target_generation",
            "payload_digest",
        }
        return {
            "schema_version": 1,
            "event_id": event_id,
            "status": "REFUSED",
            "source_alias": payload["source_alias"],
            "source_generation": payload["source_generation"],
            "target_key": payload["target_key"],
            "target_generation": payload["target_generation"],
            "payload_digest": payload["payload_digest"],
        }

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "READY",
            "alias": target.alias,
            "generation": target.generation,
        }

    monkeypatch.setattr(runtime, "request_tailnet", authorize)
    monkeypatch.setattr(runtime, "request_socket", courier)
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=str(uuid4()),
        target_alias=target.alias,
        generation=target.generation,
        message="hello",
    )

    response = receive_remote(tmp_path, envelope, OWNER)

    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert len(authorize_payloads) == 1
