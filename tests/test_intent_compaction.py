"""Intent compaction: decided rows age out of the hot file, custody never does.

The iMac intent store grew to ~31k rows / 17.7 MB, which every send and status
call pays to parse and rewrite under one lock. Decided rows older than the
archive horizon move to dated ``intents-archive/YYYY-MM.json`` files with an
``ids.json`` index; undecided rows never leave the hot file, and every event
id stays uniquely owned and checkable.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cross_agent_chat import core
from cross_agent_chat.core import (
    ChatError,
    DeliveryReceiptStore,
    Intent,
    IntentStatus,
    IntentStore,
    Route,
    session_key,
)
from cross_agent_chat.runtime import owner_event_status


def route(
    tmp_path: Path, *, provider: str = "codex", pid: int = 1200, project: str = "project"
) -> Route:
    cwd = tmp_path / project
    cwd.mkdir(parents=True, exist_ok=True)
    return Route.create(
        provider=provider, session_id=str(uuid4()), device="studio", cwd=str(cwd), pid=pid
    )


def _backdate(store: IntentStore, seconds: float) -> None:
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    stale = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    for item in raw:
        item["timestamp"] = stale
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    store.path.chmod(0o600)


def _begin(store: IntentStore, source: Route, target: Route, status: IntentStatus) -> str:
    event_id = store.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)
    if status != "PENDING":
        store.mark(event_id, status)
    return event_id


def _stored(store: IntentStore, event_id: str) -> Intent:
    intent = store.lookup(event_id)
    assert intent is not None
    return intent


@pytest.fixture()
def small_compaction(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defaults stand in when the pre-change code has no knobs at all, so the
    # same tests fail on behavior, not on a missing attribute.
    for name, value in {
        "INTENT_COMPACTION_MIN_ROWS": 4,
        "INTENT_COMPACTION_MIN_ARCHIVED": 2,
        "INTENT_ARCHIVE_AGE_SECONDS": 3600.0,
    }.items():
        monkeypatch.setattr(core, name, value, raising=False)


def test_decided_rows_archive_while_undecided_stay_and_custody_is_intact(
    tmp_path: Path, small_compaction: None
) -> None:
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    decided = route(tmp_path, project="decided")

    old_accepted = _begin(store, source, decided, "TRANSPORT_ACCEPTED")
    old_rejected = _begin(store, source, decided, "PRE_EFFECT_REJECTED")
    old_resolved = _begin(store, source, decided, "UNKNOWN_DELIVERY")
    store.resolve_by_owner(old_resolved)
    _backdate(store, 7200)
    # Undecided rows never leave the hot file regardless of age; a decided row
    # under the horizon stays too.
    old_pending = _begin(store, source, route(tmp_path, project="p1"), "PENDING")
    old_unknown = _begin(store, source, route(tmp_path, project="p2"), "UNKNOWN_DELIVERY")
    _backdate(store, 7200)
    young_accepted = _begin(store, source, decided, "TRANSPORT_ACCEPTED")

    trigger = _begin(store, source, decided, "TRANSPORT_ACCEPTED")

    hot = {item.event_id for item in store.intents()}
    assert hot == {old_pending, old_unknown, young_accepted, trigger}
    month = (datetime.now(UTC) - timedelta(seconds=7200)).strftime("%Y-%m")
    archive = json.loads((store.archive / f"{month}.json").read_text(encoding="utf-8"))
    assert {item["event_id"] for item in archive} == {
        old_accepted,
        old_rejected,
        old_resolved,
    }
    index = json.loads((store.archive / "ids.json").read_text(encoding="utf-8"))
    assert index["schema_version"] == 1
    assert index["months"] == {
        old_accepted: month,
        old_rejected: month,
        old_resolved: month,
    }

    # No-replay correctness: every event id stays uniquely owned even after
    # it left the hot file, and custody reads still find it.
    for event_id in (old_accepted, old_rejected, old_resolved):
        with pytest.raises(ChatError, match="event id is unavailable"):
            store.begin_identity(
                source_key=session_key(source.provider, source.session_id),
                source_generation=source.generation,
                source_alias=source.alias,
                target_key=session_key(decided.provider, decided.session_id),
                target_generation=decided.generation,
                payload_digest="b" * 64,
                event_id=event_id,
            )
        assert store.lookup(event_id) is not None
        assert (
            store.intent_for_source(
                event_id=event_id,
                source_key=session_key(source.provider, source.session_id),
                source_generation=source.generation,
            )
            is not None
        )
    assert owner_event_status(root, old_accepted)["status"] == "TRANSPORT_ACCEPTED"
    with pytest.raises(ChatError, match="already decided"):
        store.resolve_by_owner(old_accepted)
    assert store.resolve_by_owner(old_resolved) == "RESOLVED_BY_OWNER"

    # A late outcome still lands on an archived row, in its own dated file.
    store.mark(old_rejected, "TRANSPORT_ACCEPTED")
    assert _stored(store, old_rejected).status == "TRANSPORT_ACCEPTED"
    assert {item.event_id for item in store.intents()} == hot
    # And an archived row left UNKNOWN by a late outcome still resolves.
    store.mark(old_rejected, "UNKNOWN_DELIVERY")
    assert store.resolve_by_owner(old_rejected) == "UNKNOWN_DELIVERY"
    assert _stored(store, old_rejected).status == "RESOLVED_BY_OWNER"


def test_below_thresholds_nothing_moves(tmp_path: Path, small_compaction: None) -> None:
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, project="decided")
    first = _begin(store, source, target, "TRANSPORT_ACCEPTED")
    second = _begin(store, source, target, "PRE_EFFECT_REJECTED")
    _backdate(store, 7200)
    _begin(store, source, target, "TRANSPORT_ACCEPTED")

    # Only three rows exist and two are archivable -- under the row floor.
    assert not (root / "intents-archive").exists()
    assert len(store.intents()) == 3
    assert store.lookup(first) is not None and store.lookup(second) is not None


def test_compaction_is_atomic_safe_against_partial_archive(
    tmp_path: Path, small_compaction: None
) -> None:
    """A stale index that names a missing month must not lose the row."""
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, project="decided")
    event_id = _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _backdate(store, 7200)
    _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _begin(store, source, target, "TRANSPORT_ACCEPTED")

    assert store.lookup(event_id) is not None
    assert len(store.intents()) == 2

    # Repeating compaction keeps the archive durable and the index exact.
    another = _begin(store, source, target, "PRE_EFFECT_REJECTED")
    assert store.lookup(event_id) is not None
    assert store.lookup(another) is not None


def test_receipt_pruning_ages_out_only_old_receipts(tmp_path: Path, small_compaction: None) -> None:
    root = tmp_path / "state"
    store = IntentStore(root)
    receipts = DeliveryReceiptStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, project="decided")
    old_receipt, new_receipt = str(uuid4()), str(uuid4())
    identity = [
        {
            "target_key": session_key(target.provider, target.session_id),
            "target_generation": target.generation,
            "provider": "codex",
        }
    ]
    receipts.record(event_id=old_receipt, targets=identity)
    receipts.record(event_id=new_receipt, targets=identity)
    old = datetime.now(UTC).timestamp() - (91 * 86400)
    os.utime(receipts.dir / f"{old_receipt}.json", (old, old))

    for _ in range(4):
        _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _backdate(store, 7200)
    _begin(store, source, target, "TRANSPORT_ACCEPTED")

    assert not (receipts.dir / f"{old_receipt}.json").exists()
    assert (receipts.dir / f"{new_receipt}.json").exists()


def test_undecided_writes_never_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compaction never runs in the pre-send or in-flight window.

    A compaction failure after begin's save would leave a PENDING row no
    courier ever saw, which wedges every later send from that source to that
    target; an in-flight claim has the same shape. Neither write may compact.
    """
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    decided = route(tmp_path, project="decided")

    def explode(_rows: list[Intent]) -> None:
        raise ChatError("compaction must not run for an undecided write")

    monkeypatch.setattr(store, "_maybe_compact", explode)

    # The PENDING row lands and the send proceeds on a clean store.
    event_id = store.begin(source, decided, source_alias=source.alias, payload_digest="a" * 64)
    assert _stored(store, event_id).status == "PENDING"

    # The in-flight claim is an undecided transition; it does not compact.
    assert store.claim_remote_authorization(
        event_id=event_id,
        source_generation=source.generation,
        source_alias=source.alias,
        target_key=session_key(decided.provider, decided.session_id),
        target_generation=decided.generation,
        payload_digest="a" * 64,
    )
    assert _stored(store, event_id).status == "REMOTE_AUTHORIZED"


