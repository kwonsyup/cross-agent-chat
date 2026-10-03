from __future__ import annotations

import io
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import cli
from cross_agent_chat.cli import mcp
from cross_agent_chat.core import ChatError, IntentStore, Registry, Route
from cross_agent_chat.mcp_server import normalize_send_arguments
from cross_agent_chat.runtime import Target


def _handshake(*, identifier: int = 0) -> list[dict[str, object]]:
    return [
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


def _feed(requests: Sequence[object]) -> io.StringIO:
    return io.StringIO("".join(f"{json.dumps(request)}\n" for request in requests))


@pytest.mark.parametrize("field", ["to", "recipient", "destination"])
def test_send_accepts_one_bounded_target_synonym(field: str) -> None:
    assert normalize_send_arguments({field: "codex@studio:api:123", "message": "hello"}) == (
        "codex@studio:api:123",
        "hello",
    )


def test_send_accepts_only_false_reply_hints() -> None:
    assert normalize_send_arguments(
        {"to": "codex@studio:api:123", "message": "hello", "wait_for_reply": False}
    ) == ("codex@studio:api:123", "hello")
    with pytest.raises(ChatError, match="blocking replies"):
        normalize_send_arguments(
            {"to": "codex@studio:api:123", "message": "hello", "request_reply": True}
        )


def test_send_rejects_multiple_targets_and_unknown_fields() -> None:
    with pytest.raises(ChatError, match="exactly one target"):
        normalize_send_arguments({"to": "one", "recipient": "two", "message": "hello"})
    with pytest.raises(ChatError, match="unknown field"):
        normalize_send_arguments({"to": "one", "message": "hello", "sender": "forged"})


def test_presence_off_mcp_initializes_without_tools_or_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "state"
    requests = [
        *_handshake(identifier=1),
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "chat_peers", "arguments": {}},
        },
    ]
    monkeypatch.setenv("CROSS_AGENT_CHAT_PRESENCE", "off")
    monkeypatch.setattr("sys.stdin", _feed(requests))

    mcp("codex", "studio", str(root))

    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "cross-agent-chat"
    assert responses[0]["result"]["instructions"] == cli.MCP_INSTRUCTIONS
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    assert responses[2] == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32602, "message": "Cross Agent Chat presence is disabled"},
    }
    assert not root.exists()


def test_mcp_status_requires_the_trusted_codex_thread_and_current_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "state"
    parent_pid = 4242
    session_id = str(uuid4())
    source = Route.create(
        provider="codex",
        session_id=session_id,
        device="studio",
        cwd=str(tmp_path),
        pid=parent_pid,
    )
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=parent_pid + 1,
    )
    Registry(root).upsert(source)
    event_id = IntentStore(root).begin(
        source, target, source_alias=source.alias, payload_digest="a" * 64
    )
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr("cross_agent_chat.cli.os.getppid", lambda: parent_pid)
    monkeypatch.setattr("cross_agent_chat.runtime._route_current", lambda *_args: True)
    requests = [
        *_handshake(),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "chat_status",
                "arguments": {"event_id": event_id},
                "_meta": {"threadId": session_id},
            },
        },
    ]
    monkeypatch.setattr("sys.stdin", _feed(requests))

    mcp("codex", "studio", str(root))

    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == {
        "chat_peers",
        "chat_send",
        "chat_status",
    }
    send_tool = next(
        tool for tool in responses[1]["result"]["tools"] if tool["name"] == "chat_send"
    )
    schema = send_tool["inputSchema"]
    assert set(schema["properties"]) == {"to", "message"}
    assert schema["required"] == ["to", "message"]
    assert "opaque handle" in schema["properties"]["to"]["description"]
    assert json.loads(responses[2]["result"]["content"][0]["text"])["event_id"] == event_id

    replacement = Route.create(
        provider="codex",
        session_id=session_id,
        device="studio",
        cwd=str(tmp_path),
        pid=parent_pid,
    )
    Registry(root).upsert(replacement)
    monkeypatch.setattr(
        "sys.stdin",
        _feed(
            [
                *_handshake(),
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "chat_status",
                        "arguments": {"event_id": event_id},
                        "_meta": {"threadId": session_id},
                    },
                },
            ]
        ),
    )

    mcp("codex", "studio", str(root))

    denied = json.loads(capsys.readouterr().out.splitlines()[-1])
    # The arguments were valid; the executed lookup failed, so this is a tool
    # result with isError, not a JSON-RPC protocol error.
    assert denied["result"]["isError"] is True
    assert denied["result"]["content"][0]["text"] == "event is unavailable"


