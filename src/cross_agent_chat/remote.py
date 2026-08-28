"""Strict parsing for the trusted Tailnet peer boundary."""

from __future__ import annotations

import json
from typing import cast

from cross_agent_chat.core import ChatError, bounded_message, valid_name, valid_uuid


def parse_remote_envelope(text: str) -> tuple[str, str, str, str, str, str]:
    if len(text.encode()) > 64 * 1024:
        raise ChatError("remote envelope is invalid")
    try:
        raw = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChatError("remote envelope is invalid") from error
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "event_id",
        "source_alias",
        "source_generation",
        "target_alias",
        "generation",
        "message",
    }:
        raise ChatError("remote envelope is invalid")
    if raw.get("schema_version") != 1:
        raise ChatError("remote envelope is invalid")
    values = cast(dict[str, object], raw)
    if not all(
        isinstance(values[key], str)
        for key in (
            "event_id",
            "source_alias",
            "source_generation",
            "target_alias",
            "generation",
            "message",
        )
    ):
        raise ChatError("remote envelope is invalid")
    return (
        valid_uuid(cast(str, values["event_id"]), "event id"),
        valid_name(cast(str, values["source_alias"]), "source alias"),
        valid_uuid(cast(str, values["source_generation"]), "source generation"),
        valid_name(cast(str, values["target_alias"]), "target alias"),
        valid_uuid(cast(str, values["generation"]), "target generation"),
        bounded_message(cast(str, values["message"])),
    )
