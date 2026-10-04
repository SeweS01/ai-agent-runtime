from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from ai_agent_runtime.deployment.install import install_runtime_release
from ai_agent_runtime.migration.inventory import InventoryError, _digest


def _executable(path: Path, content: bytes = b"#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o700)
    return path


def test_install_release_is_side_by_side_verified_and_not_activated(tmp_path: Path) -> None:
    wheel = tmp_path / "ai_agent_runtime-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel-content")
    wheel_digest = f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"
    python = _executable(tmp_path / "python")
    releases = tmp_path / "releases"
    old_release = releases / "old-release"
    old_release.mkdir(parents=True)
    marker = old_release / "preserve"
    marker.write_text("old", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def runner(command: Sequence[str]) -> None:
        normalized = tuple(command)
        commands.append(normalized)
        if normalized[1:3] == ("-m", "venv"):
            target = Path(normalized[-1])
            _executable(target / "bin" / "python")
            return
        environment = Path(normalized[0]).parent
        _executable(environment / "ai-agent-runtime")
        _executable(environment / "codex-wrapper")

    report = install_runtime_release(
        wheel=wheel,
        wheel_sha256=wheel_digest,
        releases_root=releases,
        release_id="runtime-0.1.0-test",
        python=python,
        runner=runner,
    )

    release = releases / "runtime-0.1.0-test"
    assert release.is_dir()
    assert (release / "bin" / "ai-agent-runtime").is_file()
    assert (release / "bin" / "codex-wrapper").is_file()
    assert marker.read_text(encoding="utf-8") == "old"
    assert report["status"] == "INSTALLED_NOT_ACTIVATED"
    assert report["activation_performed"] is False
    assert report["services_restarted"] is False
    assert report["production_cutover_authorized"] is False
    assert report["report_digest"] == _digest(
        {key: value for key, value in report.items() if key != "report_digest"}
    )
    assert len(commands) == 2
    assert not list(releases.glob(".runtime-0.1.0-test.*"))


def test_install_release_rejects_digest_overwrite_and_cleans_failed_stage(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "runtime.whl"
    wheel.write_bytes(b"wheel")
    digest = f"sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"
    python = _executable(tmp_path / "python")
    releases = tmp_path / "releases"

    with pytest.raises(InventoryError, match="digest"):
        install_runtime_release(
            wheel=wheel,
            wheel_sha256="sha256:" + "0" * 64,
            releases_root=releases,
            release_id="runtime-1",
            python=python,
        )

    def failing_runner(command: Sequence[str]) -> None:
        target = Path(command[-1])
        _executable(target / "bin" / "python")
        raise RuntimeError("simulated install failure")

    with pytest.raises(RuntimeError, match="simulated"):
        install_runtime_release(
            wheel=wheel,
            wheel_sha256=digest,
            releases_root=releases,
            release_id="runtime-1",
            python=python,
            runner=failing_runner,
        )
    assert not (releases / "runtime-1").exists()
    assert not list(releases.glob(".runtime-1.*"))

    existing = releases / "runtime-1"
    existing.mkdir()
    with pytest.raises(InventoryError, match="overwrite"):
        install_runtime_release(
            wheel=wheel,
            wheel_sha256=digest,
            releases_root=releases,
            release_id="runtime-1",
            python=python,
        )
