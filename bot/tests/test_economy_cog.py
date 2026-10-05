"""Offline tests for cogs.economy.Economy: a real in-memory SQLite database plus small
fakes for the bot, guild, channels, members and interactions. No network. Time is
controlled by patching cogs.economy.now; coin flips use a seeded rng."""

import asyncio
import random
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

import config
import db as dbmod
import economy as E
from cogs import economy as cogmod
from cogs.economy import Economy
from logic import coins as C
from logic.lfg import Roster

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
A, B, C3, D, BOTUSER = 1, 2, 3, 4, 50
MIN = 60


def run(coro):
    return asyncio.run(coro)


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


T0 = ts(2026, 9, 30, 12)  # a Wednesday, local noon


# ---------------------------------------------------------------- fakes
class FakeVoice:
    def __init__(self, cid, name, category=None):
        self.id, self.name, self.category = cid, name, category
        self.voice_states = {}


class FakeText:
    def __init__(self, cid, name, category=None):
        self.id, self.name, self.category = cid, name, category


class FakeThread:
    def __init__(self, cid, parent):
        self.id, self.name, self.parent = cid, "a thread", parent


class FakeMember:
    def __init__(self, uid, guild, bot=False):
        self.id, self.guild, self.bot = uid, guild, bot
        self.display_name = f"user{uid}"
        self.mention = f"<@{uid}>"


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        staff = SimpleNamespace(id=900, name=config.STAFF_CATEGORY)
        community = SimpleNamespace(id=901, name="01 · community")
        voice_cat = SimpleNamespace(id=902, name="05 · voice")
        self.lobby = FakeVoice(100, config.LOBBY_VOICE, voice_cat)
        self.squad = FakeVoice(101, config.SQUAD_VOICE, voice_cat)
        self.afk_channel = FakeVoice(102, "💤 AFK", voice_cat)
        self.staff_voice = FakeVoice(103, "🔒 staff voice", staff)
        self.voice_channels = [self.lobby, self.squad, self.afk_channel, self.staff_voice]
        self.general = FakeText(200, config.GENERAL_CHANNEL, community)
        self.mod = FakeText(202, config.MOD_CHANNEL, staff)
        self.clips = FakeText(300, config.CLIPS_CHANNEL, community)
        self.clip_thread = FakeThread(310, self.clips)
        self.text_channels = [self.mod, self.general, self.clips]
        self.members = []

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db, self.guild = db, guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


class FakeResponse:
    def __init__(self, calls):
        self.calls, self.done = calls, False

    async def send_message(self, content=None, **kwargs):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True
        self.calls.append(dict(content=content, **kwargs))

    def is_done(self):
        return self.done


class FakeInteraction:
    def __init__(self, user):
        self.calls = []
        self.user = user
        self.response = FakeResponse(self.calls)

    @property
    def sent(self):
        assert len(self.calls) == 1
        return self.calls[0]

    @property
    def text(self):
        return self.sent["embed"].description

    @property
    def ephemeral(self):
        return self.sent.get("ephemeral", False)


class Attachment:
    def __init__(self, url, content_type):
        self.url, self.content_type = url, content_type


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0
        self.next_mid = 10_000

    def member(self, uid, bot=False):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, self.guild, bot=bot)
            self.guild.members.append(m)
        return m

    def inter(self, uid):
        return FakeInteraction(self.member(uid))

    def join(self, uid, channel, bot=False):
        self.member(uid, bot=bot)
        channel.voice_states[uid] = SimpleNamespace(channel=channel)

    async def say(self, uid, content="hello", channel=None, attachments=(), kind=discord.MessageType.default,
                  guild="default", bot=False):
        self.next_mid += 1
        msg = SimpleNamespace(id=self.next_mid, type=kind, author=self.member(uid, bot=bot), content=content,
                              attachments=list(attachments), channel=channel or self.guild.general,
                              guild=self.guild if guild == "default" else guild)
        await self.cog.on_message(msg)
        return msg.id

    async def bal(self, uid):
        return await E.balance(self.db, uid)

    async def ledger(self, uid=None, reason=None):
        rows = await self.db.fetchall("SELECT * FROM ledger ORDER BY id")
        return [dict(r) for r in rows if (uid is None or r["user_id"] == uid)
                and (reason is None or r["reason"] == reason)]

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def wallet(self, uid):
        row = await self.db.fetchone("SELECT * FROM wallets WHERE user_id = ?", (uid,))
        return dict(row) if row else None

    async def fund(self, uid, amount):
        await E.apply(self.db, uid, amount, "test", self.t)

    async def daily(self, uid, at=None):
        if at is not None:
            self.t = at
        i = self.inter(uid)
        await Economy.daily.callback(self.cog, i)
        return i

    async def flip(self, uid, bet, side="heads"):
        i = self.inter(uid)
        await Economy.coinflip.callback(self.cog, i, bet, app_commands.Choice(name=side, value=side))
        return i

    async def give(self, uid, target, amount):
        i = self.inter(uid)
        await Economy.give.callback(self.cog, i, target, amount)
        return i


