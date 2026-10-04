import json
import sqlite3
from pathlib import Path

import pytest

from ai_agent_runtime.domain import (
    ObservedSpecProposal,
    RuntimeBinding,
    UserNotificationCode,
    sha256_digest,
)
from ai_agent_runtime.state.store import RuntimeStore


def _store(tmp_path: Path) -> RuntimeStore:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.upsert_binding(
        RuntimeBinding(
            binding_id="binding-1",
            agent_instance_id="agent-1",
            chat_id=-1001,
            thread_id=42,
            project_id="project-1",
            canonical_cwd="/srv/project",
            expected_tmux_session="cc-n1001-42",
        )
    )
    return store


def _proposal(sequence: int = 1) -> ObservedSpecProposal:
    return ObservedSpecProposal(
        binding_id="binding-1",
        base_generation_id="gen-1",
        base_revision=4,
        base_spec_digest="sha256:" + "1" * 64,
        model="gpt-new",
        reasoning_effort="high",
        instance_id="instance-1",
        observation_sequence=sequence,
        evidence_digest="sha256:" + "2" * 64,
    )


def test_observed_proposal_is_durable_retried_and_idempotently_acked(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    event_id = store.enqueue_observed_proposal(_proposal())
    assert store.enqueue_observed_proposal(_proposal()) == event_id
    first = store.claim_outbox(now=100.0)
    assert len(first) == 1
    assert first[0]["attempt"] == 1
    assert first[0]["payload"]["base_revision"] == 4  # type: ignore[index]
    assert store.claim_outbox(now=101.0) == ()
    retried = store.claim_outbox(now=102.0)
    assert retried[0]["attempt"] == 2
    store.close()

    reopened = RuntimeStore(tmp_path / "runtime.sqlite")
    reopened.acknowledge_outbox(event_id, "STALE_OBSERVED")
    reopened.acknowledge_outbox(event_id, "STALE_OBSERVED")
    assert reopened.claim_outbox(now=1000.0) == ()
    with pytest.raises(ValueError, match="different final"):
        reopened.acknowledge_outbox(event_id, "ACKED")


def test_latest_observed_sequence_cannot_be_overtaken_by_older_pending_event(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    older = _proposal(sequence=1)
    newer = _proposal(sequence=2)
    store.enqueue_observed_proposal(older)
    store.enqueue_observed_proposal(newer)

    first = store.claim_outbox(now=100.0)
    assert len(first) == 1
    assert first[0]["event_id"] == newer.event_id
    assert first[0]["payload"]["observation_sequence"] == 2  # type: ignore[index]

    # The older proposal remains blocked even while the newer event is waiting
    # for retry after a connector failure.
    assert store.claim_outbox(now=101.0) == ()
    retried = store.claim_outbox(now=102.0)
    assert len(retried) == 1
    assert retried[0]["event_id"] == newer.event_id

    store.acknowledge_outbox(newer.event_id, "ACKED")
    released = store.claim_outbox(now=103.0)
    assert len(released) == 1
    assert released[0]["event_id"] == older.event_id


def test_existing_observed_outbox_rows_gain_ordering_sequence_on_schema_upgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite"
    proposal = _proposal(sequence=7)
    payload = proposal.payload()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE outbox (
            event_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            binding_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL NOT NULL,
            ack_state TEXT NOT NULL DEFAULT 'PENDING'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO outbox(
            event_id, kind, binding_id, payload_json, payload_digest,
            attempts, next_retry_at, ack_state
        ) VALUES(?, 'OBSERVED_SPEC_PROPOSAL', ?, ?, ?, 0, 0, 'PENDING')
        """,
        (proposal.event_id, proposal.binding_id, encoded, sha256_digest(payload)),
    )
    connection.commit()
    connection.close()

    store = RuntimeStore(database)
    store.enqueue_observed_proposal(proposal)
    claimed = store.claim_outbox(now=100.0)
    assert len(claimed) == 1
    assert claimed[0]["event_id"] == proposal.event_id
    assert claimed[0]["payload"]["observation_sequence"] == 7  # type: ignore[index]


def test_outbox_event_id_cannot_be_reused_with_another_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.enqueue_outbox(
        event_id="event-1",
        kind="NOTICE",
        binding_id="binding-1",
        payload={"message": "first"},
    )
    with pytest.raises(ValueError, match="different payload"):
        store.enqueue_outbox(
            event_id="event-1",
            kind="NOTICE",
            binding_id="binding-1",
            payload={"message": "second"},
        )


def test_user_notification_is_sanitized_durable_and_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    binding = store.get_binding("binding-1")
    assert binding is not None
    first = store.enqueue_user_notification(
        code=UserNotificationCode.RESUME_BINDING_MISMATCH,
        binding=binding,
        event_ref="request-1",
    )
    second = store.enqueue_user_notification(
        code=UserNotificationCode.RESUME_BINDING_MISMATCH,
        binding=binding,
        event_ref="request-1",
    )
    assert first == second
    events = store.claim_outbox(now=100.0)
    assert len(events) == 1
    assert events[0]["kind"] == "USER_NOTIFICATION_REQUIRED"
    assert events[0]["payload"] == {
        "schema": "ai-agent-runtime.user-notification.v1",
        "notification_code": "RESUME_BINDING_MISMATCH",
        "agent_instance_id": "agent-1",
        "binding_id": "binding-1",
        "chat_id": -1001,
        "thread_id": 42,
    }
