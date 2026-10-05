from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon.server import RuntimeServer
from ai_agent_runtime.domain import DesiredSpec, RuntimeBinding
from ai_agent_runtime.telegram_upstream.session_view import (
    SessionViewError,
    project_session_transcripts,
)


def _rollout(profile: Path, session_id: str) -> Path:
    root = profile / "sessions" / "2026" / "10" / "05"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"rollout-2026-10-05T13-00-00-{session_id}.jsonl"
    path.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    path.chmod(0o600)
    return path


def test_projects_multiple_profiles_without_merging_other_state(tmp_path: Path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    first_rollout = _rollout(first, "session-a")
    second_rollout = _rollout(second, "session-b")
    (first / "auth.json").write_text("secret", encoding="utf-8")
    (second / "state_5.sqlite").write_text("private", encoding="utf-8")
    view = tmp_path / "view"
    view.mkdir(mode=0o700)

    result = project_session_transcripts(profiles={"a": first, "b": second}, view_root=view)

    assert result.sources == result.added == 2
    assert (view / "2026/10/05" / first_rollout.name).resolve() == first_rollout
    assert (view / "2026/10/05" / second_rollout.name).resolve() == second_rollout
    assert not (view / "auth.json").exists()
    assert not (view / "state_5.sqlite").exists()
    repeated = project_session_transcripts(profiles={"a": first, "b": second}, view_root=view)
    assert repeated.added == 0
    assert repeated.existing == 2


def test_rollout_collision_fails_before_creating_any_links(tmp_path: Path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    _rollout(first, "same")
    _rollout(second, "same")
    view = tmp_path / "view"
    view.mkdir(mode=0o700)

    with pytest.raises(SessionViewError, match="filename collision"):
        project_session_transcripts(profiles={"a": first, "b": second}, view_root=view)

    assert list(view.iterdir()) == []


def test_existing_unrelated_target_is_never_replaced(tmp_path: Path) -> None:
    profile = tmp_path / "a"
    profile.mkdir(mode=0o700)
    source = _rollout(profile, "one")
    view = tmp_path / "view"
    target = view / "2026/10/05" / source.name
    target.parent.mkdir(parents=True, mode=0o700)
    target.write_text("other user's data", encoding="utf-8")

    with pytest.raises(SessionViewError, match="target collision"):
        project_session_transcripts(profiles={"a": profile}, view_root=view)

    assert target.read_text(encoding="utf-8") == "other user's data"


def test_symlinked_profile_sessions_are_refused(tmp_path: Path) -> None:
    profile = tmp_path / "a"
    profile.mkdir(mode=0o700)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    (profile / "sessions").symlink_to(other)
    view = tmp_path / "view"
    view.mkdir(mode=0o700)

    with pytest.raises(SessionViewError, match="unsafe type"):
        project_session_transcripts(profiles={"a": profile}, view_root=view)


def test_daemon_projects_new_rollouts_for_configured_binding(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "work"
    profile.mkdir(parents=True, mode=0o700)
    first = _rollout(profile, "first")
    view = tmp_path / "view"
    view.mkdir(mode=0o700)
    config = RuntimeConfig(
        agent_instance_id="agent-1",
        forum_chat_id=-1001,
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "run" / "runtime.sock",
        database_path=tmp_path / "state" / "runtime.sqlite",
        real_codex=tmp_path / "codex-real",
        real_codex_version="test",
        real_codex_sha256="sha256:" + "0" * 64,
        profiles_root=tmp_path / "profiles",
        topic_config_path=tmp_path / "topics.json",
        tmux_state_path=tmp_path / "tmux-state.json",
        upstream_sha="0" * 40,
        sessions_view_root=view,
        allowed_project_roots=(tmp_path,),
    )
    config.validate()
    server = RuntimeServer(config)
    binding = RuntimeBinding(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=2,
        project_id="project-1",
        canonical_cwd=str(tmp_path),
        expected_tmux_session="cc-n1001-2",
    )
    server.store.upsert_binding(binding)
    spec = DesiredSpec.build(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        project_id="project-1",
        canonical_cwd=str(tmp_path),
        provider="codex",
        account_profile="work",
        model="test-model",
        reasoning_effort="high",
        generation_id="gen-1",
        revision=1,
    )
    server.store.put_effective_spec(
        spec, codex_home=profile, profile_fingerprint="sha256:" + "1" * 64
    )

    server._project_session_view_once()
    assert (view / "2026/10/05" / first.name).resolve() == first
    second = _rollout(profile, "second")
    server._project_session_view_once()
    assert (view / "2026/10/05" / second.name).resolve() == second
    assert server._status("request-1")["status"]["session_view"]["error"] is None
    server.store.close()
