# Changelog

## Unreleased

- A session whose courier died and left its socket behind now recovers at
  its next registration: the courier's lifetime lock proves the courier is
  gone and records the exact socket it guarded, so only that stale socket
  is reclaimed before a fresh courier takes over the same route generation.
- A courier that briefly refuses connections because its listener backlog
  is full keeps its socket and its route generation: re-registration no
  longer unlinks its socket or mints a replacement generation while its
  lock is held.
- Couriers started before this version hold no lifetime lock, so their
  leftover sockets are not reclaimed automatically; restart the session.
  The same applies to a courier killed during its own startup, before it
  could record the socket it bound.

## 0.4.4 - 2026-09-24

- A Claude Code session keeps Cross Agent Chat in both directions when an
  npm-installed Claude Code updates itself while the session runs (issue
  #39). At registration CAC also records a private, path-independent
  identity of the exact executable image and process incarnation; after an
  update deletes the old files the same session stays current,
  re-registers onto its existing route, and its courier keeps running.
- A process that switched to a different executable image, a reused
  process ID, a different user/profile/provider, or a fresh process whose
  executable path is missing still gets no identity.
- Sessions started before 0.4.4 is installed keep the earlier path-based
  check and need a restart after such an update. No route schema change;
  downgrading to 0.4.3 ignores the new identity files.
- A courier whose recorded Claude executable path disappeared uses the
  current `claude` found on PATH.
- README first-use corrections: provider selection in one command, guarded
  rollback, idle-capable first-use pairing, per-attempt refusal, delivery
  modes, profile selection, and a provider-update note.

## 0.4.3 - 2026-09-23

- Fresh sessions register reliably on busy Macs. A session's courier now
  gets up to 15 seconds to start, as long as it is still running. Claude
  and Devin registration hooks get 30 seconds. A courier that exits still
  fails at once, and the identity checks are unchanged. Codex hooks are
  unchanged, so existing Codex hook trust carries over.
- Provider hooks and couriers start faster. They no longer load the
  installer module, which cuts Cross Agent Chat's import time by about 30%.
- A remote refusal now carries the recipient's reason for its known
  refusal causes, such as "Claude ListAgents discovery timed out", in the
  form "remote target rejected the message before provider effect:
  <reason>; nothing was delivered". Other causes keep the generic reason.
- Hook and courier errors are reported without loading the installer
  module.
- The README is rewritten around first use. It shows each collaborating
  session getting its job in its own first message, and it lists delivery
  results and troubleshooting.
- The agent instructions now separate the code-loading rule from pre-v0.4.0
  reply handles. They tell a session that has been assigned to answer a
  peer to do that work within its task.
- Cross-Mac collaboration is verified end to end: a fresh Claude session on
  one Mac asked a fresh Claude session on another Mac for a source review
  over the Tailnet. The answer came back into the original conversation
  with no manual step.

## 0.4.2 - 2026-09-22

- The sender's read-only `claude agents` session-inventory preflight now
  classifies failures instead of collapsing them — a typed timeout for the
  subprocess bound, an errno/exception category for spawn failures, and the
  exit status for nonzero exits — never provider stderr, env, or config.
- A typed inventory timeout may be retried exactly once inside the caller's
  unchanged absolute operation deadline, each attempt capped by the remaining
  budget; a sub-floor remainder fails closed. Every other failure stays
  decided: nonzero exit, spawn, malformed, authentication, ambiguous session,
  and wrong-profile errors are never retried, and no possible-effect path
  (`courier_accept`, `sendmessage`, `request_tailnet`, or any accepted or
  unknown send) gains a retry.
- After a slow or retried inventory, both the source route generation and the
  local recipient snapshot are revalidated against fresh registry state
  before an intent is created or a socket is opened, so a superseded target
  cannot receive an intent after replacement.
- README first-use path reorganized: material setup effects, consent, and
  backups precede the install command; the pre-v0.4.0 token break is
  separated from the code-loading rule; a per-failure cause/next-action list
  keeps `doctor` scoped to configuration and broker health.

## 0.4.1 - 2026-09-21

- Preserve the route generation of a live Claude session when a repeated
  SessionStart hook reports a different working directory. A responsive courier
  and a bounded native-session lookup distinguish a valid existing route from
  an invalid route that needs same-session recovery. Busy or ambiguous custody
  is retained; existing intent records are never resolved or replayed.

## 0.4.0 - 2026-09-19

- **Breaking:** `chat_peers` returns an opaque endpoint token (`cac2.…`) in the
  `handle` field, and `chat_send` accepts that token or a unique name against
  complete discovery. A token pins the raw session key and route generation to
  the stable Tailnet node (remote) or local state root that presented them and
  re-attests at send time; raw pre-upgrade handles and stale-generation tokens
  are refused before any effect. There is no binding store, raw-handle
  fallback, or mutable reply cache. After upgrading on every participating
  Mac, start fresh sender and recipient sessions: fresh-session to
  fresh-session is the supported request/reply scope. Sessions retained from
  before the upgrade may continue with other retained sessions through the
  upgraded broker; mixed pre-upgrade/new pairs are unsupported and an initial
  request can be accepted while its reply token is unusable — never replay an
  accepted or uncertain event.
- The stdio MCP surface now implements its advertised 2025-03-26 contract in
  `mcp_server.py`: a strict initialize → initialized lifecycle before tool
  dispatch, JSON-RPC batch receiving (initialize is never batched), no
  responses to notifications, request-ID validation, `ping`, and bounded
  input framing that drops an oversized or malformed line without dispatching
  its remainder.
- The installer checks macOS/launchctl/Git/runtime prerequisites before any
  write, requires `CROSS_AGENT_CHAT_APPROVE=1` — printing the setup effects
  and exiting without it — and integrates only provider roots that already
  exist, or the subset named by `CROSS_AGENT_CHAT_PROVIDERS`, while retaining
  the provider set recorded by a previous install (schema 5).
  `cross-agent-chat setup` prints a read-only plan of exact roots and
  effects, then requires `--yes` or interactive confirmation before any
  write; a repeated `--provider NAME` selects providers explicitly, and the
  hidden staged path requires `--yes`.
- Public documentation reworked for first use: receiving-mode matrix,
  disclosed setup effects, CONTRIBUTING, a source map, refreshed SECURITY,
  project URLs, and minimal CI permissions with pinned actions.
- Treat a present non-boolean `is_error` flag on a Claude helper result —
  including explicit JSON null — as uncheckable: an otherwise exact success
  receipt or canonical refusal stays an unknown outcome rather than a decided
  one.
- This does not add idle delivery for Devin or Stop-bound Codex.

## 0.3.8 - 2026-09-19

- Resolve an exact recipient handle without asking every Mac for its whole roster. A reply to the
  exact handle on a received envelope was refused before sending ("remote peer discovery is
  incomplete") when the owning Mac was busy: its broker health-checked every local session before
  answering and missed the sender's budget. A sender now asks each broker only about that one
  handle; the owning broker validates that one session exactly as before, and every other broker
  answers at once. Older brokers reject the new question immediately and are asked the old way.
- Stop waiting on unrelated Macs once the owner of an exact handle has answered. Other nodes get
  two more seconds to claim the same handle, which still refuses the send; after that discovery is
  reported incomplete and delivery proceeds to the single attested owner. An unresponsive neighbor
  previously held such a send for up to about 22 seconds.
- Keep reverse authorization moving under reciprocal traffic. With two seats per peer, several
  simultaneous sends in both directions filled each broker with deliveries waiting on callbacks
  that needed the same seats, and seated sends ended as `UNKNOWN_DELIVERY`. Authorization
  callbacks now have their own small bounded lane, and a connection the broker cannot admit is
  told so before any request byte is read while the bounded refusal lane has capacity: the
  sender records a decided `PRE_EFFECT_REJECTED` ("recipient broker is at capacity; nothing was
  delivered; send again") instead of a frozen `UNKNOWN_DELIVERY`. When that lane is also
  exhausted the broker closes silently as before and the outcome stays uncertain, as it does for
  senders older than 0.3.8.
- Recognize the Codex Native app by the running process's own bundle instead of a fixed
  `/Applications/ChatGPT.app` path: `/Applications`, `~/Applications`, or one folder below
  `/Applications`, with the helper child and its Desktop ancestor in the same bundle and a
  matching bundle identifier.
- Pin the Claude session-listing row format and the exact `SendMessage` receipt contract with
  contract tests (namesakes, malformed neighbors, extra columns, every extra receipt key). The
  receipt stays exact on purpose: it is the only evidence of delivery, and an unknown field may
  contradict it. Claude Code 2.1.278 exposes no structured cross-session target reference.
- `doctor` reports a `terminal` line when its environment carries an inherited Claude
  child-session marker. A terminal app launched from inside a Claude session (for example with
  `open -n`) passes the marker on, and every Claude session started there is a hidden child that
  never appears as a peer. Relaunching the terminal app normally clears it.
- `chat_peers` can name the exact local delivery mechanism behind a mode (`native_helper`,
  `direct_queue`, `stop_bound`, `claude_native`, `devin_prompt_bound`). It is local only, sent only
  when asked for, and never travels between Macs.
- Native helper defaults and hook constants live in one place; the app-server client reports the
  package version.
- This release does not add idle delivery for Devin or Stop-bound Codex.

## 0.3.7 - 2026-09-18

- Report how a requested answer returns to the sender. A `chat_send` result now carries
  `reply_delivery`: `while_idle` for Claude and the experimental Codex queue, `next_turn` for
  Stop-bound Codex and Devin, and `unknown` when the sender's courier cannot say. The guidance
  tells every sender to finish its turn instead of sleeping or polling `chat_status`, and a
  `next_turn` sender to tell its user the answer will appear after their next message. A fresh
  v0.3.6 Codex CLI requester had held its turn open for about eleven minutes. An unconditional
  "finish your turn" was withdrawn earlier because it would have stranded Stop-bound answers
  silently.
- This release does not add idle delivery for Stop-bound Codex or Devin; it names the limitation.
- README: state the observed Claude cross-device scope, the known-peer discovery delay, and why the
  integrations do not depend on the terminal emulator.

## 0.3.6 - 2026-09-18

- Supply the Claude courier's `SendMessage` arguments from the delivery gate instead of asking a
  helper model to reproduce an already-decided target and body. The courier now receives only
  unresolvable placeholders, so a byte of model drift can no longer produce
  `sendmessage_message_mismatch`, and a gate that fails open sends a placeholder to a session that
  does not exist rather than delivering unverified content. The gate also constrains the proposal's
  key set, so whether the provider replaces or merges the supplied arguments cannot affect what is
  delivered.
- Answer a refused remote authorization with a definite `REFUSED` frame instead of closing the
  connection with no response. A sender's own decided refusal was returning to it as
  `UNKNOWN_DELIVERY`, freezing an event that produced no effect. Older peers compare the frame
  against the exact authorization they expect and already treat a mismatch as a pre-effect
  rejection, so mixed versions degrade to the previous behaviour in both directions.
- Treat a provider answer of exactly `{"success": false, "message": ...}` with no message id as a
  decided non-delivery rather than an unknown outcome. This generalizes one measured instance, an
  unreachable target, to that exact response shape; it is an assumption about the provider, not an
  established fact, and the `live` test exists to re-measure it after a provider upgrade. Any other
  failure shape, including one carrying a message id, stays uncertain.
- Name the Cross Agent Chat delivery helper honestly in a recipient's inbox. The visible sender was
  derived from the courier's working directory, which is why messages appeared to come from
  `empty-NN`. The exact original source and a directly usable reply handle now lead the envelope,
  and the tool guidance no longer asks for a fresh `chat_peers` call before every send.
- Restrict `resolve` to genuinely undecided events, make it idempotent, and refuse an in-flight
  intent that is too young to be treated as an orphan, so resolving one does not open a
  duplicate-delivery window. The eligibility check and the owner disposition are one transition
  under the intent lock, so a result recorded concurrently is refused as decided rather than
  overwritten. Age is a heuristic, not proof that a send is dead: a result recorded after an owner
  disposition replaces it. The output no longer reads as a receipt: it states that nothing was
  contacted, cancelled or confirmed, and claims to unblock a target only when it does.
- Report the real budget when the transport envelope, not the message, crosses the size limit, and
  keep every other rejection reason accurate instead of restating it as a size problem.
- Keep delivered message bodies out of the courier's output stream, and read the gate's expectation
  through an owner-only, regular-file, non-blocking descriptor.
- This release does not establish original-idle Devin receipt, remote Native-to-Native delivery, or
  a completed two-session reply journey on installed bytes. Those remain open.

## 0.3.5 - 2026-09-15

- Emit Native bootstrap context only for a current route whose registered owner, exact bundled
  executable, and app-owned ancestor all identify Codex Native. Standalone CLI startup no longer
  receives Native-only bootstrap instructions.
- Parse the Claude agent roster only for the requested exact target before applying strict
  validation, so an unrelated malformed or uppercase row cannot reject that target. The selected
  UUID remains canonical, and selected case collisions or duplicate valid rows still fail closed.
- Treat a current incoming answer or result as material for the local user, without an
  acknowledgement, echo, or new send unless that message explicitly asks for one. New incoming
  work requests continue to require one separately addressed response.
- This does not establish standalone CLI busy acceptance or mid-command model consumption; those
  paths require a fresh post-upgrade trial. It also does not establish a completed remote response;
  the roster issue was observed as a pre-effect sender rejection.

## 0.3.4 - 2026-09-15

- Run the owned, user-facing launchd broker with the Standard scheduling class to remove the
  observed Background scheduling delay for requested chat traffic.
- Tell an agent handling an explicit report or answer request to refresh `chat_peers`, match the
  exact original CAC source handle, and send one separate `chat_send`. This is guidance only: it
  does not create an automatic reply or establish a completed live report-back case.
- This prerelease does not establish reliable remote discovery or delivery. Incomplete Tailnet
  discovery remains an open boundary, as do the existing five-surface readiness limitations.

## 0.3.3 - 2026-09-13

- Promptly recheck a completed Tailnet listener verification while keeping that listener excluded
  until verification finishes. This reduces a broker refresh blind window; it does not by itself
  explain every remote discovery timeout.

## 0.3.2 - 2026-09-13

- Accept `/` as a canonical workspace display label while retaining the exact working-directory,
  provider-process, and owner checks that route a local Devin conversation.
- Treat a local Devin prompt-hook validation error as nonblocking provider-hook feedback. The hook
  emits no route, capability, acknowledgement, or delivery effect on that error.

## 0.3.1 - 2026-09-13

- Keep a cached local Devin `UserPromptSubmit` hook compatible after an upgrade: an existing
  registered route still handles a hook command without `--device`; current hooks pass the selected
  device so a first prompt can register a new conversation.

## 0.3.0 - 2026-09-13

- Add local Devin CLI and App integration through global MCP and lifecycle hooks. A Devin
  conversation is discoverable after its first user prompt, preserving inactive conversation privacy.
- Add trusted Codex Native helper provisioning and Desktop-native message delivery for a bound
  original task. Helper creation and message arguments stay private to the hook metadata path.
- Keep unknown delivery quarantine scoped to its event, preserve fresh independent work, and reject
  reused event IDs. Prevent native queue and Stop-bound duplicate delivery paths.
- Preserve default and alternate Claude/Codex install identity while upgrading the same record with
  local Devin ownership. Codex Native hooks require normal provider trust; setup never writes Codex trust hashes.
- This beta does not claim busy Codex CLI native ingress, idle Devin delivery, isolated Fusion
  helpers, full five-surface readiness, or final fleet acceptance.

## 0.2.1 - 2026-09-12

- Treat the Claude SendMessage `summary` field as the optional, bounded one-line display preview
  the provider schema documents, instead of an exact-match control value. Exact checks remain on
  the native type, both recipient selectors, the canonical message body binding, the one-effect
  gate, and the successful receipt contract.
- Report distinct `sendmessage_type_mismatch` and `sendmessage_summary_mismatch` diagnostics;
  remote peers continue to accept the legacy `sendmessage_control_mismatch` phase.
- Reject Unicode format, private-use, and separator characters in the preview, matching the
  display-text policy used for other visible fields.
- Desktop native routing remains dependency openai/codex#45123.

## 0.2.0 - 2026-09-12

- Snapshot provider configuration after immutable staged-runtime durability work. Concurrent user
  edits in that interval are included in the staged setup while later changes remain guarded before
  write and during rollback.
- Preserve an existing exact, body-free Claude helper unknown-delivery diagnostic across the remote
  broker boundary. This does not retry, replay, or resolve the delivery.

## 0.1.9 - 2026-09-12

- Keep localhost broker health and local delivery responsive while the optional Tailnet binding
  refresh is pending. A remote listener is excluded from acceptance until its fresh validation
  completes, then retained unchanged when its verified address is unchanged.

## 0.1.8 - 2026-09-12

- Mark inbound Cross Agent Chat source metadata separately from the actual local delivery
  principal, keep peer content untrusted, and remove the generic reply instruction footer.
- Let bare Doctor use one unambiguous device identity from the active installed Claude/Codex
  routes. Conflicting or malformed installed identities fail clearly; `--device` remains explicit.
- Preserve exact recipient selection, helper/pretool validation, no-replay handling, and body-free
  durable intent state. This release does not add Desktop built-in `send_message_to_thread` routing.

## 0.1.7 - 2026-09-11

- Keep a valid broker installation healthy when its optional Tailscale address hint was unavailable
  during installation but becomes discoverable later. Doctor still rejects modified broker
  commands, unexpected environment entries, and conflicting present hints.
- Preserve existing message delivery, recipient identity, and no-replay behavior.

## 0.1.6 - 2026-09-10

- Discover remote peer identities and delivery modes before optional title hints. Slow, malformed,
  or changed title responses no longer hide already-validated peers; discovery keeps its existing
  overall time budget.

## 0.1.5 - 2026-09-10

- Complete local courier bootstrap without waiting for Claude's native session listing. Routes
  become available only after the existing native health and pre-delivery identity checks pass.
- Keep initial bootstrap ahead of native health and delivery work; pre-bootstrap health remains
  unavailable and delivery is rejected before effect.
- Reuse an exact bootstrapped courier on duplicate SessionStart events, preserving its generation
  and accepted in-memory queue while native routing is temporarily unavailable.
- Reap a failed registration's exact child and private socket before removing its route.
- Ignore unrelated stale Claude agent rows whose workspace no longer exists; an invalid selected
  target remains unavailable.
- Preserve concurrent provider-setting edits when failed setup or upgrade rollback conflicts, and
  retain recovery custody for diagnosis instead of restoring stale settings.
- Expose exact peer handles, optional native task titles, and caller readiness separately from
  recipient delivery modes. Resolve display aliases globally before sending.
- Add authenticated, read-only MCP event status without changing stored intents or replaying work.
- Preserve live couriers, route generations, and retained runtimes through compatible upgrades.
- Preserve accepted queued input only for exact-courier duplicate SessionStart reuse and the
  observed same-artifact runtime transition; do not claim pending-input version-upgrade support.
- Bind Claude native SendMessage authorization to the full message and exact target envelope while
  reporting body-free local unknown phases without retrying delivery.

## 0.1.4 - 2026-09-05

- Resolve fuzzy destinations across local and remote peers before creating a delivery intent.
  Exact local recipients retain the fast path; incomplete discovery does not silently choose
  a fuzzy local recipient.
- Preserve provider integration files shared by profile pairs or symlink aliases until their
  last recorded owner is removed, including original inbound and hooks-setting restoration.
- Report observed per-recipient delivery modes automatically through `chat_peers`, with
  negotiated compatibility and `unknown` for older couriers.
- Isolate lifecycle tests from real services, provider settings, processes, and courier sockets.
- Preserve accepted, rejected, and unknown durable delivery records through lifecycle changes.
- Bind Claude setup to the active `CLAUDE_CONFIG_DIR` and Codex setup to the active `CODEX_HOME`,
  with profile-scoped installer state and recoverable backups for explicit roots.
- Add an explicit `setup --enable-experimental-codex-native-queue` opt-in that persists only in
  the selected Codex profile's owned hooks. Ordinary setups remain natural-Stop-bound; doctor
  reports the selected profile's queue mode.
- Keep stale workspace metadata structurally readable while excluding unavailable routes from live
  discovery and delivery.
- Fix a fast-reply socket close race that intermittently hid healthy peers during discovery.
- Support Claude's native background session inventory and ListAgents display format.
- Accept ordinary Unicode project names and bound rendered peer labels without using display text as
  route authority.
- Preserve a live courier generation and its in-memory queue when a provider repeats SessionStart
  for the same verified process birth, executable, profile, session, and working directory. A busy
  courier is preserved; a missing courier can recover with a fresh generation.
- Drain Codex courier queues in whole bounded frame batches, preserving ordered remainder for a
  later natural provider boundary.
- Align the Codex MCP tool timeout with the bounded remote delivery operation.
- Bind private listeners only to Tailscale-verified identities with an exact live interface match.
  Canonical launches find the standalone Tailscale CLI, and identity loss revokes the listener; a
  generic CGNAT tunnel or persisted address without a matching interface is insufficient.

## 0.1.3 - 2026-09-02

- Let disposable provider workers opt out with `CROSS_AGENT_CHAT_PRESENCE=off` without
  registering routes, spawning couriers, or exposing MCP chat tools.
- Compact only definitively dead provider routes during normal registration while preserving live
  shared process routes.
- Bound local courier health discovery to 32 workers.

## 0.1.2 - 2026-08-30

- Stage and verify a product-owned runtime before changing the public command, provider
  configuration, or broker.
- Switch one stable executable identity atomically across uv, pipx, local-venv, custom-root,
  and symlinked predecessor layouts without guessing package-manager internals.
- Restore the exact previous pointer, public entrypoint, configuration, and broker state when
  transition or candidate-health verification fails; retain evidence on compound rollback
  failure.
- Bind broker readiness to the exact running launchd PID, version, and runtime, and safely
  retire only a verified owner-local predecessor broker that still owns the port.
- Serialize setup/install/uninstall, recover interrupted transitions from private transaction
  state, and limit deletion to marker-owned committed releases plus staging owned by the active
  installer or proven abandoned by its recorded process incarnation.
- Configure the owned Codex MCP route to run without recurring per-tool approval prompts.

## 0.1.1 - 2026-08-29

- Report exact-agent lookup, ListAgents discovery, and route revalidation failures before
  launching the native `SendMessage` courier as deterministic.
- Report the destination's actual provider when delivery is unknown.
- State the Tailnet and Tailscale ACL trust boundary accurately.
- Add distributable build, clean-wheel smoke, and package-content checks to CI.
- Remove private release-process residue and tighten owned-hook recognition.
- Document supported surfaces, security behavior, and the install lifecycle.

Downgrade note: run `cross-agent-chat uninstall` before reinstalling 0.1.0 because 0.1.1
intent state includes a status that 0.1.0 does not read.

## 0.1.0 - 2026-08-28

- Initial public release with automatic local and Tailnet discovery.
- Native Claude delivery and process-scoped Codex delivery at natural Stop.
- One-command setup, repair, upgrade, and owned uninstall on macOS.
