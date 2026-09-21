"""Run the bounded, no-secret G03 macOS service lifecycle probe."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping, Sequence


REPOSITORY: Final = "https://github.com/kwonsyup/cross-agent-chat.git"
RELEASE_TAG: Final = "v0.4.0"
RELEASE_COMMIT: Final = "d237d53bebb6abc43b96179af3f9e26e38c59756"
INSTALLER_URL: Final = (
    "https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/"
    f"{RELEASE_COMMIT}/install.sh"
)
BROKER_LABEL: Final = "io.github.kwonsyup.cross-agent-chat"
BROKER_PORT: Final = 47072
SERVER_NAME: Final = "cross-agent-chat"
COMMAND_TIMEOUT: Final = 600.0


class ProbeFailure(RuntimeError):
    """A lifecycle precondition or assertion failed."""


@dataclass(frozen=True, slots=True)
class Profile:
    """One synthetic Claude profile sharing the synthetic Codex root."""

    name: str
    claude_root: Path


def run_command(
    command: Sequence[str],
    env: Mapping[str, str],
    *,
    timeout: float = COMMAND_TIMEOUT,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded command without a shell or inherited secret environment."""
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProbeFailure(f"command failed to start: {list(command)}: {error}") from error
    if check and completed.returncode != 0:
        output = (completed.stdout + completed.stderr).strip()
        raise ProbeFailure(
            f"command exited {completed.returncode}: {list(command)}\n{output[-4000:]}"
        )
    return completed


def base_environment() -> dict[str, str]:
    """Keep only non-secret process settings needed by the public installer."""
    source = os.environ
    environment: dict[str, str] = {}
    for name in ("PATH", "LANG", "LC_ALL", "TMPDIR"):
        value = source.get(name)
        if value is not None:
            environment[name] = value
    environment.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def assert_macos() -> None:
    if platform.system() != "Darwin":
        raise ProbeFailure("G03 requires a macOS runner")


def launchctl_result(environment: Mapping[str, str], target: str) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["launchctl", "print", target],
        environment,
        timeout=15.0,
        check=False,
    )


def listener_pids(environment: Mapping[str, str]) -> list[str]:
    result = run_command(
        ["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{BROKER_PORT}", "-sTCP:LISTEN"],
        environment,
        timeout=15.0,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise ProbeFailure(f"lsof failed: {result.stderr.strip()[-2000:]}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def assert_preexisting_service_absent(environment: Mapping[str, str]) -> None:
    service = f"gui/{os.getuid()}/{BROKER_LABEL}"
    if launchctl_result(environment, service).returncode == 0:
        raise ProbeFailure(f"pre-existing CAC service is loaded: {service}")
    pids = listener_pids(environment)
    if pids:
        raise ProbeFailure(f"pre-existing listener owns port {BROKER_PORT}: {pids}")


def gui_domain_available(environment: Mapping[str, str]) -> bool:
    return launchctl_result(environment, f"gui/{os.getuid()}").returncode == 0


def fetch_immutable_installer(destination: Path, environment: Mapping[str, str]) -> None:
    tag = run_command(
        ["git", "ls-remote", REPOSITORY, f"refs/tags/{RELEASE_TAG}^{{}}"],
        environment,
        timeout=30.0,
    ).stdout.strip().split()
    if not tag or tag[0] != RELEASE_COMMIT:
        raise ProbeFailure(f"{RELEASE_TAG} does not resolve to {RELEASE_COMMIT}: {tag}")
    payload = run_command(
        ["curl", "-fsSL", INSTALLER_URL],
        environment,
        timeout=30.0,
    ).stdout.encode()
    if not payload.startswith(b"#!/bin/sh"):
        raise ProbeFailure("immutable installer did not have the expected shell entrypoint")
    destination.write_bytes(payload)
    destination.chmod(0o700)


def profile_environment(
    base: Mapping[str, str], home: Path, codex_root: Path, profile: Profile
) -> dict[str, str]:
    environment = dict(base)
    environment.update(
        {
            "HOME": str(home),
            "CODEX_HOME": str(codex_root),
            "CLAUDE_CONFIG_DIR": str(profile.claude_root),
        }
    )
    return environment


def write_json(path: Path, value: object) -> bytes:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(payload)
    path.chmod(0o600)
    return payload


def seed_synthetic_roots(home: Path, codex_root: Path, profiles: Sequence[Profile]) -> None:
    """Create valid, content-free provider files with foreign values."""
    codex_root.mkdir(parents=True, mode=0o700)
    codex_config = b'[features]\nhooks = false\n\n[foreign]\nsentinel = "keep"\n'
    codex_config_path = codex_root / "config.toml"
    codex_config_path.write_bytes(codex_config)
    codex_config_path.chmod(0o600)
    write_json(
        codex_root / "hooks.json",
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}
                ]
            }
        },
    )
    for profile in profiles:
        write_json(
            profile.claude_root / "settings.json",
            {
                "crossSessionInbound": "hold",
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}
                    ]
                },
            },
        )
        write_json(
            profile.claude_root / ".claude.json",
            {
                "mcpServers": {
                    f"foreign-{profile.name}": {"command": "/usr/bin/true", "args": []}
                }
            },
        )
    home.mkdir(parents=True, mode=0o700, exist_ok=True)


