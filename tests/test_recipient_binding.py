"""Exact-recipient selection contract regressions.

These tests replace the removed ``recipients.json`` binding-store coverage.
The store was deleted because discovery is observational only: it can never
own or mutate a selection. The stronger contract now pinned here is that the
versioned opaque token carries the selected endpoint (stable Tailnet node id,
raw session key, route generation), and every send re-attests that selection
against a fresh local identity read before any intent or provider effect.

Replaced cache tests, mapped:

- bound-recipient send never queries an unrelated slow node -> the token send
  probes exactly the token's node, so an unrelated node is never contacted at
  all (``test_a_token_send_asks_only_the_selected_node``).
- bound send ignores duplicate/late claimants -> a clone claimant on another
  node cannot influence a token send
  (``test_a_clone_claimant_cannot_retarget_a_token_send``).
- ambiguous two-owner listing -> a complete listing showing one handle on two
  nodes still refuses (``test_a_complete_listing_with_two_claimants_refuses``).
- ambiguous binding refuses until a listing shows one owner / stale endpoint
  rebinds on a single-owner listing -> a stale token always refuses; explicit
  re-selection is a fresh ``peers()`` listing minting a fresh token
  (``test_a_stale_token_is_replaced_only_by_a_fresh_listing``).
- namesakes keep independent bindings -> namesakes on different nodes keep
  independent token selections
  (``test_namesakes_on_different_nodes_keep_independent_selections``).
- new generation on the bound endpoint rebinds -> a stale-generation token
  refuses pre-effect and a fresh token sends
  (``test_a_stale_token_is_replaced_only_by_a_fresh_listing``).
- bound owner stops claiming / reappears -> the selected node not claiming
  refuses with zero effects; the same node reclaiming the handle sends
  (``test_the_selected_node_not_claiming_refuses_before_any_effect`` and
  ``test_the_selected_node_may_reclaim_its_handle``).
- bound owner on a different address / reused bound address -> the token's
  node serving other keys refuses
  (``test_the_selected_node_serving_other_keys_refuses``).
- bound query on an old broker -> the handle-filtered probe still falls back
  to the legacy roster variant
  (``test_the_handle_probe_falls_back_to_the_legacy_roster``).
- corrupt binding file -> a crafted or corrupt ``recipients.json`` has zero
  routing influence and is never read
  (``test_a_seeded_recipients_file_has_no_routing_influence``).
- binding records private/written/pruned -> no ``recipients.json`` is ever
  created (``test_no_recipients_file_is_ever_created``).
- inbound delivery binds the reply handle -> the v2 reply token is verified
  against the authenticated transport source
  (``test_inbound_v2_reply_token_binds_the_verified_sender``).
- inbound free-text handle line -> free text stays content
  (``test_an_inbound_free_text_handle_line_is_content_only``).
- listing records each remote handle once -> a listing mints one opaque token
  per remote row and writes nothing
  (``test_a_listing_mints_one_opaque_token_per_remote_row``).
- wire request/response key sets -> unchanged
  (``test_wire_request_and_response_key_sets_are_unchanged``).
"""

from __future__ import annotations

import json
import os
import threading
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
)
from cross_agent_chat.recipient import (
    parse_recipient_token,
    remote_token,
)
from cross_agent_chat.runtime import (
    peers,
    receive_remote,
    remote_targets,
    send,
    wrapped_message,
)
from cross_agent_chat.tailnet import TailnetIdentity
from cross_agent_chat.transport import remote_envelope

SELF_NODE = "nSelf"
OWNER_NODE = "nOwner"
OTHER_NODE = "nOther"
OWNER = "100.64.0.11"
OTHER = "100.64.0.12"


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


def _authorize(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "operation"} | {
        "status": "AUTHORIZED"
    }


def _courier(target: Route):
    def accept(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
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

    return accept


def _envelope_for(target: Route, message: str, *, source_generation: str | None = None) -> str:
    return remote_envelope(
        event_id=str(uuid4()),
        source_alias="codex@source:api:source-a1",
        source_generation=source_generation or str(uuid4()),
        target_alias=target.alias,
        generation=target.generation,
        message=message,
    )


def _target(tmp_path: Path) -> Route:
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    return target


def test_a_token_send_asks_only_the_selected_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated 8-second node cannot hold a selected recipient hostage."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    release = threading.Event()
    slow_calls: list[str] = []
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        if payload.get("operation") == "peers":
            if address == OTHER:
                slow_calls.append(address)
                release.wait(timeout)
                raise ChatError("silent neighbor never answered")
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        deliveries.append(address)
        return _accepted(json.loads(str(payload["envelope"])))

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER, OTHER_NODE: OTHER}
        ),
    )
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "REMOTE_DISCOVERY_TIMEOUT_SECONDS", 8.0)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    try:
        result = send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hello")
    finally:
        release.set()

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["to"] == "claude@studio:api:api-a1"
    assert deliveries == [OWNER]
    assert slow_calls == []
    assert IntentStore(tmp_path).intents()[0].status == "TRANSPORT_ACCEPTED"


