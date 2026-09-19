"""Exact route identity and content-free delivery state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, NoReturn, cast
from uuid import UUID, uuid4

Provider = Literal["claude", "codex", "devin"]
IntentStatus = Literal[
    "PENDING",
    "REMOTE_AUTHORIZED",
    "PRE_EFFECT_REJECTED",
    "TRANSPORT_ACCEPTED",
    "UNKNOWN_DELIVERY",
    "RESOLVED_BY_OWNER",
]

SCHEMA_VERSION: Final = 1
MAX_MESSAGE_BYTES: Final = 16 * 1024
CODEX_ALIAS_DIGEST_LENGTH: Final = 12
MAX_NAME_CODEPOINTS: Final = 128
MAX_ALIAS_CODEPOINTS: Final = 128
SAFE_DEVICE_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}\Z")
UUID_RE: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
DEVIN_SESSION_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")
# Devin documents this as an opaque session id. Keep a bounded, path-safe
# representation so hooks can bind it without assuming UUIDs or exposing it.


class ChatError(RuntimeError):
    """A deterministic Cross Agent Chat rejection."""


class UnknownDeliveryError(ChatError):
    """The transport may have produced an external effect."""


def fail(message: str) -> NoReturn:
    raise ChatError(message)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def valid_uuid(value: str, field: str) -> str:
    if UUID_RE.fullmatch(value) is None:
        fail(f"{field} is invalid")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ChatError(f"{field} is invalid") from error
    if str(parsed) != value:
        fail(f"{field} is invalid")
    return value


def valid_session_id(provider: str, value: str, field: str = "session id") -> str:
    if provider == "devin":
        if DEVIN_SESSION_RE.fullmatch(value) is None:
            fail(f"{field} is invalid")
        return value
    return valid_uuid(value, field)


def valid_device(value: str) -> str:
    if SAFE_DEVICE_RE.fullmatch(value) is None:
        fail("device name is invalid")
    return value


def valid_name(value: str, field: str) -> str:
    if not value or len(value) > MAX_NAME_CODEPOINTS:
        fail(f"{field} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        fail(f"{field} is invalid")
    if any(unicodedata.category(character).startswith(("C", "Zl", "Zp")) for character in value):
        fail(f"{field} is invalid")
    return value


def canonical_cwd(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        fail("working directory must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ChatError("working directory is unavailable") from error
    if not resolved.is_dir() or len(str(resolved)) > 512:
        fail("working directory is invalid")
    return str(resolved)


def stored_cwd(value: str) -> str:
    """Validate a persisted route path without requiring its workspace to survive."""
    path = Path(value)
    if (
        not path.is_absolute()
        or len(value) > 512
        or "\x00" in value
        or os.path.normpath(value) != value
    ):
        fail("working directory is invalid")
    return value


def session_key(provider: Provider, session_id: str) -> str:
    valid_session_id(provider, session_id)
    return hashlib.sha256(f"{provider}:{session_id}".encode()).hexdigest()


def friendly_alias(provider: Provider, device: str, project: str, session_id: str) -> str:
    prefix = f"{provider}@{valid_device(device)}:"
    suffix = (
        f":{session_key(provider, session_id)[:CODEX_ALIAS_DIGEST_LENGTH]}"
        if provider == "codex"
        else ""
    )
    exact_project = valid_name(project, "project")
    available = MAX_ALIAS_CODEPOINTS - len(prefix) - len(suffix)
    if available <= 0:
        fail("route alias is invalid")
    if len(exact_project) > available:
        digest = hashlib.sha256(exact_project.encode()).hexdigest()[:CODEX_ALIAS_DIGEST_LENGTH]
        marker = f"~{digest}"
        exact_project = exact_project[: available - len(marker)] + marker
    alias = f"{prefix}{exact_project}{suffix}"
    if len(alias) > MAX_ALIAS_CODEPOINTS:
        fail("route alias is invalid")
    return alias


def bounded_message(message: str) -> str:
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ChatError("message is invalid") from error
    if not message:
        fail("message must not be empty")
    if "\x00" in message or len(encoded) > MAX_MESSAGE_BYTES:
        fail("message exceeds the 16 KiB limit")
    # Leave room for provenance and protocol fields within the 64 KiB frame,
    # including control characters that expand to six bytes in JSON.
    if len(json.dumps(message, ensure_ascii=False).encode()) > 2 * MAX_MESSAGE_BYTES + 2:
        fail("message exceeds the encoded frame budget")
    return message


# A send is bounded by OPERATION_TIMEOUT_SECONDS, so a PENDING or
# REMOTE_AUTHORIZED row older than this is probably an orphan: a live operation
# would normally have marked it. Elapsed time is a heuristic, not proof -- a
# suspended host can resume and record its result later, and that result then
# replaces any owner disposition. The margin covers clock skew and a slow write.
ABANDONED_INTENT_SECONDS: Final = 600.0


def intent_age_seconds(timestamp: str) -> float:
    """Return how long ago an intent row was written, in seconds."""
    return (datetime.now(UTC) - _parse_timestamp(timestamp)).total_seconds()


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ChatError("route timestamp is invalid") from error
    if parsed.tzinfo is None:
        fail("route timestamp is invalid")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Route:
    """One generation of one live provider process."""

    schema_version: int
    provider: Provider
    session_id: str
    device: str
    cwd: str
    project: str
    alias: str
    generation: str
    pid: int
    last_seen: str
    owner_identity: str | None = None
    profile_root: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            fail("route schema is unsupported")
        if self.provider not in {"claude", "codex", "devin"}:
            fail("route provider is invalid")
        valid_session_id(self.provider, self.session_id)
        valid_uuid(self.generation, "route generation")
        valid_device(self.device)
        if stored_cwd(self.cwd) != self.cwd:
            fail("route working directory is not canonical")
        valid_name(self.project, "project")
        expected = friendly_alias(self.provider, self.device, self.project, self.session_id)
        if self.alias != expected:
            fail("route alias conflicts with its identity")
        if isinstance(self.pid, bool) or self.pid <= 0:
            fail("route process is invalid")
        _parse_timestamp(self.last_seen)
        if (
            self.owner_identity is not None
            and re.fullmatch(r"[0-9a-f]{64}", self.owner_identity) is None
        ):
            fail("route owner identity is invalid")
        if self.profile_root is not None:
            stored_cwd(self.profile_root)
            if self.owner_identity is None:
                fail("route profile has no owner identity")

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        session_id: str,
        device: str,
        cwd: str,
        pid: int,
        generation: str | None = None,
        owner_identity: str | None = None,
        profile_root: str | None = None,
    ) -> Route:
        if provider not in {"claude", "codex", "devin"}:
            fail("route provider is invalid")
        typed_provider = cast(Provider, provider)
        canonical = canonical_cwd(cwd)
        project = valid_name(Path(canonical).name or canonical, "project")
        return cls(
            schema_version=SCHEMA_VERSION,
            provider=typed_provider,
            session_id=valid_session_id(typed_provider, session_id),
            device=valid_device(device),
            cwd=canonical,
            project=project,
            alias=friendly_alias(typed_provider, device, project, session_id),
            generation=str(uuid4())
            if generation is None
            else valid_uuid(generation, "route generation"),
            pid=pid,
            last_seen=utc_now(),
            owner_identity=owner_identity,
            profile_root=profile_root,
        )

    @classmethod
    def from_object(cls, raw: object) -> Route:
        fields = {
            "schema_version",
            "provider",
            "session_id",
            "device",
            "cwd",
            "project",
            "alias",
            "generation",
            "pid",
            "last_seen",
        }
        if not isinstance(raw, dict) or (
            set(raw)
            not in (
                fields,
                fields | {"owner_identity"},
                fields | {"owner_identity", "profile_root"},
            )
        ):
            fail("route schema is unsupported")
        values = cast(dict[str, object], raw)
        if (
            not isinstance(values["schema_version"], int)
            or isinstance(values["schema_version"], bool)
            or values["provider"] not in {"claude", "codex", "devin"}
            or not all(
                isinstance(values[key], str)
                for key in (
                    "session_id",
                    "device",
                    "cwd",
                    "project",
                    "alias",
                    "generation",
                    "last_seen",
                )
            )
            or not isinstance(values["pid"], int)
            or isinstance(values["pid"], bool)
            or ("owner_identity" in values and not isinstance(values["owner_identity"], str))
            or ("profile_root" in values and not isinstance(values["profile_root"], str))
        ):
            fail("route schema is unsupported")
        return cls(
            schema_version=values["schema_version"],
            provider=values["provider"],
            session_id=cast(str, values["session_id"]),
            device=cast(str, values["device"]),
            cwd=cast(str, values["cwd"]),
            project=cast(str, values["project"]),
            alias=cast(str, values["alias"]),
            generation=cast(str, values["generation"]),
            pid=values["pid"],
            last_seen=cast(str, values["last_seen"]),
            owner_identity=cast(str | None, values.get("owner_identity")),
            profile_root=cast(str | None, values.get("profile_root")),
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "session_id": self.session_id,
            "device": self.device,
            "cwd": self.cwd,
            "project": self.project,
            "alias": self.alias,
            "generation": self.generation,
            "pid": self.pid,
            "last_seen": self.last_seen,
        }
        if self.owner_identity is not None:
            result["owner_identity"] = self.owner_identity
        if self.profile_root is not None:
            result["profile_root"] = self.profile_root
        return result

    def process_is_live(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    def cwd_is_available(self) -> bool:
        try:
            return canonical_cwd(self.cwd) == self.cwd
        except ChatError:
            return False


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    metadata = path.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        fail(f"private directory is unsafe: {path}")


def require_private_file(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        fail(f"private file is unsafe: {path}")


def atomic_json(path: Path, value: object) -> None:
    ensure_private_dir(path.parent)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def state_lock(root: Path, name: str) -> Iterator[None]:
    ensure_private_dir(root)
    lock_path = root / f".{name}.lock"
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


class Registry:
    """Atomic, private route metadata. It never stores message bodies."""

    def __init__(self, root: Path) -> None:
        self.root = root
        ensure_private_dir(root)
        self.path = root / "routes.json"
        self.devin_path = root / "devin-routes.json"

    def routes(self) -> list[Route]:
        routes = self._read(self.path, expected_provider="legacy")
        routes.extend(self._read(self.devin_path, expected_provider="devin"))
        identities = {(item.provider, item.session_id) for item in routes}
        if len(identities) != len(routes):
            fail("route registry contains duplicate identities")
        return routes

    def _read(self, path: Path, *, expected_provider: Literal["legacy", "devin"]) -> list[Route]:
        if not path.exists():
            return []
        require_private_file(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChatError("route registry is invalid") from error
        if not isinstance(raw, list):
            fail("route registry schema is unsupported")
        routes = [Route.from_object(item) for item in raw]
        if expected_provider == "legacy" and any(item.provider == "devin" for item in routes):
            fail("legacy route registry contains unsupported provider")
        if expected_provider == "devin" and any(item.provider != "devin" for item in routes):
            fail("Devin route registry contains unsupported provider")
        return routes

    def _write(self, routes: list[Route]) -> None:
        legacy = [item for item in routes if item.provider != "devin"]
        devin = [item for item in routes if item.provider == "devin"]
        atomic_json(self.path, [item.to_dict() for item in legacy])
        if devin:
            atomic_json(self.devin_path, [item.to_dict() for item in devin])
        else:
            self.devin_path.unlink(missing_ok=True)

    def upsert(self, route: Route) -> None:
        with state_lock(self.root, "routes"):
            existing = [
                item
                for item in self.routes()
                if (item.provider, item.session_id) != (route.provider, route.session_id)
            ]
            self._write([*existing, route])

    def upsert_or_reuse_live_owner(self, route: Route) -> Route:
        """Preserve a live generation when an identical provider hook repeats."""
        with state_lock(self.root, "routes"):
            existing = self.routes()
            matching = [
                item
                for item in existing
                if item.provider == route.provider
                and item.session_id == route.session_id
                and item.device == route.device
                and item.cwd == route.cwd
                and item.pid == route.pid
                and item.owner_identity is not None
                and item.owner_identity == route.owner_identity
            ]
            if len(matching) == 1:
                return matching[0]
            retained = [
                item
                for item in existing
                if (item.provider, item.session_id) != (route.provider, route.session_id)
            ]
            self._write([*retained, route])
            return route

    def remove(
        self, provider: Provider, session_id: str, pid: int, *, generation: str | None = None
    ) -> None:
        with state_lock(self.root, "routes"):
            existing = self.routes()
            retained = [
                item
                for item in existing
                if (item.provider, item.session_id, item.pid) != (provider, session_id, pid)
                or (generation is not None and item.generation != generation)
            ]
            self._write(retained)

    def current(self, route: Route) -> bool:
        return any(item == route for item in self.routes())

    def compact_dead(self) -> list[Route]:
        """Remove only routes whose provider process is definitively gone."""
        with state_lock(self.root, "routes"):
            existing = self.routes()
            retained = [item for item in existing if item.process_is_live()]
            if len(retained) != len(existing):
                self._write(retained)
            return retained


@dataclass(frozen=True, slots=True)
class Intent:
    schema_version: int
    event_id: str
    source_key: str
    source_generation: str
    source_alias: str
    target_key: str
    target_generation: str
    payload_digest: str
    status: IntentStatus
    timestamp: str

    @classmethod
    def from_object(cls, raw: object) -> Intent:
        fields = {
            "schema_version",
            "event_id",
            "source_key",
            "source_generation",
            "source_alias",
            "target_key",
            "target_generation",
            "payload_digest",
            "status",
            "timestamp",
        }
        if not isinstance(raw, dict) or set(raw) != fields:
            fail("intent schema is unsupported")
        values = cast(dict[str, object], raw)
        allowed = {
            "PENDING",
            "REMOTE_AUTHORIZED",
            "PRE_EFFECT_REJECTED",
            "TRANSPORT_ACCEPTED",
            "UNKNOWN_DELIVERY",
            "RESOLVED_BY_OWNER",
        }
        if (
            values["schema_version"] != SCHEMA_VERSION
            or not all(
                isinstance(values[key], str)
                for key in (
                    "event_id",
                    "source_key",
                    "source_generation",
                    "source_alias",
                    "target_key",
                    "target_generation",
                    "payload_digest",
                    "status",
                    "timestamp",
                )
            )
            or values["status"] not in allowed
        ):
            fail("intent schema is unsupported")
        valid_uuid(cast(str, values["event_id"]), "event id")
        valid_uuid(cast(str, values["source_generation"]), "source generation")
        valid_uuid(cast(str, values["target_generation"]), "target generation")
        valid_name(cast(str, values["source_alias"]), "source alias")
        for field in ("source_key", "target_key", "payload_digest"):
            if re.fullmatch(r"[0-9a-f]{64}", cast(str, values[field])) is None:
                fail("intent schema is unsupported")
        _parse_timestamp(cast(str, values["timestamp"]))
        return cls(
            schema_version=SCHEMA_VERSION,
            event_id=cast(str, values["event_id"]),
            source_key=cast(str, values["source_key"]),
            source_generation=cast(str, values["source_generation"]),
            source_alias=cast(str, values["source_alias"]),
            target_key=cast(str, values["target_key"]),
            target_generation=cast(str, values["target_generation"]),
            payload_digest=cast(str, values["payload_digest"]),
            status=cast(IntentStatus, values["status"]),
            timestamp=cast(str, values["timestamp"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "source_key": self.source_key,
            "source_generation": self.source_generation,
            "source_alias": self.source_alias,
            "target_key": self.target_key,
            "target_generation": self.target_generation,
            "payload_digest": self.payload_digest,
            "status": self.status,
            "timestamp": self.timestamp,
        }


class IntentStore:
    """Durable delivery status without message content."""

    def __init__(self, root: Path) -> None:
        self.root = root
        ensure_private_dir(root)
        self.path = root / "intents.json"

    def intents(self) -> list[Intent]:
        if not self.path.exists():
            return []
        require_private_file(self.path)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChatError("intent state is invalid") from error
        if not isinstance(raw, list):
            fail("intent schema is unsupported")
        intents = [Intent.from_object(item) for item in raw]
        if len({item.event_id for item in intents}) != len(intents):
            fail("intent state contains duplicate events")
        return intents

    def intent_for_source(
        self, *, event_id: str, source_key: str, source_generation: str
    ) -> Intent | None:
        """Read one exact source-owned event without changing durable state."""
        identifier = valid_uuid(event_id, "event id")
        valid_uuid(source_generation, "source generation")
        if re.fullmatch(r"[0-9a-f]{64}", source_key) is None:
            fail("source key is invalid")
        matches = [
            item
            for item in self.intents()
            if (
                item.event_id == identifier
                and item.source_key == source_key
                and item.source_generation == source_generation
            )
        ]
        if len(matches) > 1:
            fail("intent state contains duplicate events")
        return matches[0] if matches else None

    def begin(
        self,
        source: Route,
        target: Route,
        *,
        source_alias: str,
        payload_digest: str,
        event_id: str | None = None,
    ) -> str:
        return self.begin_identity(
            source_key=session_key(source.provider, source.session_id),
            source_generation=source.generation,
            source_alias=source_alias,
            target_key=session_key(target.provider, target.session_id),
            target_generation=target.generation,
            payload_digest=payload_digest,
            event_id=event_id,
        )

    def begin_identity(
        self,
        *,
        source_key: str,
        source_generation: str,
        source_alias: str,
        target_key: str,
        target_generation: str,
        payload_digest: str,
        event_id: str | None = None,
    ) -> str:
        identifier = str(uuid4()) if event_id is None else valid_uuid(event_id, "event id")
        if re.fullmatch(r"[0-9a-f]{64}", source_key) is None:
            fail("source key is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", target_key) is None:
            fail("target key is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", payload_digest) is None:
            fail("message digest is invalid")
        valid_uuid(source_generation, "source generation")
        valid_name(source_alias, "source alias")
        valid_uuid(target_generation, "target generation")
        with state_lock(self.root, "intents"):
            existing = self.intents()
            if any(item.event_id == identifier for item in existing):
                fail("event id is unavailable")
            unresolved = [
                item
                for item in existing
                if item.target_key == target_key and item.status in {"PENDING", "REMOTE_AUTHORIZED"}
            ]
            if unresolved:
                fail("target has an unresolved delivery intent")
            intent = Intent(
                schema_version=SCHEMA_VERSION,
                event_id=identifier,
                source_key=source_key,
                source_generation=source_generation,
                source_alias=source_alias,
                target_key=target_key,
                target_generation=target_generation,
                payload_digest=payload_digest,
                status="PENDING",
                timestamp=utc_now(),
            )
            atomic_json(self.path, [item.to_dict() for item in [*existing, intent]])
        return identifier

    def claim_remote_authorization(
        self,
        *,
        event_id: str,
        source_generation: str,
        source_alias: str,
        target_key: str,
        target_generation: str,
        payload_digest: str,
    ) -> bool:
        """Atomically claim one exact authenticated intent for remote delivery."""
        valid_uuid(event_id, "event id")
        valid_uuid(source_generation, "source generation")
        valid_name(source_alias, "source alias")
        valid_uuid(target_generation, "target generation")
        for value, field in (
            (target_key, "target key"),
            (payload_digest, "message digest"),
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                fail(f"{field} is invalid")
        with state_lock(self.root, "intents"):
            existing = self.intents()
            matches = [
                item
                for item in existing
                if (
                    item.event_id == event_id
                    and item.source_generation == source_generation
                    and item.source_alias == source_alias
                    and item.target_key == target_key
                    and item.target_generation == target_generation
                    and item.payload_digest == payload_digest
                    and item.status == "PENDING"
                )
            ]
            if len(matches) != 1:
                return False
            updated = [
                Intent(
                    schema_version=item.schema_version,
                    event_id=item.event_id,
                    source_key=item.source_key,
                    source_generation=item.source_generation,
                    source_alias=item.source_alias,
                    target_key=item.target_key,
                    target_generation=item.target_generation,
                    payload_digest=item.payload_digest,
                    status="REMOTE_AUTHORIZED" if item.event_id == event_id else item.status,
                    timestamp=utc_now() if item.event_id == event_id else item.timestamp,
                )
                for item in existing
            ]
            atomic_json(self.path, [item.to_dict() for item in updated])
            return True

    def mark(self, event_id: str, status: IntentStatus) -> None:
        valid_uuid(event_id, "event id")
        if status not in {
            "PENDING",
            "REMOTE_AUTHORIZED",
            "PRE_EFFECT_REJECTED",
            "TRANSPORT_ACCEPTED",
            "UNKNOWN_DELIVERY",
            "RESOLVED_BY_OWNER",
        }:
            fail("intent status is invalid")
        # A delivery outcome recorded after an owner disposition replaces it:
        # RESOLVED_BY_OWNER labels an undecided record and is never a result.
        with state_lock(self.root, "intents"):
            existing = self.intents()
            self._require_one(existing, event_id)
            self._write_status(existing, event_id, status)

    def resolve_by_owner(self, event_id: str) -> IntentStatus:
        """Record the owner's acceptance of one undecided event and return its prior status.

        Eligibility and the write are one transition under the intents lock, so a
        result recorded concurrently is refused as decided rather than overwritten.
        """
        valid_uuid(event_id, "event id")
        with state_lock(self.root, "intents"):
            existing = self.intents()
            current = self._require_one(existing, event_id)
            if current.status == "RESOLVED_BY_OWNER":
                return current.status
            # TRANSPORT_ACCEPTED and PRE_EFFECT_REJECTED are decided; resolving one
            # would launder a known result into an owner disposition.
            if current.status not in {"UNKNOWN_DELIVERY", "PENDING", "REMOTE_AUTHORIZED"}:
                fail(
                    f"event {event_id} is {current.status}, which is already decided; "
                    "nothing to resolve"
                )
            if current.status in {"PENDING", "REMOTE_AUTHORIZED"}:
                # A young row may still belong to a running send. Unblocking its
                # target would let the owner start the same work again and have
                # both arrive.
                age = intent_age_seconds(current.timestamp)
                if age < ABANDONED_INTENT_SECONDS:
                    fail(
                        f"event {event_id} is {current.status} and only "
                        f"{int(age)}s old, so it may still be in flight; wait until it "
                        f"is at least {int(ABANDONED_INTENT_SECONDS)}s old, then resolve"
                    )
            self._write_status(existing, event_id, "RESOLVED_BY_OWNER")
            return current.status

    @staticmethod
    def _require_one(existing: list[Intent], event_id: str) -> Intent:
        matches = [item for item in existing if item.event_id == event_id]
        if len(matches) != 1:
            fail("intent is unavailable")
        return matches[0]

    def _write_status(self, existing: list[Intent], event_id: str, status: IntentStatus) -> None:
        """Rewrite one row's status. The caller holds the intents lock."""
        updated = [
            replace(item, status=status, timestamp=utc_now()) if item.event_id == event_id else item
            for item in existing
        ]
        atomic_json(self.path, [item.to_dict() for item in updated])


