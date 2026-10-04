from dataclasses import replace
from pathlib import Path

import pytest

from ai_agent_runtime.config import RuntimeConfig


def test_retirement_feature_gate_requires_real_boolean(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.toml"
    config_path.write_text(
        """
[policy]
retirement_enabled = "false"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a boolean"):
        RuntimeConfig.load(config_path)


def test_enabled_retirement_requires_qualification_report(tmp_path: Path) -> None:
    config = replace(
        RuntimeConfig.defaults(),
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        upstream_sha="0" * 40,
        real_codex_version="0.0-test",
        real_codex_sha256=f"sha256:{'0' * 64}",
        allowed_project_roots=(tmp_path,),
        retirement_enabled=True,
        retirement_qualification_report_id="",
    )
    with pytest.raises(ValueError, match="qualification_report_id"):
        config.validate()


@pytest.mark.parametrize("value", [0.0, 4.9, 3600.1])
def test_control_plane_stale_window_is_bounded(tmp_path: Path, value: float) -> None:
    config = replace(
        RuntimeConfig.defaults(),
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        upstream_sha="0" * 40,
        real_codex_version="0.0-test",
        real_codex_sha256=f"sha256:{'0' * 64}",
        allowed_project_roots=(tmp_path,),
        control_plane_stale_after_seconds=value,
    )
    with pytest.raises(ValueError, match="control_plane_stale_after_seconds"):
        config.validate()
