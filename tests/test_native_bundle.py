from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import ChatError, Route

OWNER = "a" * 64


def fake_desktop_bundle(parent: Path, identifier: str = "com.openai.codex") -> Path:
    """Build a minimal fixture ChatGPT.app tree under one install parent."""

    bundle = parent / "ChatGPT.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    (bundle / "Contents" / "MacOS" / "ChatGPT").write_text("desktop", encoding="utf-8")
    (bundle / "Contents" / "Resources" / "codex").write_text("child", encoding="utf-8")
    (bundle / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": identifier})
    )
    return bundle


def patch_process_chain(
    monkeypatch: pytest.MonkeyPatch,
    executables: dict[int, Path],
    parents: dict[int, int],
) -> None:
    """Bind pids to executable paths and parents for the bounded ancestor walk."""

    def identity(_provider: str, pid: int, *_args: object) -> tuple[str, Path]:
        if pid not in executables:
            raise ChatError("provider process identity is unavailable")
        return OWNER, executables[pid]

    def ps(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0, f"{parents.get(int(command[-1]), int(command[-1]))}\n", ""
        )

    monkeypatch.setattr(runtime, "recipient_owner_identity", identity)
    monkeypatch.setattr("cross_agent_chat.runtime.subprocess.run", ps)


def codex_route(tmp_path: Path, pid: int) -> Route:
    profile = tmp_path / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    return Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(tmp_path),
        pid=pid,
        owner_identity=OWNER,
        profile_root=str(profile),
    )


def standard_chain(bundle: Path) -> tuple[dict[int, Path], dict[int, int]]:
    return (
        {
            300: bundle / "Contents" / "Resources" / "codex",
            200: bundle / "Contents" / "MacOS" / "ChatGPT",
            100: Path("/sbin/launchd"),
        },
        {300: 200, 200: 100},
    )


def test_system_applications_layout_is_recognized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "Applications"
    bundle = fake_desktop_bundle(applications)
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    executables, parents = standard_chain(bundle)
    patch_process_chain(monkeypatch, executables, parents)

    resolved = bundle.resolve(strict=True)
    assert runtime.native_desktop_bundle(300) == resolved
    assert runtime.native_desktop_process(300)
    assert runtime._native_desktop_route(tmp_path, codex_route(tmp_path, 300))


def test_home_applications_layout_is_recognized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = fake_desktop_bundle(tmp_path / "home" / "Applications")
    executables, parents = standard_chain(bundle)
    patch_process_chain(monkeypatch, executables, parents)

    assert runtime.native_desktop_bundle(300) == bundle.resolve(strict=True)
    assert runtime._native_desktop_route(tmp_path, codex_route(tmp_path, 300))


def test_applications_subfolder_layout_is_recognized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "Applications"
    bundle = fake_desktop_bundle(applications / "Utilities")
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    executables, parents = standard_chain(bundle)
    patch_process_chain(monkeypatch, executables, parents)

    assert runtime.native_desktop_bundle(300) == bundle.resolve(strict=True)


@pytest.mark.parametrize("parent", ["home/Downloads", "home/Applications/Sub", "Applications/X/Y"])
def test_unsupported_install_locations_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent: str
) -> None:
    applications = tmp_path / "Applications"
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    bundle = fake_desktop_bundle(tmp_path / parent)
    executables, parents = standard_chain(bundle)
    patch_process_chain(monkeypatch, executables, parents)

    assert runtime.native_desktop_bundle(300) is None
    assert not runtime.native_desktop_process(300)


def test_child_and_desktop_ancestor_must_share_one_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "Applications"
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    child_bundle = fake_desktop_bundle(applications)
    ancestor_bundle = fake_desktop_bundle(tmp_path / "home" / "Applications")
    patch_process_chain(
        monkeypatch,
        {
            300: child_bundle / "Contents" / "Resources" / "codex",
            200: ancestor_bundle / "Contents" / "MacOS" / "ChatGPT",
            100: Path("/sbin/launchd"),
        },
        {300: 200, 200: 100},
    )

    assert runtime.native_desktop_process(300)
    assert runtime.native_desktop_bundle(300) is None
    assert not runtime._native_desktop_route(tmp_path, codex_route(tmp_path, 300))


def test_foreign_bundle_identifier_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "Applications"
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    bundle = fake_desktop_bundle(applications, identifier="com.example.clone")
    executables, parents = standard_chain(bundle)
    patch_process_chain(monkeypatch, executables, parents)

    assert runtime.native_desktop_bundle(300) is None
    assert not runtime.native_desktop_process(300)


@pytest.mark.parametrize("variant", ["missing", "unparseable"])
def test_missing_or_unparseable_info_plist_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    applications = tmp_path / "Applications"
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    bundle = fake_desktop_bundle(applications)
    info = bundle / "Contents" / "Info.plist"
    if variant == "missing":
        info.unlink()
    else:
        info.write_bytes(b"not a property list")
    executables, parents = standard_chain(bundle)
    patch_process_chain(monkeypatch, executables, parents)

    assert runtime.native_desktop_bundle(300) is None
    assert not runtime.native_desktop_process(300)


def test_non_bundle_codex_executable_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "Applications"
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    bundle = fake_desktop_bundle(applications)
    patch_process_chain(
        monkeypatch,
        {
            300: Path("/usr/local/bin/codex"),
            200: bundle / "Contents" / "MacOS" / "ChatGPT",
            100: Path("/sbin/launchd"),
        },
        {300: 200, 200: 100},
    )

    assert runtime.native_desktop_bundle(300) is None
    assert runtime.native_desktop_process(300)


def test_desktop_binary_itself_is_not_a_codex_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "Applications"
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    bundle = fake_desktop_bundle(applications)
    patch_process_chain(
        monkeypatch,
        {200: bundle / "Contents" / "MacOS" / "ChatGPT", 100: Path("/sbin/launchd")},
        {200: 100},
    )

    assert runtime.native_desktop_bundle(200) is None


def test_desktop_ancestor_beyond_eight_hops_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "Applications"
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    bundle = fake_desktop_bundle(applications)
    executables = {pid: Path("/usr/libexec/helper") for pid in range(300, 309)}
    executables[300] = bundle / "Contents" / "Resources" / "codex"
    executables[309] = bundle / "Contents" / "MacOS" / "ChatGPT"
    patch_process_chain(monkeypatch, executables, {pid: pid + 1 for pid in range(300, 309)})

    assert not runtime.native_desktop_process(300)
    assert runtime.native_desktop_bundle(300) is None


def test_account_binary_returns_the_bound_bundle_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "Applications"
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    bundle = fake_desktop_bundle(applications / "Utilities")
    executables, parents = standard_chain(bundle)
    patch_process_chain(monkeypatch, executables, parents)
    route = codex_route(tmp_path, 300)

    assert runtime._native_account_binary(tmp_path, route) == (
        bundle / "Contents" / "Resources" / "codex"
    ).resolve(strict=True)


def test_account_binary_rejects_route_bound_to_another_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "Applications"
    monkeypatch.setattr(runtime, "NATIVE_DESKTOP_APPLICATIONS", applications)
    bundle = fake_desktop_bundle(applications)
    executables, parents = standard_chain(bundle)
    patch_process_chain(monkeypatch, executables, parents)
    route = codex_route(tmp_path, 300)
    monkeypatch.setattr(
        runtime, "recipient_owner_identity", lambda *_args: ("b" * 64, executables[300])
    )

    with pytest.raises(ChatError, match="account identity is unavailable"):
        runtime._native_account_binary(tmp_path, route)
