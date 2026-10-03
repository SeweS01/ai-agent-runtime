"""SQLite-backed local authority for bindings and verified effective specs."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from ai_agent_runtime.domain import DesiredSpec, RuntimeBinding

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
        self._connection.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '1')"
        )
        self._connection.commit()
        os.chmod(path, 0o600)

    def close(self) -> None:
        self._connection.close()

    def upsert_binding(self, binding: RuntimeBinding) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO bindings(
                    binding_id, agent_instance_id, chat_id, thread_id, project_id,
                    canonical_cwd, expected_tmux_session, configured
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(binding_id) DO UPDATE SET
                    agent_instance_id=excluded.agent_instance_id,
                    chat_id=excluded.chat_id,
                    thread_id=excluded.thread_id,
                    project_id=excluded.project_id,
                    canonical_cwd=excluded.canonical_cwd,
                    expected_tmux_session=excluded.expected_tmux_session,
                    configured=excluded.configured
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
        )

    def put_effective_spec(
        self,
        spec: DesiredSpec,
        *,
        codex_home: Path,
        profile_fingerprint: str,
    ) -> None:
        if not spec.verify_digest():
            raise ValueError("desired spec digest mismatch")
        existing = self._connection.execute(
            "SELECT generation_id, revision, spec_digest FROM effective_specs WHERE binding_id=?",
            (spec.binding_id,),
        ).fetchone()
        if existing is not None and existing["generation_id"] == spec.generation_id:
            if spec.revision < existing["revision"]:
                raise ValueError("revision rollback within generation")
            if (
                spec.revision == existing["revision"]
                and spec.spec_digest != existing["spec_digest"]
            ):
                raise ValueError("same revision has a different digest")
        payload = json.dumps(
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
        with self._connection:
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
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO instances(
                    instance_id, binding_id, wrapper_pid, wrapper_starttime, child_pid,
                    child_starttime, tmux_session, tmux_pane, lifecycle, applied_digest, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    child_pid=excluded.child_pid,
                    child_starttime=excluded.child_starttime,
                    lifecycle=excluded.lifecycle,
                    applied_digest=excluded.applied_digest,
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
                    time.time(),
                ),
            )

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
