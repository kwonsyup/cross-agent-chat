from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat.claude_runtime import (
    AGENTS_TIMEOUT_SECONDS,
    DISCOVERY_TIMEOUT_SECONDS,
    SEND_TIMEOUT_SECONDS,
    ClaudeSendMessageUnknownDelivery,
    ClaudeUnknownPhase,
    claude_binary,
    courier_environment,
    parse_claude_agents,
    parse_sendmessage_receipt,
    pretool_decision,
    run_pretool_gate,
    sendmessage,
)
from cross_agent_chat.core import (
    ChatError,
    IntentStore,
    Registry,
    Route,
    UnknownDeliveryError,
    session_key,
)
from cross_agent_chat.remote import parse_remote_envelope
from cross_agent_chat.runtime import (
    ACCEPT_TIMEOUT_SECONDS,
    REMOTE_TIMEOUT_SECONDS,
    Target,
    _send_local_target,
    courier_accept,
    unknown_delivery_diagnostic,
)


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


def test_claude_agents_skip_an_unrelated_disappeared_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import claude_runtime

    healthy_session = str(uuid4())
    stale_session = str(uuid4())
    stale_workspace = tmp_path / "disappeared"
    payload = json.dumps(
        [
            {
                "sessionId": healthy_session,
                "name": "Healthy session",
                "kind": "interactive",
                "cwd": str(tmp_path),
            },
            {
                "sessionId": stale_session,
                "name": "Stale session",
                "kind": "interactive",
                "cwd": str(stale_workspace),
            },
        ]
    )

    agents = parse_claude_agents(payload)

    assert agents == [
        {
            "session_id": healthy_session,
            "name": "Healthy session",
            "kind": "interactive",
            "cwd": str(tmp_path.resolve()),
        }
    ]
    monkeypatch.setattr(claude_runtime, "claude_agents", lambda: agents)
    with pytest.raises(ChatError, match="exact live supported"):
        claude_runtime.exact_agent(stale_session, str(stale_workspace))


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


def test_claude_binary_keeps_the_recipient_bound_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "profile-a-claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    monkeypatch.setenv("CROSS_AGENT_CHAT_CLAUDE_BINARY", str(binary))
    monkeypatch.setattr("cross_agent_chat.claude_runtime.shutil.which", lambda _: None)

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
    tool_input["content"] = "provider-rendered preview"
    assert pretool_decision(expected, payload, key.hex())
    tool_input["extra"] = "reject"
    assert not pretool_decision(expected, payload, key.hex())
    tool_input.pop("extra")
    tool_input["content"] = 7
    assert not pretool_decision(expected, payload, key.hex())
    tool_input["content"] = "provider-rendered preview"
    tool_input["message"] = "changed"
    assert not pretool_decision(expected, payload, key.hex())


def test_pretool_gate_uses_full_unicode_body_not_cosmetic_preview() -> None:
    key = bytes.fromhex("22" * 32)
    message = "e\u0301 family 👨‍👩‍👧‍👦 " + "wide界" * 20
    recipient = "API work [ABC123]"
    expected: dict[str, object] = {
        "recipient": recipient,
        "message_hmac": hmac.new(key, message.encode(), hashlib.sha256).hexdigest(),
    }
    tool_input: dict[str, object] = {
        "to": recipient,
        "recipient": recipient,
        "message": message,
        "content": "provider preview with different display width",
        "type": "message",
        "summary": "Cross Agent Chat",
    }
    payload: dict[str, object] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "SendMessage",
        "tool_input": tool_input,
    }

    assert pretool_decision(expected, payload, key.hex())
    tool_input["message"] = message[:-1] + "x"
    assert not pretool_decision(expected, payload, key.hex())


