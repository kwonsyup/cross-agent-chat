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
import select
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
# SIGKILL always ends the owned child; a shorter bound covers its reap and the
# writer's broken-pipe wakeup so no shutdown path can wait without a bound.
ENGINE_KILL_GRACE_SECONDS: Final = 2.0
# Well inside the ACP_HOST_TTL_SECONDS lease the reader enforces, so an idle
# adapter's attestation stays fresh without model or host activity.
ACP_HOST_RENEW_SECONDS: Final = 10.0
# The writer polls pipe readiness in bounded slices so shutdown can always end
# it even when a stalled write can never complete.
ENGINE_WRITE_POLL_SECONDS: Final = 0.25
MAX_QUEUED_PROMPTS: Final = 4
ENGINE_QUEUE_MAX_BYTES: Final = 4 * ACP_FRAME_MAX_BYTES
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

    Prompts, cancels and closes are decided and enqueued under one lock, and
    one writer thread writes every engine-bound frame in queue order, so the
    engine receives them in exactly the order they were decided.
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
        renew_seconds: float | None = None,
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
        self._renew_seconds = renew_seconds
        self._lock = threading.Lock()
        self._host_write = threading.Lock()
        self._engine_queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._queue_lock = threading.Lock()
        self._queued_bytes = 0
        self._input_closed = False
        self._abort_writes = threading.Event()
        self._writer: threading.Thread | None = None
        self._next_id = 0
        self._host_name: str | None = None
        self._protocol_version: int | None = None
        self._pending: dict[int, _Pending] = {}
        self._sessions: dict[str, _Session] = {}
        self._stopped = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self, *, deliver: bool = True) -> None:
        self._writer = threading.Thread(target=self.writer_loop, daemon=True)
        loops = [self.host_loop, self.engine_loop]
        if deliver:
            loops.append(self.delivery_loop)
        if self._on_sessions is not None and self._renew_seconds is not None:
            loops.append(self.renewal_loop)
        self._writer.start()
        for loop in loops:
            threading.Thread(target=loop, daemon=True).start()

    def stopped(self) -> threading.Event:
        return self._stopped

    def drain(self, timeout: float) -> bool:
        """Wait until every accepted engine-bound frame was written; True if done."""

        if self._writer is None:
            return True
        self._writer.join(timeout)
        return not self._writer.is_alive()

    def writer_done(self) -> bool:
        """Whether the engine writer is gone, so buffered stdin may be closed."""

        writer = self._writer
        return writer is None or not writer.is_alive()

    def invalidate_sessions(self, action: Callable[[], None], timeout: float) -> bool:
        """Run ``action`` serialized with every session publication.

        A publication can hold this lock while it writes to the filesystem, so
        waiting must be bounded: False means the caller should fall back to an
        unsynchronized invalidation, which the record's own lease still bounds.
        """

        if not self._lock.acquire(timeout=timeout):
            return False
        try:
            action()
        finally:
            self._lock.release()
        return True

    def close(self) -> None:
        """Accept no further engine-bound frames; already accepted ones still drain."""

        self._stop()

    def abort_writes(self) -> None:
        """End a writer stalled on a pipe that can never drain.

        Accepted frames still drain during the shutdown grace window; this is
        only raised once teardown has decided the engine cannot receive more.
        """

        self._abort_writes.set()

    def _stop(self) -> None:
        with self._queue_lock:
            if not self._input_closed:
                # Frames accepted before this point are still written in order;
                # nothing is accepted after it.
                self._input_closed = True
                self._engine_queue.put(None)
        self._stopped.set()

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

    def _to_engine(self, frame: Frame | bytes) -> bool:
        """Accept one engine-bound frame for in-order writing; False if refused."""

        data = self._encode(frame)
        with self._queue_lock:
            if self._input_closed:
                return False
            if self._queued_bytes + len(data) > ENGINE_QUEUE_MAX_BYTES:
                self._diagnostic("the engine is not reading its input; closing")
                self._input_closed = True
                self._engine_queue.put(None)
                self._abort_writes.set()
                self._stopped.set()
                return False
            self._queued_bytes += len(data)
            self._engine_queue.put(data)
        return True

    def _write_engine(self, descriptor: int, data: bytes) -> None:
        """Write one accepted frame fully on the non-blocking descriptor.

        A stalled write keeps retrying in bounded polls, so accepted frames
        still drain during the shutdown grace window; once abort_writes is
        raised the writer ends instead of hanging on a read end that may be
        held by a surviving descendant.
        """

        view = memoryview(data)
        while view:
            try:
                written = os.write(descriptor, view)
            except BlockingIOError:
                if self._abort_writes.is_set():
                    raise OSError("the engine stopped accepting input") from None
                select.select([], [descriptor], [], ENGINE_WRITE_POLL_SECONDS)
            else:
                if not written:
                    raise OSError("the engine stopped accepting input")
                view = view[written:]

    def writer_loop(self) -> None:
        try:
            descriptor = self._engine_in.fileno()
            # Only this loop writes to the engine, so raw writes on the
            # descriptor keep exactly the queue's order; a wedged pipe must
            # never be allowed to block past a bounded poll.
            os.set_blocking(descriptor, False)
            while (data := self._engine_queue.get()) is not None:
                self._write_engine(descriptor, data)
                with self._queue_lock:
                    self._queued_bytes -= len(data)
        except OSError:
            self._diagnostic("the engine stopped accepting input")
        finally:
            with self._queue_lock:
                self._input_closed = True
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

    def _membership(self) -> frozenset[str]:
        """Current nonclosing sessions; callers hold ``self._lock``."""

        return frozenset(
            session_id for session_id, session in self._sessions.items() if not session.closing
        )

    def _publish_sessions(self) -> None:
        """Publish current nonclosing membership; callers hold ``self._lock``.

        Once the mediator stopped, no publish may run: a heartbeat that fires
        after the final invalidation would otherwise resurrect the record.
        """

        if self._on_sessions is None or self._stopped.is_set():
            return
        try:
            self._on_sessions(self._membership())
        except (ChatError, OSError) as error:
            self._diagnostic(f"session attestation failed: {error}")

    def renewal_loop(self) -> None:
        """Refresh the freshness lease until shutdown, independent of activity.

        The snapshot and publication happen under ``self._lock``, the same
        lock every membership change uses, so a renewal is always serialized
        with a session closing and can never resurrect one.
        """

        interval = self._renew_seconds
        if interval is None:
            return
        while not self._stopped.wait(interval):
            with self._lock:
                self._publish_sessions()

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
            if method == "initialize":
                params = frame.get("params")
                info = params.get("clientInfo") if isinstance(params, dict) else None
                name = info.get("name") if isinstance(info, dict) else None
                self._host_name = name if isinstance(name, str) else None
                self._protocol_version = None
            session = self._sessions.get(session_id) if session_id is not None else None
            if method == "session/prompt" and session_id is not None:
                session = self._track(session_id)
                if session.closing:
                    refusal = {
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "error": {"code": -32000, "message": "the session is closing"},
                    }
                elif session.cac_prompt is not None or session.claiming or session.queued:
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
        if pending.method == "initialize":
            version = result.get("protocolVersion") if isinstance(result, dict) else None
            self._protocol_version = (
                version if isinstance(version, int) and not isinstance(version, bool) else None
            )
            return []
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
        if pending.owner == "cac" and not session.busy() and self._desktop_v1():
            # Publish under the session lock: a later peer claim/host admission
            # cannot emit working first and then be cleared by this completion.
            self._to_host(self._desktop_status(sid, "idle"))
        return []

    def _desktop_v1(self) -> bool:
        """Use only the installed Desktop v1 consumer's session metadata extension."""

        return self._protocol_version == 1 and self._host_name in {
            "windsurf",
            "windsurf-next",
            "windsurf-insiders",
        }

    @staticmethod
    def _desktop_status(session_id: str, status: str) -> Frame:
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "session_info_update"},
                "_meta": {"cognition.ai/session": {"cognition.ai/statusEnum": status}},
            },
        }

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
        if self._inbox is None or self._stopped.is_set():
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
                            **(
                                {
                                    "_meta": {
                                        "cognition.ai/session": {
                                            "cognition.ai/statusEnum": "working"
                                        }
                                    }
                                }
                                if self._desktop_v1()
                                else {}
                            ),
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
            not_submitted = False
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
                    submitted = self._to_engine(
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
                    if not submitted:
                        # Acknowledged but the engine input already closed:
                        # report the uncertain event, never retry it.
                        self._pending.pop(internal, None)
                        session.cac_prompt = None
                        self._diagnostic(f"event {taken[0]} was acknowledged but not submitted")
                        taken = None
                        not_submitted = True
                    elif session.cancel_claim:
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
                if not_submitted and not session.busy() and self._desktop_v1():
                    self._to_host(self._desktop_status(sid, "idle"))
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


def _republish_attestation(
    root: Path,
    attestation: Path,
    *,
    adapter_pid: int,
    engine_pid: int,
    sessions: frozenset[str],
) -> None:
    """Rewrite the lease; on failure never leave an older record readable.

    If the rewrite fails and the unlink also fails, the record's own lease is
    the backstop: readers refuse it once ``renewed_at`` ages past the TTL.
    """

    try:
        write_acp_host(root, adapter_pid=adapter_pid, engine_pid=engine_pid, sessions=sessions)
    except (ChatError, OSError):
        with suppress(OSError):
            attestation.unlink()
        raise


def _stop_owned_engine(
    mediator: Mediator | None,
    engine: subprocess.Popen[bytes],
    *,
    diagnostic: Callable[[str], None],
) -> int:
    """Drain, terminate, then kill and reap the owned engine, all bounded.

    Accepted frames keep their order while they drain; once admission stopped,
    a full pipe means the engine no longer reads, so ending it is what lets the
    writer finish or fail. The buffered stdin is closed only after the writer
    is confirmed gone, so close can never wait on the writer's stream lock.
    """

    if mediator is not None:
        mediator.close()
        if not mediator.drain(ENGINE_EXIT_GRACE_SECONDS):
            # The engine stopped reading; ending it unblocks the writer.
            engine.terminate()
            if not mediator.drain(ENGINE_EXIT_GRACE_SECONDS):
                # A surviving descendant can keep the pipe's read end open
                # past the child's SIGKILL, so the writer is aborted directly
                # rather than relying on the write to fail.
                mediator.abort_writes()
                engine.kill()
                mediator.drain(ENGINE_KILL_GRACE_SECONDS)
    if engine.stdin is not None:
        if mediator is not None and not mediator.writer_done():
            # The writer exits on a bounded poll even when a live descendant
            # still holds the pipe's read end, so this is defensive only: never
            # close the descriptor out from under an active writer.
            diagnostic("the engine writer never exited; leaving engine stdin open")
        else:
            with suppress(OSError):
                engine.stdin.close()
    code: int | None = None
    try:
        code = engine.wait(timeout=ENGINE_EXIT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        engine.terminate()
    if code is None:
        try:
            code = engine.wait(timeout=ENGINE_EXIT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            engine.kill()
    if code is None:
        try:
            code = engine.wait(timeout=ENGINE_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            diagnostic("the owned engine could not be reaped after SIGKILL")
    return code if code is not None and code >= 0 else 1


def run(*, state_root_value: str | None, devin: Path | None, engine_args: list[str]) -> int:
    from cross_agent_chat.runtime import presence_is_enabled, state_root

    binary = _engine_binary(devin)
    args = engine_args[1:] if engine_args[:1] == ["--"] else engine_args

    def diagnostic(text: str) -> None:
        print(f"cross-agent-chat devin-acp: {text}", file=sys.stderr, flush=True)

    engine: subprocess.Popen[bytes] | None = None
    mediator: Mediator | None = None
    attestation: Path | None = None

    def remove_attestation() -> None:
        if attestation is not None:
            with suppress(OSError):
                attestation.unlink()

    # Cover initialization too: a host may terminate before sessions or the
    # mediator exist, but the owned engine must still leave through finally.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(143))
    try:
        engine = subprocess.Popen(
            [str(binary), "acp", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        assert engine.stdin is not None and engine.stdout is not None
        root = state_root(state_root_value)
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
            assert engine is not None
            if attestation is not None:
                _republish_attestation(
                    root,
                    attestation,
                    adapter_pid=os.getpid(),
                    engine_pid=engine.pid,
                    sessions=sessions,
                )

        mediator = Mediator(
            host_in=sys.stdin.buffer,
            host_out=sys.stdout.buffer,
            engine_in=engine.stdin,
            engine_out=engine.stdout,
            inbox=inbox,
            diagnostic=diagnostic,
            on_sessions=attest if attestation is not None else None,
            renew_seconds=ACP_HOST_RENEW_SECONDS,
        )
        mediator.start()
        # Intentionally unbounded while the host stays open; the bounded part
        # is the teardown in the finally clause.
        mediator.stopped().wait()
    finally:
        if mediator is not None:
            mediator.close()
            # Stop publication first so no renewal can fire afterwards, then
            # remove the record serialized with any publish already in flight.
            # A wedged publisher cannot stall teardown: the lease bounds what
            # an unsynchronized removal can leave behind.
            if not mediator.invalidate_sessions(remove_attestation, ENGINE_EXIT_GRACE_SECONDS):
                remove_attestation()
        else:
            remove_attestation()
        # Whatever happened above, the owned child leaves with this process.
        code = (
            _stop_owned_engine(mediator, engine, diagnostic=diagnostic) if engine is not None else 1
        )
    return code


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
