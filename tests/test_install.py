from __future__ import annotations

import json
import os
import plistlib
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import MutableMapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import tomlkit
from tomlkit.exceptions import TOMLKitError

from cross_agent_chat.cli import parser
from cross_agent_chat.install import (
    BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS,
    BROKER_HEALTH_WAIT_SECONDS,
    BrokerService,
    Installer,
    InstallReport,
    PathSnapshot,
    SettingsError,
    SetupRollbackError,
    _owned_hook,
    _package_tree_digest,
    _process_identity_digest,
    _restore_path,
    _snapshot_path,
    discover_executable,
)


def test_install_script_stages_before_runtime_transition() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text()

    assert script.index("pip install") < script.index("_install-staged")
    assert "uv tool install" not in script
    assert "/usr/bin/python3" not in script
    assert "CROSS_AGENT_CHAT_PREVIOUS_RUNTIME" not in script
    assert "CROSS_AGENT_CHAT_RUNTIME_ROOT" not in script
    assert "CROSS_AGENT_CHAT_DEVICE" in script
    assert "your shell resolves" in script


def test_staged_install_parser_accepts_explicit_device(tmp_path: Path) -> None:
    arguments = parser().parse_args(
        [
            "_install-staged",
            "--staged-runtime",
            str(tmp_path / "stage"),
            "--stable-entrypoint",
            str(tmp_path / "bin/cross-agent-chat"),
            "--device",
            "mac",
        ]
    )

    assert arguments.device == "mac"


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
    assert not list(releases.glob("release-*"))
    assert not product_root.exists()


def test_install_script_rejects_symlinked_product_parent_before_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / ".local").symlink_to(outside, target_is_directory=True)
    script = Path(__file__).resolve().parents[1] / "install.sh"

    completed = subprocess.run(
        ["sh", str(script)],
        env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert completed.returncode == 2
    assert "ownership is invalid" in completed.stderr
    assert not list(outside.iterdir())


def test_install_script_supports_in_home_symlinked_data_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    storage = home / "storage"
    fake_bin = tmp_path / "fake-bin"
    (home / ".local").mkdir(parents=True)
    storage.mkdir()
    (home / ".local" / "share").symlink_to(storage, target_is_directory=True)
    fake_bin.mkdir()
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

    completed = subprocess.run(
        ["sh", str(script)],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "CROSS_AGENT_CHAT_SOURCE": "candidate-wheel",
        },
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )

    assert completed.returncode == 19
    assert "ownership is invalid" not in completed.stderr
    assert not list((storage / "cross-agent-chat-runtime" / "releases").glob("release-*"))


def test_install_script_does_not_delete_committed_runtime_after_late_failure(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    home.mkdir()
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = venv ]; then\n'
        '  stage="$4"\n'
        '  mkdir -p "$stage/bin"\n'
        '  printf "#!/bin/sh\\nexit 0\\n" > "$stage/bin/python"\n'
        "  cat > \"$stage/bin/cross-agent-chat\" <<'SCRIPT'\n"
        "#!/bin/sh\n"
        'if [ "$1" = --version ]; then\n'
        "  echo cross-agent-chat 0.1.2\n"
        "  exit 0\n"
        "fi\n"
        'stage="$3"\n'
        "printf 'cross-agent-chat-runtime-v1:committed\\n' > "
        '"$stage/.cross-agent-chat-release"\n'
        "exit 9\n"
        "SCRIPT\n"
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
            "CROSS_AGENT_CHAT_SOURCE": "candidate-wheel",
        },
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )

    releases = list((home / ".local/share/cross-agent-chat-runtime/releases").glob("release-*"))
    assert completed.returncode == 9
    assert len(releases) == 1
    assert (releases[0] / ".cross-agent-chat-release").read_text() == (
        "cross-agent-chat-runtime-v1:committed\n"
    )


def test_install_script_preserves_transaction_owned_runtime_after_child_failure(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    home.mkdir()
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = venv ]; then\n'
        '  stage="$4"\n'
        '  mkdir -p "$stage/bin"\n'
        '  printf "#!/bin/sh\\nexit 0\\n" > "$stage/bin/python"\n'
        "  cat > \"$stage/bin/cross-agent-chat\" <<'SCRIPT'\n"
        "#!/bin/sh\n"
        'if [ "$1" = --version ]; then exit 0; fi\n'
        'stage="$3"\n'
        "printf 'cross-agent-chat-runtime-v1:transaction:"
        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n\' > "$stage/.cross-agent-chat-release"\n'
        "exit 9\n"
        "SCRIPT\n"
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
            "CROSS_AGENT_CHAT_SOURCE": "candidate-wheel",
        },
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )

    releases = list((home / ".local/share/cross-agent-chat-runtime/releases").glob("release-*"))
    assert completed.returncode == 9
    assert len(releases) == 1
    assert (
        (releases[0] / ".cross-agent-chat-release")
        .read_text()
        .startswith("cross-agent-chat-runtime-v1:transaction:")
    )


def test_install_script_falls_back_from_runtime_internal_entrypoint(tmp_path: Path) -> None:
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    runtime_bin = home / ".local/share/cross-agent-chat-runtime/current/bin"
    fake_bin.mkdir()
    runtime_bin.mkdir(parents=True)
    predecessor = runtime_bin / "cross-agent-chat"
    predecessor.write_text("#!/bin/sh\nexit 0\n")
    predecessor.chmod(0o755)
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = venv ]; then\n'
        '  stage="$4"\n'
        '  mkdir -p "$stage/bin"\n'
        '  printf "#!/bin/sh\\nexit 0\\n" > "$stage/bin/python"\n'
        "  cat > \"$stage/bin/cross-agent-chat\" <<'SCRIPT'\n"
        "#!/bin/sh\n"
        'if [ "$1" = --version ]; then exit 0; fi\n'
        'printf "%s" "$5" > "$HOME/stable-captured"\n'
        'printf "cross-agent-chat-runtime-v1:committed\\n" > "$3/.cross-agent-chat-release"\n'
        "exit 0\n"
        "SCRIPT\n"
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
            "PATH": f"{fake_bin}:{runtime_bin}:/usr/bin:/bin",
            "CROSS_AGENT_CHAT_SOURCE": "candidate-wheel",
        },
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )

    assert completed.returncode == 0
    assert (home / "stable-captured").read_text() == str(
        home.resolve() / ".local/bin/cross-agent-chat"
    )


def test_install_script_signal_handlers_terminate_after_cleanup() -> None:
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text()

    assert script.index("trap cleanup EXIT") < script.index('mkdir -p "$releases_root"')
    assert script.index("trap cleanup EXIT") < script.index("staged_runtime=$(mktemp")
    assert "trap cleanup EXIT" in script
    assert "trap 'exit 129' HUP" in script
    assert "trap 'exit 130' INT" in script
    assert "trap 'exit 143' TERM" in script
    assert "trap cleanup EXIT HUP INT TERM" not in script


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
    assert config["mcp_servers"]["cross-agent-chat"]["default_tools_approval_mode"] == "approve"
    assert len(trusted) == 3
    assert all(
        isinstance(item, dict) and str(item.get("trusted_hash", "")).startswith("sha256:")
        for item in trusted.values()
    )


