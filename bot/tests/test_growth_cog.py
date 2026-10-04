"""Offline integration tests for cogs.growth.Growth: a real SQLite database plus
lightweight fakes for the bot, guild, invites, roles, members and interactions.
No network. Time is controlled by patching cogs.growth.now."""

import asyncio
from types import SimpleNamespace

import discord
from discord import app_commands

import config
import db as dbmod
from cogs import growth as cogmod
from cogs.growth import BUMP_DUE_KEY, REMINDER_TEXT, Growth
from logic import growth as G

GUILD_ID = 999
OWNER, A, B, C, D, E, F = 1, 11, 12, 13, 14, 15, 16
BOTUSER = 50
DAY = G.DAY
T0 = 1_790_000_000


def run(coro):
    return asyncio.run(coro)


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


# ---------------------------------------------------------------- fakes
class FakeRole:
    def __init__(self, rid, name, position):
        self.id = rid
        self.name = name
        self.position = position
        self.mention = f"<@&{rid}>"

    def __gt__(self, other):
        return self.position > other.position

    def __lt__(self, other):
        return self.position < other.position


class FakeText:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeMember:
    def __init__(self, uid, guild, bot=False):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.roles = []
        self.display_name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.add_fail = None

    async def add_roles(self, *roles, reason=None):
        if self.add_fail:
            raise self.add_fail
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]


class FakeInvite:
    def __init__(self, code, inviter, uses=0, max_uses=0, guild=None):
        self.code = code
        self.inviter = SimpleNamespace(id=inviter) if inviter is not None else None
        self.uses = uses
        self.max_uses = max_uses
        self.guild = guild


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.bot_commands = FakeText(300, config.BOT_COMMANDS_CHANNEL)
        self.text_channels = [FakeText(301, config.GENERAL_CHANNEL), self.bot_commands]
        self.bot_role = FakeRole(1, "Front Desk", 10)
        self.bumper = FakeRole(2, config.BUMPER_ROLE, 5)
        self.recruiter = FakeRole(3, config.RECRUITER_ROLE, 4)
        self.roles = [self.bot_role, self.bumper, self.recruiter]
        self.me = SimpleNamespace(top_role=self.bot_role)
        self.members = []
        self.invite_list: list[FakeInvite] = []
        self.invites_error = None
        self.vanity_url_code = None
        self.vanity_uses = 0
        self.invite_calls = 0

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    async def fetch_member(self, uid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")

    async def invites(self):
        self.invite_calls += 1
        if self.invites_error:
            raise self.invites_error
        return list(self.invite_list)

    async def vanity_invite(self):
        if self.vanity_url_code is None:
            return None
        return SimpleNamespace(code=self.vanity_url_code, uses=self.vanity_uses)

    def invite(self, code, inviter, uses=0, max_uses=0):
        inv = FakeInvite(code, inviter, uses, max_uses, guild=self)
        self.invite_list.append(inv)
        return inv

    def use(self, code):
        inv = next(i for i in self.invite_list if i.code == code)
        inv.uses += 1
        if inv.max_uses and inv.uses >= inv.max_uses:
            self.invite_list.remove(inv)
        return inv


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)
        self.intents = SimpleNamespace(members=True, presences=False, message_content=False)

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None

    async def wait_until_ready(self):
        return None


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls
        self.done = False

    async def send_message(self, content=None, **kwargs):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True
        self.calls.append(("send_message", dict(content=content, **kwargs)))

    def is_done(self):
        return self.done


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

    def sent(self):
        return [kw for k, kw in self.calls if k == "send_message"]


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0

    def member(self, uid, bot=False):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, self.guild, bot=bot)
            self.guild.members.append(m)
        return m

    async def join(self, uid, via=None, bot=False, at=None):
        if at is not None:
            self.t = at
        if via is not None:
            self.guild.use(via)
        m = self.member(uid, bot=bot)
        await self.cog.on_member_join(m)
        return m

    async def leave(self, uid, at=None):
        if at is not None:
            self.t = at
        m = self.guild.get_member(uid)
        self.guild.members.remove(m)
        await self.cog.on_member_remove(m)

    async def rows(self, table):
        return [dict(r) for r in await self.db.fetchall(f"SELECT * FROM {table} ORDER BY rowid")]

    async def due(self):
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (BUMP_DUE_KEY,))
        return int(row["value"]) if row else None

    def inter(self, uid):
        return FakeInteraction(self.member(uid), self.guild)