# Recipient bindings are requester-side memory, not authority: each row only
# records which transport endpoint verifiably presented a handle most recently.
RECIPIENT_BINDING_TTL_SECONDS: Final = 7.0 * 24 * 60 * 60
RECIPIENT_BINDING_LIMIT: Final = 512
RECIPIENT_ADDRESS_LIMIT: Final = 64


@dataclass(frozen=True, slots=True)
class RecipientBinding:
    """The endpoint(s) that last verifiably presented one recipient handle.

    One endpoint is a bound owner; two or more means one listing attested the
    same handle from different devices and the handle is ambiguous.
    """

    handle: str
    endpoints: dict[str, str]
    seen_at: str

    @classmethod
    def from_object(cls, raw: object) -> RecipientBinding | None:
        """Parse one durable row, tolerating corruption as no row at all."""
        if not isinstance(raw, dict) or set(raw) != {"handle", "endpoints", "seen_at"}:
            return None
        values = cast(dict[str, object], raw)
        handle = values["handle"]
        seen_at = values["seen_at"]
        endpoints = values["endpoints"]
        if (
            not isinstance(handle, str)
            or re.fullmatch(r"[0-9a-f]{64}", handle) is None
            or not isinstance(seen_at, str)
            or not isinstance(endpoints, dict)
            or not endpoints
            or len(endpoints) > 16
        ):
            return None
        items = cast(dict[object, object], endpoints)
        try:
            _parse_timestamp(seen_at)
            clean = {
                address: valid_uuid(generation, "bound generation")
                for address, generation in ((key, value) for key, value in items.items())
                if isinstance(address, str)
                and 0 < len(address) <= RECIPIENT_ADDRESS_LIMIT
                and isinstance(generation, str)
            }
        except ChatError:
            return None
        if len(clean) != len(items):
            return None
        return cls(handle=handle, endpoints=clean, seen_at=seen_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "handle": self.handle,
            "endpoints": self.endpoints,
            "seen_at": self.seen_at,
        }


