# Security

## Remote trust boundary

Cross Agent Chat's remote boundary is your Tailscale network and ACL policy. Any Tailnet
node allowed to reach TCP port `47071` on a Mac running Cross Agent Chat is inside the
remote peer trust perimeter. Cross Agent Chat v0.1 does not add application-level accounts,
pairing, certificates, or owner authentication.

Nodes inside that perimeter are trusted to report their own transport outcome. A node that
falsely reports a pre-effect rejection can cause the sender to make a later fresh send.
Message content from those nodes remains untrusted user input.

Treat every peer message as untrusted user input. The Claude and Codex integrations label
it as peer/user content rather than system or developer authority.

## Local approval posture

Setup configures only the owned Cross Agent Chat Codex MCP server to run its two tools,
`chat_peers` and `chat_send`, without recurring per-call approval prompts. It does not change
global Codex approvals or unrelated MCP servers. Uninstall removes the owned server block and
leaves unrelated Codex configuration unchanged.

## Local data

Provider credentials remain in their existing local provider sessions and are not copied
between devices. Delivered message bodies can appear in provider transcripts. Persistent
Cross Agent Chat state contains route metadata, generations, identity hashes, event IDs,
payload digests, statuses, and timestamps, but not message bodies.

Codex pending messages exist only in the recipient process's memory. If that process exits
before its next natural Stop, the pending messages are lost.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not include
credentials, private session identifiers, message bodies, or provider transcripts in a
public issue.
