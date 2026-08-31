from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from cross_agent_chat.install import (
    Installer,
    InstallReport,
    SettingsError,
    _owned_hook,
    discover_executable,
)


def test_install_script_stages_before_runtime_transition() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text()

    assert script.index("pip install") < script.index("_install-staged")
    assert "uv tool install" not in script
    assert "/usr/bin/python3" not in script
    assert "CROSS_AGENT_CHAT_PREVIOUS_RUNTIME" not in script
    assert "CROSS_AGENT_CHAT_RUNTIME_ROOT" not in script


def test_install_script_package_failure_leaves_predecessor_untouched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    predecessor_bin = home / "custom" / "bin"
    fake_bin.mkdir()
    predecessor_bin.mkdir(parents=True)
    predecessor = predecessor_bin / "cross-agent-chat"
    predecessor.write_text("predecessor")
    predecessor.chmod(0o755)
    broker_marker = home / "broker-healthy"
    broker_marker.write_text("healthy")
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = venv ]; then\n'
        '  mkdir -p "$4/bin"\n'
        '  ln -s /usr/bin/python3 "$4/bin/python"\n'
        "  exit 0\n"
        "fi\n"
        "exit 19\n"
    )
    fake_uv.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "install.sh"
    environment = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{predecessor_bin}:/usr/bin:/bin",
        "CROSS_AGENT_CHAT_SOURCE": "candidate-wheel",
    }

    completed = subprocess.run(
        ["sh", str(script)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )

    assert completed.returncode == 19
    assert predecessor.read_text() == "predecessor"
    assert broker_marker.read_text() == "healthy"
    product_root = home / ".local" / "share" / "cross-agent-chat-runtime"
    assert not (product_root / "current").exists()
    releases = product_root / "releases"
    assert not list(releases.glob(".staging-*"))
    assert not product_root.exists()


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
    installer.codex_hooks.parent.mkdir(parents=True)
    installer.codex_hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/opt/unrelated-hook",
                                    "timeout": 3,
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
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
    unrelated_key = f"{installer.codex_hooks}:stop:0:0"
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
    assert set(repaired_owned) == {*owned, unrelated_key}
    assert repaired_trust[unrelated_key] == {"trusted_hash": unrelated_hash}

    installer.uninstall()

    uninstalled = tomllib.loads(config.read_text())
    uninstalled_trust = uninstalled["hooks"]["state"]
    assert isinstance(uninstalled_trust, dict)
    assert not set(owned).intersection(uninstalled_trust)
    assert uninstalled_trust[unrelated_key] == {"trusted_hash": unrelated_hash}


def test_setup_preserves_owned_hook_position_when_unrelated_hook_follows(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    hooks = json.loads(installer.codex_hooks.read_text())
    stop_hooks = hooks["hooks"]["Stop"]
    stop_hooks.append(
        {"hooks": [{"type": "command", "command": "/opt/unrelated-hook", "timeout": 3}]}
    )
    installer.codex_hooks.write_text(json.dumps(hooks))
    unrelated_key = f"{installer.codex_hooks}:stop:1:0"
    unrelated_hash = "sha256:" + "0" * 64
    config = installer.codex_config
    config.write_text(
        config.read_text()
        + f"\n[hooks.state.{json.dumps(unrelated_key)}]\n"
        + f"trusted_hash = {json.dumps(unrelated_hash)}\n"
    )

    installer.setup()

    repaired_hooks = json.loads(installer.codex_hooks.read_text())["hooks"]["Stop"]
    assert _owned_hook(repaired_hooks[0])
    assert repaired_hooks[1]["hooks"][0]["command"] == "/opt/unrelated-hook"
    repaired_trust = tomllib.loads(config.read_text())["hooks"]["state"]
    assert repaired_trust[unrelated_key] == {"trusted_hash": unrelated_hash}


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


def _staged_runtime(installer: Installer, marker: str = "candidate") -> Path:
    stage = installer.releases / ".staging-test"
    executable = stage / "bin" / "cross-agent-chat"
    executable.parent.mkdir(parents=True)
    executable.write_text(marker)
    executable.chmod(0o755)
    return stage


def test_staged_install_commits_stable_runtime_and_provider_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "custom-tools" / "cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "verify", lambda: True)

    installer.install_staged(stage, stable)

    current = installer.current_runtime.resolve(strict=True)
    assert current.parent == installer.releases.resolve()
    assert current.name.startswith("release-")
    assert (current / "bin" / "cross-agent-chat").read_text() == "candidate"
    assert stable.is_symlink()
    assert stable.resolve() == current / "bin" / "cross-agent-chat"
    assert str(stable) in installer.claude_config.read_text()
    assert str(stable) in installer.codex_config.read_text()
    assert str(stable) in installer.launch_agent.read_text()
    assert not list(installer.transactions.iterdir())


def test_staged_install_setup_failure_restores_public_entrypoint_and_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "configured-bin" / "cross-agent-chat"
    stable.parent.mkdir(parents=True)
    stable.write_text("predecessor-entrypoint")
    stable.chmod(0o751)
    installer = Installer(home=home, executable=stable, device="studio")
    previous = installer.releases / "release-previous"
    (previous / "bin").mkdir(parents=True)
    installer.current_runtime.symlink_to("releases/release-previous")
    stage = _staged_runtime(installer)

    def fail_setup() -> InstallReport:
        raise SettingsError("setup failed")

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "setup", fail_setup)

    with pytest.raises(
        SettingsError, match="transition failed but rollback succeeded: setup failed"
    ):
        installer.install_staged(stage, stable)

    assert stable.read_text() == "predecessor-entrypoint"
    assert stat.S_IMODE(stable.stat().st_mode) == 0o751
    assert os.readlink(installer.current_runtime) == "releases/release-previous"
    assert not [
        item
        for item in installer.releases.iterdir()
        if item.name.startswith("release-") and item != previous
    ]


def test_staged_install_activation_failure_restores_custom_symlink_and_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "configured-bin" / "cross-agent-chat"
    predecessor = home / "custom-runtime" / "bin" / "cross-agent-chat"
    predecessor.parent.mkdir(parents=True)
    predecessor.write_text("predecessor")
    stable.parent.mkdir(parents=True)
    stable.symlink_to("../custom-runtime/bin/cross-agent-chat")
    installer = Installer(home=home, executable=stable, device="studio")
    previous = installer.releases / "release-previous"
    (previous / "bin").mkdir(parents=True)
    installer.current_runtime.symlink_to("releases/release-previous")
    stage = _staged_runtime(installer)
    activations = 0

    def activate() -> None:
        nonlocal activations
        activations += 1
        if activations == 1:
            raise SettingsError("candidate activation failed")
        assert stable.resolve() == predecessor

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "broker_is_healthy", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)

    with pytest.raises(
        SettingsError,
        match="transition failed but rollback succeeded: candidate activation failed",
    ):
        installer.install_staged(stage, stable)

    assert activations == 2
    assert os.readlink(stable) == "../custom-runtime/bin/cross-agent-chat"
    assert os.readlink(installer.current_runtime) == "releases/release-previous"
    assert predecessor.read_text() == "predecessor"


