"""Welcome cards: once a new member finishes Onboarding (or joins a server without it),
post a generated 1100x400 card in the welcome channel: their avatar, "Welcome, NAME",
"Member #N" and the server name. Once per member, ever (meta key card:USERID).

A card that fails to post (no permission, Discord down, no welcome channel) leaves a
retry record (meta key card:retry:USERID = time of the first failure). A loop tries
again every 10 minutes for up to 24 hours while the member is still here, then gives up.

Needs the Server Members intent for join/update events; without it the cog loads and
does nothing. The bot needs Send Messages and Attach Files in the welcome channel."""

import asyncio
import functools
import io
import logging
import time

import discord
from discord.ext import commands, tasks

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

    async def cog_load(self) -> None:
        self.retry_loop.start()

    async def cog_unload(self) -> None:
        self.retry_loop.cancel()

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
            await self.note_failure(member.id)
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
            # Release the claim so a later event or the retry loop can post it.
            await self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
            await self.note_failure(member.id)
            raise
        await self.db.execute("DELETE FROM meta WHERE key = ?", (cards.retry_key(member.id),))
        return True

    # ------------------------------------------------------------ retries
    async def note_failure(self, user_id: int) -> None:
        """Remember the first failure; later ones keep that time, so 24 h means 24 h."""
        await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                              (cards.retry_key(user_id), str(now())))

    @tasks.loop(seconds=cards.RETRY_EVERY)
    async def retry_loop(self) -> None:
        try:
            await self.retry_failed()
        except Exception:
            log.exception("cards: retry loop failed")

    @retry_loop.before_loop
    async def before_retry(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    async def find_member(guild, user_id: int):
        """(member or None, known): known is False when Discord couldn't say."""
        member = guild.get_member(user_id)
        if member is not None:
            return member, True
        try:
            return await guild.fetch_member(user_id), True
        except discord.NotFound:
            return None, True
        except discord.HTTPException:
            return None, False

    async def retry_failed(self) -> int:
        """One pass over the failed cards. Returns how many were posted."""
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        if guild is None:
            return 0
        rows = await self.db.fetchall("SELECT key, value FROM meta WHERE key LIKE ?", (cards.RETRY_PREFIX + "%",))
        posted = 0
        for row in rows:
            user_id = cards.retry_user(row["key"])
            try:
                first = int(row["value"])
            except ValueError:
                user_id = None
            if user_id is None:
                await self.db.execute("DELETE FROM meta WHERE key = ?", (row["key"],))
                continue
            member, known = await self.find_member(guild, user_id)
            if not known:
                continue  # Discord trouble: look again next time
            action = cards.retry_action(first, now(), member is not None, await self.already_carded(user_id))
            if action == cards.RETRY:
                try:
                    posted += bool(await self.post_card(member))
                except Exception as e:
                    # WARNING with no traceback: the same failure repeats every 10 minutes.
                    log.warning("cards: retry for %s failed (%s: %s)", user_id, type(e).__name__, e)
                continue
            if action == cards.GIVE_UP:
                log.warning("cards: giving up on the card for %s after %d h", user_id, cards.RETRY_FOR // 3600)
            await self.db.execute("DELETE FROM meta WHERE key = ?", (row["key"],))
        return posted


async def setup(bot) -> None:
    await bot.add_cog(Cards(bot))
