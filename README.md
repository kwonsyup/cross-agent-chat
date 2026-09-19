# Cross Agent Chat

Local-first chat between authenticated Claude Code, Codex, and local Devin sessions on your Mac
or permitted Tailnet.

Before installing, each Mac needs a supported macOS Claude Code, Codex, or local Devin installation with its
own working authenticated provider session. Install Cross Agent Chat separately on every Mac and
selected provider-profile root that will use it (`CLAUDE_CONFIG_DIR` and `CODEX_HOME` select
non-default roots). Local sessions do not need Tailscale; remote sessions need Tailnet reachability
allowed by your Tailscale ACL. Cross Agent Chat does not copy credentials, synchronize accounts or
files, or turn a remote peer into an owner.

Install v0.3.7 prerelease with:

```bash
curl -fsSL https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/v0.3.7/install.sh | sh
```

Installation requires Git because the installer builds from the release tag.

The installer supplies a Python runtime when the Mac lacks a compatible one. Its default stable
command is `~/.local/bin/cross-agent-chat`; it stages releases under
`~/.local/share/cross-agent-chat-runtime`, then writes the selected Claude `settings.json` and
`.claude.json`, selected Codex `config.toml` and `hooks.json`, local Devin global MCP and hook
settings, per-profile install metadata, and the owner-local LaunchAgent broker. Setup can replace the shared broker, so inspect active
consumers and selected roots before an install, upgrade, or uninstall. Upgrades preserve existing
couriers and their route generations. Existing sessions keep their loaded integration; fresh
Claude and Codex sessions use the updated tools and hooks. A local Devin conversation joins the
peer roster when its first user prompt is submitted.

Start a fresh session, then ask naturally:

> List my live Cross Agent Chat peers and send hello to the session on my other Mac.

Codex uses Stop-bound delivery by default: accepted input waits for its next natural turn and is
not an idle-wake guarantee. The experimental queue remains an explicit profile-local opt-in.

Cross Agent Chat follows the provider processes you already use. There are no peer files,
Cross Agent Chat accounts, or terminal-specific extensions. Permitted online Tailnet Macs appear
automatically.
Use the opaque exact `handle` returned by `chat_peers` to select a recipient. Display names are
checked across devices; multiple matches or incomplete discovery require an exact handle.
`chat_peers` reports the invoking sender's readiness separately from each recipient's delivery mode.
An incomplete roster may be refreshed with another read-only `chat_peers` call; that does not
authorize resending an accepted or unknown message. An exact handle stays valid for the life of
that peer session, so `chat_peers` is for discovery and for when an exact handle stops resolving,
not a required step before every send.

