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

external-call / external-mcp             (cli.py one-shot/stdio entrypoints)
  → owner-enrolled credential            (external.py; separate private state)
  → exact allowed cac2 peer token         (runtime.py shared send path)
  → local courier or remote broker        (runtime.py → tailnet_broker.py)
  ← optional owner-configured HTTPS callback (external_callback.py; custody only)

install/setup/uninstall                 (install.sh → cli.py _install-staged
                                         → install.py transactions)
```

## Modules

| Module | Responsibility |
|---|---|
| `__init__.py` | Runtime `__version__`, kept consistent with package and release metadata. |
| `cli.py` | Argument parsing, public commands (`setup`, `doctor`, `peers`, `resolve`, `uninstall`), owner-managed `external enroll/revoke/rotate/configure-callback`, one-shot `external-call`, stdio `external-mcp`, hidden provider hook/service entrypoints (`_`-prefixed), and MCP tool dispatch. |
| `mcp_server.py` | The stdio MCP surface itself: bounded frame reading, JSON-RPC batch handling, strict initialize lifecycle, request-ID validation, ping, and `chat_send` argument normalization. |
| `recipient.py` | Versioned opaque recipient endpoint tokens (`cac2.`): minting and strict parsing. A remote token pins session key + route generation to a stable Tailnet node; a local token pins them to the issuing state root. |
| `core.py` | Route identity, content-free intent records, validation, private atomic persistence, state locks, and the private per-generation owner image anchor sidecar (`owner-<generation>.json`). This is where durable product state is defined. |
| `external.py` | Owner-enrolled endpoint IDs, credential verifiers, generation, exact recipient scope, revocation/rotation, and private callback references. Revocation removes CAC's callback copy, not the owner's credential/config source files. The context label is not provider attestation; these records live outside the native route registry. |
| `external_callback.py` | Owner-configured bounded HTTPS POST with hostname/TLS checks, no redirects, and content-free outcome. A busy endpoint is an explicit pre-effect refusal; a 2xx is transport custody only, not model receipt, wake, or completion. |
| `runtime.py` | Hook registration, native and owner-enrolled sender authentication, peer discovery, token minting during listing and re-attestation during send, local/external recipients, local couriers (bounded per-connection worker seats; one serialized effect at a time, with admission fenced against shutdown acknowledgement and route rotation), socket framing/transport, Codex native queue plumbing, owner image anchors (built at registration; read by the anchored owner checks), reply-readiness reporting. Largest module; several responsibilities share it. |
| `tailnet.py` | Tailscale IPv4 discovery/validation and port constants (`47071` product, `47072` local health). |
| `tailnet_broker.py` | Owner-local broker: listener, admission, per-request authorization dispatch, refusal lane. |
| `remote.py` / `transport.py` | Strict parsing and serialization of the trusted Tailnet envelope (`remote` parses inbound, `transport` builds outbound). |
| `claude_runtime.py` | Claude Code discovery, constrained helper couriers, the exact argument-supply delivery gate, transient body file, receipt classification. |
| `codex.py` | Codex CLI process-memory courier and Stop-bound handoff, stdio app-server metadata and experimental queue operations. |
| `native_helper.py` | Shared provider hook recipes/defaults and the private, body-free binding between original Codex conversations and managed native helpers. |
| `devin.py` | Devin lifecycle-hook parsing and the filesystem-backed single-use capability store (`atomic_json` + `state_lock`). |
| `install.py` | Provider selection (`resolve_providers`), selected-root resolution, read-only `SetupPlan`, config payload preparation, whole-file backups, guarded transactions/rollback, LaunchAgent lifecycle, schema-5 install metadata, uninstall/restore. Largest file; most of its size is the ownership/rollback matrix. |

Native eligibility is checked in `runtime.py`: `_native_desktop_client`
recognizes only the two known bundled Codex executable layouts and verifies
the matching Desktop ancestor. `_native_account_binary` selects that exact
owner's client. Queue admission is in `codex.py`; managed helper forwarding
uses the trusted hook recipe in `native_helper.py`. None of these labels
alone proves that an active recipient consumed an envelope.

## Where the boundaries live

- **Sender identity:** `runtime.py` (`authenticate_mcp_sender`), Codex host
  `threadId` `_meta` in `cli.py:mcp`, Devin capability issuance/consumption in
  `devin.py`. External identity is authenticated from an owner-created
  credential in `external.py`; it is not provider- or Bot-attested, and a
  shared credential is one endpoint identity across every client that holds it.
- **External access and scope:** `cli.py` (`external-call`, `external-mcp`,
  and `external` management commands), `external.py` (credential verification,
  exact allowed `cac2.` recipient set, generation changes, revocation). The
  request-id guard returns prior custody only after the exact target/content
  tuple is verified; if the target is unavailable, it refuses without a new
  effect and directs the caller to `chat_status`, never a new recipient. The
  endpoint file `external-endpoints-v1.json` is separate from `routes.json`,
  so old native readers ignore it; older loaded sessions do not gain the new
  commands. A binary rollback does not itself revoke endpoint credentials.
- **External callback:** `runtime.py:_accept_external` validates the selected
  endpoint/generation and uses `external_callback.py` to POST to its private
  owner-configured HTTPS destination. HTTP 2xx means callback custody only;
  original-context receiving and model consumption require separate evidence.
- **Owner image anchor (update continuity):** `runtime.py`
  `_owner_anchor_document` builds the private per-generation sidecar
  `owner-<generation>.json` in the state root at registration, only when the
  observed image vnode is the resolved executable; `core.py`
  `Registry._publish_owner_anchor` writes it inside the routes lock before the
  route is published. Anchored readers (`_route_owner_current`,
  `_anchored_owner_current`, `_reusable_anchored_route`) validate the bound
  image device/inode and exec generation rather than the executable path;
  `Registry._prune_owner_anchors` drops anchors whose generation no route
  still owns. A route without an anchor keeps the legacy path-based owner
  check, and a malformed, foreign, or non-private anchor fails closed. The
  sidecar lives outside `routes.json` and the Route schema is unchanged, so
  older readers ignore it; continuity is best-effort and applies only to
  routes whose anchor was created.
- **Exact recipient selection:** `recipient.py` mints/parses endpoint tokens;
  `runtime.py` re-attests session key + generation + presenting endpoint at
  send time. Raw pre-upgrade handles, stale generations, ambiguous names, and
  name selection against an incomplete roster refuse before effect; an exact
  token needs only its own endpoint to answer. There is no binding store —
  tokens are self-contained.
- **Durable effects:** `core.py` intent store — event IDs, digests, statuses;
  `resolve_by_owner` is the only owner disposition and never proves delivery
  or makes resending equivalent uncertain work safe.
- **Owned configuration writes:** `install.py:_payloads` and the transaction
  layer around it; backups in `_backup` under `~/.cache/cross-agent-chat/`.
- **Message bodies:** transient gate file (`claude_runtime.py`), courier
  memory (`codex.py`), provider queue/transcript (experimental path) — never
  durable product state.

## Tests

| Area | Files |
|---|---|
| State/identity/recipient tokens | `test_core.py`, `test_recipient_binding.py`, `test_recipient_selection.py`, `test_issue9_regressions.py` |
| Experimental external endpoints | `test_external.py` |
| Owner anchor / update continuity | `test_owner_anchor.py` |
| MCP protocol/tools | `test_mcp.py`, `test_mcp_protocol.py`, `test_native_helper_mcp.py` |
| Broker/transport/courier seats | `test_broker_admission.py`, `test_broker_peek.py`, `test_courier_liveness.py`, `test_tailnet.py`, `test_transport.py`, `test_transport_deadlines.py` |
| Providers | `test_claude_remote.py`, `test_claude_runtime.py`, `test_claude_registration_cwd.py`, `test_codex.py`, `test_devin.py`, `test_native_*.py`, `test_sender_preflight_retry.py` |
| Installer | `test_install.py`, `test_install_selection.py` |
| Misc | `conftest.py` (containment boundary), `bench_intent_history.py` |

Fixtures monkeypatch module attributes by name; renaming a module or moving a
function can silently change what a guard intercepts. Check `conftest.py` and
the matching `test_*` imports before reorganizing.