@pytest.mark.parametrize(
    "replacement",
    ("", 'default_tools_approval_mode = "prompt"\n'),
)
def test_verify_configuration_requires_codex_auto_approval(
    tmp_path: Path, replacement: str
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    assert installer.verify_configuration()

    codex_config = home / ".codex" / "config.toml"
    codex_config.write_text(
        codex_config.read_text().replace('default_tools_approval_mode = "approve"\n', replacement)
    )

    assert not installer.verify_configuration()


def test_setup_removes_owned_tool_approval_overrides_and_preserves_other_properties(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(
        "[mcp_servers.'cross-agent-chat'.tools.chat_send] # owned tool\n"
        '"approval_mode" = "prompt"\n'
        "enabled = false\n"
        "\n[[unrelated]]\n"
        'approval_mode = "prompt"\n'
        '\n[mcp_servers."other"]\n'
        'command = "other"\n'
    )
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    server = config["mcp_servers"]["cross-agent-chat"]
    assert server["default_tools_approval_mode"] == "approve"
    assert server["tools"]["chat_send"] == {"enabled": False}
    assert config["unrelated"] == [{"approval_mode": "prompt"}]
    assert config["mcp_servers"]["other"] == {"command": "other"}
    assert installer.verify_configuration()


@pytest.mark.parametrize(
    "owned_tools",
    (
        '[mcp_servers."cross-agent-chat".tools]\n'
        'chat_send = { approval_mode = "never", enabled = false }\n',
        '[mcp_servers."cross-agent-chat"]\n'
        'tools = { chat_send = { approval_mode = "never", enabled = false }, '
        'chat_peers = { approval_mode = "prompt", description = "peers" } }\n',
    ),
)
def test_setup_removes_inline_owned_tool_approval_overrides(
    tmp_path: Path, owned_tools: str
) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(owned_tools + '\n[mcp_servers."other"]\ncommand = "other"\n')
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    tools = config["mcp_servers"]["cross-agent-chat"]["tools"]
    assert tools["chat_send"] == {"enabled": False}
    if "chat_peers" in tools:
        assert tools["chat_peers"] == {"description": "peers"}
    assert config["mcp_servers"]["other"] == {"command": "other"}
    assert installer.verify_configuration()


def test_setup_does_not_rewrite_assignment_text_inside_multiline_value(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    description = 'first line\napproval_mode = "never"\nlast line\n'
    codex_config.write_text(
        '[mcp_servers."cross-agent-chat".tools.chat_send]\n'
        'description = """first line\napproval_mode = "never"\nlast line\n"""\n'
    )
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    assert config["mcp_servers"]["cross-agent-chat"]["tools"]["chat_send"] == {
        "description": description
    }
    assert installer.verify_configuration()


@pytest.mark.parametrize(
    "tool_section",
    (
        '[mcp_servers."cross-agent-chat".tools]\n'
        'chat_send = { approval_mode = "never", enabled = false }\n',
        '[mcp_servers."cross-agent-chat".tools.chat_send]\n'
        'approval_mode = "never"\n'
        "enabled = false\n",
    ),
)
def test_setup_normalizes_separate_tool_table_after_server_table(
    tmp_path: Path, tool_section: str
) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text('[mcp_servers."cross-agent-chat"]\ncommand = "old"\n\n' + tool_section)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    server = config["mcp_servers"]["cross-agent-chat"]
    assert server["tools"]["chat_send"] == {"enabled": False}
    assert server["command"] == "/opt/cross-agent-chat"
    assert installer.verify_configuration()


def test_setup_preserves_non_bmp_inline_tool_property(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(
        '[mcp_servers."cross-agent-chat"]\n'
        'tools = { chat_send = { approval_mode = "never", description = "chat 🚀" } }\n'
    )
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    assert config["mcp_servers"]["cross-agent-chat"]["tools"]["chat_send"] == {
        "description": "chat 🚀"
    }
    assert installer.verify_configuration()


def test_setup_normalizes_mixed_dotted_tool_and_separate_tool_table(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(
        '[mcp_servers."cross-agent-chat"]\n'
        'command = "old"\n'
        'tools.chat_send = { approval_mode = "never" }\n\n'
        '[mcp_servers."cross-agent-chat".tools.chat_peers]\n'
        "enabled = true\n"
    )
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    tools = config["mcp_servers"]["cross-agent-chat"]["tools"]
    assert tools["chat_send"] == {}
    assert tools["chat_peers"] == {"enabled": True}
    assert installer.verify_configuration()


def test_setup_preserves_toml_escaped_delete_character(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(
        '[mcp_servers."cross-agent-chat"]\n'
        'tools = { chat_send = { approval_mode = "never", description = "chat \\u007F" } }\n'
    )
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    assert config["mcp_servers"]["cross-agent-chat"]["tools"]["chat_send"] == {
        "description": "chat \x7f"
    }
    assert installer.verify_configuration()


def test_setup_writes_non_bmp_owned_paths_as_valid_toml(tmp_path: Path) -> None:
    home = tmp_path / "home-🚀"
    executable = home / ".local/bin/cross-agent-chat"
    installer = Installer(home=home, executable=executable, device="studio")

    installer.setup()

    config = tomllib.loads(installer.codex_config.read_text())
    assert config["mcp_servers"]["cross-agent-chat"]["command"] == str(executable)
    assert installer.verify_configuration()


@pytest.mark.parametrize(
    "feature_config",
    (
        "[features] # user comment\nhooks = false\nother = true\n",
        "features.hooks = false\nfeatures.other = true\n",
    ),
)
def test_setup_normalizes_commented_or_dotted_features_table(
    tmp_path: Path, feature_config: str
) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(feature_config)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    assert config["features"] == {"hooks": True, "other": True}
    assert installer.verify_configuration()


@pytest.mark.parametrize(
    "feature_config",
    (
        "[features.sub]\nx = 1\n\n[features]\nhooks = false\n",
        "[features.sub]\nx = 1\n",
    ),
)
def test_setup_normalizes_features_subtable_before_or_without_parent(
    tmp_path: Path, feature_config: str
) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(feature_config)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    assert config["features"] == {"hooks": True, "sub": {"x": 1}}
    assert installer.verify_configuration()


def test_setup_tracks_array_table_context_when_enabling_hooks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text("[[servers]]\nfeatures.enabled = true\n\n[features]\nhooks = false\n")
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    assert config["servers"] == [{"features": {"enabled": True}}]
    assert config["features"] == {"hooks": True}
    assert installer.verify_configuration()


@pytest.mark.parametrize(
    "feature_config",
    (
        "features.hooks.trusted = true\n",
        "[features]\nhooks.trusted = true\n",
        "[features.hooks]\ntrusted = true\n",
    ),
)
def test_setup_replaces_conflicting_features_hooks_descendants(
    tmp_path: Path, feature_config: str
) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(feature_config)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    assert config["features"] == {"hooks": True}
    assert installer.verify_configuration()


def test_setup_ignores_features_header_text_inside_multiline_string(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text('description = """before\n[features]\nafter\n"""\n')
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    assert config["description"] == "before\n[features]\nafter\n"
    assert config["features"] == {"hooks": True}
    assert installer.verify_configuration()


@pytest.mark.parametrize(
    ("feature_config", "expected"),
    (
        ("features = { hooks = false, x = 1 }\n", {"hooks": True, "x": 1}),
        ("features = true\n", {"hooks": True}),
        (
            '[[features.hooks.entries]]\nname = "a"\n',
            {"hooks": True},
        ),
    ),
)
def test_setup_normalizes_inline_scalar_or_array_hooks_features(
    tmp_path: Path, feature_config: str, expected: dict[str, object]
) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(feature_config)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    installer.setup()

    config = tomllib.loads(codex_config.read_text())
    assert config["features"] == expected
    assert installer.verify_configuration()


@pytest.mark.parametrize(
    "invalid_config",
    (
        '[mcp_servers.foo]\ncommand = "one"\n[mcp_servers.foo]\ncommand = "two"\n',
        "value = 1\nvalue = 2\n",
    ),
)
def test_setup_reports_invalid_codex_toml_without_writing(
    tmp_path: Path, invalid_config: str
) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(invalid_config)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    with pytest.raises(SettingsError, match=re.escape("Codex config.toml is invalid")):
        installer.setup()

    assert codex_config.read_text() == invalid_config


def test_setup_rejects_non_table_codex_mcp_servers_without_writing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    original = '[[mcp_servers]]\nname = "unrelated"\n'
    codex_config.write_text(original)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    with pytest.raises(SettingsError, match="Codex mcp_servers must be a table"):
        installer.setup()

    assert codex_config.read_text() == original


def test_tomlkit_dependency_contract() -> None:
    with pytest.raises(TOMLKitError):
        tomlkit.parse("value = 1\nvalue = 2\n")

    array = tomlkit.parse('[[mcp_servers]]\nname = "unrelated"\n')["mcp_servers"]
    assert not isinstance(array, MutableMapping)


def test_setup_rejects_undecodable_codex_config_without_writing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    original = b"\xff\xfeinvalid"
    codex_config.write_bytes(original)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    with pytest.raises(SettingsError, match=re.escape(str(codex_config))):
        installer.setup()

    assert codex_config.read_bytes() == original


def test_uninstall_rejects_undecodable_codex_config_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    installer.codex_config.write_bytes(b"\xff\xfeinvalid")
    before = {path: path.read_bytes() for path in installer.config_paths if path.exists()}
    monkeypatch.setattr(
        installer,
        "_stop_broker",
        lambda: pytest.fail("uninstall mutated state before validating Codex config.toml"),
    )

    with pytest.raises(SettingsError, match=re.escape(str(installer.codex_config))):
        installer.uninstall()

    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("initially_present", (False, True))
def test_uninstall_never_overwrites_codex_config_changed_during_broker_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initially_present: bool
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    if not initially_present:
        installer.codex_config.unlink()
    replacement = '[mcp_servers."new-user-server"]\ncommand = "new"\n'

    def stop_broker() -> None:
        installer.codex_config.parent.mkdir(parents=True, exist_ok=True)
        installer.codex_config.write_text(replacement)

    monkeypatch.setattr(installer, "_stop_broker", stop_broker)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)

    installer.uninstall()

    assert installer.codex_config.read_text() == replacement
    assert not installer.launch_agent.exists()
    assert not installer.install_state.exists()


def test_verify_configuration_rejects_conflicting_owned_tool_approval(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    codex_config = home / ".codex" / "config.toml"
    codex_config.write_text(
        codex_config.read_text()
        + '\n[mcp_servers."cross-agent-chat".tools.chat_send]\napproval_mode = "prompt"\n'
    )

    assert not installer.verify_configuration()


def test_setup_removes_stale_owned_hook_trust_and_preserves_unrelated_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        f"[hooks.state.{json.dumps(key)}] # stale owned trust\n"
        f"# formatting must not block repair\n"
        f"trusted_hash = {json.dumps(trusted_hash)}\n\n"
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

    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
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


def test_setup_fails_closed_on_duplicate_owned_hooks_without_reindexing_trust(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    installer.setup()
    hooks = json.loads(installer.codex_hooks.read_text())
    groups = hooks["hooks"]["SessionStart"]
    groups.insert(0, groups[0])
    installer.codex_hooks.write_text(json.dumps(hooks))
    config_before = installer.codex_config.read_bytes()
    hooks_before = installer.codex_hooks.read_bytes()

    with pytest.raises(SettingsError, match="ownership is ambiguous"):
        installer.setup()

    assert installer.codex_config.read_bytes() == config_before
    assert installer.codex_hooks.read_bytes() == hooks_before


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


def test_setup_rollback_preserves_in_home_symlink_and_target_mode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / "shared/claude-settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"theme": "light"}))
    target.chmod(0o640)
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.symlink_to(target)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    with pytest.raises(SettingsError, match="verification failed"):
        installer.setup(verify=lambda: False)

    assert settings.is_symlink()
    assert settings.resolve() == target
    assert json.loads(target.read_text()) == {"theme": "light"}
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_setup_rejects_configuration_parent_symlink_outside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / ".claude").symlink_to(outside, target_is_directory=True)
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")

    with pytest.raises(SettingsError, match="configuration path escapes home"):
        installer.setup()

    assert not (outside / "settings.json").exists()


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
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)

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
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)

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
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
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
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    installer.activate()

    assert bootstrap_attempts == 2


def test_activate_stops_only_verified_owner_local_orphan_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    executable = home / "legacy-runtime" / "bin" / "cross-agent-chat"
    executable.parent.mkdir(parents=True)
    executable.write_text("legacy")
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    killed: list[tuple[int, int]] = []
    released = iter((False, True))

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(command, 0, "4242\n", "")
        if command[:5] == ["/bin/ps", "-p", "4242", "-o", "uid="]:
            return subprocess.CompletedProcess(command, 0, f"{os.getuid()}\n", "")
        if command[:5] == ["/bin/ps", "-ww", "-p", "4242", "-o"]:
            return subprocess.CompletedProcess(
                command,
                0,
                f"/usr/bin/python3 {executable} _broker\n",
                "",
            )
        if command == [str(executable), "--version"]:
            return subprocess.CompletedProcess(command, 0, "cross-agent-chat 0.1.1\n", "")
        if command[:2] == ["launchctl", "bootstrap"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 113, "", "not found")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: False)
    monkeypatch.setattr(installer, "_wait_for_broker_port_release", lambda: next(released))
    monkeypatch.setattr(
        "cross_agent_chat.install.os.kill",
        lambda pid, sent_signal: killed.append((pid, sent_signal)),
    )

    installer.activate()

    assert killed == [(4242, signal.SIGTERM)]


def test_orphan_stop_rejects_process_incarnation_change_before_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    executable = home / "legacy-runtime/bin/cross-agent-chat"
    executable.parent.mkdir(parents=True)
    executable.write_text("legacy")
    installer = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")
    identities = iter(("a" * 64, "b" * 64))
    killed: list[int] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(command, 0, "4242\n", "")
        if command[:5] == ["/bin/ps", "-p", "4242", "-o", "uid="]:
            return subprocess.CompletedProcess(command, 0, f"{os.getuid()}\n", "")
        if command[:5] == ["/bin/ps", "-ww", "-p", "4242", "-o"]:
            return subprocess.CompletedProcess(
                command, 0, f"/usr/bin/python3 {executable} _broker\n", ""
            )
        if command == [str(executable.resolve()), "--version"]:
            return subprocess.CompletedProcess(command, 0, "cross-agent-chat 0.1.1\n", "")
        return subprocess.CompletedProcess(command, 113, "", "not found")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: False)
    monkeypatch.setattr(
        "cross_agent_chat.install._process_identity_digest", lambda _pid: next(identities)
    )
    monkeypatch.setattr("cross_agent_chat.install.os.kill", lambda pid, _signal: killed.append(pid))

    with pytest.raises(SettingsError, match="process changed before termination"):
        installer._stop_owned_orphan_broker()

    assert not killed


def test_orphan_stop_accepts_process_exit_at_signal_when_port_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    executable = home / "legacy-runtime/bin/cross-agent-chat"
    executable.parent.mkdir(parents=True)
    executable.write_text("legacy")
    installer = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")
    port_checks = iter((False, True))

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(command, 0, "4242\n", "")
        if command[:5] == ["/bin/ps", "-p", "4242", "-o", "uid="]:
            return subprocess.CompletedProcess(command, 0, f"{os.getuid()}\n", "")
        if command[:5] == ["/bin/ps", "-ww", "-p", "4242", "-o"]:
            return subprocess.CompletedProcess(
                command, 0, f"/usr/bin/python3 {executable} _broker\n", ""
            )
        if command == [str(executable.resolve()), "--version"]:
            return subprocess.CompletedProcess(command, 0, "cross-agent-chat 0.1.1\n", "")
        return subprocess.CompletedProcess(command, 113, "", "not found")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: next(port_checks))
    monkeypatch.setattr("cross_agent_chat.install._process_identity_digest", lambda _pid: "a" * 64)
    monkeypatch.setattr(
        "cross_agent_chat.install.os.kill",
        lambda _pid, _signal: (_ for _ in ()).throw(ProcessLookupError()),
    )

    installer._stop_owned_orphan_broker()


def test_bootout_failure_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["launchctl", "bootout"]:
            return subprocess.CompletedProcess(command, 5, "", "failed")
        return subprocess.CompletedProcess(command, 0, "state = running\n", "")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)

    with pytest.raises(SettingsError, match="could not stop"):
        installer._bootout()


def test_stop_broker_requires_port_release_after_label_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    calls: list[str] = []
    monkeypatch.setattr(installer, "_bootout", lambda: calls.append("bootout"))
    monkeypatch.setattr(installer, "_wait_for_broker_port_release", lambda: False)
    monkeypatch.setattr(installer, "_stop_owned_orphan_broker", lambda: calls.append("orphan"))

    installer._stop_broker()

    assert calls == ["bootout", "orphan"]


def test_broker_port_probe_uses_broker_reuse_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    calls: list[str] = []

    class ProbeSocket:
        def setsockopt(self, level: int, option: int, value: int) -> None:
            assert (level, option, value) == (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            calls.append("reuse")

        def bind(self, address: tuple[str, int]) -> None:
            assert address[0] == "127.0.0.1"
            calls.append("bind")

        def listen(self, backlog: int) -> None:
            assert backlog == 1
            calls.append("listen")

        def close(self) -> None:
            calls.append("close")

    probe = ProbeSocket()
    monkeypatch.setattr("cross_agent_chat.install.socket.socket", lambda *_args: probe)
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(
            command,
            0,
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            f"python {os.getpid()} user 1u IPv4 0 0t0 "
            "TCP 127.0.0.1:47072 (LISTEN)\n",
            "",
        ),
    )

    assert installer._broker_port_is_available()
    assert calls == ["reuse", "bind", "listen", "close"]


def test_broker_port_probe_rejects_second_wildcard_listener_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )

    class ProbeSocket:
        def setsockopt(self, _level: int, _option: int, _value: int) -> None:
            return

        def bind(self, _address: tuple[str, int]) -> None:
            return

        def listen(self, _backlog: int) -> None:
            return

        def close(self) -> None:
            return

    monkeypatch.setattr("cross_agent_chat.install.socket.socket", lambda *_args: ProbeSocket())
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(
            command,
            0,
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
            f"python {os.getpid()} user 1u IPv4 0 0t0 TCP 127.0.0.1:47072 (LISTEN)\n"
            "python 4242 user 2u IPv4 0 0t0 TCP *:47072 (LISTEN)\n",
            "",
        ),
    )

    assert not installer._broker_port_is_available()