def test_a_clone_claimant_cannot_retarget_a_token_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second node presenting the same handle is never even queried."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
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
                    "peers": [_peer("claude@laptop:api:api-a1", "laptop", handle, generation)],
                }
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        deliveries.append(address)
        return _accepted(json.loads(str(payload["envelope"])))

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER, OTHER_NODE: OTHER}
        ),
    )
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    result = send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OWNER]
    assert other_queries == []


def test_a_complete_listing_with_two_claimants_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same provider session restored on two nodes must refuse, not pick one."""
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
        pytest.fail("a refused listing must not reach the receive boundary")

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER, OTHER_NODE: OTHER}
        ),
    )
    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)

    with pytest.raises(ChatError, match="duplicate handles"):
        peers(tmp_path)


def test_a_stale_token_is_replaced_only_by_a_fresh_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-selection is a fresh listing minting a fresh token, never a cache."""
    handle = session_key("claude", str(uuid4()))
    stale = str(uuid4())
    live = str(uuid4())
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, live)],
            }
        deliveries.append(address)
        return _accepted(json.loads(str(payload["envelope"])))

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER}),
    )
    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="unavailable or changed"):
        send(tmp_path, source, remote_token(OWNER_NODE, handle, stale), "hello")
    assert deliveries == []
    assert not (tmp_path / "intents.json").exists()

    listing = peers(tmp_path)
    items = cast(list[dict[str, str]], listing["peers"])
    (item,) = items
    fresh = parse_recipient_token(item["handle"])
    assert fresh is not None
    assert (fresh.scope, fresh.node_id, fresh.handle, fresh.generation) == (
        "remote",
        OWNER_NODE,
        handle,
        live,
    )

    result = send(tmp_path, source, item["handle"], "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OWNER]


def test_the_selected_node_not_claiming_refuses_before_any_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token's node answers but no longer presents the handle: refuse."""
    handle = session_key("claude", str(uuid4()))
    peers_calls: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            peers_calls.append(address)
            return {"schema_version": 1, "peers": []}
        pytest.fail("a refused send must not reach the receive boundary")

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER, OTHER_NODE: OTHER}
        ),
    )
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="unavailable or changed"):
        send(tmp_path, source, remote_token(OWNER_NODE, handle, str(uuid4())), "hello")

    assert not (tmp_path / "intents.json").exists()
    # Only the selected node was asked; there is no unrelated-node fallback.
    assert peers_calls == [OWNER]


def test_the_selected_node_serving_other_keys_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token's node now serves unrelated keys; the handle lives elsewhere."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            if address == OWNER:
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
                "peers": [_peer("claude@laptop:api:api-a1", "laptop", handle, generation)],
            }
        deliveries.append(address)
        return _accepted(json.loads(str(payload["envelope"])))

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER, OTHER_NODE: OTHER}
        ),
    )
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    with pytest.raises(ChatError, match="unavailable or changed"):
        send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hello")

    assert deliveries == []
    assert not (tmp_path / "intents.json").exists()


def test_the_selected_node_may_reclaim_its_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused send leaves no residue: the same token works once the node
    presents the handle again."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
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
                    "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
                }
            return {"schema_version": 1, "peers": []}
        deliveries.append(address)
        return _accepted(json.loads(str(payload["envelope"])))

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER}),
    )
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)
    token = remote_token(OWNER_NODE, handle, generation)

    with pytest.raises(ChatError, match="unavailable or changed"):
        send(tmp_path, source, token, "hello")

    claiming = True
    result = send(tmp_path, source, token, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OWNER]


