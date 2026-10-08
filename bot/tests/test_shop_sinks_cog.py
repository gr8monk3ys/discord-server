"""Offline tests for the newer coin sinks in cogs.shop: Gift Hype, Spotlight and the weekly
raffle. Reuses the fakes from test_shop_cog and adds pins, DMs and message ids."""

import asyncio
import random

import discord
from discord import app_commands

import config
import db as dbmod
import economy as E
from cogs import shop as cogmod
from cogs.shop import Shop
from logic import quests as Q
from logic import shop as S

import test_shop_cog as base
from test_shop_cog import A, B, C, D, TZ, FakeInteraction, http_error, not_found, ts

T0 = base.T0  # Sunday 2026-10-04 12:00 local
DRAW = ts(2026, 10, 4, 20)  # that day's raffle draw
KEY = "raffle:2026-W40"


# ---------------------------------------------------------------- fakes
class Msg(base.FakeMessage):
    _next = 9000

    def __init__(self, *args):
        super().__init__(*args)
        Msg._next += 1
        self.id = Msg._next
        self.pinned = False

    async def pin(self, reason=None):
        self.channel.guild.maybe_fail("pin")
        self.pinned = True


class Partial:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid

    async def unpin(self, reason=None):
        self.channel.guild.maybe_fail("unpin")
        msg = next((m for m in self.channel.sent if m.id == self.id), None)
        if msg is None or msg.deleted:
            raise not_found()
        msg.pinned = False


class Channel(base.FakeChannel):
    async def send(self, content=None, embed=None, allowed_mentions=None):
        self.guild.maybe_fail("send")
        msg = Msg(self, content, embed, allowed_mentions)
        self.sent.append(msg)
        return msg

    def get_partial_message(self, mid):
        return Partial(self, mid)


class Member(base.FakeMember):
    def __init__(self, uid, guild, name=None, bot=False):
        super().__init__(uid, guild, name)
        self.bot = bot
        self.dms = []
        self.dm_fail = False

    async def send(self, content=None, **kwargs):
        if self.dm_fail:
            raise discord.Forbidden(base.SimpleNamespace(status=403, reason="closed"), "closed")
        self.dms.append(dict(content=content, **kwargs))


class Env(base.Env):
    def member(self, uid, name=None, bot=False):
        m = self.guild.get_member(uid)
        if m is None:
            m = Member(uid, self.guild, name, bot)
            self.guild.members.append(m)
        return m

    async def buy(self, uid, item, message=None, friend=None, iid=None):
        if iid is None:
            self.next_iid += 1
            iid = self.next_iid
        i = FakeInteraction(iid, self.member(uid))
        await Shop.buy.callback(self.cog, i, app_commands.Choice(name=item, value=item), None, message, friend)
        return i

    async def tickets(self, uid, n=1, iid=None):
        if iid is None:
            self.next_iid += 1
            iid = self.next_iid
        i = FakeInteraction(iid, self.member(uid))
        await Shop.raffle_buy.callback(self.cog, i, n)
        return i

    async def info(self, uid):
        i = FakeInteraction(1, self.member(uid))
        await Shop.raffle_info.callback(self.cog, i)
        return i.calls[0]["embed"].description

    async def raffle_ledger(self, uid=None):
        rows = await self.db.fetchall("SELECT * FROM ledger WHERE reason = 'raffle' ORDER BY id")
        return [dict(r) for r in rows if uid is None or r["user_id"] == uid]

    async def slot(self):
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (S.SPOTLIGHT_KEY,))
        return S.spotlight_load(row["value"]) if row else None


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = base.FakeGuild()
        guild.general = Channel(guild, 200, config.GENERAL_CHANNEL)
        guild.text_channels = [guild.general, guild.announcements]
        bot = base.FakeBot(db, guild)
        cog = Shop(bot)
        cog.rng = random.Random(7)
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    asyncio.run(go())


def young_id(at):
    """A snowflake for an account created 3 days before `at`."""
    return ((at - 3 * S.DAY) * 1000 - Q.DISCORD_EPOCH_MS) << 22


