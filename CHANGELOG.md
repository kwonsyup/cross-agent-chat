# Changelog

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