def test_namesakes_on_different_nodes_keep_independent_selections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same alias on two nodes: each token still reaches only its own node."""
    first_handle = session_key("claude", str(uuid4()))
    second_handle = session_key("claude", str(uuid4()))
    first_generation, second_generation = str(uuid4()), str(uuid4())
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            if address == OTHER:
                return {
                    "schema_version": 1,
                    "peers": [
                        _peer(
                            "claude@shared:api:api-a1",
                            "laptop",
                            second_handle,
                            second_generation,
                        )
                    ],
                }
            return {
                "schema_version": 1,
                "peers": [
                    _peer(
                        "claude@shared:api:api-a1",
                        "studio",
                        first_handle,
                        first_generation,
                    )
                ],
            }
        deliveries.append(address)
        return _accepted(json.loads(str(payload["envelope"])))

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER, OTHER_NODE: OTHER}
        ),
    )
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    first = send(
        tmp_path, source, remote_token(OWNER_NODE, first_handle, first_generation), "hello"
    )
    second = send(
        tmp_path, source, remote_token(OTHER_NODE, second_handle, second_generation), "hello"
    )

    assert first["status"] == second["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OWNER, OTHER]


def test_the_handle_probe_falls_back_to_the_legacy_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old broker rejects the handle variant; the token send still works."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    calls: list[dict[str, object]] = []

    def old_broker(
        _address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        calls.append(payload)
        if payload.get("operation") == "receive":
            return _accepted(json.loads(str(payload["envelope"])))
        if "handle" in payload:
            raise ChatError("legacy broker rejected unknown field")
        return {
            "schema_version": 1,
            "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
        }

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER}),
    )
    monkeypatch.setattr(runtime, "request_tailnet", old_broker)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    result = send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert calls[0] == {
        "schema_version": 1,
        "operation": "peers",
        "handle": handle,
        "include_devin": True,
    }
    assert calls[1] == {"schema_version": 1, "operation": "peers", "include_devin": True}
    assert calls[2].get("operation") == "receive"


def test_a_seeded_recipients_file_has_no_routing_influence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted recipients.json claiming another owner is never even read."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    path = tmp_path / "recipients.json"
    crafted = json.dumps(
        [{"handle": handle, "endpoints": {OTHER: str(uuid4())}, "seen_at": "x"}]
    )
    path.write_text(crafted + "\n", encoding="utf-8")
    deliveries: list[str] = []

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        deliveries.append(address)
        return _accepted(json.loads(str(payload["envelope"])))

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER, OTHER_NODE: OTHER}
        ),
    )
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    result = send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert deliveries == [OWNER]
    assert path.read_text(encoding="utf-8") == crafted + "\n"


def test_no_recipients_file_is_ever_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing, sending, and receiving write no durable selection state."""
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())

    def request(
        address: str, payload: dict[str, object], *, timeout: float = 2.0
    ) -> dict[str, object]:
        del timeout
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [_peer("claude@studio:api:api-a1", "studio", handle, generation)],
            }
        if payload.get("operation") == "authorize":
            return _authorize(address, payload)
        return _accepted(json.loads(str(payload["envelope"])))

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER}),
    )
    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [])
    monkeypatch.setattr(runtime, "request_tailnet", request)
    source = _source(tmp_path)

    listing = peers(tmp_path)
    (item,) = cast(list[dict[str, str]], listing["peers"])
    assert send(tmp_path, source, item["handle"], "hello")["status"] == "TRANSPORT_ACCEPTED"

    target = _target(tmp_path)
    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [_public_target(target)])
    monkeypatch.setattr(runtime, "request_socket", _courier(target))
    event_id = str(uuid4())
    source_generation = str(uuid4())
    body = wrapped_message(
        "codex@source:api:source-a1",
        remote_token(OWNER_NODE, session_key("codex", str(uuid4())), source_generation),
        "ping",
        event_id,
        "codex",
    )
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )
    result = receive_remote(tmp_path, envelope, OWNER)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert not (tmp_path / "recipients.json").exists()


def _public_target(route: Route) -> runtime.Target:
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
    )