def install_profile(
    base: Mapping[str, str],
    home: Path,
    codex_root: Path,
    profile: Profile,
    installer: Path,
) -> None:
    environment = profile_environment(base, home, codex_root, profile)
    environment.update(
        {
            "CROSS_AGENT_CHAT_APPROVE": "1",
            "CROSS_AGENT_CHAT_PROVIDERS": "claude,codex",
            "CROSS_AGENT_CHAT_SOURCE": f"git+{REPOSITORY}@{RELEASE_COMMIT}",
        }
    )
    run_command(["/bin/sh", str(installer)], environment)
    executable = home / ".local" / "bin" / SERVER_NAME
    if not executable.is_symlink() or not executable.exists():
        raise ProbeFailure(f"installed stable entrypoint is missing: {executable}")


def run_cli(
    base: Mapping[str, str],
    home: Path,
    codex_root: Path,
    profile: Profile,
    arguments: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    environment = profile_environment(base, home, codex_root, profile)
    executable = home / ".local" / "bin" / SERVER_NAME
    return run_command([str(executable), *arguments], environment)


def typed_json(text: str, label: str) -> dict[str, object]:
    try:
        decoded: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProbeFailure(f"{label} was not JSON: {error}") from error
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ProbeFailure(f"{label} was not a JSON object")
    return decoded


def assert_healthy(
    base: Mapping[str, str], home: Path, codex_root: Path, profile: Profile, label: str
) -> None:
    result = run_cli(base, home, codex_root, profile, ["doctor", "--json"])
    doctor = typed_json(result.stdout, f"{label} doctor")
    if doctor.get("integration") != "healthy" or doctor.get("local_broker") != "healthy":
        raise ProbeFailure(f"{label} doctor was not healthy: {json.dumps(doctor, sort_keys=True)}")
    plist = home / "Library" / "LaunchAgents" / f"{BROKER_LABEL}.plist"
    current = home / ".local" / "share" / f"{SERVER_NAME}-runtime" / "current"
    if not plist.is_file() or not current.is_symlink():
        raise ProbeFailure(f"{label} did not leave the expected launchd/runtime files")
    if len(listener_pids(base)) != 1:
        raise ProbeFailure(f"{label} did not leave exactly one broker listener")


def assert_unhealthy_removed(base: Mapping[str, str], home: Path) -> None:
    service = f"gui/{os.getuid()}/{BROKER_LABEL}"
    if launchctl_result(base, service).returncode == 0:
        raise ProbeFailure("CAC service remained loaded after final uninstall")
    if listener_pids(base):
        raise ProbeFailure("CAC broker listener remained after final uninstall")
    plist = home / "Library" / "LaunchAgents" / f"{BROKER_LABEL}.plist"
    runtime = home / ".local" / "share" / f"{SERVER_NAME}-runtime"
    if plist.exists() or runtime.exists():
        raise ProbeFailure("owned launchd/runtime files remained after final uninstall")


def parsed_json(path: Path) -> dict[str, object]:
    try:
        decoded: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeFailure(f"invalid synthetic JSON at {path}: {error}") from error
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise ProbeFailure(f"synthetic JSON object expected at {path}")
    return decoded


def assert_claude_prior_values(profiles: Sequence[Profile]) -> None:
    for profile in profiles:
        settings = parsed_json(profile.claude_root / "settings.json")
        claude = parsed_json(profile.claude_root / ".claude.json")
        if settings.get("crossSessionInbound") != "hold" or SERVER_NAME in json.dumps(settings):
            raise ProbeFailure(f"{profile.name} Claude prior settings were not restored")
        if f"foreign-{profile.name}" not in json.dumps(claude) or SERVER_NAME in json.dumps(claude):
            raise ProbeFailure(f"{profile.name} Claude foreign MCP entry was not restored")


def assert_prior_values(profiles: Sequence[Profile], codex_root: Path) -> None:
    codex_config = (codex_root / "config.toml").read_text()
    if '[features]\nhooks = false' not in codex_config or 'sentinel = "keep"' not in codex_config:
        raise ProbeFailure("foreign Codex TOML values were not restored")
    codex_hooks = parsed_json(codex_root / "hooks.json")
    if "/usr/bin/true" not in json.dumps(codex_hooks) or SERVER_NAME in json.dumps(codex_hooks):
        raise ProbeFailure("foreign or owned Codex hooks were not restored correctly")
    assert_claude_prior_values(profiles)


def assert_shared_owner_state(codex_root: Path) -> None:
    codex_config = (codex_root / "config.toml").read_text()
    codex_hooks = json.dumps(parsed_json(codex_root / "hooks.json"))
    if "cross-agent-chat" not in codex_config or "cross-agent-chat" not in codex_hooks:
        raise ProbeFailure("shared Codex integration was removed while another owner remained")
    if 'sentinel = "keep"' not in codex_config or "/usr/bin/true" not in codex_hooks:
        raise ProbeFailure("shared Codex foreign values were changed")


def create_durable_state(home: Path) -> tuple[Path, Path, bytes, int]:
    state = home / ".local" / "state" / SERVER_NAME
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    intents = state / "intents.json"
    lock = state / ".intents.lock"
    intent_bytes = b"[]\n"
    lock_bytes = b"g03 synthetic lock\n"
    intents.write_bytes(intent_bytes)
    lock.write_bytes(lock_bytes)
    intents.chmod(0o600)
    lock.chmod(0o600)
    return intents, lock, intent_bytes, lock.stat().st_ino


def assert_durable_state(intents: Path, lock: Path, intent_bytes: bytes, lock_inode: int) -> None:
    if intents.read_bytes() != intent_bytes:
        raise ProbeFailure("durable intents bytes changed")
    if lock.read_bytes() != b"g03 synthetic lock\n" or lock.stat().st_ino != lock_inode:
        raise ProbeFailure("durable intents lock bytes or inode changed")


def run_probe() -> int:
    assert_macos()
    base = base_environment()
    assert_preexisting_service_absent(base)
    if not gui_domain_available(base):
        print(
            json.dumps(
                {
                    "status": "PARTIAL_UNSUPPORTED",
                    "reason": "gui_domain_absent",
                    "service_lifecycle": "not_run",
                    "authenticated_collaboration": "not_tested",
                    "source": f"{RELEASE_TAG}@{RELEASE_COMMIT}",
                },
                sort_keys=True,
            )
        )
        return 0

    runner_temp_value = os.environ.get("RUNNER_TEMP")
    if runner_temp_value is None:
        raise ProbeFailure("RUNNER_TEMP is required for private lifecycle roots")
    runner_temp = Path(runner_temp_value).resolve()
    runner_temp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cac-g03-", dir=runner_temp) as raw_root:
        root = Path(raw_root)
        root.chmod(0o700)
        home = root / "home"
        codex_root = root / "codex-shared"
        profiles = (
            Profile("profile-a", home / "claude-a"),
            Profile("profile-b", home / "claude-b"),
        )
        seed_synthetic_roots(home, codex_root, profiles)
        installer = root / "install.sh"
        fetch_immutable_installer(installer, base)
        installed: list[Profile] = []
        intents, lock, intent_bytes, lock_inode = create_durable_state(home)
        try:
            install_profile(base, home, codex_root, profiles[0], installer)
            installed.append(profiles[0])
            assert_healthy(base, home, codex_root, profiles[0], "profile-a initial")

            install_profile(base, home, codex_root, profiles[1], installer)
            installed.append(profiles[1])
            assert_healthy(base, home, codex_root, profiles[1], "profile-b shared")
            assert_shared_owner_state(codex_root)
            assert_durable_state(intents, lock, intent_bytes, lock_inode)

            run_cli(
                base,
                home,
                codex_root,
                profiles[1],
                ["setup", "--provider", "claude", "--provider", "codex", "--yes"],
            )
            assert_healthy(base, home, codex_root, profiles[1], "profile-b reinstall")
            assert_durable_state(intents, lock, intent_bytes, lock_inode)

            run_cli(base, home, codex_root, profiles[0], ["uninstall"])
            installed.remove(profiles[0])
            assert_healthy(base, home, codex_root, profiles[1], "profile-b after shared uninstall")
            assert_shared_owner_state(codex_root)
            assert_claude_prior_values((profiles[0],))
            assert_durable_state(intents, lock, intent_bytes, lock_inode)

            run_cli(base, home, codex_root, profiles[1], ["uninstall"])
            installed.remove(profiles[1])
            assert_unhealthy_removed(base, home)
            assert_durable_state(intents, lock, intent_bytes, lock_inode)
            assert_prior_values(profiles, codex_root)

            install_profile(base, home, codex_root, profiles[1], installer)
            installed.append(profiles[1])
            assert_healthy(base, home, codex_root, profiles[1], "profile-b canonical reinstall")
            assert_durable_state(intents, lock, intent_bytes, lock_inode)
            run_cli(base, home, codex_root, profiles[1], ["uninstall"])
            installed.remove(profiles[1])
            assert_unhealthy_removed(base, home)
            assert_durable_state(intents, lock, intent_bytes, lock_inode)
            assert_prior_values(profiles, codex_root)
        finally:
            for profile in reversed(installed):
                try:
                    run_cli(base, home, codex_root, profile, ["uninstall"])
                except ProbeFailure:
                    pass
    print(
        json.dumps(
            {
                "status": "PASS_PARTIAL",
                "service_lifecycle": "passed",
                "authenticated_collaboration": "not_tested",
                "gui_domain": "available",
                "source": f"{RELEASE_TAG}@{RELEASE_COMMIT}",
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    try:
        return run_probe()
    except ProbeFailure as error:
        print(f"G03 lifecycle probe failed: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
