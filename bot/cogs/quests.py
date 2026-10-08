"""Starter quest: five first steps for new members (pick roles, say hi in general,
join a squad, claim /daily, hop in voice), stored in `quest_steps`, shown with /quest.

Steps are picked up from events (a message in general, a role change, joining voice,
and a re-check shortly after any slash command or button, which covers /daily and the
LFG buttons) and from an hourly sweep over the tables, so a missed event or a restart
can't lose one. A member who joined after the module went live (meta key set on the
first run) and finishes within 30 days of joining is paid once (ledger ref
quest:USERID) and congratulated in general. A day after joining, members with fewer
than 3 steps get one DM with their checklist.

/privacy off: saying hi and joining voice are activity tracking, so they aren't
recorded for opted-out members; those two steps are skipped and the other three
finish the quest."""

import asyncio
import functools
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from cogs.lfg import ping_only
from logic import achievements as A
from logic import quests as Q

log = logging.getLogger(__name__)

STARTED_KEY = "quests:started"  # meta: when the module first ran; earlier joiners aren't paid
NUDGE_KEY = "quests:nudged:{}"  # meta: one nudge DM per member, ever
EVENT_DELAY = 5  # seconds: let /daily or the LFG button write its row first
MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("quests: %s failed", fn.__name__)
    return wrapper


def joined_ts(member) -> int | None:
    joined = getattr(member, "joined_at", None)
    return int(joined.timestamp()) if joined is not None else None


def created_ts(member) -> int | None:
    created = getattr(member, "created_at", None)
    return int(created.timestamp()) if created is not None else None


