"""Private, body-free bindings between original Codex tasks and native helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from cross_agent_chat.core import (
    ChatError,
    Route,
    atomic_json,
    require_private_file,
    state_lock,
    stored_cwd,
    valid_device,
    valid_uuid,
)

NativeHelperState = Literal["UNKNOWN", "REGISTERED", "RETIRED"]
NativeDispatchState = Literal["UNKNOWN"]


def native_helper_create_hook_group() -> dict[str, object]:
    """Invoke the real app create operation only after internal bootstrap success."""

    return {
        "matcher": "mcp__cross_agent_chat__native_bootstrap",
        "hooks": [
            {
                "type": "mcp_tool",
                "server": "codex_app",
                "tool": "create_thread",
                "input": {
                    "prompt": "${tool_response._meta.create_thread.prompt}",
                    "target": "${tool_response._meta.create_thread.target}",
                    "model": "${tool_response._meta.create_thread.model}",
                    "thinking": "${tool_response._meta.create_thread.thinking}",
                    "title": "${tool_response._meta.create_thread.title}",
                },
                "timeout": 30,
                "statusMessage": "Starting Cross Agent Chat helper",
            }
        ],
    }


def native_helper_dispatch_hook_group() -> dict[str, object]:
    """Invoke Desktop's original-thread operation after one dispatch claim."""

    return {
        "matcher": "mcp__cross_agent_chat__native_dispatch",
        "hooks": [
            {
                "type": "mcp_tool",
                "server": "codex_app",
                "tool": "send_message_to_thread",
                "input": {
                    "threadId": "${tool_response._meta.native_args.threadId}",
                    "prompt": "${tool_response._meta.native_args.prompt}",
                },
                "timeout": 30,
                "statusMessage": "Delivering Cross Agent Chat message",
            }
        ],
    }


