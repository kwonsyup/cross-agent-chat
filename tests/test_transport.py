from __future__ import annotations

import json
from pathlib import Path

import pytest

from cross_agent_chat.core import ChatError, UnknownDeliveryError
from cross_agent_chat.runtime import request_socket
from cross_agent_chat.transport import remote_envelope


def test_remote_envelope_binds_target_generation_without_body_persistence() -> None:
    payload = remote_envelope(
        event_id="0f7f34eb-c7ca-4bd1-89b5-83e6301fba8b",
        source_alias="codex@source:api:456",
        source_generation="6c5c83f6-6de8-4b18-875b-96c4fe09f2d7",
        target_alias="codex@peer:api:123",
        generation="7d9ae03f-f86c-4c96-a40d-69f37f0a7189",
        message="hello",
    )

    decoded = json.loads(payload)
    assert decoded["generation"] == "7d9ae03f-f86c-4c96-a40d-69f37f0a7189"
    assert decoded["message"] == "hello"
    assert payload.endswith("\n")


def test_absent_courier_socket_is_pre_effect(tmp_path: Path) -> None:
    with pytest.raises(ChatError, match="before delivery") as error:
        request_socket(tmp_path / "not-bound.sock", {"operation": "health"})
    assert not isinstance(error.value, UnknownDeliveryError)
