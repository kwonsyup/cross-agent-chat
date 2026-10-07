"""Enrolled identity, real courier input, duplicate effects and revoke ordering."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from cross_agent_chat import cli, external, external_callback, runtime
from cross_agent_chat.core import (
    ChatError,
    IntentStatus,
    IntentStore,
    Registry,
    Route,
    atomic_json,
    session_key,
)
from cross_agent_chat.external import ExternalEndpoint, ExternalEndpointStore, endpoint_effect_lock
from cross_agent_chat.recipient import local_token, parse_recipient_token, remote_token
from cross_agent_chat.tailnet import TailnetIdentity
from cross_agent_chat.tailnet_broker import handle_broker_request
from cross_agent_chat.transport import remote_envelope


def _send(
    token: str, event: str, message: str = "independent new fixture work"
) -> dict[str, object]:
    return {
        "name": "chat_send",
        "arguments": {
            "to": token,
            "message": message,
            "request_id": event,
        },
    }


def _result(value: dict[str, object]) -> dict[str, object]:
    content = cast(list[dict[str, str]], value["content"])
    return cast(dict[str, object], json.loads(content[0]["text"]))


@contextmanager
def _native_courier(root: Path, cwd: Path) -> Iterator[Route]:
    identity, _ = runtime.recipient_owner_identity("codex", os.getpid())
    route = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="fixture",
        cwd=str(cwd),
        pid=os.getpid(),
        owner_identity=identity,
    )
    Registry(root).upsert(route)
    thread = threading.Thread(
        target=runtime.courier_server,
        kwargs={
            "provider": "codex",
            "state_root_value": str(root),
            "session_id": route.session_id,
            "cwd": route.cwd,
            "generation": route.generation,
            "pid": route.pid,
        },
        daemon=True,
    )
    thread.start()
    path = runtime.socket_path(root, route)
    deadline = time.monotonic() + 3
    while True:
        try:
            runtime.request_socket(
                path,
                {"schema_version": 1, "operation": "bootstrap", "generation": route.generation},
                timeout=0.1,
            )
            break
        except ChatError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)
    try:
        yield route
    finally:
        runtime.request_socket(
            path,
            {"schema_version": 1, "operation": "shutdown", "generation": route.generation},
            timeout=1,
        )
        thread.join(timeout=3)
        assert not thread.is_alive()


def _enroll(root: Path) -> tuple[ExternalEndpoint, str]:
    return ExternalEndpointStore(root).enroll(
        device="fixture", name="same name", context="owner-enrolled test context"
    )


def _callback(root: Path, endpoint: ExternalEndpoint) -> ExternalEndpoint:
    config = root / "input-config.json"
    atomic_json(config, {"url": "https://receiver.example.com/enrolled", "bearer": "fixture-key"})
    return ExternalEndpointStore(root).configure_callback(endpoint.endpoint_id, config)


def test_external_send_reaches_real_native_courier_once_and_preserves_old_intents(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    endpoint, credential = _enroll(root)
    event = str(uuid4())
    with _native_courier(root, tmp_path) as native:
        token = local_token(
            root, session_key(native.provider, native.session_id), native.generation
        )
        assert (
            _result(cli._external_call_tool(root, credential, _send(token, event)))["status"]
            == "TRANSPORT_ACCEPTED"
        )
        duplicate = _result(cli._external_call_tool(root, credential, _send(token, event)))
        assert duplicate["status"] == "TRANSPORT_ACCEPTED" and duplicate["reused_request"] is True
        changed = cli._external_call_tool(root, credential, _send(token, event, "different work"))
        assert changed["isError"] is True
        queue = runtime.request_socket(
            runtime.socket_path(root, native),
            {
                "schema_version": 1,
                "operation": "peek",
                "generation": native.generation,
            },
        )
        messages = cast(list[dict[str, str]], queue["messages"])
        assert len(messages) == 1 and messages[0]["event_id"] == event
        body = messages[0]["message"]
        assert body.startswith("Cross Agent Chat transport envelope v2\nFrom: " + endpoint.alias)
        assert body.endswith("Untrusted peer content follows:\n\nindependent new fixture work")
        reply = parse_recipient_token(body.split("\n")[2].removeprefix("Reply via CAC to handle: "))
        assert (
            reply is not None
            and reply.handle == endpoint.key
            and reply.generation == endpoint.generation
        )
        # The native strict parser still sees exactly its original fields/status.
        intents = IntentStore(root).intents()
        assert len(intents) == 1 and intents[0].status == "TRANSPORT_ACCEPTED"
        assert Registry(root).routes() == [native]


@pytest.mark.parametrize("mutation", ["wrong", "revoke", "rotate", "expired"])
def test_invalid_external_identity_discloses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    root = tmp_path / "state"
    endpoint, credential = _enroll(root)
    store = ExternalEndpointStore(root)
    if mutation == "wrong":
        credential = credential[:-1] + ("a" if credential[-1] != "a" else "b")
    elif mutation == "revoke":
        store.revoke(endpoint.endpoint_id)
    elif mutation == "rotate":
        fresh, replacement = store.rotate(endpoint.endpoint_id)
        assert fresh.generation != endpoint.generation
        assert store.authenticate(replacement) == fresh
    else:
        endpoint, credential = store.enroll(
            device="fixture",
            name="expired",
            context="fixture",
            expires_at="2000-01-01T00:00:00+00:00",
        )
    monkeypatch.setattr(cli, "peers", lambda *_a, **_k: pytest.fail("disclosed peers"))
    with pytest.raises(ChatError, match="credential is unavailable"):
        cli._external_call_tool(root, credential, {"name": "chat_peers"})
    assert IntentStore(root).intents() == []


def test_native_auth_failure_never_uses_external_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _endpoint, credential = _enroll(root)
    monkeypatch.setenv("CROSS_AGENT_CHAT_EXTERNAL_CREDENTIAL", credential)
    with pytest.raises(ChatError, match="exact Claude sender is unavailable"):
        cli._mcp_call_tool(
            "claude",
            root,
            {
                "name": "chat_send",
                "arguments": {
                    "to": "native",
                    "message": "fixture",
                },
            },
        )
    assert IntentStore(root).intents() == []


def test_external_request_key_and_identity_fields_are_required(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _endpoint, credential = _enroll(root)
    for arguments in (
        {"to": "x", "message": "y"},
        {"to": "x", "message": "y", "request_id": str(uuid4()), "from": "claude"},
    ):
        with pytest.raises(ChatError):
            cli._external_call_tool(root, credential, {"name": "chat_send", "arguments": arguments})
    assert IntentStore(root).intents() == []


def test_selected_scope_pins_origin_and_generation(tmp_path: Path) -> None:
    root = tmp_path / "state"
    permitted = local_token(root, "a" * 64, str(uuid4()))
    _endpoint, credential = ExternalEndpointStore(root).enroll(
        device="fixture", name="scoped", context="fixture", allowed_recipients=(permitted,)
    )
    parsed = parse_recipient_token(permitted)
    assert parsed is not None
    other_root = local_token(tmp_path / "different", "a" * 64, parsed.generation)
    for disallowed in (other_root, local_token(root, "a" * 64, str(uuid4()))):
        with pytest.raises(ChatError, match="outside"):
            cli._external_call_tool(root, credential, _send(disallowed, str(uuid4())))
    assert IntentStore(root).intents() == []


def test_configure_scope_cli_rotates_and_preserves_endpoint_owned_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "state"
    endpoint, credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    callback_path = root / cast(str, endpoint.callback_ref)
    callback_bytes = callback_path.read_bytes()
    intents = IntentStore(root)
    intent_statuses: tuple[tuple[IntentStatus, str], ...] = (
        ("TRANSPORT_ACCEPTED", "a" * 64),
        ("UNKNOWN_DELIVERY", "b" * 64),
    )
    for status, target_key in intent_statuses:
        event = str(uuid4())
        event_id = intents.begin_identity(
            source_key=endpoint.key,
            source_generation=endpoint.generation,
            source_alias=endpoint.alias,
            target_key=target_key,
            target_generation=str(uuid4()),
            payload_digest="c" * 64,
            event_id=event,
        )
        intents.mark(event_id, status)
    prior_intents = intents.intents()
    prior_generation = endpoint.generation
    prior_credential_hash = endpoint.credential_hash
    first = local_token(root, "d" * 64, str(uuid4()))
    second = remote_token("nSelected", "e" * 64, str(uuid4()))

    arguments = cli.parser().parse_args(
        [
            "external",
            "--state-root",
            str(root),
            "configure-scope",
            endpoint.endpoint_id,
            "--expected-generation",
            prior_generation,
            "--allow-recipient",
            second,
            "--allow-recipient",
            first,
        ]
    )
    assert cli.run(arguments) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    configured = ExternalEndpointStore(root).endpoints()[0]
    assert report == {
        "endpoint_id": endpoint.endpoint_id,
        "generation": configured.generation,
        "alias": endpoint.alias,
        "scope_changed": True,
        "allowed_recipient_count": 2,
        "identity_assurance": "owner_enrolled_endpoint",
    }
    assert first not in output and second not in output and credential not in output
    assert configured.generation != prior_generation
    assert configured.endpoint_id == endpoint.endpoint_id
    assert configured.credential_hash == prior_credential_hash
    assert configured.callback_ref == endpoint.callback_ref
    assert configured.allowed_recipients == tuple(sorted({first, second}))
    assert callback_path.read_bytes() == callback_bytes
    assert ExternalEndpointStore(root).authenticate(credential) == configured
    assert intents.intents() == prior_intents

    state_bytes = ExternalEndpointStore(root).path.read_bytes()
    unchanged, changed = ExternalEndpointStore(root).configure_scope(
        endpoint.endpoint_id,
        expected_generation=configured.generation,
        allowed_recipients=(first, second, first),
    )
    assert not changed and unchanged == configured
    assert ExternalEndpointStore(root).path.read_bytes() == state_bytes
    assert callback_path.read_bytes() == callback_bytes
    assert intents.intents() == prior_intents


def test_configure_scope_stale_or_invalid_request_refuses_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    store = ExternalEndpointStore(root)
    callback_path = root / cast(str, endpoint.callback_ref)
    state_bytes = store.path.read_bytes()
    callback_bytes = callback_path.read_bytes()
    token = local_token(root, "f" * 64, str(uuid4()))

    with pytest.raises(ChatError, match="generation changed"):
        store.configure_scope(
            endpoint.endpoint_id,
            expected_generation=str(uuid4()),
            allowed_recipients=(token,),
        )
    with pytest.raises(ChatError, match="at least one token"):
        store.configure_scope(
            endpoint.endpoint_id,
            expected_generation=endpoint.generation,
            allowed_recipients=(),
        )
    with pytest.raises(ChatError, match="scope is invalid"):
        store.configure_scope(
            endpoint.endpoint_id,
            expected_generation=endpoint.generation,
            allowed_recipients=("not-a-cac2-token",),
        )
    foreign_token = local_token(tmp_path / "other-state", "0" * 64, str(uuid4()))
    with pytest.raises(ChatError, match="scope is invalid"):
        store.configure_scope(
            endpoint.endpoint_id,
            expected_generation=endpoint.generation,
            allowed_recipients=(foreign_token,),
        )
    assert store.path.read_bytes() == state_bytes
    assert callback_path.read_bytes() == callback_bytes
    assert IntentStore(root).intents() == []

    expired, _credential = store.enroll(
        device="fixture",
        name="expired-scope",
        context="fixture",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    expired_bytes = store.path.read_bytes()
    with pytest.raises(ChatError, match="unavailable or generation changed"):
        store.configure_scope(
            expired.endpoint_id,
            expected_generation=expired.generation,
            allowed_recipients=(token,),
        )
    assert store.path.read_bytes() == expired_bytes


def test_configure_scope_waits_for_endpoint_effect_lock(tmp_path: Path) -> None:
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    store = ExternalEndpointStore(root)
    recipient = local_token(root, "9" * 64, str(uuid4()))
    started = threading.Event()
    finished = threading.Event()
    results: list[tuple[ExternalEndpoint, bool]] = []
    errors: list[BaseException] = []

    def configure() -> None:
        started.set()
        try:
            results.append(
                store.configure_scope(
                    endpoint.endpoint_id,
                    expected_generation=endpoint.generation,
                    allowed_recipients=(recipient,),
                )
            )
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=configure)
    with endpoint_effect_lock(root, endpoint.endpoint_id):
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.1)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1 and results[0][1]


def test_external_send_rechecks_scope_after_waiting_for_effect_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    endpoint, credential = _enroll(root)
    selected = local_token(root, "a" * 64, str(uuid4()))
    remaining = local_token(root, "b" * 64, str(uuid4()))
    configured, changed = ExternalEndpointStore(root).configure_scope(
        endpoint.endpoint_id,
        expected_generation=endpoint.generation,
        allowed_recipients=(selected, remaining),
    )
    assert changed
    original_lock = endpoint_effect_lock
    narrowed = False

    @contextmanager
    def narrow_before_lock(
        lock_root: Path, endpoint_id: str, *, wait: bool = False
    ) -> Iterator[int]:
        nonlocal narrowed
        if not narrowed:
            narrowed = True
            ExternalEndpointStore(root).configure_scope(
                endpoint.endpoint_id,
                expected_generation=configured.generation,
                allowed_recipients=(remaining,),
            )
        with original_lock(lock_root, endpoint_id, wait=wait) as descriptor:
            yield descriptor

    monkeypatch.setattr("cross_agent_chat.cli.endpoint_effect_lock", narrow_before_lock)
    monkeypatch.setattr(
        cli,
        "send",
        lambda *_args, **_kwargs: pytest.fail("send bypassed narrowed scope"),
    )

    response = cli._external_call_tool(root, credential, _send(selected, str(uuid4())))
    assert response["isError"] is True
    assert (
        "outside this external endpoint's enrolled scope"
        in cast(list[dict[str, str]], response["content"])[0]["text"]
    )
    assert narrowed
    assert IntentStore(root).intents() == []


def test_configured_scope_filters_and_dispatches_only_exact_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    endpoint, credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    first = local_token(root, "1" * 64, str(uuid4()))
    second = remote_token("nSelected", "2" * 64, str(uuid4()))
    outside = local_token(root, "3" * 64, str(uuid4()))
    updated, changed = ExternalEndpointStore(root).configure_scope(
        endpoint.endpoint_id,
        expected_generation=endpoint.generation,
        allowed_recipients=(first, second),
    )
    assert changed and updated.generation != endpoint.generation

    monkeypatch.setattr(
        cli,
        "peers",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "peers": [
                {"alias": "peer:first", "handle": first},
                {"alias": "peer:second", "handle": second},
                {"alias": "peer:outside", "handle": outside},
            ],
            "remote_discovery": "complete",
        },
    )
    listing = _result(
        cli._external_call_tool(root, credential, {"name": "chat_peers", "arguments": {}})
    )
    assert [item["handle"] for item in cast(list[dict[str, str]], listing["peers"])] == [
        first,
        second,
    ]

    calls: list[tuple[str, str, str]] = []

    def fake_send(
        _root: Path,
        source: object,
        target: str,
        message: str,
        *,
        event_id: str | None = None,
        source_lock_fd: int | None = None,
        include_external: bool = False,
    ) -> dict[str, object]:
        assert isinstance(source, ExternalEndpoint)
        assert source.endpoint_id == endpoint.endpoint_id
        assert source_lock_fd is not None and include_external
        calls.append((target, message, event_id or ""))
        return {
            "schema_version": 1,
            "event_id": event_id or "",
            "status": "TRANSPORT_ACCEPTED",
            "to": target,
            "provider": "codex",
        }

    monkeypatch.setattr(cli, "send", fake_send)
    request_id = str(uuid4())
    sent = _result(
        cli._external_call_tool(
            root,
            credential,
            _send(first, request_id, "selected target only"),
        )
    )
    assert sent["status"] == "TRANSPORT_ACCEPTED"
    assert calls == [(first, "selected target only", request_id)]

    with pytest.raises(ChatError, match="outside this external endpoint's enrolled scope"):
        cli._external_call_tool(root, credential, _send(outside, str(uuid4())))
    assert calls == [(first, "selected target only", request_id)]

    old_target_handle = local_token(root, updated.key, endpoint.generation)
    native_source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="fixture",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    monkeypatch.setattr(
        external_callback,
        "post_callback",
        lambda *_args, **_kwargs: pytest.fail("stale generation reached callback"),
    )
    prior_intents = IntentStore(root).intents()
    with pytest.raises(ChatError, match="recipient is unavailable or changed"):
        runtime.send(
            root,
            native_source,
            old_target_handle,
            "stale handle must refuse",
            include_external=True,
        )
    targets = runtime.external_targets(root, handle=updated.key)
    assert len(targets) == 1 and targets[0].generation == updated.generation
    assert all(target.generation != endpoint.generation for target in targets)
    assert IntentStore(root).intents() == prior_intents


def test_native_roster_remains_usable_until_external_opt_in(tmp_path: Path) -> None:
    root = tmp_path / "state"
    endpoint, _ = _enroll(root)
    configured = _callback(root, endpoint)
    with _native_courier(root, tmp_path) as native:
        legacy = handle_broker_request(
            root, {"schema_version": 1, "operation": "peers"}, "100.64.0.2"
        )
        rows = cast(list[dict[str, str]], legacy["peers"])
        assert [row["provider"] for row in rows] == ["codex"]
        runtime._targets_from_tailnet("100.64.0.2", legacy)
        extended = handle_broker_request(
            root,
            {"schema_version": 1, "operation": "peers", "include_external": True},
            "100.64.0.2",
        )
        targets = runtime._targets_from_tailnet("100.64.0.2", extended, include_external=True)
        assert {item.session_key for item in targets} == {
            session_key(native.provider, native.session_id),
            configured.key,
        }


def test_callback_unknown_is_recorded_once_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source, credential = _enroll(root)
    target, _ = _enroll(root)
    target = _callback(root, target)
    calls: list[dict[str, object]] = []

    def uncertain(
        _config: external_callback.CallbackConfig,
        envelope: dict[str, object],
        *,
        timeout_seconds: float,
        lock_fds: tuple[int, ...],
    ) -> external_callback.CallbackOutcome:
        assert timeout_seconds > 0 and lock_fds
        calls.append(envelope)
        return "UNKNOWN_DELIVERY"

    monkeypatch.setattr(external_callback, "post_callback", uncertain)
    token = local_token(root, target.key, target.generation)
    event = str(uuid4())
    first = cli._external_call_tool(root, credential, _send(token, event))
    assert first["isError"] is True
    repeated = _result(cli._external_call_tool(root, credential, _send(token, event)))
    assert repeated["status"] == "UNKNOWN_DELIVERY"
    assert len(calls) == 1 and calls[0]["source_alias"] == source.alias
    assert calls[0]["target_alias"] == target.alias and calls[0]["event_id"] == event


def test_revocation_waits_for_admitted_source_send_and_blocks_next_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source, credential = _enroll(root)
    target, _ = _enroll(root)
    target = _callback(root, target)
    entered, finish, revoked = threading.Event(), threading.Event(), threading.Event()
    failures: list[BaseException] = []
    calls: list[str] = []

    def receiver(
        _config: external_callback.CallbackConfig,
        envelope: dict[str, object],
        *,
        timeout_seconds: float,
        lock_fds: tuple[int, ...],
    ) -> external_callback.CallbackOutcome:
        assert timeout_seconds > 0 and len(lock_fds) == 2
        calls.append(str(envelope["event_id"]))
        entered.set()
        assert finish.wait(3)
        return "TRANSPORT_ACCEPTED"

    def deliver() -> None:
        try:
            result = cli._external_call_tool(
                root,
                credential,
                _send(local_token(root, target.key, target.generation), str(uuid4())),
            )
            assert _result(result)["status"] == "TRANSPORT_ACCEPTED"
        except BaseException as error:
            failures.append(error)

    def revoke() -> None:
        try:
            ExternalEndpointStore(root).revoke(source.endpoint_id)
            revoked.set()
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(external_callback, "post_callback", receiver)
    sender = threading.Thread(target=deliver)
    sender.start()
    assert entered.wait(3)
    revoker = threading.Thread(target=revoke)
    revoker.start()
    try:
        assert not revoked.wait(0.1)
    finally:
        finish.set()
        sender.join(3)
        revoker.join(3)
    assert not failures and revoked.is_set() and len(calls) == 1
    with pytest.raises(ChatError, match="credential is unavailable"):
        cli._external_call_tool(
            root, credential, _send(local_token(root, target.key, target.generation), str(uuid4()))
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://receiver.example.com",
        "https://127.0.0.1/",
        "https://169.254.169.254/",
        "https://machine.local/",
        "https://u:p@receiver.example.com/",
        "https://receiver.example.com/#fragment",
        "https://receiver.example.com:444/",
    ],
)
def test_callback_rejects_secret_forwarding_and_private_url_modes(url: str) -> None:
    with pytest.raises(ChatError):
        external_callback.CallbackConfig(url, "fixture-key")


def test_worker_timeout_never_claims_no_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("owned callback worker", 15)

    monkeypatch.setattr(subprocess, "run", timeout)
    config = external_callback.CallbackConfig("https://receiver.example.com/", "fixture-secret")
    assert (
        external_callback.post_callback(
            config, {"message": "fixture"}, timeout_seconds=1, lock_fds=(1,)
        )
        == "UNKNOWN_DELIVERY"
    )
    assert "fixture-secret" not in repr(config)


def test_uninstall_removes_external_authority_but_preserves_intents(tmp_path: Path) -> None:
    from cross_agent_chat.install import Installer

    installer = Installer(home=tmp_path / "home", device="fixture", executable=Path("/fixture"))
    endpoint, _ = _enroll(installer.state)
    _callback(installer.state, endpoint)
    store = IntentStore(installer.state)
    store.begin_identity(
        source_key=endpoint.key,
        source_generation=endpoint.generation,
        source_alias=endpoint.alias,
        target_key="a" * 64,
        target_generation=str(uuid4()),
        payload_digest="b" * 64,
    )
    assert installer._remove_runtime_state() is True
    assert store.intents()
    assert ExternalEndpointStore(installer.state).endpoints() == []
    assert not list(installer.state.glob("external-callback-*.json"))
    assert asdict(endpoint)["schema_version"] == 1


def test_remote_external_receive_requires_exact_reverse_authorization_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    sender_root, receiver_root = tmp_path / "sender", tmp_path / "receiver"
    source, _ = _enroll(sender_root)
    target, _ = _enroll(receiver_root)
    target = _callback(receiver_root, target)
    event = str(uuid4())
    body = runtime.wrapped_message(
        source.alias,
        remote_token("nSource", source.key, source.generation),
        "new authorized remote work",
        event,
        "external",
    )
    IntentStore(sender_root).begin_identity(
        source_key=source.key,
        source_generation=source.generation,
        source_alias=source.alias,
        target_key=target.key,
        target_generation=target.generation,
        payload_digest=hashlib.sha256(body.encode()).hexdigest(),
        event_id=event,
    )
    envelope = remote_envelope(
        event_id=event,
        source_alias=source.alias,
        source_generation=source.generation,
        target_alias=target.alias,
        generation=target.generation,
        message=body,
    )
    calls: list[dict[str, object]] = []

    def authorized(
        address: str, payload: dict[str, object], *, timeout: float
    ) -> dict[str, object]:
        assert address == "100.64.0.1" and payload["operation"] == "authorize" and timeout > 0
        return handle_broker_request(sender_root, payload, "100.64.0.2")

    def receiver(
        _config: external_callback.CallbackConfig,
        message: dict[str, object],
        *,
        timeout_seconds: float,
        lock_fds: tuple[int, ...],
    ) -> external_callback.CallbackOutcome:
        assert timeout_seconds > 0 and len(lock_fds) == 1
        calls.append(message)
        return "TRANSPORT_ACCEPTED"

    monkeypatch.setattr(runtime, "request_tailnet", authorized)
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nReceiver", peers={"nSource": "100.64.0.1"}),
    )
    monkeypatch.setattr(external_callback, "post_callback", receiver)
    received = runtime.receive_remote(receiver_root, envelope, "100.64.0.1")
    assert received["status"] == "TRANSPORT_ACCEPTED"
    assert len(calls) == 1 and calls[0]["message"] == body
    replay = runtime.receive_remote(receiver_root, envelope, "100.64.0.1")
    assert replay["status"] == "PRE_EFFECT_REJECTED" and len(calls) == 1
    assert IntentStore(sender_root).intents()[0].status == "REMOTE_AUTHORIZED"


def test_callback_dns_rejects_any_nonpublic_answer_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def addresses(
        *_args: object, **_kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", addresses)
    monkeypatch.setattr(
        external_callback,
        "_PinnedHTTPSConnection",
        lambda *_a: pytest.fail("unsafe DNS reached connect"),
    )
    config = external_callback.CallbackConfig("https://receiver.example.com/", "fixture-key")
    assert external_callback._post_https(config, "{}") == "PRE_EFFECT_REJECTED"


def test_callback_redirect_is_unknown_and_never_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    connections: list[tuple[str, str]] = []
    requests: list[tuple[str, str, dict[str, str]]] = []

    class Receiver:
        def __init__(self, host: str, address: str) -> None:
            connections.append((host, address))

        def connect(self) -> None:
            pass

        def request(self, method: str, path: str, *, body: bytes, headers: dict[str, str]) -> None:
            assert body == b"{}"
            requests.append((method, path, headers))

        def getresponse(self) -> SimpleNamespace:
            return SimpleNamespace(status=302, location="https://attacker.example.com/")

        def close(self) -> None:
            pass

    monkeypatch.setattr(external_callback, "_public_addresses", lambda _host: ["8.8.8.8"])
    monkeypatch.setattr(external_callback, "_PinnedHTTPSConnection", Receiver)
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.com/")
    config = external_callback.CallbackConfig(
        "https://receiver.example.com/enrolled", "fixture-key"
    )
    assert external_callback._post_https(config, "{}") == "UNKNOWN_DELIVERY"
    assert connections == [("receiver.example.com", "8.8.8.8")]
    assert len(requests) == 1 and requests[0][0:2] == ("POST", "/enrolled")
    assert requests[0][2]["Authorization"] == "Bearer fixture-key"


def test_https_connection_uses_checked_ip_and_enrolled_tls_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket
    import ssl

    connected: list[tuple[str, int]] = []
    names: list[str] = []
    client, peer = socket.socketpair()
    connection = external_callback._PinnedHTTPSConnection("receiver.example.com", "8.8.8.8")
    assert connection._tls_context.check_hostname is True
    assert connection._tls_context.verify_mode == ssl.CERT_REQUIRED

    class TLS:
        def wrap_socket(self, sock: socket.socket, *, server_hostname: str) -> socket.socket:
            names.append(server_hostname)
            return sock

    def connect(address: tuple[str, int], timeout: float | None) -> socket.socket:
        assert timeout is not None and timeout > 0
        connected.append(address)
        return client

    monkeypatch.setattr(socket, "create_connection", connect)
    monkeypatch.setattr(connection, "_tls_context", TLS())
    try:
        connection.connect()
        assert connected == [("8.8.8.8", 443)]
        assert names == ["receiver.example.com"]
    finally:
        connection.close()
        peer.close()


def test_corrupt_external_state_does_not_break_native_exact_token_or_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _enroll(root)
    monkeypatch.setattr(runtime, "native_thread_titles", lambda **_kwargs: {})
    with _native_courier(root, tmp_path) as native:
        (root / "external-endpoints-v1.json").write_text("invalid")
        listing = runtime.peers(root, include_remote=False, include_external=True)
        assert listing["external_discovery"] == "unavailable"
        assert [row["provider"] for row in cast(list[dict[str, str]], listing["peers"])] == [
            "codex"
        ]
        token = local_token(
            root, session_key(native.provider, native.session_id), native.generation
        )
        result = runtime.send(root, native, token, "new independent native work")
        assert result["status"] == "TRANSPORT_ACCEPTED"


def test_revoke_removes_owned_callback_copy(tmp_path: Path) -> None:
    root = tmp_path / "state"
    endpoint, credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    callback = root / cast(str, endpoint.callback_ref)
    assert callback.exists()
    ExternalEndpointStore(root).revoke(endpoint.endpoint_id)
    assert not callback.exists()
    with pytest.raises(ChatError, match="credential is unavailable"):
        ExternalEndpointStore(root).authenticate(credential)


@pytest.mark.parametrize("modern", [False, True])
def test_remote_titles_use_only_the_negotiated_external_capability(
    monkeypatch: pytest.MonkeyPatch, modern: bool
) -> None:
    calls: list[dict[str, object]] = []
    peer = {
        "alias": "external@fixture:owned:123456789abc"
        if modern
        else "codex@fixture:owned:123456789abc",
        "provider": "external" if modern else "codex",
        "device": "fixture",
        "project": "owned",
        "status": "available",
        "generation": str(uuid4()),
        "session_key": "a" * 64,
    }

    def discovery(
        _address: str, payload: dict[str, object], *, timeout: float
    ) -> dict[str, object]:
        assert timeout > 0
        calls.append(payload)
        if not modern and "include_external" in payload:
            raise ChatError("old broker rejected external capability")
        rows = [] if modern and "include_external" not in payload else [dict(peer)]
        for row in rows:
            if payload.get("include_delivery_mode"):
                row["delivery_mode"] = "unknown"
            if payload.get("include_title"):
                row["title"] = "qualified display title"
        return {"schema_version": 1, "peers": rows}

    monkeypatch.setattr(runtime, "request_tailnet", discovery)
    targets, complete = runtime._remote_node_targets(
        "100.64.0.2", include_external=True, include_title=True, node_id="nSelected"
    )
    assert complete and len(targets) == 1 and targets[0].title == "qualified display title"
    rich = [request for request in calls if request.get("include_title")]
    assert len(rich) == 1 and ("include_external" in rich[0]) is modern


def test_stale_target_retry_reports_old_custody_without_replay_advice(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _endpoint, credential = _enroll(root)
    event = str(uuid4())
    with _native_courier(root, tmp_path) as native:
        token = local_token(
            root, session_key(native.provider, native.session_id), native.generation
        )
        accepted = cli._external_call_tool(root, credential, _send(token, event))
        assert _result(accepted)["status"] == "TRANSPORT_ACCEPTED"
    Registry(root).remove(
        native.provider, native.session_id, native.pid, generation=native.generation
    )
    retry = cli._external_call_tool(root, credential, _send(token, event))
    assert retry["isError"] is True
    content = cast(list[dict[str, str]], retry["content"])[0]["text"]
    assert "recorded custody TRANSPORT_ACCEPTED" in content
    assert "Do not replay" in content and "use chat_status" in content
    assert "call chat_peers" not in content
    assert len(IntentStore(root).intents()) == 1


def test_busy_endpoint_refuses_before_intent_or_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    source, credential = _enroll(root)
    target, _ = _enroll(root)
    target = _callback(root, target)
    monkeypatch.setattr(
        external_callback, "post_callback", lambda *_a, **_k: pytest.fail("busy effect ran")
    )
    token = local_token(root, target.key, target.generation)
    with endpoint_effect_lock(root, source.endpoint_id):
        busy = cli._external_call_tool(root, credential, _send(token, str(uuid4())))
    assert busy["isError"] is True and IntentStore(root).intents() == []
    with endpoint_effect_lock(root, target.endpoint_id):
        busy = cli._external_call_tool(root, credential, _send(token, str(uuid4())))
    assert busy["isError"] is True
    assert IntentStore(root).intents()[0].status == "PRE_EFFECT_REJECTED"


def test_external_meta_is_ignored_and_scoped_count_stays_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    root = tmp_path / "state"
    permitted = local_token(root, "a" * 64, str(uuid4()))
    _endpoint, credential = ExternalEndpointStore(root).enroll(
        device="fixture", name="scoped", context="fixture", allowed_recipients=(permitted,)
    )
    monkeypatch.setattr(
        cli,
        "peers",
        lambda *_a, **_k: {
            "schema_version": 1,
            "peers": [
                {"alias": "codex@fixture:owned", "handle": permitted},
                {"alias": "codex@fixture:private", "handle": "different"},
            ],
            "remote_discovery": "complete",
        },
    )
    result = _result(
        cli._external_call_tool(
            root,
            credential,
            {
                "name": "chat_peers",
                "arguments": {"query": "owned"},
                "_meta": {"progressToken": "fixture", "from": "claude"},
            },
        )
    )
    assert result["filter"] == {"query": "owned", "matched": 1, "of": 1}
    credential_path = root / "credential"
    credential_path.write_text(credential)
    credential_path.chmod(0o600)
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "fixture", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": {"progressToken": "fixture"}},
        },
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(json.dumps(item) for item in messages)))
    cli.external_mcp(root, credential_path)
    response = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert "tools" in response["result"]
    assert {tool["name"] for tool in response["result"]["tools"]} == {
        "chat_peers",
        "chat_send",
        "chat_status",
    }


def test_outer_post_spawn_oserror_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failed_communication(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("fixture error after child creation")

    monkeypatch.setattr(subprocess, "run", failed_communication)
    config = external_callback.CallbackConfig("https://receiver.example.com/", "fixture-key")
    assert (
        external_callback.post_callback(
            config, {"message": "fixture"}, timeout_seconds=1, lock_fds=(1,)
        )
        == "UNKNOWN_DELIVERY"
    )


def test_real_worker_ignores_malicious_cwd_and_ambient_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    endpoint, _ = _enroll(root)
    malicious = tmp_path / "untrusted-project"
    malicious.mkdir()
    package = malicious / "cross_agent_chat"
    package.mkdir()
    marker = tmp_path / "shadow-executed"
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('wrong module ran')\n"
    )
    (package / "external_callback.py").write_text("print('TRANSPORT_ACCEPTED')\n")
    monkeypatch.chdir(malicious)
    monkeypatch.setenv("PYTHONPATH", str(malicious))
    # Only the parent fixture's validation is replaced. The real safe-path
    # child rejects localhost before DNS/network and emits the genuine refusal.
    monkeypatch.setattr(external_callback, "callback_destination", lambda _url: ("localhost", "/"))
    config = external_callback.CallbackConfig("https://localhost/", "fixture-key")
    with endpoint_effect_lock(root, endpoint.endpoint_id) as descriptor:
        result = external_callback.post_callback(
            config, {"message": "fixture"}, timeout_seconds=2, lock_fds=(descriptor,)
        )
    assert result == "PRE_EFFECT_REJECTED" and not marker.exists()


def test_real_orphan_worker_keeps_both_endpoint_locks_until_its_own_deadline(
    tmp_path: Path,
) -> None:
    import signal
    import sys

    root = tmp_path / "state"
    source, credential = _enroll(root)
    target, _ = _enroll(root)
    target = _callback(root, target)
    credential_path = root / "credential"
    credential_path.write_text(credential)
    credential_path.chmod(0o600)
    marker = tmp_path / "worker-pid"
    pending_input = tmp_path / "pending-worker-input"
    os.mkfifo(pending_input, 0o600)
    input_custody = os.open(pending_input, os.O_RDWR | os.O_NONBLOCK)
    os.write(input_custody, b"{")
    parent_script = tmp_path / "orphan-parent.py"
    parent_script.write_text("""from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path
