"""One typed retry for the sender's read-only Claude identity inventory.

``claude agents --json`` runs inside ``canonical_source_alias`` before any
intent row or transport effect exists, so a typed subprocess timeout on that
read-only inventory may be retried exactly once inside the original operation
deadline. Every other failure -- exit status, spawn error, malformed roster,
a missing or ambiguous exact session -- stays decided, an exhausted budget
fails closed, and a superseded source generation aborts before effect.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from cross_agent_chat import claude_runtime, runtime
from cross_agent_chat.claude_runtime import (
    AGENTS_TIMEOUT_SECONDS,
    ClaudeAgentsPreflightTimeout,
)
from cross_agent_chat.core import (
    ChatError,
    IntentStore,
    Registry,
    Route,
    UnknownDeliveryError,
    session_key,
)
from cross_agent_chat.recipient import remote_token
from cross_agent_chat.runtime import (
    OPERATION_TIMEOUT_SECONDS,
    Target,
    canonical_source_alias,
)
from cross_agent_chat.tailnet import TailnetIdentity


def _claude_route(tmp_path: Path, project: str = "project") -> Route:
    cwd = tmp_path / project
    cwd.mkdir(exist_ok=True)
    return Route.create(
        provider="claude",
        session_id=str(uuid4()),
        device="studio",
        cwd=str(cwd),
        pid=os.getpid(),
    )


def _sender_agent(source: Route) -> dict[str, str]:
    return {
        "session_id": source.session_id,
        "name": "Sender",
        "kind": "background",
        "cwd": source.cwd,
    }


def _remote_target() -> Target:
    return Target(
        alias="claude@m2:parser:peer",
        provider="claude",
        device="m2",
        project="parser",
        generation=str(uuid4()),
        session_key="a" * 64,
        remote=True,
        tailnet_address="100.64.0.10",
        tailnet_node_id="nOwner",
    )


def _bind_remote_peer(
    monkeypatch: pytest.MonkeyPatch, target: Target
) -> tuple[list[dict[str, object]], list[float]]:
    """Attest one remote claimant and record every delivery attempt."""
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nOwner": "100.64.0.10"}),
    )
    monkeypatch.setattr(runtime, "_remote_node_targets", lambda *_a, **_k: ([target], True))
    deliveries: list[dict[str, object]] = []
    timeouts: list[float] = []

    def receive(_address: str, payload: dict[str, object], *, timeout: float) -> dict[str, object]:
        timeouts.append(timeout)
        envelope = json.loads(str(payload["envelope"]))
        response = {
            "schema_version": 1,
            "event_id": envelope["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": target.provider,
        }
        deliveries.append(response)
        return response

    monkeypatch.setattr(runtime, "request_tailnet", receive)
    return deliveries, timeouts


def test_remote_send_retries_one_inventory_timeout_and_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient inventory timeout is retried once, then the send completes."""
    root = tmp_path / "state"
    source = _claude_route(tmp_path)
    Registry(root).upsert(source)
    target = _remote_target()
    deliveries, _timeouts = _bind_remote_peer(monkeypatch, target)
    agent = _sender_agent(source)
    attempts: list[tuple[str, float]] = []

    def agents(session_id: str, *, timeout: float) -> list[dict[str, str]]:
        attempts.append((session_id, timeout))
        if len(attempts) == 1:
            raise ClaudeAgentsPreflightTimeout("Claude agents preflight timed out")
        return [agent]

    monkeypatch.setattr(claude_runtime, "claude_agents", agents)

    token = remote_token("nOwner", target.session_key, target.generation)
    result = runtime.send(root, source, token, "synthetic reply")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert attempts == [
        (source.session_id, AGENTS_TIMEOUT_SECONDS),
        (source.session_id, AGENTS_TIMEOUT_SECONDS),
    ]
    assert len(deliveries) == 1
    intents = IntentStore(root).intents()
    assert [intent.status for intent in intents] == ["TRANSPORT_ACCEPTED"]


