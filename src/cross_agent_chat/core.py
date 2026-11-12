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
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, NoReturn, cast
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
OWNER_ANCHOR_RE: Final = re.compile(
    r"owner-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json\Z"
)
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
# replaces any owner disposition. Deadline room only ever permits an attempt to
# begin; it cannot promise a definitive answer, so a receipt lost after possible
# acceptance stays legitimately UNKNOWN_DELIVERY rather than a guessed outcome.
# The margin covers clock skew and a slow write.
ABANDONED_INTENT_SECONDS: Final = 600.0

# Decided intent rows older than this leave the hot file for the dated archive.
# The horizon is deliberately short: archived ids stay uniquely owned and
# checkable forever, undecided rows never leave the hot file, and a late
# outcome can still update an archived row in place.
INTENT_ARCHIVE_AGE_SECONDS: Final = 7.0 * 86400.0
# Compaction piggybacks on intents-lock mutations, so it only ever runs under
# the lock; the floors keep tiny stores from paying archive bookkeeping at all.
INTENT_COMPACTION_MIN_ROWS: Final = 2048
INTENT_COMPACTION_MIN_ARCHIVED: Final = 256
_INTENT_TERMINAL_STATUSES: Final = frozenset(
    {"TRANSPORT_ACCEPTED", "PRE_EFFECT_REJECTED", "RESOLVED_BY_OWNER"}
)
# A receipt only matters while its event's custody is undecided; they are
# pruned with intent compaction at this horizon, well past the intent archive
# horizon, so a late reader can still find the transport's proof.
RECEIPT_RETENTION_SECONDS: Final = 90.0 * 86400.0


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


