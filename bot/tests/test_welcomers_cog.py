"""Offline tests for cogs.welcomers: a real in-memory SQLite database plus small fakes for the
bot, guild, channels and members. No network."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
from cogs import welcomers as cogmod
from cogs.stats import opt_out
from cogs.welcomers import Welcomers
from logic import welcomers as W

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
EVERYONE = SimpleNamespace(name="@everyone")
NEWCOMER = 100


def run(coro):
    return asyncio.run(coro)


def ts(h, mi=0, day=7):
    return int(datetime(2026, 10, day, h, mi, tzinfo=TZ).timestamp())


class FakeText:
    def __init__(self, name):
        self.id, self.name = 300, name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeVoice:
    def __init__(self, cid, name, view=True, connect=True, category=None):
        self.id, self.name = cid, name
        self.view, self.connect = view, connect
        self.category = SimpleNamespace(name=category) if category else None
        self.voice_states = {}

    def permissions_for(self, role):
        return SimpleNamespace(view_channel=self.view, connect=self.connect)


class FakeRole:
    def __init__(self, rid, name):
        self.id, self.name, self.mention = rid, name, f"<@&{rid}>"
        self.members = []


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = 1
        self.default_role = EVERYONE
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.text_channels = [self.general]
        self.lobby = FakeVoice(501, config.LOBBY_VOICE)
        self.squad = FakeVoice(502, config.SQUAD_VOICE)
        self.afk = FakeVoice(503, "AFK")
        self.staff_room = FakeVoice(504, "Staff room", category=config.STAFF_CATEGORY)
        self.hidden = FakeVoice(505, "Private", view=False)
        self.voice_channels = [self.lobby, self.squad, self.afk, self.staff_room, self.hidden]
        self.afk_channel = self.afk
        self.welcomer = FakeRole(800, config.WELCOMER_ROLE)
        self.mod = FakeRole(801, config.MOD_ROLE)
        self.roles = [self.welcomer, self.mod]
        self.members = {}

    def get_member(self, uid):
        return self.members.get(uid)


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
        self.t = ts(19)

    def member(self, uid, days_here=2, bot=False, roles=()):
        m = SimpleNamespace(id=uid, bot=bot, guild=self.guild, mention=f"<@{uid}>", roles=list(roles),
                            guild_permissions=SimpleNamespace(administrator=False),
                            joined_at=datetime.fromtimestamp(self.t - days_here * W.DAY, TZ))
        self.guild.members[uid] = m
        for role in roles:
            role.members.append(m)
        return m

    def welcomers(self, *uids):
        for uid in uids:
            self.member(uid, days_here=200, roles=[self.guild.welcomer])

    async def join(self, uid, channel=None, before=None):
        """Move a member into a voice channel (the Lobby unless told otherwise), as Discord would."""
        member = self.guild.members.get(uid) or self.member(uid)
        channel = channel or self.guild.lobby
        for ch in self.guild.voice_channels:
            ch.voice_states.pop(uid, None)
        channel.voice_states[uid] = object()
        await self.cog.on_voice_state_update(member, SimpleNamespace(channel=before), SimpleNamespace(channel=channel))

    async def leave(self, uid):
        member = self.guild.members[uid]
        for ch in self.guild.voice_channels:
            ch.voice_states.pop(uid, None)
        await self.cog.on_voice_state_update(member, SimpleNamespace(channel=self.guild.lobby),
                                             SimpleNamespace(channel=None))

    async def wait(self, seconds):
        step = 10
        for _ in range(0, seconds, step):
            self.t += step
            await self.cog.run_once()

    async def voice_history(self, uid, start):
        await self.db.execute("INSERT INTO voice_sessions (user_id, channel_id, start, \"end\") VALUES (?, ?, ?, ?)",
                              (uid, 501, start, start + 600))

    @property
    def sent(self):
        return self.guild.general.sent


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        env = Env(db, guild, Welcomers(FakeBot(db, guild)))
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        monkeypatch.setenv("CREW_QUIET_HOURS", "")
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def test_a_newcomer_alone_in_the_lobby_pings_the_role_once(monkeypatch):
    async def body(env):
        env.welcomers(10, 11)
        await env.join(NEWCOMER)
        await env.wait(20)
        assert env.sent == []  # still inside the hold
        await env.wait(20)
        assert len(env.sent) == 1
        msg = env.sent[0]
        assert msg["content"] == W.ping_text("<@&800>", f"<@{NEWCOMER}>", f"<#{env.guild.lobby.id}>")
        mentions = msg["allowed_mentions"]
        assert mentions.roles == [env.guild.welcomer] and mentions.users is False and mentions.everyone is False
        assert await env.db.meta_get(W.mark_key(NEWCOMER)) is not None
        # Leaving and coming back (even after the cooldown, with no voice history) is not a second ping.
        await env.leave(NEWCOMER)
        env.t += 2 * W.HOUR
        await env.join(NEWCOMER)
        await env.wait(60)
        assert len(env.sent) == 1
    with_env(body, monkeypatch)


def test_a_member_who_was_in_voice_before_is_not_new(monkeypatch):
    async def body(env):
        env.welcomers(10)
        env.member(NEWCOMER, days_here=3)
        await env.voice_history(NEWCOMER, env.t - W.DAY)
        await env.join(NEWCOMER)
        await env.wait(40)
        assert env.sent == []
    with_env(body, monkeypatch)


def test_the_row_stats_opens_for_this_join_does_not_count_as_history(monkeypatch):
    async def body(env):
        env.welcomers(10)
        await env.join(NEWCOMER)
        await env.voice_history(NEWCOMER, env.t - 1)  # the stats cog saw the same join a moment earlier
        await env.wait(40)
        assert len(env.sent) == 1
    with_env(body, monkeypatch)


def test_old_members_and_bots_are_ignored(monkeypatch):
    async def body(env):
        env.welcomers(10)
        env.member(20, days_here=W.NEW_DAYS + 1)
        env.member(21, bot=True)
        await env.join(20)
        await env.join(21, channel=env.guild.squad)
        await env.wait(40)
        assert env.sent == [] and env.cog.pending == {}
    with_env(body, monkeypatch)


def test_only_public_voice_channels_count(monkeypatch):
    async def body(env):
        env.welcomers(10)
        for uid, channel in ((101, env.guild.afk), (102, env.guild.staff_room), (103, env.guild.hidden)):
            await env.join(uid, channel=channel)
        await env.wait(40)
        assert env.sent == [] and env.cog.pending == {}
    with_env(body, monkeypatch)


def test_someone_who_pops_in_and_out_is_not_pinged_for(monkeypatch):
    async def body(env):
        env.welcomers(10)
        await env.join(NEWCOMER)
        await env.wait(10)
        await env.leave(NEWCOMER)
        await env.wait(40)
        assert env.sent == []
    with_env(body, monkeypatch)


def test_moving_rooms_names_the_room_they_stay_in(monkeypatch):
    async def body(env):
        env.welcomers(10)
        await env.join(NEWCOMER)
        await env.wait(20)
        await env.join(NEWCOMER, channel=env.guild.squad, before=env.guild.lobby)
        await env.wait(20)
        assert env.sent == []  # the hold starts again in the new room
        await env.wait(20)
        assert len(env.sent) == 1 and f"<#{env.guild.squad.id}>" in env.sent[0]["content"]
    with_env(body, monkeypatch)


def test_no_ping_when_a_welcomer_or_staff_is_already_there(monkeypatch):
    async def body(env):
        env.welcomers(10, 11)
        env.guild.lobby.voice_states[10] = object()
        await env.join(NEWCOMER)
        env.member(102)
        env.member(5, days_here=400, roles=[env.guild.mod])
        env.guild.squad.voice_states[5] = object()
        await env.join(102, channel=env.guild.squad)
        await env.wait(40)
        assert env.sent == []
    with_env(body, monkeypatch)


def test_no_ping_when_nobody_else_holds_the_role(monkeypatch):
    async def body(env):
        env.member(NEWCOMER, roles=[env.guild.welcomer])  # a newcomer who opted in themselves
        await env.join(NEWCOMER)
        await env.wait(40)
        assert env.sent == []
    with_env(body, monkeypatch)


def test_a_burst_of_newcomers_is_one_ping(monkeypatch):
    async def body(env):
        env.welcomers(10)
        for uid in (101, 102, 103):
            await env.join(uid)
        await env.wait(40)
        assert len(env.sent) == 1
        env.t += W.COOLDOWN
        await env.join(104, channel=env.guild.squad)
        await env.wait(40)
        assert len(env.sent) == 2
    with_env(body, monkeypatch)


def test_never_in_quiet_hours(monkeypatch):
    async def body(env):
        env.welcomers(10)
        env.t = ts(23)
        await env.join(NEWCOMER)
        await env.wait(40)
        assert env.sent == []
    with_env(body, monkeypatch)


def test_quiet_hours_follow_the_crew_setting(monkeypatch):
    async def body(env):
        monkeypatch.setenv("CREW_QUIET_HOURS", "off")
        env.welcomers(10)
        env.t = ts(23)
        await env.join(NEWCOMER)
        await env.wait(40)
        assert len(env.sent) == 1
    with_env(body, monkeypatch)


def test_members_with_tracking_off_are_skipped(monkeypatch):
    async def body(env):
        env.welcomers(10)
        await opt_out(env.db, NEWCOMER, env.t)
        await env.join(NEWCOMER)
        await env.wait(40)
        assert env.sent == []
        assert await env.db.meta_get(W.mark_key(NEWCOMER)) is None
    with_env(body, monkeypatch)


def test_without_the_role_or_the_channel_nothing_is_posted(monkeypatch):
    async def body(env):
        env.welcomers(10)
        env.guild.roles = [env.guild.mod]
        await env.join(NEWCOMER)
        await env.wait(40)
        env.guild.roles = [env.guild.welcomer, env.guild.mod]
        env.guild.text_channels = []
        await env.join(101)
        await env.wait(40)
        assert env.sent == [] and await env.db.meta_get(W.LAST_PING_KEY) is None
    with_env(body, monkeypatch)


def test_a_failed_post_costs_one_ping_not_a_retry_every_tick(monkeypatch):
    async def body(env):
        env.welcomers(10)
        env.guild.general.fail = discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        await env.join(NEWCOMER)
        await env.wait(60)
        assert await env.db.meta_get(W.mark_key(NEWCOMER)) is not None
        env.guild.general.fail = None
        await env.wait(60)
        assert env.sent == []
    with_env(body, monkeypatch)


def test_other_servers_are_ignored(monkeypatch):
    async def body(env):
        env.welcomers(10)
        stranger = SimpleNamespace(id=NEWCOMER, bot=False, guild=SimpleNamespace(id=1234), joined_at=None)
        await env.cog.on_voice_state_update(stranger, SimpleNamespace(channel=None),
                                            SimpleNamespace(channel=env.guild.lobby))
        assert env.cog.pending == {}
    with_env(body, monkeypatch)


def test_old_marks_are_pruned(monkeypatch):
    async def body(env):
        await env.db.meta_set(W.mark_key(1), env.t - W.MARK_KEEP - 1)
        await env.db.meta_set(W.mark_key(2), env.t - W.DAY)
        await env.db.meta_set(W.LAST_PING_KEY, env.t - W.MARK_KEEP - 1)
        await env.cog.run_once()
        assert await env.db.meta_get(W.mark_key(1)) is None
        assert await env.db.meta_get(W.mark_key(2)) is not None
        assert await env.db.meta_get(W.LAST_PING_KEY) is not None
    with_env(body, monkeypatch)
