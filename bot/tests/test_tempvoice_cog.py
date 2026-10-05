"""Offline tests for cogs.tempvoice.TempVoice: a real in-memory SQLite database plus
fakes for the bot, guild, channels, members and interactions. No network.
Wall time is controlled by patching cogs.tempvoice.now; the empty-channel delay
is shortened per test (the real value, 30 s, is asserted separately)."""

import asyncio
import logging
from types import SimpleNamespace

import discord
import pytest

import config
import db as dbmod
from cogs import tempvoice as cogmod
from cogs.tempvoice import TempVoice
from logic import tempvoice as T

GUILD_ID = 999
A, B, C, BOTUSER = 1, 2, 3, 50
T0 = 1_800_000_000.0
FAST = 0.05  # empty-channel delay in tests


def run(coro):
    return asyncio.run(coro)


def http_error(status=400):
    return discord.HTTPException(SimpleNamespace(status=status, reason="boom"), "boom")


# ---------------------------------------------------------------- fakes
class FakeCategory:
    def __init__(self, guild, cid, name):
        self.guild = guild
        self.id = cid
        self.name = name
        self.overwrites = {"@everyone": "category overwrites"}
        self.created = []

    async def create_voice_channel(self, name, **kwargs):
        ch = FakeVoice(self.guild, self.guild.next_id(), name, self, position=kwargs.get("position", 0))
        self.created.append(dict(name=name, **kwargs))
        self.guild.voice_channels.append(ch)
        return ch


class FakeVoice:
    def __init__(self, guild, cid, name, category=None, position=0):
        self.guild = guild
        self.id = cid
        self.name = name
        self.category = category
        self.position = position
        self.voice_states = {}
        self.user_limit = 0
        self.deleted = False
        self.edits = []
        self.sent = []

    async def delete(self, reason=None):
        if self.deleted:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")
        self.deleted = True
        self.guild.voice_channels.remove(self)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        for k, v in kwargs.items():
            if k != "reason":
                setattr(self, k, v)

    async def send(self, content=None, **kwargs):
        self.sent.append(content)

    def __repr__(self):
        return f"<voice {self.name}>"


class FakeMember:
    def __init__(self, env, uid, bot=False, game=None):
        self.env = env
        self.id = uid
        self.bot = bot
        self.guild = env.guild
        self.display_name = f"user{uid}"
        self.activities = [discord.Game(game)] if game else []
        self.voice = None
        self.moves = []
        self.fail_move = False

    def __str__(self):
        return self.display_name

    async def move_to(self, channel, reason=None):
        if self.fail_move or self.voice is None:
            raise http_error(400)  # "Target user is not connected to voice"
        self.moves.append(channel)
        self.env.place(self, channel)  # Discord sends the voice update later, over the gateway


