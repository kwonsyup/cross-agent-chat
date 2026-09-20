"""Versioned opaque recipient endpoint tokens.

A token is the only handle ``chat_peers`` returns and the only exact handle
``chat_send`` accepts. It is self-contained: the requester needs no binding
store to resolve it. A remote token pins the recipient's raw session key and
route generation to the stable Tailnet node identity that presented them; a
local token pins them to the issuing state root. Neither form can silently
retarget: send-time resolution re-reads the local Tailscale authority for the
node's current address and requires the exact handle and generation to be
re-attested by that one endpoint before any effect.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from cross_agent_chat.core import ChatError, valid_uuid

RECIPIENT_TOKEN_VERSION: Final = 2
RECIPIENT_TOKEN_PREFIX: Final = "cac2."
RECIPIENT_TOKEN_MAX_CHARS: Final = 512
_TOKEN_BODY_RE: Final = re.compile(r"[A-Za-z0-9_-]+\Z")
_HANDLE_RE: Final = re.compile(r"[0-9a-f]{64}\Z")
# tailcfg.StableNodeID renders as a compact alphanumeric id ("n…CNTRL").
_NODE_ID_RE: Final = re.compile(r"[A-Za-z0-9]{1,64}\Z")
_ORIGIN_DIGEST_PURPOSE: Final = "cac-recipient-origin-v1"
RecipientScope = Literal["local", "remote"]


@dataclass(frozen=True, slots=True)
class RecipientToken:
    """One decoded endpoint token; ``node_id`` or ``origin`` set by scope."""

    scope: RecipientScope
    handle: str
    generation: str
    node_id: str | None = None
    origin: str | None = None


def _checked_handle(value: str) -> str:
    if _HANDLE_RE.fullmatch(value) is None:
        raise ChatError("recipient token is invalid")
    return value


def _checked_node_id(value: str) -> str:
    if _NODE_ID_RE.fullmatch(value) is None:
        raise ChatError("recipient token is invalid")
    return value


def local_origin(root: Path) -> str:
    """Digest identifying the state root a local token may resolve under."""
    canonical = str(root.expanduser().resolve(strict=False))
    payload = f"{_ORIGIN_DIGEST_PURPOSE}\x00{canonical}".encode()
    return hashlib.sha256(payload).hexdigest()


def _encode(fields: dict[str, object]) -> str:
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return RECIPIENT_TOKEN_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def local_token(root: Path, handle: str, generation: str) -> str:
    """Mint a token selecting one exact local route under this state root."""
    return _encode(
        {
            "v": RECIPIENT_TOKEN_VERSION,
            "s": "local",
            "h": _checked_handle(handle),
            "g": valid_uuid(generation, "route generation"),
            "o": local_origin(root),
        }
    )


def remote_token(node_id: str, handle: str, generation: str) -> str:
    """Mint a token selecting one exact route on one stable Tailnet node."""
    return _encode(
        {
            "v": RECIPIENT_TOKEN_VERSION,
            "s": "remote",
            "h": _checked_handle(handle),
            "g": valid_uuid(generation, "route generation"),
            "n": _checked_node_id(node_id),
        }
    )


def parse_recipient_token(value: str) -> RecipientToken | None:
    """Decode one endpoint token, or return ``None`` when the value is not one.

    Anything carrying the token prefix is a token: malformed payloads are
    rejected rather than silently reinterpreted as a name query.
    """
    if not value.startswith(RECIPIENT_TOKEN_PREFIX):
        return None
    if len(value) > RECIPIENT_TOKEN_MAX_CHARS:
        raise ChatError("recipient token is invalid")
    body = value[len(RECIPIENT_TOKEN_PREFIX) :]
    if _TOKEN_BODY_RE.fullmatch(body) is None or len(body) % 4 == 1:
        raise ChatError("recipient token is invalid")
    try:
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload: object = json.loads(decoded)
    except ValueError as error:
        raise ChatError("recipient token is invalid") from error
    if not isinstance(payload, dict):
        raise ChatError("recipient token is invalid")
    fields = cast(dict[object, object], payload)
    scope = fields.get("s")
    version = fields.get("v")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != RECIPIENT_TOKEN_VERSION
        or scope not in ("local", "remote")
        or set(fields) != {"v", "s", "h", "g", "o" if scope == "local" else "n"}
    ):
        raise ChatError("recipient token is invalid")
    handle = fields["h"]
    generation = fields["g"]
    if not isinstance(handle, str) or not isinstance(generation, str):
        raise ChatError("recipient token is invalid")
    try:
        exact_generation = valid_uuid(generation, "route generation")
    except ChatError as error:
        raise ChatError("recipient token is invalid") from error
    if scope == "local":
        origin = fields["o"]
        if not isinstance(origin, str):
            raise ChatError("recipient token is invalid")
        return RecipientToken(
            scope="local",
            handle=_checked_handle(handle),
            generation=exact_generation,
            origin=_checked_handle(origin),
        )
    node_id = fields["n"]
    if not isinstance(node_id, str):
        raise ChatError("recipient token is invalid")
    return RecipientToken(
        scope="remote",
        handle=_checked_handle(handle),
        generation=exact_generation,
        node_id=_checked_node_id(node_id),
    )
