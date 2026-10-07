"""Provider-owned original input; no substitute runtime or uncertain-effect retry."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import pytest

from cross_agent_chat import codex_daemon, runtime
from cross_agent_chat.codex_daemon import CodexDaemonIngress
from cross_agent_chat.core import ChatError, Route, UnknownDeliveryError


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


def test_default_qualification_does_not_start_a_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
                        "appServerVersion": "0.160.1",
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
