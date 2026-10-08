"""Offline integration tests for cogs.stats.Stats: a real in-memory SQLite database
plus lightweight fakes for the bot, guild, channels, members and interactions.
No network. Time is controlled by patching cogs.stats.now."""

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest
from discord import app_commands

import config
import db as dbmod
from cogs import stats as cogmod
from cogs.stats import MVP_JOB, Stats
from logic.schedule import occurrence

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
A, B, C, D, BOTUSER = 1, 2, 3, 4, 50
HOUR = 3600
MIN = 60


def run(coro):
    return asyncio.run(coro)


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


T0 = ts(2026, 9, 30, 12)  # a Wednesday, local noon


def choice(value, name=None):
    return app_commands.Choice(name=name or value, value=value)


# ---------------------------------------------------------------- fakes
class FakeVoice:
    def __init__(self, cid, name, category=None):
        self.id = cid
        self.name = name
        self.category = category
        self.voice_states = {}

    def __repr__(self):
        return f"<voice {self.name}>"


class FakeText:
    def __init__(self, cid, name, category=None):
        self.id = cid
        self.name = name
        self.category = category
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))


class FakeThread:
    def __init__(self, cid, parent):
        self.id = cid
        self.name = "a thread"
        self.parent = parent


class FakeMember:
    def __init__(self, uid, guild, bot=False, activities=()):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.display_name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.display_avatar = SimpleNamespace(url=f"https://cdn.example/{uid}.png")
        self.activities = list(activities)

    def playing(self, *games):
        """A copy of this member with a different activity list (presence before/after)."""
        m = FakeMember(self.id, self.guild, self.bot, [discord.Game(g) for g in games])
        return m


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        staff = SimpleNamespace(id=900, name=config.STAFF_CATEGORY)
        community = SimpleNamespace(id=901, name="01 · community")
        voice_cat = SimpleNamespace(id=902, name="05 · voice")
        self.staff_category = staff
        self.lobby = FakeVoice(100, config.LOBBY_VOICE, voice_cat)
        self.squad = FakeVoice(101, config.SQUAD_VOICE, voice_cat)
        self.afk_channel = FakeVoice(102, "💤 AFK", voice_cat)
        self.staff_voice = FakeVoice(103, "🔒 staff voice", staff)
        self.voice_channels = [self.lobby, self.squad, self.afk_channel, self.staff_voice]
        self.general = FakeText(200, config.GENERAL_CHANNEL, community)
        self.other = FakeText(201, "🎲・random", community)
        self.mod = FakeText(202, config.MOD_CHANNEL, staff)
        self.text_channels = [self.mod, self.other, self.general]
        self.members = []

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class FakeBot:
    def __init__(self, db, guild, presences=True):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.intents = SimpleNamespace(presences=presences, members=True)
        self.dispatched = []
        self.ready = asyncio.Event()

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None

    def dispatch(self, event, *args):
        self.dispatched.append((event, args))

    async def wait_until_ready(self):
        await self.ready.wait()


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls
        self.done = False

    def _finish(self):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self._finish()
        self.calls.append(("send_message", dict(content=content, **kwargs)))

    async def defer(self, **kwargs):
        self._finish()
        self.calls.append(("defer", kwargs))

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, user):
        self.calls = []
        self.user = user
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
            m = FakeMember(uid, self.guild, **kw)
            self.guild.members.append(m)
        return m

    def inter(self, uid):
        return FakeInteraction(self.member(uid))

    async def voice(self, uid, before, after, at=None):
        if at is not None:
            self.t = at
        m = self.member(uid)
        for ch in self.guild.voice_channels:
            ch.voice_states.pop(uid, None)
        if after is not None:
            after.voice_states[uid] = SimpleNamespace(channel=after)
        await self.cog.on_voice_state_update(m, SimpleNamespace(channel=before), SimpleNamespace(channel=after))

    async def say(self, uid, channel=None, at=None, guild="default"):
        if at is not None:
            self.t = at
        msg = SimpleNamespace(type=discord.MessageType.default, author=self.member(uid), channel=channel or self.guild.general,
                              guild=self.guild if guild == "default" else guild)
        await self.cog.on_message(msg)

    async def presence(self, uid, before_games, after_games, at=None):
        if at is not None:
            self.t = at
        m = self.member(uid)
        await self.cog.on_presence_update(m.playing(*before_games), m.playing(*after_games))

    async def rows(self, table, uid=None):
        sql = f"SELECT * FROM {table}" + (" WHERE user_id = ?" if uid is not None else "")
        order = " ORDER BY rowid"
        return [dict(r) for r in await self.db.fetchall(sql + order, (uid,) if uid is not None else ())]

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def jobs(self):
        return {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs")}


def with_env(fn, monkeypatch, presences=True):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild, presences=presences)
        cog = Stats(bot)
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def mvp_key(y, m, d):
    return occurrence(MVP_JOB, date(y, m, d), TZ).key


