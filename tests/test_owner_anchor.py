"""Owner image anchors keep a registered session current through provider updates.

The fixtures are real compiled processes: only a kernel observation can show the
region-info vnode surviving the provider package directory being renamed aside
and deleted, which is the exact npm-update failure these tests exercise.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import claude_runtime, runtime
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
    "if(argc>5)return execl(argv[1],argv[2],argv[4],argv[5],(char*)0);"
    "return execl(argv[1],argv[2],(char*)0);}\n"
)
KEEPER_SOURCE = (
    "#include <unistd.h>\n"
    "#include <fcntl.h>\n"
    "#include <sys/mman.h>\n"
    "int main(int argc, char **argv){"
    "if(argc>1){"
    "int fd=open(argv[1],O_RDONLY);"
    "if(fd>=0){(void)mmap(0,4096,PROT_READ,MAP_PRIVATE,fd,0);}"
    "}"
    "if(argc>2){int m=open(argv[2],O_WRONLY|O_CREAT|O_TRUNC,0600);"
    'if(m>=0){(void)write(m,"mapped\\n",7);close(m);}}'
    "pause();return 0;}\n"
)


@pytest.fixture(scope="module")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("owner-anchor-fixtures")
    sleeper_source = root / "sleeper.c"
    sleeper_source.write_text(SLEEPER_SOURCE, encoding="utf-8")
    reexec_source = root / "reexec.c"
    reexec_source.write_text(REEXEC_SOURCE, encoding="utf-8")
    keeper_source = root / "keeper.c"
    keeper_source.write_text(KEEPER_SOURCE, encoding="utf-8")
    binaries: dict[str, Path] = {}
    for name, source in (
        ("sleeper", sleeper_source),
        ("other", sleeper_source),
        ("reexec", reexec_source),
        ("keeper", keeper_source),
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
    deadline = time.monotonic() + 15
    while process.poll() is None:
        try:
            runtime._owner_image_vnode(process.pid)
            runtime._owner_exec_facts(process.pid)
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

        # Courier answers bootstrap, so reuse must not respawn it.
        monkeypatch.setattr(
            runtime,
            "request_socket",
            lambda *_args, **_kwargs: {
                "schema_version": 1,
                "status": "BOOTSTRAPPED",
                "generation": route.generation,
            },
        )
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
        before = runtime._owner_exec_facts(process.pid)
        assert runtime._route_current(root, route)

        gate.touch()
        # Wait on the observable exec (uuid changes) rather than a fixed
        # window; the route must then read not-current.
        deadline = time.monotonic() + 30
        while True:
            try:
                after = runtime._owner_exec_facts(process.pid)
            except ChatError:
                after = before
            if after[0] != before[0]:
                break
            if time.monotonic() > deadline:
                pytest.fail("process never exec'd the other image")
            if process.poll() is not None:
                pytest.fail("process exited instead of exec'ing")
            time.sleep(0.02)
        assert not runtime._route_current(root, route)
        # Same process, same uid and birth; only the exec generation changed.
        assert before[3:] == after[3:]
        assert before[0] != after[0]
        assert before[2] != after[2]
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
                != bound[2]
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
            device, inode = runtime._owner_image_vnode(process.pid)
            p_uuid, uniqueid, idversion, uid, start_seconds, start_microseconds = (
                runtime._owner_exec_facts(process.pid)
            )
            if mutation == "wrong-uid-image":
                uid += 1
            else:
                start_seconds += 1
            assert route.profile_root is not None
            raw["image_identity"] = runtime._owner_image_identity(
                "claude",
                device,
                inode,
                uniqueid,
                idversion,
                p_uuid,
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
        device, inode = runtime._owner_image_vnode(process.pid)
        monkeypatch.setattr(
            runtime,
            "_owner_image_vnode",
            lambda _pid, _target=None: (device, inode + 1),
        )
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        assert not owner_anchor_path(root, route.generation).exists()
        assert runtime._route_current(root, route)
    finally:
        process.kill()
        process.wait()


def test_image_observation_refuses_unknown_process() -> None:
    with pytest.raises(ChatError):
        runtime._owner_image_vnode(2**22)
    with pytest.raises(ChatError):
        runtime._owner_exec_facts(2**22)


def test_execed_image_still_mapping_the_old_executable_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["reexec"], package)
    keeper = _install(compiled["keeper"], tmp_path / "keeper-pkg")
    gate = tmp_path / "gate"
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    # argv[4]/argv[5] are forwarded to the exec'd image, which mmaps the
    # registered executable's own file and then writes a marker: the anchored
    # vnode stays present inside a *different* image's address space. The exec
    # generation is what must reject the anchor, not the region scan. Waiting
    # on the marker file keeps the sequencing deterministic instead of polling
    # kernel state.
    marker = tmp_path / "mapped"
    process = _spawn(binary, str(keeper), "keeper", str(gate), str(binary), str(marker))
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        bound = runtime._route_owner_anchor(root, route)
        assert bound is not None
        before = runtime._owner_exec_facts(process.pid)

        gate.touch()
        deadline = time.monotonic() + 30
        while not marker.exists():
            if time.monotonic() > deadline:
                uuid_changed: object
                try:
                    uuid_changed = runtime._owner_exec_facts(process.pid)[0] != before[0]
                except ChatError:
                    uuid_changed = "unreadable"
                pytest.fail(
                    f"exec'd image never mapped the old executable "
                    f"(poll={process.poll()}, gate={gate.exists()}, "
                    f"uuid_changed={uuid_changed})"
                )
            if process.poll() is not None:
                pytest.fail("exec'd image exited before mapping")
            time.sleep(0.02)

        # The new image holds the old vnode mapped; the exec generation is
        # what rejects the route. Kernel reads may transiently fail under
        # suite load, so they get a short retry window of their own.
        assert process.poll() is None
        retry_deadline = time.monotonic() + 3
        while True:
            try:
                found = runtime._owner_image_vnode(process.pid, (bound[0], bound[1]))
                after = runtime._owner_exec_facts(process.pid)
            except ChatError:
                if time.monotonic() > retry_deadline:
                    raise
                time.sleep(0.05)
                continue
            break
        assert found == (bound[0], bound[1])
        assert before[3:] == after[3:]
        assert before[0] != after[0]
        assert before[2] != after[2]
        assert not runtime._anchored_owner_current(root, route)
        assert not runtime._route_owner_current(root, route)
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_reregistration_during_renamed_update_reuses_the_generation(
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
        _npm_update_aside(package)
        # Mid-update the path resolves to a different pathname, so a fresh
        # legacy identity would differ; the anchored route is reused instead.
        assert (
            runtime.recipient_owner_identity("claude", process.pid, route.profile_root)[0]
            != route.owner_identity
        )
        reused = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        assert reused == route
        assert reused.generation == route.generation
        assert Registry(root).routes() == [route]
    finally:
        process.kill()
        process.wait()


def test_anchor_is_written_before_the_route_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    anchored_at_publish: list[bool] = []
    original_write = Registry._write

    def spy_write(self: Registry, routes: list[Route]) -> None:
        anchored_at_publish.append(owner_anchor_path(self.root, routes[-1].generation).is_file())
        original_write(self, routes)

    monkeypatch.setattr(Registry, "_write", spy_write)
    try:
        _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        assert anchored_at_publish == [True]
    finally:
        process.kill()
        process.wait()


def test_validation_scans_regions_for_the_anchored_vnode_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """MOCKED-BRANCH test: no fixture can place an unrelated vnode below the
    executable's own __TEXT, so the A6 scan branch is scripted."""
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
        image_dev, image_ino, _ = bound

        regions = {
            0: (image_dev + 1, image_ino + 1, 0x3000),
            0x3000: (image_dev, image_ino, 0x4000),
        }
        monkeypatch.setattr(
            runtime, "_owner_vnode_region", lambda _pid, address: regions.get(address)
        )
        # The lowest region no longer holds the image vnode; the bounded scan
        # finds it higher in the address space and the exec facts still match.
        assert runtime._anchored_owner_current(root, route)
        assert runtime._route_current(root, route)

        monkeypatch.setattr(
            runtime,
            "_owner_vnode_region",
            lambda _pid, address: {0: (image_dev + 1, image_ino + 1, 0x3000)}.get(address),
        )
        assert not runtime._anchored_owner_current(root, route)
        assert not runtime._route_current(root, route)
    finally:
        process.kill()
        process.wait()


