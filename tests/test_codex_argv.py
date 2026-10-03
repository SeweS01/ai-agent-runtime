import pytest

from ai_agent_runtime.providers.codex.argv import (
    InvocationKind,
    parse_invocation,
    rewrite_managed_args,
)


@pytest.mark.parametrize(
    ("argv", "kind", "session_id"),
    [
        ([], InvocationKind.START, None),
        (["--version"], InvocationKind.SAFE_LOCAL, None),
        (["login"], InvocationKind.DENIED_ADMIN, None),
        (["resume", "abc", "--no-alt-screen"], InvocationKind.RESUME, "abc"),
    ],
)
def test_invocation_classification(
    argv: list[str], kind: InvocationKind, session_id: str | None
) -> None:
    parsed = parse_invocation(argv)
    assert parsed.kind is kind
    assert parsed.resume_session_id == session_id


def test_rewrite_preserves_unknown_and_unmanaged_config() -> None:
    rewritten = rewrite_managed_args(
        [
            "resume",
            "session",
            "--model=old",
            "--no-alt-screen",
            "-c",
            "mcp_servers.demo.enabled=true",
            "-c",
            "model_reasoning_effort=low",
            "--future-option",
        ],
        model="new",
        reasoning_effort="high",
    )
    assert rewritten == [
        "resume",
        "session",
        "--no-alt-screen",
        "-c",
        "mcp_servers.demo.enabled=true",
        "--future-option",
        "--model",
        "new",
        "-c",
        "model_reasoning_effort=high",
    ]


def test_rewrite_rejects_missing_managed_value() -> None:
    with pytest.raises(ValueError, match="requires"):
        rewrite_managed_args(["--model"], model="new", reasoning_effort="high")