@dataclass(frozen=True, slots=True)
class NativeHelperBinding:
    """One original Codex task and its private same-profile native helper."""

    original_session_id: str
    original_generation: str
    original_device: str
    original_cwd: str
    original_profile_root: str
    account_sha256: str
    helper_directory: str
    nonce_sha256: str
    state: NativeHelperState
    helper_session_id: str | None = None
    helper_generation: str | None = None

    def __post_init__(self) -> None:
        valid_uuid(self.original_session_id, "original session id")
        valid_uuid(self.original_generation, "original generation")
        valid_device(self.original_device)
        stored_cwd(self.original_cwd)
        stored_cwd(self.original_profile_root)
        if Path(
            self.helper_directory
        ).name != self.helper_directory or not self.helper_directory.startswith(
            "cac-native-helper-"
        ):
            raise ChatError("native helper binding is invalid")
        try:
            valid_uuid(self.helper_directory.removeprefix("cac-native-helper-"), "helper directory")
        except ChatError as error:
            raise ChatError("native helper binding is invalid") from error
        if len(self.nonce_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.nonce_sha256
        ):
            raise ChatError("native helper binding is invalid")
        if len(self.account_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.account_sha256
        ):
            raise ChatError("native helper binding is invalid")
        if self.state in {"REGISTERED", "RETIRED"}:
            if self.helper_session_id is None or self.helper_generation is None:
                raise ChatError("native helper binding is invalid")
            valid_uuid(self.helper_session_id, "helper session id")
            valid_uuid(self.helper_generation, "helper generation")
        elif self.helper_session_id is not None or self.helper_generation is not None:
            raise ChatError("native helper binding is invalid")

    @classmethod
    def reserve(
        cls, original: Route, nonce: str, helper_directory: str, account_sha256: str
    ) -> NativeHelperBinding:
        """Create the pre-create unknown record for one current original route."""

        if original.provider != "codex" or original.profile_root is None:
            raise ChatError("native helper source is unavailable")
        return cls(
            original_session_id=original.session_id,
            original_generation=original.generation,
            original_device=original.device,
            original_cwd=original.cwd,
            original_profile_root=original.profile_root,
            account_sha256=account_sha256,
            helper_directory=helper_directory,
            nonce_sha256=hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
            state="UNKNOWN",
        )

    def register(self, helper: Route, nonce: str, account_sha256: str) -> NativeHelperBinding:
        """Bind a helper only when its exact local context matches the original."""

        if self.state != "UNKNOWN" or helper.provider != "codex":
            raise ChatError("native helper registration is unavailable")
        if (
            helper.session_id == self.original_session_id
            or helper.device != self.original_device
            or helper.profile_root != self.original_profile_root
            or Path(helper.cwd).name != self.helper_directory
            or account_sha256 != self.account_sha256
        ):
            raise ChatError("native helper context is invalid")
        digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        if digest != self.nonce_sha256:
            raise ChatError("native helper token is invalid")
        return NativeHelperBinding(
            original_session_id=self.original_session_id,
            original_generation=self.original_generation,
            original_device=self.original_device,
            original_cwd=self.original_cwd,
            original_profile_root=self.original_profile_root,
            account_sha256=self.account_sha256,
            helper_directory=self.helper_directory,
            nonce_sha256=self.nonce_sha256,
            state="REGISTERED",
            helper_session_id=helper.session_id,
            helper_generation=helper.generation,
        )

    def retire(self) -> NativeHelperBinding:
        """Preserve a definitively dead helper relation before a new reservation."""

        if self.state != "REGISTERED":
            raise ChatError("native helper binding is invalid")
        return NativeHelperBinding(
            original_session_id=self.original_session_id,
            original_generation=self.original_generation,
            original_device=self.original_device,
            original_cwd=self.original_cwd,
            original_profile_root=self.original_profile_root,
            account_sha256=self.account_sha256,
            helper_directory=self.helper_directory,
            nonce_sha256=self.nonce_sha256,
            state="RETIRED",
            helper_session_id=self.helper_session_id,
            helper_generation=self.helper_generation,
        )

    def to_dict(self) -> dict[str, str]:
        """Serialize only private route relations and the nonce digest."""

        result = {
            "original_session_id": self.original_session_id,
            "original_generation": self.original_generation,
            "original_device": self.original_device,
            "original_cwd": self.original_cwd,
            "original_profile_root": self.original_profile_root,
            "account_sha256": self.account_sha256,
            "helper_directory": self.helper_directory,
            "nonce_sha256": self.nonce_sha256,
            "state": self.state,
        }
        if self.state in {"REGISTERED", "RETIRED"}:
            assert self.helper_session_id is not None and self.helper_generation is not None
            result["helper_session_id"] = self.helper_session_id
            result["helper_generation"] = self.helper_generation
        return result

    @classmethod
    def from_dict(cls, value: object) -> NativeHelperBinding:
        """Validate one private durable binding record."""

        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise ChatError("native helper binding is invalid")
        raw = cast(dict[str, object], value)
        base = {
            "original_session_id",
            "original_generation",
            "original_device",
            "original_cwd",
            "original_profile_root",
            "account_sha256",
            "helper_directory",
            "nonce_sha256",
            "state",
        }
        registered = base | {"helper_session_id", "helper_generation"}
        if (set(raw) != base and set(raw) != registered) or not all(
            isinstance(raw[key], str) for key in raw
        ):
            raise ChatError("native helper binding is invalid")
        state = raw["state"]
        if state not in {"UNKNOWN", "REGISTERED", "RETIRED"}:
            raise ChatError("native helper binding is invalid")
        return cls(
            original_session_id=cast(str, raw["original_session_id"]),
            original_generation=cast(str, raw["original_generation"]),
            original_device=cast(str, raw["original_device"]),
            original_cwd=cast(str, raw["original_cwd"]),
            original_profile_root=cast(str, raw["original_profile_root"]),
            account_sha256=cast(str, raw["account_sha256"]),
            helper_directory=cast(str, raw["helper_directory"]),
            nonce_sha256=cast(str, raw["nonce_sha256"]),
            state=state,
            helper_session_id=cast(str | None, raw.get("helper_session_id")),
            helper_generation=cast(str | None, raw.get("helper_generation")),
        )