def with_env(fn, monkeypatch, rng=None):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Economy(bot, rng=rng or random.Random(0))
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def side_for(seed: int, win: bool) -> str:
    landed = C.flip(random.Random(seed))
    return landed if win else next(s for s in C.SIDES if s != landed)


# ---------------------------------------------------------------- /daily
def test_daily_pays_and_records_streak(monkeypatch):
    async def go(env):
        i = await env.daily(A, at=T0)
        assert not i.ephemeral and "+100 coins" in i.text and "Day 1" in i.text
        assert await env.bal(A) == 100
        w = await env.wallet(A)
        assert (w["daily_streak"], w["last_daily"]) == (1, "2026-09-30")
        assert [r["ref"] for r in await env.ledger(A)] == [f"daily:2026-09-30:{A}"]
    with_env(go, monkeypatch)


def test_daily_once_per_local_day(monkeypatch):
    async def go(env):
        await env.daily(A, at=ts(2026, 9, 30, 0, 5))
        i = await env.daily(A, at=ts(2026, 9, 30, 23, 55))
        assert i.ephemeral and "already" in i.text
        assert await env.bal(A) == 100
        assert len(await env.ledger(A)) == 1
    with_env(go, monkeypatch)


def test_daily_streak_grows_caps_and_resets(monkeypatch):
    async def go(env):
        paid = []
        for day in range(1, 11):  # Oct 1..10, alternating morning/late night claims
            before = await env.bal(A)
            await env.daily(A, at=ts(2026, 10, day, 23, 59) if day % 2 else ts(2026, 10, day, 0, 1))
            paid.append(await env.bal(A) - before)
        assert paid == [100, 120, 140, 160, 180, 200, 220, 240, 240, 240]
        # Skip Oct 11: the streak resets on Oct 12.
        before = await env.bal(A)
        i = await env.daily(A, at=ts(2026, 10, 12, 9))
        assert await env.bal(A) - before == 100 and "Day 1" in i.text
        assert (await env.wallet(A))["daily_streak"] == 1
    with_env(go, monkeypatch)


def test_daily_streak_across_dst(monkeypatch):
    async def go(env):
        await env.daily(A, at=ts(2026, 10, 31, 23, 30))
        await env.daily(A, at=ts(2026, 11, 1, 23, 30))  # the 25-hour day
        await env.daily(A, at=ts(2026, 11, 2, 0, 10))
        w = await env.wallet(A)
        assert (w["daily_streak"], w["last_daily"]) == (3, "2026-11-02")
        assert await env.bal(A) == 100 + 120 + 140
    with_env(go, monkeypatch)


def test_daily_opted_out_and_works_with_existing_wallet(monkeypatch):
    async def go(env):
        await env.optout(B)
        i = await env.daily(B)
        assert i.ephemeral and "/privacy" in i.text and await env.bal(B) == 0
        await env.fund(A, 50)  # wallet row exists before the first daily
        await env.daily(A)
        assert await env.bal(A) == 150 and (await env.wallet(A))["daily_streak"] == 1
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- voice
def test_voice_pays_humans_in_company(monkeypatch):
    async def go(env):
        g = env.guild
        env.join(A, g.lobby)
        env.join(B, g.lobby)
        env.join(BOTUSER, g.lobby, bot=True)
        env.join(C3, g.squad)  # alone
        assert await env.cog.pay_voice() == 2
        assert [await env.bal(u) for u in (A, B, C3, BOTUSER)] == [2, 2, 0, 0]
        assert {r["ref"] for r in await env.ledger(reason="voice")} == \
            {f"voice:{A}:{T0 // 300}", f"voice:{B}:{T0 // 300}"}
    with_env(go, monkeypatch)


