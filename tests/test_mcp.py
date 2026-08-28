from __future__ import annotations

import pytest

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
