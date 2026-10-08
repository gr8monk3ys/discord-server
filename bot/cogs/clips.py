"""Module 3: Clip of the week. Collects clips posted in 📸・clips (and its threads), runs
a native poll every Sunday at 18:05 local, and hands the winner the Clip of the Week role.

Needs the Message Content intent (to see links) and, in 📸・clips: View Channel, Send
Messages, Read Message History (reaction counts, poll results). The role swap needs
Manage Roles with Front Desk's role above Clip of the Week."""

import json
import logging
import time
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

import config
from logic import clips as L
from logic import quests as Q
from logic.selfroles import has_dangerous_permissions
from logic.schedule import Weekly, plan

log = logging.getLogger(__name__)

DAY = 24 * 60 * 60
POLL_HOURS = 24
CLIP_JOB = Weekly("clips", weekday=6, hour=18, minute=5)  # Sundays 18:05 local
QUESTION = "Clip of the week?"
ROLE_NOTE_KEY = "clips:role_note_day"  # meta: local day of the last "can't manage the role" note


def now() -> int:
    return int(time.time())


def entries_key(week: str) -> str:
    """meta key holding the poll's entries (answer order -> clip), so a restart can read results."""
    return f"clip_poll:{week}"


def thread_key(message_id: int) -> str:
    """meta key for a clip posted in a thread: the clips table has no channel column."""
    return f"clip_channel:{message_id}"


