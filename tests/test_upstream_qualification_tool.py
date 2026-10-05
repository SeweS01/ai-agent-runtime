from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from ai_agent_runtime.migration.inventory import _digest
from ai_agent_runtime.qualification.release_gate import (
    QUALIFICATION_EVIDENCE_SCHEMA,
    release_tuple_digest,
)

_TOOL_PATH = Path(__file__).parents[1] / "tools" / "qualify_telegram_upstream.py"
_SPEC = importlib.util.spec_from_file_location("qualify_telegram_upstream", _TOOL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_evidence = _MODULE.build_evidence


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _checkout(tmp_path: Path) -> tuple[Path, str]:
    checkout = tmp_path / "telegram-agent"
    services = checkout / "src/telegram_bot/core/services"
    services.mkdir(parents=True)
    (services / "tmux_spawn.py").write_text(
        """def make_session_name(key):
    chat_id, thread_id = key
    suffix = str(thread_id) if thread_id is not None else "0"
    chat_part = f"n{-chat_id}" if chat_id < 0 else str(chat_id)
    return f"cc-{chat_part}-{suffix}"
""",
        encoding="utf-8",
    )
    (services / "tmux_manager.py").write_text(
        """class TmuxManager:
    async def _start_session_unlocked(self):
        await self._spawn_tmux()
        self._save_state()

    async def switch_or_start_session(self):
        replacement_name = "cc-n100123-44"
        replacement_name = f"{replacement_name}-switch-{time.time_ns():x}"
""",
        encoding="utf-8",
    )
    (services / "providers.py").write_text(
        'def command():\n    return ["env", "-i", "codex"]\n',
        encoding="utf-8",
    )
    _git(checkout, "init")
    _git(checkout, "config", "user.name", "Qualification Test")
    _git(checkout, "config", "user.email", "qualification@example.invalid")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "pinned upstream")
    return checkout, _git(checkout, "rev-parse", "HEAD")


def _release_tuple(sha: str) -> dict[str, object]:
    return {
        "telegram_agent_sha": sha,
        "codex_version": "0.156.1",
        "codex_binary_sha256": f"sha256:{'1' * 64}",
        "runtime_release": "0.1.0",
        "runtime_wheel_sha256": f"sha256:{'2' * 64}",
        "protocol_major": 1,
        "cus_connector_version": "runtime-v1",
    }


def test_upstream_tool_emits_release_bound_gate_evidence(tmp_path: Path) -> None:
    checkout, sha = _checkout(tmp_path)
    release_tuple = _release_tuple(sha)
    tuple_path = tmp_path / "release-tuple.json"
    tuple_path.write_text(json.dumps(release_tuple), encoding="utf-8")
    evidence = build_evidence(
        argparse.Namespace(
            checkout=checkout,
            release_tuple=str(tuple_path),
            expected_sha=sha,
            report_id="staging-001",
            environment="staging",
        )
    )
    assert evidence["schema"] == QUALIFICATION_EVIDENCE_SCHEMA
    assert evidence["gate_id"] == "upstream_source_contract"
    assert evidence["release_tuple_digest"] == release_tuple_digest(release_tuple)
    assert evidence["status"] == "PASS"
    assert {item["id"] for item in evidence["assertions"]} == {
        "exact_commit",
        "tmux_naming",
        "resume_switch_tmux_naming",
        "spawn_before_state_save",
        "safe_binary_launch",
    }
    assert evidence["evidence_digest"] == _digest(
        {key: value for key, value in evidence.items() if key != "evidence_digest"}
    )


def test_upstream_tool_rejects_dirty_or_conflicting_checkout(tmp_path: Path) -> None:
    checkout, sha = _checkout(tmp_path)
    tuple_path = tmp_path / "release-tuple.json"
    tuple_path.write_text(json.dumps(_release_tuple(sha)), encoding="utf-8")
    base = {
        "checkout": checkout,
        "release_tuple": str(tuple_path),
        "report_id": "staging-001",
        "environment": "staging",
    }
    with pytest.raises(RuntimeError, match="conflicts"):
        build_evidence(argparse.Namespace(**base, expected_sha="0" * 40))

    (checkout / "src/telegram_bot/core/services/providers.py").write_text(
        "changed\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="dirty"):
        build_evidence(argparse.Namespace(**base, expected_sha=sha))