def test_a_compaction_failure_still_lands_decided_writes(
    tmp_path: Path, small_compaction: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a decided transition, compaction is best-effort with a diagnostic."""
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    decided = route(tmp_path, project="decided")
    other = route(tmp_path, project="other")

    first = _begin(store, source, decided, "REMOTE_AUTHORIZED")
    for _ in range(3):
        _begin(store, source, other, "TRANSPORT_ACCEPTED")
    _backdate(store, 7200)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise ChatError("injected compaction failure")

    monkeypatch.setattr(IntentStore, "_read_archive", explode)

    # The mark lands even though archiving the old rows fails underneath it.
    store.mark(first, "TRANSPORT_ACCEPTED")
    assert _stored(store, first).status == "TRANSPORT_ACCEPTED"

    # An owner disposition lands the same way.
    second = _begin(store, source, other, "UNKNOWN_DELIVERY")
    assert store.resolve_by_owner(second) == "UNKNOWN_DELIVERY"
    assert _stored(store, second).status == "RESOLVED_BY_OWNER"

    # The suppressed failure leaves a diagnostic, not silence.
    diagnostic = json.loads(
        (root / "intents-compaction-diagnostic.json").read_text(encoding="utf-8")
    )
    assert "injected compaction failure" in diagnostic["error"]


def test_compaction_index_updates_only_the_months_it_writes(
    tmp_path: Path, small_compaction: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each compaction merges the months it wrote, not a full archive rescan."""
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, project="decided")

    for _ in range(4):
        _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _backdate(store, 40 * 86400)
    _begin(store, source, target, "TRANSPORT_ACCEPTED")  # first compaction
    first_month = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m")
    first_ids = {item.event_id for item in store._read_archive(first_month)}

    reads: list[str] = []
    real_read = IntentStore._read_archive

    def spy(self: IntentStore, month: str) -> list[Intent]:
        reads.append(month)
        return real_read(self, month)

    monkeypatch.setattr(IntentStore, "_read_archive", spy)

    for _ in range(3):
        _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _backdate(store, 10 * 86400)
    _begin(store, source, target, "TRANSPORT_ACCEPTED")  # second compaction
    second_month = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m")

    index = json.loads((store.archive / "ids.json").read_text(encoding="utf-8"))
    assert first_ids <= set(index["months"])
    assert set(index["months"].values()) == {first_month, second_month}
    # The second pass merged only its own month; the earlier archived month
    # was never re-parsed.
    assert first_month not in reads
    assert second_month in reads


