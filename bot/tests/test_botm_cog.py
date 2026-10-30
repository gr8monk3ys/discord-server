"""Offline tests for cogs.botm: a real in-memory SQLite database plus fakes for the guild, roles,
members, the Monkey SMP channel and interactions. Time is controlled by patching cogs.botm.now."""

import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
from cogs import botm as cogmod
from cogs.botm import Botm
from logic import botm as B

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, A, B1, C, D, MOD = 1, 11, 12, 13, 14, 20
JAN = 1_799_000_000  # 2027-01-03 (Pacific)
FEB = JAN + 31 * 24 * 3600


def run(coro):
    return asyncio.run(coro)


def http_error():
    return discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")


class Role:
    _next = 5000

    def __init__(self, name, position, guild, perms=None):
        Role._next += 1
        self.id = Role._next
        self.name = name
        self.position = position
        self.guild = guild
        self.permissions = perms or discord.Permissions.none()

    def __gt__(self, other):
        return self.position > other.position

    @property
    def members(self):
        return [m for m in self.guild.members.values() if self in m.roles]


class FakeMember:
    def __init__(self, uid, guild, roles=()):
        self.id = uid
        self.bot = False
        self.guild = guild
        self.mention = f"<@{uid}>"
        self.roles = list(roles)
        self.guild_permissions = SimpleNamespace(administrator=False)
        self.fail = None

    async def add_roles(self, *roles, reason=None):
        if self.fail:
            raise self.fail
        self.roles.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        if self.fail:
            raise self.fail
        self.roles = [r for r in self.roles if r not in roles]


class FakeText:
    def __init__(self, name):
        self.name = name
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))
        return SimpleNamespace(jump_url="https://discord.com/channels/x/y/z")


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.channel = FakeText(config.MONKEY_CHANNEL)
        self.text_channels = [FakeText(config.GENERAL_CHANNEL), self.channel]
        self.mod_role = Role(config.MOD_ROLE, 10, self)
        self.bot_role = Role("Front Desk", 8, self)
        self.roles = [Role("@everyone", 0, self), self.mod_role, self.bot_role]
        self.me = SimpleNamespace(top_role=self.bot_role)
        self.members = {}
        self.created = []
        self.create_fail = None

    async def create_role(self, name, permissions, mentionable, reason):
        if self.create_fail:
            raise self.create_fail
        role = Role(name, 1, self, permissions)
        self.roles.append(role)
        self.created.append(dict(name=name, mentionable=mentionable, permissions=permissions))
        return role

    def get_member(self, uid):
        return self.members.get(uid)

    def role(self):
        return config.match_by_name(self.roles, config.BOTM_ROLE)


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
    def __init__(self, guild, user):
        self.calls = []
        self.guild = guild
        self.user = user
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def texts(self):
        return [kw.get("content") for k, kw in self.calls if k in ("send_message", "followup")]


class Picture:
    def __init__(self, filename="base.png", content_type="image/png"):
        self.filename = filename
        self.content_type = content_type

    async def to_file(self):
        return f"file:{self.filename}"


class Env:
    def __init__(self, db, guild, cog):
        self.db, self.guild, self.cog = db, guild, cog
        self.t = JAN

    def member(self, uid):
        if uid not in self.guild.members:
            roles = [self.guild.mod_role] if uid == MOD else []
            self.guild.members[uid] = FakeMember(uid, self.guild, roles)
        return self.guild.members[uid]

    async def award(self, by, *uids, **kw):
        inter = FakeInteraction(self.guild, self.member(by))
        builders = [self.member(u) for u in uids] + [None] * (4 - len(uids))
        await Botm.award.callback(self.cog, inter, *builders, **kw)
        return inter

    def holders(self):
        role = self.guild.role()
        return sorted(m.id for m in role.members) if role else []


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        cog = Botm(SimpleNamespace(db=db, settings=SimpleNamespace(guild_id=GUILD_ID, tz=TZ)))
        env = Env(db, guild, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def test_staff_only(monkeypatch):
    async def go(env):
        inter = await env.award(A, A)
        assert "Only Moderators and Keepers" in inter.texts()[0]
        assert env.guild.role() is None and env.guild.channel.sent == []
    with_env(go, monkeypatch)


def test_first_award_creates_the_role_at_the_bottom_and_announces(monkeypatch):
    async def go(env):
        inter = await env.award(MOD, A, B1, line="We dug out the **whole** hill", picture=Picture())
        (made,) = env.guild.created
        assert made["name"] == config.BOTM_ROLE and made["mentionable"] is False
        assert made["permissions"].value == 0 and env.guild.role().position == 1
        assert env.guild.bot_role.position == 8 and env.guild.mod_role.position == 10  # nothing moved
        assert env.holders() == [A, B1]
        (sent,) = env.guild.channel.sent
        assert sent["content"].startswith("🏰 **Base of the Month, January 2027:** <@11> and <@12>!")
        assert "\\*\\*whole\\*\\*" in sent["content"] and sent["files"] == ["file:base.png"]
        mentions = sent["allowed_mentions"]
        assert [u.id for u in mentions.users] == [A, B1] and mentions.roles is False and mentions.everyone is False
        assert "Announced:" in inter.texts()[-1]
        assert B.load(await env.db.meta_get(B.STATE_KEY)) == B.State("2027-01", (A, B1), ())
    with_env(go, monkeypatch)


def test_next_month_moves_the_role_and_refuses_repeat_winners(monkeypatch):
    async def go(env):
        await env.award(MOD, A, B1)
        env.t = FEB
        inter = await env.award(MOD, C, B1)
        assert "two months running" in inter.texts()[0] and env.holders() == [A, B1]
        await env.award(MOD, C, D)
        assert env.holders() == [C, D] and len(env.guild.created) == 1
        assert "February 2027" in env.guild.channel.sent[-1]["content"]
        # a correction in the same month: checked against January, and C loses it
        inter = await env.award(MOD, A)
        assert "two months running" in inter.texts()[0]
        await env.award(MOD, D)
        assert env.holders() == [D]
    with_env(go, monkeypatch)


def test_a_role_above_the_bot_or_with_mod_powers_is_refused(monkeypatch):
    async def go(env):
        env.guild.roles.append(Role(config.BOTM_ROLE, 9, env.guild))
        inter = await env.award(MOD, A)
        assert "sits above Front Desk" in inter.texts()[-1] and env.holders() == []
        env.guild.roles[-1].position = 2
        env.guild.roles[-1].permissions = discord.Permissions(ban_members=True)
        inter = await env.award(MOD, A)
        assert "moderator permissions" in inter.texts()[-1] and env.holders() == []
        assert env.guild.created == [] and env.guild.channel.sent == []
    with_env(go, monkeypatch)


def test_input_checks_and_failures(monkeypatch):
    async def go(env):
        inter = await env.award(MOD, A, picture=Picture("notes.txt", "text/plain"))
        assert "images" in inter.texts()[0]
        inter = await env.award(MOD, A, line="x" * 301)
        assert "under 300" in inter.texts()[0]
        env.member(C).bot = True
        inter = await env.award(MOD, A, C)
        assert "Bots" in inter.texts()[0]
        env.guild.create_fail = http_error()
        inter = await env.award(MOD, A)
        assert "couldn't create" in inter.texts()[-1]
        env.guild.create_fail = None
        env.member(B1).fail = http_error()
        inter = await env.award(MOD, A, B1)
        assert "couldn't give the role to <@12>" in inter.texts()[-1] and env.holders() == [A]
    with_env(go, monkeypatch)