def test_exec_facts_unavailable_still_publishes_a_legacy_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """An unavailable exec-generation read is the same as an unavailable
    region read: no anchor, but the registration still publishes a normal
    legacy route instead of aborting."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)

    def unavailable(_pid: int) -> tuple[bytes, int, int, int, int, int]:
        raise ChatError("provider process identity is unavailable")

    monkeypatch.setattr(runtime, "_owner_exec_facts", unavailable)
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, str(uuid4()))
        assert route is not None
        assert not owner_anchor_path(root, route.generation).exists()
        assert not runtime._anchored_owner_current(root, route)
        assert runtime._route_current(root, route)
    finally:
        process.kill()
        process.wait()


def test_anchor_write_and_publish_share_the_routes_lock(
    tmp_path: Path, compiled: dict[str, Path]
) -> None:
    """Another session's compaction can never prune a just-written anchor:
    the publish methods write the sidecar inside the same routes-lock
    critical section that writes routes.json."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    process = _spawn(binary)
    try:
        route = Route.create(
            provider="claude",
            session_id=str(uuid4()),
            device="studio",
            cwd=str(cwd),
            pid=process.pid,
            owner_identity=runtime.recipient_owner_identity("claude", process.pid)[0],
            profile_root=runtime.recipient_profile_root("claude"),
        )
        document = runtime._owner_anchor_document(root, route, binary)
        assert document is not None

        # A different session compacts first (would have pruned an orphan
        # anchor under the old write-then-publish interleave), then the
        # publish still leaves route and anchor on disk together.
        Registry(root).compact_dead()
        published = Registry(root).upsert_or_reuse_live_owner(route, anchor=document)
        assert published == route
        assert owner_anchor_path(root, route.generation).is_file()
        assert Registry(root).routes() == [route]
    finally:
        process.kill()
        process.wait()


