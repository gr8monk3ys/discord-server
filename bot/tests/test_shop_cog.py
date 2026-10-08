"""Offline tests for cogs.shop.Shop: a real in-memory SQLite database plus small fakes for
the bot, guild, roles, members, channels and interactions. No network. Time is controlled
by patching cogs.shop.now."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

import config
import db as dbmod
import economy as E
from cogs import shop as cogmod
from cogs.shop import Shop
from logic import shop as S

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
A, B, C, D, E5 = 1, 2, 3, 4, 5
KEEPER_COLOUR, MOD_COLOUR = 0xF1C40F, 0x3498DB


def run(coro):
    return asyncio.run(coro)


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


T0 = ts(2026, 10, 4, 12)


def http_error(status=500):
    return discord.HTTPException(SimpleNamespace(status=status, reason="boom"), "boom")


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")


# ---------------------------------------------------------------- fakes
class FakeRole:
    def __init__(self, guild, rid, name, position, colour=0):
        self.guild, self.id, self.name, self.position = guild, rid, name, position
        self.colour = discord.Colour(colour)
        self.deleted = False

    @property
    def members(self):
        return [m for m in self.guild.members if self in m.roles]

    def __gt__(self, other):
        return self.position > other.position

    def __lt__(self, other):
        return self.position < other.position

    async def edit(self, reason=None, **changes):
        self.guild.maybe_fail("edit_role")
        for k, v in changes.items():
            setattr(self, k, v)

    async def delete(self, reason=None):
        self.guild.maybe_fail("delete_role")
        self.deleted = True
        self.guild.roles.remove(self)
        for m in self.guild.members:
            if self in m.roles:
                m.roles.remove(self)


class FakeMember:
    def __init__(self, uid, guild, name=None):
        self.id, self.guild = uid, guild
        self.display_name = name or f"user{uid}"
        self.mention = f"<@{uid}>"
        self.roles = []
        self.on_add = None  # hook run inside add_roles

    def __str__(self):
        return self.display_name

    async def add_roles(self, *roles, reason=None):
        self.guild.maybe_fail("add_roles")
        if self.on_add:
            await self.on_add()
        for r in roles:
            if r not in self.roles:
                self.roles.append(r)

    async def remove_roles(self, *roles, reason=None):
        self.guild.maybe_fail("remove_roles")
        for r in roles:
            if r in self.roles:
                self.roles.remove(r)


class FakeMessage:
    def __init__(self, channel, content, embed, allowed_mentions):
        self.channel, self.content, self.embed, self.allowed_mentions = channel, content, embed, allowed_mentions
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, guild, cid, name):
        self.guild, self.id, self.name = guild, cid, name
        self.mention = f"<#{cid}>"
        self.sent = []

    async def send(self, content=None, embed=None, allowed_mentions=None):
        self.guild.maybe_fail("send")
        msg = FakeMessage(self, content, embed, allowed_mentions)
        self.sent.append(msg)
        return msg


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.fail = {}  # action -> number of times to raise
        self.next_role = 500
        self.members = []
        self.everyone = FakeRole(self, 1, "@everyone", 0)
        self.season_role = FakeRole(self, 10, config.SEASON_ROLE, 3)
        self.hype = FakeRole(self, 11, config.HYPE_ROLE, 4)
        self.mod = FakeRole(self, 12, config.MOD_ROLE, 8, MOD_COLOUR)
        self.keeper = FakeRole(self, 13, config.KEEPER_ROLE, 9, KEEPER_COLOUR)
        self.bot_role = FakeRole(self, 14, "Front Desk", 10)
        self.roles = [self.everyone, self.season_role, self.hype, self.mod, self.keeper, self.bot_role]
        self.me = FakeMember(50, self, "Front Desk")
        self.me.top_role = self.bot_role
        self.general = FakeChannel(self, 200, config.GENERAL_CHANNEL)
        self.announcements = FakeChannel(self, 201, config.ANNOUNCEMENTS_CHANNEL)
        self.text_channels = [self.general, self.announcements]

    def maybe_fail(self, action):
        if self.fail.get(action):
            self.fail[action] -= 1
            raise http_error()

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    async def fetch_member(self, uid):
        raise not_found()

    async def create_role(self, name, colour, permissions=None, hoist=None, mentionable=None, reason=None):
        self.maybe_fail("create_role")
        self.next_role += 1
        role = FakeRole(self, self.next_role, name, 1, colour.value)
        role.permissions, role.hoist, role.mentionable = permissions, hoist, mentionable
        self.roles.append(role)
        return role


class FakeBot:
    def __init__(self, db, guild):
        self.db, self.guild = db, guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if self.guild is not None and guild_id == self.guild.id else None


class FakeResponse:
    def __init__(self, calls):
        self.calls, self.deferred = calls, False

    async def send_message(self, content=None, **kwargs):
        self.calls.append(dict(content=content, **kwargs))

    async def defer(self, **kwargs):
        self.deferred = True


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(dict(content=content, **kwargs))


class FakeInteraction:
    def __init__(self, iid, user):
        self.id, self.user = iid, user
        self.calls = []
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    @property
    def text(self):
        assert len(self.calls) == 1
        return self.calls[0]["content"]


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0
        self.next_iid = 7000

    def member(self, uid, name=None):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, self.guild, name)
            self.guild.members.append(m)
        return m

    async def fund(self, uid, amount, reason="test", at=None):
        await E.apply(self.db, uid, amount, reason, self.t if at is None else at)

    async def bal(self, uid):
        return await E.balance(self.db, uid)

    async def shop_ledger(self, uid=None):
        rows = await self.db.fetchall("SELECT * FROM ledger WHERE reason = 'shop' ORDER BY id")
        return [dict(r) for r in rows if uid is None or r["user_id"] == uid]

    async def perk(self, uid, kind):
        row = await self.db.fetchone("SELECT * FROM perks WHERE user_id = ? AND kind = ?", (uid, kind))
        return dict(row) if row else None

    async def buy(self, uid, item, color=None, message=None, iid=None):
        if iid is None:
            self.next_iid += 1
            iid = self.next_iid
        i = FakeInteraction(iid, self.member(uid))
        await Shop.buy.callback(self.cog, i, app_commands.Choice(name=item, value=item), color, message)
        return i

    async def season_cmd(self, uid):
        i = FakeInteraction(1, self.member(uid))
        await Shop.season.callback(self.cog, i)
        return i.calls[0]


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Shop(bot)
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def personal_roles(guild):
    return [r for r in guild.roles if r.id > 500]


# ---------------------------------------------------------------- /shop
def test_shop_lists_items_with_prices(monkeypatch):
    async def go(env):
        await env.fund(A, 1234)
        i = FakeInteraction(1, env.member(A))
        await Shop.shop.callback(env.cog, i)
        text = i.calls[0]["embed"].description
        assert "2,000" in text and "500" in text and "300" in text
        assert i.calls[0]["ephemeral"] and "1,234" in i.calls[0]["embed"].footer.text
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- colour
def test_color_purchase_creates_role_and_charges_once(monkeypatch):
    async def go(env):
        await env.fund(A, 2500)
        m = env.member(A, "Lorenzo")
        i = await env.buy(A, "color", color="#3BA55D")
        [role] = personal_roles(env.guild)
        assert role.name == S.role_name("Lorenzo") and role.colour.value == 0x3BA55D
        # bot 10 > Keeper 9 > Moderator 8: directly below the lowest staff role
        assert role.position == env.guild.mod.position - 1
        assert role.permissions == discord.Permissions.none() and role.hoist is False and role.mentionable is False
        assert role in m.roles
        assert await env.bal(A) == 500
        [row] = await env.shop_ledger(A)
        assert row["delta"] == -2000 and row["ref"] == f"shop:{env.next_iid}"
        perk = await env.perk(A, "color")
        assert perk["role_id"] == role.id and perk["expires_at"] == T0 + 30 * S.DAY
        assert "#3BA55D" in i.text and "500 left" in i.text
    with_env(go, monkeypatch)


def test_color_role_goes_below_lowest_of_bot_and_staff(monkeypatch):
    async def go(env):
        await env.fund(A, 2000)
        env.guild.bot_role.position, env.guild.keeper.position, env.guild.mod.position = 6, 15, 12
        await env.buy(A, "color", color="#3BA55D")
        assert personal_roles(env.guild)[0].position == 5  # bot is lowest here
    with_env(go, monkeypatch)


def test_color_refused_without_safe_slot(monkeypatch):
    async def go(env):
        await env.fund(A, 2000)
        env.guild.mod.position = 1  # nothing between @everyone and Moderator
        i = await env.buy(A, "color", color="#3BA55D")
        assert "no safe spot" in i.text and "weren't charged" in i.text
        assert personal_roles(env.guild) == [] and await env.bal(A) == 2000
        assert await env.shop_ledger() == []
    with_env(go, monkeypatch)


def test_same_interaction_retried_charges_once(monkeypatch):
    async def go(env):
        await env.fund(A, 5000)
        await env.buy(A, "color", color="#3BA55D", iid=42)
        i = await env.buy(A, "color", color="#3BA55D", iid=42)
        assert "already" in i.text
        assert await env.bal(A) == 3000 and len(await env.shop_ledger(A)) == 1
    with_env(go, monkeypatch)


def test_color_role_creation_fails_no_charge(monkeypatch):
    async def go(env):
        await env.fund(A, 2500)
        env.guild.fail["create_role"] = 1
        i = await env.buy(A, "color", color="#3BA55D")
        assert "weren't charged" in i.text
        assert await env.bal(A) == 2500 and await env.shop_ledger() == []
        assert await env.perk(A, "color") is None and personal_roles(env.guild) == []
    with_env(go, monkeypatch)


def test_color_add_role_fails_deletes_new_role_no_charge(monkeypatch):
    async def go(env):
        await env.fund(A, 2500)
        env.guild.fail["add_roles"] = 1
        i = await env.buy(A, "color", color="#3BA55D")
        assert "weren't charged" in i.text
        assert personal_roles(env.guild) == [] and await env.bal(A) == 2500
        assert await env.perk(A, "color") is None
    with_env(go, monkeypatch)


def test_color_insufficient_funds(monkeypatch):
    async def go(env):
        await env.fund(A, 1999)
        i = await env.buy(A, "color", color="#3BA55D")
        assert "2,000" in i.text and "1,999" in i.text
        assert personal_roles(env.guild) == [] and await env.bal(A) == 1999
    with_env(go, monkeypatch)


def test_color_spent_elsewhere_mid_purchase_is_undone(monkeypatch):
    async def go(env):
        await env.fund(A, 2000)
        m = env.member(A)

        async def spend():  # e.g. a coinflip lands while Discord is creating the role
            await E.apply(env.db, A, -100, "coinflip", env.t)
        m.on_add = spend
        i = await env.buy(A, "color", color="#3BA55D")
        assert "weren't charged" in i.text
        assert personal_roles(env.guild) == [] and await env.shop_ledger() == []
        assert await env.perk(A, "color") is None and await env.bal(A) == 1900
    with_env(go, monkeypatch)


def test_color_rejects_bad_dark_and_staff_colours(monkeypatch):
    async def go(env):
        await env.fund(A, 5000)
        for colour, word in (("blue", "hex"), ("#101010", "dark"), ("#F0C410", "staff"), ("#3498DA", "staff"),
                             (None, "Pick a colour")):
            i = await env.buy(A, "color", color=colour)
            assert word in i.text
        assert await env.bal(A) == 5000 and personal_roles(env.guild) == []
    with_env(go, monkeypatch)


def test_color_rebuy_extends_and_recolours_same_role(monkeypatch):
    async def go(env):
        await env.fund(A, 4000)
        await env.buy(A, "color", color="#3BA55D")
        env.t = T0 + 10 * S.DAY
        await env.buy(A, "color", color="#FF66AA")
        [role] = personal_roles(env.guild)
        assert role.colour.value == 0xFF66AA
        assert (await env.perk(A, "color"))["expires_at"] == T0 + 60 * S.DAY
        assert await env.bal(A) == 0 and len(await env.shop_ledger(A)) == 2
    with_env(go, monkeypatch)


def test_color_rebuy_when_role_was_deleted_makes_a_new_one(monkeypatch):
    async def go(env):
        await env.fund(A, 4000)
        await env.buy(A, "color", color="#3BA55D")
        await personal_roles(env.guild)[0].delete()  # a mod deleted it
        await env.buy(A, "color", color="#FF66AA")
        [role] = personal_roles(env.guild)
        assert role.colour.value == 0xFF66AA and role in env.member(A).roles
        assert (await env.perk(A, "color"))["role_id"] == role.id
    with_env(go, monkeypatch)


def test_color_recolour_fails_restores_old_colour(monkeypatch):
    async def go(env):
        await env.fund(A, 4000)
        await env.buy(A, "color", color="#3BA55D")
        env.guild.fail["add_roles"] = 0
        env.guild.fail["edit_role"] = 1
        i = await env.buy(A, "color", color="#FF66AA")
        assert "weren't charged" in i.text
        assert personal_roles(env.guild)[0].colour.value == 0x3BA55D
        assert await env.bal(A) == 2000
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- hype
def test_hype_purchase_and_rebuy_extends(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        i = await env.buy(A, "hype")
        assert env.guild.hype in env.member(A).roles and "Hype" in i.text
        assert (await env.perk(A, "hype"))["expires_at"] == T0 + S.DAY
        env.t = T0 + 3600
        await env.buy(A, "hype")
        assert (await env.perk(A, "hype"))["expires_at"] == T0 + 2 * S.DAY
        assert await env.bal(A) == 0 and len(await env.shop_ledger(A)) == 2
    with_env(go, monkeypatch)


def test_hype_missing_role(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        env.guild.roles.remove(env.guild.hype)
        i = await env.buy(A, "hype")
        assert "no `Hype` role" in i.text
        assert await env.bal(A) == 1000 and await env.perk(A, "hype") is None
    with_env(go, monkeypatch)


def test_hype_role_above_bot(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        env.guild.hype.position = 20
        i = await env.buy(A, "hype")
        assert "above `Hype`" in i.text and await env.bal(A) == 1000
        assert env.guild.hype not in env.member(A).roles
    with_env(go, monkeypatch)


def test_hype_insufficient_and_add_failure(monkeypatch):
    async def go(env):
        await env.fund(A, 499)
        i = await env.buy(A, "hype")
        assert "499" in i.text and env.guild.hype not in env.member(A).roles
        await env.fund(A, 1)
        env.guild.fail["add_roles"] = 1
        i = await env.buy(A, "hype")
        assert "weren't charged" in i.text and await env.bal(A) == 500
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- shoutout
def test_shoutout_posts_without_pings_and_charges(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        i = await env.buy(A, "shoutout", message="gg all, great raid tonight")
        [msg] = env.guild.general.sent
        assert msg.embed.description == f"📣 <@{A}> says: gg all, great raid tonight"
        am = msg.allowed_mentions
        assert am.everyone is False and am.users is False and am.roles is False
        assert await env.bal(A) == 700 and "Posted" in i.text
    with_env(go, monkeypatch)


def test_shoutout_filtering_never_charges(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        for text in ("see https://x.co", "discord.gg/abc", "hey <@2>", "@everyone", "a\nb", "x" * 141, None):
            i = await env.buy(A, "shoutout", message=text)
            assert "weren't" not in i.text and "Posted" not in i.text
        assert env.guild.general.sent == [] and await env.bal(A) == 1000
    with_env(go, monkeypatch)


def test_shoutout_cooldown_24h(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        await env.buy(A, "shoutout", message="one")
        env.t = T0 + 23 * 3600
        i = await env.buy(A, "shoutout", message="two")
        assert "24 hours" in i.text and "1h 00m" in i.text
        env.t = T0 + S.DAY
        await env.buy(A, "shoutout", message="three")
        assert [m.embed.description.split(": ")[1] for m in env.guild.general.sent] == ["one", "three"]
        assert await env.bal(A) == 400
    with_env(go, monkeypatch)


def test_shoutout_send_fails_no_charge(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        env.guild.fail["send"] = 1
        i = await env.buy(A, "shoutout", message="hello")
        assert "weren't charged" in i.text and await env.bal(A) == 1000
        assert await env.perk(A, "shoutout") is None  # no cooldown either
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- expiry
def test_expiry_removes_perks_and_rows(monkeypatch):
    async def go(env):
        await env.fund(A, 10_000)
        await env.fund(B, 10_000)
        await env.buy(A, "color", color="#3BA55D")
        await env.buy(A, "hype")
        await env.buy(A, "shoutout", message="hi")
        await env.buy(B, "hype")
        env.t = T0 + S.DAY - 60
        await env.buy(B, "hype")  # extended: still has a day left
        env.t = T0 + S.DAY
        await env.cog.run_expiry()
        a = env.member(A)
        assert env.guild.hype not in a.roles and env.guild.hype in env.member(B).roles
        assert await env.perk(A, "hype") is None and await env.perk(A, "shoutout") is None
        assert await env.perk(A, "color") is not None and personal_roles(env.guild)
        env.t = T0 + 30 * S.DAY
        await env.cog.run_expiry()
        assert personal_roles(env.guild) == [] and a.roles == []
        assert await env.perk(A, "color") is None
    with_env(go, monkeypatch)


def test_expiry_member_gone_just_cleans_up(monkeypatch):
    async def go(env):
        await env.fund(A, 10_000)
        await env.buy(A, "color", color="#3BA55D")
        await env.buy(A, "hype")
        env.guild.members.remove(env.member(A))  # left the server
        env.t = T0 + 31 * S.DAY
        await env.cog.run_expiry()
        assert await env.db.fetchall("SELECT * FROM perks") == []
        assert personal_roles(env.guild) == []
    with_env(go, monkeypatch)


def test_expiry_role_already_deleted(monkeypatch):
    async def go(env):
        await env.fund(A, 10_000)
        await env.buy(A, "color", color="#3BA55D")
        await personal_roles(env.guild)[0].delete()
        env.t = T0 + 31 * S.DAY
        await env.cog.run_expiry()
        assert await env.perk(A, "color") is None
    with_env(go, monkeypatch)


def test_expiry_discord_error_retries_next_tick(monkeypatch):
    async def go(env):
        await env.fund(A, 10_000)
        await env.buy(A, "hype")
        env.t = T0 + S.DAY
        env.guild.fail["remove_roles"] = 1
        await env.cog.run_expiry()  # doesn't raise
        assert await env.perk(A, "hype") is not None
        await env.cog.run_expiry()
        assert await env.perk(A, "hype") is None and env.guild.hype not in env.member(A).roles
    with_env(go, monkeypatch)


def test_expiry_skips_perks_rebought_after_it_read_them(monkeypatch):
    """A rebuy that lands between the expiry SELECT and the member's lock must keep
    its role: expiry re-reads the row under the lock."""
    async def go(env):
        await env.fund(A, 10_000)
        await env.buy(A, "color", color="#3BA55D")
        await env.buy(A, "hype")
        env.t = T0 + 31 * S.DAY  # both expired, loop hasn't run yet
        lock = env.cog.lock(A)
        await lock.acquire()  # /buy is mid-purchase
        task = asyncio.create_task(env.cog.run_expiry())
        for _ in range(20):
            await asyncio.sleep(0)  # expiry reads the stale rows, then waits for the lock
        color_role = personal_roles(env.guild)[0]
        for key in ("hype", "color"):  # the rebuys commit under the lock
            role_id = color_role.id if key == "color" else None
            result = await env.cog.charge(A, S.ITEMS[key], env.t + S.DAY, role_id, f"rebuy-{key}")
            assert result.ok
        lock.release()
        await task
        a = env.member(A)
        assert env.guild.hype in a.roles and color_role in a.roles and color_role in env.guild.roles
        assert (await env.perk(A, "hype"))["expires_at"] == env.t + S.DAY
        assert (await env.perk(A, "color"))["expires_at"] == env.t + S.DAY
    with_env(go, monkeypatch)


def test_loops_never_raise(monkeypatch):
    async def go(env):
        async def boom():
            raise RuntimeError("x")
        env.cog.run_expiry = boom
        env.cog.run_seasons = boom
        await Shop.expiry.coro(env.cog)
        await Shop.seasons.coro(env.cog)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- seasons
SEP = ts(2026, 9, 10, 12)


async def earn(env, uid, amount, reason="daily", at=SEP):
    await E.apply(env.db, uid, amount, reason, at)


def test_season_points_count_only_activity_in_the_month(monkeypatch):
    async def go(env):
        for reason in ("daily", "voice", "message", "clip", "lfg", "mvp", "clipweek", "trivia"):
            await earn(env, A, 10, reason)
        for reason in ("give", "coinflip", "slots", "blackjack", "predict", "test", "season"):
            await earn(env, A, 1000, reason)
        await earn(env, A, -50, "shop")  # spending doesn't lower points either
        await earn(env, A, 999, "daily", at=ts(2026, 8, 31, 23, 59))  # last month
        await earn(env, A, 999, "daily", at=ts(2026, 10, 1, 0, 0))  # next month
        await earn(env, A, 7, "voice", at=ts(2026, 9, 1, 0, 0))  # first second counts
        assert [(s.user_id, s.points) for s in await env.cog.standings("2026-09")] == [(A, 87)]
    with_env(go, monkeypatch)


def test_season_standings_ties_use_first_earning_then_id(monkeypatch):
    async def go(env):
        await earn(env, C, 100, at=ts(2026, 9, 2))
        await earn(env, B, 100, at=ts(2026, 9, 3))
        await earn(env, A, 50, at=ts(2026, 9, 3))
        await earn(env, A, 50, at=ts(2026, 9, 4))
        await earn(env, D, 100, at=ts(2026, 9, 3))
        assert [s.user_id for s in await env.cog.standings("2026-09")] == [C, A, B, D]
    with_env(go, monkeypatch)


def test_season_command_top10_my_rank_no_pings(monkeypatch):
    async def go(env):
        oct_ = ts(2026, 10, 2)
        for uid in range(100, 112):
            await earn(env, uid, 1000 - uid, at=oct_)
        await earn(env, A, 1, at=oct_)
        call = await env.season_cmd(A)
        text = call["embed"].description
        assert text.count("<@1") == 11 and "`13`  <@1>  1" in text
        assert "<@111>" not in text
        assert call["allowed_mentions"].users is False and call["allowed_mentions"].everyone is False
        assert "28 days left" in call["embed"].footer.text.lower()
        call = await env.season_cmd(B)
        assert "haven't earned" in call["embed"].description
    with_env(go, monkeypatch)


def test_rollover_first_run_marks_previous_month_without_posting(monkeypatch):
    async def go(env):
        await earn(env, A, 500)
        await env.cog.run_seasons()
        rows = [dict(r) for r in await env.db.fetchall("SELECT * FROM seasons")]
        assert rows == [{"key": "2026-09", "posted_at": T0, "top": None}]
        assert env.guild.announcements.sent == [] and env.guild.general.sent == []
        assert await env.bal(A) == 500
        await env.cog.run_seasons()
        assert env.guild.announcements.sent == []
    with_env(go, monkeypatch)


async def setup_finished_september(env):
    await env.db.execute("INSERT INTO seasons (key, posted_at, top) VALUES ('2026-08', 0, NULL)")
    await earn(env, A, 300)
    await earn(env, B, 200)
    await earn(env, C, 100)
    await earn(env, D, 50)
    await earn(env, E5, 5000, "coinflip")  # gambling doesn't count
    for uid in (A, B, C, D):
        env.member(uid)
    env.member(D).roles.append(env.guild.season_role)  # last season's champ
    env.member(A).roles.append(env.guild.season_role)  # champ again
    env.t = ts(2026, 10, 1, 0, 3)


def test_rollover_posts_top3_swaps_role_and_pays(monkeypatch):
    async def go(env):
        await setup_finished_september(env)
        await env.cog.run_seasons()
        [msg] = env.guild.announcements.sent
        assert env.guild.general.sent == []
        assert "<@1>" in msg.content and "<@2>" in msg.content and "<@3>" in msg.content
        assert "September 2026" in msg.content
        am = msg.allowed_mentions
        assert sorted(u.id for u in am.users) == [A, B, C] and am.roles is False and am.everyone is False
        assert sorted(m.id for m in env.guild.season_role.members) == [A, B, C]
        assert [await env.bal(u) for u in (A, B, C, D)] == [1300, 700, 350, 50]
        refs = [r["ref"] for r in await env.db.fetchall("SELECT ref FROM ledger WHERE reason = 'season'")]
        assert sorted(refs) == ["season:2026-09:1", "season:2026-09:2", "season:2026-09:3"]
        row = await env.db.fetchone("SELECT * FROM seasons WHERE key = '2026-09'")
        assert json.loads(row["top"]) == [A, B, C]
        await env.cog.run_seasons()  # done: nothing more
        assert len(env.guild.announcements.sent) == 1 and await env.bal(A) == 1300
    with_env(go, monkeypatch)


def test_rollover_retry_after_post_failure_is_idempotent(monkeypatch):
    async def go(env):
        await setup_finished_september(env)
        env.guild.fail["send"] = 1
        await Shop.seasons.coro(env.cog)  # the loop body swallows the error
        assert await env.db.fetchone("SELECT 1 FROM seasons WHERE key = '2026-09'") is None
        env.t += 300
        await env.cog.run_seasons()
        assert len(env.guild.announcements.sent) == 1
        assert [await env.bal(u) for u in (A, B, C)] == [1300, 700, 350]  # bonuses paid once
        assert len(await env.db.fetchall("SELECT 1 FROM ledger WHERE reason = 'season'")) == 3
        assert await env.db.fetchone("SELECT 1 FROM seasons WHERE key = '2026-09'") is not None
    with_env(go, monkeypatch)


def test_rollover_falls_back_to_general_and_skips_unusable_role(monkeypatch):
    async def go(env):
        await setup_finished_september(env)
        env.guild.text_channels.remove(env.guild.announcements)
        env.guild.season_role.position = 20  # above Front Desk: leave it alone
        await env.cog.run_seasons()
        assert len(env.guild.general.sent) == 1
        assert sorted(m.id for m in env.guild.season_role.members) == [A, D]
        assert await env.bal(A) == 1300
    with_env(go, monkeypatch)


def test_rollover_winner_left_and_no_role(monkeypatch):
    async def go(env):
        await setup_finished_september(env)
        env.guild.members.remove(env.member(B))
        await env.cog.run_seasons()
        assert sorted(m.id for m in env.guild.season_role.members) == [A, C]
        assert len(env.guild.announcements.sent) == 1
    with_env(go, monkeypatch)


def test_rollover_missing_season_role_still_posts(monkeypatch):
    async def go(env):
        await setup_finished_september(env)
        env.guild.roles.remove(env.guild.season_role)
        await env.cog.run_seasons()
        assert len(env.guild.announcements.sent) == 1 and await env.bal(A) == 1300
    with_env(go, monkeypatch)


def test_rollover_quiet_month_marks_done(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO seasons (key, posted_at, top) VALUES ('2026-08', 0, NULL)")
        env.t = ts(2026, 10, 1, 0, 3)
        await env.cog.run_seasons()
        assert env.guild.announcements.sent == []
        row = await env.db.fetchone("SELECT top FROM seasons WHERE key = '2026-09'")
        assert json.loads(row["top"]) == []
    with_env(go, monkeypatch)


def test_rollover_waits_when_guild_unavailable(monkeypatch):
    async def go(env):
        await setup_finished_september(env)
        env.bot.guild = None
        await env.cog.run_seasons()
        assert await env.db.fetchone("SELECT 1 FROM seasons WHERE key = '2026-09'") is None
        assert await env.bal(A) == 300
    with_env(go, monkeypatch)


def test_hype_role_with_mod_permissions_is_not_sold(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        env.guild.hype.permissions = discord.Permissions(manage_messages=True)
        i = await env.buy(A, "hype")
        assert "permissions" in i.text and await env.bal(A) == 1000
        assert env.guild.hype not in env.member(A).roles
    with_env(go, monkeypatch)
