from __future__ import annotations

import io
import json
import re
import subprocess
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.cli import mcp
from cross_agent_chat.core import ChatError, Registry, Route
from cross_agent_chat.native_helper import NativeHelperStore

PUBLIC_TOOLS = {"chat_peers", "chat_send", "chat_status"}


def route(
    root: Path,
    *,
    session_id: str | None = None,
    device: str = "studio",
    profile_root: Path | None = None,
) -> Route:
    profile = root / "profile" if profile_root is None else profile_root
    profile.mkdir(parents=True, exist_ok=True)
    return Route.create(
        provider="codex",
        session_id=session_id or str(uuid4()),
        device=device,
        cwd=str(root),
        pid=1,
        owner_identity="a" * 64,
        profile_root=str(profile),
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_normal_provider_tools_list_exposes_only_public_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str,
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"),
    )

    mcp(provider, "studio", str(tmp_path / "state"))

    response = json.loads(capsys.readouterr().out)
    assert {tool["name"] for tool in response["result"]["tools"]} == PUBLIC_TOOLS


def test_verified_desktop_catalog_adds_internal_names_without_list_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr("cross_agent_chat.cli.native_desktop_mcp_host", lambda: True)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"),
    )

    mcp("codex", "studio", str(tmp_path / "state"))

    response = json.loads(capsys.readouterr().out)
    tools = response["result"]["tools"]
    assert {tool["name"] for tool in tools} == PUBLIC_TOOLS | {
        "native_bootstrap",
        "native_register",
        "native_dispatch",
    }
    schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
    assert schemas["native_bootstrap"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert schemas["native_register"] == {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
        "additionalProperties": False,
    }


def test_catalog_rejects_bundled_cli_and_counterfeit_originator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setenv("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "Codex Desktop")
    monkeypatch.setattr("cross_agent_chat.cli.native_desktop_mcp_host", lambda: False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"),
    )

    mcp("codex", "studio", str(tmp_path / "state"))

    response = json.loads(capsys.readouterr().out)
    assert {tool["name"] for tool in response["result"]["tools"]} == PUBLIC_TOOLS


def test_desktop_catalog_requires_chatgpt_ancestor_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cross_agent_chat.runtime.os.getppid", lambda: 200)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.recipient_owner_identity",
        lambda _provider, _pid: ("owner", Path("/usr/local/bin/codex")),
    )
    monkeypatch.setattr(
        "cross_agent_chat.runtime.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "100\n", ""),
    )

    assert not runtime.native_desktop_mcp_host()


def test_desktop_catalog_accepts_chatgpt_ancestor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cross_agent_chat.runtime.os.getppid", lambda: 200)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.recipient_owner_identity",
        lambda _provider, pid: (
            "owner",
            Path("/usr/local/bin/codex")
            if pid == 200
            else Path("/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"),
        ),
    )
    monkeypatch.setattr(
        "cross_agent_chat.runtime.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "100\n", ""),
    )

    assert runtime.native_desktop_mcp_host()


def test_native_helper_visibility_tracks_unknown_binding_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    original = route(tmp_path, session_id="00000000-0000-4000-8000-000000000001")
    registry = Registry(state_root)
    registry.upsert(original)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    monkeypatch.setattr(runtime, "_native_account_digest", lambda *_args: "a" * 64)

    assert runtime.native_helper_tools(state_root, original) == ("native_bootstrap",)

    binding, nonce = NativeHelperStore(state_root).reserve(original, "a" * 64)
    assert binding.state == "UNKNOWN"
    assert runtime.native_helper_tools(state_root, original) == ()

    helper_root = tmp_path / binding.helper_directory
    helper_root.mkdir()
    assert original.profile_root is not None
    helper = route(
        helper_root,
        session_id="00000000-0000-4000-8000-000000000002",
        profile_root=Path(original.profile_root),
    )
    registry.upsert(helper)
    assert runtime.native_helper_tools(state_root, helper) == ("native_register",)

    runtime.native_register(state_root, helper, nonce)
    assert runtime.native_helper_tools(state_root, helper) == ("native_dispatch",)
    assert runtime.native_helper_tools(state_root, original) == ()


def test_internal_bootstrap_call_requires_host_thread_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "native_bootstrap", "arguments": {}},
                }
            )
            + "\n"
        ),
    )

    mcp("codex", "studio", str(tmp_path / "state"))

    response = json.loads(capsys.readouterr().out)
    assert "error" not in response
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == "Codex host thread identity is required"


