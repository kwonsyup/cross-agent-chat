# Changelog

## 0.1.4 - 2026-09-04

- Keep stale workspace metadata structurally readable while excluding unavailable routes from live
  discovery and delivery.
- Accept ordinary Unicode project names and bound rendered peer labels without using display text as
  route authority.
- Preserve a live courier generation and its in-memory queue when a provider repeats SessionStart
  for the same process, session, device, and working directory.
- Drain Codex courier queues in whole bounded frame batches, preserving ordered remainder for a
  later natural provider boundary.
- Align the Codex MCP tool timeout with the bounded remote delivery operation.
- Bind private broker listeners only after a running Tailscale status establishes the active address;
  a generic CGNAT tunnel or persisted address alone is insufficient.

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
