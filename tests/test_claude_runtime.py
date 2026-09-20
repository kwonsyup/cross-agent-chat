"""Claude courier outcome classification: decided pre-effect proof requirements.

The unconsumed-gate path may only conclude a decided non-delivery when the
helper stream is semantically complete: it ends with the provider's terminal
``result`` record, and the tool graph before it is closed and ordered -- every
tool_result names one earlier SendMessage use, every use is SendMessage (the
courier's only tool), and nothing contradicts the evidence. An absent or
partial terminal, records after the terminal, orphan or duplicate records, a
receipt still carrying an effect marker, or an uncheckable payload all keep
the outcome unknown.
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


def _terminal_record(**overrides: object) -> dict[str, object]:
    """The provider's final stream record, measured on Claude Code 2.1.278."""
    record: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "result_index": 0,
        "queued_turn_count": 0,
        "stop_reason": "end_turn",
        "terminal_reason": "completed",
        "permission_denials": [],
    }
    record.update(overrides)
    return record


def _init_record() -> dict[str, object]:
    return {"type": "system", "subtype": "init", "session_id": str(uuid4())}


def _record(
    *blocks: dict[str, object],
    record_type: str = "assistant",
    role: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {"content": list(blocks)}
    if role is not None:
        message["role"] = role
    return {"type": record_type, "message": message}


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
    is_error: object = None,
    content: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    block: dict[str, object] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": (
            content if content is not None else [{"type": "text", "text": json.dumps(payload)}]
        ),
    }
    if is_error is not None:
        block["is_error"] = is_error
    return block


def _success_receipt(tool_use_id: str) -> dict[str, object]:
    return _result(tool_use_id, {"success": True, "message": "sent", "msg_id": str(uuid4())})


def _stream(*records: dict[str, object]) -> str:
    return "\n".join(json.dumps(record) for record in records)


def _outcome(
    monkeypatch: pytest.MonkeyPatch,
    stream: str,
    *,
    denied: bytes | None = None,
    consumed: bool = False,
) -> ChatError | None:
    """Run one courier send against a synthetic stream and capture its outcome.

    ``denied`` writes the gate's private denied marker and ``consumed`` the
    consumed marker inside the fake run, exactly where the real hook would
    have written them. Returns the raised classification error, or None when
    the send was accepted.
    """

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        settings = json.loads(command[command.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        tokens = shlex.split(hook)
        expected_path = Path(tokens[tokens.index("--expected") + 1])
        if denied is not None:
            _write_private_marker(expected_path.parent / "denied", denied)
        if consumed:
            _write_private_marker(expected_path.parent / "consumed", b"consumed\n")
        return subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(
        "cross_agent_chat.claude_runtime.claude_binary", lambda: Path("/usr/bin/false")
    )
    monkeypatch.setattr("cross_agent_chat.claude_runtime.subprocess.run", run)
    try:
        sendmessage(_TARGET, _BODY, Path("/usr/bin/false"))
    except ChatError as error:
        assert _BODY not in str(error)
        return error
    return None


def _assert_unknown(error: ChatError | None, phase: ClaudeUnknownPhase) -> None:
    assert isinstance(error, ClaudeSendMessageUnknownDelivery)
    assert isinstance(error, UnknownDeliveryError)
    assert error.phase == phase


def _assert_refused(error: ChatError | None, reason_part: str) -> None:
    assert isinstance(error, ClaudeSendMessageRefused)
    assert not isinstance(error, UnknownDeliveryError)
    assert reason_part in str(error)


# ---------------------------------------------------------------------------
# Terminal record: no decided refusal without the provider's final result.


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param("", id="empty-stream"),
        pytest.param(_stream(_init_record()), id="init-only"),
        pytest.param(_stream(_record(_sendmessage_use())), id="use-without-terminal"),
        pytest.param(
            _stream(
                _record(_sendmessage_use()),
                _record(
                    _result("tool-1", {"success": False, "message": "gone"}),
                    record_type="user",
                ),
            ),
            id="use-and-refusal-without-terminal",
        ),
    ],
)
def test_absent_terminal_is_unknown(monkeypatch: pytest.MonkeyPatch, stream: str) -> None:
    # A stream with no terminal provider result cannot prove completion, even
    # when every record it does carry is consistent with a denial.
    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


