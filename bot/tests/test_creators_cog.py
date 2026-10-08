"""Offline tests for cogs.creators: in-memory SQLite, fake Discord objects and a fake
HTTP layer. No network."""

import asyncio
import html
import json
import re
from types import SimpleNamespace

import discord
from discord import app_commands

import config
import db as dbmod
from cogs import creators as cogmod
from cogs.creators import MESSAGES, Creators
from logic import creators as C

GUILD_ID = 999
OWNER, U1, U2, MOD = 1, 11, 12, 20
T0 = 1_790_000_000
UC = "UC" + "a1B2c3D4e5F6g7H8i9J0k_"
UC2 = "UC" + "z" * 22
ENV = {"TWITCH_CLIENT_ID": "cid-test", "TWITCH_CLIENT_SECRET": "secret-test"}


def run(coro):
    return asyncio.run(coro)


def choice(value):
    return app_commands.Choice(name=cogmod.NAMES[value], value=value)


def feed(*entries, title="Cool *Kid*", descriptions=None):
    descriptions = descriptions or {}
    body = "".join(f"<entry><yt:videoId>{vid}</yt:videoId><title>{t}</title>"
                   f"<link rel=\"alternate\" href=\"https://evil.example/{vid}\"/>"
                   f"<media:group><media:description>{html.escape(descriptions.get(vid, ''))}</media:description>"
                   f"</media:group></entry>" for vid, t in entries)
    return (f'<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            f'xmlns:media="http://search.yahoo.com/mrss/" '
            f'xmlns="http://www.w3.org/2005/Atom"><title>{title}</title>{body}</feed>').encode()


def channel_page(channel_id, description=""):
    data = json.dumps({"metadata": {"channelMetadataRenderer": {"title": "Cool Kid", "description": description}}})
    return (f'<html><head><link rel="canonical" href="https://www.youtube.com/channel/{channel_id}">'
            f'<meta property="og:description" content="{html.escape(description)}"></head>'
            f'<body><script>var ytInitialData = {data};</script></body></html>').encode()


class FakeHttp:
    """url -> (status, body) or an exception; records every call."""

    def __init__(self):
        self.routes = {}
        self.calls = []
        self.posts = []
        self.token_reply = (200, json.dumps({"access_token": "tok1", "expires_in": 3600}).encode())

    def route(self, url, status=200, body=b"", cut=False):
        self.routes[url] = (status, body, cut)

    async def get(self, url, *, params=None, headers=None, max_bytes=C.MAX_FEED_BYTES):
        self.calls.append(dict(url=url, params=params, headers=headers, max_bytes=max_bytes))
        key = url if params is None else (url, tuple(params))
        hit = self.routes.get(key, self.routes.get(url))
        if hit is None:
            raise AssertionError(f"unexpected GET {url} {params}")
        if isinstance(hit, Exception):
            raise hit
        return hit

    async def post(self, url, *, data=None):
        self.posts.append(dict(url=url, data=data))
        if isinstance(self.token_reply, Exception):
            raise self.token_reply
        return self.token_reply


class FakeText:
    def __init__(self, name):
        self.id = 300
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class Role:
    def __init__(self, name):
        self.name = name


class FakeMember:
    def __init__(self, uid, guild, roles=()):
        self.id = uid
        self.guild = guild
        self.bot = False
        self.mention = f"<@{uid}>"
        self.roles = [Role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=False)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.creators = FakeText(config.CREATORS_CHANNEL)
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.text_channels = [FakeText(config.GENERAL_CHANNEL), self.creators, self.mod_log]
        self.members = {}

    def get_member(self, uid):
        return self.members.get(uid)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)

    def get_guild(self, gid):
        return self.guild if gid == GUILD_ID else None


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
    def __init__(self, user, guild):
        self.calls = []
        self.user = user
        self.guild = guild
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def reply(self):
        """The text the member saw (the last message or followup)."""
        return [kw for k, kw in self.calls if k in ("send_message", "followup")][-1]


def code_of(reply) -> str:
    return re.search(r"FD-[A-Z0-9]{6}", reply["content"]).group(0)


