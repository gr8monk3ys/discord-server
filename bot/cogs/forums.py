"""Forums: the 📚・guides and 🖥・setups forums.

- A new post in either forum gets the forum's reaction from the bot, so no post sits at zero.
- Posts by members who allow tracking are recorded in `forum_posts`. Once a post has been up
  for a day (logic/forums.py SURVIVE) the bot reads it again and, if it clears the
  minimum-content bar, pays a small one-time reward through economy.py (ref = the thread id).
  Paying late instead of at creation means a post that was deleted, or locked by a mod, in the
  meantime never pays: there is no reward to claw back. Accounts under
  quests.MIN_ACCOUNT_DAYS old and anything past the weekly cap earn nothing.
- `fresh_line(start, end)` is the "fresh in the forums" line for the weekly recap.

Members who ran /privacy off are never recorded, paid or listed. Reading a post's text needs
the Message Content intent; without it nothing is judged, so nothing is paid. The module is
simply idle while the two forums don't exist (server/public_mode.py creates them).
"""

import asyncio
import functools
import logging
import time

import discord
from discord.ext import commands, tasks

import economy
from logic import forums as F

log = logging.getLogger(__name__)

STARTER_RETRIES = 3  # the thread-create event can arrive before the starter message exists
STARTER_WAIT = 2
SETTLE_BATCH = 20  # posts judged per run, so a backlog can't flood the API


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("forums: %s failed", fn.__name__)
    return wrapper


