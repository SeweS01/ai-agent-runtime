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

## Exact release qualification manifest

Every real harness writes one JSON evidence artifact for its gate. Evidence must be
from `environment=staging`, contain the exact required assertion IDs, carry its own
canonical digest, and reference the digest of one exact release tuple. The retirement
race artifact additionally records, for each deterministic injection window, either
`exactly_once` or `explicit_not_delivered`; silent loss, duplicate execution and
misrouting must all be explicitly false.

The release tuple JSON has exactly these fields:

```json
{
  "telegram_agent_sha": "<40 lowercase hex>",
  "codex_version": "<exact version>",
  "codex_binary_sha256": "sha256:<64 lowercase hex>",
  "runtime_release": "<exact installed release_id, including immutable source identity>",
  "runtime_wheel_sha256": "sha256:<64 lowercase hex>",
  "protocol_major": 1,
  "cus_connector_version": "<exact connector release>"
}
```

`runtime_release` is not merely the Python package version. It must equal the
`release_id` in the verified Runtime install report (for example,
`runtime-0.1.0-bb527eb7ab87`). `runtime_wheel_sha256` must equal the digest in that
same verified install report. Together they bind all qualification evidence to one
side-by-side installed wheel; evidence for another build carrying the same package
version or release label is rejected by the deployment audit.

Pin the already-produced evidence files into an immutable manifest, repeating
`--evidence` once for every required gate, then verify it:

```bash
ai-agent-runtime qualification manifest \
  --report-id staging-2026-10-04-001 \
  --release-tuple /srv/qualification/release-tuple.json \
  --evidence /srv/qualification/evidence/upstream-source-contract.json \
  --evidence /srv/qualification/evidence/phase-correlation.json \
  --evidence /srv/qualification/evidence/real-codex-launch.json \
  --evidence /srv/qualification/evidence/rollback-drill.json \
  --output /srv/qualification/qualification-manifest.json

ai-agent-runtime qualification verify \
  --manifest /srv/qualification/qualification-manifest.json \
  --output /srv/qualification/qualification-report.json
```

The first command only hashes and pins evidence; it always reports
`qualified=false`. Only `qualification verify` can produce `QUALIFIED`, and it does
so only when every gate defined by the installed Runtime release is present, intact,
bound to the same tuple/report, and fully passing. The report keeps
`TELEGRAM_E2E_VERIFIED` separate and explicitly does not infer Runtime health.

Runtime retirement is additionally feature-gated in configuration. It remains off by
default; `retirement_enabled = true` is invalid without a non-empty
`retirement_qualification_report_id`. That identifier is an audit reference, not a
substitute for the evidence required above.

The source-level baseline checks can be run against a clean checkout:

```bash
uv run python tools/qualify_telegram_upstream.py /path/to/telegram-ai-agent \
  --release-tuple /srv/qualification/release-tuple.json \
  --report-id staging-2026-10-04-001 \
  --environment staging \
  --output /srv/qualification/evidence/upstream-source-contract.json
```

This verifies exact SHA, tmux naming, spawn-before-state-save ordering, and the
sanitized `env -i` contract and emits the `upstream_source_contract` evidence bound
to the exact release tuple. A local run may use `--environment local`, but that
artifact is deliberately rejected by the production release verifier. This gate does
not replace real Telegram/Codex E2E or retirement race tests.

The deterministic retirement harness must be executed with the pinned upstream
Python environment. It imports the exact upstream `TmuxManager` and Telegram send
path, injects external tmux disappearance in all six required windows, and records
per-prompt submission and explicit-alert evidence:

```bash
/path/to/telegram-ai-agent/.venv/bin/python \
  tools/qualify_retirement_race.py \
  --upstream /path/to/telegram-ai-agent \
  --release-tuple /srv/qualification/release-tuple.json \
  --report-id staging-2026-10-04-001 \
  --environment staging \
  --output /srv/qualification/evidence/retirement-race.json
```

The harness uses deterministic tmux I/O test doubles but real pinned upstream
control-flow. An upstream alert callback is evidence of an explicit-not-delivered
outcome; actual Telegram delivery remains covered by the separate `telegram_e2e`
gate. A locally produced artifact remains `environment=local` and cannot satisfy the
release verifier.

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

After reviewing that manifest, stage its exact files into a fresh candidate root:

```bash
ai-agent-runtime storage stage \
  --manifest /var/lib/ai-agent-runtime/migration/session-storage.json \
  --output /var/lib/ai-agent-runtime/migration/session-storage-stage.json
```

Staging re-hashes every source before and after the copy, rejects any symlink or
file/directory collision, copies through no-follow file descriptors, publishes only
to a previously absent target, and verifies the complete staged file inventory.
Source profiles are not renamed, linked, modified, or removed. The resulting status
is `STAGED_NOT_ACTIVATED`: this command deliberately does not switch `CODEX_HOME`,
create shared-session links, or claim lifecycle qualification. Activation topology
remains a pinned-Codex qualification result, not an installer assumption.

## Rollback preservation versus rollback qualification

Create and verify the legacy rollback manifest described in `deployment.md` before
cutover. That verification proves only that the exact legacy commit and selected
non-secret rollback inputs remain available and unchanged. The separate
`rollback_drill` qualification gate must still exercise restoration of session
topology while preserving sessions created after staging, followed by the legacy
end-to-end path. Neither the preservation manifest nor its verifier can emit a
passing qualification evidence artifact on its own.
