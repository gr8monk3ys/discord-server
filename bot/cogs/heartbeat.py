"""Heartbeat for the downtime watchdog (server/watchdog.py).

Every 60 seconds, while the bot is connected to the gateway, writes
{"ts": <unix time>, "latency": <seconds or null>} to config.HEARTBEAT_FILE atomically
(temp file, then os.replace), so the watchdog never reads a half-written file. When the
bot is disconnected, hung or dead the file simply stops changing and goes stale.
discord.py keeps is_ready() True while it retries a dropped gateway connection, so the
cog tracks on_disconnect/on_resumed itself and also checks the socket's keep-alive.
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
LAG_INTERVAL = 5  # seconds between event-loop lag probes
LAG_WARN = 1.0  # log a warning when a probe runs this much late (discord.py only reports 10 s+)


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
        self.online = True  # until a gateway disconnect; is_ready() covers startup
        self.last_tick: float | None = None  # lag probe

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        self.online = False

    @commands.Cog.listener()
    async def on_connect(self) -> None:
        self.online = True

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.online = True

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        self.online = True

    def socket_alive(self) -> bool:
        """False when the bot has no gateway socket, no measurable latency, or no
        HEARTBEAT_ACK for two heartbeat intervals. Bots without a `ws` (tests) pass."""
        if not hasattr(self.bot, "ws"):
            return True
        ws = self.bot.ws
        if ws is None:
            return False
        keep_alive = getattr(ws, "_keep_alive", None)
        if keep_alive is None:
            latency = getattr(self.bot, "latency", None)
            return isinstance(latency, (int, float)) and math.isfinite(latency)
        interval, last_ack = getattr(keep_alive, "interval", None), getattr(keep_alive, "_last_ack", None)
        if isinstance(interval, (int, float)) and isinstance(last_ack, (int, float)):
            return time.perf_counter() - last_ack <= 2 * interval
        return True

    async def cog_load(self) -> None:
        self.beat.start()
        self.lag_probe.start()

    async def cog_unload(self) -> None:
        self.beat.cancel()
        self.lag_probe.cancel()

    def note_tick(self, t: float) -> float | None:
        """Record a lag-probe tick at monotonic time `t`; returns the lag when it was too late."""
        last, self.last_tick = self.last_tick, t
        if last is None:
            return None
        lag = t - last - LAG_INTERVAL
        if lag < LAG_WARN:
            return None
        log.warning("event loop was blocked for about %.1f s", lag)
        return lag

    @tasks.loop(seconds=LAG_INTERVAL)
    async def lag_probe(self) -> None:
        self.note_tick(time.monotonic())

    def connected(self) -> bool:
        try:
            return (self.online and bool(self.bot.is_ready()) and not self.bot.is_closed()
                    and self.socket_alive())
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