def owner_anchor_path(root: Path, generation: str) -> Path:
    """Return the private sidecar binding one route generation to its image."""
    return root / f"owner-{valid_uuid(generation, 'route generation')}.json"


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
        self._prune_owner_anchors(routes)

    def _prune_owner_anchors(self, retained: list[Route]) -> None:
        """Drop image anchors whose generation no registry entry still owns."""
        generations = {item.generation for item in retained}
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return
        for entry in entries:
            match = OWNER_ANCHOR_RE.fullmatch(entry.name)
            if match is not None and match.group(1) not in generations:
                with suppress(OSError):
                    entry.unlink()

    def _publish_owner_anchor(self, route: Route, anchor: dict[str, object] | None) -> None:
        """Write an image anchor inside the routes lock, before the route.

        Keeping this in the same critical section as _write means another
        session's compaction can never prune an anchor between its write and
        the route's publication.
        """
        if anchor is None:
            return
        with suppress(OSError, ChatError):
            atomic_json(owner_anchor_path(self.root, route.generation), anchor)

    def upsert(self, route: Route, *, anchor: dict[str, object] | None = None) -> None:
        with state_lock(self.root, "routes"):
            existing = [
                item
                for item in self.routes()
                if (item.provider, item.session_id) != (route.provider, route.session_id)
            ]
            self._publish_owner_anchor(route, anchor)
            self._write([*existing, route])

    def upsert_or_reuse_live_owner(
        self, route: Route, *, anchor: dict[str, object] | None = None
    ) -> Route:
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
            self._publish_owner_anchor(route, anchor)
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
            else:
                self._prune_owner_anchors(retained)
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
        self.archive = root / "intents-archive"

    def intents(self) -> list[Intent]:
        return self._read_intents(self.path, "intent state")

    @staticmethod
    def _read_intents(path: Path, label: str) -> list[Intent]:
        if not path.exists():
            return []
        require_private_file(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChatError(f"{label} is invalid") from error
        if not isinstance(raw, list):
            fail("intent schema is unsupported")
        intents = [Intent.from_object(item) for item in raw]
        if len({item.event_id for item in intents}) != len(intents):
            fail(f"{label} contains duplicate events")
        return intents

    def _archived_months(self) -> dict[str, str]:
        """The event-id -> archive month index; absent index means no archives."""
        path = self.archive / "ids.json"
        if not path.exists():
            return {}
        require_private_file(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChatError("intent archive index is invalid") from error
        months = raw.get("months") if isinstance(raw, dict) else None
        if (
            raw.get("schema_version") != SCHEMA_VERSION
            or set(raw) != {"schema_version", "months"}
            or not isinstance(months, dict)
            or not all(
                isinstance(event_id, str)
                and UUID_RE.fullmatch(event_id) is not None
                and isinstance(month, str)
                and re.fullmatch(r"[0-9]{4}-[0-9]{2}", month) is not None
                for event_id, month in months.items()
            )
        ):
            fail("intent archive index is invalid")
        return cast(dict[str, str], months)

    def _read_archive(self, month: str) -> list[Intent]:
        return self._read_intents(self.archive / f"{month}.json", "intent archive")

    def archived(self, event_id: str) -> Intent | None:
        """Read one exact archived event without changing durable state."""
        identifier = valid_uuid(event_id, "event id")
        month = self._archived_months().get(identifier)
        if month is None:
            return None
        matches = [item for item in self._read_archive(month) if item.event_id == identifier]
        if len(matches) > 1:
            fail("intent archive contains duplicate events")
        return matches[0] if matches else None

    def lookup(self, event_id: str) -> Intent | None:
        """Read one exact event from active or archived state."""
        identifier = valid_uuid(event_id, "event id")
        matches = [item for item in self.intents() if item.event_id == identifier]
        if len(matches) > 1:
            fail("intent state contains duplicate events")
        if matches:
            return matches[0]
        return self.archived(identifier)

    def intent_for_source(
        self, *, event_id: str, source_key: str, source_generation: str
    ) -> Intent | None:
        """Read one exact source-owned event without changing durable state."""
        identifier = valid_uuid(event_id, "event id")
        valid_uuid(source_generation, "source generation")
        if re.fullmatch(r"[0-9a-f]{64}", source_key) is None:
            fail("source key is invalid")
        item = self.lookup(identifier)
        if (
            item is not None
            and item.source_key == source_key
            and item.source_generation == source_generation
        ):
            return item
        return None

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
            # An event id must stay uniquely owned for the archive horizon too:
            # reusing an id already decided and moved out of the hot file would
            # record a second delivery under one custody record. The index is
            # consulted only for caller-chosen ids -- a fresh uuid4 cannot
            # realistically collide.
            if any(item.event_id == identifier for item in existing) or (
                event_id is not None and identifier in self._archived_months()
            ):
                fail("event id is unavailable")
            # The in-flight gate is per (source, target) pair: this sender must
            # not start a second send while its own earlier event to this
            # recipient is still undecided, but another session's unresolved
            # intent is a different event and must not block this one -- the
            # recipient courier already serializes provider effects.
            unresolved = [
                item
                for item in existing
                if item.source_key == source_key
                and item.target_key == target_key
                and item.status in {"PENDING", "REMOTE_AUTHORIZED"}
            ]
            if unresolved:
                blocker = min(unresolved, key=lambda item: intent_age_seconds(item.timestamp))
                age = intent_age_seconds(blocker.timestamp)
                if age >= ABANDONED_INTENT_SECONDS:
                    fail(
                        "target has an unresolved delivery intent from this sender: "
                        f"event {blocker.event_id} is still {blocker.status} "
                        f"({int(age)}s old -- age alone does not prove its send "
                        "finished or failed); this attempt "
                        "created no intent and sent nothing; it refuses only "
                        "another send from this session to this target, so "
                        "unrelated work still proceeds; if you accept that "
                        "event's uncertain outcome, record your disposition with "
                        f"`cross-agent-chat resolve {blocker.event_id}` -- a "
                        "disposition cancels nothing and does not make that "
                        "work safe to send again"
                    )
                fail(
                    "target has an unresolved delivery intent from this sender: "
                    f"event {blocker.event_id} is still {blocker.status} "
                    f"({int(age)}s old); this attempt created no intent and sent "
                    "nothing; do not resend that event -- wait for its recorded "
                    "result before sending again"
                )
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
            rows = [*existing, intent]
            atomic_json(self.path, [item.to_dict() for item in rows])
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
            if self._find_one(existing, event_id) is None:
                # A late outcome can still arrive for a row already archived as
                # decided; the archive file carries that update in place.
                if self._mark_archived(event_id, status):
                    return
                fail("intent is unavailable")
            updated = self._write_status(existing, event_id, status)
            self._maybe_compact(updated)

    def _mark_archived(self, event_id: str, status: IntentStatus) -> bool:
        """Update one archived row inside its dated file; caller holds the lock."""
        month = self._archived_months().get(event_id)
        if month is None:
            return False
        rows = self._read_archive(month)
        if not any(item.event_id == event_id for item in rows):
            return False
        updated = [
            replace(item, status=status, timestamp=utc_now()) if item.event_id == event_id else item
            for item in rows
        ]
        atomic_json(self.archive / f"{month}.json", [item.to_dict() for item in updated])
        return True

    def resolve_by_owner(self, event_id: str) -> IntentStatus:
        """Record the owner's acceptance of one undecided event and return its prior status.

        Eligibility and the write are one transition under the intents lock, so a
        result recorded concurrently is refused as decided rather than overwritten.
        """
        valid_uuid(event_id, "event id")
        with state_lock(self.root, "intents"):
            existing = self.intents()
            current = self._find_one(existing, event_id)
            if current is None:
                # Archived rows are decided by construction; a late outcome can
                # still mark one UNKNOWN in place, so resolve handles every
                # status an archive could honestly carry.
                archived = self.archived(event_id)
                if archived is None:
                    fail("intent is unavailable")
                if archived.status == "RESOLVED_BY_OWNER":
                    return archived.status
                if archived.status == "UNKNOWN_DELIVERY":
                    self._mark_archived(event_id, "RESOLVED_BY_OWNER")
                    return archived.status
                fail(
                    f"event {event_id} is {archived.status}, which is already decided; "
                    "nothing to resolve"
                )
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
                        f"is at least {int(ABANDONED_INTENT_SECONDS)}s old -- a "
                        "heuristic orphan test, not proof the send is gone -- then "
                        "resolve"
                    )
            updated = self._write_status(existing, event_id, "RESOLVED_BY_OWNER")
            self._maybe_compact(updated)
            return current.status

    @staticmethod
    def _find_one(existing: list[Intent], event_id: str) -> Intent | None:
        matches = [item for item in existing if item.event_id == event_id]
        if len(matches) > 1:
            fail("intent state contains duplicate events")
        return matches[0] if matches else None

    def _write_status(
        self, existing: list[Intent], event_id: str, status: IntentStatus
    ) -> list[Intent]:
        """Rewrite one row's status and return the new rows; caller holds the lock."""
        updated = [
            replace(item, status=status, timestamp=utc_now()) if item.event_id == event_id else item
            for item in existing
        ]
        atomic_json(self.path, [item.to_dict() for item in updated])
        return updated

    def _maybe_compact(self, rows: list[Intent]) -> None:
        """Best-effort archival while the intents lock is held.

        Compaction only ever runs after a decided transition has already
        landed durably. It is housekeeping, so a failure is recorded as a
        diagnostic instead of raised: a compaction error can never undo the
        transition that triggered it. It never runs in a pre-send or in-flight
        window (``begin_identity``, ``claim_remote_authorization``), where a
        failed post-write step could orphan an undecided row no courier ever
        saw.
        """
        try:
            self._compact(rows)
        except (ChatError, OSError) as error:
            with suppress(OSError, ChatError):
                atomic_json(
                    self.root / "intents-compaction-diagnostic.json",
                    {
                        "schema_version": SCHEMA_VERSION,
                        "at": utc_now(),
                        "error": f"{type(error).__name__}: {error}",
                    },
                )

    def _compact(self, rows: list[Intent]) -> None:
        """Archive decided rows past the horizon; caller holds the lock.

        The hot file is what every send and status call pays to parse; decided
        rows older than INTENT_ARCHIVE_AGE_SECONDS move to dated
        ``intents-archive/YYYY-MM.json`` files with an ``ids.json`` index so
        every event id stays uniquely owned and checkable forever. Undecided
        rows -- anything a late receipt or owner disposition could still
        change -- never leave the hot file. Archives and their index land
        before the hot file shrinks, so a crash mid-compaction can only
        duplicate a row (active lookup wins), never lose one.
        """
        if len(rows) < INTENT_COMPACTION_MIN_ROWS:
            return
        now = datetime.now(UTC)
        movable = [
            item
            for item in rows
            if item.status in _INTENT_TERMINAL_STATUSES
            and (now - _parse_timestamp(item.timestamp)).total_seconds()
            >= INTENT_ARCHIVE_AGE_SECONDS
        ]
        if len(movable) < INTENT_COMPACTION_MIN_ARCHIVED:
            return
        grouped: dict[str, dict[str, Intent]] = {}
        for item in movable:
            month = _parse_timestamp(item.timestamp).strftime("%Y-%m")
            grouped.setdefault(month, {})[item.event_id] = item
        ensure_private_dir(self.archive)
        written: dict[str, dict[str, Intent]] = {}
        for month, group in sorted(grouped.items()):
            merged = {item.event_id: item for item in self._read_archive(month)}
            merged.update(group)
            atomic_json(
                self.archive / f"{month}.json",
                [
                    item.to_dict()
                    for item in sorted(merged.values(), key=lambda entry: entry.timestamp)
                ],
            )
            written[month] = merged
        atomic_json(
            self.archive / "ids.json",
            {"schema_version": SCHEMA_VERSION, "months": self._archive_index(written)},
        )
        moved = {item.event_id for item in movable}
        atomic_json(
            self.path,
            [item.to_dict() for item in rows if item.event_id not in moved],
        )
        DeliveryReceiptStore(self.root).prune(RECEIPT_RETENTION_SECONDS)

    def _archive_index(self, written: dict[str, dict[str, Intent]]) -> dict[str, str]:
        """Merge this pass's archived months into the durable id index.

        The index only ever gains entries -- archived rows are immutable
        custody and never leave their dated file -- so updating the months
        just written keeps it exact without re-parsing every archive under
        the lock. An index that cannot be parsed is rebuilt once from the
        dated files, and entries for month files that no longer exist are
        dropped either way.
        """
        archive_dir = self.archive
        present = {
            path.stem
            for path in archive_dir.iterdir()
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}\.json", path.name) is not None
        }
        try:
            months_index = self._archived_months()
        except ChatError:
            months_index = {}
            for month in sorted(present):
                for item in self._read_archive(month):
                    months_index[item.event_id] = month
        months_index = {
            event_id: month for event_id, month in months_index.items() if month in present
        }
        for month, merged in written.items():
            for event_id in merged:
                months_index[event_id] = month
        return months_index