class Forums(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.settle.start()

    async def cog_unload(self) -> None:
        self.settle.cancel()

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def ours(self, guild) -> bool:
        return guild is not None and guild.id == self.bot.settings.guild_id

    @staticmethod
    def kind_of(thread) -> str | None:
        """GUIDES or SETUPS when `thread` is a post in one of the two forums, else None. The
        parent must really be a forum: a text channel that happens to share the name is not."""
        parent = getattr(thread, "parent", None)
        kind = F.kind_of(getattr(parent, "name", None))
        if kind is None or parent not in getattr(thread.guild, "forums", []):
            return None
        return kind

    @staticmethod
    async def pause(seconds: float) -> None:
        await asyncio.sleep(seconds)

    # ------------------------------------------------------------ new posts
    @commands.Cog.listener()
    @never_raise
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if not self.ours(thread.guild):
            return
        kind = self.kind_of(thread)
        if kind is None:
            return
        author_id = thread.owner_id
        me = getattr(self.bot, "user", None)
        author = thread.guild.get_member(author_id) if author_id else None
        if author_id is None or (me is not None and author_id == me.id) or getattr(author, "bot", False):
            return
        await self.react(thread, kind)
        if not await self.db.tracking_allowed(author_id):
            return  # /privacy off: the post is theirs, the bot keeps no record of it
        await self.db.execute(
            "INSERT OR IGNORE INTO forum_posts (thread_id, kind, author_id, title, created_at) VALUES (?, ?, ?, ?, ?)",
            (thread.id, kind, author_id, (thread.name or "")[:100], now()))

    async def react(self, thread, kind: str) -> None:
        starter = thread.get_partial_message(thread.id)  # a forum post's starter shares its id
        for attempt in range(STARTER_RETRIES):
            try:
                await starter.add_reaction(F.REACTIONS[kind])
                return
            except discord.NotFound:
                if attempt == STARTER_RETRIES - 1:
                    log.info("forums: post %s has no starter message to react to", thread.id)
                    return
                await self.pause(STARTER_WAIT)
            except discord.HTTPException:
                log.warning("forums: couldn't react to post %s", thread.id, exc_info=True)
                return

    @commands.Cog.listener()
    @never_raise
    async def on_raw_thread_delete(self, payload) -> None:
        # Deleted before it was judged: it will never pay. (A paid post stays paid; by then it
        # had been up for a day.)
        await self.db.execute(
            "UPDATE forum_posts SET state = ?, settled_at = ? WHERE thread_id = ? AND state = ?",
            (F.DELETED, now(), payload.thread_id, F.PENDING))

    # ------------------------------------------------------------ rewards
    @tasks.loop(minutes=10)
    async def settle(self) -> None:
        try:
            await self.run_settle()
        except Exception:
            log.exception("forums: settling posts failed")

    @settle.before_loop
    async def before_settle(self) -> None:
        await self.bot.wait_until_ready()

    def can_read_posts(self) -> bool:
        intents = getattr(self.bot, "intents", None)
        return intents is None or bool(intents.message_content)

    async def run_settle(self) -> None:
        """Judge every pending post that has been up for SURVIVE. A post Discord wouldn't
        show us stays pending and is tried again, until GIVE_UP."""
        guild = self.guild()
        if guild is None:
            return
        t = now()
        rows = await self.db.fetchall(
            "SELECT * FROM forum_posts WHERE state = ? AND created_at <= ? ORDER BY created_at LIMIT ?",
            (F.PENDING, t - F.SURVIVE, SETTLE_BATCH))
        readable = self.can_read_posts()
        for row in rows:
            state = None
            try:
                if readable:
                    state = await self.judge(guild, row, t)
            except discord.HTTPException:
                log.warning("forums: couldn't read post %s; trying again later", row["thread_id"], exc_info=True)
            except Exception:
                log.exception("forums: judging post %s failed", row["thread_id"])
            if state is None and t - row["created_at"] >= F.SURVIVE + F.GIVE_UP:
                await self.finish(row["thread_id"], F.SKIPPED, t)

    async def finish(self, thread_id: int, state: str, t: int, title: str | None = None) -> None:
        await self.db.execute(
            "UPDATE forum_posts SET state = ?, settled_at = ?, title = COALESCE(?, title)"
            " WHERE thread_id = ? AND state = ?", (state, t, title, thread_id, F.PENDING))

    async def judge(self, guild, row, t: int) -> str | None:
        """Settle one post and return its new state (None: nothing decided, try again)."""
        tid, author_id = row["thread_id"], row["author_id"]
        thread = guild.get_thread(tid)
        try:
            if thread is None:  # archived posts drop out of the cache
                thread = await guild.fetch_channel(tid)
            starter = await thread.fetch_message(tid)
        except discord.NotFound:
            # The post or its first message is gone: nothing to reward.
            await self.finish(tid, F.DELETED, t)
            return F.DELETED
        except discord.Forbidden:
            await self.finish(tid, F.SKIPPED, t)  # hidden from the bot since it was posted
            return F.SKIPPED
        if self.kind_of(thread) != row["kind"] or starter.author.id != author_id:
            await self.finish(tid, F.SKIPPED, t)
            return F.SKIPPED
        images = sum(F.is_image(a.filename, a.content_type) for a in starter.attachments)
        title = (thread.name or "")[:100] or None
        async with self.db.transaction() as tx:
            if await tx.fetchone("SELECT 1 FROM privacy_optout WHERE user_id = ?", (author_id,)):
                # Opted out since posting: drop the record instead of settling it.
                await tx.execute("DELETE FROM forum_posts WHERE thread_id = ?", (tid,))
                return F.DELETED
            paid = await tx.fetchone(
                "SELECT COUNT(*) AS n FROM forum_posts WHERE author_id = ? AND state = ? AND settled_at > ?",
                (author_id, F.PAID, t - F.WEEK))
            state = F.decide(row["kind"], starter.content, images, author_id=author_id,
                             created_at=row["created_at"], locked=bool(getattr(thread, "locked", False)),
                             paid_this_week=paid["n"])
            if state == F.PAID:
                result = await economy.apply_tx(tx, author_id, F.REWARD, F.REASON, t, ref=F.ref(tid))
                if not result.ok:  # already in the ledger: never pay twice, but the row is done
                    log.warning("forums: post %s was already paid", tid)
            await tx.execute(
                "UPDATE forum_posts SET state = ?, settled_at = ?, title = COALESCE(?, title)"
                " WHERE thread_id = ? AND state = ?", (state, t, title, tid, F.PENDING))
        if state == F.PAID:
            log.info("forums: paid %d coins for %s post %s", F.REWARD, row["kind"], tid)
        return state

    # ------------------------------------------------------------ recap hook
    async def fresh_line(self, start: int, end: int) -> str | None:
        """One line about posts made in [start, end), for the weekly recap; None if there were
        none. Deleted and thin posts are left out, and so is anyone who has opted out since."""
        marks = ", ".join("?" for _ in F.LISTED)
        rows = await self.db.fetchall(
            f"SELECT kind, thread_id, title, created_at FROM forum_posts WHERE created_at >= ? AND created_at < ?"
            f" AND state IN ({marks}) AND author_id NOT IN (SELECT user_id FROM privacy_optout)",
            (start, end, *F.LISTED))
        return F.fresh_line([F.Post(r["kind"], r["thread_id"], r["title"], r["created_at"]) for r in rows])


async def setup(bot) -> None:
    await bot.add_cog(Forums(bot))
