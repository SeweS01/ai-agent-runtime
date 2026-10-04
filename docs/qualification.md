# Qualification gates

No production switch is permitted merely because unit tests pass. A release must
record the exact Telegram Agent commit, Codex version and binary digest, Runtime
release, protocol major, CUS connector version, and qualification report.

Required blocking gates include:

- exact upstream tmux naming and Phase A/Phase B ordering;
- real Codex argv, TUI, rollout discovery, `/new`, `/resume`, `/tui`, `/kill`, and
  `/recycle` behavior;
- full Codex session-storage lifecycle across isolated accounts;
- same-binding and same-account resume provenance;
- provider-qualified BUSY/IDLE signals;
- deterministic external-tmux-retirement race injection for every queue/paste/turn
  window from the approved specification;
- CUS offline LKG and Runtime-daemon fail-closed behavior;
- staging rollback including session-storage topology.

Failure of a gate blocks the release. It does not authorize a Telegram Agent patch,
overlay, `.pth` shadowing, a silent UX change, or unmanaged Codex fallback.

Runtime retirement is additionally feature-gated in configuration. It remains off by
default; `retirement_enabled = true` is invalid without a non-empty
`retirement_qualification_report_id`. That identifier is an audit reference, not a
substitute for the evidence required above.

The source-level baseline checks can be run against a clean checkout:

```bash
uv run python tools/qualify_telegram_upstream.py /path/to/telegram-ai-agent \
  --expected-sha 0430c4e4ddbc6b7a2cfcccd5af95c456a3d3961b
```

This verifies exact SHA, tmux naming, spawn-before-state-save ordering, and the
sanitized `env -i` contract. It is only one input to qualification and does not
replace real Telegram/Codex E2E or retirement race tests.

## Session-storage topology manifest

Before testing a shared-session candidate, create a non-destructive manifest for
every account profile. The default includes are `sessions` and
`archived_sessions`; add exact relative index/state paths discovered for the pinned
Codex version rather than assuming their names.

```bash
ai-agent-runtime --config /etc/ai-agent-runtime/prod.toml storage manifest \
  --profile work=/home/devops/.ai_profiles/codex/work \
  --profile personal=/home/devops/.ai_profiles/codex/personal \
  --shared-root /home/devops/.codex-shared \
  --include-relative index.sqlite \
  --output /var/lib/ai-agent-runtime/migration/session-storage.json
```

The command reads metadata and hashes only, never follows a session symlink, rejects
credential paths, performs no copy/link/delete, and creates a `0600` artifact without
overwrite. Any cross-profile or pre-existing-target path collision blocks the
candidate. A collision-free manifest remains `qualified=false`: real start/resume,
archive/index/state, concurrent-account and rollback tests are still mandatory.
