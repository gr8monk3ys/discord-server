"""Community autopilot touches (rules in logic/vibes.py).

- Chat revival: when 💬・general has had no member message for 3 hours between 10:00 and
  23:00 local, one conversation starter from the QOTD bank (data/qotd.txt, shared no-repeat
  table `qotd_used`), at most once per 6 hours and never twice without a member message in
  between. The last member message time lives in meta, and a history scan on startup covers
  messages sent while the bot was down.
- Join anniversaries: 11:00 local, one shout-out in general for members whose server
  anniversary (Discord's joined_at) is today, up to 10 a day, once per member per year
  (meta anniv_last:USER).
- Boosters: a thank-you in general when a boost starts (once per boost, meta
  boost_thanked:USER:SINCE) with 1000 coins (ref boost:USER:YYYY-MM, so once a month at
  most); boosts missed while the bot was down are caught by a sweep of recent boosters.
  The 1st at 12:00 local: a 500-coin stipend for every booster (ref booststipend:YYYY-MM:USER).
- Member of the month: the 1st at 12:30 local, the member with the most counted voice hours
  + messages/50 + squads joined*2 last month (opted-out members, bots and Keeper/Moderator
  excluded; a squad is someone else's post, and only established accounts count as squad
  hosts or voice company, so alts can't pad a score) is announced in 📣・announcements with 1000 coins (ref motm:YYYY-MM).

Coins skip accounts younger than quests.MIN_ACCOUNT_DAYS. Scheduled jobs follow
logic/schedule.py semantics. Needs the Server Members intent (members, joined_at, boosts).
"""

import functools
import logging
import random
import time

import discord
from discord.ext import commands, tasks

import config
import economy
from cogs.lfg import ping_only
from logic import engagement as E
from logic import quests as Q
from logic import recap as R
from logic import shop
from logic import stats as S
from logic import vibes as V

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
STARTED_KEY = "vibes:started"  # meta: boosts before this aren't caught up by the sweep
LAST_MSG_KEY = "vibes:last_member_msg"  # meta: last member message in general (unix)
LAST_REVIVAL_KEY = "vibes:last_revival"  # meta: last conversation starter (unix)
ANNIV_KEY = "anniv_last:"  # meta: anniv_last:<user> -> last year celebrated
BOOST_KEY = "boost_thanked:{}:{}"  # meta: one thank-you per boost start
BOOST_CATCHUP = 2 * V.DAY  # the sweep thanks boosts this recent that no event caught
HISTORY_SCAN = 50
MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


def now() -> int:
    return int(time.time())


def ts(dt) -> int | None:
    return int(dt.timestamp()) if dt is not None else None


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("vibes: %s failed", fn.__name__)
    return wrapper


