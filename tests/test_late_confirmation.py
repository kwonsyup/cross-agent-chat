"""Late transport confirmation: exact receipts settle undecided custody, never resend.

Reproduces the 12 Nov incident: a claude-native courier enqueued the message at
the provider boundary while the caller's answer was lost past a deadline, so
the durable intent stayed PENDING/UNKNOWN_DELIVERY even though delivery had
happened. A courier now records its own receipt before answering, and a status
read settles the row only on that exact (event id, target key, generation)
proof -- never on a guess, a text match, or an unrelated receipt.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import runtime
from cross_agent_chat.core import (
    DeliveryReceiptStore,
    Intent,
    IntentStatus,
    IntentStore,
    Registry,
    Route,
    session_key,
)
from cross_agent_chat.native_helper import NativeHelperStore
from cross_agent_chat.recipient import remote_token
from cross_agent_chat.runtime import (
    _record_transport_receipt,
    event_status,
    inspect_remote_receiving,
    owner_event_status,
)
from cross_agent_chat.tailnet import TailnetIdentity

ACCOUNT = "b" * 64


def route(
    tmp_path: Path,
    *,
    provider: str = "codex",
    pid: int = 1200,
    project: str = "project",
    device: str = "studio",
    session_id: str | None = None,
    generation: str | None = None,
    profile_root: Path | None = None,
) -> Route:
    cwd = tmp_path / project
    cwd.mkdir(parents=True, exist_ok=True)
    if profile_root is not None:
        profile_root.mkdir(parents=True, exist_ok=True)
    return Route.create(
        provider=provider,
        session_id=session_id or str(uuid4()),
        device=device,
        cwd=str(cwd),
        pid=pid,
        generation=generation,
        owner_identity=ACCOUNT if profile_root is not None else None,
        profile_root=str(profile_root) if profile_root is not None else None,
    )


def _intent(
    store: IntentStore,
    source: Route,
    target: Route,
    status: IntentStatus,
) -> str:
    event_id = store.begin(source, target, source_alias=source.alias, payload_digest="a" * 64)
    if status != "PENDING":
        store.mark(event_id, status)
    return event_id


def _receipt(store_root: Path, intent: Intent, *, target_key=None, target_generation=None) -> None:
    DeliveryReceiptStore(store_root).record(
        event_id=intent.event_id,
        targets=[
            {
                "target_key": target_key or intent.target_key,
                "target_generation": target_generation or intent.target_generation,
                "provider": "claude",
            }
        ],
    )


@pytest.mark.parametrize("status", ["PENDING", "REMOTE_AUTHORIZED", "UNKNOWN_DELIVERY"])
def test_owner_status_settles_undecided_custody_on_the_exact_transport_receipt(
    tmp_path: Path, status: IntentStatus
) -> None:
    """The 12 Nov incident: delivered at transport, lost answer, stale custody."""
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, provider="claude")
    event_id = _intent(store, source, target, status)
    _receipt(root, store.lookup(event_id))

    result = owner_event_status(root, event_id)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["confirmed_late"] is True
    assert store.lookup(event_id).status == "TRANSPORT_ACCEPTED"
    # The late confirmation stays visible; a repeat read does not change
    # custody again or rewrite the receipt's record.
    again = owner_event_status(root, event_id)
    assert again["status"] == "TRANSPORT_ACCEPTED"
    assert again["confirmed_late"] is True
    receipt = DeliveryReceiptStore(root).receipt_for(
        event_id=event_id,
        target_key=session_key(target.provider, target.session_id),
        target_generation=target.generation,
    )
    assert receipt["confirmed"]["from_status"] == status


def test_authenticated_source_status_reports_the_same_late_settlement(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    source = route(tmp_path, project="source", pid=os.getpid())
    Registry(root).upsert(source)
    store = IntentStore(root)
    target = route(tmp_path, provider="claude")
    event_id = _intent(store, source, target, "UNKNOWN_DELIVERY")
    _receipt(root, store.lookup(event_id))

    result = event_status(root, source, event_id)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["confirmed_late"] is True
    assert store.lookup(event_id).status == "TRANSPORT_ACCEPTED"


@pytest.mark.parametrize("status", ["PENDING", "UNKNOWN_DELIVERY"])
def test_unrelated_evidence_never_settles_custody(tmp_path: Path, status: IntentStatus) -> None:
    """A receipt for another event, generation, or key proves nothing here."""
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, provider="claude")
    event_id = _intent(store, source, target, status)
    intent = store.lookup(event_id)

    # Another event's receipt.
    DeliveryReceiptStore(root).record(
        event_id=str(uuid4()),
        targets=[
            {
                "target_key": intent.target_key,
                "target_generation": intent.target_generation,
                "provider": "claude",
            }
        ],
    )
    # This event's receipt bound to a different generation.
    _receipt(root, intent, target_generation=str(uuid4()))
    # This event's receipt bound to a different target key.
    _receipt(root, intent, target_key="f" * 64)

    result = owner_event_status(root, event_id)

    assert result["status"] == status
    assert "confirmed_late" not in result
    assert store.lookup(event_id).status == status


def test_no_receipt_means_no_settlement(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, provider="claude")
    event_id = _intent(store, source, target, "UNKNOWN_DELIVERY")

    result = owner_event_status(root, event_id)

    assert result["status"] == "UNKNOWN_DELIVERY"
    assert "confirmed_late" not in result
    assert store.lookup(event_id).status == "UNKNOWN_DELIVERY"


@pytest.mark.parametrize("decided", ["TRANSPORT_ACCEPTED", "PRE_EFFECT_REJECTED"])
def test_a_decided_result_is_never_rewritten_by_late_evidence(
    tmp_path: Path, decided: IntentStatus
) -> None:
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, provider="claude")
    event_id = _intent(store, source, target, decided)
    _receipt(root, store.lookup(event_id))

    result = owner_event_status(root, event_id)

    assert result["status"] == decided
    assert store.lookup(event_id).status == decided


def test_record_transport_receipt_writes_only_for_acceptance(tmp_path: Path) -> None:
    root = tmp_path / "state"
    courier = route(tmp_path, provider="claude")
    event_id = str(uuid4())
    accepted = {
        "schema_version": 1,
        "event_id": event_id,
        "status": "TRANSPORT_ACCEPTED",
        "provider": "claude",
    }
    refused = {**accepted, "status": "PRE_EFFECT_REJECTED"}
    unknown = {**accepted, "status": "UNKNOWN_DELIVERY"}

    _record_transport_receipt(root, courier, refused)
    _record_transport_receipt(root, courier, unknown)
    _record_transport_receipt(root, courier, accepted)

    store = DeliveryReceiptStore(root)
    assert [item.name for item in store.dir.iterdir()] == [f"{event_id}.json"]
    receipt = store.receipt_for(
        event_id=event_id,
        target_key=session_key(courier.provider, courier.session_id),
        target_generation=courier.generation,
    )
    assert receipt is not None


def test_helper_courier_receipt_binds_the_addressed_original(tmp_path: Path) -> None:
    """A native helper delivers for its original; the intent names the original."""
    root = tmp_path / "state"
    profile = tmp_path / "profile"
    original = route(tmp_path, project="original", profile_root=profile, pid=os.getpid())
    Registry(root).upsert(original)
    binding, nonce = NativeHelperStore(root).reserve(original, ACCOUNT)
    helper_root = tmp_path / binding.helper_directory
    helper_root.mkdir()
    helper = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device=original.device,
        cwd=str(helper_root),
        pid=os.getpid(),
        owner_identity=ACCOUNT,
        profile_root=str(profile),
    )
    Registry(root).upsert(helper)
    NativeHelperStore(root).register(helper, nonce, original, ACCOUNT)

    event_id = str(uuid4())
    _record_transport_receipt(
        root,
        helper,
        {
            "schema_version": 1,
            "event_id": event_id,
            "status": "TRANSPORT_ACCEPTED",
            "provider": "codex",
        },
    )

    store = DeliveryReceiptStore(root)
    # The receipt binds both the helper courier and the addressed original.
    assert (
        store.receipt_for(
            event_id=event_id,
            target_key=session_key("codex", original.session_id),
            target_generation=original.generation,
        )
        is not None
    )
    assert (
        store.receipt_for(
            event_id=event_id,
            target_key=session_key("codex", helper.session_id),
            target_generation=helper.generation,
        )
        is not None
    )


def test_remote_inspect_carries_the_durable_receipt_without_a_courier(
    tmp_path: Path,
) -> None:
    """The receiving node's receipt is proof even when its courier is dead."""
    root = tmp_path / "state"
    IntentStore(root)
    target = route(tmp_path, provider="claude")
    target_key = session_key(target.provider, target.session_id)
    event_id = str(uuid4())
    DeliveryReceiptStore(root).record(
        event_id=event_id,
        targets=[
            {
                "target_key": target_key,
                "target_generation": target.generation,
                "provider": "claude",
            }
        ],
    )

    answer = inspect_remote_receiving(
        root, event_id=event_id, target_key=target_key, target_generation=target.generation
    )

    # No live courier observes this generation, yet the durable receipt still
    # answers: liveness and proof are independent.
    assert answer["observer"] == "none"
    assert answer["delivery_receipt"] == {
        "event_id": event_id,
        "recorded_at": answer["delivery_receipt"]["recorded_at"],
    }
    assert isinstance(answer["delivery_receipt"]["recorded_at"], str)


