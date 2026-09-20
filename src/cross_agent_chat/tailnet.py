"""Tailscale-backed peer discovery and transport constants."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from cross_agent_chat.core import ChatError

TAILNET_NETWORK: Final = ipaddress.ip_network("100.64.0.0/10")
TAILNET_PORT: Final = 47071
LOCAL_BROKER_HOST: Final = "127.0.0.1"
LOCAL_BROKER_PORT: Final = 47072
TAILSCALE_APP_BINARY: Final = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
TAILSCALE_STANDALONE_BINARIES: Final = (
    Path("/opt/homebrew/bin/tailscale"),
    Path("/usr/local/bin/tailscale"),
)
IFCONFIG_BINARY: Final = Path("/sbin/ifconfig")


def valid_tailnet_address(value: str) -> str:
    """Validate one private Tailscale IPv4 address."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ChatError("Tailnet address is invalid") from error
    if address.version != 4 or address not in TAILNET_NETWORK:
        raise ChatError("Tailnet address is invalid")
    return str(address)


# tailcfg.StableNodeID renders as a compact alphanumeric id ("n…CNTRL").
_NODE_ID_RE: Final = re.compile(r"[A-Za-z0-9]{1,64}\Z")


def _checked_node_id(value: object) -> str:
    if not isinstance(value, str) or _NODE_ID_RE.fullmatch(value) is None:
        raise ChatError("Tailscale status is invalid")
    return value


def _peer_tailnet_address(peer: dict[object, object]) -> str | None:
    raw_addresses = peer.get("TailscaleIPs")
    if not isinstance(raw_addresses, list) or not all(
        isinstance(item, str) for item in raw_addresses
    ):
        raise ChatError("Tailscale status is invalid")
    for item in cast(list[str], raw_addresses):
        try:
            address = ipaddress.ip_address(item)
        except ValueError:
            continue
        if address.version == 4 and address in TAILNET_NETWORK:
            return str(address)
    return None


@dataclass(frozen=True, slots=True)
class TailnetIdentity:
    """The local node's trusted read of its Tailnet world.

    ``self_node_id`` is this node's stable identity from ``tailscale status
    --json`` (``Self.ID``). ``peers`` maps each online peer's stable node
    identity (``Peer.*.ID``) to its current Tailnet IPv4. The map is the only
    authority a recipient token resolves against: a stable id that moved to a
    new address still resolves, while an address reassigned to a different
    node can never be claimed by the original token.
    """

    self_node_id: str | None
    peers: dict[str, str]


def parse_tailnet_identity(text: str) -> TailnetIdentity:
    """Read self and peer stable node identities from a status response."""
    status = _parse_status(text)
    self_value = status.get("Self")
    self_node_id: str | None = None
    if self_value is not None:
        if not isinstance(self_value, dict):
            raise ChatError("Tailscale status is invalid")
        self_node_id = _checked_node_id(cast(dict[object, object], self_value).get("ID"))
    peer_value = status.get("Peer")
    if not isinstance(peer_value, dict):
        raise ChatError("Tailscale status is invalid")
    peers: dict[str, str] = {}
    owners: dict[str, str] = {}
    for value in cast(dict[object, object], peer_value).values():
        if not isinstance(value, dict):
            raise ChatError("Tailscale status is invalid")
        peer = cast(dict[object, object], value)
        online = peer.get("Online")
        if not isinstance(online, bool):
            raise ChatError("Tailscale status is invalid")
        address = _peer_tailnet_address(peer)
        if not online or address is None:
            continue
        node_id = _checked_node_id(peer.get("ID"))
        # A conflicting read can never silently overwrite the sole identity
        # authority a recipient token resolves against.
        if node_id in peers or owners.setdefault(address, node_id) != node_id:
            raise ChatError("Tailscale status is invalid")
        peers[node_id] = address
    return TailnetIdentity(self_node_id=self_node_id, peers=peers)


def _parse_status(text: str) -> dict[object, object]:
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise ChatError("Tailscale status is invalid") from error
    if not isinstance(raw, dict):
        raise ChatError("Tailscale status is invalid")
    return cast(dict[object, object], raw)


