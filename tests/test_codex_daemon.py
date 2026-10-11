"""Provider-owned original input; no substitute runtime or uncertain-effect retry."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import pytest

from cross_agent_chat import codex_daemon, runtime
from cross_agent_chat.codex import CodexCourier
from cross_agent_chat.codex_daemon import CodexDaemonIngress
from cross_agent_chat.core import ChatError, Registry, Route, UnknownDeliveryError
from cross_agent_chat.native_helper import (
    NATIVE_QUEUE_BINARY_ENV_VAR,
    NATIVE_QUEUE_ENV_VALUE,
    NATIVE_QUEUE_ENV_VAR,
)
from cross_agent_chat.runtime import request_socket


@contextmanager
def provider(
    tmp_path: Path,
    *,
    active: bool = False,
    reply: Callable[[dict[str, Any]], object] | None = None,
    profile_changed: bool = False,
) -> Iterator[tuple[CodexDaemonIngress, list[dict[str, Any]], str]]:
    root = tmp_path / "profile"
    root.mkdir(mode=0o700)
    socket_dir = root / "app-server-control"
    socket_dir.mkdir(mode=0o700)
    endpoint = socket_dir / "app-server-control.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    short = TemporaryDirectory(prefix="cacd-", dir="/tmp")
    physical = Path(short.name) / "s"
    server.bind(str(physical))
    physical.chmod(0o600)
    endpoint.symlink_to(physical)
    server.listen(4)
    server.settimeout(0.1)
    route = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="test",
        cwd=str(tmp_path),
        pid=os.getpid(),
        profile_root=str(root),
        owner_identity="a" * 64,
    )
    turn = str(uuid4())
    calls: list[dict[str, Any]] = []
    stopping = threading.Event()
    failures: list[Exception] = []

    def serve() -> None:
        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(2)
                buf = b""

                def exact(size: int, conn: socket.socket = conn) -> bytes:
                    nonlocal buf
                    while len(buf) < size:
                        b = conn.recv(65536)
                        if not b:
                            raise EOFError()
                        buf += b
                    b, buf = buf[:size], buf[size:]
                    return b

                def send(value: object, conn: socket.socket = conn) -> None:
                    b = json.dumps(value).encode()
                    header = (
                        bytes([129, len(b)])
                        if len(b) < 126
                        else b"\x81\x7e" + struct.pack("!H", len(b))
                    )
                    conn.sendall(header + b)

                try:
                    while b"\r\n\r\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            raise EOFError()
                        buf += chunk
                    header, buf = buf.split(b"\r\n\r\n", 1)
                    fields = dict(line.split(b": ", 1) for line in header.split(b"\r\n")[1:])
                    key = fields[b"Sec-WebSocket-Key"]
                    accept = base64.b64encode(
                        hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
                    )
                    conn.sendall(
                        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                        b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                    )
                    while True:
                        flags, size = exact(2)
                        assert flags == 129 and size & 128
                        size &= 127
                        if size == 126:
                            size = struct.unpack("!H", exact(2))[0]
                        elif size == 127:
                            size = struct.unpack("!Q", exact(8))[0]
                        mask, b = exact(4), exact(size)
                        request = json.loads(bytes(x ^ mask[i % 4] for i, x in enumerate(b)))
                        calls.append(request)
                        method = request["method"]
                        if method == "initialized":
                            continue
                        params = request["params"]
                        if method == "initialize":
                            result: dict[str, object] = {
                                "codexHome": str(root if not profile_changed else tmp_path)
                            }
                        elif method == "thread/read":
                            assert params == {"threadId": route.session_id, "includeTurns": False}
                            result = {
                                "thread": {
                                    "id": route.session_id,
                                    "cwd": route.cwd,
                                    "canAcceptDirectInput": True,
                                    "originator": "codex-tui",
                                    "status": {"type": "active" if active else "idle"},
                                }
                            }
                        elif method == "thread/turns/list":
                            assert params["itemsView"] == "notLoaded"
                            result = {"data": [{"id": turn, "status": "inProgress", "items": []}]}
                        else:
                            if reply is not None:
                                response = reply(request)
                                if response is None:
                                    break
                                send(response)
                                continue
                            if method == "turn/steer":
                                assert params["expectedTurnId"] == turn
                                result = {"turnId": turn}
                            else:
                                assert method == "thread/queue/add"
                                result = {
                                    "queuedSubmission": {
                                        "id": str(uuid4()),
                                        "clientUserMessageId": params["clientUserMessageId"],
                                        "input": params["input"],
                                    }
                                }
                        send({"id": request["id"], "result": result})
                except (EOFError, ConnectionResetError, BrokenPipeError):
                    pass
                except Exception as error:
                    failures.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield CodexDaemonIngress(endpoint, route), calls, turn
    finally:
        stopping.set()
        server.close()
        thread.join(timeout=3)
        short.cleanup()
        assert not thread.is_alive()
        assert not failures


@pytest.mark.parametrize("active", [False, True])
def test_owner_input_uses_exact_event_and_body_without_settings_or_resume(
    tmp_path: Path,
    active: bool,
) -> None:
    with provider(tmp_path, active=active) as (ingress, calls, turn):
        event = str(uuid4())
        result = ingress.accept(event, "Use this exact untrusted peer body.")
        effect = calls[-1]
        assert effect["method"] == ("turn/steer" if active else "thread/queue/add")
        assert effect["params"] == {
            "threadId": ingress.route.session_id,
            "clientUserMessageId": event,
            "input": [
                {"type": "text", "text": "Use this exact untrusted peer body.", "text_elements": []}
            ],
            **({"expectedTurnId": turn} if active else {}),
        }
        assert result == {
            "schema_version": 1,
            "event_id": event,
            "status": "TRANSPORT_ACCEPTED",
            "to": ingress.route.alias,
            "provider": "codex",
        }
        assert not any(
            c["method"] in {"thread/start", "thread/resume", "turn/interrupt"} for c in calls
        )


def test_profile_or_owner_mismatch_refuses_before_any_input(tmp_path: Path) -> None:
    with provider(tmp_path, profile_changed=True) as (ingress, calls, _):
        with pytest.raises(ChatError, match="profile changed"):
            ingress.accept(str(uuid4()), "must not enter another profile")
        assert [c["method"] for c in calls] == ["initialize"]
    other = tmp_path / "other"
    other.mkdir()
    with provider(other) as (ingress, calls, _):
        route = ingress.route
        from dataclasses import replace

        ingress.route = replace(route, pid=route.pid + 1)
        with pytest.raises(ChatError, match="does not own"):
            ingress.accept(str(uuid4()), "must not enter another original")
        assert calls == []


@pytest.mark.parametrize("failure", ["disconnect", "internal", "contradiction", "wrong_receipt"])
def test_effect_uncertainty_never_falls_back_or_retries(tmp_path: Path, failure: str) -> None:
    def broken(request: dict[str, Any]) -> object:
        if failure == "disconnect":
            return None
        if failure == "internal":
            return {"id": request["id"], "error": {"code": -32603}}
        if failure == "contradiction":
            return {"id": request["id"], "error": {"code": -32602}, "result": {}}
        return {
            "id": request["id"],
            "result": {
                "queuedSubmission": {
                    "id": str(uuid4()),
                    "clientUserMessageId": str(uuid4()),
                    "input": request["params"]["input"],
                }
            },
        }

    with provider(tmp_path, reply=broken) as (ingress, calls, _):
        with pytest.raises(UnknownDeliveryError, match="do not retry"):
            ingress.accept(str(uuid4()), "possible effect")
        assert sum(c["method"] == "thread/queue/add" for c in calls) == 1
        assert not any(c["method"] == "turn/start" for c in calls)


def test_expected_turn_refusal_stays_pre_effect_without_idle_fallback(tmp_path: Path) -> None:
    with provider(
        tmp_path, active=True, reply=lambda req: {"id": req["id"], "error": {"code": -32602}}
    ) as (ingress, calls, _):
        with pytest.raises(ChatError, match="before acceptance") as raised:
            ingress.accept(str(uuid4()), "must not chase a later turn")
        assert not isinstance(raised.value, UnknownDeliveryError)
        assert sum(c["method"] == "turn/steer" for c in calls) == 1
        assert not any(c["method"] in {"turn/start", "thread/queue/add"} for c in calls)


def test_new_ingress_mode_is_negotiated_and_old_health_consumers_keep_their_shape(
    tmp_path: Path,
) -> None:
    with provider(tmp_path) as (ingress, _, _):
        old = runtime.courier_health(
            ingress.route,
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            daemon_ingress=ingress,
        )
        assert old["delivery_mode"] == "codex_stop_bound"
        assert old["delivery_mechanism"] == "stop_bound"
        new = runtime.courier_health(
            ingress.route,
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_owning_daemon=True,
            daemon_ingress=ingress,
        )
        assert new["delivery_mode"] == "codex_daemon_input"
        assert new["delivery_mechanism"] == "owning_daemon"
        target = runtime.Target(
            alias=ingress.route.alias,
            provider="codex",
            device="test",
            project="task",
            generation=ingress.route.generation,
            session_key="a" * 64,
            remote=False,
            delivery_mode="codex_daemon_input",
            delivery_mechanism="owning_daemon",
        )
        assert target.public(include_delivery_mode=True)["delivery_mode"] == "codex_stop_bound"
        assert (
            target.public(include_delivery_mode=True, include_owning_daemon=True)["delivery_mode"]
            == "codex_daemon_input"
        )


@pytest.mark.parametrize("version", ["0.160.1", "0.162.0", "0.162.1"])
def test_default_qualification_does_not_start_a_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    with provider(tmp_path) as (ingress, calls, _):
        commands: list[list[str]] = []

        def running(command: list[str], **_: object) -> object:
            import subprocess

            commands.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "status": "running",
                        "appServerVersion": version,
                        "socketPath": str(ingress.endpoint),
                    }
                ),
                "",
            )

        monkeypatch.setattr(codex_daemon.subprocess, "run", running)
        selected = codex_daemon.discover_daemon_ingress(Path("/bound/codex"), {}, ingress.route)
        assert selected is not None
        assert commands == [["/bound/codex", "app-server", "daemon", "version"]]
        assert all(c["method"] in {"initialize", "initialized", "thread/read"} for c in calls)


@pytest.mark.parametrize(
    "version",
    [
        "0.161.0",
        "0.162.2",
        "0.163.0",
        "0.162.1-alpha",
        "0.162.1-alpha.1",
        "0.162",
        "0.162.1 ",
        " 0.162.1",
        "v0.162.1",
        None,
        ["0.162.1"],
    ],
)
def test_unqualified_daemon_version_refuses_without_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: object
) -> None:
    with provider(tmp_path) as (ingress, calls, _):
        import subprocess

        monkeypatch.setattr(
            codex_daemon.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args[0],
                0,
                json.dumps(
                    {
                        "status": "running",
                        "appServerVersion": version,
                        "socketPath": str(ingress.endpoint),
                    }
                ),
                "",
            ),
        )
        assert codex_daemon.discover_daemon_ingress(Path("/bound/codex"), {}, ingress.route) is None
        assert calls == []


def test_negotiated_daemon_roster_reaches_sender_capabilities_and_legacy_parser_stays_usable(
    tmp_path: Path,
) -> None:
    """New vocabulary must never leak into a strict old broker response."""
    with provider(tmp_path) as (ingress, _, _):
        target = runtime.Target(
            alias=ingress.route.alias,
            provider="codex",
            device="test",
            project="task",
            generation=ingress.route.generation,
            session_key="a" * 64,
            remote=False,
            delivery_mode="codex_daemon_input",
            delivery_mechanism="owning_daemon",
        )

        def roster(owning: bool) -> dict[str, object]:
            row = target.public(
                include_delivery_mode=True,
                include_delivery_mechanism=True,
                include_owning_daemon=owning,
                include_handle=False,
            )
            row.update(generation=target.generation, session_key=target.session_key)
            return {"schema_version": 1, "peers": [row]}

        new = roster(True)
        with pytest.raises(ChatError, match="invalid discovery"):
            runtime._targets_from_tailnet(
                "100.64.0.2",
                new,
                include_delivery_mode=True,
                include_delivery_mechanism=True,
            )
        [parsed] = runtime._targets_from_tailnet(
            "100.64.0.2",
            new,
            include_delivery_mode=True,
            include_delivery_mechanism=True,
            include_owning_daemon=True,
        )
        receiving = runtime.destination_receiving(parsed)
        assert receiving["parked_wake"] is True
        assert receiving["active_turn_input"] is True
        assert receiving["delivery_observation"] == "not_observed"
        [legacy] = runtime._targets_from_tailnet(
            "100.64.0.2",
            roster(False),
            include_delivery_mode=True,
            include_delivery_mechanism=True,
        )
        assert legacy.delivery_mode == "codex_stop_bound"
        assert legacy.session_key == parsed.session_key
        assert legacy.generation == parsed.generation


def _running_daemon_report(version: str, endpoint: Path) -> Callable[..., object]:
    def running(command: list[str], **_: object) -> object:
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "status": "running",
                    "appServerVersion": version,
                    "socketPath": str(endpoint),
                }
            ),
            "",
        )

    return running


def _started_courier(root: Path, route: Route) -> threading.Thread:
    worker = threading.Thread(
        target=runtime.courier_server,
        kwargs={
            "provider": route.provider,
            "state_root_value": str(root),
            "session_id": route.session_id,
            "cwd": route.cwd,
            "generation": route.generation,
            "pid": route.pid,
        },
        daemon=True,
    )
    worker.start()
    path = runtime.socket_path(root, route)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            bootstrap = request_socket(
                path,
                {
                    "schema_version": 1,
                    "operation": "bootstrap",
                    "generation": route.generation,
                },
                timeout=0.5,
            )
        except ChatError:
            time.sleep(0.01)
            continue
        if bootstrap == {
            "schema_version": 1,
            "status": "BOOTSTRAPPED",
            "generation": route.generation,
        }:
            return worker
    pytest.fail("courier did not bootstrap")


def _stop_courier(root: Path, route: Route, worker: threading.Thread) -> None:
    request_socket(
        runtime.socket_path(root, route),
        {"schema_version": 1, "operation": "shutdown", "generation": route.generation},
        timeout=30.0,
    )
    worker.join(timeout=30.0)
    assert not worker.is_alive()


def test_courier_accept_prefers_daemon_ingress_over_configured_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured experimental queue never fires once a daemon ingress exists."""
    with provider(tmp_path) as (ingress, calls, _):
        courier = CodexCourier(
            alias=ingress.route.alias,
            generation=ingress.route.generation,
            native_queue=(
                Path("/bound/codex"),
                {"CODEX_HOME": "/elsewhere"},
                ingress.route.session_id,
            ),
        )
        monkeypatch.setattr(
            "cross_agent_chat.codex.queue_native_input",
            lambda **kwargs: pytest.fail("queue must not run behind a daemon ingress"),
        )
        event = str(uuid4())
        result = runtime.courier_accept(
            ingress.route, courier, event, "through the owner", daemon_ingress=ingress
        )
        assert result["status"] == "TRANSPORT_ACCEPTED"
        assert [c["method"] for c in calls].count("thread/queue/add") == 1


