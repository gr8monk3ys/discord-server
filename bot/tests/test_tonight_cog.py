"""Offline tests for cogs.tonight: a real in-memory SQLite database plus small fakes for the
bot, guild, channels and members. No network."""

import asyncio
import random
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
from cogs import tonight as cogmod
from cogs import vibes as vibesmod
from cogs.tonight import Tonight
from cogs.vibes import LAST_MSG_KEY, LAST_REVIVAL_KEY, Vibes
from logic import tonight as L

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
MIN = 60
HOUR = 3600
EVERYONE = SimpleNamespace(name="@everyone")


def run(coro):
    return asyncio.run(coro)


def ts(h, mi=0):
    return int(datetime(2026, 10, 7, h, mi, tzinfo=TZ).timestamp())


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown message")


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "missing access")


class FakeMessage:
    def __init__(self, channel, mid, content, bot=True):
        self.channel, self.id, self.content = channel, mid, content
        self.author = SimpleNamespace(bot=bot)
        self.edits = []

    async def edit(self, content=None, **kwargs):
        if self.channel.fail is not None:
            raise self.channel.fail
        if self.id not in self.channel.messages:
            raise not_found()
        self.content = content
        self.edits.append(dict(content=content, **kwargs))


class FakeText:
    def __init__(self, name):
        self.id = 300
        self.name = name
        self.messages: dict[int, FakeMessage] = {}  # in order of posting
        self.sent = []
        self.fail = None
        self.history_fail = None
        self.next_id = 7000

    def add(self, content, bot):
        self.next_id += 1
        self.messages[self.next_id] = FakeMessage(self, self.next_id, content, bot)
        return self.messages[self.next_id]

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))
        return self.add(content, bot=True)

    def get_partial_message(self, mid):
        return self.messages.get(mid) or FakeMessage(self, mid, None)

    def history(self, limit=100, after=None):
        channel = self

        async def gen():
            if channel.history_fail is not None:
                raise channel.history_fail
            for m in [m for m in channel.messages.values() if after is None or m.id > after.id][:limit]:
                yield m
        return gen()


class FakeVoice:
    def __init__(self, cid, name, view=True, connect=True, category=None):
        self.id, self.name = cid, name
        self.view, self.connect = view, connect
        self.category = SimpleNamespace(name=category) if category else None
        self.voice_states = {}

    def permissions_for(self, role):
        assert role is EVERYONE
        return SimpleNamespace(view_channel=self.view, connect=self.connect)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.default_role = EVERYONE
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.text_channels = [self.general]
        self.lobby = FakeVoice(501, config.LOBBY_VOICE)
        self.squad = FakeVoice(502, config.SQUAD_VOICE)
        self.afk = FakeVoice(503, "AFK")
        self.staff = FakeVoice(504, "Staff room", category=config.STAFF_CATEGORY)
        self.hidden = FakeVoice(505, "Private", view=False)
        self.locked = FakeVoice(506, "Locked", connect=False)
        self.voice_channels = [self.lobby, self.squad, self.afk, self.staff, self.hidden, self.locked]
        self.afk_channel = self.afk
        self.members = {}

    def get_member(self, uid):
        return self.members.get(uid)


class FakeBot:
    def __init__(self, db, guild, voice_states=True, presences=True):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.intents = SimpleNamespace(voice_states=voice_states, presences=presences)

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = ts(19)

    def join(self, uid, channel=None, game=None, bot=False):
        """Put a member in a voice channel (the Lobby unless told otherwise)."""
        for ch in self.guild.voice_channels:
            ch.voice_states.pop(uid, None)
        activities = [SimpleNamespace(type=discord.ActivityType.playing, name=game)] if game else []
        self.guild.members[uid] = SimpleNamespace(id=uid, bot=bot, activities=activities,
                                                  display_name=f"Member Name {uid}")
        (channel or self.guild.lobby).voice_states[uid] = object()

    def leave(self, *uids):
        for ch in self.guild.voice_channels:
            for uid in uids or list(ch.voice_states):
                ch.voice_states.pop(uid, None)

    async def minutes(self, n):
        for _ in range(n):
            self.t += MIN
            await self.cog.run_once()

    def say(self, uid=42, bot=False, channel=None):
        channel = channel or self.guild.general
        mid = channel.add("hello", bot=bot).id if channel is self.guild.general else 999_999
        return SimpleNamespace(id=mid, author=SimpleNamespace(id=uid, bot=bot), guild=self.guild, channel=channel)

    def restart(self, **kw):
        self.cog = Tonight(self.bot, environ=kw.pop("environ", {}))
        return self.cog

    async def meta(self, key):
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def state(self):
        return L.load(await self.meta(L.STATE_KEY))

    @property
    def lines(self):
        return [m for m in self.guild.general.messages.values() if m.author.bot]


