from pathlib import Path

import pytest

from ai_agent_runtime.domain import ObservedSpecProposal, RuntimeBinding
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
