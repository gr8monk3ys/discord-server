"""Tonight: one quiet line in 💬・general while people are together in voice (rules in
logic/tonight.py).

Every minute the cog counts the non-bot members in each public voice channel (everyone can
view and join it; not the AFK channel, not the staff category) and asks the logic what the one
message should say. It is posted once a room has held for 3 minutes, edited in place at most
every 2 minutes, and turned into a past-tense line when the rooms empty; it is never deleted.
Counts only, never names; the channel is a mention. Members with tracking off are counted, but
their presence is not read. No pings, no embed, no commands, no coins, nothing stored about
members: the state is one `meta` value (tonight:state), so a restart carries on from it and
checks it against who is in voice now.

- The previous line is reused (edited) only while it is still the newest message in general.
  Anything posted after it, by a member or by another bot post, means the next session posts
  a new line, so the line is always at the bottom (a history look on startup covers downtime).
- Not within an hour of the chat revival (cogs/vibes.py), which reads tonight:fired_at and
  waits an hour the other way.
- A message a moderator deleted is not posted again for that session.
- TONIGHT=off (server/.env): no ticks; a line that still says "right now" is closed once.
- No general channel, no voice-state intent: does nothing. No presence intent: no game named.
"""

import functools
import logging
import os
import time

import discord
from discord.ext import commands, tasks

import config
from cogs.stats import Stats, playing
from cogs.vibes import LAST_REVIVAL_KEY
from logic import tonight as L

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()


def now() -> int:
    return int(time.time())


