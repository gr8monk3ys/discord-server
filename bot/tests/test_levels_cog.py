"""Offline tests for cogs.levels: a real in-memory SQLite database plus small fakes for
the bot, guild, channels, roles, members and interactions. No network."""

import asyncio
import io
import random
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
from PIL import Image

import config
import db as dbmod
from cogs import levels as cogmod
from cogs.levels import BACKFILL_KEY, Levels
from logic import levels as L

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, U1, U2, U3, MOD, BOTUSER, ME = 1, 11, 12, 13, 20, 50, 77
T0 = 1_790_000_000
MIN = 60
VC1, VC2, AFK = 701, 702, 703


def run(coro):
    return asyncio.run(coro)


class FixedRng:
    def __init__(self, value=20):
        self.value = value

    def randint(self, a, b):
        assert (a, b) == (15, 25)
        return self.value


# ---------------------------------------------------------------- fakes
class FakeRole:
    def __init__(self, name, position, managed=False):
        self.id = 5000 + position
        self.name = name
        self.position = position
        self.managed = managed
        self.mention = f"<@&{self.id}>"


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


class FakeThread:
    def __init__(self, parent):
        self.id = parent.id + 10_000
        self.parent = parent
        self.name = "a thread"
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))


class FakeAsset:
    def __init__(self, data=None):
        self.data = data
        self.url = "https://cdn.example/a.png"

    def replace(self, **kwargs):
        return self

    async def read(self):
        if self.data is None:
            raise discord.HTTPException(SimpleNamespace(status=404, reason="nope"), "nope")
        return self.data


class FakeMember:
    def __init__(self, uid, guild, bot=False, roles=(), admin=False):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.name = f"user{uid}"
        self.display_name = f"User *{uid}*"
        self.mention = f"<@{uid}>"
        self.roles = [guild.everyone] + [guild.role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.display_avatar = FakeAsset()
        self.role_calls = []

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position)

    async def add_roles(self, *roles, reason=None):
        self.role_calls.append(("add", [r.name for r in roles]))
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.role_calls.append(("remove", [r.name for r in roles]))
        self.roles = [r for r in self.roles if r not in roles]

    def names(self):
        return {r.name for r in self.roles} - {"@everyone"}


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.everyone = FakeRole("@everyone", 0)
        # Level roles sit below the bot, except Mythic (above it) and Legend (missing).
        self.roles = [self.everyone, FakeRole("Regular", 2), FakeRole("Veteran", 3), FakeRole("Elite", 4),
                      FakeRole("Moderator", 8), FakeRole("Front Desk", 6), FakeRole("Mythic", 9)]
        self.staff = SimpleNamespace(name=config.STAFF_CATEGORY)
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.gaming = FakeText(config.GAMING_CHANNEL)
        self.counting = FakeText(config.COUNTING_CHANNEL)
        self.botcmds = FakeText(config.BOT_COMMANDS_CHANNEL)
        self.modlog = FakeText(config.MOD_LOG_CHANNEL, category=self.staff)
        self.mod = FakeText(config.MOD_CHANNEL, category=self.staff)
        self.text_channels = [self.general, self.gaming, self.counting, self.botcmds, self.modlog, self.mod]
        self.afk_channel = SimpleNamespace(id=AFK)
        self.members = []
        self.me = None

    def role(self, name):
        return next(r for r in self.roles if r.name == name)

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
    def __init__(self, user, guild):
        self.calls = []
        self.user = user
        self.guild = guild
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

    def say(self, uid, content="hello there", channel=None, kind=discord.MessageType.default):
        return SimpleNamespace(type=kind, author=self.member(uid), guild=self.guild, content=content,
                               channel=channel or self.guild.gaming)

    async def xp(self, uid):
        row = await self.db.fetchone("SELECT xp, level, last_msg_at, voice_seconds_counted FROM xp WHERE user_id = ?",
                                     (uid,))
        return dict(row) if row else None

    async def set_row(self, uid, xp, counted=0):
        await self.db.execute("INSERT INTO xp (user_id, xp, level, voice_seconds_counted) VALUES (?, ?, ?, ?)"
                              " ON CONFLICT (user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level,"
                              " voice_seconds_counted = excluded.voice_seconds_counted",
                              (uid, xp, L.level_for(xp), counted))

    async def voice(self, channel, start, end, *uids):
        for uid in uids:
            await self.db.execute('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, ?, ?, ?)',
                                  (uid, channel, start, end))

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def backfilled(self):
        await self.db.execute("INSERT INTO meta (key, value) VALUES (?, '1')", (BACKFILL_KEY,))


