from __future__ import annotations

import io
import json
import os
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pytest

from cross_agent_chat.claude_runtime import (
    AGENTS_TIMEOUT_SECONDS,
    COURIER_PLACEHOLDER_MESSAGE,
    COURIER_PLACEHOLDER_TARGET,
    COURIER_SESSION_DIRECTORY,
    DENIAL_MARKERS,
    DISCOVERY_TIMEOUT_SECONDS,
    SEND_SUMMARY,
    SEND_TIMEOUT_SECONDS,
    ClaudeSendMessageRefused,
    ClaudeSendMessageUnknownDelivery,
    ClaudeUnknownPhase,
    _listing_target_refs,
    _parse_targeted_claude_agents,
    _pretool_denial,
    _pretool_resolution,
    _tool_records,
    authoritative_tool_input,
    claude_binary,
    courier_environment,
    discover_target_ref,
    gate_consumed,
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


def _write_private_marker(path: Path, value: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _sendmessage_stream(
    target: str,
    message: str,
    *,
    tool_name: str = "SendMessage",
    tool_input: dict[str, object] | None = None,
    extra_uses: int = 0,
) -> str:
    input_value = tool_input or {
        "to": target,
        "recipient": target,
        "message": message,
        "content": "provider preview",
        "type": "message",
        "summary": "Cross Agent Chat",
    }
    uses = [
        {
            "type": "tool_use",
            "id": f"tool-{index}",
            "name": tool_name,
            "input": input_value,
        }
        for index in range(extra_uses + 1)
    ]
    return "\n".join(json.dumps({"message": {"content": [use]}}) for use in uses)


def _assert_unknown_helper_phase(
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
    phase: ClaudeUnknownPhase,
) -> None:
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, stream, ""),
    )
    body = "private body that must not be echoed"
    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        sendmessage("API work [ABC123]", body, Path("/usr/bin/false"))
    assert error.value.phase == phase
    assert body not in str(error.value)
    if stream:
        assert stream[:64] not in str(error.value)