def test_absent_terminal_without_markers_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _outcome(monkeypatch, _stream(_init_record()))

    _assert_unknown(error, "helper_stream_invalid")


@pytest.mark.parametrize(
    "records",
    [
        pytest.param(
            [
                _terminal_record(),
                _record(_sendmessage_use()),
            ],
            id="terminal-then-use",
        ),
        pytest.param(
            [
                _record(_sendmessage_use()),
                _terminal_record(),
                _record(_result("tool-1", {"success": False, "message": "gone"})),
            ],
            id="result-after-terminal",
        ),
        pytest.param(
            [
                _record(_sendmessage_use()),
                _terminal_record(),
                _terminal_record(),
            ],
            id="duplicate-terminal",
        ),
        pytest.param(
            [
                _record(_sendmessage_use()),
                _terminal_record(subtype="interrupted"),
                _record(_result("tool-1", {"success": False, "message": "gone"})),
            ],
            id="nonfinal-result-then-records",
        ),
    ],
)
def test_terminal_must_be_the_single_final_record(
    monkeypatch: pytest.MonkeyPatch, records: list[dict[str, object]]
) -> None:
    error = _outcome(monkeypatch, _stream(*records), denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


@pytest.mark.parametrize(
    "terminal",
    [
        pytest.param({"type": "result"}, id="type-only"),
        pytest.param({**_terminal_record(), "is_error": "no"}, id="mistyped-is-error"),
        pytest.param(
            {k: v for k, v in _terminal_record().items() if k != "terminal_reason"},
            id="missing-terminal-reason",
        ),
    ],
)
def test_partial_or_unknown_terminal_is_unknown(
    monkeypatch: pytest.MonkeyPatch, terminal: dict[str, object]
) -> None:
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result("tool-1", {"success": False, "message": "gone"}),
            record_type="user",
        ),
        terminal,
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


# ---------------------------------------------------------------------------
# Ordered tool graph: every result names one earlier SendMessage use.


def test_result_before_its_use_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _stream(
        _record(_result("tool-1", {"success": False, "message": "gone"}), record_type="user"),
        _record(_sendmessage_use("tool-1")),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "helper_stream_invalid")


def test_orphan_success_receipt_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # No tool_use at all but a success receipt carrying a message id: the use
    # record may be missing, and the receipt is evidence of an effect.
    stream = _stream(
        _init_record(),
        _record(_success_receipt("tool-1"), record_type="user"),
        _terminal_record(),
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
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(_result("tool-9", payload), record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "helper_stream_invalid")


def test_duplicate_use_id_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(_sendmessage_use("tool-1")),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "helper_stream_invalid")


def test_duplicate_result_for_one_use_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result("tool-1", {"success": False, "message": "gone"})
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(result, result, record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "helper_stream_invalid")


def test_multiple_sendmessage_uses_are_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(_sendmessage_use("tool-2")),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "multiple_sendmessage_tool_use")


def test_denied_marker_with_multiple_uses_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Multiple calls cannot be reconciled to the single gated action, even
    # when the gate denied them.
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(_sendmessage_use("tool-2")),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "multiple_sendmessage_tool_use")


