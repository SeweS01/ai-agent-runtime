"""SQLite-backed local authority for bindings and verified effective specs."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from ai_agent_runtime.domain import (
    DesiredSpec,
    ManagementMode,
    ObservedSpecProposal,
    PendingEvidenceInstance,
    RegisteredInstance,
    RolloutObservationCursor,
    RolloutObservationTarget,
    RuntimeBinding,
    sha256_digest,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bindings (
    binding_id TEXT PRIMARY KEY,
    agent_instance_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    canonical_cwd TEXT NOT NULL,
    expected_tmux_session TEXT NOT NULL,
    configured INTEGER NOT NULL CHECK (configured IN (0, 1)),
    config_digest TEXT,
    management_mode TEXT NOT NULL DEFAULT 'MANAGED',
    UNIQUE (agent_instance_id, chat_id, thread_id),
    UNIQUE (agent_instance_id, expected_tmux_session)
);
CREATE TABLE IF NOT EXISTS effective_specs (
    binding_id TEXT PRIMARY KEY REFERENCES bindings(binding_id) ON DELETE CASCADE,
    generation_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    spec_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    codex_home TEXT NOT NULL,
    profile_fingerprint TEXT NOT NULL,
    verified_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS desired_namespaces (
    agent_instance_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL,
    snapshot_digest TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_provenance (
    session_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
    account_profile TEXT NOT NULL,
    rollout_path TEXT NOT NULL,
    rollout_device INTEGER NOT NULL,
    rollout_inode INTEGER NOT NULL,
    observed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
    instance_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
    wrapper_pid INTEGER NOT NULL,
    wrapper_starttime INTEGER NOT NULL,
    child_pid INTEGER,
    child_starttime INTEGER,
    tmux_session TEXT NOT NULL,
    tmux_pane TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    applied_digest TEXT,
    launch_spec_json TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL,
    ack_state TEXT NOT NULL DEFAULT 'PENDING'
);
CREATE TABLE IF NOT EXISTS request_results (
    request_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_specs (
    instance_id TEXT PRIMARY KEY REFERENCES instances(instance_id) ON DELETE CASCADE,
    binding_id TEXT NOT NULL REFERENCES bindings(binding_id),
    generation_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    spec_digest TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    verified_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codex_rollout_observers (
    instance_id TEXT PRIMARY KEY REFERENCES instances(instance_id) ON DELETE CASCADE,
    rollout_device INTEGER NOT NULL,
    rollout_inode INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    observation_sequence INTEGER NOT NULL,
    last_model TEXT,
    last_reasoning_effort TEXT,
    active_turn_id TEXT,
    pending_model TEXT,
    pending_reasoning_effort TEXT,
    pending_evidence_digest TEXT,
    pending_turn_id TEXT,
    updated_at REAL NOT NULL
);
"""


class RuntimeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._connection = sqlite3.connect(path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(_SCHEMA)
        self._ensure_column("bindings", "config_digest", "TEXT")
        self._ensure_column(
            "bindings",
            "management_mode",
            "TEXT NOT NULL DEFAULT 'MANAGED'",
        )
        self._ensure_column("instances", "launch_spec_json", "TEXT")
        self._ensure_column("codex_rollout_observers", "active_turn_id", "TEXT")
        self._ensure_column("codex_rollout_observers", "pending_model", "TEXT")
        self._ensure_column("codex_rollout_observers", "pending_reasoning_effort", "TEXT")
        self._ensure_column("codex_rollout_observers", "pending_evidence_digest", "TEXT")
        self._ensure_column("codex_rollout_observers", "pending_turn_id", "TEXT")
        self._connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '1')"
        )
        self._connection.commit()
        os.chmod(path, 0o600)

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self._connection.close()

    def upsert_binding(self, binding: RuntimeBinding) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO bindings(
                    binding_id, agent_instance_id, chat_id, thread_id, project_id,
                    canonical_cwd, expected_tmux_session, configured,
                    config_digest, management_mode
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(binding_id) DO UPDATE SET
                    agent_instance_id=excluded.agent_instance_id,
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    project_id=excluded.project_id,
                    canonical_cwd=excluded.canonical_cwd,
                    expected_tmux_session=excluded.expected_tmux_session,
                    configured=excluded.configured,
                    config_digest=excluded.config_digest,
                    management_mode=excluded.management_mode
                """,
                (
                    binding.binding_id,
                    binding.agent_instance_id,
                    binding.chat_id,
                    binding.thread_id,
                    binding.project_id,
                    binding.canonical_cwd,
                    binding.expected_tmux_session,
                    int(binding.configured),
                    binding.config_digest,
                    binding.management_mode,
                ),
            )

    def list_bindings(self, agent_instance_id: str) -> tuple[RuntimeBinding, ...]:
        rows = self._connection.execute(
            "SELECT * FROM bindings WHERE agent_instance_id=? ORDER BY binding_id",
            (agent_instance_id,),
        ).fetchall()
        return tuple(
            RuntimeBinding(
                binding_id=row["binding_id"],
                agent_instance_id=row["agent_instance_id"],
                chat_id=row["chat_id"],
                thread_id=row["thread_id"],
                project_id=row["project_id"],
                canonical_cwd=row["canonical_cwd"],
                expected_tmux_session=row["expected_tmux_session"],
                configured=bool(row["configured"]),
                config_digest=row["config_digest"],
                management_mode=ManagementMode(row["management_mode"]),
            )
            for row in rows
        )

    def get_binding(self, binding_id: str) -> RuntimeBinding | None:
        row = self._connection.execute(
            "SELECT * FROM bindings WHERE binding_id=?",
            (binding_id,),
        ).fetchone()
        if row is None:
            return None
        return RuntimeBinding(
            binding_id=row["binding_id"],
            agent_instance_id=row["agent_instance_id"],
            chat_id=row["chat_id"],
            thread_id=row["thread_id"],
            project_id=row["project_id"],
            canonical_cwd=row["canonical_cwd"],
            expected_tmux_session=row["expected_tmux_session"],
            configured=bool(row["configured"]),
            config_digest=row["config_digest"],
            management_mode=ManagementMode(row["management_mode"]),
        )

    def update_binding_observation(
        self,
        binding_id: str,
        *,
        config_digest: str,
        management_mode: ManagementMode,
    ) -> None:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE bindings
                SET config_digest=?, management_mode=?
                WHERE binding_id=?
                """,
                (config_digest, management_mode, binding_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown binding: {binding_id}")

    def put_effective_spec(
        self,
        spec: DesiredSpec,
        *,
        codex_home: Path,
        profile_fingerprint: str,
    ) -> None:
        self.validate_effective_spec_update(spec)
        payload = self._encode_spec(spec)
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO desired_namespaces(
                    agent_instance_id, generation_id, snapshot_digest, updated_at
                ) VALUES(?, ?, NULL, ?)
                ON CONFLICT(agent_instance_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (spec.agent_instance_id, spec.generation_id, time.time()),
            )
            self._connection.execute(
                """
                INSERT INTO effective_specs(
                    binding_id, generation_id, revision, spec_digest, payload_json,
                    codex_home, profile_fingerprint, verified_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(binding_id) DO UPDATE SET
                    generation_id=excluded.generation_id,
                    revision=excluded.revision,
                    spec_digest=excluded.spec_digest,
                    payload_json=excluded.payload_json,
                    codex_home=excluded.codex_home,
                    profile_fingerprint=excluded.profile_fingerprint,
                    verified_at=excluded.verified_at
                """,
                (
                    spec.binding_id,
                    spec.generation_id,
                    spec.revision,
                    spec.spec_digest,
                    payload,
                    str(codex_home),
                    profile_fingerprint,
                    time.time(),
                ),
            )

    def validate_effective_spec_update(self, spec: DesiredSpec) -> None:
        if not spec.verify_digest():
            raise ValueError("desired spec digest mismatch")
        namespace = self._connection.execute(
            "SELECT generation_id FROM desired_namespaces WHERE agent_instance_id=?",
            (spec.agent_instance_id,),
        ).fetchone()
        if namespace is not None and namespace["generation_id"] != spec.generation_id:
            raise ValueError("new generation requires authoritative snapshot reconcile")
        existing = self._connection.execute(
            "SELECT generation_id, revision, spec_digest FROM effective_specs WHERE binding_id=?",
            (spec.binding_id,),
        ).fetchone()
        if existing is not None and existing["generation_id"] != spec.generation_id:
            raise ValueError("new generation requires authoritative snapshot reconcile")
        if existing is not None:
            if spec.revision < existing["revision"]:
                raise ValueError("revision rollback within generation")
            if (
                spec.revision == existing["revision"]
                and spec.spec_digest != existing["spec_digest"]
            ):
                raise ValueError("same revision has a different digest")

    def namespace_generation(self, agent_instance_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT generation_id FROM desired_namespaces WHERE agent_instance_id=?",
            (agent_instance_id,),
        ).fetchone()
        return None if row is None else str(row["generation_id"])

    @staticmethod
    def _encode_spec(spec: DesiredSpec) -> str:
        return json.dumps(
            {
                "binding_id": spec.binding_id,
                "agent_instance_id": spec.agent_instance_id,
                "project_id": spec.project_id,
                "canonical_cwd": spec.canonical_cwd,
                "provider": spec.provider,
                "account_profile": spec.account_profile,
                "model": spec.model,
                "reasoning_effort": spec.reasoning_effort,
                "generation_id": spec.generation_id,
                "revision": spec.revision,
                "spec_digest": spec.spec_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def apply_authoritative_snapshot(
        self,
        *,
        agent_instance_id: str,
        generation_id: str,
        snapshot_digest: str,
        updates: tuple[tuple[RuntimeBinding, DesiredSpec, Path, str], ...],
        retired_binding_ids: frozenset[str],
    ) -> None:
        """Atomically replace desired state without deleting runtime history."""

        if not updates and not retired_binding_ids:
            raise ValueError("authoritative snapshot cannot be empty")
        if not snapshot_digest.startswith("sha256:"):
            raise ValueError("snapshot digest must be sha256-prefixed")
        active_ids = {binding.binding_id for binding, _spec, _home, _fingerprint in updates}
        if len(active_ids) != len(updates):
            raise ValueError("snapshot contains duplicate binding ids")
        if active_ids & retired_binding_ids:
            raise ValueError("binding cannot be active and retired in the same snapshot")

        known = {binding.binding_id for binding in self.list_bindings(agent_instance_id)}
        if retired_binding_ids - known:
            raise ValueError("snapshot retires an unknown binding")
        omitted = known - active_ids - retired_binding_ids
        if omitted:
            raise ValueError(
                "snapshot omits existing bindings without explicit retirement: "
                + ", ".join(sorted(omitted))
            )

        topics: set[tuple[int, int]] = set()
        sessions: set[str] = set()
        current_namespace = self._connection.execute(
            "SELECT generation_id FROM desired_namespaces WHERE agent_instance_id=?",
            (agent_instance_id,),
        ).fetchone()
        same_generation = (
            current_namespace is not None and current_namespace["generation_id"] == generation_id
        )
        for binding, spec, _codex_home, _fingerprint in updates:
            if binding.agent_instance_id != agent_instance_id:
                raise ValueError("snapshot binding AgentInstance mismatch")
            if spec.agent_instance_id != agent_instance_id or spec.generation_id != generation_id:
                raise ValueError("snapshot desired namespace mismatch")
            if binding.binding_id != spec.binding_id:
                raise ValueError("snapshot binding/spec identity mismatch")
            if not spec.verify_digest():
                raise ValueError("desired spec digest mismatch")
            topic = (binding.chat_id, binding.thread_id)
            if topic in topics or binding.expected_tmux_session in sessions:
                raise ValueError("snapshot contains duplicate topic or tmux identity")
            topics.add(topic)
            sessions.add(binding.expected_tmux_session)
            existing = self.get_binding(binding.binding_id)
            if existing is not None and (
                existing.agent_instance_id != binding.agent_instance_id
                or existing.chat_id != binding.chat_id
                or existing.thread_id != binding.thread_id
                or existing.project_id != binding.project_id
                or existing.canonical_cwd != binding.canonical_cwd
            ):
                raise ValueError("binding identity is immutable")
            if same_generation:
                self.validate_effective_spec_update(spec)

        now = time.time()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO desired_namespaces(
                    agent_instance_id, generation_id, snapshot_digest, updated_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(agent_instance_id) DO UPDATE SET
                    generation_id=excluded.generation_id,
                    snapshot_digest=excluded.snapshot_digest,
                    updated_at=excluded.updated_at
                """,
                (agent_instance_id, generation_id, snapshot_digest, now),
            )
            for binding, spec, codex_home, fingerprint in updates:
                self._connection.execute(
                    """
                    INSERT INTO bindings(
                        binding_id, agent_instance_id, chat_id, thread_id, project_id,
                        canonical_cwd, expected_tmux_session, configured,
                        config_digest, management_mode
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(binding_id) DO UPDATE SET
                        configured=excluded.configured,
                        config_digest=excluded.config_digest,
                        management_mode=excluded.management_mode
                    """,
                    (
                        binding.binding_id,
                        binding.agent_instance_id,
                        binding.chat_id,
                        binding.thread_id,
                        binding.project_id,
                        binding.canonical_cwd,
                        binding.expected_tmux_session,
                        int(binding.configured),
                        binding.config_digest,
                        binding.management_mode,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO effective_specs(
                        binding_id, generation_id, revision, spec_digest, payload_json,
                        codex_home, profile_fingerprint, verified_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(binding_id) DO UPDATE SET
                        generation_id=excluded.generation_id,
                        revision=excluded.revision,
                        spec_digest=excluded.spec_digest,
                        payload_json=excluded.payload_json,
                        codex_home=excluded.codex_home,
                        profile_fingerprint=excluded.profile_fingerprint,
                        verified_at=excluded.verified_at
                    """,
                    (
                        spec.binding_id,
                        spec.generation_id,
                        spec.revision,
                        spec.spec_digest,
                        self._encode_spec(spec),
                        str(codex_home),
                        fingerprint,
                        now,
                    ),
                )
            for binding_id in retired_binding_ids:
                self._connection.execute(
                    "UPDATE bindings SET configured=0 WHERE binding_id=?",
                    (binding_id,),
                )
                self._connection.execute(
                    "DELETE FROM effective_specs WHERE binding_id=?",
                    (binding_id,),
                )

    def get_effective_spec(self, binding_id: str) -> tuple[DesiredSpec, Path, str] | None:
        row = self._connection.execute(
            """
            SELECT payload_json, codex_home, profile_fingerprint
            FROM effective_specs WHERE binding_id=?
            """,
            (binding_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        spec = DesiredSpec(**payload)
        if not spec.verify_digest():
            raise ValueError("persisted effective spec digest mismatch")
        return spec, Path(row["codex_home"]), row["profile_fingerprint"]

    def record_instance(
        self,
        *,
        instance_id: str,
        binding_id: str,
        wrapper_pid: int,
        wrapper_starttime: int,
        child_pid: int | None,
        child_starttime: int | None,
        tmux_session: str,
        tmux_pane: str,
        lifecycle: str,
        applied_digest: str | None,
        launch_spec: dict[str, object] | None = None,
    ) -> None:
        encoded_launch = (
            json.dumps(launch_spec, sort_keys=True, separators=(",", ":"))
            if launch_spec is not None
            else None
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO instances(
                    instance_id, binding_id, wrapper_pid, wrapper_starttime, child_pid,
                    child_starttime, tmux_session, tmux_pane, lifecycle, applied_digest,
                    launch_spec_json, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    child_pid=excluded.child_pid,
                    child_starttime=excluded.child_starttime,
                    lifecycle=excluded.lifecycle,
                    applied_digest=excluded.applied_digest,
                    launch_spec_json=COALESCE(
                        excluded.launch_spec_json,
                        instances.launch_spec_json
                    ),
                    updated_at=excluded.updated_at
                """,
                (
                    instance_id,
                    binding_id,
                    wrapper_pid,
                    wrapper_starttime,
                    child_pid,
                    child_starttime,
                    tmux_session,
                    tmux_pane,
                    lifecycle,
                    applied_digest,
                    encoded_launch,
                    time.time(),
                ),
            )

    def get_registered_instance(self, instance_id: str) -> RegisteredInstance | None:
        row = self._connection.execute(
            """
            SELECT instance_id, binding_id, wrapper_pid, wrapper_starttime,
                   tmux_session, tmux_pane, launch_spec_json
            FROM instances WHERE instance_id=?
            """,
            (instance_id,),
        ).fetchone()
        if row is None or row["launch_spec_json"] is None:
            return None
        launch_spec = json.loads(row["launch_spec_json"])
        if not isinstance(launch_spec, dict):
            raise ValueError("persisted instance launch spec is invalid")
        return RegisteredInstance(
            instance_id=row["instance_id"],
            binding_id=row["binding_id"],
            wrapper_pid=row["wrapper_pid"],
            wrapper_starttime=row["wrapper_starttime"],
            tmux_session=row["tmux_session"],
            tmux_pane=row["tmux_pane"],
            launch_spec=launch_spec,
        )

    def mark_instance_exited(
        self,
        *,
        instance_id: str,
        binding_id: str,
        wrapper_pid: int,
        wrapper_starttime: int,
        child_pid: int,
        child_starttime: int,
    ) -> None:
        with self._connection:
            updated = self._connection.execute(
                """
                UPDATE instances
                SET lifecycle='EXITED', updated_at=?
                WHERE instance_id=? AND binding_id=?
                  AND wrapper_pid=? AND wrapper_starttime=?
                  AND child_pid=? AND child_starttime=?
                """,
                (
                    time.time(),
                    instance_id,
                    binding_id,
                    wrapper_pid,
                    wrapper_starttime,
                    child_pid,
                    child_starttime,
                ),
            )
            if updated.rowcount != 1:
                raise KeyError("exited child does not match registered instance")

    def pending_evidence_instances(self) -> tuple[PendingEvidenceInstance, ...]:
        rows = self._connection.execute(
            """
            SELECT instance_id, binding_id, wrapper_pid, wrapper_starttime,
                   child_pid, child_starttime, tmux_session, tmux_pane,
                   launch_spec_json
            FROM instances
            WHERE child_pid IS NOT NULL
              AND child_starttime IS NOT NULL
              AND applied_digest IS NULL
              AND lifecycle IN ('IDLE', 'BUSY')
              AND launch_spec_json IS NOT NULL
            ORDER BY updated_at
            """
        ).fetchall()
        return tuple(
            PendingEvidenceInstance(
                instance_id=row["instance_id"],
                binding_id=row["binding_id"],
                wrapper_pid=row["wrapper_pid"],
                wrapper_starttime=row["wrapper_starttime"],
                child_pid=row["child_pid"],
                child_starttime=row["child_starttime"],
                tmux_session=row["tmux_session"],
                tmux_pane=row["tmux_pane"],
                launch_spec=json.loads(row["launch_spec_json"]),
            )
            for row in rows
        )

    def record_applied_spec(
        self,
        *,
        instance_id: str,
        binding_id: str,
        spec: DesiredSpec,
        evidence: dict[str, object],
        session_id: str,
        account_profile: str,
        rollout_path: str,
        rollout_device: int,
        rollout_inode: int,
    ) -> None:
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        with self._connection:
            existing = self.session_owner(session_id)
            expected_owner = (binding_id, account_profile)
            if existing is not None and existing != expected_owner:
                raise ValueError("session provenance is immutable")
            self._connection.execute(
                """
                INSERT INTO session_provenance(
                    session_id, binding_id, account_profile, rollout_path,
                    rollout_device, rollout_inode, observed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    rollout_path=excluded.rollout_path,
                    rollout_device=excluded.rollout_device,
                    rollout_inode=excluded.rollout_inode,
                    observed_at=excluded.observed_at
                """,
                (
                    session_id,
                    binding_id,
                    account_profile,
                    rollout_path,
                    rollout_device,
                    rollout_inode,
                    time.time(),
                ),
            )
            self._connection.execute(
                """
                INSERT INTO applied_specs(
                    instance_id, binding_id, generation_id, revision,
                    spec_digest, evidence_json, verified_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    binding_id=excluded.binding_id,
                    generation_id=excluded.generation_id,
                    revision=excluded.revision,
                    spec_digest=excluded.spec_digest,
                    evidence_json=excluded.evidence_json,
                    verified_at=excluded.verified_at
                """,
                (
                    instance_id,
                    binding_id,
                    spec.generation_id,
                    spec.revision,
                    spec.spec_digest,
                    encoded,
                    time.time(),
                ),
            )
            cursor = self._connection.execute(
                """
                UPDATE instances SET applied_digest=?, updated_at=?
                WHERE instance_id=? AND binding_id=?
                """,
                (spec.spec_digest, time.time(), instance_id, binding_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown instance: {instance_id}")

    def get_applied_evidence(self, instance_id: str) -> dict[str, object] | None:
        row = self._connection.execute(
            "SELECT evidence_json FROM applied_specs WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if row is None:
            return None
        decoded = json.loads(row["evidence_json"])
        if not isinstance(decoded, dict):
            raise ValueError("persisted AppliedSpec evidence is invalid")
        return decoded

    def rollout_observation_targets(self) -> tuple[RolloutObservationTarget, ...]:
        rows = self._connection.execute(
            """
            SELECT i.instance_id, i.binding_id, i.lifecycle, a.evidence_json
            FROM instances AS i
            JOIN applied_specs AS a ON a.instance_id=i.instance_id
            WHERE i.lifecycle IN ('IDLE', 'BUSY')
            ORDER BY i.updated_at, i.instance_id
            """
        ).fetchall()
        targets: list[RolloutObservationTarget] = []
        for row in rows:
            evidence = json.loads(row["evidence_json"])
            if not isinstance(evidence, dict):
                raise ValueError("persisted AppliedSpec evidence is invalid")
            targets.append(
                RolloutObservationTarget(
                    instance_id=str(row["instance_id"]),
                    binding_id=str(row["binding_id"]),
                    lifecycle=str(row["lifecycle"]),
                    rollout_path=str(evidence["rollout_path"]),
                    rollout_device=int(evidence["rollout_device"]),
                    rollout_inode=int(evidence["rollout_inode"]),
                    session_id=str(evidence["session_id"]),
                    codex_version=str(evidence["real_codex_version"]),
                )
            )
        return tuple(targets)

    def rollout_observation_cursor(
        self,
        *,
        instance_id: str,
        rollout_device: int,
        rollout_inode: int,
    ) -> RolloutObservationCursor:
        row = self._connection.execute(
            "SELECT * FROM codex_rollout_observers WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        if row is None:
            return RolloutObservationCursor(0, 0, None, None, None, None, None, None, None)
        if row["rollout_device"] != rollout_device or row["rollout_inode"] != rollout_inode:
            raise ValueError("observed rollout identity changed for live instance")
        return RolloutObservationCursor(
            offset=int(row["offset"]),
            observation_sequence=int(row["observation_sequence"]),
            last_model=row["last_model"],
            last_reasoning_effort=row["last_reasoning_effort"],
            active_turn_id=row["active_turn_id"],
            pending_model=row["pending_model"],
            pending_reasoning_effort=row["pending_reasoning_effort"],
            pending_evidence_digest=row["pending_evidence_digest"],
            pending_turn_id=row["pending_turn_id"],
        )

    def save_rollout_observation_cursor(
        self,
        *,
        instance_id: str,
        rollout_device: int,
        rollout_inode: int,
        cursor: RolloutObservationCursor,
        lifecycle: str,
    ) -> None:
        if cursor.offset < 0 or cursor.observation_sequence < 0:
            raise ValueError("invalid rollout observation cursor")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO codex_rollout_observers(
                    instance_id, rollout_device, rollout_inode, offset,
                    observation_sequence, last_model, last_reasoning_effort,
                    active_turn_id, pending_model, pending_reasoning_effort,
                    pending_evidence_digest, pending_turn_id, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    rollout_device=excluded.rollout_device,
                    rollout_inode=excluded.rollout_inode,
                    offset=excluded.offset,
                    observation_sequence=excluded.observation_sequence,
                    last_model=excluded.last_model,
                    last_reasoning_effort=excluded.last_reasoning_effort,
                    active_turn_id=excluded.active_turn_id,
                    pending_model=excluded.pending_model,
                    pending_reasoning_effort=excluded.pending_reasoning_effort,
                    pending_evidence_digest=excluded.pending_evidence_digest,
                    pending_turn_id=excluded.pending_turn_id,
                    updated_at=excluded.updated_at
                """,
                (
                    instance_id,
                    rollout_device,
                    rollout_inode,
                    cursor.offset,
                    cursor.observation_sequence,
                    cursor.last_model,
                    cursor.last_reasoning_effort,
                    cursor.active_turn_id,
                    cursor.pending_model,
                    cursor.pending_reasoning_effort,
                    cursor.pending_evidence_digest,
                    cursor.pending_turn_id,
                    time.time(),
                ),
            )
            updated = self._connection.execute(
                "UPDATE instances SET lifecycle=?, updated_at=? WHERE instance_id=?",
                (lifecycle, time.time(), instance_id),
            )
            if updated.rowcount != 1:
                raise KeyError(f"unknown instance: {instance_id}")

    def session_owner(self, session_id: str) -> tuple[str, str] | None:
        row = self._connection.execute(
            "SELECT binding_id, account_profile FROM session_provenance WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row["binding_id"]), str(row["account_profile"])

    def record_session_provenance(
        self,
        *,
        session_id: str,
        binding_id: str,
        account_profile: str,
        rollout_path: str = "",
        rollout_device: int = 0,
        rollout_inode: int = 0,
    ) -> None:
        existing = self.session_owner(session_id)
        expected = (binding_id, account_profile)
        if existing is not None and existing != expected:
            raise ValueError("session provenance is immutable")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO session_provenance(
                    session_id, binding_id, account_profile, rollout_path,
                    rollout_device, rollout_inode, observed_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    rollout_path=excluded.rollout_path,
                    rollout_device=excluded.rollout_device,
                    rollout_inode=excluded.rollout_inode,
                    observed_at=excluded.observed_at
                """,
                (
                    session_id,
                    binding_id,
                    account_profile,
                    rollout_path,
                    rollout_device,
                    rollout_inode,
                    time.time(),
                ),
            )

    def summary(self) -> dict[str, int]:
        return {
            "bindings": self._connection.execute("SELECT count(*) FROM bindings").fetchone()[0],
            "specs": self._connection.execute("SELECT count(*) FROM effective_specs").fetchone()[0],
            "instances": self._connection.execute("SELECT count(*) FROM instances").fetchone()[0],
            "outbox_pending": self._connection.execute(
                "SELECT count(*) FROM outbox WHERE ack_state='PENDING'"
            ).fetchone()[0],
        }

    def status_report(self, agent_instance_id: str) -> dict[str, object]:
        reports: list[dict[str, object]] = []
        for binding in self.list_bindings(agent_instance_id):
            effective = self.get_effective_spec(binding.binding_id)
            desired: dict[str, object] | None = None
            if effective is not None:
                spec, _codex_home, profile = effective
                desired = {
                    "generation_id": spec.generation_id,
                    "revision": spec.revision,
                    "spec_digest": spec.spec_digest,
                    "account_profile": spec.account_profile,
                    "model": spec.model,
                    "reasoning_effort": spec.reasoning_effort,
                    "profile_fingerprint": profile,
                }
            rows = self._connection.execute(
                """
                SELECT i.*, a.generation_id AS applied_generation,
                       a.revision AS applied_revision,
                       a.spec_digest AS verified_applied_digest
                FROM instances AS i
                LEFT JOIN applied_specs AS a ON a.instance_id=i.instance_id
                WHERE i.binding_id=?
                ORDER BY i.updated_at DESC, i.instance_id
                """,
                (binding.binding_id,),
            ).fetchall()
            instances = [
                {
                    "instance_id": row["instance_id"],
                    "lifecycle": row["lifecycle"],
                    "tmux_session": row["tmux_session"],
                    "tmux_pane": row["tmux_pane"],
                    "wrapper_pid": row["wrapper_pid"],
                    "wrapper_starttime": row["wrapper_starttime"],
                    "child_pid": row["child_pid"],
                    "child_starttime": row["child_starttime"],
                    "applied": (
                        {
                            "generation_id": row["applied_generation"],
                            "revision": row["applied_revision"],
                            "spec_digest": row["verified_applied_digest"],
                        }
                        if row["verified_applied_digest"] is not None
                        else None
                    ),
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
            active = [item for item in instances if item["lifecycle"] != "EXITED"]
            if not binding.configured or desired is None:
                health = "OUT_OF_SYNC"
            elif binding.management_mode != ManagementMode.MANAGED:
                health = binding.management_mode.value
            elif any(item["applied"] is None for item in active):
                health = "OUT_OF_SYNC"
            elif any(
                isinstance(item["applied"], dict)
                and item["applied"]["spec_digest"] != desired["spec_digest"]
                for item in active
            ):
                health = "PENDING_CHANGE"
            else:
                health = "RUNTIME_HEALTHY"
            reports.append(
                {
                    "binding_id": binding.binding_id,
                    "source_ref": {
                        "agent_instance_id": binding.agent_instance_id,
                        "chat_id": binding.chat_id,
                        "thread_id": binding.thread_id,
                    },
                    "project_id": binding.project_id,
                    "canonical_cwd": binding.canonical_cwd,
                    "configured": binding.configured,
                    "management_mode": binding.management_mode.value,
                    "health": health,
                    "desired": desired,
                    "instances": instances,
                }
            )
        return {
            "agent_instance_id": agent_instance_id,
            "bindings": reports,
            "summary": self.summary(),
        }

    def enqueue_observed_proposal(self, proposal: ObservedSpecProposal) -> str:
        self.enqueue_outbox(
            event_id=proposal.event_id,
            kind="OBSERVED_SPEC_PROPOSAL",
            binding_id=proposal.binding_id,
            payload=proposal.payload(),
        )
        return proposal.event_id

    def enqueue_outbox(
        self,
        *,
        event_id: str,
        kind: str,
        binding_id: str,
        payload: dict[str, object],
    ) -> None:
        payload_digest = sha256_digest(payload)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        existing = self._connection.execute(
            "SELECT payload_digest FROM outbox WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing is not None and existing["payload_digest"] != payload_digest:
            raise ValueError("outbox event_id reused with different payload")
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO outbox(
                    event_id, kind, binding_id, payload_json, payload_digest,
                    attempts, next_retry_at, ack_state
                ) VALUES(?, ?, ?, ?, ?, 0, 0, 'PENDING')
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, kind, binding_id, encoded, payload_digest),
            )

    def claim_outbox(
        self,
        *,
        limit: int = 50,
        now: float | None = None,
    ) -> tuple[dict[str, object], ...]:
        if limit < 1 or limit > 100:
            raise ValueError("outbox claim limit must be between 1 and 100")
        current = time.time() if now is None else now
        with self._connection:
            rows = self._connection.execute(
                """
                SELECT * FROM outbox
                WHERE ack_state='PENDING' AND next_retry_at <= ?
                ORDER BY next_retry_at, event_id
                LIMIT ?
                """,
                (current, limit),
            ).fetchall()
            events: list[dict[str, object]] = []
            for row in rows:
                attempt = int(row["attempts"]) + 1
                retry_delay = min(2 ** min(attempt, 8), 300)
                self._connection.execute(
                    """
                    UPDATE outbox
                    SET attempts=?, next_retry_at=?
                    WHERE event_id=? AND ack_state='PENDING'
                    """,
                    (attempt, current + retry_delay, row["event_id"]),
                )
                payload = json.loads(row["payload_json"])
                events.append(
                    {
                        "event_id": row["event_id"],
                        "kind": row["kind"],
                        "binding_id": row["binding_id"],
                        "payload": payload,
                        "payload_digest": row["payload_digest"],
                        "attempt": attempt,
                    }
                )
        return tuple(events)

    def acknowledge_outbox(self, event_id: str, ack_state: str) -> None:
        allowed = {"ACKED", "STALE_OBSERVED", "POLICY_REJECTED"}
        if ack_state not in allowed:
            raise ValueError("invalid outbox acknowledgement state")
        row = self._connection.execute(
            "SELECT ack_state FROM outbox WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown outbox event: {event_id}")
        current = str(row["ack_state"])
        if current != "PENDING" and current != ack_state:
            raise ValueError("outbox event already has a different final acknowledgement")
        with self._connection:
            self._connection.execute(
                "UPDATE outbox SET ack_state=? WHERE event_id=?",
                (ack_state, event_id),
            )

    def get_request_result(self, request_id: str, request_digest: str) -> dict[str, object] | None:
        row = self._connection.execute(
            "SELECT request_digest, response_json FROM request_results WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise ValueError("request_id replayed with different payload")
        decoded = json.loads(row["response_json"])
        if not isinstance(decoded, dict):
            raise ValueError("persisted request result is invalid")
        return decoded

    def put_request_result(
        self,
        request_id: str,
        request_digest: str,
        response: dict[str, object],
    ) -> None:
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"))
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO request_results(
                    request_id, request_digest, response_json, created_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(request_id) DO NOTHING
                """,
                (request_id, request_digest, encoded, time.time()),
            )
