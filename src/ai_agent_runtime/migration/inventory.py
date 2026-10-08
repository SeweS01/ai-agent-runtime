"""Read-only M0 inventory for Telegram Agent to Runtime migration.

The exported artifact deliberately contains only routing and effective-profile
metadata. Telegram/Codex credentials, raw environment values, prompts and
transcripts are never copied into the inventory.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.errors import RuntimeFailureError
from ai_agent_runtime.process_safety import (
    proc_environment,
    proc_starttime,
    proc_uid,
    process_executable_evidence,
    validate_profile_path,
)
from ai_agent_runtime.telegram_upstream.adapter import TelegramUpstreamAdapter
from ai_agent_runtime.telegram_upstream.tmux_state import UpstreamTmuxState, read_tmux_states
from ai_agent_runtime.telegram_upstream.topic_config import read_topic_config

INVENTORY_SCHEMA = "ai-agent-runtime.migration-inventory.v1"
COMPARISON_SCHEMA = "ai-agent-runtime.migration-comparison.v1"
_MAX_JSON_BYTES = 16 * 1024 * 1024
_TMUX_FORMAT = (
    "#{session_name}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{pane_dead}"
)


class InventoryError(ValueError):
    """An inventory source or artifact is unsafe or malformed."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _read_json_object(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        raise InventoryError(f"inventory source does not exist: {path}")
    if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
        raise InventoryError(f"inventory source is not a bounded regular file: {path}")
    raw = path.read_bytes()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InventoryError(f"inventory source is not valid JSON: {path}") from exc
    if not isinstance(parsed, dict):
        raise InventoryError(f"inventory source must contain a JSON object: {path}")
    return parsed, f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _source_evidence(path: Path, *, optional: bool = False) -> dict[str, object]:
    if not path.exists():
        if optional:
            return {"path": str(path), "present": False, "sha256": None}
        raise InventoryError(f"required inventory source does not exist: {path}")
    if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
        raise InventoryError(f"inventory source is not a bounded regular file: {path}")
    raw = path.read_bytes()
    return {
        "path": str(path),
        "present": True,
        "sha256": f"sha256:{hashlib.sha256(raw).hexdigest()}",
        "size": len(raw),
    }


