"""JSON-RPC 2025-03-26 contract tests for the stdio MCP surface."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cross_agent_chat import cli
from cross_agent_chat.cli import mcp
from cross_agent_chat.core import ChatError, Route

CLIENT_INFO = {"name": "test-client", "version": "1.0"}


def _stdin(payload: str) -> io.StringIO:
    return io.StringIO(payload)


def _stdin_bytes(payload: bytes) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")


def _lines(*items: object) -> str:
    return "".join(f"{json.dumps(item)}\n" for item in items)


def _initialize(identifier: int = 0, *, version: str = "2025-03-26") -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": dict(CLIENT_INFO),
        },
    }


INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _handshake() -> list[object]:
    return [_initialize(), INITIALIZED]


def _responses(capsys: pytest.CaptureFixture[str]) -> list[Any]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def _patch_send(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str]]:
    source = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=1,
    )
    sent: list[tuple[str, str]] = []

    def record_send(_root: Path, _source: Route, target: str, message: str) -> dict[str, object]:
        sent.append((target, message))
        return {"schema_version": 1, "event_id": "e"}

    monkeypatch.setattr(cli, "authenticate_mcp_sender", lambda *_args: source)
    monkeypatch.setattr(cli, "reply_delivery", lambda *_args: "next_turn")
    monkeypatch.setattr(cli, "send", record_send)
    return sent


def test_full_initialize_initialized_list_call_journey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    source = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=1,
    )
    monkeypatch.setattr(cli, "authenticate_mcp_sender", lambda *_args: source)
    monkeypatch.setattr(cli, "peers", lambda *_args, **_kwargs: {"schema_version": 1})
    monkeypatch.setattr(cli, "sender_readiness", lambda *_args: {"status": "ready"})
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                _initialize(identifier=1),
                INITIALIZED,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "chat_peers", "arguments": {}},
                },
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    # The initialized notification produced no output line.
    assert len(responses) == 3
    initialize = responses[0]
    assert initialize["id"] == 1
    assert initialize["result"]["protocolVersion"] == "2025-03-26"
    assert initialize["result"]["serverInfo"]["name"] == "cross-agent-chat"
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} == {
        "chat_peers",
        "chat_send",
        "chat_status",
    }
    called = json.loads(responses[2]["result"]["content"][0]["text"])
    assert called["sender"] == {"status": "ready"}


def test_ping_is_supported_before_and_after_initialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                {"jsonrpc": "2.0", "id": "early", "method": "ping"},
                _initialize(identifier=1),
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert responses[0] == {"jsonrpc": "2.0", "id": "early", "result": {}}
    assert responses[2] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_requests_before_initialize_are_rejected_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    sent = _patch_send(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "chat_send",
                        "arguments": {"to": "a" * 64, "message": "hi"},
                    },
                },
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert sent == []
    assert responses[0]["error"]["code"] == -32600
    assert responses[1]["error"]["code"] == -32600
    assert "not initialized" in responses[0]["error"]["message"]


def test_initialized_notification_before_initialize_does_not_open_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                INITIALIZED,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    (response,) = _responses(capsys)
    assert response["error"]["code"] == -32600
    assert "not initialized" in response["error"]["message"]


def test_tools_call_between_initialize_and_initialized_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A valid initialize response alone does not open the operation phase."""
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    sent = _patch_send(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                _initialize(),
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "chat_send",
                        "arguments": {"to": "a" * 64, "message": "hi"},
                    },
                },
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert sent == []
    assert responses[0]["result"]["protocolVersion"] == "2025-03-26"
    assert responses[1]["error"]["code"] == -32600
    assert "not initialized" in responses[1]["error"]["message"]


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"protocolVersion": 20250326, "capabilities": {}, "clientInfo": CLIENT_INFO},
        {"protocolVersion": "", "capabilities": {}, "clientInfo": CLIENT_INFO},
        {"protocolVersion": "2025-03-26"},
        {"protocolVersion": "2025-03-26", "capabilities": [], "clientInfo": CLIENT_INFO},
        {"protocolVersion": "2025-03-26", "capabilities": {}},
        {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": "codex"},
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "codex"},
        },
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "codex", "version": 7},
        },
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"version": "1.0"},
        },
    ],
)
def test_malformed_initialize_params_are_rejected_and_do_not_open_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    params: object,
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    sent = _patch_send(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params},
                INITIALIZED,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "chat_send",
                        "arguments": {"to": "a" * 64, "message": "hi"},
                    },
                },
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert sent == []
    assert responses[0]["error"]["code"] == -32602
    # The failed initialize plus an early initialized notification still leaves
    # the session unopened, so the tool call is refused without dispatch.
    assert responses[1]["error"]["code"] == -32600