def with_env(fn, monkeypatch, rng=None):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Levels(bot, rng=rng or FixedRng())
        env = Env(db, guild, bot, cog)
        guild.me = FakeMember(ME, guild, bot=True, roles=["Front Desk"])
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def pinged_only(sent, uid):
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.roles is False
    assert [u.id for u in am.users] == [uid]


def no_pings(kw):
    am = kw["allowed_mentions"]
    assert am.everyone is False and am.roles is False and am.users is False


# ---------------------------------------------------------------- chat XP
def test_message_earns_xp_then_cooldown(monkeypatch):
    async def go(env):
        await env.cog.on_message(env.say(U1))
        assert (await env.xp(U1))["xp"] == 20
        env.t += 59
        await env.cog.on_message(env.say(U1))
        assert (await env.xp(U1))["xp"] == 20  # still cooling down
        env.t += 1
        await env.cog.on_message(env.say(U1))
        row = await env.xp(U1)
        assert row["xp"] == 40 and row["last_msg_at"] == env.t
    with_env(go, monkeypatch)


def test_message_xp_uses_rng_range(monkeypatch):
    async def go(env):
        for i in range(30):
            await env.cog.on_message(env.say(U1))
            env.t += 60
        xp = (await env.xp(U1))["xp"]
        assert 30 * 15 <= xp <= 30 * 25
    with_env(go, monkeypatch, rng=random.Random(3))


def test_ignored_messages(monkeypatch):
    async def go(env):
        bot = env.member(BOTUSER, bot=True)
        await env.cog.on_message(SimpleNamespace(type=discord.MessageType.default, author=bot, guild=env.guild,
                                                 content="hello bots", channel=env.guild.gaming))
        await env.cog.on_message(env.say(U1, "hi"))  # too short
        await env.cog.on_message(env.say(U1, "   ok   "))
        await env.cog.on_message(env.say(U1, channel=env.guild.counting))
        await env.cog.on_message(env.say(U1, channel=env.guild.botcmds))
        await env.cog.on_message(env.say(U1, channel=env.guild.mod))  # staff category
        await env.cog.on_message(env.say(U1, channel=FakeThread(env.guild.mod)))  # thread in staff
        await env.cog.on_message(env.say(U1, kind=discord.MessageType.pins_add))
        other = SimpleNamespace(id=1234)
        await env.cog.on_message(SimpleNamespace(type=discord.MessageType.default, author=env.member(U2),
                                                 guild=other, content="hello there", channel=env.guild.gaming))
        assert await env.xp(BOTUSER) is None
        assert await env.xp(U1) is None
        assert await env.xp(U2) is None
    with_env(go, monkeypatch)


def test_thread_in_public_channel_counts(monkeypatch):
    async def go(env):
        await env.cog.on_message(env.say(U1, channel=FakeThread(env.guild.gaming)))
        assert (await env.xp(U1))["xp"] == 20
    with_env(go, monkeypatch)


def test_opted_out_earns_nothing(monkeypatch):
    async def go(env):
        await env.optout(U1)
        await env.cog.on_message(env.say(U1))
        assert await env.xp(U1) is None
    with_env(go, monkeypatch)


def test_level_up_posts_in_channel_pinging_only_member(monkeypatch):
    async def go(env):
        await env.set_row(U1, L.total_for_level(5) - 10)
        await env.cog.on_message(env.say(U1))
        assert (await env.xp(U1))["level"] == 5
        (post,) = env.guild.gaming.sent
        assert post["content"].startswith("<@11> reached **level 5**")
        pinged_only(post, U1)
    with_env(go, monkeypatch)


