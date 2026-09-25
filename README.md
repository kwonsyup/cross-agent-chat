# Cross Agent Chat

Cross Agent Chat lets your AI coding sessions work together. A Claude Code,
Codex, or Devin session can find the other sessions running on your Macs, hand
one of them a task, and get the answer back in its own conversation. The
sessions don't need the same provider or account, and they don't need to be on
the same Mac.

- **Ask another session for work.** "Ask the Claude session on my Mac mini to
  review this diff and send me its findings." Your agent finds that session,
  sends the request, and gets the answer back.
- **Work across Macs.** Sessions on any of your Macs reach each other over your
  Tailscale network. Sessions on the same Mac need nothing extra.
- **Keep your setup.** It uses the provider logins, terminal apps, and projects
  you already have. There are no new accounts, peer lists, or terminal plugins.
- **Reach one exact session.** Every message goes to the one session you
  picked, and Cross Agent Chat never re-sends a message on its own. Replies
  return to the session that asked.

## How you use it

A fresh session in an integrated provider loads three tools automatically:

| Tool | What it does |
|---|---|
| `chat_peers` | Lists the live sessions you can reach, on this Mac and your other Macs. |
| `chat_send` | Sends one message to one exact session. |
| `chat_status` | Shows the delivery record of a message you sent. |

You don't call these yourself. Ask your agent in plain language, and it uses
them. Collaboration stays within each owner's given task and permissions;
sessions do not automatically run arbitrary instructions from peers.

## Supported sessions

| Session | When a message arrives |
|---|---|
| Claude Code | Straight into the conversation, even while it is idle. Works in Terminal.app, iTerm2, Ghostty, and background sessions. |
| Codex Native App | Straight into the original conversation, even while it is idle, through a managed helper. |
| Codex CLI | At its next turn boundary (turn end or your next prompt). A queued message waits only in courier memory until then; the experimental queue (see *Commands*) delivers while idle. |
| Devin (CLI or App) | At the conversation's next prompt or turn end. A Devin session joins the peer list after its first prompt. Receiving into an already-idle conversation is still open ([#38](https://github.com/kwonsyup/cross-agent-chat/issues/38)). |

Start sessions from your logged-in Mac desktop (any terminal app or background
session). A Claude Code session launched inside a remote SSH shell registered
and listed in testing, but the courier could not deliver to it — SSH-hosted
execution differs from launching a desktop terminal remotely.

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

Install v0.4.4 with:

```bash
curl -fsSL https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/v0.4.4/install.sh | CROSS_AGENT_CHAT_APPROVE=1 sh
```

This builds the released `v0.4.4` tag, sets up the providers already on the
Mac, installs the command at `~/.local/bin/cross-agent-chat`, and starts the
broker. Run it once on each Mac that should take part; re-running the installer
upgrades or repairs the setup.

