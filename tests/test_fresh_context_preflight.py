from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.migration.fresh_context import build_fresh_context_preflight
from ai_agent_runtime.migration.inventory import INVENTORY_SCHEMA, InventoryError, _digest


def _inputs(tmp_path: Path) -> tuple[RuntimeConfig, dict[str, object], dict[str, object]]:
    config = RuntimeConfig(
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=tmp_path / "runtime",
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=tmp_path / "runtime" / "runtime.sqlite",
        real_codex=tmp_path / "bin" / "codex-real",
        real_codex_version="0.1.0",
        real_codex_sha256="sha256:" + "1" * 64,
        profiles_root=tmp_path / "profiles",
        topic_config_path=tmp_path / "new" / "topic_config.json",
        tmux_state_path=tmp_path / "new" / "tmux_sessions" / "state.json",
        tmux_tmpdir=tmp_path / "run" / "tmux",
        upstream_sha="0" * 40,
        allowed_project_roots=(tmp_path / "projects",),
    )
    baseline: dict[str, object] = {
        "schema": INVENTORY_SCHEMA,
        "inventory_digest": "sha256:" + "2" * 64,
        "topics": [
            {
                "thread_id": 42,
                "name": "Existing topic",
                "canonical_cwd": str(tmp_path / "projects" / "existing"),
                "engine": "codex",
                "exec_mode": "tmux",
            }
        ],
        "sources": {
            "topic_config": {"path": str(tmp_path / "legacy" / "topic_config.json")},
            "tmux_state": {"path": str(tmp_path / "legacy" / "tmux_sessions" / "state.json")},
        },
    }
    plan: dict[str, object] = {
        "schema": "ai-agent-runtime.binding-migration-plan.v1",
        "source_inventory_digest": baseline["inventory_digest"],
        "accepted": True,
        "entries": [
            {
                "status": "READY",
                "identity": {
                    "agent_instance_id": "agent-1",
                    "chat_id": -1001,
                    "thread_id": 42,
                },
            }
        ],
    }
    plan["plan_digest"] = _digest(plan)
    config.topic_config_path.parent.mkdir(parents=True)
    config.topic_config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "42": {
                        "name": "Existing topic",
                        "cwd": str(tmp_path / "projects" / "existing"),
                        "engine": "codex",
                        "exec_mode": "tmux",
                    }
                }
            }
        )
    )
    return config, baseline, plan


def _no_server() -> dict[str, object]:
    return {"available": False, "reason": "isolated_tmux_socket_absent", "panes": []}


def test_fresh_context_preflight_accepts_empty_isolated_namespace(tmp_path: Path) -> None:
    config, baseline, plan = _inputs(tmp_path)
    report = build_fresh_context_preflight(
        config, baseline=baseline, binding_plan=plan, live_tmux=_no_server()
    )
    assert report["accepted"] is True
    assert report["issues"] == []
    assert report["mutations_performed"] is False
    assert report["production_cutover_authorized"] is False


def test_fresh_context_preflight_rejects_old_state_or_live_pane(tmp_path: Path) -> None:
    config, baseline, plan = _inputs(tmp_path)
    config.tmux_state_path.parent.mkdir(parents=True)
    config.tmux_state_path.write_text(json.dumps({"-1001:42": {"session_id": "old"}}))
    report = build_fresh_context_preflight(
        config,
        baseline=baseline,
        binding_plan=plan,
        live_tmux={"available": True, "reason": None, "panes": [{"session_name": "cc-n1001-42"}]},
    )
    assert report["accepted"] is False
    assert report["issues"] == ["TARGET_TMUX_SERVER_NOT_EMPTY", "TARGET_TMUX_STATE_NOT_EMPTY"]


def test_fresh_context_preflight_rejects_topic_name_or_cwd_drift(tmp_path: Path) -> None:
    config, baseline, plan = _inputs(tmp_path)
    raw = json.loads(config.topic_config_path.read_text())
    raw["topics"]["42"]["name"] = "Renamed"
    config.topic_config_path.write_text(json.dumps(raw))
    report = build_fresh_context_preflight(
        config, baseline=baseline, binding_plan=plan, live_tmux=_no_server()
    )
    assert report["accepted"] is False
    assert report["issues"] == ["RETAINED_TOPIC_CONFIG_DRIFT"]


def test_fresh_context_preflight_rejects_missing_target_config(tmp_path: Path) -> None:
    config, baseline, plan = _inputs(tmp_path)
    config.topic_config_path.unlink()
    report = build_fresh_context_preflight(
        config, baseline=baseline, binding_plan=plan, live_tmux=_no_server()
    )
    assert report["accepted"] is False
    assert report["issues"] == ["TARGET_TOPIC_CONFIG_MISSING"]


def test_fresh_context_preflight_rejects_reused_legacy_paths(tmp_path: Path) -> None:
    config, baseline, plan = _inputs(tmp_path)
    config = replace(
        config,
        topic_config_path=tmp_path / "legacy" / "topic_config.json",
        tmux_state_path=tmp_path / "legacy" / "tmux_sessions" / "state.json",
    )
    report = build_fresh_context_preflight(
        config, baseline=baseline, binding_plan=plan, live_tmux=_no_server()
    )
    assert report["accepted"] is False
    assert "LEGACY_TMUX_STATE_PATH_REUSED" in report["issues"]
    assert "LEGACY_TOPIC_CONFIG_PATH_REUSED" in report["issues"]


def test_fresh_context_preflight_rejects_unverified_tmux_and_plan(tmp_path: Path) -> None:
    config, baseline, plan = _inputs(tmp_path)
    report = build_fresh_context_preflight(
        config,
        baseline=baseline,
        binding_plan=plan,
        live_tmux={"available": False, "reason": "tmux_exit_1", "panes": []},
    )
    assert report["accepted"] is False
    assert report["issues"] == ["TARGET_TMUX_SERVER_UNVERIFIED"]
    plan["accepted"] = False
    with pytest.raises(InventoryError, match="accepted baseline-bound plan"):
        build_fresh_context_preflight(
            config, baseline=baseline, binding_plan=plan, live_tmux=_no_server()
        )