def _self_tailnet_address(status: dict[object, object]) -> str | None:
    self_value = status.get("Self")
    if not isinstance(self_value, dict):
        raise ChatError("Tailscale status is invalid")
    raw_addresses = cast(dict[object, object], self_value).get("TailscaleIPs")
    if not isinstance(raw_addresses, list) or not all(
        isinstance(item, str) for item in raw_addresses
    ):
        raise ChatError("Tailscale status is invalid")
    for item in cast(list[str], raw_addresses):
        try:
            address = ipaddress.ip_address(item)
        except ValueError:
            continue
        if address.version == 4 and address in TAILNET_NETWORK:
            return valid_tailnet_address(str(address))
    return None


def parse_local_tailnet_address(text: str) -> str | None:
    """Return this node's Tailnet IPv4 address when Tailscale is running."""
    status = _parse_status(text)
    if status.get("BackendState") != "Running":
        return None
    return _self_tailnet_address(status)


def parse_known_tailnet_address(text: str) -> str | None:
    """Return this node's assigned Tailnet IPv4 even while its backend is stopped."""
    return _self_tailnet_address(_parse_status(text))


def parse_ifconfig_tailnet_address(text: str) -> str | None:
    """Return one Tailnet IPv4 assigned to a macOS tunnel interface."""
    interface = ""
    addresses: set[str] = set()
    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            interface = line.split(":", maxsplit=1)[0]
            continue
        if not interface.startswith("utun"):
            continue
        fields = line.split()
        if len(fields) < 2 or fields[0] != "inet":
            continue
        try:
            addresses.add(valid_tailnet_address(fields[1]))
        except ChatError:
            continue
    return next(iter(addresses)) if len(addresses) == 1 else None


def tailscale_binary() -> Path | None:
    """Find an executable Tailscale CLI without modifying the user's PATH."""
    candidate = shutil.which("tailscale")
    paths = [Path(candidate)] if candidate is not None else []
    # launchd does not inherit the shell's Homebrew PATH. Prefer the installed
    # standalone CLI before the GUI binary, which may not answer from launchd.
    paths.extend(TAILSCALE_STANDALONE_BINARIES)
    paths.append(TAILSCALE_APP_BINARY)
    for path in paths:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    return None


def _status_output() -> str | None:
    binary = tailscale_binary()
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [str(binary), "status", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _ifconfig_tailnet_address() -> str | None:
    output = _ifconfig_output()
    return None if output is None else parse_ifconfig_tailnet_address(output)


def _ifconfig_output() -> str | None:
    try:
        completed = subprocess.run(
            [str(IFCONFIG_BINARY)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return completed.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _interface_has_address(text: str, expected_address: str) -> bool:
    interface = ""
    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            interface = line.split(":", maxsplit=1)[0]
            continue
        fields = line.split()
        if (
            interface.startswith("utun")
            and len(fields) >= 2
            and fields[0] == "inet"
            and fields[1] == expected_address
        ):
            return True
    return False


def tailnet_identity() -> TailnetIdentity | None:
    """Return the local node's trusted Tailnet identity read, or none."""
    output = _status_output()
    if output is None:
        return None
    try:
        return parse_tailnet_identity(output)
    except ChatError:
        return None


def local_tailnet_address() -> str | None:
    """Return this Mac's Tailnet IPv4 address without changing Tailscale state."""
    output = _status_output()
    address: str | None
    if output is None:
        # Setup captured this exact address from Tailscale's own Self identity.
        # Its continued presence is sufficient when the status CLI is unavailable;
        # an arbitrary CGNAT tunnel must never replace the captured identity.
        configured = os.environ.get("CROSS_AGENT_CHAT_TAILNET_ADDRESS")
        if configured is None:
            return None
        try:
            address = valid_tailnet_address(configured)
        except ChatError:
            return None
    else:
        try:
            address = parse_local_tailnet_address(output)
        except ChatError:
            return None
    if address is None:
        return None
    interfaces = _ifconfig_output()
    if interfaces is None or not _interface_has_address(interfaces, address):
        return None
    return address


def known_tailnet_address() -> str | None:
    """Return this Mac's assigned Tailnet IPv4 without requiring an online backend."""
    output = _status_output()
    if output is None:
        return None
    try:
        return parse_known_tailnet_address(output)
    except ChatError:
        return None