class FakeGuild:
    def __init__(self, with_hub=True, with_category=True):
        self.id = GUILD_ID
        self._next = 5000
        self.category = FakeCategory(self, 902, config.VOICE_CATEGORY)
        self.categories = [self.category] if with_category else []
        self.hub = FakeVoice(self, 100, config.NEW_SQUAD_VOICE, self.category, position=1)
        self.squad = FakeVoice(self, 101, config.SQUAD_VOICE, self.category, position=2)
        self.lobby = FakeVoice(self, 102, config.LOBBY_VOICE, self.category, position=3)
        self.voice_channels = ([self.hub] if with_hub else []) + [self.squad, self.lobby]
        self.members = []

    def next_id(self):
        self._next += 1
        return self._next

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return next((c for c in self.voice_channels if c.id == cid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


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

    def reply(self):
        """The text the user saw (first message or followup)."""
        texts = [kw["content"] for k, kw in self.calls if k in ("send_message", "followup")]
        assert len(texts) == 1, self.calls
        return texts[0]

    def ephemeral(self):
        return all(kw.get("ephemeral") for k, kw in self.calls if k in ("send_message", "followup", "defer"))


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0
        self.queued = []

    def member(self, uid, **kw):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(self, uid, **kw)
            self.guild.members.append(m)
        return m

    def place(self, member, channel):
        """Update voice state like Discord would and queue the gateway event."""
        before = member.voice.channel if member.voice else None
        for ch in self.guild.voice_channels:
            ch.voice_states.pop(member.id, None)
        if channel is not None:
            channel.voice_states[member.id] = SimpleNamespace(channel=channel)
        member.voice = SimpleNamespace(channel=channel) if channel is not None else None
        self.queued.append((member, before, channel))

    async def settle(self):
        """Deliver queued voice events (including ones caused by bot moves)."""
        while self.queued:
            member, before, after = self.queued.pop(0)
            await self.cog.on_voice_state_update(member, SimpleNamespace(channel=before),
                                                 SimpleNamespace(channel=after))

    async def voice(self, uid, channel, at=None):
        if at is not None:
            self.t = at
        self.place(self.member(uid), channel)
        await self.settle()

    def temps(self):
        return [c for c in self.guild.voice_channels if c.id in self.cog.owners]

    async def rows(self):
        return {r["channel_id"]: r["owner_id"]
                for r in await self.db.fetchall("SELECT channel_id, owner_id FROM temp_voice")}

    def inter(self, uid):
        return FakeInteraction(self.member(uid))


def with_env(fn, monkeypatch, guild=None, rows=()):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        for channel_id, owner_id in rows:
            await db.execute("INSERT INTO temp_voice (channel_id, owner_id, created_at) VALUES (?, ?, 0)",
                             (channel_id, owner_id))
        g = guild or FakeGuild()
        bot = FakeBot(db, g)
        cog = TempVoice(bot)
        assert cog.empty_delay == T.EMPTY_DELAY == 30
        cog.empty_delay = FAST
        env = Env(db, g, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        await cog.cog_load()
        try:
            await fn(env)
        finally:
            await cog.cog_unload()
            await db.close()
    run(go())


async def wait_out():
    await asyncio.sleep(FAST * 3)


# ---------------------------------------------------------------- create + move
def test_join_hub_creates_named_after_game_and_moves(monkeypatch):
    async def go(env):
        env.member(A, game="Valorant")
        await env.voice(A, env.guild.hub)
        [temp] = env.temps()
        assert temp.name == "🎮 Valorant"
        created = env.guild.category.created[0]
        assert created["position"] == env.guild.hub.position  # right below the hub (ties sort by id)
        assert created["overwrites"] == env.guild.category.overwrites  # no extra Discord permissions
        assert env.member(A).moves == [temp]
        assert A in temp.voice_states and A not in env.guild.hub.voice_states
        assert await env.rows() == {temp.id: A}
        assert A in env.cog.joined[temp.id]
    with_env(go, monkeypatch)


def test_join_hub_without_game_uses_display_name(monkeypatch):
    async def go(env):
        await env.voice(A, env.guild.hub)
        assert [c.name for c in env.temps()] == ["🎮 user1's squad"]
    with_env(go, monkeypatch)


def test_bots_and_other_channels_ignored(monkeypatch):
    async def go(env):
        env.member(BOTUSER, bot=True)
        await env.voice(BOTUSER, env.guild.hub)
        await env.voice(A, env.guild.lobby)
        assert env.temps() == [] and env.guild.category.created == []
    with_env(go, monkeypatch)


def test_other_guild_ignored(monkeypatch):
    async def go(env):
        m = env.member(A)
        m.guild = SimpleNamespace(id=1234)
        await env.voice(A, env.guild.hub)
        assert env.guild.category.created == []
    with_env(go, monkeypatch)


def test_move_failure_deletes_new_channel(monkeypatch):
    async def go(env):
        env.member(A).fail_move = True
        await env.voice(A, env.guild.hub)
        assert len(env.guild.category.created) == 1
        assert env.temps() == [] and await env.rows() == {}
        assert all(c.name != "🎮 user1's squad" for c in env.guild.voice_channels)
    with_env(go, monkeypatch)


def test_create_failure_is_logged_not_raised(monkeypatch, caplog):
    async def go(env):
        async def boom(*a, **k):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        env.guild.category.create_voice_channel = boom
        with caplog.at_level(logging.ERROR):
            await env.voice(A, env.guild.hub)
        assert "voice update" in caplog.text
        assert await env.rows() == {}
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- cooldown
def test_rejoin_within_cooldown_moves_back_to_own_channel(monkeypatch):
    async def go(env):
        await env.voice(A, env.guild.hub, at=T0)
        [temp] = env.temps()
        await env.voice(A, env.guild.hub, at=T0 + 10)  # straight back to the hub
        assert len(env.guild.category.created) == 1
        assert env.member(A).moves == [temp, temp] and A in temp.voice_states
        await wait_out()
        assert not temp.deleted  # the pending delete was cancelled when they came back
    with_env(go, monkeypatch)


def test_cooldown_without_channel_does_nothing(monkeypatch):
    async def go(env):
        env.member(A).fail_move = True
        await env.voice(A, env.guild.hub, at=T0)  # created then cleaned up
        env.member(A).fail_move = False
        await env.voice(A, None, at=T0 + 5)
        await env.voice(A, env.guild.hub, at=T0 + 10)
        assert len(env.guild.category.created) == 1 and env.member(A).moves == []
    with_env(go, monkeypatch)


def test_new_channel_after_cooldown(monkeypatch):
    async def go(env):
        await env.voice(A, env.guild.hub, at=T0)
        await env.voice(B, env.temps()[0], at=T0 + 1)  # keeps A's first channel alive
        await env.voice(A, env.guild.hub, at=T0 + 31)
        assert len(env.guild.category.created) == 2 and len(env.temps()) == 2
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- empty cleanup
def test_empty_channel_deleted_after_delay(monkeypatch):
    async def go(env):
        await env.voice(A, env.guild.hub)
        [temp] = env.temps()
        await env.voice(A, None)
        assert not temp.deleted  # not immediately
        await wait_out()
        assert temp.deleted and await env.rows() == {} and env.cog.owners == {}
        assert env.cog.pending == {}
    with_env(go, monkeypatch)


def test_empty_delete_cancelled_when_someone_joins(monkeypatch):
    async def go(env):
        await env.voice(A, env.guild.hub)
        [temp] = env.temps()
        await env.voice(A, None)
        await env.voice(B, temp)
        await wait_out()
        assert not temp.deleted and await env.rows() == {temp.id: A}
        await env.voice(B, None)  # empties again: a fresh timer
        await wait_out()
        assert temp.deleted
    with_env(go, monkeypatch)


def test_only_bots_left_counts_as_empty(monkeypatch):
    async def go(env):
        env.member(BOTUSER, bot=True)
        await env.voice(A, env.guild.hub)
        [temp] = env.temps()
        await env.voice(BOTUSER, temp)
        await env.voice(A, None)
        await wait_out()
        assert temp.deleted
    with_env(go, monkeypatch)


def test_channel_deleted_by_hand_drops_row(monkeypatch):
    async def go(env):
        await env.voice(A, env.guild.hub)
        [temp] = env.temps()
        await temp.delete()
        await env.cog.on_guild_channel_delete(temp)
        assert await env.rows() == {} and env.cog.owners == {}
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- startup cleanup
def test_startup_cleanup(monkeypatch):
    g = FakeGuild()
    empty = FakeVoice(g, 700, "🎮 old", g.category)
    busy = FakeVoice(g, 701, "🎮 busy", g.category)
    orphan = FakeVoice(g, 702, "🎮 owner gone", g.category)
    g.voice_channels += [empty, busy, orphan]

    async def go(env):
        busy.voice_states[A] = SimpleNamespace(channel=busy)
        orphan.voice_states[C] = SimpleNamespace(channel=orphan)
        await env.cog.on_ready()
        assert empty.deleted and not busy.deleted and not orphan.deleted
        assert await env.rows() == {701: A, 702: C}  # 799 no longer exists; 702's owner B left
        assert set(env.cog.owners) == {701, 702}
        assert A in env.cog.joined[701]
    with_env(go, monkeypatch, guild=g, rows=[(700, A), (701, A), (702, B), (799, B)])


def test_startup_cleanup_without_guild_is_quiet(monkeypatch):
    async def go(env):
        env.bot.settings.guild_id = 1
        await env.cog.on_ready()
        assert await env.rows() == {700: A}
    with_env(go, monkeypatch, rows=[(700, A)])


# ---------------------------------------------------------------- ownership
def test_owner_leaves_longest_member_inherits(monkeypatch):
    async def go(env):
        await env.voice(A, env.guild.hub, at=T0)
        [temp] = env.temps()
        await env.voice(C, temp, at=T0 + 5)
        await env.voice(B, temp, at=T0 + 9)
        temp.voice_states = {B: temp.voice_states[B], C: temp.voice_states[C]}  # B listed first
        await env.voice(A, None, at=T0 + 20)
        assert await env.rows() == {temp.id: C} and env.cog.owners[temp.id] == C
        assert temp.sent and "<@3>" in temp.sent[0]
        assert not temp.deleted
    with_env(go, monkeypatch)


def test_non_owner_leaving_keeps_owner(monkeypatch):
    async def go(env):
        await env.voice(A, env.guild.hub)
        [temp] = env.temps()
        await env.voice(B, temp)
        await env.voice(B, None)
        assert await env.rows() == {temp.id: A}
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- missing hub
@pytest.mark.parametrize("kw", [dict(with_hub=False), dict(with_category=False)])
def test_missing_hub_or_category_logs_once(monkeypatch, caplog, kw):
    g = FakeGuild(**kw)

    async def go(env):
        with caplog.at_level(logging.WARNING, logger="cogs.tempvoice"):
            await env.voice(A, env.guild.lobby)
            await env.voice(B, env.guild.squad)
            await env.cog.on_ready()
        assert caplog.text.count("temp voice off") == 1
        assert env.guild.category.created == []
    with_env(go, monkeypatch, guild=g)


def test_temp_channel_named_like_hub_is_not_a_hub(monkeypatch):
    async def go(env):
        await env.voice(A, env.guild.hub)
        [temp] = env.temps()
        temp.name = config.NEW_SQUAD_VOICE  # renamed by hand in Discord
        env.guild.voice_channels.remove(temp)
        env.guild.voice_channels.insert(0, temp)  # sorts before the real hub
        await env.voice(B, temp)
        assert len(env.guild.category.created) == 1
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /squad
async def squad_in_temp(env):
    await env.voice(A, env.guild.hub)
    [temp] = env.temps()
    await env.voice(B, temp)
    return temp


def test_commands_outside_temp_channel(monkeypatch):
    async def go(env):
        for cmd, args in ((TempVoice.squad_name, ("x",)), (TempVoice.squad_limit, (3,)), (TempVoice.squad_claim, ())):
            inter = env.inter(A)  # not in voice at all
            await cmd.callback(env.cog, inter, *args)
            assert "inside your squad channel" in inter.reply() and inter.ephemeral()
            await env.voice(B, env.guild.lobby)
            inter = env.inter(B)  # in a permanent channel
            await cmd.callback(env.cog, inter, *args)
            assert "inside your squad channel" in inter.reply()
    with_env(go, monkeypatch)


def test_name_and_limit_owner_only(monkeypatch):
    async def go(env):
        temp = await squad_in_temp(env)
        for cmd, arg in ((TempVoice.squad_name, "mine now"), (TempVoice.squad_limit, 2)):
            inter = env.inter(B)
            await cmd.callback(env.cog, inter, arg)
            assert "Only the channel owner" in inter.reply() and inter.ephemeral()
        assert temp.edits == []
    with_env(go, monkeypatch)


def test_owner_sets_limit(monkeypatch):
    async def go(env):
        temp = await squad_in_temp(env)
        inter = env.inter(A)
        await TempVoice.squad_limit.callback(env.cog, inter, 4)
        assert temp.user_limit == 4 and inter.reply() == "Limit set to 4." and inter.ephemeral()
        inter = env.inter(A)
        await TempVoice.squad_limit.callback(env.cog, inter, 0)
        assert temp.user_limit == 0 and inter.reply() == "Limit removed."
    with_env(go, monkeypatch)


def test_rename_and_third_rename_refused(monkeypatch):
    async def go(env):
        temp = await squad_in_temp(env)
        for i, text in enumerate(("chill\nzone", "ranked grind")):
            env.t = T0 + i * 60
            inter = env.inter(A)
            await TempVoice.squad_name.callback(env.cog, inter, text)
            assert "Renamed" in inter.reply() and inter.ephemeral()
        assert temp.name == "ranked grind"
        env.t = T0 + 120
        inter = env.inter(A)
        await TempVoice.squad_name.callback(env.cog, inter, "third")
        assert "twice every 10 minutes" in inter.reply() and "8 minutes" in inter.reply() and inter.ephemeral()
        assert temp.name == "ranked grind" and len(temp.edits) == 2
        env.t = T0 + 600
        inter = env.inter(A)
        await TempVoice.squad_name.callback(env.cog, inter, "third")
        assert temp.name == "third"
    with_env(go, monkeypatch)


def test_rename_rejects_reserved_or_blank(monkeypatch):
    async def go(env):
        temp = await squad_in_temp(env)
        for text in (config.LOBBY_VOICE, "\x00\x01"):
            inter = env.inter(A)
            await TempVoice.squad_name.callback(env.cog, inter, text)
            assert "different name" in inter.reply()
        assert temp.edits == [] and env.cog.renames.wait(temp.id, env.t) == 0
    with_env(go, monkeypatch)


def test_claim(monkeypatch):
    async def go(env):
        temp = await squad_in_temp(env)
        inter = env.inter(B)
        await TempVoice.squad_claim.callback(env.cog, inter)
        assert "still in it" in inter.reply()
        inter = env.inter(A)
        await TempVoice.squad_claim.callback(env.cog, inter)
        assert "already own" in inter.reply()
        # A slips out without the bot seeing it (e.g. during a reconnect)
        temp.voice_states.pop(A)
        inter = env.inter(B)
        await TempVoice.squad_claim.callback(env.cog, inter)
        assert "yours now" in inter.reply() and inter.ephemeral()
        assert await env.rows() == {temp.id: B}
        inter = env.inter(B)
        await TempVoice.squad_limit.callback(env.cog, inter, 5)
        assert temp.user_limit == 5
    with_env(go, monkeypatch)