def test_voice_alone_with_bot_afk_staff_pay_nothing(monkeypatch):
    async def go(env):
        g = env.guild
        env.join(A, g.lobby)
        env.join(BOTUSER, g.lobby, bot=True)
        env.join(B, g.afk_channel)
        env.join(C3, g.afk_channel)
        env.join(D, g.staff_voice)
        env.join(5, g.staff_voice)
        assert await env.cog.pay_voice() == 0
        assert await env.ledger() == []
    with_env(go, monkeypatch)


def test_voice_opted_out_counts_as_company_but_doesnt_earn(monkeypatch):
    async def go(env):
        await env.optout(B)
        env.join(A, env.guild.lobby)
        env.join(B, env.guild.lobby)
        assert await env.cog.pay_voice() == 1
        assert (await env.bal(A), await env.bal(B)) == (2, 0)
    with_env(go, monkeypatch)


def test_voice_once_per_tick(monkeypatch):
    async def go(env):
        env.join(A, env.guild.lobby)
        env.join(B, env.guild.lobby)
        env.t = (T0 // 300) * 300 + 10
        assert await env.cog.pay_voice() == 2
        env.t += 200  # same tick (a restart, a late loop): no double pay
        assert await env.cog.pay_voice() == 0
        env.t += 100  # next tick
        assert await env.cog.pay_voice() == 2
        assert await env.bal(A) == 4
    with_env(go, monkeypatch)


def test_voice_loop_never_raises(monkeypatch):
    async def go(env):
        async def boom():
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "pay_voice", boom)
        await env.cog.voice_loop.coro(env.cog)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- messages
def test_message_earns_one_capped_at_50_per_local_day(monkeypatch):
    async def go(env):
        env.t = ts(2026, 9, 30, 23, 0)
        for _ in range(55):
            await env.say(A)
        assert await env.bal(A) == 50
        env.t = ts(2026, 10, 1, 0, 1)  # local midnight resets the cap
        await env.say(A)
        assert await env.bal(A) == 51
    with_env(go, monkeypatch)


def test_message_filters(monkeypatch):
    async def go(env):
        await env.say(BOTUSER, bot=True)
        await env.say(A, channel=env.guild.mod)  # staff
        await env.say(A, kind=discord.MessageType.pins_add)
        await env.say(A, guild=None)  # DM
        await env.say(A, guild=SimpleNamespace(id=123))  # another server
        await env.optout(B)
        await env.say(B)
        assert await env.ledger() == []
        await env.say(A, kind=discord.MessageType.reply)
        assert await env.bal(A) == 1
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- clips
def test_clip_pays_25_three_times_a_day(monkeypatch):
    async def go(env):
        g = env.guild
        mids = []
        for n in range(4):
            mids.append(await env.say(A, f"look https://medal.tv/games/x/clips/{n}", channel=g.clips))
        clip_rows = await env.ledger(A, "clip")
        assert [r["ref"] for r in clip_rows] == [f"clip:{m}" for m in mids[:3]]
        assert await env.bal(A) == 3 * 25 + 4  # plus 1 per message
        env.t += 24 * 3600
        await env.say(A, "", channel=g.clip_thread, attachments=[Attachment("https://cdn/x.mp4", "video/mp4")])
        assert len(await env.ledger(A, "clip")) == 4
    with_env(go, monkeypatch)


def test_clip_needs_a_clip_in_the_clips_channel(monkeypatch):
    async def go(env):
        g = env.guild
        await env.say(A, "https://medal.tv/games/x/clips/1", channel=g.general)  # wrong channel
        await env.say(A, "no link here", channel=g.clips)
        await env.say(A, "https://example.com/x", channel=g.clips)
        await env.say(A, "", channel=g.clips, attachments=[Attachment("https://cdn/x.png", "image/png")])
        assert await env.ledger(A, "clip") == []
        await env.optout(B)
        await env.say(B, "https://medal.tv/games/x/clips/2", channel=g.clips)
        assert await env.ledger(B) == []
    with_env(go, monkeypatch)


def test_clip_ref_is_idempotent(monkeypatch):
    async def go(env):
        msg = SimpleNamespace(id=777, type=discord.MessageType.default, author=env.member(A),
                              content="https://youtu.be/abc", attachments=[], channel=env.guild.clips,
                              guild=env.guild)
        await env.cog.on_message(msg)
        await env.cog.on_message(msg)  # redelivered
        assert [r["ref"] for r in await env.ledger(A, "clip")] == ["clip:777"]
    with_env(go, monkeypatch)