# ---------------------------------------------------------------- voice
def test_voice_join_move_leave_rows(monkeypatch):
    async def go(env):
        g = env.guild
        await env.voice(A, None, g.lobby, at=T0)
        await env.voice(A, g.lobby, g.squad, at=T0 + 10 * MIN)
        await env.voice(A, g.squad, None, at=T0 + 25 * MIN)
        rows = await env.rows("voice_sessions", A)
        assert [(r["channel_id"], r["start"], r["end"]) for r in rows] == [
            (g.lobby.id, T0, T0 + 10 * MIN),
            (g.squad.id, T0 + 10 * MIN, T0 + 25 * MIN),
        ]
    with_env(go, monkeypatch)


def test_afk_join_closes_without_opening(monkeypatch):
    async def go(env):
        g = env.guild
        await env.voice(A, None, g.lobby, at=T0)
        await env.voice(A, g.lobby, g.afk_channel, at=T0 + 5 * MIN)
        rows = await env.rows("voice_sessions", A)
        assert [(r["channel_id"], r["end"]) for r in rows] == [(g.lobby.id, T0 + 5 * MIN)]
        # AFK -> Lobby opens a fresh session
        await env.voice(A, g.afk_channel, g.lobby, at=T0 + 30 * MIN)
        rows = await env.rows("voice_sessions", A)
        assert len(rows) == 2 and rows[1]["start"] == T0 + 30 * MIN and rows[1]["end"] is None
    with_env(go, monkeypatch)


def test_mute_deafen_same_channel_is_noop(monkeypatch):
    async def go(env):
        g = env.guild
        await env.voice(A, None, g.lobby, at=T0)
        env.t = T0 + 60
        await env.cog.on_voice_state_update(env.member(A), SimpleNamespace(channel=g.lobby),
                                            SimpleNamespace(channel=g.lobby))
        rows = await env.rows("voice_sessions", A)
        assert len(rows) == 1 and rows[0]["end"] is None and rows[0]["start"] == T0
    with_env(go, monkeypatch)


def test_bots_and_other_guilds_ignored(monkeypatch):
    async def go(env):
        g = env.guild
        env.member(BOTUSER, bot=True)
        await env.voice(BOTUSER, None, g.lobby)
        await env.say(BOTUSER)
        bot_member = env.member(BOTUSER)
        await env.cog.on_presence_update(bot_member.playing(), bot_member.playing("Valorant"))
        stranger = FakeMember(A, SimpleNamespace(id=12345))
        await env.cog.on_voice_state_update(stranger, SimpleNamespace(channel=None), SimpleNamespace(channel=g.lobby))
        for table in ("voice_sessions", "message_counts", "game_sessions"):
            assert await env.rows(table) == []
    with_env(go, monkeypatch)


def test_opted_out_user_not_recorded_anywhere(monkeypatch):
    async def go(env):
        await env.optout(A)
        await env.voice(A, None, env.guild.lobby)
        await env.say(A)
        await env.presence(A, [], ["Valorant"])
        env.t += HOUR
        await env.presence(A, ["Valorant"], [])
        await env.voice(A, env.guild.lobby, None)
        for table in ("voice_sessions", "message_counts", "game_sessions"):
            assert await env.rows(table) == []
        await env.say(B)  # others still recorded
        assert len(await env.rows("message_counts", B)) == 1
    with_env(go, monkeypatch)


def test_staff_voice_channel_not_recorded(monkeypatch):
    async def go(env):
        await env.voice(A, None, env.guild.staff_voice)
        assert await env.rows("voice_sessions", A) == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- messages
