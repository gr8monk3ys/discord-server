"""Offline tests for cogs.factions: a real in-memory SQLite database plus small fakes for the bot,
guild, forum, threads, members and interactions. No network."""

import asyncio
import re
from types import SimpleNamespace

import discord

import config
import db as dbmod
from cogs import factions as cogmod
from cogs.factions import CharterModal, FactionButton, Factions, RegisterModal

GUILD_ID = 999
OWNER, A, B, MOD = 1, 11, 12, 20
GOOD = {"name": "The River Kings", "members": "Steve, Alex_2",
        "banner": "Blue field, a gold crown over two wavy white lines.",
        "charter": "We hold the river and trade with anyone. We raid the Red Monkeys and nobody else."}


def run(coro):
    return asyncio.run(coro)


def http_error(cls=discord.HTTPException, status=500):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


class Named:
    def __init__(self, name, id=0):
        self.name, self.id = name, id


class FakeMessage:
    def __init__(self, mid, kwargs):
        self.id = mid
        self.kwargs = kwargs
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeThread:
    def __init__(self, tid, name):
        self.id = tid
        self.name = name
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{tid}"
        self.sent = []
        self.messages = {}

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))

    async def fetch_message(self, mid):
        return self.messages[mid]


class FakeForum:
    def __init__(self, name):
        self.name = name
        self.available_tags = [Named("Recruiting", 1), Named(config.FACTION_TAG, 2)]
        self.threads = []
        self.fail = None

    async def create_thread(self, name, **kwargs):
        if self.fail is not None:
            raise self.fail
        tid = 7000 + len(self.threads)
        thread = FakeThread(tid, name)
        message = FakeMessage(tid, kwargs)
        thread.messages[tid] = message
        thread.kwargs = kwargs
        self.threads.append(thread)
        return SimpleNamespace(thread=thread, message=message)


class FakeMember:
    def __init__(self, uid, guild, roles=()):
        self.id = uid
        self.guild = guild
        self.mention = f"<@{uid}>"
        self.roles = [Named(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=False)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.forum = FakeForum(config.SMP_FORUM)
        self.forums = [self.forum]

    def get_thread(self, tid):
        return next((t for t in self.forum.threads if t.id == tid), None)

    async def fetch_channel(self, cid):
        raise http_error(discord.NotFound, 404)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)
        self.dynamic = set()

    def add_dynamic_items(self, *items):
        self.dynamic.update(items)

    def remove_dynamic_items(self, *items):
        self.dynamic.difference_update(items)


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls
        self.done = False

    def _finish(self):
        assert not self.done
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self._finish()
        self.calls.append(("send_message", dict(content=content, **kwargs)))

    async def edit_message(self, **kwargs):
        self._finish()
        self.calls.append(("edit_message", kwargs))

    async def defer(self, **kwargs):
        self._finish()
        self.calls.append(("defer", kwargs))

    async def send_modal(self, modal):
        self._finish()
        self.calls.append(("send_modal", {"modal": modal}))


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, guild, user, channel=None):
        self.calls = []
        self.guild = guild
        self.user = user
        self.channel = channel
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]

    def texts(self):
        return [kw.get("content") for k, kw in self.calls if k in ("send_message", "followup")]


class Env:
    def __init__(self, db, guild, cog):
        self.db, self.guild, self.cog = db, guild, cog

    def user(self, uid):
        return FakeMember(uid, self.guild, roles=[config.MOD_ROLE] if uid == MOD else [])

    async def register(self, uid, **answers):
        inter = FakeInteraction(self.guild, self.user(uid))
        await Factions.register.callback(self.cog, inter)
        modals = inter.of("send_modal")
        if not modals:
            return inter
        modal = modals[0]["modal"]
        assert isinstance(modal, RegisterModal)
        for key, value in {**GOOD, **answers}.items():
            modal.inputs[key]._value = value
        submit = FakeInteraction(self.guild, self.user(uid))
        await modal.on_submit(submit)
        return submit

    async def press(self, uid, action, fid, thread=None):
        inter = FakeInteraction(self.guild, self.user(uid), channel=thread or self.guild.forum.threads[0])
        await self.cog.decide(inter, action, fid)
        return inter

    async def rows(self):
        return [dict(r) for r in await self.db.fetchall("SELECT * FROM factions ORDER BY id")]


def with_env(fn, environ=None):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        cog = Factions(FakeBot(db, guild), environ=environ or {})
        try:
            await fn(Env(db, guild, cog))
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- wiring
def test_button_template_round_trips():
    b = FactionButton("approve", 42)
    assert b.item.custom_id == "faction:approve:42"
    pattern = FactionButton.__discord_ui_compiled_template__
    assert re.fullmatch(pattern, "faction:reject:7")["action"] == "reject"
    assert re.fullmatch(pattern, "faction:delete:7") is None


def test_cog_load_registers_dynamic_items():
    async def go():
        bot = FakeBot(None, FakeGuild())
        cog = Factions(bot, environ={})
        await cog.cog_load()
        assert FactionButton in bot.dynamic
        await cog.cog_unload()
        assert FactionButton not in bot.dynamic
    run(go())


