"""Codex provider adapter."""

from .argv import ParsedInvocation, parse_invocation, rewrite_managed_args

__all__ = ["ParsedInvocation", "parse_invocation", "rewrite_managed_args"]
