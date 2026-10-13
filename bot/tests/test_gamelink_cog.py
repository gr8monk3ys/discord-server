"""Offline tests for cogs.gamelink: in-memory SQLite, fake Discord objects and a fake HTTP
layer. No network."""

import asyncio
import inspect
import json
import re
from types import SimpleNamespace

from discord import app_commands

import config
import db as dbmod
from cogs import gamelink as cogmod
from cogs.gamelink import MESSAGES, GameLink
from logic import gamelink as G
from tests.test_gamelink import GAMES, NOT_FOUND, SID, SID2, profile_xml

GUILD_ID = 999
OWNER, U1, U2, MOD = 1, 11, 12, 20
T0 = 1_790_000_000
BY_ID = G.id_xml_url(SID)
BY_NAME = "https://steamcommunity.com/id/coolkid?xml=1"


def run(coro):
    return asyncio.run(coro)


STEAM = app_commands.Choice(name="Steam", value="steam")


class FakeHttp:
    """url -> (status, body, cut, location) or an exception; records every call."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def route(self, url, status=200, body=b"", cut=False, location=None):
        self.routes[url] = (status, body, cut, location)

    async def get(self, url, *, max_bytes=G.MAX_BODY):
        self.calls.append(dict(url=url, max_bytes=max_bytes))
        hit = self.routes.get(url)
        if hit is None:
            raise AssertionError(f"unexpected GET {url}")
        if isinstance(hit, Exception):
            raise hit
        return hit


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
        self.display_name = f"member_{uid}"
        self.roles = [Role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=False)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.text_channels = [FakeText(config.GENERAL_CHANNEL), self.mod_log]
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


class FakeInteraction:
    def __init__(self, user, guild):
        self.calls = []
        self.user = user
        self.guild = guild
        calls = self.calls

        class Response:
            async def send_message(self, content=None, **kwargs):
                calls.append(("send_message", dict(content=content, **kwargs)))

            async def defer(self, **kwargs):
                calls.append(("defer", kwargs))

        class Followup:
            async def send(self, content=None, **kwargs):
                calls.append(("followup", dict(content=content, **kwargs)))

        self.response, self.followup = Response(), Followup()

    def reply(self):
        """What the member saw (the last message or followup)."""
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

    async def link(self, uid, text):
        inter = FakeInteraction(self.member(uid), self.guild)
        await GameLink.steam.callback(self.cog, inter, text)
        return inter.reply()

    async def verify(self, uid):
        inter = FakeInteraction(self.member(uid), self.guild)
        await GameLink.verify.callback(self.cog, inter, STEAM)
        return inter.reply()

    async def approve(self, mod_uid, uid, roles=(config.MOD_ROLE,)):
        inter = FakeInteraction(self.member(mod_uid, roles=roles), self.guild)
        await GameLink.approve.callback(self.cog, inter, self.member(uid), STEAM)
        return inter.reply()

    async def remove(self, mod_uid, uid, roles=(config.MOD_ROLE,)):
        inter = FakeInteraction(self.member(mod_uid, roles=roles), self.guild)
        await GameLink.remove.callback(self.cog, inter, self.member(uid), STEAM)
        return inter.reply()

    async def unlink(self, uid):
        inter = FakeInteraction(self.member(uid), self.guild)
        await GameLink.unlink.callback(self.cog, inter, STEAM)
        return inter.reply()

    async def accounts(self, uid, target=None):
        inter = FakeInteraction(self.member(uid), self.guild)
        await GameLink.accounts.callback(self.cog, inter, self.member(target) if target else None)
        return inter.reply()

    async def link_verified(self, uid, steam_id=SID, **profile):
        """Link a Steam account and prove it with the code in the profile summary."""
        url = G.id_xml_url(steam_id)
        self.http.route(url, body=profile_xml(steam_id, **profile))
        reply = await self.link(uid, steam_id)
        self.http.route(url, body=profile_xml(steam_id, summary=f"hi {code_of(reply)}", **profile))
        self.t += G.VERIFY_COOLDOWN
        reply = await self.verify(uid)
        assert reply["content"].startswith("Verified"), reply["content"]
        return reply

    async def rows(self, uid=None):
        sql = "SELECT * FROM game_accounts" + (" WHERE user_id = ?" if uid else "") + " ORDER BY user_id, platform"
        return [dict(r) for r in await self.db.fetchall(sql, (uid,) if uid else ())]

    async def pending(self, uid=None):
        sql = ("SELECT * FROM game_account_pending" + (" WHERE user_id = ?" if uid else "")
               + " ORDER BY user_id, platform")
        return [dict(r) for r in await self.db.fetchall(sql, (uid,) if uid else ())]


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        http = FakeHttp()
        cog = GameLink(FakeBot(db, guild), http=http)
        env = Env(db, guild, http, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- /link steam
def test_link_creates_a_claim_not_an_account(monkeypatch):
    async def go(env):
        env.http.route(BY_ID, body=profile_xml(games=GAMES))
        reply = await env.link(U1, f"https://steamcommunity.com/profiles/{SID}/")
        assert reply["ephemeral"] is True and reply["allowed_mentions"].users is False
        assert "Cool \\*Kid\\*" in reply["content"] and "/link verify" in reply["content"]
        assert f"<https://steamcommunity.com/profiles/{SID}>" in reply["content"]
        assert "isn't public" not in reply["content"]
        code = code_of(reply)
        assert await env.rows() == []  # nothing shows before verification
        (p,) = await env.pending(U1)
        assert (p["platform"], p["account_id"], p["display"], p["code"], p["created_at"]) == \
            ("steam", SID, "Cool *Kid*", code, T0)
        assert env.http.calls == [dict(url=BY_ID, max_bytes=G.MAX_BODY)]
        assert await env.cog.profile_lines(U1) == []
    with_env(go, monkeypatch)


def test_link_by_vanity_resolves_to_the_steam_id(monkeypatch):
    async def go(env):
        env.http.route(BY_NAME, body=profile_xml())
        for text in ("coolkid", "https://steamcommunity.com/id/coolkid/games"):
            reply = await env.link(U1, text)
            assert "FD-" in reply["content"]
        (p,) = await env.pending(U1)
        assert p["account_id"] == SID
        assert {c["url"] for c in env.http.calls} == {BY_NAME}
    with_env(go, monkeypatch)


def test_link_again_keeps_the_live_code_and_a_new_account_gets_a_new_one(monkeypatch):
    async def go(env):
        env.http.route(BY_ID, body=profile_xml())
        env.http.route(G.id_xml_url(SID2), body=profile_xml(SID2, persona="Other"))
        first = code_of(await env.link(U1, SID))
        env.t += 3600
        assert code_of(await env.link(U1, SID)) == first
        assert (await env.pending(U1))[0]["created_at"] == T0
        other = code_of(await env.link(U1, SID2))
        assert other != first and (await env.pending(U1))[0]["account_id"] == SID2
        env.t += G.CODE_TTL
        assert code_of(await env.link(U1, SID2)) != other  # the old code had expired
    with_env(go, monkeypatch)


def test_link_warns_when_the_profile_is_private(monkeypatch):
    async def go(env):
        env.http.route(BY_ID, body=profile_xml(privacy="friendsonly"))
        reply = await env.link(U1, SID)
        assert "FD-" in reply["content"] and "isn't public" in reply["content"]
    with_env(go, monkeypatch)


def test_link_problems(monkeypatch):
    async def go(env):
        for bad in ("https://evil.example/id/coolkid", "cool kid", "12345678901234567", "x"):
            assert (await env.link(U1, bad))["content"] == MESSAGES["input"]
        assert env.http.calls == []  # nothing invalid is ever fetched
        env.http.route("https://steamcommunity.com/id/ghost?xml=1", body=NOT_FOUND)
        assert (await env.link(U1, "ghost"))["content"] == MESSAGES["not_found"]
        env.http.route("https://steamcommunity.com/id/gone?xml=1", status=404)
        assert (await env.link(U1, "gone"))["content"] == MESSAGES["not_found"]
        env.http.routes["https://steamcommunity.com/id/slow?xml=1"] = asyncio.TimeoutError()
        assert (await env.link(U1, "slow"))["content"] == MESSAGES["unreachable"]
        for name, kwargs in (("big", dict(body=b"x" * 10, cut=True)), ("down", dict(status=503)),
                             ("dtd", dict(body=b"<!DOCTYPE x><profile/>")), ("html", dict(body=b"<html></html>")),
                             ("limited", dict(status=429))):
            env.http.route(f"https://steamcommunity.com/id/{name}?xml=1", **kwargs)
            assert (await env.link(U1, name))["content"] == MESSAGES["unreachable"], name
        # asked for one SteamID64, another account answered
        env.http.route(BY_ID, body=profile_xml(SID2))
        assert (await env.link(U1, SID))["content"] == MESSAGES["unreachable"]
        assert await env.rows() == [] and await env.pending() == []
    with_env(go, monkeypatch)


def test_redirects_are_followed_only_to_steam_profiles(monkeypatch):
    async def go(env):
        env.http.route(BY_NAME, status=302, location=f"https://steamcommunity.com/profiles/{SID}/?xml=1")
        env.http.route(BY_ID, body=profile_xml())
        assert "FD-" in (await env.link(U1, "coolkid"))["content"]
        assert [c["url"] for c in env.http.calls] == [BY_NAME, BY_ID]
        for location in ("https://evil.example/id/coolkid?xml=1", "http://steamcommunity.com/id/coolkid?xml=1",
                         "https://steamcommunity.com/login/home/", None):
            env.http.calls.clear()
            env.http.route("https://steamcommunity.com/id/hop?xml=1", status=302, location=location)
            assert (await env.link(U2, "hop"))["content"] == MESSAGES["unreachable"]
            assert [c["url"] for c in env.http.calls] == ["https://steamcommunity.com/id/hop?xml=1"]
        # a loop stops after MAX_REDIRECTS hops
        env.http.calls.clear()
        env.http.route("https://steamcommunity.com/id/loop?xml=1", status=301,
                       location="https://steamcommunity.com/id/loop")
        assert (await env.link(U2, "loop"))["content"] == MESSAGES["unreachable"]
        assert len(env.http.calls) == G.MAX_REDIRECTS + 1
        assert await env.pending(U2) == []
    with_env(go, monkeypatch)


def test_link_crash_is_a_friendly_message(monkeypatch):
    async def go(env):
        async def boom(*a, **k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "owner_of", boom)
        env.http.route(BY_ID, body=profile_xml())
        assert (await env.link(U1, SID))["content"] == MESSAGES["unreachable"]
        monkeypatch.setattr(env.cog, "pending_of", boom)
        assert (await env.verify(U1))["content"] == MESSAGES["unreachable"]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /link verify
def test_verify_with_code_in_summary_links_the_account(monkeypatch):
    async def go(env):
        reply = await env.link_verified(U1, games=GAMES)
        assert reply["ephemeral"] is True and "/unlink" in reply["content"]
        (row,) = await env.rows()
        assert (row["user_id"], row["platform"], row["account_id"], row["display"], row["verified"]) == \
            (U1, "steam", SID, "Cool *Kid*", 1)
        assert row["linked_at"] == row["updated_at"] == T0 + G.VERIFY_COOLDOWN
        data = json.loads(row["data"])
        assert data["hours"] == {"riskofrain2": 1234.0, "wardogs": 54.0} and data["persona"] == "Cool *Kid*"
        assert await env.pending() == []
        assert (await env.verify(U1))["content"] == MESSAGES["already"]
    with_env(go, monkeypatch)


def test_impersonator_cannot_claim_an_account_they_dont_control(monkeypatch):
    async def go(env):
        env.http.route(BY_ID, body=profile_xml(summary="the real owner's summary"))
        code = code_of(await env.link(U2, SID))
        reply = await env.verify(U2)
        assert reply["content"] == MESSAGES["no_code"].format(code=code)
        assert await env.rows() == [] and len(await env.pending(U2)) == 1
        assert await env.cog.profile_lines(U2) == []
        # a code that is only part of a longer word, or someone else's code, doesn't count
        env.http.route(BY_ID, body=profile_xml(summary=f"x{code}x FD-AAAAAA"))
        env.t += G.VERIFY_COOLDOWN
        assert (await env.verify(U2))["content"] == MESSAGES["no_code"].format(code=code)
        # the persona name is not where the code goes
        env.http.route(BY_ID, body=profile_xml(persona=code))
        env.t += G.VERIFY_COOLDOWN
        assert (await env.verify(U2))["content"] == MESSAGES["no_code"].format(code=code)
    with_env(go, monkeypatch)


def test_verify_problems(monkeypatch):
    async def go(env):
        assert (await env.verify(U1))["content"] == MESSAGES["no_pending"]
        env.http.route(BY_ID, body=profile_xml())
        code = code_of(await env.link(U1, SID))
        env.http.route(BY_ID, body=profile_xml(privacy="private"))
        assert (await env.verify(U1))["content"] == MESSAGES["private"].format(code=code)
        # one check a minute
        reply = await env.verify(U1)
        assert reply["content"] == MESSAGES["cooldown"].format(seconds=G.VERIFY_COOLDOWN)
        env.t += G.VERIFY_COOLDOWN
        env.http.route(BY_ID, status=500)
        assert (await env.verify(U1))["content"] == MESSAGES["unreachable"]
        env.t += G.VERIFY_COOLDOWN
        env.http.route(BY_ID, body=NOT_FOUND)
        assert (await env.verify(U1))["content"] == MESSAGES["not_found"]
        env.t += G.VERIFY_COOLDOWN
        env.http.route(BY_ID, body=profile_xml(SID2, summary=code))  # the wrong account answered
        assert (await env.verify(U1))["content"] == MESSAGES["unreachable"]
        assert await env.rows() == []
        env.t = T0 + G.CODE_TTL
        env.http.route(BY_ID, body=profile_xml(summary=code))
        assert (await env.verify(U1))["content"] == MESSAGES["expired"]
        assert await env.pending() == [] and await env.rows() == []
    with_env(go, monkeypatch)


def test_one_steam_account_one_member(monkeypatch):
    async def go(env):
        await env.link_verified(U1)
        assert (await env.link(U2, SID))["content"] == MESSAGES["taken"]
        assert await env.pending(U2) == []
        # two members race for the same account: both hold a claim, the first to verify wins
        await env.unlink(U1)
        env.http.route(BY_ID, body=profile_xml())
        code1, code2 = code_of(await env.link(U1, SID)), code_of(await env.link(U2, SID))
        env.http.route(BY_ID, body=profile_xml(summary=f"{code1} {code2}"))
        env.t += G.VERIFY_COOLDOWN
        assert (await env.verify(U2))["content"].startswith("Verified")
        assert (await env.verify(U1))["content"] == MESSAGES["taken"]
        assert [(r["user_id"], r["account_id"]) for r in await env.rows()] == [(U2, SID)]
    with_env(go, monkeypatch)


def test_switching_accounts_replaces_the_old_one_only_after_verifying(monkeypatch):
    async def go(env):
        await env.link_verified(U1, games=GAMES)
        env.http.route(G.id_xml_url(SID2), body=profile_xml(SID2, persona="Second"))
        code = code_of(await env.link(U1, SID2))
        assert [r["account_id"] for r in await env.rows()] == [SID]
        env.http.route(G.id_xml_url(SID2), body=profile_xml(SID2, persona="Second", summary=code))
        env.t += G.VERIFY_COOLDOWN
        assert (await env.verify(U1))["content"].startswith("Verified")
        (row,) = await env.rows()
        assert row["account_id"] == SID2 and row["display"] == "Second"
        assert json.loads(row["data"])["hours"] == {}  # the other account's hours don't carry over
        # the first account is free for someone else now
        env.http.route(BY_ID, body=profile_xml())
        assert "FD-" in (await env.link(U2, SID))["content"]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- staff
def test_approve_verifies_by_hand_and_logs(monkeypatch):
    async def go(env):
        env.http.route(BY_ID, body=profile_xml(privacy="private"))
        await env.link(U1, SID)
        reply = await env.approve(MOD, U1)
        assert reply["content"] == f"Approved <@{U1}>'s Steam account." and reply["ephemeral"] is True
        assert reply["allowed_mentions"].users is False
        (row,) = await env.rows()
        assert row["user_id"] == U1 and row["verified"] == 1 and await env.pending() == []
        (logged,) = env.guild.mod_log.sent
        text = logged["embed"].description
        assert f"<@{MOD}>" in text and f"<@{U1}>" in text and f"<https://steamcommunity.com/profiles/{SID}>" in text
        assert logged["allowed_mentions"].users is False
        assert "already verified" in (await env.approve(MOD, U1))["content"]
        assert "no Steam link waiting" in (await env.approve(MOD, U2))["content"]
    with_env(go, monkeypatch)


def test_approve_is_staff_only_and_needs_steam(monkeypatch):
    async def go(env):
        env.http.route(BY_ID, body=profile_xml())
        await env.link(U1, SID)
        assert (await env.approve(U2, U1, roles=()))["content"] == "Only mods can approve game accounts."
        assert await env.rows() == []
        env.http.route(BY_ID, status=503)
        assert (await env.approve(MOD, U1))["content"] == MESSAGES["unreachable"]
        assert await env.rows() == [] and env.guild.mod_log.sent == []
        # the owner and Keepers count as staff; a mod log that fails doesn't undo the approval
        env.http.route(BY_ID, body=profile_xml())
        env.guild.mod_log.fail = RuntimeError("no access")
        assert (await env.approve(OWNER, U1, roles=()))["content"].startswith("Approved")
        assert len(await env.rows()) == 1
    with_env(go, monkeypatch)


def test_approve_cannot_take_an_account_verified_by_someone_else(monkeypatch):
    async def go(env):
        env.http.route(BY_ID, body=profile_xml())
        await env.link(U2, SID)
        await env.link_verified(U1)
        assert "already verified that account" in (await env.approve(MOD, U2))["content"]
        assert [(r["user_id"]) for r in await env.rows()] == [U1]
    with_env(go, monkeypatch)


def test_staff_remove(monkeypatch):
    async def go(env):
        await env.link_verified(U1)
        assert (await env.remove(U2, U1, roles=()))["content"] == "Only mods can remove someone else's account."
        assert len(await env.rows()) == 1
        reply = await env.remove(MOD, U1)
        assert reply["content"] == f"Removed <@{U1}>'s Steam account." and reply["allowed_mentions"].users is False
        assert await env.rows() == [] and len(env.guild.mod_log.sent) == 1
        assert (await env.remove(MOD, U1))["content"] == "Nothing to remove."
        assert len(env.guild.mod_log.sent) == 1
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /unlink, /accounts, profile_lines
def test_unlink_deletes_everything(monkeypatch):
    async def go(env):
        assert (await env.unlink(U1))["content"] == "You haven't linked a Steam account."
        await env.link_verified(U1, games=GAMES)
        env.http.route(G.id_xml_url(SID2), body=profile_xml(SID2))
        await env.link(U1, SID2)  # a waiting claim as well
        reply = await env.unlink(U1)
        assert reply["content"].startswith("Unlinked your Steam account.") and reply["ephemeral"] is True
        assert await env.rows() == [] and await env.pending() == []
        assert await env.cog.profile_lines(U1) == []
        for table in ("game_accounts", "game_account_pending"):
            assert (await env.db.fetchone(f"SELECT COUNT(*) AS n FROM {table}"))["n"] == 0
    with_env(go, monkeypatch)


def test_profile_lines_and_accounts(monkeypatch):
    async def go(env):
        assert (await env.accounts(U1))["embed"].description == MESSAGES["none"]
        assert (await env.accounts(U2, U1))["embed"].description == f"<@{U1}> hasn't linked a game account."
        await env.link_verified(U1, persona="[x](https://evil.example) *K*", games=GAMES)
        lines = await env.cog.profile_lines(U1)
        assert len(lines) == 3 and lines[0].startswith("**Steam** · ")
        assert lines[0].endswith(f"<https://steamcommunity.com/profiles/{SID}>")
        assert "[x](" not in lines[0] and "evil.example" not in lines[0]  # the filter dropped the persona
        assert "Risk of Rain 2 1,234 h" in lines[1] and "Wardogs 54 h" in lines[1]
        assert lines[2].startswith("Lately: ")
        reply = await env.accounts(U2, U1)
        assert reply["ephemeral"] is True and reply["allowed_mentions"].users is False
        assert reply["embed"].description == "\n".join(lines)
        assert reply["embed"].title == f"Game accounts · member\\_{U1}"
        assert (await env.accounts(U1))["embed"].description == "\n".join(lines)
    with_env(go, monkeypatch)


def test_persona_markdown_is_escaped(monkeypatch):
    async def go(env):
        await env.link_verified(U1, persona="**big** _name_ `x` ||y||")
        (line,) = await env.cog.profile_lines(U1)
        assert "**big**" not in line and "`x`" not in line and "||y||" not in line and "\\*\\*big\\*\\*" in line
    with_env(go, monkeypatch)


def test_accounts_shows_own_notes_only_to_the_owner(monkeypatch):
    async def go(env):
        env.http.route(BY_ID, body=profile_xml())
        code = code_of(await env.link(U1, SID))
        mine = (await env.accounts(U1))["embed"].description
        assert MESSAGES["none"] in mine and code in mine
        theirs = (await env.accounts(U2, U1))["embed"].description
        assert code not in theirs and "FD-" not in theirs
        env.t += G.CODE_TTL
        assert "FD-" not in (await env.accounts(U1))["embed"].description  # expired claims aren't shown
        bot_user = env.member(77)
        bot_user.bot = True
        assert "hasn't linked" in (await env.accounts(U1, 77))["embed"].description
    with_env(go, monkeypatch)


def test_profile_lines_never_raise(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO game_accounts (user_id, platform, account_id, display, verified, data,"
                             " linked_at, updated_at) VALUES (?, 'steam', ?, 'x', 1, 'not json', 1, 1)", (U1, SID))
        await env.db.execute("INSERT INTO game_accounts (user_id, platform, account_id, display, verified, data,"
                             " linked_at, updated_at) VALUES (?, 'steam', 'bad id', 'x', 1, '{}', 1, 1)", (U2,))
        await env.db.execute("INSERT INTO game_accounts (user_id, platform, account_id, display, verified, data,"
                             " linked_at, updated_at) VALUES (?, 'steam', ?, 'x', 0, '{}', 1, 1)", (33, SID2))
        assert len(await env.cog.profile_lines(U1)) == 2
        assert await env.cog.profile_lines(U2) == []
        assert await env.cog.profile_lines(33) == []  # unverified rows are never shown

        async def boom(*a, **k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.db, "fetchall", boom)
        assert await env.cog.profile_lines(U1) == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- daily refresh
def test_refresh_updates_due_accounts_once_a_day(monkeypatch):
    async def go(env):
        await env.link_verified(U1, games=GAMES)
        linked = env.t
        env.http.calls.clear()
        assert await env.cog.refresh_due() == 0 and env.http.calls == []
        env.t = linked + G.DAY - 1
        assert await env.cog.refresh_due() == 0 and env.http.calls == []
        env.t = linked + G.DAY
        env.http.route(BY_ID, body=profile_xml(persona="Renamed", games=(("Apex Legends", 1172470, "2", "60"),)))
        assert await env.cog.refresh_due() == 1
        (row,) = await env.rows()
        data = json.loads(row["data"])
        assert row["display"] == "Renamed" and row["updated_at"] == env.t and row["linked_at"] == linked
        assert data["hours"] == {"riskofrain2": 1234.0, "wardogs": 54.0, "apexlegends": 60.0}
        assert len(env.http.calls) == 1
        assert await env.cog.refresh_due() == 0 and len(env.http.calls) == 1  # never twice in a day
    with_env(go, monkeypatch)


def test_refresh_stops_after_three_failures_until_relinked(monkeypatch):
    async def go(env):
        await env.link_verified(U1, games=GAMES)
        env.http.route(BY_ID, status=503)
        env.http.calls.clear()
        for day in range(1, 4):
            env.t += G.DAY
            assert await env.cog.refresh_due() == 0
            assert len(env.http.calls) == day
            assert await env.cog.refresh_due() == 0 and len(env.http.calls) == day  # one try a day
        (row,) = await env.rows()
        assert json.loads(row["data"])["fails"] == 3 and json.loads(row["data"])["hours"]["wardogs"] == 54.0
        env.t += 30 * G.DAY
        assert await env.cog.refresh_due() == 0 and len(env.http.calls) == 3  # left alone
        assert len(await env.cog.profile_lines(U1)) >= 2  # what was known still shows
        assert "stopped updating" in (await env.accounts(U1))["embed"].description
        assert "stopped updating" not in (await env.accounts(U2, U1))["embed"].description
        # the member links again: refreshed on the spot, failures cleared
        env.http.route(BY_ID, body=profile_xml(persona="Back"))
        reply = await env.link(U1, SID)
        assert reply["content"].startswith(MESSAGES["already"])
        (row,) = await env.rows()
        assert json.loads(row["data"])["fails"] == 0 and row["display"] == "Back" and row["updated_at"] == env.t
        assert "stopped updating" not in (await env.accounts(U1))["embed"].description
        env.t += G.DAY
        assert await env.cog.refresh_due() == 1
    with_env(go, monkeypatch)


def test_a_success_resets_the_failure_count(monkeypatch):
    async def go(env):
        await env.link_verified(U1)
        for body in (None, None, profile_xml(), None, None):
            env.t += G.DAY
            env.http.route(BY_ID, **(dict(body=body) if body else dict(status=500)))
            await env.cog.refresh_due()
        (row,) = await env.rows()
        assert json.loads(row["data"])["fails"] == 2
    with_env(go, monkeypatch)


def test_refresh_failure_kinds_and_wrong_account(monkeypatch):
    async def go(env):
        await env.link_verified(U1)
        env.t += G.DAY
        env.http.route(BY_ID, body=profile_xml(SID2, persona="Somebody Else"))
        assert await env.cog.refresh_due() == 0
        (row,) = await env.rows()
        assert row["display"] == "Cool *Kid*" and json.loads(row["data"])["fails"] == 1
        env.t += G.DAY
        env.http.routes[BY_ID] = RuntimeError("boom")
        assert await env.cog.refresh_due() == 0
        env.t += G.DAY
        env.http.route(BY_ID, body=NOT_FOUND)
        assert await env.cog.refresh_due() == 0
        assert json.loads((await env.rows())[0]["data"])["fails"] == 3
    with_env(go, monkeypatch)


def test_refresh_is_batched_and_skips_members_who_left(monkeypatch):
    async def go(env):
        ids = [f"7656119800000{n:04d}" for n in range(10, 10 + G.REFRESH_BATCH + 2)]
        for n, steam_id in enumerate(ids):
            await env.link_verified(100 + n, steam_id)
        del env.guild.members[100]  # left the server
        env.t += 2 * G.DAY
        env.http.calls.clear()
        assert await env.cog.refresh_due() == G.REFRESH_BATCH
        assert [c["url"] for c in env.http.calls] == [G.id_xml_url(s) for s in ids[1:1 + G.REFRESH_BATCH]]
        assert await env.cog.refresh_due() == 1  # the rest on the next run
        assert await env.cog.refresh_due() == 0
        assert len(await env.rows()) == len(ids)  # the leaver's row is kept, just not refreshed
        env.cog.bot.guild = None
        env.t += 2 * G.DAY
        assert await env.cog.refresh_due() == 0
    with_env(go, monkeypatch)


def test_upkeep_cleans_expired_claims_and_never_raises(monkeypatch):
    async def go(env):
        env.http.route(BY_ID, body=profile_xml())
        await env.link(U1, SID)
        env.cog.last_verify[U1] = env.t
        await GameLink.upkeep_loop.coro(env.cog)
        assert len(await env.pending()) == 1 and U1 in env.cog.last_verify
        env.t += G.CODE_TTL
        await GameLink.upkeep_loop.coro(env.cog)
        assert await env.pending() == [] and env.cog.last_verify == {}

        async def boom():
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "clean_pending", boom)
        await GameLink.upkeep_loop.coro(env.cog)  # logged, not raised
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- shape
def test_commands_and_network_seam():
    group = GameLink.link
    assert group.name == "link" and group.guild_only is True
    assert {c.name for c in group.commands} == {"steam", "verify", "approve", "remove"}
    assert GameLink.unlink.name == "unlink" and GameLink.accounts.name == "accounts"
    assert GameLink.unlink.guild_only and GameLink.accounts.guild_only
    assert [c.value for c in cogmod.PLATFORM_CHOICES] == list(G.PLATFORMS)
    source = inspect.getsource(cogmod.AiohttpHttp)
    assert "allow_redirects=False" in source and "ClientTimeout(total=G.FETCH_TIMEOUT)" in source
    assert "max_bytes" in source
    assert inspect.iscoroutinefunction(GameLink.profile_lines)
