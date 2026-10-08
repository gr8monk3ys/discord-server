"""Offline tests for cogs.vibes: a real in-memory SQLite database plus small fakes for the
bot, guild, channels and members. No network."""

import asyncio
import random
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import vibes as cogmod
from cogs.vibes import LAST_MSG_KEY, LAST_REVIVAL_KEY, STARTED_KEY, Vibes
from logic import engagement as E
from logic import recap as R
from logic import vibes as V

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
HOUR = 3600
DAY = 24 * HOUR
QUESTIONS = ["Q one?", "Q two?", "Q three?"]


def run(coro):
    return asyncio.run(coro)


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


def at(t):
    return datetime.fromtimestamp(t, tz=timezone.utc)


OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)


class FakeText:
    _next = 300

    def __init__(self, name):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.sent = []
        self.fail = None
        self.past = []  # history, newest first
        self.history_fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))

    def history(self, limit=100):
        channel = self

        async def gen():
            if channel.history_fail is not None:
                raise channel.history_fail
            for m in channel.past[:limit]:
                yield m
        return gen()


class FakeRole:
    def __init__(self, name):
        self.id = hash(name) & 0xFFFF
        self.name = name


class FakeMember:
    def __init__(self, uid, guild, bot=False, joined_at=None, created_at=OLD, roles=(), premium_since=None):
        self.id = uid
        self.guild = guild
        self.bot = bot
        self.mention = f"<@{uid}>"
        self.joined_at = joined_at
        self.created_at = created_at
        self.roles = [FakeRole("@everyone"), *(FakeRole(r) for r in roles)]
        self.premium_since = premium_since


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.announcements = FakeText(config.ANNOUNCEMENTS_CHANNEL)
        self.text_channels = [self.general, self.announcements]
        self.afk_channel = None
        self.members = []

    @property
    def premium_subscribers(self):
        return [m for m in self.members if m.premium_since is not None]

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None


class Env:
    def __init__(self, db, guild, cog):
        self.db, self.guild, self.cog = db, guild, cog
        self.t = ts(2026, 10, 7, 12, 0)

    def member(self, uid, **kw):
        m = FakeMember(uid, self.guild, **kw)
        self.guild.members.append(m)
        return m

    def say(self, uid, channel=None, bot=False, type=discord.MessageType.default):
        author = SimpleNamespace(id=uid, bot=bot)
        return SimpleNamespace(author=author, guild=self.guild, channel=channel or self.guild.general, type=type,
                               created_at=at(self.t))

    async def meta(self, key):
        return await self.cog.meta(key)

    async def seen(self, job, t):
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (f"first_seen:{job.name}", str(t)))

    async def balance(self, uid):
        return await economy.balance(self.db, uid)

    async def refs(self):
        return {r["ref"]: r["user_id"] for r in await self.db.fetchall("SELECT ref, user_id FROM ledger")}


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Vibes(bot, questions=list(QUESTIONS), rng=random.Random(1))
        env = Env(db, guild, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def pinged_only(sent, uids):
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.roles is False
    assert sorted(u.id for u in am.users) == sorted(uids)


def no_pings(sent):
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.roles is False and am.users is False


# ---------------------------------------------------------------- chat revival
def test_revival_after_three_quiet_hours_then_waits_for_a_member(monkeypatch):
    async def body(env):
        ch = env.guild.general
        env.t = ts(2026, 10, 7, 10, 0)
        assert not await env.cog.revive()  # nothing known: the quiet clock starts now
        assert await env.meta(LAST_MSG_KEY) == str(env.t)
        env.t += 3 * HOUR - 60
        assert not await env.cog.revive()
        env.t += 60
        assert await env.cog.revive()
        assert len(ch.sent) == 1 and ch.sent[0]["content"].split("**")[1] in QUESTIONS
        no_pings(ch.sent[0])
        assert await env.meta(LAST_REVIVAL_KEY) == str(env.t)
        assert len(await env.db.fetchall("SELECT qid FROM qotd_used")) == 1
        env.t += 4 * HOUR  # still quiet, but nobody answered the last one
        assert not await env.cog.revive()
        await env.cog.on_message(env.say(11))
        env.t += 3 * HOUR
        assert await env.cog.revive()
        assert len(ch.sent) == 2
        assert ch.sent[0]["content"] != ch.sent[1]["content"]
    with_env(body, monkeypatch)


def test_revival_waits_six_hours_between_starters(monkeypatch):
    async def body(env):
        env.t = ts(2026, 10, 7, 10, 0)
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (LAST_MSG_KEY, str(env.t - 4 * HOUR)))
        assert await env.cog.revive()
        env.t += 60
        await env.cog.on_message(env.say(11))
        env.t += 3 * HOUR + 60  # quiet again, but only ~3 h since the last starter
        assert not await env.cog.revive()
        env.t = ts(2026, 10, 7, 16, 0)
        assert await env.cog.revive()
        assert len(env.guild.general.sent) == 2
    with_env(body, monkeypatch)