class NativeHelperStore:
    """Private binding state; unknown creates block only their original route."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "native-helpers.json"

    def bindings(self) -> list[NativeHelperBinding]:
        if not self.path.exists():
            return []
        require_private_file(self.path)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ChatError("native helper state is invalid") from error
        if not isinstance(raw, list):
            raise ChatError("native helper state is invalid")
        bindings = [NativeHelperBinding.from_dict(item) for item in raw]
        active_originals = [
            (item.original_session_id, item.original_generation)
            for item in bindings
            if item.state in {"UNKNOWN", "REGISTERED"}
        ]
        directories = [item.helper_directory for item in bindings]
        nonces = [item.nonce_sha256 for item in bindings]
        helpers = [
            (item.helper_session_id, item.helper_generation)
            for item in bindings
            if item.state in {"REGISTERED", "RETIRED"}
        ]
        if (
            len(active_originals) != len(set(active_originals))
            or len(directories) != len(set(directories))
            or len(nonces) != len(set(nonces))
            or len(helpers) != len(set(helpers))
        ):
            raise ChatError("native helper state is invalid")
        return bindings

    def reserve(
        self, original: Route, account_sha256: str, routes: list[Route] | None = None
    ) -> tuple[NativeHelperBinding, str]:
        """Reserve one unknown native create without freezing other original routes."""

        with state_lock(self.root, "native-helpers"):
            existing = self.bindings()
            matches = [
                item
                for item in existing
                if item.original_session_id == original.session_id
                and item.original_generation == original.generation
            ]
            live_helpers = [
                route
                for item in matches
                if item.state == "REGISTERED"
                for route in ([] if routes is None else routes)
                if route.session_id == item.helper_session_id
                and route.generation == item.helper_generation
                and route.process_is_live()
                and route.cwd_is_available()
            ]
            if any(item.state == "UNKNOWN" for item in matches) or live_helpers:
                raise ChatError("native helper bootstrap is unavailable")
            nonce = str(uuid4())
            helper_directory = f"cac-native-helper-{uuid4()}"
            while any(item.helper_directory == helper_directory for item in existing):
                helper_directory = f"cac-native-helper-{uuid4()}"
            binding = NativeHelperBinding.reserve(original, nonce, helper_directory, account_sha256)
            retired = [
                item.retire() if item in matches and item.state == "REGISTERED" else item
                for item in existing
            ]
            atomic_json(self.path, [item.to_dict() for item in [*retired, binding]])
            return binding, nonce

    def needs_bootstrap(self, route: Route) -> bool:
        """Whether this exact original route has no pending or registered helper."""

        return not self.is_helper_lineage(route) and not any(
            item.original_session_id == route.session_id
            and item.original_generation == route.generation
            for item in self.bindings()
        )

    def is_helper_lineage(self, route: Route) -> bool:
        """Whether a route owns a reserved helper directory, across restarts."""

        directory = Path(route.cwd).name
        for item in self.bindings():
            if directory == item.helper_directory:
                return True
            prefix = item.helper_directory + "-"
            suffix = directory.removeprefix(prefix)
            if directory.startswith(prefix) and suffix.isdecimal() and int(suffix) > 0:
                return True
        return False

    def pending_for_helper(self, route: Route) -> bool:
        """Whether a same-profile non-original task may be offered registration."""

        return any(
            item.state == "UNKNOWN"
            and item.original_device == route.device
            and item.original_profile_root == route.profile_root
            and Path(route.cwd).name == item.helper_directory
            for item in self.bindings()
        )

    def original_for_nonce(self, nonce: str, routes: list[Route]) -> Route:
        """Resolve the current original route for one private nonce digest."""

        digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        matches = [item for item in self.bindings() if item.nonce_sha256 == digest]
        if len(matches) != 1:
            raise ChatError("native helper registration is unavailable")
        binding = matches[0]
        current = [
            route
            for route in routes
            if route.provider == "codex"
            and route.session_id == binding.original_session_id
            and route.generation == binding.original_generation
        ]
        if len(current) != 1:
            raise ChatError("native helper original route changed")
        return current[0]

    def register(
        self, helper: Route, nonce: str, original_current: Route, account_sha256: str
    ) -> NativeHelperBinding:
        """Consume one unknown create only while its original route is still exact."""

        with state_lock(self.root, "native-helpers"):
            existing = self.bindings()
            matches = [
                item
                for item in existing
                if item.nonce_sha256 == hashlib.sha256(nonce.encode("utf-8")).hexdigest()
            ]
            if len(matches) != 1:
                raise ChatError("native helper registration is unavailable")
            pending = matches[0]
            if (
                original_current.session_id != pending.original_session_id
                or original_current.generation != pending.original_generation
                or original_current.provider != "codex"
            ):
                raise ChatError("native helper original route changed")
            registered = pending.register(helper, nonce, account_sha256)
            atomic_json(
                self.path,
                [(registered if item == pending else item).to_dict() for item in existing],
            )
            return registered

    def original_for_helper(self, helper: Route, routes: list[Route]) -> Route:
        """Resolve the exact current original bound to a registered helper."""

        matches = [
            item
            for item in self.bindings()
            if item.state == "REGISTERED"
            and item.helper_session_id == helper.session_id
            and item.helper_generation == helper.generation
        ]
        if len(matches) != 1:
            raise ChatError("native helper dispatch is unavailable")
        binding = matches[0]
        originals = [
            route
            for route in routes
            if route.provider == "codex"
            and route.session_id == binding.original_session_id
            and route.generation == binding.original_generation
            and route.device == binding.original_device
            and route.profile_root == binding.original_profile_root
        ]
        if len(originals) != 1:
            raise ChatError("native helper original route changed")
        return originals[0]

    def helper_for_original(self, original: Route, routes: list[Route]) -> Route | None:
        """Return the exact live helper for one original route, if registered."""

        matches = [
            item
            for item in self.bindings()
            if item.state == "REGISTERED"
            and item.original_session_id == original.session_id
            and item.original_generation == original.generation
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ChatError("native helper binding is unavailable")
        binding = matches[0]
        helpers = [
            route
            for route in routes
            if route.provider == "codex"
            and route.session_id == binding.helper_session_id
            and route.generation == binding.helper_generation
            and route.device == original.device
            and route.profile_root == original.profile_root
            and Path(route.cwd).name == binding.helper_directory
            and route.process_is_live()
            and route.cwd_is_available()
        ]
        if len(helpers) != 1:
            return None
        return helpers[0]


@dataclass(frozen=True, slots=True)
class NativeDispatch:
    """One non-retryable native-send attempt without retained message content."""

    event_id: str
    helper_session_id: str
    helper_generation: str
    original_session_id: str
    original_generation: str
    payload_sha256: str
    state: NativeDispatchState

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event id"),
            (self.helper_session_id, "helper session id"),
            (self.helper_generation, "helper generation"),
            (self.original_session_id, "original session id"),
            (self.original_generation, "original generation"),
        ):
            valid_uuid(value, name)
        if len(self.payload_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.payload_sha256
        ):
            raise ChatError("native dispatch is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "helper_session_id": self.helper_session_id,
            "helper_generation": self.helper_generation,
            "original_session_id": self.original_session_id,
            "original_generation": self.original_generation,
            "payload_sha256": self.payload_sha256,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, value: object) -> NativeDispatch:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "event_id",
                "helper_session_id",
                "helper_generation",
                "original_session_id",
                "original_generation",
                "payload_sha256",
                "state",
            }
            or not all(isinstance(item, str) for item in value.values())
        ):
            raise ChatError("native dispatch is invalid")
        raw = cast(dict[str, str], value)
        if raw["state"] != "UNKNOWN":
            raise ChatError("native dispatch is invalid")
        return cls(
            event_id=raw["event_id"],
            helper_session_id=raw["helper_session_id"],
            helper_generation=raw["helper_generation"],
            original_session_id=raw["original_session_id"],
            original_generation=raw["original_generation"],
            payload_sha256=raw["payload_sha256"],
            state="UNKNOWN",
        )


class NativeDispatchStore:
    """Durable no-retry claims for nested native sends."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "native-dispatches.json"

    def dispatches(self) -> list[NativeDispatch]:
        if not self.path.exists():
            return []
        require_private_file(self.path)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ChatError("native dispatch state is invalid") from error
        if not isinstance(raw, list):
            raise ChatError("native dispatch state is invalid")
        return [NativeDispatch.from_dict(item) for item in raw]

    def claim(self, helper: Route, original: Route, event_id: str, message: str) -> NativeDispatch:
        """Record the possible native effect before its hook executes."""

        dispatch = NativeDispatch(
            event_id=event_id,
            helper_session_id=helper.session_id,
            helper_generation=helper.generation,
            original_session_id=original.session_id,
            original_generation=original.generation,
            payload_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            state="UNKNOWN",
        )
        with state_lock(self.root, "native-dispatches"):
            existing = self.dispatches()
            if any(item.event_id == dispatch.event_id for item in existing):
                raise ChatError("native dispatch is unavailable")
            atomic_json(self.path, [item.to_dict() for item in [*existing, dispatch]])
        return dispatch

    def has_event(self, event_id: str) -> bool:
        """Whether a possible native effect has already been durably claimed."""

        identifier = valid_uuid(event_id, "event id")
        return any(item.event_id == identifier for item in self.dispatches())
