#!/usr/bin/env python3
"""Deterministically exercise external-tmux retirement windows in pinned upstream.

Run this script with the pinned Telegram Agent Python environment so imports resolve
against that checkout, for example:

    /path/to/telegram-ai-agent/.venv/bin/python \
      tools/qualify_retirement_race.py ...

The harness never talks to Telegram or CUS. It executes the pinned upstream
TmuxManager/send path with deterministic tmux test doubles and records whether every
prompt was either submitted exactly once or caused an explicit upstream alert.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import types
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT / "src"))

from ai_agent_runtime.migration.inventory import _digest, write_json_artifact  # noqa: E402
from ai_agent_runtime.qualification.release_gate import (  # noqa: E402
    QUALIFICATION_EVIDENCE_SCHEMA,
    release_tuple_digest,
    validate_release_tuple,
)

WINDOWS = (
    "first_launch_delayed_accept",
    "after_is_active_before_paste",
    "during_paste",
    "after_paste_before_enter",
    "after_pending_turn_registration",
    "turn_complete_before_runtime_idle",
    "two_simultaneous_messages",
)

_CHILD_MARKER = "AI_AGENT_RUNTIME_RETIREMENT_HARNESS_CHILD"
_SAFE_ENV_KEYS = ("HOME", "LANG", "LC_ALL", "PATH", "TERM", "TZ")


def _runtime_python_paths() -> tuple[Path, ...]:
    """Return only the checked-out or installed Runtime import roots."""

    module = Path(__file__).resolve()
    root = module.parents[1]
    source = root / "src"
    if (source / "ai_agent_runtime").is_dir():
        return (root, source)
    return (root,)


def _run_with_upstream_environment(upstream: Path, argv: list[str]) -> int | None:
    """Re-exec once in pinned upstream's venv without forwarding secret env vars."""

    if os.environ.get(_CHILD_MARKER) == "1":
        return None
    python = upstream / ".venv" / "bin" / "python"
    if not python.exists() or not os.access(python, os.X_OK):
        raise SystemExit(f"pinned upstream Python is unavailable: {python}")
    environment = {key: os.environ[key] for key in _SAFE_ENV_KEYS if os.environ.get(key)}
    environment[_CHILD_MARKER] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in _runtime_python_paths())
    completed = subprocess.run(
        [str(python), "-m", "tools.qualify_retirement_race", *argv],
        check=False,
        env=environment,
        stdin=subprocess.DEVNULL,
    )
    return completed.returncode


@dataclass
class PromptEvidence:
    submitted: Counter[str] = field(default_factory=Counter)
    explicit_alerts: Counter[str] = field(default_factory=Counter)


@dataclass
class FakePlaceholder:
    message_id: int
    deleted: bool = False

    async def delete(self) -> None:
        self.deleted = True


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        *,
        answer_barrier: asyncio.Barrier | None = None,
    ) -> None:
        self.message_id = message_id
        self.chat = types.SimpleNamespace(id=-1001)
        self._answer_barrier = answer_barrier
        self.placeholders: list[FakePlaceholder] = []

    async def answer(self, _text: str, **_kwargs: object) -> FakePlaceholder:
        if self._answer_barrier is not None:
            await self._answer_barrier.wait()
        placeholder = FakePlaceholder(self.message_id + 1000)
        self.placeholders.append(placeholder)
        return placeholder


@contextmanager
def patched_attributes(target: object, **updates: object) -> Iterator[None]:
    original = {name: getattr(target, name) for name in updates}
    try:
        for name, value in updates.items():
            setattr(target, name, value)
        yield
    finally:
        for name, value in original.items():
            setattr(target, name, value)


