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


def test_anchored_reregistration_applies_cwd_drift_keeping_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compiled: dict[str, Path]
) -> None:
    """Hook reporting a new cwd runs the same courier-verified drift logic:
    the route updates cwd but keeps the anchored generation."""
    package = tmp_path / "pkg"
    binary = _install(compiled["sleeper"], package)
    root = tmp_path / "state"
    cwd = tmp_path / "project"
    cwd.mkdir()
    moved = tmp_path / "moved"
    moved.mkdir()
    session_id = str(uuid4())
    process = _spawn(binary)
    try:
        route = _register(root, "claude", process.pid, cwd, monkeypatch, session_id)
        aside = _npm_update_aside(package)
        shutil.rmtree(aside)

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
        spawned: list[Route] = []
        monkeypatch.setattr(runtime, "_spawn_courier", lambda _root, item: spawned.append(item))

        reused = runtime.register("claude", "studio", process.pid, str(root))
        assert reused is not None
        assert reused.generation == route.generation
        assert reused.owner_identity == route.owner_identity
        assert reused.cwd == str(moved.resolve())
        assert spawned == [reused]
        assert Registry(root).routes() == [reused]
        assert runtime._anchored_owner_current(root, reused)
    finally:
        process.kill()
        process.wait()
