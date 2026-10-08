"""Offline tests for cogs.achievements: a real in-memory SQLite database plus small
fakes for the bot, guild, channels, members and interactions. No network."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import achievements as cogmod
from cogs.achievements import BACKFILL_KEY, Achievements
from logic import achievements as A

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
U1, U2, U3, BOTUSER = 11, 12, 13, 50
T0 = 1_790_000_000
HOUR = 3600
DAY = 24 * HOUR
OLD = datetime(2026, 9, 1, tzinfo=timezone.utc)  # before the founding-member cutoff
NEW = datetime(2026, 12, 1, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


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


class FakeMember:
    def __init__(self, uid, bot=False, joined_at=NEW):
        self.id = uid
        self.bot = bot
        self.name = f"user{uid}"
        self.display_name = f"user_{uid}*"
        self.mention = f"<@{uid}>"
        self.joined_at = joined_at
        self.display_avatar = SimpleNamespace(url=f"https://cdn.example/{uid}.png")


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.staff = SimpleNamespace(name=config.STAFF_CATEGORY)
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.gaming = FakeText(config.GAMING_CHANNEL)
        self.counting = FakeText(config.COUNTING_CHANNEL)
        self.mod = FakeText(config.MOD_CHANNEL, category=self.staff)
        self.text_channels = [self.general, self.gaming, self.counting, self.mod]
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

    async def defer(self, **kwargs):
        self.calls.append(("defer", kwargs))


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, user, guild, channel=None):
        self.calls = []
        self.user = user
        self.guild = guild
        self.channel = channel
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0

    def member(self, uid, **kw):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, **kw)
            m.guild = self.guild
            self.guild.members.append(m)
        return m

    async def held(self, uid):
        return await self.cog.held(uid)

    async def squad(self, host, *joiners):
        await self.db.execute("INSERT INTO lfg_posts (game, host_id, size, when_text, created_at) VALUES (?, ?, 4, 'now', ?)",
                              ("valorant", host, self.t))
        post = (await self.db.fetchone("SELECT MAX(id) AS id FROM lfg_posts"))["id"]
        for uid in (host, *joiners):
            await self.db.execute("INSERT INTO lfg_members (post_id, user_id, joined_at) VALUES (?, ?, ?)",
                                  (post, uid, self.t))

    async def messages(self, uid, n):
        await self.db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, '2026-10-01', ?)", (uid, n))

    async def voice(self, channel, start, end, *uids):
        for uid in uids:
            await self.db.execute('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, ?, ?, ?)',
                                  (uid, channel, start, end))

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def backfill(self):
        await self.db.execute("INSERT INTO meta (key, value) VALUES (?, '1')", (BACKFILL_KEY,))

    def say(self, uid, channel=None):
        return SimpleNamespace(author=self.member(uid), guild=self.guild, channel=channel or self.guild.gaming)


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Achievements(bot)
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


# ---------------------------------------------------------------- sweep
def test_first_sweep_is_silent_then_later_sweeps_congratulate(monkeypatch):
    async def go(env):
        env.member(U1, joined_at=OLD)
        env.member(U2)
        await env.squad(U2, U1)
        assert await env.cog.run_sweep() == 0
        assert await env.held(U1) == {"first_squad", "early_member"}
        assert await env.held(U2) == {"squad_host"}
        assert env.guild.general.sent == []
        assert await env.cog.backfilled()

        await economy.apply(env.db, U2, 10_000, "test", env.t)
        assert await env.cog.run_sweep() == 1
        (post,) = env.guild.general.sent
        assert "<@12>" in post["content"] and "High Roller" in post["content"]
        pinged_only(post, U2)
        assert await env.cog.run_sweep() == 0  # granted once
    with_env(go, monkeypatch)


def test_sweep_caps_posts_but_still_grants(monkeypatch):
    async def go(env):
        await env.backfill()
        uids = list(range(100, 100 + cogmod.SWEEP_POST_LIMIT + 3))
        for uid in uids:
            env.member(uid)
            await env.db.execute("INSERT INTO birthdays (user_id, month, day) VALUES (?, 1, 1)", (uid,))
        assert await env.cog.run_sweep() == cogmod.SWEEP_POST_LIMIT
        for uid in uids:
            assert "birthday" in await env.held(uid)
    with_env(go, monkeypatch)


def test_members_who_left_and_bots_get_nothing(monkeypatch):
    async def go(env):
        env.member(BOTUSER, bot=True)
        await env.squad(U3, BOTUSER)  # U3 is not in the guild
        await env.cog.run_sweep()
        assert await env.held(U3) == set() and await env.held(BOTUSER) == set()
    with_env(go, monkeypatch)


def test_sweep_without_guild_does_nothing(monkeypatch):
    async def go(env):
        env.bot.settings.guild_id = 1
        assert await env.cog.run_sweep() == 0
        assert not await env.cog.backfilled()
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- events
def test_message_triggers_check_and_congratulates_where_it_happened(monkeypatch):
    async def go(env):
        await env.backfill()
        await env.messages(U1, 100)
        await env.cog.on_message(env.say(U1))
        await env.cog.drain()
        assert await env.held(U1) == {"chatty"}
        (post,) = env.guild.gaming.sent
        assert "Chatty" in post["content"]
        pinged_only(post, U1)
        # the cooldown: a second message right away doesn't re-check
        await env.db.execute("UPDATE message_counts SET count = 1000 WHERE user_id = ?", (U1,))
        await env.cog.on_message(env.say(U1))
        await env.cog.drain()
        assert "messages_1000" not in await env.held(U1)
        env.t += cogmod.MESSAGE_COOLDOWN
        await env.cog.on_message(env.say(U1))
        await env.cog.drain()
        assert "messages_1000" in await env.held(U1)
    with_env(go, monkeypatch)


def test_quiet_and_staff_channels_send_congrats_to_general(monkeypatch):
    async def go(env):
        await env.backfill()
        await env.messages(U1, 100)
        await env.cog.on_message(env.say(U1, env.guild.counting))
        await env.cog.drain()
        await env.messages(U2, 100)
        await env.cog.on_message(env.say(U2, env.guild.mod))
        await env.cog.drain()
        assert env.guild.counting.sent == [] and env.guild.mod.sent == []
        assert len(env.guild.general.sent) == 2
    with_env(go, monkeypatch)


def test_bot_and_other_guild_messages_are_ignored(monkeypatch):
    async def go(env):
        await env.backfill()
        env.member(BOTUSER, bot=True)
        await env.cog.on_message(env.say(BOTUSER))
        other = SimpleNamespace(author=env.member(U1), guild=SimpleNamespace(id=1), channel=env.guild.gaming)
        await env.cog.on_message(other)
        assert env.cog.pending == {}
    with_env(go, monkeypatch)


def test_interaction_triggers_check(monkeypatch):
    async def go(env):
        await env.backfill()
        await env.squad(U2, U1)
        await env.cog.on_interaction(FakeInteraction(env.member(U1), env.guild, env.guild.gaming))
        await env.cog.drain()
        assert await env.held(U1) == {"first_squad"}
        assert "Squad Up" in env.guild.gaming.sent[0]["content"]
    with_env(go, monkeypatch)


def test_leaving_voice_grants_counted_voice_hours(monkeypatch):
    async def go(env):
        await env.backfill()
        env.member(U1), env.member(U2)
        await env.voice(777, T0 - 11 * HOUR, T0, U1, U2)  # together for 11 h
        await env.voice(778, T0 - 50 * HOUR, T0, U3)  # alone: doesn't count
        vc = SimpleNamespace(id=777)
        await env.cog.on_voice_state_update(env.member(U1), SimpleNamespace(channel=vc), SimpleNamespace(channel=None))
        await env.cog.on_voice_state_update(env.member(U3), SimpleNamespace(channel=vc), SimpleNamespace(channel=None))
        await env.cog.drain()
        assert await env.held(U1) == {"voice_10h"}
        assert await env.held(U3) == set()
        assert all("general" not in str(p) for p in env.guild.gaming.sent)
        assert len(env.guild.general.sent) == 1
    with_env(go, monkeypatch)


def test_weekly_mvp_and_clip_of_the_week_events(monkeypatch):
    async def go(env):
        await env.backfill()
        env.member(U1), env.member(U2)
        await env.cog.on_weekly_mvp("2026-W40", U1)
        await env.cog.on_clip_of_the_week("2026-W40", U2)
        assert await env.held(U1) == {"weekly_mvp"}
        assert await env.held(U2) == {"clip_week"}
        assert len(env.guild.general.sent) == 2
        await env.cog.on_weekly_mvp("2026-W41", U1)  # already held: no second post
        assert len(env.guild.general.sent) == 2
    with_env(go, monkeypatch)


def test_tournament_won_reads_the_table(monkeypatch):
    async def go(env):
        await env.backfill()
        env.member(U1)
        await env.db.execute("INSERT INTO tournaments (name, size, status, created_by, created_at, winner_id)"
                             " VALUES ('Cup', 8, 'done', ?, ?, ?)", (U2, env.t, U1))
        await env.cog.on_tournament_won(SimpleNamespace(id=1), env.member(U1), "junk", None)
        await env.cog.drain()
        assert await env.held(U1) == {"tourney_win"}
    with_env(go, monkeypatch)


def test_recruiter_and_streak_and_hall_of_fame(monkeypatch):
    async def go(env):
        env.member(U1)
        for n, uid in enumerate((201, 202, 203)):
            await env.db.execute("INSERT INTO joins (user_id, joined_at, inviter_id) VALUES (?, ?, ?)",
                                 (uid, env.t - 5 * DAY - n, U1))
        await env.db.execute("INSERT INTO wallets (user_id, balance, daily_streak) VALUES (?, 5, 30)", (U1,))
        await env.db.execute("INSERT INTO starboard (message_id, channel_id, author_id, board_message_id, stars, at)"
                             " VALUES (1, 2, ?, 3, 5, ?)", (U1, env.t))
        await env.db.execute("INSERT INTO starboard (message_id, channel_id, author_id, board_message_id, stars, at)"
                             " VALUES (4, 2, ?, NULL, 1, ?)", (U2, env.t))
        env.member(U2)
        await env.cog.run_sweep()
        assert await env.held(U1) == {"recruiter", "streak_7", "streak_30", "hall_of_fame"}
        assert await env.held(U2) == set()
    with_env(go, monkeypatch)


def test_opted_out_members_get_no_tracking_badges(monkeypatch):
    async def go(env):
        await env.backfill()
        await env.optout(U1)
        await env.messages(U1, 5000)
        await env.squad(U2, U1)
        await env.cog.on_message(env.say(U1))
        await env.cog.drain()
        assert await env.held(U1) == {"first_squad"}
    with_env(go, monkeypatch)


def test_failed_congratulation_still_grants_and_never_raises(monkeypatch):
    async def go(env):
        await env.backfill()
        await env.messages(U1, 100)
        env.guild.gaming.fail = discord.HTTPException(SimpleNamespace(status=403, reason="no"), "no")
        await env.cog.on_message(env.say(U1))
        await env.cog.drain()
        assert await env.held(U1) == {"chatty"}
    with_env(go, monkeypatch)


def test_listeners_swallow_errors(monkeypatch):
    async def go(env):
        async def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(env.cog, "evaluate", boom)
        env.member(U1)
        await env.cog.on_weekly_mvp("k", U1)  # no exception
        await env.cog.on_message(env.say(U1))
        await env.cog.drain()
        await env.cog.on_message(SimpleNamespace())  # malformed event: logged, not raised
        monkeypatch.setattr(env.cog, "run_sweep", boom)
        await env.cog.sweep.coro(env.cog)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /profile, /badges
def field(embed, name):
    return next(f.value for f in embed.fields if f.name.startswith(name))


def test_profile_shows_coins_stats_and_badges(monkeypatch):
    async def go(env):
        target = env.member(U1)
        await economy.apply(env.db, U1, 1234, "daily", env.t)
        await env.messages(U1, 150)
        await env.voice(777, T0 - 2 * HOUR, T0, U1, U2)
        await env.db.execute('INSERT INTO game_sessions (user_id, game, start, "end") VALUES (?, ?, ?, ?)',
                             (U1, "Valorant*", T0 - 3 * HOUR, T0))
        await env.cog.run_sweep()
        inter = FakeInteraction(env.member(U2), env.guild)
        await Achievements.profile.callback(env.cog, inter, target)
        (sent,) = inter.of("followup")
        e = sent["embed"]
        assert e.title == discord.utils.escape_markdown(target.display_name)
        assert e.thumbnail.url == target.display_avatar.url
        assert field(e, "Coins") == "1,234"
        assert field(e, "Season points") == "1,234 this month"
        assert field(e, "Voice") == "2.0 h"
        assert field(e, "Messages") == "150"
        assert field(e, "Top game").startswith("Valorant\\*")
        assert field(e, "Badges") == A.BY_KEY["chatty"].emoji
        assert any(f.name == f"Badges · 1/{A.TOTAL}" for f in e.fields)
        assert sent["allowed_mentions"].users is False
    with_env(go, monkeypatch)


def test_profile_hides_stats_for_opted_out(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.optout(U1)
        await env.messages(U1, 150)
        inter = FakeInteraction(env.member(U1), env.guild)
        await Achievements.profile.callback(env.cog, inter, None)
        e = inter.of("followup")[0]["embed"]
        names = [f.name for f in e.fields]
        assert "Messages" not in names and "Voice" not in names and "Stats" in names
        assert field(e, "Badges") == A.NO_BADGES
    with_env(go, monkeypatch)


def test_profile_of_a_bot_is_refused(monkeypatch):
    async def go(env):
        inter = FakeInteraction(env.member(U1), env.guild)
        await Achievements.profile.callback(env.cog, inter, env.member(BOTUSER, bot=True))
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"] is True
    with_env(go, monkeypatch)


def test_badges_lists_all_with_held_marked(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.db.execute("INSERT INTO achievements (user_id, key, at) VALUES (?, 'chatty', 1)", (U1,))
        inter = FakeInteraction(env.member(U1), env.guild)
        await Achievements.badges.callback(env.cog, inter)
        (sent,) = inter.of("send_message")
        lines = sent["embed"].description.split("\n")
        assert len(lines) == A.TOTAL
        assert sum(line.startswith("✅") for line in lines) == 1
        assert sent["ephemeral"] is True
    with_env(go, monkeypatch)