@pytest.mark.parametrize("version", ["2024-11-05", "2025-06-18", "1.0.0"])
def test_other_declared_versions_are_answered_with_the_supported_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    version: str,
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                _initialize(identifier=1, version=version),
                INITIALIZED,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert responses[0]["result"]["protocolVersion"] == "2025-03-26"
    # An older provider client that completes the handshake still operates.
    assert "tools" in responses[1]["result"]


def test_reinitialize_is_rejected_before_and_after_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                _initialize(identifier=1),
                _initialize(identifier=2),
                INITIALIZED,
                _initialize(identifier=3),
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert responses[0]["result"]["protocolVersion"] == "2025-03-26"
    assert responses[1]["error"]["code"] == -32600
    assert "already initialized" in responses[1]["error"]["message"]
    assert responses[2]["error"]["code"] == -32600
    assert "already initialized" in responses[2]["error"]["message"]


def test_mixed_batch_returns_one_array_without_notification_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    batch = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}},
        {"jsonrpc": "2.0", "id": "p", "method": "ping"},
    ]
    monkeypatch.setattr("sys.stdin", _stdin(_lines(_initialize(), batch)))

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    array = responses[1]
    assert [item["id"] for item in array] == [7, "p"]
    assert "tools" in array[0]["result"]
    assert array[1]["result"] == {}


def test_batched_initialize_cannot_unlock_a_tools_call_in_the_same_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    sent = _patch_send(monkeypatch, tmp_path)
    batch = [
        _initialize(identifier=1),
        INITIALIZED,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "chat_send",
                "arguments": {"to": "a" * 64, "message": "hi"},
            },
        },
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    monkeypatch.setattr("sys.stdin", _stdin(json.dumps(batch) + "\n"))

    mcp("claude", "studio", str(tmp_path / "state"))

    assert sent == []
    (array,) = _responses(capsys)
    assert array[0]["error"]["code"] == -32600
    assert "batch" in array[0]["error"]["message"]
    # The batched initialized notification is also ignored before a valid
    # initialize, so the tool call is refused without dispatch.
    assert array[1]["error"]["code"] == -32600
    assert array[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_batch_of_only_notifications_produces_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    batch = [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/unknown"},
    ]
    monkeypatch.setattr("sys.stdin", _stdin(json.dumps(batch) + "\n"))

    mcp("claude", "studio", str(tmp_path / "state"))

    assert _responses(capsys) == []


def test_empty_batch_is_a_single_invalid_request_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr("sys.stdin", _stdin("[]\n"))

    mcp("claude", "studio", str(tmp_path / "state"))

    assert _responses(capsys) == [
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    ]


def test_invalid_batch_element_gets_error_and_valid_sibling_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    batch = [42, {"jsonrpc": "2.0", "id": 1, "method": "ping"}]
    monkeypatch.setattr("sys.stdin", _stdin(json.dumps(batch) + "\n"))

    mcp("claude", "studio", str(tmp_path / "state"))

    (array,) = _responses(capsys)
    assert array[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "invalid request"},
    }
    assert array[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "opaque-page-2"},
        {"cursor": None},
        {"page": 1},
        {"_meta": 5},
        {"unknown": True},
    ],
)
def test_tools_list_rejects_cursors_and_arbitrary_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    params: object,
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": params,
    }
    monkeypatch.setattr("sys.stdin", _stdin(_lines(*_handshake(), request)))

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert responses[1]["error"]["code"] == -32602


def test_tools_list_accepts_absent_and_meta_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    requests = [
        *_handshake(),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {"_meta": {"threadId": "t"}},
        },
    ]
    monkeypatch.setattr("sys.stdin", _stdin(_lines(*requests)))

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert "tools" in responses[1]["result"]
    assert "tools" in responses[2]["result"]


@pytest.mark.parametrize("identifier", [None, True, 1.5, [1], {"a": 1}])
def test_invalid_request_ids_are_rejected_with_null_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    identifier: object,
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(json.dumps({"jsonrpc": "2.0", "id": identifier, "method": "ping"}) + "\n"),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    assert _responses(capsys) == [
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    ]


def test_request_id_cannot_be_reused_within_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert responses[0]["result"] == {}
    assert responses[1]["error"]["code"] == -32600
    assert "already used" in responses[1]["error"]["message"]


@pytest.mark.parametrize(
    "params",
    [[1, 2], None, "x"],
)
def test_non_object_params_are_invalid_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    params: object,
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params}
    monkeypatch.setattr("sys.stdin", _stdin(json.dumps(request) + "\n"))

    mcp("claude", "studio", str(tmp_path / "state"))

    assert _responses(capsys) == [
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid params"}}
    ]


