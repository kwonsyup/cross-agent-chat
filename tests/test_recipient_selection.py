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
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import (
    ChatError,
    Registry,
    Route,
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
            return {"schema_version": 1, "peers": rosters.get(address, [])}
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

    with pytest.raises(ChatError, match="unavailable or changed"):
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

    with pytest.raises(ChatError, match="unavailable or changed"):
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
    sender: object,
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
        sender(tmp_path, source, "ab" * 32, "hello")  # type: ignore[operator]


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
    monkeypatch.setattr(runtime, "local_targets", lambda *_args, **_kwargs: [duplicate])

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
    monkeypatch.setattr(runtime, "request_socket", _courier([]))
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
    assert operations == ["peers", "peers", "receive"]
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

    # Discovery probed the owner once; dispatch touched nothing, and the stale
    # address was never re-asked.
    assert [(address, str(payload.get("operation"))) for address, payload in calls] == [
        (OWNER, "peers")
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
    rosters_seen = iter(
        [
            [_peer(REMOTE_ALIAS, "studio", handle, generation)],
            [_peer(REMOTE_ALIAS, "studio", handle, str(uuid4()))],
        ]
    )

    def wire(address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        calls.append((address, dict(payload)))
        if payload.get("operation") == "peers":
            return {"schema_version": 1, "peers": next(rosters_seen)}
        if payload.get("operation") == "receive":
            pytest.fail("receive ran after a stale generation")
        raise ChatError("unexpected wire operation")

    monkeypatch.setattr(runtime, "request_tailnet", wire)

    with pytest.raises(ChatError, match="unavailable or changed"):
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
        "hello"
    )

    assert wrapped_message(alias, token, "hello", event_id, "claude") == expected
    assert wrapped_message(alias, token, "hello", event_id, "claude") == expected


# --- Receive-side v2 reply validation ---------------------------------------


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
    monkeypatch.setattr(runtime, "local_targets", lambda *_args, **_kwargs: [public_target])
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
    head = full[: -len("ping")]

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
    assert accepts == []


def test_remote_receive_rejects_a_reply_token_when_tailscale_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepts: list[dict[str, object]] = []
    target = _receive_fixture(tmp_path, monkeypatch, identity=None, accepts=accepts)
    envelope, _sa, _sg, _body = _v2_envelope(target)
    response = receive_remote(tmp_path, envelope, OWNER)
    assert response["status"] == "PRE_EFFECT_REJECTED"
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
    assert all(
        set(payload)
        in (
            {"schema_version", "operation", "handle", "include_devin"},
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
