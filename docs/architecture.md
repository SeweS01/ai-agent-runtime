# Architecture and trust boundaries

The Runtime is a local execution layer. It does not own Telegram updates, topic
routing, work-message queues, prompt injection, rollout delivery, or CUS product
state.

## Launch authorization

1. The unchanged Telegram Agent creates a tmux session and invokes
   `TELEGRAM_CODEX_BIN`.
2. `codex-wrapper` proves its pane/session identity and connects to the local Unix
   socket. It never contacts CUS.
3. Runtime performs Phase A against a fresh `topic_config.json` read, an exact
   version-qualified tmux session name, canonical cwd, and the AgentInstance
   namespace.
4. Runtime returns only a previously verified local effective spec. Unknown,
   ambiguous, unmanaged, drifted, and Codex-subprocess launches are denied.
5. Wrapper starts the real Codex as a supervised process group with an isolated
   `CODEX_HOME`.
6. Runtime performs Phase B against upstream `tmux_sessions/state.json` and another
   fresh topic-config read. Failure terminates the child group and does not create
   AppliedSpec.

Both phases are required. Cwd alone is never an identity.

## State ownership

- CUS owns authoritative desired generation/revision/digest.
- Runtime owns verified local effective specs, process evidence, session provenance,
  and the durable outbox.
- Telegram Agent owns its topic configuration and tmux state. Runtime is an
  independent file writer, so updates use eventual reconciliation rather than a
  false cross-process transaction guarantee.

## Current implementation milestone

The executable launch skeleton, local protocol, SQLite state, fresh-read adapters,
two-phase correlation, process-group supervision, and fail-closed policies are
implemented and tested. CUS connector/outbox reconciliation, provider-qualified
BUSY/IDLE evidence, rollout evidence, retirement, and real pinned-upstream
qualification remain gated work. This milestone is not a production release.
