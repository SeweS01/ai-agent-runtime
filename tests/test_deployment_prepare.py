from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ai_agent_runtime.deployment import prepare as preparation
from ai_agent_runtime.deployment.install import INSTALL_REPORT_SCHEMA
from ai_agent_runtime.migration.inventory import InventoryError, _digest, write_json_artifact


def _executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)


def _install_report(tmp_path: Path) -> tuple[Path, Path]:
    release = tmp_path / "releases" / "runtime-1"
    runtime = release / "bin" / "ai-agent-runtime"
    wrapper = release / "bin" / "codex-wrapper"
    _executable(runtime)
    _executable(wrapper)
    wheel = tmp_path / "runtime.whl"
    wheel.write_bytes(b"wheel")
    report: dict[str, object] = {
        "schema": INSTALL_REPORT_SCHEMA,
        "status": "INSTALLED_NOT_ACTIVATED",
        "release_id": "runtime-1",
        "release_path": str(release),
        "wheel_path": str(wheel),
        "wheel_sha256": f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}",
        "python_path": "/usr/bin/python3.12",
        "runtime_executable": str(runtime),
        "wrapper_executable": str(wrapper),
        "existing_releases_preserved": True,
        "activation_performed": False,
        "services_restarted": False,
        "production_cutover_authorized": False,
    }
    report["report_digest"] = _digest(report)
    report_path = tmp_path / "install-report.json"
    write_json_artifact(report_path, report)
    return report_path, release


def test_prepare_instance_links_resolves_both_entrypoints_to_one_verified_release(
    tmp_path: Path,
) -> None:
    report_path, release = _install_report(tmp_path)
    instance = tmp_path / "instances" / "ta_staging_01"
    runtime = tmp_path / "bin" / "ai-agent-runtime@ta_staging_01"
    wrapper = tmp_path / "bin" / "codex-wrapper@ta_staging_01"

    report = preparation.prepare_instance_links(
        install_report=report_path,
        agent_instance_id="ta_staging_01",
        instance_link=instance,
        runtime_link=runtime,
        wrapper_link=wrapper,
    )

    assert instance.is_symlink()
    assert instance.resolve(strict=True) == release
    assert runtime.resolve(strict=True) == release / "bin" / "ai-agent-runtime"
    assert wrapper.resolve(strict=True) == release / "bin" / "codex-wrapper"
    assert report["status"] == "PREPARED_NOT_STARTED"
    assert report["services_started"] is False
    assert report["production_cutover_authorized"] is False

    with pytest.raises(InventoryError, match="existing"):
        preparation.prepare_instance_links(
            install_report=report_path,
            agent_instance_id="ta_staging_01",
            instance_link=instance,
            runtime_link=runtime,
            wrapper_link=wrapper,
        )


def test_prepare_instance_links_rolls_back_only_new_links_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_path, _release = _install_report(tmp_path)
    instance = tmp_path / "instances" / "ta_staging_01"
    runtime = tmp_path / "bin" / "ai-agent-runtime@ta_staging_01"
    wrapper = tmp_path / "bin" / "codex-wrapper@ta_staging_01"
    original = preparation._create_symlink
    calls = 0

    def fail_third(link: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated link failure")
        original(link, target)

    monkeypatch.setattr(preparation, "_create_symlink", fail_third)
    with pytest.raises(OSError, match="simulated"):
        preparation.prepare_instance_links(
            install_report=report_path,
            agent_instance_id="ta_staging_01",
            instance_link=instance,
            runtime_link=runtime,
            wrapper_link=wrapper,
        )

    assert not instance.is_symlink()
    assert not runtime.is_symlink()
    assert not wrapper.is_symlink()
