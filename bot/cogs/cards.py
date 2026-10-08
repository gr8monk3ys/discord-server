"""Welcome cards: once a new member finishes Onboarding (or joins a server without it),
post a generated 1100x400 card in the welcome channel: their avatar, "Welcome, NAME",
"Member #N" and the server name. Once per member, ever (meta key card:USERID).

Needs the Server Members intent for join/update events; without it the cog loads and
does nothing. The bot needs Send Messages and Attach Files in the welcome channel."""

import asyncio
import functools
import io
import logging
import time

import discord
from discord.ext import commands

import config
from logic import cards

log = logging.getLogger(__name__)

ONBOARDING_TTL = 10 * 60  # re-check whether the server uses Onboarding at most this often
AVATAR_TIMEOUT = 10  # seconds to wait for the avatar download before using the initial circle
FILENAME = "welcome.png"


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("cards: %s failed", fn.__name__)
    return wrapper


class Cards(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.onboarding_cache: tuple[bool, int] | None = None  # (enabled, checked_at)

    @property
    def db(self):
        return self.bot.db

    def ours(self, guild) -> bool:
        return guild is not None and guild.id == self.bot.settings.guild_id

    # ------------------------------------------------------------ listeners
    @commands.Cog.listener()
    @never_raise
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or not self.ours(member.guild):
            return
        # Same rule as the Community welcome: straight away if they're already through
        # Onboarding or the server doesn't use it; otherwise wait for on_member_update.
        if member.flags.completed_onboarding or not await self.onboarding_enabled(member.guild):
            await self.post_card(member)

    @commands.Cog.listener()
    @never_raise
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or not self.ours(after.guild):
            return
        if not before.flags.completed_onboarding and after.flags.completed_onboarding:
            await self.post_card(after)

    async def onboarding_enabled(self, guild) -> bool:
        """Whether the server runs Onboarding (cached). If Discord won't say, assume it
        does: the member then gets their card when they finish it."""
        current = now()
        if self.onboarding_cache and current - self.onboarding_cache[1] < ONBOARDING_TTL:
            return self.onboarding_cache[0]
        try:
            enabled = bool((await guild.onboarding()).enabled)
        except discord.HTTPException:
            log.warning("cards: couldn't check whether Onboarding is on; assuming it is", exc_info=True)
            return True
        self.onboarding_cache = (enabled, current)
        return enabled

    # ------------------------------------------------------------ the card
    async def avatar_bytes(self, member) -> bytes | None:
        try:
            asset = member.display_avatar.replace(size=256, format="png")
            return await asyncio.wait_for(asset.read(), AVATAR_TIMEOUT)
        except Exception:
            log.info("cards: no avatar for %s; drawing the initial instead", member.id)
            return None

    async def already_carded(self, user_id: int) -> bool:
        return await self.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (cards.card_key(user_id),)) is not None

    async def post_card(self, member: discord.Member) -> bool:
        """Post the member's card unless they've had one. True if it was posted."""
        if not cards.should_card(member.bot, await self.already_carded(member.id)):
            return False
        guild = member.guild
        channel = config.match_by_name(guild.text_channels, config.WELCOME_CHANNEL)
        if channel is None:
            log.warning("cards: no %s channel; skipped the card for %s", config.WELCOME_CHANNEL, member.id)
            return False
        # Claim first, so two events racing can't both post.
        key = cards.card_key(member.id)
        claimed = await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, str(now())))
        if not claimed:
            return False
        try:
            avatar = await self.avatar_bytes(member)
            png = await asyncio.to_thread(cards.render_card, member.display_name, guild.member_count,
                                          guild.name, avatar, member.name)
            # Named, not pinged: the welcome in general already pings them once.
            await channel.send(member.mention, file=discord.File(io.BytesIO(png), filename=FILENAME),
                               allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            await self.db.execute("DELETE FROM meta WHERE key = ?", (key,))  # let a later event retry
            raise
        return True


async def setup(bot) -> None:
    await bot.add_cog(Cards(bot))
