from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai_agent_runtime.migration.inventory import write_json_artifact
from ai_agent_runtime.qualification.release_gate import (
    QUALIFICATION_EVIDENCE_SCHEMA,
    QUALIFICATION_GATES,
    release_tuple_digest,
)
from tools import qualify_retirement_race as retirement_harness

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_ROOT = RUNTIME_ROOT.parent / "telegram-ai-agent-upstream-validation"
UPSTREAM_PYTHON = UPSTREAM_ROOT / ".venv/bin/python"
UPSTREAM_SHA = "0430c4e4ddbc6b7a2cfcccd5af95c456a3d3961b"


def test_retirement_entrypoint_reexecs_with_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = tmp_path / "upstream"
    python = upstream / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        captured["stdin"] = kwargs["stdin"]
        return subprocess.CompletedProcess(argv, 23)

    monkeypatch.delenv(retirement_harness._CHILD_MARKER, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-cross-boundary")
    monkeypatch.setattr(retirement_harness.subprocess, "run", fake_run)

    result = retirement_harness._run_with_upstream_environment(
        upstream,
        ["--upstream", str(upstream)],
    )

    assert result == 23
    assert captured["argv"] == [
        str(python),
        "-m",
        "tools.qualify_retirement_race",
        "--upstream",
        str(upstream),
    ]
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment[retirement_harness._CHILD_MARKER] == "1"
    assert "TELEGRAM_BOT_TOKEN" not in environment
    assert environment["PYTHONPATH"] == os.pathsep.join(
        str(path) for path in retirement_harness._runtime_python_paths()
    )
    assert captured["stdin"] is subprocess.DEVNULL


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
        "runtime_wheel_sha256": f"sha256:{'2' * 64}",
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
