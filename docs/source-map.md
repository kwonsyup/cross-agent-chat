# Source map

One page to find the code behind each behavior. Runtime code lives in
`src/cross_agent_chat/`; the public entry point is the `cross-agent-chat`
console script (`cli:main`).

## Request path

```text
provider session start/prompt hooks
  → _register / _devin-prompt          (cli.py → runtime.py, devin.py)
  → route + generation persisted        (core.py)

chat_peers / chat_send / chat_status    (MCP stdio: mcp_server.py session +
                                        cli.py tool dispatch)
  → sender authentication               (runtime.py, devin.py capability)
  → exact destination resolution        (recipient.py tokens + runtime.py
                                         re-attestation)
  → local courier or remote broker      (runtime.py → tailnet_broker.py)
  → recipient-local delivery            (claude_runtime.py, codex.py,
                                         native_helper.py, devin.py)

install/setup/uninstall                 (install.sh → cli.py _install-staged
                                         → install.py transactions)
```

## Modules

| Module | Responsibility |
|---|---|
| `__init__.py` | Runtime `__version__`, kept consistent with package and release metadata. |
| `cli.py` | Argument parsing, public commands (`setup`, `doctor`, `peers`, `resolve`, `uninstall`), hidden provider hook/service entrypoints (`_`-prefixed), and per-tool MCP dispatch including tool schemas. |
| `mcp_server.py` | The stdio MCP surface itself: bounded frame reading, JSON-RPC batch handling, strict initialize lifecycle, request-ID validation, ping, and `chat_send` argument normalization. |
| `recipient.py` | Versioned opaque recipient endpoint tokens (`cac2.`): minting and strict parsing. A remote token pins session key + route generation to a stable Tailnet node; a local token pins them to the issuing state root. |
| `core.py` | Route identity, content-free intent records, validation, private atomic persistence, state locks. This is where durable product state is defined. |
| `runtime.py` | Hook registration, sender authentication, peer discovery, token minting during listing and re-attestation during send, local couriers, socket framing/transport, Codex native queue plumbing, reply-readiness reporting. Largest module; several responsibilities share it. |
| `tailnet.py` | Tailscale IPv4 discovery/validation and port constants (`47071` product, `47072` local health). |
| `tailnet_broker.py` | Owner-local broker: listener, admission, per-request authorization dispatch, refusal lane. |
| `remote.py` / `transport.py` | Strict parsing and serialization of the trusted Tailnet envelope (`remote` parses inbound, `transport` builds outbound). |
| `claude_runtime.py` | Claude Code discovery, constrained helper couriers, the exact argument-supply delivery gate, transient body file, receipt classification. |
| `codex.py` | Codex CLI process-memory courier and Stop-bound handoff, stdio app-server metadata and experimental queue operations. |
| `native_helper.py` | Shared provider hook recipes/defaults and the private, body-free binding between original Codex conversations and managed native helpers. |
| `devin.py` | Devin lifecycle-hook parsing and the filesystem-backed single-use capability store (`atomic_json` + `state_lock`). |
| `install.py` | Provider selection (`resolve_providers`), selected-root resolution, read-only `SetupPlan`, config payload preparation, whole-file backups, guarded transactions/rollback, LaunchAgent lifecycle, schema-5 install metadata, uninstall/restore. Largest file; most of its size is the ownership/rollback matrix. |

## Where the boundaries live

- **Sender identity:** `runtime.py` (`authenticate_mcp_sender`), Codex host
  `threadId` `_meta` in `cli.py:mcp`, Devin capability issuance/consumption in
  `devin.py`.
- **Exact recipient selection:** `recipient.py` mints/parses endpoint tokens;
  `runtime.py` re-attests session key + generation + presenting endpoint at
  send time. Raw pre-upgrade handles, stale generations, ambiguous names, and
  name selection against an incomplete roster refuse before effect; an exact
  token needs only its own endpoint to answer. There is no binding store —
  tokens are self-contained.
- **Durable effects:** `core.py` intent store — event IDs, digests, statuses;
  `resolve_by_owner` is the only owner disposition and never proves delivery.
- **Owned configuration writes:** `install.py:_payloads` and the transaction
  layer around it; backups in `_backup` under `~/.cache/cross-agent-chat/`.
- **Message bodies:** transient gate file (`claude_runtime.py`), courier
  memory (`codex.py`), provider queue/transcript (experimental path) — never
  durable product state.

## Tests

| Area | Files |
|---|---|
| State/identity/recipient tokens | `test_core.py`, `test_recipient_binding.py`, `test_issue9_regressions.py` |
| MCP protocol/tools | `test_mcp.py`, `test_mcp_protocol.py`, `test_native_helper_mcp.py` |
| Broker/transport | `test_broker_admission.py`, `test_broker_peek.py`, `test_tailnet.py`, `test_transport.py` |
| Providers | `test_claude_remote.py`, `test_codex.py`, `test_devin.py`, `test_native_*.py` |
| Installer | `test_install.py` |
| Misc | `conftest.py` (containment boundary), `bench_intent_history.py` |

Fixtures monkeypatch module attributes by name; renaming a module or moving a
function can silently change what a guard intercepts. Check `conftest.py` and
the matching `test_*` imports before reorganizing.