def test_messages_counted_by_local_day(monkeypatch):
    async def go(env):
        await env.say(A, at=ts(2026, 10, 1, 23, 30))
        await env.say(A, at=ts(2026, 10, 1, 23, 45))
        await env.say(A, at=ts(2026, 10, 2, 0, 30))
        rows = await env.rows("message_counts", A)
        # both instants are 2026-10-02 in UTC; local days differ
        assert [(r["day"], r["count"]) for r in rows] == [("2026-10-01", 2), ("2026-10-02", 1)]
    with_env(go, monkeypatch)


def test_staff_thread_dm_and_bot_messages_excluded(monkeypatch):
    async def go(env):
        g = env.guild
        env.member(BOTUSER, bot=True)
        await env.say(A, channel=g.mod)  # staff category
        await env.say(A, channel=FakeThread(7000, g.mod))  # thread in a staff channel
        await env.say(A, channel=SimpleNamespace(id=8000, name="dm"), guild=None)  # DM
        await env.say(BOTUSER)
        assert await env.rows("message_counts") == []
        await env.say(A, channel=FakeThread(7001, g.other))  # thread in a normal channel counts
        await env.say(A, channel=g.other)
        (row,) = await env.rows("message_counts")
        assert row["user_id"] == A and row["count"] == 2
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- presence
def test_presence_start_switch_stop_and_short_session_deleted(monkeypatch):
    async def go(env):
        await env.presence(A, [], ["Valorant"], at=T0)
        rows = await env.rows("game_sessions", A)
        assert [(r["game"], r["start"], r["end"]) for r in rows] == [("Valorant", T0, None)]
        await env.presence(A, ["Valorant"], ["Valorant"], at=T0 + 60)  # unchanged: no-op
        await env.presence(A, ["Valorant"], ["Apex Legends"], at=T0 + 10 * MIN)
        await env.presence(A, ["Apex Legends"], [], at=T0 + 12 * MIN)  # 2 min: dropped
        rows = await env.rows("game_sessions", A)
        assert [(r["game"], r["start"], r["end"]) for r in rows] == [("Valorant", T0, T0 + 10 * MIN)]
        # exactly 5 minutes is kept
        await env.presence(A, [], ["Chess"], at=T0 + HOUR)
        await env.presence(A, ["Chess"], [], at=T0 + HOUR + 5 * MIN)
        assert [r["game"] for r in await env.rows("game_sessions", A)] == ["Valorant", "Chess"]
    with_env(go, monkeypatch)


def test_non_playing_activities_ignored(monkeypatch):
    async def go(env):
        m = env.member(A)
        after = FakeMember(A, env.guild, activities=[discord.CustomActivity(name="vibing"),
                                                      discord.Streaming(name="stream", url="https://twitch.tv/x")])
        await env.cog.on_presence_update(m.playing(), after)
        assert await env.rows("game_sessions") == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- recovery / heartbeat
async def seed_crash(env):
    """Rows left open by a crash 2h ago; last heartbeat 1h ago; who's in voice now."""
    g = env.guild
    await env.db.execute("INSERT INTO voice_sessions (user_id, channel_id, start) VALUES (?, ?, ?)",
                         (A, g.lobby.id, T0 - 2 * HOUR))
    await env.db.execute("INSERT INTO game_sessions (user_id, game, start) VALUES (?, ?, ?)",
                         (A, "Valorant", T0 - 2 * HOUR))
    await env.db.execute("INSERT INTO meta (key, value) VALUES ('heartbeat', ?)", (str(T0 - HOUR),))
    env.member(A, activities=[discord.Game("Valorant")])
    env.member(B)
    env.member(C)
    env.member(D)
    env.member(BOTUSER, bot=True, activities=[discord.Game("Botting")])
    await env.optout(D)
    for uid in (A, B, BOTUSER, D):
        g.lobby.voice_states[uid] = SimpleNamespace(channel=g.lobby)
    g.afk_channel.voice_states[C] = SimpleNamespace(channel=g.afk_channel)


