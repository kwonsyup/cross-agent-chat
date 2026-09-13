from __future__ import annotations

import hashlib
import json
import os
import textwrap
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.codex import native_account_digest
from cross_agent_chat.core import ChatError, Registry, Route
from cross_agent_chat.native_helper import NativeHelperStore

ACCOUNT_EMAIL = "canary@example.invalid"
ACCOUNT_DIGEST = hashlib.sha256(
    f"cross-agent-chat:codex-account:v1\0{ACCOUNT_EMAIL}".encode()
).hexdigest()


def fake_codex_binary(tmp_path: Path, mode: str = "valid") -> tuple[Path, Path, Path]:
    home = tmp_path / "codex-home"
    home.mkdir()
    trace = tmp_path / "stdio.jsonl"
    binary = tmp_path / "fake-codex"
    binary.write_text(
        f"#!{os.sys.executable}\n"
        + textwrap.dedent(
            """
            import json, os, sys
            mode = os.environ["TEST_MODE"]
            email = os.environ["TEST_EMAIL"]
            trace = open(os.environ["TEST_TRACE"], "a", encoding="utf-8")
            for line in sys.stdin:
                request = json.loads(line)
                trace.write(json.dumps(request, sort_keys=True) + "\\n")
                trace.flush()
                if request.get("id") == 0:
                    response = {
                        "id": 0,
                        "result": {
                            "codexHome": os.environ["CODEX_HOME"]
                            if mode != "wrong-home"
                            else "/wrong-home"
                        },
                    }
                elif request.get("id") == 1:
                    if mode == "error":
                        response = {
                            "id": 1,
                            "error": {"code": -32000, "message": "unavailable"},
                        }
                    elif mode == "null":
                        response = {
                            "id": 1,
                            "result": {"requiresOpenaiAuth": True, "account": None},
                        }
                    else:
                        account_type = "apiKey" if mode == "api-key" else "chatgpt"
                        response = {
                            "id": 1,
                            "result": {
                                "requiresOpenaiAuth": True,
                                "account": {"type": account_type, "email": email},
                            },
                        }
                else:
                    continue
                print(json.dumps(response), flush=True)
            trace.close()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary, home, trace


@pytest.mark.parametrize("mode", ["null", "api-key", "error", "wrong-home"])
def test_native_account_digest_rejects_invalid_nested_account_read(
    tmp_path: Path, mode: str
) -> None:
    binary, home, trace = fake_codex_binary(tmp_path, mode)
    with pytest.raises(ChatError) as error:
        native_account_digest(
            binary=binary,
            environment={
                "CODEX_HOME": str(home),
                "TEST_TRACE": str(trace),
                "TEST_MODE": mode,
                "TEST_EMAIL": ACCOUNT_EMAIL,
            },
        )
    assert "Codex account identity is unavailable" in str(error.value)
    requests = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    initialize = next(request for request in requests if request.get("id") == 0)
    assert initialize["method"] == "initialize"
    if mode != "wrong-home":
        account_read = next(request for request in requests if request.get("id") == 1)
        assert account_read == {
            "id": 1,
            "method": "account/read",
            "params": {"refreshToken": False},
        }


def test_native_account_digest_reads_exact_chatgpt_account_without_refresh(
    tmp_path: Path,
) -> None:
    binary, home, trace = fake_codex_binary(tmp_path)
    digest = native_account_digest(
        binary=binary,
        environment={
            "CODEX_HOME": str(home),
            "TEST_TRACE": str(trace),
            "TEST_MODE": "valid",
            "TEST_EMAIL": ACCOUNT_EMAIL,
        },
    )

    assert digest == ACCOUNT_DIGEST
    requests = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert [request.get("method") for request in requests] == [
        "initialize",
        "initialized",
        "account/read",
    ]
    account_read = requests[-1]
    assert account_read["params"] == {"refreshToken": False}


def test_native_account_binary_is_the_exact_chatgpt_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=1,
        owner_identity=ACCOUNT_DIGEST,
        profile_root=str(tmp_path / "profile"),
    )
    Path(route.profile_root or "").mkdir()
    monkeypatch.setattr(
        runtime,
        "recipient_owner_identity",
        lambda *_args: (ACCOUNT_DIGEST, Path("/usr/local/bin/codex")),
    )
    monkeypatch.setattr(runtime, "native_desktop_process", lambda _pid: True)

    assert (
        runtime._native_account_binary(route)
        == Path("/Applications/ChatGPT.app/Contents/Resources/codex").resolve()
    )


def startup_route(root: Path, *, session_id: str | None = None) -> Route:
    profile = root / "profile"
    original_cwd = root / "original"
    profile.mkdir(parents=True, exist_ok=True)
    original_cwd.mkdir(parents=True, exist_ok=True)
    return Route.create(
        provider="codex",
        session_id=session_id or str(uuid4()),
        device="studio",
        cwd=str(original_cwd),
        pid=1,
        owner_identity=ACCOUNT_DIGEST,
        profile_root=str(profile),
    )


def patch_startup(
    monkeypatch: pytest.MonkeyPatch,
    route: Route,
    *,
    desktop: bool = True,
    owner_identity: str = ACCOUNT_DIGEST,
) -> None:
    monkeypatch.setattr(
        runtime,
        "hook_input",
        lambda _event: {"session_id": route.session_id, "cwd": route.cwd},
    )
    monkeypatch.setattr(runtime, "_route_current", lambda *_args: True)
    monkeypatch.setattr(runtime, "native_desktop_process", lambda _pid: desktop)
    monkeypatch.setattr(
        runtime,
        "recipient_owner_identity",
        lambda *_args: (owner_identity, Path("/usr/local/bin/codex")),
    )


def test_native_startup_offers_context_only_to_eligible_unprovisioned_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    route = startup_route(tmp_path)
    Registry(root).upsert(route)
    patch_startup(monkeypatch, route)

    assert runtime.native_startup(root, "studio", route.pid) == {
        "additionalContext": (
            "Cross Agent Chat needs its native delivery helper. Call native_bootstrap once now."
        )
    }


def test_native_startup_is_noop_for_cli_and_untrusted_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    route = startup_route(tmp_path)
    Registry(root).upsert(route)

    patch_startup(monkeypatch, route, desktop=False)
    assert runtime.native_startup(root, "studio", route.pid) == {}

    patch_startup(monkeypatch, route, owner_identity="b" * 64)
    assert runtime.native_startup(root, "studio", route.pid) == {}


def test_native_startup_is_noop_for_helper_and_registered_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    original = startup_route(tmp_path)
    registry = Registry(root)
    registry.upsert(original)
    binding, nonce = NativeHelperStore(root).reserve(original, ACCOUNT_DIGEST)
    helper_cwd = tmp_path / binding.helper_directory
    helper_cwd.mkdir()
    helper = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(helper_cwd),
        pid=1,
        owner_identity=ACCOUNT_DIGEST,
        profile_root=original.profile_root,
    )
    registry.upsert(helper)
    NativeHelperStore(root).register(helper, nonce, original, ACCOUNT_DIGEST)

    patch_startup(monkeypatch, helper)
    assert runtime.native_startup(root, "studio", helper.pid) == {}
    patch_startup(monkeypatch, original)
    assert runtime.native_startup(root, "studio", original.pid) == {}
