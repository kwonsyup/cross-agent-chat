"""ACP mediator that lets a hosted native Devin session receive while idle.

An ACP host (for example Devin Desktop, through its local agent registry)
launches ``cross-agent-chat devin-acp``; this process launches the installed
native ``devin acp`` engine as its child and relays JSON-RPC between them.
The engine keeps its own sessions, model selection, permissions, hooks and
transcript. CAC's existing hooks register the engine's root session exactly as
for any Devin conversation.

The only thing this mediator adds: when one of its sessions has no prompt in
flight and that exact session's CAC inbox holds a message, it acknowledges one
message and submits it as a native ``session/prompt`` for that root session,
echoing it to the host as a user message. Host prompts that arrive meanwhile
wait for that turn's real completion. Nothing is retried, and no message body
is stored.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import IO, Final, Protocol, cast
from uuid import uuid4

from cross_agent_chat import __version__
from cross_agent_chat.core import SCHEMA_VERSION, ChatError, Registry, UnknownDeliveryError
from cross_agent_chat.devin import (
    acp_peer_prompt,
    devin_binaries,
    devin_binary,
    prune_acp_hosts,
    write_acp_host,
)

ACP_FRAME_MAX_BYTES: Final = 64 * 1024 * 1024
DELIVERY_POLL_SECONDS: Final = 1.0
ENGINE_EXIT_GRACE_SECONDS: Final = 5.0
MAX_QUEUED_PROMPTS: Final = 4
REGISTRY_AGENT_ID: Final = "cross-agent-chat-devin"
DEFAULT_REGISTRY: Final = Path("~/.windsurf/acp/registry.json")
PLATFORMS: Final = ("darwin-aarch64", "darwin-x86_64")

Frame = dict[str, object]


class Inbox(Protocol):
    def take(self, session_id: str, still_open: Callable[[], bool]) -> tuple[str, str] | None:
        """Acknowledge and return one ``(event_id, message)`` for this exact session.

        ``still_open`` is checked immediately before acknowledging.
        """


@dataclass
class _Session:
    host_prompts: set[int] = field(default_factory=set)
    cac_prompt: int | None = None
    claiming: bool = False
    cancel_claim: bool = False
    closing: bool = False
    queued: deque[Frame] = field(default_factory=deque)
    deferred: list[Frame] = field(default_factory=list)

    def busy(self) -> bool:
        return (
            bool(self.host_prompts) or self.cac_prompt is not None or self.claiming or self.closing
        )


@dataclass(frozen=True, slots=True)
class _Pending:
    owner: str  # "host" or "cac"
    original_id: object
    method: str
    session_id: str | None


def _session_of(frame: Frame) -> str | None:
    params = frame.get("params")
    if isinstance(params, dict):
        value = cast(dict[str, object], params).get("sessionId")
        if isinstance(value, str):
            return value
    return None


def _cancelled(request_id: object) -> Frame:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "cancelled"}}


class Mediator:
    """Relay one host connection to one native engine and own CAC prompt effects.

    Host request ids are remapped into one adapter-owned id space toward the
    engine, so adapter-originated prompts never collide with them and every
    engine response returns to its real requester. Engine-originated requests
    such as permission prompts pass through unchanged; their replies come back
    from the host with the engine's own id.

    Every frame bound for the engine is decided and enqueued under one lock and
    written by one writer thread, so the engine receives prompts, cancels and
    closes in exactly the order they were decided.
    """

    def __init__(
        self,
        *,
        host_in: IO[bytes],
        host_out: IO[bytes],
        engine_in: IO[bytes],
        engine_out: IO[bytes],
        inbox: Inbox | None,
        poll_seconds: float = DELIVERY_POLL_SECONDS,
        diagnostic: Callable[[str], None] | None = None,
        on_sessions: Callable[[frozenset[str]], None] | None = None,
    ) -> None:
        self._host_in = host_in
        self._host_out = host_out
        self._engine_in = engine_in
        self._engine_out = engine_out
        self._inbox = inbox
        self._poll_seconds = poll_seconds
        self._diagnostic = diagnostic or (
            lambda text: print(f"cross-agent-chat devin-acp: {text}", file=sys.stderr, flush=True)
        )
        self._on_sessions = on_sessions
        self._lock = threading.Lock()
        self._host_write = threading.Lock()
        self._engine_queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._next_id = 0
        self._pending: dict[int, _Pending] = {}
        self._sessions: dict[str, _Session] = {}
        self._stopped = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, deliver: bool = True) -> None:
        loops = [self.host_loop, self.engine_loop, self.writer_loop]
        if deliver:
            loops.append(self.delivery_loop)
        for loop in loops:
            threading.Thread(target=loop, daemon=True).start()

    def stopped(self) -> threading.Event:
        return self._stopped

    def _stop(self) -> None:
        self._stopped.set()
        self._engine_queue.put(None)

    # -- framing -----------------------------------------------------------

    @staticmethod
    def _read(stream: IO[bytes]) -> bytes | None:
        line = stream.readline(ACP_FRAME_MAX_BYTES + 1)
        if not line:
            return None
        if len(line) > ACP_FRAME_MAX_BYTES:
            raise ChatError("ACP frame exceeds the bounded limit")
        return line

    @staticmethod
    def _encode(frame: Frame | bytes) -> bytes:
        if isinstance(frame, bytes):
            return frame
        return (json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n").encode()

    def _to_host(self, frame: Frame | bytes) -> None:
        data = self._encode(frame)
        with self._host_write:
            self._host_out.write(data)
            self._host_out.flush()

    def _to_engine(self, frame: Frame | bytes) -> None:
        """Enqueue one engine-bound frame; callers decide order under ``_lock``."""

        self._engine_queue.put(self._encode(frame))

    def writer_loop(self) -> None:
        try:
            while (data := self._engine_queue.get()) is not None:
                self._engine_in.write(data)
                self._engine_in.flush()
        except OSError:
            self._diagnostic("the engine stopped accepting input")
        finally:
            self._stopped.set()

    def _allocate(self, pending: _Pending) -> int:
        self._next_id += 1
        self._pending[self._next_id] = pending
        return self._next_id

    def _track(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = self._sessions[session_id] = _Session()
            self._publish_sessions()
        return session

    def _publish_sessions(self) -> None:
        if self._on_sessions is not None:
            try:
                self._on_sessions(frozenset(self._sessions))
            except (ChatError, OSError) as error:
                self._diagnostic(f"session attestation failed: {error}")

    # -- host -> engine ----------------------------------------------------

    def host_loop(self) -> None:
        try:
            while (line := self._read(self._host_in)) is not None:
                self._from_host(line)
        finally:
            self._stop()

    def _from_host(self, line: bytes) -> None:
        try:
            decoded: object = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._to_engine(line)
            return
        if not isinstance(decoded, dict):
            self._to_host(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "batched requests are not supported"},
                }
            )
            return
        frame = cast(Frame, decoded)
        method = frame.get("method")
        if not isinstance(method, str):
            # A reply to an engine-originated request keeps the engine's id.
            self._to_engine(frame)
            return
        session_id = _session_of(frame)
        if "id" not in frame:
            if method == "session/cancel" and session_id is not None:
                self._cancel(frame, session_id)
            else:
                self._to_engine(frame)
            return
        refusal: Frame | None = None
        with self._lock:
            session = self._sessions.get(session_id) if session_id is not None else None
            if method == "session/prompt" and session_id is not None:
                session = self._track(session_id)
                if session.cac_prompt is not None or session.claiming or session.queued:
                    if len(session.queued) < MAX_QUEUED_PROMPTS:
                        session.queued.append(frame)
                    else:
                        refusal = {
                            "jsonrpc": "2.0",
                            "id": frame["id"],
                            "error": {"code": -32000, "message": "too many prompts are waiting"},
                        }
                else:
                    internal = self._allocate(_Pending("host", frame["id"], method, session_id))
                    session.host_prompts.add(internal)
                    self._to_engine({**frame, "id": internal})
            else:
                internal = self._allocate(_Pending("host", frame["id"], method, session_id))
                remapped = {**frame, "id": internal}
                if session is not None and method in {"session/delete", "session/close"}:
                    session.closing = True
                    if session.claiming:
                        # A message may be mid-acknowledgement for this session;
                        # the close follows whatever that claim submits.
                        session.deferred.append(remapped)
                        remapped = {}
                if remapped:
                    self._to_engine(remapped)
        if refusal is not None:
            self._to_host(refusal)

    def _cancel(self, frame: Frame, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            queued: list[Frame] = []
            if session is None:
                self._to_engine(frame)
            else:
                queued = list(session.queued)
                session.queued.clear()
                if session.claiming:
                    # A peer message is being taken right now; if one is
                    # submitted, this cancel follows it.
                    session.cancel_claim = True
                elif session.cac_prompt is not None or session.host_prompts:
                    self._to_engine(frame)
        for prompt in queued:
            # The human stopped a prompt that never reached the engine.
            self._to_host(_cancelled(prompt["id"]))

    # -- engine -> host ----------------------------------------------------

    def engine_loop(self) -> None:
        try:
            while (line := self._read(self._engine_out)) is not None:
                self._from_engine(line)
        finally:
            self._stop()

    def _from_engine(self, line: bytes) -> None:
        try:
            decoded: object = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._to_host(line)
            return
        if not isinstance(decoded, dict):
            self._to_host(line)
            return
        frame = cast(Frame, decoded)
        if "method" in frame or "id" not in frame:
            self._to_host(frame)
            return
        internal = frame["id"]
        with self._lock:
            pending = (
                self._pending.pop(internal, None)
                if isinstance(internal, int) and not isinstance(internal, bool)
                else None
            )
            if pending is None or not isinstance(internal, int):
                self._diagnostic("dropped an engine response with no requester")
                return
            answers = self._settle(pending, internal, frame)
        if pending.owner == "cac":
            result = frame.get("result")
            stop = (
                cast(dict[str, object], result).get("stopReason")
                if isinstance(result, dict)
                else "error"
            )
            self._diagnostic(f"peer turn finished: {stop}")
        else:
            self._to_host({**frame, "id": pending.original_id})
        for answer in answers:
            self._to_host(answer)

    def _settle(self, pending: _Pending, internal: int, frame: Frame) -> list[Frame]:
        """Update state for one engine response; return extra host answers."""

        result = frame.get("result")
        sid = pending.session_id
        if pending.method == "session/new" and isinstance(result, dict):
            created = cast(dict[str, object], result).get("sessionId")
            if isinstance(created, str):
                self._track(created)
            return []
        if sid is None:
            return []
        if pending.method in {"session/load", "session/resume"} and "result" in frame:
            self._track(sid)
            return []
        session = self._sessions.get(sid)
        if session is None:
            return []
        if pending.method in {"session/delete", "session/close"}:
            if "result" not in frame:
                session.closing = False
                self._due(sid, session)
                return []
            del self._sessions[sid]
            self._publish_sessions()
            # Prompts that waited behind a peer turn never reached the engine.
            return [_cancelled(queued["id"]) for queued in session.queued]
        if pending.method != "session/prompt":
            return []
        session.host_prompts.discard(internal)
        if session.cac_prompt == internal:
            session.cac_prompt = None
        self._due(sid, session)
        return []

    def _due(self, session_id: str, session: _Session) -> None:
        """Submit the oldest waiting host prompt once the session is free."""

        if session.busy() or not session.queued:
            return
        frame = session.queued.popleft()
        internal = self._allocate(_Pending("host", frame["id"], "session/prompt", session_id))
        session.host_prompts.add(internal)
        self._to_engine({**frame, "id": internal})

    # -- CAC delivery ------------------------------------------------------

    def delivery_loop(self) -> None:
        while not self._stopped.wait(self._poll_seconds):
            self.deliver_once()

    def _still_open(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
            return session is not None and not session.closing and not self._stopped.is_set()

    def deliver_once(self) -> None:
        if self._inbox is None:
            return
        with self._lock:
            idle = [sid for sid, session in self._sessions.items() if not session.busy()]
            for sid in idle:
                self._sessions[sid].claiming = True
        for sid in idle:
            try:
                taken = self._inbox.take(sid, partial(self._still_open, sid))
            except UnknownDeliveryError as error:
                # The acknowledgement may have consumed a message: report, never retry.
                self._diagnostic(f"acknowledgement outcome is unknown; nothing submitted: {error}")
                taken = None
            except (ChatError, OSError) as error:
                self._diagnostic(f"inbox unavailable: {error}")
                taken = None
            message_id = str(uuid4())
            text = acp_peer_prompt(*taken) if taken is not None else ""
            if taken is not None:
                # Shown before the engine can stream anything for this turn.
                self._to_host(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": sid,
                            "update": {
                                "sessionUpdate": "user_message_chunk",
                                "content": {"type": "text", "text": text},
                                "_meta": {
                                    "cognition.ai/clientMessageId": message_id,
                                    "cognition.ai/messageSubIndex": 0,
                                },
                            },
                        },
                    }
                )
            with self._lock:
                session = self._sessions.get(sid)
                if session is None:
                    if taken is not None:
                        self._diagnostic(f"event {taken[0]} was taken for a session that ended")
                    continue
                session.claiming = False
                if taken is not None:
                    internal = self._allocate(_Pending("cac", None, "session/prompt", sid))
                    session.cac_prompt = internal
                    self._to_engine(
                        {
                            "jsonrpc": "2.0",
                            "id": internal,
                            "method": "session/prompt",
                            "params": {
                                "sessionId": sid,
                                "prompt": [{"type": "text", "text": text}],
                                "_meta": {"cognition.ai/clientMessageId": message_id},
                            },
                        }
                    )
                    if session.cancel_claim:
                        self._to_engine(
                            {
                                "jsonrpc": "2.0",
                                "method": "session/cancel",
                                "params": {"sessionId": sid},
                            }
                        )
                session.cancel_claim = False
                for deferred in session.deferred:
                    self._to_engine(deferred)
                session.deferred.clear()
                self._due(sid, session)
            if taken is not None:
                self._diagnostic(f"submitted event {taken[0]} to its idle session")


class CourierInbox:
    """Take one message from the exact CAC route this adapter's engine owns."""

    def __init__(self, root: Path, engine_pid: int) -> None:
        self.root = root
        self.engine_pid = engine_pid

    def take(self, session_id: str, still_open: Callable[[], bool]) -> tuple[str, str] | None:
        from cross_agent_chat.runtime import (
            _devin_messages,
            _route_current,
            request_socket,
            socket_path,
        )

        routes = [
            route
            for route in Registry(self.root).routes()
            if route.provider == "devin"
            and route.session_id == session_id
            and route.pid == self.engine_pid
        ]
        if len(routes) != 1 or not _route_current(self.root, routes[0]):
            return None
        route = routes[0]
        messages = _devin_messages(self.root, route)
        if not messages:
            return None
        selected = messages[0]
        if not still_open():
            return None
        # Acknowledging first makes this the only consumer: the courier
        # removes the event exactly once, so a racing Stop, prompt or tool
        # hook that loses the acknowledgement delivers nothing. The prompt goes
        # to the ACP root session itself, never to a subagent sharing its
        # hook identity, so the hooks' subagent custody hold does not apply.
        acknowledged = request_socket(
            socket_path(self.root, route),
            {
                "schema_version": SCHEMA_VERSION,
                "operation": "ack",
                "generation": route.generation,
                "event_ids": [selected["event_id"]],
            },
        )
        if acknowledged != {
            "schema_version": SCHEMA_VERSION,
            "status": "ACKNOWLEDGED",
            "event_ids": [selected["event_id"]],
        }:
            raise UnknownDeliveryError("Devin courier acknowledgement is not exact")
        return selected["event_id"], selected["message"]