# ---------------------------------------------------------------- /shop
def test_shop_lists_new_items_and_raffle(monkeypatch):
    async def go(env):
        i = FakeInteraction(1, env.member(A))
        await Shop.shop.callback(env.cog, i)
        text = i.calls[0]["embed"].description
        assert "Gift Hype" in text and "Spotlight" in text and "1,500" in text
        assert "/raffle buy" in text and "80%" in text
        assert [c.value for c in cogmod.ITEM_CHOICES] == list(S.ITEMS)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- gift hype
def test_gift_gives_friend_hype_charges_buyer_and_dms(monkeypatch):
    async def go(env):
        await env.fund(A, 600)
        friend = env.member(B)
        i = await env.buy(A, "gift", friend=friend)
        assert env.guild.hype in friend.roles and env.guild.hype not in env.member(A).roles
        assert await env.bal(A) == 100 and await env.bal(B) == 0
        assert (await env.perk(B, "hype"))["expires_at"] == T0 + S.DAY
        assert await env.perk(A, "hype") is None and await env.perk(A, "gift") is None
        [row] = await env.shop_ledger()
        assert row["user_id"] == A and row["delta"] == -500
        [dm] = friend.dms
        assert f"<@{A}>" in dm["content"] and "Hype" in dm["content"]
        am = dm["allowed_mentions"]
        assert am.users is False and am.everyone is False
        assert "<@2>" in i.text and "100 left" in i.text
    with_env(go, monkeypatch)


def test_gift_stacks_on_friends_own_hype_and_expires(monkeypatch):
    async def go(env):
        await env.fund(A, 500)
        await env.fund(B, 500)
        await env.buy(B, "hype")
        env.t = T0 + 3600
        await env.buy(A, "gift", friend=env.member(B))
        assert (await env.perk(B, "hype"))["expires_at"] == T0 + 2 * S.DAY
        env.t = T0 + 2 * S.DAY
        await env.cog.run_expiry()
        assert env.guild.hype not in env.member(B).roles and await env.perk(B, "hype") is None
    with_env(go, monkeypatch)


def test_gift_refusals_never_charge(monkeypatch):
    async def go(env):
        await env.fund(A, 5000)
        bot = env.member(D, bot=True)
        assert "friend:" in (await env.buy(A, "gift")).text
        assert "yourself" in (await env.buy(A, "gift", friend=env.member(A))).text
        assert "Bots" in (await env.buy(A, "gift", friend=bot)).text
        gone = Member(77, env.guild)  # not in guild.members
        assert "in the server" in (await env.buy(A, "gift", friend=gone)).text
        env.guild.hype.position = 20
        assert "above `Hype`" in (await env.buy(A, "gift", friend=env.member(B))).text
        assert await env.bal(A) == 5000 and await env.shop_ledger() == []
        assert env.guild.hype not in env.member(B).roles
    with_env(go, monkeypatch)


def test_gift_broke_or_role_failure_no_charge(monkeypatch):
    async def go(env):
        await env.fund(A, 499)
        i = await env.buy(A, "gift", friend=env.member(B))
        assert "499" in i.text and env.guild.hype not in env.member(B).roles
        await env.fund(A, 1)
        env.guild.fail["add_roles"] = 1
        i = await env.buy(A, "gift", friend=env.member(B))
        assert "weren't charged" in i.text and await env.bal(A) == 500
    with_env(go, monkeypatch)


def test_gift_spent_elsewhere_mid_purchase_takes_role_back(monkeypatch):
    async def go(env):
        await env.fund(A, 500)
        friend = env.member(B)

        async def spend():
            await E.apply(env.db, A, -100, "coinflip", env.t)
        friend.on_add = spend
        i = await env.buy(A, "gift", friend=friend)
        assert "weren't charged" in i.text and env.guild.hype not in friend.roles
        assert await env.perk(B, "hype") is None and friend.dms == []
    with_env(go, monkeypatch)


def test_gift_with_closed_dms_still_goes_through(monkeypatch):
    async def go(env):
        await env.fund(A, 500)
        friend = env.member(B)
        friend.dm_fail = True
        i = await env.buy(A, "gift", friend=friend)
        assert env.guild.hype in friend.roles and await env.bal(A) == 0 and "Hype" in i.text
    with_env(go, monkeypatch)


