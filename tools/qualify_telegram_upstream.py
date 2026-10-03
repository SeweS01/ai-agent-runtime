#!/usr/bin/env python3
"""Verify source-level assumptions against an exact upstream checkout."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path
from typing import Any


def _git(checkout: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise RuntimeError(f"function {name} not found in {path}")


def _qualify_tmux_name(checkout: Path) -> dict[str, Any]:
    path = checkout / "src/telegram_bot/core/services/tmux_spawn.py"
    node = _function(path, "make_session_name")
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {}
    source = "from __future__ import annotations\n" + ast.unparse(module)
    exec(compile(source, str(path), "exec"), namespace)
    function = namespace["make_session_name"]
    cases = {
        "group_topic": function((-100123, 44)),
        "private_default": function((123, None)),
    }
    expected = {"group_topic": "cc-n100123-44", "private_default": "cc-123-0"}
    if cases != expected:
        raise RuntimeError(f"tmux naming mismatch: {cases!r}")
    return cases


def _qualify_state_order(checkout: Path) -> dict[str, int]:
    path = checkout / "src/telegram_bot/core/services/tmux_manager.py"
    node = _function(path, "_start_session_unlocked")
    source = ast.get_source_segment(path.read_text(encoding="utf-8"), node)
    if source is None:
        raise RuntimeError("cannot extract _start_session_unlocked")
    spawn_position = source.find("await self._spawn_tmux(")
    save_position = source.find("self._save_state()")
    if spawn_position < 0 or save_position < 0 or spawn_position >= save_position:
        raise RuntimeError("state save no longer follows successful tmux spawn")
    return {"spawn_position": spawn_position, "save_position": save_position}


def _qualify_sanitized_environment(checkout: Path) -> dict[str, bool]:
    path = checkout / "src/telegram_bot/core/services/providers.py"
    source = path.read_text(encoding="utf-8")
    if 'return ["env", "-i"' not in source:
        raise RuntimeError("upstream no longer uses the qualified env -i launch contract")
    if '"AI_AGENT_RUNTIME_CONFIG"' in source:
        raise RuntimeError("upstream now carries Runtime config; revisit namespace mechanism")
    return {"uses_env_i": True, "runtime_config_not_allowlisted": True}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--expected-sha", required=True)
    namespace = parser.parse_args()
    checkout = namespace.checkout.resolve(strict=True)
    actual_sha = _git(checkout, "rev-parse", "HEAD")
    if actual_sha != namespace.expected_sha:
        raise SystemExit(f"wrong upstream SHA: expected {namespace.expected_sha}, got {actual_sha}")
    if _git(checkout, "status", "--porcelain"):
        raise SystemExit("upstream checkout is dirty")
    report = {
        "upstream_sha": actual_sha,
        "tmux_name": _qualify_tmux_name(checkout),
        "state_order": _qualify_state_order(checkout),
        "sanitized_environment": _qualify_sanitized_environment(checkout),
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