def test_compaction_index_rebuilds_once_when_the_index_is_damaged(
    tmp_path: Path, small_compaction: None
) -> None:
    """A corrupt ids.json heals by rescanning the dated files once."""
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, project="decided")

    for _ in range(4):
        _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _backdate(store, 40 * 86400)
    _begin(store, source, target, "TRANSPORT_ACCEPTED")
    first_month = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m")
    first_ids = {
        item["event_id"]
        for item in json.loads((store.archive / f"{first_month}.json").read_text("utf-8"))
    }
    damaged = store.archive / "ids.json"
    damaged.write_text("{corrupt", encoding="utf-8")
    damaged.chmod(0o600)

    for _ in range(3):
        _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _backdate(store, 10 * 86400)
    _begin(store, source, target, "TRANSPORT_ACCEPTED")

    index = json.loads((store.archive / "ids.json").read_text(encoding="utf-8"))
    assert first_ids <= set(index["months"])
    for event_id in first_ids:
        assert store.lookup(event_id) is not None


def test_month_files_without_an_index_fail_closed_until_compaction_rebuilds(
    tmp_path: Path, small_compaction: None
) -> None:
    """Dated files without ids.json are a damaged store, not an empty one.

    A crash between the month writes and the index write leaves archived
    events a missing index would silently disown: a caller-chosen id could be
    reused for a second delivery and a late outcome dropped. Reads refuse
    until a compaction pass rebuilds the index by rescanning the dated files.
    """
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, project="decided")

    for _ in range(4):
        _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _backdate(store, 40 * 86400)
    trigger = _begin(store, source, target, "TRANSPORT_ACCEPTED")
    first_month = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m")
    first_ids = {
        item["event_id"]
        for item in json.loads((store.archive / f"{first_month}.json").read_text("utf-8"))
    }
    (store.archive / "ids.json").unlink()

    # Damaged, not empty: archived-id lookups and caller-chosen id reuse both
    # fail closed rather than report the events absent or available.
    for event_id in first_ids:
        with pytest.raises(ChatError, match="intent archive index is invalid"):
            store.lookup(event_id)
        with pytest.raises(ChatError, match="intent archive index is invalid"):
            store.begin_identity(
                source_key=session_key(source.provider, source.session_id),
                source_generation=source.generation,
                source_alias=source.alias,
                target_key=session_key(target.provider, target.session_id),
                target_generation=target.generation,
                payload_digest="b" * 64,
                event_id=event_id,
            )
    # The hot file still answers for its own rows.
    assert _stored(store, trigger).status == "TRANSPORT_ACCEPTED"

    # The next compaction pass rebuilds the missing index by rescanning the
    # dated files, and every archived id is uniquely owned and findable again.
    for _ in range(3):
        _begin(store, source, target, "TRANSPORT_ACCEPTED")
    _backdate(store, 10 * 86400)
    _begin(store, source, target, "TRANSPORT_ACCEPTED")

    index = json.loads((store.archive / "ids.json").read_text(encoding="utf-8"))
    assert first_ids <= set(index["months"])
    for event_id in first_ids:
        assert store.lookup(event_id) is not None