def test_chat_send_target_description_does_not_demand_a_fresh_discovery_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The per-parameter text is what a model reads most closely.

    MCP_INSTRUCTIONS stopped requiring a `chat_peers` call before every send, but
    the `to` schema still said "Fresh opaque handle returned by chat_peers", which
    reimposed the ritual and hid the fact that an envelope's Reply handle is a
    valid target. Guidance has to agree with itself or the stricter line wins.
    """
    root = tmp_path / "state"
    requests = [
        *_handshake(identifier=1),
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    monkeypatch.setattr("sys.stdin", _feed(requests))

    mcp("codex", "studio", str(root))

    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    tools = {tool["name"]: tool for tool in responses[1]["result"]["tools"]}
    target = tools["chat_send"]["inputSchema"]["properties"]["to"]["description"]

    assert "Fresh" not in target
    assert "Reply handle" in target
    assert "route and protocol generation" in target
    # The stale claim that every upgrade strands a mixed-generation request was
    # narrowed to the pre-v0.4.0 reply-handle boundary, matching the README.
    assert "minted before v0.4.0 cannot be answered" in target
    assert "fresh sessions on every Mac" in target
    assert "mixed-generation request cannot be answered" not in target
    # The alias form `send()` already resolves must be stated truthfully.
    assert "exact full alias" in target
    assert "remote discovery is complete" in target
    # It must also steer away from the visible sender, which is the helper.
    assert "delivery helper" in target


def test_chat_send_result_says_how_the_answer_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "state"
    source = Route.create(
        provider="claude", session_id=str(uuid4()), device="studio", cwd=str(tmp_path), pid=1
    )
    sent: list[tuple[str, str]] = []

    def fake_send(_root: Path, _source: Route, target: str, message: str) -> dict[str, object]:
        sent.append((target, message))
        return {"schema_version": 1, "status": "TRANSPORT_ACCEPTED", "event_id": "e"}

    monkeypatch.setattr(cli, "authenticate_mcp_sender", lambda *_args: source)
    monkeypatch.setattr(cli, "send", fake_send)

    def fake_delivery(_root: Path, _source: Route) -> str:
        # Asked before the send, so a slow or failing check cannot follow an accepted one.
        assert sent == []
        return "next_turn"

    monkeypatch.setattr(cli, "reply_delivery", fake_delivery)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "chat_send", "arguments": {"to": "a" * 64, "message": "hi"}},
    }
    monkeypatch.setattr("sys.stdin", _feed([*_handshake(), request]))

    mcp("claude", "studio", str(root))

    response = json.loads(capsys.readouterr().out.splitlines()[-1])
    result = json.loads(response["result"]["content"][0]["text"])
    assert sent == [("a" * 64, "hi")]
    assert result["reply_delivery"] == "next_turn"
    assert result["status"] == "TRANSPORT_ACCEPTED"


def test_instructions_separate_first_install_from_upgrade() -> None:
    # A session that predates CAC's first install has no CAC tools at all;
    # only an upgrade leaves older tools loaded. Collapsing the two told a
    # fresh session it was "running older tools" that do not exist.
    assert "before a CAC install has no CAC tools" in cli.MCP_INSTRUCTIONS
    assert "before a CAC upgrade keeps its older loaded tools" in cli.MCP_INSTRUCTIONS
    assert "install or upgrade runs its older tools" not in cli.MCP_INSTRUCTIONS


def test_instructions_stop_senders_waiting_and_explain_both_return_paths() -> None:
    # A Codex CLI requester slept and polled chat_status for eleven minutes; an
    # unconditional "finish your turn" would instead strand Stop-bound answers.
    assert "do not sleep, wait, or poll" in cli.MCP_INSTRUCTIONS
    assert "while_idle" in cli.MCP_INSTRUCTIONS
    assert "next_turn" in cli.MCP_INSTRUCTIONS
    assert "when your current turn ends or your next prompt starts" in cli.MCP_INSTRUCTIONS


def test_instructions_describe_reply_delivery_as_the_sender_return_path() -> None:
    # A dogfood agent read reply_delivery as the destination's delivery state;
    # it only ever describes how an answer comes back to this sender.
    assert (
        "this sending session's own return path, not the recipient's state or activity"
        in cli.MCP_INSTRUCTIONS
    )


def _discovered_target(alias: str, handle_char: str, title: str | None = None) -> Target:
    return Target(
        alias=alias,
        provider="claude",
        device="imac",
        project="proj",
        generation=str(uuid4()),
        session_key=handle_char * 64,
        remote=False,
        title=title,
    )


def _stub_peer_discovery(monkeypatch: pytest.MonkeyPatch, targets: list[Target]) -> None:
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda *_args, **_kwargs: targets)
    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_identity", lambda: None)
    monkeypatch.setattr(
        "cross_agent_chat.runtime._with_codex_titles",
        lambda _root, found, _deadline: found,
    )


def _chat_peers_call(identifier: int, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {"name": "chat_peers", "arguments": arguments},
    }


def _tool_result_payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    payload: object = json.loads(result["content"][0]["text"])
    assert isinstance(payload, dict)
    return payload


def test_chat_peers_query_filters_alias_and_title_and_reports_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A filtered listing must narrow the same discovery result, not probe less."""
    root = tmp_path / "state"
    targets = [
        _discovered_target("claude@imac:Projects:W_Kluro_2Oct1PM", "a"),
        _discovered_target("codex@imac:kwonsyup:662c7d4d031e", "b", title="E_Kluro_2Oct1PM"),
        _discovered_target("claude@imac:Projects:W_Other", "c"),
    ]
    _stub_peer_discovery(monkeypatch, targets)
    requests = [
        *_handshake(),
        _chat_peers_call(1, {}),
        _chat_peers_call(2, {"query": "kluro"}),
        _chat_peers_call(3, {"query": "e_kluro"}),
    ]
    monkeypatch.setattr("sys.stdin", _feed(requests))

    mcp("claude", "studio", str(root))

    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    unfiltered = _tool_result_payload(responses[1])
    # The no-argument response keeps its exact prior shape: no filter key.
    assert set(unfiltered) == {"schema_version", "peers", "remote_discovery", "sender"}
    unfiltered_peers = unfiltered["peers"]
    assert isinstance(unfiltered_peers, list)
    assert len(unfiltered_peers) == 3

    by_alias = _tool_result_payload(responses[2])
    assert by_alias["filter"] == {"query": "kluro", "matched": 2, "of": 3}
    by_alias_peers = by_alias["peers"]
    assert isinstance(by_alias_peers, list)
    assert {peer["alias"] for peer in by_alias_peers} == {
        "claude@imac:Projects:W_Kluro_2Oct1PM",
        "codex@imac:kwonsyup:662c7d4d031e",
    }
    # Filtering never drops the opaque handle needed to address the peer.
    assert all(isinstance(peer["handle"], str) for peer in by_alias_peers)

    by_title = _tool_result_payload(responses[3])
    assert by_title["filter"] == {"query": "e_kluro", "matched": 1, "of": 3}
    by_title_peers = by_title["peers"]
    assert isinstance(by_title_peers, list)
    assert [peer["alias"] for peer in by_title_peers] == ["codex@imac:kwonsyup:662c7d4d031e"]