class Env:
    def __init__(self, db, guild, http, cog):
        self.db, self.guild, self.http, self.cog = db, guild, http, cog
        self.t = T0

    def member(self, uid, roles=()):
        if uid not in self.guild.members:
            self.guild.members[uid] = FakeMember(uid, self.guild, roles)
        return self.guild.members[uid]

    async def link(self, uid, platform, text):
        inter = FakeInteraction(self.member(uid), self.guild)
        await Creators.link.callback(self.cog, inter, choice(platform), text)
        return inter.reply()

    async def verify(self, uid, platform):
        inter = FakeInteraction(self.member(uid), self.guild)
        await Creators.verify.callback(self.cog, inter, choice(platform))
        return inter.reply()

    async def approve(self, mod_uid, uid, platform, roles=(config.MOD_ROLE,)):
        inter = FakeInteraction(self.member(mod_uid, roles=roles), self.guild)
        await Creators.approve.callback(self.cog, inter, self.member(uid), choice(platform))
        return inter.reply()

    async def link_verified(self, uid, channel_id, *, videos=()):
        """Link a YouTube channel and prove it with the code in the channel description.
        `videos` are what the feed shows at link time (marked seen on verify)."""
        self.http.route(C.feed_url(channel_id), body=feed(*videos))
        reply = await self.link(uid, "youtube", channel_id)
        self.http.route(C.channel_url(channel_id), body=channel_page(channel_id, f"hi {code_of(reply)}"))
        self.t += C.VERIFY_COOLDOWN  # separate commands, never rate limited against each other
        reply = await self.verify(uid, "youtube")
        assert reply["content"].startswith("Verified"), reply["content"]
        return reply

    async def link_twitch_verified(self, uid, live=()):
        self.http.route(HELIX_USERS, body=users_reply())
        self.http.route(HELIX_STREAMS, body=streams_reply(*live))
        reply = await self.link(uid, "twitch", "coolkid")
        self.http.route(HELIX_USERS, body=users_reply(bio=f"streams {code_of(reply)}"))
        self.t += C.VERIFY_COOLDOWN
        reply = await self.verify(uid, "twitch")
        assert reply["content"].startswith("Verified"), reply["content"]

    async def links(self, uid=None):
        sql = "SELECT * FROM creators" + (" WHERE user_id = ?" if uid else "") + " ORDER BY user_id, platform"
        return [dict(r) for r in await self.db.fetchall(sql, (uid,) if uid else ())]

    async def pending(self, uid=None):
        sql = "SELECT * FROM creator_pending" + (" WHERE user_id = ?" if uid else "") + " ORDER BY user_id, platform"
        return [dict(r) for r in await self.db.fetchall(sql, (uid,) if uid else ())]

    async def seen(self, platform):
        rows = await self.db.fetchall("SELECT item_id FROM creator_seen WHERE platform = ?", (platform,))
        return {r["item_id"] for r in rows}