def test_levels_below_the_first_reward_are_not_announced(monkeypatch):
    # Day one would otherwise ping a newcomer for levels 1, 2 and 3 within minutes.
    async def go(env):
        await env.set_row(U1, 90)
        await env.cog.on_message(env.say(U1))
        assert (await env.xp(U1))["level"] == 1
        assert env.guild.gaming.sent == []
    with_env(go, monkeypatch)


def test_level_up_posts_are_rate_limited(monkeypatch):
    async def go(env):
        await env.set_row(U1, L.total_for_level(5) - 10)
        await env.set_row(U2, L.total_for_level(5) - 10)
        await env.cog.on_message(env.say(U1))
        await env.cog.on_message(env.say(U2))  # same channel within the gap: quiet
        assert len(env.guild.gaming.sent) == 1
        assert (await env.xp(U2))["level"] == 5  # still levelled up
    with_env(go, monkeypatch)


def test_listener_never_raises(monkeypatch):
    async def go(env):
        await env.set_row(U1, 90)
        env.guild.gaming.fail = RuntimeError("discord down")
        await env.cog.on_message(env.say(U1))  # announcement fails quietly
        assert (await env.xp(U1))["level"] == 1

        async def boom(*a, **k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "add_message_xp", boom)
        await env.cog.on_message(env.say(U2))
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- roles
def test_reaching_reward_level_swaps_roles(monkeypatch):
    async def go(env):
        m = env.member(U1, roles=["Regular"])
        await env.set_row(U1, L.total_for_level(10) - 5)
        await env.cog.on_message(env.say(U1))
        assert m.names() == {"Veteran"}
        (post,) = env.guild.gaming.sent
        assert "**level 10**" in post["content"] and "**Veteran**" in post["content"]
    with_env(go, monkeypatch)


def test_role_above_bot_or_missing_is_skipped(monkeypatch):
    async def go(env):
        m = env.member(U1, roles=["Elite"])
        assert await env.cog.sync_roles(m, 30) is None  # Legend isn't on the server
        assert m.names() == set()  # Elite removed anyway
        assert await env.cog.sync_roles(m, 55) is None  # Mythic sits above the bot
        assert m.names() == set()
    with_env(go, monkeypatch)


def test_role_sync_failure_never_raises(monkeypatch):
    async def go(env):
        m = env.member(U1)

        async def fail(*roles, reason=None):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no")
        m.add_roles = fail
        assert await env.cog.sync_roles(m, 5) is None
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- voice
def test_voice_sweep_credits_incrementally_and_is_idempotent(monkeypatch):
    async def go(env):
        await env.backfilled()
        env.member(U1), env.member(U2), env.member(U3)
        await env.voice(VC1, T0 - 10 * MIN, T0, U1, U2)  # 10 counted minutes each
        await env.voice(VC2, T0 - 30 * MIN, T0, U3)  # alone: not counted
        await env.cog.run_voice_sweep()
        r1 = await env.xp(U1)
        assert r1["xp"] == 100 and r1["voice_seconds_counted"] == 600
        assert (await env.xp(U2))["xp"] == 100
        assert await env.xp(U3) is None

        await env.cog.run_voice_sweep()  # nothing new: nothing credited
        assert (await env.xp(U1))["xp"] == 100

        # They keep talking (open sessions) for 4.5 more minutes: 4 whole minutes.
        await env.voice(VC1, T0, None, U1, U2)
        env.t = T0 + 4 * MIN + 30
        await env.cog.run_voice_sweep()
        r1 = await env.xp(U1)
        assert r1["xp"] == 140 and r1["voice_seconds_counted"] == 840
        await env.cog.run_voice_sweep()
        assert (await env.xp(U1))["xp"] == 140
        env.t = T0 + 5 * MIN
        await env.cog.run_voice_sweep()  # the leftover 30 s plus 30 s: one more minute
        assert (await env.xp(U1))["xp"] == 150
    with_env(go, monkeypatch)


def test_voice_in_afk_not_counted(monkeypatch):
    async def go(env):
        await env.backfilled()
        await env.voice(AFK, T0 - 10 * MIN, T0, U1, U2)
        await env.cog.run_voice_sweep()
        assert await env.xp(U1) is None
    with_env(go, monkeypatch)


