"""Weekly recap, owner digest, member milestones and the monthly invite contest.

- Weekly recap, Mondays 10:00 local, in 📣・announcements: the previous Monday-Sunday from
  the tables the other modules keep (joins, message_counts, voice_sessions, lfg, gamenights,
  starboard, clip_polls, tournament payouts in the ledger, achievements, xp). Sections with no
  data are left out, and an empty week posts nothing. Names are mentions in an embed (embeds
  never ping) and the post carries AllowedMentions.none(). Members who opted out with
  /privacy never appear in a top list.
- Owner digest, Mondays 10:05 local: a DM to the server owner (member growth with a 7-day
  trend, open tickets/reports/suggestions, mod cases, errors counted by cogs/ops.py, pending
  partner applications, and "nothing needs you" when that's true). If the DM fails it goes to
  📋・mod-log instead.
- Milestones: the first time the member count reaches 25, 50, 100, 250, 500 or 1000, one
  no-ping celebration in 📣・announcements (meta milestone:N). The very first check only
  records the milestones already passed.
- Invite contest, the 1st at 12:00 local: last month's inviters ranked by invited members
  who are still here and stayed (logic/growth.py), top 3 announced (pinging only them) and
  paid 1500/750/300 coins with refs invitecontest:YYYY-MM:1/2/3. The result is frozen in meta
  before paying, so a retry pays the same people. Nobody qualifies: no post.

Scheduled jobs follow logic/schedule.py semantics. Level-ups compare against a level
snapshot in meta (recap:levels), refreshed after every recap. Server Members intent: owner and
member lookups from the cache (falls back to the API without it).
"""

import json
import logging
import time

import discord
from discord.ext import commands, tasks

import config
import economy
import style
from cogs.lfg import ping_only
from logic import growth as G
from logic import recap as R
from logic import stats as S
from logic import utility as U
from logic.schedule import plan

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
LEVELS_KEY = "recap:levels"  # meta: JSON {user: level} at the last recap
MILESTONE_KEY = "milestone:"  # meta: milestone:<n> -> unix time it was reached
MILESTONES_SEEN = "milestones_seen"  # meta: set on the very first milestone check
CONTEST_RESULT = "invitecontest_result:"  # meta: + YYYY-MM -> frozen JSON [[rank, user, people]]
SQUAD_LOOKBACK = 30 * 24 * 3600  # LFG posts older than this can't fill up in the week


def now() -> int:
    return int(time.time())