def test_recover_closes_at_heartbeat_and_opens_current(monkeypatch):
    async def go(env):
        await seed_crash(env)
        env.t = T0
        await env.cog.close_stale_sessions()  # cog_load
        await env.cog.on_ready()
        await env.cog.on_ready()  # reconnect: reconciles again, changes nothing
        voice = await env.rows("voice_sessions")
        assert [(r["user_id"], r["channel_id"], r["start"], r["end"]) for r in voice] == [
            (A, env.guild.lobby.id, T0 - 2 * HOUR, T0 - HOUR),
            (A, env.guild.lobby.id, T0, None),
            (B, env.guild.lobby.id, T0, None),
        ]
        games = await env.rows("game_sessions")
        assert [(r["user_id"], r["game"], r["start"], r["end"]) for r in games] == [
            (A, "Valorant", T0 - 2 * HOUR, T0 - HOUR),
            (A, "Valorant", T0, None),
        ]
    with_env(go, monkeypatch)


def test_recover_gaming_disabled_opens_no_games(monkeypatch):
    async def go(env):
        await seed_crash(env)
        await env.cog.close_stale_sessions()
        await env.cog.reconcile()
        games = await env.rows("game_sessions")
        assert [(r["start"], r["end"]) for r in games] == [(T0 - 2 * HOUR, T0 - HOUR)]
    with_env(go, monkeypatch, presences=False)


def test_recover_without_heartbeat_closes_at_now(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO voice_sessions (user_id, channel_id, start) VALUES (?, ?, ?)",
                             (A, env.guild.lobby.id, T0 - HOUR))
        await env.cog.close_stale_sessions()
        (row,) = await env.rows("voice_sessions")
        assert row["end"] == T0
    with_env(go, monkeypatch)


def test_heartbeat_writes_now(monkeypatch):
    async def go(env):
        await Stats.heartbeat.coro(env.cog)
        env.t = T0 + 60
        await Stats.heartbeat.coro(env.cog)
        row = await env.db.fetchone("SELECT value FROM meta WHERE key = 'heartbeat'")
        assert int(row["value"]) == T0 + 60
    with_env(go, monkeypatch)


def test_startup_ordering_recover_reads_heartbeat_before_loop_overwrites(monkeypatch):
    """Stale sessions close in cog_load, before the heartbeat loop starts or the gateway
    connects, so the old heartbeat is used no matter how the loop is scheduled."""
    async def go(env):
        await seed_crash(env)
        env.t = T0
        await env.cog.close_stale_sessions()  # cog_load, then the loops start
        env.cog.heartbeat.start()
        try:
            await asyncio.sleep(0)
            env.bot.ready.set()  # Client._handle_ready
            task = asyncio.create_task(env.cog.on_ready())  # then dispatch('ready')
            await task
            for _ in range(20):
                await asyncio.sleep(0.01)
        finally:
            env.cog.heartbeat.cancel()
        voice = await env.rows("voice_sessions", A)
        assert voice[0]["end"] == T0 - HOUR
        row = await env.db.fetchone("SELECT value FROM meta WHERE key = 'heartbeat'")
        assert int(row["value"]) == T0  # the loop did tick afterwards
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /stats
def test_stats_counts_voice_only_with_two_people(monkeypatch):
    async def go(env):
        g = env.guild
        await env.voice(A, None, g.lobby, at=T0 - 2 * HOUR)  # alone for 1h
        await env.voice(B, None, g.lobby, at=T0 - HOUR)
        await env.voice(B, g.lobby, None, at=T0 - 30 * MIN)
        await env.voice(A, g.lobby, None, at=T0 - 30 * MIN)
        await env.say(A, at=T0 - 10 * MIN)
        await env.presence(A, [], ["Valorant"], at=T0 - 3 * HOUR)
        await env.presence(A, ["Valorant"], [], at=T0 - 2 * HOUR)
        env.t = T0
        inter = env.inter(A)
        await Stats.stats.callback(env.cog, inter, None)
        assert [k for k, _ in inter.calls] == ["defer", "followup"]
        embed = inter.of("followup")[0]["embed"]
        assert embed.title == "user1"
        assert embed.thumbnail.url == "https://cdn.example/1.png"
        desc = embed.description
        assert desc.count("`VOICE`  30m   `MESSAGES`  1") == 2  # week and all time
        assert desc.count("`TOP GAMES`  Valorant (1h 0m)") == 2
        assert "Past 7 days" in desc and "All time" in desc
    with_env(go, monkeypatch)


