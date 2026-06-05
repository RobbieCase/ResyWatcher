"""Entry point. Boots the Discord bot and the watcher loop concurrently."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from app.bot.client import build_bot
from app.config import Settings
from app.storage.store import Store
from app.watcher.loop import WatcherLoop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")


async def amain() -> None:
    settings = Settings.from_env()
    store = Store(settings.data_path)
    await store.load()

    bot = build_bot(settings, store)
    watcher = WatcherLoop(settings, store, bot)

    # Graceful shutdown on SIGTERM (Railway sends this on redeploy)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows / some envs don't support add_signal_handler
            pass

    bot_task = asyncio.create_task(bot.start(settings.discord_token), name="bot")
    watcher_task = asyncio.create_task(watcher.run(), name="watcher")

    log.info("resy-watcher started")
    await stop.wait()
    log.info("shutdown signal received")

    watcher.stop()
    await bot.close()
    await asyncio.gather(bot_task, watcher_task, return_exceptions=True)
    log.info("clean shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
