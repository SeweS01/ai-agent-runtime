# AGENTS.md

## Mandatory project rules

1. Architecture first; runtime core must remain provider-agnostic.
2. Codex is the first provider adapter, not the runtime architecture.
3. Telegram Agent integration belongs behind a version-qualified adapter.
4. Never patch, vendor, or shadow-import Telegram Agent.
5. Never put secrets, `.env`, `auth.json`, provider profiles, tokens, or keys in Git.
6. Wrapper startup must never synchronously call CUS.
7. Unknown or ambiguous bindings fail closed.
8. Codex subprocess execution is unsupported in V1 and must not pass through.
9. Applied state requires observed process/session evidence.
10. Destructive migration or cleanup requires an explicit, reversible manifest.