def test_experimental_env_yields_to_exact_qualified_owning_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The experimental env cannot reselect a queue the running owner rejects."""
    with provider(tmp_path) as (ingress, calls, _):
        root = tmp_path / "state"
        Registry(root).upsert(ingress.route)
        monkeypatch.setenv(NATIVE_QUEUE_ENV_VAR, NATIVE_QUEUE_ENV_VALUE)
        monkeypatch.setenv(NATIVE_QUEUE_BINARY_ENV_VAR, sys.executable)
        monkeypatch.setattr(runtime, "_route_owner_current", lambda *_args: True)
        monkeypatch.setattr(runtime, "_courier_owner_binary", lambda *_args: Path("/bound/codex"))
        monkeypatch.setattr(
            codex_daemon.subprocess,
            "run",
            _running_daemon_report("0.162.1", ingress.endpoint),
        )
        monkeypatch.setattr(
            "cross_agent_chat.codex.queue_native_input",
            lambda **kwargs: pytest.fail("queue must not run for a daemon-owned route"),
        )
        worker = _started_courier(root, ingress.route)
        path = runtime.socket_path(root, ingress.route)
        event = str(uuid4())
        try:
            response = request_socket(
                path,
                {
                    "schema_version": 1,
                    "operation": "accept",
                    "generation": ingress.route.generation,
                    "event_id": event,
                    "message": "deliver through the owning daemon",
                },
                timeout=30.0,
            )
            assert response == {
                "schema_version": 1,
                "event_id": event,
                "status": "TRANSPORT_ACCEPTED",
                "to": ingress.route.alias,
                "provider": "codex",
            }
            health = request_socket(
                path,
                {
                    "schema_version": 1,
                    "operation": "health",
                    "generation": ingress.route.generation,
                    "include_delivery_mode": True,
                    "include_delivery_mechanism": True,
                    "include_owning_daemon": True,
                },
                timeout=5.0,
            )
            assert health["delivery_mode"] == "codex_daemon_input"
            assert health["delivery_mechanism"] == "owning_daemon"
        finally:
            _stop_courier(root, ingress.route, worker)
        assert [c["method"] for c in calls].count("thread/queue/add") == 1


@pytest.mark.parametrize("version", ["0.162.2", "0.162.1-alpha"])
def test_experimental_env_keeps_queue_when_daemon_is_unqualified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """An unqualified owner report never displaces the explicit queue selection."""
    with provider(tmp_path) as (ingress, calls, _):
        root = tmp_path / "state"
        Registry(root).upsert(ingress.route)
        monkeypatch.setenv(NATIVE_QUEUE_ENV_VAR, NATIVE_QUEUE_ENV_VALUE)
        monkeypatch.setenv(NATIVE_QUEUE_BINARY_ENV_VAR, sys.executable)
        monkeypatch.setattr(runtime, "_route_owner_current", lambda *_args: True)
        monkeypatch.setattr(runtime, "_courier_owner_binary", lambda *_args: Path("/bound/codex"))
        monkeypatch.setattr(
            codex_daemon.subprocess,
            "run",
            _running_daemon_report(version, ingress.endpoint),
        )
        queued: list[dict[str, object]] = []
        monkeypatch.setattr(
            "cross_agent_chat.codex.queue_native_input",
            lambda **kwargs: queued.append(kwargs),
        )
        worker = _started_courier(root, ingress.route)
        path = runtime.socket_path(root, ingress.route)
        event = str(uuid4())
        try:
            response = request_socket(
                path,
                {
                    "schema_version": 1,
                    "operation": "accept",
                    "generation": ingress.route.generation,
                    "event_id": event,
                    "message": "through the explicit experimental queue",
                },
                timeout=30.0,
            )
            assert response == {
                "schema_version": 1,
                "event_id": event,
                "status": "TRANSPORT_ACCEPTED",
                "to": ingress.route.alias,
                "provider": "codex",
            }
            health = request_socket(
                path,
                {
                    "schema_version": 1,
                    "operation": "health",
                    "generation": ingress.route.generation,
                    "include_delivery_mode": True,
                    "include_delivery_mechanism": True,
                    "include_owning_daemon": True,
                },
                timeout=5.0,
            )
            assert health["delivery_mode"] == "codex_experimental_queue"
            assert health["delivery_mechanism"] == "direct_queue"
        finally:
            _stop_courier(root, ingress.route, worker)
        assert len(queued) == 1
        assert queued[0]["thread_id"] == ingress.route.session_id
        assert queued[0]["event_id"] == event
        assert calls == []


def test_no_env_and_no_owning_daemon_stays_stop_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the experimental env or a qualified owner, custody stays local."""
    with provider(tmp_path) as (ingress, _, _):
        root = tmp_path / "state"
        Registry(root).upsert(ingress.route)
        monkeypatch.delenv(NATIVE_QUEUE_ENV_VAR, raising=False)
        monkeypatch.setattr(runtime, "_route_owner_current", lambda *_args: True)
        monkeypatch.setattr(runtime, "_courier_owner_binary", lambda *_args: None)
        worker = _started_courier(root, ingress.route)
        path = runtime.socket_path(root, ingress.route)
        event = str(uuid4())
        try:
            response = request_socket(
                path,
                {
                    "schema_version": 1,
                    "operation": "accept",
                    "generation": ingress.route.generation,
                    "event_id": event,
                    "message": "held for the stop boundary",
                },
                timeout=30.0,
            )
            assert response["status"] == "TRANSPORT_ACCEPTED"
            health = request_socket(
                path,
                {
                    "schema_version": 1,
                    "operation": "health",
                    "generation": ingress.route.generation,
                    "include_delivery_mode": True,
                    "include_delivery_mechanism": True,
                    "include_owning_daemon": True,
                },
                timeout=5.0,
            )
            assert health["delivery_mode"] == "codex_stop_bound"
            assert health["delivery_mechanism"] == "stop_bound"
            peeked = request_socket(
                path,
                {
                    "schema_version": 1,
                    "operation": "peek",
                    "generation": ingress.route.generation,
                },
                timeout=5.0,
            )
            assert peeked["messages"] == [
                {"event_id": event, "message": "held for the stop boundary"}
            ]
        finally:
            _stop_courier(root, ingress.route, worker)