def test_voice_keeps_chat_xp_and_cooldown(monkeypatch):
    async def go(env):
        await env.backfilled()
        await env.cog.on_message(env.say(U1))
        last = (await env.xp(U1))["last_msg_at"]
        await env.voice(VC1, T0 - 3 * MIN, T0, U1, U2)
        await env.cog.run_voice_sweep()
        row = await env.xp(U1)
        assert row["xp"] == 20 + 30 and row["last_msg_at"] == last
    with_env(go, monkeypatch)


def test_voice_opted_out_earns_nothing_and_baseline_restarts(monkeypatch):
    async def go(env):
        await env.backfilled()
        await env.set_row(U1, 500, counted=3600)
        await env.optout(U1)
        await env.voice(VC1, T0 - 10 * MIN, T0, U1, U2)  # leftover rows: never credited
        await env.cog.run_voice_sweep()
        row = await env.xp(U1)
        assert row["xp"] == 500 and row["voice_seconds_counted"] == 0
        assert (await env.xp(U2))["xp"] == 100
    with_env(go, monkeypatch)


def test_voice_level_up_announced_in_bot_commands(monkeypatch):
    async def go(env):
        await env.backfilled()
        env.member(U1), env.member(U2)
        await env.set_row(U1, L.total_for_level(5) - 5)
        await env.voice(VC1, T0 - MIN, T0, U1, U2)
        assert await env.cog.run_voice_sweep() == 1
        (post,) = env.guild.botcmds.sent
        assert "<@11> reached **level 5**" in post["content"]
        pinged_only(post, U1)
    with_env(go, monkeypatch)


def test_first_voice_sweep_is_quiet_but_gives_roles(monkeypatch):
    async def go(env):
        m = env.member(U1)
        env.member(U2)
        await env.voice(VC1, T0 - 600 * MIN, T0, U1, U2)  # 10 h = 6,000 XP
        await env.cog.run_voice_sweep()
        assert (await env.xp(U1))["xp"] == 6000
        assert env.guild.botcmds.sent == []
        assert m.names() == {L.reward_role(L.level_for(6000))}
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (BACKFILL_KEY,))
    with_env(go, monkeypatch)


def test_voice_sweep_loop_never_raises(monkeypatch):
    async def go(env):
        async def boom():
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "run_voice_sweep", boom)
        await env.cog.voice_sweep.coro(env.cog)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /rank and /levels
def test_rank_sends_png_card(monkeypatch):
    async def go(env):
        for uid, xp in ((U1, 300), (U2, 900), (U3, 50)):
            env.member(uid)
            await env.set_row(uid, xp)
        seen = {}
        real = L.render_rank_card

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)
        monkeypatch.setattr(cogmod.L, "render_rank_card", spy)
        inter = FakeInteraction(env.member(U1), env.guild)
        await Levels.rank.callback(env.cog, inter, None)
        assert seen["rank"] == 2 and seen["level"] == L.level_for(300) and seen["total"] == 300
        (sent,) = inter.of("followup")
        f = sent["file"]
        assert f.filename == "rank.png"
        img = Image.open(io.BytesIO(f.fp.read()))
        assert img.format == "PNG" and img.size == (900, 260)
    with_env(go, monkeypatch)


def test_rank_opted_out_and_bots(monkeypatch):
    async def go(env):
        await env.optout(U2)
        inter = FakeInteraction(env.member(U1), env.guild)
        await Levels.rank.callback(env.cog, inter, env.member(U2))
        (reply,) = inter.of("send_message")
        assert reply["ephemeral"] and "turned off" in reply["content"]
        assert "\\*" in reply["content"]  # display name markdown escaped
        inter = FakeInteraction(env.member(U1), env.guild)
        await Levels.rank.callback(env.cog, inter, env.member(BOTUSER, bot=True))
        assert inter.of("send_message")[0]["ephemeral"]
    with_env(go, monkeypatch)


