# Deployment layout

V1 uses one upstream Telegram Agent, one forum chat, and one Runtime namespace per
AgentInstance. A host may run several independent instances under the same Unix user.

For an AgentInstance named `ta_prod_01`:

- Runtime config:
  `~/.config/ai-agent-runtime/instances/ta_prod_01.toml`;
- Runtime service: `ai-agent-runtime@ta_prod_01.service`;
- Runtime executable name: `~/.local/bin/ai-agent-runtime@ta_prod_01`;
- socket: `/run/user/$UID/ai-agent-runtime/ta_prod_01/runtime.sock`;
- wrapper executable name: `~/.local/bin/codex-wrapper@ta_prod_01`;
- Telegram Agent `TELEGRAM_CODEX_BIN` points to that instance-specific executable;
- Telegram Agent config/state, bot token, tmux prefix/namespace, and systemd service
  are also instance-specific.

The instance-specific wrapper may be a symlink to the installed `codex-wrapper`
entrypoint, provided the target satisfies upstream ownership and mode validation.
Do not use `AI_AGENT_RUNTIME_CONFIG` as the production selector: the qualified
upstream strips it with `env -i`.

The supplied units are templates/examples, not authorization to deploy. Before an
actual service is enabled, its exact paths, user ownership, forum-chat uniqueness,
upstream release tuple, staging evidence, and rollback manifest must be validated.
The production Telegram Agent drop-in must retain `KillMode=process` so its restart
does not recursively destroy qualified tmux/wrapper/Codex processes.

CI builds the wheel from the checked-out source and publishes it as the
`ai-agent-runtime-wheel` workflow artifact. Record that exact file's SHA-256 in the
release record, and install it into a
new side-by-side directory. The installer refuses symlinked or tampered wheels,
unexpected digests, unsafe release roots, and any existing release ID. It never
changes an active symlink, restarts a service, or removes an older release:

```bash
ai-agent-runtime deployment install-release \
  --wheel /srv/releases/ai_agent_runtime-0.1.0-py3-none-any.whl \
  --wheel-sha256 sha256:<exact-wheel-digest> \
  --releases-root /home/devops/.local/lib/ai-agent-runtime/releases \
  --release-id runtime-0.1.0-<git-sha> \
  --python /usr/bin/python3.12 \
  --output /srv/qualification/runtime-install-report.json

ai-agent-runtime deployment verify-install \
  --report /srv/qualification/runtime-install-report.json \
  --output /srv/qualification/runtime-install-verification.json

ai-agent-runtime deployment prepare-instance-links \
  --install-report /srv/qualification/runtime-install-report.json \
  --agent-instance-id ta_staging_01 \
  --instance-link /home/devops/.local/lib/ai-agent-runtime/instances/ta_staging_01 \
  --runtime-link /home/devops/.local/bin/ai-agent-runtime@ta_staging_01 \
  --wrapper-link /home/devops/.local/bin/codex-wrapper@ta_staging_01 \
  --output /srv/qualification/ta_staging_01-link-preparation.json
```

An upgrade repeats this command with a new release ID. The result is
`INSTALLED_NOT_ACTIVATED`; activation remains a separate audited deployment step, so
rollback can continue to reference the prior directory. The installer creates the
venv at its final unique release path because Python entrypoint shebangs are not
relocatable. A private `.installing` marker exists only while that new directory is
being populated; failure removes that new directory, while success fsyncs it and
removes the marker. Merely installing a directory never activates it.

Install report schema v2 also records a sorted SHA-256 manifest for every Runtime
distribution file named by the wheel's installed `RECORD`, including the daemon,
wrapper and both qualification entrypoints. `verify-install`, instance-link
preparation and deployment audit re-hash that manifest; retaining the original wheel
while modifying installed Python code or a harness therefore blocks activation.

`prepare-instance-links` is intentionally limited to an absent instance. It creates
one instance link to the verified release and two stable, instance-named entrypoints
through that link; it refuses to replace or upgrade any existing path. Its result is
`PREPARED_NOT_STARTED`: no service is started/restarted and no production cutover is
authorized. An upgrade of an existing instance requires a separately audited switch
procedure rather than reusing this initial-preparation command.

Capture the effective, secret-free deployment values in a digest-protected
`ai-agent-runtime.deployment-manifest.v1` artifact. Each `--instance` file is a
secret-free JSON object containing one `agent_instances` entry described below.
Build and audit the artifact before staging:

```bash
ai-agent-runtime deployment manifest \
  --target staging \
  --runtime-install-report /srv/qualification/runtime-install-report.json \
  --qualification-manifest /srv/qualification/qualification-manifest.json \
  --rollback-manifest /srv/qualification/rollback-manifest.json \
  --instance /srv/qualification/instances/ta_staging_01.json \
  --output /srv/qualification/deployment-manifest.json

ai-agent-runtime deployment audit \
  --manifest /srv/qualification/deployment-manifest.json \
  --output /srv/qualification/deployment-audit.json
```

The deployment manifest has an explicit `target` (`staging` or `production`) and
pins the verified wheel/install report, `qualification_manifest`, and
`rollback_manifest`. Each AgentInstance descriptor includes the exact
`runtime_executable`; its captured Runtime service must expose `exec_start` as the
four-item argv list `[runtime_executable, "--config", config_path, "serve"]`. The
auditor resolves both instance-named Runtime/wrapper paths back to the executables in
that install report. The qualification tuple's `runtime_release` and
`runtime_wheel_sha256` must also equal the install report's exact `release_id` and
wheel digest, so evidence cannot be reused for a different wheel built with the same
semantic version or release label. For `staging`, the
qualification manifest must be structurally valid and bound to the exact tuple, but
its only permitted errors are missing evidence gates: those are the tests staging is
being created to run. Existing invalid/tampered evidence still blocks deployment.
For `production`, every gate must already be `QUALIFIED`. Both targets require the
rollback verifier to confirm that the exact legacy inputs are unchanged and their
declared control period is active. This separation prevents a cyclic requirement to
produce real staging evidence before a staging instance can exist.

Each `agent_instances` entry identifies its Runtime config, exact Telegram checkout,
instance-named wrapper, forum chat and one-way working-bot token fingerprint. It also
contains captured effective Runtime service properties (`Type=notify`,
`Restart=always`, 2-second restart, 30-second watchdog, `PrivateTmp=false`,
`Accept=false`). `PrivateTmp=false` is an execution invariant rather than a relaxed
default: Runtime verifies the exact wrapper cwd and executable through `/proc`, and
the target kernel denies those cross-namespace symlinks from a private `/tmp` mount.
Each AgentInstance instead uses a mode-0700 `TMUX_TMPDIR` below its user runtime
directory, so tmux socket isolation does not depend on a private `/tmp` namespace.
and Telegram service properties (`KillMode=process`, exact wrapper path, Codex
autoupdate disabled). The audit re-hashes the real Codex binary, reads the pinned Git
HEAD, verifies the complete qualification manifest, checks namespace/path/forum/bot
uniqueness, and performs no mutations. `READY_FOR_STAGING` explicitly does not
authorize production cutover.

After starting a staging instance, run the read-only live diagnostic before any
Telegram prompt test:

```bash
ai-agent-runtime --config ~/.config/ai-agent-runtime/instances/ta_staging_01.toml doctor
```

`doctor` fails unless it can verify the mode/owner of the local socket, obtain a
versioned daemon status response, open the SQLite store read-only and pass
`quick_check`, match the real Codex digest and version, revalidate every effective
profile fingerprint, parse both upstream config/state files, and prove exact
binding/topic/active-tmux correlation. Its report includes
`mutations_performed=false`; it neither repairs drift nor creates missing state.

# Migration M0 inventory

Before switching `TELEGRAM_CODEX_BIN`, create a read-only baseline artifact for
each AgentInstance. The command records topic routing, safe effective-profile
metadata, upstream tmux state and non-content live pane identities; it never
copies bot tokens, Codex credentials, prompts or transcript contents.

```bash
ai-agent-runtime --config /etc/ai-agent-runtime/prod.toml inventory export \
  --cus-control-store /var/lib/cus/runtime-control-v1.json \
  --output /var/lib/ai-agent-runtime/migration/pre-cutover.json
```

For the first migration from the legacy project-scoped CUS store, use its
settings file instead of `--cus-control-store`:

```bash
ai-agent-runtime --config /etc/ai-agent-runtime/prod.toml inventory export \
  --legacy-project-settings \
    /home/devops/.ai_session_control_center/project_settings_v2.json \
  --output /var/lib/ai-agent-runtime/migration/pre-cutover-from-legacy.json
```