class Quests(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.delay = EVENT_DELAY
        self.pending: dict[int, asyncio.Task] = {}
        self.said_hi: set[int] = set()  # already recorded: skip the write on every message

    async def cog_load(self) -> None:
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.sweep.cancel()
        for task in self.pending.values():
            task.cancel()
        self.pending.clear()

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def ours(self, guild) -> bool:
        return guild is not None and guild.id == self.bot.settings.guild_id

    # ------------------------------------------------------------ state
    async def started_at(self) -> int:
        await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (STARTED_KEY, str(now())))
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (STARTED_KEY,))
        return int(row["value"])

    async def done(self, user_id: int) -> set[str]:
        rows = await self.db.fetchall("SELECT step FROM quest_steps WHERE user_id = ?", (user_id,))
        return {r["step"] for r in rows}

    async def paid(self, user_id: int) -> bool:
        return await self.db.fetchone("SELECT 1 FROM ledger WHERE ref = ?", (Q.ref(user_id),)) is not None

    async def optouts(self) -> set[int]:
        return {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}

    # ------------------------------------------------------------ detection
    async def from_tables(self, ids=None) -> dict[int, set[str]]:
        """Steps the other modules' tables show (None: everyone). Voice skips opted-out members."""
        if ids is not None:
            ids = tuple(ids)
            if not ids:
                return {}
            extra = f" AND user_id IN ({','.join('?' * len(ids))})"
        else:
            ids, extra = (), ""
        queries = {
            "join_squad": "SELECT DISTINCT user_id FROM lfg_members WHERE 1 = 1",
            "claim_daily": "SELECT user_id FROM wallets WHERE last_daily IS NOT NULL",
            "join_voice": "SELECT DISTINCT user_id FROM voice_sessions"
                          " WHERE user_id NOT IN (SELECT user_id FROM privacy_optout)",
        }
        found: dict[int, set[str]] = {}
        for step, sql in queries.items():
            for r in await self.db.fetchall(sql + extra, ids):
                found.setdefault(r["user_id"], set()).add(step)
        return found

    @staticmethod
    def from_roles(member) -> set[str]:
        names = [getattr(r, "name", "") for r in getattr(member, "roles", ())]
        return {"pick_roles"} if Q.has_picked_roles(names) else set()

    # ------------------------------------------------------------ recording
    async def record(self, member, steps, done: set[str] | None = None) -> set[str]:
        """Insert these steps once each; on anything new, see whether the quest is finished.
        `done`: steps already recorded (the sweep preloads them), so nothing is written
        when nothing is new."""
        if done is not None:
            steps = {s for s in steps if s in Q.BY_KEY} - done
            if not steps:
                return set()
        t = now()
        fresh = set()
        async with self.db.transaction() as tx:
            for step in sorted(steps):
                if step not in Q.BY_KEY:
                    continue
                cur = await tx.execute("INSERT OR IGNORE INTO quest_steps (user_id, step, at) VALUES (?, ?, ?)",
                                       (member.id, step, t))
                if cur.rowcount:
                    fresh.add(step)
        if fresh:
            log.info("quests: %s did %s", member.id, ", ".join(sorted(fresh)))
            await self.maybe_finish(member)
        return fresh

    async def maybe_finish(self, member) -> bool:
        """Pay and congratulate once, if finished and eligible. True when paid now."""
        uid = member.id
        tracking = await self.db.tracking_allowed(uid)
        if not Q.is_complete(await self.done(uid), tracking):
            return False
        t = now()
        # The badge is for finishing; only the coins depend on eligibility.
        badge = False
        if Q.BADGE_KEY in A.BY_KEY:
            badge = bool(await self.db.execute(
                "INSERT OR IGNORE INTO achievements (user_id, key, at) VALUES (?, ?, ?)", (uid, Q.BADGE_KEY, t)))
        if not Q.eligible_for_reward(joined_ts(member), await self.started_at(), t, created_at=created_ts(member)):
            if badge:
                log.info("quests: %s finished the starter quest (badge only, no coin reward)", uid)
            return False
        result = await economy.apply(self.db, uid, Q.REWARD, Q.REASON, t, ref=Q.ref(uid))
        if not result.ok:
            return False  # already paid
        log.info("quests: %s finished the starter quest", uid)
        guild = self.guild()
        general = config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL) if guild else None
        if general is None:
            log.info("quests: no %s channel to congratulate in", config.GENERAL_CHANNEL)
            return True
        try:
            await general.send(Q.congrats(f"<@{uid}>", Q.REWARD, badge),
                               allowed_mentions=ping_only(users=[discord.Object(uid)]))
        except Exception:
            log.exception("quests: congratulating %s failed", uid)
        return True

    async def check(self, member) -> set[str]:
        """Re-read everything we can see for one member."""
        steps = self.from_roles(member) | (await self.from_tables({member.id})).get(member.id, set())
        return await self.record(member, steps)

    def soon(self, member) -> None:
        if member.id in self.pending:
            return

        async def later():
            try:
                await asyncio.sleep(self.delay)
                await self.check(member)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("quests: checking %s failed", member.id)
            finally:
                self.pending.pop(member.id, None)

        self.pending[member.id] = asyncio.create_task(later())

    async def drain(self) -> None:
        while self.pending:
            await asyncio.gather(*list(self.pending.values()), return_exceptions=True)

    # ------------------------------------------------------------ events
    @commands.Cog.listener()
    @never_raise
    async def on_message(self, message) -> None:
        author = message.author
        if author.bot or not self.ours(message.guild) or author.id in self.said_hi:
            return
        if getattr(message, "type", discord.MessageType.default) not in MESSAGE_TYPES:
            return
        if config.match_by_name([message.channel], config.GENERAL_CHANNEL) is None:
            return
        if not await self.db.tracking_allowed(author.id):
            return
        member = message.guild.get_member(author.id) or author
        await self.record(member, {"say_hi"})
        self.said_hi.add(author.id)

    @commands.Cog.listener()
    @never_raise
    async def on_member_update(self, before, after) -> None:
        if after.bot or not self.ours(after.guild):
            return
        if {r.id for r in before.roles} == {r.id for r in after.roles}:
            return
        if self.from_roles(after):
            await self.record(after, {"pick_roles"})

    @commands.Cog.listener()
    @never_raise
    async def on_voice_state_update(self, member, before, after) -> None:
        if member.bot or not self.ours(member.guild) or after.channel is None or before.channel == after.channel:
            return
        afk = getattr(member.guild, "afk_channel", None)
        if afk is not None and after.channel.id == afk.id:
            return
        if not await self.db.tracking_allowed(member.id):
            return
        await self.record(member, {"join_voice"})

    @commands.Cog.listener()
    @never_raise
    async def on_interaction(self, interaction) -> None:
        # Fires as the interaction arrives; the delay lets /daily or the LFG button finish.
        user = interaction.user
        if user is None or getattr(user, "bot", False) or not self.ours(interaction.guild):
            return
        self.soon(interaction.guild.get_member(user.id) or user)

    # ------------------------------------------------------------ hourly sweep
    @tasks.loop(hours=1)
    async def sweep(self) -> None:
        try:
            await self.run_sweep()
        except Exception:
            log.exception("quests sweep failed")

    @sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def run_sweep(self) -> int:
        """Record steps from roles and tables for every member, then send due nudges.
        Returns nudges sent."""
        guild = self.guild()
        if guild is None:
            return 0
        started = await self.started_at()
        tables = await self.from_tables()
        optouts = await self.optouts()
        # Preloaded once, so members with nothing new cost no queries or commits.
        done_by: dict[int, set[str]] = {}
        for r in await self.db.fetchall("SELECT user_id, step FROM quest_steps"):
            done_by.setdefault(r["user_id"], set()).add(r["step"])
        badge_known = Q.BADGE_KEY in A.BY_KEY
        holders = {r["user_id"] for r in await self.db.fetchall(
            "SELECT user_id FROM achievements WHERE key = ?", (Q.BADGE_KEY,))} if badge_known else set()
        nudged = 0
        for member in list(guild.members):
            if member.bot:
                continue
            try:
                tracking = member.id not in optouts
                done = done_by.get(member.id, set())
                fresh = await self.record(member, self.from_roles(member) | tables.get(member.id, set()), done)
                done = done | fresh
                if (not fresh and badge_known and member.id not in holders
                        and Q.is_complete(done, tracking)):
                    await self.maybe_finish(member)  # finished before the badge came with it
                nudged += await self.nudge(member, started, tracking, done)
            except Exception:
                log.exception("quests: sweeping %s failed", member.id)
        return nudged

    async def nudge(self, member, started: int, tracking: bool, done: set[str] | None = None) -> bool:
        t = now()
        if done is None:
            done = await self.done(member.id)
        joined = joined_ts(member)
        if not Q.should_nudge(joined, started, t, Q.progress(done, tracking)[0]):
            return False
        # Claim the nudge before sending, so a crash mid-send can't DM twice.
        if not await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                                     (NUDGE_KEY.format(member.id), str(t))):
            return False
        try:
            await member.send(Q.nudge_text(done, tracking, rewarded=Q.eligible_for_reward(joined, started, t, created_at=created_ts(member))),
                              allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:  # DMs closed: fine, they can still run /quest
            log.info("quests: nudge DM to %s failed (%s)", member.id, type(e).__name__)
            return False
        return True

    # ------------------------------------------------------------ /quest
    async def quest_embed(self, member) -> discord.Embed:
        uid = member.id
        tracking = await self.db.tracking_allowed(uid)
        done = await self.done(uid)
        got, total = Q.progress(done, tracking)
        if await self.paid(uid):
            status = "reward paid"
        else:
            args = (joined_ts(member), await self.started_at(), now())
            if Q.eligible_for_reward(*args, created_at=created_ts(member)):
                status = f"{Q.REWARD:,} coins when you finish"
            else:
                status = f"no coin reward ({Q.no_reward_reason(*args, created_at=created_ts(member))})"
        title = "Starter quest · done!" if got == total else f"Starter quest · {got}/{total}"
        return style.embed(title=title, description=Q.checklist(done, tracking),
                           footer=style.label("quest", status))

    @app_commands.command(name="quest", description="Your starter quest checklist")
    @app_commands.guild_only()
    async def quest(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        try:
            await self.check(member)  # pick up anything done since the last sweep
        except Exception:
            log.exception("quests: /quest refresh for %s failed", member.id)
        await interaction.response.send_message(embed=await self.quest_embed(member), ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Quests(bot))
