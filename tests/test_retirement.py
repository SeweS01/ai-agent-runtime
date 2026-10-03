import os
from dataclasses import replace
from pathlib import Path

import pytest

from ai_agent_runtime.config import RuntimeConfig
from ai_agent_runtime.daemon.server import RuntimeServer
from ai_agent_runtime.domain import RetirementCandidate, RuntimeBinding, WrapperIdentity
from ai_agent_runtime.telegram_upstream.topic_config import TopicConfigSnapshot


@pytest.mark.asyncio
async def test_retirement_loop_uses_persisted_exact_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identities: list[WrapperIdentity] = []
    retired: list[RetirementCandidate] = []
    topic_config = tmp_path / "topic_config.json"
    topic_config.write_text("{}", encoding="utf-8")
    config = replace(
        RuntimeConfig.defaults(),
        database_path=tmp_path / "runtime.sqlite",
        topic_config_path=topic_config,
        retirement_enabled=True,
        retirement_qualification_report_id="qualification-1",
    )
    daemon = RuntimeServer(config, retirement_executor=identities.append)
    candidate = RetirementCandidate(
        instance_id="instance-1",
        binding_id="binding-1",
        wrapper_pid=123,
        wrapper_starttime=456,
        tmux_session="cc-n1001-42",
        tmux_pane="%7",
        canonical_cwd="/srv/project",
        applied_spec_digest="sha256:old",
        desired_spec_digest="sha256:new",
    )
    monkeypatch.setattr(daemon.store, "retirement_candidates", lambda: (candidate,))
    monkeypatch.setattr(
        daemon.store,
        "get_binding",
        lambda _binding_id: RuntimeBinding(
            binding_id="binding-1",
            agent_instance_id="agent-1",
            chat_id=-1001,
            thread_id=42,
            project_id="project-1",
            canonical_cwd="/srv/project",
            expected_tmux_session="cc-n1001-42",
            config_digest="sha256:config",
        ),
    )
    monkeypatch.setattr(
        "ai_agent_runtime.daemon.server.read_topic_config",
        lambda _path: TopicConfigSnapshot(
            path=topic_config,
            raw={
                "topics": {
                    "42": {
                        "cwd": "/srv/project",
                        "engine": "codex",
                        "exec_mode": "tmux",
                    }
                }
            },
            digest="sha256:config",
        ),
    )
    monkeypatch.setattr(daemon.store, "mark_instance_retired", retired.append)

    await daemon._retire_pending_once()

    assert identities == [
        WrapperIdentity(
            pid=123,
            starttime=456,
            uid=os.getuid(),
            tmux_session="cc-n1001-42",
            tmux_pane="%7",
            cwd="/srv/project",
        )
    ]
    assert retired == [candidate]
    daemon.store.close()
