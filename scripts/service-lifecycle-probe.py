"""Run the bounded, no-secret G03 macOS service lifecycle probe."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPOSITORY: Final = "https://github.com/kwonsyup/cross-agent-chat.git"
SOURCE_LABEL: Final = "v0.4.1"
SOURCE_REF: Final = "refs/tags/v0.4.1^{}"
SOURCE_COMMIT: Final = "5f0f46300468dbf776151a435719b544f021e387"
INSTALLER_URL: Final = (
    f"https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/{SOURCE_COMMIT}/install.sh"
)
BROKER_LABEL: Final = "io.github.kwonsyup.cross-agent-chat"
BROKER_PORT: Final = 47072
SERVER_NAME: Final = "cross-agent-chat"
COMMAND_TIMEOUT: Final = 600.0


class ProbeFailure(RuntimeError):
    """A lifecycle precondition or assertion failed."""


class UnsupportedEnvironment(ProbeFailure):
    """The hosted runner cannot provide the real GUI launchd surface."""


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


def assert_github_hosted() -> None:
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        raise ProbeFailure("G03 must run inside GitHub Actions")
    if os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise ProbeFailure("G03 requires a github-hosted runner")


def runner_facts(environment: Mapping[str, str]) -> dict[str, object]:
    product = run_command(["sw_vers", "-productVersion"], environment, timeout=15.0).stdout.strip()
    build = run_command(["sw_vers", "-buildVersion"], environment, timeout=15.0).stdout.strip()
    return {
        "image_os": os.environ.get("ImageOS"),  # noqa: SIM112 - GitHub-defined spelling.
        "image_version": os.environ.get("ImageVersion"),  # noqa: SIM112 - GitHub-defined spelling.
        "macos_product": product,
        "macos_build": build,
        "uid": os.getuid(),
    }


def launchctl_result(
    environment: Mapping[str, str], target: str
) -> subprocess.CompletedProcess[str]:
    return run_command(
        ["launchctl", "print", target],
        environment,
        timeout=15.0,
        check=False,
    )


def is_known_launchctl_not_found(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return any(
        marker in output
        for marker in ("could not find service", "service not found", "no such process")
    )


def require_gui_domain(environment: Mapping[str, str]) -> None:
    result = launchctl_result(environment, f"gui/{os.getuid()}")
    if result.returncode == 0:
        return
    if is_known_launchctl_not_found(result):
        raise UnsupportedEnvironment("the user GUI launchd domain is absent")
    raise ProbeFailure(
        "launchctl GUI-domain probe failed with an unknown error: "
        f"{(result.stdout + result.stderr).strip()[-2000:]}"
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


def running_service(
    environment: Mapping[str, str],
) -> tuple[int, Path]:
    service = f"gui/{os.getuid()}/{BROKER_LABEL}"
    result = launchctl_result(environment, service)
    if result.returncode != 0:
        raise ProbeFailure(f"CAC service is not running: {service}")
    state = re.search(r"^\s*state = (\S+)\s*$", result.stdout, re.MULTILINE)
    pid = re.search(r"^\s*pid = ([0-9]+)\s*$", result.stdout, re.MULTILINE)
    program = re.search(r"^\s*program = (.+?)\s*$", result.stdout, re.MULTILINE)
    if state is None or state.group(1) != "running" or pid is None or program is None:
        raise ProbeFailure(f"CAC launchd record is not running: {result.stdout[-2000:]}")
    return int(pid.group(1)), Path(program.group(1))


def assert_preexisting_service_absent(environment: Mapping[str, str]) -> None:
    service = f"gui/{os.getuid()}/{BROKER_LABEL}"
    result = launchctl_result(environment, service)
    if result.returncode == 0:
        raise ProbeFailure(f"pre-existing CAC service is loaded: {service}")
    if not is_known_launchctl_not_found(result):
        raise ProbeFailure(
            f"could not establish CAC service absence: {(result.stdout + result.stderr).strip()}"
        )
    pids = listener_pids(environment)
    if pids:
        raise ProbeFailure(f"pre-existing listener owns port {BROKER_PORT}: {pids}")


def fetch_immutable_installer(destination: Path, environment: Mapping[str, str]) -> None:
    tag_result = run_command(
        [
            "git",
            "ls-remote",
            REPOSITORY,
            SOURCE_REF,
        ],
        environment,
        timeout=30.0,
    )
    tag_hashes = [line.split()[0] for line in tag_result.stdout.splitlines() if line.split()]
    if SOURCE_COMMIT not in tag_hashes:
        raise ProbeFailure(f"{SOURCE_REF} does not resolve to {SOURCE_COMMIT}: {tag_hashes}")
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


def install_state_path(home: Path, codex_root: Path, profile: Profile) -> Path:
    identity = json.dumps(
        {
            "claude_config_dir": str(profile.claude_root.resolve()),
            "codex_home": str(codex_root.resolve()),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return home / ".config" / SERVER_NAME / f"install-{digest}.json"


def owned_launch_agent(path: Path, home: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        decoded: object = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return False
    if not isinstance(decoded, dict) or decoded.get("Label") != BROKER_LABEL:
        return False
    arguments = decoded.get("ProgramArguments")
    if (
        not isinstance(arguments, list)
        or not arguments
        or not all(isinstance(item, str) for item in arguments)
    ):
        return False
    program = Path(arguments[0])
    try:
        return program.name == SERVER_NAME and program.resolve(strict=False).is_relative_to(
            home.resolve()
        )
    except (OSError, RuntimeError):
        return False


def add_cleanup_candidate(candidates: list[Profile], profile: Profile) -> None:
    if profile not in candidates:
        candidates.append(profile)


def remove_cleanup_candidate(candidates: list[Profile], profile: Profile) -> None:
    if profile in candidates:
        candidates.remove(profile)


def cleanup_profile(
    base: Mapping[str, str],
    home: Path,
    codex_root: Path,
    profile: Profile,
) -> list[str]:
    """Attempt owned CLI cleanup, then a tightly verified launchd fallback."""
    errors: list[str] = []
    state = install_state_path(home, codex_root, profile)
    entrypoint = home / ".local" / "bin" / SERVER_NAME
    launch_agent = home / "Library" / "LaunchAgents" / f"{BROKER_LABEL}.plist"
    metadata_dir = state.parent
    other_states = (
        [item for item in metadata_dir.glob("install*.json") if item != state]
        if metadata_dir.exists()
        else []
    )
    should_uninstall = state.is_file() or (entrypoint.is_symlink() and not other_states)
    if should_uninstall:
        try:
            run_cli(base, home, codex_root, profile, ["uninstall"])
        except ProbeFailure as error:
            errors.append(f"{profile.name} uninstall failed: {error}")
    if not other_states and owned_launch_agent(launch_agent, home):
        service = f"gui/{os.getuid()}/{BROKER_LABEL}"
        loaded = launchctl_result(base, service)
        if loaded.returncode == 0:
            result = run_command(
                ["launchctl", "bootout", service],
                base,
                timeout=15.0,
                check=False,
            )
            if result.returncode == 0:
                launch_agent.unlink(missing_ok=True)
            else:
                errors.append(f"owned launchd fallback failed: {result.stderr.strip()[-2000:]}")
        elif not is_known_launchctl_not_found(loaded):
            errors.append(
                f"could not establish exact service cleanup: {loaded.stderr.strip()[-2000:]}"
            )
    return errors


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
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}]}},
    )
    for profile in profiles:
        write_json(
            profile.claude_root / "settings.json",
            {
                "crossSessionInbound": "hold",
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "/usr/bin/true"}]}]
                },
            },
        )
        write_json(
            profile.claude_root / ".claude.json",
            {"mcpServers": {f"foreign-{profile.name}": {"command": "/usr/bin/true", "args": []}}},
        )
    home.mkdir(parents=True, mode=0o700, exist_ok=True)


def install_profile(
    base: Mapping[str, str],
    home: Path,
    codex_root: Path,
    profile: Profile,
    installer: Path,
    cleanup_candidates: list[Profile],
) -> None:
    add_cleanup_candidate(cleanup_candidates, profile)
    environment = profile_environment(base, home, codex_root, profile)
    environment.update(
        {
            "CROSS_AGENT_CHAT_APPROVE": "1",
            "CROSS_AGENT_CHAT_PROVIDERS": "claude,codex",
            "CROSS_AGENT_CHAT_SOURCE": f"git+{REPOSITORY}@{SOURCE_COMMIT}",
            "CROSS_AGENT_CHAT_DEVICE": "g03-ci-probe",
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
    stable = home / ".local" / "bin" / SERVER_NAME
    if (
        not plist.is_file()
        or plist.is_symlink()
        or not current.is_symlink()
        or not stable.is_symlink()
    ):
        raise ProbeFailure(f"{label} did not leave the expected launchd/runtime files")
    pid, program = running_service(base)
    try:
        if program.resolve(strict=True) != stable.resolve(strict=True):
            raise ProbeFailure(f"{label} launchd program is not the owned stable entrypoint")
    except OSError as error:
        raise ProbeFailure(f"{label} launchd program is unavailable: {program}") from error
    if listener_pids(base) != [str(pid)]:
        raise ProbeFailure(f"{label} listener PID does not match launchd PID {pid}")
    version = run_command([str(program), "--version"], base, timeout=15.0)
    if version.stdout.strip() != f"{SERVER_NAME} 0.4.1":
        raise ProbeFailure(f"{label} launchd program has unexpected version: {version.stdout}")


def assert_unhealthy_removed(base: Mapping[str, str], home: Path) -> None:
    service = f"gui/{os.getuid()}/{BROKER_LABEL}"
    result = launchctl_result(base, service)
    if result.returncode == 0:
        raise ProbeFailure("CAC service remained loaded after final uninstall")
    if not is_known_launchctl_not_found(result):
        raise ProbeFailure("could not establish CAC service removal")
    if listener_pids(base):
        raise ProbeFailure("CAC broker listener remained after final uninstall")
    plist = home / "Library" / "LaunchAgents" / f"{BROKER_LABEL}.plist"
    runtime = home / ".local" / "share" / f"{SERVER_NAME}-runtime"
    stable = home / ".local" / "bin" / SERVER_NAME
    current = runtime / "current"
    metadata = home / ".config" / SERVER_NAME
    metadata_files = list(metadata.glob("install*.json")) if metadata.exists() else []
    if (
        plist.exists()
        or plist.is_symlink()
        or stable.exists()
        or stable.is_symlink()
        or runtime.exists()
        or runtime.is_symlink()
        or current.exists()
        or current.is_symlink()
        or metadata.is_symlink()
        or bool(metadata_files)
    ):
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
        serialized_settings = json.dumps(settings)
        if (
            settings.get("crossSessionInbound") != "hold"
            or "/usr/bin/true" not in serialized_settings
            or SERVER_NAME in serialized_settings
        ):
            raise ProbeFailure(f"{profile.name} Claude prior settings were not restored")
        if f"foreign-{profile.name}" not in json.dumps(claude) or SERVER_NAME in json.dumps(claude):
            raise ProbeFailure(f"{profile.name} Claude foreign MCP entry was not restored")


def assert_prior_values(profiles: Sequence[Profile], codex_root: Path) -> None:
    codex_config = (codex_root / "config.toml").read_text()
    if (
        "[features]\nhooks = false" not in codex_config
        or 'sentinel = "keep"' not in codex_config
        or SERVER_NAME in codex_config
    ):
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
    # Synthetic metadata, with no accompanying sends. Keeping nonempty history
    # catches an uninstall that silently resets the file to an empty list.
    fixture_rows: list[dict[str, str | int]] = [
        {
            "schema_version": 1,
            "event_id": f"00000000-0000-4000-8000-00000000000{index}",
            "source_key": "1" * 64,
            "source_generation": "00000000-0000-4000-8000-000000000010",
            "source_alias": "claude@lifecycle-probe:synthetic",
            "target_key": "2" * 64,
            "target_generation": "00000000-0000-4000-8000-000000000020",
            "payload_digest": hashlib.sha256(b"synthetic lifecycle metadata").hexdigest(),
            "status": status,
            "timestamp": "2026-09-01T00:00:00+00:00",
        }
        for index, status in enumerate(("TRANSPORT_ACCEPTED", "UNKNOWN_DELIVERY"), 1)
    ]
    intent_bytes = (json.dumps(fixture_rows, sort_keys=True) + "\n").encode()
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
    assert_github_hosted()
    base = base_environment()
    print(json.dumps({"runner": runner_facts(base)}, sort_keys=True))
    require_gui_domain(base)
    assert_preexisting_service_absent(base)
    runner_temp_value = os.environ.get("RUNNER_TEMP")
    if runner_temp_value is None:
        raise ProbeFailure("RUNNER_TEMP is required for private lifecycle roots")
    runner_temp = Path(runner_temp_value).resolve()
    runner_temp.mkdir(parents=True, exist_ok=True)
    raw_root = Path(tempfile.mkdtemp(prefix="cac-g03-", dir=runner_temp))
    root = raw_root
    cleanup_candidates: list[Profile] = []
    failure: Exception | None = None
    cleanup_errors: list[str] = []
    home = root / "home"
    codex_root = root / "codex-shared"
    profiles = (
        Profile("profile-a", home / "claude-a"),
        Profile("profile-b", home / "claude-b"),
    )
    try:
        root.chmod(0o700)
        seed_synthetic_roots(home, codex_root, profiles)
        installer = root / "install.sh"
        fetch_immutable_installer(installer, base)
        intents, lock, intent_bytes, lock_inode = create_durable_state(home)
        install_profile(base, home, codex_root, profiles[0], installer, cleanup_candidates)
        assert_healthy(base, home, codex_root, profiles[0], "profile-a initial")

        install_profile(base, home, codex_root, profiles[1], installer, cleanup_candidates)
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
        assert_healthy(base, home, codex_root, profiles[1], "profile-b after shared uninstall")
        assert_shared_owner_state(codex_root)
        assert_claude_prior_values((profiles[0],))
        assert_durable_state(intents, lock, intent_bytes, lock_inode)
        remove_cleanup_candidate(cleanup_candidates, profiles[0])

        run_cli(base, home, codex_root, profiles[1], ["uninstall"])
        assert_unhealthy_removed(base, home)
        assert_durable_state(intents, lock, intent_bytes, lock_inode)
        assert_prior_values(profiles, codex_root)
        remove_cleanup_candidate(cleanup_candidates, profiles[1])

        install_profile(base, home, codex_root, profiles[1], installer, cleanup_candidates)
        assert_healthy(base, home, codex_root, profiles[1], "profile-b canonical reinstall")
        assert_durable_state(intents, lock, intent_bytes, lock_inode)
        run_cli(base, home, codex_root, profiles[1], ["uninstall"])
        assert_unhealthy_removed(base, home)
        assert_durable_state(intents, lock, intent_bytes, lock_inode)
        assert_prior_values(profiles, codex_root)
        remove_cleanup_candidate(cleanup_candidates, profiles[1])
    except Exception as error:
        failure = error

    for profile in reversed(cleanup_candidates):
        try:
            cleanup_errors.extend(cleanup_profile(base, home, codex_root, profile))
        except Exception as error:
            cleanup_errors.append(f"{profile.name} cleanup crashed: {error}")
    try:
        assert_unhealthy_removed(base, home)
    except Exception as error:
        cleanup_errors.append(f"final cleanup residue check failed: {error}")
    if cleanup_errors:
        print(
            f"G03 cleanup failed; preserving temporary root {root}: " + " | ".join(cleanup_errors),
            file=sys.stderr,
        )
        raise ProbeFailure("cleanup failed; inspect the preserved RUNNER_TEMP root")
    shutil.rmtree(root)
    if failure is not None:
        if isinstance(failure, ProbeFailure):
            raise failure
        raise ProbeFailure(f"probe failed: {failure}") from failure
    print(
        json.dumps(
            {
                "status": "PASS_PARTIAL",
                "service_lifecycle": "passed",
                "authenticated_collaboration": "not_tested",
                "gui_domain": "available",
                "source": f"{SOURCE_LABEL}@{SOURCE_COMMIT}",
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    try:
        return run_probe()
    except UnsupportedEnvironment as error:
        print(
            json.dumps(
                {
                    "status": "PARTIAL_UNSUPPORTED",
                    "reason": str(error),
                    "service_lifecycle": "not_run",
                    "authenticated_collaboration": "not_tested",
                    "source": f"{SOURCE_LABEL}@{SOURCE_COMMIT}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except ProbeFailure as error:
        print(f"G03 lifecycle probe failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