def test_uncertain_daemon_effect_never_falls_through_to_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-write malformed daemon reply stays UNKNOWN; no queue re-effect."""
    with provider(
        tmp_path,
        reply=lambda request: {
            "id": request["id"],
            "result": {
                "queuedSubmission": {
                    "id": str(uuid4()),
                    "clientUserMessageId": str(uuid4()),
                    "input": request["params"]["input"],
                }
            },
        },
    ) as (ingress, calls, _):
        root = tmp_path / "state"
        Registry(root).upsert(ingress.route)
        monkeypatch.setenv(NATIVE_QUEUE_ENV_VAR, NATIVE_QUEUE_ENV_VALUE)
        monkeypatch.setenv(NATIVE_QUEUE_BINARY_ENV_VAR, sys.executable)
        monkeypatch.setattr(runtime, "_route_owner_current", lambda *_args: True)
        monkeypatch.setattr(runtime, "_courier_owner_binary", lambda *_args: Path("/bound/codex"))
        monkeypatch.setattr(
            codex_daemon.subprocess,
            "run",
            _running_daemon_report("0.162.1", ingress.endpoint),
        )
        monkeypatch.setattr(
            "cross_agent_chat.codex.queue_native_input",
            lambda **kwargs: pytest.fail("an uncertain daemon effect must not fall through"),
        )
        worker = _started_courier(root, ingress.route)
        path = runtime.socket_path(root, ingress.route)
        event = str(uuid4())
        try:
            response = request_socket(
                path,
                {
                    "schema_version": 1,
                    "operation": "accept",
                    "generation": ingress.route.generation,
                    "event_id": event,
                    "message": "possible effect",
                },
                timeout=30.0,
            )
            assert response == {
                "schema_version": 1,
                "event_id": event,
                "status": "UNKNOWN_DELIVERY",
                "provider": "codex",
            }
        finally:
            _stop_courier(root, ingress.route, worker)
        assert [c["method"] for c in calls].count("thread/queue/add") == 1