def test_message_listener_never_raises(monkeypatch):
    async def go(env):
        await env.cog.on_message(SimpleNamespace())  # garbage in: logged, not raised
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- events
def test_lfg_squad_full_pays_each_member_once(monkeypatch):
    async def go(env):
        env.member(BOTUSER, bot=True)
        await env.optout(C3)
        roster = Roster(host_id=A, size=4, members=(A, B, C3, BOTUSER))
        await env.cog.on_lfg_squad_full(42, roster)
        await env.cog.on_lfg_squad_full(42, roster)
        assert [await env.bal(u) for u in (A, B, C3, BOTUSER)] == [20, 20, 0, 0]
        assert {r["ref"] for r in await env.ledger()} == {f"lfg:42:{A}", f"lfg:42:{B}"}
        await env.cog.on_lfg_squad_full(43, Roster(host_id=A, size=2, members=(A, B)))
        assert await env.bal(A) == 40
    with_env(go, monkeypatch)


def test_weekly_mvp_and_clip_of_the_week_pay_once(monkeypatch):
    async def go(env):
        await env.cog.on_weekly_mvp("mvp:2026-W40", A)
        await env.cog.on_weekly_mvp("mvp:2026-W40", A)
        await env.cog.on_clip_of_the_week("2026-W40", B)
        await env.cog.on_clip_of_the_week("2026-W40", B)
        assert (await env.bal(A), await env.bal(B)) == (250, 500)
        assert [r["ref"] for r in await env.ledger()] == [f"mvp:2026-W40:{A}", "clipweek:2026-W40"]
        await env.optout(C3)
        await env.cog.on_weekly_mvp("mvp:2026-W41", C3)
        assert await env.bal(C3) == 0
    with_env(go, monkeypatch)


def test_event_listeners_never_raise(monkeypatch):
    async def go(env):
        async def boom(*a):
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "pay_event", boom)
        await env.cog.on_lfg_squad_full(1, Roster(host_id=A, size=2, members=(A, B)))
        await env.cog.on_weekly_mvp("mvp:2026-W40", A)
        await env.cog.on_clip_of_the_week("2026-W40", A)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /give
def test_give_moves_coins_and_pings_nobody(monkeypatch):
    async def go(env):
        await env.fund(A, 100)
        i = await env.give(A, env.member(B), 30)
        assert not i.ephemeral and "30 coins" in i.text
        assert i.sent["allowed_mentions"].users is False and i.sent["allowed_mentions"].roles is False
        assert (await env.bal(A), await env.bal(B)) == (70, 30)
    with_env(go, monkeypatch)


def test_give_validation(monkeypatch):
    async def go(env):
        await env.fund(A, 100)
        for target, amount, needle in ((env.member(A), 10, "yourself"),
                                       (env.member(BOTUSER, bot=True), 10, "Bots"),
                                       (env.member(B), 0, "or more"),
                                       (env.member(B), -5, "or more"),
                                       (env.member(B), 101, "only have 100")):
            i = await env.give(A, target, amount)
            assert i.ephemeral and needle in i.text, needle
        assert (await env.bal(A), await env.bal(B)) == (100, 0)
        assert len(await env.ledger()) == 1
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /coinflip
def test_coinflip_bounds(monkeypatch):
    async def go(env):
        await env.fund(A, 6000)
        await env.fund(B, 50)
        for uid, bet, needle in ((A, 9, "smallest"), (A, 5001, "biggest"), (B, 51, "only have 50"),
                                 (C3, 10, "only have 0")):
            i = await env.flip(uid, bet)
            assert i.ephemeral and needle in i.text, needle
        assert (await env.bal(A), await env.bal(B)) == (6000, 50)
        assert len(await env.ledger(reason="coinflip")) == 0
    with_env(go, monkeypatch)


def test_coinflip_win_pays_double(monkeypatch):
    async def go(env):
        await env.fund(A, 100)
        i = await env.flip(A, 40, side_for(7, win=True))
        assert not i.ephemeral and "won 40" in i.text
        assert await env.bal(A) == 140
        assert [r["delta"] for r in await env.ledger(A, "coinflip")] == [-40, 80]
    with_env(go, monkeypatch, rng=random.Random(7))


def test_coinflip_loss_takes_bet(monkeypatch):
    async def go(env):
        await env.fund(A, 100)
        i = await env.flip(A, 100, side_for(7, win=False))
        assert "lost 100" in i.text
        assert await env.bal(A) == 0
        assert [r["delta"] for r in await env.ledger(A, "coinflip")] == [-100]
    with_env(go, monkeypatch, rng=random.Random(7))


