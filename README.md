# Cross Agent Chat

Chat between Claude Code, Codex, and local Devin sessions on one Mac or across
permitted Macs on your Tailnet — using the provider accounts and terminal apps
you already have.

**Status: macOS prerelease.** Peer discovery and sending work between the
supported providers below. How a delivered message enters the recipient's
conversation depends on that provider's receiving mode, and idle receipt is not
established for every surface. Read the receiving column before relying on a
reply.

> **Documentation note:** this source tree documents the unreleased candidate.
> Text marked *(candidate)* describes behavior not yet in the published v0.3.8
> prerelease; the install command below still installs that released tag.

## What it does

Inside a supported coding session, the agent gets three local tools over MCP:
`chat_peers` discovers live sessions, `chat_send` delivers one message to an
exact recipient, and `chat_status` reads the sender's own content-free custody
record. A per-user broker coordinates delivery on the Mac and over your
Tailnet. There are no Cross Agent Chat accounts, no peer files to edit, and no
terminal extensions — the integration follows the provider process, not the
terminal window.

## Compatibility and receiving

| Surface | Receiving a delivered message |
|---|---|
| Claude Code | Native cross-session delivery through constrained helpers. Recorded working in GUI-hosted terminal sessions (Terminal.app, iTerm2, Ghostty). One SSH-launched session registered and listed, but delivery to it failed; start recipients from the logged-in desktop session. |
| Codex CLI | Stop-bound by default: a queued message is handed over at the conversation's next natural turn. Reception while idle is not guaranteed, and pending input held only in the courier's memory is lost if it exits first. A profile-local opt-in experimental queue can deliver while idle on tested versions. |
| Codex Native App | A managed helper bound to the original conversation/profile/account uses Desktop-native task messaging through trusted hooks. |
| Local Devin (CLI or App) | Delivered at a prompt or Stop boundary. A conversation becomes discoverable only after its first user prompt. Receiving in an already-idle original conversation is a known gap, not a working feature. |

Peers never need matching provider accounts or a shared coding platform. Remote
sessions additionally require Tailscale reachability allowed by your ACL;
product traffic uses the Mac's Tailnet IPv4 address on TCP `47071`, and the
local broker listens on `127.0.0.1:47072`. Same-Mac sessions do not need
Tailscale. A roster can stay incomplete when an online non-CAC Tailnet node
(such as a phone) fails discovery; that affects alias/fuzzy sends, not an
exact token whose owner answered. ChatGPT/Claude web apps, Gemini, Grok,
cloud Devin, Windows, and Linux have no support claim.

**After any install or upgrade, start fresh sessions on every participating
Mac** — see *Install* below. The supported request/reply scope is fresh
sender session to fresh recipient session; retained pre-upgrade sessions may
keep exchanging only with other retained sessions.

## Prerequisites

- macOS with a supported Claude Code, Codex, or local Devin installation that
  already has its own working authenticated session.
- Git (the installer builds from a release tag) and either `uv` or Python
  ≥ 3.11; the installer bootstraps a runtime tool when neither is present.
- Install separately on every Mac and on every selected provider-profile root
  that will participate (`CLAUDE_CONFIG_DIR` and `CODEX_HOME` select
  non-default roots).
- For remote peers: Tailscale with ACL-permitted reachability between the Macs.

## What setup changes

Installation is not just a binary copy, and it is never silent. *(Candidate)*
the installer requires explicit approval — without `CROSS_AGENT_CHAT_APPROVE=1`
it prints this effect summary and exits before any staging or write. With
approval it snapshots every affected configuration file into
`~/.cache/cross-agent-chat/backups/` for rollback, then writes:

- Selected Claude root (`~/.claude` or `$CLAUDE_CONFIG_DIR`): sets
  `crossSessionInbound` to `accept` in `settings.json`, merges owned
  SessionStart/SessionEnd hooks, and registers the Cross Agent Chat MCP server
  in `.claude.json`.
- Selected Codex root (`~/.codex` or `$CODEX_HOME`): enables the hooks feature
  and adds the owned MCP server and approval behavior in `config.toml`, and
  merges owned SessionStart/SessionEnd/Stop plus native-helper hooks into
  `hooks.json`.
- Devin global root (`~/.config/devin`): registers the MCP server and merges
  lifecycle hooks (SessionStart, SessionEnd, Stop, UserPromptSubmit,
  PreToolUse) — only when the Devin root exists or Devin was explicitly
  selected.
