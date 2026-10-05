"""Two-phase binding correlation."""

from __future__ import annotations

from pathlib import Path

from ai_agent_runtime.domain import RuntimeBinding, WrapperIdentity
from ai_agent_runtime.errors import ErrorCode, RuntimeFailureError

from .adapter import matches_managed_tmux_name
from .tmux_state import UpstreamTmuxState
from .topic_config import TopicConfigSnapshot


def phase_a_identity_candidates(
    *,
    identity: WrapperIdentity,
    bindings: tuple[RuntimeBinding, ...],
    resume_session_id: str | None = None,
) -> tuple[RuntimeBinding, ...]:
    return tuple(
        binding
        for binding in bindings
        if matches_managed_tmux_name(
            expected=binding.expected_tmux_session,
            observed=identity.tmux_session,
            resume_requested=resume_session_id is not None,
        )
        and Path(binding.canonical_cwd).resolve() == Path(identity.cwd).resolve()
    )


def phase_a_correlate(
    *,
    identity: WrapperIdentity,
    bindings: tuple[RuntimeBinding, ...],
    topic_config: TopicConfigSnapshot,
    resume_session_id: str | None = None,
) -> RuntimeBinding:
    candidates = phase_a_identity_candidates(
        identity=identity,
        bindings=bindings,
        resume_session_id=resume_session_id,
    )
    if not candidates:
        raise RuntimeFailureError(
            ErrorCode.UNKNOWN_BINDING, "no binding matches tmux identity and cwd"
        )
    if len(candidates) != 1:
        raise RuntimeFailureError(
            ErrorCode.AMBIGUOUS, "multiple bindings match tmux identity and cwd"
        )
    binding = candidates[0]
    if binding.management_mode.value == "CONFIG_DRIFT":
        raise RuntimeFailureError(ErrorCode.CONFIG_DRIFT, "binding config drift is unresolved")
    if binding.management_mode.value != "MANAGED":
        raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "binding is currently unmanaged")
    if binding.config_digest != topic_config.digest:
        raise RuntimeFailureError(
            ErrorCode.CONFIG_DRIFT,
            "fresh topic_config digest has not been reconciled",
        )
    entry = topic_config.topic(binding.thread_id)
    if entry is None:
        raise RuntimeFailureError(ErrorCode.CONFIG_DRIFT, "upstream topic key is absent")
    if entry.engine != "codex":
        raise RuntimeFailureError(ErrorCode.UNKNOWN_BINDING, "binding is currently unmanaged")
    if entry.exec_mode != "tmux":
        raise RuntimeFailureError(
            ErrorCode.UNSUPPORTED_CODEX_SUBPROCESS,
            "Codex subprocess is unsupported in Runtime V1",
        )
    if entry.cwd is None or Path(entry.cwd).resolve() != Path(binding.canonical_cwd).resolve():
        raise RuntimeFailureError(ErrorCode.CONFIG_DRIFT, "topic cwd differs from Runtime Binding")
    return binding


def phase_b_verify(
    *,
    binding: RuntimeBinding,
    identity: WrapperIdentity,
    states: tuple[UpstreamTmuxState, ...],
    topic_config: TopicConfigSnapshot,
) -> UpstreamTmuxState:
    candidates = tuple(
        state
        for state in states
        if state.chat_id == binding.chat_id
        and state.thread_id == binding.thread_id
        and state.session_name == identity.tmux_session
        and state.provider == "codex"
        and Path(state.cwd).resolve() == Path(binding.canonical_cwd).resolve()
    )
    if len(candidates) != 1:
        raise RuntimeFailureError(
            ErrorCode.CORRELATION_MISMATCH,
            f"expected one upstream state entry, found {len(candidates)}",
        )
    entry = topic_config.topic(binding.thread_id)
    if entry is None or not entry.is_managed_candidate:
        raise RuntimeFailureError(ErrorCode.CONFIG_DRIFT, "topic stopped being codex+tmux")
    if entry.cwd is None or Path(entry.cwd).resolve() != Path(binding.canonical_cwd).resolve():
        raise RuntimeFailureError(ErrorCode.CONFIG_DRIFT, "topic cwd changed during startup")
    return candidates[0]
