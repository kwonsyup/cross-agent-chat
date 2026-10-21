"""Recipient selection contract: a token reaches only its attesting endpoint.

Replaces the retired ``recipients.json`` binding cache tests. A public handle is
now a self-contained versioned token pinning raw session key + route generation
to a trusted Tailnet stable node ID (remote) or to the issuing state root
(local). Discovery is observational only: nothing a listing or another caller
does can retarget a minted selection, and there is no mutable selection cache
to lose, corrupt, or evict. Sends that cannot re-attest the exact handle and
generation at the selected endpoint refuse before any externally visible
effect.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal, NoReturn, cast
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import (
    ChatError,
    IntentStore,
    Registry,
    Route,
    UnknownDeliveryError,
    session_key,
)
from cross_agent_chat.recipient import (
    RecipientToken,
    local_origin,
    local_token,
    parse_recipient_token,
    remote_token,
)
from cross_agent_chat.runtime import (
    Target,
    peers,
    receive_remote,
    send,
    send_local,
    wrapped_message,
)
from cross_agent_chat.tailnet import TailnetIdentity
from cross_agent_chat.tailnet_broker import handle_broker_request
from cross_agent_chat.transport import remote_envelope

OWNER = "100.64.0.11"
OTHER = "100.64.0.12"
MOVED = "100.64.0.13"
SELF_NODE = "nSelfNode"
OWNER_NODE = "nOwnerNode"
OTHER_NODE = "nOtherNode"
REMOTE_ALIAS = "claude@studio:api:api-a1"


def _route(root: Path, *, provider: str = "claude", device: str = "studio") -> Route:
    route = Route.create(
        provider=provider,
        session_id=str(uuid4()),
        device=device,
        cwd=str(root),
        pid=os.getpid(),
    )
    Registry(root).upsert(route)
    return route


def _source(root: Path, device: str = "imac") -> Route:
    return _route(root, provider="codex", device=device)


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


def _identity(
    self_node_id: str | None = SELF_NODE, peers: dict[str, str] | None = None
) -> TailnetIdentity:
    return TailnetIdentity(self_node_id=self_node_id, peers={} if peers is None else dict(peers))


def _remote_wire(
    calls: list[tuple[str, dict[str, object]]],
    rosters: dict[str, list[dict[str, object]]],
) -> object:
    """A wire stub: each address answers its own roster and accepts receives."""

    def wire(address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        calls.append((address, dict(payload)))
        if payload.get("operation") == "peers":
            response: dict[str, object] = {"schema_version": 1, "peers": rosters.get(address, [])}
            if payload.get("include_local_discovery") is True:
                response["local_discovery"] = "complete"
            return response
        if payload.get("operation") == "receive":
            return _accepted(json.loads(str(payload["envelope"])))
        raise ChatError("unexpected wire operation")

    return wire


def _live_alias(route: Route) -> str:
    """The alias a live courier reports: claude aliases carry the agent suffix."""
    return route.alias if route.provider != "claude" else f"{route.alias}:agent-a1"


def _courier(routes: list[Route], accepts: list[dict[str, object]] | None = None) -> object:
    """A local socket stub answering health checks and accept deliveries."""
    by_generation = {route.generation: route for route in routes}

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        route = by_generation.get(str(payload["generation"]))
        if route is None:
            raise ChatError("unknown fixture route")
        if payload["operation"] == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "generation": route.generation,
                "alias": _live_alias(route),
            }
        if payload["operation"] == "accept":
            if accepts is not None:
                accepts.append(dict(payload))
            return {
                "schema_version": 1,
                "event_id": payload["event_id"],
                "status": "TRANSPORT_ACCEPTED",
                "to": _live_alias(route),
                "provider": route.provider,
            }
        raise ChatError("unexpected socket operation")

    return courier


def _public_handle(item: dict[str, str]) -> RecipientToken:
    token = parse_recipient_token(item["handle"])
    assert token is not None
    return token


# --- Token codec -----------------------------------------------------------


def _encode_token(fields: dict[str, object]) -> str:
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return "cac2." + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def test_local_and_remote_tokens_round_trip_their_exact_selection() -> None:
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())

    local = parse_recipient_token(local_token(Path("/tmp/state"), handle, generation))
    assert local is not None
    assert local.scope == "local"
    assert local.handle == handle
    assert local.generation == generation
    assert local.origin == local_origin(Path("/tmp/state"))
    assert local.node_id is None

    remote = parse_recipient_token(remote_token(OWNER_NODE, handle, generation))
    assert remote is not None
    assert remote.scope == "remote"
    assert remote.handle == handle
    assert remote.generation == generation
    assert remote.node_id == OWNER_NODE
    assert remote.origin is None


@pytest.mark.parametrize(
    "version",
    ["2", 2.0, True, 3, 0, None],
    ids=["string", "float", "bool", "newer", "zero", "null"],
)
def test_token_parser_rejects_malformed_version_values(version: object) -> None:
    handle = session_key("claude", str(uuid4()))
    fields = {
        "v": version,
        "s": "remote",
        "h": handle,
        "g": str(uuid4()),
        "n": OWNER_NODE,
    }
    with pytest.raises(ChatError, match="recipient token is invalid"):
        parse_recipient_token(_encode_token(fields))


@pytest.mark.parametrize(
    "fields",
    [
        {"v": 2, "s": "remote", "h": "h" * 64, "g": str(uuid4()), "n": OWNER_NODE},
        {
            "v": 2,
            "s": "remote",
            "h": "f" * 64,
            "g": "not-a-uuid",
            "n": OWNER_NODE,
        },
        {"v": 2, "s": "remote", "h": "f" * 64, "g": str(uuid4()), "n": "bad node!"},
        {"v": 2, "s": "remote", "h": "f" * 64, "g": str(uuid4())},
        {"v": 2, "s": "local", "h": "f" * 64, "g": str(uuid4()), "n": OWNER_NODE},
        {
            "v": 2,
            "s": "remote",
            "h": "f" * 64,
            "g": str(uuid4()),
            "n": OWNER_NODE,
            "x": 1,
        },
        {"v": 2, "s": "bridged", "h": "f" * 64, "g": str(uuid4()), "n": OWNER_NODE},
    ],
    ids=[
        "short-handle",
        "bad-generation",
        "bad-node-id",
        "missing-node-id",
        "local-with-remote-key",
        "extra-field",
        "unknown-scope",
    ],
)
def test_token_parser_enforces_closed_fields_and_field_shapes(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ChatError, match="recipient token is invalid"):
        parse_recipient_token(_encode_token(fields))


@pytest.mark.parametrize(
    "value",
    [
        "cac2.",
        "cac2." + "A" * 5000,
        "cac2.not-base64!",
        "cac2." + base64.urlsafe_b64encode(b"[1,2,3]").decode().rstrip("="),
        "cac2." + base64.urlsafe_b64encode(b"12345").decode().rstrip("="),
    ],
    ids=["empty", "oversized", "bad-charset", "json-array", "json-scalar"],
)
def test_token_parser_rejects_unbounded_or_non_object_payloads(value: str) -> None:
    with pytest.raises(ChatError, match="recipient token is invalid"):
        parse_recipient_token(value)


def test_non_token_values_are_not_parsed_as_tokens() -> None:
    assert parse_recipient_token("claude@studio:api:api-a1") is None
    assert parse_recipient_token("f" * 64) is None
    assert parse_recipient_token("cac1.legacy") is None


@pytest.mark.parametrize("sender", [send, send_local], ids=["send", "send_local"])
@pytest.mark.parametrize(
    "recipient",
    [
        "cac2.not-base64!",
        _encode_token({"v": 2, "s": "remote", "h": "f" * 64, "n": OWNER_NODE}),
    ],
    ids=["invalid-encoding", "missing-generation"],
)
def test_malformed_recipient_refuses_before_intent_or_provider_without_settling_old_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: Callable[[Path, Route, str, str], dict[str, object]],
    recipient: str,
) -> None:
    source, target = _source(tmp_path), _route(tmp_path)
    store = IntentStore(tmp_path)
    old_event = store.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)
    store.mark(old_event, "UNKNOWN_DELIVERY")
    before = store.path.read_bytes()

    def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("malformed recipient reached discovery, intent admission or provider I/O")

    for name in (
        "_local_discovery",
        "_remote_discovery",
        "_begin_delivery",
        "request_socket",
        "request_tailnet",
    ):
        monkeypatch.setattr(runtime, name, unexpected)

    with pytest.raises(ChatError) as refused:
        sender(tmp_path, source, recipient, "new independent fixture request")

    assert type(refused.value) is ChatError
    assert str(refused.value) == (
        "recipient token is invalid; nothing was delivered; "
        "call chat_peers and choose a fresh exact recipient handle"
    )
    assert recipient not in str(refused.value)
    assert store.path.read_bytes() == before
    assert [(item.event_id, item.status) for item in store.intents()] == [
        (old_event, "UNKNOWN_DELIVERY")
    ]


@pytest.mark.parametrize(
    ("sender", "scope"),
    [(send, "local"), (send_local, "local"), (send, "remote")],
    ids=["send-local", "send_local", "send-remote"],
)
def test_sender_does_not_reclassify_downstream_unknown_after_valid_recipient_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: Callable[[Path, Route, str, str], dict[str, object]],
    scope: Literal["local", "remote"],
) -> None:
    source, target = _source(tmp_path), _route(tmp_path)
    handle = session_key(target.provider, target.session_id)
    recipient = (
        local_token(tmp_path, handle, target.generation)
        if scope == "local"
        else remote_token(OWNER_NODE, handle, target.generation)
    )
    expected = parse_recipient_token(recipient)
    assert expected is not None
    unknown = UnknownDeliveryError("provider effect may have happened; do not replay")
    selected: list[RecipientToken] = []

    def downstream(
        _root: Path,
        _source: runtime.SourceIdentity,
        token: RecipientToken,
        _message: str,
        **_kwargs: object,
    ) -> NoReturn:
        selected.append(token)
        raise unknown

    monkeypatch.setattr(runtime, f"_send_{scope}_token_target", downstream)
    with pytest.raises(UnknownDeliveryError) as refused:
        sender(tmp_path, source, recipient, "independent fixture request")

    assert selected == [expected]
    assert refused.value is unknown
    assert "nothing was delivered" not in str(refused.value)


# --- Public listing --------------------------------------------------------


def _listing_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity: TailnetIdentity,
    rosters: dict[str, list[dict[str, object]]],
) -> tuple[Route, list[tuple[str, dict[str, object]]]]:
    local = _route(tmp_path, provider="claude", device="studio")
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(runtime, "tailnet_identity", lambda: identity)
    monkeypatch.setattr(runtime, "request_tailnet", _remote_wire(calls, rosters))
    monkeypatch.setattr(runtime, "request_socket", _courier([local]))
    return local, calls


def test_public_peers_mints_opaque_tokens_and_internal_keeps_raw_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_handle = session_key("claude", str(uuid4()))
    remote_generation = str(uuid4())
    local, _calls = _listing_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OWNER_NODE: OWNER}),
        rosters={OWNER: [_peer(REMOTE_ALIAS, "studio", remote_handle, remote_generation)]},
    )

    listing = peers(tmp_path)
    items = {item["alias"]: item for item in cast(list[dict[str, str]], listing["peers"])}
    assert listing["remote_discovery"] == "complete"
    assert set(items) == {_live_alias(local), REMOTE_ALIAS}

    local_token_value = _public_handle(items[_live_alias(local)])
    assert local_token_value.scope == "local"
    assert local_token_value.handle == session_key("claude", local.session_id)
    assert local_token_value.generation == local.generation

    remote_token_value = _public_handle(items[REMOTE_ALIAS])
    assert remote_token_value.scope == "remote"
    assert remote_token_value.handle == remote_handle
    assert remote_token_value.generation == remote_generation
    assert remote_token_value.node_id == OWNER_NODE

    internal = peers(tmp_path, include_remote=False, internal=True)
    (internal_item,) = cast(list[dict[str, str]], internal["peers"])
    assert "handle" not in internal_item
    assert internal_item["session_key"] == session_key("claude", local.session_id)
    assert internal_item["generation"] == local.generation


def test_public_peers_omits_remote_rows_without_a_verified_self(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Self.ID means no source reply token, so no selectable remote peer."""
    local, _calls = _listing_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(self_node_id=None, peers={OWNER_NODE: OWNER}),
        rosters={
            OWNER: [
                _peer(REMOTE_ALIAS, "studio", session_key("claude", str(uuid4())), str(uuid4()))
            ]
        },
    )

    listing = peers(tmp_path)

    assert [item["alias"] for item in cast(list[dict[str, str]], listing["peers"])] == [
        _live_alias(local)
    ]
    # The roster itself was reachable; the rows are withheld, not undiscovered.
    assert listing["remote_discovery"] == "complete"