- `~/Library/LaunchAgents/io.github.kwonsyup.cross-agent-chat.plist`: the
  owner-local broker, plus install metadata under
  `~/.config/cross-agent-chat`, state under `~/.local/state/cross-agent-chat`,
  and staged runtimes under `~/.local/share/cross-agent-chat-runtime`.

*(Candidate)* a fresh install selects only the provider roots that already
exist on the Mac; `CROSS_AGENT_CHAT_PROVIDERS` (a comma-separated subset of
`claude,codex,devin`) selects explicitly, as does a repeated
`cross-agent-chat setup --provider NAME`. An upgrade retains the provider set
the previous install recorded — a recorded provider is never silently
dropped — and setup refuses before writing when shared-file ownership would
be ambiguous. `cross-agent-chat setup` prints a read-only plan of the exact
selected roots and effects, then requires `--yes` or one interactive
confirmation before any write; it never reads piped stdin for consent.
Unrelated settings, hooks, MCP servers, and credentials are preserved.

## Install

Install v0.3.8 prerelease with:

```bash
curl -fsSL https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/v0.3.8/install.sh | CROSS_AGENT_CHAT_APPROVE=1 sh
```

`CROSS_AGENT_CHAT_APPROVE=1` is set on `sh`, not on `curl`. On the candidate
installer it is the required consent surface described above; the published
v0.3.8 script predates that gate and ignores the variable, so this command is
safe for both. This installs the released `v0.3.8` tag and performs setup in
one step. The stable command is `~/.local/bin/cross-agent-chat`; if your shell
does not resolve it, put `~/.local/bin` on `PATH` or invoke the absolute path.
Re-running the installer upgrades and repairs the owned configuration.

### Session compatibility after install or upgrade

*(Candidate)* recipient handles changed to opaque endpoint tokens — see *First
use*. **After installing or upgrading on every participating Mac, start fresh
sender *and* recipient provider sessions.** The supported request/reply scope
on this release is fresh-session to fresh-session.

- Sessions retained from before the upgrade may keep exchanging with other
  retained sessions through the upgraded broker.
- Mixed pre-upgrade/new sessions are unsupported: an initial request can be
  accepted (`TRANSPORT_ACCEPTED`) while the reply token in its envelope is
  unusable by the older side. Do not retry that exchange under a new event —
  start fresh sessions on both ends. Not every mixed-mode failure happens
  before effect.
- There is no raw-handle fallback, no translation or migration, and no
  binding cache to repair — the token is self-contained, so a fresh
  `chat_peers` listing is the only fix.

## First use

Open two fresh supported sessions — for example Claude Code on this Mac and
Codex on another permitted Mac. In one, ask naturally:

> List my Cross Agent Chat peers, then ask the session on my other Mac what
> project is in its working directory and what language it uses, and send me
> the answer.

The agent calls `chat_peers`, picks the exact `handle` of the intended peer,
and sends. The recipient receives the request at its receiving boundary (see
the matrix), does the work, and replies with a separate send. On a Stop-bound
or prompt-bound requester the answer is handed over at that session's next
turn — do not mistake that for delivery while it sits idle.

*(Candidate)* the `handle` that `chat_peers` returns is an opaque endpoint
token, not a raw session handle. A token pins the exact session key and route
generation to the endpoint that presented them — the stable Tailnet node for
a remote peer, or the local state root on the same Mac — and is
self-contained: at send time the sender re-reads the Tailscale authority for
the node's current address and requires that one endpoint to re-attest the
exact session and generation before any effect. A token whose session
restarted under a new generation stops resolving; run `chat_peers` again and
send to the fresh token. A raw pre-upgrade handle is refused with guidance to
re-list. Display-name and alias selection still work against a complete
roster; multiple matches or incomplete remote discovery require an exact
token. Tokens are selectors, not secrets — their encoding is not
authentication, and holding one only names a recipient. An incomplete roster
can be refreshed with another read-only `chat_peers` call, which never
authorizes resending an accepted or unknown message.

A delivered message arrives through the recipient provider's own inbox, so its
visible sender is the local delivery helper, not the peer. Replies go to the
`Reply via CAC to handle:` value in the envelope — on the candidate that
value is the sender's reply token — not to the visible sender.

Disposable worker sessions can opt out of the roster with
`CROSS_AGENT_CHAT_PRESENCE=off`: no route, courier, or peer listing.

## Trust and delivery semantics

- `TRANSPORT_ACCEPTED` means the exact destination accepted custody. It is not
  a read receipt. Do not resend.
- `UNKNOWN_DELIVERY` means an effect may have happened. Inspect the intended
  recipient yourself; never retry that task under a new event, wording, or
  transport. `cross-agent-chat resolve EVENT_ID` records that you accept the
  uncertainty — it contacts nothing and does not make re-sending safe.