def test_missing_method_and_wrong_jsonrpc_are_invalid_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                {"jsonrpc": "2.0", "id": 1},
                {"jsonrpc": "1.0", "id": 2, "method": "ping"},
            )
            + "not json at all\n"
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert responses[0] == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32600, "message": "invalid request"},
    }
    assert responses[1] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "invalid request"},
    }
    assert responses[2]["error"]["code"] == -32700


def test_unknown_and_cancel_notifications_get_no_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        _stdin(
            _lines(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 9},
                },
                {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
                {"jsonrpc": "2.0", "method": "anything/at_all"},
            )
        ),
    )

    mcp("claude", "studio", str(tmp_path / "state"))

    assert _responses(capsys) == []


def test_notification_shaped_method_with_an_id_is_method_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    request = {"jsonrpc": "2.0", "id": 1, "method": "notifications/cancelled"}
    monkeypatch.setattr("sys.stdin", _stdin(_lines(*_handshake(), request)))

    mcp("claude", "studio", str(tmp_path / "state"))

    assert _responses(capsys)[-1] == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32601, "message": "method not found"},
    }


def test_tools_call_notification_has_no_effect_and_no_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    sent = _patch_send(monkeypatch, tmp_path)
    notification = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "chat_send", "arguments": {"to": "a" * 64, "message": "hi"}},
    }
    monkeypatch.setattr("sys.stdin", _stdin(_lines(*_handshake(), notification)))

    mcp("claude", "studio", str(tmp_path / "state"))

    assert sent == []
    assert len(_responses(capsys)) == 1  # only the initialize response


def test_cancel_notification_cannot_recall_a_dispatched_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    sent = _patch_send(monkeypatch, tmp_path)
    batch = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "chat_send",
                "arguments": {"to": "a" * 64, "message": "hi"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 1},
        },
    ]
    monkeypatch.setattr("sys.stdin", _stdin(_lines(*_handshake(), batch)))

    mcp("claude", "studio", str(tmp_path / "state"))

    # The dispatch was already a possible effect, so the cancellation changes
    # nothing: the send ran once and its response was still emitted.
    assert sent == [("a" * 64, "hi")]
    responses = _responses(capsys)
    array = responses[-1]
    assert array[0]["id"] == 1
    assert json.loads(array[0]["result"]["content"][0]["text"])["event_id"] == "e"


def test_execution_failure_after_a_possible_effect_is_a_tool_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    source = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=1,
    )
    effects: list[str] = []

    def uncertain_send(*_args: object) -> dict[str, object]:
        effects.append("maybe-delivered")
        raise ChatError("delivery outcome is uncertain")

    monkeypatch.setattr(cli, "authenticate_mcp_sender", lambda *_args: source)
    monkeypatch.setattr(cli, "reply_delivery", lambda *_args: "next_turn")
    monkeypatch.setattr(cli, "send", uncertain_send)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "chat_send",
                "arguments": {"to": "a" * 64, "message": "hi"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "chat_send", "arguments": {"bogus": True}},
        },
    ]
    monkeypatch.setattr("sys.stdin", _stdin(_lines(*_handshake(), *requests)))

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert effects == ["maybe-delivered"]
    # Execution uncertainty is a tool result the client must not confuse with a
    # refused call; the malformed second call is a JSON-RPC invalid-params error.
    assert responses[1]["result"]["isError"] is True
    assert "uncertain" in responses[1]["result"]["content"][0]["text"]
    assert responses[2]["error"]["code"] == -32602


def test_oversized_frame_is_dropped_and_framing_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    oversized = '{"jsonrpc":"2.0","id":1,"method":"ping","pad":"' + "x" * 70000 + '"}'
    payload = oversized + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n"
    monkeypatch.setattr("sys.stdin", _stdin(payload))

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert responses[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "request exceeds the bounded limit"},
    }
    # The remainder of the dropped line was never dispatched as a frame.
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert len(responses) == 2


def test_multibyte_oversized_frame_is_dropped_by_encoded_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    # 20000 two-byte characters: under the character bound, over the byte bound.
    oversized = '{"pad":"' + "é" * 40000 + '"}'
    payload = oversized + "\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
    monkeypatch.setattr("sys.stdin", _stdin(payload))

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert responses[0]["error"]["code"] == -32700
    assert responses[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_malformed_utf8_frame_costs_one_frame_and_framing_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    valid = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    monkeypatch.setattr("sys.stdin", _stdin_bytes(b'\xff\xfe\n{"bad json\n' + valid + b"\n"))

    mcp("claude", "studio", str(tmp_path / "state"))

    responses = _responses(capsys)
    assert responses[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "parse error"},
    }
    assert responses[1]["error"]["code"] == -32700
    assert responses[2] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert len(responses) == 3