def test_mutual_gifts_at_once_do_not_deadlock(monkeypatch):
    async def go(env):
        await env.fund(A, 500)
        await env.fund(B, 500)
        a, b = env.member(A), env.member(B)
        await asyncio.wait_for(asyncio.gather(env.buy(A, "gift", friend=b), env.buy(B, "gift", friend=a)), 5)
        assert env.guild.hype in a.roles and env.guild.hype in b.roles
        assert await env.bal(A) == 0 and await env.bal(B) == 0
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- spotlight
def test_spotlight_posts_pins_and_charges(monkeypatch):
    async def go(env):
        await env.fund(A, 2000)
        i = await env.buy(A, "spotlight", message="LFG *ranked* tonight, ~9pm")
        [msg] = env.guild.general.sent
        assert msg.pinned and "Spotlight" in msg.embed.title
        assert msg.embed.description == f"<@{A}>: LFG \\*ranked\\* tonight, \\~9pm"  # markdown escaped
        am = msg.allowed_mentions
        assert am.everyone is False and am.users is False and am.roles is False
        assert await env.bal(A) == 500 and "Pinned" in i.text
        assert await env.slot() == S.Spotlight(A, 200, msg.id, T0 + S.DAY)
        assert (await env.perk(A, "spotlight"))["expires_at"] == T0 + 7 * S.DAY
    with_env(go, monkeypatch)


def test_spotlight_one_at_a_time_and_weekly_per_member(monkeypatch):
    async def go(env):
        for uid in (A, B):
            await env.fund(uid, 5000)
        await env.buy(A, "spotlight", message="one")
        i = await env.buy(B, "spotlight", message="two")
        assert "until" in i.text and await env.bal(B) == 5000
        env.t = T0 + S.DAY  # A's is over, loop hasn't run: B's purchase unpins it first
        await env.buy(B, "spotlight", message="two")
        first, second = env.guild.general.sent
        assert not first.pinned and second.pinned
        assert (await env.slot()).user_id == B
        env.t = T0 + 2 * S.DAY
        await env.cog.run_expiry()
        assert not second.pinned and await env.slot() is None
        i = await env.buy(A, "spotlight", message="again")
        assert "once a week" in i.text.lower() or "a week" in i.text
        env.t = T0 + 7 * S.DAY
        await env.cog.run_expiry()
        await env.buy(A, "spotlight", message="again")
        assert env.guild.general.sent[-1].pinned and await env.bal(A) == 2000
    with_env(go, monkeypatch)


def test_spotlight_filters_and_failures_never_charge(monkeypatch):
    async def go(env):
        await env.fund(A, 2000)
        for text in ("https://x.co", "<@2>", None, "a\nb"):
            assert "Pinned" not in (await env.buy(A, "spotlight", message=text)).text
        env.guild.fail["send"] = 1
        assert "weren't charged" in (await env.buy(A, "spotlight", message="hi")).text
        env.guild.fail["pin"] = 1
        assert "weren't charged" in (await env.buy(A, "spotlight", message="hi")).text
        [msg] = env.guild.general.sent
        assert msg.deleted
        assert await env.bal(A) == 2000 and await env.slot() is None and await env.perk(A, "spotlight") is None
    with_env(go, monkeypatch)


def test_spotlight_broke_after_post_deletes_it(monkeypatch):
    async def go(env):
        await env.fund(A, 1500)
        orig = Msg.pin

        async def pin_and_spend(self, reason=None):
            await orig(self, reason)
            await E.apply(env.db, A, -1, "coinflip", env.t)
        Msg.pin = pin_and_spend
        try:
            i = await env.buy(A, "spotlight", message="hi")
        finally:
            Msg.pin = orig
        assert "weren't charged" in i.text and env.guild.general.sent[0].deleted
        assert await env.slot() is None and await env.bal(A) == 1499
    with_env(go, monkeypatch)


