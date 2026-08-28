"""Claude Code live-session discovery and one-shot SendMessage courier."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Final, TypedDict, cast

from cross_agent_chat.core import ChatError, bounded_message, canonical_cwd, valid_name, valid_uuid

AGENTS_TIMEOUT_SECONDS: Final = 15.0
DISCOVERY_TIMEOUT_SECONDS: Final = 30.0
SEND_TIMEOUT_SECONDS: Final = 90.0
COURIER_ENV_KEYS: Final = (
    "HOME",
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "SHELL",
)
CLAUDE_AUTH_ENV_KEYS: Final = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)
TARGET_REF_RE: Final = re.compile(r"(?P<name>.+) \[(?P<token>[A-Za-z0-9]{6})\]\Z")


class ClaudeAgent(TypedDict):
    session_id: str
    name: str
    kind: str
    cwd: str


def courier_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Retain only the session context required by a Claude delivery courier."""
    environment = os.environ if source is None else source
    return {
        key: environment[key]
        for key in (*COURIER_ENV_KEYS, *CLAUDE_AUTH_ENV_KEYS)
        if key in environment
    }


def _environment() -> dict[str, str]:
    return courier_environment()


def claude_binary() -> Path:
    candidate = shutil.which("claude")
    if candidate is None:
        fallback = Path.home() / ".local" / "bin" / "claude"
        candidate = str(fallback) if fallback.exists() else None
    if candidate is None:
        raise ChatError("Claude Code executable is unavailable")
    try:
        binary = Path(candidate).resolve(strict=True)
    except OSError as error:
        raise ChatError("Claude Code executable is unavailable") from error
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ChatError("Claude Code executable is unavailable")
    return binary


def parse_claude_agents(text: str) -> list[ClaudeAgent]:
    try:
        raw = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChatError("Claude agents response is invalid") from error
    if not isinstance(raw, list):
        raise ChatError("Claude agents response is invalid")
    agents: list[ClaudeAgent] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ChatError("Claude agents response is invalid")
        session_id = item.get("sessionId")
        name = item.get("name")
        kind = item.get("kind")
        cwd = item.get("cwd")
        if not all(isinstance(value, str) for value in (session_id, name, kind, cwd)):
            raise ChatError("Claude agents response is invalid")
        agents.append(
            {
                "session_id": valid_uuid(cast(str, session_id), "Claude session id"),
                "name": valid_name(cast(str, name), "Claude session name"),
                "kind": cast(str, kind),
                "cwd": canonical_cwd(cast(str, cwd)),
            }
        )
    return agents