def test_no_revival_at_night(monkeypatch):
    async def body(env):
        env.t = ts(2026, 10, 7, 23, 30)
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (LAST_MSG_KEY, str(env.t - 9 * HOUR)))
        assert not await env.cog.revive()
        env.t = ts(2026, 10, 8, 9, 59)
        assert not await env.cog.revive()
        env.t = ts(2026, 10, 8, 10, 0)
        assert await env.cog.revive()
    with_env(body, monkeypatch)


def test_history_scan_covers_messages_sent_while_down(monkeypatch):
    async def body(env):
        env.t = ts(2026, 10, 7, 15, 0)
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (LAST_MSG_KEY, str(env.t - 9 * HOUR)))
        env.guild.general.past = [SimpleNamespace(author=SimpleNamespace(bot=True), created_at=at(env.t - 60)),
                                  SimpleNamespace(author=SimpleNamespace(bot=False), created_at=at(env.t - HOUR))]
        assert not await env.cog.revive()
        assert await env.meta(LAST_MSG_KEY) == str(env.t - HOUR)
    with_env(body, monkeypatch)


def test_history_scan_finds_a_long_quiet_room(monkeypatch):
    async def body(env):
        env.t = ts(2026, 10, 7, 15, 0)
        env.guild.general.past = [SimpleNamespace(author=SimpleNamespace(bot=False), created_at=at(env.t - 5 * HOUR))]
        assert await env.cog.revive()
    with_env(body, monkeypatch)


def test_history_failure_falls_back_to_now(monkeypatch):
    async def body(env):
        env.guild.general.history_fail = discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no")
        assert not await env.cog.revive()
        assert env.cog.last_member_at == env.t
    with_env(body, monkeypatch)


def test_only_member_messages_in_general_count(monkeypatch):
    async def body(env):
        assert not await env.cog.revive()
        start = env.t
        env.t += HOUR
        await env.cog.on_message(env.say(11, bot=True))
        await env.cog.on_message(env.say(11, channel=SimpleNamespace(name=config.GENERAL_CHANNEL, parent=object())))
        await env.cog.on_message(env.say(11, channel=env.guild.announcements))
        await env.cog.on_message(env.say(11, type=discord.MessageType.pins_add))
        assert env.cog.last_member_at == start
        await env.cog.on_message(env.say(11))
        assert env.cog.last_member_at == env.t
    with_env(body, monkeypatch)


def test_failed_revival_is_not_retried_every_minute(monkeypatch):
    async def body(env):
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (LAST_MSG_KEY, str(env.t - 4 * HOUR)))
        env.guild.general.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
        assert not await env.cog.revive()
        env.guild.general.fail = None
        env.t += 60
        assert not await env.cog.revive()
        assert await env.db.fetchall("SELECT qid FROM qotd_used") == []
    with_env(body, monkeypatch)


def test_no_questions_no_revival(monkeypatch):
    async def body(env):
        env.cog.questions = []
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (LAST_MSG_KEY, str(env.t - 4 * HOUR)))
        assert not await env.cog.revive()
        assert env.guild.general.sent == []
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- anniversaries
async def run_anniv(env):
    await env.cog.run(V.ANNIV_JOB, E.plan_daily, env.cog.anniversaries)


