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
    assert "fresh sessions" in target
    assert "mixed-generation request cannot be answered" in target
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


def test_instructions_stop_senders_waiting_and_explain_both_return_paths() -> None:
    # A Codex CLI requester slept and polled chat_status for eleven minutes; an
    # unconditional "finish your turn" would instead strand Stop-bound answers.
    assert "do not sleep, wait, or poll" in cli.MCP_INSTRUCTIONS
    assert "while_idle" in cli.MCP_INSTRUCTIONS
    assert "next_turn" in cli.MCP_INSTRUCTIONS
    assert "when your current turn ends or your next prompt starts" in cli.MCP_INSTRUCTIONS
