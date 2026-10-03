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


def test_authoritative_snapshot_changes_generation_and_requires_explicit_retirement(
    tmp_path: Path,
) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    first = RuntimeBinding(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=42,
        project_id="project-1",
        canonical_cwd="/srv/project",
        expected_tmux_session="cc-n1001-42",
        config_digest="sha256:config",
    )
    store.upsert_binding(first)
    store.put_effective_spec(_spec(), codex_home=tmp_path, profile_fingerprint="fp")

    second = RuntimeBinding(
        binding_id="binding-2",
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=43,
        project_id="project-2",
        canonical_cwd="/srv/project-2",
        expected_tmux_session="cc-n1001-43",
        config_digest="sha256:config",
    )
    second_spec = DesiredSpec.build(
        binding_id="binding-2",
        agent_instance_id="agent-1",
        project_id="project-2",
        canonical_cwd="/srv/project-2",
        provider="codex",
        account_profile="work",
        model="gpt-test",
        reasoning_effort="high",
        generation_id="gen-2",
        revision=1,
    )
    generation_two = (
        (first, _spec(generation="gen-2"), tmp_path, "fp"),
        (second, second_spec, tmp_path, "fp"),
    )
    store.apply_authoritative_snapshot(
        agent_instance_id="agent-1",
        generation_id="gen-2",
        snapshot_digest="sha256:snapshot-1",
        updates=generation_two,
        retired_binding_ids=frozenset(),
    )
    assert store.get_effective_spec("binding-1")[0].generation_id == "gen-2"  # type: ignore[index]
    assert store.get_effective_spec("binding-2")[0].generation_id == "gen-2"  # type: ignore[index]

    with pytest.raises(ValueError, match="omits existing"):
        store.apply_authoritative_snapshot(
            agent_instance_id="agent-1",
            generation_id="gen-2",
            snapshot_digest="sha256:snapshot-2",
            updates=(generation_two[0],),
            retired_binding_ids=frozenset(),
        )

    store.apply_authoritative_snapshot(
        agent_instance_id="agent-1",
        generation_id="gen-2",
        snapshot_digest="sha256:snapshot-3",
        updates=(generation_two[0],),
        retired_binding_ids=frozenset({"binding-2"}),
    )
    assert store.get_binding("binding-2").configured is False  # type: ignore[union-attr]
    assert store.get_effective_spec("binding-2") is None


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