def with_env(fn, monkeypatch, environ=None, **bot_kw):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild, **bot_kw)
        env = Env(db, guild, bot, Tonight(bot, environ=environ or {}))
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        monkeypatch.setattr(vibesmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


async def session(env, minutes=10, people=(1, 2, 3), **kw):
    for uid in people:
        env.join(uid, **kw)
    await env.cog.run_once()
    await env.minutes(minutes)


# ---------------------------------------------------------------- the line
def test_line_is_posted_after_three_minutes_without_pings_or_names(monkeypatch):
    async def body(env):
        ch = env.guild.general
        env.join(1, game="Risk of Rain 2")
        env.join(2, game="Risk of Rain 2")
        env.join(3)
        await env.cog.run_once()
        await env.minutes(2)
        assert ch.sent == []
        await env.minutes(1)
        assert [s["content"] for s in ch.sent] == [
            "3 people are in <#501> right now, playing Risk of Rain 2. Click the channel to join."]
        am = ch.sent[0]["allowed_mentions"]
        assert am.everyone is False and am.roles is False and am.users is False
        assert set(ch.sent[0]) == {"content", "allowed_mentions"}  # no embed, no view
        assert "Member Name" not in ch.sent[0]["content"]
        assert await env.meta(L.FIRED_KEY) == str(env.t)
    with_env(body, monkeypatch)


def test_quick_join_and_leave_posts_nothing_and_stores_nothing(monkeypatch):
    async def body(env):
        env.join(1)
        env.join(2)
        await env.cog.run_once()
        await env.minutes(2)
        env.leave()
        await env.minutes(10)
        assert env.guild.general.sent == []
        assert await env.state() == L.State()
    with_env(body, monkeypatch)


def test_edited_in_place_then_left_as_a_past_tense_record(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env, minutes=30, people=(1, 2))
        env.join(3)
        env.join(4)
        await env.minutes(30)
        line = env.lines[0]
        assert line.content.startswith("4 people are in <#501>") and len(line.edits) == 1
        env.leave()
        await env.cog.run_once()
        await env.minutes(2)
        assert line.content.startswith("4 people are in")  # empty for under 3 minutes: left alone
        await env.minutes(1)
        assert line.content == "That voice session ran 1 h with up to 4 people."
        am = line.edits[-1]["allowed_mentions"]
        assert am.everyone is False and am.roles is False and am.users is False
        await env.minutes(120)
        assert len(ch.sent) == 1 and len(line.edits) == 2 and line.id in ch.messages  # kept, not deleted
    with_env(body, monkeypatch)


def test_state_holds_no_member_ids(monkeypatch):
    async def body(env):
        big = 100_000_000_000_000_000
        await session(env, people=(big + 1, big + 2), game="Risk of Rain 2")
        raw = await env.meta(L.STATE_KEY)
        assert raw and str(big)[:12] not in raw and "Member Name" not in raw
        tables = await env.db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%tonight%'")
        assert tables == []
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- who counts
def test_bots_do_not_count(monkeypatch):
    async def body(env):
        env.join(1)
        env.join(90, bot=True)
        await env.cog.run_once()
        await env.minutes(10)
        assert env.guild.general.sent == []
    with_env(body, monkeypatch)


def test_afk_staff_hidden_and_locked_channels_do_not_count(monkeypatch):
    async def body(env):
        g = env.guild
        for i, channel in enumerate((g.afk, g.staff, g.hidden, g.locked)):
            env.join(10 * i + 1, channel=channel)
            env.join(10 * i + 2, channel=channel)
        await env.cog.run_once()
        await env.minutes(10)
        assert g.general.sent == [] and await env.cog.rooms(g) == []
        env.join(1, channel=g.squad)
        env.join(2, channel=g.squad)
        assert await env.cog.rooms(g) == [L.Room(502, 2, (None, None))]
    with_env(body, monkeypatch)


def test_a_channel_that_cannot_be_checked_is_not_public(monkeypatch):
    async def body(env):
        env.guild.lobby.permissions_for = None  # raises when called
        env.join(1)
        env.join(2)
        assert await env.cog.rooms(env.guild) == []
    with_env(body, monkeypatch)


def test_opted_out_members_count_but_their_game_is_not_read(monkeypatch):
    async def body(env):
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (2, env.t))
        env.join(1)
        env.join(2, game="Some Private Game")
        env.join(3)
        assert await env.cog.rooms(env.guild) == [L.Room(501, 3, (None, None))]
        await env.cog.run_once()
        await env.minutes(3)
        assert env.lines[0].content == "3 people are in <#501> right now. Click the channel to join."
    with_env(body, monkeypatch)


