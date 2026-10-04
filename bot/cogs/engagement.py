"""Module 9: Engagement autopilot.

- Question of the day, 12:00 local in 💬・general, with a thread for answers. Picked from
  data/qotd.txt with no repeats until the bank runs out (`qotd_used`).
- "This or that" native poll, 18:00 local in 💬・general, open 24 h, from data/polls.txt
  (`poll_used`).
- 🔢・counting: only the next plain integer counts and nobody counts twice in a row. ✅ on a
  good count; ❌, a reset and a short note on a bad one. The best run is kept, and the
  Counting Champ role goes to whoever counted most in it.
- Weekly auto game night: Fridays at 12:00 local, if nobody has a game night that Friday
  evening, the bot creates a 21:00 event in 🎮 Squad for the most played server game of the
  past 7 days (or Anything). It is a normal `gamenights` row, so cogs/events.py reminds people.
- Birthdays: /birthday set|remove|list. 09:00 local: a shout-out in 💬・general, the Birthday
  role for 24 h and 250 coins (ref bday:<year>:<user>).

Every job follows logic/schedule.py semantics (latest due period only, first run only marks
it done, marked done after it worked). All state is in SQLite. Needs the Message Content
intent (counting) and the Server Members intent (role swaps, birthday lookups).
"""

import logging
import random
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from cogs.lfg import ping_only
from logic import engagement as E
from logic import stats as S
from logic.engagement import CountState, Daily
from logic.schedule import Weekly, plan

log = logging.getLogger(__name__)

DAY = 24 * 60 * 60
NO_PINGS = discord.AllowedMentions.none()
QOTD_JOB = Daily("qotd", 12, 0)
POLL_JOB = Daily("thisorthat", 18, 0)
BIRTHDAY_JOB = Daily("birthdays", 9, 0)
GAMENIGHT_JOB = Weekly("autogamenight", weekday=4, hour=12, minute=0)  # Fridays 12:00 local
POLL_HOURS = 24
POLL_QUESTION = "This or that?"
BIRTHDAY_COINS = 250
BIRTHDAY_ROLE_SECONDS = DAY
BIRTHDAY_LIST_SIZE = 5
CHAMP_KEY = "counting_champ"
ROLE_KEY = "bday_role:"  # meta: bday_role:<user> -> unix time the Birthday role comes off
LAST_KEY = "bday_last:"  # meta: bday_last:<user> -> last year celebrated (one party a year)
CHECK, CROSS = "✅", "❌"
LIVE_EVENTS = (discord.EventStatus.scheduled, discord.EventStatus.active)


def now() -> int:
    return int(time.time())


