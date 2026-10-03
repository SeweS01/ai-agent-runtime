# Local Runtime protocol v1

Transport is a same-user Unix stream socket with length-delimited JSON frames. Each
request carries `protocol_major`, `protocol_minor`, `action`, and a globally unique
`request_id`. Runtime verifies `SO_PEERCRED`; wrapper registration additionally
requires its claimed PID and UID to match the socket peer.

Successful mutating responses are stored durably with a digest of the request. A
retry using the same ID and payload returns the original outcome, including after a
daemon restart. Reusing an ID with a different payload is rejected.

## Wrapper actions

- `register`: Phase A launch authorization. Inputs include AgentInstance, wrapper
  PID/starttime/UID, cwd, tmux session/pane, and optional resume session ID.
- `verify_started`: Phase B verification after the real child starts. Inputs add
  instance/binding IDs and child PID/starttime.

Unknown binding, unverified resume provenance, config drift, ambiguous identity, and
Codex subprocess mode fail closed.

## Control-plane action

`apply_binding` accepts a binding identity and complete DesiredSpec. Before touching
Telegram Agent configuration, Runtime verifies:

- exact AgentInstance and forum chat;
- canonical cwd containment in the configured project roots;
- immutable binding/topic identity and no implicit project relocation;
- provider/account profile safety;
- canonical DesiredSpec digest;
- monotonic revision within the existing generation.

The upstream-created topic key must already exist. Runtime then performs a fresh-read
allowlisted patch, atomic replace, backup, and post-write verification before storing
the local effective spec. A different generation is rejected from this incremental
action and requires `apply_snapshot`.

`apply_snapshot` is the only action allowed to introduce a new generation. It
contains the complete AgentInstance desired inventory, one common generation, a
canonical snapshot digest, and explicit `retired_binding_ids`. Runtime validates all
identities, profiles, digests, topic keys, and inventory coverage before one batched
topic-config replacement and one SQLite transaction. Existing bindings may not be
silently omitted. Retirement disables future managed launches and removes the local
effective spec, but preserves binding, instance, and session-provenance history.

This protocol is local execution control, not a public CUS backend API. The CUS
backend remains provider-neutral and talks through its Runtime connector.

## Durable outbox

`outbox_pull` claims up to 100 due events and advances a bounded exponential retry
lease. `outbox_ack` finalizes an event as `ACKED`, `STALE_OBSERVED`, or
`POLICY_REJECTED`; replaying the same ACK is idempotent, while changing a final ACK
is rejected. Observed proposals carry their base generation/revision/digest so CUS
can reject delayed state rather than overwrite a newer user choice.