def test_mixed_and_unshowable_games(monkeypatch):
    async def body(env):
        env.join(1, game="Risk of Rain 2")
        env.join(2, game="Overwatch 2")
        await env.cog.run_once()
        await env.minutes(3)
        assert "playing a few different things" in env.lines[0].content
        env.join(1, game="join discord.gg/abcdef")
        env.join(2, game="join discord.gg/abcdef")
        await env.minutes(2)
        assert env.lines[0].content == "2 people are in <#501> right now. Click the channel to join."
    with_env(body, monkeypatch)


def test_without_the_presence_intent_no_game_is_named(monkeypatch):
    async def body(env):
        await session(env, game="Risk of Rain 2")
        assert env.lines[0].content == "3 people are in <#501> right now. Click the channel to join."
    with_env(body, monkeypatch, presences=False)


# ---------------------------------------------------------------- degrading
def test_without_the_voice_state_intent_nothing_happens(monkeypatch):
    async def body(env):
        await session(env)
        assert env.guild.general.sent == [] and await env.meta(L.STATE_KEY) is None
    with_env(body, monkeypatch, voice_states=False)


def test_without_the_general_channel_nothing_happens(monkeypatch):
    async def body(env):
        env.guild.text_channels = []
        await session(env)
        assert await env.meta(L.STATE_KEY) is None
        env.bot.guild = None  # not in the server at all
        await env.minutes(2)
    with_env(body, monkeypatch)


def test_a_post_that_fails_is_retried_later_not_every_minute(monkeypatch):
    async def body(env):
        ch = env.guild.general
        ch.fail = forbidden()
        await session(env, minutes=3)
        assert (await env.state()).tried_at == env.t and ch.sent == []
        ch.fail = None
        await env.minutes(1)
        assert ch.sent == []
        await env.minutes(1)
        assert len(ch.sent) == 1
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- stacking
def test_second_session_edits_the_old_line_while_it_is_still_the_newest_message(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env)
        env.leave()
        await env.minutes(120)
        fired = await env.meta(L.FIRED_KEY)
        await session(env, people=(1, 2))
        assert len(ch.sent) == 1 and env.lines[0].content.startswith("2 people are in")
        assert int(await env.meta(L.FIRED_KEY)) > int(fired)  # reusing the line counts as it appearing
    with_env(body, monkeypatch)


def test_another_bot_post_under_the_line_means_a_new_line_not_a_buried_edit(monkeypatch):
    """The chat revival, a welcome or an anniversary posted under yesterday's line: editing that
    line would say "right now" where nobody looks, so the new session posts at the bottom."""
    async def body(env):
        ch = env.guild.general
        await session(env)
        env.leave()
        await env.minutes(120)
        await env.cog.on_message(env.say(bot=True))  # question of the day, from a bot
        await session(env, people=(1, 2))
        assert len(ch.sent) == 2 and ch.sent[1]["content"].startswith("2 people are in")
        assert env.lines[0].content.startswith("That voice session ran")
    with_env(body, monkeypatch)


def test_a_bot_post_made_while_down_is_found_in_history(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env)
        env.leave()
        await env.minutes(120)
        env.say(bot=True)  # posted while the bot was down: no event, only history
        env.restart()
        await session(env, people=(1, 2))
        assert len(ch.sent) == 2
    with_env(body, monkeypatch)


def test_second_session_posts_a_new_line_after_a_member_wrote(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env)
        env.leave()
        await env.minutes(120)
        await env.cog.on_message(env.say(channel=SimpleNamespace(name="other", id=1)))  # not general
        await env.cog.on_message(env.say())
        await session(env, people=(1, 2))
        assert len(ch.sent) == 2
        assert env.lines[0].content.startswith("That voice session ran")
        env.leave()
        await env.minutes(120)
        await session(env, people=(1, 2))  # and now nobody wrote after the second line
        assert len(ch.sent) == 2
    with_env(body, monkeypatch)


def test_member_messages_sent_while_down_are_found_in_history(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env)
        env.leave()
        await env.minutes(120)
        env.say()  # written while the bot was down: no event, only history
        env.restart()
        await session(env, people=(1, 2))
        assert len(ch.sent) == 2
    with_env(body, monkeypatch)


