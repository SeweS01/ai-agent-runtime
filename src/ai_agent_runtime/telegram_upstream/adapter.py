"""Pure rules coupled to an explicitly pinned Telegram Agent release."""

from __future__ import annotations

from dataclasses import dataclass

# Review/qualification baseline from the approved specification. Production may
# pin a different SHA only after its own golden qualification report.
VALIDATION_BASELINE_SHA = "0430c4e4ddbc6b7a2cfcccd5af95c456a3d3961b"


@dataclass(frozen=True, slots=True)
class TelegramUpstreamAdapter:
    upstream_sha: str
    session_name_prefix: str = "cc-"

    def expected_session_name(self, *, chat_id: int, thread_id: int | None) -> str:
        if chat_id == 0:
            raise ValueError("chat_id must be non-zero")
        suffix = str(thread_id) if thread_id is not None else "0"
        chat_part = f"n{-chat_id}" if chat_id < 0 else str(chat_id)
        return f"{self.session_name_prefix}{chat_part}-{suffix}"

    def assert_qualified(self, qualified_shas: set[str]) -> None:
        if self.upstream_sha not in qualified_shas:
            raise ValueError(f"unqualified Telegram Agent SHA: {self.upstream_sha}")
