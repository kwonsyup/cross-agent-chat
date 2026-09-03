from __future__ import annotations

import errno
import json
import os
import select
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pytest

import cross_agent_chat.tailnet_broker as tailnet_broker_module
from cross_agent_chat import __version__
from cross_agent_chat.cli import parser
from cross_agent_chat.core import ChatError, IntentStore, Registry, Route, session_key
from cross_agent_chat.runtime import (
    REMOTE_DISCOVERY_TIMEOUT_SECONDS,
    Target,
    authorize_remote,
    receive_remote,
    remote_targets,
    request_tailnet,
    send,
)
from cross_agent_chat.tailnet import parse_local_tailnet_address, parse_tailnet_nodes
from cross_agent_chat.tailnet_broker import (
    BrokerAdmission,
    bind_broker_listener,
    broker_bindings,
    broker_server,
    dispatch_broker_connection,
    dispatch_ready_brokers,
    handle_broker_request,
    serve_broker_connection,
)
from cross_agent_chat.transport import remote_envelope


def test_tailnet_discovery_returns_only_online_ipv4_nodes() -> None:
    payload = json.dumps(
        {
            "Peer": {
                "node-a": {
                    "Online": True,
                    "TailscaleIPs": ["100.64.0.11", "fd7a:115c:a1e0::1"],
                },
                "node-b": {
                    "Online": False,
                    "TailscaleIPs": ["100.64.0.12"],
                },
                "node-c": {
                    "Online": True,
                    "TailscaleIPs": ["192.0.2.10", "fd7a:115c:a1e0::2"],
                },
            }
        }
    )

    assert parse_tailnet_nodes(payload) == ["100.64.0.11"]


def test_tailnet_discovery_rejects_malformed_status() -> None:
    with pytest.raises(ChatError, match="Tailscale status"):
        parse_tailnet_nodes('{"Peer": []}')


def test_local_tailnet_address_uses_only_running_self_ipv4() -> None:
    payload = json.dumps(
        {
            "BackendState": "Running",
            "Self": {
                "TailscaleIPs": ["100.64.0.13", "fd7a:115c:a1e0::1"],
            },
        }
    )

    assert parse_local_tailnet_address(payload) == "100.64.0.13"
    assert (
        parse_local_tailnet_address(
            json.dumps({"BackendState": "Stopped", "Self": {"TailscaleIPs": ["100.1.2.3"]}})
        )
        is None
    )


def test_tailnet_broker_exposes_only_local_live_peers(tmp_path: Path) -> None:
    assert handle_broker_request(
        tmp_path,
        {"schema_version": 1, "operation": "peers"},
        "100.64.0.10",
    ) == {"schema_version": 1, "peers": []}


def test_broker_binds_localhost_and_installer_discovered_tailnet_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CROSS_AGENT_CHAT_TAILNET_ADDRESS", "100.64.0.13")

    assert broker_bindings() == [
        ("127.0.0.1", 47072),
        ("100.64.0.13", 47071),
    ]


def test_tailnet_listener_defers_only_until_address_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = mock.Mock()
    listener.bind.side_effect = OSError(errno.EADDRNOTAVAIL, "fixture")
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.socket.socket", lambda *_args: listener)

    assert bind_broker_listener(("100.64.0.13", 47071), allow_unavailable=True) is None
    listener.close.assert_called_once_with()


def test_local_listener_does_not_suppress_unavailable_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = mock.Mock()
    listener.bind.side_effect = OSError(errno.EADDRNOTAVAIL, "fixture")
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.socket.socket", lambda *_args: listener)

    with pytest.raises(OSError) as error:
        bind_broker_listener(("127.0.0.1", 47072))

    assert error.value.errno == errno.EADDRNOTAVAIL
    listener.close.assert_called_once_with()


def test_tailnet_listener_does_not_suppress_address_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = mock.Mock()
    listener.bind.side_effect = OSError(errno.EADDRINUSE, "fixture")
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.socket.socket", lambda *_args: listener)

    with pytest.raises(OSError) as error:
        bind_broker_listener(("100.64.0.13", 47071), allow_unavailable=True)

    assert error.value.errno == errno.EADDRINUSE
    listener.close.assert_called_once_with()