def test_unreadable_history_never_stacks(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env)
        env.leave()
        await env.minutes(120)
        env.say()
        env.restart()
        ch.history_fail = forbidden()
        await session(env, people=(1, 2))
        assert len(ch.sent) == 1 and env.lines[0].content.startswith("2 people are in")
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- chat revival (cogs/vibes.py)
def test_no_new_line_within_an_hour_of_the_chat_revival(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (LAST_REVIVAL_KEY, str(env.t - 30 * MIN)))
        await session(env, minutes=29)
        assert ch.sent == []
        await env.minutes(1)
        assert len(ch.sent) == 1
    with_env(body, monkeypatch)


def test_chat_revival_waits_an_hour_after_the_line(monkeypatch):
    async def body(env):
        ch = env.guild.general
        vibes = Vibes(env.bot, questions=["Q one?", "Q two?"], rng=random.Random(1))
        env.t = ts(14)
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (LAST_MSG_KEY, str(env.t - 5 * HOUR)))
        await session(env, minutes=3)
        assert len(ch.sent) == 1
        assert not await vibes.revive()  # quiet for hours, but the voice line just went up
        env.t += 59 * MIN
        assert not await vibes.revive()
        env.t += MIN
        assert await vibes.revive()
        assert len(ch.sent) == 2
    with_env(body, monkeypatch)


def test_chat_revival_is_unchanged_without_a_line(monkeypatch):
    async def body(env):
        vibes = Vibes(env.bot, questions=["Q one?"], rng=random.Random(1))
        env.t = ts(14)
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (LAST_MSG_KEY, str(env.t - 5 * HOUR)))
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (L.FIRED_KEY, "garbage"))
        assert await vibes.revive()
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- restarts
def test_restart_mid_session_keeps_the_same_message(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env, minutes=30)
        env.restart()
        env.t += 2 * MIN
        env.join(4)
        await env.cog.run_once()
        await env.minutes(2)
        assert len(ch.sent) == 1 and env.lines[0].content.startswith("4 people are in")
    with_env(body, monkeypatch)


def test_restart_after_everyone_left_closes_the_line(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env, minutes=30)
        env.leave()
        env.restart()
        env.t += 20 * MIN
        await env.cog.run_once()
        assert len(ch.sent) == 1
        assert env.lines[0].content == "That voice session ran 30 min with up to 3 people."
    with_env(body, monkeypatch)


def test_deleted_message_is_not_recreated_for_that_session(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env)
        del ch.messages[env.lines[0].id]  # a moderator removed it
        env.join(4)
        await env.minutes(30)
        env.leave()
        await env.minutes(10)
        await env.cog.on_message(env.say())
        await session(env, minutes=20, people=(1, 2))  # back within 45 minutes: same session
        state = await env.state()
        assert len(ch.sent) == 1 and state.gone and state.message_id is None
        env.leave()
        await env.minutes(60)
        await session(env, people=(1, 2))  # a later session gets a line again
        assert len(ch.sent) == 2
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- off switch
def test_off_switch_posts_nothing(monkeypatch):
    async def body(env):
        assert not env.cog.enabled
        await session(env)
        await env.cog.on_message(env.say())
        assert env.guild.general.sent == [] and await env.meta(L.STATE_KEY) is None
        assert env.cog.newest_id is None
    with_env(body, monkeypatch, environ={"TONIGHT": "off"})


def test_switching_off_closes_a_live_line_once(monkeypatch):
    async def body(env):
        ch = env.guild.general
        await session(env, minutes=30)
        env.restart(environ={"TONIGHT": "off"})
        env.t += 8 * HOUR
        await env.cog.close_line()
        assert env.lines[0].content == "That voice session ran 30 min with up to 3 people."
        await env.cog.close_line()
        await env.cog.run_once()
        assert len(ch.sent) == 1 and len(env.lines[0].edits) == 1
        assert not (await env.state()).live
    with_env(body, monkeypatch)


def test_cog_starts_the_right_loop(monkeypatch):
    async def body(env):
        started = []
        for environ, name in (({}, "tick"), ({"TONIGHT": "off"}, "wrap_up")):
            cog = Tonight(env.bot, environ=environ)
            monkeypatch.setattr(cog.tick, "start", lambda: started.append("tick"))
            monkeypatch.setattr(cog.wrap_up, "start", lambda: started.append("wrap_up"))
            await cog.cog_load()
            assert started.pop() == name and started == []
    with_env(body, monkeypatch)


def test_registered_with_the_intents_it_reads():
    import main
    assert ("cogs.tonight", {"members", "presences"}) in main.MODULES
    assert not Tonight.__cog_app_commands__  # no commands
