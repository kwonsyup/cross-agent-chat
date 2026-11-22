"""KD-270: Claude-provider hooks invoked by a Devin harness are silent no-ops.

Devin CLI imports the installed Claude hooks through ``read_config_from`` and
fires them for its own lifecycle events; Devin documents no ``cwd`` field for
hook stdin.  The hook decides from its own invocation -- the invoker's
executable, or the envelope's own missing fields when the invoker is already
gone -- never from an inherited environment variable, so a real Claude
session nested under Devin still registers as itself.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from cross_agent_chat import claude_runtime, cli, runtime
from cross_agent_chat.core import ChatError, Registry, Route
from cross_agent_chat.install import (
    Installer,
    SettingsError,
    _hook_group,
    _owned_hook,
)

OWNER = "a" * 64
DEVIN_BINARY = Path("/opt/devin/bin/devin")
CLAUDE_BINARY = Path("/opt/claude/bin/claude")


def _devin_envelope(event: str) -> dict[str, object]:
    """The documented Devin lifecycle payload: session identity, no cwd."""

    envelope: dict[str, object] = {
        "hook_event_name": event,
        "session_id": str(uuid4()),
    }
    if event == "SessionStart":
        envelope["source"] = "startup"
    if event == "SessionEnd":
        envelope["reason"] = "complete"
    return envelope


def _claude_envelope(event: str, cwd: Path) -> dict[str, object]:
    envelope: dict[str, object] = {
        "hook_event_name": event,
        "session_id": str(uuid4()),
        "transcript_path": str(cwd / "transcript.jsonl"),
        "cwd": str(cwd),
    }
    if event == "SessionStart":
        envelope["source"] = "startup"
    return envelope


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _invoker(monkeypatch: pytest.MonkeyPatch, binary: Path | None) -> None:
    # The stub honours the pid argument: hooks pass their harness pid through,
    # and a lookup for any other pid must not silently claim the same invoker.
    monkeypatch.setattr(
        runtime,
        "_hook_invoker_binary",
        lambda pid: binary if pid == os.getpid() else None,
    )
    monkeypatch.setattr(runtime, "devin_binaries", lambda: (DEVIN_BINARY,))


def _registration_stubs(monkeypatch: pytest.MonkeyPatch, profile_root: Path) -> None:
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_args: (OWNER, Path("/bin/echo"))
    )
    monkeypatch.setattr(runtime, "recipient_profile_root", lambda _provider: str(profile_root))
    monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: None)


def test_claude_envelope_registers_as_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    root = tmp_path / "state"
    _feed_stdin(monkeypatch, _claude_envelope("SessionStart", cwd))
    _invoker(monkeypatch, CLAUDE_BINARY)
    _registration_stubs(monkeypatch, tmp_path / "profile")

    route = runtime.register("claude", "studio", os.getpid(), str(root))

    assert route is not None
    assert route.provider == "claude"
    assert [item.session_id for item in Registry(root).routes()] == [route.session_id]


def test_devin_invoked_claude_register_is_a_silent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The invoker decides before the envelope is even read: a Devin harness
    # firing this command never mints a claude route, whatever the payload.
    cwd = tmp_path / "project"
    cwd.mkdir()
    root = tmp_path / "state"
    _feed_stdin(monkeypatch, _claude_envelope("SessionStart", cwd))
    _invoker(monkeypatch, DEVIN_BINARY)

    assert runtime.register("claude", "studio", os.getpid(), str(root)) is None

    assert Registry(root).routes() == []


def test_dead_invoker_devin_envelope_still_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When the invoker is already gone, Devin's documented envelope -- valid
    # session identity and no cwd/transcript_path -- is the foreign signal.
    root = tmp_path / "state"
    _feed_stdin(monkeypatch, _devin_envelope("SessionStart"))
    _invoker(monkeypatch, None)

    assert runtime.register("claude", "studio", os.getpid(), str(root)) is None

    assert Registry(root).routes() == []


def test_foreign_envelope_is_a_distinct_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _feed_stdin(monkeypatch, _devin_envelope("SessionStart"))
    with pytest.raises(runtime.ForeignHookInput):
        runtime.hook_input("SessionStart")


def test_transcript_without_cwd_is_a_plain_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Boundary: session identity plus an owning-harness transcript_path but no
    # cwd is malformed input from the owning harness, not a foreign envelope.
    _feed_stdin(
        monkeypatch,
        {
            "hook_event_name": "SessionStart",
            "session_id": str(uuid4()),
            "transcript_path": "/tmp/t.jsonl",
        },
    )
    with pytest.raises(ChatError, match="lacks session identity or cwd") as error:
        runtime.hook_input("SessionStart")
    assert not isinstance(error.value, runtime.ForeignHookInput)


def test_hook_invoker_binary_resolves_the_running_process() -> None:
    # proc_pidpath reports the real interpreter image, not a venv shim.
    resolved = runtime._hook_invoker_binary(os.getpid())
    assert resolved is not None
    assert resolved.is_file()
    assert resolved.name.lower().startswith("python")


def test_devin_hook_invocation_matches_binary_name_without_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Detection cannot depend on an inherited PATH: a resolved executable
    # named devin counts even when devin_binaries() finds nothing.
    monkeypatch.setattr(runtime, "_hook_invoker_binary", lambda pid: Path("/elsewhere/devin"))
    monkeypatch.setattr(runtime, "devin_binaries", lambda: (DEVIN_BINARY,))
    assert runtime._devin_hook_invocation(os.getpid())


def test_malformed_owning_harness_envelope_keeps_the_plain_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An envelope carrying an owning-harness field but missing its session
    # identity is malformed input, not a foreign harness: it stays loud.
    _feed_stdin(
        monkeypatch,
        {"hook_event_name": "SessionStart", "transcript_path": "t", "cwd": str(tmp_path)},
    )
    with pytest.raises(ChatError, match="lacks session identity or cwd") as error:
        runtime.hook_input("SessionStart")
    assert not isinstance(error.value, runtime.ForeignHookInput)


def test_devin_invoked_claude_unregister_is_a_silent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _feed_stdin(monkeypatch, _devin_envelope("SessionEnd"))
    _invoker(monkeypatch, DEVIN_BINARY)

    runtime.unregister("claude", os.getpid(), str(root))

    _feed_stdin(monkeypatch, _devin_envelope("SessionEnd"))
    _invoker(monkeypatch, None)
    runtime.unregister("claude", os.getpid(), str(root))


def test_nested_claude_inherits_devin_env_and_still_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Claude launched inside a Devin leaf's shell inherits DEVIN_PROJECT_DIR;
    # its invoker is still the Claude process, so it registers as itself.
    workspace = tmp_path / "project"
    workspace.mkdir()
    root = tmp_path / "state"
    monkeypatch.setenv("DEVIN_PROJECT_DIR", str(workspace))
    _feed_stdin(monkeypatch, _claude_envelope("SessionStart", workspace))
    _invoker(monkeypatch, CLAUDE_BINARY)
    _registration_stubs(monkeypatch, tmp_path / "profile")

    route = runtime.register("claude", "studio", os.getpid(), str(root))

    assert route is not None
    assert route.provider == "claude"
    assert route.cwd == str(workspace.resolve())


def test_codex_foreign_envelope_keeps_the_loud_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The silent no-op is scoped to the claude hooks Devin actually imports.
    _feed_stdin(monkeypatch, _devin_envelope("SessionStart"))
    with pytest.raises(ChatError, match="lacks session identity or cwd"):
        runtime.register("codex", "studio", os.getpid(), str(tmp_path / "state"))


def test_devin_provider_hook_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("DEVIN_PROJECT_DIR", str(workspace))
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_args: (OWNER, Path("/bin/echo"))
    )
    _feed_stdin(monkeypatch, _devin_envelope("SessionStart"))

    # Devin's own hook still validates the documented envelope and defers
    # registration to the first trusted prompt.
    assert runtime.register_devin("studio", os.getpid(), str(tmp_path / "state")) is None

    _feed_stdin(monkeypatch, {"hook_event_name": "SessionStart"})
    with pytest.raises(ChatError, match="lacks session identity"):
        runtime.register_devin("studio", os.getpid(), str(tmp_path / "state"))


def test_cli_devin_invoked_register_and_unregister_exit_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _invoker(monkeypatch, DEVIN_BINARY)
    state = str(tmp_path / "state")

    _feed_stdin(monkeypatch, _devin_envelope("SessionStart"))
    assert (
        cli.main(
            [
                "_register",
                "--provider",
                "claude",
                "--device",
                "studio",
                "--pid",
                str(os.getpid()),
                "--state-root",
                state,
            ]
        )
        == 0
    )
    _feed_stdin(monkeypatch, _devin_envelope("SessionEnd"))
    assert (
        cli.main(
            [
                "_unregister",
                "--provider",
                "claude",
                "--pid",
                str(os.getpid()),
                "--state-root",
                state,
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_owned_hook_sees_through_the_devin_env_guard() -> None:
    group = _hook_group(Path("/opt/cross-agent-chat"), "claude", "studio", "SessionStart")
    guarded = copy.deepcopy(group)
    hook = cast(list[dict[str, object]], guarded["hooks"])[0]
    hook["command"] = '[ -n "$DEVIN_PROJECT_DIR" ] && exit 0; ' + str(hook["command"])

    assert _owned_hook(guarded)


def test_setup_replaces_the_devin_guarded_workaround(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        providers=("claude",),
    )
    installer.claude_settings.parent.mkdir(parents=True)
    guarded: dict[str, object] = {"theme": "dark", "hooks": {}}
    hook_map = cast(dict[str, object], guarded["hooks"])
    for event in ("SessionStart", "SessionEnd"):
        group = _hook_group(installer.executable, "claude", "studio", event)
        guarded_group = copy.deepcopy(group)
        hook = cast(list[dict[str, object]], guarded_group["hooks"])[0]
        hook["command"] = '[ -n "$DEVIN_PROJECT_DIR" ] && exit 0; ' + str(hook["command"])
        hook_map[event] = [guarded_group, {"hooks": [{"type": "command", "command": "keep-me"}]}]
    installer.claude_settings.write_text(json.dumps(guarded))

    installer.setup()

    settings = json.loads(installer.claude_settings.read_text())
    for event in ("SessionStart", "SessionEnd"):
        groups = settings["hooks"][event]
        assert any(item["hooks"][0]["command"] == "keep-me" for item in groups)
        owned = [item for item in groups if _owned_hook(item)]
        assert len(owned) == 1
        command = owned[0]["hooks"][0]["command"]
        assert "DEVIN_PROJECT_DIR" not in command
        expected = cast(
            list[dict[str, object]],
            _hook_group(installer.executable, "claude", "studio", event)["hooks"],
        )[0]
        assert command == expected["command"]


def test_setup_collapses_a_guarded_and_unguarded_twin(tmp_path: Path) -> None:
    # A setup run under an installer that could not see the guard appended an
    # unguarded twin beside the guarded command. Both are ours: the merge must
    # collapse them into one clean entry, not refuse on ambiguous ownership.
    home = tmp_path / "home"
    installer = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        providers=("claude",),
    )
    installer.claude_settings.parent.mkdir(parents=True)
    guarded: dict[str, object] = {"theme": "dark", "hooks": {}}
    hook_map = cast(dict[str, object], guarded["hooks"])
    for event in ("SessionStart", "SessionEnd"):
        group = _hook_group(installer.executable, "claude", "studio", event)
        guarded_group = copy.deepcopy(group)
        hook = cast(list[dict[str, object]], guarded_group["hooks"])[0]
        hook["command"] = '[ -n "$DEVIN_PROJECT_DIR" ] && exit 0; ' + str(hook["command"])
        hook_map[event] = [
            guarded_group,
            group,
            {"hooks": [{"type": "command", "command": "keep-me"}]},
        ]
    installer.claude_settings.write_text(json.dumps(guarded))

    installer.setup()

    settings = json.loads(installer.claude_settings.read_text())
    for event in ("SessionStart", "SessionEnd"):
        groups = settings["hooks"][event]
        assert any(item["hooks"][0]["command"] == "keep-me" for item in groups)
        owned = [item for item in groups if _owned_hook(item)]
        assert len(owned) == 1
        assert "DEVIN_PROJECT_DIR" not in owned[0]["hooks"][0]["command"]


def test_setup_still_refuses_two_unguarded_owned_entries(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        providers=("claude",),
    )
    installer.claude_settings.parent.mkdir(parents=True)
    group = _hook_group(installer.executable, "claude", "studio", "SessionStart")
    installer.claude_settings.write_text(
        json.dumps({"hooks": {"SessionStart": [group, copy.deepcopy(group)]}})
    )

    with pytest.raises(SettingsError, match="ownership is ambiguous"):
        installer.setup()


def _claude_route(tmp_path: Path, name: str) -> Route:
    cwd = tmp_path / name
    cwd.mkdir()
    return Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(cwd),
        pid=os.getpid(),
        owner_identity=OWNER,
        profile_root=str(tmp_path / "profile"),
    )


def _no_exact_agent(*_args: object, **_kwargs: object) -> claude_runtime.ClaudeAgent:
    raise ChatError("Claude target is not one exact live supported session")


def _unlisted_exact_agent(*_args: object, **_kwargs: object) -> claude_runtime.ClaudeAgent:
    raise claude_runtime.ClaudeAgentUnlisted(
        "Claude target is not one exact live supported session"
    )


def test_exact_agent_marks_an_absent_session_unlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_runtime, "claude_agents", lambda *a, **k: [])
    with pytest.raises(claude_runtime.ClaudeAgentUnlisted):
        claude_runtime.exact_agent(str(uuid4()), "/tmp")


def test_exact_agent_row_under_another_kind_is_a_plain_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A listed row that cannot be the target is uncertain, not decided: only a
    # session with no row at all is unlisted.
    session_id = str(uuid4())
    monkeypatch.setattr(
        claude_runtime,
        "claude_agents",
        lambda *a, **k: [
            {"session_id": session_id, "name": "hidden-1", "kind": "hidden", "cwd": "/tmp"}
        ],
    )
    with pytest.raises(ChatError) as error:
        claude_runtime.exact_agent(session_id, "/tmp")
    assert not isinstance(error.value, claude_runtime.ClaudeAgentUnlisted)


def test_exact_agent_listed_row_with_drifted_cwd_is_a_plain_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A live interactive row under a different cwd (cwd drift) is a real row;
    # it must not masquerade as an unlisted, decided-unsupported session.
    session_id = str(uuid4())
    monkeypatch.setattr(
        claude_runtime,
        "claude_agents",
        lambda *a, **k: [
            {
                "session_id": session_id,
                "name": "drift-1",
                "kind": "interactive",
                "cwd": "/var/elsewhere",
            }
        ],
    )
    with pytest.raises(ChatError) as error:
        claude_runtime.exact_agent(session_id, "/tmp")
    assert not isinstance(error.value, claude_runtime.ClaudeAgentUnlisted)


def test_targeted_parse_preserves_a_row_whose_cwd_cannot_resolve() -> None:
    # A listed row with a dead cwd still counts as present, so presence is
    # decided only by an inventory that genuinely has no row for the session.
    session_id = str(uuid4())
    rows = claude_runtime._parse_targeted_claude_agents(
        json.dumps(
            [
                {
                    "sessionId": session_id,
                    "name": "gone-1",
                    "kind": "interactive",
                    "cwd": "/definitely/not/a/real/path/kd270",
                }
            ]
        ),
        session_id,
    )

    assert len(rows) == 1
    assert rows[0]["session_id"] == session_id


def test_live_unlisted_session_reports_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hidden child session keeps a live route but holds no row at all in
    # `claude agents --json`: its courier attests the diagnostic UNSUPPORTED
    # status rather than an uncertain UNAVAILABLE.
    route = _claude_route(tmp_path, "hidden")
    monkeypatch.setattr(runtime, "exact_agent", _unlisted_exact_agent)

    assert runtime.courier_health(route)["status"] == "UNSUPPORTED"


def test_failed_agents_inventory_stays_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unreadable inventory is not evidence of unaddressability.
    route = _claude_route(tmp_path, "project")
    monkeypatch.setattr(runtime, "exact_agent", _no_exact_agent)

    assert runtime.courier_health(route)["status"] == "UNAVAILABLE"


def test_dead_owner_stays_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    route = _claude_route(tmp_path, "project")
    monkeypatch.setattr(Route, "process_is_live", lambda _self: False)
    monkeypatch.setattr(runtime, "exact_agent", _unlisted_exact_agent)

    assert runtime.courier_health(route)["status"] == "UNAVAILABLE"


def _unsupported_and_healthy_probes(
    monkeypatch: pytest.MonkeyPatch, hidden: Route, healthy: Route
) -> None:
    monkeypatch.setattr(
        runtime,
        "request_socket",
        lambda _path, payload, timeout=None: {
            hidden.generation: {
                "schema_version": 1,
                "status": "UNSUPPORTED",
                "generation": hidden.generation,
            },
            healthy.generation: {
                "schema_version": 1,
                "status": "READY",
                "generation": healthy.generation,
                "alias": f"{healthy.alias}:healthy-1",
                "delivery_mode": "claude_native_cross_session",
                "delivery_mechanism": "claude_native",
            },
        }[str(payload["generation"])],
    )


def test_discovery_stays_incomplete_while_an_unsupported_route_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # UNSUPPORTED is diagnostic only: it is not a stable signal, so a live
    # unaddressable route keeps discovery incomplete rather than pretending
    # the addressable roster is fully known.
    root = tmp_path / "state"
    hidden = _claude_route(tmp_path, "hidden")
    healthy = _claude_route(tmp_path, "healthy")
    registry = Registry(root)
    registry.upsert(hidden)
    registry.upsert(healthy)
    _unsupported_and_healthy_probes(monkeypatch, hidden, healthy)

    targets, complete = runtime._local_discovery(root)

    assert not complete
    assert [target.alias for target in targets] == [f"{healthy.alias}:healthy-1"]


def test_alias_send_refuses_while_an_unsupported_route_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The namesake hazard: an alias that uniquely matched one attested target
    # could silently re-resolve to the wrong session while its real owner sat
    # unaddressable. Incomplete discovery refuses any name-based send instead.
    root = tmp_path / "state"
    hidden = _claude_route(tmp_path, "hidden")
    healthy = _claude_route(tmp_path, "healthy")
    registry = Registry(root)
    registry.upsert(hidden)
    registry.upsert(healthy)
    _unsupported_and_healthy_probes(monkeypatch, hidden, healthy)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )

    with pytest.raises(ChatError, match="local peer discovery is incomplete"):
        runtime.send(root, source, f"{healthy.alias}:healthy-1", "must not resolve")