def _gate_payload(tool_input: Mapping[str, object] | None = None) -> dict[str, object]:
    """One observed PreToolUse event for the courier's placeholder SendMessage."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "SendMessage",
        "tool_input": (
            tool_input
            if tool_input is not None
            else authoritative_tool_input(
                COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
            )
        ),
    }


def _expected_gate(recipient: str, message: str, summary: str) -> dict[str, object]:
    return {"recipient": recipient, "message": message, "summary": summary}


def _run_gate(
    gate: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    expected: dict[str, object],
    payload: dict[str, object],
) -> tuple[bool, dict[str, object]]:
    """Run the one-shot gate and return (allowed, hookSpecificOutput)."""
    expected_path = gate / "expected.json"
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    expected_path.chmod(0o600)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    allowed = run_pretool_gate(str(expected_path))
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    return allowed, output


def _sendmessage_receipt_stream(
    tool_input: Mapping[str, object], message_id: str | None = None
) -> str:
    """Provider stream logging one SendMessage use plus its successful result."""
    msg_id = message_id or str(uuid4())
    use = {"type": "tool_use", "id": "tool-1", "name": "SendMessage", "input": tool_input}
    result = {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": [
            {
                "type": "text",
                "text": json.dumps({"success": True, "message": "sent", "msg_id": msg_id}),
            }
        ],
    }
    return "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))


def _assert_sendmessage_delivered(
    monkeypatch: pytest.MonkeyPatch, tool_input: dict[str, object]
) -> None:
    """Assert one delivery completes whatever the logged proposal contained."""
    stream = _sendmessage_receipt_stream(tool_input)

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        _write_private_marker(expected_path.parent / "consumed", b"consumed\n")
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)

    sendmessage("API work [ABC123]", "private body that must not be echoed", Path("/usr/bin/false"))


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
    monkeypatch.setattr(claude_runtime, "claude_agents", lambda *_: agents)
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


def test_exact_agent_ignores_unrelated_noncanonical_roster_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import claude_runtime

    session_id = str(uuid4())
    payload = json.dumps(
        [
            {
                "sessionId": session_id,
                "name": "Exact target",
                "kind": "interactive",
                "cwd": str(tmp_path),
            },
            {
                "sessionId": str(uuid4()).upper(),
                "name": "Unrelated uppercase row",
                "kind": "interactive",
                "cwd": str(tmp_path),
            },
        ]
    )
    monkeypatch.setattr(claude_runtime, "claude_binary", lambda: Path("/opt/claude"))
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, payload, ""),
    )

    assert claude_runtime.exact_agent(session_id, str(tmp_path)) == {
        "session_id": session_id,
        "name": "Exact target",
        "kind": "interactive",
        "cwd": str(tmp_path.resolve()),
    }


def test_exact_agent_rejects_noncanonical_casecollision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import claude_runtime

    session_id = str(uuid4())
    payload = json.dumps(
        [
            {
                "sessionId": session_id.upper(),
                "name": "Case collision",
                "kind": "interactive",
                "cwd": str(tmp_path),
            }
        ]
    )
    monkeypatch.setattr(claude_runtime, "claude_binary", lambda: Path("/opt/claude"))
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, payload, ""),
    )

    with pytest.raises(ChatError, match="Claude session id is invalid"):
        claude_runtime.exact_agent(session_id, str(tmp_path))


def test_exact_agent_rejects_duplicate_valid_selected_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import claude_runtime

    session_id = str(uuid4())
    row = {
        "sessionId": session_id,
        "name": "Duplicate target",
        "kind": "interactive",
        "cwd": str(tmp_path),
    }
    payload = json.dumps([row, row])
    monkeypatch.setattr(claude_runtime, "claude_binary", lambda: Path("/opt/claude"))
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, payload, ""),
    )

    with pytest.raises(ChatError, match="exact live supported"):
        claude_runtime.exact_agent(session_id, str(tmp_path))


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
    # Used to HMAC-compare the model's transcription against the expected body;
    # the gate now ignores the proposal and supplies the authoritative
    # recipient/message/summary from the expected file itself.
    message = "one exact body"
    recipient = "API work [ABC123]"
    expected = _expected_gate(recipient, message, SEND_SUMMARY)
    payload = _gate_payload()

    phase, arguments = _pretool_resolution(expected, payload)
    assert phase is None
    assert arguments == authoritative_tool_input(recipient, message, SEND_SUMMARY)
    assert pretool_decision(expected, payload)

    # The delivered arguments do not depend on what the model proposed.
    payload["tool_input"] = {"to": "Other work [XYZ789]", "message": "forged"}
    phase, again = _pretool_resolution(expected, payload)
    assert phase is None
    assert again == arguments

    # A malformed expected contract still denies deterministically.
    assert not pretool_decision({"recipient": recipient, "message": message}, payload)
    assert not pretool_decision(
        {"recipient": "API work", "message": message, "summary": SEND_SUMMARY}, payload
    )


@pytest.mark.parametrize(
    "summary",
    [
        "Cross Agent Chat",
        "Delivery for API work review",
        "결과 회신",
        "shell-like `rm -rf` text stays inert data",
        "x" * 200,
        "\tindented but one line",
        "",
    ],
)
def test_pretool_gate_accepts_bounded_one_line_summary_variants(summary: str) -> None:
    recipient = "API work [ABC123]"
    message = "one exact body"
    expected = _expected_gate(recipient, message, summary)

    phase, arguments = _pretool_resolution(expected, _gate_payload())

    assert phase is None
    assert arguments == authoritative_tool_input(recipient, message, summary)


# Used to deny `type`/`summary` violations inside the model-transcribed input;
# the proposal is never inspected now, so summary validation moved to the
# sender-declared `expected` file and every violation is a payload mismatch.
@pytest.mark.parametrize(
    "summary",
    [
        "two\nlines",
        "carriage\rreturn",
        "x" * 201,
        "split\u2028line",
        "para\u2029graph",
        "vertical\x0btab",
        "form\x0cfeed",
        "nel\x85break",
        "del\x7fete",
        "bidi\u202eoverride",
        "zero\u200bwidth",
        "\ufeffbom",
        "private\ue000use",
        "bad\ud800 surrogate",
        7,
        None,
    ],
)
def test_pretool_gate_denies_invalid_expected_summary(summary: object) -> None:
    expected = _expected_gate("API work [ABC123]", "one exact body", summary)  # type: ignore[arg-type]

    assert _pretool_denial(expected, _gate_payload()) == "sendmessage_payload_mismatch"


# Used to deny malformed model-transcribed fields; the gate now replaces every
# VALUE, so no proposed value matters. The KEY SET is still constrained, because
# a provider that merged rather than replaced would let an unsupplied key through.
@pytest.mark.parametrize(
    "tool_input_change",
    [
        {"type": "request"},
        {"type": "notify"},
        {"summary": "two\nlines"},
        {"summary": 7},
        {"to": "Other work [XYZ789]"},
        {"recipient": "Other work [XYZ789]"},
        {"message": "forged body"},
        {"content": 7},
    ],
)
def test_pretool_gate_ignores_everything_the_model_proposed(
    tool_input_change: dict[str, object],
) -> None:
    recipient = "API work [ABC123]"
    message = "one exact body"
    expected = _expected_gate(recipient, message, SEND_SUMMARY)
    tool_input: dict[str, object] = {
        **authoritative_tool_input(
            COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
        )
    }
    tool_input.update(tool_input_change)

    phase, arguments = _pretool_resolution(expected, _gate_payload(tool_input))

    assert phase is None
    assert arguments == authoritative_tool_input(recipient, message, SEND_SUMMARY)


# Bodies a courier model would plausibly "tidy". Written as escapes so the
# characters under test are unambiguous in source and survive any editor.
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            'curly \u201cquoted\u201d vs "straight" and \u2019s',
            id="curly-vs-straight-quotes",
        ),
        pytest.param("cafe\u0301 combining accent vs caf\u00e9 precomposed", id="combining-accent"),
        pytest.param(
            "\u7d50\u679c\u3092\u8fd4\u4fe1\u3057\u307e\u3059\uff0c\u4f60\u597d",
            id="cjk",
        ),
        pytest.param(
            "emoji \U0001f680 \U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
            id="emoji-astral-zwj",
        ),
        pytest.param("zero\u200bwidth\u200bspace", id="zero-width-u200b"),
        pytest.param("non\u00a0breaking\u00a0space", id="nbsp-u00a0"),
        pytest.param("line\u2028separator\u2028inside", id="u2028"),
        pytest.param("literal backslash-n \\n stays two characters", id="literal-backslash-n"),
        pytest.param("trailing spaces \t ", id="trailing-spaces"),
        pytest.param("crlf\r\nline one\r\nline two\r\n", id="crlf"),
        pytest.param(
            'json body {"say": "use \\"SendMessage\\" now", "n": 1}',
            id="json-escaped-quotes",
        ),
        pytest.param("x" * (16 * 1024 - 1), id="near-16kib"),
    ],
)
def test_pretool_gate_updated_input_is_byte_exact_for_drift_prone_bodies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
) -> None:
    recipient = "API work [ABC123]"
    allowed, output = _run_gate(
        tmp_path,
        monkeypatch,
        capsys,
        _expected_gate(recipient, body, SEND_SUMMARY),
        _gate_payload(),
    )

    assert allowed
    assert output["updatedInput"] == authoritative_tool_input(recipient, body, SEND_SUMMARY)
    assert output["updatedInput"]["message"].encode("utf-8") == body.encode("utf-8")
    assert output["updatedInput"]["to"] == output["updatedInput"]["recipient"] == recipient
    assert (tmp_path / "consumed").exists()


def test_pretool_gate_updated_input_is_independent_of_the_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recipient = "API work [ABC123]"
    expected = _expected_gate(recipient, "authoritative body", SEND_SUMMARY)
    proposals: list[Mapping[str, object]] = [
        authoritative_tool_input(
            COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
        ),
        # Same key set, every value wrong: the gate must still deliver the
        # sender's arguments. Out-of-set keys are denied, covered separately.
        {
            "to": "Wrong [ZZZ999]",
            "recipient": "Wrong [ZZZ999]",
            "message": "truncated",
            "content": "",
            "type": "notify",
            "summary": "forged",
        },
    ]
    delivered = []
    for index, tool_input in enumerate(proposals):
        gate = tmp_path / f"gate-{index}"
        gate.mkdir()
        gate.chmod(0o700)
        allowed, output = _run_gate(gate, monkeypatch, capsys, expected, _gate_payload(tool_input))
        assert allowed
        delivered.append(output["updatedInput"])

    expected_arguments = authoritative_tool_input(recipient, "authoritative body", SEND_SUMMARY)
    assert delivered == [expected_arguments, expected_arguments]


# Used to deny when the model's `to`/`recipient` aliases diverged; the proposal
# is ignored now, so the equivalent guard is that the sender-declared recipient
# must be an exact `name [TOKEN]` target reference.
@pytest.mark.parametrize(
    "recipient",
    [
        "API work",
        "API work [ABC12]",
        "API work [ABC1234]",
        "[ABC123]",
        "API work [ABC123] extra",
        7,
        None,
    ],
)
def test_pretool_gate_denies_malformed_expected_target_ref(
    recipient: object,
) -> None:
    expected = _expected_gate(recipient, "one exact body", SEND_SUMMARY)  # type: ignore[arg-type]

    assert _pretool_denial(expected, _gate_payload()) == "sendmessage_payload_mismatch"


def test_pretool_gate_denies_replay_after_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = "API work [ABC123]"
    message = "hello"
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(
        json.dumps(_expected_gate(target, message, SEND_SUMMARY)),
        encoding="utf-8",
    )
    expected_path.chmod(0o600)
    payload = _gate_payload()
    stdin = io.StringIO(json.dumps(payload))
    monkeypatch.setattr("sys.stdin", stdin)

    assert run_pretool_gate(str(expected_path))
    assert (tmp_path / "consumed").exists()

    stdin.seek(0)
    assert not run_pretool_gate(str(expected_path))
    assert (tmp_path / "denied").read_bytes() == DENIAL_MARKERS["pretool_gate_denied"]
    outputs = [
        json.loads(line)["hookSpecificOutput"] for line in capsys.readouterr().out.splitlines()
    ]
    assert [output["permissionDecision"] for output in outputs] == ["allow", "deny"]
    # updatedInput is emitted only on allow, never on deny.
    assert "updatedInput" in outputs[0]
    assert "updatedInput" not in outputs[1]


def test_pretool_gate_uses_full_unicode_body_not_cosmetic_preview() -> None:
    # Used to assert the model's transcription HMAC-matched the full body; the
    # gate now supplies the body itself, so updatedInput must carry it verbatim.
    message = "e\u0301 family 👨‍👩‍👧‍👦 " + "wide界" * 20
    recipient = "API work [ABC123]"
    expected = _expected_gate(recipient, message, SEND_SUMMARY)

    phase, arguments = _pretool_resolution(expected, _gate_payload())

    assert phase is None
    assert arguments == authoritative_tool_input(recipient, message, SEND_SUMMARY)
    assert arguments["message"].encode("utf-8") == message.encode("utf-8")


# Used to deny with sendmessage_message_mismatch when the model's proposed body
# contained an unpaired surrogate; the proposal is never inspected or delivered
# now, so the gate allows and substitutes the authoritative arguments.
def test_pretool_gate_ignores_an_unpaired_surrogate_in_the_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recipient = "API work [ABC123]"
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(
        json.dumps(_expected_gate(recipient, "hello", SEND_SUMMARY)), encoding="utf-8"
    )
    expected_path.chmod(0o600)
    surrogate = "\ud800"
    payload = _gate_payload({"to": surrogate, "recipient": surrogate, "message": surrogate})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert run_pretool_gate(str(expected_path))
    assert (tmp_path / "consumed").exists()
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "allow"
    assert output["updatedInput"] == authoritative_tool_input(recipient, "hello", SEND_SUMMARY)


def test_pretool_gate_writes_owned_private_consumed_marker_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = "API work [ABC123]"
    message = "hello"
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(
        json.dumps(_expected_gate(target, message, SEND_SUMMARY)),
        encoding="utf-8",
    )
    expected_path.chmod(0o600)
    payload = _gate_payload()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert run_pretool_gate(str(expected_path))
    consumed = tmp_path / "consumed"
    assert consumed.read_bytes() == b"consumed\n"
    assert stat.S_ISREG(consumed.stat().st_mode)
    assert consumed.stat().st_uid == os.getuid()
    assert stat.S_IMODE(consumed.stat().st_mode) == 0o600
    response = json.loads(capsys.readouterr().out)
    assert response["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert response["hookSpecificOutput"]["updatedInput"] == authoritative_tool_input(
        target, message, SEND_SUMMARY
    )


@pytest.mark.parametrize(
    ("marker", "mode"),
    [
        (b"", 0o600),
        (b"consumed", 0o600),
        (b"consumed\nextra", 0o600),
        (b"denied\n", 0o644),
    ],
)
def test_gate_consumed_rejects_malformed_or_unsafe_marker(
    tmp_path: Path, marker: bytes, mode: int
) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    marker_path = gate / "consumed"
    marker_path.write_bytes(marker)
    marker_path.chmod(mode)

    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        gate_consumed(gate)

    assert error.value.phase == "pretool_gate_unreadable"


def test_gate_consumed_rejects_marker_symlink(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    target = tmp_path / "target"
    target.write_bytes(b"consumed\n")
    (gate / "consumed").symlink_to(target)

    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        gate_consumed(gate)

    assert error.value.phase == "pretool_gate_unreadable"


def test_gate_consumed_rejects_unsafe_parent_mode(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o755)
    _write_private_marker(gate / "consumed", b"consumed\n")

    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        gate_consumed(gate)

    assert error.value.phase == "pretool_gate_unreadable"


def test_gate_consumed_rejects_parent_symlink(tmp_path: Path) -> None:
    real_gate = tmp_path / "real-gate"
    real_gate.mkdir()
    _write_private_marker(real_gate / "consumed", b"consumed\n")
    gate = tmp_path / "gate-link"
    gate.symlink_to(real_gate, target_is_directory=True)

    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        gate_consumed(gate)

    assert error.value.phase == "pretool_gate_unreadable"


def test_gate_consumed_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    os.mkfifo(gate / "consumed", 0o600)
    script = """