def test_anchored_reregistration_respawns_a_missing_courier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """A repeated SessionStart after the update still runs courier recovery:
    no socket means the courier is gone and it is spawned again."""
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

        spawned: list[Route] = []
        monkeypatch.setattr(runtime, "_spawn_courier", lambda _root, item: spawned.append(item))
        reused = runtime.register("claude", "studio", process.pid, str(root))
        assert reused == route
        assert spawned == [route]
        assert Registry(root).routes() == [route]
    finally:
        process.kill()
        process.wait()


def test_anchored_reregistration_keeps_courier_when_it_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """Healthy courier answering bootstrap: reuse without touching anything."""
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

        monkeypatch.setattr(
            runtime,
            "request_socket",
            lambda *_args, **_kwargs: {
                "schema_version": 1,
                "status": "BOOTSTRAPPED",
                "generation": route.generation,
            },
        )
        monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: pytest.fail("respawn"))
        reused = runtime.register("claude", "studio", process.pid, str(root))
        assert reused == route
        assert Registry(root).routes() == [route]
    finally:
        process.kill()
        process.wait()


def test_anchored_reregistration_keeps_route_when_hook_cwd_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """A drifted hook cwd inside an updated session keeps the original route
    exactly as published -- generation, cwd, project, alias -- and only runs
    courier health. The occupied generation socket means any respawn attempt
    would raise; none may happen, and nothing may be left mutated."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    moved = tmp_path / "moved"
    moved.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    real_spawn = runtime._spawn_courier
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)

        # _register stubbed _spawn_courier; restore the real function so any
        # spawn the handover attempts runs the true socket-exists check. The
        # generation-keyed socket belongs to the live courier: if the handover
        # mutated the route and respawned, the real spawn would collide with
        # this occupied path after the route changed.
        monkeypatch.setattr(runtime, "_spawn_courier", real_spawn)
        occupied = runtime.socket_path(root, route)
        occupied.touch(mode=0o600)
        try:
            monkeypatch.setattr(
                runtime,
                "hook_input",
                lambda _event: {"session_id": session_id, "cwd": str(moved)},
            )
            monkeypatch.setattr(
                runtime,
                "request_socket",
                lambda *_args, **_kwargs: {
                    "schema_version": 1,
                    "status": "BOOTSTRAPPED",
                    "generation": route.generation,
                },
            )
            # The native session confirms the drifted cwd: any code still
            # applying drift would now mutate the route and collide with the
            # occupied socket on the real spawn path.
            monkeypatch.setattr(
                claude_runtime,
                "claude_agents",
                lambda _session_id, timeout=2.0: [
                    {
                        "session_id": session_id,
                        "name": "Probe",
                        "kind": "interactive",
                        "cwd": str(moved.resolve()),
                    }
                ],
            )
            # The courier answers bootstrap, so the real _spawn_courier is
            # never called; had it run, the occupied socket above would have
            # raised ChatError("session courier socket already exists").
            reused = runtime.register("claude", "studio", process.pid, str(root))
            assert reused == route
            assert Registry(root).routes() == [route]
        finally:
            occupied.unlink(missing_ok=True)
    finally:
        process.kill()
        process.wait()


def _dead_courier_socket(path: Path) -> None:
    """Leave a bound-then-orphaned 0600 socket, like a SIGKILLed courier."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
    finally:
        listener.close()