def test_stats_for_other_member_and_no_games_line_when_gaming_off(monkeypatch):
    async def go(env):
        inter = env.inter(A)
        await Stats.stats.callback(env.cog, inter, env.member(B))
        embed = inter.of("followup")[0]["embed"]
        assert embed.title == "user2"
        assert "TOP GAMES" not in embed.description
        assert "`VOICE`  0m   `MESSAGES`  0" in embed.description
    with_env(go, monkeypatch, presences=False)


def test_stats_for_opted_out_member(monkeypatch):
    async def go(env):
        await env.optout(B)
        inter = env.inter(A)
        await Stats.stats.callback(env.cog, inter, env.member(B))
        assert inter.calls == [("send_message", {"content": "user2 has stats turned off.", "ephemeral": True})]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /leaderboard
async def seed_messages(env, n=12, base=100):
    day = env.cog.local_day(env.t)
    for i in range(n):
        await env.db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, ?, ?)",
                             (base + i, day, 20 - i))


def test_leaderboard_ranks_and_you_row(monkeypatch):
    async def go(env):
        await seed_messages(env)
        inter = env.inter(111)  # 12th place, count 9
        await Stats.leaderboard.callback(env.cog, inter, choice("messages", "Messages"), None)
        kw = inter.of("followup")[0]
        lines = kw["embed"].description.split("\n")
        assert lines[0] == "`01`  <@100>  20"
        assert lines[9] == "`10`  <@109>  11"
        assert lines[10:] == ["…", "`12`  <@111>  9"]
        assert kw["embed"].title == "Messages · Past 7 days"
        am = kw["allowed_mentions"]
        assert (am.everyone, am.users, am.roles, am.replied_user) == (False, False, False, False)

        inter = env.inter(102)  # inside the top 10: no extra row
        await Stats.leaderboard.callback(env.cog, inter, choice("messages", "Messages"),
                                         choice("all", "All time"))
        desc = inter.of("followup")[0]["embed"].description
        assert "…" not in desc and len(desc.split("\n")) == 10
        assert inter.of("followup")[0]["embed"].title == "Messages · All time"
    with_env(go, monkeypatch)


def test_leaderboard_ties_share_rank_and_voice_duration(monkeypatch):
    async def go(env):
        g = env.guild
        await env.voice(A, None, g.lobby, at=T0 - HOUR)
        await env.voice(B, None, g.lobby, at=T0 - HOUR)
        env.t = T0
        inter = env.inter(C)
        await Stats.leaderboard.callback(env.cog, inter, choice("voice", "Voice time"), None)
        desc = inter.of("followup")[0]["embed"].description
        assert desc == "`01`  <@1>  1h 0m\n`01`  <@2>  1h 0m"
    with_env(go, monkeypatch)


def test_leaderboard_empty(monkeypatch):
    async def go(env):
        inter = env.inter(A)
        await Stats.leaderboard.callback(env.cog, inter, choice("gaming", "Game time"), None)
        assert inter.of("followup")[0]["embed"].description == "Nobody's on the board yet."
    with_env(go, monkeypatch)


def test_leaderboard_gaming_refused_without_presences(monkeypatch):
    async def go(env):
        inter = env.inter(A)
        await Stats.leaderboard.callback(env.cog, inter, choice("gaming", "Game time"), None)
        assert [k for k, _ in inter.calls] == ["send_message"]
        kw = inter.of("send_message")[0]
        assert kw["ephemeral"] is True and "Presence intent" in kw["content"]
    with_env(go, monkeypatch, presences=False)


# ---------------------------------------------------------------- /privacy
async def seed_all(env, uid):
    await env.db.execute("INSERT INTO voice_sessions (user_id, channel_id, start, \"end\") VALUES (?, 100, ?, ?)",
                         (uid, T0 - HOUR, T0))
    await env.db.execute("INSERT INTO game_sessions (user_id, game, start) VALUES (?, 'Valorant', ?)",
                         (uid, T0 - HOUR))
    await env.db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, '2026-09-30', 3)", (uid,))


