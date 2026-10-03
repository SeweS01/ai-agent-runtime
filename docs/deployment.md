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
