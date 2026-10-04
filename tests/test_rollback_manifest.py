from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_agent_runtime.migration import rollback as rollback_module
from ai_agent_runtime.migration.inventory import InventoryError, write_json_artifact
from ai_agent_runtime.migration.rollback import (
    build_rollback_manifest,
    verify_rollback_manifest,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _legacy_checkout(root: Path) -> Path:
    repo = root / "legacy-agent"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Runtime Test")
    _git(repo, "config", "user.email", "runtime@example.invalid")
    (repo / "agent.py").write_text("print('legacy')\n", encoding="utf-8")
    _git(repo, "add", "agent.py")
    _git(repo, "commit", "-m", "legacy baseline")
    _git(repo, "remote", "add", "origin", "https://token@example.invalid/private.git")
    return repo


def _inputs(tmp_path: Path) -> tuple[Path, Path, dict[str, Path]]:
    repo = _legacy_checkout(tmp_path)
    binary = tmp_path / "bin" / "legacy-codex-wrapper"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o700)
    unit = tmp_path / "units" / "legacy-agent.service"
    unit.parent.mkdir()
    unit.write_text("[Service]\nExecStart=/opt/legacy-agent\n", encoding="utf-8")
    return repo, binary, {"legacy_service_unit": unit}


def test_git_inspection_uses_only_command_local_safe_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "root-owned-release-source"
    observed: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="verified\n", stderr="")

    monkeypatch.setattr(rollback_module.subprocess, "run", fake_run)

    assert rollback_module._run_git(repo, "rev-parse", "HEAD") == "verified"
    assert observed == [
        "git",
        "-c",
        f"safe.directory={repo}",
        "-C",
        str(repo),
        "rev-parse",
        "HEAD",
    ]


def test_manifest_pins_clean_legacy_path_without_exposing_remote_secret(tmp_path: Path) -> None:
    repo, binary, artifacts = _inputs(tmp_path)
    manifest = build_rollback_manifest(
        legacy_repo=repo,
        legacy_service="legacy-telegram-agent.service",
        legacy_codex_bin=binary,
        artifacts=artifacts,
        control_period_end="2099-01-02T03:04:05+03:00",
    )
    serialized = json.dumps(manifest)
    assert manifest["status"] == "PINNED_NOT_DRILLED"
    assert manifest["control_period_end"] == "2099-01-02T00:04:05Z"
    assert manifest["policy"]["repository_delete_allowed"] is False
    assert manifest["policy"]["production_restore_authorized"] is False
    assert manifest["safety"]["services_mutated"] is False
    assert "https://token@" not in serialized
    assert "origin_fingerprint" in serialized
    records = manifest["artifacts"]
    assert [record["name"] for record in records] == [
        "legacy_codex_bin",
        "legacy_service_unit",
    ]

    manifest_path = tmp_path / "rollback.json"
    write_json_artifact(manifest_path, manifest)
    report = verify_rollback_manifest(manifest_path)
    assert report["accepted"] is True
    assert report["status"] == "VERIFIED_NOT_DRILLED"
    assert report["production_restore_authorized"] is False
    assert report["cleanup_authorized"] is False
    assert report["mutations_performed"] is False


def test_verify_fails_if_artifact_or_legacy_checkout_changes(tmp_path: Path) -> None:
    repo, binary, artifacts = _inputs(tmp_path)
    manifest = build_rollback_manifest(
        legacy_repo=repo,
        legacy_service="legacy.service",
        legacy_codex_bin=binary,
        artifacts=artifacts,
        control_period_end="2099-01-01T00:00:00Z",
    )
    manifest_path = tmp_path / "rollback.json"
    write_json_artifact(manifest_path, manifest)

    binary.write_text("changed\n", encoding="utf-8")
    (repo / "agent.py").write_text("tracked change\n", encoding="utf-8")
    report = verify_rollback_manifest(manifest_path)
    assert report["accepted"] is False
    assert report["status"] == "ROLLBACK_PATH_INVALID"
    assert any("tracked worktree changes" in failure for failure in report["failures"])
    assert any("legacy_codex_bin" in failure for failure in report["failures"])


def test_manifest_rejects_secret_artifacts_symlinks_and_past_period(tmp_path: Path) -> None:
    repo, binary, artifacts = _inputs(tmp_path)
    secret = tmp_path / "auth.json"
    secret.write_text("secret", encoding="utf-8")
    with pytest.raises(InventoryError, match="secret material"):
        build_rollback_manifest(
            legacy_repo=repo,
            legacy_service="legacy.service",
            legacy_codex_bin=binary,
            artifacts={"bad": secret},
            control_period_end="2099-01-01T00:00:00Z",
        )

    link = tmp_path / "legacy-link"
    link.symlink_to(binary)
    with pytest.raises(InventoryError, match="non-symlink"):
        build_rollback_manifest(
            legacy_repo=repo,
            legacy_service="legacy.service",
            legacy_codex_bin=link,
            artifacts=artifacts,
            control_period_end="2099-01-01T00:00:00Z",
        )

    with pytest.raises(InventoryError, match="future"):
        build_rollback_manifest(
            legacy_repo=repo,
            legacy_service="legacy.service",
            legacy_codex_bin=binary,
            artifacts=artifacts,
            control_period_end="2020-01-01T00:00:00Z",
        )


def test_verify_rejects_tampered_manifest(tmp_path: Path) -> None:
    repo, binary, artifacts = _inputs(tmp_path)
    manifest = build_rollback_manifest(
        legacy_repo=repo,
        legacy_service="legacy.service",
        legacy_codex_bin=binary,
        artifacts=artifacts,
        control_period_end="2099-01-01T00:00:00Z",
    )
    manifest["status"] = "DRILLED"
    manifest_path = tmp_path / "rollback.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InventoryError, match="digest verification failed"):
        verify_rollback_manifest(manifest_path)