def test_anniversaries_shout_out_once(monkeypatch):
    async def body(env):
        await env.seen(V.ANNIV_JOB, ts(2026, 10, 1))
        env.t = ts(2026, 10, 7, 11, 0)
        env.member(1, joined_at=datetime(2025, 10, 7, 20, 0, tzinfo=TZ))
        env.member(2, joined_at=datetime(2023, 10, 7, 1, 0, tzinfo=TZ))
        env.member(3, joined_at=datetime(2026, 10, 7, 9, 0, tzinfo=TZ))  # joined today
        env.member(4, joined_at=datetime(2024, 10, 8, 9, 0, tzinfo=TZ))
        env.member(5, joined_at=datetime(2024, 10, 7, 9, 0, tzinfo=TZ), bot=True)
        # 2025-10-08 03:00 UTC is still Oct 7 in Pacific time
        env.member(6, joined_at=datetime(2025, 10, 8, 3, 0, tzinfo=timezone.utc))
        await run_anniv(env)
        sent = env.guild.general.sent
        assert len(sent) == 1
        assert "<@2> (3 years)" in sent[0]["content"]
        assert "<@1> (1 year)" in sent[0]["content"] and "<@6> (1 year)" in sent[0]["content"]
        assert "<@3>" not in sent[0]["content"] and "<@4>" not in sent[0]["content"]
        pinged_only(sent[0], [1, 2, 6])
        assert await env.meta("anniv_last:2") == "2026"
        await run_anniv(env)
        env.t += 3 * HOUR
        await run_anniv(env)
        await env.cog.anniversaries(E.daily_occurrence(V.ANNIV_JOB, datetime(2026, 10, 7).date(), TZ))
        assert len(sent) == 1
    with_env(body, monkeypatch)


def test_anniversaries_first_run_only_marks_done(monkeypatch):
    async def body(env):
        env.t = ts(2026, 10, 7, 11, 5)
        env.member(1, joined_at=datetime(2025, 10, 7, 12, tzinfo=TZ))
        await run_anniv(env)
        assert env.guild.general.sent == []
    with_env(body, monkeypatch)


def test_anniversaries_capped_at_ten(monkeypatch):
    async def body(env):
        await env.seen(V.ANNIV_JOB, ts(2026, 10, 1))
        env.t = ts(2026, 10, 7, 11, 0)
        for uid in range(1, 16):
            env.member(uid, joined_at=datetime(2025, 10, 7, 12, tzinfo=TZ))
        await run_anniv(env)
        sent = env.guild.general.sent
        assert len(sent) == 1 and len(sent[0]["allowed_mentions"].users) == V.ANNIV_MAX
    with_env(body, monkeypatch)


def test_anniversary_send_failure_retries(monkeypatch):
    async def body(env):
        await env.seen(V.ANNIV_JOB, ts(2026, 10, 1))
        env.t = ts(2026, 10, 7, 11, 0)
        env.member(1, joined_at=datetime(2025, 10, 7, 12, tzinfo=TZ))
        env.guild.general.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
        try:
            await run_anniv(env)
        except discord.HTTPException:
            pass
        assert await env.meta("anniv_last:1") is None
        env.guild.general.fail = None
        env.t += 60
        await run_anniv(env)
        assert len(env.guild.general.sent) == 1
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- boosters
def boost(member, since):
    before = SimpleNamespace(premium_since=member.premium_since)
    member.premium_since = since
    return before, member


