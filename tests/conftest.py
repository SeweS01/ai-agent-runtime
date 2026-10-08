import sys
from pathlib import Path

import pytest

from ai_agent_runtime import process_safety


@pytest.fixture
def allow_hosted_runner_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """Permit only this test process's Python in process-evidence tests.

    GitHub's hosted Python can be group/world writable. These tests exercise
    process identity and integration behaviour, not executable policy; the
    production validator and its dedicated rejection tests remain unchanged.
    """

    test_python = Path(sys.executable).resolve(strict=True)
    validate = process_safety.validate_owned_executable

    def validate_test_executable(path: Path, *, uid: int | None = None) -> Path:
        if path.resolve(strict=True) == test_python:
            return test_python
        return validate(path, uid=uid)

    monkeypatch.setattr(process_safety, "validate_owned_executable", validate_test_executable)