A delivered Cross Agent Chat message arrives through your provider's own inbox, so its visible
sender is this host's Cross Agent Chat delivery helper, not the peer. Reply to the `Reply via CAC
to handle:` value in the message's envelope; replying to the visible sender address reaches the
helper, which is already gone.

Disposable worker launchers can set `CROSS_AGENT_CHAT_PRESENCE=off`. That worker remains out
of Cross Agent Chat's peer roster and creates no route or courier; ordinary sessions remain
visible by default.

To have an existing coding agent assist with installation, give it this prompt:

> Install the released `v0.3.7` tag, not an arbitrary PR. Identify active consumers and the
> selected Claude/Codex roots and local Devin configuration, obtain approval before shared effects, preserve existing intent
> records, run `cross-agent-chat doctor --json`, and test only fresh actors.

## Supported surfaces

v0.3.7 is a macOS prerelease. It runs the owned user-facing broker with launchd's Standard
scheduling class to avoid the observed Background scheduling delay. Normal-budget discovery has
been observed for participating macOS nodes. An aggregate roster can still be incomplete when an
online non-CAC Tailnet node, such as an iOS node, fails discovery, and an unresponsive neighbor can
delay a send to an exact known peer by up to about 22 seconds. Claude Code request/result was
observed on public v0.3.6 between two Macs in both directions, and on one Mac, with exact
payloads and idle receipt. Other remote pairings are not established by that evidence.

The integrations attach to the provider's own process, configuration, hooks, and messaging
operations, not to a terminal emulator, so ordinary iTerm2, Terminal.app, or Ghostty launches of
the same supported provider and profile use the same path. tmux, SSH, IDE-hosted, and other hosts
still need the provider's own process, authentication, and hooks to load normally.

Claude Code uses its native cross-session mechanism. Codex Native uses the built-in Desktop message
operation through a trusted, automatically managed helper;
its body and task-creation arguments remain private to the trusted hook path. Local Devin uses its
global MCP and lifecycle hooks; a conversation becomes discoverable only after its first user prompt,
then receives work at a prompt or Stop boundary through its exact provider identity.

| Surface | Current behavior |
|---|---|
| Claude Code | Native cross-session delivery through the selected Claude configuration. |
| Codex Native App | Trusted hooks can provision a native helper and use Desktop-native task messaging for a bound original conversation. |
| Codex CLI | Stop-bound by default: a queued message is handed over at the conversation's next turn boundary. With the experimental queue it can arrive while idle. A fresh public v0.3.6 Codex CLI requester received its Claude answer. Busy acceptance and later original-owner consumption still require revalidation. |
| Local Devin CLI or App | Global MCP and prompt/Stop hooks support prompt-active conversation discovery and delivery. |
| Same Mac or permitted Tailnet Mac | Discovery and delivery use the local broker or your Tailscale ACL. Normal-budget discovery has succeeded for participating macOS nodes; an aggregate roster may remain incomplete for an online non-CAC Tailnet node. |

Codex CLI busy original-native ingress is not proven, and an active CLI writer may reject native
ingress. Devin idle delivery and a safely isolated Fusion helper remain parked provider boundaries.
Unprompted Devin conversations are intentionally not published. This prerelease is not full five-surface
technical readiness or final fleet acceptance. ChatGPT web, Claude web, Windows, and Linux have no
live-support claim.

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
- `chat_status(EVENT_ID)` reads the exact sender's body-free custody record. It never contacts a
  provider, replays delivery, or treats custody as consumption.

`chat_peers` also reports each recipient's observed delivery mode: Claude native messaging,
Codex Stop-bound delivery or the experimental Codex queue, and Devin prompt/Stop-bound delivery.
Older couriers report `unknown`.
The mode identifies the active adapter; it does not establish consumption or a reply.

A `chat_send` result also reports `reply_delivery` for the sending session itself: `while_idle`
means a requested answer can arrive as a new message after that session's turn ends; `next_turn`
means it is handed over only at the session's next turn boundary, normally after its user's next
message; `unknown` promises neither. Senders should finish their turn rather than wait or poll.

Codex uses natural Stop delivery by default: a received message is delivered at the next natural
turn boundary. An explicit, profile-local experimental queue can
be enabled for fresh Codex sessions; it uses Codex's version-bound stdio app-server
`thread/queue/add` interface, whose provider owns queued message bodies. The Cross Agent Chat
state remains content-free. The experimental path was observed on Codex Native 0.153.1 and CLI
0.152.1/0.153.2; it is not enabled by default and must not be treated as a fleet-wide guarantee.
Active work is not interrupted. Provider transcripts contain delivered messages; persistent
Cross Agent Chat state contains metadata and digests, never message bodies.
Claude delivery uses two constrained Haiku helper calls, so its latency and account quota also
depend on those calls. Helpers retain the recipient's executable and selected profile context.

## Commands

```bash
cross-agent-chat setup
cross-agent-chat setup --enable-experimental-codex-native-queue
cross-agent-chat doctor --json
cross-agent-chat peers --json
cross-agent-chat uninstall
```

Running the installer again upgrades and repairs the owned configuration. Upgrades briefly
restart the shared broker while preserving existing couriers, pending input, and route identity.
Older runtimes are retained while route registrations remain, so repeated upgrades do not remove
a live courier's executable. Fresh sessions load the updated integration. Uninstall stops
couriers; let pending Stop-bound deliveries consume before uninstalling. A temporary profile
does not isolate the shared service. Neither operation restarts Claude, Codex, or Devin coding processes.
If a failed setup or upgrade finds newer provider settings during rollback, it retains those
settings and recovery custody for diagnosis before retrying.
`uninstall`
removes only Cross Agent Chat-owned runtime, hooks, MCP routes, service, and transient route
state, and restores the prior shared Claude inbound setting. Durable content-free delivery intents
remain intact, including unresolved delivery records; uninstall never resolves or replays them.
When another configured profile remains, uninstall keeps the shared runtime and broker.
Shared provider files, including symlinked files, retain their integration until their last
recorded owner is removed. The last owner restores the recorded original Claude inbound and
Codex hooks settings while retaining unrelated settings. Older install records that lack file
ownership information are handled conservatively; unavailable original values are not invented.
Sharing only one of a Codex config file and its hook file across profiles is rejected before
setup writes.

Claude users may choose `dialogExpiry: "never"` in trusted user settings to remove the provider
approval-dialog deadline for future held inbound messages. It does not change the recipient inbound
policy, extend an existing hold, survive recipient shutdown, or guarantee delivery. Cross Agent
Chat does not set it globally or store/retry message bodies.

`setup` uses the active provider roots: by default Claude reads `~/.claude/settings.json` and
`~/.claude.json`, while an explicit `CLAUDE_CONFIG_DIR=/path/to/profile` reads
`/path/to/profile/settings.json` and `/path/to/profile/.claude.json`. Codex uses the active
`CODEX_HOME` for `config.toml` and `hooks.json`. Configure each selected root separately; setup,
doctor, backups, and uninstall stay on that exact root. Existing same-root account switches need
fresh provider sessions; Cross Agent Chat does not copy credentials or retarget live sessions.
Use `setup --disable-experimental-codex-native-queue` to return that profile's fresh Codex
sessions to next-turn delivery. Local Devin integration is global at `~/.config/devin`; it preserves
unrelated MCP and hook entries and becomes active for a conversation after its first user prompt.

## Architecture

The path is `install → hooks → registration/bootstrap → discovery → exact destination validation
→ provider delivery → separate reply → cleanup/recovery`. Setup merges owned Claude SessionStart/
SessionEnd and Codex SessionStart/SessionEnd/Stop hooks into the selected provider roots, adds the
local Devin lifecycle and MCP integration, and starts one owner-local LaunchAgent broker. A fresh
Claude or Codex hook registers its provider, process and profile context; a Devin route waits for
its first user prompt. Bootstrap health and later native-provider health are checked separately.

The broker discovers live routes. `chat_send` resolves one exact, current destination from an exact
or unique fuzzy query; ambiguous names and incomplete discovery are rejected before an effect. It
hands delivery to the recipient's process-scoped courier (or, only with explicit profile-local
opt-in, Codex's version-bound native queue). A reply is a separate send and consumption event, not
proof supplied by the original send.
Session-end hooks remove owned live routes; recovery preserves content-free intent metadata and does
not replay accepted or unknown events.

Local couriers use owner-local sockets. Remote broker traffic binds directly to the Mac's Tailnet
address and relies on the Tailnet ACL; SSH is operator tooling, not product transport. A default
Codex Stop-bound courier holds pending input only in memory, so it can lose it if that courier exits
before the next natural Stop. The experimental queue leaves the message body with Codex's provider
queue and transcript, while Cross Agent Chat retains only metadata and digests. Neither mode
synchronizes permissions, accounts, files, or provider state between Macs.

### Provider updates and contained contributor checks

For a provider update, record the installed provider version, selected profile roots, and native
queue schema/mode; run the affected contained adapter contracts; perform one fresh owned idle,
busy, or compaction smoke as applicable; then update the tested matrix. Do not infer support for
future provider versions from these checks.

From a development environment with the dev dependencies installed, run the contained checks with:

```bash
python -m pytest tests/test_install.py tests/test_codex.py tests/test_claude_remote.py tests/test_tailnet.py
```

The test fixtures redirect `HOME`, `CODEX_HOME`, sockets, temporary files, and provider/service
calls into fixture-owned paths; they test contracts and failure boundaries, not a live account or
installed macOS service. They also guard the subprocess calls made by the suite, but are not an OS
sandbox for every arbitrary child process. Review any new child-process path and use an approved
environment for live installation or provider testing.

See [SECURITY.md](SECURITY.md) for the trust boundary and vulnerability reporting.

Licensed under Apache-2.0.