def test_broker_port_probe_timeout_returns_false_and_closes_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    closed = False

    class ProbeSocket:
        def setsockopt(self, _level: int, _option: int, _value: int) -> None:
            return

        def bind(self, _address: tuple[str, int]) -> None:
            return

        def listen(self, _backlog: int) -> None:
            return

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr("cross_agent_chat.install.socket.socket", lambda *_args: ProbeSocket())
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(["lsof"], 5)),
    )

    assert not installer._broker_port_is_available()
    assert closed


def test_orphan_probe_accepts_port_released_before_listener_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    port_checks = iter((False, True))
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: next(port_checks))
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 1, "", "not found"),
    )

    installer._stop_owned_orphan_broker()


def test_orphan_probe_accepts_port_released_after_listener_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    port_checks = iter((False, True))
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: next(port_checks))
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, "4242\n", ""),
    )
    monkeypatch.setattr("cross_agent_chat.install._process_identity_digest", lambda _pid: None)

    installer._stop_owned_orphan_broker()


def test_orphan_probe_accepts_port_release_after_lsof_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    port_checks = iter((False, True))
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: next(port_checks))
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(["lsof"], 5)),
    )

    installer._stop_owned_orphan_broker()


def test_orphan_probe_timeout_remains_fail_closed_when_port_is_occupied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: False)
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(["lsof"], 5)),
    )

    with pytest.raises(SettingsError, match="unverified process"):
        installer._stop_owned_orphan_broker()


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
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)

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
    monkeypatch.setattr(installer, "_wait_for_previous_broker_health", healthy)
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
    assert not installer.claude_settings.exists()
    assert not installer.launch_agent.exists()


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
    monkeypatch.setattr(installer, "_wait_for_previous_broker_health", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)

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
    monkeypatch.setattr(installer, "_wait_for_previous_broker_health", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)
    monkeypatch.setattr(installer, "verify", lambda **_kwargs: False)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
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
    monkeypatch.setattr(installer, "_wait_for_previous_broker_health", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)

    with pytest.raises(SettingsError) as captured:
        installer.install()

    message = str(captured.value)
    assert "candidate activation failed" in message
    assert "predecessor restart failed" in message
    backups = list((home / ".cache" / "cross-agent-chat" / "backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "manifest.json").exists()


def _staged_runtime(installer: Installer, marker: str = "candidate") -> Path:
    stage = installer.releases / "release-test"
    executable = stage / "bin" / "cross-agent-chat"
    executable.parent.mkdir(parents=True)
    executable.write_text(marker)
    executable.chmod(0o755)
    identity = _process_identity_digest(os.getpid())
    assert identity is not None
    (stage / ".cross-agent-chat-release").write_text(
        f"cross-agent-chat-runtime-v1:staged:{os.getpid()}:{identity}\n"
    )
    return stage


def _bind_transaction_marker(stage: Path, transaction: Path) -> None:
    (stage / ".cross-agent-chat-release").write_text(
        f"cross-agent-chat-runtime-v1:transaction:{transaction.name}\n"
    )


def test_restore_absent_path_fsyncs_parent_after_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = tmp_path / "runtime/current"
    pointer.parent.mkdir()
    pointer.symlink_to("candidate")
    fsynced: list[Path] = []
    monkeypatch.setattr(
        "cross_agent_chat.install._fsync_directory", lambda path: fsynced.append(path)
    )

    _restore_path(PathSnapshot(path=pointer, kind="absent"))

    assert not pointer.exists()
    assert fsynced == [pointer.parent]


def test_staged_install_commits_stable_runtime_and_provider_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "custom-tools" / "cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "verify", lambda **_kwargs: True)

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


def test_staged_upgrade_persists_entrypoint_selected_for_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    previous_stable = home / "previous-bin/cross-agent-chat"
    installer = Installer(home=home, executable=previous_stable, device="studio")
    installer.setup()
    selected_stable = home / "selected-bin/cross-agent-chat"
    stage = _staged_runtime(installer)
    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "verify", lambda **_kwargs: True)

    installer.install_staged(stage, selected_stable)

    metadata = json.loads(installer.install_state.read_text())
    assert metadata["stable_entrypoint"] == str(selected_stable.relative_to(home))


def test_staged_install_fsyncs_backup_and_journal_parents_before_first_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / ".local/bin/cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    events: list[str] = []
    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
    monkeypatch.setattr(
        "cross_agent_chat.install._fsync_directory",
        lambda path: events.append(f"fsync:{path}"),
    )
    monkeypatch.setattr(installer, "_stop_couriers", lambda: events.append("first-effect"))
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "verify", lambda **_kwargs: True)

    installer.install_staged(stage, stable)

    before_effect = events[: events.index("first-effect")]
    assert f"fsync:{installer.cache / 'backups'}" in before_effect
    assert f"fsync:{installer.cache}" in before_effect
    assert f"fsync:{home / '.cache'}" in before_effect
    assert f"fsync:{installer.runtime_root}" in before_effect
    assert f"fsync:{home / '.local/share'}" in before_effect
    assert f"fsync:{home / '.local'}" in before_effect
    assert f"fsync:{installer.transactions}" in before_effect
    assert f"fsync:{stage}" in before_effect
    assert f"fsync:{installer.releases}" in before_effect
    for durable_parent in (
        stable.parent,
        installer.claude_settings.parent,
        installer.codex_config.parent,
        installer.install_state.parent,
        installer.launch_agent.parent,
    ):
        assert f"fsync:{durable_parent}" in before_effect
    assert f"fsync:{home}" in before_effect


