"""Offline tests for cogs.recap: a real in-memory SQLite database plus small fakes for the
bot, guild, channels and members. No network."""

import asyncio
import json
import threading
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import recap as cogmod
from cogs.recap import Recap
from logic import recap as R

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER = 1
U1, U2, U3, U4, BOTUSER = 11, 12, 13, 14, 50
HOUR = 3600
DAY = 24 * HOUR


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


MON = ts(2026, 10, 12, 10, 0)  # recap time; the week is Oct 5 00:00 .. Oct 12 00:00
WEEK_START = ts(2026, 10, 5)


def run(coro):
    return asyncio.run(coro)


def http_error(cls=discord.HTTPException):
    return cls(SimpleNamespace(status=403, reason="nope"), "nope")


class FakeText:
    _next = 300

    def __init__(self, name):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeMember:
    def __init__(self, uid, bot=False):
        self.id = uid
        self.bot = bot
        self.mention = f"<@{uid}>"
        self.dms = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.dms.append(dict(content=content, **kwargs))


class FakeThread:
    def __init__(self, tags=(), archived=False):
        self.applied_tags = [SimpleNamespace(name=t) for t in tags]
        self.archived = archived


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.name = "Test *Server*"
        self.announcements = FakeText(config.ANNOUNCEMENTS_CHANNEL)
        self.modlog = FakeText(config.MOD_LOG_CHANNEL)
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.text_channels = [self.general, self.announcements, self.modlog]
        self.forums = []
        self.afk_channel = None
        self.members = []
        self.member_count = 10
        self.owner_id = OWNER
        self.owner = None
        self.fetched = []

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    async def fetch_member(self, uid):
        self.fetched.append(uid)
        raise http_error(discord.NotFound)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.cogs = {}

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None

    def get_cog(self, name):
        return self.cogs.get(name)

    async def fetch_user(self, uid):
        raise http_error(discord.NotFound)


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = MON

    def member(self, uid, **kw):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, **kw)
            self.guild.members.append(m)
        return m

    async def ex(self, sql, params=()):
        await self.db.execute(sql, params)

    async def join(self, uid, at, inviter=None, left=None):
        await self.ex("INSERT INTO joins (user_id, joined_at, inviter_id, left_at) VALUES (?, ?, ?, ?)",
                      (uid, at, inviter, left))

    async def optout(self, uid):
        await self.ex("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def first_seen(self, name, at):
        await self.ex("INSERT INTO meta (key, value) VALUES (?, ?)", (f"first_seen:{name}", str(at)))

    async def done(self, key):
        return await self.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", (key,)) is not None


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Recap(bot)
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def period_for(job, at):
    from logic.schedule import latest_due
    return latest_due(job, at, TZ)


def fields(embed):
    return {f.name: f.value for f in embed.fields}


# ---------------------------------------------------------------- weekly recap
async def busy_week(env):
    s = WEEK_START
    await env.join(U1, s + HOUR)
    await env.join(U2, s + 2 * HOUR, left=s + 3 * HOUR)
    await env.join(U3, s - DAY, left=s + DAY)
    await env.join(U4, s - 10 * DAY)
    for uid, day, n in ((U1, "2026-10-05", 40), (U2, "2026-10-11", 25), (U3, "2026-10-06", 90),
                        (U4, "2026-10-04", 999), (U4, "2026-10-12", 999)):
        await env.ex("INSERT INTO message_counts (user_id, day, count) VALUES (?, ?, ?)", (uid, day, n))
    for uid in (U1, U2):
        await env.ex('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, 77, ?, ?)',
                     (uid, s + DAY, s + DAY + 2 * HOUR))
    # LFG: one squad of 2 fills this week, one never fills
    await env.ex("INSERT INTO lfg_posts (id, game, host_id, size, when_text, created_at) VALUES (1, 'x', ?, 2, 'now', ?)",
                 (U1, s + HOUR))
    await env.ex("INSERT INTO lfg_members (post_id, user_id, joined_at) VALUES (1, ?, ?), (1, ?, ?)",
                 (U1, s + HOUR, U2, s + 2 * HOUR))
    await env.ex("INSERT INTO lfg_posts (id, game, host_id, size, when_text, created_at) VALUES (2, 'y', ?, 4, 'now', ?)",
                 (U3, s + HOUR))
    await env.ex("INSERT INTO lfg_members (post_id, user_id, joined_at) VALUES (2, ?, ?)", (U3, s + HOUR))
    await env.ex("INSERT INTO gamenights (event_id, host_id, starts_at) VALUES (5, ?, ?)", (U1, s + 4 * DAY))
    await env.ex("INSERT INTO starboard (message_id, channel_id, author_id, board_message_id, stars, at)"
                 " VALUES (1, 2, ?, 900, 3, ?), (2, 2, ?, 0, 3, ?)", (U1, s + DAY, U2, s + DAY))
    await env.ex("INSERT INTO clip_polls (week, message_id, ends_at, winner_id, done) VALUES ('2026-W41', 1, ?, ?, 1)",
                 (s + 5 * DAY, U2))
    await env.ex("INSERT INTO tournaments (id, name, size, status, created_by, created_at, winner_id)"
                 " VALUES (3, 'Friday *Cup*', 4, 'done', ?, ?, ?)", (U1, s, U3))
    await economy.apply(env.db, U3, 500, "tournament", s + 2 * DAY, ref="tourney:3:first")
    await economy.apply(env.db, U1, 200, "tournament", s + 2 * DAY, ref="tourney:3:second")
    await env.ex("INSERT INTO achievements (user_id, key, at) VALUES (?, 'a', ?), (?, 'b', ?), (?, 'c', ?)",
                 (U1, s + DAY, U2, s + DAY, U3, s - DAY))


def test_recap_posts_every_section_with_no_pings(monkeypatch):
    async def go(env):
        await busy_week(env)
        await env.ex("INSERT INTO xp (user_id, xp, level) VALUES (?, 100, 3), (?, 50, 1)", (U1, U2))
        await env.cog.seed_levels()
        await env.ex("UPDATE xp SET level = 5 WHERE user_id = ?", (U1,))
        assert await env.cog.post_recap(period_for(R.RECAP_JOB, MON)) is True
        (post,) = env.guild.announcements.sent
        assert post["allowed_mentions"].users is False and post["allowed_mentions"].everyone is False
        f = fields(post["embed"])
        assert post["embed"].description == "Oct 5 – Oct 11"
        assert f["Members"] == "**2** joined, **2** left (net **+0**)"
        assert f["Chat"].startswith("**155** messages")
        assert "<@13> 90 · <@11> 40 · <@12> 25" in f["Chat"]
        assert f["Voice"].startswith("**4** hours in voice")
        assert "<@11> 2h 0m" in f["Voice"]
        assert f["Squads"] == "**1** squad formed in LFG"
        assert f["Game nights"] == "**1** game night held"
        assert f["Hall of fame"] == "**1** post made the hall of fame"
        assert f["Clip of the week"] == "<@12>"
        assert f["Tournaments"] == "<@13> won **Friday \\*Cup\\***"
        assert f["Badges"] == "**2** new badges earned"
        assert f["Level-ups"] == "<@11> reached level **5**"
        # the snapshot moved on, so next week starts from here
        assert json.loads((await env.db.fetchone("SELECT value FROM meta WHERE key = ?",
                                                 (cogmod.LEVELS_KEY,)))["value"]) == {"11": 5, "12": 1}
    with_env(go, monkeypatch)


def test_recap_hides_opted_out_members_and_bots_from_top_lists(monkeypatch):
    async def go(env):
        await busy_week(env)
        await env.optout(U3)
        env.member(U1, bot=True)
        await env.cog.post_recap(period_for(R.RECAP_JOB, MON))
        f = fields(env.guild.announcements.sent[0]["embed"])
        assert "<@13>" not in f["Chat"] and "<@11>" not in f["Chat"] and "<@12> 25" in f["Chat"]
        assert "Voice" not in f  # the only other person in voice was the bot
    with_env(go, monkeypatch)


def test_quiet_week_posts_nothing_but_counts_as_done(monkeypatch):
    async def go(env):
        await env.first_seen("recap", MON - 30 * DAY)
        await env.ex("INSERT INTO jobs (key, done_at) VALUES ('recap:2026-W41', 1)")
        await env.join(U1, WEEK_START - DAY)  # last week
        await env.cog.run_weekly(R.RECAP_JOB, env.cog.post_recap)
        assert env.guild.announcements.sent == []
        assert await env.done("recap:2026-W42")
    with_env(go, monkeypatch)


def test_recap_schedule_first_run_marks_done_then_runs_once(monkeypatch):
    async def go(env):
        await busy_week(env)
        env.t = MON + 60
        await env.cog.run_weekly(R.RECAP_JOB, env.cog.post_recap)
        assert env.guild.announcements.sent == []  # first ever: only marked done
        assert await env.done("recap:2026-W42")
        env.t = MON + 7 * DAY - 60  # just before next Monday 10:00
        await env.cog.run_weekly(R.RECAP_JOB, env.cog.post_recap)
        assert env.guild.announcements.sent == []
        await env.join(U2, MON + DAY)
        env.t = MON + 7 * DAY + 60
        await env.cog.run_weekly(R.RECAP_JOB, env.cog.post_recap)
        await env.cog.run_weekly(R.RECAP_JOB, env.cog.post_recap)
        (post,) = env.guild.announcements.sent
        assert fields(post["embed"])["Members"].startswith("**1** joined")
        assert await env.done("recap:2026-W43")
    with_env(go, monkeypatch)


def test_recap_post_failure_retries(monkeypatch):
    async def go(env):
        await busy_week(env)
        await env.first_seen("recap", MON - 30 * DAY)
        env.guild.announcements.fail = http_error()
        await env.cog.run_weekly(R.RECAP_JOB, env.cog.post_recap)
        assert not await env.done("recap:2026-W42")
        env.guild.announcements.fail = None
        await env.cog.run_weekly(R.RECAP_JOB, env.cog.post_recap)
        assert len(env.guild.announcements.sent) == 1 and await env.done("recap:2026-W42")
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- owner digest
class FakeOps:
    def __init__(self, totals):
        self.lock = threading.Lock()
        self.monitor = SimpleNamespace(totals=totals)


def test_digest_dms_the_owner_and_says_nothing_needs_them(monkeypatch):
    async def go(env):
        env.guild.owner = env.member(OWNER)
        env.bot.cogs["Ops"] = FakeOps({})
        env.guild.member_count = 12
        await env.join(U1, WEEK_START + DAY + HOUR)  # Tuesday
        await env.join(U2, WEEK_START + 3 * DAY + HOUR)  # Thursday
        await env.join(U3, MON - HOUR)  # after the week: the Sunday count excludes them
        env.t = MON + 5 * 60
        assert await env.cog.send_digest(period_for(R.DIGEST_JOB, env.t)) is True
        (dm,) = env.guild.owner.dms
        assert dm["allowed_mentions"].users is False
        assert dm["embed"].description.startswith("Test \\*Server\\*")
        f = fields(dm["embed"])
        assert f["Members"].startswith("**12** now · **2** joined, **0** left (net **+2**)")
        assert "Mon 9 · Tue 10 · Wed 10 · Thu 11 · Fri 11 · Sat 11 · Sun 11" in f["Members"]
        assert f["Mod actions"] == "None this week."
        assert f["Errors"] == "None since the bot started."
        assert f["Needs you"] == "Nothing needs you this week."
        assert env.guild.modlog.sent == []
    with_env(go, monkeypatch)


def test_digest_lists_what_needs_attention(monkeypatch):
    async def go(env):
        env.guild.owner = env.member(OWNER)
        env.bot.cogs["Ops"] = FakeOps({"cogs.x": 3, "cogs.y": 1})
        forum = SimpleNamespace(name=config.SUGGESTIONS_FORUM, threads=[
            FakeThread(["Idea"]), FakeThread(["Accepted"]), FakeThread([]), FakeThread([], archived=True)])
        env.guild.forums = [forum]
        await env.ex("INSERT INTO tickets (thread_id, user_id, opened_at) VALUES (5, ?, 1), (-1, ?, 1)", (U1, U2))
        await env.ex("INSERT INTO tickets (thread_id, user_id, opened_at, closed_at) VALUES (6, ?, 1, 2)", (U3,))
        await env.ex("INSERT INTO reports (reporter_id, target_id, reason, at) VALUES (?, ?, 'x', 1)", (U1, U2))
        for kind in ("warn", "warn", "timeout"):
            await env.ex("INSERT INTO cases (user_id, kind, reason, at) VALUES (?, ?, 'r', ?)", (U1, kind, WEEK_START + DAY))
        await env.ex("INSERT INTO cases (user_id, kind, reason, at) VALUES (?, 'ban', 'r', ?)", (U1, WEEK_START - DAY))
        await env.ex("INSERT INTO partners (user_id, server_name, invite_code, description, created_at)"
                     " VALUES (?, 'S', 'c', 'd', 1)", (U1,))
        await env.ex("INSERT INTO partners (user_id, server_name, invite_code, description, created_at, status)"
                     " VALUES (?, 'S', 'c', 'd', 1, 'approved')", (U2,))
        await env.cog.send_digest(period_for(R.DIGEST_JOB, MON + 5 * 60))
        f = fields(env.guild.owner.dms[0]["embed"])
        assert f["Mod actions"] == "**3** this week: 1 timeout, 2 warn"
        assert "Tickets open: **1**" in f["Queue"] and "Reports open: **1**" in f["Queue"]
        assert "Partner applications: **1**" in f["Queue"]
        assert "Suggestions without a status: **2**" in f["Queue"]
        assert f["Errors"].startswith("**4** since")
        assert f["Needs you"] == ("1 open ticket · 1 open report · 1 partner application · "
                                  "2 suggestions without a status · 4 errors")
    with_env(go, monkeypatch)


def test_digest_falls_back_to_mod_log_when_dms_fail(monkeypatch):
    async def go(env):
        owner = env.member(OWNER)
        owner.fail = http_error(discord.Forbidden)
        # not in guild.owner: found via get_member(owner_id)
        assert await env.cog.send_digest(period_for(R.DIGEST_JOB, MON + 5 * 60)) is True
        (post,) = env.guild.modlog.sent
        assert "couldn't DM" in post["content"] and post["allowed_mentions"].users is False
        assert "Errors" not in fields(post["embed"])  # ops not loaded
    with_env(go, monkeypatch)


def test_digest_with_no_owner_found_uses_mod_log_and_retries_if_that_fails(monkeypatch):
    async def go(env):
        await env.first_seen("ownerdigest", MON - 30 * DAY)
        env.t = MON + 6 * 60
        env.guild.modlog.fail = http_error()
        await env.cog.run_weekly(R.DIGEST_JOB, env.cog.send_digest)
        assert env.guild.fetched == [OWNER]
        assert not await env.done("ownerdigest:2026-W42")
        env.guild.modlog.fail = None
        await env.cog.run_weekly(R.DIGEST_JOB, env.cog.send_digest)
        assert len(env.guild.modlog.sent) == 1 and await env.done("ownerdigest:2026-W42")
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- milestones
def test_milestones_first_check_is_silent_then_each_posts_once(monkeypatch):
    async def go(env):
        env.guild.member_count = 30
        await env.cog.check_milestones()
        assert env.guild.announcements.sent == []
        env.guild.member_count = 49
        await env.cog.check_milestones()
        assert env.guild.announcements.sent == []
        env.guild.member_count = 50
        await env.cog.check_milestones()
        env.guild.member_count = 48
        await env.cog.check_milestones()
        env.guild.member_count = 51
        await env.cog.check_milestones()
        (post,) = env.guild.announcements.sent
        assert "**50 members**" in post["content"]
        assert post["allowed_mentions"].users is False and post["allowed_mentions"].everyone is False
        keys = {r["key"] for r in await env.db.fetchall("SELECT key FROM meta WHERE key LIKE 'milestone:%'")}
        assert keys == {"milestone:25", "milestone:50"}
    with_env(go, monkeypatch)


def test_milestone_post_failure_retries_and_skips_to_highest(monkeypatch):
    async def go(env):
        env.guild.member_count = 10
        await env.cog.check_milestones()
        env.guild.member_count = 101
        env.guild.announcements.fail = http_error()
        await env.cog.check_milestones()
        env.guild.announcements.fail = None
        await env.cog.check_milestones()
        await env.cog.check_milestones()
        (post,) = env.guild.announcements.sent
        assert "**100 members**" in post["content"]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- invite contest
CONTEST_AT = ts(2026, 10, 1, 12, 0)
SEP = ts(2026, 9, 1)


def contest_period():
    return R.latest_due_monthly(R.CONTEST_JOB, CONTEST_AT + 60, TZ)


async def invites(env):
    # U1: 3 people, U2: 2, U3: 1, U4: 1 (later than U3)
    n = 100
    for inviter, count, day in ((U1, 3, 2), (U2, 2, 3), (U3, 1, 4), (U4, 1, 5)):
        for _ in range(count):
            n += 1
            await env.join(n, SEP + day * DAY, inviter=inviter)
    for uid in (U1, U2, U3, U4):
        env.member(uid)


async def balance(env, uid):
    return await economy.balance(env.db, uid)


def test_contest_pays_and_pings_only_the_top_three(monkeypatch):
    async def go(env):
        await invites(env)
        env.t = CONTEST_AT + 60
        assert await env.cog.invite_contest(contest_period()) is True
        (post,) = env.guild.announcements.sent
        assert [u.id for u in post["allowed_mentions"].users] == [U1, U2, U3]
        assert post["allowed_mentions"].roles is False and post["allowed_mentions"].everyone is False
        assert "September 2026" in post["content"] and "<@14>" not in post["content"]
        assert [await balance(env, u) for u in (U1, U2, U3, U4)] == [1500, 750, 300, 0]
        refs = {r["ref"] for r in await env.db.fetchall("SELECT ref FROM ledger")}
        assert refs == {"invitecontest:2026-09:1", "invitecontest:2026-09:2", "invitecontest:2026-09:3"}
    with_env(go, monkeypatch)


def test_contest_skips_opted_out_and_departed_inviters(monkeypatch):
    async def go(env):
        await invites(env)
        await env.optout(U1)
        env.guild.members = [m for m in env.guild.members if m.id != U2]  # U2 left
        env.t = CONTEST_AT + 60
        await env.cog.invite_contest(contest_period())
        (post,) = env.guild.announcements.sent
        assert [u.id for u in post["allowed_mentions"].users] == [U3, U4]
        assert [await balance(env, u) for u in (U1, U2, U3, U4)] == [0, 0, 1500, 750]
    with_env(go, monkeypatch)


def test_contest_with_nobody_qualifying_posts_nothing(monkeypatch):
    async def go(env):
        await env.join(200, ts(2026, 9, 30), inviter=U1)  # hasn't stayed 3 days yet
        env.member(U1)
        env.t = CONTEST_AT + 60
        assert await env.cog.invite_contest(contest_period()) is True
        assert env.guild.announcements.sent == []
        assert await env.db.fetchone("SELECT 1 FROM ledger") is None
    with_env(go, monkeypatch)


def test_contest_retry_after_failed_post_pays_once_and_keeps_the_result(monkeypatch):
    async def go(env):
        await invites(env)
        await env.first_seen("invitecontest", ts(2026, 8, 15))
        env.t = CONTEST_AT + 60
        env.guild.announcements.fail = http_error()
        await env.cog.run_contest()
        assert not await env.done("invitecontest:2026-09")
        assert await balance(env, U1) == 1500
        env.guild.members = [m for m in env.guild.members if m.id != U1]  # leaves before the retry
        env.guild.announcements.fail = None
        env.t += 60
        await env.cog.run_contest()
        await env.cog.run_contest()
        (post,) = env.guild.announcements.sent
        assert [u.id for u in post["allowed_mentions"].users] == [U1, U2, U3]  # the frozen result
        assert [await balance(env, u) for u in (U1, U2, U3, U4)] == [1500, 750, 300, 0]
        assert await env.done("invitecontest:2026-09")
    with_env(go, monkeypatch)


def test_contest_first_run_only_marks_done(monkeypatch):
    async def go(env):
        await invites(env)
        env.t = CONTEST_AT + 60
        await env.cog.run_contest()
        assert env.guild.announcements.sent == [] and await env.done("invitecontest:2026-09")
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- the loop never raises
def test_tick_swallows_job_errors(monkeypatch):
    async def go(env):
        async def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "check_milestones", boom)
        monkeypatch.setattr(env.cog, "run_contest", boom)
        await env.cog.tick.coro(env.cog)
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (cogmod.LEVELS_KEY,))
    with_env(go, monkeypatch)
