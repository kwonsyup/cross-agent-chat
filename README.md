# Cross Agent Chat

Let your existing Claude Code, Codex, and local Devin sessions work together
across your Macs — different accounts, providers, and terminals can work
together. Ask another session for a review or an investigation, and get the
answer back where you asked. No new account, no manual peer lists; keep your
current projects and provider logins.

## Install

Use a Mac where a supported coding tool is already installed and working —
its existing account is reused, and signing in again is not a normal
installation step. Git is required; an existing Python 3.11+ or `uv` is
reused, otherwise the installer bootstraps a runtime. Cross-Mac use needs
Tailscale already connecting the Macs and permitting the traffic — discovery
reads its existing status and never brings it up. Same-Mac use needs none.

The command below approves integration edits to the Claude Code, Codex, and
local Devin configuration roots that already exist on this Mac, a local
background broker, and private configuration backups. It does not migrate
credentials or touch your provider logins; its private local backups are
whole-file copies of the edited configuration files and can contain any
secrets those files hold (see [setup and data handling](SECURITY.md)).
Adding `CROSS_AGENT_CHAT_PROVIDERS=claude,codex` to the install command
limits the set.

Install v0.4.6 with:

```sh
curl -fsSL https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/v0.4.6/install.sh | CROSS_AGENT_CHAT_APPROVE=1 sh
```

Running the same command without `CROSS_AGENT_CHAT_APPROVE=1` only prints the
planned effects and exits, so you can preview it first.

This builds the released `v0.4.6` tag and installs the command at
`~/.local/bin/cross-agent-chat` by default (an existing owner-local Cross
Agent Chat entrypoint is reused instead) — if a bare `cross-agent-chat` is
not found in your shell, use the full path the installer reports. Run it on
each participating Mac.

**Then open a new conversation, or relaunch the provider and resume an
existing one when convenient** — sessions load Cross Agent Chat when they
start and already-running ones keep what they loaded, so don't interrupt
ongoing work just to activate it. A local Devin conversation joins the peer
list after its first prompt.

**Codex:** newly installed hooks may need a one-time review in the selected
profile through its normal hook-review flow (the CLI documents `/hooks`).
That review permits the hooks to run — not a login, and separate from
MCP-tool approval. Hooks changed by an upgrade may need review again; Desktop
builds vary, so use each build's supported path rather than bypassing trust.

An agent can do the installation after your approval:

> Install Cross Agent Chat (github.com/kwonsyup/cross-agent-chat) with its
> documented installer for the coding tools I already use on this Mac. Keep
> my current accounts and preserve my unrelated settings. Tell me only any
> remaining provider
> approval and which next session will load it; do not restart my active
> work.

## Use it normally

Tell the session that will answer requests once, in its own conversation:

> Work on this repository and answer review requests from my Claude Code
> session on my MacBook through Cross Agent Chat, within your existing
> permissions.

Then ask your agent:

> Ask the Claude Code session on my Mac mini to review this change, send it
> the relevant diff or an accessible revision, and bring its findings back
> here.

The requester finds the recipient, sends the request, and ends its turn; the
recipient works with its own tools and account, then replies to the original
conversation — no manual relaying. Messages carry peer requests, never owner
instructions, and nothing synchronizes files or widens permissions. Start a
short-lived session with `CROSS_AGENT_CHAT_PRESENCE=off` to keep it off the
peer list.

## Supported today