def test_staged_install_executes_non_relocated_venv_after_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / ".local" / "bin" / "cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = installer.releases / "release-real-venv"
    subprocess.run([sys.executable, "-m", "venv", str(stage)], check=True, timeout=30.0)
    identity = _process_identity_digest(os.getpid())
    assert identity is not None
    (stage / ".cross-agent-chat-release").write_text(
        f"cross-agent-chat-runtime-v1:staged:{os.getpid()}:{identity}\n"
    )
    candidate = stage / "bin" / "cross-agent-chat"
    candidate.write_text(
        f"#!{stage / 'bin' / 'python'}\n"
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('cross-agent-chat 0.1.2')\n"
        "elif sys.argv[1:] == ['_broker', '--help']:\n"
        "    print('broker help')\n"
        "else:\n"
        "    raise SystemExit(2)\n"
    )
    candidate.chmod(0o755)
    source_root = Path(__file__).resolve().parents[1] / "src"
    monkeypatch.setenv("PYTHONPATH", str(source_root))
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "verify", lambda **_kwargs: True)

    installer.install_staged(stage, stable)

    completed = subprocess.run(
        [str(stable), "--version"],
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "cross-agent-chat 0.1.2"
    assert stage.exists()


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
    (previous / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    installer.current_runtime.symlink_to("releases/release-previous")
    stage = _staged_runtime(installer)

    def fail_setup(*, prepared: object) -> InstallReport:
        assert prepared is not None
        raise SettingsError("setup failed")

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
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


def test_staged_install_rolls_back_under_in_home_symlinked_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    storage = home / "storage"
    (home / ".local").mkdir(parents=True)
    storage.mkdir()
    (home / ".local/share").symlink_to(storage, target_is_directory=True)
    stable = home / "bin/cross-agent-chat"
    stable.parent.mkdir()
    stable.write_text("predecessor")
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)

    def fail_setup(*, prepared: object) -> InstallReport:
        assert prepared is not None
        raise SettingsError("setup failed")

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "setup", fail_setup)

    with pytest.raises(SettingsError, match="transition failed but rollback succeeded"):
        installer.install_staged(stage, stable)

    assert stable.read_text() == "predecessor"
    assert not stage.exists()
    assert not list(installer.transactions.iterdir())


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
    (previous / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
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
    monkeypatch.setattr(installer, "_wait_for_previous_broker_health", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)

    with pytest.raises(
        SettingsError,
        match="transition failed but rollback succeeded: candidate activation failed",
    ):
        installer.install_staged(stage, stable)

    assert activations == 2
    assert os.readlink(stable) == "../custom-runtime/bin/cross-agent-chat"
    assert os.readlink(installer.current_runtime) == "releases/release-previous"
    assert predecessor.read_text() == "predecessor"


def test_staged_install_restores_independent_surfaces_when_rollback_bootout_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "configured-bin" / "cross-agent-chat"
    stable.parent.mkdir(parents=True)
    stable.write_text("predecessor-entrypoint")
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "light"}))
    original_settings = settings.read_bytes()
    installer = Installer(home=home, executable=stable, device="studio")
    previous = installer.releases / "release-previous"
    (previous / "bin").mkdir(parents=True)
    (previous / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    installer.current_runtime.symlink_to("releases/release-previous")
    stage = _staged_runtime(installer)

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "_wait_for_previous_broker_health", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(
        installer,
        "activate",
        lambda: (_ for _ in ()).throw(SettingsError("candidate activation failed")),
    )
    monkeypatch.setattr(
        installer,
        "_bootout",
        lambda: (_ for _ in ()).throw(SettingsError("candidate bootout failed")),
    )

    with pytest.raises(SettingsError, match="transition and rollback failed") as captured:
        installer.install_staged(stage, stable)

    assert "candidate bootout failed" in str(captured.value)
    assert stable.read_text() == "predecessor-entrypoint"
    assert os.readlink(installer.current_runtime) == "releases/release-previous"
    assert settings.read_bytes() == original_settings
    assert any(installer.transactions.iterdir())


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


def test_staged_install_fails_before_mutation_for_unmanaged_orphan_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / ".local/bin/cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    stopped = False

    def stop() -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: False)
    monkeypatch.setattr(installer, "_stop_couriers", stop)

    with pytest.raises(SettingsError, match="unmanaged; no changes were made"):
        installer.install_staged(stage, stable)

    assert not stopped
    assert stage.exists()
    assert not stable.exists()
    assert not installer.transactions.exists()


def test_staged_install_rejects_stable_parent_symlink_outside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = home / "linked-bin"
    linked_parent.parent.mkdir(parents=True)
    linked_parent.symlink_to(outside, target_is_directory=True)
    installer = Installer(home=home, executable=linked_parent / "cross-agent-chat", device="studio")

    with pytest.raises(SettingsError, match="owner-local"):
        installer._validate_stable_entrypoint(linked_parent / "cross-agent-chat")


def test_durable_parent_rejects_escape_before_creating_external_child(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / ".config").symlink_to(outside, target_is_directory=True)
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )

    with pytest.raises(SettingsError, match="escapes home"):
        installer._ensure_durable_parent(home / ".config/cross-agent-chat")

    assert not (outside / "cross-agent-chat").exists()


def test_durable_parent_creates_private_managed_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    parents = (
        home / ".claude",
        home / ".codex",
        home / ".config/cross-agent-chat",
        home / ".local/bin",
        home / "Library/LaunchAgents",
    )

    for parent in parents:
        installer._ensure_durable_parent(parent)

    for parent in parents:
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_staged_install_canonicalizes_entrypoint_under_symlinked_home(tmp_path: Path) -> None:
    physical_home = tmp_path / "physical-home"
    physical_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(physical_home, target_is_directory=True)
    installer = Installer(
        home=linked_home,
        executable=linked_home / ".local/bin/cross-agent-chat",
        device="studio",
    )

    stable = installer._validate_stable_entrypoint(linked_home / ".local/bin/cross-agent-chat")

    assert stable == physical_home / ".local/bin/cross-agent-chat"


