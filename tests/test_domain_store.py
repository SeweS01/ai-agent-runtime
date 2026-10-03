from dataclasses import replace
from pathlib import Path

import pytest

from ai_agent_runtime.domain import DesiredSpec, RuntimeBinding
from ai_agent_runtime.state.store import RuntimeStore


def _spec(revision: int = 1, *, generation: str = "gen-1") -> DesiredSpec:
    return DesiredSpec.build(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        project_id="project-1",
        canonical_cwd="/srv/project",
        provider="codex",
        account_profile="work",
        model="gpt-test",
        reasoning_effort="high",
        generation_id=generation,
        revision=revision,
    )


def test_spec_digest_is_stable_and_detects_tampering() -> None:
    spec = _spec()
    assert spec.verify_digest()
    altered = replace(spec, model="different")
    assert not altered.verify_digest()


def test_store_rejects_revision_rollback_and_conflicting_digest(tmp_path: Path) -> None:
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
    store.put_effective_spec(_spec(2), codex_home=tmp_path, profile_fingerprint="fp")
    with pytest.raises(ValueError, match="rollback"):
        store.put_effective_spec(_spec(1), codex_home=tmp_path, profile_fingerprint="fp")

    conflicting = DesiredSpec.build(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        project_id="project-1",
        canonical_cwd="/srv/project",
        provider="codex",
        account_profile="work",
        model="different",
        reasoning_effort="high",
        generation_id="gen-1",
        revision=2,
    )
    with pytest.raises(ValueError, match="different digest"):
        store.put_effective_spec(
            conflicting,
            codex_home=tmp_path,
            profile_fingerprint="fp",
        )
    with pytest.raises(ValueError, match="authoritative snapshot"):
        store.put_effective_spec(
            _spec(1, generation="gen-2"),
            codex_home=tmp_path,
            profile_fingerprint="fp",
        )


def test_session_provenance_is_immutable(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    for binding_id in ("binding-1", "binding-2"):
        store.upsert_binding(
            RuntimeBinding(
                binding_id=binding_id,
                agent_instance_id="agent-1",
                chat_id=-1001,
                thread_id=1 if binding_id == "binding-1" else 2,
                project_id="project-1",
                canonical_cwd="/srv/project",
                expected_tmux_session=f"session-{binding_id}",
            )
        )
    store.record_session_provenance(
        session_id="session-id",
        binding_id="binding-1",
        account_profile="work",
    )
    with pytest.raises(ValueError, match="immutable"):
        store.record_session_provenance(
            session_id="session-id",
            binding_id="binding-2",
            account_profile="work",
        )
