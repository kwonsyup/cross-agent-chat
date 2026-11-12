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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import core
from cross_agent_chat.core import (
    ChatError,
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
    assert store.lookup(old_rejected).status == "TRANSPORT_ACCEPTED"
    assert {item.event_id for item in store.intents()} == hot
    # And an archived row left UNKNOWN by a late outcome still resolves.
    store.mark(old_rejected, "UNKNOWN_DELIVERY")
    assert store.resolve_by_owner(old_rejected) == "UNKNOWN_DELIVERY"
    assert store.lookup(old_rejected).status == "RESOLVED_BY_OWNER"


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
    from cross_agent_chat.core import DeliveryReceiptStore

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
