"""Operator CLI for Runtime lifecycle and diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon.server import RuntimeServer
from ai_agent_runtime.ipc.client import request
from ai_agent_runtime.ipc.protocol import versioned
from ai_agent_runtime.process_safety import validate_owned_executable
from ai_agent_runtime.state.store import RuntimeStore
from ai_agent_runtime.telegram_upstream.topic_config import read_topic_config


def _load_config(path: str) -> RuntimeConfig:
    config = RuntimeConfig.load(Path(path).expanduser())
    config.validate()
    return config


async def _status(config: RuntimeConfig) -> int:
    response = await request(
        config.socket_path,
        versioned("status", str(uuid.uuid4())),
    )
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0 if response.get("ok") else 1


def _doctor(config: RuntimeConfig) -> int:
    checks: dict[str, dict[str, str | bool]] = {}

    def run_check(name: str, callback: object) -> None:
        try:
            assert callable(callback)
            detail = callback()
            checks[name] = {"ok": True, "detail": str(detail)}
        except Exception as exc:
            checks[name] = {"ok": False, "detail": str(exc)}

    run_check("real_codex", lambda: validate_owned_executable(config.real_codex))
    run_check("profiles_root", lambda: config.profiles_root.resolve(strict=True))
    run_check("topic_config", lambda: read_topic_config(config.topic_config_path).digest)
    run_check("tmux_state_parent", lambda: config.tmux_state_path.parent.resolve(strict=True))
    run_check("state_dir", lambda: config.state_dir.resolve(strict=True))
    print(json.dumps(checks, indent=2, sort_keys=True))
    return 0 if all(bool(item["ok"]) for item in checks.values()) else 1


def _local_summary(config: RuntimeConfig) -> int:
    store = RuntimeStore(config.database_path)
    try:
        print(json.dumps(store.summary(), indent=2, sort_keys=True))
    finally:
        store.close()
    return 0


def main() -> None:
    default_config = os.environ.get(
        "AI_AGENT_RUNTIME_CONFIG",
        str(Path.home() / ".config" / "ai-agent-runtime" / "config.toml"),
    )
    parser = argparse.ArgumentParser(prog="ai-agent-runtime")
    parser.add_argument("--config", default=default_config)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    commands.add_parser("status")
    commands.add_parser("local-summary")
    commands.add_parser("doctor")
    namespace = parser.parse_args()
    try:
        config = _load_config(namespace.config)
        if namespace.command == "serve":
            asyncio.run(RuntimeServer(config).serve())
            return
        if namespace.command == "status":
            raise SystemExit(asyncio.run(_status(config)))
        if namespace.command == "local-summary":
            raise SystemExit(_local_summary(config))
        if namespace.command == "doctor":
            raise SystemExit(_doctor(config))
    except (OSError, ValueError) as exc:
        print(f"ai-agent-runtime: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