import sys
from pathlib import Path
from cross_agent_chat.claude_runtime import ClaudeSendMessageUnknownDelivery, gate_consumed

try:
    gate_consumed(Path(sys.argv[1]))
except ClaudeSendMessageUnknownDelivery as error:
    print(error.phase)
    """
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [sys.executable, "-c", script, str(gate)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=1,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "pretool_gate_unreadable"


@pytest.mark.parametrize("operation", ["fstat", "read"])
def test_gate_consumed_converts_marker_io_errors_to_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    _write_private_marker(gate / "consumed", b"consumed\n")
    monkeypatch.setattr(
        f"cross_agent_chat.claude_runtime.os.{operation}",
        lambda *_: (_ for _ in ()).throw(OSError("EIO")),
    )

    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        gate_consumed(gate)

    assert error.value.phase == "pretool_gate_unreadable"


def test_gate_consumed_rejects_unsafe_parent_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    _write_private_marker(gate / "consumed", b"consumed\n")
    current_uid = os.getuid()
    monkeypatch.setattr("cross_agent_chat.claude_runtime.os.getuid", lambda: current_uid + 1)

    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        gate_consumed(gate)

    assert error.value.phase == "pretool_gate_unreadable"


def test_gate_consumed_accepts_consumed_only_marker(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    _write_private_marker(gate / "consumed", b"consumed\n")

    assert gate_consumed(gate)


def test_gate_consumed_reports_denied_only_marker_as_not_consumed(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    _write_private_marker(gate / "denied", b"denied\n")

    assert not gate_consumed(gate)


def test_gate_consumed_rejects_conflicting_markers(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    _write_private_marker(gate / "consumed", b"consumed\n")
    _write_private_marker(gate / "denied", b"denied\n")

    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        gate_consumed(gate)

    assert error.value.phase == "pretool_gate_conflict"


def test_sendmessage_with_valid_helper_but_no_markers_is_unobserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_unknown_helper_phase(
        monkeypatch,
        _sendmessage_stream("API work [ABC123]", "private body that must not be echoed"),
        "pretool_gate_unobserved",
    )


def test_sendmessage_with_valid_helper_and_denied_marker_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate denied the courier's call and the stream carries no delivered
    # receipt, so the call provably never ran. That is a decided pre-effect
    # rejection, not an uncertain delivery.
    stream = _sendmessage_stream("API work [ABC123]", "private body that must not be echoed")

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        _write_private_marker(expected_path.parent / "denied", b"denied\n")
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    with pytest.raises(ClaudeSendMessageRefused) as error:
        sendmessage(
            "API work [ABC123]", "private body that must not be echoed", Path("/usr/bin/false")
        )

    assert "denied" in str(error.value)
    assert not isinstance(error.value, UnknownDeliveryError)


def test_sendmessage_denied_marker_with_a_delivered_receipt_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A denied marker plus a delivered receipt contradict each other: the deny
    # did not hold, and the event must stay unknown rather than look decided.
    stream = _sendmessage_receipt_stream(
        authoritative_tool_input("API work [ABC123]", "body", SEND_SUMMARY)
    )

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        _write_private_marker(expected_path.parent / "denied", b"denied\n")
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        sendmessage(
            "API work [ABC123]", "private body that must not be echoed", Path("/usr/bin/false")
        )

    assert error.value.phase == "pretool_gate_conflict"


def test_sendmessage_denied_marker_with_a_provider_refusal_surfaces_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A denied marker plus the provider's own decided refusal receipt both say
    # nothing was delivered; the provider's cause-specific reason is the better
    # report.
    reason = "No agent named 'API work [ABC123]' is reachable."
    stream = _sendmessage_refusal_stream(json.dumps({"success": False, "message": reason}))

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        _write_private_marker(expected_path.parent / "denied", b"denied\n")
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    with pytest.raises(ClaudeSendMessageRefused) as error:
        sendmessage(
            "API work [ABC123]", "private body that must not be echoed", Path("/usr/bin/false")
        )

    assert str(error.value) == reason


def test_sendmessage_denied_marker_with_an_unreadable_stream_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gate denied the call but the stream cannot be checked for a delivered
    # receipt, so the outcome cannot be proven decided.
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        _write_private_marker(expected_path.parent / "denied", b"denied\n")
        return subprocess.CompletedProcess(command, 0, "{malformed", "")

    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        sendmessage(
            "API work [ABC123]", "private body that must not be echoed", Path("/usr/bin/false")
        )

    assert error.value.phase == "helper_stream_invalid"


def test_sendmessage_with_conflicting_markers_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _sendmessage_stream("API work [ABC123]", "private body that must not be echoed")

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        _write_private_marker(expected_path.parent / "consumed", b"consumed\n")
        _write_private_marker(expected_path.parent / "denied", b"denied\n")
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        sendmessage(
            "API work [ABC123]", "private body that must not be echoed", Path("/usr/bin/false")
        )

    assert error.value.phase == "pretool_gate_conflict"


@pytest.mark.parametrize(
    ("stream", "phase"),
    [
        (
            _sendmessage_stream(
                "API work [ABC123]", "private body that must not be echoed", tool_name="ListAgents"
            ),
            "helper_stream_invalid",
        ),
        (
            _sendmessage_stream(
                "API work [ABC123]", "private body that must not be echoed", extra_uses=1
            ),
            "multiple_sendmessage_tool_use",
        ),
        ("{malformed", "helper_stream_invalid"),
        ("[]", "helper_stream_invalid"),
        ("x" * (64 * 1024 + 1), "helper_stream_invalid"),
        ("\ud800", "helper_stream_invalid"),
    ],
)
def test_sendmessage_helper_stream_failures_are_body_free_and_enum_bound(
    monkeypatch: pytest.MonkeyPatch, stream: str, phase: ClaudeUnknownPhase
) -> None:
    _assert_unknown_helper_phase(monkeypatch, stream, phase)


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param("", id="empty-stream"),
        pytest.param(
            json.dumps({"type": "system", "subtype": "init", "session_id": "abc"}),
            id="records-without-a-tool-call",
        ),
    ],
)
def test_sendmessage_without_a_tool_call_is_decided(
    monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    # A complete, readable stream without any SendMessage tool call proves the
    # courier finished without sending: the only delivery path is that tool,
    # so nothing was delivered and the outcome is decided, not unknown.
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, stream, ""),
    )
    with pytest.raises(ClaudeSendMessageRefused) as error:
        sendmessage(
            "API work [ABC123]", "private body that must not be echoed", Path("/usr/bin/false")
        )

    assert "SendMessage" in str(error.value)
    assert not isinstance(error.value, UnknownDeliveryError)


# These used to assert that a divergent target, an altered body, or an
# unexpected field in the model's proposal produced an unknown delivery; the
# gate now replaces the whole tool input, so none of the logged proposal's
# contents can affect the delivered arguments.
@pytest.mark.parametrize(
    "tool_input",
    [
        authoritative_tool_input("Other work [XYZ789]", "forged body", SEND_SUMMARY),
        {
            "to": "API work [ABC123]",
            "recipient": "API work [ABC123]",
            "message": "altered private body",
            "content": "provider preview",
            "type": "message",
            "summary": "Cross Agent Chat",
        },
        {
            "to": "API work [ABC123]",
            "recipient": "API work [ABC123]",
            "message": "private body that must not be echoed",
            "content": "provider preview",
            "type": "message",
            "summary": "Cross Agent Chat",
            "unexpected": "reject",
        },
        # Same key set, every value wrong: the gate must still deliver the
        # sender's arguments. Out-of-set keys are denied, covered separately.
        {
            "to": "Wrong [ZZZ999]",
            "recipient": "Wrong [ZZZ999]",
            "message": "truncated",
            "content": "",
            "type": "notify",
            "summary": "forged",
        },
    ],
)
def test_sendmessage_ignores_the_courier_proposal(
    monkeypatch: pytest.MonkeyPatch, tool_input: dict[str, object]
) -> None:
    _assert_sendmessage_delivered(monkeypatch, tool_input)


# The gate no longer inspects the model's proposal, so the old per-field
# denials (target/message/type/summary mismatch) collapse into one contract
# check: sendmessage_payload_mismatch for an invalid expected file or a
# non-SendMessage event, pretool_gate_denied for unreadable/malformed inputs.
@pytest.mark.parametrize(
    ("denial", "expected_kind", "payload_kind"),
    [
        # The pre-repair HMAC schema is now a malformed expected file.
        ("sendmessage_payload_mismatch", "legacy_hmac_schema", None),
        ("sendmessage_payload_mismatch", "missing_summary", None),
        ("sendmessage_payload_mismatch", "extra_key", None),
        ("sendmessage_payload_mismatch", "bad_target_ref", None),
        ("sendmessage_payload_mismatch", "invalid_summary", None),
        ("sendmessage_payload_mismatch", "non_string_message", None),
        ("sendmessage_payload_mismatch", None, "wrong_event"),
        ("sendmessage_payload_mismatch", None, "wrong_tool"),
        ("sendmessage_payload_mismatch", None, "non_dict_tool_input"),
        ("sendmessage_payload_mismatch", None, "non_dict_payload"),
        ("pretool_gate_denied", "unparseable_json", None),
        ("pretool_gate_denied", "non_dict_expected", None),
        ("pretool_gate_denied", None, "unparseable_payload"),
        ("pretool_gate_denied", None, "oversized_payload"),
    ],
)
def test_pretool_gate_writes_specific_fixed_denial_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    denial: ClaudeUnknownPhase,
    expected_kind: str | None,
    payload_kind: str | None,
) -> None:
    target = "API work [ABC123]"
    message = "private body"
    expected: object = _expected_gate(target, message, SEND_SUMMARY)
    if expected_kind == "legacy_hmac_schema":
        expected = {"recipient": target, "message_hmac": "0" * 64}
    elif expected_kind == "missing_summary":
        expected = {"recipient": target, "message": message}
    elif expected_kind == "extra_key":
        expected = {**expected, "extra": "reject"}  # type: ignore[dict-item]
    elif expected_kind == "bad_target_ref":
        expected = _expected_gate("API work", message, SEND_SUMMARY)
    elif expected_kind == "invalid_summary":
        expected = _expected_gate(target, message, "one line\nsecond line")
    elif expected_kind == "non_string_message":
        expected = _expected_gate(target, 7, SEND_SUMMARY)  # type: ignore[arg-type]
    elif expected_kind == "non_dict_expected":
        expected = ["not", "a", "dict"]

    expected_path = tmp_path / "expected.json"
    if expected_kind == "unparseable_json":
        expected_path.write_text("{malformed", encoding="utf-8")
        expected_path.chmod(0o600)
    else:
        expected_path.write_text(json.dumps(expected), encoding="utf-8")
        expected_path.chmod(0o600)

    payload: object = _gate_payload()
    if payload_kind == "wrong_event":
        payload = {**payload, "hook_event_name": "PostToolUse"}  # type: ignore[dict-item]
    elif payload_kind == "wrong_tool":
        payload = {**payload, "tool_name": "ListAgents"}  # type: ignore[dict-item]
    elif payload_kind == "non_dict_tool_input":
        payload = {**payload, "tool_input": "SendMessage"}  # type: ignore[dict-item]
    elif payload_kind == "non_dict_payload":
        payload = ["not", "a", "dict"]

    if payload_kind == "unparseable_payload":
        stdin_text = "{malformed"
    elif payload_kind == "oversized_payload":
        # Exercises the BYTE cap specifically: valid JSON that stdin can read
        # whole (under 65537 characters) but whose UTF-8 encoding exceeds 65536
        # bytes, so resolution is refused by the size guard rather than by a
        # parse failure. ensure_ascii=False keeps the characters multibyte.
        stdin_text = json.dumps({"pad": "\u754c" * 30000}, ensure_ascii=False)
        assert len(stdin_text) <= 65536 < len(stdin_text.encode())
    else:
        stdin_text = json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))

    assert not run_pretool_gate(str(expected_path))
    assert (tmp_path / "denied").read_bytes() == DENIAL_MARKERS[denial]
    assert not (tmp_path / "consumed").exists()
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "updatedInput" not in output


def test_pretool_gate_fails_closed_for_unsafe_parent_without_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gate = tmp_path / "unsafe"
    gate.mkdir()
    gate.chmod(0o755)
    expected_path = gate / "expected.json"
    expected_path.write_text(json.dumps(_expected_gate("API work [ABC123]", "hello", SEND_SUMMARY)))
    expected_path.chmod(0o600)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

    assert not run_pretool_gate(str(expected_path))
    assert not (gate / "consumed").exists()
    assert not (gate / "denied").exists()
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "updatedInput" not in output


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
    message_id = str(uuid4())
    # The provider stream logs the courier's placeholder proposal, which the
    # gate replaced before execution; the receipt never inspects it.
    use = {
        "type": "tool_use",
        "id": "tool-1",
        "name": "SendMessage",
        "input": authoritative_tool_input(
            COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
        ),
    }
    text_block: dict[str, object] = {
        "type": "text",
        "text": json.dumps({"success": True, "message": "sent", "msg_id": message_id}),
    }
    result: dict[str, object] = {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": [text_block],
    }
    stream = "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))

    assert parse_sendmessage_receipt(stream) == message_id

    result["is_error"] = True
    rejected = "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))
    with pytest.raises(ChatError, match="receipt"):
        parse_sendmessage_receipt(rejected)

    result["is_error"] = False
    text_block["text"] = json.dumps(
        {"success": True, "message": "sent", "msg_id": message_id, "latency_ms": 3}
    )
    rejected = "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))
    with pytest.raises(ChatError, match="receipt"):
        parse_sendmessage_receipt(rejected)
    text_block["text"] = json.dumps({"success": True, "msg_id": message_id})
    rejected = "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))
    with pytest.raises(ChatError, match="receipt"):
        parse_sendmessage_receipt(rejected)
    text_block["text"] = "sent ok"
    rejected = "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))
    with pytest.raises(ChatError, match="receipt"):
        parse_sendmessage_receipt(rejected)
    text_block["text"] = json.dumps({"success": True, "message": "sent", "msg_id": "not-a-uuid"})
    rejected = "\n".join(json.dumps({"message": {"content": [block]}}) for block in (use, result))
    with pytest.raises(ChatError, match="invalid"):
        parse_sendmessage_receipt(rejected)


@pytest.mark.parametrize("uses", [0, 2])
def test_sendmessage_receipt_requires_exactly_one_sendmessage_use(uses: int) -> None:
    blocks = [
        {
            "type": "tool_use",
            "id": f"tool-{index}",
            "name": "SendMessage",
            "input": authoritative_tool_input(
                COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
            ),
        }
        for index in range(uses)
    ]
    stream = "\n".join(json.dumps({"message": {"content": [block]}}) for block in blocks)

    with pytest.raises(ChatError, match="receipt"):
        parse_sendmessage_receipt(stream)


# Used to verify the logged proposal's display-only `summary` was accepted; the
# receipt no longer inspects tool_use.input at all, so any logged summary — or
# none — still validates against the successful result alone.
@pytest.mark.parametrize(
    "summary",
    ["Cross Agent Chat", "Different one-line preview", "회신 미리보기"],
)
def test_sendmessage_receipt_accepts_display_only_summary_variants(
    summary: str,
) -> None:
    message_id = str(uuid4())
    tool_input: dict[str, object] = {
        **authoritative_tool_input(COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, summary)
    }
    stream = _sendmessage_receipt_stream(tool_input, message_id)

    assert parse_sendmessage_receipt(stream) == message_id

    del tool_input["summary"]
    stream = _sendmessage_receipt_stream(tool_input, message_id)
    assert parse_sendmessage_receipt(stream) == message_id


# Used to reject proposals carrying control/`summary` violations; the logged
# tool_use.input is the model's obsolete proposal, so the receipt returns the
# msg_id regardless of what it contains.
@pytest.mark.parametrize(
    "tool_input_change",
    [
        {"type": "request"},
        {"summary": "two\nlines"},
        {"summary": "carriage\rreturn"},
        {"summary": "x" * 201},
        {"summary": "split\u2028line"},
        {"summary": "para\u2029graph"},
        {"summary": "nel\x85break"},
        {"summary": "del\x7fete"},
        {"summary": "bidi\u202eoverride"},
        {"summary": "zero\u200bwidth"},
        {"summary": "\ufeffbom"},
        {"summary": 7},
        {"summary": None},
        {"notify_when_idle": True},
    ],
)
def test_sendmessage_receipt_ignores_proposal_field_violations(
    tool_input_change: dict[str, object],
) -> None:
    message_id = str(uuid4())
    tool_input: dict[str, object] = {
        **authoritative_tool_input(
            COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
        )
    }
    tool_input.update(tool_input_change)
    stream = _sendmessage_receipt_stream(tool_input, message_id)

    assert parse_sendmessage_receipt(stream) == message_id


@pytest.mark.parametrize(
    "tool_id,result_override,expected_results",
    [
        (7, None, "one"),
        ("tool-1", {"content": "not-a-list"}, "one"),
        ("tool-1", {"content": []}, "one"),
        (
            "tool-1",
            {
                "content": [
                    {"type": "text", "text": "{}"},
                    {"type": "text", "text": "{}"},
                ]
            },
            "one",
        ),
        ("tool-1", {"is_error": True}, "one"),
        ("tool-1", None, "unmatched"),
        ("tool-1", None, "duplicate"),
    ],
)
def test_sendmessage_receipt_rejects_result_contract_violations(
    tool_id: object, result_override: dict[str, object] | None, expected_results: str
) -> None:
    message_id = str(uuid4())
    use = {
        "type": "tool_use",
        "id": tool_id,
        "name": "SendMessage",
        "input": authoritative_tool_input(
            COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
        ),
    }
    result: dict[str, object] = {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": [
            {
                "type": "text",
                "text": json.dumps({"success": True, "message": "sent", "msg_id": message_id}),
            }
        ],
    }
    if result_override is not None:
        result.update(result_override)
    results = [result]
    if expected_results == "unmatched":
        results = [dict(result, tool_use_id="other-tool")]
    elif expected_results == "duplicate":
        results = [result, dict(result)]
    stream = "\n".join(json.dumps({"message": {"content": [block]}}) for block in [use, *results])

    with pytest.raises(ChatError, match="receipt"):
        parse_sendmessage_receipt(stream)


# Used to reject when the logged tool_use.input body differed from the expected
# message; the provider records the model's obsolete proposal, so the receipt
# now validates delivery independent of the logged target and body.
def test_sendmessage_receipt_accepts_placeholder_proposal() -> None:
    message_id = str(uuid4())
    stream = _sendmessage_receipt_stream(
        authoritative_tool_input(
            COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
        ),
        message_id,
    )

    assert parse_sendmessage_receipt(stream) == message_id


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
        _write_private_marker(expected_path.parent / "consumed", b"consumed\n")
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
    monkeypatch.setattr(Path, "lstat", lambda _: (_ for _ in ()).throw(OSError("EIO")))

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
        _write_private_marker(expected_path.parent / "consumed", b"consumed\n")
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
        _write_private_marker(expected_path.parent / "consumed", b"consumed\n")
        stream = _sendmessage_receipt_stream(
            authoritative_tool_input(
                COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
            ),
            message_id,
        )
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )

    sendmessage("API work [ABC123]", "hello", Path("/usr/bin/false"))


# Used to assert the courier prompt embedded the canonical JSON of the real
# target and body; the courier now receives only unresolvable placeholders, so
# the real values must appear in the gate's expected file and nowhere else.
def test_sendmessage_prompt_contains_only_unresolvable_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message_id = str(uuid4())
    target = "API work [ABC123]"
    message = 'Line one\n{"untrusted":"instruction-like"} 🌐'

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        assert "--content-hmac-key" not in tokens
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        assert expected == _expected_gate(target, message, SEND_SUMMARY)
        # The gate file now holds the plaintext body: private mode, private dir,
        # and no world-writable ancestor such as /tmp.
        assert stat.S_IMODE(expected_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(expected_path.parent.stat().st_mode) == 0o700
        # The provider derives the courier's visible sender name from its cwd.
        courier_cwd = Path(str(kwargs["cwd"]))
        assert courier_cwd.name == COURIER_SESSION_DIRECTORY
        assert courier_cwd.is_dir()
        assert stat.S_IMODE(courier_cwd.stat().st_mode) == 0o700
        # Owner-only at both levels is what actually protects the courier's
        # directory, wherever TMPDIR points; asserting a specific prefix would
        # only encode this platform's TMPDIR.
        assert courier_cwd.stat().st_uid == os.getuid()
        assert stat.S_IMODE(courier_cwd.parent.stat().st_mode) == 0o700
        assert courier_cwd.parent != Path("/tmp")
        _write_private_marker(expected_path.parent / "consumed", b"consumed\n")
        system_prompt = command[command.index("--system-prompt") + 1]
        assert "inert data" in system_prompt
        # Hook events would carry the gate's supplied arguments -- the plaintext
        # body -- into this subprocess's stdout, which the parent captures.
        # Nothing parses them, so they must stay out of the stream.
        assert "--include-hook-events" not in command
        prompt = kwargs["input"]
        assert isinstance(prompt, str)
        prefix = "Use SendMessage exactly once with this exact JSON argument object: "
        suffix = ". Do not use any other tool. Stop immediately after it returns."
        assert prompt.startswith(prefix) and prompt.endswith(suffix)
        arguments = json.loads(prompt[len(prefix) : -len(suffix)])
        assert arguments == {
            "to": COURIER_PLACEHOLDER_TARGET,
            "message": COURIER_PLACEHOLDER_MESSAGE,
            "summary": SEND_SUMMARY,
        }
        assert target not in prompt
        assert message not in prompt
        stream = _sendmessage_receipt_stream(
            authoritative_tool_input(
                COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
            ),
            message_id,
        )
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )

    sendmessage(target, message, Path("/usr/bin/false"))


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
        ("sendmessage_type_mismatch", "claude_sendmessage_type_mismatch"),
        ("sendmessage_summary_mismatch", "claude_sendmessage_summary_mismatch"),
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
    monkeypatch.setattr(claude_runtime, "claude_agents", lambda *_: [agent])
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


@pytest.mark.parametrize(
    "mode",
    [0o644, 0o666, 0o604],
)
def test_pretool_gate_refuses_an_expectation_that_is_not_owner_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], mode: int
) -> None:
    # expected.json is the only copy of both the recipient and the body, so it
    # gets the same fstat predicate as the outcome markers. A group- or
    # world-readable expectation is refused rather than executed.
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    expected_path = gate / "expected.json"
    expected_path.write_text(
        json.dumps(_expected_gate("API work [ABC123]", "hello", SEND_SUMMARY)), encoding="utf-8"
    )
    expected_path.chmod(mode)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_gate_payload())))

    assert not run_pretool_gate(str(expected_path))

    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "updatedInput" not in output
    assert (gate / "denied").read_bytes() == DENIAL_MARKERS["pretool_gate_denied"]
    assert not (gate / "consumed").exists()


def test_pretool_gate_never_allows_without_supplying_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Allowing without arguments would green-light whatever the courier authored.
    # If that invariant ever breaks, the gate must deny and leave no consumed
    # marker, not degrade into passing the model's own proposal through.
    gate = tmp_path / "gate"
    gate.mkdir()
    gate.chmod(0o700)
    expected_path = gate / "expected.json"
    expected_path.write_text(
        json.dumps(_expected_gate("API work [ABC123]", "hello", SEND_SUMMARY)), encoding="utf-8"
    )
    expected_path.chmod(0o600)
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime._pretool_resolution",
        lambda expected, payload: (None, None),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_gate_payload())))

    assert not run_pretool_gate(str(expected_path))

    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "updatedInput" not in output
    assert not (gate / "consumed").exists()


@pytest.mark.live
def test_live_gate_substitutes_the_target_the_courier_never_saw(tmp_path: Path) -> None:
    """The provider still honors ``updatedInput``. Deselected by default.

    Every other test here mocks ``subprocess.run`` and asserts what the gate
    PRINTS. This one asserts what the provider DOES with it, which is the
    property the whole repair rests on and the only thing a provider upgrade can
    silently break.

    It needs no receiver. The courier is told to address
    ``COURIER_PLACEHOLDER_TARGET``; the gate substitutes a well-formed but absent
    target. Whichever name the provider reports as unreachable tells us which
    input actually executed, so this keys on the READ rather than on success or
    failure and cannot be satisfied by an error state. If ``updatedInput`` were
    dropped, the provider would report the placeholder and this fails.

    Measured on Claude Code 2.1.274 (2026-09-17) alongside three live deliveries
    of 433 B, 815 B and 8835 B bodies, each byte-exact at the receiver, plus a
    hook-removed control that reported the placeholder as unreachable.

    Run with: pytest -m live tests/test_claude_remote.py
    """
    absent = "cac-regression-absent [ZZZZZZ]"
    source_root = Path(__file__).parents[1] / "src"
    shim = tmp_path / "cross-agent-chat"
    # PYTHONPATH rather than an inline sys.path.insert: nesting quotes inside the
    # -c string terminates the shell quoting and the hook silently fails to run.
    shim.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(source_root))} "
        f"exec {shlex.quote(sys.executable)} -c "
        "'import sys; from cross_agent_chat.cli import main; sys.exit(main())' \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o700)

    streams: list[str] = []
    real_run = subprocess.run

    def capture(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        completed = real_run(command, **kwargs)  # type: ignore[call-overload]
        streams.append(str(completed.stdout))
        return completed  # type: ignore[no-any-return]

    with (
        mock.patch.object(subprocess, "run", capture),
        # The provider answers that the substituted target is unreachable, which
        # is a decided non-delivery, not an uncertain one. This also exercises
        # that classification against the real provider rather than a fixture.
        pytest.raises(ClaudeSendMessageRefused) as refused,
    ):
        sendmessage(absent, "live gate regression probe", shim)

    assert absent in str(refused.value)

    assert streams, "the courier subprocess never ran"
    uses, results = _tool_records(streams[0])
    assert len(uses) == 1, "the courier did not make exactly one SendMessage call"
    proposal = uses[0]["input"]
    assert isinstance(proposal, dict)
    # The courier proposed the placeholder: it was never shown the real target.
    assert proposal["to"] == COURIER_PLACEHOLDER_TARGET
    # The provider acted on the gate's substituted target, not on that proposal.
    assert len(results) == 1
    reported = json.dumps(results[0].get("content"))
    assert absent in reported, "the provider did not act on the gate's updatedInput"
    assert COURIER_PLACEHOLDER_TARGET not in reported, (
        "the provider reported the placeholder, so updatedInput was not applied"
    )


def test_authoritative_tool_input_key_set_is_pinned_to_a_measured_provider_shape() -> None:
    """Pin the argument shape so a silent edit shows up as a visible diff.

    This asserts nothing about the provider. The shape was measured by capturing
    a real PreToolUse payload on Claude Code 2.1.274 (2026-09-17): the provider
    expands `to` into `recipient`, `message` into `content`, and adds
    `type: "message"` before the gate observes the call. If a future provider
    normalizes differently, re-measure with a capture-only hook and update this
    set together with `authoritative_tool_input`; the live test above is what
    will tell you it broke.
    """
    arguments = authoritative_tool_input("API work [ABC123]", "body", SEND_SUMMARY)

    assert set(arguments) == {"to", "recipient", "message", "content", "type", "summary"}
    assert arguments["to"] == arguments["recipient"]
    assert arguments["message"] == arguments["content"]
    assert arguments["type"] == "message"


def _sendmessage_refusal_stream(result_text: str) -> str:
    """Provider stream logging one SendMessage use whose result is a refusal."""
    use = {
        "type": "tool_use",
        "id": "tool-1",
        "name": "SendMessage",
        "input": authoritative_tool_input(
            COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
        ),
    }
    result = {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": [{"type": "text", "text": result_text}],
    }
    return "\n".join(
        [
            json.dumps({"type": "assistant", "message": {"content": [use]}}),
            json.dumps({"type": "user", "message": {"content": [result]}}),
        ]
    )


def test_receipt_treats_a_reported_non_delivery_as_decided_not_unknown() -> None:
    # Exactly one SendMessage ran and the gate was consumed exactly once, both
    # established before this parse. The provider then answered that it did not
    # deliver and carried no message id, so nothing was created. Reporting that
    # as UNKNOWN froze an event that provably delivered nothing, which is what
    # drove the hand relays in the field reports.
    stream = _sendmessage_refusal_stream(
        json.dumps(
            {
                "success": False,
                "message": "No agent named 'Gone [ABC123]' is reachable.",
            }
        )
    )

    with pytest.raises(ClaudeSendMessageRefused) as caught:
        parse_sendmessage_receipt(stream)

    assert "is reachable" in str(caught.value)
    # It must be a deterministic rejection, never an unknown-delivery outcome.
    assert not isinstance(caught.value, UnknownDeliveryError)


def test_receipt_treats_a_refusal_with_a_display_rendering_as_decided() -> None:
    # Claude Code 2.1.278 emits the same decided refusal with a `display` key
    # carrying the refusal text rendered for a UI surface. The key cannot mark
    # an effect -- only `msg_id` can -- so the refusal stays decided.
    reason = "No agent named 'Gone [ABC123]' is reachable."
    stream = _sendmessage_refusal_stream(
        json.dumps(
            {
                "success": False,
                "message": reason,
                "display": f"Error: {reason}",
            }
        )
    )

    with pytest.raises(ClaudeSendMessageRefused) as caught:
        parse_sendmessage_receipt(stream)

    assert str(caught.value) == reason


def test_receipt_keeps_a_refusal_with_a_nonstring_display_uncertain() -> None:
    # A `display` value that is not the provider's string rendering is not the
    # observed contract and stays unknown.
    stream = _sendmessage_refusal_stream(
        json.dumps({"success": False, "message": "gone", "display": 7})
    )

    with pytest.raises(ChatError) as caught:
        parse_sendmessage_receipt(stream)

    assert not isinstance(caught.value, ClaudeSendMessageRefused)


@pytest.mark.parametrize(
    "result_text",
    [
        pytest.param(
            json.dumps({"success": False, "message": "gone", "msg_id": str(uuid4())}),
            id="refusal-carrying-a-message-id",
        ),
        pytest.param(json.dumps({"success": False}), id="refusal-without-a-reason"),
        pytest.param(json.dumps({"success": "no", "message": "gone"}), id="non-boolean-success"),
        pytest.param(json.dumps(["not", "a", "dict"]), id="non-dict-result"),
    ],
)
def test_receipt_keeps_an_ambiguous_outcome_uncertain(result_text: str) -> None:
    # Only the exact refusal contract is decided. Anything else -- including a
    # failure that still carries a message id, meaning something was created --
    # must stay uncertain rather than be promoted to a definite non-delivery.
    stream = _sendmessage_refusal_stream(result_text)

    with pytest.raises(ChatError) as caught:
        parse_sendmessage_receipt(stream)

    assert not isinstance(caught.value, ClaudeSendMessageRefused)


@pytest.mark.parametrize(
    "tool_input_change",
    [
        pytest.param({"extra": "field"}, id="unknown-key"),
        pytest.param({"notify_when_idle": True}, id="unsupplied-schema-key"),
    ],
)
def test_pretool_gate_denies_a_proposal_carrying_a_key_it_does_not_supply(
    tool_input_change: dict[str, object],
) -> None:
    """The supplied arguments are expected to replace the proposal wholesale.

    Nothing here can observe whether a provider replaces or merges: every key the
    gate supplies wins either way, so only an UNSUPPLIED key would reveal the
    difference -- and under merge it would ride along onto a real delivery.
    Constraining the proposal's key set makes that question irrelevant instead of
    assumed, and it keeps holding across provider upgrades.
    """
    expected = _expected_gate("API work [ABC123]", "one exact body", SEND_SUMMARY)
    tool_input: dict[str, object] = {
        **authoritative_tool_input(
            COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
        )
    }
    tool_input.update(tool_input_change)

    phase, arguments = _pretool_resolution(expected, _gate_payload(tool_input))

    assert phase == "sendmessage_payload_mismatch"
    assert arguments is None


def test_pretool_gate_denies_an_expectation_over_the_product_message_bound() -> None:
    # The gate is the authority on delivered content, so it re-applies the
    # product's own bound rather than trusting whoever wrote the expectation.
    expected = _expected_gate("API work [ABC123]", "x" * (16 * 1024 + 1), SEND_SUMMARY)

    phase, arguments = _pretool_resolution(expected, _gate_payload())

    assert phase == "sendmessage_payload_mismatch"
    assert arguments is None


def _agents_listing_record(listing: str) -> str:
    """One stream-json record carrying a ListAgents tool_use_result."""
    return json.dumps({"tool_use_result": {"listing": listing}})


def _run_target_discovery(monkeypatch: pytest.MonkeyPatch, listing: str) -> None:
    from cross_agent_chat import claude_runtime

    monkeypatch.setattr(claude_runtime, "claude_binary", lambda: Path("/opt/claude"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, _agents_listing_record(listing), ""
        ),
    )


def test_listing_target_refs_pins_the_exact_row_shape() -> None:
    # Measured on Claude Code 2.1.274 and re-confirmed from the listing
    # builder embedded in the installed 2.1.278 binary: two leading spaces,
    # the printable `name [ref]` address, then `  ·  ` columns led by the
    # kind. This text form is the only documented SendMessage address for a
    # cross-session peer ("the name IS the address; there is no separate
    # address syntax" -- the provider's own tool description).
    assert _listing_target_refs("  API work [a1b2c3]  ·  interactive  ·  idle", "API work") == [
        "API work [a1b2c3]"
    ]
    assert _listing_target_refs("  API work [a1b2c3]  ·  bg  ·  busy", "API work") == [
        "API work [a1b2c3]"
    ]


def test_listing_target_refs_ignores_extra_unknown_columns() -> None:
    # The provider appends columns over time; they cannot change the selected
    # identity. A row carrying only the kind column is also inside the shape.
    listing = (
        "  API work [a1b2c3]  ·  interactive  ·  idle  ·  tmux main"
        '  ·  started 3m ago  ·  future {"json": 1} column\n'
        "  API work [a1b2c3]  ·  interactive"
    )
    assert _listing_target_refs(listing, "API work") == [
        "API work [a1b2c3]",
        "API work [a1b2c3]",
    ]


def test_listing_target_refs_reports_every_live_namesake_row() -> None:
    # Two live rows sharing one name are ambiguous, not a first-match win.
    listing = "  API work [a1b2c3]  ·  interactive  ·  idle\n  API work [d4e5f6]  ·  bg  ·  busy"
    assert _listing_target_refs(listing, "API work") == [
        "API work [a1b2c3]",
        "API work [d4e5f6]",
    ]


@pytest.mark.parametrize(
    "neighbor",
    [
        pytest.param("  API work [d4e5f6]  ·  stopped  ·  gone", id="stale-kind"),
        pytest.param("  API work  ·  interactive  ·  idle", id="no-ref"),
        pytest.param("  API work [d4e5f]  ·  interactive  ·  idle", id="short-token"),
        pytest.param("  API work [d4e5f67]  ·  interactive  ·  idle", id="long-token"),
        pytest.param("API work [d4e5f6]  ·  interactive  ·  idle", id="no-indent"),
        pytest.param("   API work [d4e5f6]  ·  interactive  ·  idle", id="wide-indent"),
        pytest.param("  API work [d4e5f6] · interactive · idle", id="wrong-separator"),
        pytest.param("  Other work [d4e5f6]  ·  interactive  ·  idle", id="other-name"),
    ],
)
def test_listing_target_refs_skips_nonconforming_namesakes(neighbor: str) -> None:
    # A namesake row outside the measured shape never competes with the exact
    # live row, and never fails the targeted lookup: it is skipped, not
    # approximated.
    listing = f"  API work [a1b2c3]  ·  interactive  ·  idle\n{neighbor}"
    assert _listing_target_refs(listing, "API work") == ["API work [a1b2c3]"]


def test_listing_target_refs_counts_a_trailing_space_namesake_as_ambiguous() -> None:
    # A trailing space still fits the measured row shape, so the row counts as
    # a live namesake and discovery fails as ambiguous rather than risk the
    # wrong target.
    listing = (
        "  API work [a1b2c3]  ·  interactive  ·  idle\n"
        "  API work [d4e5f6]  ·  interactive  ·  idle "
    )
    assert _listing_target_refs(listing, "API work") == [
        "API work [a1b2c3]",
        "API work [d4e5f6]",
    ]


def test_discover_target_ref_rejects_live_namesakes(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_target_discovery(
        monkeypatch,
        "  API work [a1b2c3]  ·  interactive  ·  idle\n"
        "  API work [d4e5f6]  ·  interactive  ·  busy",
    )
    with pytest.raises(ChatError, match="exact supported match"):
        discover_target_ref("API work")


def test_discover_target_ref_skips_stale_and_malformed_neighbors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_target_discovery(
        monkeypatch,
        "Sessions you can message:\n"
        "  API work [d4e5f6]  ·  stopped  ·  gone\n"
        "  API work [a1b2c3]  ·  interactive  ·  idle  ·  tmux main\n"
        "  Other work [ffffff]  ·  bg  ·  busy",
    )
    assert discover_target_ref("API work") == "API work [a1b2c3]"


def test_targeted_claude_agents_selects_the_exact_session_over_a_namesake(
    tmp_path: Path,
) -> None:
    session_id = str(uuid4())
    payload = json.dumps(
        [
            {
                "sessionId": session_id,
                "name": "API work",
                "kind": "interactive",
                "cwd": str(tmp_path),
            },
            {
                "sessionId": str(uuid4()),
                "name": "API work",
                "kind": "interactive",
                "cwd": str(tmp_path),
            },
        ]
    )
    assert _parse_targeted_claude_agents(payload, session_id) == [
        {
            "session_id": session_id,
            "name": "API work",
            "kind": "interactive",
            "cwd": str(tmp_path.resolve()),
        }
    ]


def test_targeted_claude_agents_skips_malformed_neighbors(tmp_path: Path) -> None:
    session_id = str(uuid4())
    payload = json.dumps(
        [
            "not a dict",
            {"noSessionId": "unrelated"},
            {"sessionId": 7},
            {"sessionId": str(uuid4()), "name": "Other work"},
            {
                "sessionId": session_id,
                "name": "API work",
                "kind": "interactive",
                "cwd": str(tmp_path),
            },
        ]
    )
    assert _parse_targeted_claude_agents(payload, session_id) == [
        {
            "session_id": session_id,
            "name": "API work",
            "kind": "interactive",
            "cwd": str(tmp_path.resolve()),
        }
    ]


def test_targeted_claude_agents_ignores_unknown_fields_on_the_selected_row(
    tmp_path: Path,
) -> None:
    # The provider adds fields over time (status, pid, startedAt); they must
    # not change the selected identity.
    session_id = str(uuid4())
    payload = json.dumps(
        [
            {
                "sessionId": session_id,
                "name": "API work",
                "kind": "interactive",
                "cwd": str(tmp_path),
                "status": "busy",
                "pid": 1234,
                "startedAt": 1789839828243,
                "future": {"nested": ["unknown", 1]},
            }
        ]
    )
    assert _parse_targeted_claude_agents(payload, session_id) == [
        {
            "session_id": session_id,
            "name": "API work",
            "kind": "interactive",
            "cwd": str(tmp_path.resolve()),
        }
    ]


def test_targeted_claude_agents_rejects_a_malformed_selected_row(tmp_path: Path) -> None:
    # A row matching the requested session id but missing a required field is
    # not skipped like a neighbor: the selected identity itself is unusable,
    # so the lookup fails rather than guess.
    session_id = str(uuid4())
    payload = json.dumps([{"sessionId": session_id, "kind": "interactive", "cwd": str(tmp_path)}])
    with pytest.raises(ChatError, match="invalid"):
        _parse_targeted_claude_agents(payload, session_id)


def _sendmessage_result_stream(result: object) -> str:
    """Provider stream logging one SendMessage use plus one JSON result text."""
    use = {
        "type": "tool_use",
        "id": "tool-1",
        "name": "SendMessage",
        "input": authoritative_tool_input(
            COURIER_PLACEHOLDER_TARGET, COURIER_PLACEHOLDER_MESSAGE, SEND_SUMMARY
        ),
    }
    block = {
        "type": "tool_result",
        "tool_use_id": "tool-1",
        "content": [{"type": "text", "text": json.dumps(result)}],
    }
    return "\n".join(json.dumps({"message": {"content": [b]}}) for b in (use, block))


@pytest.mark.parametrize(
    "extra_key",
    [
        "error",
        "errorClass",
        "error_class",
        "failure",
        "denied",
        "refused",
        "rejected",
        "status",
        "state",
        "delivered",
        "degradedClass",
        "degraded_class",
        "queued",
        "recipient",
        "target",
        "to",
        "session_id",
        "sessionId",
        "agent_id",
        "agentId",
        "id",
        "message_id",
        "request_id",
        "display",
        "inlineHandback",
        "latency_ms",
        "routing",
        "pin",
        "resumedAgentId",
        "errorCode",
        "note",
    ],
)
def test_sendmessage_receipt_rejects_any_extra_key(extra_key: str) -> None:
    # The receipt is the only evidence of delivery and the parser has no
    # expected recipient to check an extra field against, so ANY key beside
    # the success triple -- authoritative-looking or not -- is uncertain,
    # never success. Fails on the rejected additive-tolerance extension.
    message_id = str(uuid4())
    receipt = {
        "success": True,
        "message": "sent",
        "msg_id": message_id,
        extra_key: "unexpected",
    }

    with pytest.raises(ChatError, match="receipt"):
        parse_sendmessage_receipt(_sendmessage_result_stream(receipt))


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param({"success": True, "message": "sent"}, id="missing-msg-id"),
        pytest.param({"success": True, "msg_id": "ID"}, id="missing-message"),
        pytest.param({"success": True, "message": "sent", "msg_id": ""}, id="empty-msg-id"),
        pytest.param({"success": True, "message": "sent", "msg_id": 7}, id="nonstring-msg-id"),
        pytest.param({"success": True, "message": 7, "msg_id": "ID"}, id="nonstring-message"),
        pytest.param(
            {"success": "yes", "message": "sent", "msg_id": "ID"},
            id="nonboolean-success",
        ),
    ],
)
def test_sendmessage_receipt_rejects_incomplete_or_mistyped_contracts(
    receipt: dict[str, object],
) -> None:
    message_id = str(uuid4())
    shaped = {key: (message_id if value == "ID" else value) for key, value in receipt.items()}

    with pytest.raises(ChatError, match="invalid"):
        parse_sendmessage_receipt(_sendmessage_result_stream(shaped))


@pytest.mark.parametrize(
    "receipt",
    [
        pytest.param({"success": False, "message": "gone", "routing": {}}, id="additive-refusal"),
        pytest.param(
            {"success": False, "message": "gone", "request_id": "r1"},
            id="protocol-refusal",
        ),
        pytest.param(
            {"success": False, "message": "gone", "status": "refused"},
            id="authority-refusal",
        ),
    ],
)
def test_sendmessage_receipt_keeps_a_noncanonical_refusal_unknown(
    receipt: dict[str, object],
) -> None:
    # The decided refusal contract is exactly {success, message} -- the shape
    # observed on Claude Code 2.1.274 and still emitted by 2.1.278. A refusal
    # carrying anything else is not that contract and stays unknown.
    with pytest.raises(ChatError) as caught:
        parse_sendmessage_receipt(_sendmessage_result_stream(receipt))

    assert not isinstance(caught.value, ClaudeSendMessageRefused)


def test_sendmessage_helper_timeout_is_body_free_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, SEND_TIMEOUT_SECONDS)

    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    body = "private body that must not be echoed"
    with pytest.raises(ClaudeSendMessageUnknownDelivery) as error:
        sendmessage("API work [ABC123]", body, Path("/usr/bin/false"))

    assert error.value.phase == "helper_timeout"
    assert body not in str(error.value)