async def open_db(path=":memory:"):
    db = dbmod.Database(path)
    await db.connect()
    await db.migrate()
    return db


def with_env(fn, monkeypatch, guild=None):
    async def go():
        db = await open_db()
        g = guild or FakeGuild()
        bot = FakeBot(db, g)
        cog = Growth(bot)
        env = Env(db, g, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def disboard_message(guild, name="bump", user=A, embeds=("Bump done! :thumbsup:",), author=config.DISBOARD_BOT_ID):
    return SimpleNamespace(
        author=SimpleNamespace(id=author, bot=True),
        guild=guild,
        interaction_metadata=SimpleNamespace(user=SimpleNamespace(id=user)),
        _interaction=SimpleNamespace(name=name, user=SimpleNamespace(id=user)) if name else None,
        embeds=[discord.Embed(description=e) for e in embeds],
    )


# ---------------------------------------------------------------- invite cache
def test_ready_caches_invites_and_vanity(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A, uses=2)
        env.guild.invite("bbb", B, uses=0, max_uses=1)
        env.guild.vanity_url_code = "myserver"
        env.guild.vanity_uses = 7
        await env.cog.on_ready()
        assert {r["code"]: (r["inviter_id"], r["uses"]) for r in await env.rows("invite_uses")} == {
            "aaa": (A, 2), "bbb": (B, 0), "myserver": (None, 7)}
    with_env(go, monkeypatch)


def test_invite_create_and_delete_update_cache(monkeypatch):
    async def go(env):
        await env.cog.on_ready()
        inv = env.guild.invite("new", C)
        await env.cog.on_invite_create(inv)
        assert [(r["code"], r["inviter_id"]) for r in await env.rows("invite_uses")] == [("new", C)]
        await env.cog.on_invite_delete(SimpleNamespace(code="new", guild=env.guild))
        assert await env.rows("invite_uses") == []
    with_env(go, monkeypatch)


def test_other_guild_invites_ignored(monkeypatch):
    async def go(env):
        other = SimpleNamespace(id=1234)
        await env.cog.on_invite_create(FakeInvite("zzz", A, guild=other))
        assert await env.rows("invite_uses") == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- joins
def test_join_attributed_to_inviter(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A, uses=2)
        env.guild.invite("bbb", B, uses=5)
        await env.cog.on_ready()
        await env.join(C, via="aaa")
        rows = await env.rows("joins")
        assert [(r["user_id"], r["inviter_id"], r["invite_code"], r["left_at"]) for r in rows] == [
            (C, A, "aaa", None)]
        cache = {r["code"]: r["uses"] for r in await env.rows("invite_uses")}
        assert cache == {"aaa": 3, "bbb": 5}  # refreshed for the next diff
    with_env(go, monkeypatch)


def test_one_use_invite_consumed_and_deleted_before_join(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A, uses=2)
        env.guild.invite("once", B, uses=0, max_uses=1)
        await env.cog.on_ready()
        env.guild.use("once")  # Discord deletes it...
        await env.cog.on_invite_delete(SimpleNamespace(code="once", guild=env.guild))  # ...and says so first
        await env.cog.on_member_join(env.member(C))
        rows = await env.rows("joins")
        assert [(r["inviter_id"], r["invite_code"]) for r in rows] == [(B, "once")]
        assert {r["code"] for r in await env.rows("invite_uses")} == {"aaa"}
    with_env(go, monkeypatch)


def test_ambiguous_join_records_no_inviter(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A, uses=2)
        env.guild.invite("bbb", B, uses=5)
        await env.cog.on_ready()
        env.guild.use("aaa")
        env.guild.use("bbb")
        await env.join(C)
        rows = await env.rows("joins")
        assert [(r["user_id"], r["inviter_id"], r["invite_code"]) for r in rows] == [(C, None, None)]
    with_env(go, monkeypatch)


def test_vanity_join_has_code_but_no_inviter(monkeypatch):
    async def go(env):
        env.guild.vanity_url_code = "myserver"
        env.guild.vanity_uses = 7
        await env.cog.on_ready()
        env.guild.vanity_uses = 8
        await env.join(C)
        rows = await env.rows("joins")
        assert [(r["inviter_id"], r["invite_code"]) for r in rows] == [(None, "myserver")]
    with_env(go, monkeypatch)


def test_bots_never_count(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A, uses=2)
        await env.cog.on_ready()
        await env.join(BOTUSER, via="aaa", bot=True)
        await env.cog.on_member_remove(env.member(BOTUSER))
        assert await env.rows("joins") == []
    with_env(go, monkeypatch)


def test_forbidden_invites_logs_once_and_still_records(monkeypatch, caplog):
    async def go(env):
        env.guild.invites_error = forbidden()
        await env.cog.on_ready()
        await env.join(C)
        await env.join(D)
        rows = await env.rows("joins")
        assert [(r["user_id"], r["inviter_id"], r["invite_code"]) for r in rows] == [(C, None, None), (D, None, None)]
        warnings = [r for r in caplog.records if "Manage Server" in r.getMessage()]
        assert len(warnings) == 1
    with_env(go, monkeypatch)


def test_listener_errors_do_not_propagate(monkeypatch, caplog):
    async def go(env):
        env.guild.invites_error = RuntimeError("boom")
        await env.join(C)  # must not raise
        assert await env.rows("joins") == []
        assert any("recording join" in r.getMessage() for r in caplog.records)
        await env.cog.on_message(SimpleNamespace(author=None))  # malformed: logged, not raised
    with_env(go, monkeypatch)


def test_leave_sets_left_at_on_latest_open_row(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A, uses=0)
        await env.cog.on_ready()
        await env.join(C, via="aaa", at=T0)
        await env.leave(C, at=T0 + DAY)
        await env.join(C, via="aaa", at=T0 + 2 * DAY)
        await env.leave(C, at=T0 + 3 * DAY)
        rows = await env.rows("joins")
        assert [(r["joined_at"], r["left_at"]) for r in rows] == [(T0, T0 + DAY), (T0 + 2 * DAY, T0 + 3 * DAY)]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /invites, /recruiters, Recruiter role
def test_invites_command_three_day_rule(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A, uses=0)
        await env.cog.on_ready()
        await env.join(C, via="aaa", at=T0)
        await env.join(D, via="aaa", at=T0 + 10)
        await env.leave(D, at=T0 + DAY)
        await env.join(E, via="aaa", at=T0 + 2 * DAY)
        env.t = T0 + 3 * DAY + 100
        inter = env.inter(A)
        await Growth.invites.callback(env.cog, inter, None)
        desc = inter.sent()[0]["embed"].description
        assert "`INVITED`  3" in desc and "`STILL HERE`  2" in desc and "`STAYED 3D+`  1" in desc
    with_env(go, monkeypatch)


def test_recruiters_board_past_30_days(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A)
        env.guild.invite("bbb", B)
        await env.cog.on_ready()
        await env.join(C, via="aaa", at=T0)
        await env.join(D, via="bbb", at=T0 + 1)
        await env.join(E, via="bbb", at=T0 + 2)
        env.t = T0 + 5 * DAY
        inter = env.inter(OWNER)
        await Growth.recruiters.callback(env.cog, inter)
        lines = inter.sent()[0]["embed"].description.splitlines()
        assert lines == [f"`01`  <@{B}>  2 stayed", f"`02`  <@{A}>  1 stayed"]
        env.t = T0 + 40 * DAY  # all outside the window now
        inter = env.inter(OWNER)
        await Growth.recruiters.callback(env.cog, inter)
        assert inter.sent()[0]["embed"].description == "Nobody's on the board yet."
    with_env(go, monkeypatch)


def test_recruiter_role_at_threshold_and_never_removed(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A)
        env.member(A)
        await env.cog.on_ready()
        for uid in (C, D):
            await env.join(uid, via="aaa", at=T0)
        env.t = T0 + 4 * DAY
        assert await env.cog.award_recruiters() == 0  # 2 stayed
        await env.join(E, via="aaa", at=T0 + 4 * DAY)
        assert env.guild.recruiter not in env.member(A).roles  # E hasn't stayed yet
        env.t = T0 + 8 * DAY
        assert await env.cog.award_recruiters() == 1
        assert env.guild.recruiter in env.member(A).roles
        await env.leave(C, at=T0 + 9 * DAY)
        await env.leave(D, at=T0 + 9 * DAY)
        assert await env.cog.award_recruiters() == 0
        assert env.guild.recruiter in env.member(A).roles
    with_env(go, monkeypatch)


def test_recruiter_role_skipped_when_above_bot_or_missing(monkeypatch):
    async def go(env):
        env.guild.invite("aaa", A)
        env.member(A)
        await env.cog.on_ready()
        for uid in (C, D, E):
            await env.join(uid, via="aaa", at=T0)
        env.t = T0 + 4 * DAY
        env.guild.recruiter.position = 20  # above the bot's top role
        assert await env.cog.award_recruiters() == 0
        env.guild.roles.remove(env.guild.recruiter)
        assert await env.cog.award_recruiters() == 0
        assert env.member(A).roles == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- bumps
def test_disboard_bump_recorded_and_reminder_scheduled(monkeypatch):
    async def go(env):
        assert await env.cog.check_bump(disboard_message(env.guild, user=A))
        assert [(r["user_id"], r["at"]) for r in await env.rows("bumps")] == [(A, T0)]
        assert await env.due() == T0 + 2 * 3600
    with_env(go, monkeypatch)


def test_other_bot_and_non_bump_ignored(monkeypatch):
    async def go(env):
        assert not await env.cog.check_bump(disboard_message(env.guild, author=777))
        assert not await env.cog.check_bump(disboard_message(env.guild, name="help"))
        assert not await env.cog.check_bump(disboard_message(env.guild, embeds=("Please wait another 30 minutes",)))
        assert await env.rows("bumps") == []
        assert await env.due() is None
    with_env(go, monkeypatch)


def test_bump_without_readable_embed_still_counts(monkeypatch, caplog):
    async def go(env):
        caplog.set_level("DEBUG", logger=cogmod.log.name)
        await env.cog.on_message(disboard_message(env.guild, embeds=()))
        assert len(await env.rows("bumps")) == 1
        assert await env.due() == T0 + 2 * 3600
        assert any(r.levelname == "DEBUG" and "embed" in r.getMessage() for r in caplog.records)
    with_env(go, monkeypatch)


def test_reminder_survives_restart_and_posts_once(monkeypatch, tmp_path):
    clock = {"t": T0}
    monkeypatch.setattr(cogmod, "now", lambda: clock["t"])
    path = str(tmp_path / "growth.db")

    async def go():
        guild = FakeGuild()
        db = await open_db(path)
        cog = Growth(FakeBot(db, guild))
        await cog.check_bump(disboard_message(guild, user=A))
        clock["t"] = T0 + 3600
        assert not await cog.send_due_reminder()  # not yet
        await db.close()  # "restart"

        db = await open_db(path)
        cog = Growth(FakeBot(db, guild))
        clock["t"] = T0 + 2 * 3600 + 30
        await cog.bump_reminder.coro(cog)  # the loop body
        await cog.bump_reminder.coro(cog)
        assert len(guild.bot_commands.sent) == 1
        sent = guild.bot_commands.sent[0]
        assert sent["content"] == f"{guild.bumper.mention} {REMINDER_TEXT}"
        am = sent["allowed_mentions"]
        assert am.roles == [guild.bumper] and am.users is False and am.everyone is False
        row = await db.fetchone("SELECT value FROM meta WHERE key = ?", (BUMP_DUE_KEY,))
        assert row is None
        await db.close()
    run(go())


def test_reminder_without_role_posts_without_ping(monkeypatch):
    async def go(env):
        env.guild.roles.remove(env.guild.bumper)
        await env.cog.check_bump(disboard_message(env.guild))
        env.t += 2 * 3600
        assert await env.cog.send_due_reminder()
        sent = env.guild.bot_commands.sent[0]
        assert sent["content"] == REMINDER_TEXT
        assert sent["allowed_mentions"].roles is False
    with_env(go, monkeypatch)


def test_reminder_retries_after_http_error(monkeypatch):
    async def go(env):
        await env.cog.check_bump(disboard_message(env.guild))
        env.t += 2 * 3600
        env.guild.bot_commands.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "oops")
        await env.cog.bump_reminder.coro(env.cog)  # logged, not raised
        assert await env.due() is not None
        env.guild.bot_commands.fail = None
        await env.cog.bump_reminder.coro(env.cog)
        assert len(env.guild.bot_commands.sent) == 1
        assert await env.due() is None
    with_env(go, monkeypatch)


