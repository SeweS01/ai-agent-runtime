"""Minimal sd_notify/watchdog support without a runtime dependency."""

from __future__ import annotations

import asyncio
import os
import socket


def notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    notifier = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        notifier.connect(address)
        notifier.sendall(message.encode())
        return True
    finally:
        notifier.close()


def _watchdog_interval(raw_usec: str) -> float:
    # Quarter deadline, capped at five seconds, leaves scheduling margin while
    # still allowing systemd to detect an event loop blocked past its deadline.
    return max(min(float(raw_usec) / 4_000_000, 5.0), 0.5)


async def watchdog_loop() -> None:
    raw_interval = os.environ.get("WATCHDOG_USEC")
    if not raw_interval:
        return
    interval = _watchdog_interval(raw_interval)
    while True:
        await asyncio.sleep(interval)
        notify("WATCHDOG=1")