def _dead_courier_lock(path: Path, socket_file: Path | None = None) -> None:
    """Leave the released lifetime lock a SIGKILLed courier held.

    ``socket_file`` records the bound socket's inode exactly like a live
    courier does; without it the lock stays empty like a courier killed
    before it could record, or one that predates the inode binding.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    if socket_file is not None:
        metadata = socket_file.lstat()
        os.write(descriptor, f"{metadata.st_dev} {metadata.st_ino}\n".encode())
    os.close(descriptor)


def _backlogged_listener(path: Path) -> tuple[socket.socket, socket.socket]:
    """Bind a live listener whose single-slot backlog is already full.

    Returns the listener and the client occupying its backlog: on macOS the
    next connect to it is refused with ECONNREFUSED, which is exactly what a
    busy courier costs a probe.
    """
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    return listener, client


def _held_courier_lock(path: Path) -> int:
    """Hold LOCK_EX on the lifetime lock exactly like a live courier does.

    A second descriptor inside this process still conflicts, because flock
    locks are per open-file-description.
    """
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return descriptor


def _bootstrap_listener(path: Path, generation: str, *, answer: bool = True) -> socket.socket:
    """Bind a real courier-style listener on the generation socket.

    With ``answer`` it replies BOOTSTRAPPED to every probe; without it the
    listener accepts each connection and holds it silently past the 0.5s
    handover probe deadline, which is exactly what a busy courier costs.
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    path.chmod(0o600)
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            try:
                connection.settimeout(5.0)
                runtime.read_frame(connection)
                if answer:
                    runtime.emit_frame(
                        connection,
                        {
                            "schema_version": 1,
                            "status": "BOOTSTRAPPED",
                            "generation": generation,
                        },
                    )
                else:
                    time.sleep(2.0)
            except (OSError, ChatError):
                pass
            finally:
                connection.close()

    threading.Thread(target=serve, daemon=True).start()
    return server


class _FakeCourierProcess:
    """Live-child stand-in for a spawned courier; never a real process."""

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout: float = 0.0) -> int:
        return 0


def test_anchored_reregistration_respawns_past_a_stale_courier_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """A SIGKILLed courier leaves its bound 0600 socket behind and the kernel
    has already dropped its lifetime lock: the bootstrap connect is refused,
    and reuse must clear that dead path and spawn a courier on the SAME
    generation instead of dying on the occupied-path guard."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    real_spawn = runtime._spawn_courier
    servers: list[socket.socket] = []
    stale: Path | None = None
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        stale = runtime.socket_path(root, route)
        _dead_courier_socket(stale)
        _dead_courier_lock(runtime.courier_lock_path(root, route), stale)

        spawned: list[Route] = []

        def fake_popen(_command: list[str], **_kwargs: object) -> _FakeCourierProcess:
            # The stale socket must already be gone: the real spawn guard
            # would refuse an occupied generation path. The replacement
            # courier binds the same socket and answers the bootstrap probe.
            assert not stale.exists()
            spawned.append(route)
            servers.append(_bootstrap_listener(stale, route.generation))
            return _FakeCourierProcess()

        # _register stubbed _spawn_courier; restore the real one so its true
        # occupied-path guard stays under test while Popen is fixture-bound.
        monkeypatch.setattr(runtime, "_spawn_courier", real_spawn)
        monkeypatch.setattr(runtime, "claude_binary", lambda: Path("/bin/echo"))
        monkeypatch.setattr("cross_agent_chat.runtime.subprocess.Popen", fake_popen)

        reused = runtime.register("claude", "studio", process.pid, str(root))
        assert reused == route
        assert reused.generation == route.generation
        assert spawned == [route]
        assert Registry(root).routes() == [route]
    finally:
        for server in servers:
            server.close()
        if stale is not None:
            stale.unlink(missing_ok=True)
        process.kill()
        process.wait()


def test_anchored_handover_never_replaces_a_live_courier_with_a_full_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """A live courier whose listen backlog is full also answers ECONNREFUSED:
    refusal alone never proves death. While its lifetime lock stays held the
    handover must leave socket and route untouched and spawn nothing."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    listener: socket.socket | None = None
    backlog: socket.socket | None = None
    lock_descriptor: int | None = None
    occupied: Path | None = None
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        occupied = runtime.socket_path(root, route)

        # A busy live courier: listening, but one unaccepted client already
        # fills its backlog so the next connect is refused.
        listener, backlog = _backlogged_listener(occupied)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError) as refused:
                probe.connect(str(occupied))
            assert refused.value.errno == errno.ECONNREFUSED
        finally:
            probe.close()
        lock_descriptor = _held_courier_lock(runtime.courier_lock_path(root, route))

        monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: pytest.fail("respawn"))
        reused = runtime.register("claude", "studio", process.pid, str(root))
        assert reused == route
        assert occupied.exists()
        assert Registry(root).routes() == [route]
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if backlog is not None:
            backlog.close()
        if listener is not None:
            listener.close()
        if occupied is not None:
            occupied.unlink(missing_ok=True)
        process.kill()
        process.wait()


