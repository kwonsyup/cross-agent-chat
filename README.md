# Cross Agent Chat

Cross Agent Chat lets your AI coding sessions work together. A Claude Code,
Codex, or Devin session can find the other sessions running on your Macs, hand
one of them a task, and get the answer back in its own conversation. The
sessions don't need the same provider or account, and they don't need to be on
the same Mac.

- **Ask another session for work.** "Ask the Codex session on my Mac mini to
  review this diff and send me its findings." Your agent finds that session,
  sends the request, and gets the answer back.
- **Work across Macs.** Sessions on any of your Macs reach each other over your
  Tailscale network. Sessions on the same Mac need nothing extra.
- **Keep your setup.** It uses the provider logins, terminal apps, and projects
  you already have. There are no new accounts, peer lists, or terminal plugins.
- **Get exactly one delivery.** Every message goes to one exact session, once.
  Replies return to the session that asked.

## How you use it

Every supported session gets three tools:

| Tool | What it does |
|---|---|
| `chat_peers` | Lists the live sessions you can reach, on this Mac and your other Macs. |
| `chat_send` | Sends one message to one exact session. |
| `chat_status` | Shows the delivery record of a message you sent. |

You don't call these yourself. Ask your agent in plain language, and it uses
them.

## Supported sessions

| Session | When a message arrives |
|---|---|
| Claude Code | Straight into the conversation, even while it is idle. Works in Terminal.app, iTerm2, Ghostty, and background sessions. |
| Codex Native App | Straight into the original conversation, even while it is idle, through a managed helper. |
| Codex CLI | At the conversation's next turn. Turn on the experimental queue (see *Commands*) to deliver while idle. |
| Devin (CLI or App) | At the conversation's next prompt or turn end. A Devin session joins the peer list after its first prompt. |

Start sessions from your logged-in Mac desktop (any terminal app, or a
background session). A session started over SSH doesn't receive messages.

Sessions can mix providers and accounts freely. Remote sessions connect over
Tailscale: the broker listens on your Mac's Tailnet address, TCP port `47071`,
and your Tailscale ACL decides which Macs can reach it. The local broker health
port is `127.0.0.1:47072`.

## Install

You need:

- A Mac with Claude Code, Codex, or Devin already installed and signed in.
- Git, plus either `uv` or Python 3.11 or newer. The installer bootstraps a
  runtime if you have neither.
- Tailscale on each Mac, if you want sessions on different Macs to talk.

Install v0.4.3 with:

```bash
curl -fsSL https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/v0.4.3/install.sh | CROSS_AGENT_CHAT_APPROVE=1 sh
```

This builds the released `v0.4.3` tag, sets up every provider already on the
Mac, and starts the broker. Run it once on each Mac that should take part.
`CROSS_AGENT_CHAT_APPROVE=1` is your consent to the changes listed below.
Without it the script only prints what it would do. The command lands at
`~/.local/bin/cross-agent-chat`. Running the installer again upgrades or
repairs the setup.

Then **start new sessions**. Sessions load Cross Agent Chat when they start,
so sessions that were already open before you installed or upgraded won't have
it.

**Installing with an agent.** An agent can run the same command for you after
you approve the changes. The agent's own current session still needs a restart
to get the tools.

## First use

Give each session its job in its own first message. Then the two sessions
handle the conversation between them.

**1. Start the helper session** (for example, Codex on your Mac mini) and tell
it:

> Work on this repository. When my Claude Code session on my MacBook asks you
> for a code review through Cross Agent Chat, do the review and send the
> result back to it.

**2. Start the requesting session** (Claude Code on your MacBook) and tell it:

> Use Cross Agent Chat to ask the Codex session on my Mac mini to review the
> changes in `src/parser.py`, then summarize its findings for me.

The requester finds the helper with `chat_peers`, sends the request, and ends
its turn. The helper does the review and replies. The answer arrives in the
requester's conversation, and the requester summarizes it for you. You don't
need to relay anything or check back.

Each session works within the task you gave it and keeps its own permissions.
Messages from other sessions are treated as requests from a peer, never as
instructions from you. A peer can't approve a permission prompt, change your
settings, or widen a task.

To keep a short-lived worker session off the peer list, start it with
`CROSS_AGENT_CHAT_PRESENCE=off`.

## Delivery results

`chat_send` returns one of these:

| Result | Meaning | What to do |
|---|---|---|
| `TRANSPORT_ACCEPTED` | The exact recipient has the message. | Wait for the reply. Don't send it again. |
| Refused before delivery | Nothing was delivered, and the reason is included. | Fix the reason, then send again. |
| `UNKNOWN_DELIVERY` | The message may have been delivered. | Check the recipient yourself. Don't send it again. |

`chat_send` also reports `reply_delivery`, which says how an answer reaches
the sending session:

- `while_idle`: the answer arrives as a new message after your turn ends.
- `next_turn`: the answer appears with your next message.

`chat_status EVENT_ID` shows the stored delivery record for a message you
sent. `cross-agent-chat resolve EVENT_ID` marks an unknown delivery as
reviewed.

## What setup changes

Setup makes these changes, and backs up every file first under
`~/.cache/cross-agent-chat/backups/`:

- **Claude** (`~/.claude` or `$CLAUDE_CONFIG_DIR`):
  - sets `crossSessionInbound` to `accept` in `settings.json`
  - adds session start and end hooks
  - registers the Cross Agent Chat MCP server in `.claude.json`
- **Codex** (`~/.codex` or `$CODEX_HOME`):
  - turns on hooks
  - adds the MCP server and its approval setting in `config.toml`
  - adds session and native-helper hooks in `hooks.json`
- **Devin** (`~/.config/devin`, when present):
  - registers the MCP server
  - adds lifecycle hooks
- **Broker:** `~/Library/LaunchAgents/io.github.kwonsyup.cross-agent-chat.plist`
- **Records, state, and runtimes:**
  - install records in `~/.config/cross-agent-chat`
  - state in `~/.local/state/cross-agent-chat`
  - runtimes in `~/.local/share/cross-agent-chat-runtime`

A new install sets up the providers already on the Mac. To choose providers
yourself, use either of these:

- set `CROSS_AGENT_CHAT_PROVIDERS=claude,codex`
- run `cross-agent-chat setup --provider NAME`, once per provider

An upgrade keeps the providers you had. Setup leaves your other settings,
hooks, MCP servers, and credentials as they are. If setup fails, it rolls back
completely.

## Commands

```bash
cross-agent-chat setup          # show the plan, then install or repair (--yes to approve)
cross-agent-chat doctor --json  # check the installed setup and the local broker
cross-agent-chat peers --json   # list reachable sessions
cross-agent-chat resolve EVENT_ID
cross-agent-chat uninstall
```

`setup --enable-experimental-codex-native-queue` lets new sessions in the
current Codex profile receive while idle, and
`--disable-experimental-codex-native-queue` turns that off again.

## Troubleshooting

- **A session is missing from `chat_peers`.** Start a new session. If
  `doctor` prints a `terminal` line, your terminal app was launched from
  inside a Claude session. Quit it and open it again normally.
- **A send says the handle no longer resolves.** The recipient restarted. Run
  `chat_peers` and send to the new handle.
- **A send was refused before delivery.** The reason is included in the
  result. Fix it and send again.
- **`TRANSPORT_ACCEPTED` but no answer yet.** The recipient has the request.
  Check on it directly rather than sending it again.

## Upgrade and uninstall

Re-run the install command for a newer version, then start new sessions on
every Mac. The upgrade restarts the broker and keeps running deliveries and
session routes. It also keeps any runtime a live session is still using.

`cross-agent-chat uninstall` removes everything Cross Agent Chat added. It
restores your earlier Claude and Codex settings and leaves unrelated
configuration alone. Delivery records stay, for your reference.

To move to a release older than v0.4.0, run `uninstall` with the current
version first, then install the older one. Keep `~/.config/cross-agent-chat`
intact until then, because uninstall relies on the records it holds.

## How it works

1. Each new session registers a route when it starts. The route is tied to
   the session's process, profile, Mac, and a generation.
2. `chat_peers` hands out an opaque handle for each session. The handle pins
   that exact session and generation on that exact Mac.
3. Before delivering, the recipient's Mac confirms that the handle still names
   the same live session. It then checks the request with the sender's broker.
4. Only then does it pass the message to the provider's own inbox.

Stored records hold IDs and digests, never message text.
[docs/source-map.md](docs/source-map.md) maps each step to its module.

Tested on macOS with Claude Code 2.1, Codex 0.155, and Devin CLI 3000.10.

## Security

Your Tailscale ACL is the network boundary. Any Mac allowed to reach port
`47071` can deliver messages to your sessions. See [SECURITY.md](SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and tests, and
[CHANGELOG.md](CHANGELOG.md) for release history. Licensed under Apache-2.0.