class Recap(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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

    @staticmethod
    def channel(guild, name: str):
        return config.match_by_name(guild.text_channels, name) if guild else None

    @staticmethod
    async def member(guild, user_id: int):
        """The member from the cache, else from the API; None if they left."""
        m = guild.get_member(user_id)
        if m is not None:
            return m
        try:
            return await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None

    async def optouts(self) -> set[int]:
        return {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}

    async def meta(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    async def count(self, sql: str, params=()) -> int:
        row = await self.db.fetchone(sql, params)
        return row["n"] if row else 0

    # ------------------------------------------------------------ scheduling
    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        jobs = (
            ("level snapshot", self.seed_levels),
            ("weekly recap", lambda: self.run_weekly(R.RECAP_JOB, self.post_recap)),
            ("owner digest", lambda: self.run_weekly(R.DIGEST_JOB, self.send_digest)),
            ("invite contest", self.run_contest),
            ("milestones", self.check_milestones),
        )
        for name, job in jobs:
            try:
                await job()
            except Exception:
                log.exception("recap: %s failed", name)

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

    async def finish(self, period, action) -> None:
        # Marked done only after it worked: a failure retries on the next tick.
        if await action(period):
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now()))

    async def run_weekly(self, job, action) -> None:
        period = await self.due(job.name, lambda t, done, seen: plan(job, t, self.tz, done, seen))
        if period is not None:
            await self.finish(period, action)

    async def run_contest(self) -> None:
        job = R.CONTEST_JOB
        period = await self.due(job.name, lambda t, done, seen: R.plan_monthly(job, t, self.tz, done, seen))
        if period is not None:
            await self.finish(period, self.invite_contest)

    # ------------------------------------------------------------ weekly recap
    async def levels(self) -> dict[int, int]:
        return {r["user_id"]: r["level"] for r in await self.db.fetchall("SELECT user_id, level FROM xp")}

    async def seed_levels(self) -> None:
        """The first snapshot, so the first recap has something to compare with."""
        if await self.meta(LEVELS_KEY) is None:
            await self.save_levels()

    async def save_levels(self) -> None:
        await self.set_meta(LEVELS_KEY, json.dumps({str(u): lvl for u, lvl in (await self.levels()).items()}))

    async def snapshot_levels(self) -> dict[int, int] | None:
        raw = await self.meta(LEVELS_KEY)
        if raw is None:
            return None
        try:
            return {int(u): int(lvl) for u, lvl in json.loads(raw).items()}
        except (ValueError, AttributeError):
            return None

    async def gather_recap(self, guild, start: int, end: int, first) -> R.Recap:
        skip = await self.optouts()
        bots = {m.id for m in getattr(guild, "members", []) if m.bot}
        hidden = skip | bots
        joined = await self.count("SELECT COUNT(DISTINCT user_id) AS n FROM joins WHERE joined_at >= ? AND joined_at < ?",
                                  (start, end))
        left = await self.count("SELECT COUNT(*) AS n FROM joins WHERE left_at >= ? AND left_at < ?", (start, end))

        days = R.day_keys(first)
        rows = await self.db.fetchall("SELECT user_id, SUM(count) AS n FROM message_counts WHERE day >= ? AND day <= ?"
                                      " GROUP BY user_id", (days[0], days[-1]))
        chat = {r["user_id"]: r["n"] for r in rows}

        rows = await self.db.fetchall('SELECT user_id, channel_id AS k, start, "end" FROM voice_sessions'
                                      ' WHERE start < ? AND ("end" IS NULL OR "end" > ?)', (end, start))
        afk = getattr(guild, "afk_channel", None)
        voice = S.counted_voice_seconds([S.Session(r["user_id"], r["k"], r["start"], r["end"]) for r in rows],
                                        start, end, now(), excluded_channels={afk.id} if afk else set(),
                                        bot_ids=bots)

        rows = await self.db.fetchall("SELECT p.id, p.size, m.joined_at FROM lfg_posts p JOIN lfg_members m"
                                      " ON m.post_id = p.id WHERE p.created_at >= ? AND p.created_at < ?",
                                      (start - SQUAD_LOOKBACK, end))
        posts: dict[int, tuple[int, list[int]]] = {}
        for r in rows:
            posts.setdefault(r["id"], (r["size"], []))[1].append(r["joined_at"])
        squads = R.squads_formed(posts.values(), start, end)

        gamenights = await self.count("SELECT COUNT(*) AS n FROM gamenights WHERE starts_at >= ? AND starts_at < ?",
                                      (start, end))
        hall = await self.count("SELECT COUNT(*) AS n FROM starboard WHERE at >= ? AND at < ?"
                                " AND board_message_id IS NOT NULL AND board_message_id != 0", (start, end))
        clip = await self.db.fetchone("SELECT winner_id FROM clip_polls WHERE winner_id IS NOT NULL"
                                      " AND ends_at >= ? AND ends_at < ? ORDER BY ends_at DESC LIMIT 1", (start, end))

        champions = []
        rows = await self.db.fetchall("SELECT user_id, ref FROM ledger WHERE ref LIKE 'tourney:%:first'"
                                      " AND at >= ? AND at < ? ORDER BY at", (start, end))
        for r in rows:
            try:
                tid = int(r["ref"].split(":")[1])
            except (IndexError, ValueError):
                continue
            t = await self.db.fetchone("SELECT name FROM tournaments WHERE id = ?", (tid,))
            champions.append((r["user_id"], t["name"] if t else f"Tournament #{tid}"))

        badges = await self.count("SELECT COUNT(*) AS n FROM achievements WHERE at >= ? AND at < ?", (start, end))

        before = await self.snapshot_levels()
        ups = R.level_ups(before, await self.levels(), exclude=hidden) if before is not None else []

        return R.Recap(
            joined=joined, left=left, messages=sum(chat.values()), voice_seconds=sum(voice.values()),
            chatters=R.top(chat, exclude=hidden), voice=R.top(voice, exclude=hidden), squads=squads,
            gamenights=gamenights, hall=hall, clip_winner=clip["winner_id"] if clip else None,
            champions=champions, badges=badges, levelups=ups,
        )

    async def post_recap(self, period) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        start, end, first = R.week_bounds(period.scheduled_at, self.tz)
        recap = await self.gather_recap(guild, start, end, first)
        sections = R.recap_sections(recap)
        if not sections:
            log.info("recap %s: quiet week, nothing posted", period.key)
            await self.save_levels()
            return True
        channel = self.channel(guild, config.ANNOUNCEMENTS_CHANNEL)
        if channel is None:
            log.warning("recap %s skipped: no %s channel", period.key, config.ANNOUNCEMENTS_CHANNEL)
            return True
        embed = style.embed(title="The week in review", description=R.week_label(first),
                            footer=style.label("weekly recap", first.isoformat()))
        for name, value in sections:
            embed.add_field(name=name, value=value[:1024], inline=False)
        try:
            await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("recap %s: couldn't post; retrying", period.key, exc_info=True)
            return False
        await self.save_levels()
        log.info("recap %s posted (%d sections)", period.key, len(sections))
        return True

    # ------------------------------------------------------------ owner digest
    def error_total(self) -> int | None:
        ops = self.bot.get_cog("Ops")
        monitor, lock = getattr(ops, "monitor", None), getattr(ops, "lock", None)
        if monitor is None or lock is None:
            return None
        with lock:
            return sum(dict(monitor.totals).values())

    @staticmethod
    def open_suggestions(guild) -> int | None:
        forum = config.match_by_name(getattr(guild, "forums", []), config.SUGGESTIONS_FORUM)
        if forum is None:
            return None
        statuses = set(U.STATUS_TAGS.values())
        return sum(1 for t in getattr(forum, "threads", [])
                   if not getattr(t, "archived", False)
                   and not statuses & {tag.name for tag in getattr(t, "applied_tags", [])})

    async def gather_digest(self, guild, start: int, end: int, first) -> R.Digest:
        members = guild.member_count or len(getattr(guild, "members", []))
        joined = await self.count("SELECT COUNT(DISTINCT user_id) AS n FROM joins WHERE joined_at >= ? AND joined_at < ?",
                                  (start, end))
        left = await self.count("SELECT COUNT(*) AS n FROM joins WHERE left_at >= ? AND left_at < ?", (start, end))
        rows = await self.db.fetchall("SELECT joined_at, left_at FROM joins WHERE joined_at > ? OR left_at > ?",
                                      (start, start))
        trend = R.member_trend(members, [(r["joined_at"], r["left_at"]) for r in rows], R.day_ends(first, self.tz))
        cases = {r["kind"]: r["n"] for r in await self.db.fetchall(
            "SELECT kind, COUNT(*) AS n FROM cases WHERE at >= ? AND at < ? GROUP BY kind", (start, end))}
        return R.Digest(
            member_count=members, joined=joined, left=left, trend=trend,
            open_tickets=await self.count("SELECT COUNT(*) AS n FROM tickets WHERE closed_at IS NULL AND thread_id > 0"),
            open_reports=await self.count("SELECT COUNT(*) AS n FROM reports WHERE status = 'open'"),
            open_suggestions=self.open_suggestions(guild),
            cases=cases, errors=self.error_total(),
            pending_partners=await self.count("SELECT COUNT(*) AS n FROM partners WHERE status = 'pending'"),
        )

    async def owner(self, guild):
        if getattr(guild, "owner", None) is not None:
            return guild.owner
        owner_id = getattr(guild, "owner_id", None)
        if owner_id is None:
            return None
        m = await self.member(guild, owner_id)
        if m is not None:
            return m
        try:
            return await self.bot.fetch_user(owner_id)
        except discord.HTTPException:
            return None

    async def send_digest(self, period) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        start, end, first = R.week_bounds(period.scheduled_at, self.tz)
        digest = await self.gather_digest(guild, start, end, first)
        embed = style.embed(title="Your weekly digest",
                            description=f"{discord.utils.escape_markdown(guild.name)} · {R.week_label(first)}",
                            footer=style.label("owner digest", "private"),
                            color=style.FOREST if R.needs_you(digest) else style.MUTED)
        for name, value in R.digest_fields(digest, first):
            embed.add_field(name=name, value=value[:1024], inline=False)
        owner = await self.owner(guild)
        if owner is not None:
            try:
                await owner.send(embed=embed, allowed_mentions=NO_PINGS)
                log.info("digest %s sent to the owner", period.key)
                return True
            except discord.HTTPException:
                log.warning("digest %s: couldn't DM the owner; posting in %s", period.key, config.MOD_LOG_CHANNEL)
        channel = self.channel(guild, config.MOD_LOG_CHANNEL)
        if channel is None:
            log.warning("digest %s skipped: no DM and no %s channel", period.key, config.MOD_LOG_CHANNEL)
            return True
        try:
            await channel.send("Owner digest (I couldn't DM the owner):", embed=embed, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("digest %s: couldn't post in %s; retrying", period.key, config.MOD_LOG_CHANNEL, exc_info=True)
            return False
        return True

    # ------------------------------------------------------------ milestones
    async def check_milestones(self) -> None:
        guild = self.guild()
        count = getattr(guild, "member_count", None) if guild else None
        if not count:
            return
        rows = await self.db.fetchall("SELECT key FROM meta WHERE key LIKE ?", (MILESTONE_KEY + "%",))
        done = set()
        for r in rows:
            try:
                done.add(int(r["key"][len(MILESTONE_KEY):]))
            except ValueError:
                continue
        first = await self.meta(MILESTONES_SEEN) is None
        post, mark = R.milestones_due(count, done, first)
        if post is not None:
            channel = self.channel(guild, config.ANNOUNCEMENTS_CHANNEL)
            if channel is None:
                log.warning("milestone %d: no %s channel, not announced", post, config.ANNOUNCEMENTS_CHANNEL)
            else:
                try:
                    await channel.send(R.milestone_text(post), allowed_mentions=NO_PINGS)
                except discord.HTTPException:
                    log.warning("milestone %d: couldn't post; retrying", post, exc_info=True)
                    return
                log.info("milestone %d members announced", post)
        t = str(now())
        async with self.db.transaction() as tx:
            if first:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (MILESTONES_SEEN, t))
            for m in mark:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (f"{MILESTONE_KEY}{m}", t))

    # ------------------------------------------------------------ invite contest
    async def contest_winners(self, guild, period) -> list[tuple[int, int, int]]:
        """The frozen result if this month was judged before (a retry), else judge it now."""
        key = CONTEST_RESULT + period.month
        raw = await self.meta(key)
        if raw is not None:
            return [tuple(w) for w in json.loads(raw)]
        rows = await self.db.fetchall("SELECT user_id, inviter_id, joined_at, left_at FROM joins")
        joins = [G.Join(r["user_id"], r["inviter_id"], r["joined_at"], r["left_at"]) for r in rows]
        ranking = R.contest_ranking(joins, period.window_start, period.window_end, now(),
                                    exclude=await self.optouts(), n=len(joins))
        winners = []
        for _, uid, people in ranking:
            m = await self.member(guild, uid)
            if m is None or m.bot:
                continue  # left the server (or a bot's invite): the next one moves up
            winners.append((len(winners) + 1, uid, people))
            if len(winners) == len(R.CONTEST_PRIZES):
                break
        await self.set_meta(key, json.dumps(winners))
        return winners

    async def invite_contest(self, period) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        winners = await self.contest_winners(guild, period)
        if not winners:
            log.info("invite contest %s: nobody qualified", period.month)
            return True
        t = now()
        for rank, uid, _ in winners:
            # Refs make this safe to repeat after a failed announcement.
            await economy.apply(self.db, uid, R.CONTEST_PRIZES[rank - 1], R.CONTEST_REASON, t,
                                ref=R.contest_ref(period.month, rank))
        channel = self.channel(guild, config.ANNOUNCEMENTS_CHANNEL)
        if channel is None:
            log.warning("invite contest %s: paid, but no %s channel to announce it", period.month,
                        config.ANNOUNCEMENTS_CHANNEL)
            return True
        users = [discord.Object(uid) for _, uid, _ in winners]
        try:
            await channel.send(R.contest_text(period.month, winners), allowed_mentions=ping_only(users=users))
        except discord.HTTPException:
            log.warning("invite contest %s: couldn't announce; retrying", period.month, exc_info=True)
            return False
        log.info("invite contest %s: %s", period.month, [uid for _, uid, _ in winners])
        return True


async def setup(bot) -> None:
    await bot.add_cog(Recap(bot))