def test_spotlight_expiry_message_gone_or_error(monkeypatch):
    async def go(env):
        await env.fund(A, 1500)
        await env.buy(A, "spotlight", message="hi")
        env.t = T0 + S.DAY
        env.guild.fail["unpin"] = 1
        await env.cog.run_expiry()  # Discord error: kept for the next tick
        assert await env.slot() is not None
        env.guild.general.sent[0].deleted = True  # a mod deleted it
        await env.cog.run_expiry()
        assert await env.slot() is None
        await env.db.execute("INSERT INTO meta (key, value) VALUES (?, 'junk')", (S.SPOTLIGHT_KEY,))
        await env.cog.run_expiry()  # unreadable rows are dropped
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (S.SPOTLIGHT_KEY,)) is None
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- raffle: buying
def test_raffle_buy_charges_and_caps_at_ten(monkeypatch):
    async def go(env):
        await env.fund(A, 5000)
        i = await env.tickets(A, 3)
        assert "**3** tickets" in i.text and "240 coins" in i.text and i.calls[0]["ephemeral"]
        await env.tickets(A, 7)
        i = await env.tickets(A, 1)
        assert "maximum 10" in i.text
        assert await env.bal(A) == 4000
        rows = await env.raffle_ledger(A)
        assert [r["delta"] for r in rows] == [-300, -700]
        assert all(r["ref"].startswith(f"{KEY}:buy:{A}:") for r in rows)
    with_env(go, monkeypatch)


