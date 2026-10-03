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
CREATE TABLE IF NOT EXISTS request_results (
    request_id TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL
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

    def validate_effective_spec_update(self, spec: DesiredSpec) -> None:
        if not spec.verify_digest():
            raise ValueError("desired spec digest mismatch")
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