def test_chat_peers_rejects_malformed_query_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "state"
    _stub_peer_discovery(monkeypatch, [])
    requests = [
        *_handshake(),
        _chat_peers_call(1, {"bogus": "x"}),
        _chat_peers_call(2, {"query": 5}),
        _chat_peers_call(3, {"query": "bad\x00query"}),
        _chat_peers_call(4, {"query": "x" * 200}),
    ]
    monkeypatch.setattr("sys.stdin", _feed(requests))

    mcp("claude", "studio", str(root))

    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    # Argument-shape failures stay JSON-RPC errors; content failures that pass
    # the shape check are operation refusals (isError), like event_status.
    assert responses[1]["error"]["code"] == -32602
    assert responses[2]["error"]["code"] == -32602
    assert responses[3]["result"]["isError"] is True
    assert responses[3]["result"]["content"][0]["text"] == "peer query is invalid"
    assert responses[4]["result"]["isError"] is True


def test_chat_peers_rejects_a_malformed_query_before_any_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed public argument must be refused before local or remote work.

    The query only narrows an already-collected listing, so a bad value used to
    still pay the full local probe and remote discovery. The refusal has to run
    first: any recorded probe here means the malformed call reached discovery.
    """
    root = tmp_path / "state"
    probed: list[str] = []

    def recorded_local(*_args: object, **_kwargs: object) -> list[Target]:
        probed.append("local")
        return []

    def recorded_identity() -> None:
        probed.append("tailnet")
        return None

    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", recorded_local)
    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_identity", recorded_identity)
    monkeypatch.setattr(
        "sys.stdin", _feed([*_handshake(), _chat_peers_call(1, {"query": "bad\x00query"})])
    )

    mcp("claude", "studio", str(root))

    response = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "peer query is invalid"
    assert probed == []

    # A well-formed query still reaches discovery and reports its filter scope.
    monkeypatch.setattr(
        "sys.stdin", _feed([*_handshake(), _chat_peers_call(2, {"query": "kluro"})])
    )
    mcp("claude", "studio", str(root))

    filtered = _tool_result_payload(json.loads(capsys.readouterr().out.splitlines()[-1]))
    assert filtered["filter"] == {"query": "kluro", "matched": 0, "of": 0}
    assert probed.count("local") == 1


def _stub_alias_send(monkeypatch: pytest.MonkeyPatch, targets: list[Target]) -> list[Target]:
    resolved: list[Target] = []
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda *_args, **_kwargs: targets)
    monkeypatch.setattr("cross_agent_chat.runtime._remote_discovery", lambda **_kwargs: ([], True))

    def fake_local_send(
        _root: Path, _source: Route, target: Target, _message: str, *, deadline: float
    ) -> dict[str, object]:
        resolved.append(target)
        return {
            "schema_version": 1,
            "event_id": "e",
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": target.provider,
        }

    monkeypatch.setattr("cross_agent_chat.runtime._send_local_target", fake_local_send)
    return resolved


def test_chat_send_resolves_a_unique_exact_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """send() already resolves a unique alias; the MCP path must expose it."""
    root = tmp_path / "state"
    source = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=1,
    )
    target = _discovered_target("claude@imac:Projects:W_Kluro_2Oct1PM", "d")
    resolved = _stub_alias_send(monkeypatch, [target])
    monkeypatch.setattr(cli, "authenticate_mcp_sender", lambda *_args: source)
    monkeypatch.setattr(cli, "reply_delivery", lambda *_args: "unknown")
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "chat_send",
            # Case-insensitive: the stored alias differs in case from the query.
            "arguments": {
                "to": "CLAUDE@IMAC:PROJECTS:W_KLURO_2OCT1PM",
                "message": "hi",
            },
        },
    }
    monkeypatch.setattr("sys.stdin", _feed([*_handshake(), request]))

    mcp("claude", "studio", str(root))

    response = json.loads(capsys.readouterr().out.splitlines()[-1])
    result = _tool_result_payload(response)
    assert resolved == [target]
    # The human-readable recipient is surfaced alongside the effect result.
    assert result["to"] == "claude@imac:Projects:W_Kluro_2Oct1PM"
    assert result["status"] == "TRANSPORT_ACCEPTED"


def test_chat_send_refuses_an_ambiguous_alias_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "state"
    source = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=1,
    )
    same_alias = "claude@imac:Projects:W_Kluro_2Oct1PM"
    resolved = _stub_alias_send(
        monkeypatch,
        [_discovered_target(same_alias, "d"), _discovered_target(same_alias, "e")],
    )
    monkeypatch.setattr(cli, "authenticate_mcp_sender", lambda *_args: source)
    monkeypatch.setattr(cli, "reply_delivery", lambda *_args: "unknown")
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "chat_send",
            "arguments": {"to": same_alias, "message": "hi"},
        },
    }
    monkeypatch.setattr("sys.stdin", _feed([*_handshake(), request]))

    mcp("claude", "studio", str(root))

    response = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "target is ambiguous or unavailable"
    assert resolved == []


def test_chat_send_refuses_a_fuzzy_only_alias_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reported hazard: `...:Kluro` must not fuzzy-land on the W_ twin."""
    root = tmp_path / "state"
    source = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=1,
    )
    target = _discovered_target("claude@imac:Projects:W_Kluro_2Oct1PM", "d")
    resolved = _stub_alias_send(monkeypatch, [target])
    monkeypatch.setattr(cli, "authenticate_mcp_sender", lambda *_args: source)
    monkeypatch.setattr(cli, "reply_delivery", lambda *_args: "unknown")
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "chat_send",
            # A near alias that only fuzzy-matches the discovered peer.
            "arguments": {"to": "claude@imac:Projects:Kluro", "message": "hi"},
        },
    }
    monkeypatch.setattr("sys.stdin", _feed([*_handshake(), request]))

    mcp("claude", "studio", str(root))

    response = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == (
        "recipient is not an exact handle or exact alias of one discovered peer; "
        "call chat_peers and choose the recipient"
    )
    assert resolved == []
