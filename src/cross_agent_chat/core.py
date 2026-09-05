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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, NoReturn, cast
from uuid import UUID, uuid4

Provider = Literal["claude", "codex"]
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
    if any(unicodedata.category(character).startswith("C") for character in value):
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
    valid_uuid(session_id, "session id")
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
        if self.provider not in {"claude", "codex"}:
            fail("route provider is invalid")
        valid_uuid(self.session_id, "session id")
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
        if provider not in {"claude", "codex"}:
            fail("route provider is invalid")
        typed_provider = cast(Provider, provider)
        canonical = canonical_cwd(cwd)
        project = valid_name(Path(canonical).name, "project")
        return cls(
            schema_version=SCHEMA_VERSION,
            provider=typed_provider,
            session_id=valid_uuid(session_id, "session id"),
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
            or values["provider"] not in {"claude", "codex"}
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

    def routes(self) -> list[Route]:
        if not self.path.exists():
            return []
        require_private_file(self.path)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChatError("route registry is invalid") from error
        if not isinstance(raw, list):
            fail("route registry schema is unsupported")
        routes = [Route.from_object(item) for item in raw]
        identities = {(item.provider, item.session_id) for item in routes}
        if len(identities) != len(routes):
            fail("route registry contains duplicate identities")
        return routes

    def upsert(self, route: Route) -> None:
        with state_lock(self.root, "routes"):
            existing = [
                item
                for item in self.routes()
                if (item.provider, item.session_id) != (route.provider, route.session_id)
            ]
            atomic_json(self.path, [item.to_dict() for item in [*existing, route]])

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
            atomic_json(self.path, [item.to_dict() for item in [*retained, route]])
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
            atomic_json(self.path, [item.to_dict() for item in retained])

    def current(self, route: Route) -> bool:
        return any(item == route for item in self.routes())

    def compact_dead(self) -> list[Route]:
        """Remove only routes whose provider process is definitively gone."""
        with state_lock(self.root, "routes"):
            existing = self.routes()
            retained = [item for item in existing if item.process_is_live()]
            if len(retained) != len(existing):
                atomic_json(self.path, [item.to_dict() for item in retained])
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
            unresolved = [
                item
                for item in existing
                if item.target_key == target_key
                and item.status in {"PENDING", "REMOTE_AUTHORIZED", "UNKNOWN_DELIVERY"}
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
        with state_lock(self.root, "intents"):
            existing = self.intents()
            matches = [item for item in existing if item.event_id == event_id]
            if len(matches) != 1:
                fail("intent is unavailable")
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
                    status=status if item.event_id == event_id else item.status,
                    timestamp=utc_now() if item.event_id == event_id else item.timestamp,
                )
                for item in existing
            ]
            atomic_json(self.path, [item.to_dict() for item in updated])


def authenticate_sender(
    routes: list[Route], provider: str, parent_pid: int, host_thread_id: str | None
) -> Route:
    if provider == "claude":
        matches = [item for item in routes if item.provider == "claude" and item.pid == parent_pid]
        if len(matches) != 1:
            fail("exact Claude sender is unavailable")
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