def test_internal_bootstrap_mcp_call_preserves_private_create_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread_id = "00000000-0000-4000-8000-000000000006"
    expected = {
        "content": [{"type": "text", "text": "Native delivery helper setup was submitted."}],
        "_meta": {
            "create_thread": {
                "target": {"type": "projectless", "directoryName": "cac-native-helper-test"},
                "model": "gpt-5.6-luna",
            }
        },
    }
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr("cross_agent_chat.cli.native_desktop_mcp_host", lambda: True)
    monkeypatch.setattr("cross_agent_chat.cli.authenticate_mcp_sender", lambda *_args: object())
    monkeypatch.setattr("cross_agent_chat.cli.native_bootstrap", lambda *_args: expected)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "native_bootstrap",
                        "arguments": {},
                        "_meta": {"threadId": thread_id},
                    },
                }
            )
            + "\n"
        ),
    )

    mcp("codex", "studio", str(tmp_path / "state"))

    response = json.loads(capsys.readouterr().out)
    assert response == {"jsonrpc": "2.0", "id": 1, "result": expected}


def test_failed_internal_mcp_call_is_tool_error_and_does_not_become_jsonrpc_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread_id = "00000000-0000-4000-8000-000000000007"
    monkeypatch.delenv("CROSS_AGENT_CHAT_PRESENCE", raising=False)
    monkeypatch.setattr("cross_agent_chat.cli.native_desktop_mcp_host", lambda: True)
    monkeypatch.setattr("cross_agent_chat.cli.authenticate_mcp_sender", lambda *_args: object())
    monkeypatch.setattr(
        "cross_agent_chat.cli.native_bootstrap",
        lambda *_args: (_ for _ in ()).throw(ChatError("native helper bootstrap is unavailable")),
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "native_bootstrap",
                        "arguments": {},
                        "_meta": {"threadId": thread_id},
                    },
                }
            )
            + "\n"
        ),
    )

    mcp("codex", "studio", str(tmp_path / "state"))

    response = json.loads(capsys.readouterr().out)
    assert "error" not in response
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["type"] == "text"
    assert "native helper bootstrap is unavailable" in response["result"]["content"][0]["text"]


def test_mcp_bootstrap_register_duplicate_journey_is_single_and_nonrecursive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    original = route(tmp_path / "original", session_id="00000000-0000-4000-8000-000000000008")
    Registry(state_root).upsert(original)
    routes = {original.session_id: original}
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    monkeypatch.setattr(runtime, "_native_account_digest", lambda *_args: "a" * 64)
    monkeypatch.setattr(runtime, "_native_hook_ready", lambda *_args: True)
    monkeypatch.setattr("cross_agent_chat.cli.native_desktop_mcp_host", lambda: True)

    def authenticate(_root: Path, provider: str, _parent_pid: int, thread_id: str | None) -> Route:
        assert provider == "codex"
        assert thread_id is not None
        return routes[thread_id]

    monkeypatch.setattr("cross_agent_chat.cli.authenticate_mcp_sender", authenticate)
    bootstrap_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "native_bootstrap",
            "arguments": {},
            "_meta": {"threadId": original.session_id},
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bootstrap_request) + "\n"))
    mcp("codex", "studio", str(state_root))
    bootstrap_response = json.loads(capsys.readouterr().out)
    bootstrap_result = bootstrap_response["result"]
    create_thread = bootstrap_result["_meta"]["create_thread"]
    helper_directory = create_thread["target"]["directoryName"]
    prompt = create_thread["prompt"]
    token_match = re.search(r"token ([0-9a-f-]{36})", prompt)
    assert token_match is not None
    token = token_match.group(1)
    assert helper_directory == NativeHelperStore(state_root).bindings()[0].helper_directory

    helper_root = tmp_path / helper_directory
    helper_root.mkdir()
    assert original.profile_root is not None
    helper = route(
        helper_root,
        session_id="00000000-0000-4000-8000-000000000009",
        profile_root=Path(original.profile_root),
    )
    Registry(state_root).upsert(helper)
    routes[helper.session_id] = helper

    register_request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "native_register",
            "arguments": {"token": token},
            "_meta": {"threadId": helper.session_id},
        },
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(register_request) + "\n"))
    mcp("codex", "studio", str(state_root))
    register_response = json.loads(capsys.readouterr().out)
    assert register_response["result"] == {
        "content": [{"type": "text", "text": "Native helper is ready."}]
    }
    assert runtime.native_helper_tools(state_root, helper) == ("native_dispatch",)

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(bootstrap_request) + "\n"))
    mcp("codex", "studio", str(state_root))
    duplicate_response = json.loads(capsys.readouterr().out)
    assert duplicate_response["result"]["isError"] is True
    assert "bootstrap is unavailable" in duplicate_response["result"]["content"][0]["text"]
    bindings = NativeHelperStore(state_root).bindings()
    assert len(bindings) == 1
    assert bindings[0].state == "REGISTERED"


