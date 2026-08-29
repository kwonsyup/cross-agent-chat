from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat.claude_runtime import (
    AGENTS_TIMEOUT_SECONDS,
    DISCOVERY_TIMEOUT_SECONDS,
    SEND_TIMEOUT_SECONDS,
    claude_binary,
    courier_environment,
    parse_claude_agents,
    parse_sendmessage_receipt,
    pretool_decision,
    sendmessage,
)
from cross_agent_chat.core import ChatError, Route, UnknownDeliveryError
from cross_agent_chat.remote import parse_remote_envelope
from cross_agent_chat.runtime import ACCEPT_TIMEOUT_SECONDS, REMOTE_TIMEOUT_SECONDS, courier_accept


def test_remote_transport_outlives_claude_delivery_window() -> None:
    assert ACCEPT_TIMEOUT_SECONDS >= (
        2 * AGENTS_TIMEOUT_SECONDS + DISCOVERY_TIMEOUT_SECONDS + SEND_TIMEOUT_SECONDS
    )
    assert REMOTE_TIMEOUT_SECONDS > ACCEPT_TIMEOUT_SECONDS


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


def test_sendmessage_without_gate_receipt_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "failed"),
    )
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )

    with pytest.raises(UnknownDeliveryError, match="unknown"):
        sendmessage("API work [ABC123]", "hello", Path("/usr/bin/false"))


def test_sendmessage_uncertainty_after_gate_consumption_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        (expected_path.parent / "consumed").write_text("consumed\n")
        return subprocess.CompletedProcess(command, 1, "", "receipt lost")

    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )

    with pytest.raises(UnknownDeliveryError, match="unknown"):
        sendmessage("API work [ABC123]", "hello", Path("/usr/bin/false"))


def test_sendmessage_gate_read_error_after_invocation_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr(Path, "read_bytes", lambda _: (_ for _ in ()).throw(OSError("EIO")))

    with pytest.raises(UnknownDeliveryError, match="unknown"):
        sendmessage("API work [ABC123]", "hello", Path("/usr/bin/false"))


def test_sendmessage_subprocess_error_after_gate_consumption_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        (expected_path.parent / "consumed").write_text("consumed\n")
        raise OSError("pipe failed after child execution")

    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )

    with pytest.raises(UnknownDeliveryError, match="unknown"):
        sendmessage("API work [ABC123]", "hello", Path("/usr/bin/false"))


def test_sendmessage_accepts_exact_success_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    message_id = str(uuid4())

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        (expected_path.parent / "consumed").write_text("consumed\n")
        use = {
            "type": "tool_use",
            "id": "tool-1",
            "name": "SendMessage",
            "input": {"recipient": "API work [ABC123]", "content": "hello"},
        }
        result = {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"success": True, "message": "sent", "msg_id": message_id}),
                }
            ],
        }
        stream = "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )

    sendmessage("API work [ABC123]", "hello", Path("/usr/bin/false"))


@pytest.mark.parametrize("failure", ["lookup", "discovery", "revalidation"])
def test_claude_lookup_discovery_and_revalidation_fail_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    route = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    event_id = str(uuid4())

    calls = 0

    def exact_agent(*_: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if failure == "lookup":
            raise ChatError("exact-agent failed")
        name = "API A" if calls == 1 or failure != "revalidation" else "API B"
        return {
            "session_id": route.session_id,
            "name": name,
            "kind": "interactive",
            "cwd": route.cwd,
        }

    def discover(*_: object) -> str:
        if failure == "discovery":
            raise ChatError("ListAgents failed")
        return "API A [ABC123]"

    monkeypatch.setattr("cross_agent_chat.runtime.exact_agent", exact_agent)
    monkeypatch.setattr("cross_agent_chat.runtime.discover_target_ref", discover)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.sendmessage",
        lambda *_: pytest.fail("SendMessage ran after a pre-effect failure"),
    )

    response = courier_accept(route, None, event_id, "hello")

    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["provider"] == "claude"


def test_claude_unknown_response_keeps_actual_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    agent = {
        "session_id": route.session_id,
        "name": "API A",
        "kind": "interactive",
        "cwd": route.cwd,
    }
    monkeypatch.setattr("cross_agent_chat.runtime.exact_agent", lambda *_: agent)
    monkeypatch.setattr("cross_agent_chat.runtime.discover_target_ref", lambda *_: "API A [ABC123]")
    monkeypatch.setattr(
        "cross_agent_chat.runtime.sendmessage",
        lambda *_: (_ for _ in ()).throw(UnknownDeliveryError("receipt lost")),
    )

    response = courier_accept(route, None, str(uuid4()), "hello")

    assert response["status"] == "UNKNOWN_DELIVERY"
    assert response["provider"] == "claude"


def test_claude_alias_is_validated_before_sendmessage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / ("p" * 70)
    project.mkdir()
    route = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(project),
        pid=os.getpid(),
    )
    agent = {
        "session_id": route.session_id,
        "name": "a" * 70,
        "kind": "interactive",
        "cwd": route.cwd,
    }
    monkeypatch.setattr("cross_agent_chat.runtime.exact_agent", lambda *_: agent)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.discover_target_ref", lambda *_: "Long name [ABC123]"
    )
    monkeypatch.setattr(
        "cross_agent_chat.runtime.sendmessage",
        lambda *_: pytest.fail("SendMessage ran before alias validation"),
    )

    response = courier_accept(route, None, str(uuid4()), "hello")

    assert response["status"] == "PRE_EFFECT_REJECTED"
    assert response["provider"] == "claude"