def test_anchored_handover_leaves_a_socket_the_lock_does_not_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """A free lock is not proof the socket belongs to its former holder: a
    courier spawned by a version that never took the lock can sit on the same
    generation path while the recorded inode belongs to a long-gone socket.
    The live listener must survive -- no unlink, no spawn, route returned."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    listener: socket.socket | None = None
    backlog: socket.socket | None = None
    occupied: Path | None = None
    decoy: Path | None = None
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        occupied = runtime.socket_path(root, route)

        # The socket now belongs to a courier that never took the lock; the
        # released lock file still records an inode that is not this one.
        listener, backlog = _backlogged_listener(occupied)
        decoy = occupied.with_name("decoy.sock")
        _dead_courier_socket(decoy)
        _dead_courier_lock(runtime.courier_lock_path(root, route), decoy)
        decoy.unlink()
        decoy = None

        monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: pytest.fail("respawn"))
        reused = runtime.register("claude", "studio", process.pid, str(root))
        assert reused == route
        assert occupied.exists()
        assert Registry(root).routes() == [route]
    finally:
        if backlog is not None:
            backlog.close()
        if listener is not None:
            listener.close()
        if occupied is not None:
            occupied.unlink(missing_ok=True)
        if decoy is not None:
            decoy.unlink(missing_ok=True)
        process.kill()
        process.wait()


def test_anchored_handover_leaves_a_legacy_couriers_stale_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """A courier started before the lifetime lock existed holds no lock, so a
    refused connect cannot prove it dead: the stale socket stays, nothing
    spawns, and the session recovers on restart."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    stale: Path | None = None
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        stale = runtime.socket_path(root, route)
        _dead_courier_socket(stale)

        monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: pytest.fail("respawn"))
        reused = runtime.register("claude", "studio", process.pid, str(root))
        assert reused == route
        assert stale.exists()
        assert Registry(root).routes() == [route]
    finally:
        if stale is not None:
            stale.unlink(missing_ok=True)
        process.kill()
        process.wait()


@pytest.mark.parametrize("occupant", ["symlink", "loose-mode", "directory"])
def test_anchored_handover_refuses_an_unsafe_courier_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compiled: dict[str, Path],
    occupant: str,
) -> None:
    """A lifetime lock path that is not this uid's own 0600 regular file --
    a symlink, a loose-mode file, or a directory -- fails closed: the
    registration refuses, the stale socket stays, nothing spawns."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    stale: Path | None = None
    lock_path: Path | None = None
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        stale = runtime.socket_path(root, route)
        _dead_courier_socket(stale)
        lock_path = runtime.courier_lock_path(root, route)
        if occupant == "symlink":
            target = lock_path.with_name("lock-target.lock")
            _dead_courier_lock(target)
            lock_path.symlink_to(target)
        elif occupant == "loose-mode":
            _dead_courier_lock(lock_path)
            lock_path.chmod(0o644)
        else:
            lock_path.mkdir()

        monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: pytest.fail("respawn"))
        with pytest.raises(ChatError, match="unsafe"):
            runtime.register("claude", "studio", process.pid, str(root))
        assert stale.exists()
        assert Registry(root).routes() == [route]
    finally:
        if lock_path is not None:
            if lock_path.is_symlink() or lock_path.is_file():
                lock_path.unlink(missing_ok=True)
            elif lock_path.is_dir():
                lock_path.rmdir()
            target = lock_path.with_name("lock-target.lock")
            target.unlink(missing_ok=True)
        if stale is not None:
            stale.unlink(missing_ok=True)
        process.kill()
        process.wait()


def test_anchored_handover_keeps_the_route_for_answering_or_busy_couriers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """Real listeners must never be respawned: a courier answering bootstrap
    keeps the route, and a busy courier whose probe times out keeps it too,
    with its socket left exactly in place."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    real_spawn = runtime._spawn_courier
    servers: list[socket.socket] = []
    stale: Path | None = None
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        stale = runtime.socket_path(root, route)

        monkeypatch.setattr(runtime, "_spawn_courier", real_spawn)
        monkeypatch.setattr(
            "cross_agent_chat.runtime.subprocess.Popen",
            lambda *_args, **_kwargs: pytest.fail("respawn"),
        )

        servers.append(_bootstrap_listener(stale, route.generation))
        assert runtime.register("claude", "studio", process.pid, str(root)) == route
        assert stale.exists()

        # Busy courier: accepts but cannot answer inside the probe deadline.
        servers[-1].close()
        stale.unlink()
        servers.append(_bootstrap_listener(stale, route.generation, answer=False))
        assert runtime.register("claude", "studio", process.pid, str(root)) == route
        assert stale.exists()
        assert Registry(root).routes() == [route]
    finally:
        for server in servers:
            server.close()
        if stale is not None:
            stale.unlink(missing_ok=True)
        process.kill()
        process.wait()


