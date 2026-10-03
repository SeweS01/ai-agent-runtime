# AI Agent Runtime

AI Agent Runtime is the execution/control layer between an unchanged
`pavel-molyanov/telegram-ai-agent` installation and provider CLIs. V1 supervises
Codex inside tmux while CUS supplies versioned desired state outside the message path.

The repository deliberately does not contain Telegram routing, CUS business logic,
credentials, provider profiles, or patched Telegram Agent code.

## V1 invariants

- Telegram Agent creates tmux and owns message delivery.
- `codex-wrapper` starts only after an exact local Runtime Binding is found.
- Wrapper startup never performs a network request to CUS.
- Unknown, ambiguous, drifted, or non-tmux Codex launches fail closed.
- CUS outage may use a verified local effective spec; Runtime daemon outage denies a new start.
- Runtime and Telegram Agent are integrated only through the configured binary and
  version-qualified filesystem/process adapters.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run mypy src/
```

The production release tuple and qualification evidence are generated later; a moving
upstream branch is never a production dependency.

The current code is an implementation milestone, not a production release. See
[`docs/architecture.md`](docs/architecture.md) and
[`docs/qualification.md`](docs/qualification.md) for trust boundaries and blocking
release gates. `config/runtime.example.toml` contains paths only and no credentials.