def test_repeated_inventory_timeout_never_reaches_an_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry budget is one attempt; a second timeout is decided, not looped."""
    root = tmp_path / "state"
    source = _claude_route(tmp_path)
    Registry(root).upsert(source)
    target = _remote_target()
    deliveries, _timeouts = _bind_remote_peer(monkeypatch, target)
    attempts: list[float] = []

    def agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        attempts.append(timeout)
        raise ClaudeAgentsPreflightTimeout("Claude agents preflight timed out")

    monkeypatch.setattr(claude_runtime, "claude_agents", agents)

    token = remote_token("nOwner", target.session_key, target.generation)
    with pytest.raises(ClaudeAgentsPreflightTimeout, match="timed out"):
        runtime.send(root, source, token, "synthetic reply")

    assert len(attempts) == 2
    assert deliveries == []
    assert IntentStore(root).intents() == []


def test_retry_attempts_share_the_original_operation_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each inventory attempt is capped by what the operation has left."""
    now = [1_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    root = tmp_path / "state"
    source = _claude_route(tmp_path)
    Registry(root).upsert(source)
    target = _remote_target()
    deliveries, tailnet_timeouts = _bind_remote_peer(monkeypatch, target)
    agent = _sender_agent(source)
    attempts: list[float] = []

    def agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        attempts.append(timeout)
        if len(attempts) == 1:
            now[0] += OPERATION_TIMEOUT_SECONDS - 9.0
            raise ClaudeAgentsPreflightTimeout("Claude agents preflight timed out")
        now[0] += 4.0
        return [agent]

    monkeypatch.setattr(claude_runtime, "claude_agents", agents)

    token = remote_token("nOwner", target.session_key, target.generation)
    result = runtime.send(root, source, token, "synthetic reply")

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert attempts == [AGENTS_TIMEOUT_SECONDS, 9.0]
    assert tailnet_timeouts == [5.0]
    assert len(deliveries) == 1


def test_inventory_below_the_retry_floor_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget too small for a real attempt keeps the decided timeout."""
    now = [1_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    root = tmp_path / "state"
    source = _claude_route(tmp_path)
    Registry(root).upsert(source)
    target = _remote_target()
    deliveries, _timeouts = _bind_remote_peer(monkeypatch, target)
    attempts: list[float] = []

    def agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        attempts.append(timeout)
        now[0] += OPERATION_TIMEOUT_SECONDS - 0.5
        raise ClaudeAgentsPreflightTimeout("Claude agents preflight timed out")

    monkeypatch.setattr(claude_runtime, "claude_agents", agents)

    token = remote_token("nOwner", target.session_key, target.generation)
    with pytest.raises(ClaudeAgentsPreflightTimeout, match="timed out"):
        runtime.send(root, source, token, "synthetic reply")

    assert attempts == [AGENTS_TIMEOUT_SECONDS]
    assert deliveries == []
    assert IntentStore(root).intents() == []


def test_exhausted_deadline_never_spawns_the_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spent operation deadline refuses before the first provider spawn."""
    now = [1_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    root = tmp_path / "state"
    source = _claude_route(tmp_path)
    Registry(root).upsert(source)
    target = _remote_target()
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nOwner": "100.64.0.10"}),
    )

    def node_targets(*_args: object, **_kwargs: object) -> tuple[list[Target], bool]:
        now[0] += OPERATION_TIMEOUT_SECONDS + 1.0
        return [target], True

    monkeypatch.setattr(runtime, "_remote_node_targets", node_targets)
    monkeypatch.setattr(
        runtime,
        "request_tailnet",
        lambda *_a, **_k: pytest.fail("delivery ran after the operation deadline"),
    )
    monkeypatch.setattr(
        claude_runtime,
        "claude_agents",
        lambda *_a, **_k: pytest.fail("inventory ran after the operation deadline"),
    )

    token = remote_token("nOwner", target.session_key, target.generation)
    with pytest.raises(ChatError, match="timed out") as error:
        runtime.send(root, source, token, "synthetic reply")

    assert not isinstance(error.value, UnknownDeliveryError)
    assert IntentStore(root).intents() == []


@pytest.mark.parametrize(
    ("failure", "match"),
    [
        ("exit", r"preflight failed \(exit 2\)"),
        ("malformed", "response is invalid"),
        ("absent", "not one exact live supported session"),
        ("ambiguous", "not one exact live supported session"),
    ],
)
def test_decided_inventory_failures_are_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, match: str
) -> None:
    """Only the typed timeout retries; every other failure surfaces decided."""
    root = tmp_path / "state"
    source = _claude_route(tmp_path)
    Registry(root).upsert(source)
    target = _remote_target()
    deliveries, _timeouts = _bind_remote_peer(monkeypatch, target)
    agent = _sender_agent(source)
    attempts: list[float] = []

    def agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        attempts.append(timeout)
        if failure == "exit":
            raise ChatError("Claude agents preflight failed (exit 2)")
        if failure == "malformed":
            raise ChatError("Claude agents response is invalid")
        if failure == "ambiguous":
            return [agent, agent]
        return []

    monkeypatch.setattr(claude_runtime, "claude_agents", agents)

    token = remote_token("nOwner", target.session_key, target.generation)
    with pytest.raises(ChatError, match=match):
        runtime.send(root, source, token, "synthetic reply")

    assert len(attempts) == 1
    assert deliveries == []
    assert IntentStore(root).intents() == []