def test_denied_marker_with_foreign_tool_use_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The courier is restricted to SendMessage; any other observed tool means
    # the stream is not the courier's own and cannot be checked.
    stream = _stream(
        _record(_sendmessage_use(name="ListAgents")),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


def test_denied_marker_with_mismatched_success_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A success receipt the stream cannot correlate to any observed use is
    # still delivery evidence: the use record may have been truncated away.
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(_success_receipt("tool-9"), record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


# ---------------------------------------------------------------------------
# Contradictions and unknown shapes: evidence of a possible effect or records
# that cannot be checked keep the outcome unknown.


def test_denied_marker_with_correlated_success_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(
        _record(_sendmessage_use()),
        _record(_success_receipt("tool-1"), record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "pretool_gate_conflict")


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
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "pretool_gate_conflict")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"success": False, "message": "gone", "routing": {"table": 1}},
            id="unknown-extra-field",
        ),
        pytest.param({"success": "maybe", "message": "gone"}, id="nonboolean-success"),
        pytest.param({"success": False}, id="refusal-without-reason"),
        pytest.param(["not", "a", "dict"], id="non-dict-payload"),
    ],
)
def test_denied_marker_with_unknown_result_shape_is_unknown(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    # A result that is not the measured canonical refusal cannot be proven to
    # carry no effect; only the exact {success,message[,display]} contract is
    # decided denial evidence.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(_result("tool-1", payload), record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param([{"type": "text", "text": "not-json"}], id="non-json-text"),
        pytest.param([], id="empty-content"),
        pytest.param(
            [
                {"type": "text", "text": json.dumps({"success": False, "message": "a"})},
                {"type": "text", "text": json.dumps({"success": True, "message": "b"})},
            ],
            id="two-text-blocks",
        ),
    ],
)
def test_denied_marker_with_uncheckable_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch, content: list[dict[str, object]]
) -> None:
    stream = _stream(
        _record(_sendmessage_use()),
        _record(_result("tool-1", None, content=content), record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


def test_denied_marker_with_arbitrary_error_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An is_error tool_result carrying arbitrary text is not non-effect
    # evidence: its payload cannot be scanned for a receipt, so the denial
    # cannot be proven.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result("tool-1", None, is_error=True, content=[{"type": "text", "text": "denied"}]),
            record_type="user",
        ),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


def test_denied_marker_with_bare_use_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The marker proves the gate was told to deny; it does not prove the
    # provider's denial completed. A use with no result is an incomplete call,
    # not a measured denial.
    stream = _stream(
        _record(_sendmessage_use()),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


def test_denied_marker_with_success_and_refusal_results_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two results answering the same use break the ordered tool graph before
    # either payload can be trusted: the stream itself is inconsistent.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result("tool-1", {"success": False, "message": "gone"}),
            _success_receipt("tool-1"),
            record_type="user",
        ),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


def test_unobserved_gate_with_use_and_mismatched_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(
        _record(_sendmessage_use("tool-1")),
        _record(_result("tool-9", {"success": False, "message": "gone"}), record_type="user"),
        _terminal_record(),
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
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "pretool_gate_unobserved")


# ---------------------------------------------------------------------------
# Closed tool-result container: the shared analyzer may not filter away or
# reinterpret blocks, keys or flags it cannot check, on either outcome path.


def _assert_unknown_on_both_paths(monkeypatch: pytest.MonkeyPatch, stream: str) -> None:
    denied = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])
    _assert_unknown(denied, "helper_stream_invalid")
    consumed = _outcome(monkeypatch, stream, consumed=True)
    _assert_unknown(consumed, "receipt_invalid")


@pytest.mark.parametrize("is_error", [True, "yes", 1], ids=["true", "string", "int"])
def test_is_error_result_is_unknown_on_both_paths(
    monkeypatch: pytest.MonkeyPatch, is_error: object
) -> None:
    # A true or non-boolean is_error may conceal arbitrary provider output; it
    # cannot be checked for effect evidence on either path.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result("tool-1", {"success": False, "message": "gone"}, is_error=is_error),
            record_type="user",
        ),
        _terminal_record(),
    )

    _assert_unknown_on_both_paths(monkeypatch, stream)


def test_consumed_gate_with_null_is_error_receipt_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An explicit JSON null is_error is a present non-boolean flag, not an
    # absent one: it may conceal an errored result the payload cannot check,
    # so an otherwise exact success receipt stays uncheckable.
    result = _success_receipt("tool-1")
    result["is_error"] = None
    stream = _stream(
        _record(_sendmessage_use()),
        _record(result, record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, consumed=True)

    _assert_unknown(error, "receipt_invalid")


def test_denied_marker_with_null_is_error_refusal_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same present null flag on the canonical refusal payload: the denial
    # cannot be decided from a result block whose error flag cannot be checked.
    reason = "No agent named 'API work [ABC123]' is reachable."
    result = _result("tool-1", {"success": False, "message": reason})
    result["is_error"] = None
    stream = _stream(
        _record(_sendmessage_use()),
        _record(result, record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_unknown(error, "helper_stream_invalid")


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": False, "message": "gone"}),
                    },
                    {"type": "image", "source": {"data": "..."}},
                ],
            },
            id="unknown-non-text-sibling",
        ),
        pytest.param(
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": False, "message": "gone"}),
                        "extra": 1,
                    }
                ],
            },
            id="extra-text-block-key",
        ),
        pytest.param(
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "cache_control": {"type": "ephemeral"},
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": False, "message": "gone"}),
                    }
                ],
            },
            id="extra-result-block-key",
        ),
        pytest.param(
            {
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": [{"type": "text"}],
            },
            id="text-block-missing-text-key",
        ),
    ],
)
def test_unclosed_result_block_is_unknown_on_both_paths(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, object]
) -> None:
    # The measured result block is exactly {type, tool_use_id, content} plus an
    # optional boolean is_error, and its content is exactly one {type, text}
    # block. Sibling blocks or unlisted keys are never filtered away.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(result, record_type="user"),
        _terminal_record(),
    )

    _assert_unknown_on_both_paths(monkeypatch, stream)