class UpstreamScenario:
    def __init__(self, upstream_root: Path, scratch: Path) -> None:
        sys.path.insert(0, str(upstream_root / "src"))
        from telegram_bot.core.handlers import streaming
        from telegram_bot.core.services import tmux_manager as tmux_module
        from telegram_bot.core.services.tmux_manager import TmuxManager, TmuxSessionState

        self.streaming = streaming
        self.tmux_module = tmux_module
        self.key = (-1001, 42)
        self.manager = TmuxManager(scratch / "tmux_sessions")
        self.state = TmuxSessionState(
            session_name="cc-n1001-42",
            session_dir=str(scratch / "tmux_sessions" / "cc-n1001-42"),
            session_id="session-1",
            mode="free",
            cwd=str(scratch / "project"),
            mcp_config="",
            chat_id=-1001,
            runner_version="codex-tui-v1",
            provider="codex",
            model="qualified-model",
            transcript_path=None,
        )
        self.manager._sessions[self.key] = self.state
        self.manager._cancel_events[self.key] = asyncio.Event()
        self.alive = True
        self.pasted: set[str] = set()
        self.evidence = PromptEvidence()
        self.capture_after_paste: Counter[str] = Counter()
        self.manager._tmux_alive = lambda _session_name: self.alive  # type: ignore[method-assign]
        self.manager.get_topic_config = lambda: None  # type: ignore[method-assign]
        self.manager._transcript_path_for_state = lambda _state: None  # type: ignore[method-assign]
        self.manager._codex_delivery_visible = (  # type: ignore[method-assign]
            lambda _before, _after, prompt: self.alive and prompt in self.pasted
        )
        self.manager._codex_prompt_still_in_input_bar = (  # type: ignore[method-assign]
            lambda _pane, _prompt: False
        )

        async def alert(
            _manager: object,
            _channel_key: object,
            _state: object,
            prompt: str,
            _pane: str,
            **_kwargs: object,
        ) -> None:
            self.evidence.explicit_alerts[prompt] += 1

        self.manager._send_modal_alert = types.MethodType(alert, self.manager)  # type: ignore[method-assign]

    def retire(self) -> None:
        # External Runtime retirement kills tmux but does not mutate Telegram
        # Agent's in-process session registry.
        self.alive = False

    async def capture(self, _session_name: str) -> str:
        if not self.alive:
            return ""
        if not self.pasted:
            return "codex ready"
        prompt = sorted(self.pasted)[-1]
        self.capture_after_paste[prompt] += 1
        return f"codex ready\n> {prompt}"

    async def paste(self, _session_name: str, prompt: str, *, submit_enter: bool) -> None:
        assert submit_enter is False
        if not self.alive:
            raise OSError("tmux disappeared before paste")
        self.pasted.add(prompt)

    async def enter(self, _session_name: str) -> None:
        if not self.alive:
            raise OSError("tmux disappeared before Enter")
        if not self.pasted:
            raise OSError("no prompt was pasted")
        prompt = sorted(self.pasted)[-1]
        self.evidence.submitted[prompt] += 1

    @contextmanager
    def patches(self) -> Iterator[None]:
        with (
            patched_attributes(
                self.streaming,
                _resolve_stream_mode=lambda *_args, **_kwargs: "off",
            ),
            patched_attributes(
                self.tmux_module,
                capture_pane=self.capture,
                send_text_to_tmux=self.paste,
                send_enter=self.enter,
                _CODEX_PASTE_POLL_BUDGET_SEC=0.0,
                _CODEX_PASTE_POLL_STEP_SEC=0.0,
                _CODEX_PASTE_VISIBILITY_ATTEMPT_LIMIT=1,
                _ENTER_RETRY_SETTLE_SEC=0.0,
                _ENTER_RETRY_LIMIT=1,
            ),
        ):
            yield

    async def dispatch(
        self, prompt: str, message_id: int, *, barrier: asyncio.Barrier | None = None
    ) -> None:
        await self.streaming.send_to_tmux_if_active(
            self.key,
            prompt,
            FakeMessage(message_id, answer_barrier=barrier),
            self.manager,
        )


def _prompt_outcomes(evidence: PromptEvidence, prompts: tuple[str, ...]) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for prompt in prompts:
        submitted = evidence.submitted[prompt]
        alerts = evidence.explicit_alerts[prompt]
        if submitted > 0 and alerts > 0:
            outcomes[prompt] = "contradictory_alert"
        elif submitted == 1:
            outcomes[prompt] = "exactly_once"
        elif submitted == 0 and alerts >= 1:
            outcomes[prompt] = "explicit_not_delivered"
        elif submitted > 1:
            outcomes[prompt] = "duplicate_execution"
        else:
            outcomes[prompt] = "silent_loss"
    return outcomes


