"""Cross Agent Chat command line and hidden provider entrypoints."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn, cast

from cross_agent_chat import __version__
from cross_agent_chat.core import ChatError, IntentStore
from cross_agent_chat.install import (
    Installer,
    SettingsError,
    default_device,
    discover_executable,
)
from cross_agent_chat.mcp_server import normalize_send_arguments
from cross_agent_chat.runtime import (
    authenticate_mcp_sender,
    codex_stop,
    courier_server,
    peers,
    presence_is_enabled,
    register,
    send,
    state_root,
    unregister,
)
from cross_agent_chat.tailnet import local_tailnet_address
from cross_agent_chat.tailnet_broker import broker_server


def _fail(message: str) -> NoReturn:
    raise ChatError(message)


def _installer(device: str | None) -> Installer:
    return Installer(
        home=Path.home(),
        executable=discover_executable(Path(sys.argv[0])),
        device=default_device() if device is None else device,
        tailnet_address=local_tailnet_address(),
    )


def _mcp_response(
    identifier: object, *, result: object | None = None, error: tuple[int, str] | None = None
) -> None:
    payload: dict[str, object] = {"jsonrpc": "2.0", "id": identifier}
    if error is None:
        payload["result"] = result
    else:
        payload["error"] = {"code": error[0], "message": error[1]}
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def mcp(provider: str, device: str, state_root_value: str | None) -> None:
    presence_enabled = presence_is_enabled()
    root = state_root(state_root_value) if presence_enabled else None
    for line in sys.stdin:
        if len(line.encode()) > 65536:
            _mcp_response(None, error=(-32700, "request exceeds the bounded limit"))
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _mcp_response(None, error=(-32700, "parse error"))
            continue
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            _mcp_response(None, error=(-32600, "invalid request"))
            continue
        identifier = request.get("id")
        method = request.get("method")
        params = request.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            _mcp_response(identifier, error=(-32602, "invalid params"))
            continue
        typed_params = cast(dict[str, object], params)
        try:
            if method == "notifications/initialized":
                continue
            if method == "initialize":
                _mcp_response(
                    identifier,
                    result={
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "cross-agent-chat", "version": __version__},
                    },
                )
            elif method == "tools/list":
                _mcp_response(
                    identifier,
                    result={
                        "tools": []
                        if not presence_enabled
                        else [
                            {
                                "name": "chat_peers",
                                "description": (
                                    "List exact live Claude and Codex peers before addressing one."
                                ),
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {},
                                    "additionalProperties": False,
                                },
                            },
                            {
                                "name": "chat_send",
                                "description": (
                                    "Send one asynchronous message. A reply is a separate send."
                                ),
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "to": {"type": "string"},
                                        "recipient": {"type": "string"},
                                        "destination": {"type": "string"},
                                        "message": {"type": "string"},
                                        "wait_for_reply": {"type": "boolean", "const": False},
                                        "request_reply": {"type": "boolean", "const": False},
                                    },
                                    "additionalProperties": False,
                                },
                            },
                        ]
                    },
                )
            elif method == "tools/call":
                if not presence_enabled:
                    _fail("Cross Agent Chat presence is disabled")
                name = typed_params.get("name")
                arguments = typed_params.get("arguments", {})
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    _fail("MCP tool call is invalid")
                typed_arguments = cast(dict[str, object], arguments)
                if name == "chat_peers" and not typed_arguments:
                    assert root is not None
                    result = peers(root)
                elif name == "chat_send":
                    target, message = normalize_send_arguments(typed_arguments)
                    metadata = typed_params.get("_meta")
                    thread_id: str | None = None
                    if provider == "codex":
                        if not isinstance(metadata, dict):
                            _fail("Codex host thread identity is required")
                        raw_thread = metadata.get("threadId")
                        if not isinstance(raw_thread, str):
                            _fail("Codex host thread identity is required")
                        thread_id = raw_thread
                    assert root is not None
                    source = authenticate_mcp_sender(root, provider, os.getppid(), thread_id)
                    result = send(root, source, target, message)
                else:
                    _fail("MCP tool call is invalid")
                _mcp_response(
                    identifier,
                    result={
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result, sort_keys=True, separators=(",", ":")),
                            }
                        ]
                    },
                )
            else:
                _mcp_response(identifier, error=(-32601, "method not found"))
        except ChatError as error:
            _mcp_response(identifier, error=(-32602, str(error)))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cross-agent-chat")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="install and verify native provider integrations")
    setup.add_argument("--device")
    doctor = commands.add_parser("doctor", help="verify installed integrations")
    doctor.add_argument("--device")
    doctor.add_argument("--json", action="store_true")
    uninstall = commands.add_parser("uninstall", help="remove only owned integrations")
    uninstall.add_argument("--device")
    peers_parser = commands.add_parser("peers", help="list exact available live sessions")
    peers_parser.add_argument("--local-only", action="store_true")
    peers_parser.add_argument("--json", action="store_true")
    resolve_parser = commands.add_parser(
        "resolve",
        help="acknowledge one confirmed unknown event so a later fresh send is allowed",
    )
    resolve_parser.add_argument("event_id")

    register_parser = commands.add_parser("_register")
    register_parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    register_parser.add_argument("--device", required=True)
    register_parser.add_argument("--pid", type=int, required=True)
    register_parser.add_argument("--state-root")
    unregister_parser = commands.add_parser("_unregister")
    unregister_parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    unregister_parser.add_argument("--pid", type=int, required=True)
    unregister_parser.add_argument("--state-root")
    stop_parser = commands.add_parser("_codex-stop")
    stop_parser.add_argument("--pid", type=int, required=True)
    stop_parser.add_argument("--state-root")
    courier = commands.add_parser("_courier")
    courier.add_argument("--provider", choices=("claude", "codex"), required=True)
    courier.add_argument("--state-root", required=True)
    courier.add_argument("--session-id", required=True)
    courier.add_argument("--cwd", required=True)
    courier.add_argument("--generation", required=True)
    courier.add_argument("--pid", type=int, required=True)
    broker = commands.add_parser("_broker")
    broker.add_argument("--state-root")
    mcp_parser = commands.add_parser("_mcp")
    mcp_parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    mcp_parser.add_argument("--device", required=True)
    mcp_parser.add_argument("--state-root")
    pretool = commands.add_parser("_pretool")
    pretool.add_argument("--expected", required=True)
    pretool.add_argument("--content-hmac-key", required=True)
    staged_install = commands.add_parser("_install-staged")
    staged_install.add_argument("--staged-runtime", type=Path, required=True)
    staged_install.add_argument("--stable-entrypoint", type=Path, required=True)
    staged_install.add_argument("--device")
    return root


def run(arguments: argparse.Namespace) -> int:
    command = cast(str, arguments.command)
    if command == "setup":
        _installer(arguments.device).install()
        print(
            f"Cross Agent Chat is ready on {arguments.device or default_device()}. "
            "Start fresh Claude/Codex sessions."
        )
    elif command == "doctor":
        installer = _installer(arguments.device)
        integration_healthy = installer.verify_configuration()
        broker_healthy = installer.broker_is_healthy()
        healthy = integration_healthy and broker_healthy
        doctor_result = {
            "version": __version__,
            "integration": "healthy" if integration_healthy else "needs setup",
            "local_broker": "healthy" if broker_healthy else "unavailable",
            "remote_trust": "tailscale_acl",
            "next": "start fresh Claude/Codex sessions" if healthy else "cross-agent-chat setup",
        }
        print(
            json.dumps(doctor_result, sort_keys=True)
            if arguments.json
            else "\n".join(f"{k}: {v}" for k, v in doctor_result.items())
        )
        return 0 if healthy else 1
    elif command == "uninstall":
        _installer(arguments.device).uninstall()
        print("Removed Cross Agent Chat-owned provider and background configuration.")
    elif command == "peers":
        peer_result = peers(state_root(), include_remote=not arguments.local_only)
        if arguments.json:
            print(json.dumps(peer_result, sort_keys=True))
        else:
            for peer_item in cast(list[dict[str, str]], peer_result["peers"]):
                print(f"{peer_item['alias']}\t{peer_item['status']}")
    elif command == "resolve":
        IntentStore(state_root()).mark(arguments.event_id, "RESOLVED_BY_OWNER")
        print(f"Resolved event {arguments.event_id}. A later fresh send is now allowed.")
    elif command == "_register":
        register(arguments.provider, arguments.device, arguments.pid, arguments.state_root)
    elif command == "_unregister":
        unregister(arguments.provider, arguments.pid, arguments.state_root)
    elif command == "_codex-stop":
        codex_stop(arguments.pid, arguments.state_root)
    elif command == "_courier":
        courier_server(
            provider=arguments.provider,
            state_root_value=arguments.state_root,
            session_id=arguments.session_id,
            cwd=arguments.cwd,
            generation=arguments.generation,
            pid=arguments.pid,
        )
    elif command == "_broker":
        broker_server(arguments.state_root)
    elif command == "_mcp":
        mcp(arguments.provider, arguments.device, arguments.state_root)
    elif command == "_pretool":
        from cross_agent_chat.claude_runtime import run_pretool_gate

        return 0 if run_pretool_gate(arguments.expected, arguments.content_hmac_key) else 2
    elif command == "_install-staged":
        device = arguments.device or default_device()
        installer = Installer(
            home=Path.home(),
            executable=arguments.stable_entrypoint,
            device=device,
            tailnet_address=local_tailnet_address(),
        )
        installer.install_staged(arguments.staged_runtime, arguments.stable_entrypoint)
        print(f"Cross Agent Chat is ready on {device}. Start fresh Claude/Codex sessions.")
    else:
        _fail("unsupported command")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except (ChatError, SettingsError, OSError) as error:
        print(f"cross-agent-chat: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
