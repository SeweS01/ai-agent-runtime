"""Fresh-read, eventual-reconciliation adapter for upstream topic_config.json."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _digest_bytes(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


@dataclass(frozen=True, slots=True)
class TopicConfigEntry:
    thread_id: int
    cwd: str | None
    engine: str
    exec_mode: str
    raw: dict[str, Any]

    @property
    def is_managed_candidate(self) -> bool:
        return self.engine == "codex" and self.exec_mode == "tmux"


@dataclass(frozen=True, slots=True)
class TopicConfigSnapshot:
    path: Path
    digest: str
    raw: dict[str, Any]

    def topic(self, thread_id: int) -> TopicConfigEntry | None:
        topics = self.raw.get("topics")
        if not isinstance(topics, dict):
            return None
        value = topics.get(str(thread_id))
        if not isinstance(value, dict):
            return None
        cwd_value = value.get("cwd")
        return TopicConfigEntry(
            thread_id=thread_id,
            cwd=str(cwd_value) if isinstance(cwd_value, str) else None,
            engine=str(value.get("engine", "claude")),
            exec_mode=str(value.get("exec_mode", "subprocess")),
            raw=dict(value),
        )


def read_topic_config(path: Path) -> TopicConfigSnapshot:
    raw_bytes = path.read_bytes()
    parsed = json.loads(raw_bytes)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("topics"), dict):
        raise ValueError("topic_config must contain an object-valued topics field")
    return TopicConfigSnapshot(path=path, digest=_digest_bytes(raw_bytes), raw=parsed)


def reconcile_managed_topic(
    path: Path,
    *,
    thread_id: int,
    cwd: str,
    expected_digest: str | None,
    backup_dir: Path,
) -> TopicConfigSnapshot:
    """Patch allowlisted fields without claiming cross-process serializability.

    The upstream-created thread key must already exist. A caller should retain
    the returned digest and keep watching for a later lost update.
    """

    before = read_topic_config(path)
    if expected_digest is not None and before.digest != expected_digest:
        raise RuntimeError("topic_config changed before reconciliation")
    topics = before.raw["topics"]
    assert isinstance(topics, dict)
    current = topics.get(str(thread_id))
    if not isinstance(current, dict):
        raise KeyError(f"upstream topic key does not exist: {thread_id}")

    updated_entry = dict(current)
    updated_entry.update({"cwd": cwd, "engine": "codex", "exec_mode": "tmux"})
    updated_root = dict(before.raw)
    updated_topics = dict(topics)
    updated_topics[str(thread_id)] = updated_entry
    updated_root["topics"] = updated_topics

    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / f"topic-config-{time.time_ns()}-{before.digest[7:19]}.json"
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)

    encoded = (json.dumps(updated_root, ensure_ascii=False, indent=2) + "\n").encode()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp_path.unlink(missing_ok=True)

    after = read_topic_config(path)
    actual = after.topic(thread_id)
    if actual is None or actual.cwd != cwd or not actual.is_managed_candidate:
        raise RuntimeError("topic_config post-write verification failed")
    return after
