from __future__ import annotations

import hashlib
from pathlib import Path

from ai_agent_runtime import __version__
from ai_agent_runtime.deployment.audit import (
    DEPLOYMENT_MANIFEST_SCHEMA,
    audit_deployment_manifest,
)
from ai_agent_runtime.migration.inventory import _digest, write_json_artifact


def _executable(path: Path, content: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o700)
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _instance(tmp_path: Path, *, instance_id: str = "agent-1", chat_id: int = -1001) -> dict:
    root = tmp_path / instance_id
    root.mkdir()
    codex = root / "codex-real"
    codex_digest = _executable(codex, b"#!/bin/sh\nexit 0\n")
    wrapper = root / f"codex-wrapper@{instance_id}"
    _executable(wrapper, b"#!/bin/sh\nexit 1\n")
    checkout = root / "telegram"
    checkout.mkdir()
    config = root / "runtime.toml"
    config.write_text(
        f"""
[runtime]
agent_instance_id = "{instance_id}"
forum_chat_id = {chat_id}
state_dir = "{root / "state"}"
socket = "{root / "run/runtime.sock"}"
database = "{root / "state/runtime.sqlite"}"

[telegram_agent]
topic_config = "{root / "telegram/topic_config.json"}"
tmux_state = "{root / "telegram/tmux_sessions/state.json"}"
upstream_sha = "{"0" * 40}"
tmux_session_prefix = "cc-"

[codex]
real_binary = "{codex}"
version = "0.156.1"
sha256 = "{codex_digest}"
profiles_root = "{root / "profiles"}"

[policy]
allowed_project_roots = ["{root / "projects"}"]
retirement_enabled = false
retirement_qualification_report_id = ""
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return {
        "agent_instance_id": instance_id,
        "forum_chat_id": chat_id,
        "telegram_bot_token_fingerprint": f"sha256:{'1' * 64}",
        "runtime_config": str(config),
        "wrapper_path": str(wrapper),
        "telegram_checkout": str(checkout),
        "runtime_service": {
            "unit_name": f"ai-agent-runtime@{instance_id}.service",
            "type": "notify",
            "restart": "always",
            "restart_sec_seconds": 2,
            "watchdog_sec_seconds": 30,
            "runtime_directory_mode": "0700",
            "umask": "0077",
            "socket_mode": "0600",
            "socket_accept": False,
            "config_path": str(config),
        },
        "telegram_service": {
            "unit_name": f"telegram-ai-agent@{instance_id}.service",
            "kill_mode": "process",
            "telegram_codex_bin": str(wrapper),
            "codex_auto_update_enabled": False,
        },
    }


def _qualified() -> dict[str, object]:
    return {
        "accepted": True,
        "report_id": "qualification-1",
        "release_tuple": {
            "telegram_agent_sha": "0" * 40,
            "codex_version": "0.156.1",
            "codex_binary_sha256": "placeholder",
            "runtime_release": __version__,
            "protocol_major": 1,
            "cus_connector_version": "runtime-v1",
        },
    }


def _manifest(tmp_path: Path, instances: list[dict]) -> Path:
    qualification = tmp_path / "qualification.json"
    qualification.write_text("{}\n", encoding="utf-8")
    rollback = tmp_path / "rollback.json"
    rollback.write_text("{}\n", encoding="utf-8")
    material: dict[str, object] = {
        "schema": DEPLOYMENT_MANIFEST_SCHEMA,
        "qualification_manifest": str(qualification),
        "rollback_manifest": str(rollback),
        "agent_instances": instances,
    }
    material["manifest_digest"] = _digest(material)
    path = tmp_path / "deployment.json"
    write_json_artifact(path, material)
    return path


def _verifier_for(path: Path) -> dict[str, object]:
    result = _qualified()
    codex = path.parent / "agent-1" / "codex-real"
    release_tuple = result["release_tuple"]
    assert isinstance(release_tuple, dict)
    release_tuple["codex_binary_sha256"] = (
        f"sha256:{hashlib.sha256(codex.read_bytes()).hexdigest()}"
    )
    return result


def _rollback_verified(_path: Path) -> dict[str, object]:
    return {
        "accepted": True,
        "manifest_digest": f"sha256:{'2' * 64}",
        "control_period_end": "2099-01-01T00:00:00Z",
        "control_period_active": True,
    }


def test_deployment_audit_accepts_exact_isolated_staging_layout(tmp_path: Path) -> None:
    path = _manifest(tmp_path, [_instance(tmp_path)])
    report = audit_deployment_manifest(
        path,
        qualification_verifier=_verifier_for,
        rollback_verifier=_rollback_verified,
        git_head_reader=lambda _path: "0" * 40,
    )
    assert report["accepted"] is True
    assert report["status"] == "READY_FOR_STAGING"
    assert report["mutations_performed"] is False
    assert report["production_cutover_authorized"] is False


def test_deployment_audit_blocks_duplicate_forum_and_unsafe_systemd(tmp_path: Path) -> None:
    first = _instance(tmp_path, instance_id="agent-1", chat_id=-1001)
    second = _instance(tmp_path, instance_id="agent-2", chat_id=-1001)
    second["telegram_bot_token_fingerprint"] = f"sha256:{'2' * 64}"
    second["telegram_service"]["kill_mode"] = "control-group"
    path = _manifest(tmp_path, [first, second])
    report = audit_deployment_manifest(
        path,
        qualification_verifier=_verifier_for,
        rollback_verifier=_rollback_verified,
        git_head_reader=lambda _path: "0" * 40,
    )
    assert report["accepted"] is False
    assert report["status"] == "BLOCKED"
    assert "deployment_forum_chat_id_not_unique" in report["errors"]
    assert "agent-2:telegram_service:kill_mode_mismatch" in report["errors"]


def test_deployment_audit_blocks_unqualified_release(tmp_path: Path) -> None:
    path = _manifest(tmp_path, [_instance(tmp_path)])

    def blocked(_path: Path) -> dict[str, object]:
        result = _verifier_for(_path)
        result["accepted"] = False
        return result

    report = audit_deployment_manifest(
        path,
        qualification_verifier=blocked,
        rollback_verifier=_rollback_verified,
        git_head_reader=lambda _path: "0" * 40,
    )
    assert report["accepted"] is False
    assert "release_not_qualified" in report["errors"]


def test_deployment_audit_blocks_unverified_rollback_path(tmp_path: Path) -> None:
    path = _manifest(tmp_path, [_instance(tmp_path)])
    report = audit_deployment_manifest(
        path,
        qualification_verifier=_verifier_for,
        rollback_verifier=lambda _path: {"accepted": False},
        git_head_reader=lambda _path: "0" * 40,
    )
    assert report["accepted"] is False
    assert "rollback_path_not_verified" in report["errors"]