def test_staged_install_fails_closed_on_unknown_current_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / ".local" / "bin" / "cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    installer.current_runtime.write_text("unexpected")
    stopped = False

    def stop() -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "_stop_couriers", stop)

    with pytest.raises(SettingsError, match="predecessor state is unknown"):
        installer.install_staged(stage, stable)

    assert not stopped
    assert installer.current_runtime.read_text() == "unexpected"


def test_staged_install_fails_closed_on_unfinished_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / ".local" / "bin" / "cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    unfinished = installer.transactions / "retained"
    unfinished.mkdir(parents=True)
    (unfinished / "metadata.json").write_text("{}")
    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())

    with pytest.raises(SettingsError, match="unfinished transaction"):
        installer.install_staged(stage, stable)

    assert stage.exists()
    assert not stable.exists()


def test_staged_install_courier_failure_preserves_healthy_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "configured-bin" / "cross-agent-chat"
    predecessor = home / "legacy-root" / "bin" / "cross-agent-chat"
    predecessor.parent.mkdir(parents=True)
    predecessor.write_text("predecessor")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(predecessor)
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    activations = 0

    def fail_shutdown() -> None:
        raise SettingsError("courier shutdown failed")

    def activate() -> None:
        nonlocal activations
        activations += 1

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "broker_is_healthy", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", fail_shutdown)
    monkeypatch.setattr(installer, "activate", activate)

    with pytest.raises(
        SettingsError,
        match="transition failed but rollback succeeded: courier shutdown failed",
    ):
        installer.install_staged(stage, stable)

    assert stable.resolve() == predecessor
    assert predecessor.read_text() == "predecessor"
    assert stage.exists()
    assert activations == 0