def test_raffle_buy_partial_limit_broke_and_retry(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        await env.tickets(A, 8)
        assert "2 or fewer" in (await env.tickets(A, 3)).text
        i = await env.tickets(A, 2)
        assert "**10** tickets" in i.text and await env.bal(A) == 0
    with_env(go, monkeypatch)


def test_raffle_buy_broke_and_same_interaction_twice(monkeypatch):
    async def go(env):
        await env.fund(A, 250)
        assert "cost 300" in (await env.tickets(A, 3)).text
        await env.tickets(A, 2, iid=42)
        assert "already went through" in (await env.tickets(A, 2, iid=42)).text
        assert await env.bal(A) == 50
        assert "2 or fewer" not in (await env.tickets(A, 8)).text  # 8 more would be 10: allowed, just broke
    with_env(go, monkeypatch)


def test_raffle_young_accounts_and_dms_refused(monkeypatch):
    async def go(env):
        young = young_id(T0)
        await env.fund(young, 5000)
        i = await env.tickets(young, 1)
        assert "too new" in i.text and await env.bal(young) == 5000
        dm = FakeInteraction(1, base.SimpleNamespace(id=A))  # a DM: no roles
        await Shop.raffle_buy.callback(env.cog, dm, 1)
        assert "in the server" in dm.calls[0]["content"]
    with_env(go, monkeypatch)


def test_raffle_tickets_after_the_draw_time_go_to_next_week(monkeypatch):
    async def go(env):
        await env.fund(A, 5000)
        await env.tickets(A, 10)
        env.t = DRAW
        i = await env.tickets(A, 10)  # a fresh week: the limit resets
        assert "**10** tickets" in i.text
        refs = [r["ref"] for r in await env.raffle_ledger(A)]
        assert refs[0].startswith("raffle:2026-W40:") and refs[1].startswith("raffle:2026-W41:")
    with_env(go, monkeypatch)


def test_raffle_info_shows_prize_and_my_chance(monkeypatch):
    async def go(env):
        for uid, n in ((A, 3), (B, 1)):
            await env.fund(uid, 1000)
            await env.tickets(uid, n)
        text = await env.info(A)
        assert "320 coins" in text and "400-coin pot" in text
        assert "**Entrants:** 2" in text and "**Tickets:** 4" in text and "**Yours:** 3 (75% chance)" in text
        assert f"<t:{DRAW}:F>" in text
        assert "**Yours:** 0 (0% chance)" in await env.info(C)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- raffle: the draw
async def two_entrants(env):
    for uid, n in ((A, 6), (B, 4)):
        await env.fund(uid, 1000)
        await env.tickets(uid, n)


def test_draw_pays_80_percent_posts_once_and_burns_the_rest(monkeypatch):
    async def go(env):
        await two_entrants(env)
        env.t = DRAW + 30  # inside the grace window: not yet
        await env.cog.run_raffles()
        assert env.guild.general.sent == []
        env.t = DRAW + S.DRAW_GRACE
        await env.cog.run_raffles()
        [win] = [r for r in await env.raffle_ledger() if r["delta"] > 0]
        assert win["ref"] == f"{KEY}:win" and win["delta"] == 800
        winner = win["user_id"]
        assert winner in (A, B)
        assert await env.bal(A) + await env.bal(B) == 2000 - 1000 + 800  # 200 burned
        [post] = env.guild.general.sent
        assert f"<@{winner}>" in post.content and "800 coins" in post.embed.description
        assert "200 coins were burned" in post.embed.description
        am = post.allowed_mentions
        assert [u.id for u in am.users] == [winner] and am.roles is False and am.everyone is False
        assert await env.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", (KEY,)) is not None
        env.t += 3600
        await env.cog.run_raffles()
        assert len(env.guild.general.sent) == 1 and len(await env.raffle_ledger()) == 3
    with_env(go, monkeypatch)


def test_ticket_counts_come_from_the_ledger(monkeypatch):
    async def go(env):
        await two_entrants(env)
        async with env.db.transaction() as tx:
            tickets = await env.cog.tickets_tx(tx, KEY)
        assert tickets == [(A, 6), (B, 4)]
    with_env(go, monkeypatch)


def test_draw_post_failure_retries_without_paying_twice(monkeypatch):
    async def go(env):
        await two_entrants(env)
        env.t = DRAW + 300
        env.guild.fail["send"] = 1
        await Shop.raffles.coro(env.cog)  # the loop swallows the error
        assert await env.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", (KEY,)) is None
        [first] = [r for r in await env.raffle_ledger() if r["delta"] > 0]
        env.cog.rng = random.Random(12345)  # a different rng must not pick a new winner
        env.t += 300
        await env.cog.run_raffles()
        wins = [r for r in await env.raffle_ledger() if r["delta"] > 0]
        assert wins == [first]
        [post] = env.guild.general.sent
        assert f"<@{first['user_id']}>" in post.content
    with_env(go, monkeypatch)


def test_draw_single_entrant_is_refunded_quietly(monkeypatch):
    async def go(env):
        await env.fund(A, 1000)
        await env.tickets(A, 5)
        env.t = DRAW + 300
        await env.cog.run_raffles()
        await env.cog.run_raffles()
        assert await env.bal(A) == 1000 and env.guild.general.sent == []
        refunds = [r for r in await env.raffle_ledger() if r["delta"] > 0]
        assert [(r["delta"], r["ref"]) for r in refunds] == [(500, f"{KEY}:refund:{A}")]
    with_env(go, monkeypatch)


def test_draw_catches_up_after_downtime_and_waits_for_guild(monkeypatch):
    async def go(env):
        await two_entrants(env)
        env.t = DRAW + 60
        for uid in (C, D):
            await env.fund(uid, 1000)
            await env.tickets(uid, 1)  # next week's draw
        env.bot.guild = None
        env.t = ts(2026, 10, 19, 9)  # bot was off over both Sundays
        await env.cog.run_raffles()
        assert [r for r in await env.raffle_ledger() if r["delta"] > 0] == []
        env.bot.guild = env.guild
        await env.cog.run_raffles()
        wins = sorted(r["ref"] for r in await env.raffle_ledger() if r["delta"] > 0)
        assert wins == ["raffle:2026-W40:win", "raffle:2026-W41:win"]
        assert len(env.guild.general.sent) == 2
    with_env(go, monkeypatch)


def test_draw_without_general_channel_still_pays_and_finishes(monkeypatch):
    async def go(env):
        await two_entrants(env)
        env.guild.text_channels.remove(env.guild.general)
        env.t = DRAW + 300
        await env.cog.run_raffles()
        assert len([r for r in await env.raffle_ledger() if r["delta"] > 0]) == 1
        assert await env.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", (KEY,)) is not None
    with_env(go, monkeypatch)


def test_raffle_loop_never_raises(monkeypatch):
    async def go(env):
        async def boom():
            raise RuntimeError("x")
        env.cog.run_raffles = boom
        await Shop.raffles.coro(env.cog)
    with_env(go, monkeypatch)
