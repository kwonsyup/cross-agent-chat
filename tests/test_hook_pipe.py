from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import ChatError


def test_native_startup_finishes_while_provider_keeps_stdin_open(tmp_path: Path) -> None:
    # The owning app-server writes a complete event but may wait for hook exit
    # before closing its pipe. A reader that requires EOF deadlocks this caller.
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src"))
    process = subprocess.Popen(
        [sys.executable, "-c",
         "import json,sys; from pathlib import Path; "
         "from cross_agent_chat.runtime import native_startup; "
         "print(json.dumps(native_startup(Path(sys.argv[1]), 'studio', int(sys.argv[2]))))",
         str(tmp_path / "state"), str(os.getpid())],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
    )
    assert process.stdin is not None
    try:
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": str(uuid4()),
                   "cwd": str(tmp_path), "prompt": 'nested { } and escaped " quote 😃',
                   "nested": {"values": [{"field": "}"}]}}
        process.stdin.write(json.dumps(payload, ensure_ascii=False, indent=2).encode())
        process.stdin.flush()
        process.wait(timeout=2)
        assert process.returncode == 0
        assert process.stdout is not None
        assert json.loads(process.stdout.read()) == {}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdin.close()


@pytest.mark.parametrize("event", ["UserPromptSubmit", "SessionStart"])
def test_framed_hook_preserves_event_and_identity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: str,
) -> None:
    payload = {"hook_event_name": event, "session_id": str(uuid4()), "cwd": str(tmp_path)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert runtime.hook_input(event)["session_id"] == payload["session_id"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(ChatError, match="hook event is invalid"):
        runtime.hook_input("SessionEnd")
    payload["session_id"] = "not-a-session"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(ChatError):
        runtime.hook_input(event)


def test_framed_hook_keeps_byte_bound_and_refuses_incomplete_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "MAX_FRAME_BYTES", 32)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"text":"' + "😃" * 8 + '"}'))
    with pytest.raises(ChatError, match="bounded limit"):
        runtime.hook_input("UserPromptSubmit")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"field":'))
    with pytest.raises(ChatError, match="not JSON"):
        runtime.hook_input("UserPromptSubmit")
