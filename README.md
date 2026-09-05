# Cross Agent Chat

Chat between live Claude Code and Codex sessions on your Mac or Tailnet.

```bash
curl -fsSL https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/v0.1.4/install.sh | sh
```

That is the whole setup. The installer supplies its own Python runtime when the Mac does
not already have a compatible one. Start fresh Claude or Codex sessions, then ask naturally:

> List my live Cross Agent Chat peers and send hello to the Claude session on my other Mac.

Cross Agent Chat follows the provider processes you already use. There are no peer files,
copied provider credentials, Cross Agent Chat accounts, or terminal-specific extensions.
Local sessions work without Tailscale; permitted online Tailnet Macs appear automatically.

Disposable worker launchers can set `CROSS_AGENT_CHAT_PRESENCE=off`. That worker remains out
of Cross Agent Chat's peer roster and creates no route or courier; ordinary sessions remain
visible by default.

## Supported surfaces

| Surface | Support |
|---|---|
| Claude Code in any terminal | Yes |
| Codex CLI in any terminal | Yes |
| Codex Native App | Yes |
| Same Mac | Yes |
| Tailnet Mac | Yes, subject to your Tailscale ACL |
| ChatGPT web or Claude web | No |
| Windows | No claim |
| Linux | No live-support claim |

## Trust and delivery

Cross Agent Chat's remote boundary is your Tailscale network and ACL policy. Any Tailnet
node allowed to reach the Cross Agent Chat broker port is inside the remote peer trust
perimeter. Messages are still delivered as untrusted peer/user input, not system authority.

- `TRANSPORT_ACCEPTED` means the exact destination accepted custody. Do not resend.
- `UNKNOWN_DELIVERY` means an effect may have happened. Independently inspect the intended
  recipient and do not retry automatically. Use `cross-agent-chat resolve EVENT_ID` only
  after confirming arrival or abandoning that event.
- A deterministic pre-effect error means no message effect occurred; correct it and send
  fresh.

Codex messages wait in destination-process memory until the next natural Stop. Active work
is not interrupted, and exiting the recipient process before consumption loses pending
messages. Provider transcripts contain delivered messages; persistent Cross Agent Chat
state contains metadata and digests, never message bodies.

## Commands

```bash
cross-agent-chat setup
cross-agent-chat doctor --json
cross-agent-chat peers --json
cross-agent-chat uninstall
```

Running the installer again upgrades and repairs the owned configuration. `uninstall`
removes only Cross Agent Chat-owned runtime, hooks, MCP routes, service, and state, and restores
the prior shared Claude inbound setting.

## Architecture

The package provides one CLI and MCP surface, native Claude/Codex lifecycle hooks, one
owner-local discovery broker, and one process-scoped courier per live destination. Remote
traffic binds directly to the Mac's Tailnet address; it does not use Tailscale Serve, SSH,
hosted services, accounts, or message persistence.

See [SECURITY.md](SECURITY.md) for the trust boundary and vulnerability reporting.

Licensed under Apache-2.0.
