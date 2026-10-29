"""Welcomers (rules in logic/welcomers.py): when someone new joins a public voice channel for the
first time, one line in 💬・general pings the opt-in Welcomer role so a regular can hop in.

- A voice join is noted in memory only when it is into a public voice channel (everyone can view
  and join it; not AFK, not staff: the tonight module's rule) by a member who joined the server
  in the last 14 days. A 10-second tick looks at each join once it has held for 30 seconds and
  asks the logic whether to ping. Every answer is final: a skipped first join is not retried.
- "First time in voice" is the stats module's voice history (`voice_sessions`), so a member who
  was in voice before this module existed is not new. Members with tracking off are skipped
  entirely (their voice history is not kept, so there is nothing to decide with).
- The ping and the cooldown are claimed in `meta` before the send (welcomers:last_ping, and
  welcomers:pinged:<id> so it is once per newcomer even if their voice history is later deleted);
  a failing channel costs one ping, never one a minute. Marks older than 30 days are pruned hourly.
- Only the role is pinged (`ping_only(roles=[role])`); the newcomer is a mention that does not
  ping them, and the channel is a mention (no member or channel text is copied).

Needs the Server Members intent (role members, join dates). Without the Welcomer role, the
general channel or anyone holding the role, it posts nothing. No commands, no coins."""

import functools
import logging
import time

import discord
from discord.ext import commands, tasks

import config
from cogkit import is_staff
from cogs.lfg import ping_only
from cogs.tonight import Tonight
from logic import crew
from logic import welcomers as W

log = logging.getLogger(__name__)

PRUNE_EVERY = W.HOUR


def now() -> int:
    return int(time.time())


def ts(dt) -> int | None:
    return int(dt.timestamp()) if dt is not None else None


def never_raise(fn):
    """Listeners and ticks log and carry on."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            return await fn(self, *args)
        except Exception:
            log.exception("welcomers: %s failed", fn.__name__)
    return wrapper


class Welcomers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # user id -> (voice channel id, first public voice join, join into that channel): joins
        # waiting out the hold. In memory: a restart forgets them (that join is then not pinged for).
        self.pending: dict[int, tuple[int, int, int]] = {}
        self.last_prune = 0

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def ours(self, guild) -> bool:
        return guild is not None and guild.id == self.bot.settings.guild_id

    @staticmethod
    def role(guild):
        return config.match_by_name(guild.roles, config.WELCOMER_ROLE) if guild is not None else None

    @staticmethod
    def voice_channel(guild, channel_id: int):
        return next((c for c in getattr(guild, "voice_channels", []) if c.id == channel_id), None)

    # ------------------------------------------------------------ voice joins
    @commands.Cog.listener()
    @never_raise
    async def on_voice_state_update(self, member, before, after) -> None:
        channel = getattr(after, "channel", None)
        if member.bot or not self.ours(member.guild) or channel is None or channel == before.channel:
            return
        t = now()
        if not W.is_newcomer(ts(getattr(member, "joined_at", None)), t):
            return  # most voice joins stop here, before any lookup
        if not Tonight.public_room(member.guild, channel):
            return
        first = self.pending[member.id][1] if member.id in self.pending else t
        self.pending[member.id] = (channel.id, first, t)  # moved rooms: the hold starts again there

    # ------------------------------------------------------------ the tick
    @tasks.loop(seconds=10)
    async def tick(self) -> None:
        await self.run_once()

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    @never_raise
    async def run_once(self) -> None:
        t = now()
        for uid, (channel_id, first, joined) in list(self.pending.items()):
            if not W.held(joined, t):
                continue
            del self.pending[uid]
            try:
                await self.consider(uid, channel_id, first, t)
            except Exception:
                log.exception("welcomers: deciding for %s failed", uid)
        if t - self.last_prune >= PRUNE_EVERY:
            self.last_prune = t
            await self.db.execute("DELETE FROM meta WHERE key LIKE ? AND CAST(value AS INTEGER) < ?",
                                  (W.MARK_PREFIX + "%", t - W.MARK_KEEP))

    async def been_in_voice(self, user_id: int, first: int) -> bool:
        row = await self.db.fetchone("SELECT 1 FROM voice_sessions WHERE user_id = ? AND start < ? LIMIT 1",
                                     (user_id, first - W.HISTORY_SLACK))
        return row is not None

    async def consider(self, user_id: int, channel_id: int, first: int, t: int) -> bool:
        """Ping for this held first voice join if the rules allow it. True when a ping was sent."""
        guild = self.guild()
        role = self.role(guild)
        general = config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL) if guild else None
        member = guild.get_member(user_id) if guild is not None else None
        channel = self.voice_channel(guild, channel_id)
        if role is None or general is None or member is None or channel is None:
            return False  # not set up, or they left the server
        here = [m for uid in channel.voice_states if uid != user_id and (m := guild.get_member(uid)) is not None]
        last = await self.db.meta_get(W.LAST_PING_KEY)
        reason = W.skip_reason(
            now=t, tz=self.tz, quiet=crew.quiet_hours(config.crew_env()[1]), is_bot=member.bot,
            joined_at=ts(getattr(member, "joined_at", None)),
            opted_out=not await self.db.tracking_allowed(user_id),
            pinged_before=await self.db.meta_get(W.mark_key(user_id)) is not None,
            been_in_voice=await self.been_in_voice(user_id, first),
            still_there=user_id in channel.voice_states and Tonight.public_room(guild, channel),
            welcomers=sum(1 for m in role.members if not m.bot and m.id != user_id),
            host_there=any(not m.bot and (role in getattr(m, "roles", []) or is_staff(m)) for m in here),
            last_ping_at=int(last) if last and last.isdigit() else None)
        if reason is not None:
            log.info("welcomers: no ping for a first voice join (%s)", reason)
            return False
        # Claimed before sending: a failing channel costs one ping, never one every tick.
        async with self.db.transaction() as tx:
            for key in (W.LAST_PING_KEY, W.mark_key(user_id)):
                await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(t)))
        try:
            await general.send(W.ping_text(role.mention, member.mention, f"<#{channel.id}>"),
                               allowed_mentions=ping_only(roles=[role]))
        except discord.HTTPException:
            log.warning("welcomers: couldn't post the Welcomer ping", exc_info=True)
            return False
        log.info("welcomers: pinged for a first voice join")
        return True


async def setup(bot) -> None:
    await bot.add_cog(Welcomers(bot))