def test_privacy_off_deletes_own_rows_only_idempotent_then_on(monkeypatch):
    async def go(env):
        await seed_all(env, A)
        await seed_all(env, B)
        for _ in range(2):
            inter = env.inter(A)
            await Stats.privacy.callback(env.cog, inter, choice("off"))
            kw = inter.of("send_message")[0]
            assert kw["ephemeral"] is True and "deleted" in kw["content"]
            for table in ("voice_sessions", "game_sessions", "message_counts"):
                assert await env.rows(table, A) == []
                assert len(await env.rows(table, B)) == 1
            assert [r["user_id"] for r in await env.rows("privacy_optout")] == [A]
        assert await env.db.tracking_allowed(A) is False

        await env.say(A)
        assert await env.rows("message_counts", A) == []

        inter = env.inter(A)
        await Stats.privacy.callback(env.cog, inter, choice("on"))
        assert inter.of("send_message")[0]["content"] == "Tracking is back on, starting now."
        assert await env.db.tracking_allowed(A) is True
        await env.say(A)
        assert len(await env.rows("message_counts", A)) == 1
    with_env(go, monkeypatch)


def test_privacy_on_while_in_voice_tracks_from_that_moment(monkeypatch):
    async def go(env):
        g = env.guild
        await env.optout(A)
        await env.optout(B)
        await env.voice(A, None, g.lobby, at=T0)
        await env.voice(B, None, g.lobby, at=T0)
        for uid in (A, B):
            await Stats.privacy.callback(env.cog, env.inter(uid), choice("on"))
        env.t = T0 + HOUR
        scores = await env.cog.voice_scores(T0, T0 + HOUR)
        assert scores.get(A, 0) == HOUR
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- weekly MVP
async def busy_week(env):
    """Thursday 2026-10-01: A+B in voice 1h, C joins 30m; messages C=5, A=1."""
    g = env.guild
    thu = ts(2026, 10, 1, 20)
    await env.voice(A, None, g.lobby, at=thu)
    await env.voice(B, None, g.lobby, at=thu)
    await env.voice(C, None, g.lobby, at=thu + 30 * MIN)
    for uid in (A, B, C):
        await env.voice(uid, g.lobby, None, at=thu + HOUR)
    for _ in range(5):
        await env.say(C, at=thu + 2 * HOUR)
    await env.say(A, at=thu + 2 * HOUR)


def test_weekly_first_run_marks_done_then_posts_once(monkeypatch):
    async def go(env):
        g = env.guild
        env.t = T0  # Wed: latest due is Sun 9/27, before the bot existed
        await env.cog.run_weekly()
        assert await env.jobs() == {mvp_key(2026, 9, 27)}
        assert g.general.sent == [] and env.bot.dispatched == []

        await busy_week(env)
        env.t = ts(2026, 10, 4, 17, 55)  # not yet
        await env.cog.run_weekly()
        assert g.general.sent == []

        env.t = ts(2026, 10, 4, 18, 0)
        await env.cog.run_weekly()
        env.t = ts(2026, 10, 4, 18, 5)
        await env.cog.run_weekly()  # no repost
        assert len(g.general.sent) == 1
        assert g.other.sent == [] and g.mod.sent == []
        sent = g.general.sent[0]
        desc = sent["embed"].description
        assert desc.startswith(f"**MVP**  <@{A}>")
        assert f"`VOICE   `  <@{A}>  1h 0m" in desc
        assert f"`MESSAGES`  <@{C}>  5" in desc
        assert "GAMING" not in desc  # nobody played
        assert [o.id for o in sent["allowed_mentions"].users] == [A]
        key = mvp_key(2026, 10, 4)
        assert sent["embed"].footer.text == f"WEEKLY · {key.split(':')[1]}"
        assert env.bot.dispatched == [("weekly_mvp", (key, A))]
        assert key in await env.jobs()
    with_env(go, monkeypatch)


def test_weekly_pc_off_three_weeks_posts_only_latest(monkeypatch):
    async def go(env):
        g = env.guild
        env.t = T0
        await env.cog.run_weekly()
        # activity in the week ending Sun 10/25
        thu = ts(2026, 10, 22, 20)
        await env.voice(A, None, g.lobby, at=thu)
        await env.voice(B, None, g.lobby, at=thu)
        await env.voice(A, g.lobby, None, at=thu + HOUR)
        await env.voice(B, g.lobby, None, at=thu + HOUR)
        env.t = ts(2026, 10, 25, 21, 0)  # PC back on Sunday evening
        await env.cog.run_weekly()
        await env.cog.run_weekly()
        assert len(g.general.sent) == 1
        assert env.bot.dispatched == [("weekly_mvp", (mvp_key(2026, 10, 25), A))]
        assert await env.jobs() == {mvp_key(2026, 9, 27), mvp_key(2026, 10, 4), mvp_key(2026, 10, 11),
                                    mvp_key(2026, 10, 18), mvp_key(2026, 10, 25)}
    with_env(go, monkeypatch)


