"""Provider-neutral domain records and state axes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class LifecycleState(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    CONFIGURED_STOPPED = "CONFIGURED_STOPPED"
    PROVISIONAL_STARTING = "PROVISIONAL_STARTING"
    IDLE = "IDLE"
    BUSY = "BUSY"
    PENDING_CHANGE = "PENDING_CHANGE"
    RETIRED_FOR_RESTART = "RETIRED_FOR_RESTART"
    START_FAILED = "START_FAILED"


class ManagementMode(StrEnum):
    MANAGED = "MANAGED"
    UNMANAGED = "UNMANAGED"
    UNSUPPORTED_CODEX_SUBPROCESS = "UNSUPPORTED_CODEX_SUBPROCESS"


class SyncState(StrEnum):
    IN_SYNC = "IN_SYNC"
    DEGRADED_CUS_SYNC = "DEGRADED_CUS_SYNC"
    OBSERVED_PENDING_SYNC = "OBSERVED_PENDING_SYNC"
    OBSERVED_POLICY_REJECTED = "OBSERVED_POLICY_REJECTED"
    OUT_OF_SYNC = "OUT_OF_SYNC"


class CorrelationState(StrEnum):
    UNKNOWN = "UNKNOWN"
    PROVISIONAL = "PROVISIONAL"
    VERIFIED = "VERIFIED"
    AMBIGUOUS = "AMBIGUOUS"
    MISMATCH = "MISMATCH"


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_digest(value: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(value).encode()).hexdigest()}"


def canonical_cwd(path: str) -> str:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("cwd must be absolute")
    return str(candidate.resolve(strict=True))


@dataclass(frozen=True, slots=True)
class RevisionKey:
    generation_id: str
    revision: int
    spec_digest: str

    def __post_init__(self) -> None:
        if not self.generation_id:
            raise ValueError("generation_id is required")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if not self.spec_digest.startswith("sha256:"):
            raise ValueError("spec_digest must be sha256-prefixed")


@dataclass(frozen=True, slots=True)
class DesiredSpec:
    binding_id: str
    agent_instance_id: str
    project_id: str
    canonical_cwd: str
    provider: str
    account_profile: str
    model: str
    reasoning_effort: str
    generation_id: str
    revision: int
    spec_digest: str

    @classmethod
    def build(
        cls,
        *,
        binding_id: str,
        agent_instance_id: str,
        project_id: str,
        canonical_cwd: str,
        provider: str,
        account_profile: str,
        model: str,
        reasoning_effort: str,
        generation_id: str,
        revision: int,
    ) -> DesiredSpec:
        payload: dict[str, Any] = {
            "account_profile": account_profile,
            "agent_instance_id": agent_instance_id,
            "binding_id": binding_id,
            "canonical_cwd": canonical_cwd,
            "generation_id": generation_id,
            "model": model,
            "project_id": project_id,
            "provider": provider,
            "reasoning_effort": reasoning_effort,
            "revision": revision,
        }
        return cls(
            binding_id=binding_id,
            agent_instance_id=agent_instance_id,
            project_id=project_id,
            canonical_cwd=canonical_cwd,
            provider=provider,
            account_profile=account_profile,
            model=model,
            reasoning_effort=reasoning_effort,
            generation_id=generation_id,
            revision=revision,
            spec_digest=sha256_digest(payload),
        )

    @property
    def revision_key(self) -> RevisionKey:
        return RevisionKey(self.generation_id, self.revision, self.spec_digest)

    def verify_digest(self) -> bool:
        payload = {
            "account_profile": self.account_profile,
            "agent_instance_id": self.agent_instance_id,
            "binding_id": self.binding_id,
            "canonical_cwd": self.canonical_cwd,
            "generation_id": self.generation_id,
            "model": self.model,
            "project_id": self.project_id,
            "provider": self.provider,
            "reasoning_effort": self.reasoning_effort,
            "revision": self.revision,
        }
        return self.spec_digest == sha256_digest(payload)


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    binding_id: str
    agent_instance_id: str
    chat_id: int
    thread_id: int
    project_id: str
    canonical_cwd: str
    expected_tmux_session: str
    configured: bool = True
    config_digest: str | None = None
    management_mode: ManagementMode = ManagementMode.MANAGED


@dataclass(frozen=True, slots=True)
class WrapperIdentity:
    pid: int
    starttime: int
    uid: int
    tmux_session: str
    tmux_pane: str
    cwd: str


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    binding_id: str
    revision: RevisionKey
    codex_home: str
    account_profile: str
    model: str
    reasoning_effort: str
    real_codex: str


@dataclass(frozen=True, slots=True)
class ObservedSpecProposal:
    binding_id: str
    base_generation_id: str
    base_revision: int
    base_spec_digest: str
    model: str
    reasoning_effort: str
    instance_id: str
    observation_sequence: int
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.base_revision < 0 or self.observation_sequence < 0:
            raise ValueError("proposal revisions and sequence must be non-negative")
        if not self.base_spec_digest.startswith("sha256:"):
            raise ValueError("base spec digest must be sha256-prefixed")
        if not self.evidence_digest.startswith("sha256:"):
            raise ValueError("evidence digest must be sha256-prefixed")

    def payload(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "base_generation_id": self.base_generation_id,
            "base_revision": self.base_revision,
            "base_spec_digest": self.base_spec_digest,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "instance_id": self.instance_id,
            "observation_sequence": self.observation_sequence,
            "evidence_digest": self.evidence_digest,
        }

    @property
    def event_id(self) -> str:
        identity = {
            "binding_id": self.binding_id,
            "instance_id": self.instance_id,
            "observation_sequence": self.observation_sequence,
            "evidence_digest": self.evidence_digest,
        }
        return f"observed:{sha256_digest(identity)[7:]}"