@pytest.mark.parametrize(
    "records",
    [
        pytest.param(
            [
                _record(_sendmessage_use(), record_type="user"),
                _terminal_record(),
            ],
            id="tool-use-in-user-record",
        ),
        pytest.param(
            [
                _record(_sendmessage_use()),
                _record(
                    _result("tool-1", {"success": False, "message": "gone"}),
                    record_type="assistant",
                ),
                _terminal_record(),
            ],
            id="tool-result-in-assistant-record",
        ),
        pytest.param(
            [
                _record(_sendmessage_use()),
                _record(
                    _result("tool-1", {"success": False, "message": "gone"}),
                    {"type": "text", "text": "note"},
                    record_type="user",
                ),
                _terminal_record(),
            ],
            id="result-with-inert-sibling",
        ),
        pytest.param(
            [
                _record(_sendmessage_use(), role="user"),
                _terminal_record(),
            ],
            id="assistant-record-with-user-role",
        ),
        pytest.param(
            [
                _record(_sendmessage_use(), role="assistant"),
                _record(
                    _result("tool-1", {"success": False, "message": "gone"}),
                    record_type="user",
                    role="assistant",
                ),
                _terminal_record(),
            ],
            id="user-record-with-assistant-role",
        ),
        pytest.param(
            [
                {"type": "bogus"},
                _record(_sendmessage_use()),
                _terminal_record(),
            ],
            id="unknown-outer-type",
        ),
        pytest.param(
            [
                {"type": "future_effect", "msg_id": str(uuid4())},
                _record(_sendmessage_use()),
                _terminal_record(),
            ],
            id="unknown-outer-type-with-effect-key",
        ),
        pytest.param(
            [
                _record({"type": "image", "source": {"data": "..."}}),
                _record(_sendmessage_use()),
                _terminal_record(),
            ],
            id="unknown-content-block-type",
        ),
        pytest.param(
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "message": {
                        "content": [{"type": "tool_use", "id": "tool-9", "name": "SendMessage"}]
                    },
                },
                _record(_sendmessage_use()),
                _terminal_record(),
            ],
            id="tool-block-in-metadata-record",
        ),
    ],
)
def test_wrong_role_or_unknown_shape_is_unknown_on_both_paths(
    monkeypatch: pytest.MonkeyPatch, records: list[dict[str, object]]
) -> None:
    # Tool evidence belongs to its measured role: uses in assistant records,
    # results alone in user records, nothing tool-bearing elsewhere. Unknown
    # record or block types are uncheckable even without a recognized tool.
    _assert_unknown_on_both_paths(monkeypatch, _stream(*records))


# ---------------------------------------------------------------------------
# Decided controls: complete measured evidence stays decided or accepted.


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param(_stream(_terminal_record()), id="terminal-only"),
        pytest.param(_stream(_init_record(), _terminal_record()), id="init-and-terminal"),
    ],
)
def test_complete_stream_without_tool_records_is_decided(
    monkeypatch: pytest.MonkeyPatch, stream: str
) -> None:
    # A complete stream with no tool records proves the courier finished
    # without a SendMessage call -- the only delivery path.
    error = _outcome(monkeypatch, stream)

    _assert_refused(error, "SendMessage")