def test_broker_keeps_local_listener_and_discovers_tailnet_after_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_listener = mock.Mock()
    tailnet_listener = mock.Mock()
    bindings: list[tuple[tuple[str, int], bool]] = []

    def bind(binding: tuple[str, int], *, allow_unavailable: bool = False) -> object:
        bindings.append((binding, allow_unavailable))
        if allow_unavailable:
            return tailnet_listener
        return local_listener

    select_timeouts: list[float | None] = []

    def select_once_then_stop(
        _readers: object, _writers: object, _errors: object, timeout: float | None
    ) -> tuple[list[object], list[object], list[object]]:
        select_timeouts.append(timeout)
        if len(select_timeouts) == 2:
            raise RuntimeError("stop fixture")
        return [], [], []

    monkeypatch.setattr("cross_agent_chat.tailnet_broker.bind_broker_listener", bind)
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.select.select", select_once_then_stop)
    discovered = iter(
        (
            [("127.0.0.1", 47072)],
            [("127.0.0.1", 47072), ("100.64.0.13", 47071)],
        )
    )
    monkeypatch.setattr("cross_agent_chat.tailnet_broker.broker_bindings", lambda: next(discovered))

    with pytest.raises(RuntimeError, match="stop fixture"):
        broker_server(str(tmp_path))

    assert bindings == [
        (("127.0.0.1", 47072), False),
        (("100.64.0.13", 47071), True),
    ]
    assert select_timeouts == [5.0, None]
    local_listener.close.assert_called_once_with()
    tailnet_listener.close.assert_called_once_with()


def test_tailnet_broker_rejects_extra_request_fields(tmp_path: Path) -> None:
    with pytest.raises(ChatError, match="broker request"):
        handle_broker_request(
            tmp_path,
            {"schema_version": 1, "operation": "peers", "extra": True},
            "100.64.0.10",
        )


def test_tailnet_broker_serves_one_framed_request(tmp_path: Path) -> None:
    client, server = socket.socketpair()
    try:
        client.sendall(b'{"schema_version":1,"operation":"peers"}\n')
        client.shutdown(socket.SHUT_WR)

        serve_broker_connection(tmp_path, server, "100.64.0.10")

        response = json.loads(client.recv(4096))
        assert response == {"schema_version": 1, "peers": []}
    finally:
        client.close()
        server.close()


