# Qualification gates

No production switch is permitted merely because unit tests pass. A release must
record the exact Telegram Agent commit, Codex version and binary digest, Runtime
release, protocol major, CUS connector version, and qualification report.

Required blocking gates include:

- exact upstream base and live-`/resume` switch tmux naming and Phase A/Phase B ordering;
- real Codex argv, TUI, rollout discovery, `/new`/`/clear` after a switched
  `/resume` tmux name, `/resume`, `/tui`, `/kill`, and
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

For a staging-only diagnostic of Codex's hook-consent startup screen,
`[policy] qualification_disable_codex_hooks = true` makes the wrapper add the
real Codex CLI's `--disable hooks` flag to new managed launches. It never
trusts or executes unapproved hooks and leaves existing Codex processes
unchanged. Runtime status exposes `qualification_only.codex_hooks_disabled`;
the deployment audit rejects this setting for a production target. A PASS with
hooks disabled proves only that this startup modal was absent, not that normal
first-launch delivery or the production release is qualified.

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

## Release-bound staging run plan

Before starting any staging harness, generate one immutable run plan from the
verified install report, exact release tuple and clean pinned upstream checkout. The
evidence directory and both output parents must already exist, belong to the current
Unix user and have mode `0700`:

```bash
ai-agent-runtime qualification plan \
  --report-id staging-2026-10-04-001 \
  --release-tuple /srv/qualification/release-tuple.json \
  --runtime-install-report /srv/qualification/runtime-install-report.json \
  --upstream-checkout /srv/telegram-ai-agent-upstream \
  --evidence-directory /srv/qualification/evidence \
  --manifest-output /srv/qualification/qualification-manifest.json \
  --report-output /srv/qualification/qualification-report.json \
  --output /srv/qualification/staging-run-plan.json
```

The plan enumerates every required gate and assertion, pins the two installed
Runtime harness entrypoints, and supplies argv arrays for those harnesses and the
final manifest/verify steps. The other gates remain explicitly
`external_staging_required`; a plan never creates PASS evidence, starts services or
authorizes production cutover.

The source-level baseline checks are installed from the same digest-bound Runtime
wheel as the daemon and wrapper. Run the installed release entrypoint against a
clean upstream checkout:

```bash
/home/devops/.local/lib/ai-agent-runtime/releases/<release-id>/bin/ai-agent-runtime-qualify-upstream \
  /path/to/telegram-ai-agent \
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
path, injects external tmux disappearance in all six required windows plus a
delayed-accept first-launch window, and records
per-prompt submission and explicit-alert evidence:

```bash
/home/devops/.local/lib/ai-agent-runtime/releases/<release-id>/bin/ai-agent-runtime-qualify-retirement \
  --upstream /path/to/telegram-ai-agent \
  --release-tuple /srv/qualification/release-tuple.json \
  --report-id staging-2026-10-04-001 \
  --environment staging \
  --output /srv/qualification/evidence/retirement-race.json
```

The installed retirement entrypoint re-executes itself once through the pinned
upstream checkout's `.venv/bin/python`, while exposing only the installed Runtime
package path and a small allowlist of non-secret environment variables. This keeps
upstream dependencies pinned without adding Telegram packages to the Runtime daemon
environment. The harness uses deterministic tmux I/O test doubles but real pinned
upstream control-flow. An upstream alert callback is evidence of an
explicit-not-delivered outcome; actual Telegram delivery remains covered by the
separate `telegram_e2e` gate. A locally produced artifact remains
`environment=local` and cannot satisfy the release verifier.

The `telegram_e2e` gate must include a **fresh first launch** from a configured
user-created topic: prove that the upstream queue accepted the prompt, the
Codex rollout contains exactly one copy of it, and the final answer was sent
to Telegram exactly once. A Codex rollout alone is insufficient: the pinned
upstream can report `enter_did_not_clear_after_retries` while Codex later
executes the prompt, leaving transcript discovery and Telegram delivery
unstarted. Do not automatically resend after this alert. Keep the gate failed
until the live Telegram result is verified.

The `cus_bot_onboarding` gate separately requires a **user-created topic** to
receive its setup notice automatically, without an operator manually issuing
the token or sending the message. A synthetic webhook test or manual recovery
does not satisfy `user_created_topic_auto_notice`.

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

Once that topology is selected for a migration rehearsal, regenerate the M0
inventory and the digest-bound session-provenance plan described in `deployment.md`.
The `legacy_session_provenance_import` and same-binding `/resume` assertions must
prove both an imported pre-cutover current session and a session created natively
after Runtime launch. The legacy import must be rejected unless the live pane's
process identity, qualified binary digest, canonical cwd and safe `CODEX_HOME`
independently prove the same account selected by the binding plan. Any changed rollout
path/device/inode after planning must make the import fail closed; an unproven older
picker entry must also fail closed rather than being assigned by cwd.

## Rollback preservation versus rollback qualification

Create and verify the legacy rollback manifest described in `deployment.md` before
cutover. That verification proves only that the exact legacy commit and selected
non-secret rollback inputs remain available and unchanged. The separate
`rollback_drill` qualification gate must still exercise restoration of session
topology while preserving sessions created after staging, followed by the legacy
end-to-end path. Neither the preservation manifest nor its verifier can emit a
passing qualification evidence artifact on its own.