def test_native_bootstrap_is_one_effect_and_returns_private_projectless_create_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    original = route(tmp_path, session_id="00000000-0000-4000-8000-000000000003")
    Registry(state_root).upsert(original)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    monkeypatch.setattr(runtime, "_native_account_digest", lambda *_args: "a" * 64)
    monkeypatch.setattr(runtime, "_native_hook_ready", lambda *_args: True)

    result = runtime.native_bootstrap(state_root, original)
    assert result["content"] == [
        {"type": "text", "text": "Native delivery helper setup was submitted."}
    ]
    assert "structuredContent" not in result
    metadata = cast(dict[str, object], result["_meta"])
    create_thread = cast(dict[str, object], metadata["create_thread"])
    target = cast(dict[str, object], create_thread["target"])
    assert target["type"] == "projectless"
    assert cast(str, target["directoryName"]).startswith("cac-native-helper-")
    assert NativeHelperStore(state_root).bindings()[0].state == "UNKNOWN"

    with pytest.raises(ChatError, match="bootstrap"):
        runtime.native_bootstrap(state_root, original)
    assert NativeHelperStore(state_root).bindings()[0].state == "UNKNOWN"


def test_native_bootstrap_create_args_keep_their_installed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the private create_thread arguments independently of their producer."""

    state_root = tmp_path / "state"
    original = route(tmp_path, session_id="00000000-0000-4000-8000-00000000000a")
    Registry(state_root).upsert(original)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    monkeypatch.setattr(runtime, "_native_account_digest", lambda *_args: "a" * 64)
    monkeypatch.setattr(runtime, "_native_hook_ready", lambda *_args: True)

    result = runtime.native_bootstrap(state_root, original)

    binding = NativeHelperStore(state_root).bindings()[0]
    metadata = cast(dict[str, object], result["_meta"])
    prompt = cast(str, cast(dict[str, object], metadata["create_thread"])["prompt"])
    token_match = re.search(r"token ([0-9a-f-]{36})", prompt)
    assert token_match is not None
    assert result == {
        "content": [{"type": "text", "text": "Native delivery helper setup was submitted."}],
        "_meta": {
            "create_thread": {
                "prompt": (
                    "You are the Cross Agent Chat native delivery helper. "
                    f"Call native_register once with token {token_match.group(1)}, "
                    "then wait for inbound work. "
                    "Do not inspect memory, source files, or unrelated tasks."
                ),
                "target": {"type": "projectless", "directoryName": binding.helper_directory},
                "model": "gpt-5.6-luna",
                "thinking": "high",
                "title": "Cross Agent Chat helper",
            }
        },
    }


def test_failed_registration_preserves_unknown_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    original = route(tmp_path, session_id="00000000-0000-4000-8000-000000000004")
    wrong_profile = tmp_path / "other-profile"
    wrong_helper = route(
        tmp_path,
        session_id="00000000-0000-4000-8000-000000000005",
        profile_root=wrong_profile,
    )
    registry = Registry(state_root)
    registry.upsert(original)
    registry.upsert(wrong_helper)
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    monkeypatch.setattr(runtime, "_native_account_digest", lambda *_args: "a" * 64)
    _, nonce = NativeHelperStore(state_root).reserve(original, "a" * 64)

    with pytest.raises(ChatError, match="context"):
        runtime.native_register(state_root, wrong_helper, nonce)
    bindings = NativeHelperStore(state_root).bindings()
    assert len(bindings) == 1
    assert bindings[0].state == "UNKNOWN"
    assert bindings[0].helper_session_id is None