def test_new_bump_replaces_pending_reminder(monkeypatch):
    async def go(env):
        await env.cog.check_bump(disboard_message(env.guild, user=A))
        env.t += 2 * 3600 + 60
        await env.cog.check_bump(disboard_message(env.guild, user=B))
        assert not await env.cog.send_due_reminder()
        assert await env.due() == env.t + 2 * 3600
    with_env(go, monkeypatch)


def test_bumpers_board(monkeypatch):
    async def go(env):
        for user, at in ((A, T0), (A, T0 + 3 * 3600), (B, T0 + 6 * 3600), (C, T0 - 40 * DAY)):
            env.t = at
            await env.cog.check_bump(disboard_message(env.guild, user=user))
        env.t = T0 + DAY
        inter = env.inter(OWNER)
        await Growth.bumpers.callback(env.cog, inter)
        lines = inter.sent()[0]["embed"].description.splitlines()
        assert lines == [f"`01`  <@{A}>  2 bumps", f"`02`  <@{B}>  1 bumps"]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /bumpping
def ping(value):
    return app_commands.Choice(name=value, value=value)


def test_bumpping_on_and_off(monkeypatch):
    async def go(env):
        inter = env.inter(A)
        await Growth.bumpping.callback(env.cog, inter, ping("on"))
        assert env.guild.bumper in env.member(A).roles
        assert inter.sent()[0]["ephemeral"] is True
        inter = env.inter(A)
        await Growth.bumpping.callback(env.cog, inter, ping("off"))
        assert env.guild.bumper not in env.member(A).roles
        assert inter.sent()[0]["content"] == "Bump pings are off."
    with_env(go, monkeypatch)