def test_boost_thanks_and_pays_once(monkeypatch):
    async def body(env):
        m = env.member(7)
        await env.cog.on_member_update(*boost(m, at(env.t)))
        sent = env.guild.general.sent
        assert len(sent) == 1 and "<@7>" in sent[0]["content"] and "1,000" in sent[0]["content"]
        pinged_only(sent[0], [7])
        assert await env.balance(7) == V.BOOST_COINS
        assert (await env.refs()) == {"boost:7:2026-10": 7}
        await env.cog.on_member_update(SimpleNamespace(premium_since=None), m)  # duplicate event
        assert len(sent) == 1
        # Unboost and boost again the same month: thanked again, no second payout.
        await env.cog.on_member_update(*boost(m, None))
        env.t += DAY
        await env.cog.on_member_update(*boost(m, at(env.t)))
        assert len(sent) == 2 and "coins" not in sent[1]["content"]
        assert await env.balance(7) == V.BOOST_COINS
        # Next month: paid again.
        await env.cog.on_member_update(*boost(m, None))
        env.t = ts(2026, 11, 3, 12)
        await env.cog.on_member_update(*boost(m, at(env.t)))
        assert await env.balance(7) == 2 * V.BOOST_COINS
    with_env(body, monkeypatch)


def test_young_account_booster_is_thanked_without_coins(monkeypatch):
    async def body(env):
        m = env.member(7, created_at=at(env.t - 5 * DAY))
        await env.cog.on_member_update(*boost(m, at(env.t)))
        assert len(env.guild.general.sent) == 1 and "coins" not in env.guild.general.sent[0]["content"]
        assert await env.balance(7) == 0
    with_env(body, monkeypatch)


def test_other_member_updates_ignored(monkeypatch):
    async def body(env):
        m = env.member(7, premium_since=at(env.t - DAY))
        await env.cog.on_member_update(SimpleNamespace(premium_since=at(env.t - DAY)), m)
        b = env.member(8, bot=True)
        await env.cog.on_member_update(*boost(b, at(env.t)))
        await env.cog.on_member_update(object(), object())  # never raises
        assert env.guild.general.sent == []
    with_env(body, monkeypatch)


def test_sweep_catches_boosts_missed_while_down(monkeypatch):
    async def body(env):
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (STARTED_KEY, str(env.t - 10 * DAY)))
        env.member(7, premium_since=at(env.t - HOUR))  # missed while down
        env.member(8, premium_since=at(env.t - 20 * DAY))  # before the module went live
        env.member(9, premium_since=at(env.t - 5 * DAY))  # too long ago to call it new
        assert await env.cog.sweep_boosts() == 1
        assert await env.cog.sweep_boosts() == 0
        assert len(env.guild.general.sent) == 1 and "<@7>" in env.guild.general.sent[0]["content"]
        # A fresh cog (restart) doesn't thank again either.
        again = Vibes(env.cog.bot, questions=[])
        assert await again.sweep_boosts() == 0
    with_env(body, monkeypatch)


def test_monthly_stipend(monkeypatch):
    async def body(env):
        await env.seen(V.STIPEND_JOB, ts(2026, 10, 2))
        env.member(7, premium_since=at(env.t))
        env.member(8, premium_since=at(env.t), created_at=at(env.t - DAY))  # fresh alt
        env.member(9)
        env.member(10, premium_since=at(env.t), bot=True)
        env.t = ts(2026, 11, 1, 11, 59)
        await env.cog.run(V.STIPEND_JOB, R.plan_monthly, env.cog.stipend)
        assert await env.refs() == {}
        env.t = ts(2026, 11, 1, 12, 0)
        await env.cog.run(V.STIPEND_JOB, R.plan_monthly, env.cog.stipend)
        assert await env.refs() == {"booststipend:2026-11:7": 7}
        await env.cog.stipend(R.latest_due_monthly(V.STIPEND_JOB, env.t, TZ))  # a rerun can't pay twice
        assert await env.balance(7) == V.STIPEND_COINS
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- member of the month
async def voice(env, uid, other, start, hours):
    for u in (uid, other):
        await env.db.execute('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, 5, ?, ?)',
                             (u, start, start + int(hours * HOUR)))


async def messages(env, uid, day, n):
    await env.db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, ?, ?)", (uid, day, n))


async def squad(env, uid, t):
    await env.db.execute("INSERT INTO lfg_posts (game, host_id, size, when_text, created_at) VALUES ('x', 77, 4, 'now', ?)",
                         (t,))
    post = (await env.db.fetchone("SELECT MAX(id) AS id FROM lfg_posts"))["id"]
    await env.db.execute("INSERT INTO lfg_members (post_id, user_id, joined_at) VALUES (?, ?, ?)", (post, uid, t))