class Clips(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.weekly.start()
        self.winners.start()

    async def cog_unload(self) -> None:
        self.weekly.cancel()
        self.winners.cancel()

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def clips_channel(self, guild):
        return config.match_by_name(guild.text_channels, config.CLIPS_CHANNEL) if guild else None

    @staticmethod
    def in_clips(channel) -> bool:
        """The clips channel itself or a thread in it."""
        for ch in (channel, getattr(channel, "parent", None)):
            if ch is not None and getattr(ch, "name", None) is not None \
                    and config.match_by_name([ch], config.CLIPS_CHANNEL) is not None:
                return True
        return False

    def jump(self, guild_id: int, channel_id: int, message_id: int) -> str:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    # ------------------------------------------------------------ collecting
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self.collect(message)
        except Exception:
            log.exception("clip collect failed")

    async def collect(self, message) -> None:
        if message.author.bot or message.guild is None or message.guild.id != self.bot.settings.guild_id:
            return
        if not self.in_clips(message.channel):
            return
        url = L.clip_url(message.content, [(a.url, a.content_type) for a in message.attachments])
        if url is None:
            return
        if not await self.db.tracking_allowed(message.author.id):
            return
        posted = int(message.created_at.timestamp())
        is_thread = getattr(message.channel, "parent", None) is not None
        async with self.db.transaction() as tx:
            await tx.execute("INSERT OR IGNORE INTO clips (message_id, user_id, url, posted_at) VALUES (?, ?, ?, ?)",
                             (message.id, message.author.id, url, posted))
            if is_thread:
                await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                 (thread_key(message.id), str(message.channel.id)))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload) -> None:
        try:
            await self.forget([payload.message_id])
        except Exception:
            log.exception("clip delete failed")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload) -> None:
        try:
            await self.forget(list(payload.message_ids))
        except Exception:
            log.exception("clip bulk delete failed")

    async def forget(self, message_ids: list[int]) -> None:
        async with self.db.transaction() as tx:
            for mid in message_ids:
                await tx.execute("DELETE FROM clips WHERE message_id = ?", (mid,))
                await tx.execute("DELETE FROM meta WHERE key = ?", (thread_key(mid),))

    # ------------------------------------------------------------ role checks
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        try:
            guild = self.guild()
            if guild is not None:
                await self.usable_role(guild)
        except Exception:
            log.exception("clip role check failed")

    async def usable_role(self, guild):
        """The Clip of the Week role if Front Desk can hand it out, else None (and a note in
        🛡️・mod, at most once per local day)."""
        role = config.match_by_name(guild.roles, config.CLIP_ROLE)
        me = getattr(guild, "me", None)
        if role is not None and me is not None and me.top_role > role and not has_dangerous_permissions(role):
            return role
        problem = (f"There's no `{config.CLIP_ROLE}` role." if role is None else
                   f"`{config.CLIP_ROLE}` has moderator permissions, so it isn't handed out."
                   if has_dangerous_permissions(role) else
                   f"Front Desk's role has to be above `{config.CLIP_ROLE}` to hand it out.")
        await self.note_mods(guild, f"Clip of the week: {problem} Winners are still announced.")
        return None

    async def note_mods(self, guild, text: str) -> None:
        today = datetime.fromtimestamp(now(), self.tz).date().isoformat()
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (ROLE_NOTE_KEY,))
        if row is not None and row["value"] == today:
            return
        channel = config.match_by_name(guild.text_channels, config.MOD_CHANNEL)
        if channel is None:
            log.warning("%s (no %s channel)", text, config.MOD_CHANNEL)
            return
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (ROLE_NOTE_KEY, today))

    # ------------------------------------------------------------ weekly poll
    @tasks.loop(minutes=5)
    async def weekly(self) -> None:
        try:
            await self.run_weekly()
        except Exception:
            log.exception("clip of the week poll job failed")

    @weekly.before_loop
    async def before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    async def run_weekly(self) -> None:
        job = CLIP_JOB
        seen_key = f"first_seen:{job.name}"
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (seen_key,))
        first_seen = int(row["value"]) if row else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{job.name}:%",))}
        t = now()
        todo = plan(job, t, self.tz, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        if todo.run is not None:
            # Marked done only after posting: a Discord error retries on the next tick.
            await self.post_poll(todo.run)
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (todo.run.key, now()))

    async def clip_channel_id(self, message_id: int, default: int) -> int:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (thread_key(message_id),))
        return int(row["value"]) if row else default

    async def reaction_count(self, guild, channel_id: int, message_id: int) -> int | None:
        """Total reactions on a clip; None if the message is gone."""
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return 0
        try:
            msg = await channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            log.warning("couldn't fetch clip %s for reactions", message_id)
            return 0
        return sum(r.count for r in msg.reactions)

    async def post_poll(self, period) -> None:
        week = L.week_of(period.key)
        if await self.db.fetchone("SELECT 1 FROM clip_polls WHERE week = ?", (week,)):
            return  # posted before a crash/restart: don't post twice
        rows = await self.db.fetchall(
            "SELECT message_id, user_id, posted_at FROM clips WHERE posted_at >= ? AND posted_at < ?"
            " ORDER BY posted_at, message_id", (period.window_start, period.window_end))
        guild = self.guild()
        channel = self.clips_channel(guild)
        if len(rows) < 2 or channel is None:
            log.info("clip poll %s: %s", week, f"{len(rows)} clip(s), no poll" if len(rows) < 2
                     else f"no {config.CLIPS_CHANNEL} channel")
            return
        where = {r["message_id"]: await self.clip_channel_id(r["message_id"], channel.id) for r in rows}
        clips = []
        for r in rows:
            # Fetched even when every clip fits: it's also how a clip deleted while the
            # bot was off gets noticed, so it can't sit in the poll as a dead link.
            reactions = await self.reaction_count(guild, where[r["message_id"]], r["message_id"])
            if reactions is None:  # deleted while the bot was off
                await self.forget([r["message_id"]])
                continue
            clips.append(L.Clip(r["message_id"], r["user_id"], r["posted_at"], reactions))
        entries = L.poll_entries(clips)
        if len(entries) < 2:
            log.info("clip poll %s: fewer than 2 clips left, no poll", week)
            return

        poll = discord.Poll(question=QUESTION, duration=timedelta(hours=POLL_HOURS))
        lines = [f"**Clip of the week** · {week}", "Vote below. The poll closes in 24 hours.", ""]
        for n, c in enumerate(entries, start=1):
            member = guild.get_member(c.user_id)
            name = member.display_name if member else f"user {c.user_id}"
            poll.add_answer(text=L.answer_text(n, name))
            lines.append(f"`#{n}` {self.jump(guild.id, where[c.message_id], c.message_id)} · <@{c.user_id}>")
        message = await channel.send("\n".join(lines), poll=poll, allowed_mentions=discord.AllowedMentions.none())
        stored = [{"message_id": c.message_id, "channel_id": where[c.message_id], "user_id": c.user_id}
                  for c in entries]
        async with self.db.transaction() as tx:
            await tx.execute("INSERT OR IGNORE INTO clip_polls (week, message_id, ends_at) VALUES (?, ?, ?)",
                             (week, message.id, now() + POLL_HOURS * 3600))
            await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                             (entries_key(week), json.dumps(stored)))
        log.info("posted clip poll %s with %d clips", week, len(entries))

    # ------------------------------------------------------------ winner
    @tasks.loop(minutes=5)
    async def winners(self) -> None:
        try:
            await self.check_polls()
        except Exception:
            log.exception("clip of the week winner job failed")

    @winners.before_loop
    async def before_winners(self) -> None:
        await self.bot.wait_until_ready()

    async def check_polls(self) -> None:
        rows = await self.db.fetchall(
            "SELECT week, message_id, ends_at FROM clip_polls WHERE done = 0 AND ends_at <= ? ORDER BY ends_at",
            (now(),))
        for r in rows:
            try:
                await self.finish_poll(r["week"], r["message_id"], r["ends_at"])
            except Exception:
                log.exception("clip poll %s: winner check failed", r["week"])

    async def mark_poll(self, week: str, winner_id: int | None) -> None:
        await self.db.execute("UPDATE clip_polls SET done = 1, winner_id = ? WHERE week = ?", (winner_id, week))

    async def finish_poll(self, week: str, message_id: int, ends_at: int) -> None:
        guild = self.guild()
        channel = self.clips_channel(guild)
        late = L.give_up(now(), ends_at)
        message = None
        if channel is not None:
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                log.warning("clip poll %s: poll message is gone, no winner", week)
                await self.mark_poll(week, None)
                return
            except discord.HTTPException:
                log.warning("clip poll %s: couldn't fetch the poll message", week)
        poll = getattr(message, "poll", None)
        if poll is None or not poll.is_finalized():
            if late:
                log.warning("clip poll %s: results never arrived 48h after it ended, giving up", week)
                await self.mark_poll(week, None)
            return  # retry on the next tick

        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (entries_key(week),))
        entries = json.loads(row["value"]) if row else []
        try:
            votes = await self.counted_votes(poll, entries, ends_at)
        except discord.HTTPException:
            log.warning("clip poll %s: couldn't read the voters", week)
            if late:
                await self.mark_poll(week, None)
            return  # retry on the next tick
        index = L.winner_index(len(entries), votes)
        if index is None:
            log.info("clip poll %s: no votes, no winner", week)
            await self.mark_poll(week, None)
            return
        win = entries[index]
        winner_id = win["user_id"]

        role = await self.usable_role(guild)
        if role is not None:
            await self.swap_role(guild, role, winner_id)
        link = self.jump(guild.id, win["channel_id"], win["message_id"])
        await channel.send(f"🏆 Clip of the week ({week}): <@{winner_id}> with #{index + 1}. {link}",
                           allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                                    users=[discord.Object(winner_id)]))
        self.bot.dispatch("clip_of_the_week", week, winner_id)
        await self.mark_poll(week, winner_id)
        log.info("clip of the week %s: %s", week, winner_id)

    @staticmethod
    async def counted_votes(poll, entries: list[dict], ends_at: int) -> dict[int, int]:
        """Answer number -> votes from established accounts other than that clip's author,
        so alts (or a self-vote) can't hand someone the coins and the role."""
        votes = {}
        for answer in poll.answers:
            author = entries[answer.id - 1]["user_id"] if 0 < answer.id <= len(entries) else None
            n = 0
            async for voter in answer.voters():
                if not getattr(voter, "bot", False) and voter.id != author and Q.established(voter.id, ends_at):
                    n += 1
            votes[answer.id] = n
        return votes

    async def swap_role(self, guild, role, winner_id: int) -> None:
        reason = "Clip of the week"
        try:
            for member in list(role.members):
                if member.id != winner_id:
                    await member.remove_roles(role, reason=reason)
            winner = guild.get_member(winner_id)
            if winner is None:
                try:
                    winner = await guild.fetch_member(winner_id)
                except discord.NotFound:
                    log.info("clip of the week winner %s left the server, no role", winner_id)
                    return
            if role not in winner.roles:
                await winner.add_roles(role, reason=reason)
        except discord.HTTPException:
            log.exception("clip of the week role swap failed")
            await self.note_mods(guild, f"Clip of the week: couldn't move the `{config.CLIP_ROLE}` role "
                                        "(check Manage Roles). The winner was still announced.")


async def setup(bot) -> None:
    await bot.add_cog(Clips(bot))