def _assert_source_stable(
    name: str,
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    if before != after:
        raise InventoryError(f"{name} changed while inventory was being collected")


def _safe_cwd(raw: object) -> tuple[str | None, str | None, bool, list[str]]:
    if raw is None:
        return None, None, False, ["CWD_UNCONFIGURED"]
    if not isinstance(raw, str) or not raw:
        return None, None, False, ["CWD_INVALID"]
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return raw, None, False, ["CWD_NOT_ABSOLUTE"]
    canonical = str(path.resolve(strict=False))
    exists = path.is_dir()
    return raw, canonical, exists, [] if exists else ["CWD_MISSING"]


def collect_live_tmux(tmux_tmpdir: Path | None = None) -> dict[str, object]:
    """Collect non-content tmux pane identity without reading pane text/commands."""

    command = ["tmux"]
    if tmux_tmpdir is not None:
        if not tmux_tmpdir.is_absolute() or ".." in tmux_tmpdir.parts or tmux_tmpdir.is_symlink():
            return {"available": False, "reason": "unsafe_tmux_tmpdir", "panes": []}
        try:
            base = tmux_tmpdir.stat()
            socket_directory = tmux_tmpdir / f"tmux-{os.getuid()}"
            socket_parent = socket_directory.stat()
            socket_path = socket_directory / "default"
            socket_metadata = socket_path.lstat()
        except FileNotFoundError:
            return {"available": False, "reason": "isolated_tmux_socket_absent", "panes": []}
        except OSError:
            return {"available": False, "reason": "isolated_tmux_socket_unreadable", "panes": []}
        if (
            not stat.S_ISDIR(base.st_mode)
            or base.st_uid != os.getuid()
            or base.st_mode & 0o077
            or socket_directory.is_symlink()
            or not stat.S_ISDIR(socket_parent.st_mode)
            or socket_parent.st_uid != os.getuid()
            or socket_parent.st_mode & 0o077
            or not stat.S_ISSOCK(socket_metadata.st_mode)
            or socket_metadata.st_uid != os.getuid()
        ):
            return {"available": False, "reason": "unsafe_isolated_tmux_socket", "panes": []}
        command.extend(["-S", str(socket_path)])
    command.extend(["list-panes", "-a", "-F", _TMUX_FORMAT])
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": type(exc).__name__, "panes": []}
    if result.returncode != 0:
        return {
            "available": False,
            "reason": f"tmux_exit_{result.returncode}",
            "panes": [],
        }
    panes: list[dict[str, object]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        session, window_id, pane_id, pid_raw, cwd, dead_raw = parts
        try:
            pane_pid = int(pid_raw)
            dead = bool(int(dead_raw))
        except ValueError:
            continue
        panes.append(
            {
                "session_name": session,
                "window_id": window_id,
                "pane_id": pane_id,
                "pane_pid": pane_pid,
                "cwd": cwd,
                "dead": dead,
            }
        )
    panes.sort(key=lambda pane: (str(pane["session_name"]), str(pane["pane_id"])))
    return {"available": True, "reason": None, "panes": panes}


def _observe_live_codex_account(
    config: RuntimeConfig,
    pane: dict[str, object],
    *,
    expected_cwd: str,
) -> dict[str, object]:
    """Prove a live legacy pane's account without exporting arbitrary environment."""

    def unverified(reason: str) -> dict[str, object]:
        return {
            "verified": False,
            "code": "LIVE_CODEX_ACCOUNT_UNVERIFIED",
            "reason": reason,
        }

    raw_pid = pane.get("pane_pid")
    if not isinstance(raw_pid, int) or isinstance(raw_pid, bool) or raw_pid <= 0:
        return unverified("PANE_PID_INVALID")
    try:
        starttime = proc_starttime(raw_pid)
        uid = proc_uid(raw_pid)
        if uid != os.getuid():
            return unverified("PROCESS_UID_MISMATCH")
        process_cwd = Path(f"/proc/{raw_pid}/cwd").resolve(strict=True)
        if str(process_cwd) != expected_cwd:
            return unverified("PROCESS_CWD_MISMATCH")
        executable = process_executable_evidence(raw_pid)
        if executable.get("sha256") != config.real_codex_sha256:
            return unverified("BINARY_DIGEST_MISMATCH")
        environment = proc_environment(raw_pid)
        raw_codex_home = environment.get("CODEX_HOME")
        if not raw_codex_home:
            return unverified("CODEX_HOME_MISSING")
        profile = validate_profile_path(
            Path(raw_codex_home),
            root=config.profiles_root,
            uid=uid,
        )
        profiles_root = config.profiles_root.resolve(strict=True)
        if profile.parent != profiles_root or not profile.name:
            return unverified("PROFILE_NOT_DIRECT_CHILD")
        if proc_starttime(raw_pid) != starttime:
            return unverified("PROCESS_IDENTITY_CHANGED")
        return {
            "verified": True,
            "pid": raw_pid,
            "starttime": starttime,
            "uid": uid,
            "codex_home": str(profile),
            "account_profile": profile.name,
            "binary_sha256": executable["sha256"],
            "executable": executable,
        }
    except (OSError, RuntimeFailureError, UnicodeError, ValueError):
        return unverified("PROCESS_EVIDENCE_UNAVAILABLE")


def _load_cus_bindings(
    path: Path | None,
    *,
    agent_instance_id: str,
) -> tuple[dict[tuple[int, int], list[dict[str, object]]], dict[str, object]]:
    if path is None:
        return {}, {"configured": False, "present": False}
    raw, sha256 = _read_json_object(path)
    bindings = raw.get("bindings")
    if not isinstance(bindings, dict):
        raise InventoryError("CUS control store does not contain object-valued bindings")
    indexed: dict[tuple[int, int], list[dict[str, object]]] = defaultdict(list)
    for binding_id, value in bindings.items():
        if not isinstance(binding_id, str) or not isinstance(value, dict):
            continue
        if value.get("agent_instance_id") != agent_instance_id:
            continue
        chat_id = value.get("chat_id")
        thread_id = value.get("thread_id")
        desired = value.get("desired")
        if not isinstance(chat_id, int) or not isinstance(thread_id, int):
            continue
        safe: dict[str, object] = {
            "binding_id": binding_id,
            "project_id": value.get("project_id"),
            "canonical_cwd": value.get("canonical_cwd"),
            "lifecycle_status": value.get("lifecycle_status"),
        }
        if isinstance(desired, dict):
            safe["desired"] = {
                "generation_id": desired.get("generation_id"),
                "revision": desired.get("revision"),
                "spec_digest": desired.get("spec_digest"),
                "provider": desired.get("provider"),
                "account_profile": desired.get("account_profile"),
                "model": desired.get("model"),
                "reasoning_effort": desired.get("reasoning_effort"),
            }
        indexed[(chat_id, thread_id)].append(safe)
    return indexed, {
        "configured": True,
        "path": str(path),
        "present": True,
        "sha256": sha256,
        "store_revision": raw.get("store_revision"),
    }


def _load_legacy_project_settings(
    path: Path | None,
) -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    """Load the old project-scoped CUS settings without copying credentials.

    The legacy store has no Telegram topic identity, so records are indexed by
    canonical cwd. ``build_inventory`` only projects a record onto a topic when
    that cwd belongs to exactly one topic. Shared cwd is deliberately reported
    as ambiguous because Runtime Binding settings are topic-scoped in V1.
    """

    if path is None:
        return {}, {"configured": False, "present": False}
    raw, sha256 = _read_json_object(path)
    if raw.get("version") != 2:
        raise InventoryError("legacy project settings must use version 2")
    projects = raw.get("projects")
    if not isinstance(projects, dict):
        raise InventoryError("legacy project settings do not contain object-valued projects")

    indexed: dict[str, list[dict[str, object]]] = defaultdict(list)
    for map_key, value in projects.items():
        if not isinstance(map_key, str) or not map_key or not isinstance(value, dict):
            raise InventoryError("legacy project settings contain a malformed project record")
        project_id = value.get("project_id")
        project_path = value.get("project_path")
        if not isinstance(project_id, str) or not project_id:
            raise InventoryError("legacy project settings contain an invalid project_id")
        if project_id != map_key:
            raise InventoryError("legacy project settings project_id does not match its map key")
        if not isinstance(project_path, str) or not project_path:
            raise InventoryError("legacy project settings contain an invalid project_path")
        expanded = Path(project_path).expanduser()
        if not expanded.is_absolute():
            raise InventoryError("legacy project settings project_path must be absolute")
        canonical_cwd = str(expanded.resolve(strict=False))

        provider = value.get("provider_id")
        account_profile = value.get("account_profile")
        model = value.get("model")
        reasoning_effort = value.get("reasoning_effort")
        for field_name, field_value in (
            ("provider_id", provider),
            ("account_profile", account_profile),
            ("model", model),
            ("reasoning_effort", reasoning_effort),
        ):
            if field_value is not None and not isinstance(field_value, str):
                raise InventoryError(f"legacy project settings contain a non-string {field_name}")

        indexed[canonical_cwd].append(
            {
                "project_id": project_id,
                "canonical_cwd": canonical_cwd,
                "lifecycle_status": "CONFIGURED",
                "desired": {
                    "provider": provider,
                    "account_profile": account_profile,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                },
                "migration_source": "legacy_project_settings_v2",
            }
        )

    return indexed, {
        "configured": True,
        "path": str(path),
        "present": True,
        "sha256": sha256,
        "store_revision": raw.get("store_revision"),
        "safe_project_count": sum(len(records) for records in indexed.values()),
        "safe_records_digest": _digest(indexed),
    }


def _load_runtime_bindings(
    path: Path,
    *,
    agent_instance_id: str,
) -> tuple[dict[tuple[int, int], list[dict[str, object]]], dict[str, object]]:
    if not path.exists():
        return {}, {"path": str(path), "present": False, "sha256": None}
    if not path.is_file():
        raise InventoryError("Runtime database is not a regular file")
    sha256 = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    uri = f"file:{path}?mode=ro"
    indexed: dict[tuple[int, int], list[dict[str, object]]] = defaultdict(list)
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT b.binding_id, b.chat_id, b.thread_id, b.project_id,
                   b.canonical_cwd, b.configured, b.management_mode,
                   e.generation_id, e.revision, e.spec_digest, e.payload_json
              FROM bindings b
              LEFT JOIN effective_specs e ON e.binding_id = b.binding_id
             WHERE b.agent_instance_id = ?
             ORDER BY b.binding_id
            """,
            (agent_instance_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise InventoryError("Runtime database cannot be inventoried read-only") from exc
    finally:
        if "connection" in locals():
            connection.close()
    for row in rows:
        effective: dict[str, object] | None = None
        payload_json = row["payload_json"]
        if isinstance(payload_json, str):
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                effective = {
                    "generation_id": row["generation_id"],
                    "revision": row["revision"],
                    "spec_digest": row["spec_digest"],
                    "provider": payload.get("provider"),
                    "account_profile": payload.get("account_profile"),
                    "model": payload.get("model"),
                    "reasoning_effort": payload.get("reasoning_effort"),
                }
        indexed[(int(row["chat_id"]), int(row["thread_id"]))].append(
            {
                "binding_id": row["binding_id"],
                "project_id": row["project_id"],
                "canonical_cwd": row["canonical_cwd"],
                "configured": bool(row["configured"]),
                "management_mode": row["management_mode"],
                "effective": effective,
            }
        )
    return indexed, {
        "path": str(path),
        "present": True,
        "sha256": sha256,
        "safe_records_digest": _digest(
            [
                {"chat_id": chat_id, "thread_id": thread_id, "bindings": records}
                for (chat_id, thread_id), records in sorted(indexed.items())
            ]
        ),
    }


def build_inventory(
    config: RuntimeConfig,
    *,
    cus_control_store: Path | None = None,
    legacy_project_settings: Path | None = None,
    live_tmux: dict[str, object] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Build a secret-free, read-only inventory artifact for one AgentInstance."""

    if cus_control_store is not None and legacy_project_settings is not None:
        raise InventoryError("cus_control_store and legacy_project_settings are mutually exclusive")

    topic_evidence = _source_evidence(config.topic_config_path)
    topic_snapshot = read_topic_config(config.topic_config_path)
    topic_evidence_after = _source_evidence(config.topic_config_path)
    _assert_source_stable("topic_config", topic_evidence, topic_evidence_after)
    if topic_snapshot.digest != topic_evidence["sha256"]:
        raise InventoryError("topic_config digest does not match parsed snapshot")
    tmux_evidence = _source_evidence(config.tmux_state_path, optional=True)
    tmux_states = read_tmux_states(config.tmux_state_path)
    tmux_evidence_after = _source_evidence(config.tmux_state_path, optional=True)
    _assert_source_stable("tmux state", tmux_evidence, tmux_evidence_after)
    state_by_topic: dict[tuple[int, int], list[UpstreamTmuxState]] = defaultdict(list)
    for state in tmux_states:
        if state.thread_id is not None:
            state_by_topic[(state.chat_id, state.thread_id)].append(state)
    cus_by_topic, cus_evidence = _load_cus_bindings(
        cus_control_store,
        agent_instance_id=config.agent_instance_id,
    )
    legacy_by_cwd, legacy_evidence = _load_legacy_project_settings(
        legacy_project_settings,
    )
    runtime_by_topic, runtime_evidence = _load_runtime_bindings(
        config.database_path,
        agent_instance_id=config.agent_instance_id,
    )
    observed_tmux = live_tmux if live_tmux is not None else collect_live_tmux(config.tmux_tmpdir)
    panes = observed_tmux.get("panes", [])
    panes_by_session: dict[str, list[dict[str, object]]] = defaultdict(list)
    if isinstance(panes, list):
        for pane in panes:
            if isinstance(pane, dict) and isinstance(pane.get("session_name"), str):
                panes_by_session[str(pane["session_name"])].append(pane)

    topics_raw = topic_snapshot.raw["topics"]
    assert isinstance(topics_raw, dict)
    configured_cwd_counts: dict[str, int] = defaultdict(int)
    for candidate in topics_raw.values():
        if not isinstance(candidate, dict):
            continue
        _raw, candidate_canonical, _exists, _issues = _safe_cwd(candidate.get("cwd"))
        if candidate_canonical is not None:
            configured_cwd_counts[candidate_canonical] += 1
    adapter = TelegramUpstreamAdapter(
        config.upstream_sha,
        session_name_prefix=config.tmux_session_prefix,
    )
    topics: list[dict[str, object]] = []
    invalid_topic_keys: list[str] = []
    for raw_thread_id, raw_entry in topics_raw.items():
        try:
            thread_id = int(raw_thread_id)
        except (TypeError, ValueError):
            invalid_topic_keys.append(str(raw_thread_id))
            continue
        if thread_id <= 0 or not isinstance(raw_entry, dict):
            invalid_topic_keys.append(str(raw_thread_id))
            continue
        raw_cwd, canonical, cwd_exists, issues = _safe_cwd(raw_entry.get("cwd"))
        engine = str(raw_entry.get("engine", "claude"))
        exec_mode = str(raw_entry.get("exec_mode", "subprocess"))
        expected_session = adapter.expected_session_name(
            chat_id=config.forum_chat_id,
            thread_id=thread_id,
        )
        states = state_by_topic.get((config.forum_chat_id, thread_id), [])
        if len(states) > 1:
            issues.append("DUPLICATE_TMUX_STATE")
        state_payloads: list[dict[str, object]] = []
        for state in states:
            state_issues: list[str] = []
            state_canonical = str(Path(state.cwd).expanduser().resolve(strict=False))
            if canonical is not None and state_canonical != canonical:
                state_issues.append("STATE_CWD_MISMATCH")
                issues.append("STATE_CWD_MISMATCH")
            if state.session_name != expected_session:
                state_issues.append("STATE_SESSION_MISMATCH")
                issues.append("STATE_SESSION_MISMATCH")
            if state.provider != engine:
                state_issues.append("STATE_PROVIDER_MISMATCH")
                issues.append("STATE_PROVIDER_MISMATCH")
            state_payloads.append(
                {
                    "session_name": state.session_name,
                    "cwd": state.cwd,
                    "canonical_cwd": state_canonical,
                    "provider": state.provider,
                    "session_id": state.session_id,
                    "transcript_path": state.transcript_path,
                    "issues": sorted(set(state_issues)),
                }
            )
        if legacy_project_settings is not None and canonical is not None:
            cus_records = legacy_by_cwd.get(canonical, [])
            if configured_cwd_counts[canonical] > 1 and cus_records:
                issues.append("LEGACY_PROJECT_SCOPE_AMBIGUOUS")
        else:
            cus_records = cus_by_topic.get((config.forum_chat_id, thread_id), [])
        runtime_records = runtime_by_topic.get((config.forum_chat_id, thread_id), [])
        if len(cus_records) > 1:
            issues.append("DUPLICATE_CUS_BINDING")
        if len(runtime_records) > 1:
            issues.append("DUPLICATE_RUNTIME_BINDING")
        for record in [*cus_records, *runtime_records]:
            record_cwd = record.get("canonical_cwd")
            if canonical is not None and isinstance(record_cwd, str) and record_cwd != canonical:
                issues.append("BINDING_CWD_MISMATCH")
        live_panes = panes_by_session.get(expected_session, [])
        if len(live_panes) > 1:
            issues.append("DUPLICATE_LIVE_TMUX_PANE")
        live_pane_exact = False
        live_codex_process: dict[str, object] = {
            "verified": False,
            "code": "LIVE_CODEX_ACCOUNT_UNVERIFIED",
            "reason": "EXACT_LIVE_PANE_UNAVAILABLE",
        }
        if len(live_panes) == 1:
            pane = live_panes[0]
            pane_cwd = pane.get("cwd")
            pane_dead = pane.get("dead")
            if pane_dead is not False:
                issues.append("LIVE_TMUX_PANE_DEAD_OR_UNKNOWN")
            elif not isinstance(pane_cwd, str) or canonical is None:
                issues.append("LIVE_TMUX_CWD_UNKNOWN")
            elif str(Path(pane_cwd).expanduser().resolve(strict=False)) != canonical:
                issues.append("LIVE_TMUX_CWD_MISMATCH")
            else:
                live_pane_exact = True
                live_codex_process = _observe_live_codex_account(
                    config,
                    pane,
                    expected_cwd=canonical,
                )
        stable_issues = sorted(set(issues))
        exact_state = (
            len(state_payloads) == 1
            and state_payloads[0]["issues"] == []
            and canonical is not None
            and cwd_exists
        )
        if stable_issues or canonical is None or not cwd_exists:
            shadow_confidence = "UNKNOWN"
        elif exact_state and live_pane_exact:
            shadow_confidence = "OBSERVED_VERIFIED"
        else:
            shadow_confidence = "OBSERVED_PARTIAL"
        topics.append(
            {
                "source_ref": (f"{config.agent_instance_id}/{config.forum_chat_id}/{thread_id}"),
                "agent_instance_id": config.agent_instance_id,
                "chat_id": config.forum_chat_id,
                "thread_id": thread_id,
                "name": str(raw_entry.get("name", "")),
                "type": str(raw_entry.get("type", "assistant")),
                "mode": str(raw_entry.get("mode", "free")),
                "cwd": raw_cwd,
                "canonical_cwd": canonical,
                "cwd_exists": cwd_exists,
                "engine": engine,
                "exec_mode": exec_mode,
                "model_override": raw_entry.get("model"),
                "expected_tmux_session": expected_session,
                "tmux_state": state_payloads,
                "live_tmux_panes": live_panes,
                "live_codex_process": live_codex_process,
                "cus_bindings": cus_records,
                "runtime_bindings": runtime_records,
                "shadow_confidence": shadow_confidence,
                "shadow_evidence": {
                    "topic_config_valid": canonical is not None and cwd_exists,
                    "exact_upstream_state": exact_state,
                    "exact_live_tmux_pane": live_pane_exact,
                    "account_or_applied_revision_proven": False,
                },
                "issues": stable_issues,
            }
        )
    topics.sort(key=lambda item: cast(int, item["thread_id"]))

    cwd_groups: dict[str, list[str]] = defaultdict(list)
    for topic in topics:
        grouped_canonical = topic.get("canonical_cwd")
        if isinstance(grouped_canonical, str):
            cwd_groups[grouped_canonical].append(str(topic["source_ref"]))
    shared_cwds = [
        {"canonical_cwd": cwd, "source_refs": sorted(refs)}
        for cwd, refs in sorted(cwd_groups.items())
        if len(refs) > 1
    ]
    known_topics = {(config.forum_chat_id, cast(int, topic["thread_id"])) for topic in topics}
    orphan_states = [
        {
            "chat_id": state.chat_id,
            "thread_id": state.thread_id,
            "session_name": state.session_name,
            "cwd": state.cwd,
            "provider": state.provider,
        }
        for state in tmux_states
        if state.thread_id is None or (state.chat_id, state.thread_id) not in known_topics
    ]
    timestamp = generated_at or datetime.now(tz=UTC)
    shadow_counts = {
        confidence: sum(topic["shadow_confidence"] == confidence for topic in topics)
        for confidence in ("OBSERVED_VERIFIED", "OBSERVED_PARTIAL", "UNKNOWN")
    }
    body: dict[str, object] = {
        "schema": INVENTORY_SCHEMA,
        "generated_at": timestamp.isoformat().replace("+00:00", "Z"),
        "agent_instance": {
            "agent_instance_id": config.agent_instance_id,
            "forum_chat_id": config.forum_chat_id,
            "pinned_upstream_sha": config.upstream_sha,
            "tmux_session_prefix": config.tmux_session_prefix,
        },
        "sources": {
            "topic_config": topic_evidence,
            "tmux_state": tmux_evidence,
            "runtime_database": runtime_evidence,
            "cus_control_store": cus_evidence,
            "legacy_project_settings": legacy_evidence,
        },
        "topics": topics,
        "shared_canonical_cwds": shared_cwds,
        "invalid_topic_keys": sorted(invalid_topic_keys),
        "orphan_tmux_states": orphan_states,
        "live_tmux": observed_tmux,
        "shadow_confidence_counts": shadow_counts,
    }
    body["inventory_digest"] = _digest(body)
    return body


def write_inventory(path: Path, inventory: dict[str, object]) -> None:
    """Create an immutable-by-default 0600 inventory artifact atomically."""

    if path.exists() or path.is_symlink():
        raise InventoryError(f"refusing to overwrite inventory artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    encoded = json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        tmp.unlink(missing_ok=True)


def read_inventory(path: Path) -> dict[str, object]:
    payload, _sha256 = _read_json_object(path)
    if payload.get("schema") != INVENTORY_SCHEMA:
        raise InventoryError("unsupported inventory schema")
    supplied = payload.get("inventory_digest")
    if not isinstance(supplied, str):
        raise InventoryError("inventory digest is missing")
    unsigned = dict(payload)
    unsigned.pop("inventory_digest", None)
    if supplied != _digest(unsigned):
        raise InventoryError("inventory digest verification failed")
    return payload


def _topic_index(inventory: dict[str, object]) -> tuple[dict[str, dict[str, object]], list[str]]:
    raw_topics = inventory.get("topics")
    if not isinstance(raw_topics, list):
        raise InventoryError("inventory topics must be a list")
    indexed: dict[str, dict[str, object]] = {}
    duplicates: list[str] = []
    for raw in raw_topics:
        if not isinstance(raw, dict) or not isinstance(raw.get("source_ref"), str):
            raise InventoryError("inventory contains a malformed topic")
        source_ref = str(raw["source_ref"])
        if source_ref in indexed:
            duplicates.append(source_ref)
        indexed[source_ref] = raw
    return indexed, sorted(set(duplicates))


def compare_inventories(
    baseline: dict[str, object],
    candidate: dict[str, object],
    *,
    binding_plan: dict[str, object] | None = None,
) -> dict[str, object]:
    """Compare stable topic routing fields for migration acceptance."""

    base_topics, base_duplicates = _topic_index(baseline)
    candidate_topics, candidate_duplicates = _topic_index(candidate)
    missing = sorted(set(base_topics) - set(candidate_topics))
    added = sorted(set(candidate_topics) - set(base_topics))
    field_changes: list[dict[str, object]] = []
    compared_fields = ("name", "canonical_cwd", "engine", "exec_mode")
    for source_ref in sorted(set(base_topics) & set(candidate_topics)):
        changes = {
            field: {
                "before": base_topics[source_ref].get(field),
                "after": candidate_topics[source_ref].get(field),
            }
            for field in compared_fields
            if base_topics[source_ref].get(field) != candidate_topics[source_ref].get(field)
        }
        if changes:
            field_changes.append({"source_ref": source_ref, "changes": changes})
    profile_issues: list[dict[str, str]] = []
    if binding_plan is not None:
        if (
            binding_plan.get("schema") != "ai-agent-runtime.binding-migration-plan.v1"
            or binding_plan.get("source_inventory_digest") != baseline.get("inventory_digest")
            or binding_plan.get("accepted") is not True
            or binding_plan.get("plan_digest")
            != _digest({key: value for key, value in binding_plan.items() if key != "plan_digest"})
        ):
            raise InventoryError(
                "binding plan is not accepted or does not match baseline inventory"
            )
        entries = binding_plan.get("entries")
        if not isinstance(entries, list):
            raise InventoryError("binding plan entries are malformed")
        planned_sources: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("source_ref"), str):
                raise InventoryError("binding plan entry is malformed")
            source_ref = entry["source_ref"]
            if source_ref in planned_sources:
                raise InventoryError("binding plan has duplicate source refs")
            planned_sources.add(source_ref)
            if entry.get("status") != "READY":
                continue
            expected = entry.get("desired")
            topic = candidate_topics.get(source_ref)
            if not isinstance(expected, dict) or topic is None:
                profile_issues.append(
                    {"source_ref": source_ref, "code": "READY_TOPIC_OR_SPEC_MISSING"}
                )
                continue
            for field, spec_field in (
                ("cus_bindings", "desired"),
                ("runtime_bindings", "effective"),
            ):
                records = topic.get(field)
                if (
                    not isinstance(records, list)
                    or len(records) != 1
                    or not isinstance(records[0], dict)
                ):
                    profile_issues.append(
                        {"source_ref": source_ref, "code": f"{field.upper()}_MISSING_OR_AMBIGUOUS"}
                    )
                    continue
                record = records[0]
                observed = record.get(spec_field)
                if (
                    record.get("binding_id") != entry.get("binding_id")
                    or record.get("project_id") != entry.get("project_id")
                    or record.get("canonical_cwd") != expected.get("canonical_cwd")
                    or (field == "cus_bindings" and record.get("lifecycle_status") != "CONFIGURED")
                    or (
                        field == "runtime_bindings"
                        and (
                            record.get("configured") is not True
                            or record.get("management_mode") != "MANAGED"
                        )
                    )
                    or not isinstance(observed, dict)
                    or any(
                        observed.get(key) != expected.get(key)
                        for key in ("provider", "account_profile", "model", "reasoning_effort")
                    )
                ):
                    profile_issues.append(
                        {"source_ref": source_ref, "code": f"{field.upper()}_PROFILE_MISMATCH"}
                    )
        if planned_sources != set(base_topics):
            raise InventoryError("binding plan does not cover baseline source refs")
    blocking = bool(
        missing
        or added
        or field_changes
        or base_duplicates
        or candidate_duplicates
        or candidate.get("invalid_topic_keys")
        or candidate.get("orphan_tmux_states")
        or profile_issues
    )
    report: dict[str, object] = {
        "schema": COMPARISON_SCHEMA,
        "baseline_digest": baseline.get("inventory_digest"),
        "candidate_digest": candidate.get("inventory_digest"),
        "accepted": not blocking,
        "missing_source_refs": missing,
        "unexpected_source_refs": added,
        "field_changes": field_changes,
        "binding_profile_issues": profile_issues,
        "baseline_duplicate_source_refs": base_duplicates,
        "candidate_duplicate_source_refs": candidate_duplicates,
        "candidate_invalid_topic_keys": candidate.get("invalid_topic_keys", []),
        "candidate_orphan_tmux_states": candidate.get("orphan_tmux_states", []),
    }
    report["comparison_digest"] = _digest(report)
    return report


def write_json_artifact(path: Path, payload: dict[str, object]) -> None:
    write_inventory(path, payload)