from cross_agent_chat import cli, external_callback
from cross_agent_chat.external import read_credential
from cross_agent_chat.external_callback import CallbackConfig, CallbackOutcome

real_post = external_callback.post_callback
def bounded(config: CallbackConfig, envelope: dict[str, object], *,
    timeout_seconds: float, lock_fds: tuple[int, ...]) -> CallbackOutcome:
    return real_post(config, envelope, timeout_seconds=min(timeout_seconds, 2.0), lock_fds=lock_fds)

def paused_worker(command: list[str], *, input: str, text: bool, capture_output: bool,
    timeout: float, env: dict[str, str], pass_fds: tuple[int, ...], check: bool
) -> subprocess.CompletedProcess[str]:
    if len(pass_fds) != 2:
        raise RuntimeError("missing source or target lock custody")
    with Path(sys.argv[5]).open("rb") as pending_input:
        child = subprocess.Popen(command, stdin=pending_input, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, pass_fds=pass_fds)
    Path(sys.argv[3]).write_text(str(child.pid))
    # Keep stdin open to model a stuck resolver/I/O phase without any network.
    # The real worker's kernel alarm is the only surviving deadline after kill.
    while True:
        time.sleep(0.05)

external_callback.post_callback = bounded
external_callback.subprocess.run = paused_worker
request = json.loads(Path(sys.argv[4]).read_text())
cli._external_call_tool(Path(sys.argv[1]), read_credential(Path(sys.argv[2])), request)
""")
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(_send(local_token(root, target.key, target.generation), str(uuid4())))
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(runtime.__file__).resolve().parent.parent)
    parent = subprocess.Popen(
        [
            sys.executable,
            str(parent_script),
            str(root),
            str(credential_path),
            str(marker),
            str(request_path),
            str(pending_input),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    workers: list[threading.Thread] = []
    finished: list[str] = []
    errors: list[BaseException] = []

    def revoke(identifier: str) -> None:
        try:
            ExternalEndpointStore(root).revoke(identifier)
            finished.append(identifier)
        except BaseException as error:
            errors.append(error)

    child_pid: int | None = None
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            if parent.poll() is not None:
                pytest.fail("fixture parent failed before real worker creation")
            time.sleep(0.01)
        assert marker.exists()
        child_pid = int(marker.read_text())
        parent.kill()
        parent.communicate(timeout=2)
        for endpoint in (source, target):
            thread = threading.Thread(target=revoke, args=(endpoint.endpoint_id,))
            workers.append(thread)
            thread.start()
        time.sleep(0.15)
        assert finished == []
        for thread in workers:
            thread.join(timeout=3)
        assert not errors and set(finished) == {source.endpoint_id, target.endpoint_id}
        assert all(not item.available() for item in ExternalEndpointStore(root).endpoints())
        assert IntentStore(root).intents()[0].status == "PENDING"
    finally:
        os.close(input_custody)
        if parent.poll() is None:
            parent.kill()
            parent.communicate(timeout=2)
        if child_pid is not None and len(finished) != 2:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
        for thread in workers:
            thread.join(timeout=3)


def _callback_input(root: Path, url: str) -> Path:
    config = root / f"callback-input-{uuid4().hex}.json"
    atomic_json(config, {"url": url, "bearer": f"fixture-key-{uuid4().hex}"})
    return config


def _delivered_urls(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: ExternalEndpoint,
    generation: str,
) -> list[str]:
    """Resolve one exact recipient handle through the real send path."""
    urls: list[str] = []

    def capture(
        config: external_callback.CallbackConfig,
        _envelope: dict[str, object],
        *,
        timeout_seconds: float,
        lock_fds: tuple[int, ...],
    ) -> external_callback.CallbackOutcome:
        assert timeout_seconds > 0 and lock_fds
        urls.append(config.url)
        return "TRANSPORT_ACCEPTED"

    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="fixture",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(root).upsert(source)
    monkeypatch.setattr(external_callback, "post_callback", capture)
    runtime.send(
        root,
        source,
        local_token(root, endpoint.key, generation),
        "callback binding fixture",
        event_id=str(uuid4()),
        include_external=True,
    )
    return urls


@contextmanager
def _fail_writes(root: Path, failing: Path) -> Iterator[None]:
    real_atomic_json = external.atomic_json

    def faulty(path: Path, value: object) -> None:
        if path == failing:
            raise OSError("injected write failure, not a real disk error")
        real_atomic_json(path, value)

    external.atomic_json = faulty
    try:
        yield
    finally:
        external.atomic_json = real_atomic_json


def test_callback_update_commit_failure_never_redirects_old_handles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash between the binding copy and the record commit keeps old custody."""
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    old_url = external_callback.read_callback(root / cast(str, endpoint.callback_ref)).url

    with (
        _fail_writes(root, root / "external-endpoints-v1.json"),
        pytest.raises(OSError, match="injected write failure"),
    ):
        ExternalEndpointStore(root).configure_callback(
            endpoint.endpoint_id,
            _callback_input(root, "https://replacement.example.com/new"),
        )

    # A fresh store models the restarted reader: the committed record still
    # selects the original binding, so the old handle keeps the old destination
    # instead of silently reaching the uncommitted replacement.
    restarted = ExternalEndpointStore(root)
    assert restarted.endpoints() == [endpoint] and restarted.current(endpoint)
    retained = restarted.endpoints()[0]
    assert external_callback.read_callback(root / cast(str, retained.callback_ref)).url == old_url
    assert {path.name for path in root.glob("external-callback-*.json")} == {
        cast(str, endpoint.callback_ref)
    }
    assert _delivered_urls(root, tmp_path, monkeypatch, endpoint, endpoint.generation) == [old_url]


