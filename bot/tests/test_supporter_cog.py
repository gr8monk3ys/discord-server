"""Offline tests for cogs.supporter: a real in-memory SQLite database plus small fakes for the
bot, guild, roles, channels, members and interactions. No network, no real clock."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import supporter as cogmod
from cogs.supporter import STARTED_KEY, Supporter
from logic import quests as Q
from logic import recap as R
from logic import supporter as S

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
BOT_ID = 50
OWNER_ID = 1
DAY = 24 * 3600


def run(coro):
    return asyncio.run(coro)


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


def http_error(cls=discord.Forbidden, status=403):
    return cls(SimpleNamespace(status=status, reason="x"), "x")


def young(now, days=5):
    return ((now - days * DAY) * 1000 - Q.DISCORD_EPOCH_MS) << 22


class FakeRole:
    _next = 1000

    def __init__(self, name, guild, position=5, managed=False, rid=None):
        FakeRole._next += 1
        self.id = rid or FakeRole._next
        self.name, self.guild, self.position, self.managed = name, guild, position, managed
        self.permissions = discord.Permissions.none()
        self.mention = f"<@&{self.id}>"

    @property
    def members(self):
        return [m for m in self.guild.members if self in m.roles]

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id

    def __hash__(self):
        return self.id


class FakeText:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeMember:
    def __init__(self, uid, guild, roles=(), bot=False):
        self.id, self.guild, self.bot = uid, guild, bot
        self.name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.roles = [guild.default, *roles]
        self.guild_permissions = discord.Permissions.none()
        self.fail = None
        self.reasons = []

    async def add_roles(self, *roles, reason=None):
        if self.fail is not None:
            raise self.fail
        self.reasons.append(reason)
        self.roles.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        if self.fail is not None:
            raise self.fail
        self.reasons.append(reason)
        self.roles = [r for r in self.roles if r not in roles]

    def without(self, role):
        """A copy as the member was before/after a role change (for on_member_update)."""
        copy = FakeMember(self.id, self.guild)
        copy.roles = [r for r in self.roles if r != role]
        return copy


class FakeGuild:
    def __init__(self, with_role=True):
        self.id, self.owner_id = GUILD_ID, OWNER_ID
        self.members = []
        self.chunked = True
        self.default = FakeRole("@everyone", self, position=0, rid=GUILD_ID)
        self.top = FakeRole("Front Desk", self, position=50)
        self.supporter = FakeRole(config.SUPPORTER_ROLE, self)
        self.mod = FakeRole(config.MOD_ROLE, self, position=60)
        self.roles = [self.default, self.top, self.mod] + ([self.supporter] if with_role else [])
        self.me = SimpleNamespace(id=BOT_ID, top_role=self.top,
                                  guild_permissions=discord.Permissions(manage_roles=True))
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.text_channels = [self.general, self.mod_log]

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db, self.guild = db, guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))

    async def defer(self, **kwargs):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = ts(2026, 10, 7, 12, 0)

    def member(self, uid, supporter=False, roles=(), **kw):
        m = FakeMember(uid, self.guild, roles=[*roles, *([self.guild.supporter] if supporter else [])], **kw)
        self.guild.members.append(m)
        return m

    def interaction(self, user):
        return SimpleNamespace(guild=self.guild, user=user, response=FakeResponse(), followup=FakeFollowup())

    async def gain(self, member):
        """The role arrives from outside (Ko-fi's bot): the gateway event."""
        before = member.without(self.guild.supporter)
        member.roles.append(self.guild.supporter)
        await self.cog.on_member_update(before, member)

    async def lose(self, member):
        before = FakeMember(member.id, self.guild, roles=[self.guild.supporter])
        member.roles = [r for r in member.roles if r != self.guild.supporter]
        await self.cog.on_member_update(before, member)

    async def row(self, uid):
        r = await self.db.fetchone("SELECT * FROM supporters WHERE user_id = ?", (uid,))
        return dict(r) if r else None

    async def balance(self, uid):
        return await economy.balance(self.db, uid)

    async def refs(self):
        return sorted(r["ref"] for r in await self.db.fetchall("SELECT ref FROM ledger"))

    def log_lines(self):
        return [s["embed"].description for s in self.guild.mod_log.sent]


def with_env(fn, monkeypatch, with_role=True):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild(with_role)
        bot = FakeBot(db, guild)
        cog = Supporter(bot)
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        monkeypatch.delenv("SUPPORT_URL", raising=False)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def pinged_only(sent, uids):
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.roles is False
    assert sorted(u.id for u in am.users) == sorted(uids)


# ---------------------------------------------------------------- gaining the role
def test_gaining_the_role_thanks_once_and_pays_this_months_stipend(monkeypatch):
    async def body(env):
        m = env.member(11)
        await env.gain(m)
        sent = env.guild.general.sent
        assert len(sent) == 1 and sent[0]["content"] == S.thanks_text("<@11>", S.STIPEND_COINS)
        pinged_only(sent[0], [11])
        assert await env.balance(11) == S.STIPEND_COINS
        assert await env.refs() == ["supporterstipend:2026-10:11"]
        row = await env.row(11)
        assert (row["since"], row["source"], row["thanked_at"], row["ended_at"]) == (env.t, "role", env.t, None)
        assert env.log_lines() == ["💜 <@11> (user11 · `11`) is now a Supporter."]
        assert env.guild.mod_log.sent[0]["allowed_mentions"].users is False
        # Unrelated member updates and a duplicate event change nothing.
        await env.cog.on_member_update(m, m)
        assert await env.cog.gained(m) is False
        assert len(sent) == 1 and await env.balance(11) == S.STIPEND_COINS
    with_env(body, monkeypatch)


def test_fresh_accounts_are_thanked_but_not_paid(monkeypatch):
    async def body(env):
        uid = young(env.t)
        await env.gain(env.member(uid))
        assert env.guild.general.sent[0]["content"] == S.thanks_text(f"<@{uid}>", 0)
        assert await env.balance(uid) == 0 and await env.refs() == []
    with_env(body, monkeypatch)


def test_bots_and_other_servers_are_ignored(monkeypatch):
    async def body(env):
        bot = env.member(11, bot=True)
        await env.gain(bot)
        elsewhere = FakeMember(12, SimpleNamespace(id=5, default=env.guild.default), roles=[env.guild.supporter])
        await env.cog.on_member_update(FakeMember(12, elsewhere.guild), elsewhere)
        assert env.guild.general.sent == [] and await env.row(11) is None and await env.row(12) is None
    with_env(body, monkeypatch)


def test_losing_the_role_is_quiet_and_keeps_coins(monkeypatch):
    async def body(env):
        m = env.member(11)
        await env.gain(m)
        env.t += 3 * DAY
        await env.lose(m)
        assert len(env.guild.general.sent) == 1  # nothing public about leaving
        row = await env.row(11)
        assert row["ended_at"] == env.t and await env.balance(11) == S.STIPEND_COINS
        assert env.log_lines()[-1] == "💜 <@11> (user11 · `11`) is no longer a Supporter."
        assert await env.cog.profile_flair(m) is None
        assert await env.cog.lost(m) is False  # already closed: no second log line
        assert len(env.log_lines()) == 2
    with_env(body, monkeypatch)


def test_role_flapping_never_thanks_or_pays_twice(monkeypatch):
    async def body(env):
        m = env.member(11)
        for _ in range(4):
            await env.gain(m)
            env.t += 3600
            await env.lose(m)
            env.t += 3600
        assert len(env.guild.general.sent) == 1 and await env.balance(11) == S.STIPEND_COINS
        # Back after a long break: thanked again, and a new month's stipend.
        env.t += S.RETHANK_AFTER
        await env.gain(m)
        assert len(env.guild.general.sent) == 2
        assert await env.refs() == ["supporterstipend:2026-10:11", "supporterstipend:2026-12:11"]
        assert (await env.row(11))["since"] == env.t
    with_env(body, monkeypatch)


def test_a_bulk_role_sync_cannot_flood_general(monkeypatch):
    async def body(env):
        for uid in range(11, 21):
            await env.gain(env.member(uid))
            env.t += 5
        assert len(env.guild.general.sent) == S.THANKS_BURST
        rows = await env.db.fetchall("SELECT thanked_at FROM supporters WHERE ended_at IS NULL")
        assert len(rows) == 10 and all(r["thanked_at"] for r in rows)  # recorded, so never thanked late
        assert len(await env.refs()) == 10  # everyone still got the stipend
    with_env(body, monkeypatch)


def test_without_channels_the_role_still_counts(monkeypatch):
    async def body(env):
        env.guild.text_channels = []
        await env.gain(env.member(11))
        assert (await env.row(11))["ended_at"] is None and await env.balance(11) == S.STIPEND_COINS
        env.guild.text_channels = [env.guild.general]
        env.guild.general.fail = http_error()
        await env.gain(env.member(12))  # a failed post is logged, not raised
        assert (await env.row(12))["thanked_at"] == env.t
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- flair
def test_profile_flair_follows_the_role(monkeypatch):
    async def body(env):
        m, other = env.member(11), env.member(12)
        assert await env.cog.profile_flair(m) is None
        await env.gain(m)
        assert await env.cog.profile_flair(m) == f"💜 **Supporter** since <t:{env.t}:D>"
        assert await env.cog.profile_flair(other) is None
        held = env.member(13, supporter=True)  # has the role, no row yet (bot was down)
        assert await env.cog.profile_flair(held) == "💜 **Supporter**"
    with_env(body, monkeypatch)


def test_profile_embed_shows_the_flair(monkeypatch):
    from cogs.achievements import Achievements

    async def body(env):
        m = env.member(11)
        m.display_name = "Mo"
        m.display_avatar = SimpleNamespace(url="https://cdn.example/a.png")
        env.guild.afk_channel = None
        env.bot.get_cog = lambda name: env.cog if name == "Supporter" else None
        ach = Achievements(env.bot)
        assert (await ach.profile_embed(m)).description is None
        await env.gain(m)
        assert (await ach.profile_embed(m)).description == f"💜 **Supporter** since <t:{env.t}:D>"
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- reconcile
def test_first_reconcile_records_holders_without_posting(monkeypatch):
    async def body(env):
        env.member(11, supporter=True)
        env.member(12, supporter=True)
        env.member(13, supporter=True, bot=True)
        await env.cog.reconcile()
        assert env.guild.general.sent == [] and env.guild.mod_log.sent == []
        assert (await env.row(11))["thanked_at"] == env.t and await env.row(13) is None
        assert await env.balance(11) == S.STIPEND_COINS  # they are supporters: this month's stipend
        assert await env.cog.meta(STARTED_KEY) == str(env.t)
        # Later: a gain and a loss the bot missed while it was down.
        env.t += DAY
        env.member(14, supporter=True)
        gone = env.guild.get_member(12)
        gone.roles = [env.guild.default]
        await env.cog.reconcile()
        assert [s["content"] for s in env.guild.general.sent] == [S.thanks_text("<@14>", S.STIPEND_COINS)]
        assert (await env.row(12))["ended_at"] == env.t
        await env.cog.reconcile()
        assert len(env.guild.general.sent) == 1 and len(env.guild.mod_log.sent) == 2
    with_env(body, monkeypatch)


def test_reconcile_does_nothing_without_the_role_or_the_member_list(monkeypatch):
    async def body(env):
        env.member(11)
        await env.gain(env.guild.get_member(11))
        env.guild.chunked = False  # no Server Members intent: an empty cache isn't "everyone left"
        env.guild.members = []
        await env.cog.reconcile()
        assert (await env.row(11))["ended_at"] is None
    with_env(body, monkeypatch)

    async def no_role(env):
        await env.cog.reconcile()
        await env.cog.run_stipend()
        assert await env.cog.meta(STARTED_KEY) is None and env.guild.general.sent == []
    with_env(no_role, monkeypatch, with_role=False)


# ---------------------------------------------------------------- monthly stipend
def test_monthly_stipend_pays_holders_once(monkeypatch):
    async def body(env):
        env.member(11, supporter=True)
        env.member(12, supporter=True)
        env.member(young(ts(2026, 11, 1)), supporter=True)
        env.member(13)  # not a supporter
        env.member(14, supporter=True, bot=True)
        await env.cog.run_stipend()  # first sighting: marks October done, pays nothing
        assert await env.refs() == []
        env.t = ts(2026, 11, 1, 12, 14)
        await env.cog.run_stipend()
        assert await env.refs() == []
        env.t = ts(2026, 11, 1, 12, 15)
        await env.cog.run_stipend()
        assert await env.refs() == ["supporterstipend:2026-11:11", "supporterstipend:2026-11:12"]
        env.t += 300
        await env.cog.run_stipend()
        assert len(await env.refs()) == 2 and await env.balance(11) == S.STIPEND_COINS
        key = R.latest_due_monthly(S.STIPEND_JOB, env.t, TZ).key
        assert await env.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", (key,)) is not None
        assert env.guild.general.sent == []  # the stipend is silent
    with_env(body, monkeypatch)


def test_stipend_on_gain_and_monthly_job_share_one_ref(monkeypatch):
    async def body(env):
        await env.cog.run_stipend()
        env.t = ts(2026, 11, 1, 9, 0)
        await env.gain(env.member(11))  # joined the tier on the 1st, before the job
        env.t = ts(2026, 11, 1, 12, 15)
        await env.cog.run_stipend()
        assert await env.refs() == ["supporterstipend:2026-11:11"]
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- /support
def test_support_says_not_open_until_the_url_is_set_and_allowed(monkeypatch):
    async def body(env):
        async def ask():
            i = env.interaction(env.member(11 + len(env.guild.members)))
            await Supporter.support.callback(env.cog, i)
            return i.response.sent[0]

        assert await ask() == dict(content=S.NOT_OPEN, ephemeral=True)
        for bad in ("http://ko-fi.com/x", "https://evil.example/x", "javascript:alert(1)"):
            monkeypatch.setenv("SUPPORT_URL", bad)
            assert await ask() == dict(content=S.NOT_OPEN, ephemeral=True), bad
        monkeypatch.setenv("SUPPORT_URL", "https://ko-fi.com/frontdesk#x")
        sent = await ask()
        assert sent["ephemeral"] is True and sent["embed"].description == S.perks_text()
        (button,) = sent["view"].children
        assert button.url == "https://ko-fi.com/frontdesk" and button.style is discord.ButtonStyle.link
    with_env(body, monkeypatch)


def test_a_bad_support_url_is_warned_about_once_without_the_value(monkeypatch, caplog):
    async def body(env):
        monkeypatch.setenv("SUPPORT_URL", "https://evil.example/secret-path")
        with caplog.at_level("WARNING", logger="cogs.supporter"):
            for _ in range(3):
                assert env.cog.support_url() is None
        lines = [r.getMessage() for r in caplog.records if "SUPPORT_URL" in r.getMessage()]
        assert len(lines) == 1 and "secret-path" not in lines[0] and "evil.example" not in lines[0]
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- /supporter grant|revoke
def test_staff_grant_and_revoke_are_logged(monkeypatch):
    async def body(env):
        mod, m = env.member(2, roles=[env.guild.mod]), env.member(11)
        i = env.interaction(mod)
        await Supporter.grant.callback(env.cog, i, m)
        assert env.guild.supporter in m.roles and "by user2 (2)" in m.reasons[0]
        assert i.followup.sent[0]["content"] == "Supporter granted to <@11>." and i.followup.sent[0]["ephemeral"]
        assert env.log_lines() == ["💜 Supporter granted to <@11> (user11 · `11`)\n**By** <@2>"]
        assert (await env.row(11))["source"] == "manual"
        assert len(env.guild.general.sent) == 1 and await env.balance(11) == S.STIPEND_COINS
        # The gateway event for that change arrives afterwards: nothing is doubled.
        await env.cog.on_member_update(m.without(env.guild.supporter), m)
        assert len(env.log_lines()) == 1 and len(env.guild.general.sent) == 1
        again = env.interaction(mod)
        await Supporter.grant.callback(env.cog, again, m)
        assert "already has" in again.response.sent[0]["content"]

        env.t += DAY
        j = env.interaction(mod)
        await Supporter.revoke.callback(env.cog, j, m)
        assert env.guild.supporter not in m.roles and (await env.row(11))["ended_at"] == env.t
        assert env.log_lines()[-1] == "💜 Supporter revoked from <@11> (user11 · `11`)\n**By** <@2>"
        assert len(env.log_lines()) == 2 and len(env.guild.general.sent) == 1
        assert env.cog.manual == set()
        k = env.interaction(mod)
        await Supporter.revoke.callback(env.cog, k, m)
        assert "does not have" in k.response.sent[0]["content"]
    with_env(body, monkeypatch)


def test_manual_path_is_staff_only_and_checks_the_role(monkeypatch):
    perms = Supporter.supporter.default_permissions
    assert perms is not None and perms.value == discord.Permissions(moderate_members=True).value
    assert Supporter.support.default_permissions is None

    async def body(env):
        mod, m = env.member(2, roles=[env.guild.mod]), env.member(11)

        async def grant(user, target):
            i = env.interaction(user)
            await Supporter.grant.callback(env.cog, i, target)
            return (i.response.sent or i.followup.sent)[0]["content"]

        assert await grant(env.member(3), m) == cogmod.STAFF_ONLY
        assert "not a bot" in await grant(mod, env.member(12, bot=True))
        env.guild.supporter.managed = True  # a Server Subscriptions role
        assert "managed by Discord" in await grant(mod, m)
        env.guild.supporter.managed = False
        env.guild.supporter.position = 55  # above Front Desk
        assert "below Front Desk" in await grant(mod, m)
        env.guild.supporter.position = 5
        m.fail = http_error()
        assert "didn't let me" in await grant(mod, m)
        assert env.guild.supporter not in m.roles and await env.row(11) is None
        assert env.guild.mod_log.sent == [] and env.cog.manual == set()
    with_env(body, monkeypatch)

    async def no_role(env):
        i = env.interaction(env.member(2, roles=[env.guild.mod]))
        await Supporter.grant.callback(env.cog, i, env.member(11))
        assert i.response.sent[0]["content"] == cogmod.NO_ROLE
    with_env(no_role, monkeypatch, with_role=False)
