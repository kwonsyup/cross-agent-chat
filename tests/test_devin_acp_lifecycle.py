"""Contained lifecycle regressions for the ACP-hosted Devin adapter.

The adapter owns a real engine child: every exit path must drain accepted
input, boundedly terminate, then kill and reap it, and no path may close the
buffered engine stdin while the writer can still own its lock. The private
attestation carries a freshness lease that is renewed from the current
nonclosing membership and must expire when renewal can neither rewrite nor
remove it.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import IO, cast

import pytest
from test_devin_acp import Side

from cross_agent_chat import devin, devin_acp
from cross_agent_chat.core import ChatError, Route
from cross_agent_chat.devin_acp import Mediator

SOURCE = Path(__file__).parents[1] / "src"
SESSION = "sess-live1"
Frame = dict[str, object]


def _until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- real adapter subprocess against a fixture-owned engine ------------------


def _fake_engine(tmp_path: Path, body: str) -> Path:
    """Write a fixture-owned executable named ``devin`` the adapter may run."""

    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    script = directory / "devin"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(0o700)
    return directory


def _adapter_process(
    tmp_path: Path,
    engine_body: str,
    *,
    state_root: str | None = None,
    stdin: int | None = subprocess.PIPE,
) -> subprocess.Popen[bytes]:
    """Launch the real ``devin-acp`` command against a fixture engine."""

    bindir = _fake_engine(tmp_path, engine_body)
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    command = [
        sys.executable,
        "-m",
        "cross_agent_chat.cli",
        "devin-acp",
        "--devin",
        str(bindir / "devin"),
        "--state-root",
        state_root if state_root is not None else str(root),
    ]
    proc = subprocess.Popen(
        command,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=tmp_path,
    )
    return proc


def _feed(proc: subprocess.Popen[bytes], payload: bytes) -> None:
    """Write host frames then close stdin, without ever blocking the test."""

    def send() -> None:
        assert proc.stdin is not None
        with suppress(BrokenPipeError, OSError, ValueError):
            proc.stdin.write(payload)
            proc.stdin.flush()
        with suppress(OSError, ValueError):
            proc.stdin.close()

    threading.Thread(target=send, daemon=True).start()


def _engine_pid(path: Path, timeout: float = 15.0) -> int:
    assert _until(path.exists, timeout), "fixture engine did not report its pid"
    return int(path.read_text().strip())


def _assert_dead(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def _finish(proc: subprocess.Popen[bytes], engine_pid: int) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    with suppress(ProcessLookupError):
        os.kill(engine_pid, signal.SIGKILL)


def _stubborn_engine(tmp_path: Path) -> str:
    pid_file = tmp_path / "engine.pid"
    return (
        "import os, pathlib, signal, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(3600)\n"
    )


def _echo_engine(tmp_path: Path) -> str:
    pid_file = tmp_path / "engine.pid"
    return (
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        request = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    if not isinstance(request, dict) or 'id' not in request:\n"
        "        continue\n"
        "    new = request.get('method') == 'session/new'\n"
        "    result = {'sessionId': 'sess-live1'} if new else {}\n"
        "    reply = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}\n"
        "    sys.stdout.write(json.dumps(reply) + '\\n')\n"
        "    sys.stdout.flush()\n"
    )


def test_full_pipe_engine_ignoring_sigterm_is_killed_and_reaped(tmp_path: Path) -> None:
    """Writer wedged on a real full pipe: shutdown kills the engine, unblocks
    the writer, and never closes buffered stdin under it."""
    proc = _adapter_process(tmp_path, _stubborn_engine(tmp_path))
    pid = _engine_pid(tmp_path / "engine.pid")
    try:
        # One oversized frame cannot drain through a pipe the engine never reads.
        _feed(proc, b"x" * (4 << 20) + b"\n")
        assert proc.wait(timeout=45) == 1
        _assert_dead(pid)
    finally:
        _finish(proc, pid)


def test_drained_writer_engine_ignoring_sigterm_is_killed_and_reaped(tmp_path: Path) -> None:
    """Writer drained but the engine stays alive past SIGTERM: the engine wait
    must still terminate, kill and reap inside the bound."""
    proc = _adapter_process(tmp_path, _stubborn_engine(tmp_path))
    pid = _engine_pid(tmp_path / "engine.pid")
    try:
        _feed(proc, b"{}\n")
        assert proc.wait(timeout=45) == 1
        _assert_dead(pid)
    finally:
        _finish(proc, pid)


def test_inherited_read_end_cannot_keep_the_engine_writer_alive(tmp_path: Path) -> None:
    """Counterexample for the writer contract: a fixture grandchild inherits
    the pipe's read end and survives the engine's SIGKILL, so the wedged write
    can never fail with EPIPE. The writer must still end boundedly."""
    grandchild_file = tmp_path / "grandchild.pid"
    engine = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, pathlib, signal, subprocess, sys, time\n"
            f"pathlib.Path({str(tmp_path / 'engine.pid')!r}).write_text(str(os.getpid()))\n"
            "child = subprocess.Popen("
            f"[sys.executable, '-c', 'import time; time.sleep(3600)'])\n"
            f"pathlib.Path({str(grandchild_file)!r}).write_text(str(child.pid))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(3600)\n",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert engine.stdin is not None and engine.stdout is not None
    host_r, _host_w = os.pipe()
    _out_r, out_w = os.pipe()
    mediator = Mediator(
        host_in=os.fdopen(host_r, "rb"),
        host_out=os.fdopen(out_w, "wb"),
        engine_in=engine.stdin,
        engine_out=engine.stdout,
        inbox=None,
        diagnostic=lambda _text: None,
    )
    mediator.start(deliver=False)
    result: list[int] = []
    try:
        assert _engine_pid(tmp_path / "engine.pid")
        grandchild = _engine_pid(grandchild_file)
        # Fill the real pipe so the writer stalls on a read end that stays open.
        assert mediator._to_engine(b"x" * (4 << 20))
        assert not mediator.drain(1.0), "writer unexpectedly finished a wedged write"
        done = threading.Event()

        def shutdown() -> None:
            result.append(
                devin_acp._stop_owned_engine(mediator, engine, diagnostic=lambda _text: None)
            )
            done.set()

        threading.Thread(target=shutdown, daemon=True).start()
        assert done.wait(45), "shutdown never returned"
        assert result == [1]
        assert engine.poll() == -signal.SIGKILL
        assert mediator.writer_done(), "the engine writer outlived bounded shutdown"
        with suppress(ProcessLookupError):
            os.kill(grandchild, signal.SIGKILL)
    finally:
        if engine.poll() is None:
            engine.kill()
            engine.wait()
        mediator.close()
        if grandchild_file.exists():
            with suppress(ProcessLookupError, OSError):
                os.kill(int(grandchild_file.read_text().strip()), signal.SIGKILL)


def test_host_eof_lets_a_cooperative_engine_exit_cleanly(tmp_path: Path) -> None:
    """Ordinary shutdown: accepted frames drain, the engine exits on stdin EOF,
    the attestation is removed and the child is reaped."""
    proc = _adapter_process(tmp_path, _echo_engine(tmp_path))
    pid = _engine_pid(tmp_path / "engine.pid")
    try:
        _feed(proc, b"{}\n")
        assert proc.wait(timeout=45) == 0
        _assert_dead(pid)
        assert list((tmp_path / "state").glob("devin-acp-host-*")) == []
    finally:
        _finish(proc, pid)


def test_startup_failure_still_reaps_the_owned_engine(tmp_path: Path) -> None:
    """An error after the child is launched must not leak it: a relative state
    root aborts startup and the owned engine is still ended."""
    proc = _adapter_process(
        tmp_path,
        _stubborn_engine(tmp_path),
        state_root="relative/not-absolute",
        stdin=subprocess.DEVNULL,
    )
    pid = _engine_pid(tmp_path / "engine.pid")
    try:
        assert proc.wait(timeout=45) == 2
        _assert_dead(pid)
    finally:
        _finish(proc, pid)


def test_attestation_tracks_sessions_and_dies_with_the_adapter(tmp_path: Path) -> None:
    """The record publishes on session open, drops the closed session, and is
    removed at shutdown; the engine is reaped."""
    proc = _adapter_process(tmp_path, _echo_engine(tmp_path))
    pid = _engine_pid(tmp_path / "engine.pid")
    assert proc.stdin is not None and proc.stdout is not None
    root = tmp_path / "state"
    route = cast(
        Route,
        type("R", (), {"provider": "devin", "pid": pid, "session_id": SESSION}),
    )
    frames: queue.Queue[Frame] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            with suppress(json.JSONDecodeError, UnicodeDecodeError):
                frames.put(json.loads(raw))

    threading.Thread(target=pump, daemon=True).start()

    def record() -> dict[str, object] | None:
        records = list(root.glob("devin-acp-host-*"))
        return json.loads(records[0].read_text()) if records else None

    def sessions() -> object:
        current = record()
        return current["sessions"] if current is not None else None

    try:
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}}).encode()
            + b"\n"
        )
        proc.stdin.flush()
        assert frames.get(timeout=15) == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"sessionId": SESSION},
        }
        assert _until(lambda: sessions() == [SESSION], 15)
        assert devin.acp_host_observed(root, route)
        current = record()
        assert current is not None
        assert isinstance(current["renewed_at"], (int, float))
        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/close",
                    "params": {"sessionId": SESSION},
                }
            ).encode()
            + b"\n"
        )
        proc.stdin.flush()
        assert _until(lambda: sessions() == [], 15)
        assert not devin.acp_host_observed(root, route)
        _feed(proc, b"")
        assert proc.wait(timeout=45) == 0
        _assert_dead(pid)
        assert list(root.glob("devin-acp-host-*")) == []
    finally:
        _finish(proc, pid)


# -- renewal of the freshness lease under the session lock --------------------


@contextmanager
def _renewing_mediator(
    on_sessions: Callable[[frozenset[str]], None], renew_seconds: float = 0.05
) -> Iterator[tuple[Mediator, Side, Side]]:
    host_to_med_r, host_to_med_w = os.pipe()
    med_to_host_r, med_to_host_w = os.pipe()
    med_to_engine_r, med_to_engine_w = os.pipe()
    engine_to_med_r, engine_to_med_w = os.pipe()
    host_writer = os.fdopen(host_to_med_w, "wb")
    engine_writer = os.fdopen(engine_to_med_w, "wb")
    streams: list[IO[bytes]] = [
        os.fdopen(host_to_med_r, "rb"),
        os.fdopen(med_to_host_w, "wb"),
        os.fdopen(med_to_engine_w, "wb"),
        os.fdopen(engine_to_med_r, "rb"),
    ]
    mediator = Mediator(
        host_in=streams[0],
        host_out=streams[1],
        engine_in=streams[2],
        engine_out=streams[3],
        inbox=None,
        on_sessions=on_sessions,
        renew_seconds=renew_seconds,
        diagnostic=lambda _text: None,
    )
    mediator.start(deliver=False)
    host = Side(os.fdopen(med_to_host_r, "rb"), host_writer)
    engine = Side(os.fdopen(med_to_engine_r, "rb"), engine_writer)
    try:
        yield mediator, host, engine
    finally:
        host_writer.close()
        engine_writer.close()
        mediator.close()
        for stream in streams:
            with suppress(OSError):
                stream.close()


def _new_session(host: Side, engine: Side, session_id: str, host_id: object = 1) -> None:
    host.send({"jsonrpc": "2.0", "id": host_id, "method": "session/new", "params": {}})
    request = engine.recv()
    assert request["method"] == "session/new"
    engine.send({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": session_id}})
    assert host.recv() == {
        "jsonrpc": "2.0",
        "id": host_id,
        "result": {"sessionId": session_id},
    }


def test_renewal_republishes_current_nonclosing_membership_until_stopped() -> None:
    """Renewal fires without model activity, follows membership, drops a
    closing session, and stops entirely at shutdown."""
    published: list[frozenset[str]] = []
    with _renewing_mediator(published.append) as (mediator, host, engine):
        assert _until(lambda: bool(published)), "idle renewal never published"
        _new_session(host, engine, "sess-alpha1")
        assert _until(lambda: published[-1] == frozenset({"sess-alpha1"}))
        host.send(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "session/close",
                "params": {"sessionId": "sess-alpha1"},
            }
        )
        request = engine.recv()
        assert request["method"] == "session/close"
        # Before the engine even confirms, renewal reports only nonclosing sessions.
        assert _until(lambda: published[-1] == frozenset())
        engine.send({"jsonrpc": "2.0", "id": request["id"], "result": {}})
        assert host.recv() == {"jsonrpc": "2.0", "id": 9, "result": {}}
        time.sleep(0.15)
        assert published[-1] == frozenset()
        mediator.close()
        count = len(published)
        time.sleep(0.25)
        assert len(published) == count, "renewal kept publishing after stop"


def test_a_publish_in_flight_cannot_resurrect_a_closed_session() -> None:
    """Publication holds the session lock: a renewal that captured the old
    membership lands before the close's publish, never after it."""
    published: list[frozenset[str]] = []
    entered, release, armed = threading.Event(), threading.Event(), threading.Event()

    def publish(sessions: frozenset[str]) -> None:
        published.append(sessions)
        if armed.is_set():
            armed.clear()
            entered.set()
            release.wait(10)

    with _renewing_mediator(publish) as (_mediator, host, engine):
        try:
            _new_session(host, engine, "sess-alpha1")
            assert _until(lambda: frozenset({"sess-alpha1"}) in published)
            armed.set()
            # The next renewal blocks mid-write holding the session lock.
            assert entered.wait(10)
            host.send(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "session/close",
                    "params": {"sessionId": "sess-alpha1"},
                }
            )
            # The close is queued behind the in-flight publish on the session
            # lock; releasing lets the stale publish land and the close proceed.
            release.set()
            request = engine.recv()
            engine.send({"jsonrpc": "2.0", "id": request["id"], "result": {}})
            assert host.recv() == {"jsonrpc": "2.0", "id": 9, "result": {}}
            assert _until(lambda: published[-1] == frozenset())
            time.sleep(0.15)
            # Once the session was gone from one published set, it never reappears.
            snapshot = list(published)
            joined = next(i for i, call in enumerate(snapshot) if "sess-alpha1" in call)
            first_drop = snapshot.index(frozenset(), joined)
            assert all("sess-alpha1" not in call for call in snapshot[first_drop:]), (
                "a renewal resurrected a closed session"
            )
            assert snapshot[-1] == frozenset()
        finally:
            release.set()