def test_simultaneous_listings_mint_identical_tokens_without_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = session_key("claude", str(uuid4()))
    generation = str(uuid4())
    _listing_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OWNER_NODE: OWNER}),
        rosters={OWNER: [_peer(REMOTE_ALIAS, "studio", handle, generation)]},
    )

    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def list_peers() -> None:
        try:
            results.append(peers(tmp_path))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=list_peers) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 4
    assert all(result == results[0] for result in results)
    assert not (tmp_path / "recipients.json").exists()


# --- Exact sends ------------------------------------------------------------


def _remote_send_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity: TailnetIdentity,
    rosters: dict[str, list[dict[str, object]]],
) -> tuple[Route, list[tuple[str, dict[str, object]]]]:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(runtime, "tailnet_identity", lambda: identity)
    monkeypatch.setattr(runtime, "request_tailnet", _remote_wire(calls, rosters))
    monkeypatch.setattr(runtime, "request_socket", _courier([]))
    return _source(tmp_path), calls


def _remote_selection() -> tuple[str, str]:
    return session_key("claude", str(uuid4())), str(uuid4())


def test_token_send_probes_only_the_selected_node_and_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, generation = _remote_selection()
    source, calls = _remote_send_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OWNER_NODE: OWNER, OTHER_NODE: OTHER}),
        rosters={OWNER: [_peer(REMOTE_ALIAS, "studio", handle, generation)]},
    )
    token = remote_token(OWNER_NODE, handle, generation)

    result = send(tmp_path, source, token, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["to"] == REMOTE_ALIAS
    operations = [(address, str(payload.get("operation"))) for address, payload in calls]
    # The unrelated node is never even probed.
    assert all(address == OWNER for address, _ in operations)
    peers_calls = [payload for _a, payload in calls if payload.get("operation") == "peers"]
    assert peers_calls and all(payload.get("handle") == handle for payload in peers_calls)
    receive_calls = [payload for _a, payload in calls if payload.get("operation") == "receive"]
    assert len(receive_calls) == 1
    assert not (tmp_path / "recipients.json").exists()


def test_same_node_address_move_still_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token pins the stable node, not the address observed at listing."""
    handle, generation = _remote_selection()
    source, calls = _remote_send_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OWNER_NODE: MOVED}),
        rosters={MOVED: [_peer(REMOTE_ALIAS, "studio", handle, generation)]},
    )

    result = send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hi")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert {address for address, _ in calls} == {MOVED}


def test_token_send_refuses_when_the_node_was_reassigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old address now belongs to another node: refuse before any probe."""
    handle, generation = _remote_selection()
    source, calls = _remote_send_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OTHER_NODE: OWNER}),
        rosters={OWNER: [_peer(REMOTE_ALIAS, "studio", handle, generation)]},
    )

    with pytest.raises(ChatError, match="not on the tailnet"):
        send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hi")

    # The reassigned address is never contacted at all.
    assert calls == []


