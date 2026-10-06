from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from ai_agent_runtime.migration.inventory import InventoryError, _digest
from ai_agent_runtime.qualification.release_gate import QUALIFICATION_GATES
from ai_agent_runtime.qualification.run_plan import (
    QUALIFICATION_RUN_PLAN_SCHEMA,
    build_staging_run_plan,
)


def _release_tuple() -> dict[str, object]:
    return {
        "telegram_agent_sha": "1" * 40,
        "codex_version": "codex-cli 0.156.1",
        "codex_binary_sha256": f"sha256:{'2' * 64}",
        "runtime_release": "runtime-1",
        "runtime_wheel_sha256": f"sha256:{'3' * 64}",
        "protocol_major": 1,
        "cus_connector_version": "runtime-v1",
    }


def _inputs(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    release_tuple = _release_tuple()
    release_tuple_path = tmp_path / "release-tuple.json"
    release_tuple_path.write_text(json.dumps(release_tuple), encoding="utf-8")
    install_report = tmp_path / "install-report.json"
    install_report.write_text("{}\n", encoding="utf-8")
    upstream = tmp_path / "upstream"
    upstream.mkdir(mode=0o700)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    verification = {
        "accepted": True,
        "release_id": "runtime-1",
        "wheel_sha256": f"sha256:{'3' * 64}",
        "runtime_executable": "/release/bin/ai-agent-runtime",
        "qualification_executables": {
            "ai-agent-runtime-qualify-upstream": "/release/bin/qualify-upstream",
            "ai-agent-runtime-qualify-retirement": "/release/bin/qualify-retirement",
        },
        "verification_digest": f"sha256:{'4' * 64}",
    }
    return {
        "report_id": "staging-001",
        "release_tuple": release_tuple,
        "release_tuple_path": release_tuple_path,
        "runtime_install_report": install_report,
        "upstream_checkout": upstream,
        "evidence_directory": evidence,
        "qualification_manifest_output": tmp_path / "qualification-manifest.json",
        "qualification_report_output": tmp_path / "qualification-report.json",
        "install_verifier": lambda _path: verification,
        "git_runner": lambda _path, args: "1" * 40 if args[0] == "rev-parse" else "",
    }


def test_staging_run_plan_binds_every_gate_and_exact_release(tmp_path: Path) -> None:
    plan = build_staging_run_plan(**_inputs(tmp_path))

    assert plan["schema"] == QUALIFICATION_RUN_PLAN_SCHEMA
    assert plan["environment"] == "staging"
    assert plan["qualification_complete"] is False
    assert plan["production_cutover_authorized"] is False
    gates = cast(list[dict[str, object]], plan["gates"])
    assert {str(gate["gate_id"]) for gate in gates} == set(QUALIFICATION_GATES)
    assert "legacy_session_provenance_import" in QUALIFICATION_GATES["session_storage_lifecycle"]
    assert "first_launch_prompt_accepted" in QUALIFICATION_GATES["telegram_e2e"]
    assert "first_launch_reply_exactly_once" in QUALIFICATION_GATES["telegram_e2e"]
    assert "user_created_topic_auto_notice" in QUALIFICATION_GATES["cus_bot_onboarding"]
    for gate in gates:
        gate_id = str(gate["gate_id"])
        assert gate["required_assertion_ids"] == list(QUALIFICATION_GATES[gate_id])
        assert gate["state"] == "PENDING"
    commands = cast(dict[str, list[str]], plan["commands"])
    assert commands["upstream_source_contract"][0] == "/release/bin/qualify-upstream"
    assert commands["retirement_race"][0] == "/release/bin/qualify-retirement"
    manifest_command = commands["build_manifest_after_all_gates"]
    assert manifest_command.count("--evidence") == len(QUALIFICATION_GATES)
    assert plan["plan_digest"] == _digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )


def test_staging_run_plan_rejects_release_or_checkout_mismatch(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs["git_runner"] = lambda _path, _args: "0" * 40
    with pytest.raises(InventoryError, match="upstream checkout"):
        build_staging_run_plan(**inputs)

    inputs = _inputs(tmp_path / "other")
    inputs["install_verifier"] = lambda _path: {
        "accepted": True,
        "release_id": "different-release",
        "wheel_sha256": f"sha256:{'3' * 64}",
    }
    with pytest.raises(InventoryError, match="release does not match"):
        build_staging_run_plan(**inputs)


def test_staging_run_plan_rejects_unsafe_or_existing_artifact_paths(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    evidence = cast(Path, inputs["evidence_directory"])
    evidence.chmod(0o755)
    with pytest.raises(InventoryError, match="mode 0700"):
        build_staging_run_plan(**inputs)

    evidence.chmod(0o700)
    manifest = cast(Path, inputs["qualification_manifest_output"])
    manifest.write_text("{}\n", encoding="utf-8")
    with pytest.raises(InventoryError, match="already exists"):
        build_staging_run_plan(**inputs)
