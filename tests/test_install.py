from __future__ import annotations

import json
import plistlib
import subprocess
import tomllib
from pathlib import Path

import pytest

from cross_agent_chat.install import Installer, InstallReport, SettingsError, _owned_hook


def test_setup_preserves_unrelated_provider_configuration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude_settings = home / ".claude" / "settings.json"
    claude_config = home / ".claude.json"
    codex_config = home / ".codex" / "config.toml"
    codex_hooks = home / ".codex" / "hooks.json"
    claude_settings.parent.mkdir(parents=True)
    codex_config.parent.mkdir(parents=True)
    claude_settings.write_text(json.dumps({"theme": "dark", "hooks": {"Other": []}}))
    claude_config.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}))
    codex_config.write_text('[mcp_servers.other]\ncommand = "other"\n')
    codex_hooks.write_text(json.dumps({"hooks": {"Other": [{"command": "other"}]}}))

    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()

    assert json.loads(claude_settings.read_text())["theme"] == "dark"
    assert "other" in json.loads(claude_config.read_text())["mcpServers"]
    assert "[mcp_servers.other]" in codex_config.read_text()
    assert "Other" in json.loads(codex_hooks.read_text())["hooks"]


def test_setup_is_idempotent_and_local_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    first = installer.setup()
    snapshot = {path: path.read_bytes() for path in first.changed_paths}
    installer.setup()

    assert {path: path.read_bytes() for path in first.changed_paths} == snapshot
    assert not (home / ".local/state/cross-agent-chat/peers.json").exists()


def test_hook_ownership_requires_exact_cross_agent_chat_command() -> None:
    assert _owned_hook(
        {"hooks": [{"command": "/opt/cross-agent-chat _register --provider claude"}]}
    )
    assert not _owned_hook({"hooks": [{"command": "other-tool _register --provider claude"}]})


def test_setup_installs_owned_background_broker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    executable = Path("/opt/cross-agent-chat")
    installer = Installer(home=home, executable=executable, device="studio")

    report = installer.setup()

    launch_agent = home / "Library/LaunchAgents/io.github.kwonsyup.cross-agent-chat.plist"
    assert launch_agent in report.changed_paths
    payload = plistlib.loads(launch_agent.read_bytes())
    assert payload == {
        "Label": "io.github.kwonsyup.cross-agent-chat",
        "ProgramArguments": [str(executable), "_broker"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
    }


def test_setup_passes_auto_discovered_tailnet_address_to_broker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        tailnet_address="100.64.0.10",
    )

    installer.setup()

    payload = plistlib.loads(installer.launch_agent.read_bytes())
    assert payload["EnvironmentVariables"] == {"CROSS_AGENT_CHAT_TAILNET_ADDRESS": "100.64.0.10"}


def test_setup_trusts_each_owned_codex_hook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads((home / ".codex" / "config.toml").read_text())
    trusted = config["hooks"]["state"]
    assert config["mcp_servers"]["cross-agent-chat"]["tool_timeout_sec"] == 120
    assert len(trusted) == 3
    assert all(
        isinstance(item, dict) and str(item.get("trusted_hash", "")).startswith("sha256:")
        for item in trusted.values()
    )


def test_setup_removes_stale_owned_hook_trust_and_preserves_unrelated_trust(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    config = home / ".codex" / "config.toml"
    parsed = tomllib.loads(config.read_text())
    trusted = parsed["hooks"]["state"]
    assert isinstance(trusted, dict)
    owned: dict[str, str] = {}
    for key, value in trusted.items():
        if not str(key).startswith(f"{installer.codex_hooks}:"):
            continue
        assert isinstance(value, dict)
        trusted_hash = value.get("trusted_hash")
        assert isinstance(trusted_hash, str)
        owned[str(key)] = trusted_hash
    assert len(owned) == 3

    stale = "".join(
        f"[hooks.state.{json.dumps(key)}]\ntrusted_hash = {json.dumps(trusted_hash)}\n\n"
        for key, trusted_hash in owned.items()
    )
    unrelated_key = "unrelated-hooks.json:stop:0:0"
    unrelated_hash = "sha256:" + "0" * 64
    unrelated = (
        f"[hooks.state.{json.dumps(unrelated_key)}]\n"
        f"trusted_hash = {json.dumps(unrelated_hash)}\n\n"
    )
    config.write_text(
        config.read_text().replace(
            "# cross-agent-chat:start",
            stale + unrelated + "# cross-agent-chat:start",
        )
    )

    installer.setup()

    repaired = tomllib.loads(config.read_text())
    repaired_trust = repaired["hooks"]["state"]
    assert isinstance(repaired_trust, dict)
    repaired_owned = [
        key for key in repaired_trust if str(key).startswith(f"{installer.codex_hooks}:")
    ]
    assert len(repaired_owned) == 3
    assert repaired_trust[unrelated_key] == {"trusted_hash": unrelated_hash}

    installer.uninstall()

    uninstalled = tomllib.loads(config.read_text())
    uninstalled_trust = uninstalled["hooks"]["state"]
    assert isinstance(uninstalled_trust, dict)
    assert not any(str(key).startswith(f"{installer.codex_hooks}:") for key in uninstalled_trust)
    assert uninstalled_trust[unrelated_key] == {"trusted_hash": unrelated_hash}


def test_setup_rolls_back_when_verification_fails(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "light"}))
    original = settings.read_bytes()
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    with pytest.raises(SettingsError, match="verification failed"):
        installer.setup(verify=lambda: False)

    assert settings.read_bytes() == original


