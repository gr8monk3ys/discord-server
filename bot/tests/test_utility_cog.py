"""Offline integration tests for cogs.utility.Utility: a real in-memory SQLite database
plus lightweight fakes for the bot, guild, channels, threads, members and interactions.
No network. Time is controlled by patching cogs.utility.now."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest
from discord import app_commands

import config
import db as dbmod
from cogs import utility as cogmod
from cogs.utility import (RENAMED_KEY, STAT_KEY, TICKET_MESSAGE_KEY, TicketCloseButton, TicketOpenButton,
                          Utility)
from logic import utility as U

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, A, B, C, MOD, KEEPER, BOTUSER, ME = 1, 11, 12, 13, 20, 21, 50, 77
MIN, HOUR, DAY = 60, 3600, 86400
T0 = int(datetime(2026, 10, 7, 15, 0, tzinfo=TZ).timestamp())  # Wednesday 3pm


def run(coro):
    return asyncio.run(coro)


def http_error(cls=discord.HTTPException, status=500):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


def choice(value, name=None):
    return app_commands.Choice(name=name or value, value=value)


# ---------------------------------------------------------------- fakes
class Named:
    _next = 5000

    def __init__(self, name):
        Named._next += 1
        self.id = Named._next
        self.name = name
        self.mention = f"<@&{self.id}>"


class Ids:
    n = 7000

    @classmethod
    def next(cls):
        cls.n += 1
        return cls.n


class FakePartial:
    def __init__(self, thread, mid):
        self.thread = thread
        self.id = mid

    async def add_reaction(self, emoji):
        if self.thread.reaction_failures:
            self.thread.reaction_failures -= 1
            raise http_error(discord.NotFound, 404)
        self.thread.reactions.append(emoji)


class FakeThread:
    def __init__(self, parent, name="a post", tags=(), guild=None):
        self.id = Ids.next()
        self.name = name
        self.parent = parent
        self.guild = guild
        self.mention = f"<#{self.id}>"
        self.applied_tags = list(tags)
        self.archived = False
        self.locked = False
        self.edits = []
        self.sent = []
        self.added = []
        self.reactions = []
        self.reaction_failures = 0
        self.edit_fail = None

    async def edit(self, **kw):
        if self.edit_fail is not None:
            raise self.edit_fail
        self.edits.append(kw)
        if "applied_tags" in kw:
            self.applied_tags = list(kw["applied_tags"])
        self.archived = kw.get("archived", self.archived)
        self.locked = kw.get("locked", self.locked)

    async def add_user(self, user):
        self.added.append(user.id)

    async def send(self, content=None, **kw):
        self.sent.append(dict(content=content, **kw))
        return SimpleNamespace(id=Ids.next())

    def get_partial_message(self, mid):
        return FakePartial(self, mid)


class FakeText:
    def __init__(self, name, guild):
        self.id = Ids.next()
        self.name = name
        self.guild = guild
        self.mention = f"<#{self.id}>"
        self.sent = []
        self.messages = set()
        self.fail = None
        self.threads = []
        self.read_only = False  # members can view but not post

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=True, send_messages=not self.read_only,
                               send_messages_in_threads=not self.read_only)

    async def send(self, content=None, **kw):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kw))
        mid = Ids.next()
        self.messages.add(mid)
        return SimpleNamespace(id=mid)

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise http_error(discord.NotFound, 404)
        return SimpleNamespace(id=mid)

    async def create_thread(self, **kw):
        t = FakeThread(self, name=kw["name"], guild=self.guild)
        t.create_kwargs = kw
        self.threads.append(t)
        self.guild.threads[t.id] = t
        return t


class FakeVoice:
    def __init__(self, name, category, overwrites=None):
        self.id = Ids.next()
        self.name = name
        self.category = category
        self.overwrites = dict(overwrites or {})
        self.edits = []
        self.fail = None

    def overwrites_for(self, target):
        return self.overwrites.get(target, discord.PermissionOverwrite())

    async def set_permissions(self, target, **kw):
        kw.pop("reason", None)
        self.overwrites[target] = discord.PermissionOverwrite(**kw)

    async def edit(self, **kw):
        if self.fail is not None:
            raise self.fail
        self.edits.append(kw)
        self.name = kw.get("name", self.name)


class FakeCategory:
    def __init__(self, name):
        self.id = Ids.next()
        self.name = name
        self.voice_channels = []


class FakeForum:
    def __init__(self, name, tag_names):
        self.id = Ids.next()
        self.name = name
        self.available_tags = [SimpleNamespace(id=Ids.next(), name=n) for n in tag_names]

    def tag(self, name):
        return next(t for t in self.available_tags if t.name == name)


class FakeMember:
    def __init__(self, uid, guild, bot=False, roles=(), admin=False, status=discord.Status.online):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.name = f"user{uid}"
        self.display_name = f"User_{uid}"
        self.mention = f"<@{uid}>"
        self.roles = [guild.role("@everyone")] + [guild.role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.status = status
        self.dms = []
        self.timed_out = False

    def is_timed_out(self):
        return self.timed_out

    async def send(self, content=None, **kw):
        self.dms.append(dict(content=content, **kw))


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.roles = [Named(n) for n in ["@everyone", config.MOD_ROLE, config.KEEPER_ROLE]]
        self.default_role = self.roles[0]
        self.me = Named("Front Desk")
        self.front = FakeCategory("01 · front desk")
        self.categories = [self.front]
        self.help = FakeText(config.HELP_CHANNEL, self)
        self.general = FakeText(config.GENERAL_CHANNEL, self)
        self.text_channels = [self.general, self.help]
        self.suggestions = FakeForum(config.SUGGESTIONS_FORUM, ["Idea", "Accepted", "Denied", "Done", "Bug"])
        self.lfg = FakeForum(config.LFG_FORUM, ["Valorant"])
        self.members = []
        self.member_count = 0
        self.threads = {}
        self.created_voice = []

    def role(self, name):
        return config.match_by_name(self.roles, name)

    @property
    def voice_channels(self):
        return list(self.front.voice_channels)

    def get_channel(self, cid):
        everything = [*self.text_channels, *self.voice_channels]
        return next((c for c in everything if c.id == cid), None)

    def get_thread(self, tid):
        return self.threads.get(tid)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    async def create_voice_channel(self, name, *, category, position, overwrites, reason=None):
        ch = FakeVoice(name, category, overwrites)
        ch.position = position
        category.voice_channels.append(ch)
        self.created_voice.append(ch)
        return ch


class FakeBot:
    def __init__(self, db, guild, presences=True):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.intents = SimpleNamespace(presences=presences, members=True)
        self.dynamic = set()
        self.cogs = {}
        self.users = {}
        self.ready = asyncio.Event()

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def get_channel(self, cid):
        return self.guild.get_channel(cid) or self.guild.get_thread(cid)

    async def fetch_channel(self, cid):
        ch = self.get_channel(cid)
        if ch is None:
            raise http_error(discord.NotFound, 404)
        return ch

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        if uid not in self.users:
            raise http_error(discord.NotFound, 404)
        return self.users[uid]

    def add_dynamic_items(self, *items):
        self.dynamic.update(items)

    def remove_dynamic_items(self, *items):
        self.dynamic.difference_update(items)

    def get_cog(self, name):
        return self.cogs.get(name)

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

    async def send_message(self, content=None, **kw):
        self._finish()
        self.calls.append(("send_message", dict(content=content, **kw)))

    async def defer(self, **kw):
        self._finish()
        self.calls.append(("defer", kw))

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kw):
        self.calls.append(("followup", dict(content=content, **kw)))


class FakeInteraction:
    def __init__(self, bot, user, channel=None):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.channel = channel
        self.channel_id = getattr(channel, "id", None)
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def replies(self):
        return [(kw.get("content"), kw.get("ephemeral")) for k, kw in self.calls
                if k in ("send_message", "followup")]

    def last(self):
        return [kw for k, kw in self.calls if k in ("send_message", "followup")][-1]


class FakeMessage:
    def __init__(self, author, channel, mentions=()):
        self.author = author
        self.guild = author.guild
        self.channel = channel
        self.mentions = list(mentions)
        self.replies = []

    async def reply(self, content=None, **kw):
        self.replies.append(dict(content=content, **kw))


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0
        self.members = {}

    def member(self, uid, **kw):
        if uid not in self.members:
            m = FakeMember(uid, self.guild, **kw)
            self.members[uid] = m
            self.guild.members.append(m)
            self.guild.member_count = len(self.guild.members)
            self.bot.users[uid] = m
        return self.members[uid]

    def inter(self, uid, channel=None):
        return FakeInteraction(self.bot, self.member(uid), channel or self.guild.general)

    async def rows(self, table, where="1"):
        return [dict(r) for r in await self.db.fetchall(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid")]

    async def meta(self, key):
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def remind(self, uid, when, what, channel=None):
        inter = self.inter(uid, channel)
        await Utility.remind.callback(self.cog, inter, when, what)
        return inter

    async def say(self, uid, channel=None, mentions=()):
        msg = FakeMessage(self.member(uid), channel or self.guild.general,
                          [self.member(m) for m in mentions])
        await self.cog.on_message(msg)
        return msg

    def restart(self):
        self.cog = Utility(self.bot)
        self.bot.cogs["Utility"] = self.cog
        return self.cog


def with_env(fn, monkeypatch, presences=True):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild, presences)
        cog = Utility(bot)
        bot.cogs["Utility"] = cog
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        env.member(MOD, roles=[config.MOD_ROLE])
        env.member(KEEPER, roles=[config.KEEPER_ROLE])
        env.member(OWNER)
        env.member(BOTUSER, bot=True)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- load
def test_cog_load_registers_buttons_clears_placeholders_and_unload_removes(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO tickets (thread_id, user_id, opened_at) VALUES (-11, 11, 1)")
        await env.db.execute("INSERT INTO tickets (thread_id, user_id, opened_at) VALUES (55, 12, 1)")
        await env.db.execute("INSERT INTO afk (user_id, reason, since) VALUES (12, 'x', 1)")
        await env.cog.cog_load()
        assert {TicketOpenButton, TicketCloseButton} <= env.bot.dynamic
        assert [r["thread_id"] for r in await env.rows("tickets")] == [55]
        assert env.cog.afk_ids == {12}
        await env.cog.cog_unload()
        assert not env.bot.dynamic
    with_env(go, monkeypatch)


def test_dynamic_items_rebuild_from_custom_id():
    async def go():
        assert isinstance(await TicketOpenButton.from_custom_id(None, None, None), TicketOpenButton)
        assert (await TicketCloseButton.from_custom_id(None, None, None)).item.custom_id == "ticket:close"
    run(go())


# ---------------------------------------------------------------- /remind
def test_remind_saves_and_confirms_privately(monkeypatch):
    async def go(env):
        inter = await env.remind(A, "2h", "  stretch  ")
        (r,) = await env.rows("reminders")
        assert (r["user_id"], r["channel_id"], r["due_at"], r["text"], r["created_at"], r["done"]) == \
            (A, env.guild.general.id, T0 + 2 * HOUR, "stretch", T0, 0)
        ((text, eph),) = inter.replies()
        assert eph and f"<t:{T0 + 2 * HOUR}:R>" in text and f"#{r['id']}" in text
    with_env(go, monkeypatch)


def test_remind_rejects_bad_times_without_saving(monkeypatch):
    async def go(env):
        for when in ("whenever", "31d", "10s"):
            inter = await env.remind(A, when, "x")
            ((text, eph),) = inter.replies()
            assert eph and text
        assert await env.rows("reminders") == []
    with_env(go, monkeypatch)


def test_remind_caps_active_reminders_at_ten(monkeypatch):
    async def go(env):
        for i in range(10):
            await env.remind(A, f"{i + 1}h", f"r{i}")
        inter = await env.remind(A, "1d", "one too many")
        assert "10 reminders" in inter.replies()[0][0]
        assert len(await env.rows("reminders")) == 10
        await env.remind(B, "1d", "someone else is fine")
        await env.db.execute("UPDATE reminders SET done = 1 WHERE text = 'r0'")
        await env.remind(A, "1d", "room again")
        assert len(await env.rows("reminders", "user_id = 11 AND done = 0")) == 10
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- delivery
def test_delivery_pings_only_the_owner_and_marks_done(monkeypatch):
    async def go(env):
        await env.remind(A, "10m", "<@&5> @everyone check the oven")
        await env.remind(B, "1h", "later")
        env.t += 10 * MIN
        await env.cog.deliver_due()
        (sent,) = env.guild.general.sent
        assert sent["content"].startswith(f"⏰ <@{A}>") and "check the oven" in sent["content"]
        am = sent["allowed_mentions"]
        assert am.everyone is False and am.roles is False and [u.id for u in am.users] == [A]
        assert [r["done"] for r in await env.rows("reminders")] == [1, 0]
        await env.cog.deliver_due()  # nothing twice
        assert len(env.guild.general.sent) == 1
    with_env(go, monkeypatch)


def test_delivery_survives_restart(monkeypatch):
    async def go(env):
        await env.remind(A, "10m", "after restart")
        cog = env.restart()
        env.t += HOUR
        await cog.deliver_due()
        assert "after restart" in env.guild.general.sent[0]["content"]
    with_env(go, monkeypatch)


def test_delivery_retries_on_errors_then_gives_up_after_a_day(monkeypatch):
    async def go(env):
        await env.remind(A, "10m", "flaky")
        env.guild.general.fail = http_error()
        env.t += 10 * MIN
        await env.cog.deliver_due()
        assert (await env.rows("reminders"))[0]["done"] == 0
        env.t += DAY + MIN
        await env.cog.deliver_due()
        assert (await env.rows("reminders"))[0]["done"] == 1
    with_env(go, monkeypatch)


def test_delivery_falls_back_to_dm_when_channel_is_gone(monkeypatch):
    async def go(env):
        await env.remind(A, "10m", "dm me")
        env.guild.text_channels.remove(env.guild.general)
        env.t += 10 * MIN
        await env.cog.deliver_due()
        (dm,) = env.member(A).dms
        assert "dm me" in dm["content"]
        assert (await env.rows("reminders"))[0]["done"] == 1
    with_env(go, monkeypatch)


def test_delivery_forbidden_channel_uses_dm(monkeypatch):
    async def go(env):
        await env.remind(A, "10m", "no perms")
        env.guild.general.fail = http_error(discord.Forbidden, 403)
        env.t += 10 * MIN
        await env.cog.deliver_due()
        assert env.member(A).dms and (await env.rows("reminders"))[0]["done"] == 1
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /reminders
def test_reminders_list_and_cancel(monkeypatch):
    async def go(env):
        inter = env.inter(A)
        await Utility.reminders.callback(env.cog, inter, None)
        assert "no reminders" in inter.replies()[0][0]

        await env.remind(A, "2h", "second")
        await env.remind(A, "1h", "first")
        await env.remind(B, "1h", "not yours")
        inter = env.inter(A)
        await Utility.reminders.callback(env.cog, inter, None)
        embed = inter.last()["embed"]
        assert inter.last()["ephemeral"]
        lines = embed.description.splitlines()
        assert "first" in lines[0] and "second" in lines[1] and len(lines) == 2

        rid_b = (await env.rows("reminders", "user_id = 12"))[0]["id"]
        inter = env.inter(A)
        await Utility.reminders.callback(env.cog, inter, rid_b)
        assert "don't have" in inter.replies()[0][0]
        assert (await env.rows("reminders", "user_id = 12"))[0]["done"] == 0

        rid = (await env.rows("reminders", "text = 'first'"))[0]["id"]
        inter = env.inter(A)
        await Utility.reminders.callback(env.cog, inter, rid)
        assert f"Cancelled reminder `#{rid}`" in inter.replies()[0][0]
        env.t += 2 * HOUR
        await env.cog.deliver_due()
        assert [s["content"] for s in env.guild.general.sent if "first" in s["content"]] == []
    with_env(go, monkeypatch)


def test_cancel_autocomplete_lists_only_own_waiting(monkeypatch):
    async def go(env):
        await env.remind(A, "2h", "buy milk")
        await env.remind(A, "1h", "call mom")
        await env.remind(B, "1h", "hidden")
        choices = await env.cog.cancel_choices(env.inter(A), "")
        assert [c.name.split(" · ")[2] for c in choices] == ["call mom", "buy milk"]
        assert all(len(c.name) <= 100 for c in choices)
        milk = await env.cog.cancel_choices(env.inter(A), "milk")
        assert len(milk) == 1 and isinstance(milk[0].value, int)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- AFK
async def set_afk(env, uid, reason=None):
    inter = env.inter(uid)
    await Utility.afk.callback(env.cog, inter, reason)
    return inter


def test_afk_set_mention_notice_without_pings_and_cooldown(monkeypatch):
    async def go(env):
        inter = await set_afk(env, A, "  dinner  ")
        assert inter.replies()[0][1] is True
        (row,) = await env.rows("afk")
        assert (row["user_id"], row["reason"], row["since"]) == (A, "dinner", T0)

        msg = await env.say(B, mentions=[A])
        (reply,) = msg.replies
        assert reply["content"] == f"💤 User\\_{A} is AFK: dinner (since <t:{T0}:R>)"
        am = reply["allowed_mentions"]
        assert am.everyone is False and am.users is False and am.roles is False and am.replied_user is False

        env.t += 9 * MIN
        assert (await env.say(C, mentions=[A])).replies == []  # same channel, cooldown
        other = FakeText("🎲・random", env.guild)
        assert len((await env.say(C, channel=other, mentions=[A])).replies) == 1  # other channel
        env.t += MIN
        assert len((await env.say(C, mentions=[A])).replies) == 1  # 10 min later
    with_env(go, monkeypatch)


def test_afk_without_reason_and_several_mentions_in_one_reply(monkeypatch):
    async def go(env):
        await set_afk(env, A)
        await set_afk(env, B, "work")
        msg = await env.say(C, mentions=[A, B, MOD])
        (reply,) = msg.replies
        assert reply["content"].splitlines() == [
            f"💤 User\\_{A} is AFK (since <t:{T0}:R>)", f"💤 User\\_{B} is AFK: work (since <t:{T0}:R>)"]
    with_env(go, monkeypatch)


def test_afk_clears_when_member_talks(monkeypatch):
    async def go(env):
        await set_afk(env, A, "brb")
        env.t += 5 * MIN
        msg = await env.say(A)
        (reply,) = msg.replies
        assert "Welcome back" in reply["content"] and reply["allowed_mentions"].users is False
        assert await env.rows("afk") == [] and A not in env.cog.afk_ids
        assert (await env.say(B, mentions=[A])).replies == []
        assert (await env.say(A)).replies == []  # only once
    with_env(go, monkeypatch)


def test_afk_self_mention_and_other_guild_ignored(monkeypatch):
    async def go(env):
        await set_afk(env, A)
        bot = env.member(BOTUSER)
        msg = FakeMessage(bot, env.guild.general, [env.member(A)])
        await env.cog.on_message(msg)
        assert msg.replies == []
        msg = FakeMessage(env.member(B), env.guild.general, [env.member(A)])
        msg.guild = SimpleNamespace(id=1)
        await env.cog.on_message(msg)
        assert msg.replies == [] and A in env.cog.afk_ids
    with_env(go, monkeypatch)


def test_afk_survives_restart(monkeypatch):
    async def go(env):
        await set_afk(env, A, "sleeping")
        cog = env.restart()
        await cog.load_afk()
        msg = await env.say(B, mentions=[A])
        assert "sleeping" in msg.replies[0]["content"]
    with_env(go, monkeypatch)


def test_on_message_never_raises(monkeypatch):
    async def go(env):
        await set_afk(env, A)

        async def boom(*a, **k):
            raise RuntimeError("x")
        msg = FakeMessage(env.member(B), env.guild.general, [env.member(A)])
        msg.reply = boom
        await env.cog.on_message(msg)  # logged, not raised
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- suggestions
def test_new_suggestion_gets_idea_tag_and_votes(monkeypatch):
    async def go(env):
        forum = env.guild.suggestions
        t = FakeThread(forum, guild=env.guild)
        await env.cog.on_thread_create(t)
        assert [x.name for x in t.applied_tags] == ["Idea"]
        assert t.reactions == ["👍", "👎"]

        tagged = FakeThread(forum, tags=[forum.tag("Bug")], guild=env.guild)
        await env.cog.on_thread_create(tagged)
        assert tagged.edits == [] and tagged.reactions == ["👍", "👎"]

        lfg = FakeThread(env.guild.lfg, guild=env.guild)
        await env.cog.on_thread_create(lfg)
        assert lfg.edits == [] and lfg.reactions == []
    with_env(go, monkeypatch)


def test_new_suggestion_retries_until_starter_exists(monkeypatch):
    async def go(env):
        async def no_wait(seconds):
            pass
        monkeypatch.setattr(Utility, "pause", staticmethod(no_wait))
        t = FakeThread(env.guild.suggestions, guild=env.guild)
        t.reaction_failures = 2
        await env.cog.on_thread_create(t)
        assert t.reactions == ["👍", "👎"]
        t2 = FakeThread(env.guild.suggestions, guild=env.guild)
        t2.reaction_failures = 99
        await env.cog.on_thread_create(t2)  # gives up quietly
        assert t2.reactions == []
    with_env(go, monkeypatch)


async def set_status(env, uid, thread, status, note=None):
    inter = env.inter(uid, channel=thread)
    await Utility.suggestion_status.callback(env.cog, inter, choice(status, U.STATUS_TAGS[status]), note)
    return inter


def test_suggestion_status_staff_only_and_inside_the_forum(monkeypatch):
    async def go(env):
        forum = env.guild.suggestions
        t = FakeThread(forum, tags=[forum.tag("Idea")], guild=env.guild)
        inter = await set_status(env, A, t, "accepted")
        assert "Only Moderators" in inter.replies()[0][0] and t.edits == []

        inter = await set_status(env, MOD, env.guild.general, "accepted")
        assert config.SUGGESTIONS_FORUM in inter.replies()[0][0]
        inter = await set_status(env, MOD, FakeThread(env.guild.lfg, guild=env.guild), "accepted")
        assert inter.replies()[0][1] is True
    with_env(go, monkeypatch)


def test_suggestion_status_sets_tag_and_posts_note(monkeypatch):
    async def go(env):
        forum = env.guild.suggestions
        t = FakeThread(forum, tags=[forum.tag("Idea"), forum.tag("Bug")], guild=env.guild)
        inter = await set_status(env, MOD, t, "accepted", "coming next week")
        assert [x.name for x in t.applied_tags] == ["Accepted", "Bug"]
        post = inter.last()
        assert post["embed"].description == f"✅ **Accepted** by <@{MOD}>\n> coming next week"
        assert post["allowed_mentions"].users is False

        t.archived = True
        await set_status(env, KEEPER, t, "done")
        assert [x.name for x in t.applied_tags] == ["Done", "Bug"] and t.archived is False
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- stat channels
def test_stat_channels_created_locked_at_top_and_stored(monkeypatch):
    async def go(env):
        env.member(A)
        env.member(B, status=discord.Status.offline)
        await env.cog.ensure_stat_channels(env.guild)
        members, online = env.guild.created_voice
        assert members.name == "👥 Members: 5" and online.name == "🟢 Online: 4"  # bots don't count
        assert (members.position, online.position) == (0, 1)
        assert members.category is env.guild.front
        everyone = members.overwrites[env.guild.default_role]
        assert everyone.view_channel is True and everyone.connect is False
        assert await env.meta(STAT_KEY.format(kind="members")) == str(members.id)
        assert await env.meta(STAT_KEY.format(kind="online")) == str(online.id)

        await env.cog.ensure_stat_channels(env.guild)  # idempotent
        assert len(env.guild.created_voice) == 2 and members.edits == []
    with_env(go, monkeypatch)


def test_stat_rename_waits_ten_minutes_and_only_on_change(monkeypatch):
    async def go(env):
        await env.cog.ensure_stat_channels(env.guild)
        members, _ = env.guild.created_voice
        env.member(A)
        env.t += 9 * MIN
        await env.cog.update_stats(env.guild)
        assert members.edits == []
        env.t += MIN
        await env.cog.update_stats(env.guild)
        assert members.name == "👥 Members: 4"
        env.t += 10 * MIN
        await env.cog.update_stats(env.guild)  # same number: no rename
        assert len(members.edits) == 1
        assert await env.meta(RENAMED_KEY.format(kind="members")) == str(env.t - 10 * MIN)
    with_env(go, monkeypatch)


def test_stat_rename_failure_is_not_recorded(monkeypatch):
    async def go(env):
        await env.cog.ensure_stat_channels(env.guild)
        members, _ = env.guild.created_voice
        before = await env.meta(RENAMED_KEY.format(kind="members"))
        env.member(A)
        env.t += 10 * MIN
        members.fail = http_error(status=429)
        await env.cog.update_stats(env.guild)
        assert await env.meta(RENAMED_KEY.format(kind="members")) == before
        members.fail = None
        await env.cog.update_stats(env.guild)
        assert members.name == "👥 Members: 4"
    with_env(go, monkeypatch)


def test_without_presences_only_members_channel(monkeypatch):
    async def go(env):
        await env.cog.ensure_stat_channels(env.guild)
        assert [c.name for c in env.guild.created_voice] == ["👥 Members: 3"]
    with_env(go, monkeypatch, presences=False)


def test_stat_channels_adopted_when_meta_is_lost_and_relocked(monkeypatch):
    async def go(env):
        old = FakeVoice("👥 Members: 1", env.guild.front)
        env.guild.front.voice_channels.append(old)
        await env.cog.ensure_stat_channels(env.guild)
        assert [c.name for c in env.guild.created_voice] == ["🟢 Online: 3"]
        assert await env.meta(STAT_KEY.format(kind="members")) == str(old.id)
        assert old.overwrites[env.guild.default_role].connect is False
        assert old.name == "👥 Members: 3"  # never renamed before: updated right away
    with_env(go, monkeypatch)


def test_on_ready_does_stats_and_ticket_post_and_never_raises(monkeypatch):
    async def go(env):
        await env.cog.on_ready()
        assert len(env.guild.created_voice) == 2 and len(env.guild.help.sent) == 1
        env.guild.categories = []
        env.guild.help.fail = RuntimeError("x")
        await env.db.execute("DELETE FROM meta WHERE key = ?", (TICKET_MESSAGE_KEY,))
        await env.cog.on_ready()  # logged, not raised
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- tickets
def test_ticket_button_posted_once_and_reposted_if_deleted(monkeypatch):
    async def go(env):
        await env.cog.ensure_ticket_message(env.guild)
        (post,) = env.guild.help.sent
        button = post["view"].children[0]
        assert button.custom_id == "ticket:open" and button.item.label == "Contact the mods"
        await env.cog.ensure_ticket_message(env.guild)
        assert len(env.guild.help.sent) == 1
        env.guild.help.messages.clear()
        await env.cog.ensure_ticket_message(env.guild)
        assert len(env.guild.help.sent) == 2
        mid = (await env.meta(TICKET_MESSAGE_KEY)).split(":")[1]
        assert int(mid) in env.guild.help.messages
    with_env(go, monkeypatch)


async def open_ticket(env, uid):
    inter = env.inter(uid, channel=env.guild.help)
    await env.cog.open_ticket(inter)
    return inter


def test_open_ticket_creates_private_thread_and_pings_mods(monkeypatch):
    async def go(env):
        inter = await open_ticket(env, A)
        (thread,) = env.guild.help.threads
        kw = thread.create_kwargs
        assert kw["name"] == f"ticket-user{A}" and kw["type"] is discord.ChannelType.private_thread
        assert kw["invitable"] is False
        assert thread.added == [A]
        (post,) = thread.sent
        mod_role = env.guild.role(config.MOD_ROLE)
        assert f"<@{A}>" in post["content"] and mod_role.mention in post["content"]
        am = post["allowed_mentions"]
        assert am.everyone is False and [r.id for r in am.roles] == [mod_role.id] and [u.id for u in am.users] == [A]
        assert post["view"].children[0].custom_id == "ticket:close"
        (row,) = await env.rows("tickets")
        assert (row["thread_id"], row["user_id"], row["opened_at"], row["closed_at"]) == (thread.id, A, T0, None)
        assert thread.mention in inter.replies()[-1][0] and inter.replies()[-1][1] is True
    with_env(go, monkeypatch)


def test_one_open_ticket_per_member(monkeypatch):
    async def go(env):
        await open_ticket(env, A)
        inter = await open_ticket(env, A)
        assert "already have an open ticket" in inter.replies()[-1][0]
        assert len(env.guild.help.threads) == 1
        await open_ticket(env, B)
        assert len(env.guild.help.threads) == 2
    with_env(go, monkeypatch)


def test_ticket_whose_thread_vanished_doesnt_block(monkeypatch):
    async def go(env):
        await open_ticket(env, A)
        (thread,) = env.guild.help.threads
        del env.guild.threads[thread.id]
        await open_ticket(env, A)
        assert len(env.guild.help.threads) == 2
        rows = await env.rows("tickets")
        assert rows[0]["closed_at"] == T0 and rows[1]["closed_at"] is None
    with_env(go, monkeypatch)


def test_ticket_in_progress_blocks_double_click(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO tickets (thread_id, user_id, opened_at) VALUES (?, ?, ?)", (-A, A, T0))
        inter = await open_ticket(env, A)
        assert "being opened" in inter.replies()[-1][0] and env.guild.help.threads == []
    with_env(go, monkeypatch)


def test_ticket_thread_creation_failure_releases_claim(monkeypatch):
    async def go(env):
        async def fail(**kw):
            raise http_error(discord.Forbidden, 403)
        env.guild.help.create_thread = fail
        with pytest.raises(discord.Forbidden):
            await open_ticket(env, A)
        assert await env.rows("tickets") == []
    with_env(go, monkeypatch)


def test_ticket_without_help_channel(monkeypatch):
    async def go(env):
        env.guild.text_channels.remove(env.guild.help)
        inter = await open_ticket(env, A)
        assert "aren't set up" in inter.replies()[0][0]
    with_env(go, monkeypatch)


async def press_close(env, uid, thread):
    inter = env.inter(uid, channel=thread)
    await env.cog.close_ticket(inter)
    return inter


def test_close_ticket_by_owner_or_staff_only(monkeypatch):
    async def go(env):
        await open_ticket(env, A)
        (thread,) = env.guild.help.threads
        inter = await press_close(env, B, thread)
        assert "Only the member" in inter.replies()[0][0] and not thread.archived

        env.t += HOUR
        inter = await press_close(env, A, thread)
        assert thread.archived and thread.locked
        assert inter.replies()[0][0] == f"🔒 Ticket closed by <@{A}>."
        assert (await env.rows("tickets"))[0]["closed_at"] == T0 + HOUR

        inter = await press_close(env, A, thread)
        assert "already closed" in inter.replies()[0][0]

        await open_ticket(env, A)  # can open a new one now
        new = env.guild.help.threads[-1]
        await press_close(env, MOD, new)
        assert new.locked and (await env.rows("tickets"))[-1]["closed_at"] is not None
    with_env(go, monkeypatch)


def test_close_failure_keeps_ticket_open(monkeypatch):
    async def go(env):
        await open_ticket(env, A)
        (thread,) = env.guild.help.threads
        thread.edit_fail = http_error()
        with pytest.raises(discord.HTTPException):
            await press_close(env, A, thread)
        assert (await env.rows("tickets"))[0]["closed_at"] is None
    with_env(go, monkeypatch)


def test_deleted_ticket_thread_is_closed(monkeypatch):
    async def go(env):
        await open_ticket(env, A)
        (thread,) = env.guild.help.threads
        await env.cog.on_raw_thread_delete(SimpleNamespace(thread_id=thread.id))
        assert (await env.rows("tickets"))[0]["closed_at"] == T0
    with_env(go, monkeypatch)


def test_button_callbacks_reach_the_cog_and_report_errors(monkeypatch):
    async def go(env):
        inter = env.inter(A, channel=env.guild.help)
        await TicketOpenButton().callback(inter)
        assert len(env.guild.help.threads) == 1

        async def boom(interaction):
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "close_ticket", boom)
        inter = env.inter(A, channel=env.guild.help.threads[0])
        await TicketCloseButton().callback(inter)
        assert "went wrong" in inter.replies()[0][0]
    with_env(go, monkeypatch)


def test_remind_in_a_read_only_channel_is_delivered_by_dm(monkeypatch):
    """Security: /remind in a channel the member can't post in (e.g. rules) must not make
    the bot post their text there."""
    async def go(env):
        env.guild.help.read_only = True
        await env.remind(A, "10m", "sneaky announcement", channel=env.guild.help)
        (r,) = await env.rows("reminders")
        assert r["channel_id"] == 0
        env.t += HOUR
        await env.cog.deliver_due()
        assert env.guild.help.sent == [] and env.guild.general.sent == []
        assert "sneaky announcement" in env.member(A).dms[0]["content"]
    with_env(go, monkeypatch)


def test_delivery_rechecks_permission_and_timeout(monkeypatch):
    async def go(env):
        await env.remind(A, "10m", "first")
        await env.remind(A, "20m", "second")
        env.member(A).timed_out = True  # timed out after setting it
        env.t += 15 * MIN
        await env.cog.deliver_due()
        assert env.guild.general.sent == [] and "first" in env.member(A).dms[0]["content"]
        env.member(A).timed_out = False
        env.guild.general.read_only = True  # channel locked after setting it
        env.t += HOUR
        await env.cog.deliver_due()
        assert env.guild.general.sent == [] and "second" in env.member(A).dms[1]["content"]
    with_env(go, monkeypatch)


def test_reminder_text_cannot_format_or_embed(monkeypatch):
    async def go(env):
        await env.remind(A, "10m", "**big** https://example.com @everyone")
        env.t += HOUR
        await env.cog.deliver_due()
        (sent,) = env.guild.general.sent
        assert r"\*\*big\*\*" in sent["content"] and sent["suppress_embeds"] is True
        assert "@​everyone" in sent["content"]
    with_env(go, monkeypatch)


def test_afk_reason_cannot_render_a_masked_link(monkeypatch):
    """Security: an AFK reason is repeated in a bot message, so a masked phishing link
    must not render as a trusted link from the bot."""
    async def go(env):
        await set_afk(env, A, "[free nitro](https://phish.example)")
        msg = await env.say(B, mentions=[A])
        (reply,) = msg.replies
        # The escaped "[" stops Discord treating it as a masked link; the raw URL stays visible.
        assert "\\[free nitro](" in reply["content"] and reply["suppress_embeds"] is True
    with_env(go, monkeypatch)