def test_tailnet_client_crosses_real_tcp_boundary(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            serve_broker_connection(tmp_path, connection, "127.0.0.1")

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        response = request_tailnet(
            "127.0.0.1",
            {"schema_version": 1, "operation": "health"},
            port=port,
        )
        assert response == {
            "schema_version": 1,
            "status": "READY",
            "pid": os.getpid(),
            "version": __version__,
            "module_path": str(Path(tailnet_broker_module.__file__).resolve()),
        }
    finally:
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive()


def test_silent_connection_does_not_block_another_broker_request(tmp_path: Path) -> None:
    silent_client, silent_server = socket.socketpair()
    active_client, active_server = socket.socketpair()
    admission = BrokerAdmission()
    with ThreadPoolExecutor(max_workers=2) as workers:
        dispatch_broker_connection(workers, tmp_path, silent_server, "100.64.0.11", admission)
        dispatch_broker_connection(workers, tmp_path, active_server, "100.64.0.10", admission)
        active_client.sendall(b'{"schema_version":1,"operation":"peers"}\n')
        active_client.settimeout(0.5)

        assert json.loads(active_client.recv(4096)) == {
            "schema_version": 1,
            "peers": [],
        }

        silent_client.close()
        active_client.close()


def test_vanished_ready_connection_does_not_block_broker_loop(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert (
                dispatch_ready_brokers(
                    workers,
                    tmp_path,
                    [listener],
                    BrokerAdmission(),
                )
                == 0
            )
    finally:
        listener.close()


def test_aborted_ready_connection_does_not_block_another_listener(tmp_path: Path) -> None:
    class AbortedListener(socket.socket):
        def accept(self) -> tuple[socket.socket, tuple[str, int]]:
            raise ConnectionAbortedError

    aborted = AbortedListener(socket.AF_INET, socket.SOCK_STREAM)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    client = socket.create_connection(listener.getsockname(), timeout=2)
    client.sendall(b'{"schema_version":1,"operation":"health"}\n')
    client.shutdown(socket.SHUT_WR)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert (
                dispatch_ready_brokers(
                    workers,
                    tmp_path,
                    [aborted, listener],
                    BrokerAdmission(),
                )
                == 1
            )
        client.settimeout(2)
        response = json.loads(client.recv(4096))
        assert response["status"] == "READY"
    finally:
        client.close()
        listener.close()
        aborted.close()


def test_broker_admission_rejects_third_connection_from_one_peer() -> None:
    admission = BrokerAdmission()

    assert admission.acquire("100.64.0.10")
    assert admission.acquire("100.64.0.10")
    assert not admission.acquire("100.64.0.10")

    admission.release("100.64.0.10")
    admission.release("100.64.0.10")


def test_remote_authorization_binds_source_target_and_payload(tmp_path: Path) -> None:
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="source",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    event_id = str(uuid4())
    target_key = "b" * 64
    target_generation = str(uuid4())
    IntentStore(tmp_path).begin_identity(
        source_key=session_key(source.provider, source.session_id),
        source_generation=source.generation,
        source_alias=source.alias,
        target_key=target_key,
        target_generation=target_generation,
        payload_digest="a" * 64,
        event_id=event_id,
    )

    assert (
        authorize_remote(
            tmp_path,
            event_id=event_id,
            source_alias=source.alias,
            source_generation=source.generation,
            target_key=target_key,
            target_generation=target_generation,
            payload_digest="a" * 64,
        )["status"]
        == "AUTHORIZED"
    )

    with pytest.raises(ChatError, match="not authorized"):
        authorize_remote(
            tmp_path,
            event_id=event_id,
            source_alias=source.alias,
            source_generation=source.generation,
            target_key=target_key,
            target_generation=target_generation,
            payload_digest="a" * 64,
        )

    assert IntentStore(tmp_path).intents()[0].status == "REMOTE_AUTHORIZED"


def test_remote_receive_requires_source_authorization_before_provider_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    envelope = remote_envelope(
        event_id=str(uuid4()),
        source_alias="codex@source:api:source-a1",
        source_generation=str(uuid4()),
        target_alias=target.alias,
        generation=target.generation,
        message="hello",
    )

    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda *_args, **_kwargs: {"schema_version": 1, "status": "DENIED"},
    )

    def provider_boundary(
        _path: Path, payload: dict[str, object], **_: object
    ) -> dict[str, object]:
        if payload.get("operation") == "health":
            return {
                "schema_version": 1,
                "status": "READY",
                "generation": target.generation,
                "alias": target.alias,
            }
        pytest.fail("provider delivery ran before source authorization")

    monkeypatch.setattr("cross_agent_chat.runtime.request_socket", provider_boundary)

    with pytest.raises(ChatError, match="not authorized"):
        receive_remote(tmp_path, envelope, "100.64.0.11")


def test_tailnet_client_keeps_write_side_open_for_serve_proxy() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve_like_tailscale() -> None:
        connection, _ = listener.accept()
        with connection:
            request = b""
            while not request.endswith(b"\n"):
                request += connection.recv(4096)
            readable, _, _ = select.select([connection], [], [], 0.05)
            if readable and connection.recv(1) == b"":
                return
            connection.sendall(b'{"schema_version":1,"peers":[]}\n')

    thread = threading.Thread(target=serve_like_tailscale)
    thread.start()
    try:
        assert request_tailnet(
            "127.0.0.1",
            {"schema_version": 1, "operation": "peers"},
            port=port,
        ) == {"schema_version": 1, "peers": []}
    finally:
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive()


def test_remote_targets_are_discovered_without_peer_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def nodes() -> list[str]:
        return ["100.64.0.11"]

    def request(address: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        assert address == "100.64.0.11"
        assert payload == {"schema_version": 1, "operation": "peers"}
        assert timeout == REMOTE_DISCOVERY_TIMEOUT_SECONDS
        return {
            "schema_version": 1,
            "peers": [
                {
                    "alias": "claude@studio:api:api-a1",
                    "provider": "claude",
                    "device": "studio",
                    "project": "api",
                    "status": "available",
                    "generation": "7d9ae03f-f86c-4c96-a40d-69f37f0a7189",
                    "session_key": "a" * 64,
                }
            ],
        }

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_nodes", nodes)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        request,
    )

    targets = remote_targets(tmp_path)

    assert len(targets) == 1
    assert targets[0].alias == "claude@studio:api:api-a1"
    assert targets[0].tailnet_address == "100.64.0.11"


def test_remote_send_uses_tailnet_broker_without_ssh_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "100.64.0.11"
    target_alias = "claude@studio:api:api-a1"

    def nodes() -> list[str]:
        return [address]

    def request(
        actual_address: str,
        payload: dict[str, object],
        *,
        port: int = 47071,
        timeout: float = 2.0,
    ) -> dict[str, object]:
        assert actual_address == address
        assert port == 47071
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [
                    {
                        "alias": target_alias,
                        "provider": "claude",
                        "device": "studio",
                        "project": "api",
                        "status": "available",
                        "generation": "7d9ae03f-f86c-4c96-a40d-69f37f0a7189",
                        "session_key": "a" * 64,
                    }
                ],
            }
        assert timeout > 90
        envelope = json.loads(str(payload["envelope"]))
        return {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target_alias,
            "provider": "claude",
        }

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_nodes", nodes)
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="imac",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    result = send(tmp_path, source, target_alias, "hello")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert "hello" not in (tmp_path / "intents.json").read_text()


