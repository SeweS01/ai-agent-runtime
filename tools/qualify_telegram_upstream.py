#!/usr/bin/env python3
"""Verify source-level assumptions against an exact upstream checkout."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path
from typing import Any

from ai_agent_runtime.migration.inventory import _digest, write_json_artifact
from ai_agent_runtime.qualification.release_gate import (
    QUALIFICATION_EVIDENCE_SCHEMA,
    release_tuple_digest,
    validate_release_tuple,
)


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


def _qualify_resume_switch_name(checkout: Path) -> dict[str, str]:
    path = checkout / "src/telegram_bot/core/services/tmux_manager.py"
    node = _function(path, "switch_or_start_session")
    source = ast.unparse(node)
    qualified_expression = "replacement_name = f'{replacement_name}-switch-{time.time_ns():x}'"
    if qualified_expression not in source:
        raise RuntimeError("upstream live /resume tmux naming contract changed")
    return {
        "base": "cc-n100123-44",
        "live_resume_form": "cc-n100123-44-switch-<hex time_ns>",
    }


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


def build_evidence(args: argparse.Namespace) -> dict[str, object]:
    checkout = Path(args.checkout).expanduser().resolve(strict=True)
    release_tuple = validate_release_tuple(
        json.loads(Path(args.release_tuple).read_text(encoding="utf-8"))
    )
    expected_sha = str(release_tuple["telegram_agent_sha"])
    if args.expected_sha is not None and args.expected_sha != expected_sha:
        raise RuntimeError("--expected-sha conflicts with the release tuple")
    actual_sha = _git(checkout, "rev-parse", "HEAD")
    if actual_sha != expected_sha:
        raise RuntimeError(f"wrong upstream SHA: expected {expected_sha}, got {actual_sha}")
    if _git(checkout, "status", "--porcelain"):
        raise RuntimeError("upstream checkout is dirty")

    tmux_name = _qualify_tmux_name(checkout)
    resume_switch_name = _qualify_resume_switch_name(checkout)
    state_order = _qualify_state_order(checkout)
    environment_contract = _qualify_sanitized_environment(checkout)
    evidence: dict[str, object] = {
        "schema": QUALIFICATION_EVIDENCE_SCHEMA,
        "gate_id": "upstream_source_contract",
        "report_id": args.report_id,
        "release_tuple_digest": release_tuple_digest(release_tuple),
        "environment": args.environment,
        "status": "PASS",
        "harness": {
            "kind": "pinned-upstream-source-contract",
            "telegram_agent_sha": actual_sha,
            "checkout_clean": True,
        },
        "assertions": [
            {
                "id": "exact_commit",
                "passed": True,
                "observed_sha": actual_sha,
            },
            {
                "id": "tmux_naming",
                "passed": True,
                "cases": tmux_name,
            },
            {
                "id": "resume_switch_tmux_naming",
                "passed": True,
                "contract": resume_switch_name,
            },
            {
                "id": "spawn_before_state_save",
                "passed": True,
                "positions": state_order,
            },
            {
                "id": "safe_binary_launch",
                "passed": True,
                "contract": environment_contract,
            },
        ],
    }
    evidence["evidence_digest"] = _digest(evidence)
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--release-tuple", required=True)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--environment", choices=("local", "staging"), default="local")
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-sha")
    namespace = parser.parse_args()
    try:
        evidence = build_evidence(namespace)
        write_json_artifact(Path(namespace.output).expanduser(), evidence)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"upstream qualification failed: {exc}") from exc
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