def test_healthy_idle_renewal_keeps_a_real_attestation_fresh(tmp_path: Path) -> None:
    """With no model or host activity, renewal rewrites the record's lease and
    hosted qualification stays true."""
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    attestations: list[Path] = []

    def attest(sessions: frozenset[str]) -> None:
        attestations.append(
            devin.write_acp_host(
                root,
                adapter_pid=os.getpid(),
                engine_pid=child.pid,
                sessions=sessions,
            )
        )

    def renewed() -> object:
        return json.loads(attestations[0].read_text()).get("renewed_at") if attestations else None

    try:
        with _renewing_mediator(attest) as (_mediator, host, engine):
            _new_session(host, engine, "sess-alpha1")
            route = cast(
                Route,
                type(
                    "R",
                    (),
                    {"provider": "devin", "pid": child.pid, "session_id": "sess-alpha1"},
                ),
            )
            assert _until(lambda: devin.acp_host_observed(root, route))
            first = renewed()
            assert isinstance(first, (int, float))
            assert _until(lambda: renewed() != first, 5), "idle renewal never refreshed"
            assert devin.acp_host_observed(root, route)
    finally:
        child.kill()
        child.wait()


# -- the lease refuses bad evidence and expires on failure --------------------


@pytest.fixture
def owned_engine(tmp_path: Path) -> Iterator[tuple[Path, subprocess.Popen[bytes], Route]]:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    route = cast(
        Route,
        type("R", (), {"provider": "devin", "pid": child.pid, "session_id": "sess-alpha1"}),
    )
    yield root, child, route
    child.kill()
    child.wait()