@pytest.mark.parametrize("relative", ["current/bin", "releases/release-old/bin"])
def test_staged_install_rejects_runtime_internal_stable_entrypoint(
    tmp_path: Path, relative: str
) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    internal = installer.runtime_root / relative / "cross-agent-chat"

    with pytest.raises(SettingsError, match="owner-local"):
        installer._validate_stable_entrypoint(internal)


def test_staged_install_rejects_symlinked_product_runtime_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    installer.runtime_root.parent.mkdir(parents=True)
    installer.runtime_root.symlink_to(outside, target_is_directory=True)
    stage = outside / "releases" / "release-test"
    stage.mkdir(parents=True)

    with pytest.raises(SettingsError, match="runtime ownership is invalid"):
        installer._validate_staged_runtime(stage)


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

    with pytest.raises(SettingsError, match="transaction metadata is invalid"):
        installer.install_staged(stage, stable)

    assert stage.exists()
    assert not stable.exists()


def test_recovery_discards_pre_mutation_preparing_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    preparing = installer.transactions / ".preparing-interrupted"
    preparing.mkdir(parents=True)
    (preparing / "partial").write_text("incomplete")

    installer._recover_unfinished_transaction()

    assert not preparing.exists()
    assert not list(installer.transactions.iterdir())


def test_recovery_discards_non_transaction_file_residue(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    installer.transactions.mkdir(parents=True)
    (installer.transactions / ".DS_Store").write_text("residue")

    installer._recover_unfinished_transaction()

    assert not list(installer.transactions.iterdir())


def test_recovery_names_ambiguous_transaction_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    ambiguous = installer.transactions / "unknown-directory"
    ambiguous.mkdir(parents=True)

    with pytest.raises(SettingsError, match=re.escape(str(ambiguous))):
        installer._recover_unfinished_transaction()


def test_prepared_recovery_preserves_newer_runtime_and_entrypoint_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    stable.parent.mkdir(parents=True)
    stable.write_text("predecessor")
    installer = Installer(home=home, executable=stable, device="studio")
    previous = installer.releases / "release-previous"
    (previous / "bin").mkdir(parents=True)
    (previous / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    installer.current_runtime.symlink_to("releases/release-previous")
    stage = _staged_runtime(installer)
    prepared = installer._prepare_setup()
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    installer._record_transaction(
        transaction,
        phase="prepared",
        staged=stage,
        stable_entrypoint=stable,
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=_package_tree_digest(stage),
    )
    _bind_transaction_marker(stage, transaction)
    newer = installer.releases / "release-newer"
    (newer / "bin").mkdir(parents=True)
    (newer / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    installer.current_runtime.unlink()
    installer.current_runtime.symlink_to("releases/release-newer")
    stable.write_text("newer-entrypoint")

    installer._recover_unfinished_transaction()

    assert os.readlink(installer.current_runtime) == "releases/release-newer"
    assert stable.read_text() == "newer-entrypoint"
    assert not stage.exists()
    assert not transaction.exists()


def test_prepared_recovery_accepts_absent_entrypoint_parent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    stable = home / ".local/bin/cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    prepared = installer._prepare_setup()
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    installer._record_transaction(
        transaction,
        phase="prepared",
        staged=stage,
        stable_entrypoint=stable,
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=_package_tree_digest(stage),
    )
    _bind_transaction_marker(stage, transaction)

    installer._recover_unfinished_transaction()

    assert not stable.parent.exists()
    assert not stage.exists()
    assert not transaction.exists()


def test_prepared_recovery_drops_journal_before_candidate_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    prepared = installer._prepare_setup()
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    installer._record_transaction(
        transaction,
        phase="prepared",
        staged=stage,
        stable_entrypoint=stable,
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=_package_tree_digest(stage),
    )
    _bind_transaction_marker(stage, transaction)
    original_remove = installer._remove_release
    monkeypatch.setattr(
        installer,
        "_remove_release",
        lambda _candidate: (_ for _ in ()).throw(OSError("interrupted cleanup")),
    )

    with pytest.raises(OSError, match="interrupted cleanup"):
        installer._recover_unfinished_transaction()

    assert not transaction.exists()
    assert stage.exists()
    monkeypatch.setattr(installer, "_remove_release", original_remove)
    installer._prune_abandoned_staged_releases()
    assert not stage.exists()


def test_recovery_rejects_transaction_that_names_active_committed_release(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    stable.parent.mkdir(parents=True)
    installer = Installer(home=home, executable=stable, device="studio")
    active = installer.releases / "release-active"
    (active / "bin").mkdir(parents=True)
    (active / "bin/cross-agent-chat").write_text("active")
    (active / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    installer.current_runtime.symlink_to("releases/release-active")
    stable.symlink_to(installer.current_runtime / "bin/cross-agent-chat")
    prepared = installer._prepare_setup()
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    installer._record_transaction(
        transaction,
        phase="prepared",
        staged=active,
        stable_entrypoint=stable,
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=_package_tree_digest(active),
    )

    with pytest.raises(SettingsError, match="transaction metadata is invalid"):
        installer._recover_unfinished_transaction()

    assert active.exists()
    assert installer.current_runtime.resolve() == active.resolve()
    assert stable.resolve() == active / "bin/cross-agent-chat"


def test_recovery_rejects_runtime_internal_stable_entrypoint(tmp_path: Path) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    stable.parent.mkdir(parents=True)
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    prepared = installer._prepare_setup()
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    installer._record_transaction(
        transaction,
        phase="prepared",
        staged=stage,
        stable_entrypoint=stable,
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=_package_tree_digest(stage),
    )
    _bind_transaction_marker(stage, transaction)
    metadata_path = transaction / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["stable_entrypoint"] = str(
        (installer.runtime_root / "current/bin/cross-agent-chat").relative_to(home)
    )
    metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(SettingsError, match="owner-local"):
        installer._recover_unfinished_transaction()

    assert stage.exists()


def test_runtime_switching_recovery_restores_pointer_and_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    stable.parent.mkdir(parents=True)
    stable.write_text("predecessor")
    installer = Installer(home=home, executable=stable, device="studio")
    previous = installer.releases / "release-previous"
    (previous / "bin").mkdir(parents=True)
    (previous / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    installer.current_runtime.symlink_to("releases/release-previous")
    stage = _staged_runtime(installer)
    prepared = installer._prepare_setup()
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    installer._record_transaction(
        transaction,
        phase="runtime_switching",
        staged=stage,
        stable_entrypoint=stable,
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=_package_tree_digest(stage),
    )
    _bind_transaction_marker(stage, transaction)
    installer.current_runtime.unlink()
    installer.current_runtime.symlink_to("releases/release-test")
    stable.unlink()
    stable.symlink_to(installer.current_runtime / "bin/cross-agent-chat")
    monkeypatch.setattr(installer, "_bootout", lambda: None)

    installer._recover_unfinished_transaction()

    assert os.readlink(installer.current_runtime) == "releases/release-previous"
    assert stable.read_text() == "predecessor"
    assert not stage.exists()


def test_committing_recovery_conservatively_rolls_back_committed_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    prepared = installer._prepare_setup()
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    digest = _package_tree_digest(stage)
    installer._record_transaction(
        transaction,
        phase="committing",
        staged=stage,
        stable_entrypoint=stable,
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=digest,
    )
    installer.current_runtime.symlink_to("releases/release-test")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(installer.current_runtime / "bin/cross-agent-chat")
    (stage / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)

    installer._recover_unfinished_transaction()

    assert not installer.current_runtime.exists()
    assert not stable.exists()
    assert not stage.exists()
    assert not transaction.exists()


def test_recovery_stops_service_effect_after_service_starting_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    stable.parent.mkdir(parents=True)
    stable.write_text("predecessor")
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    prepared = installer._prepare_setup()
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    installer._record_transaction(
        transaction,
        phase="service_starting",
        staged=stage,
        stable_entrypoint=stable,
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=_package_tree_digest(stage),
    )
    _bind_transaction_marker(stage, transaction)
    bootouts = 0

    def bootout() -> None:
        nonlocal bootouts
        bootouts += 1

    monkeypatch.setattr(installer, "_bootout", bootout)
    monkeypatch.setattr(installer, "_wait_for_broker_port_release", lambda: True)

    installer._recover_unfinished_transaction()

    assert bootouts == 1
    assert stable.read_text() == "predecessor"
    assert not stage.exists()


def test_remove_release_rejects_symlink_to_predecessor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")
    predecessor = installer.releases / "release-predecessor"
    predecessor.mkdir(parents=True)
    (predecessor / ".cross-agent-chat-release").write_text(
        "cross-agent-chat-runtime-v1:committed\n"
    )
    candidate = installer.releases / "release-candidate"
    candidate.symlink_to(predecessor, target_is_directory=True)

    with pytest.raises(SettingsError, match="unowned runtime"):
        installer._remove_release(candidate)

    assert predecessor.exists()
    assert candidate.is_symlink()


def test_recovery_rejects_symlinked_transaction_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")
    outside = tmp_path / "outside"
    victim = outside / ".preparing-victim"
    victim.mkdir(parents=True)
    (victim / "keep").write_text("unrelated")
    installer.transactions.parent.mkdir(parents=True)
    installer.transactions.symlink_to(outside, target_is_directory=True)

    with pytest.raises(SettingsError, match="runtime ownership is invalid"):
        installer._recover_unfinished_transaction()

    assert (victim / "keep").read_text() == "unrelated"


def test_recovery_rejects_symlinked_transaction_entry(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")
    outside = tmp_path / "outside-transaction"
    outside.mkdir()
    installer.transactions.mkdir(parents=True)
    (installer.transactions / "transaction").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SettingsError, match="transaction state is ambiguous"):
        installer._recover_unfinished_transaction()

    assert outside.exists()


def test_recovery_rejects_stable_entrypoint_parent_escape(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    stable.parent.mkdir(parents=True)
    stable.write_text("predecessor")
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)
    prepared = installer._prepare_setup()
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    installer._record_transaction(
        transaction,
        phase="prepared",
        staged=stage,
        stable_entrypoint=stable,
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=_package_tree_digest(stage),
    )
    _bind_transaction_marker(stage, transaction)
    metadata_path = transaction / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["stable_entrypoint"] = "../outside/cross-agent-chat"
    metadata_path.write_text(json.dumps(metadata))
    outside = tmp_path / "outside/cross-agent-chat"
    outside.parent.mkdir()
    outside.write_text("unrelated")

    with pytest.raises(SettingsError, match="transaction metadata is invalid"):
        installer._recover_unfinished_transaction()

    assert outside.read_text() == "unrelated"


def test_interrupted_transaction_restores_pointer_entrypoint_and_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "configured-bin" / "cross-agent-chat"
    stable.parent.mkdir(parents=True)
    stable.write_text("predecessor-entrypoint")
    installer = Installer(home=home, executable=stable, device="studio")
    previous = installer.releases / "release-previous"
    (previous / "bin").mkdir(parents=True)
    (previous / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    installer.current_runtime.symlink_to("releases/release-previous")
    stage = _staged_runtime(installer)
    current_snapshot = _snapshot_path(installer.current_runtime)
    entrypoint_snapshot = _snapshot_path(stable)
    prepared = installer._prepare_setup()
    digest = _package_tree_digest(stage)
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True, mode=0o700)
    transaction.chmod(0o700)
    installer._record_transaction(
        transaction,
        phase="config_written",
        staged=stage,
        stable_entrypoint=stable,
        current_snapshot=current_snapshot,
        entrypoint_snapshot=entrypoint_snapshot,
        config_backup=prepared.backup,
        previous_broker_loaded=False,
        previous_broker_healthy=False,
        package_tree_sha256=digest,
    )
    _bind_transaction_marker(stage, transaction)
    installer.current_runtime.unlink()
    installer.current_runtime.symlink_to("releases/release-test")
    stable.unlink()
    stable.symlink_to(installer.current_runtime / "bin" / "cross-agent-chat")
    installer.setup(prepared=prepared)
    assert str(stable) in installer.claude_config.read_text()
    monkeypatch.setattr(installer, "_bootout", lambda: None)

    installer._recover_unfinished_transaction()

    assert os.readlink(installer.current_runtime) == "releases/release-previous"
    assert stable.read_text() == "predecessor-entrypoint"
    assert not installer.claude_config.exists()
    assert not stage.exists()
    assert not transaction.exists()


def test_setup_retains_backup_and_reports_compound_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "light"}))
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    prepared = installer._prepare_setup()
    writes = 0

    def fail_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            path.write_bytes(payload)
            return
        if writes == 2:
            raise OSError("candidate config write failed")
        raise OSError("provider rollback write failed")

    monkeypatch.setattr("cross_agent_chat.install._atomic_write", fail_write)

    with pytest.raises(SetupRollbackError) as captured:
        installer.setup(prepared=prepared)

    assert "candidate config write failed" in str(captured.value)
    assert "provider rollback write failed" in str(captured.value)
    assert captured.value.backup == prepared.backup
    assert prepared.backup.exists()


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
    monkeypatch.setattr(installer, "_wait_for_previous_broker_health", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", fail_shutdown)
    monkeypatch.setattr(installer, "activate", activate)

    with pytest.raises(
        SettingsError,
        match="transition failed but rollback succeeded: courier shutdown failed",
    ):
        installer.install_staged(stage, stable)

    assert stable.resolve() == predecessor
    assert predecessor.read_text() == "predecessor"
    assert not stage.exists()
    assert activations == 0


def test_staged_install_does_not_restore_config_before_config_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    stable.parent.mkdir(parents=True)
    stable.write_text("predecessor")
    settings = home / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"generation": "preflight"}))
    installer = Installer(home=home, executable=stable, device="studio")
    stage = _staged_runtime(installer)

    def fail_shutdown() -> None:
        settings.write_text(json.dumps({"generation": "newer"}))
        raise SettingsError("courier shutdown failed")

    monkeypatch.setattr(installer, "_validate_staged_runtime", lambda _: stage.resolve())
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", fail_shutdown)

    with pytest.raises(SettingsError, match="transition failed but rollback succeeded"):
        installer.install_staged(stage, stable)

    assert json.loads(settings.read_text()) == {"generation": "newer"}


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
    monkeypatch.setattr(installer, "_wait_for_previous_broker_health", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)
    monkeypatch.setattr(installer, "verify", lambda **_kwargs: False)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    with pytest.raises(SettingsError, match="background broker did not become healthy"):
        installer.install_staged(stage, stable)

    assert activations == 2
    assert stable.resolve() == predecessor
    assert predecessor.read_text() == "predecessor"


def test_rollback_restores_loaded_unhealthy_predecessor_without_health_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    candidate = _staged_runtime(installer)
    transaction = installer.transactions / "retained"
    transaction.mkdir(parents=True)
    activations = 0

    def activate() -> None:
        nonlocal activations
        activations += 1

    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(
        installer,
        "_wait_for_previous_broker_health",
        lambda: pytest.fail("previously unhealthy broker was promoted to a health requirement"),
    )

    installer._rollback_transition(
        transaction_path=transaction,
        config_backup=home / "unused-backup",
        current_snapshot=_snapshot_path(installer.current_runtime),
        entrypoint_snapshot=_snapshot_path(stable),
        candidate=candidate,
        runtime_transition_started=False,
        config_transition_started=False,
        service_transition_started=True,
        previous_broker_loaded=True,
        previous_broker_healthy=False,
    )

    assert activations == 1
    assert not candidate.exists()
    assert not transaction.exists()


def test_rollback_rejects_unloaded_previously_loaded_unhealthy_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")
    candidate = _staged_runtime(installer)
    transaction = installer.transactions / "retained"
    transaction.mkdir(parents=True)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "broker_is_loaded", lambda: False)

    with pytest.raises(SettingsError, match="previous broker did not become loaded"):
        installer._rollback_transition(
            transaction_path=transaction,
            config_backup=home / "unused-backup",
            current_snapshot=_snapshot_path(installer.current_runtime),
            entrypoint_snapshot=_snapshot_path(home / "bin/cross-agent-chat"),
            candidate=candidate,
            runtime_transition_started=False,
            config_transition_started=False,
            service_transition_started=True,
            previous_broker_loaded=True,
            previous_broker_healthy=False,
        )

    assert candidate.exists()
    assert transaction.exists()


