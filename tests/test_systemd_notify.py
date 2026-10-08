"""Watchdog pings leave enough scheduling margin on a busy host."""

from ai_agent_runtime.systemd_notify import _watchdog_interval


def test_watchdog_interval_has_headroom_for_thirty_second_deadline() -> None:
    assert _watchdog_interval("30000000") == 5.0


def test_watchdog_interval_respects_short_deadlines() -> None:
    assert _watchdog_interval("4000000") == 1.0
    assert _watchdog_interval("1000000") == 0.5
