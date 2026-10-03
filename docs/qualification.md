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

The source-level baseline checks can be run against a clean checkout:

```bash
uv run python tools/qualify_telegram_upstream.py /path/to/telegram-ai-agent \
  --expected-sha 0430c4e4ddbc6b7a2cfcccd5af95c456a3d3961b
```

This verifies exact SHA, tmux naming, spawn-before-state-save ordering, and the
sanitized `env -i` contract. It is only one input to qualification and does not
replace real Telegram/Codex E2E or retirement race tests.