def test_rollback_durably_drops_journal_before_candidate_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")
    candidate = _staged_runtime(installer)
    transaction = installer.transactions / ("a" * 32)
    transaction.mkdir(parents=True)
    _bind_transaction_marker(candidate, transaction)
    original_remove = installer._remove_release
    monkeypatch.setattr(
        installer,
        "_remove_release",
        lambda _candidate: (_ for _ in ()).throw(OSError("interrupted cleanup")),
    )

    with pytest.raises(OSError, match="interrupted cleanup"):
        installer._rollback_transition(
            transaction_path=transaction,
            config_backup=home / "unused-backup",
            current_snapshot=_snapshot_path(installer.current_runtime),
            entrypoint_snapshot=_snapshot_path(home / "bin/cross-agent-chat"),
            candidate=candidate,
            runtime_transition_started=False,
            config_transition_started=False,
            service_transition_started=False,
            previous_broker_loaded=False,
            previous_broker_healthy=False,
        )

    assert not transaction.exists()
    assert candidate.exists()
    monkeypatch.setattr(installer, "_remove_release", original_remove)
    installer._prune_abandoned_staged_releases()
    assert not candidate.exists()


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
    monkeypatch.setattr(installer, "_wait_for_previous_broker_health", lambda: True)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", activate)
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)

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

    def health(*, deadline: float) -> bool:
        del deadline
        nonlocal health_checks
        health_checks += 1
        return health_checks > 1

    monkeypatch.setattr(installer, "broker_is_loaded", lambda: True)
    monkeypatch.setattr(installer, "_previous_broker_is_healthy", health)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)
    monkeypatch.setattr(installer, "activate", lambda: None)
    monkeypatch.setattr(installer, "verify", lambda **_kwargs: True)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", lambda _: None)

    installer.install()

    assert health_checks == 2


def test_install_allows_bounded_launchd_throttle_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    checks = 0

    def verify(**_kwargs: float) -> bool:
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

    def verify(**_kwargs: float) -> bool:
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
    monkeypatch.setattr(installer, "verify", lambda **_kwargs: True)

    assert installer.install() == report
    assert calls == ["stop", "remove", "activate"]