| Coding surface on macOS | Receiving behavior |
|---|---|
| Claude Code | Receives while idle in supported desktop-launched contexts. |
| Codex Native App | Receives while idle in the original conversation through its managed helper, when the required provider capabilities and hooks are available. |
| Codex CLI | By default, receives at a turn boundary: the current turn's end or the next prompt. An explicitly enabled experimental queue has different behavior. |
| Local Devin CLI/App | Receives at the next prompt or turn end. A conversation joins the peer list after its first prompt; receiving into an already-idle conversation is open in [#38](https://github.com/kwonsyup/cross-agent-chat/issues/38). |

Idle reception and input during an active turn are separate capabilities.
The direct experimental Codex route uses `thread/queue/add`: a message can
wait behind the current turn. The managed Native helper forwards through
the app's `send_message_to_thread`; active-turn handling depends on that
installed host operation. Default Codex CLI delivery remains turn-bound.
`reply_delivery` describes the sender's return path, and
`TRANSPORT_ACCEPTED` proves custody, not model consumption.

It follows the coding session, not a terminal tab; Terminal.app, iTerm2, and
Ghostty have recorded supported cases on Intel and Apple silicon. No claim
for every OS/provider combination, other agents or IDE chat surfaces, web
chats, Windows, or Linux. A Claude session launched inside a remote SSH shell
registered but could not receive — remotely opening a desktop terminal is
different.

<details>
<summary>Custom profiles and installing with an agent</summary>

Setup uses the current `CLAUDE_CONFIG_DIR` and `CODEX_HOME` when set,
otherwise their normal defaults; local Devin uses its own local root:

```sh
CODEX_HOME="$HOME/.codex-work" CLAUDE_CONFIG_DIR="$HOME/.claude-work" \
  "$HOME/.local/bin/cross-agent-chat" setup --yes
```

Use the actual command path printed by installation if it differs. To select
providers explicitly, name the complete intended set — including providers
this installation already recorded, which cannot be dropped:

```sh
"$HOME/.local/bin/cross-agent-chat" setup --provider claude --provider codex
```

An installation agent should reuse your existing provider contexts, describe
the owned changes, use the canonical installer, verify with `doctor --json`
at the installed command path, and report only the remaining approval or
activation step
— never copying credentials, creating an account, writing hook-trust hashes,
disabling approvals, scanning unrelated profiles, or enabling experimental
modes to make a check green. A missing peer or timeout alone is not evidence
that a login is required.

</details>

<details>
<summary>Tools, delivery results, and recovery</summary>

Sessions load three tools: `chat_peers` finds recipients, `chat_send` sends
one message, `chat_status` reads sender-local custody. Plain language works
instead of invoking them.

`chat_send` reports `TRANSPORT_ACCEPTED` (custody, not a read receipt — do
not re-send), a pre-delivery refusal (nothing was handed over; correcting
that refused attempt is safe only if no earlier attempt of the same work was
accepted or uncertain), or `UNKNOWN_DELIVERY` (check the recipient directly;
do not re-send). Its `reply_delivery` — `while_idle`, `next_turn`, or
`unknown` — says how an answer can return.

```sh
"$HOME/.local/bin/cross-agent-chat" doctor --json
"$HOME/.local/bin/cross-agent-chat" peers --json
"$HOME/.local/bin/cross-agent-chat" resolve EVENT_ID
```

`doctor` reports what it can check about setup and the broker — not whether a
model read a task. A missing peer warrants one read-only relist, not a
replay; persistent absence may need the provider's normal registration event
(resume the Claude conversation, or prompt the original Devin workspace), not
idle supervision. `resolve` records an owner's disposition of an undecided
event — it does not cancel work or make replay safe. Turn-bound queues live
in courier memory; a crash can lose a pending copy. See
[SECURITY.md](SECURITY.md).

</details>

<details>
<summary>What setup changes</summary>

Setup writes a full backup of each managed file under
`~/.cache/cross-agent-chat/backups/` before changing:

- **Claude** (`~/.claude` or `$CLAUDE_CONFIG_DIR`), **Codex** (`~/.codex` or
  `$CODEX_HOME`), **Devin** (`~/.config/devin` when present): owned
  session/lifecycle hooks and the Cross Agent Chat MCP server in each;
  Claude also accepts inbound cross-session messages, Codex enables its
  hooks feature and the server's approval setting.
- An owner-local launchd broker under `~/Library/LaunchAgents/`.

Cross-Mac sessions connect over Tailscale: the broker listens on the Mac's
Tailnet address, TCP port `47071`, and your Tailscale ACL decides which Macs
may reach it; local broker health is `127.0.0.1:47072`.
`setup --enable-experimental-codex-native-queue` and
`--disable-experimental-codex-native-queue` toggle idle reception for new
sessions in the current Codex profile.

</details>

<details>
<summary>Updating or removing Cross Agent Chat</summary>

Use the installer for a new release, then activate in new or relaunched
sessions when convenient. Releases before v0.4.0 minted a different
reply-handle format — crossing that boundary needs fresh sessions on every
Mac (not a claim every patch breaks compatibility), and moving to a
pre-v0.4.0 release means uninstalling with the current version first.

`cross-agent-chat uninstall` removes owned integrations and restores recorded
prior settings; delivery records remain for owner inspection, and
`~/.config/cross-agent-chat` must stay intact because uninstall relies on its
records.

Sessions started before 0.4.4 can lose Cross Agent Chat when an npm-installed
Claude Code updates in place while running; recovery is resuming the
restarted conversation, and continuity applies only to sessions started
after 0.4.4 was installed. See
[issue #39](https://github.com/kwonsyup/cross-agent-chat/issues/39).

</details>

For code and contribution details: [source map](docs/source-map.md),
[contributing](CONTRIBUTING.md), [release history](CHANGELOG.md). Licensed
under Apache-2.0.
