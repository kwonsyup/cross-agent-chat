# Cross Agent Chat

Chat between live Claude Code and Codex sessions on your Mac or Tailnet.

```bash
curl -fsSL https://raw.githubusercontent.com/kwonsyup/cross-agent-chat/v0.1.0/install.sh | sh
```

That is the whole setup. The installer supplies its own Python runtime when your Mac
does not already have a compatible one. Start fresh Claude or Codex sessions, then say,
for example:

> List my live Cross Agent Chat peers and send hello to the Claude session on my other Mac.

Cross Agent Chat uses your existing signed-in Tailscale network automatically when it
is available. There are no peer maps, copied provider credentials, or additional
Cross Agent Chat accounts. Without Tailscale, sessions on the same Mac still work.

`TRANSPORT_ACCEPTED` means the exact destination accepted custody. It does not mean the
recipient has read or acted. Never automatically retry `TRANSPORT_ACCEPTED` or
`UNKNOWN_DELIVERY`.

If delivery is unknown, independently check the intended recipient. After you confirm
the event arrived or decide to abandon it, run `cross-agent-chat resolve EVENT_ID` before
sending a fresh message. Do not use `resolve` as an automatic retry step.

This first release targets macOS. Codex messages wait in destination-process memory and
are delivered at the next natural Stop; exiting that process before consumption loses
the pending message. Provider transcripts contain delivered messages even though Cross
Agent Chat persistent state does not. `cross-agent-chat doctor` verifies both provider
configuration and the live local broker. Run `cross-agent-chat uninstall` to remove only
the provider hooks, background broker, and Tailnet port that Cross Agent Chat owns.

Licensed under Apache-2.0.
