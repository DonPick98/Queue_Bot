from __future__ import annotations

import asyncio


def channel_publish_lock(application, channel: str) -> asyncio.Lock:
    bot_data = getattr(application, "bot_data", None)
    if bot_data is None:
        bot_data = {}
        application.bot_data = bot_data
    locks = bot_data.setdefault("channel_publish_locks", {})
    lock = locks.get(channel)
    if lock is None:
        lock = asyncio.Lock()
        locks[channel] = lock
    return lock
