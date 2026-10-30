"""SMP watch: one ping in ⛏️・monkey-smp when 3 or more are on Monkey SMP (rules in
logic/smpwatch.py).

Every 5 minutes the cog looks the public join address up through its SRV record (cached for an
hour, or until a ping through it fails; no record: the name itself on port 25565) and sends one
Server List Ping (cogs/monkey.py `ping_server`). It reads only the player count. When the logic
says so, it posts one line that mentions the opt-in SMP Ping role (members pick it in 🎭・roles;
`server/public_mode.py --apply` creates it) and saves the state in `meta`.

- Independent of the Minecraft bridge (cogs/monkey.py): no MONKEY_* settings, no RCON, no
  secrets. The address is public.
- Mentions only that role: `AllowedMentions(roles=[role])`, never @everyone or members.
- No role or no channel yet: nothing is posted and nothing is saved (logged once), so the first
  ping goes out once they exist.
- DNS: a plain UDP query to the resolvers in /etc/resolv.conf, then 1.1.1.1 and 8.8.8.8, in a
  worker thread. Only the public SRV name is sent.
- SMP_WATCH=off (server/.env) runs nothing.
"""

import asyncio
import functools
import logging
import os
import secrets
import socket
import time

import discord
from discord.ext import commands, tasks

import config
from cogs.monkey import PingError, ping_server
from logic import monkey as R
from logic import smpwatch as L

log = logging.getLogger(__name__)

RESOLV_CONF = "/etc/resolv.conf"


def now() -> int:
    return int(time.time())


def resolvers() -> list[str]:
    try:
        with open(RESOLV_CONF, encoding="utf-8", errors="replace") as f:
            system = L.resolv_conf_servers(f.read())
    except OSError:
        system = []
    return [*system, *(r for r in L.PUBLIC_RESOLVERS if r not in system)]


def udp_exchange(server: str, packet: bytes, timeout: float) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(packet, (server, L.DNS_PORT))
        data, _ = s.recvfrom(L.MAX_REPLY)
        return data


async def lookup_srv(name: str) -> list[L.Srv]:
    """The SRV records of `name` from the first resolver that answers usefully."""
    last: Exception | None = None
    for server in resolvers()[:4]:
        qid = secrets.randbelow(1 << 16)
        try:
            data = await asyncio.to_thread(udp_exchange, server, L.srv_query(name, qid), L.DNS_TIMEOUT)
            return L.parse_srv_reply(data, qid)
        except (OSError, L.DnsError) as exc:
            last = exc
    raise L.DnsError(type(last).__name__ if last else "no resolver")


def quietly(fn):
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            return await fn(self, *args)
        except Exception:
            log.exception("smpwatch: %s failed", fn.__name__)
    return wrapper


class SmpWatch(commands.Cog):
    def __init__(self, bot, environ=None, pinger=None, lookup=None):
        self.bot = bot
        self.settings = L.read_settings(os.environ if environ is None else environ)
        self.pinger = pinger or ping_server
        self.lookup = lookup or lookup_srv
        self.target: tuple[str, int] | None = None
        self.target_at = 0
        self.state: L.State | None = None
        self.said: set[str] = set()

    async def cog_load(self) -> None:
        for problem in self.settings.problems:
            log.warning("smpwatch: %s", problem)
        if self.settings.enabled:
            log.info("smpwatch: on (%s, %d or more)", self.settings.address, self.settings.min_online)
            self.tick.start()
        else:
            log.info("smpwatch: off (SMP_WATCH=off)")

    async def cog_unload(self) -> None:
        self.tick.cancel()

    @property
    def db(self):
        return self.bot.db

    def once(self, key: str, text: str, *args) -> None:
        if key not in self.said:
            self.said.add(key)
            log.info(text, *args)

    # ------------------------------------------------------------ the server
    async def resolve(self, t: int) -> tuple[str, int]:
        if self.settings.port is not None:
            return self.settings.host, self.settings.port
        if self.target is not None and t - self.target_at < L.SRV_CACHE:
            return self.target
        try:
            found = L.pick_srv(await self.lookup(L.srv_name(self.settings.host)))
        except L.DnsError as exc:
            self.once("dns", "smpwatch: no SRV answer for %s (%s); trying port %d", self.settings.host, exc,
                      R.DEFAULT_PORT)
            found = None
        else:
            self.said.discard("dns")
        self.target, self.target_at = found or L.fallback(self.settings.host), t
        return self.target

    async def online_now(self, t: int) -> int | None:
        host, port = await self.resolve(t)
        try:
            status = R.read_status(await self.pinger(host, port))
        except PingError as exc:
            self.target = None  # look it up again next time: the server may have moved
            self.once("ping", "smpwatch: the server did not answer a status ping (%s)", exc)
            return None
        self.said.discard("ping")
        return status.online

    # ------------------------------------------------------------ the ping
    def where(self):
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        if guild is None:
            return None, None
        channel = config.match_by_names(guild.text_channels, (config.MONKEY_CHANNEL, *config.MONKEY_CHANNEL_WAS))
        return channel, config.match_by_name(guild.roles, config.SMP_PING_ROLE)

    async def post(self, online: int) -> bool:
        channel, role = self.where()
        if channel is None or role is None:
            self.once("missing", "smpwatch: no %s %s yet (server/public_mode.py --apply creates it); not pinging",
                      *(("channel", config.MONKEY_CHANNEL) if channel is None else ("role", config.SMP_PING_ROLE)))
            return False
        self.said.discard("missing")
        try:
            await channel.send(L.ping_text(role.mention, online, self.settings.address),
                               allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role]))
        except discord.HTTPException as exc:
            self.once("send", "smpwatch: could not post in %s (%s)", config.MONKEY_CHANNEL, type(exc).__name__)
            return False
        self.said.discard("send")
        return True

    async def check_once(self) -> None:
        t = now()
        online = await self.online_now(t)
        if self.state is None:
            self.state = L.load(await self.db.meta_get(L.STATE_KEY))
        go, state = L.decide(self.state, online, t, self.bot.settings.tz, self.settings.min_online)
        if go and not await self.post(online):
            return  # try again next check, as if nothing happened
        if state != self.state:
            await self.db.meta_set(L.STATE_KEY, L.dump(state))
            self.state = state

    @tasks.loop(seconds=L.CHECK_EVERY)
    async def tick(self) -> None:
        await self.run_once()

    @quietly
    async def run_once(self) -> None:
        await self.check_once()

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(SmpWatch(bot))
