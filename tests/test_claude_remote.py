from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat.claude_runtime import (
    SEND_TIMEOUT_SECONDS,
    claude_binary,
    courier_environment,
    parse_claude_agents,
    parse_sendmessage_receipt,
    pretool_decision,
)
from cross_agent_chat.core import ChatError
from cross_agent_chat.remote import parse_remote_envelope
from cross_agent_chat.runtime import REMOTE_TIMEOUT_SECONDS


def test_remote_transport_outlives_claude_delivery_window() -> None:
    assert REMOTE_TIMEOUT_SECONDS > SEND_TIMEOUT_SECONDS


def test_claude_courier_preserves_session_auth_without_unrelated_secrets() -> None:
    environment = courier_environment(
        {
            "HOME": "/Users/example",
            "PATH": "/usr/bin:/bin",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
            "ANTHROPIC_API_KEY": "api-secret",
            "CLAUDECODE": "1",
            "GITHUB_TOKEN": "unrelated-secret",
        }
    )

    assert environment == {
        "HOME": "/Users/example",
        "PATH": "/usr/bin:/bin",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
        "ANTHROPIC_API_KEY": "api-secret",
    }


def test_claude_agents_requires_exact_interactive_identity(tmp_path: Path) -> None:
    session_id = str(uuid4())
    payload = json.dumps(
        [
            {
                "sessionId": session_id,
                "name": "API work",
                "kind": "interactive",
                "cwd": str(tmp_path),
                "status": "busy",
            }
        ]
    )

    agents = parse_claude_agents(payload)

    assert agents == [
        {
            "session_id": session_id,
            "name": "API work",
            "kind": "interactive",
            "cwd": str(tmp_path.resolve()),
        }
    ]


def test_claude_binary_uses_fixed_user_local_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / ".local" / "bin" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    monkeypatch.setattr("cross_agent_chat.claude_runtime.shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert claude_binary() == binary.resolve()


def test_pretool_gate_binds_recipient_and_full_message() -> None:
    key = bytes.fromhex("11" * 32)
    message = "one exact body"
    recipient = "API work [ABC123]"
    expected: dict[str, object] = {
        "recipient": recipient,
        "message_hmac": hmac.new(key, message.encode(), hashlib.sha256).hexdigest(),
    }
    tool_input: dict[str, object] = {
        "to": recipient,
        "recipient": recipient,
        "message": message,
        "content": message,
        "type": "message",
        "summary": "Cross Agent Chat",
    }
    payload: dict[str, object] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "SendMessage",
        "tool_input": tool_input,
    }

    assert pretool_decision(expected, payload, key.hex())
    tool_input["message"] = "changed"
    assert not pretool_decision(expected, payload, key.hex())


def test_remote_envelope_is_exact_and_generation_bound() -> None:
    event_id = str(uuid4())
    generation = str(uuid4())
    raw = json.dumps(
        {
            "schema_version": 1,
            "event_id": event_id,
            "source_alias": "codex@source:api:456",
            "source_generation": str(uuid4()),
            "target_alias": "codex@peer:api:123",
            "generation": generation,
            "message": "hello",
        }
    )

    parsed = parse_remote_envelope(raw)
    assert parsed[0] == event_id
    assert parsed[1] == "codex@source:api:456"
    assert parsed[3:] == ("codex@peer:api:123", generation, "hello")
    with pytest.raises(ChatError, match="envelope"):
        parse_remote_envelope(raw[:-1] + ', "extra": true}')


def test_sendmessage_receipt_requires_exact_success_contract() -> None:
    tool_id = "tool-1"
    message_id = str(uuid4())
    target = "API work [ABC123]"
    message = "hello"
    use = {
        "type": "tool_use",
        "id": tool_id,
        "name": "SendMessage",
        "input": {"recipient": target, "content": message},
    }
    result: dict[str, object] = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": [
            {
                "type": "text",
                "text": json.dumps({"success": True, "message": "sent", "msg_id": message_id}),
            }
        ],
    }
    stream = "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))

    assert parse_sendmessage_receipt(stream, target, message) == message_id

    result["is_error"] = True
    rejected = "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))
    with pytest.raises(ChatError, match="receipt"):
        parse_sendmessage_receipt(rejected, target, message)
