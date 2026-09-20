"""Bounded stdio MCP surface implementing the 2025-03-26 JSON-RPC contract."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from typing import IO, Final, NamedTuple, TextIO, cast

from cross_agent_chat.core import ChatError, bounded_message

TARGET_FIELDS = ("to", "recipient", "destination")
REPLY_HINTS = ("wait_for_reply", "request_reply")

MAX_FRAME_BYTES: Final = 65536

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603


class MethodNotFound(Exception):
    """Raised by the dispatch callback for a method this server does not implement."""


class DroppedFrame(NamedTuple):
    message: str


def _discard_line_remainder(stream: IO[str] | IO[bytes], limit: int) -> None:
    while True:
        try:
            chunk = stream.readline(limit + 1)
        except UnicodeDecodeError:
            continue
        if chunk in ("", b"") or (
            chunk.endswith(b"\n") if isinstance(chunk, bytes) else chunk.endswith("\n")
        ):
            return


def read_frames(
    stream: IO[str] | IO[bytes], limit: int = MAX_FRAME_BYTES
) -> Iterator[bytes | DroppedFrame]:
    """Yield newline-delimited frames without ever allocating a whole oversized line.

    Each read is capped at ``limit + 1`` characters or bytes. A frame whose
    encoded length exceeds ``limit`` is dropped and its remainder consumed up to
    the next newline, so the following frame starts at a real boundary and can
    never dispatch leftover bytes. On a binary stream each yielded frame is
    decoded by the caller, so undecodable input costs exactly one frame; on a
    text stream an undecodable frame is dropped the same way.
    """
    while True:
        try:
            chunk = stream.readline(limit + 1)
        except UnicodeDecodeError:
            _discard_line_remainder(stream, limit)
            yield DroppedFrame("parse error")
            continue
        if chunk in ("", b""):
            return
        data = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
        if len(data) > limit:
            if not data.endswith(b"\n"):
                _discard_line_remainder(stream, limit)
            yield DroppedFrame("request exceeds the bounded limit")
            continue
        yield data


def _success(identifier: object, result: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error(identifier: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _valid_request_id(identifier: object) -> bool:
    # MCP request IDs are strings or integers; null, booleans, floats and
    # structured values are not valid even though base JSON-RPC is looser.
    return isinstance(identifier, str) or (
        isinstance(identifier, int) and not isinstance(identifier, bool)
    )


class _Session:
    def __init__(
        self,
        *,
        emit: Callable[[object], None],
        initialize_result: Callable[[], dict[str, object]],
        dispatch: Callable[[str, dict[str, object]], object],
    ) -> None:
        self._emit = emit
        self._initialize_result = initialize_result
        self._dispatch = dispatch
        self._initialized = False
        self._seen_ids: set[object] = set()

    def handle(self, message: object) -> None:
        if isinstance(message, list):
            if not message:
                self._emit(_error(None, INVALID_REQUEST, "invalid request"))
                return
            responses = [
                response
                for element in message
                if (response := self._handle_one(element, in_batch=True)) is not None
            ]
            # A batch of only notifications produces no output at all.
            if responses:
                self._emit(responses)
            return
        response = self._handle_one(message, in_batch=False)
        if response is not None:
            self._emit(response)

    def _handle_one(self, element: object, *, in_batch: bool) -> dict[str, object] | None:
        if not isinstance(element, dict) or element.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "invalid request")
        if "id" not in element:
            # A notification never gets a response. notifications/initialized,
            # notifications/cancelled and unknown notifications are all accepted
            # silently; a cancellation can never undo or authorize replaying an
            # effect that may already have happened, and an effect-carrying call
            # with no id would leave no receipt to reconcile.
            return None
        identifier = element["id"]
        if not _valid_request_id(identifier):
            return _error(None, INVALID_REQUEST, "invalid request")
        if identifier in self._seen_ids:
            return _error(identifier, INVALID_REQUEST, "request id was already used")
        self._seen_ids.add(identifier)
        method = element.get("method")
        if not isinstance(method, str):
            return _error(identifier, INVALID_REQUEST, "invalid request")
        params = element.get("params", {})
        if not isinstance(params, dict):
            return _error(identifier, INVALID_PARAMS, "invalid params")
        if method == "initialize":
            if in_batch:
                return _error(identifier, INVALID_REQUEST, "initialize must not be sent in a batch")
            if self._initialized:
                return _error(identifier, INVALID_REQUEST, "session is already initialized")
            self._initialized = True
            return _success(identifier, self._initialize_result())
        if method == "ping":
            return _success(identifier, {})
        try:
            return _success(identifier, self._dispatch(method, cast(dict[str, object], params)))
        except MethodNotFound:
            return _error(identifier, METHOD_NOT_FOUND, "method not found")
        except ChatError as error:
            return _error(identifier, INVALID_PARAMS, str(error))
        except Exception:
            return _error(identifier, INTERNAL_ERROR, "internal error")


def serve(
    *,
    stream: TextIO,
    emit: Callable[[object], None],
    initialize_result: Callable[[], dict[str, object]],
    dispatch: Callable[[str, dict[str, object]], object],
) -> None:
    """Run one stdio JSON-RPC session until the input stream ends.

    ``emit`` receives one response object, or one array for a batch, per output
    line. Requests are accepted without a completed handshake because existing
    provider integrations dispatch single calls directly; lifecycle rules are
    still enforced where the revision requires them (single initialize, never
    batched, request-id uniqueness).
    """
    session = _Session(emit=emit, initialize_result=initialize_result, dispatch=dispatch)
    # Reading the byte buffer keeps each malformed UTF-8 frame to exactly one
    # dropped line; a text stream that already decoded its input is supported too.
    source: IO[str] | IO[bytes] = stream.buffer if isinstance(stream, io.TextIOWrapper) else stream
    for frame in read_frames(source):
        if isinstance(frame, DroppedFrame):
            emit(_error(None, PARSE_ERROR, frame.message))
            continue
        try:
            message = json.loads(frame)
        except (json.JSONDecodeError, UnicodeDecodeError):
            emit(_error(None, PARSE_ERROR, "parse error"))
            continue
        session.handle(message)


def normalize_send_arguments(arguments: dict[str, object]) -> tuple[str, str]:
    allowed = {*TARGET_FIELDS, "message", *REPLY_HINTS}
    unknown = set(arguments) - allowed
    if unknown:
        raise ChatError(f"unknown field: {sorted(unknown)[0]}")
    targets = [field for field in TARGET_FIELDS if field in arguments]
    if len(targets) != 1:
        raise ChatError("exactly one target field is required")
    for hint in REPLY_HINTS:
        if hint in arguments and arguments[hint] is not False:
            raise ChatError("blocking replies are not supported")
    target = arguments[targets[0]]
    message = arguments.get("message")
    if not isinstance(target, str) or not target.strip():
        raise ChatError("target is invalid")
    if not isinstance(message, str):
        raise ChatError("message is invalid")
    return target, bounded_message(message)