def claude_agents() -> list[ClaudeAgent]:
    try:
        completed = subprocess.run(
            [str(claude_binary()), "agents", "--json"],
            cwd="/var/empty",
            env=_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=AGENTS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ChatError("Claude agents preflight failed") from error
    if completed.returncode != 0:
        raise ChatError("Claude agents preflight failed")
    return parse_claude_agents(completed.stdout)


def exact_agent(session_id: str, cwd: str) -> ClaudeAgent:
    matches = [
        agent
        for agent in claude_agents()
        if agent["session_id"] == session_id
        and agent["kind"] == "interactive"
        and agent["cwd"] == cwd
    ]
    if len(matches) != 1:
        raise ChatError("Claude target is not one exact live interactive session")
    return matches[0]


def claude_alias(device: str, project: str, agent: ClaudeAgent) -> str:
    return valid_name(f"claude@{device}:{project}:{agent['name']}", "Claude alias")


def discover_target_ref(session_name: str) -> str:
    command = [
        str(claude_binary()),
        "--safe-mode",
        "-p",
        "--model",
        "haiku",
        "--system-prompt",
        "Use ListAgents exactly once and stop.",
        "--tools",
        "ListAgents",
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd="/var/empty",
            env=_environment(),
            input="Use ListAgents exactly once.",
            capture_output=True,
            text=True,
            timeout=DISCOVERY_TIMEOUT_SECONDS,
            check=False,
        )
        records = [json.loads(line) for line in completed.stdout.splitlines() if line]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ChatError("Claude ListAgents discovery failed") from error
    listings: list[str] = []
    for record in records:
        result = record.get("tool_use_result") if isinstance(record, dict) else None
        listing = result.get("listing") if isinstance(result, dict) else None
        if isinstance(listing, str) and len(listing.encode()) <= 128 * 1024:
            listings.append(listing)
    if completed.returncode != 0 or len(listings) != 1:
        raise ChatError("Claude ListAgents discovery failed")
    pattern = re.compile(
        r"^  (?P<name>.+?) \[(?P<token>[A-Za-z0-9]{6})\]  ·  interactive(?:  ·  [^\r\n]+)*$"
    )
    refs = [
        f"{match.group('name')} [{match.group('token')}]"
        for line in listings[0].splitlines()
        if (match := pattern.fullmatch(line)) and match.group("name") == session_name
    ]
    if len(refs) != 1 or TARGET_REF_RE.fullmatch(refs[0]) is None:
        raise ChatError("Claude target discovery is not one exact interactive match")
    return refs[0]


def content_mirror(message: str) -> str:
    return message if len(message) <= 50 else message[:49] + "…"


def pretool_decision(expected: dict[str, object], payload: object, content_hmac_key: str) -> bool:
    if set(expected) != {"recipient", "message_hmac"}:
        return False
    recipient = expected.get("recipient")
    digest = expected.get("message_hmac")
    if (
        not isinstance(recipient, str)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", content_hmac_key) is None
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(payload, dict)
        or payload.get("hook_event_name") != "PreToolUse"
        or payload.get("tool_name") != "SendMessage"
    ):
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or set(tool_input) != {
        "to",
        "message",
        "recipient",
        "content",
        "type",
        "summary",
    }:
        return False
    message = tool_input.get("message")
    if not isinstance(message, str):
        return False
    actual = hmac.new(bytes.fromhex(content_hmac_key), message.encode(), hashlib.sha256).hexdigest()
    return (
        tool_input.get("to") == recipient
        and tool_input.get("recipient") == recipient
        and hmac.compare_digest(digest, actual)
        and tool_input.get("content") == content_mirror(message)
        and tool_input.get("type") == "message"
        and tool_input.get("summary") == "Cross Agent Chat"
    )


def run_pretool_gate(expected_path: str, content_hmac_key: str) -> bool:
    try:
        path = Path(expected_path)
        if not path.is_absolute() or path.is_symlink():
            raise ChatError("pre-tool expectation is unsafe")
        expected_raw = json.loads(path.read_text(encoding="utf-8"))
        payload_text = sys.stdin.read(65537)
        payload = json.loads(payload_text)
        allowed = (
            isinstance(expected_raw, dict)
            and len(payload_text.encode()) <= 65536
            and pretool_decision(cast(dict[str, object], expected_raw), payload, content_hmac_key)
        )
        if allowed:
            consumed = path.parent / "consumed"
            descriptor = os.open(
                consumed,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(b"consumed\n")
                handle.flush()
                os.fsync(handle.fileno())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ChatError):
        allowed = False
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allowed else "deny",
            "permissionDecisionReason": (
                "Exact Cross Agent Chat action"
                if allowed
                else "Cross Agent Chat rejected a mismatched action"
            ),
        }
    }
    print(json.dumps(output, separators=(",", ":")), flush=True)
    return allowed


