"""Read-only parser for pinned upstream tmux state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class UpstreamTmuxState:
    chat_id: int
    thread_id: int | None
    session_name: str
    cwd: str
    provider: str
    session_id: str | None
    transcript_path: str | None


def read_tmux_states(path: Path) -> tuple[UpstreamTmuxState, ...]:
    if not path.exists():
        return ()
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("tmux state must be an object")
    states: list[UpstreamTmuxState] = []
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        chat_raw, separator, thread_raw = key.partition(":")
        if not separator:
            continue
        try:
            chat_id = int(chat_raw)
            thread_id = None if thread_raw == "None" else int(thread_raw)
        except ValueError:
            continue
        session_name = value.get("session_name")
        cwd = value.get("cwd")
        if not isinstance(session_name, str) or not isinstance(cwd, str):
            continue
        provider = value.get("provider", "claude")
        session_id = value.get("session_id")
        transcript_path = value.get("transcript_path")
        states.append(
            UpstreamTmuxState(
                chat_id=chat_id,
                thread_id=thread_id,
                session_name=session_name,
                cwd=cwd,
                provider=str(provider),
                session_id=session_id if isinstance(session_id, str) else None,
                transcript_path=(transcript_path if isinstance(transcript_path, str) else None),
            )
        )
    return tuple(states)
