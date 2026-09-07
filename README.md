# Cross Agent Chat

Chat between authenticated Claude Code and Codex sessions on your Mac or permitted Tailnet.

Before installing, each Mac needs a supported macOS Claude Code or Codex installation with its
own working authenticated provider session. Install Cross Agent Chat separately on every Mac and
selected provider-profile root that will use it (`CLAUDE_CONFIG_DIR` and `CODEX_HOME` select
non-default roots). Local sessions do not need Tailscale; remote sessions need Tailnet reachability
allowed by your Tailscale ACL. Cross Agent Chat does not copy credentials, synchronize accounts or
files, or turn a remote peer into an owner.

```bash
curl -fsSL https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/v0.1.5/install.sh | sh
```

The installer supplies a Python runtime when the Mac lacks a compatible one. Its default stable
command is `~/.local/bin/cross-agent-chat`; it stages releases under
`~/.local/share/cross-agent-chat-runtime`, then writes the selected Claude `settings.json` and
`.claude.json`, selected Codex `config.toml` and `hooks.json`, per-profile install metadata, and
the owner-local LaunchAgent broker. Setup can stop and replace the shared broker and transient
couriers, so inspect active consumers and selected roots before an install, upgrade, or uninstall.
Existing provider sessions keep their loaded hooks; open fresh Claude or Codex sessions afterward.

Start a fresh session, then ask naturally:

> List my live Cross Agent Chat peers and send hello to the Claude session on my other Mac.

Cross Agent Chat follows the provider processes you already use. There are no peer files,
Cross Agent Chat accounts, or terminal-specific extensions. Permitted online Tailnet Macs appear
automatically.
Use the opaque exact `handle` returned by `chat_peers` when display labels repeat. Fuzzy names are checked across devices;
multiple matches or incomplete remote discovery require a more precise recipient.
`chat_peers` reports the invoking sender's readiness separately from each recipient's delivery mode.

Disposable worker launchers can set `CROSS_AGENT_CHAT_PRESENCE=off`. That worker remains out
of Cross Agent Chat's peer roster and creates no route or courier; ordinary sessions remain
visible by default.

To have an existing coding agent assist with installation, give it this prompt:

> Inspect this repository and install the latest released Cross Agent Chat tag, not an arbitrary
> PR. Identify active consumers and the selected Claude/Codex roots, obtain approval before shared
> effects, preserve existing intent records, run `cross-agent-chat doctor --json`, and test only
> fresh actors. If the README's v0.1.5 candidate tag is not published, report that condition rather
> than substituting a branch.

## Supported surfaces

| Surface or mode | Support |
|---|---|
| Claude Code 2.1.263 | iMac interactive startup and same-cwd native-messaging request/reply tested with the configured cross-session inbound `accept` policy |
| Claude Code 2.1.261 (historical observation) | Interactive and native background sessions; native SendMessage delivery |
| Codex CLI 0.152.1 / 0.153.2 (historical observation), default Stop | Next-turn delivery; accepted work remains pending while genuinely idle |
| Codex Native, embedded 0.153.1 (historical observation), default Stop | Next-turn delivery; accepted work remains pending while genuinely idle |
| Explicit experimental Codex native queue | Version-bound idle queue path observed on Native 0.153.1 and CLI 0.152.1/0.153.2; not selected by default |
| Same Mac | Yes |
| Tailnet Mac | Yes, subject to your Tailscale ACL |
| ChatGPT web or Claude web | No |
| Windows | No claim |
| Linux | No live-support claim |

These are per-version observations, not v0.1.5 beta certification, fleet parity, or
account/profile-parity claims. The package classifier remains Alpha while beta acceptance is open.

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
Codex Stop-bound delivery, or the experimental Codex queue. Older couriers report `unknown`.
The mode identifies the active adapter; it does not establish consumption or a reply.

Codex uses natural Stop delivery by default. An explicit, profile-local experimental queue can
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

Running the installer again upgrades and repairs the owned configuration. An upgrade or
uninstall pauses Cross Agent Chat messaging and stops its couriers, not Claude or Codex coding
processes. Let pending Stop-bound deliveries consume before that pause; new sessions after an
upgrade load the updated integration. A temporary profile does not isolate that shared service.
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

`setup` uses the active provider roots: by default Claude reads `~/.claude/settings.json` and
`~/.claude.json`, while an explicit `CLAUDE_CONFIG_DIR=/path/to/profile` reads
`/path/to/profile/settings.json` and `/path/to/profile/.claude.json`. Codex uses the active
`CODEX_HOME` for `config.toml` and `hooks.json`. Configure each selected root separately; setup,
doctor, backups, and uninstall stay on that exact root. Existing same-root account switches need
fresh provider sessions; Cross Agent Chat does not copy credentials or retarget live sessions.
Use `setup --disable-experimental-codex-native-queue` to return that profile's fresh Codex
sessions to next-turn delivery.

## Architecture

The path is `install → hooks → registration/bootstrap → discovery → exact destination validation
→ provider delivery → separate reply → cleanup/recovery`. Setup merges owned Claude SessionStart/
SessionEnd and Codex SessionStart/SessionEnd/Stop hooks into the selected provider roots and starts
one owner-local LaunchAgent broker. A fresh hook registers its provider, process and profile context;
bootstrap health and later native-provider health are checked separately.

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
