from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from cross_agent_chat.cli import mcp
from cross_agent_chat.core import ChatError
from cross_agent_chat.mcp_server import normalize_send_arguments


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
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "chat_peers", "arguments": {}},
        },
    ]
    monkeypatch.setenv("CROSS_AGENT_CHAT_PRESENCE", "off")
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("".join(f"{json.dumps(item)}\n" for item in requests))
    )

    mcp("codex", "studio", str(root))

    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "cross-agent-chat"
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    assert responses[2] == {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {"code": -32602, "message": "Cross Agent Chat presence is disabled"},
    }
    assert not root.exists()