@pytest.mark.parametrize("occupant", ["regular-file", "symlink", "loose-socket"])
def test_anchored_handover_never_removes_an_unsafe_socket_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compiled: dict[str, Path],
    occupant: str,
) -> None:
    """Anything at the generation path that fails the require_socket
    ownership invariant -- a plain file, a symlink, or a socket with loose
    mode -- is left on disk and never respawned over."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    stale: Path | None = None
    target: Path | None = None
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        stale = runtime.socket_path(root, route)

        if occupant == "regular-file":
            stale.touch()
            stale.chmod(0o600)
        elif occupant == "symlink":
            # Keep the target inside the short fixture socket dir; tmp_path
            # names can exceed the AF_UNIX bind limit.
            target = stale.with_name("fixture-target.sock")
            _dead_courier_socket(target)
            stale.symlink_to(target)
        else:
            _dead_courier_socket(stale)
            stale.chmod(0o644)

        monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: pytest.fail("respawn"))
        reused = runtime.register("claude", "studio", process.pid, str(root))
        assert reused == route
        assert stale.is_symlink() or stale.exists()
        if occupant == "symlink":
            assert target is not None and target.exists()
        assert Registry(root).routes() == [route]
    finally:
        if stale is not None:
            stale.unlink(missing_ok=True)
        if target is not None:
            target.unlink(missing_ok=True)
        process.kill()
        process.wait()


def test_courier_lock_is_exclusive_and_released_on_close(tmp_path: Path) -> None:
    """The lifetime lock can be taken once; a competing open-file-description
    is refused while it is held and succeeds only after the holder's
    descriptor closes -- the same release the kernel performs on SIGKILL."""
    lock_path = tmp_path / "held.lock"
    descriptor = runtime._acquire_courier_lock(lock_path)
    try:
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        with pytest.raises(ChatError, match="already running"):
            runtime._acquire_courier_lock(lock_path)
    finally:
        os.close(descriptor)
    released = runtime._acquire_courier_lock(lock_path)
    os.close(released)


def test_courier_lock_records_the_bound_socket_identity(tmp_path: Path) -> None:
    """The courier records the exact inode it bound into its held lock, so a
    later reclaim can attribute the stale socket to the lock's holder."""
    root = tmp_path / "state"
    root.mkdir()
    item = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
    )
    socket_file = runtime.socket_path(root, item)
    _dead_courier_socket(socket_file)
    lock_path = runtime.courier_lock_path(root, item)
    descriptor = runtime._acquire_courier_lock(lock_path)
    try:
        runtime._record_courier_socket(descriptor, socket_file)
    finally:
        os.close(descriptor)
    try:
        metadata = socket_file.lstat()
        reader = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            assert runtime._recorded_socket_identity(reader) == (
                metadata.st_dev,
                metadata.st_ino,
            )
        finally:
            os.close(reader)
        assert lock_path.read_text(encoding="ascii") == (f"{metadata.st_dev} {metadata.st_ino}\n")
    finally:
        socket_file.unlink(missing_ok=True)


@pytest.mark.parametrize("occupant", ["regular-file", "symlink", "loose-socket"])
def test_reclaim_refuses_an_unsafe_socket_under_a_released_lock(
    tmp_path: Path, occupant: str
) -> None:
    """With the lock provably free, a generation path that is not this uid's
    own 0600 socket is evidence of tampering, not of death: reclaim fails
    closed and unlinks nothing."""
    root = tmp_path / "state"
    root.mkdir()
    item = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
    )
    path = runtime.socket_path(root, item)
    lock_path = runtime.courier_lock_path(root, item)
    _dead_courier_lock(lock_path)
    target: Path | None = None
    if occupant == "regular-file":
        path.touch()
        path.chmod(0o600)
    elif occupant == "symlink":
        target = path.with_name("fixture-target.sock")
        _dead_courier_socket(target)
        path.symlink_to(target)
    else:
        _dead_courier_socket(path)
        path.chmod(0o644)

    try:
        with pytest.raises(ChatError, match="unsafe"):
            runtime._reclaim_dead_courier_socket(root, item)
        assert path.is_symlink() or path.exists()
        if target is not None:
            assert target.exists()
    finally:
        path.unlink(missing_ok=True)
        if target is not None:
            target.unlink(missing_ok=True)


