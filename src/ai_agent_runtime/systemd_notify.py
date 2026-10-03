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


async def watchdog_loop() -> None:
    raw_interval = os.environ.get("WATCHDOG_USEC")
    if not raw_interval:
        return
    interval = max(float(raw_interval) / 2_000_000, 0.5)
    while True:
        await asyncio.sleep(interval)
        notify("WATCHDOG=1")
