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
that install report, so a different or incomplete release cannot pass. For `staging`, the
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
`Restart=always`, 2-second restart, 30-second watchdog, private modes, `Accept=false`)
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

The plan is evidence, not an apply command. Import it into CUS with the separately
versioned `cus-runtime-migration-import` tool. Run that tool without `--commit` first;
only a reviewed, digest-valid, unblocked plan can be committed. CUS then sends one
authoritative Runtime snapshot, keeping non-Codex entries explicitly `UNMANAGED`
instead of rewriting their Telegram Agent engine.

The same inventory command is the M1 shadow-mode observer when it runs before the
`TELEGRAM_CODEX_BIN` switch. Every topic receives one conservative confidence:

- `OBSERVED_VERIFIED` requires one exact upstream state entry and one live,
  non-dead tmux pane with the expected session name and canonical cwd;
- `OBSERVED_PARTIAL` means topic configuration is valid but complete live identity
  is not present (including a stopped topic);
- `UNKNOWN` means identity, cwd, state, or pane evidence is malformed, conflicting,
  or ambiguous.

Even `OBSERVED_VERIFIED` explicitly does not prove account, `CODEX_HOME`, model,
reasoning, or applied revision. Those facts require Runtime process evidence after
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

The private GitHub repository, local legacy checkout, overlay, old virtual
environment, and server files remain protected rollback inputs until all of the
following are true: the staging rollback drill passed, production completed its
declared control period, the new session-storage topology no longer needs rollback,
and the Owner made a separate cleanup decision. The manifest intentionally reports
`cleanup_authorized=false`; deletion is never an automatic deployment step.
