"""Runtime configuration loaded from a non-secret TOML file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _xdg_state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    return Path(configured) if configured else Path.home() / ".local" / "state"


def _xdg_runtime_dir() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured)
    return Path("/run/user") / str(os.getuid())


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    agent_instance_id: str
    forum_chat_id: int
    state_dir: Path
    socket_path: Path
    database_path: Path
    real_codex: Path
    real_codex_version: str
    real_codex_sha256: str
    profiles_root: Path
    topic_config_path: Path
    tmux_state_path: Path
    upstream_sha: str
    tmux_session_prefix: str = "cc-"
    allowed_project_roots: tuple[Path, ...] = ()

    @classmethod
    def defaults(cls) -> RuntimeConfig:
        state_dir = _xdg_state_home() / "ai-agent-runtime"
        return cls(
            agent_instance_id="",
            forum_chat_id=0,
            state_dir=state_dir,
            socket_path=_xdg_runtime_dir() / "ai-agent-runtime" / "runtime.sock",
            database_path=state_dir / "runtime.sqlite",
            real_codex=Path.home() / ".local" / "bin" / "codex-real",
            real_codex_version="",
            real_codex_sha256="",
            profiles_root=Path.home() / ".local" / "share" / "ai-agent-runtime" / "profiles",
            topic_config_path=Path.home() / "apps" / "telegram-ai-agent" / "topic_config.json",
            tmux_state_path=Path.home()
            / "apps"
            / "telegram-ai-agent"
            / "tmux_sessions"
            / "state.json",
            upstream_sha="",
            allowed_project_roots=(),
        )

    @classmethod
    def load(cls, path: Path) -> RuntimeConfig:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        defaults = cls.defaults()
        runtime = _table(raw, "runtime")
        telegram = _table(raw, "telegram_agent")
        codex = _table(raw, "codex")
        policy = _table(raw, "policy")
        state_dir = Path(runtime.get("state_dir", defaults.state_dir)).expanduser()
        raw_roots = policy.get("allowed_project_roots", [])
        if not isinstance(raw_roots, list) or not all(isinstance(item, str) for item in raw_roots):
            raise ValueError("policy.allowed_project_roots must be a list of paths")
        return cls(
            agent_instance_id=str(runtime.get("agent_instance_id", "")),
            forum_chat_id=int(runtime.get("forum_chat_id", 0)),
            state_dir=state_dir,
            socket_path=Path(runtime.get("socket", defaults.socket_path)).expanduser(),
            database_path=Path(runtime.get("database", state_dir / "runtime.sqlite")).expanduser(),
            real_codex=Path(codex.get("real_binary", defaults.real_codex)).expanduser(),
            real_codex_version=str(codex.get("version", "")),
            real_codex_sha256=str(codex.get("sha256", "")),
            profiles_root=Path(codex.get("profiles_root", defaults.profiles_root)).expanduser(),
            topic_config_path=Path(
                telegram.get("topic_config", defaults.topic_config_path)
            ).expanduser(),
            tmux_state_path=Path(telegram.get("tmux_state", defaults.tmux_state_path)).expanduser(),
            upstream_sha=str(telegram.get("upstream_sha", "")),
            tmux_session_prefix=str(telegram.get("tmux_session_prefix", "cc-")),
            allowed_project_roots=tuple(Path(item).expanduser() for item in raw_roots),
        )

    def validate(self) -> None:
        if not self.agent_instance_id:
            raise ValueError("runtime.agent_instance_id is required")
        if self.forum_chat_id >= 0:
            raise ValueError("runtime.forum_chat_id must be a Telegram supergroup id")
        if len(self.upstream_sha) != 40 or any(
            ch not in "0123456789abcdef" for ch in self.upstream_sha
        ):
            raise ValueError("telegram_agent.upstream_sha must be a lowercase 40-char commit SHA")
        for name, value in {
            "real_codex": self.real_codex,
            "profiles_root": self.profiles_root,
            "topic_config_path": self.topic_config_path,
            "tmux_state_path": self.tmux_state_path,
        }.items():
            if not value.is_absolute():
                raise ValueError(f"{name} must be absolute")
        if not self.allowed_project_roots:
            raise ValueError("policy.allowed_project_roots must not be empty")
        if any(not root.is_absolute() for root in self.allowed_project_roots):
            raise ValueError("every allowed project root must be absolute")
        if not self.real_codex_version or len(self.real_codex_version) > 100:
            raise ValueError("codex.version is required")
        if (
            not self.real_codex_sha256.startswith("sha256:")
            or len(self.real_codex_sha256) != 71
            or any(ch not in "0123456789abcdef" for ch in self.real_codex_sha256[7:])
        ):
            raise ValueError("codex.sha256 must be a sha256-prefixed lowercase digest")


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{key}] must be a TOML table")
    return value