def _engine_binary(requested: Path | None) -> Path:
    if requested is None:
        return devin_binary()
    try:
        resolved = requested.expanduser().resolve(strict=True)
    except OSError as error:
        raise ChatError("requested Devin CLI is unavailable") from error
    if resolved not in devin_binaries():
        raise ChatError("requested Devin CLI is not an installed Devin binary")
    return resolved


def run(*, state_root_value: str | None, devin: Path | None, engine_args: list[str]) -> int:
    from cross_agent_chat.runtime import presence_is_enabled, state_root

    binary = _engine_binary(devin)
    args = engine_args[1:] if engine_args[:1] == ["--"] else engine_args
    engine = subprocess.Popen(
        [str(binary), "acp", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert engine.stdin is not None and engine.stdout is not None
    root = state_root(state_root_value)
    attestation: Path | None = None
    inbox: Inbox | None = None
    if presence_is_enabled():
        try:
            prune_acp_hosts(root)
            attestation = write_acp_host(root, adapter_pid=os.getpid(), engine_pid=engine.pid)
            inbox = CourierInbox(root, engine.pid)
        except (ChatError, OSError) as error:
            print(
                f"cross-agent-chat devin-acp: idle receiving is unavailable: {error}",
                file=sys.stderr,
                flush=True,
            )

    def attest(sessions: frozenset[str]) -> None:
        write_acp_host(root, adapter_pid=os.getpid(), engine_pid=engine.pid, sessions=sessions)

    mediator = Mediator(
        host_in=sys.stdin.buffer,
        host_out=sys.stdout.buffer,
        engine_in=engine.stdin,
        engine_out=engine.stdout,
        inbox=inbox,
        on_sessions=attest if attestation is not None else None,
    )
    # The host ends an agent with SIGTERM; exit through the cleanup below.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(143))
    mediator.start()
    try:
        mediator.stopped().wait()
    finally:
        if attestation is not None:
            with suppress(OSError):
                attestation.unlink()
        with suppress(OSError):
            engine.stdin.close()
        try:
            code = engine.wait(ENGINE_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            engine.terminate()
            code = engine.wait()
    return code if code >= 0 else 1


# -- Local ACP registry entry ---------------------------------------------


def registry_entry(command: Path) -> dict[str, object]:
    target = {"archive": "", "cmd": str(command), "args": ["devin-acp"]}
    return {
        "id": REGISTRY_AGENT_ID,
        "name": "Devin + Cross Agent Chat",
        "version": __version__,
        "description": "Native Devin CLI (devin acp) that can receive Cross Agent Chat "
        "messages while idle",
        "authors": ["Cross Agent Chat"],
        "license": "MIT",
        "distribution": {"binary": {platform: dict(target) for platform in PLATFORMS}},
    }


def _skeleton() -> dict[str, object]:
    return {"version": "1.0.0", "agents": [], "extensions": []}


def _load_registry(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChatError(f"ACP registry is not valid JSON: {path}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("agents"), list):
        raise ChatError(f"ACP registry has no agents list: {path}")
    return cast(dict[str, object], raw)


def _write_registry(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _ours(item: object) -> bool:
    return isinstance(item, dict) and cast(dict[str, object], item).get("id") == REGISTRY_AGENT_ID


def register(path: Path, command: Path) -> str:
    """Add or refresh only the CAC agent entry, preserving every other entry."""

    path = path.expanduser()
    document = _load_registry(path) or _skeleton()
    agents = cast(list[object], document["agents"])
    entry = registry_entry(command)
    replaced = [entry if _ours(item) else item for item in agents]
    document["agents"] = replaced if any(_ours(item) for item in agents) else [*agents, entry]
    _write_registry(path, document)
    return f"registered {REGISTRY_AGENT_ID} in {path}"


def unregister(path: Path) -> str:
    """Remove only the CAC agent entry; delete a registry left as the bare skeleton."""

    path = path.expanduser()
    document = _load_registry(path)
    if document is None or not any(_ours(item) for item in cast(list[object], document["agents"])):
        return f"{REGISTRY_AGENT_ID} is not registered in {path}"
    document["agents"] = [
        item for item in cast(list[object], document["agents"]) if not _ours(item)
    ]
    if document == _skeleton():
        path.unlink()
    else:
        _write_registry(path, document)
    return f"removed {REGISTRY_AGENT_ID} from {path}"


def registry_status(path: Path) -> dict[str, object]:
    path = path.expanduser()
    document = _load_registry(path)
    entries = (
        [item for item in cast(list[object], document["agents"]) if _ours(item)]
        if document is not None
        else []
    )
    return {"registry": str(path), "registered": bool(entries), "agent_id": REGISTRY_AGENT_ID}


def default_command() -> Path:
    """The stable installed entrypoint the host should launch."""

    invoked = Path(sys.argv[0])
    if invoked.name == "cross-agent-chat" and invoked.is_absolute():
        return invoked
    found = shutil.which("cross-agent-chat")
    if found is None:
        raise ChatError("cross-agent-chat entrypoint is unavailable; pass --entrypoint")
    return Path(found).absolute()
