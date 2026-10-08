"""Audited, reversible switch of one existing instance to a verified release."""

from __future__ import annotations

import fcntl
import os
import stat
import uuid
from pathlib import Path
from typing import Final

from ai_agent_runtime.deployment.install import verify_runtime_install_report
from ai_agent_runtime.deployment.prepare import _INSTANCE_ID_RE
from ai_agent_runtime.migration.inventory import InventoryError, _digest, write_json_artifact

INSTANCE_SWITCH_SCHEMA: Final = "ai-agent-runtime.instance-switch.v1"


def _owned_parent(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or path.parent == Path("/"):
        raise InventoryError("instance switch path is unsafe")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise InventoryError("instance switch parent must be an existing real directory")
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise InventoryError("instance switch parent ownership/mode is unsafe")


def _exact_link(link: Path, target: Path) -> None:
    if not link.is_symlink() or os.readlink(link) != str(target):
        raise InventoryError(f"instance entrypoint link differs from expected target: {link}")
    if link.lstat().st_uid != os.getuid():
        raise InventoryError("instance entrypoint link owner is unsafe")


def _replace_link(link: Path, target: Path) -> None:
    temporary = link.parent / f".{link.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.symlink_to(target)
        os.replace(temporary, link)
        directory_fd = os.open(link.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def switch_instance_release(
    *,
    old_install_report: Path,
    new_install_report: Path,
    agent_instance_id: str,
    instance_link: Path,
    runtime_link: Path,
    wrapper_link: Path,
    intent_path: Path,
    result_path: Path,
) -> dict[str, object]:
    """Switch only the stable release symlink; leave services and children untouched.

    An immutable intent is written before the atomic replacement. Reversing the
    old/new reports with fresh artifact paths is the rollback operation.
    """

    if not _INSTANCE_ID_RE.fullmatch(agent_instance_id):
        raise InventoryError("AgentInstance id is invalid")
    paths = (instance_link, runtime_link, wrapper_link, intent_path, result_path)
    if len(set(paths)) != len(paths):
        raise InventoryError("instance switch paths must be distinct")
    for path in paths:
        _owned_parent(path)
    if instance_link.name != agent_instance_id:
        raise InventoryError("instance link name must equal AgentInstance id")
    if runtime_link.name != f"ai-agent-runtime@{agent_instance_id}":
        raise InventoryError("Runtime entrypoint name does not match AgentInstance")
    if wrapper_link.name != f"codex-wrapper@{agent_instance_id}":
        raise InventoryError("wrapper entrypoint name does not match AgentInstance")
    if any(path.exists() or path.is_symlink() for path in (intent_path, result_path)):
        raise InventoryError("instance switch artifacts must not exist")

    old = verify_runtime_install_report(old_install_report)
    new = verify_runtime_install_report(new_install_report)
    if old.get("accepted") is not True or new.get("accepted") is not True:
        raise InventoryError("both Runtime install reports must be verified")
    old_release = Path(str(old["release_path"]))
    new_release = Path(str(new["release_path"]))
    if old_release == new_release:
        raise InventoryError("instance switch requires different releases")
    if old_release.parent != new_release.parent:
        raise InventoryError("instance switch releases must share a releases root")

    lock_path = instance_link.parent / f".{agent_instance_id}.switch.lock"
    if lock_path.is_symlink():
        raise InventoryError("instance switch lock path is a symlink")
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != os.getuid()
            or lock_info.st_mode & 0o077
        ):
            raise InventoryError("instance switch lock file is unsafe")
        _exact_link(instance_link, old_release)
        _exact_link(runtime_link, instance_link / "bin" / "ai-agent-runtime")
        _exact_link(wrapper_link, instance_link / "bin" / "codex-wrapper")
        intent: dict[str, object] = {
            "schema": INSTANCE_SWITCH_SCHEMA,
            "status": "INTENT_NOT_APPLIED",
            "agent_instance_id": agent_instance_id,
            "old_install_report": str(old_install_report.resolve(strict=True)),
            "new_install_report": str(new_install_report.resolve(strict=True)),
            "old_release": str(old_release),
            "new_release": str(new_release),
            "instance_link": str(instance_link),
            "runtime_link": str(runtime_link),
            "wrapper_link": str(wrapper_link),
            "services_restarted": False,
            "production_cutover_authorized": False,
        }
        intent["report_digest"] = _digest(intent)
        write_json_artifact(intent_path, intent)
        try:
            # Recheck after the durable intent and immediately before replacement.
            _exact_link(instance_link, old_release)
            _replace_link(instance_link, new_release)
            _exact_link(instance_link, new_release)
            if runtime_link.resolve(strict=True) != Path(str(new["runtime_executable"])):
                raise InventoryError("switched Runtime entrypoint does not match install")
            if wrapper_link.resolve(strict=True) != Path(str(new["wrapper_executable"])):
                raise InventoryError("switched wrapper entrypoint does not match install")
            result = dict(intent)
            result["status"] = "SWITCHED_NOT_RESTARTED"
            result["intent_path"] = str(intent_path)
            result["intent_digest"] = intent["report_digest"]
            result.pop("report_digest")
            result["report_digest"] = _digest(result)
            write_json_artifact(result_path, result)
            return result
        except Exception:
            # _replace_link can raise after os.replace (for example fsync failed).
            # Inspect the actual link rather than trusting its return value.
            if instance_link.is_symlink() and os.readlink(instance_link) == str(new_release):
                try:
                    _replace_link(instance_link, old_release)
                except Exception as exc:
                    raise InventoryError(
                        "instance switch rollback failed; inspect durable intent and live link"
                    ) from exc
            raise
    finally:
        os.close(lock_fd)