def with_env(fn, monkeypatch, environ=None):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        http = FakeHttp()
        cog = Creators(FakeBot(db, guild), http=http, environ=environ if environ is not None else {})
        env = Env(db, guild, http, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- YouTube linking
def test_link_youtube_creates_a_claim_not_a_link(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed(("aaaaaaaaaaa", "old one")))
        reply = await env.link(U1, "youtube", f"https://www.youtube.com/channel/{UC}")
        assert reply["ephemeral"] is True and reply["allowed_mentions"].users is False
        assert "Cool \\*Kid\\*" in reply["content"] and "/creator verify" in reply["content"]
        code = code_of(reply)
        assert C.CODE.fullmatch(code)
        assert await env.links() == []  # nothing is announced before verification
        (p,) = await env.pending(U1)
        assert (p["platform"], p["external_id"], p["handle"], p["code"], p["created_at"]) == \
            ("youtube", UC, UC, code, T0)
        assert await env.seen("youtube") == set()
    with_env(go, monkeypatch)


def test_verify_seeds_existing_uploads_and_enables_announcements(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.link_verified(U1, UC, videos=[("aaaaaaaaaaa", "old one"), ("bbbbbbbbbbb", "older")])
        (row,) = await env.links(U1)
        assert (row["platform"], row["external_id"], row["handle"], row["verified"]) == ("youtube", UC, UC, 1)
        assert await env.pending() == []
        assert await env.seen("youtube") == {"aaaaaaaaaaa", "bbbbbbbbbbb"}
        page_call = [c for c in env.http.calls if c["url"] == C.channel_url(UC)][0]
        assert page_call["max_bytes"] == C.MAX_PAGE_BYTES
        assert await env.cog.poll_youtube() == 0  # nothing new yet
    with_env(go, monkeypatch)


def test_link_youtube_handle_resolves_channel_page(monkeypatch):
    async def go(env):
        page = f'<html><link rel="canonical" href="https://www.youtube.com/channel/{UC}"> "channelId":"{UC2}"'
        env.http.route("https://www.youtube.com/@coolkid", body=page.encode())
        env.http.route(C.feed_url(UC), body=feed())
        reply = await env.link(U1, "youtube", "https://youtube.com/@coolkid/videos")
        assert "FD-" in reply["content"]
        (p,) = await env.pending(U1)
        assert p["external_id"] == UC and p["handle"] == "@coolkid"
        assert env.http.calls[0]["max_bytes"] == C.MAX_PAGE_BYTES
        assert env.http.calls[1]["max_bytes"] == C.MAX_FEED_BYTES
    with_env(go, monkeypatch)


def test_link_youtube_problems(monkeypatch):
    async def go(env):
        assert (await env.link(U1, "youtube", "https://evil.example/@x"))["content"] == MESSAGES["youtube_input"]
        env.http.route("https://www.youtube.com/@ghost", status=404)
        assert (await env.link(U1, "youtube", "@ghost"))["content"] == MESSAGES["not_found"]
        env.http.route("https://www.youtube.com/@noid", body=b"<html>nothing</html>")
        assert (await env.link(U1, "youtube", "@noid"))["content"] == MESSAGES["not_found"]
        env.http.routes["https://www.youtube.com/@slow"] = asyncio.TimeoutError()
        assert "couldn't reach YouTube" in (await env.link(U1, "youtube", "@slow"))["content"]
        env.http.route(C.feed_url(UC), body=b"x" * 10, cut=True)
        assert "couldn't reach" in (await env.link(U1, "youtube", UC))["content"]
        env.http.route(C.feed_url(UC), body=b"<!DOCTYPE x><feed/>")
        assert "couldn't reach" in (await env.link(U1, "youtube", UC))["content"]
        assert await env.links() == [] and await env.pending() == []
    with_env(go, monkeypatch)


def test_channel_verified_by_someone_else_is_taken(monkeypatch):
    async def go(env):
        await env.link_verified(U1, UC)
        assert (await env.link(U2, "youtube", UC))["content"] == MESSAGES["taken"]
        assert await env.pending(U2) == []
        assert (await env.link(U1, "youtube", UC))["content"] == MESSAGES["already"].format(platform="YouTube")
        assert len(await env.links()) == 1
    with_env(go, monkeypatch)


def test_impersonator_cannot_claim_a_channel_they_dont_control(monkeypatch):
    """The reported attack: link a famous channel nobody here has linked."""
    async def go(env):
        env.member(U2)
        env.http.route(C.feed_url(UC), body=feed())
        code = code_of(await env.link(U2, "youtube", UC))
        env.http.route(C.channel_url(UC), body=channel_page(UC, "the real owner's bio"))
        reply = await env.verify(U2, "youtube")
        assert reply["content"] == MESSAGES["no_code"].format(code=code, where=cogmod.WHERE["youtube"],
                                                              platform="YouTube")
        assert await env.links() == [] and len(await env.pending(U2)) == 1
        env.http.route(C.feed_url(UC), body=feed(("ggggggggggg", "new")))
        assert await env.cog.poll_youtube() == 0 and env.guild.creators.sent == []
    with_env(go, monkeypatch)


def test_unverified_claim_does_not_block_the_real_owner(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO creators (user_id, platform, handle, external_id, added_at) "
                             "VALUES (?, 'youtube', 'h', ?, 1)", (U2, UC))  # someone's old, unverified claim
        env.http.route(C.feed_url(UC), body=feed())
        code_of(await env.link(U2, "youtube", UC))  # the claimant tries too
        await env.link_verified(U1, UC)
        assert [(r["user_id"], r["verified"]) for r in await env.links()] == [(U1, 1)]
        env.t += C.VERIFY_COOLDOWN
        assert (await env.verify(U2, "youtube"))["content"] in (
            MESSAGES["taken"], MESSAGES["no_code"].format(code=(await env.pending(U2))[0]["code"],
                                                          where=cogmod.WHERE["youtube"], platform="YouTube"))
        assert [(r["user_id"], r["verified"]) for r in await env.links()] == [(U1, 1)]
    with_env(go, monkeypatch)


def test_relinking_the_same_channel_keeps_the_code_and_another_replaces_it(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed())
        env.http.route(C.feed_url(UC2), body=feed())
        first = code_of(await env.link(U1, "youtube", UC))
        env.t += 3600
        assert code_of(await env.link(U1, "youtube", UC)) == first
        (p,) = await env.pending(U1)
        assert p["created_at"] == T0  # the 24 hours aren't extended
        code_of(await env.link(U1, "youtube", UC2))
        (p,) = await env.pending(U1)
        assert p["external_id"] == UC2 and p["created_at"] == T0 + 3600
    with_env(go, monkeypatch)


def test_verifying_a_new_channel_replaces_the_old_link(monkeypatch):
    async def go(env):
        await env.link_verified(U1, UC)
        await env.link_verified(U1, UC2)
        (row,) = await env.links(U1)
        assert row["external_id"] == UC2 and row["verified"] == 1
    with_env(go, monkeypatch)


def test_limit_of_two_platforms(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO creators (user_id, platform, handle, external_id, added_at) "
                             "VALUES (?, 'a', 'x', 'x', 1), (?, 'b', 'y', 'y', 1)", (U1, U1))
        assert (await env.link(U1, "youtube", UC))["content"] == MESSAGES["limit"]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /creator verify
def test_verify_wrong_code_keeps_the_claim(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed())
        code = code_of(await env.link(U1, "youtube", UC))
        wrong = "FD-" + ("2" if code[3] != "2" else "3") + code[4:]
        env.http.route(C.channel_url(UC), body=channel_page(UC, f"my code is {wrong}"))
        reply = await env.verify(U1, "youtube")
        assert code in reply["content"] and "couldn't find" in reply["content"]
        assert reply["allowed_mentions"].users is False
        assert await env.links() == [] and len(await env.pending(U1)) == 1
        env.t += C.VERIFY_COOLDOWN
        env.http.route(C.channel_url(UC), body=channel_page(UC, f"my code is {code.lower()}"))
        assert (await env.verify(U1, "youtube"))["content"].startswith("Verified")
    with_env(go, monkeypatch)


def test_verify_expired_code(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed())
        code = code_of(await env.link(U1, "youtube", UC))
        env.http.route(C.channel_url(UC), body=channel_page(UC, code))
        env.t += C.CODE_TTL
        env.http.calls.clear()
        assert (await env.verify(U1, "youtube"))["content"] == MESSAGES["expired"]
        assert env.http.calls == []  # not even checked
        assert await env.pending() == [] and await env.links() == []
        assert code_of(await env.link(U1, "youtube", UC)) != code  # a fresh one
    with_env(go, monkeypatch)


def test_verify_is_rate_limited_per_member(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed())
        env.http.route(C.feed_url(UC2), body=feed())
        env.http.route(C.channel_url(UC), body=channel_page(UC, "nothing"))
        env.http.route(C.channel_url(UC2), body=channel_page(UC2, "nothing"))
        await env.link(U1, "youtube", UC)
        await env.link(U2, "youtube", UC2)
        assert "couldn't find" in (await env.verify(U1, "youtube"))["content"]
        env.t += 20
        env.http.calls.clear()
        assert (await env.verify(U1, "youtube"))["content"] == MESSAGES["cooldown"].format(
            seconds=C.VERIFY_COOLDOWN - 20)
        assert env.http.calls == []
        assert "couldn't find" in (await env.verify(U2, "youtube"))["content"]  # others aren't affected
        env.t += C.VERIFY_COOLDOWN - 20
        assert "couldn't find" in (await env.verify(U1, "youtube"))["content"]
    with_env(go, monkeypatch)


def test_verify_without_a_claim(monkeypatch):
    async def go(env):
        assert (await env.verify(U1, "youtube"))["content"] == MESSAGES["no_pending"].format(platform="YouTube")
        await env.db.execute("INSERT INTO creators (user_id, platform, handle, external_id, added_at) "
                             "VALUES (?, 'youtube', 'h', ?, 1)", (U1, UC))
        assert (await env.verify(U1, "youtube"))["content"] == MESSAGES["legacy"].format(platform="YouTube")
        await env.db.execute("UPDATE creators SET verified = 1")
        assert (await env.verify(U1, "youtube"))["content"] == MESSAGES["already"].format(platform="YouTube")
    with_env(go, monkeypatch)


def test_verify_from_a_video_description_in_the_feed(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed(("hhhhhhhhhhh", "v")))
        code = code_of(await env.link(U1, "youtube", UC))
        env.http.route(C.channel_url(UC), body=channel_page(UC, ""))
        env.http.route(C.feed_url(UC), body=feed(("hhhhhhhhhhh", "v"), descriptions={"hhhhhhhhhhh": f"code {code}"}))
        assert (await env.verify(U1, "youtube"))["content"].startswith("Verified")
    with_env(go, monkeypatch)


def test_verify_ignores_a_page_for_another_channel(monkeypatch):
    """If the channel URL ends up on some other channel's page, its description doesn't count."""
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed())
        code = code_of(await env.link(U1, "youtube", UC))
        env.http.route(C.channel_url(UC), body=channel_page(UC2, code))
        assert "couldn't find" in (await env.verify(U1, "youtube"))["content"]
    with_env(go, monkeypatch)


def test_verify_outage_keeps_the_claim(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed())
        await env.link(U1, "youtube", UC)
        env.http.routes[C.channel_url(UC)] = asyncio.TimeoutError()
        assert "couldn't reach YouTube" in (await env.verify(U1, "youtube"))["content"]
        env.t += C.VERIFY_COOLDOWN
        env.http.route(C.channel_url(UC), status=404)
        assert (await env.verify(U1, "youtube"))["content"] == MESSAGES["not_found"]
        assert len(await env.pending(U1)) == 1
    with_env(go, monkeypatch)


def test_verify_never_raises(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed())
        await env.link(U1, "youtube", UC)

        async def boom(*a, **k):
            raise RuntimeError("bug")
        monkeypatch.setattr(env.cog, "youtube_proof", boom)
        assert "couldn't reach" in (await env.verify(U1, "youtube"))["content"]
    with_env(go, monkeypatch)


def test_cleanup_drops_day_old_claims(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed())
        env.http.route(C.feed_url(UC2), body=feed())
        await env.link(U1, "youtube", UC)
        env.t += 3600
        await env.link(U2, "youtube", UC2)
        env.cog.last_verify = {U1: env.t - 5, U2: env.t - 3600}
        env.t = T0 + C.CODE_TTL
        await env.cog.cleanup_loop.coro(env.cog)
        assert [p["user_id"] for p in await env.pending()] == [U2]
        assert env.cog.last_verify == {}
        async def broken():
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "clean_pending", broken)
        await env.cog.cleanup_loop.coro(env.cog)  # logs and carries on
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /creator approve
def test_approve_is_staff_only_and_logged(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed(("iiiiiiiiiii", "old")))
        await env.link(U1, "youtube", UC)
        reply = await env.approve(U2, U1, "youtube", roles=())
        assert "Only mods" in reply["content"] and await env.links() == []
        reply = await env.approve(MOD, U1, "youtube")
        assert "Approved <@11>" in reply["content"] and reply["allowed_mentions"].users is False
        (row,) = await env.links(U1)
        assert row["verified"] == 1 and await env.pending() == []
        assert await env.seen("youtube") == {"iiiiiiiiiii"}
        (logged,) = env.guild.mod_log.sent
        desc = logged["embed"].description
        assert "<@20>" in desc and "<@11>" in desc and C.channel_url(UC) in desc
        assert logged["allowed_mentions"].users is False
    with_env(go, monkeypatch)