class DeliveryReceiptStore:
    """The transport's own durable proof of a provider-boundary acceptance.

    A courier writes one small per-event file under ``delivered/`` before it
    answers an accept request, so a caller that timed out or died after the
    provider hand-off can still learn the decided result from this exact
    receipt. A receipt binds the event id to the addressed target identities --
    the courier's own (key, generation) and, for a native helper, the original
    session the sender actually addressed. It is transport-custody evidence
    only: it never claims provider or original-session consumption, and no
    receipt is ever written for a send that was not accepted.
    """

    _RECEIPT_KEYS: Final = frozenset({"schema_version", "event_id", "targets", "recorded_at"})
    _TARGET_KEYS: Final = frozenset({"target_key", "target_generation", "provider"})

    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root / "delivered"

    def _path(self, event_id: str) -> Path:
        identifier = valid_uuid(event_id, "event id")
        return self.dir / f"{identifier}.json"

    def record(
        self, *, event_id: str, targets: list[dict[str, str]], recorded_at: str | None = None
    ) -> None:
        """Write the receipt for one accepted event id (idempotent overwrite)."""
        identifier = valid_uuid(event_id, "event id")
        if not targets:
            fail("delivery receipt requires at least one target identity")
        for target in targets:
            if set(target) != self._TARGET_KEYS:
                fail("delivery receipt target identity is invalid")
        ensure_private_dir(self.root)
        ensure_private_dir(self.dir)
        atomic_json(
            self.dir / f"{identifier}.json",
            {
                "schema_version": SCHEMA_VERSION,
                "event_id": identifier,
                "targets": targets,
                "recorded_at": recorded_at or utc_now(),
            },
        )

    def _read(self, event_id: str) -> dict[str, Any] | None:
        path = self._path(event_id)
        if not path.exists():
            return None
        require_private_file(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChatError("delivery receipt is invalid") from error
        if not isinstance(raw, dict) or not self._RECEIPT_KEYS <= set(raw) <= (
            self._RECEIPT_KEYS | {"confirmed"}
        ):
            fail("delivery receipt is invalid")
        if (
            raw.get("schema_version") != SCHEMA_VERSION
            or raw.get("event_id") != valid_uuid(event_id, "event id")
            or not isinstance(raw.get("targets"), list)
            or not raw["targets"]
            or not all(
                isinstance(target, dict)
                and set(target) == self._TARGET_KEYS
                and isinstance(target["target_key"], str)
                and re.fullmatch(r"[0-9a-f]{64}", target["target_key"]) is not None
                and isinstance(target["target_generation"], str)
                and UUID_RE.fullmatch(target["target_generation"]) is not None
                and target["provider"] in {"claude", "codex", "devin"}
                for target in raw["targets"]
            )
            or not isinstance(raw.get("recorded_at"), str)
            or (
                raw.get("confirmed") is not None
                and (
                    not isinstance(raw["confirmed"], dict)
                    or set(raw["confirmed"]) != {"from_status", "at"}
                    or not isinstance(raw["confirmed"]["from_status"], str)
                    or not isinstance(raw["confirmed"]["at"], str)
                )
            )
        ):
            fail("delivery receipt is invalid")
        return cast(dict[str, Any], raw)

    def receipt_for(
        self, *, event_id: str, target_key: str, target_generation: str
    ) -> dict[str, Any] | None:
        """The receipt only when it proves this exact (event, target) pair."""
        valid_uuid(event_id, "event id")
        if re.fullmatch(r"[0-9a-f]{64}", target_key) is None:
            fail("target key is invalid")
        valid_uuid(target_generation, "target generation")
        receipt = self._read(event_id)
        if receipt is None:
            return None
        for target in receipt["targets"]:
            if (
                target["target_key"] == target_key
                and target["target_generation"] == target_generation
            ):
                return receipt
        return None

    def confirm(self, event_id: str, *, from_status: str) -> None:
        """Annotate that an undecided intent was settled late by this receipt."""
        receipt = self._read(event_id)
        if receipt is None or receipt.get("confirmed") is not None:
            return
        receipt["confirmed"] = {"from_status": from_status, "at": utc_now()}
        atomic_json(self._path(event_id), receipt)

    def prune(self, retention_seconds: float) -> int:
        """Drop receipts past the retention horizon; best-effort and bounded."""
        # A symlinked directory or entry is never followed: pruning must only
        # ever remove regular files the store itself wrote inside its root.
        if self.dir.is_symlink() or not self.dir.is_dir():
            return 0
        cutoff = datetime.now(UTC).timestamp() - retention_seconds
        pruned = 0
        for path in self.dir.iterdir():
            if re.fullmatch(r"[0-9a-f-]{36}\.json", path.name) is None:
                continue
            try:
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                if metadata.st_mtime < cutoff:
                    path.unlink()
                    pruned += 1
            except OSError:
                continue
        return pruned


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