def test_reclaim_releases_the_lock_so_the_replacement_can_take_it(
    tmp_path: Path,
) -> None:
    """A successful reclaim removes the stale socket and frees the lock, so
    the respawned courier's own acquisition cannot refuse it."""
    root = tmp_path / "state"
    root.mkdir()
    item = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
    )
    path = runtime.socket_path(root, item)
    lock_path = runtime.courier_lock_path(root, item)
    _dead_courier_socket(path)
    _dead_courier_lock(lock_path, path)

    try:
        assert runtime._reclaim_dead_courier_socket(root, item)
        assert not path.exists()
        replacement = runtime._acquire_courier_lock(lock_path)
        os.close(replacement)
    finally:
        path.unlink(missing_ok=True)


def test_reclaim_leaves_a_socket_whose_lock_records_another_inode(
    tmp_path: Path,
) -> None:
    """The lock names the exact inode its holder bound: a socket at the same
    path with a different inode belongs to a courier the lock never held,
    so it is left untouched."""
    root = tmp_path / "state"
    root.mkdir()
    item = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
    )
    path = runtime.socket_path(root, item)
    decoy = path.with_name("decoy.sock")
    _dead_courier_socket(path)
    _dead_courier_socket(decoy)
    _dead_courier_lock(runtime.courier_lock_path(root, item), decoy)
    decoy.unlink()
    try:
        assert not runtime._reclaim_dead_courier_socket(root, item)
        assert path.exists()
    finally:
        path.unlink(missing_ok=True)


def test_reclaim_leaves_a_socket_whose_lock_record_is_empty(
    tmp_path: Path,
) -> None:
    """An empty or unrecorded lock cannot attribute the socket to any dead
    holder, so nothing is unlinked."""
    root = tmp_path / "state"
    root.mkdir()
    item = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
    )
    path = runtime.socket_path(root, item)
    _dead_courier_socket(path)
    _dead_courier_lock(runtime.courier_lock_path(root, item))
    try:
        assert not runtime._reclaim_dead_courier_socket(root, item)
        assert path.exists()
    finally:
        path.unlink(missing_ok=True)


def test_locked_courier_inode_must_still_be_the_lock_path_entry(tmp_path: Path) -> None:
    """Once the directory entry no longer names the inode a descriptor
    locked, the lock proves nothing: a live courier could still hold the
    orphaned old inode after an unlink-and-recreate swap."""
    lock_path = tmp_path / "swapped.lock"
    _dead_courier_lock(lock_path)
    descriptor = runtime._open_courier_lock(lock_path, os.O_RDWR)
    assert descriptor is not None
    try:
        assert runtime._try_courier_lock(descriptor)
        assert runtime._lock_path_is_current(descriptor, lock_path)
        # Swap the path for a fresh inode while the old one is locked.
        lock_path.unlink()
        _dead_courier_lock(lock_path)
        assert not runtime._lock_path_is_current(descriptor, lock_path)
    finally:
        os.close(descriptor)


