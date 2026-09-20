"""Claude courier outcome classification: decided pre-effect proof requirements.

The unconsumed-gate path may only conclude a decided non-delivery when the
helper stream is semantically complete: every tool_result correlates to an
observed tool_use, every use is SendMessage (the courier's only tool), and no
record contradicts the evidence. An orphan receipt, a mismatched success, a
result still carrying a message id, or an uncheckable record is possible
effect evidence and keeps the outcome unknown.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat.claude_runtime import (
    DENIAL_MARKERS,
    SEND_SUMMARY,
    ClaudeSendMessageRefused,
    ClaudeSendMessageUnknownDelivery,
    ClaudeUnknownPhase,
    sendmessage,
)
from cross_agent_chat.core import ChatError, UnknownDeliveryError

_TARGET = "API work [ABC123]"
_BODY = "private body that must not be echoed"


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


def _record(*blocks: dict[str, object], record_type: str = "assistant") -> dict[str, object]:
    return {"type": record_type, "message": {"content": list(blocks)}}


def _sendmessage_use(identifier: str = "tool-1", name: str = "SendMessage") -> dict[str, object]:
    return {
        "type": "tool_use",
        "id": identifier,
        "name": name,
        "input": {
            "to": _TARGET,
            "recipient": _TARGET,
            "message": _BODY,
            "content": _BODY,
            "type": "message",
            "summary": SEND_SUMMARY,
        },
    }


def _result(
    tool_use_id: str,
    payload: object,
    *,
    is_error: bool = False,
    content: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    block: dict[str, object] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": (
            content if content is not None else [{"type": "text", "text": json.dumps(payload)}]
        ),
    }
    if is_error:
        block["is_error"] = True
    return block


def _stream(*records: dict[str, object]) -> str:
    return "\n".join(json.dumps(record) for record in records)


def _outcome(
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
    *,
    denied: bytes | None = None,
) -> ChatError:
    """Run one courier send against a synthetic stream and capture its outcome.

    ``denied`` writes the gate's private denied marker inside the fake run,
    exactly where the real hook would have written it.
    """

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        if denied is not None:
            _write_private_marker(expected_path.parent / "denied", denied)
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    with pytest.raises(ChatError) as error:
        sendmessage(_TARGET, _BODY, Path("/usr/bin/false"))
    assert _BODY not in str(error.value)
    return error.value


def _assert_unknown(error: ChatError, phase: ClaudeUnknownPhase) -> None:
    assert isinstance(error, ClaudeSendMessageUnknownDelivery)
    assert isinstance(error, UnknownDeliveryError)
    assert error.phase == phase


def _assert_refused(error: ChatError, reason_part: str) -> None:
    assert isinstance(error, ClaudeSendMessageRefused)
    assert not isinstance(error, UnknownDeliveryError)
    assert reason_part in str(error)


# ---------------------------------------------------------------------------
# Decided controls: complete pre-effect evidence stays decided.


def test_denied_marker_with_bare_use_is_decided(monkeypatch: pytest.MonkeyPatch) -> None:
    # The marker proves the provider was told to deny; a use with no recorded
    # result is the expected shape of a denied call.
    error = _outcome(
        monkeypatch,
        _stream(_record(_sendmessage_use())),
        denied=DENIAL_MARKERS["pretool_gate_denied"],
    )

    _assert_refused(error, "denied")


def test_denied_marker_with_provider_refusal_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "No agent named 'API work [ABC123]' is reachable."
    stream = _stream(
        _record(_sendmessage_use()),
        _record(_result("tool-1", {"success": False, "message": reason}), record_type="user"),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_refused(error, reason)


def test_denied_marker_with_error_result_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An is_error tool_result is the provider's own denial surface: consistent
    # with the deny, carrying no parseable receipt to check.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result("tool-1", None, is_error=True, content=[{"type": "text", "text": "denied"}]),
            record_type="user",
        ),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_refused(error, "denied")


def test_denied_marker_with_empty_stream_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _outcome(monkeypatch, "", denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_refused(error, "denied")


def test_denied_marker_with_multiple_denied_uses_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every observed SendMessage call went through the same one-shot gate; an
    # allow would have written the consumed marker instead.
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(_sendmessage_use("tool-2")),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_refused(error, "denied")


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
def test_unobserved_gate_without_tool_records_is_decided(
    monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    # A semantically complete stream with no tool records at all proves the
    # courier finished without a SendMessage call -- the only delivery path.
    error = _outcome(monkeypatch, stream)

    _assert_refused(error, "SendMessage")


# ---------------------------------------------------------------------------
# Contradictions: evidence of a possible effect keeps the outcome unknown.


def test_denied_marker_with_correlated_success_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Control: a delivered receipt for the denied call itself contradicts the
    # marker and stays unknown.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result(
                "tool-1",
                {"success": True, "message": "sent", "msg_id": str(uuid4())},
            ),
            record_type="user",
        ),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "pretool_gate_conflict")


def test_denied_marker_with_mismatched_success_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A success receipt the stream cannot correlate to any observed use is
    # still delivery evidence: the use record may have been truncated away.
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(
            _result(
                "tool-9",
                {"success": True, "message": "sent", "msg_id": str(uuid4())},
            ),
            record_type="user",
        ),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


def test_denied_marker_with_msg_id_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A correlated result that reports failure yet still carries a message id
    # is contradictory: msg_id is the only effect marker in this protocol, so
    # something may have been created despite the deny.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result(
                "tool-1",
                {"success": False, "message": "unclear", "msg_id": str(uuid4())},
            ),
            record_type="user",
        ),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "pretool_gate_conflict")


def test_denied_marker_with_orphan_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even a refusal-shaped result that correlates to no observed use means
    # the stream is partial or inconsistent, so no pre-effect conclusion can
    # rely on it.
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(
            _result("tool-9", {"success": False, "message": "gone"}),
            record_type="user",
        ),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param([{"type": "text", "text": "not-json"}], id="non-json-text"),
        pytest.param([{"type": "text", "text": json.dumps(["a", "list"])}], id="non-dict-payload"),
        pytest.param(
            [
                {"type": "text", "text": json.dumps({"success": False, "message": "a"})},
                {"type": "text", "text": json.dumps({"success": True, "message": "b"})},
            ],
            id="two-text-blocks",
        ),
        pytest.param([], id="empty-content"),
    ],
)
def test_denied_marker_with_uncheckable_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch, content: list[dict[str, object]]
) -> None:
    # A correlated result that cannot be parsed cannot be proven to carry no
    # receipt, so the denial cannot be confirmed pre-effect.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(_result("tool-1", None, content=content), record_type="user"),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


def test_denied_marker_with_foreign_tool_use_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The courier is restricted to SendMessage; any other observed tool means
    # the stream is not the courier's own and cannot be checked.
    stream = _stream(
        _record(_sendmessage_use(name="ListAgents")),
        _record(_sendmessage_use("tool-2")),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


def test_denied_marker_with_success_and_refusal_results_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result("tool-1", {"success": False, "message": "gone"}),
            _result(
                "tool-1",
                {"success": True, "message": "sent", "msg_id": str(uuid4())},
            ),
            record_type="user",
        ),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "pretool_gate_conflict")


# ---------------------------------------------------------------------------
# Orphan records without a denied marker: a result with no use is a partial
# stream, never proof the courier finished without sending.


def test_orphan_success_receipt_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # No tool_use at all but a success receipt carrying a message id: the use
    # record may be missing, and the receipt is evidence of an effect.
    stream = _stream(
        _record(
            _result(
                "tool-1",
                {"success": True, "message": "sent", "msg_id": str(uuid4())},
            ),
            record_type="user",
        )
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "helper_stream_invalid")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"success": False, "message": "gone"}, id="orphan-refusal"),
        pytest.param({"success": True, "message": "sent"}, id="orphan-bare-success"),
    ],
)
def test_orphan_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    # Any result that correlates to no observed use -- effect-bearing or not --
    # proves the stream lost records, so it cannot decide non-delivery.
    stream = _stream(
        _record(_result("tool-9", payload), record_type="user"),
        _record(_sendmessage_use("tool-1")),
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "helper_stream_invalid")


def test_unobserved_gate_with_use_and_mismatched_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(_result("tool-9", {"success": False, "message": "gone"}), record_type="user"),
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "helper_stream_invalid")


def test_unobserved_gate_with_use_and_msg_id_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No markers at all: the gate was never observed, so even a contradictory
    # correlated result stays an unobserved-gate unknown.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result(
                "tool-1",
                {"success": False, "message": "unclear", "msg_id": str(uuid4())},
            ),
            record_type="user",
        ),
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "pretool_gate_unobserved")