def test_weekly_quiet_week_posts_nothing_but_marked_done(monkeypatch):
    async def go(env):
        env.t = T0
        await env.cog.run_weekly()
        env.t = ts(2026, 10, 4, 18, 1)
        await env.cog.run_weekly()
        assert env.guild.general.sent == [] and env.bot.dispatched == []
        assert mvp_key(2026, 10, 4) in await env.jobs()
    with_env(go, monkeypatch)


def test_weekly_includes_gaming_board_when_enabled(monkeypatch):
    async def go(env):
        env.t = T0
        await env.cog.run_weekly()
        await env.presence(B, [], ["Valorant"], at=ts(2026, 10, 2, 20))
        await env.presence(B, ["Valorant"], [], at=ts(2026, 10, 2, 22))
        env.t = ts(2026, 10, 4, 18, 0)
        await env.cog.run_weekly()
        desc = env.guild.general.sent[0]["embed"].description
        assert f"**MVP**  <@{B}>" in desc and f"`GAMING  `  <@{B}>  2h 0m" in desc
    with_env(go, monkeypatch)


def test_weekly_messages_before_window_start_not_counted(monkeypatch):
    async def go(env):
        await env.say(C, at=ts(2026, 9, 27, 10))  # Sun morning, belongs to the week that ended 9/27 18:00
        env.t = T0
        await env.cog.run_weekly()
        env.t = ts(2026, 10, 4, 18, 0)
        await env.cog.run_weekly()
        assert env.guild.general.sent == []  # nothing happened inside 9/27 18:00 .. 10/4 18:00
    with_env(go, monkeypatch)


def test_reconnect_closes_sessions_for_people_who_left_during_outage(monkeypatch):
    async def go(env):
        g = env.guild
        await env.voice(A, None, g.lobby, at=T0)
        await env.voice(B, None, g.lobby, at=T0)
        # Gateway drops; B leaves and C joins while no events arrive.
        g.lobby.voice_states.pop(B, None)
        g.lobby.voice_states[A] = SimpleNamespace(channel=g.lobby)
        g.lobby.voice_states[C] = SimpleNamespace(channel=g.lobby)
        env.t = T0 + HOUR
        await env.cog.on_ready()  # reconnect rebuilt the cache
        rows = {(r["user_id"], r["end"]) for r in await env.rows("voice_sessions")}
        assert rows == {(A, None), (B, T0 + HOUR), (C, None)}
    with_env(go, monkeypatch)


def test_reconnect_after_network_drop_closes_vanished_sessions_at_disconnect(monkeypatch):
    """The PC stays awake (heartbeat keeps ticking) but the gateway is gone for an hour:
    people who left meanwhile are closed when the gateway went away, not at reconnect."""
    async def go(env):
        g = env.guild
        await env.voice(A, None, g.lobby, at=T0)
        await env.voice(B, None, g.lobby, at=T0)
        await env.presence(B, [], ["Valorant"], at=T0)
        env.t = T0 + 10 * MIN
        await env.cog.on_disconnect()
        await env.cog.on_disconnect()  # repeated reconnect attempts keep the first time
        for minute in range(11, 60):
            env.t = T0 + minute * MIN
            await Stats.heartbeat.coro(env.cog)
        g.lobby.voice_states.pop(B, None)
        g.lobby.voice_states[A] = SimpleNamespace(channel=g.lobby)
        env.member(B).activities = []
        env.t = T0 + HOUR
        await env.cog.on_ready()
        voice = {(r["user_id"], r["end"]) for r in await env.rows("voice_sessions")}
        assert voice == {(A, None), (B, T0 + 10 * MIN)}
        games = await env.rows("game_sessions", B)
        assert [(r["start"], r["end"]) for r in games] == [(T0, T0 + 10 * MIN)]
        # The outage is handled: a later READY with no new disconnect closes at now.
        g.lobby.voice_states.pop(A, None)
        env.t = T0 + 2 * HOUR
        await env.cog.on_ready()
        assert (await env.rows("voice_sessions", A))[0]["end"] == T0 + 2 * HOUR
    with_env(go, monkeypatch)


