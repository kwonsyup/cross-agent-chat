"""Peer display labels: same-cwd Devin sessions stay distinguishable.

Devin exposes no provider title, so nine sessions in one directory listed as
nine identical ``devin@device:/`` rows and no lane could self-identify. The
fallback label is derived from the exact session key and emitted through the
existing negotiated ``title`` field: a display hint only -- never a selector,
never authority, and a truncated-digest collision stays ambiguous because
exact handles and generation checks remain the only selection path.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import (
    CODEX_ALIAS_DIGEST_LENGTH,
    ChatError,
    Registry,
    Route,
    session_key,
)
from cross_agent_chat.recipient import local_token, parse_recipient_token


def _devin_routes(root: Path, cwd: Path, count: int) -> list[Route]:
    cwd.mkdir(parents=True, exist_ok=True)
    routes = [
        Route.create(
            provider="devin",
            session_id=f"devin-{uuid4()}",
            device="studio",
            cwd=str(cwd),
            pid=os.getpid(),
        )
        for _ in range(count)
    ]
    for route in routes:
        Registry(root).upsert(route)
    return routes


def _courier_stub(monkeypatch: pytest.MonkeyPatch, routes: list[Route]) -> None:
    """Answer every fixture route's health probe with its own exact alias."""
    by_generation = {route.generation: route for route in routes}

    def courier(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        route = by_generation.get(str(payload["generation"]))
        if route is None:
            raise ChatError("unknown fixture route")
        assert payload["operation"] == "health"
        return {
            "schema_version": 1,
            "status": "READY",
            "generation": route.generation,
            "alias": route.alias,
        }

    monkeypatch.setattr(runtime, "request_socket", courier)


def _expected_title(route: Route) -> str:
    return f"session {session_key('devin', route.session_id)[:CODEX_ALIAS_DIGEST_LENGTH]}"


def test_nine_same_cwd_devin_peers_list_distinct_display_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field failure: identical rows a peer could never tell apart."""
    root = tmp_path / "state"
    routes = _devin_routes(root, tmp_path / "shared", 9)
    _courier_stub(monkeypatch, routes)

    listing = runtime.peers(root, include_remote=False)
    items = cast(list[dict[str, str]], listing["peers"])
    assert len(items) == 9
    # The aliases stay identical: the label does the distinguishing, and the
    # exact handle remains the only authority a selection can trust.
    assert {item["alias"] for item in items} == {routes[0].alias}
    assert [item["title"] for item in items] == [
        _expected_title(route)
        for route in sorted(routes, key=lambda route: session_key("devin", route.session_id))
    ]
    by_key = {session_key("devin", route.session_id): route for route in routes}
    for item in items:
        token = parse_recipient_token(item["handle"])
        assert token is not None
        assert token.handle in by_key
        assert token.generation == by_key[token.handle].generation


def test_devin_sender_readiness_matches_its_own_roster_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An authenticated Devin sender can find its own row among identical peers."""
    root = tmp_path / "state"
    routes = _devin_routes(root, tmp_path / "shared", 9)
    _courier_stub(monkeypatch, routes)
    source = routes[4]

    readiness = runtime.sender_readiness_for_route(root, source)
    assert readiness == {
        "status": "ready",
        "title": _expected_title(source),
        "provider": source.provider,
        "alias": source.alias,
        "handle": local_token(
            root, session_key(source.provider, source.session_id), source.generation
        ),
    }

    listing = runtime.peers(root, include_remote=False)
    items = cast(list[dict[str, str]], listing["peers"])
    matches = [item for item in items if item.get("title") == readiness["title"]]
    assert len(matches) == 1
    token = parse_recipient_token(matches[0]["handle"])
    assert token is not None
    assert token.handle == session_key("devin", source.session_id)
    assert token.generation == source.generation


def test_internal_title_disabled_listing_keeps_the_exact_old_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old broker readers must see no new key until they ask for titles."""
    root = tmp_path / "state"
    routes = _devin_routes(root, tmp_path / "shared", 2)
    _courier_stub(monkeypatch, routes)

    internal = runtime.peers(root, include_remote=False, internal=True)
    items = cast(list[dict[str, str]], internal["peers"])
    assert len(items) == 2
    for item in items:
        assert "title" not in item
        assert "handle" not in item
        assert set(item) == {
            "alias",
            "provider",
            "device",
            "project",
            "status",
            "generation",
            "session_key",
        }

    titled = runtime.peers(root, include_remote=False, internal=True, include_title=True)
    assert [item["title"] for item in cast(list[dict[str, str]], titled["peers"])] == [
        _expected_title(route)
        for route in sorted(routes, key=lambda route: session_key("devin", route.session_id))
    ]


def test_identical_devin_aliases_stay_unselectable_and_tokens_stay_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A display label never becomes a selector; a stale generation never sends."""
    root = tmp_path / "state"
    routes = _devin_routes(root, tmp_path / "shared", 2)
    _courier_stub(monkeypatch, routes)
    source = routes[0]
    target = routes[1]

    targets = runtime.local_targets(root)
    with pytest.raises(ChatError, match="ambiguous"):
        runtime.resolve_target(targets, target.alias)

    token = parse_recipient_token(
        local_token(root, session_key("devin", target.session_id), target.generation)
    )
    assert token is not None
    assert token.handle == session_key("devin", target.session_id)
    assert token.generation == target.generation

    # A successor registration rotates the generation; the pinned token must
    # keep refusing rather than silently delivering to the replacement.
    successor = Route.create(
        provider="devin",
        session_id=target.session_id,
        device="studio",
        cwd=target.cwd,
        pid=target.pid,
    )
    Registry(root).upsert(successor)
    _courier_stub(monkeypatch, [*routes, successor])
    with pytest.raises(ChatError, match="unavailable or changed"):
        runtime._send_local_token_target(
            root,
            source,
            token,
            "must not reach the successor",
            deadline=time.monotonic() + 5.0,
        )


def test_human_peers_rows_distinguish_same_alias_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two live Devin sessions sharing one alias must read as different rows."""
    from cross_agent_chat import cli

    listing: dict[str, object] = {
        "schema_version": 1,
        "remote_discovery": "not_requested",
        "peers": [
            {
                "alias": "devin@studio:/",
                "status": "available",
                "handle": "cac.local.a" + "1" * 40,
                "title": "session aaaabbbbcccc",
                "delivery_mode": "devin_tool_boundary",
                "current_boundary": "next_prompt_custom_subagent",
            },
            {
                "alias": "devin@studio:/",
                "status": "available",
                "handle": "cac.local.b" + "2" * 40,
                "title": "session ddddeeeeffff",
                "delivery_mode": "devin_stop_or_prompt_bound",
            },
        ],
    }
    requested: dict[str, object] = {}

    def fake_peers(*_args: object, **kwargs: object) -> dict[str, object]:
        requested.update(kwargs)
        return listing

    monkeypatch.setattr(cli, "peers", fake_peers)
    assert cli.run(cli.parser().parse_args(["peers", "--local-only"])) == 0
    rows = capsys.readouterr().out.splitlines()
    assert len(rows) == 2
    assert rows[0] != rows[1]
    assert rows[0].split("\t") == [
        "devin@studio:/",
        "available",
        "session aaaabbbbcccc",
        "devin_tool_boundary",
        "next_prompt_custom_subagent",
    ]
    assert rows[1].split("\t") == [
        "devin@studio:/",
        "available",
        "session ddddeeeeffff",
        "devin_stop_or_prompt_bound",
        "-",
    ]
    # Human rows request the receiving metadata; they never become selectors.
    assert requested["include_delivery_mode"] is True
    assert requested["include_delivery_mechanism"] is True


def test_peers_json_keeps_the_exact_handle_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json output must not grow display fields or new request metadata."""
    from cross_agent_chat import cli

    row = {
        "alias": "devin@studio:/",
        "status": "available",
        "handle": "cac.local.a" + "1" * 40,
    }
    requested: dict[str, object] = {}

    def fake_peers(*_args: object, **kwargs: object) -> dict[str, object]:
        requested.update(kwargs)
        return {"schema_version": 1, "remote_discovery": "not_requested", "peers": [row]}

    monkeypatch.setattr(cli, "peers", fake_peers)
    assert cli.run(cli.parser().parse_args(["peers", "--local-only", "--json"])) == 0
    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert cast(list[dict[str, str]], payload["peers"]) == [row]
    assert "include_delivery_mode" not in requested
    assert "include_delivery_mechanism" not in requested


def test_a_malformed_peers_query_is_refused_before_any_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shape validation precedes probes: a bad query discovers nothing."""
    root = tmp_path / "state"
    probed: list[str] = []

    def local_targets(*_args: object, **_kwargs: object) -> list[object]:
        probed.append("local")
        return []

    def tailnet_identity() -> None:
        probed.append("tailnet")

    monkeypatch.setattr(runtime, "local_targets", local_targets)
    monkeypatch.setattr(runtime, "tailnet_identity", tailnet_identity)

    with pytest.raises(ChatError, match="peer query is invalid"):
        runtime.peers(root, query="bad\x00query")
    assert probed == []

    result = runtime.peers(root, include_remote=False, query="still nothing")
    assert result["filter"] == {"query": "still nothing", "matched": 0, "of": 0}
    assert probed == ["local"]