def test_activate_starts_only_owned_launch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 113, "", "not found")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)

    installer.activate()

    assert [
        "launchctl",
        "bootstrap",
        f"gui/{__import__('os').getuid()}",
        str(installer.launch_agent),
    ] in calls


def test_activate_reloads_existing_owned_launch_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    calls: list[list[str]] = []
    stopped = False

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal stopped
        calls.append(command)
        if command[:2] == ["launchctl", "bootout"]:
            stopped = True
        if command[:2] == ["launchctl", "print"] and stopped:
            return subprocess.CompletedProcess(command, 113, "", "not found")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)

    installer.activate()

    service = f"gui/{__import__('os').getuid()}/io.github.kwonsyup.cross-agent-chat"
    assert ["launchctl", "bootout", service] in calls
    assert [
        "launchctl",
        "bootstrap",
        f"gui/{__import__('os').getuid()}",
        str(installer.launch_agent),
    ] in calls


def test_activate_waits_for_existing_broker_to_stop_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    print_after_bootout = 0
    booted_out = False
    bootstrap_print_count: int | None = None

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal booted_out, print_after_bootout, bootstrap_print_count
        if command[:2] == ["launchctl", "bootout"]:
            booted_out = True
        elif command[:2] == ["launchctl", "print"] and booted_out:
            print_after_bootout += 1
            code = 0 if print_after_bootout < 3 else 113
            return subprocess.CompletedProcess(command, code, "", "")
        elif command[:2] == ["launchctl", "bootstrap"]:
            bootstrap_print_count = print_after_bootout
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    installer.activate()

    assert bootstrap_print_count == 3


def test_activate_retries_transient_launchd_bootstrap_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    bootstrap_attempts = 0

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal bootstrap_attempts
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 113, "", "not found")
        if command[:2] == ["launchctl", "bootstrap"]:
            bootstrap_attempts += 1
            code = 5 if bootstrap_attempts == 1 else 0
            return subprocess.CompletedProcess(command, code, "", "transient")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    installer.activate()

    assert bootstrap_attempts == 2


def test_install_restores_provider_files_if_activation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "light"}))
    original = settings.read_bytes()
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    def fail() -> None:
        raise SettingsError("activation failed")

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "activate", fail)

    with pytest.raises(SettingsError, match="activation failed"):
        installer.install()

    assert settings.read_bytes() == original
    assert not installer.launch_agent.exists()


def test_install_preserves_healthy_broker_when_courier_shutdown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "light"}))
    original = settings.read_bytes()
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    health_checks = 0
    bootouts = 0

    def healthy() -> bool:
        nonlocal health_checks
        health_checks += 1
        return True

    def fail_shutdown() -> None:
        raise SettingsError("courier shutdown failed")

    def bootout() -> None:
        nonlocal bootouts
        bootouts += 1

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "broker_is_healthy", healthy)
    monkeypatch.setattr(installer, "_stop_couriers", fail_shutdown)
    monkeypatch.setattr(installer, "_bootout", bootout)

    with pytest.raises(SettingsError, match="courier shutdown failed"):
        installer.install()

    assert settings.read_bytes() == original
    assert bootouts == 0
    assert health_checks == 2