def test_uninstall_removes_only_owned_background_surfaces(
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
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)

    installer.uninstall()

    claude = json.loads((home / ".claude.json").read_text())
    assert "cross-agent-chat" not in claude.get("mcpServers", {})
    assert not installer.launch_agent.exists()
    assert [
        "launchctl",
        "bootout",
        f"gui/{__import__('os').getuid()}/io.github.kwonsyup.cross-agent-chat",
    ] in calls


def test_uninstall_preserves_shared_claude_cross_session_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"crossSessionInbound": "accept"}))
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()

    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    installer.uninstall()

    assert json.loads(settings.read_text())["crossSessionInbound"] == "accept"


def test_uninstall_preserves_in_home_provider_configuration_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    shared = home / "shared-config"
    shared.mkdir(parents=True)
    provider_paths = (
        installer.claude_settings,
        installer.claude_config,
        installer.codex_config,
        installer.codex_hooks,
        installer.launch_agent,
    )
    targets: dict[Path, Path] = {}
    for index, path in enumerate(provider_paths):
        target = shared / f"provider-{index}"
        target.write_text("" if path == installer.codex_config else "{}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        targets[path] = target
    installer.setup()
    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    monkeypatch.setattr(installer, "_stop_couriers", lambda: None)
    monkeypatch.setattr(installer, "_remove_runtime_state", lambda: None)

    installer.uninstall()

    assert all(path.is_symlink() for path in provider_paths)
    assert not targets[installer.launch_agent].exists()
    assert json.loads(targets[installer.claude_config].read_text()).get("mcpServers", {}) == {}
    hooks = json.loads(targets[installer.codex_hooks].read_text()).get("hooks", {})
    assert all(not _owned_hook(group) for groups in hooks.values() for group in groups)

    installer.setup()

    assert all(path.is_symlink() for path in provider_paths)
    assert all(target.exists() for target in targets.values())


def test_uninstall_restores_absent_claude_cross_session_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()

    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    installer.uninstall()

    assert "crossSessionInbound" not in json.loads(installer.claude_settings.read_text())


def test_uninstall_restores_prior_claude_cross_session_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"crossSessionInbound": "prompt"}))
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()

    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    installer.uninstall()

    assert json.loads(settings.read_text())["crossSessionInbound"] == "prompt"


def test_verify_requires_loaded_responsive_background_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    runtime = home / "runtime"
    executable = runtime / "bin" / "cross-agent-chat"
    module = runtime / "lib" / "cross_agent_chat" / "tailnet_broker.py"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate")
    module.parent.mkdir(parents=True)
    module.write_text("candidate")
    installer = Installer(home=home, executable=executable, device="studio")
    installer.setup()

    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(command, 113, "", "not found"),
    )
    assert not installer.verify()

    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(
            command,
            0,
            f"state = running\nprogram = {executable}\npid = 4242\n",
            "",
        ),
    )
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "status": "READY",
            "pid": 4242,
            "version": "0.1.2",
            "module_path": str(module),
        },
    )
    assert installer.verify()


def test_broker_health_rejects_spawn_scheduled_job_with_orphan_ready_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    executable = home / "runtime" / "bin" / "cross-agent-chat"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate")
    installer = Installer(home=home, executable=executable, device="studio")
    monkeypatch.setattr(
        "cross_agent_chat.install.subprocess.run",
        lambda command, **_: subprocess.CompletedProcess(
            command,
            0,
            f"state = spawn scheduled\nprogram = {executable}\nlast exit code = 2\n",
            "",
        ),
    )
    requested = False

    def ready(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal requested
        requested = True
        return {"schema_version": 1, "status": "READY"}

    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", ready)

    assert not installer.broker_is_healthy()
    assert not requested


def test_previous_broker_health_accepts_legacy_response_only_from_launchd_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / ".local" / "bin" / "cross-agent-chat"
    legacy = home / "legacy-runtime" / "bin" / "cross-agent-chat"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy")
    installer = Installer(home=home, executable=stable, device="studio")

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(command, 0, "4242\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            f"state = running\nprogram = {legacy}\npid = 4242\n",
            "",
        )

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda *_args, **_kwargs: {"schema_version": 1, "status": "READY"},
    )

    assert not installer.broker_is_healthy()
    assert installer._previous_broker_is_healthy()


def test_previous_broker_health_accepts_same_program_legacy_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "legacy-runtime" / "bin" / "cross-agent-chat"
    executable.parent.mkdir(parents=True)
    executable.write_text("legacy")
    installer = Installer(home=tmp_path / "home", executable=executable, device="studio")

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(command, 0, "4242\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            f"state = running\nprogram = {executable}\npid = 4242\n",
            "",
        )

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda *_args, **_kwargs: {"schema_version": 1, "status": "READY"},
    )

    assert installer._previous_broker_is_healthy()


def test_previous_broker_health_binds_rich_version_to_predecessor_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "previous-runtime"
    executable = runtime / "bin/cross-agent-chat"
    module = runtime / "lib/cross_agent_chat/tailnet_broker.py"
    executable.parent.mkdir(parents=True)
    executable.write_text("previous")
    module.parent.mkdir(parents=True)
    module.write_text("previous")
    installer = Installer(home=tmp_path / "home", executable=executable, device="studio")
    response_version = "0.1.1"

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "/usr/sbin/lsof":
            return subprocess.CompletedProcess(command, 0, "4242\n", "")
        if command == [str(executable), "--version"]:
            return subprocess.CompletedProcess(command, 0, "cross-agent-chat 0.1.1\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            f"state = running\nprogram = {executable}\npid = 4242\n",
            "",
        )

    def request(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "READY",
            "pid": 4242,
            "version": response_version,
            "module_path": str(module),
        }

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)

    assert installer._previous_broker_is_healthy()
    response_version = "0.1.0"
    assert not installer._previous_broker_is_healthy()


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


def test_broker_health_uses_bounded_ten_second_local_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    executable = home / "runtime/bin/cross-agent-chat"
    module = home / "runtime/lib/cross_agent_chat/tailnet_broker.py"
    executable.parent.mkdir(parents=True)
    executable.write_text("candidate")
    module.parent.mkdir(parents=True)
    module.write_text("candidate")
    installer = Installer(home=home, executable=executable, device="studio")
    timeouts: list[float] = []
    monkeypatch.setattr(
        installer,
        "_broker_service",
        lambda **_kwargs: BrokerService(pid=4242, program=executable),
    )

    def request(*_args: object, **kwargs: object) -> dict[str, object]:
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, float)
        timeouts.append(timeout)
        return {
            "schema_version": 1,
            "status": "READY",
            "pid": 4242,
            "version": "0.1.2",
            "module_path": str(module),
        }

    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)

    assert installer.broker_is_healthy()
    assert len(timeouts) == 1
    assert 9.0 < timeouts[0] <= BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "status": "READY",
            "pid": 4242,
            "version": "9.9.9",
            "module_path": str(module),
        },
    )
    assert not installer.broker_is_healthy()


def test_candidate_health_wait_has_one_monotonic_total_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = Installer(
        home=tmp_path / "home", executable=Path("/opt/cross-agent-chat"), device="studio"
    )
    now = 0.0
    timeouts: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(duration: float) -> None:
        nonlocal now
        now += duration

    def unavailable(timeout: float) -> bool:
        nonlocal now
        timeouts.append(timeout)
        now += timeout
        return False

    monkeypatch.setattr("cross_agent_chat.install.time.monotonic", monotonic)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", sleep)

    assert not installer._wait_for_broker_health(unavailable)
    assert now == pytest.approx(BROKER_HEALTH_WAIT_SECONDS)
    assert timeouts
    assert all(0 < timeout <= BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS for timeout in timeouts)


def test_predecessor_health_wait_uses_one_request_per_iteration_and_total_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "legacy/bin/cross-agent-chat"
    installer = Installer(home=tmp_path / "home", executable=executable, device="studio")
    now = 0.0
    timeouts: list[float] = []
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(duration: float) -> None:
        nonlocal now
        sleeps.append(duration)
        now += duration

    def request(*_args: object, **kwargs: object) -> dict[str, object]:
        nonlocal now
        timeout = kwargs.get("timeout")
        assert isinstance(timeout, float)
        timeouts.append(timeout)
        now += timeout
        return {"schema_version": 1, "status": "UNAVAILABLE"}

    monkeypatch.setattr("cross_agent_chat.install.time.monotonic", monotonic)
    monkeypatch.setattr("cross_agent_chat.install.time.sleep", sleep)
    monkeypatch.setattr(
        installer,
        "_broker_service",
        lambda **_kwargs: BrokerService(pid=4242, program=tmp_path / "previous/bin/chat"),
    )
    monkeypatch.setattr(installer, "_local_broker_listener_pid", lambda **_kwargs: 4242)
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)

    assert not installer._wait_for_previous_broker_health()
    assert now == pytest.approx(BROKER_HEALTH_WAIT_SECONDS)
    assert len(timeouts) == len(sleeps) + 1
    assert all(0 < timeout <= BROKER_HEALTH_REQUEST_TIMEOUT_SECONDS for timeout in timeouts)