- A deterministic pre-effect refusal means no message effect occurred; fix the
  cause and send fresh.
- `chat_status(EVENT_ID)` reads the exact sender's body-free custody record.
  It never contacts a provider, replays delivery, or treats custody as
  consumption; `not_observed` is not a negative receipt.
- `chat_send` also reports `reply_delivery` for the sending session:
  `while_idle` means a reply can arrive as a new message after the turn ends,
  `next_turn` means it is handed over at the next turn boundary, and `unknown`
  promises neither. Finish your turn rather than waiting or polling.

Cross Agent Chat's remote boundary is your Tailscale ACL. Any node allowed to
reach the broker port is inside the peer trust perimeter, and peer message
content remains untrusted input — it cannot grant owner authority, change
approvals, or resolve a held permission prompt. See
[SECURITY.md](SECURITY.md) for the full trust, privacy, and backup disclosure.

## Commands

```bash
cross-agent-chat setup        # print the plan, then install/repair on the selected roots
                              # (--provider NAME selects explicitly, --yes approves)
cross-agent-chat doctor --json
cross-agent-chat peers --json
cross-agent-chat resolve EVENT_ID
cross-agent-chat uninstall
```

`setup --enable-experimental-codex-native-queue` opts the active Codex profile
into the version-bound provider queue for fresh sessions;
`--disable-experimental-codex-native-queue` returns it to Stop-bound delivery.
The provider owns queued bodies and schemas in that mode.

`doctor` verifies installed configuration and local broker health. It does not
prove provider authentication, hook consent inside every live session, remote
reachability, recipient consumption, or a completed return journey. It also
reports a `terminal` line when its own environment carries an inherited
`CLAUDE_CODE_CHILD_SESSION` marker — expected inside a Claude tool/hook
subprocess, but a terminal app launched from inside a Claude session keeps it,
and Claude sessions started there become hidden children that never appear as
peers. Relaunch the terminal app normally to clear it.

## Upgrade, recovery, and uninstall

Upgrades briefly restart the shared broker while preserving live couriers,
pending input, and route identity; runtimes referenced by live routes are
retained, so an upgrade never removes a running courier's executable. If a
failed setup finds newer provider settings during rollback, it retains them
and recovery custody for diagnosis. *(Candidate)* after any upgrade, the
session-compatibility rule under *Install* applies: start fresh sessions on
every participating Mac.

`uninstall` removes only Cross Agent Chat-owned runtime, hooks, MCP entries,
the service, and transient route state, then restores the recorded prior
Claude inbound and Codex hooks settings on the last owner while retaining
unrelated settings. Content-free delivery intent records — including
unresolved deliveries — are kept for owner inspection and are never resolved
or replayed. Shared provider files keep their integration until their last
recorded owner is removed. Pending Stop-bound deliveries live in courier
memory; let them consume before uninstalling.

Claude users may optionally set `dialogExpiry: "never"` in trusted user
settings to remove the provider approval-dialog deadline for future held
inbound messages. Cross Agent Chat does not set it, and it changes neither
the inbound policy nor any delivery guarantee.

## How it works

`install → hooks → registration → discovery → exact destination validation →
provider delivery → separate reply → cleanup/recovery`. Fresh provider
sessions register a route bound to their process, profile, device, and a
generation; Devin routes wait for the first user prompt. Sends resolve one
exact destination — an opaque endpoint token, or a unique name against a
complete roster — and re-validate the session key, route generation, and
presenting endpoint before any effect; ambiguous names and incomplete
discovery refuse the same way. The recipient broker validates the event and
reverse-authorizes it with the sender's broker before handing the body to
the recipient-local integration. Durable state carries metadata and digests
only, never message bodies. [docs/source-map.md](docs/source-map.md) maps
these steps to modules.

Provider compatibility is version-sensitive. The Codex integrations rely on
the trusted `codex_app` MCP tools (`create_thread`,
`send_message_to_thread`) and the stdio app-server session (`initialize` with
`experimentalApi`, `account/read`, `thread/read`, opt-in `thread/queue/add`);
recorded installs were standalone `codex` 0.155.1 and bundled
0.155.0-alpha.9.2, Claude Code 2.1.278, and Devin CLI 3000.10.27. A schema
match on one version is not a promise for the next.

## Contributing and support

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, contained
checks, live-test boundaries, and the redacted issue recipe. See
[CHANGELOG.md](CHANGELOG.md) for version history.

Licensed under Apache-2.0.