`CROSS_AGENT_CHAT_APPROVE=1` consents to the setup effects listed under
[*What setup changes*](#what-setup-changes) — provider file edits, the launch
agent, and the runtime — with a full backup of each managed file written under
`~/.cache/cross-agent-chat/backups/` first (see [SECURITY.md](SECURITY.md)).
Without it the script only prints what it would do.

Then **start new sessions**. Sessions load Cross Agent Chat when they start, so
sessions already open before the first install have no tools. A session kept
open across an upgrade keeps running the tool version it loaded at start until
it is restarted.

**Non-default profiles.** If you use custom profile locations, point setup at
them:

```bash
CODEX_HOME=~/.codex-work CLAUDE_CONFIG_DIR=~/.claude-work cross-agent-chat setup --yes
```

**Installing with an agent.** An agent can run setup after you approve the
plan. The agent's own current session must be restarted to load the tools.

## First use

Give each session its job in its own first message. Then the two sessions
handle the conversation between them.

**1. Start the helper session** — an idle-capable one, such as Claude Code or
the Codex Native App on your Mac mini — and tell it:

> Work on this repository. When my Claude Code session on my MacBook asks you
> for a code review through Cross Agent Chat, do the review and send the
> result back to it.

**2. Start the requesting session** (Claude Code on your MacBook) and tell it:

> Use Cross Agent Chat to ask the Claude Code session on my Mac mini to review
> the changes in `src/parser.py`, then summarize its findings for me.

The requester finds the helper with `chat_peers`, sends the request, and ends
its turn. The helper receives the request while idle, does the review, and
replies. The answer arrives in the requester's conversation without manual
relaying — Claude's own reply mode returns while idle; a turn-bound requester
would see the answer at its next turn instead.

*(A default Codex CLI or local Devin helper is turn-bound: it handles an
incoming request at its next turn or prompt, so it needs that interaction
first.)*

Each session works strictly within the task you gave it and keeps its own
permissions. Messages from peers arrive as peer requests, never as owner
instructions. A peer cannot approve prompts, change settings, or widen a task.

To keep a short-lived worker session off the peer list, start it with
`CROSS_AGENT_CHAT_PRESENCE=off`.

## Delivery results

`chat_send` reports the handover result:

| Result | Meaning | What to do |
|---|---|---|
| `TRANSPORT_ACCEPTED` | Accepted for delivery into the recipient's courier or inbox. Not a read receipt — it does not confirm the model saw the message or did the work. | Wait for the reply. Do not send again. |
| Refused before delivery | Nothing was handed over. The refusal applies only to that one attempted send. | Fix the reported cause (e.g. a discovery timeout), then send again — safe only for that refused attempt, and only if no earlier attempt of the same task was `TRANSPORT_ACCEPTED` or `UNKNOWN_DELIVERY`. |
| `UNKNOWN_DELIVERY` | The send's outcome could not be confirmed; the message may or may not have been delivered. | Check the recipient session directly. Do not re-send. |

`chat_send` also reports `reply_delivery`, how an answer can return to your
session:

- `while_idle`: an answer can arrive as a new message after your turn ends.
- `next_turn`: an answer appears at your next turn boundary — when your current turn ends or your next prompt starts.
- `unknown`: the return path cannot be confirmed.

`chat_status EVENT_ID` shows the stored custody record for a message you sent.
`cross-agent-chat resolve EVENT_ID` marks an undecided or unknown delivery as
reviewed in your local history. It does not cancel, deliver, or contact the
recipient, and never makes re-sending that task safe.

## What setup changes

Setup writes a full backup of each managed file under
`~/.cache/cross-agent-chat/backups/` before changing:

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

A default install integrates the provider roots already on the Mac. To select
providers yourself, name every provider in one command with repeated
`--provider` flags, or set `CROSS_AGENT_CHAT_PROVIDERS=claude,codex` on the
install command:

```bash
cross-agent-chat setup --provider claude --provider codex
```

The set must include every provider an existing install already recorded —
recorded providers cannot be dropped — and each named provider's configuration
root must exist. If setup hits a conflict or fails verification, it performs a
guarded rollback toward your prior state; if rollback itself is obstructed, the
backup paths and failure report stay in place for manual recovery.

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

- **A session is missing from `chat_peers`.** A session that just started, or
  one whose courier did not answer its health probe in time, can be absent
  from one listing — list again (the call is read-only) before concluding it
  is gone. If it stays missing, start a fresh session. If `doctor` prints a
  `terminal` note it is process-local: the marker is expected inside Claude
  tool and hook subprocesses, and a normally opened terminal app showing it
  was launched from inside a Claude session — relaunch that one instance.
- **A send says the handle no longer resolves.** The recipient restarted. Run
  `chat_peers` to get the new handle.
- **A send was refused before delivery.** That attempt delivered nothing; once
  the reported cause is fixed, sending again is safe only for that refused
  attempt — a later refusal never authorizes replaying an earlier attempt of
  the same task that was `TRANSPORT_ACCEPTED` or `UNKNOWN_DELIVERY`.
- **`TRANSPORT_ACCEPTED` but no answer yet.** The recipient's courier accepted
  the request. Check the recipient session directly rather than re-sending.

## Upgrade and uninstall

Re-run the install command for a newer version, then start fresh sessions. The
upgrade restarts the broker and keeps routes and runtimes still used by live
sessions. Handles minted before v0.4.0 cannot be answered, so crossing that
boundary needs fresh sessions on every Mac.

`cross-agent-chat uninstall` removes the integrations it owns, restores the
earlier provider settings it recorded, and leaves delivery intent records and
unrelated settings in place.

Moving to a release older than v0.4.0 crosses the pre-v0.4.0 configuration
format break: run `uninstall` with the current version first, then install the
older one. Keep `~/.config/cross-agent-chat` intact until then, because
uninstall relies on the records it holds.

**Provider updates.** Sessions started before CAC 0.4.4 can lose Cross Agent
Chat when an npm-installed Claude Code updates in place while running; the fix
applies to sessions started after 0.4.4 is installed. For an affected older
session, recovery is restarting it (resuming the conversation). See
[issue #39](https://github.com/kwonsyup/cross-agent-chat/issues/39).

## How it works

1. Each new session registers a route when it starts, bound to its process,
   profile, Mac, and generation.
2. `chat_peers` issues an opaque handle pinning that exact session instance.
3. The recipient's Mac confirms the handle still names the same live session
   and checks the request with the sender's broker before accepting delivery.
4. Messages pass into the recipient provider's native inbox.

Stored records retain event IDs and cryptographic digests, never raw message
bodies. See [docs/source-map.md](docs/source-map.md).

## Security

Your Tailscale network and ACLs form the authorization boundary. Any node
permitted to reach port `47071` can exchange messages with your sessions. See
[SECURITY.md](SECURITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).
Licensed under Apache-2.0.