def test_reclaim_refuses_a_lock_inode_the_path_no_longer_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a lock recording this exact socket must not reclaim it once the
    locked inode was orphaned by a path swap: the flock was taken on an
    inode no directory entry names."""
    root = tmp_path / "state"
    root.mkdir()
    item = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
    )
    path = runtime.socket_path(root, item)
    lock_path = runtime.courier_lock_path(root, item)
    _dead_courier_socket(path)
    _dead_courier_lock(lock_path, path)

    real_open = runtime._open_courier_lock

    def swapped_open(lock: Path, flags: int) -> int | None:
        descriptor = real_open(lock, flags)
        # The path is recreated between open and flock: the descriptor locks
        # the orphaned inode even though it still records this socket.
        lock.unlink()
        _dead_courier_lock(lock)
        return descriptor

    monkeypatch.setattr(runtime, "_open_courier_lock", swapped_open)
    try:
        assert not runtime._reclaim_dead_courier_socket(root, item)
        assert path.exists()
    finally:
        path.unlink(missing_ok=True)


def test_reclaim_never_touches_a_socket_while_the_lock_is_held(
    tmp_path: Path,
) -> None:
    """A held lifetime lock means a live courier: reclaim returns False and
    the refused socket stays exactly where the courier left it."""
    root = tmp_path / "state"
    root.mkdir()
    item = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
    )
    path = runtime.socket_path(root, item)
    _dead_courier_socket(path)
    descriptor = _held_courier_lock(runtime.courier_lock_path(root, item))
    try:
        assert not runtime._reclaim_dead_courier_socket(root, item)
        assert path.exists()
    finally:
        os.close(descriptor)
        path.unlink(missing_ok=True)


def test_reclaim_returns_false_without_a_lock_file(tmp_path: Path) -> None:
    """A pre-version courier holds no lock: its stale socket is unprovable
    and left in place, never unlinked and never spawned over."""
    root = tmp_path / "state"
    root.mkdir()
    item = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
    )
    path = runtime.socket_path(root, item)
    _dead_courier_socket(path)
    try:
        assert not runtime._reclaim_dead_courier_socket(root, item)
        assert path.exists()
    finally:
        path.unlink(missing_ok=True)


def test_reclaim_tolerates_a_socket_vanished_under_the_taken_lock(
    tmp_path: Path,
) -> None:
    """If the stale socket disappears between the refused connect and the
    reclaim, the freed path is still safe to spawn on."""
    root = tmp_path / "state"
    root.mkdir()
    item = Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=os.getpid(),
        owner_identity="a" * 64,
    )
    _dead_courier_lock(runtime.courier_lock_path(root, item))
    assert runtime._reclaim_dead_courier_socket(root, item)


def test_anchored_reregistration_without_a_current_owner_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """The stale-socket removal never weakens ownership: when the anchor no
    longer proves the route the registration still refuses, and the leftover
    socket is nobody's to delete."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    stale: Path | None = None
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)
        stale = runtime.socket_path(root, route)
        _dead_courier_socket(stale)
        owner_anchor_path(root, route.generation).unlink()

        monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: pytest.fail("respawn"))
        with pytest.raises(ChatError):
            runtime.register("claude", "studio", process.pid, str(root))
        assert stale.exists()
    finally:
        if stale is not None:
            stale.unlink(missing_ok=True)
        process.kill()
        process.wait()


def _codex_route(root: Path, session_id: str, cwd: Path) -> Route:
    identity, _ = runtime.recipient_owner_identity("codex", os.getpid())
    route = Route.create(
        provider="codex",
        session_id=session_id,
        device="studio",
        cwd=str(cwd),
        pid=os.getpid(),
        owner_identity=identity,
    )
    Registry(root).upsert(route)
    return route


def test_second_reuse_keeps_the_route_while_the_courier_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unanchored reuse path must not mint a new generation over a live
    courier: a full-backlog ECONNREFUSED while the courier's lifetime lock
    is held keeps the registered route exactly as published."""
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    first = _codex_route(root, session_id, cwd)
    occupied = runtime.socket_path(root, first)
    listener, backlog = _backlogged_listener(occupied)
    lock_descriptor = _held_courier_lock(runtime.courier_lock_path(root, first))
    try:
        monkeypatch.setattr(
            runtime,
            "hook_input",
            lambda _event: {"session_id": session_id, "cwd": str(cwd)},
        )
        monkeypatch.setattr(runtime, "_spawn_courier", lambda *_args: pytest.fail("respawn"))
        reused = runtime.register("codex", "studio", os.getpid(), str(root))
        assert reused == first
        assert occupied.exists()
        assert Registry(root).routes() == [first]
    finally:
        os.close(lock_descriptor)
        backlog.close()
        listener.close()
        occupied.unlink(missing_ok=True)


def test_second_reuse_mints_again_when_the_registered_courier_has_no_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered route whose courier predates the lifetime lock keeps the
    existing recovery: a refused connect mints a replacement generation and
    spawns a courier for it."""
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    session_id = str(uuid4())
    first = _codex_route(root, session_id, cwd)
    occupied = runtime.socket_path(root, first)
    _dead_courier_socket(occupied)
    spawned: list[Route] = []
    try:
        monkeypatch.setattr(
            runtime,
            "hook_input",
            lambda _event: {"session_id": session_id, "cwd": str(cwd)},
        )
        monkeypatch.setattr(runtime, "_spawn_courier", lambda _root, item: spawned.append(item))
        reused = runtime.register("codex", "studio", os.getpid(), str(root))
        assert reused is not None
        assert reused.generation != first.generation
        assert spawned == [reused]
        assert Registry(root).routes() == [reused]
        assert occupied.exists()
    finally:
        occupied.unlink(missing_ok=True)