def test_approve_a_link_from_before_verification(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.db.execute("INSERT INTO creators (user_id, platform, handle, external_id, added_at) "
                             "VALUES (?, 'youtube', 'h', ?, 1)", (U1, UC))
        env.http.route(C.feed_url(UC), body=feed(("jjjjjjjjjjj", "posted while unverified")))
        assert await env.cog.poll_youtube() == 0  # legacy rows are quiet
        await env.approve(MOD, U1, "youtube")
        (row,) = await env.links(U1)
        assert row["verified"] == 1
        assert await env.cog.poll_youtube() == 0  # the backlog was marked seen, not announced
        assert (await env.approve(MOD, U1, "youtube"))["content"].endswith("already verified.")
    with_env(go, monkeypatch)


def test_approve_nothing_waiting_or_outage(monkeypatch):
    async def go(env):
        assert "no YouTube link waiting" in (await env.approve(MOD, U1, "youtube"))["content"]
        env.http.route(C.feed_url(UC), body=feed())
        await env.link(U1, "youtube", UC)
        env.http.routes[C.feed_url(UC)] = asyncio.TimeoutError()
        assert "couldn't reach" in (await env.approve(MOD, U1, "youtube"))["content"]
        assert await env.links() == [] and env.guild.mod_log.sent == []
    with_env(go, monkeypatch)


def test_approve_refuses_a_channel_verified_by_someone_else(monkeypatch):
    async def go(env):
        env.http.route(C.feed_url(UC), body=feed())
        await env.link(U2, "youtube", UC)
        await env.link_verified(U1, UC)
        assert "already verified that channel" in (await env.approve(MOD, U2, "youtube"))["content"]
        assert [r["user_id"] for r in await env.links()] == [U1]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- YouTube polling
def test_poll_announces_new_uploads_once_with_rebuilt_links(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.link_verified(U1, UC, videos=[("aaaaaaaaaaa", "old")])
        env.http.route(C.feed_url(UC), body=feed(("ccccccccccc", "New *video* @everyone"), ("aaaaaaaaaaa", "old")))
        assert await env.cog.poll_youtube() == 1
        (post,) = env.guild.creators.sent
        e = post["embed"]
        assert e.url == "https://www.youtube.com/watch?v=ccccccccccc"
        assert "evil.example" not in json.dumps(e.to_dict())
        assert "\\*video\\*" in e.title and "@everyone" not in e.title.replace("@​everyone", "")
        assert "<@11>" in post["content"]
        am = post["allowed_mentions"]
        assert am.everyone is False and am.users is False and am.roles is False
        assert await env.cog.poll_youtube() == 0
        assert len(env.guild.creators.sent) == 1
    with_env(go, monkeypatch)


def test_poll_skips_unverified_rows(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.db.execute("INSERT INTO creators (user_id, platform, handle, external_id, added_at) "
                             "VALUES (?, 'youtube', 'h', ?, 1)", (U1, UC))
        env.http.calls.clear()
        assert await env.cog.poll_youtube() == 0
        assert env.http.calls == []  # not even fetched
    with_env(go, monkeypatch)


def test_poll_caps_a_burst_and_posts_oldest_first(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.link_verified(U1, UC)
        vids = [(f"vid{i:08d}", f"t{i}") for i in range(5, 0, -1)]  # newest first
        env.http.route(C.feed_url(UC), body=feed(*vids))
        assert await env.cog.poll_youtube() == C.MAX_NEW_PER_POLL
        titles = [p["embed"].title for p in env.guild.creators.sent]
        assert titles == ["t3", "t4", "t5"]
        assert await env.seen("youtube") == {v for v, _ in vids}
    with_env(go, monkeypatch)


def test_poll_skips_members_who_left_and_survives_failures(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.link_verified(U1, UC)
        await env.link_verified(U2, UC2)
        del env.guild.members[U2]  # U2 left the server
        env.http.routes[C.feed_url(UC)] = asyncio.TimeoutError()
        env.http.route(C.feed_url(UC2), body=feed(("ddddddddddd", "x")))
        env.http.calls.clear()
        assert await env.cog.poll_youtube() == 0
        assert [c["url"] for c in env.http.calls] == [C.feed_url(UC)]
        await env.cog.youtube_loop.coro(env.cog)  # never raises
    with_env(go, monkeypatch)


def test_poll_without_creators_channel_does_nothing(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.link_verified(U1, UC)
        env.guild.text_channels.remove(env.guild.creators)
        env.http.route(C.feed_url(UC), body=feed(("eeeeeeeeeee", "x")))
        assert await env.cog.poll_youtube() == 0
        assert "eeeeeeeeeee" not in await env.seen("youtube")  # still announced once the channel exists
    with_env(go, monkeypatch)


def test_failed_post_is_not_retried(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.link_verified(U1, UC)
        env.http.route(C.feed_url(UC), body=feed(("fffffffffff", "x")))
        env.guild.creators.fail = discord.HTTPException(SimpleNamespace(status=403, reason="no"), "no")
        assert await env.cog.poll_youtube() == 1
        env.guild.creators.fail = None
        assert await env.cog.poll_youtube() == 0
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- Twitch
HELIX_USERS = f"{cogmod.HELIX}/users"
HELIX_STREAMS = f"{cogmod.HELIX}/streams"


def users_reply(uid="77", login="CoolKid", bio=""):
    return json.dumps({"data": [{"id": uid, "login": login, "display_name": login, "description": bio}]}).encode()


def streams_reply(*streams):
    return json.dumps({"data": [dict(id=sid, user_id=uid, user_login=login, title=title, game_name="Valorant",
                                     type="live") for sid, uid, login, title in streams]}).encode()


def test_twitch_not_configured(monkeypatch):
    async def go(env):
        assert (await env.link(U1, "twitch", "coolkid"))["content"] == MESSAGES["twitch_off"]
        assert env.http.calls == [] and env.http.posts == []
        assert await env.cog.poll_twitch() == 0
    with_env(go, monkeypatch)


def test_twitch_link_verify_and_go_live_once_per_stream(monkeypatch):
    async def go(env):
        env.member(U1)
        env.http.route(HELIX_USERS, body=users_reply())
        reply = await env.link(U1, "twitch", "https://www.twitch.tv/CoolKid")
        assert "https://twitch.tv/coolkid" in reply["content"] and "Twitch bio" in reply["content"]
        assert await env.links() == []
        (p,) = await env.pending(U1)
        assert (p["handle"], p["external_id"]) == ("coolkid", "77")
        (post,) = env.http.posts
        assert post["url"] == cogmod.TOKEN_URL and post["data"]["grant_type"] == "client_credentials"
        h = env.http.calls[0]["headers"]
        assert h["Client-Id"] == "cid-test" and h["Authorization"] == "Bearer tok1"

        env.http.route(HELIX_USERS, body=users_reply(bio=f"hi {p['code']}"))
        env.http.route(HELIX_STREAMS, body=streams_reply())
        env.http.calls.clear()
        assert (await env.verify(U1, "twitch"))["content"].startswith("Verified")
        assert env.http.calls[0]["params"] == [("id", "77")]  # re-fetched by id, not by login
        (row,) = await env.links(U1)
        assert (row["handle"], row["external_id"], row["verified"]) == ("coolkid", "77", 1)

        env.http.route(HELIX_STREAMS, body=streams_reply(("5001", "77", "coolkid", "ranked _grind_")))
        assert await env.cog.poll_twitch() == 1
        (sent,) = env.guild.creators.sent
        assert sent["embed"].url == "https://twitch.tv/coolkid"
        assert "\\_grind\\_" in sent["embed"].title
        assert sent["allowed_mentions"].users is False
        assert await env.cog.poll_twitch() == 0  # same stream id
        env.http.route(HELIX_STREAMS, body=streams_reply(("5002", "77", "coolkid", "again")))
        assert await env.cog.poll_twitch() == 1
        assert len(env.http.posts) == 1  # token reused
    with_env(go, monkeypatch, environ=ENV)


def test_twitch_wrong_bio_and_other_account(monkeypatch):
    async def go(env):
        env.http.route(HELIX_USERS, body=users_reply())
        code = code_of(await env.link(U1, "twitch", "coolkid"))
        env.http.route(HELIX_STREAMS, body=streams_reply())
        env.http.route(HELIX_USERS, body=users_reply(bio="no code here"))
        assert "couldn't find" in (await env.verify(U1, "twitch"))["content"]
        env.t += C.VERIFY_COOLDOWN
        env.http.route(HELIX_USERS, body=users_reply(uid="78", bio=code))  # a reply for someone else
        assert (await env.verify(U1, "twitch"))["content"] == MESSAGES["not_found"]
        assert await env.links() == []
    with_env(go, monkeypatch, environ=ENV)


def test_twitch_live_at_verify_time_is_not_announced(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.link_twitch_verified(U1, live=[("6001", "77", "coolkid", "already on")])
        assert await env.cog.poll_twitch() == 0
    with_env(go, monkeypatch, environ=ENV)


def test_twitch_poll_skips_unverified_rows(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.db.execute("INSERT INTO creators (user_id, platform, handle, external_id, added_at) "
                             "VALUES (?, 'twitch', 'coolkid', '77', 1)", (U1,))
        assert await env.cog.poll_twitch() == 0
        assert env.http.calls == []
    with_env(go, monkeypatch, environ=ENV)


def test_twitch_unknown_login_and_outage(monkeypatch):
    async def go(env):
        env.http.route(HELIX_USERS, body=json.dumps({"data": []}).encode())
        assert (await env.link(U1, "twitch", "nobodyhere"))["content"] == MESSAGES["not_found"]
        assert (await env.link(U1, "twitch", "bad name!"))["content"] == MESSAGES["twitch_input"]
        env.http.token_reply = (400, b'{"message":"invalid client secret-test"}')
        env.cog.token = None
        assert "couldn't reach Twitch" in (await env.link(U1, "twitch", "coolkid"))["content"]
        assert await env.links() == [] and await env.pending() == []
    with_env(go, monkeypatch, environ=ENV)


def test_twitch_401_drops_token_and_poll_never_raises(monkeypatch):
    async def go(env):
        env.member(U1)
        await env.link_twitch_verified(U1)
        env.http.route(HELIX_STREAMS, status=401)
        assert await env.cog.poll_twitch() == 0
        assert env.cog.token is None
        await env.cog.twitch_loop.coro(env.cog)
        assert len(env.http.posts) == 2  # fetched a fresh token
    with_env(go, monkeypatch, environ=ENV)


def test_token_expiry_triggers_refresh(monkeypatch):
    async def go(env):
        await env.cog.twitch_token()
        env.t += 3600
        await env.cog.twitch_token()
        assert len(env.http.posts) == 2
    with_env(go, monkeypatch, environ=ENV)


# ---------------------------------------------------------------- unlink, list, remove
def test_unlink(monkeypatch):
    async def go(env):
        await env.link_verified(U1, UC)
        inter = FakeInteraction(env.member(U1), env.guild)
        await Creators.unlink.callback(env.cog, inter, choice("youtube"))
        assert "Unlinked" in inter.reply()["content"] and await env.links() == []
        await Creators.unlink.callback(env.cog, inter, choice("youtube"))
        assert "haven't linked" in inter.reply()["content"]
        await env.link(U1, "youtube", UC)  # a waiting claim is unlinked too
        await Creators.unlink.callback(env.cog, inter, choice("youtube"))
        assert "Unlinked" in inter.reply()["content"] and await env.pending() == []
    with_env(go, monkeypatch)


def test_list_shows_verified_present_members_without_pings(monkeypatch):
    async def go(env):
        env.member(U1)
        env.member(U2)
        await env.db.execute("INSERT INTO creators VALUES (?, 'youtube', '@cool_kid', ?, 1, 1)", (U1, UC))
        await env.db.execute("INSERT INTO creators VALUES (?, 'twitch', 'gone', '9', 2, 1)", (99,))
        await env.db.execute("INSERT INTO creators VALUES (?, 'youtube', '@claimer', ?, 3, 0)", (U2, UC2))
        inter = FakeInteraction(env.member(U1), env.guild)  # 99 isn't in the server
        await Creators.list_.callback(env.cog, inter)
        sent = inter.reply()
        desc = sent["embed"].description
        assert "<@11>" in desc and C.channel_url(UC) in desc and "@cool\\_kid" in desc
        assert "gone" not in desc
        assert "claimer" not in desc and "<@12>" not in desc  # unverified claims aren't shown
        assert "isn't verified" not in desc
        assert sent["allowed_mentions"].users is False
    with_env(go, monkeypatch)


def test_list_tells_members_about_their_unverified_and_waiting_links(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO creators VALUES (?, 'youtube', '@me', ?, 3, 0)", (U2, UC2))
        await env.db.execute("INSERT INTO creator_pending VALUES (?, 'twitch', 'me', '5', 'FD-ABCDEF', ?)",
                             (U2, T0))
        inter = FakeInteraction(env.member(U2), env.guild)
        await Creators.list_.callback(env.cog, inter)
        desc = inter.reply()["embed"].description
        assert "Nobody has linked" in desc
        assert "YouTube link isn't verified" in desc and "/creator link" in desc
        assert "FD-ABCDEF" in desc and "/creator verify" in desc
        env.t += C.CODE_TTL
        await Creators.list_.callback(env.cog, inter)
        assert "FD-ABCDEF" not in inter.reply()["embed"].description
    with_env(go, monkeypatch)


def test_list_empty(monkeypatch):
    async def go(env):
        inter = FakeInteraction(env.member(U1), env.guild)
        await Creators.list_.callback(env.cog, inter)
        assert "Nobody" in inter.reply()["embed"].description
    with_env(go, monkeypatch)


def test_remove_is_staff_only(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO creators (user_id, platform, handle, external_id, added_at) "
                             "VALUES (?, 'youtube', 'h', ?, 1)", (U1, UC))
        await env.db.execute("INSERT INTO creator_pending VALUES (?, 'twitch', 'me', '5', 'FD-ABCDEF', ?)", (U1, T0))
        target = env.member(U1)
        inter = FakeInteraction(env.member(U2), env.guild)
        await Creators.remove.callback(env.cog, inter, target, None)
        assert "Only mods" in inter.reply()["content"] and len(await env.links()) == 1
        inter = FakeInteraction(env.member(MOD, roles=[config.MOD_ROLE]), env.guild)
        await Creators.remove.callback(env.cog, inter, target, None)
        assert "Removed 1 link" in inter.reply()["content"] and await env.links() == []
        assert await env.pending() == []
    with_env(go, monkeypatch)


def test_twitch_credentials_never_logged(monkeypatch, caplog):
    async def go(env):
        env.http.token_reply = RuntimeError("boom secret-test")
        env.member(U1)
        await env.db.execute("INSERT INTO creators VALUES (?, 'twitch', 'coolkid', '77', 1, 1)", (U1,))
        await env.cog.twitch_loop.coro(env.cog)
        assert "secret-test" not in caplog.text
    with_env(go, monkeypatch, environ=ENV)