def test_concurrent_flips_cant_spend_the_same_coins(monkeypatch):
    class AlwaysTails:
        def choice(self, seq):
            return "tails"

    async def go(env):
        await env.fund(A, 100)
        flips = await asyncio.gather(*(env.flip(A, 100, "heads") for _ in range(3)))
        assert sorted(i.ephemeral for i in flips) == [False, True, True]
        assert await env.bal(A) == 0
        assert [r["delta"] for r in await env.ledger(A, "coinflip")] == [-100]
    with_env(go, monkeypatch, rng=AlwaysTails())


# ---------------------------------------------------------------- /balance and /richest
def test_balance_shows_balance_streak_and_rank(monkeypatch):
    async def go(env):
        await env.fund(B, 500)
        await env.daily(A)
        i = env.inter(C3)
        await Economy.balance.callback(env.cog, i, env.member(A))
        assert "100 coins" in i.text and "1 day" in i.text and "#2" in i.text
        assert i.sent["embed"].title == "user1" and not i.ephemeral
        env.t += 3 * 24 * 3600  # streak lapsed
        i = env.inter(A)
        await Economy.balance.callback(env.cog, i, None)
        assert "0 days" in i.text
        i = env.inter(C3)
        await Economy.balance.callback(env.cog, i, None)
        assert "0 coins" in i.text and "unranked" in i.text
        i = env.inter(A)
        await Economy.balance.callback(env.cog, i, env.member(BOTUSER, bot=True))
        assert i.ephemeral
    with_env(go, monkeypatch)


def test_richest_top_ten_in_order_no_pings(monkeypatch):
    async def go(env):
        for uid in range(1, 13):
            await env.fund(uid, uid * 10)
        await env.fund(20, 120)  # ties with user 12
        i = env.inter(1)
        await Economy.richest.callback(env.cog, i)
        lines = i.text.split("\n")
        assert lines[0] == "`01`  <@12>  120 coins" and lines[1] == "`01`  <@20>  120 coins"
        assert lines[2] == "`03`  <@11>  110 coins"
        assert lines[9] == "`10`  <@4>  40 coins"
        assert lines[-1] == "`13`  <@1>  10 coins"  # me, outside the top 10
        assert i.sent["allowed_mentions"].users is False
        assert not i.ephemeral
    with_env(go, monkeypatch)


def test_richest_empty(monkeypatch):
    async def go(env):
        i = env.inter(A)
        await Economy.richest.callback(env.cog, i)
        assert "Nobody" in i.text
    with_env(go, monkeypatch)


def test_voice_coins_are_capped_per_day(monkeypatch):
    """Security: two accounts idling in voice 24/7 can't mint coins without limit."""
    async def go(env):
        env.join(A, env.guild.lobby)
        env.join(B, env.guild.lobby)
        for i in range(200):  # 200 ticks = 1000 minutes
            env.t = T0 + i * C.VOICE_TICK_SECONDS
            await env.cog.pay_voice()
        assert await env.bal(A) <= C.VOICE_DAILY_CAP * 2  # at most one cap per local day touched
    with_env(go, monkeypatch)


def test_deafened_members_dont_earn_or_count_as_company(monkeypatch):
    async def go(env):
        env.join(A, env.guild.lobby)
        env.join(B, env.guild.lobby)
        env.guild.lobby.voice_states[B].self_deaf = True
        assert await env.cog.pay_voice() == 0
        assert (await env.bal(A), await env.bal(B)) == (0, 0)
    with_env(go, monkeypatch)


def test_lfg_coins_are_capped_per_day(monkeypatch):
    """Security: making 2-person squads with an alt over and over pays at most the cap."""
    async def go(env):
        for post in range(10):
            await env.cog.on_lfg_squad_full(post, SimpleNamespace(members=(A, B)))
        assert await env.bal(A) == C.LFG_DAILY_CAP
    with_env(go, monkeypatch)


def test_lfg_cap_holds_under_concurrent_squads(monkeypatch):
    async def go(env):
        await asyncio.gather(*(env.cog.on_lfg_squad_full(p, SimpleNamespace(members=(A, B))) for p in range(10)))
        assert await env.bal(A) == C.LFG_DAILY_CAP and await env.bal(B) == C.LFG_DAILY_CAP
    with_env(go, monkeypatch)