class Engagement(commands.Cog):
    birthday = app_commands.Group(name="birthday", description="Get a shout-out, a role and coins on your birthday",
                                  guild_only=True)

    def __init__(self, bot, questions: list[str] | None = None, pairs: list[tuple[str, str]] | None = None,
                 rng: random.Random | None = None):
        self.bot = bot
        self.rng = rng or random.Random()
        self.questions = questions if questions is not None else self._load(E.load_questions, "questions")
        self.pairs = pairs if pairs is not None else self._load(E.load_pairs, "poll pairs")
        self.warned: set[str] = set()  # one log line per stuck role removal, not one a minute

    @staticmethod
    def _load(loader, what: str) -> list:
        try:
            items = loader()
        except OSError:
            log.exception("couldn't read the %s bank", what)
            return []
        log.info("engagement: %d %s loaded", len(items), what)
        return items

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

    def general(self, guild):
        return config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL) if guild else None

    @staticmethod
    async def member(guild, user_id: int):
        """The member from the cache, else from the API; None if they left."""
        m = guild.get_member(user_id)
        if m is not None:
            return m
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None

    # ------------------------------------------------------------ scheduling
    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        jobs = (
            ("qotd", lambda: self.run_daily(QOTD_JOB, self.post_qotd)),
            ("poll", lambda: self.run_daily(POLL_JOB, self.post_poll)),
            ("birthdays", lambda: self.run_daily(BIRTHDAY_JOB, self.celebrate)),
            ("birthday roles", self.expire_birthday_roles),
            ("auto game night", self.run_gamenight),
        )
        for name, job in jobs:
            try:
                await job()
            except Exception:
                log.exception("engagement: %s failed", name)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def due(self, name: str, make_plan):
        """Record first_seen and skipped periods; return the period to run now, if any."""
        seen_key = f"first_seen:{name}"
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (seen_key,))
        first_seen = int(row["value"]) if row else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{name}:%",))}
        t = now()
        todo = make_plan(t, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        return todo.run

    async def finish(self, period, action) -> None:
        # Marked done only after it worked: an error retries on the next tick.
        if await action(period):
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now()))

    async def run_daily(self, job: Daily, action) -> None:
        period = await self.due(job.name, lambda t, done, seen: E.plan_daily(job, t, self.tz, done, seen))
        if period is not None:
            await self.finish(period, action)

    async def run_gamenight(self) -> None:
        job = GAMENIGHT_JOB
        period = await self.due(job.name, lambda t, done, seen: plan(job, t, self.tz, done, seen))
        if period is not None:
            await self.finish(period, self.auto_gamenight)

    async def pick(self, table: str, col: str, size: int) -> E.Pick | None:
        used = {r[col] for r in await self.db.fetchall(f"SELECT {col} FROM {table}")}
        last = await self.db.fetchone(f"SELECT {col} FROM {table} ORDER BY used_at DESC, rowid DESC LIMIT 1")
        return E.pick_next(size, used, self.rng, last[col] if last else None)

    async def mark_used(self, table: str, col: str, picked: E.Pick) -> None:
        async with self.db.transaction() as tx:
            if picked.reset:
                await tx.execute(f"DELETE FROM {table}")
            await tx.execute(f"INSERT OR REPLACE INTO {table} ({col}, used_at) VALUES (?, ?)", (picked.index, now()))

    # ------------------------------------------------------------ question of the day
    async def post_qotd(self, period: E.DailyPeriod) -> bool:
        channel = self.general(self.guild())
        if channel is None or not self.questions:
            log.warning("qotd %s skipped: %s", period.key,
                        "no questions" if channel else f"no {config.GENERAL_CHANNEL} channel")
            return True
        picked = await self.pick("qotd_used", "qid", len(self.questions))
        question = self.questions[picked.index]
        day = f"{E.MONTHS[period.day.month - 1][:3]} {period.day.day}"
        embed = style.embed(title="Question of the day", description=question,
                            footer=style.label("qotd", period.day.isoformat(), "answer in the thread"))
        message = await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        await self.mark_used("qotd_used", "qid", picked)
        try:
            await message.create_thread(name=f"QOTD {day}: answers", auto_archive_duration=1440)
        except discord.HTTPException:
            # The question is up; people can still answer in the channel. Don't repost it.
            log.warning("qotd %s: couldn't open the answers thread", period.key, exc_info=True)
        log.info("posted qotd %s (#%d)", period.key, picked.index)
        return True

    # ------------------------------------------------------------ this or that
    async def post_poll(self, period: E.DailyPeriod) -> bool:
        channel = self.general(self.guild())
        if channel is None or not self.pairs:
            log.warning("poll %s skipped: %s", period.key,
                        "no pairs" if channel else f"no {config.GENERAL_CHANNEL} channel")
            return True
        picked = await self.pick("poll_used", "pid", len(self.pairs))
        a, b = self.pairs[picked.index]
        poll = discord.Poll(question=POLL_QUESTION, duration=timedelta(hours=POLL_HOURS))
        poll.add_answer(text=a)
        poll.add_answer(text=b)
        await channel.send(poll=poll, allowed_mentions=NO_PINGS)
        await self.mark_used("poll_used", "pid", picked)
        log.info("posted poll %s (#%d)", period.key, picked.index)
        return True

    # ------------------------------------------------------------ counting
    def is_counting(self, channel) -> bool:
        return (getattr(channel, "parent", None) is None
                and config.match_by_name([channel], config.COUNTING_CHANNEL) is not None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Edits and deletions have no listener on purpose: only the original message counts.
        try:
            if (message.author.bot or message.guild is None or message.guild.id != self.bot.settings.guild_id
                    or message.type not in (discord.MessageType.default, discord.MessageType.reply)
                    or not self.is_counting(message.channel)):
                return
            n = E.parse_count(message.content)
            if n is None:
                return  # chatter, emoji, images: ignored
            await self.count(message, n)
        except Exception:
            log.exception("counting failed")

    async def count(self, message: discord.Message, n: int) -> None:
        uid, cid = message.author.id, message.channel.id
        champ_change = None
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT current, last_user, best FROM counting WHERE channel_id = ?", (cid,))
            state = CountState(row["current"], row["last_user"], row["best"]) if row else CountState()
            step = E.count_step(state, uid, n)
            s = step.state
            await tx.execute(
                "INSERT INTO counting (channel_id, current, last_user, best, updated_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (channel_id) DO UPDATE SET current = excluded.current,"
                " last_user = excluded.last_user, best = excluded.best, updated_at = excluded.updated_at",
                (cid, s.current, s.last_user, s.best, now()))
            if not step.ok:
                await tx.execute("DELETE FROM counting_run")
            else:
                # Opted-out members still count, but their share isn't recorded (no Champ role).
                if not await tx.fetchone("SELECT 1 FROM privacy_optout WHERE user_id = ?", (uid,)):
                    await tx.execute("INSERT INTO counting_run (user_id, n) VALUES (?, 1)"
                                     " ON CONFLICT (user_id) DO UPDATE SET n = n + 1", (uid,))
                if step.new_best:
                    runs = {r["user_id"]: r["n"] for r in await tx.fetchall("SELECT user_id, n FROM counting_run")}
                    old = await tx.fetchone("SELECT value FROM meta WHERE key = ?", (CHAMP_KEY,))
                    old_id = int(old["value"]) if old else None
                    new_id = E.champion(runs, old_id)
                    if new_id is not None and new_id != old_id:
                        await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                         (CHAMP_KEY, str(new_id)))
                        champ_change = (old_id, new_id)
        try:
            await message.add_reaction(CHECK if step.ok else CROSS)
        except discord.HTTPException:
            log.warning("counting: couldn't react", exc_info=True)
        if not step.ok:
            await self.announce_reset(message, step, n)
        if champ_change:
            await self.swap_champ(message.guild, *champ_change)

    async def announce_reset(self, message, step: E.Step, n: int) -> None:
        who = message.author.mention
        if step.kind == "double":
            why = f"{who} counted twice in a row"
        else:
            why = f"{who} said **{n}**, but the next number was **{step.broke_at + 1}**"
        text = (f"{CROSS} {why}. " + (f"The run ended at **{step.broke_at}**. " if step.broke_at else "")
                + f"Start again from **1**. Best run: **{step.state.best}**.")
        try:
            await message.channel.send(text, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("counting: couldn't post the reset note", exc_info=True)

    async def swap_champ(self, guild, old_id: int | None, new_id: int) -> None:
        role = config.match_by_name(guild.roles, config.COUNTING_ROLE)
        if role is None:
            log.warning("counting: no %s role", config.COUNTING_ROLE)
            return
        try:
            holders = {m.id: m for m in getattr(role, "members", [])}
            if old_id is not None and old_id not in holders:
                old = await self.member(guild, old_id)
                if old is not None:
                    holders[old_id] = old
            for uid, m in holders.items():
                if uid != new_id:
                    await m.remove_roles(role, reason="Counting Champ passed on")
            new = await self.member(guild, new_id)
            if new is not None:
                await new.add_roles(role, reason="New best counting run")
            log.info("counting champ: %s -> %s", old_id, new_id)
        except discord.HTTPException:
            log.warning("counting: couldn't swap the %s role", config.COUNTING_ROLE, exc_info=True)

    # ------------------------------------------------------------ birthdays: commands
    @birthday.command(name="set", description="Save your birthday (no year)")
    @app_commands.describe(month="Month, 1-12", day="Day of the month")
    async def birthday_set(self, interaction: discord.Interaction, month: app_commands.Range[int, 1, 12],
                           day: app_commands.Range[int, 1, 31]) -> None:
        await self.set_birthday(interaction, month, day)

    @birthday.command(name="remove", description="Forget your birthday")
    async def birthday_remove(self, interaction: discord.Interaction) -> None:
        await self.remove_birthday(interaction)

    @birthday.command(name="list", description="The next few birthdays on the server")
    async def birthday_list(self, interaction: discord.Interaction) -> None:
        await self.list_birthdays(interaction)

    async def set_birthday(self, interaction, month: int, day: int) -> None:
        if not E.valid_birthday(month, day):
            await interaction.response.send_message(
                f"{E.MONTHS[month - 1]} doesn't have a day {day}. Try again?", ephemeral=True)
            return
        await self.db.execute("INSERT OR REPLACE INTO birthdays (user_id, month, day) VALUES (?, ?, ?)",
                              (interaction.user.id, month, day))
        extra = " In years without a February 29 you're celebrated on the 28th." if (month, day) == (2, 29) else ""
        await interaction.response.send_message(
            f"Saved: **{E.birthday_text(month, day)}**. On the day you get a shout-out, the Birthday role "
            f"for 24 hours and {BIRTHDAY_COINS} coins.{extra}", ephemeral=True)

    async def remove_birthday(self, interaction) -> None:
        n = await self.db.execute("DELETE FROM birthdays WHERE user_id = ?", (interaction.user.id,))
        text = "Done, your birthday is forgotten." if n else "You don't have a birthday saved."
        await interaction.response.send_message(text, ephemeral=True)

    async def list_birthdays(self, interaction) -> None:
        guild = interaction.guild
        rows = [(r["user_id"], r["month"], r["day"]) for r in await self.db.fetchall("SELECT * FROM birthdays")]
        if guild is not None and self.bot.intents.members:  # members who left aren't listed
            rows = [r for r in rows if guild.get_member(r[0]) is not None]
        today = datetime.fromtimestamp(now(), self.tz).date()
        nxt = E.upcoming(rows, today, BIRTHDAY_LIST_SIZE)
        if not nxt:
            text = "No birthdays saved yet. Add yours with `/birthday set`."
        else:
            text = "\n".join(f"`{d.strftime('%b')} {d.day:>2}`  <@{u}>" + ("  🎂 today" if d == today else "")
                             for u, d in nxt)
        embed = style.embed(title="Coming up", description=text, footer=style.label("birthdays"))
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ birthdays: the day
    async def celebrate(self, period: E.DailyPeriod) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        year = period.day.year
        rows = [(r["user_id"], r["month"], r["day"]) for r in await self.db.fetchall("SELECT * FROM birthdays")]
        ids = E.birthdays_on(rows, period.day)
        if not ids:
            return True
        last = {r["key"][len(LAST_KEY):]: r["value"] for r in
                await self.db.fetchall("SELECT key, value FROM meta WHERE key LIKE ?", (LAST_KEY + "%",))}
        role = config.match_by_name(guild.roles, config.BIRTHDAY_ROLE)
        people = []
        for uid in ids:
            if last.get(str(uid)) == str(year):
                continue  # already celebrated this year (birthday changed since)
            m = await self.member(guild, uid)
            if m is None or m.bot:
                continue
            people.append(m)
            t = now()
            # Every step is safe to repeat: a retry after a failed post can't pay or extend twice.
            if await self.db.tracking_allowed(uid):
                await economy.apply(self.db, uid, BIRTHDAY_COINS, "birthday", t, ref=f"bday:{year}:{uid}")
            if role is not None:
                try:
                    await m.add_roles(role, reason="Birthday")
                    await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                                          (f"{ROLE_KEY}{uid}", str(t + BIRTHDAY_ROLE_SECONDS)))
                except discord.HTTPException:
                    log.warning("birthday: couldn't give %s the role", uid, exc_info=True)
        if not people:
            return True
        channel = self.general(guild)
        if channel is not None:
            names = [m.mention for m in people]
            joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            text = (f"🎂 Happy birthday, {joined}! Enjoy the Birthday role today, and {BIRTHDAY_COINS} coins "
                    f"are in your wallet. Drop them some love, everyone.")
            await channel.send(text, allowed_mentions=ping_only(users=people))
        else:
            log.warning("birthday %s: no %s channel for the shout-out", period.key, config.GENERAL_CHANNEL)
        async with self.db.transaction() as tx:
            for m in people:
                await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                 (f"{LAST_KEY}{m.id}", str(year)))
        log.info("birthdays %s: %s", period.key, [m.id for m in people])
        return True

    async def expire_birthday_roles(self) -> None:
        rows = await self.db.fetchall("SELECT key, value FROM meta WHERE key LIKE ?", (ROLE_KEY + "%",))
        due = [r["key"] for r in rows if int(r["value"]) <= now()]
        if not due:
            return
        guild = self.guild()
        if guild is None:
            return
        role = config.match_by_name(guild.roles, config.BIRTHDAY_ROLE)
        for key in due:
            uid = int(key[len(ROLE_KEY):])
            try:
                m = await self.member(guild, uid) if role is not None else None
                if m is not None:
                    await m.remove_roles(role, reason="Birthday is over")
            except discord.HTTPException:
                if key not in self.warned:
                    self.warned.add(key)
                    log.warning("birthday: couldn't remove the role from %s; retrying", uid, exc_info=True)
                continue
            await self.db.execute("DELETE FROM meta WHERE key = ?", (key,))

    # ------------------------------------------------------------ auto game night
    async def auto_gamenight(self, period) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        friday = datetime.fromtimestamp(period.scheduled_at, self.tz).date()
        ev_start, ev_end, start = E.friday_evening(friday, self.tz)
        if now() > start - E.TOO_LATE:
            log.info("auto game night %s: too late to create it", period.key)
            return True
        if await self.db.fetchone("SELECT 1 FROM gamenights WHERE starts_at >= ? AND starts_at < ?",
                                  (ev_start, ev_end)):
            log.info("auto game night %s: a member already has one", period.key)
            return True
        for ev in getattr(guild, "scheduled_events", []):
            if ev.status in LIVE_EVENTS and ev_start <= int(ev.start_time.timestamp()) < ev_end:
                log.info("auto game night %s: event %s already that evening", period.key, ev.id)
                return True
        voice = config.match_by_name(guild.voice_channels, config.SQUAD_VOICE)
        if voice is None:
            log.warning("auto game night %s: no %s voice channel", period.key, config.SQUAD_VOICE)
            return True
        g = await self.top_game(period.window_start, period.window_end)
        when = datetime.fromtimestamp(start, timezone.utc)
        description = ("The weekly community game night. "
                       + (f"{g.role} got the most play time on the server this week, so that's the pick. "
                          if g else "Bring whatever you're playing. ")
                       + "Tap Interested for a reminder, and hop in Squad voice.")
        try:
            event = await guild.create_scheduled_event(
                name=f"{g.role} game night" if g else "Game night",
                description=description,
                start_time=when,
                entity_type=discord.EntityType.voice,
                channel=voice,
                privacy_level=discord.PrivacyLevel.guild_only,
                reason="Weekly auto game night",
            )
        except discord.HTTPException:
            log.warning("auto game night %s: Discord wouldn't create the event (Manage Events?); retrying",
                        period.key, exc_info=True)
            return False
        await self.db.execute(
            "INSERT OR REPLACE INTO gamenights (event_id, host_id, game, starts_at, reminded) VALUES (?, ?, ?, ?, 0)",
            (event.id, self.bot.user.id, g.key if g else None, start))
        await self.announce_gamenight(guild, g, event, voice, when)
        log.info("auto game night %s: created %s (%s)", period.key, event.id, g.key if g else "anything")
        return True

    async def top_game(self, start: int, end: int):
        rows = await self.db.fetchall(
            'SELECT user_id, game AS k, start, "end" FROM game_sessions WHERE start < ? AND ("end" IS NULL OR "end" > ?)',
            (end, start))
        per_user = S.game_seconds([S.Session(r["user_id"], r["k"], r["start"], r["end"]) for r in rows],
                                  start, end, now())
        totals: dict[str, int] = {}
        for games in per_user.values():
            for name, secs in games.items():
                totals[name] = totals.get(name, 0) + secs
        return E.top_game(totals)

    async def announce_gamenight(self, guild, g, event, voice, when) -> None:
        channel = None
        if g is not None:
            channel = config.match_by_name(guild.text_channels, g.channel_name)
        channel = channel or config.match_by_name(guild.text_channels, config.GAMING_CHANNEL)
        if channel is None:
            return
        text = (f"🎮 **{event.name}** tonight {discord.utils.format_dt(when, 't')} "
                f"({discord.utils.format_dt(when, 'R')}) in {voice.mention}. Everyone's welcome. "
                f"Tap Interested for a reminder: {event.url}")
        try:
            await channel.send(text, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("auto game night: couldn't announce it", exc_info=True)


async def setup(bot) -> None:
    await bot.add_cog(Engagement(bot))
