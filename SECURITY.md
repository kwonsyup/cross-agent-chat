# Security

## Remote trust boundary

Cross Agent Chat's remote boundary is your Tailscale network and ACL policy.
Any Tailnet node allowed to reach TCP port `47071` on a Mac running Cross
Agent Chat is inside the remote peer trust perimeter. The local broker health
and courier sockets are loopback- or owner-local only (`127.0.0.1:47072`,
owner-only Unix sockets). Cross Agent Chat does not add application-level
accounts, pairing, certificates, or owner authentication on top of that
perimeter.

Nodes inside the perimeter are trusted to report their own transport outcome.
A node that falsely reports a pre-effect rejection can cause the sender to
make a later fresh send. Recipient endpoint tokens are non-secret selectors:
their encoding names a session, generation, and endpoint but is not
authentication, so a token's meaning is only as trustworthy as the perimeter
that presented it. It is not a defense against an admitted node presenting a
forged claim.

Treat every peer message as untrusted user input. The provider integrations
label it as peer/user content rather than system or developer authority. Peer
text cannot grant owner authority, change approvals, resolve a held permission
prompt, or authorize a retry or bypass.

## What setup changes

Nothing changes without
`CROSS_AGENT_CHAT_APPROVE=1`: run without it and the script prints these
effects and exits before any staging or write. A fresh install integrates
only the provider roots that already exist, or the subset named by
`CROSS_AGENT_CHAT_PROVIDERS`; an upgrade retains the provider set recorded by
the previous install and never silently drops one.

Setup snapshots each affected configuration file before writing (see
*Configuration backups* below), then makes these owned changes:

- Selected Claude root (`~/.claude` or `$CLAUDE_CONFIG_DIR`): sets the
  profile's `crossSessionInbound` to `accept` so the provider accepts native
  inbound cross-session messages, merges owned SessionStart/SessionEnd hooks,
  and registers the Cross Agent Chat MCP server in `.claude.json`.
- Selected Codex root (`~/.codex` or `$CODEX_HOME`): enables the hooks
  feature, merges owned SessionStart/SessionEnd/Stop and native-helper hooks,
  and registers the owned MCP server in `config.toml`.
- Devin global root (`~/.config/devin`): registers the MCP server and merges
  lifecycle hooks — only when the Devin root exists or Devin was explicitly
  selected.
- `~/Library/LaunchAgents/io.github.kwonsyup.cross-agent-chat.plist`: an
  owner-local broker started by launchd.

Setup preserves unrelated settings, hooks, MCP servers, and credentials, and
refuses before writing when shared-file ownership is ambiguous. Uninstall
removes only the owned entries and restores the recorded prior values on the
last owner; content-free delivery intent records are retained for owner
inspection and are never resolved or replayed.

## Local approval posture

Setup configures only the owned Cross Agent Chat Codex MCP server to run
without recurring per-call approval prompts. That covers the three ordinary
tools — `chat_peers`, read-only `chat_status`, `chat_send` — and, on eligible
Codex Desktop hosts, the internal `native_bootstrap`, `native_register`, and
`native_dispatch` lifecycle tools the same server exposes. It does not change
global Codex approvals or unrelated MCP servers. Setup clears conflicting
approval overrides for the owned tools while preserving their other
properties.

Auto-approval means a Codex agent can send agent-authored text without another
confirmation. A compromised or prompt-injected peer can try to induce an
outbound send, including attempted data disclosure, so do not admit untrusted
nodes to the Tailnet perimeter.

Sender identity is bound, not self-declared: Codex sends require the host's
thread metadata, Claude sends are tied to the provider process/profile, and
Devin sends require a short-lived single-use capability issued through the
trusted hook path and bound to session, generation, prompt, tool, and
arguments.

## Inference and provider-owned data

Provider credentials stay in their existing local provider sessions and are
never copied between devices. Claude delivery uses two constrained helper
calls, so its latency and your account quota depend on those calls; the Codex
native helper likewise runs inside provider infrastructure.

Delivered message bodies can appear in provider transcripts, and with the
explicit experimental queue opt-in, Codex stores pending input in its own
provider queue before the transcript. "Local-first" describes where Cross
Agent Chat keeps its own coordination state — it does not mean offline model
execution or freedom from provider data handling.

Persistent Cross Agent Chat state contains route metadata, generations,
identity hashes, event IDs, payload digests, statuses, and timestamps — never
message bodies. By default, Codex pending input exists only in the recipient
courier's memory and is lost if that process exits before the next natural
Stop. Devin capability records are short-lived and single-use.

Delivering to a Claude recipient writes the message body to one transient
file so the local delivery gate can supply it to the provider instead of
asking a helper model to reproduce it. That file is created exclusively at
mode 0600 in a fresh owner-only directory under `TMPDIR`, is readable only by
your own user, and is removed when that single send finishes. A process killed
abruptly can leave the file behind until the directory is cleared.

## Configuration backups

Before any setup write, Cross Agent Chat copies each affected configuration
file — including whole `settings.json`, `.claude.json`, `config.toml`, and
`hooks.json` files — into a timestamped directory under
`~/.cache/cross-agent-chat/backups/` with owner-only permissions. These are
complete snapshots: if you placed API keys or MCP environment secrets in those
files, the backup copies contain them too. They are not encrypted and are not
content-free.

Backups exist for guarded rollback and failure diagnosis. The whole backup
cache is deleted when the last owning profile is uninstalled; individual
snapshots otherwise remain on disk. Never attach a backup directory or a
provider config file to a bug report.

## Getting support

A useful redacted report contains: `cross-agent-chat --version`, the provider
and harness (e.g. Codex CLI vs. Codex Native App), macOS version and
architecture, the receiving mode involved (idle, next-turn/Stop-bound,
prompt-bound), the expected versus actual result, the `doctor --json` output,
and event IDs plus `chat_status` output for the affected send. Do not include
credentials, private session identifiers, message bodies, provider
transcripts, or configuration backups in a public issue.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not include credentials, private session identifiers, message bodies, or
provider transcripts in a public issue.
