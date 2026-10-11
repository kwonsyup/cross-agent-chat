"""Contained contracts for the ACP-hosted Devin mediator.

The test plays both the ACP host and the native engine over real pipes, so
each assertion observes the exact frames the mediator writes on each side.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, cast

import pytest

from cross_agent_chat import core, devin, devin_acp
from cross_agent_chat.core import ChatError, Route
from cross_agent_chat.devin_acp import Mediator

EVENT = "11111111-2222-4333-8444-555555555555"
OTHER_EVENT = "66666666-7777-4888-9999-000000000000"
Frame = dict[str, object]


@dataclass
class FakeInbox:
    pending: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    fail: bool = False

    def take(self, session_id: str, still_open: Callable[[], bool]) -> tuple[str, str] | None:
        self.asked.append(session_id)
        if not still_open():
            return None
        if self.fail:
            raise ChatError("Devin courier response is invalid")
        items = self.pending.get(session_id) or []
        return items.pop(0) if items else None


class Side:
    """One end of the mediator: frames it receives arrive on a queue."""

    def __init__(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        self._writer = writer
        self._frames: queue.Queue[Frame] = queue.Queue()
        threading.Thread(target=self._pump, args=(reader,), daemon=True).start()

    def _pump(self, reader: IO[bytes]) -> None:
        for line in reader:
            self._frames.put(cast(Frame, json.loads(line)))

    def send(self, frame: Frame) -> None:
        self._writer.write((json.dumps(frame) + "\n").encode())
        self._writer.flush()

    def recv(self, timeout: float = 5.0) -> Frame:
        return self._frames.get(timeout=timeout)

    def quiet(self, timeout: float = 0.3) -> bool:
        try:
            frame = self._frames.get(timeout=timeout)
        except queue.Empty:
            return True
        raise AssertionError(f"unexpected frame: {frame}")


@dataclass
class Wired:
    mediator: Mediator
    host: Side
    engine: Side
    inbox: FakeInbox
    host_writer: IO[bytes]
    engine_writer: IO[bytes]


@pytest.fixture
def wired() -> Iterator[Wired]:
    host_to_med_r, host_to_med_w = os.pipe()
    med_to_host_r, med_to_host_w = os.pipe()
    med_to_engine_r, med_to_engine_w = os.pipe()
    engine_to_med_r, engine_to_med_w = os.pipe()
    host_writer = os.fdopen(host_to_med_w, "wb")
    engine_writer = os.fdopen(engine_to_med_w, "wb")
    inbox = FakeInbox()
    mediator = Mediator(
        host_in=os.fdopen(host_to_med_r, "rb"),
        host_out=os.fdopen(med_to_host_w, "wb"),
        engine_in=os.fdopen(med_to_engine_w, "wb"),
        engine_out=os.fdopen(engine_to_med_r, "rb"),
        inbox=inbox,
        diagnostic=lambda _text: None,
    )
    mediator.start(deliver=False)
    host = Side(os.fdopen(med_to_host_r, "rb"), host_writer)
    engine = Side(os.fdopen(med_to_engine_r, "rb"), engine_writer)
    yield Wired(mediator, host, engine, inbox, host_writer, engine_writer)
    host_writer.close()
    engine_writer.close()


def _new_session(w: Wired, session_id: str, host_id: object = 1) -> None:
    w.host.send({"jsonrpc": "2.0", "id": host_id, "method": "session/new", "params": {}})
    request = w.engine.recv()
    assert request["method"] == "session/new"
    w.engine.send({"jsonrpc": "2.0", "id": request["id"], "result": {"sessionId": session_id}})
    assert w.host.recv() == {"jsonrpc": "2.0", "id": host_id, "result": {"sessionId": session_id}}


def _prompt(session_id: str, host_id: object, text: str = "hi") -> Frame:
    return {
        "jsonrpc": "2.0",
        "id": host_id,
        "method": "session/prompt",
        "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
    }


def _end(request: Frame, reason: str = "end_turn") -> Frame:
    return {"jsonrpc": "2.0", "id": request["id"], "result": {"stopReason": reason}}


def _human_turn(w: Wired, session_id: str, host_id: object) -> None:
    w.host.send(_prompt(session_id, host_id))
    request = w.engine.recv()
    w.engine.send(_end(request))
    assert w.host.recv() == {"jsonrpc": "2.0", "id": host_id, "result": {"stopReason": "end_turn"}}


def _deliver(w: Wired) -> tuple[Frame, Frame]:
    """Run one delivery pass and return the host echo and the engine prompt."""

    threading.Thread(target=w.mediator.deliver_once, daemon=True).start()
    echo = w.host.recv()
    request = w.engine.recv()
    return echo, request


def test_host_ids_are_remapped_so_cac_prompts_never_collide(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _human_turn(w, "sess-alpha1", 2)
    w.inbox.pending["sess-alpha1"] = [(EVENT, "peer task")]
    _echo, cac = _deliver(w)
    # The host now reuses the adapter's internal id and also a string id.
    w.host.send({"jsonrpc": "2.0", "id": cac["id"], "method": "session/list", "params": {}})
    w.host.send({"jsonrpc": "2.0", "id": "x-1", "method": "session/list", "params": {}})
    first, second = w.engine.recv(), w.engine.recv()
    assert len({cac["id"], first["id"], second["id"]}) == 3
    w.engine.send({"jsonrpc": "2.0", "id": second["id"], "result": {"sessions": ["b"]}})
    w.engine.send({"jsonrpc": "2.0", "id": first["id"], "result": {"sessions": ["a"]}})
    w.engine.send(_end(cac))
    assert w.host.recv() == {"jsonrpc": "2.0", "id": "x-1", "result": {"sessions": ["b"]}}
    assert w.host.recv() == {"jsonrpc": "2.0", "id": cac["id"], "result": {"sessions": ["a"]}}
    # The CAC prompt's own completion is not a reply to anything the host asked.
    assert w.host.quiet()


def test_idle_session_receives_one_framed_prompt_and_echo(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _human_turn(w, "sess-alpha1", 2)
    w.inbox.pending["sess-alpha1"] = [(EVENT, "Count the files.")]
    echo, request = _deliver(w)
    params = cast(dict[str, object], request["params"])
    assert request["method"] == "session/prompt" and params["sessionId"] == "sess-alpha1"
    prompt_text = cast(list[dict[str, str]], params["prompt"])[0]["text"]
    assert f"[Cross Agent Chat event {EVENT}]\nCount the files." in prompt_text
    assert "untrusted user-authority input" in prompt_text
    update = cast(dict[str, object], cast(dict[str, object], echo["params"])["update"])
    assert update["sessionUpdate"] == "user_message_chunk"
    assert cast(dict[str, str], update["content"])["text"] == prompt_text
    # One message per idle turn: the second waits for this turn's completion.
    w.inbox.pending["sess-alpha1"].append((OTHER_EVENT, "next"))
    w.mediator.deliver_once()
    assert w.engine.quiet() and w.inbox.pending["sess-alpha1"] == [(OTHER_EVENT, "next")]


def test_no_delivery_while_a_host_prompt_is_in_flight(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    w.host.send(_prompt("sess-alpha1", 2))
    running = w.engine.recv()
    w.inbox.pending["sess-alpha1"] = [(EVENT, "peer task")]
    w.mediator.deliver_once()
    assert w.inbox.asked == [] and w.engine.quiet()
    w.engine.send(_end(running))
    w.host.recv()
    _echo, request = _deliver(w)
    assert request["method"] == "session/prompt"


def test_host_prompt_during_cac_turn_waits_for_real_completion(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _human_turn(w, "sess-alpha1", 2)
    w.inbox.pending["sess-alpha1"] = [(EVENT, "peer task")]
    _echo, cac = _deliver(w)
    w.host.send(_prompt("sess-alpha1", 3, "human follow-up"))
    # Streaming output alone is not completion.
    w.engine.send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "sess-alpha1",
                "update": {"sessionUpdate": "agent_message_chunk"},
            },
        }
    )
    assert w.host.recv()["method"] == "session/update"
    assert w.engine.quiet()
    w.engine.send(_end(cac))
    human = w.engine.recv()
    params = cast(dict[str, object], human["params"])
    assert cast(list[dict[str, str]], params["prompt"])[0]["text"] == "human follow-up"
    w.engine.send(_end(human))
    assert w.host.recv() == {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "end_turn"}}


def test_permission_during_cac_turn_reaches_host_and_returns_to_engine(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _human_turn(w, "sess-alpha1", 2)
    w.inbox.pending["sess-alpha1"] = [(EVENT, "fetch a page")]
    _echo, cac = _deliver(w)
    ask: Frame = {
        "jsonrpc": "2.0",
        "id": cac["id"],  # the engine's own namespace may reuse any value
        "method": "session/request_permission",
        "params": {"sessionId": "sess-alpha1", "options": [{"optionId": "allow_once"}]},
    }
    w.engine.send(ask)
    assert w.host.recv() == ask
    reply: Frame = {
        "jsonrpc": "2.0",
        "id": cac["id"],
        "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
    }
    w.host.send(reply)
    assert w.engine.recv() == reply
    w.engine.send(_end(cac))
    assert w.host.quiet()


def test_cancel_during_cac_turn_stops_it_and_answers_queued_human_prompt(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _human_turn(w, "sess-alpha1", 2)
    w.inbox.pending["sess-alpha1"] = [(EVENT, "long task")]
    _echo, cac = _deliver(w)
    w.host.send(_prompt("sess-alpha1", 3))
    cancel: Frame = {
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": "sess-alpha1"},
    }
    w.host.send(cancel)
    assert w.host.recv() == {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}}
    assert w.engine.recv() == cancel
    w.engine.send(_end(cac, "cancelled"))
    # The cancelled human prompt is never replayed to the engine afterwards.
    assert w.engine.quiet() and w.host.quiet()


def test_inbox_failure_and_closed_sessions_submit_nothing(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _new_session(w, "sess-spare1", host_id=5)
    w.host.send(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "session/delete",
            "params": {"sessionId": "sess-spare1"},
        }
    )
    delete = w.engine.recv()
    w.engine.send({"jsonrpc": "2.0", "id": delete["id"], "result": {}})
    w.host.recv()
    w.inbox.fail = True
    w.mediator.deliver_once()
    assert w.inbox.asked == ["sess-alpha1"] and w.engine.quiet()
    w.inbox.fail = False
    _human_turn(w, "sess-alpha1", 7)


def test_engine_exit_stops_the_mediator(wired: Wired) -> None:
    wired.engine_writer.close()
    assert wired.mediator.stopped().wait(5)


def test_courier_inbox_takes_only_the_exact_engine_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cross_agent_chat import runtime

    def route(session_id: str, pid: int) -> Route:
        return cast(
            Route,
            type(
                "R",
                (),
                {"provider": "devin", "session_id": session_id, "pid": pid, "generation": "g"},
            ),
        )

    routes = [route("sess-alpha1", 41), route("sess-alpha1", 42), route("sess-other", 42)]
    monkeypatch.setattr(core.Registry, "routes", lambda _self: routes)
    monkeypatch.setattr(runtime, "_route_current", lambda _root, _route: True)
    monkeypatch.setattr(runtime, "socket_path", lambda _root, r: r)
    monkeypatch.setattr(
        runtime, "_devin_messages", lambda _root, r: [{"event_id": EVENT, "message": f"m{r.pid}"}]
    )
    acks: list[tuple[object, object]] = []
    reply: dict[str, object] = {"schema_version": 1, "status": "ACKNOWLEDGED", "event_ids": [EVENT]}

    def request(target: object, payload: dict[str, object]) -> dict[str, object]:
        acks.append((target, payload["event_ids"]))
        return reply

    monkeypatch.setattr(runtime, "request_socket", request)
    inbox = devin_acp.CourierInbox(tmp_path, engine_pid=42)
    # A session the host closed after the peek consumes nothing.
    assert inbox.take("sess-alpha1", lambda: False) is None and acks == []
    assert inbox.take("sess-alpha1", lambda: True) == (EVENT, "m42")
    assert acks == [(routes[1], [EVENT])]
    # A losing or malformed acknowledgement never yields a message to submit.
    reply = {"schema_version": 1, "status": "ACKNOWLEDGED", "event_ids": []}
    with pytest.raises(ChatError):
        inbox.take("sess-alpha1", lambda: True)
    assert devin_acp.CourierInbox(tmp_path, engine_pid=43).take("sess-alpha1", lambda: True) is None


def test_host_attestation_names_only_the_live_owned_engine(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:

        def route(pid: int, session_id: str = "sess-alpha1") -> Route:
            return cast(
                Route, type("R", (), {"provider": "devin", "pid": pid, "session_id": session_id})
            )

        owned, foreign = route(child.pid), route(os.getppid())
        assert not devin.acp_host_observed(root, owned)
        path = devin.write_acp_host(root, adapter_pid=os.getpid(), engine_pid=child.pid)
        # The engine is owned, but this session is not one the adapter relays.
        assert not devin.acp_host_observed(root, owned)
        path = devin.write_acp_host(
            root, adapter_pid=os.getpid(), engine_pid=child.pid, sessions=frozenset({"sess-alpha1"})
        )
        assert devin.acp_host_observed(root, owned)
        assert not devin.acp_host_observed(root, route(child.pid, "sess-other1"))
        assert not devin.acp_host_observed(root, foreign)
        record = json.loads(path.read_text())
        assert set(record) == {
            "adapter_pid",
            "adapter_start",
            "engine_pid",
            "engine_start",
            "sessions",
        }
        path.write_text(json.dumps({**record, "adapter_pid": os.getppid()}))
        assert not devin.acp_host_observed(root, owned)
        path.write_text(json.dumps(record))
        path.chmod(0o644)
        assert not devin.acp_host_observed(root, owned)
        with pytest.raises(ChatError):
            devin.write_acp_host(root, adapter_pid=os.getppid(), engine_pid=child.pid)
        devin.prune_acp_hosts(root)
        assert path.exists()
    finally:
        child.kill()
        child.wait()
    assert not devin.acp_host_observed(root, owned)
    devin.prune_acp_hosts(root)
    assert not path.exists()


def test_registry_add_and_remove_touch_only_the_cac_entry(tmp_path: Path) -> None:
    registry = tmp_path / "acp" / "registry.json"
    command = tmp_path / "bin" / "cross-agent-chat"
    devin_acp.register(registry, command)
    created = json.loads(registry.read_text())
    assert [agent["id"] for agent in created["agents"]] == [devin_acp.REGISTRY_AGENT_ID]
    devin_acp.unregister(registry)
    assert not registry.exists()

    foreign = {"id": "opencode", "name": "OpenCode", "distribution": {"binary": {}}}
    registry.write_text(json.dumps({"version": "1.0.0", "agents": [foreign], "extensions": ["x"]}))
    devin_acp.register(registry, command)
    devin_acp.register(registry, command)
    document = json.loads(registry.read_text())
    assert document["agents"][0] == foreign and len(document["agents"]) == 2
    entry = document["agents"][1]
    target = entry["distribution"]["binary"]["darwin-aarch64"]
    assert target == {"archive": "", "cmd": str(command), "args": ["devin-acp"]}
    assert devin_acp.registry_status(registry)["registered"] is True
    devin_acp.unregister(registry)
    assert json.loads(registry.read_text()) == {
        "version": "1.0.0",
        "agents": [foreign],
        "extensions": ["x"],
    }
    registry.write_text("{not json")
    with pytest.raises(ChatError):
        devin_acp.register(registry, command)
    assert registry.read_text() == "{not json"


def test_registry_cli_dispatches_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cross_agent_chat import cli

    registry = tmp_path / "registry.json"
    entrypoint = tmp_path / "cross-agent-chat"
    assert (
        cli.main(
            [
                "devin-acp-registry",
                "add",
                "--registry",
                str(registry),
                "--entrypoint",
                str(entrypoint),
            ]
        )
        == 0
    )
    assert cli.main(["devin-acp-registry", "status", "--registry", str(registry)]) == 0
    assert '"registered": true' in capsys.readouterr().out
    assert cli.main(["devin-acp-registry", "remove", "--registry", str(registry)]) == 0
    assert not registry.exists()


def test_cancel_during_take_follows_the_peer_prompt_instead_of_overtaking_it(
    wired: Wired,
) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _human_turn(w, "sess-alpha1", 2)
    entered, release = threading.Event(), threading.Event()
    original = w.inbox.take

    def slow_take(session_id: str, still_open: Callable[[], bool]) -> tuple[str, str] | None:
        entered.set()
        release.wait(5)
        return original(session_id, still_open)

    w.inbox.pending["sess-alpha1"] = [(EVENT, "peer task")]
    w.inbox.take = slow_take  # type: ignore[method-assign]
    threading.Thread(target=w.mediator.deliver_once, daemon=True).start()
    assert entered.wait(5)
    cancel: Frame = {
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": "sess-alpha1"},
    }
    w.host.send(cancel)
    # Nothing is running yet, so nothing reaches the engine before the prompt.
    assert w.engine.quiet()
    release.set()
    w.host.recv()  # echo of the peer message
    prompt = w.engine.recv()
    assert prompt["method"] == "session/prompt"
    assert w.engine.recv() == cancel
    w.engine.send(_end(prompt, "cancelled"))
    assert w.host.quiet()


def test_cancel_after_promotion_reaches_the_engine_after_that_prompt(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _human_turn(w, "sess-alpha1", 2)
    w.inbox.pending["sess-alpha1"] = [(EVENT, "peer task")]
    _echo, cac = _deliver(w)
    w.host.send(_prompt("sess-alpha1", 3))
    assert w.engine.quiet()
    w.engine.send(_end(cac))
    promoted = w.engine.recv()
    cancel: Frame = {
        "jsonrpc": "2.0",
        "method": "session/cancel",
        "params": {"sessionId": "sess-alpha1"},
    }
    w.host.send(cancel)
    # Whatever the interleaving, the engine never sees the cancel before the prompt.
    assert w.engine.recv() == cancel
    w.engine.send(_end(promoted, "cancelled"))
    assert w.host.recv() == {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}}


def test_session_close_answers_every_prompt_still_waiting(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _human_turn(w, "sess-alpha1", 2)
    w.inbox.pending["sess-alpha1"] = [(EVENT, "peer task")]
    _echo, cac = _deliver(w)
    for host_id in (3, "q-4"):
        w.host.send(_prompt("sess-alpha1", host_id))
    w.host.send(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "session/close",
            "params": {"sessionId": "sess-alpha1"},
        }
    )
    close = w.engine.recv()
    assert close["method"] == "session/close"
    w.engine.send({"jsonrpc": "2.0", "id": close["id"], "result": {}})
    answers = [w.host.recv() for _ in range(3)]
    assert answers[0] == {"jsonrpc": "2.0", "id": 5, "result": {}}
    assert answers[1:] == [
        {"jsonrpc": "2.0", "id": 3, "result": {"stopReason": "cancelled"}},
        {"jsonrpc": "2.0", "id": "q-4", "result": {"stopReason": "cancelled"}},
    ]
    w.engine.send(_end(cac))
    assert w.engine.quiet() and w.host.quiet()


def test_waiting_prompts_are_bounded_with_an_explicit_refusal(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    _human_turn(w, "sess-alpha1", 2)
    w.inbox.pending["sess-alpha1"] = [(EVENT, "peer task")]
    _deliver(w)
    for host_id in range(10, 10 + devin_acp.MAX_QUEUED_PROMPTS):
        w.host.send(_prompt("sess-alpha1", host_id))
    w.host.send(_prompt("sess-alpha1", 99))
    refusal = w.host.recv()
    assert refusal["id"] == 99 and "error" in refusal
    assert w.engine.quiet()


def test_a_closing_session_refuses_new_prompts(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    w.host.send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/close",
            "params": {"sessionId": "sess-alpha1"},
        }
    )
    close = w.engine.recv()
    w.host.send(_prompt("sess-alpha1", 4))
    refusal = w.host.recv()
    assert refusal["id"] == 4 and "error" in refusal
    assert w.engine.quiet()
    w.engine.send({"jsonrpc": "2.0", "id": close["id"], "result": {}})
    assert w.host.recv() == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_close_drains_accepted_frames_and_refuses_later_ones(wired: Wired) -> None:
    w = wired
    _new_session(w, "sess-alpha1")
    w.host.send(_prompt("sess-alpha1", 2))
    prompt = w.engine.recv()
    w.mediator.close()
    assert w.mediator.drain(5)
    # A peer prompt acknowledged after close is reported, never queued behind the end.
    w.inbox.pending["sess-alpha1"] = [(EVENT, "late")]
    w.engine.send(_end(prompt))
    w.host.recv()
    w.mediator.deliver_once()
    assert w.inbox.asked == [] and w.engine.quiet()


def test_a_stalled_engine_bounds_queued_input(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(devin_acp, "ENGINE_QUEUE_MAX_BYTES", 0)
    wired.host.send({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
    assert wired.mediator.stopped().wait(5)