class RecipientBindings:
    """Private, bounded handle-to-endpoint memory. It never stores content."""

    def __init__(self, root: Path) -> None:
        self.root = root
        ensure_private_dir(root)
        self.path = root / "recipients.json"

    def bindings(self) -> list[RecipientBinding]:
        """Read every row; a missing or corrupt file is simply no memory."""
        if not self.path.exists():
            return []
        try:
            require_private_file(self.path)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ChatError):
            return []
        if not isinstance(raw, list):
            return []
        by_handle: dict[str, RecipientBinding] = {}
        for item in raw:
            binding = RecipientBinding.from_object(item)
            if binding is None:
                continue
            current = by_handle.get(binding.handle)
            if current is None or _parse_timestamp(binding.seen_at) > _parse_timestamp(
                current.seen_at
            ):
                by_handle[binding.handle] = binding
        return list(by_handle.values())

    def binding_for(self, handle: str) -> RecipientBinding | None:
        if re.fullmatch(r"[0-9a-f]{64}", handle) is None:
            return None
        for binding in self.bindings():
            if binding.handle == handle:
                return binding
        return None

    def record(self, handle: str, address: str, generation: str) -> None:
        """Bind one handle to the single endpoint that verifiably presented it."""
        self.record_observations({handle: {address: generation}})

    def record_observations(self, observations: dict[str, dict[str, str]]) -> None:
        """Replace the endpoint map for each observed handle in one locked write."""
        clean: dict[str, dict[str, str]] = {}
        for handle, endpoints in observations.items():
            if (
                re.fullmatch(r"[0-9a-f]{64}", handle) is None
                or not endpoints
                or len(endpoints) > 16
            ):
                fail("recipient binding is invalid")
            checked: dict[str, str] = {}
            for address, generation in endpoints.items():
                if not isinstance(address, str) or not 0 < len(address) <= (
                    RECIPIENT_ADDRESS_LIMIT
                ):
                    fail("recipient binding is invalid")
                checked[address] = valid_uuid(generation, "bound generation")
            clean[handle] = checked
        if not clean:
            return
        with state_lock(self.root, "recipients"):
            by_handle = {item.handle: item for item in self.bindings()}
            now = utc_now()
            for handle, endpoints in clean.items():
                by_handle[handle] = RecipientBinding(
                    handle=handle, endpoints=endpoints, seen_at=now
                )
            cutoff = datetime.now(UTC) - timedelta(seconds=RECIPIENT_BINDING_TTL_SECONDS)
            retained = sorted(
                (
                    binding
                    for binding in by_handle.values()
                    if _parse_timestamp(binding.seen_at) >= cutoff
                ),
                key=lambda binding: _parse_timestamp(binding.seen_at),
                reverse=True,
            )[:RECIPIENT_BINDING_LIMIT]
            atomic_json(self.path, [binding.to_dict() for binding in retained])


