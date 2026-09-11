"""Regression coverage for Issue #9 routing and shared profile ownership."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest

import cross_agent_chat.install as install_module
from cross_agent_chat import runtime
from cross_agent_chat.core import ChatError, Route
from cross_agent_chat.install import Installer, SettingsError


def _safe_uninstall(installer: Installer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)


def test_fuzzy_target_is_ambiguous_across_local_and_remote_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = runtime.Target(
        alias="claude@m1:parser:alpha",
        provider="claude",
        device="m1",
        project="parser",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
    )
    remote = runtime.Target(
        alias="claude@m2:parser:beta",
        provider="claude",
        device="m2",
        project="parser",
        generation=str(uuid4()),
        session_key="b" * 64,
        remote=True,
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="m1",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    monkeypatch.setattr(runtime, "local_targets", lambda _: [local])
    monkeypatch.setattr(runtime, "_remote_discovery", lambda: ([remote], True))
    monkeypatch.setattr(
        runtime,
        "_send_local_target",
        lambda *args, **kwargs: pytest.fail("ambiguous query reached local delivery"),
    )

    with pytest.raises(ChatError, match="ambiguous"):
        runtime.send(tmp_path / "state", source, "claude parser", "synthetic probe")


@pytest.mark.parametrize("alternate_only", ["claude", "codex"])
def test_uninstall_preserves_integration_for_partially_shared_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alternate_only: str
) -> None:
    home = tmp_path / "home"
    executable = Path("/opt/cross-agent-chat")
    default = Installer(home=home, executable=executable, device="studio")
    alternate = Installer(
        home=home,
        executable=executable,
        device="studio",
        claude_config_dir=tmp_path / "other-claude" if alternate_only == "claude" else None,
        codex_home=tmp_path / "other-codex" if alternate_only == "codex" else None,
    )
    default.setup()
    alternate.setup()
    monkeypatch.setattr(
        alternate,
        "_stop_broker",
        lambda: pytest.fail("profile removal stopped the shared broker"),
    )

    alternate.uninstall()
    assert default.install_state.exists()
    assert default.launch_agent.exists()
    if alternate_only == "claude":
        shared = tomllib.loads(default.codex_config.read_text())
        assert "cross-agent-chat" in shared.get("mcp_servers", {})
    else:
        shared = json.loads(default.claude_config.read_text())
        assert "cross-agent-chat" in shared.get("mcpServers", {})


@pytest.mark.parametrize("shared_provider", ["claude", "codex"])
@pytest.mark.parametrize("remove_first", ["first", "second"])
def test_shared_canonical_root_survives_first_uninstall_and_restores_on_last_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_provider: str,
    remove_first: str,
) -> None:
    home = tmp_path / "home"
    claude_root = tmp_path / "claude-root"
    codex_root = tmp_path / "codex-root"
    claude_alias = tmp_path / "claude-alias"
    codex_alias = tmp_path / "codex-alias"
    for root, alias in ((claude_root, claude_alias), (codex_root, codex_alias)):
        root.mkdir()
        alias.symlink_to(root, target_is_directory=True)
    (claude_root / "settings.json").write_text(json.dumps({"crossSessionInbound": "prompt"}))

    first = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        claude_config_dir=claude_root if shared_provider == "claude" else tmp_path / "claude-one",
        codex_home=codex_root if shared_provider == "codex" else tmp_path / "codex-one",
    )
    second = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        claude_config_dir=claude_alias if shared_provider == "claude" else tmp_path / "claude-two",
        codex_home=codex_alias if shared_provider == "codex" else tmp_path / "codex-two",
    )
    first.setup()
    second.setup()
    for installer in (first, second):
        _safe_uninstall(installer, monkeypatch)

    ordered = (first, second) if remove_first == "first" else (second, first)
    ordered[0].uninstall()

    if shared_provider == "claude":
        claude_config = json.loads((claude_root / ".claude.json").read_text())
        claude_settings = json.loads((claude_root / "settings.json").read_text())
        assert "cross-agent-chat" in claude_config["mcpServers"]
        assert claude_settings["crossSessionInbound"] == "accept"
    else:
        codex_config = tomllib.loads((codex_root / "config.toml").read_text())
        assert "cross-agent-chat" in codex_config["mcp_servers"]

    ordered[1].uninstall()

    if shared_provider == "claude":
        claude_settings = json.loads((claude_root / "settings.json").read_text())
        assert claude_settings["crossSessionInbound"] == "prompt"
        assert "cross-agent-chat" not in json.loads((claude_root / ".claude.json").read_text()).get(
            "mcpServers", {}
        )
    else:
        assert "cross-agent-chat" not in tomllib.loads(
            (codex_root / "config.toml").read_text()
        ).get("mcp_servers", {})


def test_exact_local_handle_does_not_wait_for_remote_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = runtime.Target(
        alias="codex@m1:parser:alpha",
        provider="codex",
        device="m1",
        project="parser",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="m1",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    expected = {"status": "TRANSPORT_ACCEPTED"}
    monkeypatch.setattr(runtime, "local_targets", lambda _: [target])
    monkeypatch.setattr(
        runtime,
        "_remote_discovery",
        lambda: pytest.fail("exact local delivery queried remote peers"),
    )
    monkeypatch.setattr(runtime, "_send_local_target", lambda *args, **kwargs: expected)

    assert (
        runtime.send(tmp_path / "state", source, target.session_key, "synthetic probe") == expected
    )


def test_fuzzy_target_refuses_incomplete_remote_discovery_before_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = runtime.Target(
        alias="claude@m1:parser:alpha",
        provider="claude",
        device="m1",
        project="parser",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="m1",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    monkeypatch.setattr(runtime, "local_targets", lambda _: [target])
    monkeypatch.setattr(runtime, "_remote_discovery", lambda: ([], False))

    with pytest.raises(ChatError, match="discovery is incomplete"):
        runtime.send(tmp_path / "state", source, "claude parser", "synthetic probe")
    assert not (tmp_path / "state").exists()


def test_exact_remote_handle_survives_unrelated_incomplete_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = runtime.Target(
        alias="claude@m2:parser:beta",
        provider="claude",
        device="m2",
        project="parser",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=True,
        tailnet_address="100.64.0.10",
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="m1",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    monkeypatch.setattr(runtime, "local_targets", lambda _: [])
    monkeypatch.setattr(runtime, "_remote_discovery", lambda: ([target], False))
    monkeypatch.setattr(runtime, "canonical_source_alias", lambda *_: source.alias)
    responses: list[dict[str, object]] = []

    def send_remote(
        _address: str, payload: dict[str, object], **_kwargs: object
    ) -> dict[str, object]:
        envelope = json.loads(str(payload["envelope"]))
        response = {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": target.provider,
        }
        responses.append(response)
        return response

    monkeypatch.setattr(runtime, "request_tailnet", send_remote)

    result = runtime.send(tmp_path / "state", source, target.session_key, "synthetic probe")
    assert result["to"] == target.alias
    assert len(responses) == 1


def test_fuzzy_local_target_sends_after_complete_global_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = runtime.Target(
        alias="claude@m1:parser:alpha",
        provider="claude",
        device="m1",
        project="parser",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=False,
        session_id=str(uuid4()),
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="m1",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    expected = {"status": "TRANSPORT_ACCEPTED", "to": target.alias}
    monkeypatch.setattr(runtime, "local_targets", lambda _: [target])
    monkeypatch.setattr(runtime, "_remote_discovery", lambda: ([], True))
    monkeypatch.setattr(runtime, "_send_local_target", lambda *args, **kwargs: expected)

    assert runtime.send(tmp_path / "state", source, "claude parser", "synthetic probe") == expected


def test_shared_codex_config_with_distinct_hooks_fails_before_second_setup(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    first_root = tmp_path / "codex-one"
    second_root = tmp_path / "codex-two"
    first_root.mkdir()
    second_root.mkdir()
    shared_config = first_root / "shared-config.toml"
    shared_config.write_text("")
    (first_root / "config.toml").symlink_to(shared_config)
    (second_root / "config.toml").symlink_to(shared_config)
    first = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        codex_home=first_root,
    )
    second = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        codex_home=second_root,
    )
    first.setup()
    before = shared_config.read_bytes()

    with pytest.raises(SettingsError, match="same profile ownership"):
        second.setup()

    assert shared_config.read_bytes() == before
    assert first.verify_configuration()


def test_shared_codex_hooks_with_distinct_configs_fails_before_second_setup(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    first_root = tmp_path / "codex-one"
    second_root = tmp_path / "codex-two"
    first_root.mkdir()
    second_root.mkdir()
    shared_hooks = first_root / "shared-hooks.json"
    shared_hooks.write_text("{}")
    (first_root / "hooks.json").symlink_to(shared_hooks)
    (second_root / "hooks.json").symlink_to(shared_hooks)
    first = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        codex_home=first_root,
        codex_native_queue=False,
    )
    second = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        codex_home=second_root,
        codex_native_queue=True,
    )
    first.setup()
    before = shared_hooks.read_bytes()

    with pytest.raises(SettingsError, match="same profile ownership"):
        second.setup()

    assert shared_hooks.read_bytes() == before
    assert first.verify_configuration()


@pytest.mark.parametrize("path_name", ("claude_settings", "claude_config", "codex_hooks"))
def test_uninstall_retries_json_cleanup_without_losing_concurrent_user_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_name: str
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    installer.setup()
    selected = getattr(installer, path_name)
    original_json_object = install_module._json_object
    calls = 0
    inject_on_read = 1 if path_name == "claude_config" else 2

    def read_then_user_writes(path: Path) -> dict[str, object]:
        nonlocal calls
        value = original_json_object(path)
        if path == selected:
            calls += 1
            if calls == inject_on_read:
                concurrent = original_json_object(path)
                concurrent["concurrent_user_key"] = "keep"
                path.write_text(json.dumps(concurrent))
        return value

    monkeypatch.setattr(install_module, "_json_object", read_then_user_writes)
    _safe_uninstall(installer, monkeypatch)

    installer.uninstall()

    assert original_json_object(selected)["concurrent_user_key"] == "keep"


def test_optional_delivery_mode_preserves_legacy_local_health_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="m1",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    baseline = {
        "schema_version": 1,
        "status": "READY",
        "generation": route.generation,
        "alias": route.alias,
    }

    monkeypatch.setattr(runtime, "request_socket", lambda *args, **kwargs: baseline)
    legacy = runtime._local_target(tmp_path / "state", route)

    assert legacy is not None
    assert legacy.delivery_mode is None

    extended = {**baseline, "delivery_mode": "codex_experimental_queue"}
    monkeypatch.setattr(runtime, "request_socket", lambda *args, **kwargs: extended)
    observed = runtime._local_target(tmp_path / "state", route)

    assert observed is not None
    assert observed.delivery_mode == "codex_experimental_queue"


def test_optional_delivery_mode_preserves_legacy_remote_peer_shape() -> None:
    peer = {
        "alias": "claude@m2:parser:beta",
        "provider": "claude",
        "device": "m2",
        "project": "parser",
        "status": "available",
        "generation": str(uuid4()),
        "session_key": "a" * 64,
    }
    legacy = runtime._targets_from_tailnet("100.64.0.10", {"schema_version": 1, "peers": [peer]})
    observed = runtime._targets_from_tailnet(
        "100.64.0.10",
        {"schema_version": 1, "peers": [{**peer, "delivery_mode": "claude_native_cross_session"}]},
        include_delivery_mode=True,
    )

    assert legacy[0].delivery_mode is None
    assert observed[0].delivery_mode == "claude_native_cross_session"

    unknown = runtime._targets_from_tailnet(
        "100.64.0.10",
        {"schema_version": 1, "peers": [{**peer, "delivery_mode": "unknown"}]},
        include_delivery_mode=True,
    )
    assert unknown[0].delivery_mode is None


def test_last_codex_config_owner_restores_prior_hooks_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("[features]\nhooks = false\n")
    installer = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        codex_home=codex_home,
    )
    installer.setup()
    _safe_uninstall(installer, monkeypatch)

    installer.uninstall()

    assert tomllib.loads((codex_home / "config.toml").read_text())["features"]["hooks"] is False


@pytest.mark.parametrize(
    "path_name",
    ("claude_settings", "claude_config", "codex_config", "codex_hooks"),
)
def test_uninstall_rejects_retargeted_recorded_provider_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_name: str
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    selected = getattr(installer, path_name)
    selected.parent.mkdir(parents=True)
    original = selected.parent / f"{path_name}-original"
    replacement = selected.parent / f"{path_name}-replacement"
    payload = "" if path_name == "codex_config" else "{}"
    original.write_text(payload)
    replacement.write_text(payload)
    selected.symlink_to(original.name)
    installer.setup()
    before = replacement.read_bytes()
    selected.unlink()
    selected.symlink_to(replacement.name)
    _safe_uninstall(installer, monkeypatch)

    with pytest.raises(SettingsError, match="ownership changed"):
        installer.uninstall()

    assert replacement.read_bytes() == before


def test_uninstall_rechecks_recorded_paths_after_broker_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    selected = installer.claude_settings
    selected.parent.mkdir(parents=True)
    original = selected.parent / "settings-original.json"
    replacement = selected.parent / "settings-replacement.json"
    original.write_text(json.dumps({"crossSessionInbound": "hold"}))
    replacement.write_text(json.dumps({"crossSessionInbound": "accept", "user_owned": True}))
    selected.symlink_to(original.name)
    installer.setup()
    before = replacement.read_bytes()

    def retarget_during_stop() -> None:
        selected.unlink()
        selected.symlink_to(replacement.name)

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_broker", retarget_during_stop)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)

    with pytest.raises(SettingsError, match="ownership changed"):
        installer.uninstall()

    assert replacement.read_bytes() == before


@pytest.mark.parametrize("path_name", ("claude_settings", "codex_config"))
def test_setup_rejects_retargeted_schema4_provider_path(tmp_path: Path, path_name: str) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    selected = getattr(installer, path_name)
    selected.parent.mkdir(parents=True)
    original = selected.parent / f"{path_name}-original"
    replacement = selected.parent / f"{path_name}-replacement"
    if path_name == "claude_settings":
        original.write_text(json.dumps({"crossSessionInbound": "hold"}))
        replacement.write_text(json.dumps({"crossSessionInbound": "prompt", "user_owned": True}))
    else:
        original.write_text("[features]\nhooks = false\n")
        replacement.write_text("[features]\nhooks = true\n")
    selected.symlink_to(original.name)
    installer.setup()
    state_before = installer.install_state.read_bytes()
    replacement_before = replacement.read_bytes()
    selected.unlink()
    selected.symlink_to(replacement.name)

    with pytest.raises(SettingsError, match="ownership changed"):
        installer.setup()

    assert installer.install_state.read_bytes() == state_before
    assert replacement.read_bytes() == replacement_before


def test_uninstall_rejects_malformed_sibling_state_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    installer.setup()
    malformed = installer.install_state.parent / "install-malformed.json"
    malformed.write_text("{}")
    monkeypatch.setattr(installer, "_stop_broker", lambda: pytest.fail("broker stop ran"))

    with pytest.raises(SettingsError, match="install state is invalid"):
        installer.uninstall()


@pytest.mark.parametrize("path_name", ("claude_config", "codex_hooks"))
def test_uninstall_preflights_malformed_json_before_broker_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_name: str
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    installer.setup()
    malformed = getattr(installer, path_name)
    malformed.write_text("{")
    monkeypatch.setattr(installer, "_stop_broker", lambda: pytest.fail("broker stop ran"))
    monkeypatch.setattr(installer, "_stop_couriers", lambda: pytest.fail("courier stop ran"))

    with pytest.raises(SettingsError, match="invalid JSON"):
        installer.uninstall()

    assert malformed.read_text() == "{"
    assert installer.install_state.exists()


def test_uninstall_preflights_malformed_toml_before_broker_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    installer.setup()
    installer.codex_config.write_text("[")
    monkeypatch.setattr(installer, "_stop_broker", lambda: pytest.fail("broker stop ran"))
    monkeypatch.setattr(installer, "_stop_couriers", lambda: pytest.fail("courier stop ran"))

    with pytest.raises(SettingsError, match=r"Codex config\.toml is invalid"):
        installer.uninstall()

    assert installer.codex_config.read_text() == "["
    assert installer.install_state.exists()


def test_uninstall_reactivates_after_post_stop_retarget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    selected = installer.claude_settings
    selected.parent.mkdir(parents=True)
    original = selected.parent / "settings-original.json"
    replacement = selected.parent / "settings-replacement.json"
    original.write_text(json.dumps({"crossSessionInbound": "hold"}))
    replacement.write_text(json.dumps({"crossSessionInbound": "accept", "user_owned": True}))
    selected.symlink_to(original.name)
    installer.setup()
    replacement_before = replacement.read_bytes()
    reactivated: list[bool] = []

    def retarget_during_stop() -> None:
        selected.unlink()
        selected.symlink_to(replacement.name)

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "_stop_broker", retarget_during_stop)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: reactivated.append(True))

    with pytest.raises(SettingsError, match="ownership changed"):
        installer.uninstall()

    assert replacement.read_bytes() == replacement_before
    assert reactivated == [True]


def test_uninstall_reactivates_after_retarget_during_codex_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    selected = installer.codex_config
    selected.parent.mkdir(parents=True)
    original = selected.parent / "original.toml"
    replacement = selected.parent / "replacement.toml"
    original.write_text("")
    replacement.write_text('[mcp_servers.user]\ncommand = "keep"\n')
    selected.symlink_to(original.name)
    installer.setup()
    replacement_before = replacement.read_bytes()
    state_before = installer.install_state.read_bytes()
    require_paths = installer._require_recorded_provider_paths
    events: list[str] = []
    post_stop_checks = 0

    def require(recorded: dict[str, str]) -> None:
        nonlocal post_stop_checks
        if "stop" in events:
            post_stop_checks += 1
            if post_stop_checks == 2:
                selected.unlink()
                selected.symlink_to(replacement.name)
        require_paths(recorded)

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "_stop_broker", lambda: events.append("stop"))
    monkeypatch.setattr(installer, "_stop_couriers", lambda: events.append("couriers"))
    monkeypatch.setattr(installer, "activate", lambda: events.append("activate"))
    monkeypatch.setattr(installer, "_require_recorded_provider_paths", require)

    with pytest.raises(SettingsError, match="ownership changed"):
        installer.uninstall()

    assert replacement.read_bytes() == replacement_before
    assert installer.install_state.read_bytes() == state_before
    assert events == ["stop", "couriers", "activate"]


@pytest.mark.parametrize("failure_point", ("couriers", "toml", "transform"))
def test_uninstall_reactivates_for_post_stop_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    installer.setup()
    settings_before = installer.claude_settings.read_bytes()
    events: list[str] = []

    def stop() -> None:
        events.append("stop")
        if failure_point == "toml":
            installer.codex_config.write_text("[")

    def stop_couriers() -> None:
        if failure_point == "couriers":
            raise SettingsError("injected courier failure")

    def fail_transform(text: str, keys: set[str]) -> str:
        raise SettingsError("injected transform failure")

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "_stop_broker", stop)
    monkeypatch.setattr(installer, "_stop_couriers", stop_couriers)
    monkeypatch.setattr(installer, "activate", lambda: events.append("activate"))
    if failure_point == "transform":
        monkeypatch.setattr(install_module, "_remove_owned_hook_trust", fail_transform)

    with pytest.raises(SettingsError):
        installer.uninstall()

    assert events == ["stop", "activate"]
    assert installer.claude_settings.read_bytes() == settings_before
    assert installer.install_state.exists()
    if failure_point == "toml":
        assert installer.codex_config.read_text() == "["


def test_uninstall_rolls_back_owned_writes_after_later_json_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    installer.setup()
    surfaces = (
        installer.claude_settings,
        installer.claude_config,
        installer.codex_config,
        installer.codex_hooks,
    )
    before = {path: path.read_bytes() for path in surfaces}
    original_atomic_write = install_module._atomic_write
    reactivated: list[bool] = []

    def fail_claude_settings(path: Path, payload: bytes, mode: int = 0o600) -> None:
        if path == installer.claude_settings:
            raise OSError("injected Claude settings write failure")
        original_atomic_write(path, payload, mode)

    monkeypatch.setattr(install_module, "_atomic_write", fail_claude_settings)
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: reactivated.append(True))

    with pytest.raises(OSError, match="injected Claude settings"):
        installer.uninstall()

    assert {path: path.read_bytes() for path in surfaces} == before
    assert installer.install_state.exists()
    assert reactivated == [True]


def test_uninstall_reports_reactivation_timeout_after_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    installer.setup()

    def fail_couriers() -> None:
        raise SettingsError("injected courier failure")

    def fail_activation() -> None:
        raise subprocess.TimeoutExpired(["launchctl", "bootstrap"], 10)

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    monkeypatch.setattr(installer, "_stop_couriers", fail_couriers)
    monkeypatch.setattr(installer, "activate", fail_activation)

    with pytest.raises(SettingsError, match=r"uninstall failed: injected.*rollback failed"):
        installer.uninstall()

    assert installer.install_state.exists()


@pytest.mark.parametrize("schema", (1, 2, 3))
def test_uninstall_preserves_legacy_install_metadata_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema: int
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    installer.setup()
    current = json.loads(installer.install_state.read_text())
    legacy = {
        "schema_version": schema,
        "claude_cross_session_inbound": current["claude_cross_session_inbound"],
    }
    if schema >= 2:
        legacy["stable_entrypoint"] = current["stable_entrypoint"]
    if schema == 3:
        legacy["managed_entrypoints"] = current["managed_entrypoints"]
    installer.install_state.write_text(json.dumps(legacy))
    _safe_uninstall(installer, monkeypatch)

    installer.uninstall()

    assert not installer.install_state.exists()
    assert "crossSessionInbound" not in json.loads(installer.claude_settings.read_text())