@pytest.mark.parametrize("timeout_first", [False, True])
def test_source_generation_change_during_inventory_aborts_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout_first: bool
) -> None:
    """The exact source generation is revalidated after a slow inventory."""
    root = tmp_path / "state"
    source = _claude_route(tmp_path)
    Registry(root).upsert(source)
    target = _remote_target()
    deliveries, _timeouts = _bind_remote_peer(monkeypatch, target)
    agent = _sender_agent(source)
    attempts: list[float] = []

    def agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        attempts.append(timeout)
        if timeout_first and len(attempts) == 1:
            raise ClaudeAgentsPreflightTimeout("Claude agents preflight timed out")
        replacement = Route.create(
            provider="claude",
            session_id=source.session_id,
            device=source.device,
            cwd=source.cwd,
            pid=source.pid,
        )
        Registry(root).upsert(replacement)
        return [agent]

    monkeypatch.setattr(claude_runtime, "claude_agents", agents)

    token = remote_token("nOwner", target.session_key, target.generation)
    with pytest.raises(ChatError, match="sender route changed"):
        runtime.send(root, source, token, "synthetic reply")

    assert len(attempts) == (2 if timeout_first else 1)
    assert deliveries == []
    assert IntentStore(root).intents() == []


def test_unknown_delivery_is_recorded_once_and_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A possible-effect failure stays UNKNOWN; nothing in the send retries."""
    root = tmp_path / "state"
    source = _claude_route(tmp_path)
    Registry(root).upsert(source)
    target = _remote_target()
    monkeypatch.setattr(
        runtime,
        "tailnet_identity",
        lambda: TailnetIdentity(self_node_id="nSelf", peers={"nOwner": "100.64.0.10"}),
    )
    monkeypatch.setattr(runtime, "_remote_node_targets", lambda *_a, **_k: ([target], True))
    agent = _sender_agent(source)
    monkeypatch.setattr(claude_runtime, "claude_agents", lambda *_a, **_k: [agent])
    deliveries = 0

    def receive(_address: str, _payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        nonlocal deliveries
        deliveries += 1
        raise UnknownDeliveryError("delivery state is unknown")

    monkeypatch.setattr(runtime, "request_tailnet", receive)

    token = remote_token("nOwner", target.session_key, target.generation)
    with pytest.raises(UnknownDeliveryError, match="do not retry automatically"):
        runtime.send(root, source, token, "synthetic reply")

    assert deliveries == 1
    intents = IntentStore(root).intents()
    assert [intent.status for intent in intents] == ["UNKNOWN_DELIVERY"]


def test_source_alias_without_a_deadline_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Callers without an operation budget keep the single-attempt behavior."""
    root = tmp_path / "state"
    source = _claude_route(tmp_path)
    Registry(root).upsert(source)
    attempts = 0

    def agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        nonlocal attempts
        attempts += 1
        raise ClaudeAgentsPreflightTimeout("Claude agents preflight timed out")

    monkeypatch.setattr(claude_runtime, "claude_agents", agents)

    with pytest.raises(ClaudeAgentsPreflightTimeout):
        canonical_source_alias(root, source)

    assert attempts == 1


