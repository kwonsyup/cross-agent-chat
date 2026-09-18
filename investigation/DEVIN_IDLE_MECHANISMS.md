# Devin idle-delivery mechanisms

Question: after the ORIGINAL already-open local Devin conversation (Devin CLI REPL
or the Devin Native App chat) has finished its turn and is genuinely idle, can an
externally arriving message be delivered into THAT SAME live conversation so the
model processes it without another user prompt? Excluded by the requirement:
`devin -r/--resume`, a new `devin acp` session, a cloud session, a background
worker held active, a transcript scraper, or typed keystrokes.

Checked against the installed build:

- CLI: `devin 3000.10.27 (bcbe88c7)` — `/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin --version`
- App: `CFBundleShortVersionString = 3.10.27` (`defaults read /Applications/Devin.app/Contents/Info.plist`); `product.json` reports `version: 1.126.0`, `urlProtocol: devin`, `oldUrlProtocol: windsurf`

Architecture fact that frames every row below: the REPL is itself an ACP client
of an in-process `devin acp` child it spawns ("the in-process ACP server the REPL
spawns (a `devin acp` child that inherits the env)" — `devin acp --help`). The
session therefore lives inside a private stdio pipe pair between the REPL and
its child; there is no second client endpoint. Separately, the binary enforces a
per-session cross-process lock: `Failed to lock session '…'`, `'…' is already
open in another process. Close the other instance before opening it here.` and
`… is already open in another process. Close the other instance before deleting
it.` (strings in the 3000.10.27 binary). So even a mechanism that could *resume*
the same session id elsewhere cannot do so while the original is open — and once
the original is closed the writer is a replacement, not delivery into the
original.

| mechanism | evidence (command or URL + quoted output) | attaches to the original idle conversation? | why |
|---|---|---|---|
| CLI subcommand surface (recursive `--help` of every subcommand) | `devin --help` lists: `auth mcp models doctor rules skills plugins cloud desktop list rm ssh forward update version migrate sandbox setup uninstall acp help`. Every subcommand's `--help` enumerated (incl. `auth login/logout/status`, `mcp add/list/get/remove/login/logout/enable/disable`, `cloud drs …`, `plugins install/list/info/update/remove/prune`, `migrate hooks/workflows`, `sandbox setup`). | no | No `attach`, `send`, `message`, `inbox`, `notify`, `server`, `daemon`, `socket`, `remote-control`, or `api` subcommand exists. Guessed names (`devin send`, `inject`, `notify`, `daemon`, `socket`, `api`, `serve`, `remote`, `format`) all fall through to top-level help. |
| Hidden CLI commands | `devin connect --help` → "Connect this machine to a cloud Devin session for local tool use … `Usage: devin connect [OPTIONS] <CODE>`"; `devin worker --help` → "Run as an Outposts worker: start the worker or manage outposts"; `devin _internal --help` → only `self-manage`, `generate-man`; `devin acp --cloud` (changelog: "relays the ACP connection to Devin cloud instead of running the local agent"). | no | `connect` pairs this machine to a *cloud* session as a tool executor; `worker` serves queued *cloud* sessions; `_internal` is build/install tooling; `acp --cloud` is a cloud relay. None touches a local REPL conversation (and cloud paths are excluded anyway). |
| ACP `session/load` of the same session id | Throwaway `devin acp` (pid captured and killed): `initialize` result → `"agentCapabilities": { "loadSession": true, … "sessionCapabilities": { "list": {}, "delete": {}, "additionalDirectories": {} } }`. ACP spec (https://agentclientprotocol.com/protocol/session-setup): "`session/load` … Restore session context … The Agent MUST replay the entire conversation to the Client". | no | `session/load` restores the session **inside the `devin acp` process that received the request** — a second writer holding a *new* live copy of the same session record, not the REPL's live conversation. Independently, the session lock (`'…' is already open in another process`) rejects opening a session still open in the REPL. Functionally equivalent to `devin -r` — excluded. |
| ACP `session/resume` / `session/close` | Same `initialize` response: `sessionCapabilities` advertises only `list`, `delete`, `additionalDirectories` — no `resume`, no `close`. | no | Not advertised by this build; even where spec'd, `session/resume` "reconnect[s] to an existing session" in the serving agent process — still a second writer, not a push into the original. |
| ACP private `_meta` extensions (`cognition.ai/queuedMessages`, `userShellCommand`, `sessionUserActions`, `sessionHeartbeat`, `sessionReboot`, `secretSend`, `tailReplay`, …) | `initialize` `_meta` lists e.g. `"cognition.ai/multiRootWorkspace": true`, `"cognition.ai/userShellCommand": true`, `"cognition.ai/chains": true`; binary strings add `cognition.ai/queuedMessages`, `cognition.ai/sessionReboot`, `cognition.ai/sessionHeartbeat`, `cognition.ai/sessionUserActions`, `cognition.ai/tailReplay`. Changelog: "Queued messages render in a distinct 'N queued' section above the input box". | no | These are the private protocol between Devin's own REPL/Desktop client and its spawned `devin acp` child over the child's private stdin/stdout. `lsof -U` shows only anonymous socketpairs (`unix 0x… ->0x…`), no named socket — an external process cannot reach the child's stdio. Message queueing itself is user-typed input in the REPL's own input box while a turn is busy; it cannot fire while idle. |
| MCP server-initiated notifications (`notifications/message`, `notifications/resources/updated`, `tools/list_changed`, …) | Docs (bundled `extensibility/mcp/overview.mdx` + https://docs.devin.ai/cli/extensibility/mcp/overview.md): "its tools become available to the agent … The agent discovers what tools the server provides" — tools and prompts only; no notification semantics documented. Binary contains the rmcp notification types (`LoggingMessageNotification`, `ResourceUpdatedNotification`, `ToolListChangedNotification`, …). | no | No documented or observable path turns an MCP notification into a new agent turn. The wire types exist in the client library, but the agent loop is driven by prompts; a notification has no injection point into an idle session. |
| MCP server-initiated requests (`sampling/createMessage`, `elicitation/create`, `roots/list`) | Binary contains `sampling/createMessage`, `elicitation/create`, `roots/list` and client-capability fields `roots`, `sampling`, `elicitation`. | no | Sampling/elicitation/roots are requests a server may make **while the agent is actively calling that server** inside a turn — they are answered by the in-flight turn machinery and cannot originate a turn on an idle session. |
| Lifecycle hooks | Bundled docs `extensibility/hooks/overview.mdx` + https://docs.devin.ai/cli/extensibility/hooks/lifecycle-hooks.md list exactly 8 events: `PreToolUse`, `PostToolUse`, `PermissionRequest`, `UserPromptSubmit`, `Stop`, `PostCompaction`, `SessionStart`, `SessionEnd`. Hook types are only `command` and `prompt`. | no | No idle, notification, or timer event exists. Every event fires on a prompt/turn/session boundary the agent itself creates — hooks (CAC's current mechanism) can piggyback on a boundary (`UserPromptSubmit` additionalContext, `Stop` block) but cannot *create* one while the session sits idle. |
| App URL scheme `devin://` / `windsurf://` | `Info.plist`: `CFBundleURLSchemes = ["devin", "windsurf"]`; `product.json`: `"urlProtocol": "devin"`. Extension `dist/extension.js`: `window.registerUriHandler(this._uriHandler)` and the handler dispatches only `"/refresh-authentication-session"===A.path ? refreshAuthenticationSession() : maybeHandleUriWithToken(A)` — auth-redirect handling only. | no | The only registered URI handler consumes authentication redirects (`parseAuthRedirectUri`). No URL route targets conversation input. |
| App commands (`package.json` contributes + internal ids) | `extensions/windsurf/package.json` `contributes.commands`: 60 entries — closest are `devin.addCurrentFileToChat` ("Add current file to conversation") and `devin.newConversation`/`windsurf.triggerCascade` ("Start new conversation"). Internal `devin.sendChatActionMessage` takes `ChatActionType` ∈ {`promise`, `toggleFocus`, `openChatPanel`, `codeBlockMention`, `fileMention`, `setCascadeId`, `markCascadeIdActive`, `setApiKey`, `setEligibleDevinAccountsState`, `setUserIdentity`, `setTelemetryState`, `updateStateForCascadeFilesWithInIdeDiffs`, `explainAndFixProblem`, `devinGitBranchChanged`, `devinEnterReviewMode`, `showResolveGitWorktreeChangesModal`}. | no | No command submits a user message into an existing conversation; `sendChatActionMessage` only pushes panel UI state (mentions, ids, account data). Commands are internal `commands.executeCommand` ids — not externally invocable without driving the app's UI, which reduces to typed input anyway. |
| Local IPC / socket endpoint on a running session | `lsof -U | grep devin` on this host: all `devin` unix fds are anonymous socketpairs (`unix 0x… ->0x…`), i.e. REPL↔`devin acp` child pipes and MCP stdio — no filesystem socket, no listener. Binary strings show `devin-listener`/`devin-ssh` sockets only for desktop-open and `devin ssh` paths. | no | There is no IPC endpoint an external process can connect to in order to reach a live session's turn loop. |
| In-session slash commands and input box (`/loop`, `/btw`, `/handoff`, queued messages) | `reference/commands.mdx`: `/loop <prompt>` "Run a prompt then auto-review the diff in a loop"; `/btw <prompt>` "Ask a quick side question"; `handoff.mdx`: `/handoff` "transfer the current session to a cloud Devin session". Changelog: "a way to flush queued messages … pressing Enter on an empty input box while the agent is busy". | no | All are typed by the user inside that session's own input box — the very "another user prompt" the requirement excludes; `/handoff` creates a cloud session (excluded). |
| Devin Cloud API "send message" | docs.devin.ai api-reference: `POST /v3/organizations/{org_id}/sessions/{devin_id}/messages` — "Send message … Interact with an active session by sending messages to Devin" ("sessions are automatically resumed if suspended"). | no | Targets cloud-hosted Devin sessions (`devin-*` ids on `api.devin.ai`), not the local CLI conversation — cloud sessions are excluded regardless. |
| `devin -c` / `devin -r` / `devin list` / `devin rm` | `devin --help`: `-c, --continue`, `-r, --resume [<SESSION_ID>]`; `devin list` / `devin rm` operate on stored sessions. | no | Resume spawns a replacement writer (explicitly excluded) and is blocked outright while the session is open elsewhere ("already open in another process"); `list`/`rm` are read/delete only. |

## Verdict

On the installed build (CLI 3000.10.27 `bcbe88c7`, Devin.app 3.10.27 / product
1.126.0) there is **no mechanism that delivers an externally arriving message
into the same already-open, idle Devin conversation**. Every surface that can
put text into a live session is either (a) the session's own input box — a user
prompt, or (b) the private ACP stdio between the REPL/Desktop and its spawned
`devin acp` child, which is unreachable from outside. The eight lifecycle hooks
fire only on prompt/turn/session boundaries and cannot fire while idle; ACP
`session/load` builds a second writer inside a *new* `devin acp` process and is
additionally blocked by the per-session cross-process lock (`'…' is already open
in another process`); MCP exposes no notification-to-turn path; the app's
`devin://` URI handler only processes auth redirects and no contributed or
internal command submits text to an existing conversation; and the cloud
message API addresses cloud sessions only. The boundary-injection design CAC
already uses (`UserPromptSubmit`/`Stop` hooks, PreToolUse capability injection)
remains the only reachable channel, and it cannot wake a genuinely idle
conversation.

Versions checked: Devin CLI `3000.10.27` (commit `bcbe88c7`,
`/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin`);
Devin.app `CFBundleShortVersionString 3.10.27` (`product.json` version
`1.126.0`). Docs checked: bundled `share/devin/docs` (same tree) and
https://docs.devin.ai/cli (commands, hooks, subagents, ACP, MCP pages) plus
https://agentclientprotocol.com/protocol/session-setup.
