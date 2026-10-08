"""Offline tests for cogs.quests: a real in-memory SQLite database plus small fakes for
the bot, guild, channels, members and interactions. No network."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import quests as cogmod
from cogs.quests import NUDGE_KEY, STARTED_KEY, Quests
from logic import achievements as A
from logic import quests as Q

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
U1, U2, U3, BOTUSER = 11, 12, 13, 50
T0 = 1_790_000_000
HOUR = 3600
DAY = 24 * HOUR


def run(coro):
    return asyncio.run(coro)


def at(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class FakeText:
    _next = 300

    def __init__(self, name, category=None):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.category = category
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeRole:
    _next = 700

    def __init__(self, name):
        FakeRole._next += 1
        self.id = FakeRole._next
        self.name = name


class FakeMember:
    def __init__(self, uid, guild, bot=False, joined_at=None, roles=(), created_at=None):
        self.id = uid
        # An established account by default; young accounts aren't paid (alt farming).
        self.created_at = created_at or datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.bot = bot
        self.guild = guild
        self.name = f"user{uid}"
        self.display_name = f"user_{uid}*"
        self.mention = f"<@{uid}>"
        self.joined_at = joined_at
        self.roles = [FakeRole("@everyone"), *(FakeRole(r) for r in roles)]
        self.dms = []
        self.dm_fail = None

    async def send(self, content=None, **kwargs):
        if self.dm_fail is not None:
            raise self.dm_fail
        self.dms.append(dict(content=content, **kwargs))


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.gaming = FakeText(config.GAMING_CHANNEL)
        self.text_channels = [self.general, self.gaming]
        self.afk_channel = None
        self.members = []

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls

    async def send_message(self, content=None, **kwargs):
        self.calls.append(("send_message", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, user, guild, channel=None):
        self.calls = []
        self.user = user
        self.guild = guild
        self.channel = channel
        self.response = FakeResponse(self.calls)

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0

    def member(self, uid, joined=None, **kw):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, self.guild, joined_at=at(self.t + 1 if joined is None else joined), **kw)
            self.guild.members.append(m)
        return m

    async def start(self):
        """Module went live at T0."""
        await self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (STARTED_KEY, str(T0)))

    async def done(self, uid):
        return await self.cog.done(uid)

    async def squad(self, uid):
        await self.db.execute("INSERT INTO lfg_posts (game, host_id, size, when_text, created_at) VALUES (?, ?, 4, 'now', ?)",
                              ("valorant", 77, self.t))
        post = (await self.db.fetchone("SELECT MAX(id) AS id FROM lfg_posts"))["id"]
        await self.db.execute("INSERT INTO lfg_members (post_id, user_id, joined_at) VALUES (?, ?, ?)",
                              (post, uid, self.t))

    async def daily(self, uid):
        await self.db.execute("INSERT INTO wallets (user_id, balance, daily_streak, last_daily) VALUES (?, 0, 1, '2026-10-07')",
                              (uid,))

    async def voice(self, uid):
        await self.db.execute('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, 5, ?, ?)',
                              (uid, self.t, self.t + 60))

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    def say(self, uid, channel=None, type=discord.MessageType.default):
        return SimpleNamespace(author=self.member(uid), guild=self.guild, channel=channel or self.guild.general,
                               type=type)

    def voice_update(self, before=None, after=None):
        return (SimpleNamespace(channel=before), SimpleNamespace(channel=after))


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Quests(bot)
        cog.delay = 0
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await cog.drain()
            await db.close()
    run(go())


def pinged_only(sent, uid):
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.roles is False
    assert [u.id for u in am.users] == [uid]


async def finish_all_but_voice(env, uid):
    m = env.member(uid, roles=[config.PLATFORM_ROLES[0]])
    await env.squad(uid)
    await env.daily(uid)
    await env.cog.on_message(env.say(uid))
    await env.cog.check(m)
    return m


# ---------------------------------------------------------------- start key
def test_started_at_is_set_once_on_first_run(monkeypatch):
    async def go(env):
        assert await env.cog.started_at() == T0
        env.t += DAY
        assert await env.cog.started_at() == T0
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- events
def test_message_in_general_records_say_hi_once(monkeypatch):
    async def go(env):
        await env.start()
        await env.cog.on_message(env.say(U1, channel=env.guild.gaming))
        assert await env.done(U1) == set()
        await env.cog.on_message(env.say(U1, type=discord.MessageType.new_member))
        assert await env.done(U1) == set()
        await env.cog.on_message(env.say(U1))
        assert await env.done(U1) == {"say_hi"}
        assert U1 in env.cog.said_hi
    with_env(go, monkeypatch)


def test_bots_and_other_guilds_are_ignored(monkeypatch):
    async def go(env):
        bot_member = env.member(BOTUSER, bot=True)
        await env.cog.on_message(SimpleNamespace(author=bot_member, guild=env.guild, channel=env.guild.general,
                                                 type=discord.MessageType.default))
        other = SimpleNamespace(id=1)
        await env.cog.on_message(SimpleNamespace(author=env.member(U1), guild=other, channel=env.guild.general,
                                                 type=discord.MessageType.default))
        assert await env.done(BOTUSER) == set() and await env.done(U1) == set()
    with_env(go, monkeypatch)


def test_role_update_records_pick_roles_only_for_listed_roles(monkeypatch):
    async def go(env):
        before = env.member(U1)
        after = FakeMember(U1, env.guild, joined_at=before.joined_at, roles=["LFG"])
        await env.cog.on_member_update(before, after)
        assert await env.done(U1) == set()
        after2 = FakeMember(U1, env.guild, joined_at=before.joined_at, roles=[config.GAMES[0].role])
        await env.cog.on_member_update(after, after2)
        assert await env.done(U1) == {"pick_roles"}
    with_env(go, monkeypatch)


def test_voice_join_records_step_but_not_afk_or_leaving(monkeypatch):
    async def go(env):
        m = env.member(U1)
        afk = SimpleNamespace(id=1, name="AFK")
        env.guild.afk_channel = afk
        await env.cog.on_voice_state_update(m, *env.voice_update(None, afk))
        await env.cog.on_voice_state_update(m, *env.voice_update(SimpleNamespace(id=2), None))
        assert await env.done(U1) == set()
        await env.cog.on_voice_state_update(m, *env.voice_update(None, SimpleNamespace(id=2)))
        assert await env.done(U1) == {"join_voice"}
    with_env(go, monkeypatch)


def test_opted_out_members_skip_message_and_voice_detection(monkeypatch):
    async def go(env):
        m = env.member(U1)
        await env.optout(U1)
        await env.cog.on_message(env.say(U1))
        await env.cog.on_voice_state_update(m, *env.voice_update(None, SimpleNamespace(id=2)))
        await env.voice(U1)  # a leftover row must not count either
        await env.cog.check(m)
        assert await env.done(U1) == set()
    with_env(go, monkeypatch)


def test_interaction_rechecks_tables_after_delay(monkeypatch):
    async def go(env):
        m = env.member(U1)
        await env.daily(U1)
        await env.squad(U1)
        await env.cog.on_interaction(FakeInteraction(m, env.guild))
        await env.cog.drain()
        assert await env.done(U1) == {"claim_daily", "join_squad"}
    with_env(go, monkeypatch)


def test_listener_errors_never_raise(monkeypatch):
    async def go(env):
        async def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(env.cog, "record", boom)
        await env.cog.on_message(env.say(U1))
        m = env.member(U1)
        await env.cog.on_voice_state_update(m, *env.voice_update(None, SimpleNamespace(id=2)))
        await env.cog.on_member_update(env.member(U2),
                                       FakeMember(U2, env.guild, roles=[config.REGION_ROLES[0]]))
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- finishing
def test_new_member_finishing_is_paid_once_and_congratulated(monkeypatch):
    async def go(env):
        await env.start()
        m = await finish_all_but_voice(env, U1)
        assert await economy.balance(env.db, U1) == 0
        assert env.guild.general.sent == []
        await env.cog.on_voice_state_update(m, *env.voice_update(None, SimpleNamespace(id=2)))
        assert await economy.balance(env.db, U1) == Q.REWARD
        (post,) = env.guild.general.sent
        assert "<@11>" in post["content"] and f"{Q.REWARD:,}" in post["content"]
        pinged_only(post, U1)
        row = await env.db.fetchone("SELECT reason FROM ledger WHERE ref = ?", (Q.ref(U1),))
        assert row["reason"] == Q.REASON
        # a later sweep or check doesn't pay again
        await env.voice(U1)
        await env.cog.run_sweep()
        assert await env.cog.maybe_finish(m) is False
        assert await economy.balance(env.db, U1) == Q.REWARD
        assert len(env.guild.general.sent) == 1
    with_env(go, monkeypatch)


def test_starter_badge_only_when_catalogue_has_it(monkeypatch):
    async def go(env):
        await env.start()
        await env.voice(U1)
        await finish_all_but_voice(env, U1)
        held = {r["key"] for r in await env.db.fetchall("SELECT key FROM achievements WHERE user_id = ?", (U1,))}
        assert (Q.BADGE_KEY in held) == (Q.BADGE_KEY in A.BY_KEY)
    with_env(go, monkeypatch)


def test_starter_badge_granted_when_present(monkeypatch):
    monkeypatch.setitem(A.BY_KEY, Q.BADGE_KEY, object())

    async def go(env):
        await env.start()
        await env.voice(U1)
        await finish_all_but_voice(env, U1)
        assert await env.db.fetchone("SELECT 1 FROM achievements WHERE user_id = ? AND key = ?", (U1, Q.BADGE_KEY))
        assert "badge" in env.guild.general.sent[0]["content"].lower()
    with_env(go, monkeypatch)


def test_members_who_joined_before_the_module_are_not_paid(monkeypatch):
    async def go(env):
        await env.start()
        env.member(U1, joined=T0 - DAY, roles=[config.PLATFORM_ROLES[0]])
        await env.voice(U1)
        await finish_all_but_voice(env, U1)
        assert Q.is_complete(await env.done(U1), True)
        assert await economy.balance(env.db, U1) == 0 and env.guild.general.sent == []
    with_env(go, monkeypatch)


def test_finishing_after_30_days_is_not_paid(monkeypatch):
    async def go(env):
        await env.start()
        env.member(U1, joined=T0 + 10, roles=[config.PLATFORM_ROLES[0]])
        env.t = T0 + 10 + Q.NEW_MEMBER_DAYS * DAY + 1
        await env.voice(U1)
        await finish_all_but_voice(env, U1)
        assert Q.is_complete(await env.done(U1), True)
        assert await economy.balance(env.db, U1) == 0
    with_env(go, monkeypatch)


def test_opted_out_member_finishes_with_three_steps(monkeypatch):
    async def go(env):
        await env.start()
        await env.optout(U1)
        await finish_all_but_voice(env, U1)
        assert await env.done(U1) == {"pick_roles", "join_squad", "claim_daily"}
        assert await economy.balance(env.db, U1) == Q.REWARD
    with_env(go, monkeypatch)


def test_congrats_failure_still_pays(monkeypatch):
    async def go(env):
        await env.start()
        env.guild.general.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        await env.voice(U1)
        await finish_all_but_voice(env, U1)
        assert await economy.balance(env.db, U1) == Q.REWARD
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- sweep + nudges
def test_first_sweep_sets_start_and_pays_nobody(monkeypatch):
    async def go(env):
        m = env.member(U1, joined=T0 - DAY, roles=[config.PLATFORM_ROLES[0]])
        await env.squad(U1)
        await env.daily(U1)
        await env.voice(U1)
        await env.db.execute("INSERT INTO quest_steps (user_id, step, at) VALUES (?, 'say_hi', ?)", (U1, T0))
        await env.cog.run_sweep()
        assert await env.done(U1) == {s.key for s in Q.STEPS}
        assert await env.cog.started_at() == T0
        assert await economy.balance(env.db, U1) == 0 and m.dms == []
    with_env(go, monkeypatch)


def test_sweep_skips_bots(monkeypatch):
    async def go(env):
        await env.start()
        env.member(BOTUSER, bot=True, roles=[config.PLATFORM_ROLES[0]])
        await env.cog.run_sweep()
        assert await env.done(BOTUSER) == set()
    with_env(go, monkeypatch)


def test_nudge_once_a_day_after_joining_when_behind(monkeypatch):
    async def go(env):
        await env.start()
        slow = env.member(U1, joined=T0 + 10)
        busy = env.member(U2, joined=T0 + 10, roles=[config.REGION_ROLES[0]])
        await env.squad(U2)
        await env.daily(U2)
        assert await env.cog.run_sweep() == 0  # too soon
        env.t = T0 + 10 + DAY
        assert await env.cog.run_sweep() == 1
        (dm,) = slow.dms
        assert "/quest" in dm["content"] and "0/5" in dm["content"]
        assert dm["allowed_mentions"].users is False
        assert busy.dms == []
        env.t += HOUR
        assert await env.cog.run_sweep() == 0
        assert len(slow.dms) == 1
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (NUDGE_KEY.format(U1),))
    with_env(go, monkeypatch)


def test_nudge_dm_failure_is_fine_and_not_retried(monkeypatch):
    async def go(env):
        await env.start()
        m = env.member(U1, joined=T0 + 10)
        m.dm_fail = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "closed")
        env.t = T0 + 10 + DAY
        assert await env.cog.run_sweep() == 0
        m.dm_fail = None
        env.t += HOUR
        assert await env.cog.run_sweep() == 0
        assert m.dms == []
    with_env(go, monkeypatch)


def test_old_members_are_not_nudged(monkeypatch):
    async def go(env):
        await env.start()
        m = env.member(U1, joined=T0 - DAY)
        env.t = T0 + DAY
        assert await env.cog.run_sweep() == 0 and m.dms == []
    with_env(go, monkeypatch)


def test_sweep_without_guild_is_a_no_op(monkeypatch):
    async def go(env):
        env.bot.guild = None
        assert await env.cog.run_sweep() == 0
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /quest
def test_quest_command_shows_ephemeral_checklist(monkeypatch):
    async def go(env):
        await env.start()
        m = env.member(U1, roles=[config.PLATFORM_ROLES[0]])
        await env.daily(U1)
        inter = FakeInteraction(m, env.guild)
        await Quests.quest.callback(env.cog, inter)
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"] is True
        embed = sent["embed"]
        assert "2/5" in embed.title
        assert embed.description.count("✅") == 2
        assert f"{Q.REWARD:,}" in embed.footer.text
    with_env(go, monkeypatch)


def test_quest_command_for_opted_out_and_paid(monkeypatch):
    async def go(env):
        await env.start()
        await env.optout(U1)
        m = await finish_all_but_voice(env, U1)
        inter = FakeInteraction(m, env.guild)
        await Quests.quest.callback(env.cog, inter)
        embed = inter.of("send_message")[0]["embed"]
        assert "done" in embed.title.lower()
        assert "/privacy" in embed.description
        assert "PAID" in embed.footer.text
    with_env(go, monkeypatch)


def test_quest_command_for_old_member_mentions_no_reward(monkeypatch):
    async def go(env):
        await env.start()
        m = env.member(U1, joined=T0 - DAY)
        inter = FakeInteraction(m, env.guild)
        await Quests.quest.callback(env.cog, inter)
        embed = inter.of("send_message")[0]["embed"]
        assert "0/5" in embed.title and "NO COIN REWARD" in embed.footer.text
    with_env(go, monkeypatch)



def test_young_account_finishes_but_is_not_paid(monkeypatch):
    async def go(env):
        await env.start()
        env.member(U1, created_at=at(env.t - 3 * DAY), roles=[config.PLATFORM_ROLES[0]])
        await env.voice(U1)
        await finish_all_but_voice(env, U1)
        assert await env.done(U1) == {s.key for s in Q.STEPS}
        assert await economy.balance(env.db, U1) == 0
    with_env(go, monkeypatch)