def test_local_send_retries_one_inventory_timeout_and_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local sender path threads the same deadline into the same retry."""
    root = tmp_path / "state"
    source = _claude_route(tmp_path, project="source")
    target_route = _claude_route(tmp_path, project="target")
    Registry(root).upsert(source)
    Registry(root).upsert(target_route)
    target = Target(
        alias=target_route.alias,
        provider="claude",
        device=target_route.device,
        project=target_route.project,
        generation=target_route.generation,
        session_key=session_key("claude", target_route.session_id),
        remote=False,
        session_id=target_route.session_id,
        cwd=target_route.cwd,
        pid=target_route.pid,
    )
    agent = _sender_agent(source)
    attempts: list[float] = []

    def agents(_session_id: str, *, timeout: float) -> list[dict[str, str]]:
        attempts.append(timeout)
        if len(attempts) == 1:
            raise ClaudeAgentsPreflightTimeout("Claude agents preflight timed out")
        return [agent]

    monkeypatch.setattr(claude_runtime, "claude_agents", agents)
    socket_calls: list[dict[str, object]] = []

    def accept(_path: Path, payload: dict[str, object], **_kwargs: object) -> dict[str, object]:
        socket_calls.append(payload)
        return {
            "schema_version": 1,
            "event_id": payload["event_id"],
            "status": "TRANSPORT_ACCEPTED",
            "to": target.alias,
            "provider": "claude",
        }

    monkeypatch.setattr(runtime, "request_socket", accept)

    result = runtime._send_local_target(
        root, source, target, "synthetic reply", deadline=time.monotonic() + 60.0
    )

    assert result["status"] == "TRANSPORT_ACCEPTED"
    assert attempts == [AGENTS_TIMEOUT_SECONDS, AGENTS_TIMEOUT_SECONDS]
    assert len(socket_calls) == 1
    intents = IntentStore(root).intents()
    assert [intent.status for intent in intents] == ["TRANSPORT_ACCEPTED"]


def test_inventory_timeout_is_typed_and_carries_no_provider_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subprocess timeout surfaces as the typed, decided preflight error."""
    monkeypatch.setattr(claude_runtime, "claude_binary", lambda: Path("/usr/bin/false"))

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=AGENTS_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(ClaudeAgentsPreflightTimeout, match="timed out") as error:
        claude_runtime.claude_agents()
    assert isinstance(error.value, ChatError)
    assert not isinstance(error.value, UnknownDeliveryError)


def test_inventory_nonzero_exit_reports_only_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonzero exit names its status and never leaks captured stderr."""
    monkeypatch.setattr(claude_runtime, "claude_binary", lambda: Path("/usr/bin/false"))

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["claude"], 2, "", "provider stderr must stay private")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(ChatError, match=r"preflight failed \(exit 2\)") as error:
        claude_runtime.claude_agents()
    assert "provider stderr" not in str(error.value)
    assert not isinstance(error.value, ClaudeAgentsPreflightTimeout)


def test_inventory_spawn_failure_reports_only_the_errno_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn failure names its errno category, not the OS error text."""
    monkeypatch.setattr(claude_runtime, "claude_binary", lambda: Path("/usr/bin/false"))

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise PermissionError(errno.EACCES, "private executable path detail")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(ChatError, match=r"\(spawn EACCES\)") as error:
        claude_runtime.claude_agents()
    assert "private executable path detail" not in str(error.value)
    assert not isinstance(error.value, ClaudeAgentsPreflightTimeout)