def test_install_aborts_before_setup_when_previous_broker_state_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    setup_called = False

    def unknown() -> bool:
        raise SettingsError("could not determine previous broker state")

    def setup() -> InstallReport:
        nonlocal setup_called
        setup_called = True
        return InstallReport(changed_paths=(), backup=tmp_path / "backup")

    monkeypatch.setattr(installer, "broker_is_loaded", unknown)
    monkeypatch.setattr(installer, "setup", setup)

    with pytest.raises(SettingsError, match="could not determine previous broker state"):
        installer.install()

    assert not setup_called
    assert not home.exists()


def test_install_restores_and_restarts_healthy_broker_after_activation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "light"}))
    original = settings.read_bytes()
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    activations = 0

    def activate() -> None:
        nonlocal activations
        activations += 1
        if activations == 1:
            raise SettingsError("activation failed")

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "broker_is_healthy", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)

    with pytest.raises(SettingsError, match="activation failed"):
        installer.install()

    assert settings.read_bytes() == original
    assert activations == 2


def test_install_restores_and_restarts_healthy_broker_after_candidate_health_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "light"}))
    original = settings.read_bytes()
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    activations = 0

    def activate() -> None:
        nonlocal activations
        activations += 1

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "broker_is_healthy", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)
    monkeypatch.setattr(installer, "verify", lambda: False)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    with pytest.raises(SettingsError, match="background broker did not become healthy"):
        installer.install()

    assert settings.read_bytes() == original
    assert activations == 2


def test_install_reports_primary_and_rollback_restart_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    activations = 0

    def activate() -> None:
        nonlocal activations
        activations += 1
        if activations == 1:
            raise SettingsError("candidate activation failed")
        raise SettingsError("predecessor restart failed")

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "broker_is_healthy", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)

    with pytest.raises(SettingsError) as captured:
        installer.install()

    message = str(captured.value)
    assert "candidate activation failed" in message
    assert "predecessor restart failed" in message
    backups = list((home / ".cache" / "cross-agent-chat" / "backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "manifest.json").exists()


def test_install_allows_bounded_launchd_throttle_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    checks = 0

    def verify() -> bool:
        nonlocal checks
        checks += 1
        return checks > 21

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "verify", verify)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    installer.install()

    assert checks == 22


def test_upgrade_install_stops_old_couriers_before_reloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    calls: list[str] = []
    report = InstallReport(changed_paths=(), backup=tmp_path / "backup")
    monkeypatch.setattr(installer, "setup", lambda: report)
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: calls.append("stop"))
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: calls.append("remove"))
    monkeypatch.setattr(installer, "activate", lambda: calls.append("activate"))
    monkeypatch.setattr(installer, "verify", lambda: True)

    assert installer.install() == report
    assert calls == ["stop", "remove", "activate"]


def test_uninstall_removes_only_owned_background_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)

    installer.uninstall()

    claude = json.loads((home / ".claude.json").read_text())
    assert "cross-agent-chat" not in claude.get("mcpServers", {})
    assert not installer.launch_agent.exists()
    assert [
        "launchctl",
        "bootout",
        f"gui/{__import__('os').getuid()}/io.github.kwonsyup.cross-agent-chat",
    ] in calls


def test_uninstall_preserves_shared_claude_cross_session_setting(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"crossSessionInbound": "accept"}))
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()

    installer.uninstall()

    assert json.loads(settings.read_text())["crossSessionInbound"] == "accept"


def test_uninstall_restores_absent_claude_cross_session_setting(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()

    installer.uninstall()

    assert "crossSessionInbound" not in json.loads(installer.claude_settings.read_text())


def test_uninstall_restores_prior_claude_cross_session_setting(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"crossSessionInbound": "prompt"}))
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()

    installer.uninstall()

    assert json.loads(settings.read_text())["crossSessionInbound"] == "prompt"


def test_verify_requires_loaded_responsive_background_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()

    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 113, "", "not found"),
    )
    assert not installer.verify()

    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda *_args, **_kwargs: {"schema_version": 1, "status": "READY"},
    )
    assert installer.verify()


def test_broker_health_reports_timeout_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["launchctl"], 5)
        ),
    )

    assert not installer.broker_is_healthy()


def test_uninstall_removes_owned_runtime_state_and_backups(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    installer.state.mkdir(parents=True, mode=0o700)
    installer.state.chmod(0o700)
    (installer.state / "marker").write_text("owned")

    installer.uninstall()

    assert not installer.state.exists()
    assert not installer.cache.exists()
    assert not installer.install_state.exists()
