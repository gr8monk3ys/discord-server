"""Offline tests for cogs.referral, driven the way Tebex drives it: signed webhooks into cogs.tebex's
handler, which hands payments to the referral cog. A real in-memory SQLite database, fake Discord and a
fake Monkey SMP bridge; no network, no RCON."""

import asyncio
import json
from types import SimpleNamespace

import discord

import config
import db as dbmod
from cogs.monkey import RconError
from cogs.referral import NEED_LINK, OFF, Referrals
from cogs.tebex import Tebex
from logic import monkey as R
from logic import referral as L
from logic import tebex as T

GUILD_ID = 999
SECRET = "k" * 40
TEBEX_ENV = {"TEBEX_WEBHOOK_SECRET": SECRET, "TEBEX_WEBHOOK_PORT": "0"}
ON = {"REFERRALS": "on"}
STEVE, KID, ALT = 1, 2, 3


def run(coro):
    return asyncio.run(coro)


def http_error():
    return discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")


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


class FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.dms = []

    async def send(self, content=None, **kwargs):
        self.dms.append(dict(content=content, **kwargs))


class FakeBridge:
    """The MonkeySmp cog's grant_call, with its allow-list, recording what it was asked to send."""

    def __init__(self):
        self.sent = []
        self.fail_at = None

    async def grant_call(self, command):
        if not R.grant_sendable(command):
            raise RconError("that command is not one this bot sends")
        if self.fail_at is not None and len(self.sent) >= self.fail_at:
            raise RconError("no answer (TimeoutError)")
        self.sent.append(command)
        return ""


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)
        self.cogs = {}
        self.users = {}

    def get_guild(self, gid):
        return self.guild if gid == GUILD_ID else None

    def get_cog(self, name):
        return self.cogs.get(name)

    def get_user(self, uid):
        return self.users.setdefault(uid, FakeUser(uid))

    async def fetch_user(self, uid):
        return self.get_user(uid)


class FakeRequest:
    def __init__(self, body):
        self.body = body
        self.headers = {"X-Signature": T.signature(SECRET, body)}

    async def read(self):
        return self.body


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))


def payment(code, buyer="NewKid", tx="tbx-1", products=None):
    products = products if products is not None else [
        {"id": 7, "name": "Server Pass", "variables": [{"identifier": "referral", "option": code}]}]
    return {"transaction_id": tx, "customer": {"username": {"id": "u", "username": buyer}}, "products": products}


class Env:
    def __init__(self, db, guild, bot, tebex, ref, bridge):
        self.db, self.guild, self.bot, self.tebex, self.ref, self.bridge = db, guild, bot, tebex, ref, bridge
        self.n = 0

    async def link(self, uid, name):
        await self.db.execute("INSERT INTO monkey_links (name_key, name, user_id, state, linked_at, updated_at)"
                              " VALUES (?, ?, ?, 'ok', 0, 0)", (name.lower(), name, uid))

    async def referral(self, uid):
        inter = SimpleNamespace(user=SimpleNamespace(id=uid), response=FakeResponse())
        await Referrals.referral.callback(self.ref, inter)
        return inter.response.sent[0]["content"]

    async def code(self, uid, name):
        await self.link(uid, name)
        await self.referral(uid)
        return (await self.db.fetchone("SELECT code FROM referral_codes WHERE user_id = ?", (uid,)))["code"]

    async def webhook(self, kind, subject, event_id=None):
        self.n += 1
        body = json.dumps({"id": event_id or f"wh-{self.n}", "type": kind, "subject": subject}).encode()
        return await self.tebex.handle(FakeRequest(body))

    async def claims(self):
        return [dict(r) for r in await self.db.fetchall("SELECT * FROM referral_claims ORDER BY id")]


def with_env(fn, environ=ON):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        ref = Referrals(bot, environ=environ)
        bridge = FakeBridge()
        bot.cogs.update(Referrals=ref, MonkeySmp=bridge)
        tebex = Tebex(bot, environ=TEBEX_ENV)
        try:
            await fn(Env(db, guild, bot, tebex, ref, bridge))
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- /referral
def test_off_by_default():
    async def go(env):
        assert await env.referral(STEVE) == OFF
        response = await env.webhook(L.PAYMENT_COMPLETED, payment("MKABCDEF"))
        assert response.status == 200 and response.text == "ignored"
        assert await env.db.fetchall("SELECT * FROM tebex_webhooks") == []
    with_env(go, environ={})


def test_referral_needs_a_link_and_keeps_one_code():
    async def go(env):
        assert await env.referral(STEVE) == NEED_LINK
        await env.link(STEVE, "Steve")
        first = await env.referral(STEVE)
        code = (await env.db.fetchone("SELECT code FROM referral_codes WHERE user_id = ?", (STEVE,)))["code"]
        assert L.CODE_RE.fullmatch(code) and code in first and "No discount" in first
        assert code in await env.referral(STEVE)
        assert len(await env.db.fetchall("SELECT * FROM referral_codes")) == 1
    with_env(go)


