"""Bounded stdio MCP surface."""

from __future__ import annotations

from cross_agent_chat.core import ChatError, bounded_message

TARGET_FIELDS = ("to", "recipient", "destination")
REPLY_HINTS = ("wait_for_reply", "request_reply")


def normalize_send_arguments(arguments: dict[str, object]) -> tuple[str, str]:
    allowed = {*TARGET_FIELDS, "message", *REPLY_HINTS}
    unknown = set(arguments) - allowed
    if unknown:
        raise ChatError(f"unknown field: {sorted(unknown)[0]}")
    targets = [field for field in TARGET_FIELDS if field in arguments]
    if len(targets) != 1:
        raise ChatError("exactly one target field is required")
    for hint in REPLY_HINTS:
        if hint in arguments and arguments[hint] is not False:
            raise ChatError("blocking replies are not supported")
    target = arguments[targets[0]]
    message = arguments.get("message")
    if not isinstance(target, str) or not target.strip():
        raise ChatError("target is invalid")
    if not isinstance(message, str):
        raise ChatError("message is invalid")
    return target, bounded_message(message)