def authenticate_sender(
    routes: list[Route], provider: str, parent_pid: int, host_thread_id: str | None
) -> Route:
    if provider in {"claude", "devin"}:
        matches = [item for item in routes if item.provider == provider and item.pid == parent_pid]
        if len(matches) != 1:
            fail(f"exact {provider.capitalize()} sender is unavailable")
        return matches[0]
    if provider == "codex":
        if host_thread_id is None:
            fail("Codex host thread identity is required")
        identifier = valid_uuid(host_thread_id, "Codex host thread identity")
        matches = [
            item
            for item in routes
            if item.provider == "codex" and item.session_id == identifier and item.pid == parent_pid
        ]
        if len(matches) != 1:
            fail("exact Codex sender is unavailable")
        return matches[0]
    fail("sender provider is invalid")


def _query_matches(route: Route, query: str) -> bool:
    if query.casefold() == route.alias.casefold():
        return True
    query_tokens = re.findall(r"[^\W_]+", query.casefold())
    route_tokens = set(re.findall(r"[^\W_]+", route.alias.casefold()))
    return bool(query_tokens) and all(token in route_tokens for token in query_tokens)


def resolve_target(routes: list[Route], query: str) -> Route:
    if not query.strip() or len(query) > 160:
        fail("target query is invalid")
    matches = [item for item in routes if _query_matches(item, query)]
    if len(matches) != 1:
        candidates = ", ".join(item.alias for item in matches)
        suffix = f": {candidates}" if candidates else ""
        fail(f"target is ambiguous or unavailable{suffix}")
    return matches[0]