def test_staged_install_health_failure_restores_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "configured-bin" / "cross-agent-chat"
    predecessor = home / "legacy-root" / "bin" / "cross-agent-chat"
    predecessor.parent.mkdir(parents=True)
    predecessor.write_text("predecessor")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(predecessor)
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    activations = 0

    def activate() -> None:
        nonlocal activations
        activations += 1

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "broker_is_healthy", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)
    monkeypatch.setattr(installer, "verify", lambda: False)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    with pytest.raises(SettingsError, match="background broker did not become healthy"):
        installer.install_staged(stage, stable)

    assert activations == 2
    assert stable.resolve() == predecessor
    assert predecessor.read_text() == "predecessor"


def test_staged_install_retains_evidence_when_predecessor_restart_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "configured-bin" / "cross-agent-chat"
    predecessor = home / "legacy-root" / "bin" / "cross-agent-chat"
    predecessor.parent.mkdir(parents=True)
    predecessor.write_text("predecessor")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(predecessor)
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    activations = 0

    def activate() -> None:
        nonlocal activations
        activations += 1
        if activations == 1:
            raise SettingsError("candidate activation failed")
        raise SettingsError("predecessor restart failed")

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "broker_is_healthy", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)

    with pytest.raises(SettingsError, match="transition and rollback failed") as captured:
        installer.install_staged(stage, stable)

    assert "candidate activation failed" in str(captured.value)
    assert "predecessor restart failed" in str(captured.value)
    assert stable.resolve() == predecessor
    assert any(installer.transactions.iterdir())
    retained = [item for item in installer.releases.iterdir() if item.name.startswith("release-")]
    assert len(retained) == 1


def test_loaded_predecessor_health_snapshot_retries_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    health_checks = 0

    def health() -> bool:
        nonlocal health_checks
        health_checks += 1
        return health_checks > 1

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "broker_is_healthy", health)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "verify", lambda: True)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    installer.install()

    assert health_checks == 2


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


def test_install_allows_broker_readiness_after_sixty_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    checks = 0

    def verify() -> bool:
        nonlocal checks
        checks += 1
        return checks == 241

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "verify", verify)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    installer.install()

    assert checks == 241


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


def test_uninstall_removes_stable_runtime_but_not_legacy_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "configured-bin" / "cross-agent-chat"
    legacy = home / "configured-tool-root" / "bin" / "cross-agent-chat"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy")
    installer = Installer(home=home, executable=stable, device="studio")
    release = installer.releases / "release-current"
    candidate = release / "bin" / "cross-agent-chat"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("candidate")
    installer.current_runtime.symlink_to("releases/release-current")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(installer.current_runtime / "bin" / "cross-agent-chat")
    installer.setup()
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "", ""),
    )

    installer.uninstall()

    assert not stable.exists()
    assert not installer.current_runtime.exists()
    assert not release.exists()
    assert legacy.read_text() == "legacy"


def test_discover_executable_preserves_stable_symlink_identity(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime" / "bin" / "cross-agent-chat"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("candidate")
    stable = tmp_path / "bin" / "cross-agent-chat"
    stable.parent.mkdir()
    stable.symlink_to(runtime)

    assert discover_executable(stable) == stable.absolute()