def test_callback_update_failure_before_any_write_leaves_state_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    old_url = external_callback.read_callback(root / cast(str, endpoint.callback_ref)).url
    state_bytes = ExternalEndpointStore(root).path.read_bytes()

    incoming = _callback_input(root, "https://replacement.example.com/new")
    real_atomic_json = external.atomic_json

    def fail_first(path: Path, value: object) -> None:
        if path != ExternalEndpointStore(root).path:
            raise OSError("injected write failure, not a real disk error")
        real_atomic_json(path, value)

    external.atomic_json = fail_first
    try:
        with pytest.raises(OSError, match="injected write failure"):
            ExternalEndpointStore(root).configure_callback(endpoint.endpoint_id, incoming)
    finally:
        external.atomic_json = real_atomic_json

    assert ExternalEndpointStore(root).path.read_bytes() == state_bytes
    assert {path.name for path in root.glob("external-callback-*.json")} == {
        cast(str, endpoint.callback_ref)
    }
    assert _delivered_urls(root, tmp_path, monkeypatch, endpoint, endpoint.generation) == [old_url]


def test_callback_commit_reported_failure_keeps_a_committed_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.replace may commit before a later fsync fails; no cleanup then."""
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    store = ExternalEndpointStore(root)
    new_url = "https://replacement.example.com/fsync"
    incoming = _callback_input(root, new_url)

    state = {"fail_dir_fsync": False}
    real_fsync = os.fsync
    real_atomic_json = external.atomic_json

    def fsync_with_post_commit_fault(fd: int) -> None:
        if state["fail_dir_fsync"] and stat.S_ISDIR(os.fstat(fd).st_mode):
            state["fail_dir_fsync"] = False
            raise OSError("injected fsync failure after committed replace")
        real_fsync(fd)

    def committing(path: Path, value: object) -> None:
        if path == store.path:
            state["fail_dir_fsync"] = True
        real_atomic_json(path, value)

    monkeypatch.setattr(external, "atomic_json", committing)
    monkeypatch.setattr(os, "fsync", fsync_with_post_commit_fault)
    try:
        with pytest.raises(OSError, match="fsync failure"):
            store.configure_callback(endpoint.endpoint_id, incoming)
    finally:
        external.atomic_json = real_atomic_json

    # The rename already committed, so the fresh reader sees the new
    # generation selecting the new binding -- which must still exist.
    restarted = ExternalEndpointStore(root)
    retained = restarted.endpoints()[0]
    assert retained.generation != endpoint.generation
    assert retained.callback_ref != endpoint.callback_ref
    committed = root / cast(str, retained.callback_ref)
    assert committed.exists()
    assert external_callback.read_callback(committed).url == new_url
    assert _delivered_urls(root, tmp_path, monkeypatch, retained, retained.generation) == [new_url]
    with pytest.raises(ChatError, match="unavailable or changed"):
        _delivered_urls(root, tmp_path, monkeypatch, endpoint, endpoint.generation)


def test_callback_commit_failure_with_unreadable_record_keeps_the_orphan(
    tmp_path: Path,
) -> None:
    """A failed selection readback must never authorize binding cleanup."""
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    store = ExternalEndpointStore(root)
    incoming = _callback_input(root, "https://replacement.example.com/orphan")

    state = {"armed": False}
    real_atomic_json = external.atomic_json
    real_endpoints = ExternalEndpointStore.endpoints

    def committing(path: Path, value: object) -> None:
        if path == store.path:
            state["armed"] = True
            raise OSError("injected write failure, not a real disk error")
        real_atomic_json(path, value)

    def unreadable_endpoints(self: ExternalEndpointStore) -> list[ExternalEndpoint]:
        if state["armed"]:
            raise ChatError("injected record readback failure")
        return real_endpoints(self)

    external.atomic_json = committing
    ExternalEndpointStore.endpoints = unreadable_endpoints  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError, match="injected write failure"):
            store.configure_callback(endpoint.endpoint_id, incoming)
    finally:
        external.atomic_json = real_atomic_json
        ExternalEndpointStore.endpoints = real_endpoints  # type: ignore[method-assign]

    # The old binding stays selected and live; the uncommitted copy is an
    # unreachable orphan that cleanup may only retire, never confuse with it.
    assert store.endpoints() == [endpoint] and store.current(endpoint)
    assert (
        external_callback.read_callback(root / cast(str, endpoint.callback_ref)).url
        == "https://receiver.example.com/enrolled"
    )
    orphans = [
        path for path in root.glob("external-callback-*.json") if path.name != endpoint.callback_ref
    ]
    assert len(orphans) == 1
    store.revoke(endpoint.endpoint_id)
    assert not list(root.glob("external-callback-*.json"))


def test_callback_reconfigure_switches_generation_and_destination_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    store = ExternalEndpointStore(root)

    updated = store.configure_callback(
        endpoint.endpoint_id,
        _callback_input(root, "https://replacement.example.com/second"),
    )
    assert updated.generation != endpoint.generation
    assert updated.callback_ref is not None and updated.callback_ref != endpoint.callback_ref
    # Exactly one live binding file remains after the atomic switch.
    assert {path.name for path in root.glob("external-callback-*.json")} == {updated.callback_ref}
    # The stale handle refuses rather than following the replaced binding.
    with pytest.raises(ChatError, match="unavailable or changed"):
        _delivered_urls(root, tmp_path, monkeypatch, endpoint, endpoint.generation)
    assert _delivered_urls(root, tmp_path, monkeypatch, updated, updated.generation) == [
        "https://replacement.example.com/second"
    ]

    third = store.configure_callback(
        endpoint.endpoint_id,
        _callback_input(root, "https://replacement.example.com/third"),
    )
    assert {path.name for path in root.glob("external-callback-*.json")} == {third.callback_ref}
    assert not (root / cast(str, endpoint.callback_ref)).exists()
    assert not (root / updated.callback_ref).exists()
    assert _delivered_urls(root, tmp_path, monkeypatch, third, third.generation) == [
        "https://replacement.example.com/third"
    ]


def test_callback_reconfigure_between_match_and_effect_refuses_stale_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit landing while a send waits for the effect lock stays refused."""
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    replacement = _callback_input(root, "https://replacement.example.com/new")
    original_lock = runtime.endpoint_effect_lock
    reconfigured = False

    @contextmanager
    def reconfigure_before_lock(
        lock_root: Path, endpoint_id: str, *, wait: bool = False
    ) -> Iterator[int]:
        nonlocal reconfigured
        if not reconfigured:
            reconfigured = True
            ExternalEndpointStore(root).configure_callback(endpoint.endpoint_id, replacement)
        with original_lock(lock_root, endpoint_id, wait=wait) as descriptor:
            yield descriptor

    monkeypatch.setattr(runtime, "endpoint_effect_lock", reconfigure_before_lock)
    monkeypatch.setattr(
        external_callback,
        "post_callback",
        lambda *_a, **_k: pytest.fail("stale snapshot reached callback"),
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="fixture",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(root).upsert(source)
    with pytest.raises(ChatError, match="changed before effect"):
        runtime.send(
            root,
            source,
            local_token(root, endpoint.key, endpoint.generation),
            "stale snapshot fixture",
            event_id=str(uuid4()),
            include_external=True,
        )
    assert reconfigured


def test_callback_binding_survives_scope_and_credential_updates(tmp_path: Path) -> None:
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    store = ExternalEndpointStore(root)
    callback_ref = cast(str, endpoint.callback_ref)
    callback_bytes = (root / callback_ref).read_bytes()

    rotated, rotated_credential = store.rotate(endpoint.endpoint_id)
    assert rotated.generation != endpoint.generation
    assert rotated.callback_ref == callback_ref
    assert store.authenticate(rotated_credential).callback_ref == callback_ref

    scoped, changed = store.configure_scope(
        endpoint.endpoint_id,
        expected_generation=rotated.generation,
        allowed_recipients=(local_token(root, "a" * 64, str(uuid4())),),
    )
    assert changed and scoped.callback_ref == callback_ref
    assert (root / callback_ref).read_bytes() == callback_bytes

    # A scope update armed against a pre-callback generation refuses cleanly.
    with pytest.raises(ChatError, match="generation changed"):
        store.configure_scope(
            endpoint.endpoint_id,
            expected_generation=endpoint.generation,
            allowed_recipients=(local_token(root, "b" * 64, str(uuid4())),),
        )
    assert (root / callback_ref).read_bytes() == callback_bytes


def test_configure_callback_waits_for_endpoint_effect_lock(tmp_path: Path) -> None:
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    incoming = _callback_input(root, "https://replacement.example.com/new")
    started, finished = threading.Event(), threading.Event()
    results: list[ExternalEndpoint] = []
    errors: list[BaseException] = []

    def configure() -> None:
        started.set()
        try:
            results.append(
                ExternalEndpointStore(root).configure_callback(endpoint.endpoint_id, incoming)
            )
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=configure)
    with endpoint_effect_lock(root, endpoint.endpoint_id):
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.1)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == [] and len(results) == 1
    assert results[0].generation != endpoint.generation


