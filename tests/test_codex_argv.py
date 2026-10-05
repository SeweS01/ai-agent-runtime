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
        (
            ["resume", "01a10b77-3aa7-7420-8941-979e966a8ab6", "--no-alt-screen"],
            InvocationKind.RESUME,
            "01a10b77-3aa7-7420-8941-979e966a8ab6",
        ),
        (["resume", "abc", "--no-alt-screen"], InvocationKind.RESUME, None),
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


def test_resume_parser_skips_upstream_mcp_config_before_session_id() -> None:
    session_id = "01a10b77-3aa7-7420-8941-979e966a8ab6"
    argv = [
        "resume",
        "-c",
        "mcp_servers.example.enabled=false",
        "-c",
        'mcp_servers.example.command="/usr/bin/python3"',
        "-c",
        'mcp_servers.example.args=["-m", "example"]',
        session_id,
        "--no-alt-screen",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        "/srv/project",
    ]
    parsed = parse_invocation(argv)
    assert parsed.kind is InvocationKind.RESUME
    assert parsed.resume_session_id == session_id
    assert parsed.argv == tuple(argv)


def test_resume_parser_rejects_ambiguous_session_ids() -> None:
    first = "01a10b77-3aa7-7420-8941-979e966a8ab6"
    second = "01a10bc8-da6c-7380-b05e-0f5fc98da5de"
    assert parse_invocation(["resume", first, second]).resume_session_id is None


def test_rewrite_rejects_missing_managed_value() -> None:
    with pytest.raises(ValueError, match="requires"):
        rewrite_managed_args(["--model"], model="new", reasoning_effort="high")