def test_bumpping_missing_role(monkeypatch):
    async def go(env):
        env.guild.roles.remove(env.guild.bumper)
        inter = env.inter(A)
        await Growth.bumpping.callback(env.cog, inter, ping("on"))
        reply = inter.sent()[0]
        assert "no Bumper role" in reply["content"] and reply["ephemeral"] is True
    with_env(go, monkeypatch)


def test_bumpping_role_above_bot_or_forbidden(monkeypatch):
    async def go(env):
        env.guild.bumper.position = 20
        inter = env.inter(A)
        await Growth.bumpping.callback(env.cog, inter, ping("on"))
        assert "can't hand out" in inter.sent()[0]["content"]
        assert env.member(A).roles == []

        env.guild.bumper.position = 5
        env.member(A).add_fail = forbidden()
        inter = env.inter(A)
        await Growth.bumpping.callback(env.cog, inter, ping("on"))
        assert "can't hand out" in inter.sent()[0]["content"]
    with_env(go, monkeypatch)


def test_setup_loads_without_members_intent(monkeypatch):
    async def go(env):
        env.bot.intents.members = False
        added = []
        env.bot.add_cog = lambda cog: added.append(cog) or asyncio.sleep(0)
        await cogmod.setup(env.bot)
        assert isinstance(added[0], Growth)
    with_env(go, monkeypatch)