class Vibes(commands.Cog):
    def __init__(self, bot, questions: list[str] | None = None, rng: random.Random | None = None):
        self.bot = bot
        self.rng = rng or random.Random()
        self.questions = questions if questions is not None else self._load_questions()
        self.last_member_at: int | None = None
        self.saved_last: int | None = None  # what meta holds, so tick writes only on change
        self.loaded = False
        self.thanked: set[str] = set()  # boost keys already claimed: the sweep skips them

    @staticmethod
    def _load_questions() -> list[str]:
        try:
            return E.load_questions()
        except OSError:
            log.exception("vibes: couldn't read the question bank")
            return []

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
    def channel(guild, name: str):
        return config.match_by_name(guild.text_channels, name) if guild else None

    async def meta(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_meta(self, key: str, value) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    async def started_at(self) -> int:
        await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (STARTED_KEY, str(now())))
        return int(await self.meta(STARTED_KEY))

    # ------------------------------------------------------------ scheduling
    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        jobs = (
            ("chat revival", self.revive),
            ("boost sweep", self.sweep_boosts),
            ("anniversaries", lambda: self.run(V.ANNIV_JOB, E.plan_daily, self.anniversaries)),
            ("booster stipend", lambda: self.run(V.STIPEND_JOB, R.plan_monthly, self.stipend)),
            ("member of the month", lambda: self.run(V.MOTM_JOB, R.plan_monthly, self.member_of_month)),
        )
        for name, job in jobs:
            try:
                await job()
            except Exception:
                log.exception("vibes: %s failed", name)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def due(self, name: str, make_plan):
        """Record first_seen and skipped periods; return the period to run now, if any."""
        seen_key = f"first_seen:{name}"
        first_seen = await self.meta(seen_key)
        first_seen = int(first_seen) if first_seen else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{name}:%",))}
        t = now()
        todo = make_plan(t, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        return todo.run

    async def run(self, job, planner, action) -> None:
        period = await self.due(job.name, lambda t, done, seen: planner(job, t, self.tz, done, seen))
        # Marked done only after it worked: a failure retries on the next tick.
        if period is not None and await action(period):
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now()))

    # ------------------------------------------------------------ chat revival
    def is_general(self, channel) -> bool:
        return (getattr(channel, "parent", None) is None
                and config.match_by_name([channel], config.GENERAL_CHANNEL) is not None)

    @commands.Cog.listener()
    @never_raise
    async def on_message(self, message) -> None:
        if message.author.bot or not self.ours(message.guild) or not self.is_general(message.channel):
            return
        if getattr(message, "type", discord.MessageType.default) not in MESSAGE_TYPES:
            return
        self.last_member_at = max(self.last_member_at or 0, now())

    async def latest_in_history(self, channel) -> int | None:
        """The newest member message in the channel's recent history (messages sent while down)."""
        try:
            async for m in channel.history(limit=HISTORY_SCAN):
                if not m.author.bot:
                    return ts(m.created_at)
        except Exception as e:  # no Read Message History: meta still works
            log.info("vibes: couldn't scan %s history (%s)", config.GENERAL_CHANNEL, type(e).__name__)
        return None

    async def load_last(self, channel) -> None:
        saved = await self.meta(LAST_MSG_KEY)
        self.saved_last = int(saved) if saved else None
        found = [t for t in (self.saved_last, await self.latest_in_history(channel), self.last_member_at)
                 if t is not None]
        # Nothing known yet: start the quiet clock now rather than posting the minute we boot.
        self.last_member_at = max(found) if found else now()
        self.loaded = True

    async def revive(self) -> bool:
        """Post a conversation starter if general is quiet. True when one was posted."""
        channel = self.channel(self.guild(), config.GENERAL_CHANNEL)
        if channel is None:
            return False
        if not self.loaded:
            await self.load_last(channel)
        if self.last_member_at != self.saved_last:
            await self.set_meta(LAST_MSG_KEY, self.last_member_at)
            self.saved_last = self.last_member_at
        last_revival = await self.meta(LAST_REVIVAL_KEY)
        t = now()
        if not self.questions or not V.should_revive(t, self.tz, self.last_member_at,
                                                     int(last_revival) if last_revival else None):
            return False
        used = {r["qid"] for r in await self.db.fetchall("SELECT qid FROM qotd_used")}
        last = await self.db.fetchone("SELECT qid FROM qotd_used ORDER BY used_at DESC, rowid DESC LIMIT 1")
        picked = E.pick_next(len(self.questions), used, self.rng, last["qid"] if last else None)
        # Claimed before sending: a failing channel waits out the 6-hour gap, not one try a minute.
        await self.set_meta(LAST_REVIVAL_KEY, t)
        try:
            await channel.send(V.revival_text(self.questions[picked.index]), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("vibes: couldn't post the conversation starter", exc_info=True)
            return False
        async with self.db.transaction() as tx:
            if picked.reset:
                await tx.execute("DELETE FROM qotd_used")
            await tx.execute("INSERT OR REPLACE INTO qotd_used (qid, used_at) VALUES (?, ?)", (picked.index, t))
        log.info("vibes: chat revival (#%d)", picked.index)
        return True

    # ------------------------------------------------------------ join anniversaries
    async def anniversaries(self, period: E.DailyPeriod) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        day = period.day
        members = {m.id: m for m in guild.members if not m.bot and m.joined_at is not None}
        joined = [(uid, m.joined_at.astimezone(self.tz).date()) for uid, m in members.items()]
        rows = await self.db.fetchall("SELECT key, value FROM meta WHERE key LIKE ?", (ANNIV_KEY + "%",))
        done = {}
        for r in rows:
            try:
                done[int(r["key"][len(ANNIV_KEY):])] = int(r["value"])
            except ValueError:
                continue
        people = V.anniversaries(joined, day, done)
        if not people:
            return True
        channel = self.channel(guild, config.GENERAL_CHANNEL)
        if channel is None:
            log.warning("anniversaries %s: no %s channel", period.key, config.GENERAL_CHANNEL)
            return True
        chosen = [members[uid] for uid, _ in people]
        text = V.anniversary_text([(m.mention, years) for m, (_, years) in zip(chosen, people)])
        await channel.send(text, allowed_mentions=ping_only(users=chosen))
        async with self.db.transaction() as tx:
            for uid, _ in people:
                await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                 (f"{ANNIV_KEY}{uid}", str(day.year)))
        log.info("anniversaries %s: %s", period.key, [uid for uid, _ in people])
        return True

    # ------------------------------------------------------------ boosters
    @commands.Cog.listener()
    @never_raise
    async def on_member_update(self, before, after) -> None:
        if after.bot or not self.ours(after.guild):
            return
        if V.boost_started(getattr(before, "premium_since", None), getattr(after, "premium_since", None)):
            await self.thank_boost(after)

    async def thank_boost(self, member) -> bool:
        """Thank and pay once per boost start. True when this call did it."""
        key = BOOST_KEY.format(member.id, ts(member.premium_since))
        self.thanked.add(key)
        t = now()
        if not await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, str(t))):
            return False  # already thanked (the event and the sweep both saw it)
        coins = 0
        if V.old_enough(ts(getattr(member, "created_at", None)), t):
            result = await economy.apply(self.db, member.id, V.BOOST_COINS, V.BOOST_REASON, t,
                                         ref=V.boost_ref(member.id, shop.month_key(t, self.tz)))
            coins = V.BOOST_COINS if result.ok else 0  # a second boost this month: thanks, no coins
        log.info("vibes: %s boosted (%d coins)", member.id, coins)
        channel = self.channel(self.guild(), config.GENERAL_CHANNEL)
        if channel is None:
            log.info("vibes: no %s channel to thank a booster in", config.GENERAL_CHANNEL)
            return True
        try:
            await channel.send(V.boost_text(member.mention, coins), allowed_mentions=ping_only(users=[member]))
        except discord.HTTPException:
            log.warning("vibes: couldn't thank booster %s", member.id, exc_info=True)
        return True

    async def sweep_boosts(self) -> int:
        """Thank recent boosts that no event delivered (the bot was down). Returns how many."""
        guild = self.guild()
        if guild is None:
            return 0
        started, t, n = await self.started_at(), now(), 0
        for m in list(getattr(guild, "premium_subscribers", [])):
            since = ts(getattr(m, "premium_since", None))
            if m.bot or since is None or since < started or t - since > BOOST_CATCHUP:
                continue
            if BOOST_KEY.format(m.id, since) in self.thanked:
                continue
            n += await self.thank_boost(m)
        return n

    async def stipend(self, period: R.MonthPeriod) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        month = shop.month_key(period.scheduled_at, self.tz)  # the month being paid for
        t, paid = now(), 0
        for m in list(getattr(guild, "premium_subscribers", [])):
            if m.bot or getattr(m, "premium_since", None) is None or not V.old_enough(ts(m.created_at), t):
                continue
            result = await economy.apply(self.db, m.id, V.STIPEND_COINS, V.STIPEND_REASON, t,
                                         ref=V.stipend_ref(month, m.id))
            paid += result.ok
        log.info("booster stipend %s: paid %d", month, paid)
        return True

    # ------------------------------------------------------------ member of the month
    async def activity(self, guild, start: int, end: int, month: str):
        hidden = {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}
        bots = {m.id for m in guild.members if m.bot}
        rows = await self.db.fetchall('SELECT user_id, channel_id AS k, start, "end" FROM voice_sessions'
                                      ' WHERE start < ? AND ("end" IS NULL OR "end" > ?)', (end, start))
        afk = getattr(guild, "afk_channel", None)
        sessions = [S.Session(r["user_id"], r["k"], r["start"], r["end"]) for r in rows]
        fresh = {s.user_id for s in sessions if not Q.established(s.user_id, end)}  # alts aren't company
        voice = S.counted_voice_seconds(sessions, start, end, now(), excluded_channels={afk.id} if afk else set(),
                                        bot_ids=bots | fresh)
        rows = await self.db.fetchall("SELECT user_id, SUM(count) AS n FROM message_counts WHERE day LIKE ?"
                                      " GROUP BY user_id", (f"{month}-%",))
        messages = {r["user_id"]: r["n"] for r in rows}
        # Joins of someone else's post (the host's own row isn't a join) by an established host.
        rows = await self.db.fetchall(
            "SELECT m.user_id, COUNT(DISTINCT m.post_id) AS n FROM lfg_members m JOIN lfg_posts p ON p.id = m.post_id"
            " WHERE m.user_id != p.host_id AND m.joined_at >= ? AND m.joined_at < ?"
            f" AND {Q.established_sql('p.host_id', 'p.created_at')}"
            " GROUP BY m.user_id", (start, end))
        squads = {r["user_id"]: r["n"] for r in rows}
        skip = hidden | bots
        return tuple({u: v for u, v in d.items() if u not in skip} for d in (voice, messages, squads))

    def eligible(self, guild, uid: int, t: int) -> bool:
        m = guild.get_member(uid)
        return (m is not None and not m.bot and not V.is_staff(r.name for r in m.roles)
                and V.old_enough(ts(m.created_at), t))

    async def member_of_month(self, period: R.MonthPeriod) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        month, t = period.month, now()
        voice, messages, squads = await self.activity(guild, period.window_start, period.window_end, month)
        paid = await self.db.fetchone("SELECT user_id FROM ledger WHERE ref = ?", (V.motm_ref(month),))
        if paid is not None:
            winner = paid["user_id"]  # a retry after a failed announcement: same winner
        else:
            winner = V.pick_winner(V.scores(voice, messages, squads), lambda u: self.eligible(guild, u, t))
            if winner is None:
                log.info("member of the month %s: nobody qualified", month)
                return True
            await economy.apply(self.db, winner, V.MOTM_COINS, V.MOTM_REASON, t, ref=V.motm_ref(month))
        log.info("member of the month %s: %s", month, winner)
        channel = self.channel(guild, config.ANNOUNCEMENTS_CHANNEL)
        if channel is None:
            log.warning("member of the month %s: no %s channel", month, config.ANNOUNCEMENTS_CHANNEL)
            return True
        member = guild.get_member(winner) or discord.Object(winner)
        text = V.motm_text(f"<@{winner}>", month, voice.get(winner, 0), messages.get(winner, 0),
                           squads.get(winner, 0), V.MOTM_COINS)
        await channel.send(text, allowed_mentions=ping_only(users=[member]))
        return True


async def setup(bot) -> None:
    await bot.add_cog(Vibes(bot))
