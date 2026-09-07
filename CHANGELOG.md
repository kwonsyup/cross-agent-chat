# Changelog

## 0.1.5 - Unreleased

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
