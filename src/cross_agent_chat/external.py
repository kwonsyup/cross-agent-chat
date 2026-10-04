"""Owner-enrolled external identities, separate from native process routes."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from cross_agent_chat.core import (
    ChatError,
    atomic_json,
    ensure_private_dir,
    require_private_file,
    state_lock,
    valid_device,
    valid_name,
    valid_uuid,
)
from cross_agent_chat.recipient import parse_recipient_token

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CREDENTIAL = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.([0-9a-f]{64})\Z"
)


@dataclass(frozen=True, slots=True)
class ExternalEndpoint:
    """A credential binds an enrolled destination, never a native identity."""

    schema_version: int
    endpoint_id: str
    generation: str
    owner_uid: int
    device: str
    name: str
    context: str
    credential_hash: str
    expires_at: str | None
    revoked: bool
    allowed_recipients: tuple[str, ...] | None = None
    callback_ref: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ChatError("external endpoint schema is unsupported")
        valid_uuid(self.endpoint_id, "external endpoint id")
        valid_uuid(self.generation, "external endpoint generation")
        if isinstance(self.owner_uid, bool) or self.owner_uid != os.getuid():
            raise ChatError("external endpoint owner is invalid")
        valid_device(self.device)
        valid_name(self.name, "external endpoint name")
        valid_name(self.context, "owner-enrolled context")
        if _HASH.fullmatch(self.credential_hash) is None:
            raise ChatError("external credential verifier is invalid")
        if self.expires_at is not None:
            try:
                expiry = datetime.fromisoformat(self.expires_at)
            except ValueError as error:
                raise ChatError("external credential expiry is invalid") from error
            if expiry.tzinfo is None:
                raise ChatError("external credential expiry is invalid")

        if self.allowed_recipients is not None:
            for recipient in self.allowed_recipients:
                if parse_recipient_token(recipient) is None:
                    raise ChatError("external recipient scope is invalid")
        if (
            self.callback_ref is not None
            and self.callback_ref != f"external-callback-{self.endpoint_id}.json"
        ):
            raise ChatError("external callback reference is invalid")

    @property
    def key(self) -> str:
        return hashlib.sha256(f"cac-external-endpoint-v1\0{self.endpoint_id}".encode()).hexdigest()

    @property
    def alias(self) -> str:
        # The suffix prevents same-name contexts from silently sharing an alias.
        return f"external@{self.device}:{self.name[:40]}:{self.key[:12]}"

    def available(self) -> bool:
        return not self.revoked and (
            self.expires_at is None
            or datetime.fromisoformat(self.expires_at).astimezone(UTC) > datetime.now(UTC)
        )

    @classmethod
    def from_object(cls, raw: object) -> ExternalEndpoint:
        fields = {
            "schema_version",
            "endpoint_id",
            "generation",
            "owner_uid",
            "device",
            "name",
            "context",
            "credential_hash",
            "expires_at",
            "revoked",
            "allowed_recipients",
            "callback_ref",
        }
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ChatError("external endpoint schema is unsupported")
        values = cast(dict[str, object], raw)
        string_fields = fields - {
            "schema_version",
            "owner_uid",
            "expires_at",
            "revoked",
            "allowed_recipients",
            "callback_ref",
        }
        if (
            not all(isinstance(values[field], str) for field in string_fields)
            or not isinstance(values["schema_version"], int)
            or not isinstance(values["owner_uid"], int)
            or not isinstance(values["revoked"], bool)
            or (values["expires_at"] is not None and not isinstance(values["expires_at"], str))
        ):
            raise ChatError("external endpoint schema is unsupported")
        return cls(
            schema_version=values["schema_version"],
            endpoint_id=cast(str, values["endpoint_id"]),
            generation=cast(str, values["generation"]),
            owner_uid=values["owner_uid"],
            device=cast(str, values["device"]),
            name=cast(str, values["name"]),
            context=cast(str, values["context"]),
            credential_hash=cast(str, values["credential_hash"]),
            expires_at=values["expires_at"],
            revoked=values["revoked"],
            allowed_recipients=_recipient_scope(values["allowed_recipients"]),
            callback_ref=_optional_string(values["callback_ref"]),
        )


def _optional_string(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ChatError("external endpoint schema is unsupported")


def _recipient_scope(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ChatError("external recipient scope is invalid")
    return tuple(cast(list[str], value))


class ExternalEndpointStore:
    """Private records that old native registry readers never encounter."""

    def __init__(self, root: Path) -> None:
        ensure_private_dir(root)
        self.root = root
        self.path = root / "external-endpoints-v1.json"

    def endpoints(self) -> list[ExternalEndpoint]:
        if not self.path.exists():
            return []
        require_private_file(self.path)
        try:
            raw: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise ChatError("external endpoint state is invalid") from error
        if not isinstance(raw, list):
            raise ChatError("external endpoint schema is unsupported")
        endpoints = [ExternalEndpoint.from_object(item) for item in raw]
        if len({item.endpoint_id for item in endpoints}) != len(endpoints):
            raise ChatError("external endpoint state contains duplicate identities")
        return endpoints

    def enroll(
        self,
        *,
        device: str,
        name: str,
        context: str,
        expires_at: str | None = None,
        allowed_recipients: tuple[str, ...] | None = None,
    ) -> tuple[ExternalEndpoint, str]:
        endpoint_id = str(uuid4())
        credential = f"{endpoint_id}.{secrets.token_hex(32)}"
        endpoint = ExternalEndpoint(
            schema_version=1,
            endpoint_id=endpoint_id,
            generation=str(uuid4()),
            owner_uid=os.getuid(),
            device=device,
            name=name,
            context=context,
            credential_hash=hashlib.sha256(credential.encode()).hexdigest(),
            expires_at=expires_at,
            revoked=False,
            allowed_recipients=allowed_recipients,
        )
        with state_lock(self.root, "external-endpoints"):
            atomic_json(self.path, [asdict(item) for item in [*self.endpoints(), endpoint]])
        return endpoint, credential

    def authenticate(self, credential: str) -> ExternalEndpoint:
        match = _CREDENTIAL.fullmatch(credential)
        if match is None:
            raise ChatError("external credential is unavailable; enroll this endpoint")
        verifier = hashlib.sha256(credential.encode()).hexdigest()
        matches = [
            item
            for item in self.endpoints()
            if item.endpoint_id == match[1]
            and item.available()
            and hmac.compare_digest(item.credential_hash, verifier)
        ]
        if len(matches) != 1:
            raise ChatError("external credential is unavailable; enroll this endpoint")
        return matches[0]

    def current(self, endpoint: ExternalEndpoint) -> bool:
        return endpoint.available() and any(item == endpoint for item in self.endpoints())

    def rotate(self, endpoint_id: str) -> tuple[ExternalEndpoint, str]:
        identifier = valid_uuid(endpoint_id, "external endpoint id")
        credential = f"{identifier}.{secrets.token_hex(32)}"
        with (
            endpoint_effect_lock(self.root, identifier, wait=True),
            state_lock(self.root, "external-endpoints"),
        ):
            existing = self.endpoints()
            matches = [
                item for item in existing if item.endpoint_id == identifier and item.available()
            ]
            if len(matches) != 1:
                raise ChatError("external endpoint is unavailable")
            # Rotation ends the old generation: old reply handles deliberately
            # refuse rather than silently succeeding under a changed authority.
            endpoint = replace(
                matches[0],
                generation=str(uuid4()),
                credential_hash=hashlib.sha256(credential.encode()).hexdigest(),
            )
            atomic_json(
                self.path,
                [
                    asdict(endpoint) if item.endpoint_id == identifier else asdict(item)
                    for item in existing
                ],
            )
        return endpoint, credential

    def configure_callback(self, endpoint_id: str, config_path: Path) -> ExternalEndpoint:
        from cross_agent_chat.external_callback import read_callback

        identifier = valid_uuid(endpoint_id, "external endpoint id")
        config = read_callback(config_path)
        with (
            endpoint_effect_lock(self.root, identifier, wait=True),
            state_lock(self.root, "external-endpoints"),
        ):
            existing = self.endpoints()
            matches = [
                item for item in existing if item.endpoint_id == identifier and item.available()
            ]
            if len(matches) != 1:
                raise ChatError("external endpoint is unavailable")
            endpoint = replace(
                matches[0],
                generation=str(uuid4()),
                callback_ref=f"external-callback-{identifier}.json",
            )
            atomic_json(self.root / cast(str, endpoint.callback_ref), asdict(config))
            atomic_json(
                self.path,
                [
                    asdict(endpoint) if item.endpoint_id == identifier else asdict(item)
                    for item in existing
                ],
            )
        return endpoint

    def revoke(self, endpoint_id: str) -> None:
        identifier = valid_uuid(endpoint_id, "external endpoint id")
        with (
            endpoint_effect_lock(self.root, identifier, wait=True),
            state_lock(self.root, "external-endpoints"),
        ):
            existing = self.endpoints()
            if not any(item.endpoint_id == identifier for item in existing):
                raise ChatError("external endpoint is unavailable")
            callback = self.root / f"external-callback-{identifier}.json"
            if callback.exists() or callback.is_symlink():
                callback.unlink()
            atomic_json(
                self.path,
                [
                    asdict(replace(item, revoked=True, generation=str(uuid4())))
                    if item.endpoint_id == identifier
                    else asdict(item)
                    for item in existing
                ],
            )


@contextmanager
def endpoint_effect_lock(root: Path, endpoint_id: str, *, wait: bool = False) -> Iterator[int]:
    """One bounded send/admission owner; revocation waits for admitted work."""
    identifier = valid_uuid(endpoint_id, "external endpoint id")
    ensure_private_dir(root)
    descriptor = os.open(
        root / f".external-effect-{identifier}.lock",
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise ChatError("external endpoint is busy; nothing was delivered") from error
        yield descriptor
    finally:
        os.close(descriptor)


def read_credential(path: Path) -> str:
    """Read a bounded private credential without printing its value or path."""
    try:
        require_private_file(path)
        with path.open("rb") as stream:
            raw = stream.read(256)
        value = raw.decode("ascii").strip()
    except (ChatError, OSError, UnicodeDecodeError) as error:
        raise ChatError("external credential file is unavailable or unsafe") from error
    if _CREDENTIAL.fullmatch(value) is None:
        raise ChatError("external credential file is invalid")
    return value


def write_credential(path: Path, credential: str) -> None:
    """Create a new owner-private file; never overwrite an existing credential."""
    ensure_private_dir(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(credential + "\n")
        stream.flush()
        os.fsync(stream.fileno())
