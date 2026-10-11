"""Exact original input through an already running, owning Codex CLI daemon.

No daemon is started, thread resumed, permission changed, or failed effect retried.
The qualified 0.160.1, 0.162.0, and 0.162.1 protocols use a private Unix WebSocket.
queue/add wakes an idle TUI original; steer targets the exact active turn without
interrupting tools.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import stat
import struct
import subprocess as subprocess
import time
from pathlib import Path
from typing import Final, cast

from cross_agent_chat import __version__
from cross_agent_chat.core import (
    ChatError,
    Route,
    UnknownDeliveryError,
    bounded_message,
    valid_uuid,
)

SUPPORTED_VERSIONS: Final = frozenset({"0.160.1", "0.162.0", "0.162.1"})
PROBE_SECONDS: Final = 2.0
ACCEPT_SECONDS: Final = 10.0
MAX_RESPONSE_BYTES: Final = 256 * 1024
LOCAL_PEERPID: Final = 2  # Darwin sys/un.h, SOL_LOCAL = 0


class _Client:
    """One bounded RFC6455 connection authenticated to the route's owner PID."""

    def __init__(self, endpoint: Path, route: Route, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.buffer = b""
        self.identifier = 0
        self.effect_attempted = False
        try:
            resolved = endpoint.resolve(strict=True)
            info, parent = resolved.stat(), resolved.parent.stat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or parent.st_uid != os.geteuid()
                or parent.st_mode & 0o077
            ):
                raise ChatError("Codex owning daemon socket is unsafe")
            self.socket.settimeout(self.remaining())
            self.socket.connect(str(resolved))
            peer = struct.unpack("i", self.socket.getsockopt(0, LOCAL_PEERPID, 4))[0]
            if peer != route.pid:
                raise ChatError("Codex daemon does not own this original")
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {key}\r\n\r\n"
            )
            self._send(request.encode("ascii"))
            while b"\r\n\r\n" not in self.buffer:
                self._receive()
                if len(self.buffer) > 8192:
                    raise ChatError("Codex daemon handshake is invalid")
            header, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            lines = header.decode("ascii").split("\r\n")
            fields = {
                name.lower(): value
                for name, value in (line.split(":", 1) for line in lines[1:] if ":" in line)
            }
            expected = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode("ascii")
            if (
                not lines[0].startswith("HTTP/1.1 101 ")
                or fields.get("sec-websocket-accept", "").strip() != expected
            ):
                raise ChatError("Codex daemon handshake is invalid")
            initialized = self.call(
                "initialize",
                {
                    "clientInfo": {"name": "cross-agent-chat", "version": __version__},
                    "capabilities": {"experimentalApi": True},
                },
            )
            if (
                not isinstance(initialized.get("codexHome"), str)
                or Path(str(initialized["codexHome"])).resolve()
                != Path(str(route.profile_root)).resolve()
            ):
                raise ChatError("Codex daemon profile changed")
            self._frame({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self.socket.close()

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex daemon deadline expired")
        return remaining

    def _send(self, data: bytes) -> None:
        self.socket.settimeout(self.remaining())
        self.socket.sendall(data)

    def _receive(self) -> None:
        self.socket.settimeout(self.remaining())
        chunk = self.socket.recv(65536)
        if not chunk:
            raise EOFError("Codex daemon disconnected")
        self.buffer += chunk
        if len(self.buffer) > MAX_RESPONSE_BYTES:
            raise ChatError("Codex daemon response exceeds the bounded limit")

    def _exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            self._receive()
        value, self.buffer = self.buffer[:size], self.buffer[size:]
        return value

    def _write_frame(self, data: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        size = len(data)
        if size < 126:
            header = bytes([0x80 | opcode, 0x80 | size])
        elif size <= 65535:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", size)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", size)
        self._send(
            header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        )

    def _frame(self, payload: dict[str, object]) -> None:
        self._write_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def _read_message(self) -> object:
        message = bytearray()
        started = False
        while True:
            flags, size_byte = self._exact(2)
            opcode, final = flags & 15, bool(flags & 0x80)
            if flags & 0x70 or size_byte & 0x80:
                raise ChatError("Codex daemon frame is invalid")
            size = size_byte & 0x7F
            if size == 126:
                size = struct.unpack("!H", self._exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._exact(8))[0]
            if size > MAX_RESPONSE_BYTES or len(message) + size > MAX_RESPONSE_BYTES:
                raise ChatError("Codex daemon response exceeds the bounded limit")
            body = self._exact(size)
            if opcode == 8:
                raise EOFError("Codex daemon disconnected")
            if opcode in {9, 10}:
                if not final or size > 125:
                    raise ChatError("Codex daemon frame is invalid")
                if opcode == 9:
                    self._write_frame(body, 10)
                continue
            if (opcode == 1 and started) or opcode not in {0, 1} or (opcode == 0 and not started):
                raise ChatError("Codex daemon frame is invalid")
            started = True
            message.extend(body)
            if final:
                return json.loads(message.decode("utf-8"))

    def call(
        self, method: str, params: dict[str, object], *, effect: bool = False
    ) -> dict[str, object]:
        self.identifier += 1
        identifier = self.identifier
        # Set before the first byte: a partial write is also an uncertain effect.
        self.effect_attempted |= effect
        self._frame({"id": identifier, "method": method, "params": params})
        while True:
            value = self._read_message()
            if not isinstance(value, dict):
                raise ChatError("Codex daemon response is invalid")
            if isinstance(value.get("id"), bool):
                raise ChatError("Codex daemon response is invalid")
            if value.get("id") != identifier:
                # Notifications are never recorded or interpreted as receipts.
                continue
            if "result" in value and "error" in value:
                raise ChatError("Codex daemon response is contradictory")
            error = value.get("error")
            if isinstance(error, dict):
                if not effect or error.get("code") in {-32600, -32601, -32602}:
                    self.effect_attempted = False
                    raise ChatError("Codex daemon rejected input before acceptance")
                raise UnknownDeliveryError("Codex owning daemon delivery is unknown")
            result = value.get("result")
            if "error" in value or not isinstance(result, dict):
                raise ChatError("Codex daemon response is invalid")
            return cast(dict[str, object], result)


class CodexDaemonIngress:
    """An exact route already owned by the qualified provider daemon."""

    def __init__(self, endpoint: Path, route: Route) -> None:
        self.endpoint, self.route = endpoint, route

    def _thread(self, client: _Client) -> dict[str, object]:
        result = client.call(
            "thread/read", {"threadId": self.route.session_id, "includeTurns": False}
        )
        thread = result.get("thread")
        if (
            not isinstance(thread, dict)
            or thread.get("id") != self.route.session_id
            or thread.get("cwd") != self.route.cwd
            or thread.get("canAcceptDirectInput") is not True
            or thread.get("originator") != "codex-tui"
            or not isinstance(thread.get("status"), dict)
            or thread["status"].get("type") not in {"idle", "active"}
        ):
            raise ChatError("Codex daemon original input is unavailable")
        return cast(dict[str, object], thread)

    def check(self) -> None:
        client = _Client(self.endpoint, self.route, PROBE_SECONDS)
        try:
            self._thread(client)
        finally:
            client.close()

    def accept(self, event_id: str, message: str) -> dict[str, object]:
        identifier, body = valid_uuid(event_id, "event id"), bounded_message(message)
        client: _Client | None = None
        try:
            client = _Client(self.endpoint, self.route, ACCEPT_SECONDS)
            thread = self._thread(client)
            inputs = [{"type": "text", "text": body, "text_elements": []}]
            params: dict[str, object] = {
                "threadId": self.route.session_id,
                "clientUserMessageId": identifier,
                "input": inputs,
            }
            status = cast(dict[str, object], thread["status"])
            if status["type"] == "active":
                turns = client.call(
                    "thread/turns/list",
                    {"threadId": self.route.session_id, "limit": 1, "itemsView": "notLoaded"},
                ).get("data")
                if (
                    not isinstance(turns, list)
                    or len(turns) != 1
                    or not isinstance(turns[0], dict)
                    or turns[0].get("status") != "inProgress"
                    or not isinstance(turns[0].get("id"), str)
                ):
                    raise ChatError("Codex daemon active turn changed before input")
                expected = valid_uuid(turns[0]["id"], "active turn id")
                params["expectedTurnId"] = expected
                result = client.call("turn/steer", params, effect=True)
                if result.get("turnId") != expected:
                    raise UnknownDeliveryError("Codex owning daemon delivery is unknown")
            else:
                result = client.call("thread/queue/add", params, effect=True)
                queued = result.get("queuedSubmission")
                if (
                    not isinstance(queued, dict)
                    or queued.get("clientUserMessageId") != identifier
                    or queued.get("input") != inputs
                    or not isinstance(queued.get("id"), str)
                ):
                    raise UnknownDeliveryError("Codex owning daemon delivery is unknown")
                valid_uuid(queued["id"], "queued submission id")
            return {
                "schema_version": 1,
                "event_id": identifier,
                "status": "TRANSPORT_ACCEPTED",
                "to": self.route.alias,
                "provider": "codex",
            }
        except (OSError, EOFError, ValueError, ChatError) as error:
            if isinstance(error, UnknownDeliveryError) or (client and client.effect_attempted):
                raise UnknownDeliveryError(
                    "Codex owning daemon delivery is unknown; do not retry"
                ) from error
            if isinstance(error, ChatError):
                raise
            raise ChatError("Codex owning daemon is unavailable before input") from error
        finally:
            if client is not None:
                client.close()


def discover_daemon_ingress(
    binary: Path, environment: dict[str, str], route: Route
) -> CodexDaemonIngress | None:
    """Qualify only a running owner; missing/opted-out/unsupported routes stay Stop-bound."""
    if route.profile_root is None or route.provider != "codex":
        return None
    try:
        status = subprocess.run(
            [str(binary), "app-server", "daemon", "version"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=PROBE_SECONDS,
            check=False,
        )
        if status.returncode != 0 or len(status.stdout) > 8192:
            return None
        report = json.loads(status.stdout)
        if (
            not isinstance(report, dict)
            or report.get("status") != "running"
            or not isinstance(report.get("appServerVersion"), str)
            or report.get("appServerVersion") not in SUPPORTED_VERSIONS
            or not isinstance(report.get("socketPath"), str)
        ):
            return None
        endpoint = Path(report["socketPath"])
        expected = Path(route.profile_root) / "app-server-control" / "app-server-control.sock"
        if endpoint != expected:
            return None
        ingress = CodexDaemonIngress(endpoint, route)
        ingress.check()
        return ingress
    except (OSError, EOFError, ValueError, ChatError, subprocess.SubprocessError):
        return None
