"""Codex CLI classification and managed argument rewriting."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class InvocationKind(StrEnum):
    START = "start"
    RESUME = "resume"
    SAFE_LOCAL = "safe_local"
    DENIED_ADMIN = "denied_admin"


_SAFE_FLAGS = {"--help", "-h", "--version", "-V"}
_DENIED_COMMANDS = {"login", "logout", "update"}
_REASONING_KEYS = {"model_reasoning_effort", "model_reasoning_summary"}


@dataclass(frozen=True, slots=True)
class ParsedInvocation:
    kind: InvocationKind
    argv: tuple[str, ...]
    resume_session_id: str | None = None


def parse_invocation(argv: list[str]) -> ParsedInvocation:
    if argv and argv[0] in _SAFE_FLAGS:
        return ParsedInvocation(InvocationKind.SAFE_LOCAL, tuple(argv))
    if argv and argv[0] in _DENIED_COMMANDS:
        return ParsedInvocation(InvocationKind.DENIED_ADMIN, tuple(argv))
    if argv and argv[0] == "resume":
        session_id = next((arg for arg in argv[1:] if not arg.startswith("-")), None)
        return ParsedInvocation(InvocationKind.RESUME, tuple(argv), session_id)
    return ParsedInvocation(InvocationKind.START, tuple(argv))


def rewrite_managed_args(argv: list[str], *, model: str, reasoning_effort: str) -> list[str]:
    """Replace only Runtime-owned model/reasoning arguments.

    Unknown options and unrelated `-c key=value` values retain their exact order.
    """

    rewritten: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--model":
            if index + 1 >= len(argv):
                raise ValueError("--model requires a value")
            index += 2
            continue
        if token.startswith("--model="):
            index += 1
            continue
        if token == "-c":
            if index + 1 >= len(argv):
                raise ValueError("-c requires key=value")
            value = argv[index + 1]
            key = value.partition("=")[0]
            if key not in _REASONING_KEYS:
                rewritten.extend((token, value))
            index += 2
            continue
        rewritten.append(token)
        index += 1
    rewritten.extend(("--model", model, "-c", f"model_reasoning_effort={reasoning_effort}"))
    return rewritten
