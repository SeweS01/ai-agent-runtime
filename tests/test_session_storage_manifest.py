from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_runtime.migration.inventory import InventoryError, write_json_artifact
from ai_agent_runtime.migration.session_storage import build_session_storage_manifest


def _profile(root: Path, name: str, *, session_name: str, content: str) -> Path:
    profile = root / name
    session = profile / "sessions" / "2026" / "10"
    session.mkdir(parents=True)
    (session / session_name).write_text(content, encoding="utf-8")
    archived = profile / "archived_sessions"
    archived.mkdir()
    (profile / "auth.json").write_text("secret-must-not-be-read", encoding="utf-8")
    return profile


def test_manifest_records_counts_digests_and_rollback_without_secrets(tmp_path: Path) -> None:
    first = _profile(tmp_path, "profile-a", session_name="a.jsonl", content="a")
    second = _profile(tmp_path, "profile-b", session_name="b.jsonl", content="bb")
    (first / "index.sqlite").write_bytes(b"index")
    manifest = build_session_storage_manifest(
        profiles={"account-a": first, "account-b": second},
        shared_root=tmp_path / "shared",
        includes=("sessions", "archived_sessions", "index.sqlite"),
    )
    assert manifest["status"] == "READY_FOR_QUALIFICATION"
    assert manifest["qualified"] is False
    assert manifest["collisions"] == []
    operations = manifest["operations"]
    assert isinstance(operations, list) and len(operations) == 6
    assert all(operation["collision_policy"] == "BLOCK" for operation in operations)
    assert all(operation["rollback"]["never_delete_new_target_files"] for operation in operations)
    assert "secret-must-not-be-read" not in str(manifest)
    assert "auth.json" not in str(manifest)
    output = tmp_path / "artifacts" / "storage.json"
    write_json_artifact(output, manifest)
    assert output.stat().st_mode & 0o777 == 0o600


def test_manifest_blocks_cross_profile_and_existing_target_collisions(tmp_path: Path) -> None:
    first = _profile(tmp_path, "profile-a", session_name="same.jsonl", content="a")
    second = _profile(tmp_path, "profile-b", session_name="same.jsonl", content="b")
    shared = tmp_path / "shared"
    target = shared / "sessions" / "2026" / "10"
    target.mkdir(parents=True)
    (target / "same.jsonl").write_text("existing", encoding="utf-8")
    manifest = build_session_storage_manifest(
        profiles={"account-a": first, "account-b": second},
        shared_root=shared,
    )
    assert manifest["status"] == "BLOCKED_COLLISIONS"
    collisions = manifest["collisions"]
    assert isinstance(collisions, list) and len(collisions) == 1
    assert {source["owner"] for source in collisions[0]["sources"]} == {
        "account-a",
        "account-b",
        "existing-target",
    }


def test_manifest_rejects_secret_include_and_never_follows_session_symlink(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "auth.json").write_text("secret", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("must-not-be-hashed", encoding="utf-8")
    (profile / "sessions").symlink_to(outside, target_is_directory=True)
    manifest = build_session_storage_manifest(
        profiles={"account": profile},
        shared_root=tmp_path / "shared",
    )
    profiles = manifest["profiles"]
    assert isinstance(profiles, list)
    session_scan = profiles[0]["includes"][0]
    assert session_scan["source_type"] == "symlink"
    assert session_scan["file_count"] == 0
    assert "must-not-be-hashed" not in str(manifest)
    with pytest.raises(InventoryError, match="credential material"):
        build_session_storage_manifest(
            profiles={"account": profile},
            shared_root=tmp_path / "shared",
            includes=("auth.json",),
        )
