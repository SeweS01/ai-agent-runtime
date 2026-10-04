from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_agent_runtime.migration.inventory import write_json_artifact
from ai_agent_runtime.qualification.release_gate import (
    QUALIFICATION_EVIDENCE_SCHEMA,
    QUALIFICATION_GATES,
    release_tuple_digest,
)

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_ROOT = RUNTIME_ROOT.parent / "telegram-ai-agent-upstream-validation"
UPSTREAM_PYTHON = UPSTREAM_ROOT / ".venv/bin/python"
UPSTREAM_SHA = "0430c4e4ddbc6b7a2cfcccd5af95c456a3d3961b"


@pytest.mark.skipif(
    not (UPSTREAM_ROOT / ".git").is_dir() or not UPSTREAM_PYTHON.is_file(),
    reason="pinned Telegram Agent qualification checkout/environment absent",
)
def test_pinned_upstream_retirement_fault_matrix_is_safe(tmp_path: Path) -> None:
    release_tuple = {
        "telegram_agent_sha": UPSTREAM_SHA,
        "codex_version": "0.156.1",
        "codex_binary_sha256": f"sha256:{'1' * 64}",
        "runtime_release": "test-runtime",
        "protocol_major": 1,
        "cus_connector_version": "test-connector",
    }
    tuple_path = tmp_path / "release-tuple.json"
    write_json_artifact(tuple_path, release_tuple)
    output = tmp_path / "retirement-race.json"
    result = subprocess.run(
        [
            str(UPSTREAM_PYTHON),
            str(RUNTIME_ROOT / "tools/qualify_retirement_race.py"),
            "--upstream",
            str(UPSTREAM_ROOT),
            "--release-tuple",
            str(tuple_path),
            "--report-id",
            "pytest-retirement-race",
            "--environment",
            "local",
            "--output",
            str(output),
        ],
        cwd=RUNTIME_ROOT,
        capture_output=True,
        text=True,
        # Importing the pinned upstream environment dominates this subprocess
        # and can exceed one minute on a loaded CI host. The scenarios
        # themselves remain deterministic and internally bounded.
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["schema"] == QUALIFICATION_EVIDENCE_SCHEMA
    assert evidence["gate_id"] == "retirement_race"
    assert evidence["status"] == "PASS"
    assert evidence["environment"] == "local"
    assert evidence["release_tuple_digest"] == release_tuple_digest(release_tuple)
    assertions = {item["id"]: item for item in evidence["assertions"]}
    assert set(assertions) == set(QUALIFICATION_GATES["retirement_race"])
    assert all(item["passed"] is True for item in assertions.values())
    assert all(item["silent_loss"] is False for item in assertions.values())
    assert all(item["duplicate_execution"] is False for item in assertions.values())
    assert all(item["misroute"] is False for item in assertions.values())