The two source options are mutually exclusive. The legacy reader copies only
the project id/path and account-profile slug, provider, model and reasoning;
tokens, credentials and unrelated legacy fields are omitted. Because the old
store is project-scoped rather than topic-scoped, any legacy record whose cwd
is shared by multiple topics is marked `LEGACY_PROJECT_SCOPE_AMBIGUOUS` and
cannot be imported until the operator assigns a profile to each topic.

The destination is created atomically with mode `0600` and is never
overwritten. Store it in the change record and apply the operator's detached
signature outside the repository. After cutover, export a new artifact and
compare the stable routing fields:

```bash
ai-agent-runtime --config /etc/ai-agent-runtime/prod.toml inventory compare \
  --baseline /var/lib/ai-agent-runtime/migration/pre-cutover.json \
  --candidate /var/lib/ai-agent-runtime/migration/post-cutover.json \
  --output /var/lib/ai-agent-runtime/migration/acceptance.json
```

Exit status `1` blocks acceptance when a topic is lost, added, rebound, renamed,
duplicated, malformed, or leaves orphan upstream tmux state. Multiple topics
sharing one canonical cwd are preserved as separate source refs and reported
explicitly; they are never merged automatically.

Generate a non-mutating Runtime Binding plan only after the baseline artifact
has been reviewed. A managed topic is marked `READY` only when one exact CUS
binding supplies its account, model and reasoning; incomplete data remains
`BLOCKED` and no default profile is invented. Non-Codex topics remain
`UNMANAGED`.

```bash
ai-agent-runtime --config /etc/ai-agent-runtime/prod.toml inventory plan-bindings \
  --inventory /var/lib/ai-agent-runtime/migration/pre-cutover.json \
  --generation-id migration-2026-10-03 \
  --output /var/lib/ai-agent-runtime/migration/binding-plan.json
```

Topics that have already been deleted from the Telegram forum are not silently
dropped. First retain the blocked base plan for review. An owner may then provide a
digest-protected `ai-agent-runtime.binding-migration-decisions.v1` artifact bound to
that exact inventory and base-plan digest. Each decision must identify the exact
source ref, binding ID, thread ID and topic name, and use
`EXCLUDE_DELETED_TOPIC` with a recorded reason and approval timestamp. Rebuild the
plan with:

```bash
ai-agent-runtime --config /etc/ai-agent-runtime/prod.toml inventory plan-bindings \
  --inventory /var/lib/ai-agent-runtime/migration/pre-cutover.json \
  --generation-id migration-2026-10-03 \
  --decisions /var/lib/ai-agent-runtime/migration/owner-decisions.json \
  --output /var/lib/ai-agent-runtime/migration/binding-plan-resolved.json
```

The resolved rows are marked `EXCLUDED`, carry the decision digest, and do not
create Runtime Bindings. This is a migration exclusion, not destructive cleanup of
legacy files. Legacy `topic_config.json` and CUS artifacts remain available for
rollback until the control period ends.

The plan is evidence, not an apply command. Import it into CUS with the separately
versioned `cus-runtime-migration-import` tool. Run that tool without `--commit` first;
only a reviewed, digest-valid, unblocked plan can be committed. CUS then sends one
authoritative Runtime snapshot, keeping non-Codex entries explicitly `UNMANAGED`
instead of rewriting their Telegram Agent engine.

After that authoritative snapshot exists in Runtime, create a separate provenance
plan for the exact Codex sessions named by the pre-cutover upstream tmux state. Do
this only after the qualified session-storage topology and rollout paths are final:

```bash
ai-agent-runtime inventory plan-session-provenance \
  --inventory /var/lib/ai-agent-runtime/migration/pre-cutover.json \
  --binding-plan /var/lib/ai-agent-runtime/migration/binding-plan.json \
  --output /var/lib/ai-agent-runtime/migration/session-provenance-plan.json

ai-agent-runtime --config /etc/ai-agent-runtime/prod.toml provenance import \
  --plan /var/lib/ai-agent-runtime/migration/session-provenance-plan.json
```