def test_pretool_gate_denies_an_unpaired_surrogate_without_consuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "33" * 32
    recipient = "API work [ABC123]"
    expected = tmp_path / "expected.json"
    expected.write_text(
        json.dumps({"recipient": recipient, "message_hmac": "0" * 64}), encoding="utf-8"
    )
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "SendMessage",
        "tool_input": {
            "to": recipient,
            "recipient": recipient,
            "message": "\ud800",
            "content": "preview",
            "type": "message",
            "summary": "Cross Agent Chat",
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert not run_pretool_gate(str(expected), key)
    assert not (tmp_path / "consumed").exists()
    response = json.loads(capsys.readouterr().out)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"


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
        "input": {
            "to": target,
            "recipient": target,
            "message": message,
            "content": "provider-rendered preview",
            "type": "message",
            "summary": "Cross Agent Chat",
        },
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


def test_sendmessage_receipt_rejects_full_body_alteration_despite_same_preview() -> None:
    message_id = str(uuid4())
    target = "API work [ABC123]"
    use = {
        "type": "tool_use",
        "id": "tool-1",
        "name": "SendMessage",
        "input": {
            "to": target,
            "recipient": target,
            "message": "altered full body",
            "content": "same preview",
            "type": "message",
            "summary": "Cross Agent Chat",
        },
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

    with pytest.raises(ChatError, match="receipt"):
        parse_sendmessage_receipt(stream, target, "original full body")


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
            "input": {
                "to": "API work [ABC123]",
                "recipient": "API work [ABC123]",
                "message": "hello",
                "content": "provider-rendered preview",
                "type": "message",
                "summary": "Cross Agent Chat",
            },
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


@pytest.mark.parametrize(
    ("phase", "diagnostic"),
    [
        ("pretool_gate_unobserved", "claude_pretool_gate_unobserved"),
        ("pretool_gate_unreadable", "claude_pretool_gate_unreadable"),
        ("helper_timeout", "claude_helper_timeout"),
        ("helper_execution_failed", "claude_helper_execution_failed"),
        ("helper_exit_nonzero", "claude_helper_exit_nonzero"),
        ("receipt_invalid", "claude_receipt_invalid"),
    ],
)
def test_claude_unknown_phase_is_body_free_and_exact_response_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: ClaudeUnknownPhase, diagnostic: str
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
        lambda *_: (_ for _ in ()).throw(ClaudeSendMessageUnknownDelivery(phase)),
    )
    event_id = str(uuid4())

    response = courier_accept(route, None, event_id, "hello")

    assert response == {
        "schema_version": 1,
        "event_id": event_id,
        "status": "UNKNOWN_DELIVERY",
        "provider": "claude",
        "diagnostic": diagnostic,
    }
    assert unknown_delivery_diagnostic(response, event_id, "claude") == diagnostic
    assert unknown_delivery_diagnostic({**response, "extra": "reject"}, event_id, "claude") is None
    assert unknown_delivery_diagnostic(response, event_id, "codex") is None
    assert unknown_delivery_diagnostic({**response, "diagnostic": []}, event_id, "claude") is None
    assert unknown_delivery_diagnostic({**response, "diagnostic": {}}, event_id, "claude") is None


@pytest.mark.parametrize(
    ("response_kind", "includes_phase"),
    [
        ("exact", True),
        ("extra", False),
        ("non_claude", False),
    ],
)
def test_local_claude_diagnostic_marks_one_unknown_without_body_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_kind: str,
    includes_phase: bool,
) -> None:
    root = tmp_path / "state"
    source = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    target_route = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(root).upsert(source)
    Registry(root).upsert(target_route)
    target = Target(
        alias=target_route.alias,
        provider="claude",
        device=target_route.device,
        project=target_route.project,
        generation=target_route.generation,
        session_key=session_key("claude", target_route.session_id),
        remote=False,
        session_id=target_route.session_id,
        cwd=target_route.cwd,
        pid=target_route.pid,
    )
    monkeypatch.setattr("cross_agent_chat.runtime.canonical_source_alias", lambda *_: source.alias)

    def response(_path: Path, payload: dict[str, object], **_: object) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "UNKNOWN_DELIVERY",
            "provider": "claude",
            "diagnostic": "claude_receipt_invalid",
        }
        if response_kind == "extra":
            value["extra"] = "reject"
        if response_kind == "non_claude":
            value["provider"] = "codex"
        return value

    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", response)

    with pytest.raises(UnknownDeliveryError) as error:
        _send_local_target(
            root,
            source,
            target,
            "private message body",
            deadline=time.monotonic() + 1,
        )

    rendered = str(error.value)
    assert "private message body" not in rendered
    assert ("claude_receipt_invalid" in rendered) is includes_phase
    intents = IntentStore(root).intents()
    assert len(intents) == 1
    assert intents[0].status == "UNKNOWN_DELIVERY"


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
        "name": "a" * 70 + "\x01",
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


@pytest.mark.parametrize("kind", ["interactive", "background"])
def test_supported_claude_kind_preserves_exact_native_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from cross_agent_chat import claude_runtime

    session_id = str(uuid4())
    agent = {"session_id": session_id, "name": "Exact target", "kind": kind, "cwd": str(tmp_path)}
    monkeypatch.setattr(claude_runtime, "claude_agents", lambda: [agent])
    assert claude_runtime.exact_agent(session_id, str(tmp_path)) == agent
    with pytest.raises(ChatError):
        claude_runtime.exact_agent(str(uuid4()), str(tmp_path))
    listing_kind = "bg" if kind == "background" else kind
    listing = f"  Exact target [ABC123]  ·  {listing_kind}  ·  idle"
    record = json.dumps({"tool_use_result": {"listing": listing}})
    monkeypatch.setattr(claude_runtime, "claude_binary", lambda: Path("/opt/claude"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, record, ""),
    )
    assert claude_runtime.discover_target_ref("Exact target") == "Exact target [ABC123]"
    with pytest.raises(ChatError):
        claude_runtime.discover_target_ref("Wrong target")


def test_long_claude_alias_retains_exact_session_disambiguation() -> None:
    from cross_agent_chat.claude_runtime import ClaudeAgent, claude_alias

    a: ClaudeAgent = {
        "session_id": str(uuid4()),
        "name": "이름" * 50,
        "kind": "interactive",
        "cwd": "/tmp",
    }
    b: ClaudeAgent = {**a, "session_id": str(uuid4())}
    first = claude_alias("device", "p" * 110, a)
    second = claude_alias("device", "p" * 110, b)
    assert len(first) <= 128 and len(second) <= 128
    assert first != second


def test_claude_helper_keeps_selected_endpoint_and_backend_context() -> None:
    selected = {
        "CLAUDE_CONFIG_DIR": "/profiles/a",
        "ANTHROPIC_BASE_URL": "https://gateway.example.invalid",
        "ANTHROPIC_AUTH_TOKEN": "synthetic-helper-token",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_PROFILE": "selected-profile",
        "AWS_SESSION_TOKEN": "synthetic-session-token",
    }
    assert courier_environment({**selected, "UNRELATED_SECRET": "do-not-inherit"}) == selected