def test_denied_marker_with_complete_stream_and_no_uses_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _outcome(
        monkeypatch,
        _stream(_init_record(), _terminal_record()),
        denied=DENIAL_MARKERS["pretool_gate_denied"],
    )

    _assert_refused(error, "denied")


def test_denied_marker_with_canonical_refusal_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The measured provider denial: gate marker plus the complete stream's
    # canonical {success,message} refusal for the observed call.
    reason = "No agent named 'API work [ABC123]' is reachable."
    stream = _stream(
        _record(_sendmessage_use()),
        _record(_result("tool-1", {"success": False, "message": reason}), record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_refused(error, reason)


def test_denied_marker_with_display_refusal_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "No agent named 'API work [ABC123]' is reachable."
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result(
                "tool-1",
                {"success": False, "message": reason, "display": f"Error: {reason}"},
            ),
            record_type="user",
        ),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_refused(error, reason)


def test_unobserved_gate_with_bare_use_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(_record(_sendmessage_use()), _terminal_record())

    error = _outcome(monkeypatch, stream)

    _assert_unknown(error, "pretool_gate_unobserved")


def test_consumed_gate_with_exact_success_receipt_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(
        _record(_sendmessage_use()),
        _record(_success_receipt("tool-1"), record_type="user"),
        _terminal_record(),
    )

    assert _outcome(monkeypatch, stream, consumed=True) is None


def test_consumed_gate_with_matched_success_and_orphan_result_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A valid matched receipt cannot be accepted when the same stream carries
    # a second, uncorrelatable result: the stream is inconsistent.
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _success_receipt("tool-1"),
            _result("tool-9", {"success": False, "message": "gone"}),
            record_type="user",
        ),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, consumed=True)

    _assert_unknown(error, "receipt_invalid")


def test_consumed_gate_without_terminal_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(
        _record(_sendmessage_use()),
        _record(_success_receipt("tool-1"), record_type="user"),
    )

    error = _outcome(monkeypatch, stream, consumed=True)

    _assert_unknown(error, "receipt_invalid")


def test_consumed_gate_with_msg_id_refusal_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _stream(
        _record(_sendmessage_use()),
        _record(
            _result(
                "tool-1",
                {"success": False, "message": "unclear", "msg_id": str(uuid4())},
            ),
            record_type="user",
        ),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, consumed=True)

    _assert_unknown(error, "receipt_invalid")


def test_consumed_gate_with_canonical_refusal_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "No agent named 'API work [ABC123]' is reachable."
    stream = _stream(
        _record(_sendmessage_use()),
        _record(_result("tool-1", {"success": False, "message": reason}), record_type="user"),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, consumed=True)

    _assert_refused(error, reason)


def test_consumed_gate_with_measured_metadata_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Harmless variation inside the measured contract stays accepted: a
    # rate_limit_event record, volatile outer metadata keys, agreeing message
    # roles, inert text/reasoning blocks and an explicit is_error:false.
    stream = _stream(
        _init_record(),
        {"type": "rate_limit_event", "rate_limit": {"resets_at": 1758.0}},
        {
            **_record(
                {"type": "thinking", "thinking": "planning"},
                {"type": "redacted_thinking", "data": "..."},
                _sendmessage_use(),
                role="assistant",
            ),
            "uuid": str(uuid4()),
        },
        {
            **_record(
                _result(
                    "tool-1",
                    {"success": True, "message": "sent", "msg_id": str(uuid4())},
                    is_error=False,
                ),
                record_type="user",
                role="user",
            ),
            "parent_tool_use_id": None,
        },
        _terminal_record(duration_ms=5),
    )

    assert _outcome(monkeypatch, stream, consumed=True) is None


def test_denied_marker_with_measured_metadata_is_decided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reason = "No agent named 'API work [ABC123]' is reachable."
    stream = _stream(
        {"type": "rate_limit_event", "rate_limit": {"resets_at": 1758.0}},
        _record(
            {"type": "text", "text": "calling the tool"},
            _sendmessage_use(),
            role="assistant",
        ),
        _record(
            _result("tool-1", {"success": False, "message": reason}, is_error=False),
            record_type="user",
            role="user",
        ),
        _terminal_record(),
    )

    error = _outcome(monkeypatch, stream, denied=DENIAL_MARKERS["pretool_gate_denied"])

    _assert_refused(error, reason)
