"""Focused tests for the selected-provider setup contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cross_agent_chat import cli
from cross_agent_chat.cli import parser
from cross_agent_chat.core import ChatError
from cross_agent_chat.install import (
    Installer,
    SettingsError,
    resolve_providers,
)


def _installer(
    home: Path,
    *,
    providers: tuple[str, ...] | None = None,
    codex_home: Path | None = None,
    codex_native_queue: bool | None = None,
    devin_global: bool = False,
) -> Installer:
    return Installer(
        home=home,
        executable=Path("/opt/cross-agent-chat"),
        device="studio",
        codex_home=codex_home,
        codex_native_queue=codex_native_queue,
        devin_global=devin_global,
        providers=providers,
    )


def _metadata(
    *,
    schema_version: int,
    provider_paths: dict[str, str],
    providers: list[str] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": schema_version,
        "claude_cross_session_inbound": {"present": False, "value": None},
        "stable_entrypoint": ".local/bin/cross-agent-chat",
        "managed_entrypoints": [".local/bin/cross-agent-chat"],
        "provider_paths": provider_paths,
        "codex_hooks_feature": {"present": False, "value": None},
    }
    if providers is not None:
        record["providers"] = providers
    return record


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_resolve_providers_refuses_before_mutation_without_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(SettingsError, match="no supported provider"):
        resolve_providers(home=home)

    assert not list(home.iterdir())


@pytest.mark.parametrize("json_output", (True, False))
def test_real_cli_doctor_reports_fresh_profile_without_writes(
    tmp_path: Path, json_output: bool
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.pop("CODEX_HOME", None)
    environment.pop("CLAUDE_CONFIG_DIR", None)
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    arguments = ["doctor"] + (["--json"] if json_output else [])

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cross_agent_chat.cli import main; raise SystemExit(main())",
            *arguments,
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr == ""
    if json_output:
        assert json.loads(result.stdout) == {
            "codex_native_queue": "stop-bound",
            "integration": "needs setup",
            "local_broker": "unavailable",
            "next": "cross-agent-chat setup",
            "remote_trust": "tailscale_acl",
            "version": "0.4.5",
        }
    else:
        assert result.stdout == (
            "version: 0.4.5\n"
            "integration: needs setup\n"
            "codex_native_queue: stop-bound\n"
            "local_broker: unavailable\n"
            "remote_trust: tailscale_acl\n"
            "next: cross-agent-chat setup\n"
        )
    assert tuple(home.iterdir()) == ()


def test_real_cli_setup_preserves_strict_explicit_provider_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.pop("CODEX_HOME", None)
    environment.pop("CLAUDE_CONFIG_DIR", None)
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cross_agent_chat.cli import main; raise SystemExit(main())",
            "setup",
            "--provider",
            "codex",
            "--yes",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "provider configuration roots are absent: codex" in result.stderr
    assert tuple(home.iterdir()) == ()


def test_resolve_providers_selects_only_existing_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".config" / "devin").mkdir(parents=True)

    assert resolve_providers(home=home) == ("claude", "codex")
    assert resolve_providers(home=home, devin_global=True) == ("claude", "codex", "devin")


def test_resolve_providers_refuses_absent_explicit_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)

    with pytest.raises(SettingsError, match="roots are absent: codex"):
        resolve_providers(home=home, requested=("claude", "codex"))
    with pytest.raises(SettingsError, match="roots are absent: devin"):
        resolve_providers(home=home, requested=("devin",), devin_global=True)

    assert not (home / ".codex").exists()


def test_resolve_providers_rejects_unknown_and_empty_requests(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)

    with pytest.raises(SettingsError, match="provider selection is invalid"):
        resolve_providers(home=home, requested=())
    with pytest.raises(SettingsError, match="provider selection is invalid"):
        resolve_providers(home=home, requested=("claude", "gemini"))


def test_schema4_installation_retains_recorded_provider_set(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".config" / "devin").mkdir(parents=True)
    seeded = _installer(home, providers=("claude", "codex"))
    seeded.install_state.parent.mkdir(parents=True)
    seeded.install_state.write_text(
        json.dumps(
            _metadata(
                schema_version=4,
                provider_paths={
                    "claude_settings": str(seeded.claude_settings.resolve()),
                    "claude_config": str(seeded.claude_config.resolve()),
                    "codex_config": str(seeded.codex_config.resolve()),
                    "codex_hooks": str(seeded.codex_hooks.resolve()),
                },
            )
        )
    )

    assert resolve_providers(home=home, devin_global=True) == ("claude", "codex")

    updated = _installer(home, providers=("claude", "codex"))
    updated.setup()
    record = json.loads(updated.install_state.read_text())
    assert record["schema_version"] == 5
    assert record["providers"] == ["claude", "codex"]
    assert set(record["provider_paths"]) == {
        "claude_settings",
        "claude_config",
        "codex_config",
        "codex_hooks",
    }
    assert not (home / ".config" / "devin" / "mcp_config.json").exists()


def test_explicit_selection_cannot_drop_recorded_providers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    seeded = _installer(home, providers=("claude", "codex"))
    seeded.setup()

    with pytest.raises(SettingsError, match="cannot be dropped"):
        resolve_providers(home=home, requested=("claude",))
    assert resolve_providers(home=home, requested=("claude", "codex")) == ("claude", "codex")


def test_constructor_rejects_inconsistent_provider_requests(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(SettingsError, match="Devin global"):
        _installer(home, providers=("devin",))
    with pytest.raises(SettingsError, match="requires the codex provider"):
        _installer(home, providers=("claude",), codex_native_queue=True)
    with pytest.raises(SettingsError, match="provider selection is invalid"):
        _installer(home, providers=())


def test_plan_is_read_only_and_discloses_selected_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('[mcp_servers.other]\ncommand = "other"\n')
    installer = _installer(home, providers=("claude", "codex"))
    before = _snapshot_tree(home)

    plan = installer.plan(staged=True)

    assert _snapshot_tree(home) == before
    assert plan.providers == ("claude", "codex")
    assert plan.roots == {
        "claude": (home / ".claude").resolve(),
        "codex": (home / ".codex").resolve(),
    }
    assert set(plan.managed_paths) == {
        installer.claude_settings.resolve(),
        installer.claude_config.resolve(),
        installer.codex_config.resolve(),
        installer.codex_hooks.resolve(),
        installer.launch_agent.resolve(),
        installer.install_state.resolve(),
    }
    description = plan.describe()
    assert "may contain secrets" in description
    assert "launchd" in description
    assert "Devin" not in description


def test_subset_setup_writes_only_selected_provider(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    codex_config = home / ".codex" / "config.toml"
    codex_hooks = home / ".codex" / "hooks.json"
    codex_config.parent.mkdir()
    codex_config.write_text('[mcp_servers.other]\ncommand = "other"\n')
    codex_hooks.write_text(json.dumps({"hooks": {"Other": [{"command": "other"}]}}))
    installer = _installer(home, providers=("claude",))

    installer.setup()

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert settings["crossSessionInbound"] == "accept"
    assert "cross-agent-chat" in json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert codex_config.read_text() == '[mcp_servers.other]\ncommand = "other"\n'
    assert codex_hooks.read_text() == json.dumps({"hooks": {"Other": [{"command": "other"}]}})
    assert not (home / ".config" / "devin").exists()
    assert installer.launch_agent.exists()
    record = json.loads(installer.install_state.read_text())
    assert record["schema_version"] == 5
    assert record["providers"] == ["claude"]
    assert set(record["provider_paths"]) == {"claude_settings", "claude_config"}
    assert installer.verify_configuration()


def test_subset_uninstall_leaves_unselected_and_shared_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    primary = _installer(home, providers=("claude", "codex"))
    primary.setup()
    alternate = _installer(home, providers=("claude",), codex_home=home / "alt-codex")
    alternate.install_state.parent.mkdir(parents=True, exist_ok=True)
    alternate.install_state.write_text(
        json.dumps(
            _metadata(
                schema_version=5,
                providers=["claude"],
                provider_paths={
                    "claude_settings": str(alternate.claude_settings.resolve()),
                    "claude_config": str(alternate.claude_config.resolve()),
                },
            )
        )
    )

    monkeypatch.setattr(primary, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(primary, "_stop_broker", lambda: None)

    primary.uninstall()

    assert "cross-agent-chat" in (home / ".claude" / "settings.json").read_text()
    assert "cross-agent-chat" in json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert "cross-agent-chat" not in (home / ".codex" / "hooks.json").read_text()
    assert "cross-agent-chat" not in (home / ".codex" / "config.toml").read_text()
    assert not primary.install_state.exists()
    assert alternate.install_state.exists()
    assert primary.launch_agent.exists()


def test_schema5_codex_only_uninstall_never_reads_unselected_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    installer = _installer(home, providers=("codex",))
    installer.setup()
    # Corrupt the unselected Claude roots after the codex-only install: the
    # recorded schema-5 metadata must satisfy the uninstall without reading
    # them, and their bytes must survive untouched.
    claude_settings = home / ".claude" / "settings.json"
    claude_settings.parent.mkdir()
    claude_settings.write_bytes(b"{malformed")
    claude_config = home / ".claude.json"
    claude_config.write_bytes(b"not json{")
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)

    installer.uninstall()

    assert "cross-agent-chat" not in (home / ".codex" / "hooks.json").read_text()
    assert "cross-agent-chat" not in (home / ".codex" / "config.toml").read_text()
    assert claude_settings.read_bytes() == b"{malformed"
    assert claude_config.read_bytes() == b"not json{"
    assert not installer.install_state.exists()


def test_schema5_devin_only_uninstall_never_reads_unselected_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    devin_root = home / ".config" / "devin"
    devin_root.mkdir(parents=True)
    installer = _installer(home, providers=("devin",), devin_global=True)
    installer.setup()
    claude_settings = home / ".claude" / "settings.json"
    claude_settings.parent.mkdir()
    claude_settings.write_bytes(b"{malformed")
    claude_config = home / ".claude.json"
    claude_config.write_bytes(b"not json{")
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)

    installer.uninstall()

    assert "cross-agent-chat" not in (devin_root / "mcp_config.json").read_text()
    assert "cross-agent-chat" not in (devin_root / "config.json").read_text()
    assert claude_settings.read_bytes() == b"{malformed"
    assert claude_config.read_bytes() == b"not json{"
    assert not installer.install_state.exists()


def test_schema5_uninstall_still_surfaces_selected_provider_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolation only covers unselected roots: a malformed selected codex
    # config must still fail the codex-only uninstall.
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    installer = _installer(home, providers=("codex",))
    installer.setup()
    (home / ".codex" / "config.toml").write_bytes(b"\xff\xfe not toml")
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)

    with pytest.raises(SettingsError):
        installer.uninstall()


def test_cli_subset_profile_ignores_invalid_unselected_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A recorded codex-only profile resolves its provider set first, so doctor
    # and uninstall derive the device from the codex root alone and never read
    # the invalid or foreign unselected Claude/Devin configuration.
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    installer = _installer(home, providers=("codex",))
    installer.setup()
    claude_config = home / ".claude.json"
    claude_config.write_bytes(b"not json{")
    claude_settings = home / ".claude" / "settings.json"
    claude_settings.parent.mkdir(exist_ok=True)
    claude_settings.write_bytes(b"{malformed")
    devin_mcp = home / ".config" / "devin" / "mcp_config.json"
    devin_mcp.parent.mkdir(parents=True)
    devin_mcp.write_bytes(b"not json{")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "known_tailnet_address", lambda: None)
    monkeypatch.setattr(cli, "discover_executable", lambda _: Path("/opt/cross-agent-chat"))
    monkeypatch.setattr(Installer, "broker_is_healthy", lambda self, **_: True)
    monkeypatch.setattr(Installer, "broker_is_loaded", lambda self: False)
    monkeypatch.setattr(Installer, "_stop_broker", lambda self: None)

    assert cli.run(parser().parse_args(["doctor", "--json"])) == 0
    capsys.readouterr()
    cli.run(parser().parse_args(["uninstall"]))

    assert claude_config.read_bytes() == b"not json{"
    assert claude_settings.read_bytes() == b"{malformed"
    assert devin_mcp.read_bytes() == b"not json{"
    assert "cross-agent-chat" not in (home / ".codex" / "config.toml").read_text()


def test_install_script_requires_explicit_approval(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = Path(__file__).resolve().parents[1] / "install.sh"

    completed = subprocess.run(
        ["sh", str(script)],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "CROSS_AGENT_CHAT_APPROVE": "",
        },
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    assert completed.returncode == 2
    assert "CROSS_AGENT_CHAT_APPROVE=1" in completed.stderr
    assert "may contain secrets" in completed.stderr
    # The unapproved path writes nothing anywhere in HOME -- including probe
    # side effects like a macOS bytecode cache under Library/Caches.
    assert _snapshot_tree(home) == {}
    assert not any(path.is_dir() for path in home.rglob("*"))


def test_install_script_rejects_unknown_provider_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = Path(__file__).resolve().parents[1] / "install.sh"

    completed = subprocess.run(
        ["sh", str(script)],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "CROSS_AGENT_CHAT_APPROVE": "1",
            "CROSS_AGENT_CHAT_PROVIDERS": "claude,gemini",
        },
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    assert completed.returncode == 2
    assert "unsupported CROSS_AGENT_CHAT_PROVIDERS entry" in completed.stderr
    assert _snapshot_tree(home) == {}
    assert not any(path.is_dir() for path in home.rglob("*"))


def test_install_script_forwards_providers_and_yes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    staged_args = tmp_path / "staged-args"
    home.mkdir()
    fake_bin.mkdir()
    fake_cross_agent = tmp_path / "fake-cross-agent"
    fake_cross_agent.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = --version ]; then exit 0; fi\n'
        'if [ "$1" = _install-staged ]; then\n'
        '  printf "%s\\n" "$@" > "$STAGED_ARGS_CAPTURE"\n'
        '  printf "cross-agent-chat-runtime-v1:committed\\n" > "$3/.cross-agent-chat-release"\n'
        "fi\n"
        "exit 0\n"
    )
    fake_cross_agent.chmod(0o700)
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = venv ]; then\n'
        '  stage="$4"\n'
        '  mkdir -p "$stage/bin"\n'
        '  printf "#!/bin/sh\\nexit 0\\n" > "$stage/bin/python"\n'
        '  cp "$FAKE_CROSS_AGENT" "$stage/bin/cross-agent-chat"\n'
        '  chmod +x "$stage/bin/python" "$stage/bin/cross-agent-chat"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    fake_uv.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "install.sh"

    completed = subprocess.run(
        ["sh", str(script)],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_CROSS_AGENT": str(fake_cross_agent),
            "STAGED_ARGS_CAPTURE": str(staged_args),
            "CROSS_AGENT_CHAT_SOURCE": "candidate-wheel",
            "CROSS_AGENT_CHAT_APPROVE": "1",
            "CROSS_AGENT_CHAT_PROVIDERS": "claude,devin",
        },
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )

    assert completed.returncode == 0
    forwarded = staged_args.read_text().split()
    assert forwarded[0] == "_install-staged"
    assert "--yes" in forwarded
    providers = [forwarded[index + 1] for index, arg in enumerate(forwarded) if arg == "--provider"]
    assert providers == ["claude", "devin"]


def _cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *providers: str) -> Path:
    home = tmp_path / "home"
    for provider in providers:
        (home / f".{provider}").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(cli, "known_tailnet_address", lambda: None)
    monkeypatch.setattr(cli, "discover_executable", lambda _: Path("/opt/cross-agent-chat"))
    return home


def test_setup_cli_prints_plan_then_threads_provider_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _cli_home(tmp_path, monkeypatch, "claude", "codex")
    installed: list[tuple[str, ...]] = []
    monkeypatch.setattr(Installer, "install", lambda self: installed.append(self.providers))

    assert cli.run(parser().parse_args(["setup", "--provider", "claude", "--yes"])) == 0

    output = capsys.readouterr().out
    plan, _, ready = output.partition("Cross Agent Chat is ready")
    assert "Cross Agent Chat setup plan:" in plan
    assert f"claude at {(home / '.claude').resolve()}" in plan
    assert "codex at" not in plan
    assert ready
    assert installed == [("claude",)]


def test_setup_cli_non_tty_without_yes_refuses_without_stdin_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _cli_home(tmp_path, monkeypatch, "claude")

    class _NonTty:
        def isatty(self) -> bool:
            return False

        def read(self, *_args: object) -> str:
            raise AssertionError("stdin was read")

    monkeypatch.setattr(sys, "stdin", _NonTty())
    monkeypatch.setattr(
        Installer, "install", lambda self: pytest.fail("install ran without approval")
    )

    assert cli.main(["setup"]) == 2

    captured = capsys.readouterr()
    assert "Cross Agent Chat setup plan:" in captured.out
    assert "--yes" in captured.err
    assert not (home / ".claude" / "settings.json").exists()
    assert not (home / ".config" / "cross-agent-chat").exists()


def test_setup_cli_tty_decline_and_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _cli_home(tmp_path, monkeypatch, "claude")

    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _Tty())
    installed: list[tuple[str, ...]] = []
    monkeypatch.setattr(Installer, "install", lambda self: installed.append(self.providers))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    with pytest.raises(ChatError, match="not approved"):
        cli.run(parser().parse_args(["setup"]))
    assert installed == []
    assert not (home / ".claude" / "settings.json").exists()

    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    assert cli.run(parser().parse_args(["setup"])) == 0
    assert installed == [("claude",)]


def test_setup_cli_queue_flag_requires_selected_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_home(tmp_path, monkeypatch, "claude", "codex")
    monkeypatch.setattr(
        Installer, "install", lambda self: pytest.fail("install ran without approval")
    )

    assert (
        cli.main(["setup", "--provider", "claude", "--enable-experimental-codex-native-queue"]) == 2
    )


def test_install_staged_requires_yes_before_any_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_home(tmp_path, monkeypatch, "claude")
    monkeypatch.setattr(
        Installer,
        "install_staged",
        lambda self, _stage, _stable: pytest.fail("staged install ran without --yes"),
    )

    with pytest.raises(ChatError, match="requires --yes"):
        cli.run(
            parser().parse_args(
                [
                    "_install-staged",
                    "--staged-runtime",
                    str(tmp_path / "stage"),
                    "--stable-entrypoint",
                    str(tmp_path / "bin/cross-agent-chat"),
                ]
            )
        )


def test_install_staged_threads_providers_and_prints_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = _cli_home(tmp_path, monkeypatch, "claude", "codex")
    installed: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        Installer,
        "install_staged",
        lambda self, _stage, _stable: installed.append(self.providers),
    )

    assert (
        cli.run(
            parser().parse_args(
                [
                    "_install-staged",
                    "--staged-runtime",
                    str(tmp_path / "stage"),
                    "--stable-entrypoint",
                    str(tmp_path / "bin/cross-agent-chat"),
                    "--provider",
                    "claude",
                    "--yes",
                ]
            )
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "Cross Agent Chat setup plan:" in output
    assert f"claude at {(home / '.claude').resolve()}" in output
    assert "codex at" not in output
    assert "Install the runtime" in output
    assert installed == [("claude",)]