def test_a_non_dict_archive_index_is_a_chat_error(tmp_path: Path) -> None:
    """A well-formed JSON index that is not an object is invalid, not a crash."""
    root = tmp_path / "state"
    store = IntentStore(root)
    store.archive.mkdir(mode=0o700)
    index = store.archive / "ids.json"

    for payload in ('["not", "a", "dict"]', "7", '"index"'):
        index.write_text(payload, encoding="utf-8")
        index.chmod(0o600)
        with pytest.raises(ChatError, match="intent archive index is invalid"):
            store.lookup(str(uuid4()))


def test_a_receipt_with_extra_keys_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "state"
    receipts = DeliveryReceiptStore(root)
    event_id = str(uuid4())
    target_key, target_generation = "a" * 64, str(uuid4())
    receipts.record(
        event_id=event_id,
        targets=[
            {
                "target_key": target_key,
                "target_generation": target_generation,
                "provider": "claude",
            }
        ],
    )
    path = receipts.dir / f"{event_id}.json"

    tamper: Callable[[dict[str, Any]], None]
    for tamper in (
        lambda raw: raw.update(unexpected="x"),
        lambda raw: raw.update(confirmed="yes"),
        lambda raw: raw.update(confirmed={"from_status": "PENDING"}),
        lambda raw: raw.update(confirmed={"from_status": "PENDING", "at": "now", "x": 1}),
    ):
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("confirmed", None)
        tamper(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")
        path.chmod(0o600)
        with pytest.raises(ChatError, match="delivery receipt is invalid"):
            receipts.receipt_for(
                event_id=event_id,
                target_key=target_key,
                target_generation=target_generation,
            )

    # The store's own lateness annotation is the only accepted extra key.
    receipts.record(
        event_id=event_id,
        targets=[
            {
                "target_key": target_key,
                "target_generation": target_generation,
                "provider": "claude",
            }
        ],
    )
    receipts.confirm(event_id, from_status="PENDING")
    receipt = receipts.receipt_for(
        event_id=event_id, target_key=target_key, target_generation=target_generation
    )
    assert receipt is not None
    assert receipt["confirmed"]["from_status"] == "PENDING"


def test_prune_never_follows_a_symlinked_receipt_dir(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    root.chmod(0o700)
    receipts = DeliveryReceiptStore(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    foreign = elsewhere / f"{uuid4()}.json"
    foreign.write_text("{}")
    old = datetime.now(UTC).timestamp() - (91 * 86400)
    os.utime(foreign, (old, old))
    receipts.dir.symlink_to(elsewhere)

    assert receipts.prune(90.0 * 86400.0) == 0
    assert foreign.exists()