def test_code_collisions_are_retried():
    async def go(env):
        made = iter(["MKAAAAAA", "MKAAAAAA", "MKBBBBBB"])
        assert await env.ref.code_for(STEVE, "Steve", make=lambda: next(made)) == "MKAAAAAA"
        assert await env.ref.code_for(KID, "Kid", make=lambda: next(made)) == "MKBBBBBB"
    with_env(go)


# ---------------------------------------------------------------- a purchase with a code
def test_staff_mode_posts_the_commands_once_and_dms_the_referrer():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        body = payment(code.lower())
        response = await env.webhook(L.PAYMENT_COMPLETED, body, event_id="wh-a")
        assert response.status == 200
        (post,) = env.guild.mod.sent
        assert "Run over RCON" in post["content"]
        for command in L.grant_commands("Steve", "NewKid"):
            assert command in post["content"]
        assert post["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        assert env.bridge.sent == []  # staff mode never touches RCON
        (dm,) = env.bot.users[STEVE].dms
        assert "NewKid" in dm["content"] and L.SKIN in dm["content"]
        (claim,) = await env.claims()
        assert (claim["referrer_id"], claim["referrer_name"], claim["buyer_name"], claim["transaction_id"]) == (
            STEVE, "Steve", "NewKid", "tbx-1")
        # Tebex retries the same webhook: nothing again
        assert (await env.webhook(L.PAYMENT_COMPLETED, body, event_id="wh-a")).status == 200
        assert len(env.guild.mod.sent) == 1
    with_env(go)


def test_rcon_mode_sends_the_unlock_and_posts_a_short_note():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        await env.webhook(L.PAYMENT_COMPLETED, payment(code))
        assert env.bridge.sent == L.grant_commands("Steve", "NewKid")
        (post,) = env.guild.mod.sent
        assert "Done over RCON" in post["content"] and "```" not in post["content"]
    with_env(go, environ={**ON, "REFERRAL_GRANT": "rcon"})


def test_rcon_failing_part_way_posts_the_rest_for_staff():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        env.bridge.fail_at = 2
        await env.webhook(L.PAYMENT_COMPLETED, payment(code))
        cmds = L.grant_commands("Steve", "NewKid")
        assert env.bridge.sent == cmds[:2]
        (post,) = env.guild.mod.sent
        assert "stopped after 2 of 3" in post["content"] and cmds[2] in post["content"] and cmds[0] not in post["content"]
        assert len(await env.claims()) == 1
    with_env(go, environ={**ON, "REFERRAL_GRANT": "rcon"})


def test_rcon_done_counts_even_if_the_note_cannot_post():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        env.guild.mod.fail = http_error()
        response = await env.webhook(L.PAYMENT_COMPLETED, payment(code))
        assert response.status == 200 and len(env.bridge.sent) == 3 and len(await env.claims()) == 1
    with_env(go, environ={**ON, "REFERRAL_GRANT": "rcon"})


def test_nothing_out_means_500_and_a_clean_retry():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        env.guild.mod.fail = http_error()
        response = await env.webhook(L.PAYMENT_COMPLETED, payment(code), event_id="wh-x")
        assert response.status == 500
        assert await env.claims() == [] and await env.db.fetchall("SELECT * FROM tebex_webhooks") == []
        assert env.bot.users.get(STEVE) is None
        env.guild.mod.fail = None
        response = await env.webhook(L.PAYMENT_COMPLETED, payment(code), event_id="wh-x")
        assert response.status == 200 and len(await env.claims()) == 1 and len(env.guild.mod.sent) == 1
    with_env(go)


# ---------------------------------------------------------------- the guards
def test_a_buyer_is_referred_once_ever_and_a_transaction_counts_once():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        other = await env.code(ALT, "Other_1")
        await env.webhook(L.PAYMENT_COMPLETED, payment(code, tx="tbx-1"))
        await env.webhook(L.PAYMENT_COMPLETED, payment(code, tx="tbx-2"))  # renewal, or a second package
        await env.webhook(L.PAYMENT_COMPLETED, payment(other, buyer="newkid", tx="tbx-3"))  # another code, same buyer
        await env.webhook(L.PAYMENT_COMPLETED, payment(other, buyer="Someone", tx="tbx-1"))  # same transaction
        assert [c["transaction_id"] for c in await env.claims()] == ["tbx-1"]
        assert len(env.guild.mod.sent) == 1  # repeats are quiet
    with_env(go)


def test_self_referral_is_refused_with_a_staff_note():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        await env.link(ALT, "SteveAlt")
        await env.webhook(L.PAYMENT_COMPLETED, payment(code, buyer="STEVE", tx="tbx-1"))
        # one link per member: Steve relinks to his alt, and buys it a pass with his own code
        await env.db.execute("DELETE FROM monkey_links WHERE name_key = 'steve'")
        await env.db.execute("UPDATE monkey_links SET user_id = ? WHERE name_key = 'stevealt'", (STEVE,))
        await env.webhook(L.PAYMENT_COMPLETED, payment(code, buyer="SteveAlt", tx="tbx-2"))
        assert await env.claims() == []
        assert len(env.guild.mod.sent) == 2 and all("own referral code" in p["content"] for p in env.guild.mod.sent)
    with_env(go)


def test_unknown_codes_no_codes_and_other_packages_do_nothing():
    async def go(env):
        await env.code(STEVE, "Steve")
        for subject in (payment("MKZZZZZZ"), payment(""), payment("not a code"),
                        payment((await env.db.fetchone("SELECT code FROM referral_codes"))["code"],
                                products=[{"id": 9, "name": "Crown", "variables": [
                                    {"identifier": "referral",
                                     "option": (await env.db.fetchone("SELECT code FROM referral_codes"))["code"]}]}])):
            response = await env.webhook(L.PAYMENT_COMPLETED, subject)
            assert response.status == 200
        assert await env.claims() == [] and env.guild.mod.sent == []
    with_env(go, environ={**ON, "REFERRAL_PACKAGES": "Server Pass"})


def test_a_referrer_who_unlinked_still_counts_by_the_name_kept_with_the_code():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        await env.db.execute("DELETE FROM monkey_links WHERE user_id = ?", (STEVE,))
        await env.webhook(L.PAYMENT_COMPLETED, payment(code))
        assert (await env.claims())[0]["referrer_name"] == "Steve"
    with_env(go)


# ---------------------------------------------------------------- refunds
def test_a_refund_posts_the_undo_commands_once_and_takes_nothing_back():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        await env.webhook(L.PAYMENT_COMPLETED, payment(code, tx="tbx-1"))
        response = await env.webhook(L.PAYMENT_REFUNDED, {"transaction_id": "tbx-1"})
        assert response.status == 200
        post = env.guild.mod.sent[-1]["content"]
        assert "refunded" in post and "scoreboard players remove Steve rw.invites 1" in post
        assert (await env.claims())[0]["status"] == "refunded"
        await env.webhook(L.CHARGEBACK, {"transaction_id": "tbx-1"})
        await env.webhook(L.PAYMENT_REFUNDED, {"transaction_id": "tbx-unrelated"})
        assert len(env.guild.mod.sent) == 2
    with_env(go, environ={**ON, "REFERRAL_GRANT": "rcon"})


def test_a_refund_note_that_cannot_post_is_retried():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        await env.webhook(L.PAYMENT_COMPLETED, payment(code, tx="tbx-1"))
        env.guild.mod.fail = http_error()
        response = await env.webhook(L.PAYMENT_REFUNDED, {"transaction_id": "tbx-1"}, event_id="wh-r")
        assert response.status == 500 and (await env.claims())[0]["status"] == "granted"
        env.guild.mod.fail = None
        assert (await env.webhook(L.PAYMENT_REFUNDED, {"transaction_id": "tbx-1"}, event_id="wh-r")).status == 200
        assert (await env.claims())[0]["status"] == "refunded"
    with_env(go)


def test_cancellation_alerts_still_work_with_referrals_on():
    async def go(env):
        subject = {"reference": "tbx-r-1", "last_payment": {"customer": {"username": {"username": "Alex"}},
                                                            "products": [{"name": "Monthly Pass"}]}}
        response = await env.webhook(T.CANCELLATION, subject)
        assert response.status == 200 and "Pass ending" in env.guild.mod.sent[0]["content"]
    with_env(go)


def test_leaving_or_a_ban_retires_the_code_and_keeps_the_claims():
    async def go(env):
        code = await env.code(STEVE, "Steve")
        await env.code(ALT, "Other_1")
        await env.webhook(L.PAYMENT_COMPLETED, payment(code, tx="tbx-1"))
        await env.ref.on_member_remove(SimpleNamespace(id=STEVE, guild=env.guild))
        await env.ref.on_member_ban(env.guild, SimpleNamespace(id=ALT))
        assert await env.db.fetchall("SELECT * FROM referral_codes") == []
        assert len(await env.claims()) == 1
        await env.webhook(L.PAYMENT_COMPLETED, payment(code, buyer="Later", tx="tbx-2"))
        assert len(await env.claims()) == 1
    with_env(go)
