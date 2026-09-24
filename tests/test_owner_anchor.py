"""Owner image anchors keep a registered session current through provider updates.

The fixtures are real compiled processes: only a kernel observation can show the
region-info vnode surviving the provider package directory being renamed aside
and deleted, which is the exact npm-update failure these tests exercise.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import (
    ChatError,
    IntentStore,
    Registry,
    Route,
    atomic_json,
    owner_anchor_path,
    session_key,
)
from cross_agent_chat.devin import DevinHookEvent

# Captured at import, before conftest's per-test subprocess guard is installed;
# the guard rightly refuses non-fixture binaries, and cc is only ever asked to
# produce fixture binaries here.
_REAL_RUN = subprocess.run

SLEEPER_SOURCE = "#include <unistd.h>\nint main(void){pause();return 0;}\n"
REEXEC_SOURCE = (
    "#include <unistd.h>\n"
    "int main(int argc, char **argv){"
    "if(argc<4)return 2;"
    "while(access(argv[3],F_OK)!=0)usleep(20000);"
    "execl(argv[1],argv[2],(char*)0);"
    "return 3;}\n"
)


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("owner-anchor-fixtures")
    sleeper_source = root / "sleeper.c"
    sleeper_source.write_text(SLEEPER_SOURCE, encoding="utf-8")
    reexec_source = root / "reexec.c"
    reexec_source.write_text(REEXEC_SOURCE, encoding="utf-8")
    binaries: dict[str, Path] = {}
    for name, source in (
        ("sleeper", sleeper_source),
        ("other", sleeper_source),
        ("reexec", reexec_source),
    ):
        binary = root / name
        result = _REAL_RUN(
            ["cc", str(source), "-o", str(binary)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"cc is unavailable: {result.stderr.strip()}")
        binaries[name] = binary
    return binaries


def _install(fixture: Path, package: Path) -> Path:
    package.mkdir(parents=True)
    binary = package / "provider.exe"
    shutil.copy2(fixture, binary)
    return binary


def _spawn(binary: Path, *extra: str) -> subprocess.Popen[bytes]:
    process = subprocess.Popen([str(binary), *extra])
    deadline = time.monotonic() + 3
    while process.poll() is None:
        try:
            runtime._owner_image_observation(process.pid)
        except ChatError:
            if time.monotonic() > deadline:
                break
            time.sleep(0.02)
            continue
        return process
    process.kill()
    process.wait()
    pytest.fail("fixture process image never became observable")


def _register(
    root: Path,
    provider: str,
    pid: int,
    cwd: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
) -> Route:
    hook = {"session_id": session_id, "cwd": str(cwd)}
    monkeypatch.setattr(runtime, "hook_input", lambda _event: hook)
    monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: None)
    route = runtime.register(provider, "studio", pid, str(root))
    assert route is not None
    return route


def _npm_update_aside(package: Path) -> Path:
    aside = package.with_name(f".{package.name}-old")
    package.rename(aside)
    return aside


def test_anchored_registration_survives_provider_package_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        anchor = owner_anchor_path(root, route.generation)
        assert anchor.is_file()
        assert stat.S_IMODE(anchor.stat().st_mode) == 0o600
        assert runtime._route_current(root, route)

        aside = _npm_update_aside(package)
        # Mid-update the path string moved aside: it still resolves, so the
        # legacy identity now binds a different pathname and mismatches, while
        # the image anchor still proves the same running process.
        assert (
            runtime.recipient_owner_identity("claude", process.pid, route.profile_root)[0]
            != route.owner_identity
        )
        assert runtime._route_current(root, route)

        shutil.rmtree(aside)
        with pytest.raises(ChatError):
            runtime.recipient_owner_identity("claude", process.pid, route.profile_root)
        # The courier loop's own consumer check stays current on the image.
        assert runtime._route_current(root, route)
        assert runtime._anchored_owner_current(root, route)
    finally:
        process.kill()
        process.wait()


def test_unchanged_install_passes_both_checks_and_unanchored_stays_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        assert runtime._route_current(root, route)
        assert runtime._anchored_owner_current(root, route)
        assert (
            runtime.recipient_owner_identity("claude", process.pid, route.profile_root)[0]
            == route.owner_identity
        )

        owner_anchor_path(root, route.generation).unlink()
        assert runtime._route_current(root, route)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        assert not runtime._route_current(root, route)
    finally:
        process.kill()
        process.wait()


def test_fresh_process_with_deleted_path_gets_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    try:
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        session_id = str(uuid4())
        monkeypatch.setattr(
            runtime, "hook_input", lambda _event: {"session_id": session_id, "cwd": str(cwd)}
        )
        with pytest.raises(ChatError):
            runtime.register("claude", "studio", process.pid, str(root))
        assert Registry(root).routes() == []
    finally:
        process.kill()
        process.wait()


def test_reregistration_after_update_reuses_only_the_anchored_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)

        monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: pytest.fail("respawn"))
        reused = runtime.register("claude", "studio", process.pid, str(root))
        assert reused == route
        assert Registry(root).routes() == [route]

        monkeypatch.setattr(
            runtime,
            "hook_input",
            lambda _event: {"session_id": str(uuid4()), "cwd": str(cwd)},
        )
        with pytest.raises(ChatError):
            runtime.register("claude", "studio", process.pid, str(root))
        assert Registry(root).routes() == [route]
    finally:
        process.kill()
        process.wait()


def test_process_execing_a_different_image_loses_the_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["reexec"], package)
    other = _install(compiled["other"], tmp_path / "other-pkg")
    gate = tmp_path / "gate"
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary, str(other), "other", str(gate))
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        before = runtime._owner_image_observation(process.pid)
        assert runtime._route_current(root, route)

        gate.touch()
        deadline = time.monotonic() + 5
        while runtime._route_current(root, route):
            if time.monotonic() > deadline:
                pytest.fail("exec'd image kept the original anchor")
            time.sleep(0.02)
        after = runtime._owner_image_observation(process.pid)
        # Same process, same uid and birth; only the mapped image changed.
        assert before[2:] == after[2:]
        assert before[:2] != after[:2]
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_renamed_over_path_keeps_anchor_for_old_image_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        bound = runtime._route_owner_anchor(root, route)
        assert bound is not None

        staging = tmp_path / "staging.exe"
        shutil.copy2(compiled["other"], staging)
        os.replace(staging, binary)
        # The old image is still mapped: both checks pass, the anchor on image.
        assert runtime._route_current(root, route)
        assert runtime._anchored_owner_current(root, route)
        assert (
            runtime.recipient_owner_identity("claude", process.pid, route.profile_root)[0]
            == route.owner_identity
        )

        replacement = _spawn(binary)
        try:
            assert (
                runtime.recipient_owner_image_identity(
                    "claude", replacement.pid, route.profile_root
                )
                != bound
            )
            foreign_pid_route = replace(route, pid=replacement.pid)
            assert not runtime._route_owner_current(root, foreign_pid_route)
        finally:
            replacement.kill()
            replacement.wait()
    finally:
        process.kill()
        process.wait()


@pytest.mark.parametrize(
    "mutation",
    [
        "provider",
        "pid",
        "generation",
        "owner_identity",
        "profile_root",
        "wrong-uid-image",
        "different-start-image",
    ],
)
def test_anchor_binding_mismatches_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compiled: dict[str, Path],
    mutation: str,
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        anchor = owner_anchor_path(root, route.generation)
        raw = json.loads(anchor.read_text(encoding="utf-8"))
        assert route.profile_root is not None
        if mutation == "provider":
            raw["provider"] = "codex"
        elif mutation == "pid":
            raw["pid"] = process.pid + 1
        elif mutation == "generation":
            raw["generation"] = str(uuid4())
        elif mutation == "owner_identity":
            raw["owner_identity"] = "b" * 64
        elif mutation == "profile_root":
            raw["profile_root"] = str(tmp_path / "elsewhere")
        else:
            device, inode, uid, start_seconds, start_microseconds = (
                runtime._owner_image_observation(process.pid)
            )
            if mutation == "wrong-uid-image":
                uid += 1
            else:
                start_seconds += 1
            raw["image_identity"] = runtime._owner_image_identity(
                "claude",
                device,
                inode,
                uid,
                start_seconds,
                start_microseconds,
                route.profile_root,
            )
        atomic_json(anchor, raw)

        # The path identity still verifies; the foreign anchor must fail closed
        # rather than fall back to the legacy check.
        assert (
            runtime.recipient_owner_identity("claude", process.pid, route.profile_root)[0]
            == route.owner_identity
        )
        assert not runtime._route_current(root, route)
        assert not runtime._route_owner_current(root, route)
    finally:
        process.kill()
        process.wait()


@pytest.mark.parametrize("corruption", ["malformed", "non-private", "foreign-route"])
def test_invalid_anchor_files_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compiled: dict[str, Path],
    corruption: str,
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        anchor = owner_anchor_path(root, route.generation)
        if corruption == "malformed":
            anchor.write_bytes(b"not an anchor{")
        elif corruption == "non-private":
            anchor.chmod(0o644)
        else:
            other = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
            foreign = json.loads(
                owner_anchor_path(root, other.generation).read_text(encoding="utf-8")
            )
            atomic_json(anchor, foreign)

        assert (
            runtime.recipient_owner_identity("claude", process.pid, route.profile_root)[0]
            == route.owner_identity
        )
        assert not runtime._route_current(root, route)
    finally:
        process.kill()
        process.wait()


def test_route_removal_deletes_anchor_and_leaves_intents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        anchor = owner_anchor_path(root, route.generation)
        assert anchor.is_file()

        store = IntentStore(root)
        accepted = store.begin_identity(
            source_key=session_key(route.provider, route.session_id),
            source_generation=route.generation,
            source_alias=route.alias,
            target_key="b" * 64,
            target_generation=str(uuid4()),
            payload_digest="c" * 64,
        )
        store.mark(accepted, "TRANSPORT_ACCEPTED")
        unknown = store.begin_identity(
            source_key=session_key(route.provider, route.session_id),
            source_generation=route.generation,
            source_alias=route.alias,
            target_key="d" * 64,
            target_generation=str(uuid4()),
            payload_digest="e" * 64,
        )
        store.mark(unknown, "UNKNOWN_DELIVERY")
        recorded = store.intents()

        Registry(root).remove(
            route.provider, route.session_id, route.pid, generation=route.generation
        )
        assert not anchor.exists()
        assert not runtime._route_current(root, route)
        assert store.intents() == recorded
        assert {item.status for item in store.intents()} == {
            "TRANSPORT_ACCEPTED",
            "UNKNOWN_DELIVERY",
        }
    finally:
        process.kill()
        process.wait()


def test_devin_prompt_registration_anchors_and_reuses_after_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package).rename(package / "devin")
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    monkeypatch.setenv("DEVIN_PROJECT_DIR", str(cwd))
    monkeypatch.setattr(runtime, "devin_binary", lambda: binary.resolve(strict=True))
    monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: None)
    session_id = "devin-session-1"
    event = DevinHookEvent(
        hook_event_name="UserPromptSubmit",
        session_id=session_id,
        prompt_id=str(uuid4()),
        stop_hook_active=None,
    )
    process = _spawn(binary)
    try:
        route = runtime._register_devin_prompt("studio", process.pid, root, event)
        assert owner_anchor_path(root, route.generation).is_file()
        assert runtime._route_current(root, route)

        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        assert runtime._route_current(root, route)
        assert runtime._register_devin_prompt("studio", process.pid, root, event) == route
    finally:
        process.kill()
        process.wait()


def test_courier_owner_binary_falls_back_only_for_anchored_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    discovered = tmp_path / "discovered.exe"
    shutil.copy2(compiled["sleeper"], discovered)
    process = _spawn(binary)
    try:
        anchored = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        unanchored = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        owner_anchor_path(root, unanchored.generation).unlink()
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        monkeypatch.setattr(runtime, "_provider_binary", lambda _provider: discovered)

        assert runtime._courier_owner_binary(root, anchored) == discovered
        with pytest.raises(ChatError):
            runtime._courier_owner_binary(root, unanchored)
    finally:
        process.kill()
        process.wait()


def test_anchor_not_written_when_region_and_path_disagree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    try:
        device, inode, uid, start_seconds, start_microseconds = runtime._owner_image_observation(
            process.pid
        )
        monkeypatch.setattr(
            runtime,
            "_owner_image_observation",
            lambda _pid: (device, inode + 1, uid, start_seconds, start_microseconds),
        )
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        assert not owner_anchor_path(root, route.generation).exists()
        assert runtime._route_current(root, route)
    finally:
        process.kill()
        process.wait()


def test_image_observation_refuses_unknown_process() -> None:
    with pytest.raises(ChatError):
        runtime._owner_image_observation(2**22)