# ---------------------------------------------------------------- registering
def test_register_posts_one_tagged_forum_post_with_no_pings():
    async def go(env):
        inter = await env.register(A, name="**Kings** @everyone")
        assert "name can use letters" in inter.texts()[0]
        assert env.guild.forum.threads == []

        inter = await env.register(A)
        assert "Posted:" in inter.texts()[-1]
        thread = env.guild.forum.threads[0]
        assert thread.name == "The River Kings"
        kw = thread.kwargs
        assert [t.name for t in kw["applied_tags"]] == [config.FACTION_TAG]
        assert kw["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        embed = kw["embed"]
        assert embed.title == "The River Kings" and "trade with anyone" in embed.description
        assert [f.name for f in embed.fields] == ["Members", "Banner", "Registered by"]
        assert "Alex\\_2" in embed.fields[0].value  # escaped
        assert [c.item.custom_id for c in kw["view"].children] == ["faction:approve:1", "faction:reject:1"]
        (row,) = await env.rows()
        assert row["status"] == "pending" and row["thread_id"] == thread.id and row["season"] == "1"
    with_env(go)


def test_one_registration_per_leader_and_name_and_member():
    async def go(env):
        await env.register(A)
        inter = await env.register(A)
        assert "already registered **The River Kings**" in inter.texts()[0] and not inter.of("send_modal")
        inter = await env.register(B, name="the river-kings", members="Herobrine")
        assert "already a faction called" in inter.texts()[-1]
        inter = await env.register(B, name="Blue Lions", members="herobrine, STEVE")
        assert "`steve` is already on" in inter.texts()[-1]
        assert len(await env.rows()) == 1
    with_env(go)


def test_champion_name_is_held():
    async def go(env):
        inter = await env.register(A, name="Red Monkey")
        assert "last season's champion" in inter.texts()[-1]
    with_env(go, environ={"FACTIONS_RESERVED": "Red Monkeys"})


def test_no_forum_means_closed_and_a_failed_post_leaves_no_row():
    async def go(env):
        env.guild.forum.fail = http_error()
        try:
            await env.register(A)
        except discord.HTTPException:
            pass
        assert await env.rows() == []
        env.guild.forums = []
        inter = await env.register(A)
        assert "isn't open" in inter.texts()[0]
    with_env(go)


def test_forum_name_comes_from_env():
    async def go(env):
        inter = await env.register(A)
        assert "isn't open" in inter.texts()[0]
    with_env(go, environ={"FACTIONS_FORUM": "factions"})


# ---------------------------------------------------------------- staff decisions
def test_only_staff_decide_and_only_once():
    async def go(env):
        await env.register(A)
        inter = await env.press(B, "approve", 1)
        assert "Only Moderators and Keepers" in inter.texts()[0]
        assert (await env.rows())[0]["status"] == "pending"

        inter = await env.press(MOD, "approve", 1)
        (edit,) = inter.of("edit_message")
        assert edit["view"] is None and "APPROVED" in edit["embed"].footer.text
        row = (await env.rows())[0]
        assert row["status"] == "approved" and row["decided_by"] == MOD
        said = env.guild.forum.threads[0].sent[-1]
        assert "is registered" in said["content"] and said["allowed_mentions"].users[0].id == A
        assert said["allowed_mentions"].roles is False and said["allowed_mentions"].everyone is False

        inter = await env.press(MOD, "reject", 1)
        assert "already handled" in inter.texts()[0]
        inter = await env.press(MOD, "reject", 99)
        assert "gone" in inter.texts()[0]
    with_env(go)


def test_a_rejected_registration_frees_the_leader_and_the_name():
    async def go(env):
        await env.register(A)
        inter = await env.press(MOD, "reject", 1)
        assert "NOT APPROVED" in inter.of("edit_message")[0]["embed"].footer.text
        assert "didn't approve" in env.guild.forum.threads[0].sent[-1]["content"]
        inter = await env.register(A)
        assert "Posted:" in inter.texts()[-1]
        assert [r["status"] for r in await env.rows()] == ["rejected", "pending"]
    with_env(go)


def test_a_new_season_starts_empty():
    async def go(env):
        await env.register(A)
        env.cog.settings = cogmod.F.read_settings({"FACTIONS_SEASON": "2"}, config.SMP_FORUM)
        inter = await env.register(A)
        assert "Posted:" in inter.texts()[-1]
    with_env(go)


# ---------------------------------------------------------------- charter
def test_leader_rewrites_the_charter_in_place():
    async def go(env):
        inter = FakeInteraction(env.guild, env.user(A))
        await Factions.charter.callback(env.cog, inter)
        assert "haven't registered" in inter.texts()[0]

        await env.register(A)
        inter = FakeInteraction(env.guild, env.user(A))
        await Factions.charter.callback(env.cog, inter)
        modal = inter.of("send_modal")[0]["modal"]
        assert isinstance(modal, CharterModal) and modal.charter.default == GOOD["charter"]

        modal.charter._value = "tiny"
        submit = FakeInteraction(env.guild, env.user(A))
        await modal.on_submit(submit)
        assert "charter must be" in submit.texts()[0]

        modal.charter._value = "We left the river.\nNow we hold the mountain and trade with nobody."
        submit = FakeInteraction(env.guild, env.user(A))
        await modal.on_submit(submit)
        assert submit.texts()[-1] == "Charter updated."
        assert (await env.rows())[0]["charter"] == "We left the river. Now we hold the mountain and trade with nobody."
        edit = env.guild.forum.threads[0].messages[7000].edits[-1]
        assert "mountain" in edit["embed"].description and edit["view"] is not None  # still pending

        # someone else can't use that form
        submit = FakeInteraction(env.guild, env.user(B))
        await modal.on_submit(submit)
        assert "closed" in submit.texts()[0]
    with_env(go)