def _tool_records(text: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    uses: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line:
            continue
        record = json.loads(line)
        message = record.get("message") if isinstance(record, dict) else None
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                uses.append(cast(dict[str, object], block))
            if isinstance(block, dict) and block.get("type") == "tool_result":
                results.append(cast(dict[str, object], block))
    return uses, results


def parse_sendmessage_receipt(text: str, target_ref: str, message: str) -> str:
    """Validate one exact successful native SendMessage tool receipt."""
    try:
        uses, results = _tool_records(text)
    except json.JSONDecodeError as error:
        raise ChatError("Claude SendMessage receipt is invalid") from error
    if len(uses) != 1 or uses[0].get("name") != "SendMessage":
        raise ChatError("Claude SendMessage receipt is invalid")
    tool_id = uses[0].get("id")
    tool_input = uses[0].get("input")
    matches = [item for item in results if item.get("tool_use_id") == tool_id]
    if (
        not isinstance(tool_id, str)
        or not isinstance(tool_input, dict)
        or tool_input.get("recipient") != target_ref
        or tool_input.get("content") != content_mirror(message)
        or len(matches) != 1
        or matches[0].get("is_error") is True
    ):
        raise ChatError("Claude SendMessage receipt is invalid")
    content = matches[0].get("content")
    if not isinstance(content, list):
        raise ChatError("Claude SendMessage receipt is invalid")
    text_blocks = [
        item
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    if len(text_blocks) != 1:
        raise ChatError("Claude SendMessage receipt is invalid")
    try:
        result = json.loads(cast(str, text_blocks[0]["text"]))
    except json.JSONDecodeError as error:
        raise ChatError("Claude SendMessage receipt is invalid") from error
    if (
        not isinstance(result, dict)
        or set(result) != {"success", "message", "msg_id"}
        or result.get("success") is not True
        or not isinstance(result.get("message"), str)
        or not isinstance(result.get("msg_id"), str)
    ):
        raise ChatError("Claude SendMessage receipt is invalid")
    return valid_uuid(cast(str, result["msg_id"]), "courier message id")


def sendmessage(target_ref: str, message: str, executable: Path) -> None:
    bounded_message(message)
    if TARGET_REF_RE.fullmatch(target_ref) is None:
        raise ChatError("Claude target reference is invalid")
    key = secrets.token_hex(32)
    expected = {
        "recipient": target_ref,
        "message_hmac": hmac.new(bytes.fromhex(key), message.encode(), hashlib.sha256).hexdigest(),
    }
    prompt = (
        "Use SendMessage exactly once with "
        f"recipient {target_ref!r}, content {message!r}, type 'message', and summary "
        "'Cross Agent Chat'. Do not use any other tool. Stop immediately after it returns."
    )
    try:
        with tempfile.TemporaryDirectory(prefix="cross-agent-chat-gate.", dir="/tmp") as temporary:
            gate = Path(temporary)
            gate.chmod(0o700)
            expected_path = gate / "expected.json"
            expected_path.write_text(json.dumps(expected, separators=(",", ":")), encoding="utf-8")
            expected_path.chmod(0o600)
            hook = " ".join(
                shlex.quote(part)
                for part in (
                    str(executable),
                    "_pretool",
                    "--expected",
                    str(expected_path),
                    "--content-hmac-key",
                    key,
                )
            )
            settings = {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "SendMessage",
                            "hooks": [{"type": "command", "command": hook, "timeout": 10}],
                        }
                    ]
                }
            }
            command = [
                str(claude_binary()),
                "-p",
                "--model",
                "haiku",
                "--system-prompt",
                "You are a deterministic cross-session courier. Use only the named tool.",
                "--tools",
                "SendMessage",
                "--allowed-tools",
                "SendMessage",
                "--permission-mode",
                "bypassPermissions",
                "--settings",
                json.dumps(settings, sort_keys=True, separators=(",", ":")),
                "--setting-sources",
                "local",
                "--disable-slash-commands",
                "--no-chrome",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--no-session-persistence",
                "--output-format",
                "stream-json",
                "--include-hook-events",
                "--verbose",
            ]
            completed = subprocess.run(
                command,
                cwd="/var/empty",
                env=_environment(),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=SEND_TIMEOUT_SECONDS,
                check=False,
            )
            if (gate / "consumed").read_bytes() != b"consumed\n":
                raise ChatError("Claude one-shot gate was not consumed")
        message_id = parse_sendmessage_receipt(completed.stdout, target_ref, message)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise ChatError("Claude SendMessage courier failed") from error
    if completed.returncode != 0:
        raise ChatError("Claude SendMessage courier failed")
    valid_uuid(message_id, "courier message id")
