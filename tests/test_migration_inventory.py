from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.migration.inventory import (
    InventoryError,
    build_inventory,
    compare_inventories,
    read_inventory,
    write_inventory,
)


def _config(tmp_path: Path) -> RuntimeConfig:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    return RuntimeConfig(
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=tmp_path / "runtime",
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=tmp_path / "runtime" / "runtime.sqlite",
        real_codex=tmp_path / "bin" / "codex-real",
        real_codex_version="0.1.0",
        real_codex_sha256="sha256:" + "1" * 64,
        profiles_root=tmp_path / "profiles",
        topic_config_path=tmp_path / "telegram" / "topic_config.json",
        tmux_state_path=tmp_path / "telegram" / "tmux_sessions" / "state.json",
        upstream_sha="0" * 40,
        allowed_project_roots=(project_root,),
    )


def _write_sources(config: RuntimeConfig) -> tuple[Path, Path]:
    first = config.allowed_project_roots[0] / "shared"
    second = config.allowed_project_roots[0] / "other"
    first.mkdir()
    second.mkdir()
    config.topic_config_path.parent.mkdir(parents=True)
    config.topic_config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "42": {
                        "name": "Alpha",
                        "type": "project",
                        "mode": "free",
                        "cwd": str(first),
                        "engine": "codex",
                        "exec_mode": "tmux",
                    },
                    "43": {
                        "name": "Beta",
                        "type": "project",
                        "mode": "task",
                        "cwd": str(first),
                        "engine": "codex",
                        "exec_mode": "tmux",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    config.tmux_state_path.parent.mkdir(parents=True)
    config.tmux_state_path.write_text(
        json.dumps(
            {
                "-1001:42": {
                    "session_name": "cc-n1001-42",
                    "cwd": str(first),
                    "provider": "codex",
                    "session_id": "session-42",
                }
            }
        ),
        encoding="utf-8",
    )
    return first, second


def test_inventory_is_secret_free_digested_and_preserves_binding_scope(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first, _second = _write_sources(config)
    cus_store = tmp_path / "cus-control.json"
    cus_store.write_text(
        json.dumps(
            {
                "version": 1,
                "store_revision": 4,
                "agent_instances": {},
                "bindings": {
                    "binding-42": {
                        "binding_id": "binding-42",
                        "agent_instance_id": "agent-1",
                        "chat_id": -1001,
                        "thread_id": 42,
                        "project_id": "project-shared",
                        "canonical_cwd": str(first),
                        "lifecycle_status": "CONFIGURED",
                        "desired": {
                            "generation_id": "gen-1",
                            "revision": 2,
                            "spec_digest": "sha256:" + "2" * 64,
                            "provider": "codex",
                            "account_profile": "work",
                            "model": "gpt-test",
                            "reasoning_effort": "high",
                            "auth_json": "must-not-leak",
                        },
                    }
                },
                "telegram_bot_token": "must-not-leak",
            }
        ),
        encoding="utf-8",
    )
    inventory = build_inventory(
        config,
        cus_control_store=cus_store,
        live_tmux={"available": True, "reason": None, "panes": []},
        generated_at=datetime(2026, 10, 3, tzinfo=UTC),
    )
    assert len(inventory["topics"]) == 2  # type: ignore[arg-type]
    topics = inventory["topics"]
    assert isinstance(topics, list)
    assert [topic["shadow_confidence"] for topic in topics] == [
        "OBSERVED_PARTIAL",
        "OBSERVED_PARTIAL",
    ]
    assert inventory["shadow_confidence_counts"] == {
        "OBSERVED_VERIFIED": 0,
        "OBSERVED_PARTIAL": 2,
        "UNKNOWN": 0,
    }
    shared = inventory["shared_canonical_cwds"]
    assert isinstance(shared, list) and len(shared) == 1
    assert len(shared[0]["source_refs"]) == 2
    encoded = json.dumps(inventory)
    assert "must-not-leak" not in encoded
    assert "telegram_bot_token" not in encoded
    target = tmp_path / "artifacts" / "baseline.json"
    write_inventory(target, inventory)
    assert target.stat().st_mode & 0o777 == 0o600
    assert read_inventory(target) == inventory
    with pytest.raises(InventoryError, match="refusing to overwrite"):
        write_inventory(target, inventory)


def test_compare_blocks_lost_added_or_rebound_topic(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _first, second = _write_sources(config)
    baseline = build_inventory(
        config,
        live_tmux={"available": False, "reason": "test", "panes": []},
        generated_at=datetime(2026, 10, 3, tzinfo=UTC),
    )
    raw = json.loads(config.topic_config_path.read_text(encoding="utf-8"))
    raw["topics"].pop("43")
    raw["topics"]["42"]["cwd"] = str(second)
    raw["topics"]["44"] = {
        "name": "Unexpected",
        "cwd": str(second),
        "engine": "codex",
        "exec_mode": "tmux",
    }
    config.topic_config_path.write_text(json.dumps(raw), encoding="utf-8")
    candidate = build_inventory(
        config,
        live_tmux={"available": False, "reason": "test", "panes": []},
        generated_at=datetime(2026, 10, 3, 1, tzinfo=UTC),
    )
    report = compare_inventories(baseline, candidate)
    assert report["accepted"] is False
    assert report["missing_source_refs"] == ["agent-1/-1001/43"]
    assert report["unexpected_source_refs"] == ["agent-1/-1001/44"]
    assert report["field_changes"] == [
        {
            "source_ref": "agent-1/-1001/42",
            "changes": {"canonical_cwd": {"before": str(_first), "after": str(second)}},
        }
    ]


def test_inventory_digest_detects_tampering(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_sources(config)
    inventory = build_inventory(
        config,
        live_tmux={"available": False, "reason": "test", "panes": []},
    )
    target = tmp_path / "inventory.json"
    write_inventory(target, inventory)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["topics"][0]["name"] = "tampered"
    target.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(target, 0o600)
    with pytest.raises(InventoryError, match="digest verification"):
        read_inventory(target)


def test_inventory_uses_agent_instance_tmux_prefix(tmp_path: Path) -> None:
    base = _config(tmp_path)
    config = replace(base, tmux_session_prefix="prod-a-")
    _write_sources(config)
    raw_state = json.loads(config.tmux_state_path.read_text(encoding="utf-8"))
    raw_state["-1001:42"]["session_name"] = "prod-a-n1001-42"
    config.tmux_state_path.write_text(json.dumps(raw_state), encoding="utf-8")
    inventory = build_inventory(
        config,
        live_tmux={"available": False, "reason": "test", "panes": []},
    )
    topics = inventory["topics"]
    assert isinstance(topics, list)
    assert topics[0]["expected_tmux_session"] == "prod-a-n1001-42"


def test_shadow_confidence_requires_exact_state_and_live_pane(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first, _second = _write_sources(config)
    inventory = build_inventory(
        config,
        live_tmux={
            "available": True,
            "reason": None,
            "panes": [
                {
                    "session_name": "cc-n1001-42",
                    "window_id": "@1",
                    "pane_id": "%1",
                    "pane_pid": 123,
                    "cwd": str(first),
                    "dead": False,
                }
            ],
        },
    )
    topics = inventory["topics"]
    assert isinstance(topics, list)
    assert topics[0]["shadow_confidence"] == "OBSERVED_VERIFIED"
    assert topics[0]["shadow_evidence"] == {
        "topic_config_valid": True,
        "exact_upstream_state": True,
        "exact_live_tmux_pane": True,
        "account_or_applied_revision_proven": False,
    }
    assert topics[1]["shadow_confidence"] == "OBSERVED_PARTIAL"


def test_shadow_confidence_is_unknown_for_ambiguous_live_identity(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first, _second = _write_sources(config)
    pane = {
        "session_name": "cc-n1001-42",
        "window_id": "@1",
        "pane_id": "%1",
        "pane_pid": 123,
        "cwd": str(first),
        "dead": False,
    }
    inventory = build_inventory(
        config,
        live_tmux={
            "available": True,
            "reason": None,
            "panes": [pane, {**pane, "pane_id": "%2", "pane_pid": 124}],
        },
    )
    topics = inventory["topics"]
    assert isinstance(topics, list)
    assert topics[0]["shadow_confidence"] == "UNKNOWN"
    assert "DUPLICATE_LIVE_TMUX_PANE" in topics[0]["issues"]
