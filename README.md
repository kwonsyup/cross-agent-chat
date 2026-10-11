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

Install v0.5.3 with:

```sh
curl -fsSL https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/v0.5.3/install.sh | CROSS_AGENT_CHAT_APPROVE=1 sh
```

Running the same command without `CROSS_AGENT_CHAT_APPROVE=1` only prints the
planned effects and exits, so you can preview it first.

This builds the released `v0.5.3` tag and installs the command at
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

The requester finds the recipient, sends the request, and continues any
independent work. The recipient works with its own tools and account, then
replies to the original conversation according to the requester's receiving
mode — no manual relaying. Messages carry peer requests, never owner
instructions, and nothing synchronizes files or widens permissions. Start a
short-lived session with `CROSS_AGENT_CHAT_PRESENCE=off` to keep it off the
peer list.

## Supported today

Idle wake and active-turn input are separate capabilities, qualified per
harness and mode:

| Coding surface on macOS | Parked/idle wake | Active-turn input | Deferred behavior | Qualification |
|---|---|---|---|---|
| Claude Code | Qualified idle wake and new-turn input | Between tool calls; a running tool is not interrupted | — | Supported desktop-launched contexts; a session inside a remote SSH shell registered but could not receive |
| Codex CLI 0.160.1 / 0.162.0 / 0.162.1 (owning app-server daemon) | Queued input wakes the parked original | Direct input reaches the expected active turn | — | Only a `codex-tui` 0.160.1, 0.162.0, or 0.162.1 conversation already owned by its own daemon, verified by route PID, profile, thread, cwd, and originator over a private owner-only socket; CAC starts no daemon and uses no UI relay |
| Codex CLI `--no-daemon` or any unqualified version (older or newer) | No | No | Stop-bound: the current turn's end or the next prompt | Default when no owning daemon is present |
| Codex Native App (managed helper) | Qualified on the listed host | Qualified on the listed host | — | Intel macOS, Codex 0.159.2 in Native app 26.928.21956, trusted hooks available; does not qualify every host version |
| Codex CLI experimental native queue | Provider-held queue input | No | Waits behind the current turn | Only under explicit `setup --enable-experimental-codex-native-queue`; not active by default. A qualified owning daemon on the same route takes precedence over this queue |
| Local Devin CLI/App | No — [#38](https://github.com/kwonsyup/cross-agent-chat/issues/38) open | Between the root conversation's tool calls; a running tool is not interrupted. While a built-in subagent (`subagent_general`, `subagent_explore`) is running, only after the root's own subagent or question tool calls; while a custom profile (which may nest) runs or a launch outcome was not observed, the message waits for the next prompt. A child counts as running until Devin reports it finished to the root (`read_subagent` or a foreground return); one known only through Devin's completion notification keeps that session restricted until it ends | An idle conversation receives at its next prompt | Reported as active-turn input only when the receiving Mac runs 0.5.3 and that session has already run its tool hook; otherwise reported conservatively. A newer receiver also reports its current subagent restriction as `current_boundary`, and `active_turn_input` is then `false` (held until the next prompt) or `limited` (root-only tools). Qualified 8 Oct 2026 with Devin CLI 3000.11.3 and the Desktop-bundled CLI 3000.10.48 (Apple silicon) receiving from Codex CLI 0.162.0 and Claude Code 2.1.294 across Macs; the Desktop app window itself was not exercised |
| Grokbot 0.66.0 (external endpoint) | One author-reported idle webhook return | Unqualified | — | One owner-enrolled Bot; does not certify another Bot, fresh-user setup, or M2 |

`destination_receiving` in a send result describes the destination's observed
route mode and mechanism — which of these capabilities apply — never a
receipt. `reply_delivery` describes the sender's own return path, and
`TRANSPORT_ACCEPTED` proves custody, not model consumption.
The experimental Codex route uses `thread/queue/add`, and the managed Native
helper forwards through the app's `send_message_to_thread`; the Native
qualification above includes a cross-Mac return to the same active original
turn.
After requesting peer work, continue independent work within your task;
finish when none remains instead of holding a turn open to wait.

It follows the coding session, not a terminal tab; Terminal.app, iTerm2, and
Ghostty have recorded supported cases on Intel and Apple silicon. No claim
for every OS/provider combination, other agents or IDE chat surfaces, web
chats, Windows, or Linux. A Claude session launched inside a remote SSH shell
registered but could not receive — remotely opening a desktop terminal is
different.

## Optional Grokbot connection

Cross Agent Chat 0.5.0 adds an owner-enrolled external CLI/MCP connection.
Grokbot 0.66.0 was qualified with one owner's webhook and local-shell routine:
the original Bot sent work to an iMac/M1 Claude Opus 5.5 session in the
bypass-permission class, and the same Grok conversation used the automatic
webhook return while idle. This is an owner-enrolled endpoint, not provider
attestation or per-Bot identity. Active-turn Grok receiving, another Bot,
fresh-user setup, and M2 are not qualified.

Keep the optional setup in ~/.config/cross-agent-chat/external/grok/.
Store the CAC credential in credential and Grok's callback URL/key in
callback.json; keep both owner-private. Grok generates the webhook key in the
owned routine panel. Its routine update API does not return that key to the
Bot. During authorized setup, capture the key from the panel directly into
callback.json, then pass only the file path to configure-callback. Do not put
credential values in model chat, command arguments, or logs.

Enroll the endpoint and configure its callback with the existing CLI.
Connect the Grok local-shell routine to external-call, or configure a stdio
MCP client with external-mcp; pass the CAC credential file path, never its
value. The external MCP exposes chat_peers, chat_send, and chat_status.
Scope, request, callback, and revocation details are in the
[external-client contract](docs/source-map.md#external-client-contract).
The Mac's CAC broker must be reachable when Grok sends work.

```sh
GROK_DIR="$HOME/.config/cross-agent-chat/external/grok"
CAC="$HOME/.local/bin/cross-agent-chat"

# Enroll: writes a new mode-0600 credential file (it refuses to overwrite an
# existing path) and prints a JSON result containing the endpoint_id.
# --device is this Mac's CAC device name; it becomes part of the endpoint
# alias.
ENROLL=$("$CAC" external enroll --device thismac --name grok \
  --context "owner Grokbot routine" --credential-file "$GROK_DIR/credential")
ENDPOINT_ID=$(printf '%s' "$ENROLL" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["endpoint_id"])')

# Create the private callback source (exactly {"url","bearer"}) without
# placing the Grok-issued values on a command line or in logs; paste the
# webhook URL, then the key, at the hidden prompts. The file is created
# mode-0600 and is never overwritten.
mkdir -p "$GROK_DIR" && chmod 700 "$GROK_DIR"
python3 - "$GROK_DIR/callback.json" <<'PY'
import getpass, json, os, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    json.dump(
        {"url": getpass.getpass("callback URL: "),
         "bearer": getpass.getpass("callback key: ")},
        stream,
    )
PY

# Bind the callback from that private JSON file; this rotates the endpoint
# generation, so use the ENDPOINT_ID printed by enroll (or rotate).
"$CAC" external configure-callback "$ENDPOINT_ID" \
  --config-file "$GROK_DIR/callback.json"

# One-shot local-shell client: one JSON request object on standard input.
"$CAC" external-call --credential-file "$GROK_DIR/credential" <<'JSON'
{"name":"chat_peers","arguments":{}}
JSON

# Each callback POST carries the envelope's reply handle. The owner routine
# reads the incoming envelope, does the work, and answers with a separate
# send to that exact reply handle and a fresh request_id per event:
"$CAC" external-call --credential-file "$GROK_DIR/credential" <<'JSON'
{"name":"chat_send","arguments":{"to":"<reply handle from the received envelope>","message":"<the routine's answer>","request_id":"<fresh UUID>"}}
JSON

# Or serve a stdio MCP client with the same credential file path:
"$CAC" external-mcp --credential-file "$GROK_DIR/credential"
```

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
instead of invoking them. `chat_peers` accepts an optional `query` that
narrows the same listing by case-insensitive substring on alias and title —
a malformed value is refused before any probing — and its `sender` entry
identifies your authenticated session by alias and exact handle. `chat_send`
also resolves one peer's exact full alias or, for a Codex peer, its exact
provider title — or, for an owner-enrolled external endpoint, its exact
endpoint name — refusing when it matches zero or several, or when the
metadata is incomplete and cannot establish uniqueness. Peer rows may
show a descriptive title; a Devin row's is a stable opaque session label, not
a provider title or selector. An exact handle or uniquely matching full alias
always selects.

`chat_send` reports `TRANSPORT_ACCEPTED` (custody, not a read receipt — do
not re-send), a pre-delivery refusal (nothing was handed over; correcting
that refused attempt is safe only if no earlier attempt of the same work was
accepted or uncertain), or `UNKNOWN_DELIVERY` (check the recipient directly;
do not re-send). Only an explicit decided no-effect refusal proves a refused
send delivered nothing — any other error can carry an uncertain effect, and
a new event id, recipient, or provider does not make equivalent uncertain
work safe to resend. Its `reply_delivery` — `while_idle`, `next_turn`, or
`unknown` — says how an answer can return.

```sh
"$HOME/.local/bin/cross-agent-chat" doctor --json
"$HOME/.local/bin/cross-agent-chat" peers --json
"$HOME/.local/bin/cross-agent-chat" status EVENT_ID
"$HOME/.local/bin/cross-agent-chat" resolve EVENT_ID
```

`doctor` reports what it can check about setup and the broker — not whether a
model read a task. Its `codex_native_queue` value is the configured
experimental setting, not an observed receiving mode; `peers` shows each
session's actual mode and current blocker. `status` is read-only and
body-free: for a local receiver it says whether the event is still pending in
courier memory (with count and oldest age), was handed to the provider
boundary, or is unknown — handed off never means read. A missing peer warrants one read-only relist, not a
replay; persistent absence may need the provider's normal registration event
(resume the Claude conversation, or prompt the original Devin workspace), not
idle supervision. `resolve` records an owner's disposition of an undecided
event — it does not cancel work or make resending equivalent uncertain work
safe. Turn-bound queues live
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
