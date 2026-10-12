# AI Agent Runtime

AI Agent Runtime is the provider-agnostic execution and control layer between an
unchanged Telegram Agent installation and AI CLI providers. Runtime V1 supervises
Codex inside tmux while AI Session Control Center (CUS) supplies versioned desired
state outside the message path.

## V1 invariants

- Telegram Agent creates tmux sessions and owns message delivery.
- Runtime integrates through a version-qualified adapter; it does not patch, vendor,
  or shadow-import Telegram Agent.
- `codex-wrapper` starts only after an exact local Runtime binding is proven.
- Wrapper startup never performs a network request to CUS.
- Unknown, ambiguous, drifted, or non-tmux Codex launches fail closed.
- CUS outage may use verified local last-known-good state; Runtime daemon outage
  denies a new launch.
- Codex subprocess execution is unsupported in Runtime V1.
- Credentials, provider profiles, Telegram routing, and CUS business logic do not
  belong in this repository or protocol.

## Development

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked --dev
uv run pytest -q
uv run ruff check .
uv run mypy src/
```

See [architecture](docs/architecture.md), [protocol](docs/protocol.md),
[deployment](docs/deployment.md), and [qualification](docs/qualification.md).
A secret-free example configuration is available at
[`config/runtime.example.toml`](config/runtime.example.toml).

The companion control plane is
[`SeweS01/ai-session-control-center`](https://github.com/SeweS01/ai-session-control-center).

## Contributing and license

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Report security
issues according to [SECURITY.md](SECURITY.md). This project is available under the
[MIT License](LICENSE), Copyright (c) 2026 SeweS-DS.
