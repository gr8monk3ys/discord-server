"""Offline tests for cogs.tebex: the request handler with a fake request, plus one real round trip
through the HTTP server on a free loopback port. A real in-memory SQLite database; no Discord."""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import discord

import config
import db as dbmod
from cogs.tebex import Tebex
from logic import tebex as L

GUILD_ID = 999
SECRET = "k" * 40
ENV = {"TEBEX_WEBHOOK_SECRET": SECRET, "TEBEX_WEBHOOK_PORT": "0"}


def run(coro):
    return asyncio.run(coro)


class FakeText:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.mod = FakeText(config.MOD_CHANNEL)
        self.text_channels = [FakeText(config.GENERAL_CHANNEL), self.mod]


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)

    def get_guild(self, gid):
        return self.guild if gid == GUILD_ID else None


class FakeRequest:
    def __init__(self, body: bytes, signature: str | None):
        self.body = body
        self.headers = {} if signature is None else {"X-Signature": signature}

    async def read(self):
        return self.body


def body_of(kind=L.CANCELLATION, event_id="wh-1", subject=None):
    subject = subject if subject is not None else {
        "reference": "tbx-r-1", "next_payment_at": "2026-12-07T17:00:00+00:00",
        "last_payment": {"customer": {"username": {"username": "Alex_*"}}, "products": [{"name": "Monthly Pass"}]}}
    return json.dumps({"id": event_id, "type": kind, "date": "2026-11-07T17:00:00+00:00",
                       "subject": subject}).encode()


def signed(body: bytes, secret=SECRET) -> FakeRequest:
    return FakeRequest(body, L.signature(secret, body))


def with_cog(fn, environ=ENV):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        cog = Tebex(FakeBot(db, guild), environ=environ)
        try:
            await fn(cog, guild, db)
        finally:
            await cog.cog_unload()
            await db.close()
    run(go())


def test_off_without_the_secret_and_no_server_starts():
    async def go(cog, guild, db):
        await cog.cog_load()
        assert cog.settings is None and cog.runner is None
    with_cog(go, environ={})


def test_unsigned_or_wrongly_signed_requests_are_refused():
    async def go(cog, guild, db):
        for request in (FakeRequest(body_of(), None), FakeRequest(body_of(), "00" * 32),
                        signed(body_of(), secret="x" * 40)):
            response = await cog.handle(request)
            assert response.status == 401
        assert guild.mod.sent == []
    with_cog(go)


def test_validation_webhook_is_answered_with_its_id():
    async def go(cog, guild, db):
        response = await cog.handle(signed(body_of(L.VALIDATION, "val-9", {})))
        assert response.status == 200 and json.loads(response.body) == {"id": "val-9"}
        response = await cog.handle(signed(b"not json"))
        assert response.status == 400
        response = await cog.handle(signed(body_of("payment.completed")))
        assert response.status == 200 and guild.mod.sent == []
    with_cog(go)


def test_cancellation_posts_once_in_mod_with_no_pings():
    async def go(cog, guild, db):
        response = await cog.handle(signed(body_of()))
        assert response.status == 200
        (sent,) = guild.mod.sent
        assert "**Alex\\_\\***" in sent["content"] and "Monthly Pass" in sent["content"]
        assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        response = await cog.handle(signed(body_of()))  # Tebex retried
        assert response.status == 200 and len(guild.mod.sent) == 1
        await cog.handle(signed(body_of(event_id="wh-2")))
        assert len(guild.mod.sent) == 2
    with_cog(go)


def test_a_failed_post_answers_500_so_tebex_retries():
    async def go(cog, guild, db):
        guild.mod.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
        response = await cog.handle(signed(body_of()))
        assert response.status == 500
        assert await db.fetchall("SELECT * FROM tebex_webhooks") == []
        guild.mod.fail = None
        response = await cog.handle(signed(body_of()))
        assert response.status == 200 and len(guild.mod.sent) == 1
        guild.text_channels.remove(guild.mod)
        response = await cog.handle(signed(body_of(event_id="wh-3")))
        assert response.status == 500
    with_cog(go)


def test_real_server_round_trip_on_loopback():
    async def go(cog, guild, db):
        await cog.cog_load()
        assert cog.runner is not None and cog.bound_port
        url = f"http://127.0.0.1:{cog.bound_port}{L.PATH}"
        body = body_of()
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=body) as r:
                assert r.status == 401
            async with session.get(url) as r:
                assert r.status == 405
            async with session.post(f"http://127.0.0.1:{cog.bound_port}/", data=body) as r:
                assert r.status == 404
            async with session.post(url, data=b"x" * (L.MAX_BODY + 10),
                                    headers={"X-Signature": "0"}) as r:
                assert r.status == 413
            async with session.post(url, data=body, headers={"X-Signature": L.signature(SECRET, body)}) as r:
                assert r.status == 200
        assert len(guild.mod.sent) == 1
        await cog.cog_unload()
        assert cog.runner is None
    with_cog(go)