def test_remote_receipt_settles_undecided_custody_over_the_inspect_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote node's own receipt settles a sender-side undecided row."""
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, provider="claude")
    event_id = _intent(store, source, target, "UNKNOWN_DELIVERY")
    intent = store.lookup(event_id)
    token = remote_token("nBroker", intent.target_key, intent.target_generation)
    calls: list[dict[str, object]] = []

    def broker(_address: str, payload: dict[str, object], *, timeout: float):
        del timeout
        calls.append(payload)
        return {
            "schema_version": 1,
            "status": "INSPECTED",
            "event_id": event_id,
            "observer": "none",
            "reason": "no_local_receiver",
            "delivery_receipt": {
                "event_id": event_id,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        }

    monkeypatch.setattr(runtime, "request_tailnet", broker)
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nBroker": "100.64.0.9"}),
    )

    result = owner_event_status(root, event_id, recipient=token)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["confirmed_late"] is True
    assert store.lookup(event_id).status == "TRANSPORT_ACCEPTED"
    # The probe was one bounded read-only inspect; nothing was sent.
    assert calls == [
        {
            "schema_version": 1,
            "operation": "inspect",
            "event_id": event_id,
            "target_key": intent.target_key,
            "target_generation": intent.target_generation,
        }
    ]


def test_a_remote_receipt_for_another_event_fails_the_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, provider="claude")
    event_id = _intent(store, source, target, "PENDING")
    intent = store.lookup(event_id)
    token = remote_token("nBroker", intent.target_key, intent.target_generation)

    def broker(_address: str, payload: dict[str, object], *, timeout: float):
        del timeout
        return {
            "schema_version": 1,
            "status": "INSPECTED",
            "event_id": event_id,
            "observer": "none",
            "reason": "no_local_receiver",
            "delivery_receipt": {
                "event_id": str(uuid4()),
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        }

    monkeypatch.setattr(runtime, "request_tailnet", broker)
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nBroker": "100.64.0.9"}),
    )

    result = owner_event_status(root, event_id, recipient=token)

    assert result["status"] == "PENDING"
    assert "confirmed_late" not in result
    assert result["receiving_observation"]["reason"] == "remote_observation_invalid"
    assert store.lookup(event_id).status == "PENDING"


def test_an_unwritable_store_reports_recorded_custody_instead_of_failing(
    tmp_path: Path,
) -> None:
    """A status read on a store that cannot take the write lock still answers.

    The receipt proof is real but the durable settlement cannot land, so the
    read reports the recorded custody rather than crashing -- and it never
    reports an upgrade the record did not take.
    """
    root = tmp_path / "state"
    store = IntentStore(root)
    source = route(tmp_path, project="source")
    target = route(tmp_path, provider="claude")
    event_id = _intent(store, source, target, "PENDING")
    _receipt(root, store.lookup(event_id))

    lock = root / ".intents.lock"
    lock.chmod(0o400)
    try:
        result = owner_event_status(root, event_id)
    finally:
        lock.chmod(0o600)

    assert result["status"] == "PENDING"
    assert "confirmed_late" not in result
    assert store.lookup(event_id).status == "PENDING"