def test_token_send_refuses_when_the_node_stopped_claiming_the_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, generation = _remote_selection()
    source, calls = _remote_send_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OWNER_NODE: OWNER}),
        rosters={
            OWNER: [_peer(REMOTE_ALIAS, "studio", session_key("claude", str(uuid4())), generation)]
        },
    )

    with pytest.raises(
        ChatError, match="selected recipient could not be verified; nothing was delivered"
    ):
        send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hi")

    assert [str(payload.get("operation")) for _a, payload in calls] == ["peers"]


def test_stale_generation_is_refused_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, generation = _remote_selection()
    source, calls = _remote_send_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OWNER_NODE: OWNER}),
        rosters={OWNER: [_peer(REMOTE_ALIAS, "studio", handle, str(uuid4()))]},
    )

    with pytest.raises(
        ChatError, match="selected recipient could not be verified; nothing was delivered"
    ):
        send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hi")

    assert [str(payload.get("operation")) for _a, payload in calls] == ["peers"]


def test_token_send_survives_another_callers_incomplete_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token minted from roster A still selects A after a B-only listing."""
    handle, generation = _remote_selection()
    local = _route(tmp_path, provider="claude", device="studio")
    source = _source(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(runtime, "request_socket", _courier([local, source]))

    identities = iter(
        [
            _identity(peers={OWNER_NODE: OWNER}),
            _identity(peers={OTHER_NODE: OTHER}),
            _identity(peers={OWNER_NODE: OWNER}),
        ]
    )
    monkeypatch.setattr(runtime, "tailnet_identity", lambda: next(identities))

    def wire(address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        calls.append((address, dict(payload)))
        if address == OTHER:
            # Another caller's world: B is present but never answers.
            raise ChatError("silent node")
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [_peer(REMOTE_ALIAS, "studio", handle, generation)],
            }
        if payload.get("operation") == "receive":
            return _accepted(json.loads(str(payload["envelope"])))
        raise ChatError("unexpected wire operation")

    monkeypatch.setattr(runtime, "request_tailnet", wire)

    first = peers(tmp_path)
    (token_item,) = [
        item for item in cast(list[dict[str, str]], first["peers"]) if item["alias"] == REMOTE_ALIAS
    ]
    token = token_item["handle"]

    second = peers(tmp_path)
    assert second["remote_discovery"] == "incomplete"
    assert all(
        item["alias"] != REMOTE_ALIAS for item in cast(list[dict[str, str]], second["peers"])
    )

    result = send(tmp_path, source, token, "hello")
    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["to"] == REMOTE_ALIAS
    assert not (tmp_path / "recipients.json").exists()


def test_a_corrupt_or_stale_cache_file_has_no_routing_influence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, generation = _remote_selection()
    source, calls = _remote_send_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OWNER_NODE: OWNER}),
        rosters={OWNER: [_peer(REMOTE_ALIAS, "studio", handle, generation)]},
    )
    # A legacy-shaped cache pointing the same handle at a different endpoint.
    stale = tmp_path / "recipients.json"
    stale.write_text(
        json.dumps(
            [
                {
                    "handle": handle,
                    "endpoints": {OTHER: generation},
                    "seen_at": "2025-01-01T00:00:00+00:00",
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    before = stale.read_bytes()

    result = send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hi")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert {address for address, _ in calls} == {OWNER}
    assert stale.read_bytes() == before

    stale.write_bytes(b"{corrupt")
    result = send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hi")
    assert result["status"] == "TRANSPORT_ACCEPTED"


def test_local_token_send_delivers_to_the_exact_local_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _route(tmp_path, provider="claude", device="studio")
    source = _source(tmp_path)
    accepts: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "request_socket", _courier([target, source], accepts))
    monkeypatch.setattr(
        runtime,
        "request_tailnet",
        lambda *_args, **_kwargs: pytest.fail("local send touched the tailnet"),
    )
    token = local_token(tmp_path, session_key("claude", target.session_id), target.generation)

    result = send(tmp_path, source, token, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["to"] == _live_alias(target)
    (accept,) = accepts
    reply = parse_recipient_token(
        str(accept["message"]).split("Reply via CAC to handle: ", 1)[1].split("\n", 1)[0]
    )
    assert reply is not None and reply.scope == "local"
    assert reply.handle == session_key("codex", source.session_id)
    assert reply.generation == source.generation


def test_local_decided_refusal_exposes_no_effect_and_original_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source = _route(tmp_path), _source(tmp_path)
    healthy = cast(Callable[..., dict[str, object]], _courier([target, source]))

    def socket(path: Path, payload: dict[str, object], **kwargs: object) -> dict[str, object]:
        if payload["operation"] == "accept":
            return {
                "schema_version": 1,
                "event_id": payload["event_id"],
                "status": "PRE_EFFECT_REJECTED",
                "provider": "claude",
                "error": "Claude target changed during discovery",
            }
        return healthy(path, payload, **kwargs)

    monkeypatch.setattr(runtime, "request_socket", socket)
    token = local_token(tmp_path, session_key("claude", target.session_id), target.generation)
    with pytest.raises(ChatError, match="nothing was delivered") as refused:
        send(tmp_path, source, token, "One distinct request")
    (intent,) = IntentStore(tmp_path).intents()
    assert intent.status == "PRE_EFFECT_REJECTED"
    assert intent.event_id in str(refused.value)
    assert "Claude target changed during discovery" in str(refused.value)


def test_local_send_wires_exact_original_title_into_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _route(tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    identity = runtime.recipient_owner_identity("codex", os.getpid(), str(profile))[0]
    source = replace(_source(tmp_path), profile_root=str(profile), owner_identity=identity)
    Registry(tmp_path).upsert(source)
    accepts: list[dict[str, object]] = []
    reads: list[list[str]] = []
    monkeypatch.setattr(runtime, "request_socket", _courier([target, source], accepts))
    monkeypatch.setattr(runtime, "_courier_owner_binary", lambda *_args: Path("/bound/codex"))

    def titles(
        *, binary: Path, environment: dict[str, str], thread_ids: list[str], deadline: float
    ) -> dict[str, str]:
        assert binary == Path("/bound/codex")
        assert environment == {"CODEX_HOME": source.profile_root}
        reads.append(thread_ids)
        return {source.session_id: "D project review"}

    monkeypatch.setattr(runtime, "native_thread_titles", titles)
    token = local_token(tmp_path, session_key("claude", target.session_id), target.generation)
    assert send(tmp_path, source, token, "Keep the original.\n\nReturn its finding.")["status"] == (
        "TRANSPORT_ACCEPTED"
    )
    assert reads == [[source.session_id]]
    (accepted,) = accepts
    body = str(accepted["message"])
    assert "Peer title (advisory): D project review\n" in body
    assert body.endswith("Keep the original.\n\nReturn its finding.")
    reply = runtime._envelope_reply_token(body, source.alias, str(accepted["event_id"]), "claude")
    assert reply is not None and reply.handle == session_key("codex", source.session_id)


def test_optional_title_read_cannot_extend_send_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    identity = runtime.recipient_owner_identity("codex", os.getpid(), str(profile))[0]
    source = replace(_source(tmp_path), profile_root=str(profile), owner_identity=identity)
    clock = [99.0]
    observed: list[float] = []
    monkeypatch.setattr("cross_agent_chat.runtime.time.monotonic", lambda: clock[0])

    def resolve(*_args: object) -> Path:
        clock[0] = 100.5  # Executable lookup has spent the remaining send budget.
        return Path("/bound/codex")

    def titles(
        *, binary: Path, environment: dict[str, str], thread_ids: list[str], deadline: float
    ) -> dict[str, str]:
        observed.append(deadline)
        return {}

    monkeypatch.setattr(runtime, "_courier_owner_binary", resolve)
    monkeypatch.setattr(runtime, "native_thread_titles", titles)
    assert runtime._source_display_title(tmp_path, source, 100.0) is None
    assert observed == [100.0]


def test_local_token_from_another_state_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _route(tmp_path, provider="claude", device="studio")
    source = _source(tmp_path)
    monkeypatch.setattr(
        runtime,
        "request_socket",
        lambda *_args, **_kwargs: pytest.fail("provider was touched"),
    )
    foreign = tmp_path / "other-state"
    foreign.mkdir()
    token = local_token(foreign, session_key("claude", target.session_id), target.generation)

    with pytest.raises(ChatError, match="different state"):
        send(tmp_path, source, token, "hello")


def test_local_scope_token_cannot_send_to_a_remote_peer_and_vice_versa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, generation = _remote_selection()
    source, _calls = _remote_send_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OWNER_NODE: OWNER}),
        rosters={OWNER: [_peer(REMOTE_ALIAS, "studio", handle, generation)]},
    )

    with pytest.raises(ChatError, match=r"different state|unavailable or changed"):
        send(tmp_path, source, local_token(tmp_path, handle, generation), "hi")
    with pytest.raises(ChatError, match="remote device"):
        send_local(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hi")


@pytest.mark.parametrize("sender", [send, send_local], ids=["send", "send_local"])
def test_legacy_raw_handles_are_rejected_before_any_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: Callable[[Path, Route, str, str], dict[str, object]],
) -> None:
    source = _source(tmp_path)
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: pytest.fail("raw handle reached discovery"),
    )
    monkeypatch.setattr(
        runtime,
        "request_tailnet",
        lambda *_args, **_kwargs: pytest.fail("raw handle reached the wire"),
    )
    monkeypatch.setattr(
        runtime,
        "request_socket",
        lambda *_args, **_kwargs: pytest.fail("raw handle reached a provider"),
    )

    with pytest.raises(ChatError, match="opaque tokens"):
        sender(tmp_path, source, "ab" * 32, "hello")


def test_duplicate_local_aliases_are_ambiguous_and_cannot_shadow_a_remote_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, generation = _remote_selection()
    source = _source(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(runtime, "request_tailnet", _remote_wire(calls, {}))
    monkeypatch.setattr(runtime, "tailnet_identity", lambda: _identity(peers={OWNER_NODE: OWNER}))
    duplicate = Target(
        alias=REMOTE_ALIAS,
        provider="claude",
        device="studio",
        project="api",
        generation=str(uuid4()),
        session_key=session_key("claude", str(uuid4())),
        remote=False,
        session_id=str(uuid4()),
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    monkeypatch.setattr(runtime, "_local_discovery", lambda *_args, **_kwargs: ([duplicate], True))

    calls.clear()
    monkeypatch.setattr(
        runtime,
        "request_tailnet",
        _remote_wire(calls, {OWNER: [_peer(REMOTE_ALIAS, "studio", handle, generation)]}),
    )
    with pytest.raises(ChatError, match="ambiguous"):
        send(tmp_path, source, REMOTE_ALIAS, "hello")

    # The scoped token never consults the local duplicate at all.
    calls.clear()
    result = send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hi")
    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert {address for address, _ in calls} == {OWNER}


# --- Alias sends revalidate like token sends --------------------------------


@pytest.mark.parametrize("sender", [send, send_local], ids=["send", "send_local"])
def test_local_discovery_health_miss_cannot_hide_duplicate_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: Callable[[Path, Route, str, str], dict[str, object]],
) -> None:
    """Partial health cannot make a legitimate duplicate full alias unique."""
    first = _route(tmp_path, provider="devin")
    second = _route(tmp_path, provider="devin")
    assert first.alias == second.alias
    assert first.session_id != second.session_id
    routes = {item.generation: item for item in [first, second]}
    missing: set[str] = set()
    probes: list[str] = []
    selected: list[str] = []
    monkeypatch.setattr(Route, "process_is_live", lambda _route: True)
    monkeypatch.setattr(runtime, "_remote_discovery", lambda **_kwargs: ([], True))

    def health(_path: Path, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        assert payload["operation"] == "health"
        route = routes[str(payload["generation"])]
        probes.append(route.generation)
        if route.generation in missing:
            raise ChatError("fixture live courier health timed out")
        return {
            "schema_version": 1,
            "status": "READY",
            "generation": route.generation,
            "alias": route.alias,
        }

    def select(
        _root: Path,
        _source: runtime.SourceIdentity,
        target: Target,
        _message: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        selected.append(target.session_key)
        return {"status": "DRY_RUN_SELECTION_ONLY"}

    monkeypatch.setattr(runtime, "request_socket", health)
    monkeypatch.setattr(runtime, "_send_local_target", select)
    complete = peers(tmp_path, include_remote=False, query=first.alias)
    assert complete["local_discovery"] == "complete"
    assert len(cast(list[dict[str, str]], complete["peers"])) == 2
    with pytest.raises(ChatError, match="ambiguous"):
        sender(tmp_path, first, first.alias, "fixture only")
    missing.add(second.generation)
    partial = peers(tmp_path, include_remote=False, query=first.alias)
    assert partial["local_discovery"] == "incomplete"
    (healthy,) = cast(list[dict[str, str]], partial["peers"])
    assert _public_handle(healthy).handle == session_key("devin", first.session_id)
    with pytest.raises(ChatError, match="local peer discovery is incomplete"):
        sender(tmp_path, first, first.alias, "fixture only")
    # Even the endpoint-extension path must not accept this partial alias.
    with pytest.raises(ChatError, match="local peer discovery is incomplete"):
        send(tmp_path, first, first.alias, "fixture only", include_external=True)
    assert selected == []
    probes.clear()
    assert sender(tmp_path, first, healthy["handle"], "fixture only")["status"] == (
        "DRY_RUN_SELECTION_ONLY"
    )
    assert probes == [first.generation]
    # Scoped endpoint discovery also keeps an exact healthy native token usable.
    assert (
        send(tmp_path, first, healthy["handle"], "fixture only", include_external=True)["status"]
        == "DRY_RUN_SELECTION_ONLY"
    )
    missing_token = local_token(
        tmp_path, session_key("devin", second.session_id), second.generation
    )
    with pytest.raises(ChatError, match="unavailable or changed"):
        sender(tmp_path, first, missing_token, "fixture only")
    # A successor at the same session key never inherits the old generation.
    successor = replace(first, generation=str(uuid4()))
    Registry(tmp_path).upsert(successor)
    routes[successor.generation] = successor
    with pytest.raises(ChatError, match="unavailable or changed"):
        sender(tmp_path, successor, healthy["handle"], "fixture only")
    assert selected == [session_key("devin", first.session_id)] * 2
    assert IntentStore(tmp_path).intents() == []
    filtered = peers(tmp_path, include_remote=False, query="absent label")
    assert filtered["peers"] == []
    assert filtered["local_discovery"] == "incomplete"
    with pytest.raises(ChatError, match="local peer discovery is incomplete"):
        peers(tmp_path, include_remote=False, internal=True)
    internal = peers(tmp_path, include_remote=False, internal=True, include_local_discovery=True)
    assert set(internal) == {"schema_version", "peers", "local_discovery"}
    assert internal["local_discovery"] == "incomplete"


def test_broker_partial_local_roster_cannot_make_a_remote_alias_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A responding broker must carry its own missed-twin coverage to the sender."""
    requester, broker = tmp_path / "requester", tmp_path / "broker"
    requester.mkdir(mode=0o700)
    broker.mkdir(mode=0o700)
    source = _source(requester)
    first, second = _route(broker, provider="devin"), _route(broker, provider="devin")
    assert first.alias == second.alias
    routes = {route.generation: route for route in (source, first, second)}
    missing: set[str] = set()
    selected: list[str] = []
    monkeypatch.setattr(Route, "process_is_live", lambda _route: True)
    monkeypatch.setattr(runtime, "_with_codex_titles", lambda _root, rows, _deadline: rows)
    monkeypatch.setattr(runtime, "tailnet_identity", lambda: _identity(peers={OWNER_NODE: OWNER}))

    def health(_path: Path, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        assert payload["operation"] == "health"
        route = routes[str(payload["generation"])]
        if route.generation in missing:
            raise ChatError("fixture live courier health timed out")
        return {
            "schema_version": 1,
            "status": "READY",
            "generation": route.generation,
            "alias": route.alias,
        }

    def wire(_address: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        assert payload["operation"] == "peers"
        return handle_broker_request(broker, payload, "100.64.0.10")

    def select(
        _root: Path,
        _source: runtime.SourceIdentity,
        target: Target,
        _message: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        selected.append(target.session_key)
        return {"status": "DRY_RUN_SELECTION_ONLY"}

    monkeypatch.setattr(runtime, "request_socket", health)
    monkeypatch.setattr(runtime, "request_tailnet", wire)
    monkeypatch.setattr(runtime, "_send_remote_target", select)
    complete = peers(requester, query=first.alias)
    assert complete["remote_discovery"] == "complete"
    assert len(cast(list[object], complete["peers"])) == 2
    with pytest.raises(ChatError, match="ambiguous"):
        send(requester, source, first.alias, "fixture only")

    missing.add(second.generation)
    partial = peers(requester, query=first.alias)
    assert partial["remote_discovery"] == "incomplete"
    (healthy,) = cast(list[dict[str, str]], partial["peers"])
    with pytest.raises(ChatError, match="remote peer discovery is incomplete"):
        send(requester, source, first.alias, "fixture only")
    assert selected == []
    # An old caller cannot understand the new marker: the new broker must
    # refuse its unscoped partial response, not emit a legacy-valid singleton.
    with pytest.raises(ChatError, match="local peer discovery is incomplete"):
        handle_broker_request(
            broker,
            {"schema_version": 1, "operation": "peers", "include_devin": True},
            "100.64.0.10",
        )
    assert send(requester, source, healthy["handle"], "fixture only")["status"] == (
        "DRY_RUN_SELECTION_ONLY"
    )
    missing_token = remote_token(
        OWNER_NODE, session_key(second.provider, second.session_id), second.generation
    )
    with pytest.raises(ChatError, match="nothing was delivered"):
        send(requester, source, missing_token, "fixture only")
    assert selected == [session_key(first.provider, first.session_id)]
    assert IntentStore(requester).intents() == []


@pytest.mark.parametrize("reject_unknown_flag", [False, True])
def test_legacy_node_without_coverage_keeps_rows_but_cannot_justify_alias_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reject_unknown_flag: bool
) -> None:
    source = _source(tmp_path)
    handle, generation = _remote_selection()
    row = _peer(REMOTE_ALIAS, "studio", handle, generation)
    selected: list[str] = []
    monkeypatch.setattr(runtime, "_local_discovery", lambda *_a, **_k: ([], True))
    monkeypatch.setattr(runtime, "_with_codex_titles", lambda _root, rows, _deadline: rows)
    monkeypatch.setattr(runtime, "tailnet_identity", lambda: _identity(peers={OWNER_NODE: OWNER}))

    def legacy(_address: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        assert payload["operation"] == "peers"
        if reject_unknown_flag and "include_local_discovery" in payload:
            raise ChatError("legacy broker rejected unknown field")
        return {"schema_version": 1, "peers": [row]}

    def select(
        _root: Path,
        _source: runtime.SourceIdentity,
        target: Target,
        _message: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        selected.append(target.session_key)
        return {"status": "DRY_RUN_SELECTION_ONLY"}

    monkeypatch.setattr(runtime, "request_tailnet", legacy)
    monkeypatch.setattr(runtime, "_send_remote_target", select)
    listing = peers(tmp_path, query=REMOTE_ALIAS)
    assert listing["remote_discovery"] == "incomplete"
    (healthy,) = cast(list[dict[str, str]], listing["peers"])
    with pytest.raises(ChatError, match="remote peer discovery is incomplete"):
        send(tmp_path, source, REMOTE_ALIAS, "fixture only")
    assert selected == []
    assert send(tmp_path, source, healthy["handle"], "fixture only")["status"] == (
        "DRY_RUN_SELECTION_ONLY"
    )
    assert selected == [handle]
    assert IntentStore(tmp_path).intents() == []


def test_remote_alias_refuses_when_title_pass_reveals_another_same_alias_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    handle, generation = _remote_selection()
    first = _peer(REMOTE_ALIAS, "studio", handle, generation)
    second = _peer(REMOTE_ALIAS, "studio", "b" * 64, str(uuid4()))
    probes: list[dict[str, object]] = []
    monkeypatch.setattr(runtime, "_local_discovery", lambda *_a, **_k: ([], True))
    monkeypatch.setattr(runtime, "tailnet_identity", lambda: _identity(peers={OWNER_NODE: OWNER}))

    def wire(_address: str, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        assert payload["operation"] == "peers"
        probes.append(payload)
        return {
            "schema_version": 1,
            "peers": [first, second] if payload.get("include_title") is True else [first],
            "local_discovery": "complete",
        }

    def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("a newer duplicate was hidden by the earlier singleton")

    monkeypatch.setattr(runtime, "request_tailnet", wire)
    monkeypatch.setattr(runtime, "_send_remote_target", unexpected)
    with pytest.raises(ChatError, match="remote peer discovery is incomplete"):
        send(tmp_path, source, REMOTE_ALIAS, "fixture only")
    assert len(probes) == 2 and probes[-1]["include_title"] is True
    assert IntentStore(tmp_path).intents() == []


def test_local_discovery_incomplete_cannot_establish_unique_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    survivor = Target(
        alias=source.alias,
        provider="codex",
        device=source.device,
        project=source.project,
        generation=source.generation,
        session_key=session_key("codex", source.session_id),
        remote=False,
        title="Current coordinator",
    )
    monkeypatch.setattr(runtime, "_local_discovery", lambda _root: ([survivor], False))
    monkeypatch.setattr(runtime, "_remote_discovery", lambda **_kwargs: ([], True))
    monkeypatch.setattr(
        runtime, "_send_local_target", lambda *_args, **_kwargs: pytest.fail("selected a title")
    )
    with pytest.raises(ChatError, match="local peer discovery is incomplete"):
        send(tmp_path, source, "Current coordinator", "fixture only")


def test_local_discovery_empty_registry_is_complete(tmp_path: Path) -> None:
    assert runtime._local_discovery(tmp_path) == ([], True)


def test_local_discovery_unsubmitted_probe_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _route(tmp_path, provider="devin")
    monkeypatch.setattr(Route, "process_is_live", lambda _route: True)
    monkeypatch.setattr(runtime, "LOCAL_DISCOVERY_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(
        runtime, "request_socket", lambda *_args, **_kwargs: pytest.fail("probe started")
    )
    assert runtime._local_discovery(tmp_path) == ([], False)


def test_local_discovery_external_failure_cannot_establish_unique_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    survivor = Target(
        alias=source.alias,
        provider="codex",
        device=source.device,
        project=source.project,
        generation=source.generation,
        session_key=session_key("codex", source.session_id),
        remote=False,
    )
    monkeypatch.setattr(runtime, "_local_discovery", lambda _root: ([survivor], True))

    def unavailable(_root: Path, *, handle: str | None = None) -> list[Target]:
        raise ChatError("fixture external registry unavailable")

    monkeypatch.setattr(runtime, "external_targets", unavailable)
    monkeypatch.setattr(
        runtime, "_send_local_target", lambda *_args, **_kwargs: pytest.fail("selected an alias")
    )
    with pytest.raises(ChatError, match="local peer discovery is incomplete"):
        send_local(tmp_path, source, source.alias, "fixture only")
    with pytest.raises(ChatError, match="local peer discovery is incomplete"):
        send(tmp_path, source, source.alias, "fixture only", include_external=True)


def _alias_send_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identities: list[TailnetIdentity],
    rosters: dict[str, list[dict[str, object]]],
) -> tuple[Route, list[tuple[str, dict[str, object]]]]:
    """Alias selection reads identity once for discovery, again at dispatch."""
    source = _source(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []
    sequence = iter(identities)
    monkeypatch.setattr(runtime, "tailnet_identity", lambda: next(sequence))
    monkeypatch.setattr(runtime, "request_tailnet", _remote_wire(calls, rosters))
    monkeypatch.setattr(runtime, "request_socket", _courier([source]))
    return source, calls


def test_alias_send_succeeds_when_the_selected_node_moved_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, generation = _remote_selection()
    source, calls = _alias_send_fixture(
        tmp_path,
        monkeypatch,
        identities=[
            _identity(peers={OWNER_NODE: OWNER}),
            _identity(peers={OWNER_NODE: MOVED}),
        ],
        rosters={
            OWNER: [_peer(REMOTE_ALIAS, "studio", handle, generation)],
            MOVED: [_peer(REMOTE_ALIAS, "studio", handle, generation)],
        },
    )

    result = send(tmp_path, source, REMOTE_ALIAS, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    operations = [str(payload.get("operation")) for _a, payload in calls]
    # Discovery probes base plus the negotiated title/mechanism pass, then
    # dispatch re-attests once before the envelope.
    assert operations == ["peers", "peers", "peers", "receive"]
    assert calls[-1][0] == MOVED


def test_alias_send_refuses_when_the_selected_node_left_the_tailnet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, generation = _remote_selection()
    source, calls = _alias_send_fixture(
        tmp_path,
        monkeypatch,
        identities=[
            _identity(peers={OWNER_NODE: OWNER}),
            _identity(peers={OTHER_NODE: OTHER}),
        ],
        rosters={OWNER: [_peer(REMOTE_ALIAS, "studio", handle, generation)]},
    )

    with pytest.raises(ChatError, match="not on the tailnet"):
        send(tmp_path, source, REMOTE_ALIAS, "hello")

    # Discovery probed the owner twice (base roster plus negotiated title
    # enrichment); dispatch touched nothing, and the stale address was never
    # re-asked.
    assert [(address, str(payload.get("operation"))) for address, payload in calls] == [
        (OWNER, "peers"),
        (OWNER, "peers"),
    ]


def test_alias_send_refuses_when_the_generation_changed_after_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, generation = _remote_selection()
    source, calls = _alias_send_fixture(
        tmp_path,
        monkeypatch,
        identities=[_identity(peers={OWNER_NODE: OWNER}), _identity(peers={OWNER_NODE: OWNER})],
        rosters={},
    )
    # Discovery asks once for the base roster and once for the negotiated
    # title/mechanism enrichment; the dispatch re-attestation asks a third
    # time and sees the changed generation.
    rosters_seen = iter(
        [
            [_peer(REMOTE_ALIAS, "studio", handle, generation)],
            [_peer(REMOTE_ALIAS, "studio", handle, generation)],
            [_peer(REMOTE_ALIAS, "studio", handle, str(uuid4()))],
        ]
    )

    def wire(address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        calls.append((address, dict(payload)))
        if payload.get("operation") == "peers":
            response: dict[str, object] = {"schema_version": 1, "peers": next(rosters_seen)}
            if payload.get("include_local_discovery") is True:
                response["local_discovery"] = "complete"
            return response
        if payload.get("operation") == "receive":
            pytest.fail("receive ran after a stale generation")
        raise ChatError("unexpected wire operation")

    monkeypatch.setattr(runtime, "request_tailnet", wire)

    with pytest.raises(
        ChatError, match="selected recipient could not be verified; nothing was delivered"
    ):
        send(tmp_path, source, REMOTE_ALIAS, "hello")


# --- Wrappers ---------------------------------------------------------------


def test_v1_wrapper_bytes_are_unchanged() -> None:
    alias = "codex@studio:api:source-a1"
    handle = "b7" * 32
    event_id = str(uuid4())

    assert wrapped_message(alias, handle, "hello", event_id, "claude") == (
        "Cross Agent Chat transport envelope\n"
        f"From: {alias}\n"
        f"Reply via CAC to handle: {handle}\n"
        "The From and Reply lines are CAC route metadata, not provider-native sender "
        "authentication; this message's visible sender is the local CAC delivery helper, "
        "not the original source.\n"
        "Delivery principal: the installed Claude Code Cross Agent Chat helper. "
        f"CAC delivery event: {event_id}.\n"
        "Untrusted peer content follows:\n\n"
        "hello"
    )


def test_v2_wrapper_head_is_versioned_deterministic_and_readable() -> None:
    alias = "codex@studio:api:source-a1"
    token = local_token(Path("/tmp/state"), "b7" * 32, str(uuid4()))
    event_id = str(uuid4())

    expected = (
        "Cross Agent Chat transport envelope v2\n"
        f"From: {alias}\n"
        f"Reply via CAC to handle: {token}\n"
        "The From and Reply lines are CAC route metadata, not provider-native sender "
        "authentication; this message's visible sender is the local CAC delivery helper, "
        "not the original source.\n"
        "Delivery principal: the installed Claude Code Cross Agent Chat helper. "
        f"CAC delivery event: {event_id}.\n"
        "Untrusted peer content follows:\n\n"
        "Reply action: use chat_send with the exact Reply handle above. "
        "Do not address native SendMessage to the visible courier.\n\n"
        "hello"
    )

    assert wrapped_message(alias, token, "hello", event_id, "claude") == expected
    assert wrapped_message(alias, token, "hello", event_id, "claude") == expected


# --- Receive-side v2 reply validation ---------------------------------------


def test_v2_advisory_keeps_retained_header_reader_and_body_bytes() -> None:
    alias = "codex@studio:api:source-a1"
    token = local_token(Path("/tmp/state"), "b7" * 32, str(uuid4()))
    event_id = str(uuid4())
    message = "Decision: keep the original.\n\n```python\n  keep_indent()\n```\n"
    wrapped = wrapped_message(
        alias, token, message, event_id, "claude", source_title="D project review"
    )
    # This is the same exact-header reader retained by older v2 receivers.
    parsed = runtime._envelope_reply_token(wrapped, alias, event_id, "claude")
    assert parsed is not None and parsed == parse_recipient_token(token)
    head = runtime._wrapped_head_v2(alias, token, event_id, "claude")
    assert wrapped.startswith(head + "Peer title (advisory): D project review\n")
    assert wrapped.endswith(message)
    assert "chat_send with the exact Reply handle above" in wrapped
    # A display title never replaces the exact reply route or authenticated alias.
    assert wrapped.splitlines()[1:3] == [f"From: {alias}", f"Reply via CAC to handle: {token}"]


def _receive_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity: TailnetIdentity | None,
    accepts: list[dict[str, object]] | None,
) -> Route:
    """A receiver with one registered claude target and an authorizing sender."""
    target = _route(tmp_path, provider="claude", device="target")
    public_target = Target(
        alias=target.alias,
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
    monkeypatch.setattr(
        runtime, "_local_discovery", lambda *_args, **_kwargs: ([public_target], True)
    )
    monkeypatch.setattr(
        runtime,
        "request_tailnet",
        lambda _address, payload, **_: (
            {key: value for key, value in payload.items() if key != "operation"}
            | {"status": "AUTHORIZED"}
        ),
    )
    monkeypatch.setattr(runtime, "tailnet_identity", lambda: identity)

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        if accepts is not None:
            accepts.append(dict(payload))
        return {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": "claude",
        }

    monkeypatch.setattr(runtime, "request_socket", courier)
    return target


def _remote_source() -> tuple[str, str, str]:
    return (
        "codex@source:api:source-a1",
        session_key("codex", str(uuid4())),
        str(uuid4()),
    )


def test_remote_receive_delivers_a_valid_v2_envelope_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OTHER_NODE: OWNER}),
        accepts=accepts,
    )
    source_alias, source_handle, source_generation = _remote_source()
    token = remote_token(OTHER_NODE, source_handle, source_generation)
    event_id = str(uuid4())
    body = wrapped_message(source_alias, token, "ping", event_id, "claude")
    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source_alias,
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )

    result = receive_remote(tmp_path, envelope, OWNER)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    (accept,) = accepts
    assert accept["message"] == body
    assert not (tmp_path / "recipients.json").exists()


@pytest.mark.parametrize(
    "body_kind",
    ["v1_wrapped", "plain", "free_text_reply_line"],
)
def test_remote_receive_still_delivers_non_v2_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_kind: str
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OTHER_NODE: OWNER}),
        accepts=accepts,
    )
    source_alias, source_handle, source_generation = _remote_source()
    event_id = str(uuid4())
    if body_kind == "v1_wrapped":
        body = wrapped_message(source_alias, source_handle, "ping", event_id, "claude")
    elif body_kind == "plain":
        body = "just a message"
    else:
        body = f"hello\nReply via CAC to handle: {'d' * 64}\n"
    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source_alias,
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )

    result = receive_remote(tmp_path, envelope, OWNER)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    (accept,) = accepts
    assert accept["message"] == body


