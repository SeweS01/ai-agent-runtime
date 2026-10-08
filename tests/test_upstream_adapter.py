import json
from pathlib import Path

import pytest

from ai_agent_runtime.domain import RuntimeBinding, WrapperIdentity
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError
from ai_agent_runtime.telegram_upstream import topic_config as topic_config_adapter
from ai_agent_runtime.telegram_upstream.adapter import (
    TelegramUpstreamAdapter,
    matches_managed_tmux_name,
)
from ai_agent_runtime.telegram_upstream.correlation import phase_a_correlate, phase_b_verify
from ai_agent_runtime.telegram_upstream.tmux_state import UpstreamTmuxState, read_tmux_states
from ai_agent_runtime.telegram_upstream.topic_config import (
    read_topic_config,
    reconcile_managed_topic,
    reconcile_managed_topics,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_unbound_topic_discovery_uses_only_named_upstream_entries(tmp_path: Path) -> None:
    path = tmp_path / "topic_config.json"
    _write_json(
        path,
        {
            "topics": {
                "1": {"name": "General"},
                "2": {"engine": "codex"},
                "42": {"name": " Already bound "},
                "151": {"name": " CUS Auto QA 2 ", "cwd": "/tmp/ignored"},
                "0152": {"name": "Noncanonical"},
                "153": {"name": " "},
                "154": "malformed",
                "9" * 100: {"name": "Oversized key"},
            }
        },
    )
    assert read_topic_config(path).unbound_named_topics({42}) == [
        {"thread_id": 151, "topic_name": "CUS Auto QA 2"}
    ]


def test_tmux_name_matches_pinned_upstream_algorithm() -> None:
    adapter = TelegramUpstreamAdapter("0" * 40)
    assert adapter.expected_session_name(chat_id=-100123, thread_id=44) == "cc-n100123-44"
    assert adapter.expected_session_name(chat_id=123, thread_id=None) == "cc-123-0"


def test_live_resume_switch_name_requires_resume_and_exact_base() -> None:
    observed = "cc-n1001-42-switch-18db9e8bb41cdf5b"
    assert matches_managed_tmux_name(
        expected="cc-n1001-42", observed=observed, resume_requested=True
    )
    assert not matches_managed_tmux_name(
        expected="cc-n1001-42", observed=observed, resume_requested=False
    )
    assert not matches_managed_tmux_name(
        expected="cc-n1001-43", observed=observed, resume_requested=True
    )
    assert not matches_managed_tmux_name(
        expected="cc-n1001-42",
        observed="cc-n1001-42-switch-nothex",
        resume_requested=True,
    )


def test_phase_a_allows_qualified_live_resume_switch_only_with_resume_id(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    topic_path = tmp_path / "topic_config.json"
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
        tmux_session="cc-n1001-42-switch-18db9e8bb41cdf5b",
        tmux_pane="%1",
        cwd=str(cwd),
    )
    with pytest.raises(RuntimeFailureError) as denied:
        phase_a_correlate(identity=identity, bindings=(binding,), topic_config=snapshot)
    assert denied.value.code is ErrorCode.UNKNOWN_BINDING
    assert (
        phase_a_correlate(
            identity=identity,
            bindings=(binding,),
            topic_config=snapshot,
            resume_session_id="owned-session",
        )
        == binding
    )

    prior_state = UpstreamTmuxState(
        chat_id=-1001,
        thread_id=42,
        session_name=identity.tmux_session,
        cwd=str(cwd),
        provider="codex",
        session_id="old-session",
        transcript_path=None,
    )
    assert (
        phase_a_correlate(
            identity=identity,
            bindings=(binding,),
            topic_config=snapshot,
            states=(prior_state,),
        )
        == binding
    )
    with pytest.raises(RuntimeFailureError) as foreign_state:
        phase_a_correlate(
            identity=identity,
            bindings=(binding,),
            topic_config=snapshot,
            states=(
                prior_state.__class__(
                    chat_id=-1001,
                    thread_id=43,
                    session_name=identity.tmux_session,
                    cwd=str(cwd),
                    provider="codex",
                    session_id="old-session",
                    transcript_path=None,
                ),
            ),
        )
    assert foreign_state.value.code is ErrorCode.UNKNOWN_BINDING
    with pytest.raises(RuntimeFailureError) as ambiguous_state:
        phase_a_correlate(
            identity=identity,
            bindings=(binding,),
            topic_config=snapshot,
            states=(prior_state, prior_state),
        )
    assert ambiguous_state.value.code is ErrorCode.UNKNOWN_BINDING

    with pytest.raises(RuntimeFailureError) as stale_phase_b:
        phase_b_verify(
            binding=binding,
            identity=identity,
            states=(prior_state,),
            topic_config=snapshot,
            replaced_session_id="old-session",
        )
    assert stale_phase_b.value.code is ErrorCode.CORRELATION_MISMATCH
    fresh_state = UpstreamTmuxState(
        chat_id=-1001,
        thread_id=42,
        session_name=identity.tmux_session,
        cwd=str(cwd),
        provider="codex",
        session_id=None,
        transcript_path=None,
    )
    assert (
        phase_b_verify(
            binding=binding,
            identity=identity,
            states=(fresh_state,),
            topic_config=snapshot,
            replaced_session_id="old-session",
        )
        == fresh_state
    )


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


def test_concurrent_upstream_create_is_preserved_after_runtime_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "topic_config.json"
    _write_json(path, {"topics": {"42": {"name": "Existing", "cwd": None}}})
    before = read_topic_config(path)
    real_exchange = topic_config_adapter._exchange_paths
    injected = False

    def exchange(source: Path, target: Path) -> None:
        nonlocal injected
        if not injected:
            injected = True
            upstream = json.loads(path.read_text(encoding="utf-8"))
            upstream["topics"]["43"] = {"name": "New", "cwd": None}
            replacement = tmp_path / "upstream.tmp"
            _write_json(replacement, upstream)
            replacement.replace(path)
        real_exchange(source, target)

    monkeypatch.setattr(topic_config_adapter, "_exchange_paths", exchange)
    after = reconcile_managed_topic(
        path,
        thread_id=42,
        cwd=str(tmp_path),
        expected_digest=before.digest,
        backup_dir=tmp_path / "backups",
    )
    assert injected
    assert after.topic(42).is_managed_candidate  # type: ignore[union-attr]
    assert after.topic(43).raw["name"] == "New"  # type: ignore[union-attr]
    assert len(list((tmp_path / "backups").iterdir())) == 2


def test_concurrent_upstream_rename_is_preserved_after_runtime_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "topic_config.json"
    _write_json(path, {"topics": {"42": {"name": "Old", "cwd": None}}})
    before = read_topic_config(path)
    real_exchange = topic_config_adapter._exchange_paths
    injected = False

    def exchange(source: Path, target: Path) -> None:
        nonlocal injected
        if not injected:
            injected = True
            upstream = json.loads(path.read_text(encoding="utf-8"))
            upstream["topics"]["42"]["name"] = "Renamed"
            replacement = tmp_path / "upstream.tmp"
            _write_json(replacement, upstream)
            replacement.replace(path)
        real_exchange(source, target)

    monkeypatch.setattr(topic_config_adapter, "_exchange_paths", exchange)
    after = reconcile_managed_topic(
        path,
        thread_id=42,
        cwd=str(tmp_path),
        expected_digest=before.digest,
        backup_dir=tmp_path / "backups",
    )
    assert after.topic(42).raw["name"] == "Renamed"  # type: ignore[union-attr]
    assert after.topic(42).is_managed_candidate  # type: ignore[union-attr]


def test_concurrent_engine_change_is_restored_and_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "topic_config.json"
    _write_json(
        path,
        {
            "topics": {
                "42": {
                    "name": "Existing",
                    "cwd": str(tmp_path),
                    "engine": "codex",
                    "exec_mode": "tmux",
                }
            }
        },
    )
    before = read_topic_config(path)
    real_exchange = topic_config_adapter._exchange_paths
    injected = False

    def exchange(source: Path, target: Path) -> None:
        nonlocal injected
        if not injected:
            injected = True
            upstream = json.loads(path.read_text(encoding="utf-8"))
            upstream["topics"]["42"]["engine"] = "claude"
            replacement = tmp_path / "upstream.tmp"
            _write_json(replacement, upstream)
            replacement.replace(path)
        real_exchange(source, target)

    monkeypatch.setattr(topic_config_adapter, "_exchange_paths", exchange)
    with pytest.raises(RuntimeError, match="managed fields changed"):
        reconcile_managed_topic(
            path,
            thread_id=42,
            cwd=str(tmp_path),
            expected_digest=before.digest,
            backup_dir=tmp_path / "backups",
        )
    assert read_topic_config(path).topic(42).engine == "claude"  # type: ignore[union-attr]


def test_concurrent_upstream_delete_is_restored_and_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "topic_config.json"
    _write_json(
        path,
        {"topics": {"42": {"cwd": str(tmp_path), "engine": "codex", "exec_mode": "tmux"}}},
    )
    before = read_topic_config(path)
    real_exchange = topic_config_adapter._exchange_paths
    injected = False

    def exchange(source: Path, target: Path) -> None:
        nonlocal injected
        if not injected:
            injected = True
            replacement = tmp_path / "upstream.tmp"
            _write_json(replacement, {"topics": {}})
            replacement.replace(path)
        real_exchange(source, target)

    monkeypatch.setattr(topic_config_adapter, "_exchange_paths", exchange)
    with pytest.raises(RuntimeError, match="managed fields changed"):
        reconcile_managed_topic(
            path,
            thread_id=42,
            cwd=str(tmp_path),
            expected_digest=before.digest,
            backup_dir=tmp_path / "backups",
        )
    assert read_topic_config(path).topic(42) is None


def test_two_stale_upstream_creates_are_both_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "topic_config.json"
    original = {"topics": {"42": {"name": "Existing", "cwd": None}}}
    _write_json(path, original)
    before = read_topic_config(path)
    real_exchange = topic_config_adapter._exchange_paths
    exchanges = 0

    def exchange(source: Path, target: Path) -> None:
        nonlocal exchanges
        exchanges += 1
        if exchanges in (1, 2):
            # The second upstream writer loaded its stale snapshot before the
            # first create, so its atomic replace does not include topic 43.
            concurrent = json.loads(json.dumps(original))
            thread = "43" if exchanges == 1 else "44"
            concurrent["topics"][thread] = {"name": f"New {thread}", "cwd": None}
            replacement = tmp_path / f"upstream-{exchanges}.tmp"
            _write_json(replacement, concurrent)
            replacement.replace(path)
        real_exchange(source, target)

    monkeypatch.setattr(topic_config_adapter, "_exchange_paths", exchange)
    after = reconcile_managed_topic(
        path,
        thread_id=42,
        cwd=str(tmp_path),
        expected_digest=before.digest,
        backup_dir=tmp_path / "backups",
    )
    assert exchanges == 3
    assert after.topic(43).raw["name"] == "New 43"  # type: ignore[union-attr]
    assert after.topic(44).raw["name"] == "New 44"  # type: ignore[union-attr]


def test_exhausted_concurrent_writes_fail_with_recoverable_upstream_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "topic_config.json"
    _write_json(path, {"topics": {"42": {"name": "Existing", "cwd": None}}})
    real_exchange = topic_config_adapter._exchange_paths
    exchanges = 0

    def exchange(source: Path, target: Path) -> None:
        nonlocal exchanges
        exchanges += 1
        upstream = json.loads(path.read_text(encoding="utf-8"))
        upstream["topics"][str(42 + exchanges)] = {
            "name": f"New {42 + exchanges}", "cwd": None
        }
        replacement = tmp_path / f"upstream-{exchanges}.tmp"
        _write_json(replacement, upstream)
        replacement.replace(path)
        real_exchange(source, target)

    monkeypatch.setattr(topic_config_adapter, "_exchange_paths", exchange)
    with pytest.raises(
        topic_config_adapter.TopicConfigUnresolvedConflictError,
        match="recover upstream changes from",
    ) as failure:
        reconcile_managed_topic(
            path, thread_id=42, cwd=str(tmp_path), expected_digest=None,
            backup_dir=tmp_path / "backups",
        )
    assert exchanges == topic_config_adapter._MAX_CONCURRENT_WRITES
    recovery_path = Path(str(failure.value).split("recover upstream changes from ", 1)[1])
    assert recovery_path.is_file()
    assert json.loads(recovery_path.read_text(encoding="utf-8"))["topics"]["46"]["name"] == "New 46"


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

    _write_json(
        topic_path,
        {
            "topics": {
                "42": {
                    "cwd": str(cwd),
                    "engine": "codex",
                    "exec_mode": "tmux",
                    "name": "Renamed while Codex started",
                }
            }
        },
    )
    with pytest.raises(RuntimeFailureError) as changed_config:
        phase_b_verify(
            binding=binding,
            identity=identity,
            states=read_tmux_states(state_path),
            topic_config=read_topic_config(topic_path),
        )
    assert changed_config.value.code is ErrorCode.CONFIG_DRIFT


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