def test_remote_pre_effect_rejection_is_safe_and_does_not_block_fresh_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "100.64.0.11"
    target_alias = "claude@studio:api:api-a1"
    generation = str(uuid4())

    def request(
        _address: str,
        payload: dict[str, object],
        **_: object,
    ) -> dict[str, object]:
        if payload.get("operation") == "peers":
            return {
                "schema_version": 1,
                "peers": [
                    {
                        "alias": target_alias,
                        "provider": "claude",
                        "device": "studio",
                        "project": "api",
                        "status": "available",
                        "generation": generation,
                        "session_key": "a" * 64,
                    }
                ],
            }
        envelope = json.loads(str(payload["envelope"]))
        return {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "PRE_EFFECT_REJECTED",
            "provider": "claude",
            "error": "peer-controlled wording must not escape",
        }

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_nodes", lambda: [address])
    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", request)
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="source",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    with pytest.raises(ChatError, match="remote target rejected") as caught:
        send(tmp_path, source, target_alias, "hello")

    assert "peer-controlled" not in str(caught.value)
    intent = IntentStore(tmp_path).intents()[0]
    assert intent.status == "PRE_EFFECT_REJECTED"
    assert IntentStore(tmp_path).begin_identity(
        source_key=session_key(source.provider, source.session_id),
        source_generation=source.generation,
        source_alias=source.alias,
        target_key="a" * 64,
        target_generation=generation,
        payload_digest="b" * 64,
    )


def test_remote_receiver_sanitizes_pre_effect_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="target",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(target)
    public_target = Target(
        alias=target.alias,
        provider=target.provider,
        device=target.device,
        project=target.project,
        generation=target.generation,
        session_key=session_key(target.provider, target.session_id),
        remote=False,
        session_id=target.session_id,
        cwd=target.cwd,
        pid=target.pid,
    )
    event_id = str(uuid4())
    source_generation = str(uuid4())
    envelope = remote_envelope(
        event_id=event_id,
        source_alias="codex@source:api:source-a1",
        source_generation=source_generation,
        target_alias=target.alias,
        generation=target.generation,
        message="hello",
    )
    monkeypatch.setattr("cross_agent_chat.runtime.local_targets", lambda _: [public_target])

    def authorize(_address: str, payload: dict[str, object], **_: object) -> dict[str, object]:
        return {key: value for key, value in payload.items() if key != "operation"} | {
            "status": "AUTHORIZED"
        }

    monkeypatch.setattr("cross_agent_chat.runtime.request_tailnet", authorize)
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_socket",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "event_id": event_id,
            "status": "PRE_EFFECT_REJECTED",
            "provider": "codex",
            "error": "internal path and peer-shaped text",
        },
    )

    response = receive_remote(tmp_path, envelope, "100.64.0.11")

    assert response == {
        "schema_version": 1,
        "event_id": event_id,
        "status": "PRE_EFFECT_REJECTED",
        "provider": "codex",
        "error": "remote destination rejected before provider effect",
    }


def test_wrapped_message_limit_rejects_before_intent_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    address = "100.64.0.10"
    target_alias = "claude@studio:api:api-a1"
    generation = str(uuid4())

    monkeypatch.setattr("cross_agent_chat.runtime.tailnet_nodes", lambda: [address])
    monkeypatch.setattr(
        "cross_agent_chat.runtime.request_tailnet",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "peers": [
                {
                    "alias": target_alias,
                    "provider": "claude",
                    "device": "studio",
                    "project": "api",
                    "status": "available",
                    "generation": generation,
                    "session_key": "a" * 64,
                }
            ],
        },
    )
    source = Route.create(
        provider="codex",
        session_id=str(uuid4()),
        device="source",
        cwd=str(tmp_path),
        pid=os.getpid(),
    )
    Registry(tmp_path).upsert(source)

    with pytest.raises(ChatError, match="16 KiB"):
        send(tmp_path, source, target_alias, "x" * (16 * 1024))

    assert not (tmp_path / "intents.json").exists()


def test_cli_has_private_tailnet_broker_entrypoint(tmp_path: Path) -> None:
    arguments = parser().parse_args(["_broker", "--state-root", str(tmp_path)])

    assert arguments.command == "_broker"
    assert arguments.state_root == str(tmp_path)