def _v2_envelope(
    target: Route, *, token: str | None = None, head_override: str | None = None
) -> tuple[str, str, str, str]:
    source_alias, source_handle, source_generation = _remote_source()
    event_id = str(uuid4())
    if head_override is not None:
        body = head_override
    else:
        exact_token = (
            token
            if token is not None
            else remote_token(OTHER_NODE, source_handle, source_generation)
        )
        body = wrapped_message(source_alias, exact_token, "ping", event_id, "claude")
    envelope = remote_envelope(
        event_id=event_id,
        source_alias=source_alias,
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )
    return envelope, source_alias, source_generation, body


def test_remote_receive_rejects_every_truncated_v2_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every prefix still opening with the v2 marker is refused pre-effect."""
    marker = "Cross Agent Chat transport envelope v2"
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OTHER_NODE: OWNER}),
        accepts=accepts,
    )
    source_alias, source_handle, source_generation = _remote_source()
    token = remote_token(OTHER_NODE, source_handle, source_generation)
    event_id = str(uuid4())
    full = wrapped_message(source_alias, token, "ping", event_id, "claude")
    # Advisory text is content after the verified header. Every truncated
    # header remains invalid; arbitrary complete-header content remains valid.
    head = runtime._wrapped_head_v2(source_alias, token, event_id, "claude")

    rejections = 0
    for cut in range(len(marker), len(head)):
        body = full[:cut]
        envelope = remote_envelope(
            event_id=event_id,
            source_alias=source_alias,
            source_generation=source_generation,
            target_alias=target.alias,
            generation=target.generation,
            message=body,
        )
        response = receive_remote(tmp_path, envelope, OWNER)
        assert response["status"] == "PRE_EFFECT_REJECTED"
        rejections += 1

    assert rejections == len(head) - len(marker)
    assert accepts == []


def test_remote_receive_rejects_malformed_v2_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OTHER_NODE: OWNER}),
        accepts=accepts,
    )
    marker = "Cross Agent Chat transport envelope v2"
    bad_heads = [
        f"{marker}\nFrom: x@y:z:w\nGarbage line\n\nbody",
        f"{marker}\nFrom: x@y:z:w\nReply via CAC to handle: not-a-token\n\nbody",
        f"{marker}\nNot the from line\nReply via CAC to handle: cac2.xx\n\nbody",
    ]
    for head in bad_heads:
        envelope, _a, _g, _b = _v2_envelope(target, head_override=head)
        response = receive_remote(tmp_path, envelope, OWNER)
        assert response["status"] == "PRE_EFFECT_REJECTED"
        assert response["error"] == "reply token is invalid"
    assert accepts == []


def test_remote_receive_rejects_a_local_scoped_reply_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OTHER_NODE: OWNER}),
        accepts=accepts,
    )
    _a, source_handle, source_generation = _remote_source()
    envelope, _sa, _sg, _body = _v2_envelope(
        target,
        token=local_token(tmp_path, source_handle, source_generation),
    )
    response = receive_remote(tmp_path, envelope, OWNER)
    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["error"] == "reply token scope is invalid"
    assert accepts == []


def test_remote_receive_rejects_a_reply_token_generation_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OTHER_NODE: OWNER}),
        accepts=accepts,
    )
    _a, source_handle, _sg = _remote_source()
    envelope, _sa, _sg2, _body = _v2_envelope(
        target, token=remote_token(OTHER_NODE, source_handle, str(uuid4()))
    )
    response = receive_remote(tmp_path, envelope, OWNER)
    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["error"] == "reply token does not match the envelope generation"
    assert accepts == []


def test_remote_receive_rejects_a_reply_token_from_the_wrong_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={OTHER_NODE: MOVED}),
        accepts=accepts,
    )
    envelope, _sa, _sg, _body = _v2_envelope(target)
    response = receive_remote(tmp_path, envelope, OWNER)
    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["error"] == "reply token does not match the authenticated sender"
    assert accepts == []


def test_remote_receive_rejects_a_reply_token_for_an_absent_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path,
        monkeypatch,
        identity=_identity(peers={SELF_NODE: OWNER}),
        accepts=accepts,
    )
    envelope, _sa, _sg, _body = _v2_envelope(target)
    response = receive_remote(tmp_path, envelope, OWNER)
    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["error"] == "reply token does not match the authenticated sender"
    assert accepts == []


def test_remote_receive_rejects_a_reply_token_when_tailscale_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(tmp_path, monkeypatch, identity=None, accepts=accepts)
    envelope, _sa, _sg, _body = _v2_envelope(target)
    response = receive_remote(tmp_path, envelope, OWNER)
    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["error"] == "reply token does not match the authenticated sender"
    assert accepts == []


@pytest.mark.parametrize("boundary", ["_envelope_reply_token", "_verify_remote_reply_token"])
def test_remote_receive_never_reclassifies_an_unknown_reply_check_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path, monkeypatch, identity=_identity(peers={OTHER_NODE: OWNER}), accepts=accepts
    )
    envelope, _alias, _generation, _body = _v2_envelope(target)
    unknown = UnknownDeliveryError("fixture reply-check outcome remains unknown")

    def unresolved(*_args: object, **_kwargs: object) -> NoReturn:
        raise unknown

    monkeypatch.setattr(runtime, boundary, unresolved)
    with pytest.raises(UnknownDeliveryError) as caught:
        receive_remote(tmp_path, envelope, OWNER)
    assert caught.value is unknown
    assert accepts == []


def test_remote_receive_keeps_an_unrecognized_reply_verifier_failure_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(
        tmp_path, monkeypatch, identity=_identity(peers={OTHER_NODE: OWNER}), accepts=accepts
    )
    envelope, _alias, _generation, _body = _v2_envelope(target)

    def failed(*_args: object, **_kwargs: object) -> NoReturn:
        raise ChatError("fixture-private read failure")

    monkeypatch.setattr(runtime, "_verify_remote_reply_token", failed)
    response = receive_remote(tmp_path, envelope, OWNER)
    assert set(response) == {"schema_version", "event_id", "status", "provider", "error"}
    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["error"] == runtime.REMOTE_REPLY_BINDING_FAILURE
    assert "fixture-private" not in str(response)
    assert accepts == []


# --- Wire schema ------------------------------------------------------------


def test_wire_request_and_response_key_sets_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tokens live inside existing fields: no wire payload may gain a key."""
    handle, generation = _remote_selection()
    sent: list[tuple[str, dict[str, object]]] = []

    def broker(address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        sent.append((address, dict(payload)))
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [_peer(REMOTE_ALIAS, "studio", handle, generation)],
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

    monkeypatch.setattr(runtime, "tailnet_identity", lambda: _identity(peers={OWNER_NODE: OWNER}))
    monkeypatch.setattr(runtime, "request_tailnet", broker)
    monkeypatch.setattr(
        runtime,
        "request_socket",
        lambda *_args, **_kwargs: pytest.fail("unexpected courier call"),
    )
    source = _source(tmp_path)

    result = send(tmp_path, source, remote_token(OWNER_NODE, handle, generation), "hello")
    assert result["status"] == "TRANSPORT_ACCEPTED"

    peers_requests = [payload for _a, payload in sent if payload.get("operation") == "peers"]
    assert peers_requests
    negotiated = {
        "include_delivery_mode",
        "include_delivery_mechanism",
        "include_owning_daemon",
    }
    tool_boundary = negotiated | {"include_devin_tool_boundary"}
    boundary = tool_boundary | {"include_current_boundary"}
    acp_host = tool_boundary | {"include_devin_acp_host"}
    acp_boundary = boundary | {"include_devin_acp_host"}
    assert all(
        set(payload)
        in (
            {"schema_version", "operation", "handle", "include_devin"} | acp_boundary,
            {"schema_version", "operation", "handle", "include_devin"} | acp_host,
            {"schema_version", "operation", "handle", "include_devin"} | boundary,
            {"schema_version", "operation", "handle", "include_devin"} | tool_boundary,
            {"schema_version", "operation", "handle", "include_devin"} | negotiated,
            {"schema_version", "operation", "handle", "include_devin"},
            {"schema_version", "operation", "include_devin"} | acp_boundary,
            {"schema_version", "operation", "include_devin"} | acp_host,
            {"schema_version", "operation", "include_devin"} | boundary,
            {"schema_version", "operation", "include_devin"} | tool_boundary,
            {"schema_version", "operation", "include_devin"} | negotiated,
            {"schema_version", "operation", "include_devin"},
            {"schema_version", "operation"},
        )
        for payload in peers_requests
    )
    receive_requests = [payload for _a, payload in sent if payload.get("operation") == "receive"]
    assert [set(payload) for payload in receive_requests] == [
        {"schema_version", "operation", "envelope"}
    ]

    # The receiver side asks exactly the v0.3.8 authorize fields and accepts
    # the same decided refusal keys an old sender validates.
    target = _route(tmp_path, provider="codex", device="target")
    event_id = str(uuid4())

    def authorize(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
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