Planning is read-only. It accepts a session only when the inventory and binding-plan
digests match, the binding is `READY`, upstream state provides one session ID and
rollout path, and the sole exact live tmux pane proves its current Codex account.
That account proof binds pane PID, Linux process starttime/UID, canonical process cwd,
qualified real-Codex binary digest, and a safe direct-child `CODEX_HOME` beneath the
configured profiles root. The observed account must equal the reviewed binding-plan
account. The owned rollout's first `session_meta` record must also prove the exact
session ID and canonical cwd. The artifact records only sanitized process identity
and path/device/inode/size metadata, never arbitrary environment, credentials, prompt,
or transcript content. Import revalidates the live process identity, executable,
cwd and `CODEX_HOME`, then re-opens every rollout and requires unchanged filesystem
identity before atomically inserting immutable binding/account ownership. If the
legacy process exits or changes between planning and import, the import fails closed;
repeating the same digest-bound import while evidence remains valid is idempotent.

This migration deliberately covers only sessions whose topic ownership is proven by
the exact upstream state at inventory time. Older picker entries without such proof
are preserved on disk but are not guessed from cwd or account; they remain
fail-closed for `/resume`. Regenerate both inventory and provenance plan if upstream
state or the session-storage topology changes before cutover.

The same inventory command is the M1 shadow-mode observer when it runs before the
`TELEGRAM_CODEX_BIN` switch. Every topic receives one conservative confidence:

- `OBSERVED_VERIFIED` requires one exact upstream state entry and one live,
  non-dead tmux pane with the expected session name and canonical cwd;
- `OBSERVED_PARTIAL` means topic configuration is valid but complete live identity
  is not present (including a stopped topic);
- `UNKNOWN` means identity, cwd, state, or pane evidence is malformed, conflicting,
  or ambiguous.

`OBSERVED_VERIFIED` by itself does not prove account, `CODEX_HOME`, model, reasoning,
or applied revision. A separate `live_codex_process` inventory field may prove the
legacy account for provenance planning; this does not upgrade shadow confidence or
claim model/reasoning/revision. Full applied facts require Runtime process evidence after
cutover; shadow observation never promotes cwd or process name into AppliedSpec.

Session-storage candidates follow a separate manifest-and-stage workflow documented
in `qualification.md`. Staging is copy-only and does not activate a topology; no
deployment script may replace a non-empty profile directory with a symlink or delete
the original data.

## Preserve the legacy rollback path

Before production cutover, pin the exact clean legacy checkout, the legacy wrapper
binary, and every non-secret file needed to restore the old system. The command below
only hashes and records existing inputs. It does not copy secrets, stop services,
restore files, or authorize cleanup:

```bash
ai-agent-runtime rollback manifest \
  --legacy-repo /srv/rollback/telegram-ai-agent-private \
  --legacy-service telegram-ai-agent-private.service \
  --legacy-codex-bin /srv/rollback/bin/legacy-codex-wrapper \
  --artifact service-unit=/srv/rollback/systemd/telegram-ai-agent-private.service \
  --artifact overlay-pth=/srv/rollback/python/00-cus-telegram-overlay.pth \
  --artifact pre-cutover-inventory=/srv/qualification/pre-cutover.json \
  --control-period-end 2026-11-04T00:00:00Z \
  --output /srv/qualification/rollback-manifest.json

ai-agent-runtime rollback verify \
  --manifest /srv/qualification/rollback-manifest.json \
  --output /srv/qualification/rollback-verification.json
```

The Git origin is stored only as a fingerprint because remote URLs can contain
credentials. Secret-shaped paths, symlinks, dirty tracked worktrees, tampered files,
and changed Git commits fail closed. `VERIFIED_NOT_DRILLED` means the inputs still
exist unchanged; it is not rollback-drill evidence and does not authorize a restore.
The reserved `legacy_codex_bin` input may be the actual launcher symlink: its link
inode/target and the resolved regular binary inode/digest are both pinned. Symlinks
remain forbidden for every operator-supplied additional artifact.
An immutable release checkout may be owned by root; Git inspection uses a command-local
`safe.directory` value for that one already validated absolute worktree path and never
modifies global or repository Git configuration.

The private GitHub repository, local legacy checkout, overlay, old virtual
environment, and server files remain protected rollback inputs until all of the
following are true: the staging rollback drill passed, production completed its
declared control period, the new session-storage topology no longer needs rollback,
and the Owner made a separate cleanup decision. The manifest intentionally reports
`cleanup_authorized=false`; deletion is never an automatic deployment step.
