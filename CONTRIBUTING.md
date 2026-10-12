# Contributing

Contributions are welcome through GitHub issues and pull requests.

Before a substantial change, open an issue describing the problem, proposed
behavior, trust-boundary impact, and verification plan. Keep the Runtime core
provider-agnostic. Provider behavior belongs in an adapter, and Telegram Agent
integration must remain version-qualified without patching or vendoring upstream.
Unknown or ambiguous bindings must continue to fail closed.

Never include credentials, provider profiles, bot tokens, environment files,
deployment state, private paths, or customer data in an issue, commit, test fixture,
or log excerpt.

## Development checks

```bash
uv sync --locked --dev
uv run ruff check .
uv run mypy src/
uv run pytest -q
```

A pull request should explain what changed, why the change is safe, and which checks
were run. New behavior should include tests. Deployment evidence from one environment
must not be presented as qualification for another release tuple.