async def run_motm(env):
    await env.cog.run(V.MOTM_JOB, R.plan_monthly, env.cog.member_of_month)


def test_member_of_the_month(monkeypatch):
    async def body(env):
        await env.seen(V.MOTM_JOB, ts(2026, 10, 1))
        for uid in (1, 2, 3, 4, 5, 6):
            env.member(uid)
        env.guild.members[2].roles.append(FakeRole(config.MOD_ROLE))  # uid 3 is staff
        env.guild.members[4].created_at = at(ts(2026, 10, 20))  # uid 5 is a fresh account
        oct10 = ts(2026, 10, 10, 20)
        await voice(env, 1, 2, oct10, 3)  # 1 and 2: 3 h each
        await messages(env, 1, "2026-10-11", 100)  # 1: +2
        await squad(env, 1, oct10)  # 1: +2 -> 7
        await messages(env, 3, "2026-10-12", 5000)  # staff: excluded
        await messages(env, 4, "2026-10-12", 4000)  # opted out: excluded
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (4, 0)")
        await messages(env, 5, "2026-10-12", 3000)  # fresh account: excluded
        await messages(env, 6, "2026-11-01", 3000)  # November: not counted
        await messages(env, 6, "2026-09-30", 3000)  # September: not counted
        await messages(env, 77, "2026-10-12", 3000)  # left the server
        env.t = ts(2026, 11, 1, 12, 30)
        await run_motm(env)
        sent = env.guild.announcements.sent
        assert len(sent) == 1
        text = sent[0]["content"]
        assert "<@1>" in text and "October 2026" in text and "3.0 h" in text and "100 messages" in text
        assert "1 squad" in text
        pinged_only(sent[0], [1])
        assert await env.refs() == {"motm:2026-10": 1}
        assert await env.balance(1) == V.MOTM_COINS
        await run_motm(env)
        assert len(sent) == 1
    with_env(body, monkeypatch)


def test_member_of_the_month_retry_keeps_the_winner(monkeypatch):
    async def body(env):
        await env.seen(V.MOTM_JOB, ts(2026, 10, 1))
        env.member(1)
        env.member(2)
        await messages(env, 1, "2026-10-11", 500)
        env.t = ts(2026, 11, 1, 12, 30)
        env.guild.announcements.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
        try:
            await run_motm(env)
        except discord.HTTPException:
            pass
        assert await env.refs() == {"motm:2026-10": 1}
        await messages(env, 2, "2026-10-12", 5000)  # data changed meanwhile
        env.guild.announcements.fail = None
        env.t += 60
        await run_motm(env)
        assert len(env.guild.announcements.sent) == 1 and "<@1>" in env.guild.announcements.sent[0]["content"]
        assert await env.balance(1) == V.MOTM_COINS and await env.balance(2) == 0
    with_env(body, monkeypatch)


def test_quiet_month_has_no_winner(monkeypatch):
    async def body(env):
        await env.seen(V.MOTM_JOB, ts(2026, 10, 1))
        env.member(1)
        await messages(env, 1, "2026-10-11", 10)  # 0.2 points
        env.t = ts(2026, 11, 1, 12, 30)
        await run_motm(env)
        assert env.guild.announcements.sent == [] and await env.refs() == {}
        assert await env.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", ("motm:2026-10",))
    with_env(body, monkeypatch)


def test_motm_first_run_only_marks_done(monkeypatch):
    async def body(env):
        env.member(1)
        await messages(env, 1, "2026-10-11", 500)
        env.t = ts(2026, 11, 1, 13, 0)
        await run_motm(env)
        assert env.guild.announcements.sent == []
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- tick
def test_tick_never_raises(monkeypatch):
    async def body(env):
        async def boom(*a):
            raise RuntimeError("x")
        env.cog.revive = boom
        env.cog.sweep_boosts = boom
        await env.cog.tick.coro(env.cog)
    with_env(body, monkeypatch)
