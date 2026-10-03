# Deployment layout

V1 uses one upstream Telegram Agent, one forum chat, and one Runtime namespace per
AgentInstance. A host may run several independent instances under the same Unix user.

For an AgentInstance named `ta_prod_01`:

- Runtime config:
  `~/.config/ai-agent-runtime/instances/ta_prod_01.toml`;
- Runtime service: `ai-agent-runtime@ta_prod_01.service`;
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