async def run_window(window: str, upstream_root: Path, scratch: Path) -> dict[str, object]:
    scenario = UpstreamScenario(upstream_root, scratch / window)
    prompts = (f"race-{window}-1",)
    with scenario.patches():
        if window == "first_launch_delayed_accept":
            # The first Enter is accepted by Codex, but its input bar still
            # displays the prompt for longer than upstream's settle budget.
            # Upstream must not tell the user to resend an already accepted
            # prompt. This reproduces the real 2026-10-06 staging failure.
            scenario.manager._codex_prompt_still_in_input_bar = (  # type: ignore[method-assign]
                lambda _pane, _prompt: True
            )
            enter_calls = 0

            async def delayed_enter(_session_name: str) -> None:
                nonlocal enter_calls
                enter_calls += 1
                if enter_calls == 1:
                    scenario.evidence.submitted[prompts[0]] += 1

            scenario.tmux_module.send_enter = delayed_enter
            await scenario.dispatch(prompts[0], 1)
        elif window == "after_is_active_before_paste":
            original_is_active = scenario.manager.is_active
            calls = 0

            def retire_after_probe(key: object) -> bool:
                nonlocal calls
                active = original_is_active(key)  # type: ignore[arg-type]
                calls += 1
                if calls == 1 and active:
                    scenario.retire()
                return active

            scenario.manager.is_active = retire_after_probe  # type: ignore[method-assign]
            await scenario.dispatch(prompts[0], 1)
        elif window == "during_paste":
            original_paste = scenario.paste

            async def retire_during_paste(
                session_name: str, prompt: str, *, submit_enter: bool
            ) -> None:
                await original_paste(session_name, prompt, submit_enter=submit_enter)
                scenario.retire()

            scenario.tmux_module.send_text_to_tmux = retire_during_paste
            await scenario.dispatch(prompts[0], 1)
        elif window == "after_paste_before_enter":
            original_capture = scenario.capture

            async def retire_before_enter(session_name: str) -> str:
                pane = await original_capture(session_name)
                if scenario.pasted:
                    prompt = sorted(scenario.pasted)[-1]
                    if scenario.capture_after_paste[prompt] >= 2:
                        scenario.retire()
                        return ""
                return pane

            scenario.tmux_module.capture_pane = retire_before_enter
            await scenario.dispatch(prompts[0], 1)
        elif window == "after_pending_turn_registration":
            original_mark = scenario.manager.mark_prompt_pending

            def retire_after_pending(key: object) -> None:
                original_mark(key)  # type: ignore[arg-type]
                scenario.retire()

            scenario.manager.mark_prompt_pending = retire_after_pending  # type: ignore[method-assign]
            await scenario.dispatch(prompts[0], 1)
        elif window == "turn_complete_before_runtime_idle":
            from telegram_bot.core.services.claude import StreamEvent

            await scenario.dispatch(prompts[0], 1)
            scenario.manager.handle_turn_event(
                scenario.key,
                StreamEvent("turn_start", "", turn_id="turn-1"),
            )
            scenario.manager.handle_turn_event(
                scenario.key,
                StreamEvent("turn_end", "", turn_id="turn-1"),
            )
            scenario.retire()
        elif window == "two_simultaneous_messages":
            prompts = ("race-two-messages-1", "race-two-messages-2")
            barrier = asyncio.Barrier(2)
            original_paste = scenario.paste
            paste_calls = 0

            async def retire_first_paste(
                session_name: str, prompt: str, *, submit_enter: bool
            ) -> None:
                nonlocal paste_calls
                paste_calls += 1
                if paste_calls == 1:
                    scenario.retire()
                    raise OSError("tmux retired during first concurrent paste")
                await original_paste(session_name, prompt, submit_enter=submit_enter)

            scenario.tmux_module.send_text_to_tmux = retire_first_paste
            await asyncio.gather(
                scenario.dispatch(prompts[0], 1, barrier=barrier),
                scenario.dispatch(prompts[1], 2, barrier=barrier),
            )
        else:
            raise ValueError(f"unknown retirement window: {window}")

    outcomes = _prompt_outcomes(scenario.evidence, prompts)
    silent_loss = any(outcome == "silent_loss" for outcome in outcomes.values())
    duplicate = any(outcome == "duplicate_execution" for outcome in outcomes.values())
    contradictory_alert = any(outcome == "contradictory_alert" for outcome in outcomes.values())
    safe = not silent_loss and not duplicate and not contradictory_alert
    overall = (
        "exactly_once"
        if safe and all(outcome == "exactly_once" for outcome in outcomes.values())
        else "explicit_not_delivered"
        if safe
        else "unsafe"
    )
    return {
        "id": window,
        "passed": safe,
        "outcome": overall,
        "silent_loss": silent_loss,
        "duplicate_execution": duplicate,
        "contradictory_alert": contradictory_alert,
        "misroute": False,
        "prompt_outcomes": outcomes,
        "submitted_counts": dict(scenario.evidence.submitted),
        "explicit_alert_counts": dict(scenario.evidence.explicit_alerts),
    }


def _git_head(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


async def run(args: argparse.Namespace) -> dict[str, object]:
    upstream_root = Path(args.upstream).expanduser().resolve(strict=True)
    release_tuple = validate_release_tuple(
        json.loads(Path(args.release_tuple).read_text(encoding="utf-8"))
    )
    actual_sha = _git_head(upstream_root)
    expected_sha = str(release_tuple["telegram_agent_sha"])
    if actual_sha != expected_sha:
        raise SystemExit(
            f"pinned upstream mismatch: expected {expected_sha}, observed {actual_sha}"
        )
    output = Path(args.output).expanduser()
    scratch = output.parent / f".{output.name}.scratch"
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    assertions = [await run_window(window, upstream_root, scratch) for window in WINDOWS]
    status = "PASS" if all(item["passed"] is True for item in assertions) else "FAIL"
    evidence: dict[str, object] = {
        "schema": QUALIFICATION_EVIDENCE_SCHEMA,
        "gate_id": "retirement_race",
        "report_id": args.report_id,
        "release_tuple_digest": release_tuple_digest(release_tuple),
        "environment": args.environment,
        "status": status,
        "harness": {
            "kind": "pinned-upstream-deterministic-fault-injection",
            "telegram_agent_sha": actual_sha,
        },
        "assertions": assertions,
    }
    evidence["evidence_digest"] = _digest(evidence)
    write_json_artifact(output, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--release-tuple", required=True)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--environment", choices=("local", "staging"), default="local")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    upstream_root = Path(args.upstream).expanduser().resolve(strict=True)
    child_result = _run_with_upstream_environment(upstream_root, sys.argv[1:])
    if child_result is not None:
        return child_result
    evidence = asyncio.run(run(args))
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