def test_expired_endpoint_callback_update_refuses_without_writes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = ExternalEndpointStore(root)
    expired, _credential = store.enroll(
        device="fixture",
        name="expired",
        context="fixture",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    state_bytes = store.path.read_bytes()
    with pytest.raises(ChatError, match="unavailable"):
        store.configure_callback(
            expired.endpoint_id,
            _callback_input(root, "https://replacement.example.com/new"),
        )
    assert store.path.read_bytes() == state_bytes
    assert not list(root.glob("external-callback-*.json"))


def test_revoke_removes_every_binding_generation_but_never_a_live_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    first, _ = _enroll(root)
    first = _callback(root, first)
    second, _ = _enroll(root)
    second = _callback(root, second)
    store = ExternalEndpointStore(root)

    store.revoke(first.endpoint_id)
    assert not list(root.glob(f"external-callback-{first.endpoint_id}-*.json"))
    assert not (root / f"external-callback-{first.endpoint_id}.json").exists()
    assert (root / cast(str, second.callback_ref)).exists()

    # An unreachable versioned copy left by a crashed commit is still retired
    # with its endpoint; a live sibling binding is never touched.
    orphan = root / f"external-callback-{second.endpoint_id}-{uuid4()}.json"
    atomic_json(orphan, {"url": "https://orphan.example.com/", "bearer": "fixture"})
    live_bytes = (root / cast(str, second.callback_ref)).read_bytes()
    store.revoke(second.endpoint_id)
    assert not orphan.exists()
    assert not list(root.glob("external-callback-*.json"))
    assert live_bytes


def test_revoke_commit_failure_keeps_the_live_binding(tmp_path: Path) -> None:
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    endpoint = _callback(root, endpoint)
    callback_path = root / cast(str, endpoint.callback_ref)
    store = ExternalEndpointStore(root)

    with (
        _fail_writes(root, store.path),
        pytest.raises(OSError, match="injected write failure"),
    ):
        store.revoke(endpoint.endpoint_id)
    assert callback_path.exists() and store.current(endpoint)


def test_callback_ref_accepts_only_exact_endpoint_owned_names(tmp_path: Path) -> None:
    root = tmp_path / "state"
    endpoint, _credential = _enroll(root)
    record = asdict(endpoint)
    eid, generation = endpoint.endpoint_id, str(uuid4())
    for accepted in (
        f"external-callback-{eid}.json",
        f"external-callback-{eid}-{generation}.json",
        f"external-callback-{eid}-{uuid4()}.json",
    ):
        assert (
            ExternalEndpoint.from_object({**record, "callback_ref": accepted}).callback_ref
            == accepted
        )
    for rejected in (
        "../outside.json",
        ".json",
        f"-{generation}.json",
        f"xexternal-callback-{eid}.json",
        f"external-callback-{uuid4()}.json",
        f"external-callback-{eid}-not-a-uuid.json",
        f"external-callback-{eid}-{generation}.json.bak",
        f"external-callback-{uuid4()}-{generation}.json",
        f"external-callback-{eid}-{generation[:-1]}z.json",
    ):
        with pytest.raises(ChatError, match="callback reference is invalid"):
            ExternalEndpoint.from_object({**record, "callback_ref": rejected})


def test_local_enrolled_name_survives_codex_title_enrichment_and_selects_its_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex metadata must not erase an enrolled receiver's independently owned name."""
    root = tmp_path / "state"
    sender, credential = ExternalEndpointStore(root).enroll(
        device="fixture", name="requester", context="owned source for name-selection regression"
    )
    receiver, _ = _enroll(root)
    receiver = _callback(root, receiver)
    event = str(uuid4())
    submissions: list[dict[str, object]] = []

    def callback(
        config: external_callback.CallbackConfig,
        envelope: dict[str, object],
        **_: object,
    ) -> str:
        assert config.url == "https://receiver.example.com/enrolled"
        submissions.append(envelope)
        return "TRANSPORT_ACCEPTED"

    # Real enrollment, binding publication, authentication, title selection and
    # callback readback run; the final HTTP boundary stays wholly contained.
    monkeypatch.setattr(external_callback, "post_callback", callback)
    monkeypatch.setattr(runtime, "_remote_discovery", lambda **_: ([], True))
    tool_result = cli._external_call_tool(root, credential, _send(receiver.name, event))
    assert tool_result.get("isError") is not True, tool_result
    result = _result(tool_result)

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert result["event_id"] == event
    assert result["to"] == receiver.alias
    assert len(submissions) == 1
    submitted = submissions[0]
    assert submitted["source_alias"] == sender.alias
    assert submitted["source_generation"] == sender.generation
    assert submitted["target_alias"] == receiver.alias
    assert submitted["generation"] == receiver.generation
    reply = parse_recipient_token(str(submitted["reply_handle"]))
    assert reply is not None
    assert reply.handle == sender.key
    assert reply.generation == sender.generation