def test_uninstall_removes_owned_runtime_state_and_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(home=home, executable=Path("/opt/cross-agent-chat"), device="studio")
    installer.setup()
    installer.state.mkdir(parents=True, mode=0o700)
    installer.state.chmod(0o700)
    (installer.state / "marker").write_text("owned")

    monkeypatch.setattr(installer, "_stop_broker", lambda: None)
    installer.uninstall()

    assert not installer.state.exists()
    assert not installer.cache.exists()
    assert not installer.install_state.exists()


def test_uninstall_recovery_failure_precedes_all_configuration_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    installer.setup()
    before = {path: path.read_bytes() for path in installer.config_paths if path.exists()}
    monkeypatch.setattr(
        installer,
        "_recover_unfinished_transaction",
        lambda: (_ for _ in ()).throw(SettingsError("recovery failed")),
    )

    with pytest.raises(SettingsError, match="recovery failed"):
        installer.uninstall()

    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("residue", ["current-file", "committed-release"])
def test_uninstall_rejects_malformed_runtime_before_configuration_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, residue: str
) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    installer.setup()
    if residue == "current-file":
        installer.current_runtime.parent.mkdir(parents=True, exist_ok=True)
        installer.current_runtime.write_text("malformed")
    else:
        release = installer.releases / "release-residue"
        release.mkdir(parents=True)
        (release / ".cross-agent-chat-release").write_text(
            "cross-agent-chat-runtime-v1:committed\n"
        )
    before = {path: path.read_bytes() for path in installer.config_paths if path.exists()}
    monkeypatch.setattr(
        installer,
        "_stop_broker",
        lambda: pytest.fail("uninstall effect ran before runtime ownership preflight"),
    )

    with pytest.raises(SettingsError, match="installed runtime ownership is invalid"):
        installer.uninstall()

    assert {path: path.read_bytes() for path in before} == before


def test_runtime_removal_preflight_prunes_abandoned_staging(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = Installer(
        home=home, executable=home / ".local/bin/cross-agent-chat", device="studio"
    )
    staging = installer.releases / "release-abandoned"
    staging.mkdir(parents=True)
    (staging / ".cross-agent-chat-release").write_text(
        "cross-agent-chat-runtime-v1:staged:999999:" + "0" * 64 + "\n"
    )

    assert installer._prepare_runtime_removal(None) is None
    assert not staging.exists()


def test_uninstall_through_release_binary_removes_persisted_public_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / ".local/bin/cross-agent-chat"
    release = home / ".local/share/cross-agent-chat-runtime/releases/release-current"
    candidate = release / "bin/cross-agent-chat"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("candidate")
    (release / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    current = home / ".local/share/cross-agent-chat-runtime/current"
    current.symlink_to("releases/release-current")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(current / "bin/cross-agent-chat")
    Installer(home=home, executable=stable, device="studio").setup()
    direct = Installer(home=home, executable=candidate, device="studio")
    monkeypatch.setattr(direct, "_stop_broker", lambda: None)
    monkeypatch.setattr(direct, "_stop_couriers", lambda: None)

    direct.uninstall()

    assert not stable.exists()
    assert not current.exists()
    assert not release.exists()


def test_uninstall_ignores_unrelated_broken_default_symlink_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "custom-bin/cross-agent-chat"
    release = home / ".local/share/cross-agent-chat-runtime/releases/release-current"
    candidate = release / "bin/cross-agent-chat"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("candidate")
    (release / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    current = home / ".local/share/cross-agent-chat-runtime/current"
    current.symlink_to("releases/release-current")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(current / "bin/cross-agent-chat")
    installed = Installer(home=home, executable=stable, device="studio")
    installed.setup()
    default = home / ".local/bin/cross-agent-chat"
    default.parent.mkdir(parents=True)
    default.symlink_to(home / "missing-unrelated")
    monkeypatch.setattr(installed, "_stop_broker", lambda: None)
    monkeypatch.setattr(installed, "_stop_couriers", lambda: None)

    installed.uninstall()

    assert default.is_symlink()
    assert not default.exists()
    assert not stable.exists()
    assert not current.exists()
    assert not release.exists()


def test_runtime_removal_revalidates_entrypoint_before_unlink(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    stable = home / "custom-bin/cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    release = installer.releases / "release-current"
    candidate = release / "bin/cross-agent-chat"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("candidate")
    (release / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    installer.current_runtime.symlink_to("releases/release-current")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(installer.current_runtime / "bin/cross-agent-chat")
    plan = installer._prepare_runtime_removal(stable)
    assert plan is not None
    replacement = home / "replacement"
    replacement.write_text("unrelated")
    stable.unlink()
    stable.symlink_to(replacement)

    with pytest.raises(SettingsError, match="ownership changed"):
        installer._remove_installed_runtime(plan)

    assert stable.is_symlink()
    assert stable.resolve() == replacement
    assert replacement.read_text() == "unrelated"
    assert installer.current_runtime.resolve() == release.resolve()
    assert release.exists()


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
    (release / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    unrelated = installer.releases / "release-unrelated"
    unrelated.mkdir()
    (unrelated / "keep").write_text("unrelated")
    staging = installer.releases / "release-in-progress"
    staging.mkdir()
    identity = _process_identity_digest(os.getpid())
    assert identity is not None
    (staging / ".cross-agent-chat-release").write_text(
        f"cross-agent-chat-runtime-v1:staged:{os.getpid()}:{identity}\n"
    )
    abandoned = installer.releases / "release-abandoned"
    abandoned.mkdir()
    (abandoned / ".cross-agent-chat-release").write_text(
        "cross-agent-chat-runtime-v1:staged:999999999:" + "0" * 64 + "\n"
    )
    reused = installer.releases / "release-reused-pid"
    reused.mkdir()
    (reused / ".cross-agent-chat-release").write_text(
        f"cross-agent-chat-runtime-v1:staged:{os.getpid()}:" + "0" * 64 + "\n"
    )
    installer.current_runtime.symlink_to("releases/release-current")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(installer.current_runtime / "bin" / "cross-agent-chat")
    installer.setup()
    stopped = False

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal stopped
        if command[:2] == ["launchctl", "bootout"]:
            stopped = True
        if command[:2] == ["launchctl", "print"] and stopped:
            return subprocess.CompletedProcess(command, 113, "", "not found")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("cross_agent_chat.install.subprocess.run", run)
    monkeypatch.setattr(installer, "_broker_port_is_available", lambda: True)
    monkeypatch.setattr(
        "cross_agent_chat.install._process_identity_digest",
        lambda pid: identity if pid == os.getpid() else None,
    )

    installer.uninstall()

    assert not stable.exists()
    assert not installer.current_runtime.exists()
    assert not release.exists()
    assert (unrelated / "keep").read_text() == "unrelated"
    assert (staging / ".cross-agent-chat-release").read_text() == (
        f"cross-agent-chat-runtime-v1:staged:{os.getpid()}:{identity}\n"
    )
    assert not abandoned.exists()
    assert not reused.exists()
    assert legacy.read_text() == "legacy"


def test_uninstall_retains_runtime_when_broker_port_cannot_be_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    stable = home / "bin/cross-agent-chat"
    installer = Installer(home=home, executable=stable, device="studio")
    release = installer.releases / "release-current"
    candidate = release / "bin/cross-agent-chat"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("candidate")
    (release / ".cross-agent-chat-release").write_text("cross-agent-chat-runtime-v1:committed\n")
    installer.current_runtime.symlink_to("releases/release-current")
    stable.parent.mkdir(parents=True)
    stable.symlink_to(installer.current_runtime / "bin/cross-agent-chat")
    installer.setup()
    monkeypatch.setattr(
        installer,
        "_stop_broker",
        lambda: (_ for _ in ()).throw(SettingsError("broker port remained occupied")),
    )

    with pytest.raises(SettingsError, match="broker port remained occupied"):
        installer.uninstall()

    assert stable.is_symlink()
    assert installer.current_runtime.is_symlink()
    assert release.exists()


def test_discover_executable_preserves_stable_symlink_identity(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime" / "bin" / "cross-agent-chat"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("candidate")
    stable = tmp_path / "bin" / "cross-agent-chat"
    stable.parent.mkdir()
    stable.symlink_to(runtime)

    assert discover_executable(stable) == stable.absolute()


def test_install_lock_serializes_concurrent_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    first = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")
    second = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")
    active = 0
    maximum_active = 0
    guard = threading.Lock()

    def transaction(_stage: Path, _stable: Path) -> InstallReport:
        nonlocal active, maximum_active
        with guard:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return InstallReport(changed_paths=(), backup=tmp_path / "backup")

    monkeypatch.setattr(first, "_install_staged", transaction)
    monkeypatch.setattr(second, "_install_staged", transaction)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(
                lambda installer: installer.install_staged(
                    tmp_path / "stage", home / "bin/cross-agent-chat"
                ),
                (first, second),
            )
        )

    assert len(results) == 2
    assert maximum_active == 1


def test_install_lock_rejects_symlinked_share_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".local").mkdir(parents=True)
    (home / ".local" / "share").symlink_to(outside, target_is_directory=True)
    installer = Installer(home=home, executable=home / "bin/cross-agent-chat", device="studio")

    with (
        pytest.raises(SettingsError, match="lock ownership is invalid"),
        installer._exclusive_lock(),
    ):
        pytest.fail("unsafe lock acquired")

    assert not list(outside.iterdir())