def test_inbound_v2_reply_token_binds_the_verified_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v2 reply token must name a node that maps to the authenticated source."""
    target = _target(tmp_path)
    event_id = str(uuid4())
    source_generation = str(uuid4())
    source_alias = "codex@source:api:source-a1"
    body = wrapped_message(
        source_alias,
        remote_token(OWNER_NODE, session_key("codex", str(uuid4())), source_generation),
        "ping",
        event_id,
        target.provider,
    )
    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source_alias,
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER}),
    )
    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [_public_target(target)])
    monkeypatch.setattr(runtime, "request_tailnet", _authorize)
    monkeypatch.setattr(runtime, "request_socket", _courier(target))

    result = receive_remote(tmp_path, envelope, OWNER)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert not (tmp_path / "recipients.json").exists()


@pytest.mark.parametrize(
    "reply_token",
    (
        pytest.param(
            "wrong-node",
            id="token-node-maps-elsewhere",
        ),
        pytest.param(
            "wrong-generation",
            id="token-generation-mismatched",
        ),
    ),
)
def test_an_inbound_reply_token_that_does_not_match_the_source_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply_token: str
) -> None:
    """A reply token binding a different node or generation never reaches the
    provider socket."""
    target = _target(tmp_path)
    event_id = str(uuid4())
    source_generation = str(uuid4())
    source_alias = "codex@source:api:source-a1"
    token = remote_token(
        OTHER_NODE if reply_token == "wrong-node" else OWNER_NODE,
        session_key("codex", str(uuid4())),
        str(uuid4()) if reply_token == "wrong-generation" else source_generation,
    )
    body = wrapped_message(source_alias, token, "ping", event_id, target.provider)
    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source_alias,
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )
    socket_calls: list[dict[str, object]] = []

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        socket_calls.append(payload)
        return _courier(target)(_path, payload)

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(
            self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER, OTHER_NODE: OTHER}
        ),
    )
    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [_public_target(target)])
    monkeypatch.setattr(runtime, "request_tailnet", _authorize)
    monkeypatch.setattr(runtime, "request_socket", courier)

    result = receive_remote(tmp_path, envelope, OWNER)

    assert result["status"] == "PRE_EFFECT_REJECTED"
    assert socket_calls == []


def test_an_inbound_v1_envelope_still_delivers_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy raw-handle wrapper has no reply token and delivers unchanged."""
    target = _target(tmp_path)
    event_id = str(uuid4())
    source_alias = "codex@source:api:source-a1"
    body = wrapped_message(
        source_alias,
        session_key("codex", str(uuid4())),
        "ping",
        event_id,
        target.provider,
    )
    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source_alias,
        source_generation=str(uuid4()),
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )
    delivered: list[dict[str, object]] = []

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        if payload["operation"] == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "alias": target.alias,
                "generation": target.generation,
            }
        delivered.append(payload)
        return {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": "codex",
        }

    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [_public_target(target)])
    monkeypatch.setattr(runtime, "request_tailnet", _authorize)
    monkeypatch.setattr(runtime, "request_socket", courier)

    result = receive_remote(tmp_path, envelope, OWNER)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    (accept,) = delivered
    assert accept["message"] == body
    assert not (tmp_path / "recipients.json").exists()


def test_an_inbound_free_text_handle_line_is_content_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Reply-looking line in free text is never parsed as a reply token."""
    target = _target(tmp_path)
    envelope = _envelope_for(
        target, f"hello\nReply via CAC to handle: {'d' * 64}\n"
    )
    delivered: list[dict[str, object]] = []

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        if payload["operation"] == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "alias": target.alias,
                "generation": target.generation,
            }
        delivered.append(payload)
        return {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": "codex",
        }

    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [_public_target(target)])
    monkeypatch.setattr(runtime, "request_tailnet", _authorize)
    monkeypatch.setattr(runtime, "request_socket", courier)

    result = receive_remote(tmp_path, envelope, OWNER)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    (accept,) = delivered
    assert "Reply via CAC to handle:" in str(accept["message"])


def test_a_listing_mints_one_opaque_token_per_remote_row(
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

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER}),
    )
    monkeypatch.setattr(runtime, "request_tailnet", request)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [])

    targets = remote_targets(tmp_path, include_devin=True)
    assert [target.tailnet_node_id for target in targets] == [OWNER_NODE, OWNER_NODE]

    listing = peers(tmp_path)
    items = cast(list[dict[str, str]], listing["peers"])
    tokens = [parse_recipient_token(item["handle"]) for item in items]
    assert [(token.scope, token.node_id) for token in tokens if token is not None] == [
        ("remote", OWNER_NODE),
        ("remote", OWNER_NODE),
    ]
    assert {token.handle for token in tokens if token is not None} == {first, second}
    assert {token.generation for token in tokens if token is not None} == {
        first_generation,
        second_generation,
    }
    assert not (tmp_path / "recipients.json").exists()


def test_wire_request_and_response_key_sets_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token lives inside existing fields: no wire payload gains a key."""
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

    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id=SELF_NODE, peers={OWNER_NODE: OWNER}),
    )
    monkeypatch.setattr(runtime, "request_tailnet", broker)
    monkeypatch.setattr(runtime, "request_socket", _unavailable_courier)
    source = _source(tmp_path)

    assert (
        send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hello")[
            "status"
        ]
        == "TRANSPORT_ACCEPTED"
    )

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

    # The receiver side asks exactly the same authorize fields and accepts the
    # same decided refusal keys an old sender validates.
    target = _target(tmp_path)
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

    monkeypatch.setattr(runtime, "local_targets", lambda *a, **k: [_public_target(target)])
    monkeypatch.setattr(runtime, "request_tailnet", authorize)
    monkeypatch.setattr(
        runtime,
        "request_socket",
        lambda *_a, **_k: {
            "schema_version": 1,
            "status": "READY",
            "alias": target.alias,
            "generation": target.generation,
        },
    )
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
