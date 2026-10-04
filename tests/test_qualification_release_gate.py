from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from ai_agent_runtime.cli import main
from ai_agent_runtime.migration.inventory import _digest, write_json_artifact
from ai_agent_runtime.qualification.release_gate import (
    QUALIFICATION_EVIDENCE_SCHEMA,
    QUALIFICATION_GATES,
    build_qualification_manifest,
    release_tuple_digest,
    verify_qualification_manifest,
)


def _release_tuple() -> dict[str, object]:
    return {
        "telegram_agent_sha": "0" * 40,
        "codex_version": "0.156.1",
        "codex_binary_sha256": f"sha256:{'1' * 64}",
        "runtime_release": "0.1.0",
        "runtime_wheel_sha256": f"sha256:{'2' * 64}",
        "protocol_major": 1,
        "cus_connector_version": "runtime-v1",
    }


def _write_evidence(
    root: Path,
    gate_id: str,
    *,
    report_id: str = "qual-test-001",
    release_tuple: dict[str, object] | None = None,
) -> Path:
    assertions: list[dict[str, Any]] = []
    for assertion_id in QUALIFICATION_GATES[gate_id]:
        assertion: dict[str, Any] = {"id": assertion_id, "passed": True}
        if gate_id == "retirement_race":
            assertion.update(
                {
                    "outcome": "exactly_once",
                    "silent_loss": False,
                    "duplicate_execution": False,
                    "misroute": False,
                }
            )
        assertions.append(assertion)
    evidence: dict[str, object] = {
        "schema": QUALIFICATION_EVIDENCE_SCHEMA,
        "gate_id": gate_id,
        "report_id": report_id,
        "release_tuple_digest": release_tuple_digest(release_tuple or _release_tuple()),
        "environment": "staging",
        "status": "PASS",
        "assertions": assertions,
    }
    evidence["evidence_digest"] = _digest(evidence)
    path = root / f"{gate_id}.json"
    write_json_artifact(path, evidence)
    return path


def _complete_manifest(tmp_path: Path) -> Path:
    evidence_root = tmp_path / "evidence"
    evidence_paths = [_write_evidence(evidence_root, gate_id) for gate_id in QUALIFICATION_GATES]
    manifest = build_qualification_manifest(
        report_id="qual-test-001",
        release_tuple=_release_tuple(),
        evidence_paths=evidence_paths,
    )
    path = tmp_path / "qualification.json"
    write_json_artifact(path, manifest)
    return path


def test_complete_exact_tuple_evidence_is_qualified(tmp_path: Path) -> None:
    path = _complete_manifest(tmp_path)
    report = verify_qualification_manifest(path)
    assert report["accepted"] is True
    assert report["status"] == "QUALIFIED"
    assert report["telegram_e2e_verified"] is True
    assert report["runtime_health_inferred"] is False
    assert report["verified_gate_ids"] == sorted(QUALIFICATION_GATES)
    assert report["errors"] == []


def test_missing_gate_blocks_release(tmp_path: Path) -> None:
    evidence_paths = [
        _write_evidence(tmp_path / "evidence", gate_id)
        for gate_id in QUALIFICATION_GATES
        if gate_id != "telegram_e2e"
    ]
    manifest = build_qualification_manifest(
        report_id="qual-test-001",
        release_tuple=_release_tuple(),
        evidence_paths=evidence_paths,
    )
    path = tmp_path / "qualification.json"
    write_json_artifact(path, manifest)
    report = verify_qualification_manifest(path)
    assert report["accepted"] is False
    assert report["telegram_e2e_verified"] is False
    assert report["missing_gate_ids"] == ["telegram_e2e"]
    assert "telegram_e2e:evidence_missing" in report["errors"]


def test_tampered_evidence_is_rejected_by_manifest_hash(tmp_path: Path) -> None:
    path = _complete_manifest(tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    evidence_path = Path(manifest["evidence"][0]["path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["status"] = "FAIL"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    report = verify_qualification_manifest(path)
    assert report["accepted"] is False
    gate_id = manifest["evidence"][0]["gate_id"]
    assert f"{gate_id}:artifact_digest_mismatch" in report["errors"]


def test_retirement_evidence_requires_safe_deterministic_outcome(tmp_path: Path) -> None:
    path = _complete_manifest(tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    reference = next(item for item in manifest["evidence"] if item["gate_id"] == "retirement_race")
    evidence_path = Path(reference["path"])
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["assertions"][0]["outcome"] = "retried_automatically"
    evidence["assertions"][0]["duplicate_execution"] = True
    evidence["evidence_digest"] = _digest(
        {key: value for key, value in evidence.items() if key != "evidence_digest"}
    )
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    reference["sha256"] = (
        "sha256:" + __import__("hashlib").sha256(evidence_path.read_bytes()).hexdigest()
    )
    manifest["manifest_digest"] = _digest(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    )
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = verify_qualification_manifest(path)
    assert report["accepted"] is False
    assert any(error.endswith(":invalid_outcome") for error in report["errors"])
    assert any(error.endswith(":duplicate_execution_not_false") for error in report["errors"])


def test_evidence_from_other_release_tuple_cannot_be_reused(tmp_path: Path) -> None:
    other_tuple = _release_tuple()
    other_tuple["runtime_release"] = "0.2.0"
    paths = []
    for gate_id in QUALIFICATION_GATES:
        paths.append(
            _write_evidence(
                tmp_path / "evidence",
                gate_id,
                release_tuple=other_tuple if gate_id == "real_codex_launch" else None,
            )
        )
    manifest = build_qualification_manifest(
        report_id="qual-test-001",
        release_tuple=_release_tuple(),
        evidence_paths=paths,
    )
    path = tmp_path / "qualification.json"
    write_json_artifact(path, manifest)
    report = verify_qualification_manifest(path)
    assert report["accepted"] is False
    assert "real_codex_launch:release_tuple_mismatch" in report["errors"]


def test_qualification_verify_cli_does_not_require_runtime_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _complete_manifest(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai-agent-runtime",
            "--config",
            str(tmp_path / "does-not-exist.toml"),
            "qualification",
            "verify",
            "--manifest",
            str(manifest),
        ],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 0
