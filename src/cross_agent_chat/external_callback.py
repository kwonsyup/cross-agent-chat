"""One bounded owner-configured HTTPS delivery seam; no provider wake claim."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from cross_agent_chat.core import ChatError, bounded_message, require_private_file

CallbackOutcome = Literal["TRANSPORT_ACCEPTED", "PRE_EFFECT_REJECTED", "UNKNOWN_DELIVERY"]
CALLBACK_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class CallbackConfig:
    url: str = field(repr=False)
    bearer: str = field(repr=False)

    def __post_init__(self) -> None:
        callback_destination(self.url)
        if (
            not self.bearer
            or len(self.bearer) > 4096
            or any(ord(character) < 33 or ord(character) > 126 for character in self.bearer)
        ):
            raise ChatError("external callback credential is invalid")


def callback_destination(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as error:
        raise ChatError("external callback destination is invalid") from error
    if (
        len(url) > 4096
        or parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or port not in (None, 443)
        or host.endswith((".local", ".localhost", ".internal"))
        or host == "localhost"
        or "." not in host
        or any(ord(character) < 33 or ord(character) > 126 for character in url)
    ):
        raise ChatError("external callback destination is invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ChatError("external callback must use a public HTTPS hostname")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host, path


def read_callback(path: Path) -> CallbackConfig:
    try:
        require_private_file(path)
        with path.open("rb") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ChatError("external callback configuration exceeds the bounded limit")
        value: object = json.loads(raw)
    except (ChatError, OSError, UnicodeDecodeError, ValueError) as error:
        raise ChatError("external callback configuration is unavailable or invalid") from error
    if not isinstance(value, dict) or set(value) != {"url", "bearer"}:
        raise ChatError("external callback configuration is invalid")
    if not isinstance(value["url"], str) or not isinstance(value["bearer"], str):
        raise ChatError("external callback configuration is invalid")
    return CallbackConfig(value["url"], value["bearer"])


def _public_addresses(host: str) -> list[str]:
    addresses = [str(item[4][0]) for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ChatError("external callback destination is not public")
    return list(dict.fromkeys(addresses))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str) -> None:
        super().__init__(
            host, timeout=CALLBACK_TIMEOUT_SECONDS, context=ssl.create_default_context()
        )
        self._tls_context = ssl.create_default_context()
        self._checked_address = address

    def connect(self) -> None:
        # Connect the checked IP directly, while TLS and Host use the enrolled
        # hostname. No second DNS lookup, proxy environment, tunnel or redirect.
        raw = socket.create_connection((self._checked_address, 443), self.timeout)
        try:
            self.sock = self._tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _post_https(config: CallbackConfig, body: str) -> CallbackOutcome:
    attempted = False
    connection: _PinnedHTTPSConnection | None = None
    try:
        host, path = callback_destination(config.url)
        addresses = _public_addresses(host)
        connection = _PinnedHTTPSConnection(host, addresses[0])
        connection.connect()
        # From this point even header/body write failure is uncertain.
        attempted = True
        connection.request(
            "POST",
            path,
            body=body.encode(),
            headers={
                "Authorization": "Bearer " + config.bearer,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        # No response body is necessary for custody, and no redirects/retries
        # are followed. Any other response has no qualified no-run meaning.
        return "TRANSPORT_ACCEPTED" if 200 <= response.status < 300 else "UNKNOWN_DELIVERY"
    except (ChatError, OSError, ValueError, http.client.HTTPException):
        return "UNKNOWN_DELIVERY" if attempted else "PRE_EFFECT_REJECTED"
    finally:
        if connection is not None:
            connection.close()


def post_callback(
    config: CallbackConfig,
    envelope: dict[str, object],
    *,
    timeout_seconds: float,
    lock_fds: tuple[int, ...],
) -> CallbackOutcome:
    """Bound DNS, TLS, headers and I/O together in one owned short-lived worker.

    The worker accepts secrets through stdin only and emits one content-free
    outcome. Killing it at the deadline never establishes that no POST ran.
    """
    if timeout_seconds <= 0 or timeout_seconds > CALLBACK_TIMEOUT_SECONDS:
        raise ChatError("external callback deadline is exhausted")
    if not lock_fds:
        raise ChatError("external callback effect custody is unavailable")
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    if len(body.encode()) > 65536:
        raise ChatError("external callback envelope exceeds the bounded limit")
    payload = json.dumps({"url": config.url, "bearer": config.bearer, "body": body})
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTHON") and key not in {"SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    # Source and installed wheels both resolve from the code the parent already
    # loaded; cwd and ambient PYTHONPATH cannot replace that package.
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-P",
                "-m",
                "cross_agent_chat.external_callback",
                "--timeout-seconds",
                str(timeout_seconds),
            ],
            input=payload,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            env=environment,
            pass_fds=lock_fds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "UNKNOWN_DELIVERY"
    except OSError:
        return "UNKNOWN_DELIVERY"
    outcome = result.stdout.strip()
    if result.returncode == 0 and outcome in {
        "TRANSPORT_ACCEPTED",
        "PRE_EFFECT_REJECTED",
        "UNKNOWN_DELIVERY",
    }:
        return cast(CallbackOutcome, outcome)
    return "UNKNOWN_DELIVERY"


def _worker() -> None:
    # A kernel-enforced alarm survives loss of the parent and interrupts even
    # a stuck resolver. Default SIGALRM termination keeps the receipt unknown.
    if len(sys.argv) != 3 or sys.argv[1] != "--timeout-seconds":
        raise SystemExit(2)
    try:
        timeout = float(sys.argv[2])
    except ValueError:
        raise SystemExit(2) from None
    if not 0 < timeout <= CALLBACK_TIMEOUT_SECONDS:
        raise SystemExit(2)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    raw = sys.stdin.buffer.read(131073)
    outcome: CallbackOutcome = "PRE_EFFECT_REJECTED"
    try:
        if len(raw) > 131072:
            raise ChatError("callback worker input exceeds bound")
        value: object = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"url", "bearer", "body"}:
            raise ChatError("callback worker input is invalid")
        if not all(isinstance(value[key], str) for key in ("url", "bearer", "body")):
            raise ChatError("callback worker input is invalid")
        bounded_message(cast(str, json.loads(cast(str, value["body"]))["message"]))
        outcome = _post_https(
            CallbackConfig(cast(str, value["url"]), cast(str, value["bearer"])),
            cast(str, value["body"]),
        )
    except (ChatError, ValueError, KeyError, TypeError):
        pass
    print(outcome)


if __name__ == "__main__":
    _worker()