def test_reconnect_after_sleep_closes_vanished_sessions_at_last_heartbeat(monkeypatch):
    """The PC sleeps: the heartbeat stops and the disconnect is only noticed on wake, so the
    gap in heartbeats (not the late on_disconnect) marks when the gateway went away."""
    async def go(env):
        g = env.guild
        await env.voice(A, None, g.lobby, at=T0)
        await env.voice(B, None, g.lobby, at=T0)
        for minute in range(1, 6):
            env.t = T0 + minute * MIN
            await Stats.heartbeat.coro(env.cog)
        # Asleep from T0+5m to T0+9h. On wake the loop ticks before the socket error.
        env.t = T0 + 9 * HOUR
        await Stats.heartbeat.coro(env.cog)
        await env.cog.on_disconnect()
        for uid in (A, B):
            g.lobby.voice_states.pop(uid, None)
        await env.cog.on_ready()
        rows = {(r["user_id"], r["end"]) for r in await env.rows("voice_sessions")}
        assert rows == {(A, T0 + 5 * MIN), (B, T0 + 5 * MIN)}
    with_env(go, monkeypatch)


def test_resumed_session_forgets_the_disconnect(monkeypatch):
    async def go(env):
        g = env.guild
        await env.voice(A, None, g.lobby, at=T0)
        env.t = T0 + MIN
        await env.cog.on_disconnect()
        env.t = T0 + 2 * MIN
        await env.cog.on_resumed()  # Discord replayed what was missed
        g.lobby.voice_states.pop(A, None)
        env.t = T0 + HOUR
        await env.cog.on_ready()  # some later fresh IDENTIFY with no disconnect seen
        assert (await env.rows("voice_sessions", A))[0]["end"] == T0 + HOUR
    with_env(go, monkeypatch)


def test_privacy_off_racing_a_recorder_leaves_no_rows(monkeypatch):
    """/privacy off can commit between a recorder's opt-out check and its write (the
    lock is FIFO); the write must re-check inside the same statement/transaction."""
    async def go(env):
        g = env.guild
        await env.voice(B, None, g.lobby, at=T0)
        await env.presence(C, [], ["Valorant"], at=T0)
        env.t = T0 + HOUR
        recorders = {
            A: env.say(A),
            B: env.voice(B, g.lobby, g.squad),
            C: env.presence(C, ["Valorant"], ["Minecraft"]),
        }
        leaked = []
        for uid, recorder in recorders.items():
            await asyncio.gather(recorder, Stats.privacy.callback(env.cog, env.inter(uid), choice("off")))
            for table in ("voice_sessions", "message_counts", "game_sessions"):
                leaked += [(uid, table)] if await env.rows(table, uid) else []
        assert leaked == []
    with_env(go, monkeypatch)


def test_weekly_mvp_ignores_fresh_alts_as_company_and_as_winners(monkeypatch):
    from logic import quests as Q

    async def go(env):
        g = env.guild
        env.t = T0
        await env.cog.run_weekly()
        thu = ts(2026, 10, 1, 20)
        alt = ((thu - 3 * 24 * HOUR) * 1000 - Q.DISCORD_EPOCH_MS) << 22
        alt2 = alt + (1 << 22)
        assert not Q.established(alt, thu)
        # A main sits with a fresh alt; two alts keep each other company and chat a lot.
        for uid in (A, alt, alt2):
            await env.voice(uid, None, g.lobby, at=thu)
        for uid in (A, alt, alt2):
            await env.voice(uid, g.lobby, None, at=thu + 3 * HOUR)
        for _ in range(9):
            await env.say(alt, at=thu + 4 * HOUR)
        for _ in range(3):
            await env.say(B, at=thu + 4 * HOUR)
        await env.say(A, at=thu + 4 * HOUR)
        env.t = ts(2026, 10, 4, 18, 0)
        await env.cog.run_weekly()
        (sent,) = g.general.sent
        assert sent["content"] == f"MVP this week: <@{B}>"
        assert f"<@{alt}>" not in sent["embed"].description
    with_env(go, monkeypatch)
