# Current slice

State: PROVEN

The v0.1.0 candidate was exercised through fresh, normally authenticated Claude Code
and Codex sessions. Same-device delivery passed. Peer-Mac discovery and a full
Codex-to-Claude request/reply journey also passed without a peer map, copied provider
credential, second product account, or automatic retry.

Independent readback confirmed one exact reply in the originating session. The sender
saw an unknown-delivery result for the request and correctly did not retry; the recipient
accepted its separately authorized reply. This proves both delivery semantics and the
owner-recovery boundary without claiming that transport acceptance means processing.

The repository retains only this content-free result. Raw transcripts, message bodies,
event identifiers, session identities, host addresses, provider account data, and device
captures remain outside the source tree.

Release review, tagged installation, and public release readback remain separate gates.

A later exact-candidate attempt failed before recipient effect: the destination could
reach the source node, but the callback redundantly required the transient source route
after an authenticated pending intent already existed. Independent recipient transcript
readback found no event or requested reply. No retry occurred. The single proof-enabling
repair replaces that race with an atomic, one-time claim bound to the exact sender
generation, target generation, and payload digest.

The bounded retry passed on the identical installed candidate. One request produced one
separately authorized reply; the request remained truthfully classified as unknown and
was not retried, the peer recipient recorded its reply as transport accepted, and the
originating Codex session consumed the exact requested reply once at its natural Stop.
This restores state `PROVEN` without weakening the no-retry or receipt semantics.