def test_levels_top_hides_opted_out_and_departed(monkeypatch):
    async def go(env):
        for uid, xp in ((U1, 300), (U2, 900), (U3, 5000)):
            env.member(uid)
            await env.set_row(uid, xp)
        await env.set_row(4242, 99999)  # left the server
        await env.optout(U3)
        inter = FakeInteraction(env.member(U1), env.guild)
        await Levels.levels.callback(env.cog, inter)
        (sent,) = inter.of("send_message")
        text = sent["embed"].description
        assert text.splitlines()[0].startswith("`01`  <@12>")
        assert "<@11>" in text and "<@13>" not in text and "4242" not in text
        no_pings(sent)
    with_env(go, monkeypatch)


def test_levels_shows_me_outside_top(monkeypatch):
    async def go(env):
        for uid in range(100, 112):
            env.member(uid)
            await env.set_row(uid, 1000 + uid)
        env.member(U1)
        await env.set_row(U1, 10)
        inter = FakeInteraction(env.member(U1), env.guild)
        await Levels.levels.callback(env.cog, inter)
        text = inter.of("send_message")[0]["embed"].description
        assert text.splitlines()[-1].startswith("`13`  <@11>")
    with_env(go, monkeypatch)


def test_levels_empty(monkeypatch):
    async def go(env):
        inter = FakeInteraction(env.member(U1), env.guild)
        await Levels.levels.callback(env.cog, inter)
        assert "Nobody" in inter.of("send_message")[0]["embed"].description
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /xp
def test_xp_set_by_staff_logs_and_syncs_roles(monkeypatch):
    async def go(env):
        mod = env.member(MOD, roles=["Moderator"])
        m = env.member(U1, roles=["Veteran"])
        await env.set_row(U1, 100, counted=1200)
        inter = FakeInteraction(mod, env.guild)
        await Levels.xp_set.callback(env.cog, inter, m, L.total_for_level(20))
        row = await env.xp(U1)
        assert row["xp"] == L.total_for_level(20) and row["level"] == 20
        assert row["voice_seconds_counted"] == 1200  # voice isn't paid again
        assert m.names() == {"Elite"}
        (reply,) = inter.of("send_message")
        assert reply["ephemeral"]
        (logged,) = env.guild.modlog.sent
        assert "<@20>" in logged["embed"].description and "<@11>" in logged["embed"].description
        no_pings(logged)
    with_env(go, monkeypatch)


def test_xp_reset_clears_roles(monkeypatch):
    async def go(env):
        owner = env.member(OWNER)
        m = env.member(U1, roles=["Regular"])
        await env.set_row(U1, 900)
        inter = FakeInteraction(owner, env.guild)
        await Levels.xp_reset.callback(env.cog, inter, m)
        row = await env.xp(U1)
        assert row["xp"] == 0 and row["level"] == 0
        assert m.names() == set()
        assert len(env.guild.modlog.sent) == 1
    with_env(go, monkeypatch)


def test_xp_commands_staff_only(monkeypatch):
    async def go(env):
        m = env.member(U1)
        await env.set_row(U1, 900)
        inter = FakeInteraction(env.member(U2), env.guild)
        await Levels.xp_set.callback(env.cog, inter, m, 5)
        await Levels.xp_reset.callback(env.cog, inter, m)
        assert (await env.xp(U1))["xp"] == 900
        assert all(r["ephemeral"] for r in inter.of("send_message"))
        assert env.guild.modlog.sent == []
        inter = FakeInteraction(env.member(OWNER), env.guild)
        await Levels.xp_set.callback(env.cog, inter, env.member(BOTUSER, bot=True), 5)
        assert await env.xp(BOTUSER) is None
    with_env(go, monkeypatch)


def test_level_role_with_mod_permissions_is_not_given(monkeypatch):
    async def go(env):
        m = env.member(U1)
        regular = next(r for r in env.guild.roles if r.name == "Regular")
        regular.permissions = discord.Permissions(kick_members=True)
        await env.set_row(U1, L.total_for_level(5) - 10)
        await env.cog.on_message(env.say(U1))
        assert (await env.xp(U1))["level"] == 5
        assert "Regular" not in m.names()
    with_env(go, monkeypatch)