def quietly(fn):
    """A tick or listener logs and carries on."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            return await fn(self, *args)
        except Exception:
            log.exception("tonight: %s failed", fn.__name__)
    return wrapper


class Tonight(commands.Cog):
    def __init__(self, bot, environ=None):
        self.bot = bot
        self.enabled = config.tonight_enabled(os.environ if environ is None else environ)
        self.state: L.State | None = None  # loaded from meta on first use
        self.newest_id: int | None = None  # newest message seen in general; None: not known yet
        self.warned = False
        self.write_failed = False  # the last post or edit was refused (said once, not every retry)

    async def cog_load(self) -> None:
        if self.enabled:
            self.tick.start()
        else:
            log.info("tonight: off (TONIGHT=off)")
            self.wrap_up.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.wrap_up.cancel()

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def general(self, guild):
        return config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL) if guild else None

    def voice_ok(self) -> bool:
        return bool(getattr(getattr(self.bot, "intents", None), "voice_states", False))

    def presence_ok(self) -> bool:
        return bool(getattr(getattr(self.bot, "intents", None), "presences", False))

    async def load(self) -> L.State:
        if self.state is None:
            self.state = L.load(await self.db.meta_get(L.STATE_KEY))
        return self.state

    async def save(self, state: L.State) -> None:
        if state != self.state:
            await self.db.meta_set(L.STATE_KEY, L.dump(state))
            self.state = state

    # ------------------------------------------------------------ what is true now
    @staticmethod
    def public_room(guild, channel) -> bool:
        """Everyone can see and join it, and it is neither the AFK channel nor a staff channel.
        Anything that cannot be confirmed is not public."""
        try:
            afk = getattr(guild, "afk_channel", None)
            if (afk is not None and afk.id == channel.id) or Stats.in_staff(channel):
                return False
            perms = channel.permissions_for(guild.default_role)
            return bool(perms.view_channel and perms.connect)
        except Exception:
            return False

    async def rooms(self, guild) -> list[L.Room]:
        rooms, hidden = [], None
        for channel in getattr(guild, "voice_channels", []):
            if not self.public_room(guild, channel):
                continue
            members = [(uid, guild.get_member(uid)) for uid in channel.voice_states]
            # Only members the cache knows: someone it can't see might be a bot, and "2 people"
            # must never be one person and a music bot.
            people = [(uid, m) for uid, m in members if m is not None and not m.bot]
            if len(people) < L.MIN_PEOPLE:
                continue
            games = ()
            if self.presence_ok():
                if hidden is None:
                    hidden = {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}
                games = tuple(playing(m) for uid, m in people if m is not None and uid not in hidden)
            rooms.append(L.Room(channel.id, len(people), games))
        return rooms

    def is_general(self, channel) -> bool:
        return (getattr(channel, "parent", None) is None
                and config.match_by_name([channel], config.GENERAL_CHANNEL) is not None)

    @commands.Cog.listener()
    @quietly
    async def on_message(self, message) -> None:
        if not self.enabled or message.guild is None:
            return
        if message.guild.id == self.bot.settings.guild_id and self.is_general(message.channel):
            self.newest_id = max(self.newest_id or 0, message.id)  # anyone's, our own line included

    async def scan(self, channel, message_id: int) -> int:
        """The id of a message posted in general after our line, or the line's own id when it
        is still the newest. Unknown counts as still the newest: the old line is edited rather
        than a second line stacked under it."""
        try:
            async for m in channel.history(limit=1, after=discord.Object(message_id)):
                return max(message_id, m.id)
        except Exception as e:
            log.info("tonight: couldn't scan %s history (%s)", config.GENERAL_CHANNEL, type(e).__name__)
        return message_id

    async def quiet_until(self) -> int:
        raw = await self.db.meta_get(LAST_REVIVAL_KEY)
        return int(raw) + L.REVIVAL_GAP if raw and raw.isdigit() else 0

    # ------------------------------------------------------------ the tick
    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        await self.run_once()

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    @quietly
    async def run_once(self) -> None:
        guild = self.guild()
        channel = self.general(guild)
        if not self.enabled or guild is None or channel is None or not self.voice_ok():
            if guild is not None and not self.warned and self.enabled:
                self.warned = True
                log.info("tonight: idle (%s)", "no voice-state intent" if channel is not None
                         else f"no {config.GENERAL_CHANNEL} channel")
            return
        state, t = await self.load(), now()
        rooms = await self.rooms(guild)
        if self.newest_id is None and state.message_id is not None:
            self.newest_id = await self.scan(channel, state.message_id)
        buried = state.message_id is not None and (self.newest_id or 0) > state.message_id
        state, action = L.step(state, rooms, t, self.bot.settings.tz, buried=buried,
                               quiet_until=await self.quiet_until())
        await self.save(await self.perform(channel, state, action, t))

    async def perform(self, channel, state: L.State, action, t: int) -> L.State:
        """Do the one thing the logic asked for and fold the result into the state."""
        if action is None:
            return state
        before = state
        try:
            if isinstance(action, L.Post):
                message = await channel.send(action.text, allowed_mentions=NO_PINGS)
                state = L.posted(state, message.id, action.text, t, self.bot.settings.tz)
                self.newest_id = max(self.newest_id or 0, message.id)
            else:
                await channel.get_partial_message(action.message_id).edit(content=action.text,
                                                                          allowed_mentions=NO_PINGS)
                state = L.edited(state, action.text, t)
        except discord.HTTPException as e:
            if isinstance(e, discord.NotFound) and isinstance(action, L.Edit):
                log.info("tonight: the line was deleted")
                return L.gone(state, t)
            # Retried after the edit gap. Said once, not on every retry of a long session.
            if not self.write_failed:
                self.write_failed = True
                log.warning("tonight: couldn't %s the line (%s); retrying quietly",
                            "post" if isinstance(action, L.Post) else "edit", type(e).__name__)
            return L.failed(state, t)
        self.write_failed = False
        if L.fired(before, state):
            await self.db.meta_set(L.FIRED_KEY, t)
            log.info("tonight: line %s", "posted" if isinstance(action, L.Post) else "reused")
        return state

    # ------------------------------------------------------------ switched off
    @tasks.loop(count=1)
    async def wrap_up(self) -> None:
        await self.close_line()

    @wrap_up.before_loop
    async def before_wrap_up(self) -> None:
        await self.bot.wait_until_ready()

    @quietly
    async def close_line(self) -> None:
        """Off: a line left saying "right now" becomes the past-tense line, once."""
        guild = self.guild()
        if guild is None:
            return
        t = now()
        state, action = L.stop(await self.load(), t)
        channel = self.general(guild)
        if action is not None and channel is not None:
            state = await self.perform(channel, state, action, t)
        await self.save(state)


async def setup(bot) -> None:
    await bot.add_cog(Tonight(bot))
