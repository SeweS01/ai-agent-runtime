import json
from pathlib import Path

import pytest

from ai_agent_runtime.domain import RuntimeBinding, WrapperIdentity
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.telegram_upstream.adapter import TelegramUpstreamAdapter
from ai_agent_runtime.telegram_upstream.correlation import phase_a_correlate, phase_b_verify
from ai_agent_runtime.telegram_upstream.tmux_state import read_tmux_states
from ai_agent_runtime.telegram_upstream.topic_config import (
    read_topic_config,
    reconcile_managed_topic,
    reconcile_managed_topics,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_tmux_name_matches_pinned_upstream_algorithm() -> None:
    adapter = TelegramUpstreamAdapter("0" * 40)
    assert adapter.expected_session_name(chat_id=-100123, thread_id=44) == "cc-n100123-44"
    assert adapter.expected_session_name(chat_id=123, thread_id=None) == "cc-123-0"


def test_topic_reconcile_never_creates_missing_key_and_preserves_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "topic_config.json"
    _write_json(
        path,
        {"topics": {"42": {"cwd": "/old", "engine": "claude", "custom": 7}}},
    )
    before = read_topic_config(path)
    with pytest.raises(KeyError):
        reconcile_managed_topic(
            path,
            thread_id=99,
            cwd="/new",
            expected_digest=before.digest,
            backup_dir=tmp_path / "backups",
        )
    after = reconcile_managed_topic(
        path,
        thread_id=42,
        cwd="/new",
        expected_digest=before.digest,
        backup_dir=tmp_path / "backups",
    )
    entry = after.topic(42)
    assert entry is not None
    assert entry.raw["custom"] == 7
    assert entry.is_managed_candidate
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_topic_snapshot_reconcile_is_one_atomic_write(tmp_path: Path) -> None:
    path = tmp_path / "topic_config.json"
    _write_json(
        path,
        {
            "topics": {
                "42": {"cwd": "/old-a", "engine": "claude", "custom": "a"},
                "43": {"cwd": "/old-b", "engine": "claude", "custom": "b"},
            }
        },
    )
    before = read_topic_config(path)
    after = reconcile_managed_topics(
        path,
        managed_topics={42: "/new-a", 43: "/new-b"},
        expected_digest=before.digest,
        backup_dir=tmp_path / "backups",
    )
    assert after.topic(42).cwd == "/new-a"  # type: ignore[union-attr]
    assert after.topic(43).cwd == "/new-b"  # type: ignore[union-attr]
    assert after.topic(42).raw["custom"] == "a"  # type: ignore[union-attr]
    assert after.topic(43).raw["custom"] == "b"  # type: ignore[union-attr]
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_two_phase_correlation(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    topic_path = tmp_path / "topic_config.json"
    state_path = tmp_path / "state.json"
    _write_json(
        topic_path,
        {"topics": {"42": {"cwd": str(cwd), "engine": "codex", "exec_mode": "tmux"}}},
    )
    snapshot = read_topic_config(topic_path)
    binding = RuntimeBinding(
        binding_id="binding-1",
        agent_instance_id="agent-1",
        chat_id=-1001,
        thread_id=42,
        project_id="project-1",
        canonical_cwd=str(cwd),
        expected_tmux_session="cc-n1001-42",
        config_digest=snapshot.digest,
    )
    identity = WrapperIdentity(
        pid=1,
        starttime=2,
        uid=1000,
        tmux_session="cc-n1001-42",
        tmux_pane="%1",
        cwd=str(cwd),
    )
    assert (
        phase_a_correlate(
            identity=identity,
            bindings=(binding,),
            topic_config=snapshot,
        )
        == binding
    )
    with pytest.raises(RuntimeFailureError) as missing:
        phase_b_verify(
            binding=binding,
            identity=identity,
            states=(),
            topic_config=read_topic_config(topic_path),
        )
    assert missing.value.code is ErrorCode.CORRELATION_MISMATCH
    _write_json(
        state_path,
        {
            "-1001:42": {
                "session_name": "cc-n1001-42",
                "cwd": str(cwd),
                "provider": "codex",
                "session_id": "codex-session",
            }
        },
    )
    verified = phase_b_verify(
        binding=binding,
        identity=identity,
        states=read_tmux_states(state_path),
        topic_config=read_topic_config(topic_path),
    )
    assert verified.session_id == "codex-session"


def test_codex_subprocess_config_fails_closed(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    topic_path = tmp_path / "topic_config.json"
    _write_json(
        topic_path,
        {"topics": {"42": {"cwd": str(cwd), "engine": "codex", "exec_mode": "subprocess"}}},
    )
    snapshot = read_topic_config(topic_path)
    binding = RuntimeBinding(
        "binding-1",
        "agent-1",
        -1001,
        42,
        "project-1",
        str(cwd),
        "cc-n1001-42",
        config_digest=snapshot.digest,
    )
    identity = WrapperIdentity(1, 2, 1000, "cc-n1001-42", "%1", str(cwd))
    with pytest.raises(RuntimeFailureError) as failure:
        phase_a_correlate(
            identity=identity,
            bindings=(binding,),
            topic_config=snapshot,
        )
    assert failure.value.code is ErrorCode.UNSUPPORTED_CODEX_SUBPROCESS