def _controlled_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [1_700_000_000.0]
    monkeypatch.setattr(devin, "time", SimpleNamespace(time=lambda: clock[0]))
    return clock


def test_freshness_lease_refuses_stale_future_and_malformed_records(
    owned_engine: tuple[Path, subprocess.Popen[bytes], Route],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, child, route = owned_engine
    clock = _controlled_clock(monkeypatch)
    path = devin.write_acp_host(
        root, adapter_pid=os.getpid(), engine_pid=child.pid, sessions=frozenset({"sess-alpha1"})
    )
    assert devin.acp_host_observed(root, route)
    # Fresh until the lease lapses, then refused without needing the adapter to act.
    clock[0] += devin.ACP_HOST_TTL_SECONDS - 5
    assert devin.acp_host_observed(root, route)
    clock[0] += 10
    assert not devin.acp_host_observed(root, route)
    record = json.loads(path.read_text())
    for bad in (
        clock[0] + 60,
        float("nan"),
        float("inf"),
        float("-inf"),
        "soon",
        None,
        True,
        -3,
        10**1000,
        -(10**1000),
        1e300,
    ):
        record["renewed_at"] = bad
        path.write_text(json.dumps(record))
        assert not devin.acp_host_observed(root, route), bad
    del record["renewed_at"]
    path.write_text(json.dumps(record))
    assert not devin.acp_host_observed(root, route)
    record["renewed_at"] = clock[0]
    path.write_text(json.dumps(record))
    assert devin.acp_host_observed(root, route)


def test_failed_rewrite_and_failed_unlink_expire_within_the_lease(
    owned_engine: tuple[Path, subprocess.Popen[bytes], Route],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, child, route = owned_engine
    clock = _controlled_clock(monkeypatch)
    path = devin.write_acp_host(
        root, adapter_pid=os.getpid(), engine_pid=child.pid, sessions=frozenset({"sess-alpha1"})
    )
    assert devin.acp_host_observed(root, route)

    def failing_write(*_args: object, **_kwargs: object) -> Path:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(devin_acp, "write_acp_host", failing_write)
    original_unlink = Path.unlink

    def guarded_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == path:
            raise OSError("unlink denied")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)
    with pytest.raises(OSError):
        devin_acp._republish_attestation(
            root,
            path,
            adapter_pid=os.getpid(),
            engine_pid=child.pid,
            sessions=frozenset({"sess-alpha1"}),
        )
    # The stale record stays readable but only inside the lease window.
    assert devin.acp_host_observed(root, route)
    clock[0] += devin.ACP_HOST_TTL_SECONDS + 1
    assert not devin.acp_host_observed(root, route)


def test_failed_rewrite_removes_the_record_when_unlink_works(
    owned_engine: tuple[Path, subprocess.Popen[bytes], Route],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, child, route = owned_engine
    path = devin.write_acp_host(
        root, adapter_pid=os.getpid(), engine_pid=child.pid, sessions=frozenset({"sess-alpha1"})
    )

    def failing_write(*_args: object, **_kwargs: object) -> Path:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(devin_acp, "write_acp_host", failing_write)
    with pytest.raises(OSError):
        devin_acp._republish_attestation(
            root,
            path,
            adapter_pid=os.getpid(),
            engine_pid=child.pid,
            sessions=frozenset({"sess-alpha1"}),
        )
    assert not path.exists()
    assert not devin.acp_host_observed(root, route)


@pytest.mark.live
def test_live_adapter_lifecycle_is_bounded(tmp_path: Path) -> None:
    """Post-fix live check: the real installed ``devin acp`` exits bounded when
    the host goes away. Runs only with ``-m live``."""
    try:
        binary = devin.devin_binary()
    except ChatError:
        pytest.skip("Devin CLI is unavailable")
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cross_agent_chat.cli",
            "devin-acp",
            "--devin",
            str(binary),
            "--state-root",
            str(root),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.close()
        assert proc.wait(timeout=60) in {0, 1}
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_sigterm_during_initialization_still_reaps_the_owned_engine(tmp_path: Path) -> None:
    bindir = _fake_engine(tmp_path, _stubborn_engine(tmp_path))
    marker = tmp_path / "initializing"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    driver = (
        "import pathlib,time\n"
        "from cross_agent_chat import runtime,devin_acp\n"
        "def hold_setup(value):\n"
        f"    pathlib.Path({str(marker)!r}).touch()\n"
        "    time.sleep(3600)\n"
        "runtime.state_root=hold_setup\n"
        "devin_acp.run(state_root_value=None,"
        f"devin=pathlib.Path({str(bindir / 'devin')!r}),engine_args=[])\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", driver],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=tmp_path,
    )
    pid = _engine_pid(tmp_path / "engine.pid")
    try:
        assert _until(marker.exists)
        proc.terminate()
        assert proc.wait(timeout=30) == 143
        _assert_dead(pid)
    finally:
        _finish(proc, pid)
