"""Module 6: Hall of fame. When a message gets 3 ⭐ from people other than its
author (bots don't count), the bot reposts it in the hall-of-fame channel with a
jump link, then keeps the star count on that post up to date. Each message is
posted at most once; the post stays even if stars drop below the threshold.

Uses raw reaction events, so it works for messages that aren't cached. Copying
the text needs the Message Content intent; without it posts just show images."""

import asyncio
import functools
import logging
import time
import weakref
from datetime import datetime, timezone

import discord
from discord.ext import commands

import config
import style
from logic import starboard as S

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
NO_TEXT = "*(no text)*"


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("starboard: %s failed", fn.__name__)
    return wrapper


def in_staff(channel) -> bool:
    """Staff channels (and threads in them)."""
    parent = getattr(channel, "parent", None) or channel
    category = getattr(parent, "category", None)
    return category is not None and config.match_by_name([category], config.STAFF_CATEGORY) is not None


def is_public(channel, guild) -> bool:
    """Only repost what @everyone can already read: never leak private channels
    (06 · squad, staff) or private threads into the public hall. Fails closed."""
    try:
        private_thread = getattr(channel, "is_private", None)  # only threads have it
        if callable(private_thread) and private_thread():
            return False
        target = getattr(channel, "parent", None) or channel
        return bool(target.permissions_for(guild.default_role).view_channel)
    except Exception:
        return False


def is_nsfw(channel) -> bool:
    check = getattr(channel, "is_nsfw", None)
    return bool(check()) if check else False


def jump_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Jump to message", url=url))
    return view


def render(message, channel_name: str, stars: int) -> discord.Embed:
    image = S.pick_image(message.attachments, message.embeds)
    description = S.text(message.content) or (None if image else NO_TEXT)
    e = style.embed(description=description, footer=S.footer(channel_name, stars))
    e.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    if image:
        e.set_image(url=image)
    e.timestamp = message.created_at
    return e


class Starboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # One lock per message being updated; entries vanish once nobody holds them.
        self.locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self.warned_no_hall = False

    @property
    def db(self):
        return self.bot.db

    def lock(self, message_id: int) -> asyncio.Lock:
        lock = self.locks.get(message_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[message_id] = lock
        return lock

    # ------------------------------------------------------------ events
    @commands.Cog.listener()
    @never_raise
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self.handle(payload)

    @commands.Cog.listener()
    @never_raise
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self.handle(payload)

    @commands.Cog.listener()
    @never_raise
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.unpost([payload.message_id], payload.guild_id)

    @commands.Cog.listener()
    @never_raise
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        await self.unpost(list(payload.message_ids), payload.guild_id)

    async def unpost(self, message_ids: list[int], guild_id: int | None) -> None:
        """A deleted original takes its hall copy with it, so a mod removing a
        message can't be undone by the starboard keeping a copy."""
        if guild_id != self.bot.settings.guild_id:
            return
        guild = self.bot.get_guild(guild_id)
        hall = self.hall(guild) if guild else None
        for mid in message_ids:
            async with self.lock(mid):
                row = await self.db.fetchone(
                    "SELECT channel_id, board_message_id, at FROM starboard WHERE message_id = ?", (mid,))
                if row is None:
                    continue
                await self.db.execute("DELETE FROM starboard WHERE message_id = ?", (mid,))
                board_id = row["board_message_id"]
                if hall is not None and board_id == S.PENDING:
                    # An unfinished claim (we hold the lock, so nobody is sending it
                    # now): the post may still have reached the hall.
                    try:
                        board_id = await self.find_post(hall, row["channel_id"], mid, row["at"])
                    except discord.HTTPException:
                        log.warning("couldn't search the hall for deleted message %s", mid, exc_info=True)
                if hall is not None and board_id:
                    try:
                        await hall.get_partial_message(board_id).delete()
                    except discord.NotFound:
                        pass
                    except discord.HTTPException:
                        log.warning("couldn't remove hall post for deleted message %s", mid, exc_info=True)

    async def handle(self, payload) -> None:
        if payload.guild_id != self.bot.settings.guild_id or not S.is_star(payload.emoji):
            return
        # Serialise per message: a burst of stars must not race to post twice.
        async with self.lock(payload.message_id):
            await self.refresh(payload.guild_id, payload.channel_id, payload.message_id)

    # ------------------------------------------------------------ the work
    def hall(self, guild):
        channel = config.match_by_name(guild.text_channels, config.HALL_OF_FAME_CHANNEL)
        if channel is None:
            if not self.warned_no_hall:
                log.warning("no %s channel: the hall of fame is off until it exists", config.HALL_OF_FAME_CHANNEL)
                self.warned_no_hall = True
        else:
            self.warned_no_hall = False
        return channel

    async def channel(self, guild, channel_id: int):
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel

    async def board(self, message_id: int) -> S.Board | None:
        row = await self.db.fetchone("SELECT board_message_id, stars, at FROM starboard WHERE message_id = ?",
                                     (message_id,))
        return S.Board(row["board_message_id"], row["stars"], row["at"]) if row else None

    async def find_post(self, hall, channel_id: int, message_id: int, since: int) -> int | None:
        """Our hall post for a message (found by its jump button), sent since `since`."""
        me = getattr(self.bot, "user", None)
        suffix = f"/{channel_id}/{message_id}"
        after = datetime.fromtimestamp(since - 60, tz=timezone.utc)
        async for m in hall.history(limit=200, after=after):
            if me is not None and m.author.id != me.id:
                continue
            for row in getattr(m, "components", None) or ():
                for item in getattr(row, "children", None) or ():
                    if (getattr(item, "url", None) or "").endswith(suffix):
                        return m.id
        return None

    async def recover(self, hall, channel, message, board: S.Board) -> S.Board | None:
        """Settle a stale PENDING claim: adopt the hall post if the send got through,
        otherwise drop the claim so the message can be posted again."""
        found = await self.find_post(hall, channel.id, message.id, board.at)
        if found:
            await self.db.execute(
                "UPDATE starboard SET board_message_id = ? WHERE message_id = ? AND board_message_id = ?",
                (found, message.id, S.PENDING))
            log.info("recovered hall post %s for message %s", found, message.id)
            return S.Board(found, board.stars, board.at)
        await self.db.execute("DELETE FROM starboard WHERE message_id = ? AND board_message_id = ?",
                              (message.id, S.PENDING))
        log.info("dropped unfinished hall claim for message %s", message.id)
        return None

    @staticmethod
    async def count(message) -> int:
        reaction = next((r for r in message.reactions if S.is_star(r.emoji)), None)
        if reaction is None:
            return 0
        return S.count_stars([u async for u in reaction.users(limit=None)], message.author.id)

    async def refresh(self, guild_id: int, channel_id: int, message_id: int) -> None:
        """Bring the hall in line with a message's current stars."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        hall = self.hall(guild)
        if hall is None:
            return
        channel = await self.channel(guild, channel_id)
        if channel is None or S.skip_channel(channel.id == hall.id, in_staff(channel), is_nsfw(channel)):
            return
        if not is_public(channel, guild):
            return
        board = await self.board(message_id)
        if board is None:
            created = discord.utils.snowflake_time(message_id).timestamp()
            if S.too_old(created, now()):
                return  # old messages are never newly posted (already-posted ones still update)
        elif board.board_message_id == S.PENDING and not S.stale_claim(board, now()):
            return  # another update is posting it right now
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            return
        if message.author.bot:
            return
        if S.stale_claim(board, now()):
            board = await self.recover(hall, channel, message, board)
        stars = await self.count(message)
        action = S.plan(board, stars)
        if action is S.Action.POST:
            await self.post(hall, channel, message, stars)
        elif action is S.Action.UPDATE:
            await self.update(hall, channel, message, board.board_message_id, stars)

    async def post(self, hall, channel, message, stars: int) -> None:
        # Claim the row first, so even two racing updates can't both post.
        claimed = await self.db.execute(
            "INSERT OR IGNORE INTO starboard (message_id, channel_id, author_id, board_message_id, stars, at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (message.id, channel.id, message.author.id, S.PENDING, stars, now()))
        if not claimed:
            return
        try:
            sent = await hall.send(embed=render(message, channel.name, stars), view=jump_view(message.jump_url),
                                   allowed_mentions=NO_PINGS)
        except Exception:
            await self.db.execute("DELETE FROM starboard WHERE message_id = ? AND board_message_id = ?",
                                  (message.id, S.PENDING))
            raise
        await self.db.execute("UPDATE starboard SET board_message_id = ? WHERE message_id = ?",
                              (sent.id, message.id))

    async def update(self, hall, channel, message, board_message_id: int, stars: int) -> None:
        try:
            await hall.get_partial_message(board_message_id).edit(
                embed=render(message, channel.name, stars), view=jump_view(message.jump_url))
        except discord.NotFound:
            # Someone deleted the hall post: keep the count, never repost.
            log.info("hall post %s for message %s is gone; not reposting", board_message_id, message.id)
        await self.db.execute("UPDATE starboard SET stars = ? WHERE message_id = ?", (stars, message.id))


async def setup(bot) -> None:
    await bot.add_cog(Starboard(bot))
