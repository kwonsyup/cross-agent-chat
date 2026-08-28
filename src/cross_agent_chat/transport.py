"""Strict remote message envelopes."""

from __future__ import annotations

import json

from cross_agent_chat.core import bounded_message, valid_name, valid_uuid


def remote_envelope(
    *,
    event_id: str,
    source_alias: str,
    source_generation: str,
    target_alias: str,
    generation: str,
    message: str,
) -> str:
    payload = {
        "schema_version": 1,
        "event_id": valid_uuid(event_id, "event id"),
        "source_alias": valid_name(source_alias, "source alias"),
        "source_generation": valid_uuid(source_generation, "source generation"),
        "target_alias": valid_name(target_alias, "target alias"),
        "generation": valid_uuid(generation, "target generation"),
        "message": bounded_message(message),
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
