"""Heartbeat for the downtime watchdog (server/watchdog.py).

Every 60 seconds, while the bot is connected to the gateway, writes
{"ts": <unix time>, "latency": <seconds or null>} to config.HEARTBEAT_FILE atomically
(temp file, then os.replace), so the watchdog never reads a half-written file. When the
bot is disconnected, hung or dead the file simply stops changing and goes stale.
Nothing about members is recorded.
"""

import json
import logging
import math
import os
import time
from pathlib import Path

from discord.ext import commands, tasks

import config

log = logging.getLogger(__name__)

INTERVAL = 60  # seconds


def now() -> int:
    return int(time.time())


def write_heartbeat(path: Path, ts: int, latency) -> None:
    """Atomic write of the heartbeat JSON. Non-finite latency (nan/inf) is stored as null."""
    try:
        lat = float(latency)
    except (TypeError, ValueError):
        lat = None
    if lat is not None and not math.isfinite(lat):
        lat = None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"ts": int(ts), "latency": None if lat is None else round(lat, 4)}),
                   encoding="utf-8")
    os.replace(tmp, path)


class Heartbeat(commands.Cog):
    def __init__(self, bot, path: Path | None = None):
        self.bot = bot
        self.path = Path(path) if path is not None else config.HEARTBEAT_FILE

    async def cog_load(self) -> None:
        self.beat.start()

    async def cog_unload(self) -> None:
        self.beat.cancel()

    def connected(self) -> bool:
        try:
            return bool(self.bot.is_ready()) and not self.bot.is_closed()
        except Exception:
            return False

    async def write_once(self) -> bool:
        """Write one heartbeat if connected. True when written. Never raises."""
        try:
            if not self.connected():
                return False
            write_heartbeat(self.path, now(), getattr(self.bot, "latency", None))
            return True
        except Exception:
            log.exception("heartbeat write failed")
            return False

    @tasks.loop(seconds=INTERVAL)
    async def beat(self) -> None:
        await self.write_once()

    @beat.before_loop
    async def before_beat(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Heartbeat(bot))
