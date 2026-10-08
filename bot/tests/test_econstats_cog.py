"""Offline tests for cogs.econstats: a real in-memory SQLite database and small fakes."""

import asyncio
from types import SimpleNamespace

import config
import db as dbmod
import economy as E
from cogs import econstats as cogmod
from cogs.econstats import EconStats
from logic import econstats as ES

T = 1_800_000_000
DAY = 24 * 60 * 60


class Role:
    def __init__(self, name):
        self.name = name


class Guild:
    owner_id = 999


class User:
    def __init__(self, uid, roles=(), admin=False, guild=Guild()):
        self.id = uid
        self.roles = [Role(n) for n in roles]
        self.guild = guild
        self.guild_permissions = SimpleNamespace(administrator=admin)


class Response:
    def __init__(self, calls):
        self.calls = calls

    async def send_message(self, content=None, **kwargs):
        self.calls.append(dict(content=content, **kwargs))


class Interaction:
    def __init__(self, user):
        self.user = user
        self.calls = []
        self.response = Response(self.calls)


def with_cog(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        cog = EconStats(SimpleNamespace(db=db))
        monkeypatch.setattr(cogmod, "now", lambda: T)
        try:
            await fn(db, cog)
        finally:
            await db.close()
    asyncio.run(go())


async def call(cog, user):
    i = Interaction(user)
    await EconStats.economy.callback(cog, i)
    return i.calls[0]


def fields(embed):
    return {f.name: f.value for f in embed.fields}


def test_staff_only(monkeypatch):
    async def go(db, cog):
        c = await call(cog, User(1))
        assert "Only staff" in c["content"] and c["ephemeral"]
        c = await call(cog, User(1, roles=[config.MOD_ROLE]))
        assert c["embed"] is not None and c["ephemeral"]
        assert (await call(cog, User(2, admin=True)))["embed"] is not None
        assert (await call(cog, User(999)))["embed"] is not None  # owner
        assert "Only staff" in (await call(cog, SimpleNamespace(id=5, roles=[])))["content"]  # a DM
    with_cog(go, monkeypatch)


def test_report_numbers_from_wallets_and_last_7_days(monkeypatch):
    async def go(db, cog):
        await E.apply(db, 1, 5000, "daily", T - 30 * DAY)  # old: supply only
        await E.apply(db, 1, 240, "daily", T - DAY)
        await E.apply(db, 2, 1000, "challenge", T - 2 * DAY)
        await E.transfer(db, 2, 3, 300, "give", T - DAY)  # moves coins, mints none
        await E.apply(db, 1, -2000, "shop", T - DAY)
        await E.apply(db, 3, -100, "raffle", T - DAY)
        await E.apply(db, 3, 80, "raffle", T - 3600)
        await E.apply(db, 2, 999, "daily", T)  # "now" is outside [T - 7d, T)
        c = await call(cog, User(1, roles=[config.KEEPER_ROLE]))
        f = fields(c["embed"])
        assert f["In circulation"] == "5,219 coins\n3 wallets"  # 3240 + 1699 + 280
        assert f["Minted vs burned"] == "+1,240 / -2,020\nnet -780"
        assert f["Top sources"] == "Weekly challenges: +1,000\nDaily: +240"
        assert f["Top sinks"] == "Shop: -2,000\nRaffle: -20"
        assert "Gifts" not in f["Top sources"] + f["Top sinks"]
        assert "keeping up" in c["embed"].description
        assert "median 1,699" in f["Typical wallet"] and "100.0%" in f["Typical wallet"]
    with_cog(go, monkeypatch)


def test_empty_economy(monkeypatch):
    async def go(db, cog):
        c = await call(cog, User(1, admin=True))
        f = fields(c["embed"])
        assert f["In circulation"] == "0 coins\n0 wallets" and f["Top sources"] == "none"
        assert "No coins" in c["embed"].description
    with_cog(go, monkeypatch)


def test_db_error_is_reported_not_raised(monkeypatch):
    async def go(db, cog):
        async def boom(t):
            raise RuntimeError("x")
        cog.report = boom
        c = await call(cog, User(1, admin=True))
        assert "Couldn't" in c["content"]
    with_cog(go, monkeypatch)


def test_window_is_seven_days():
    assert ES.WINDOW == 7 * DAY
