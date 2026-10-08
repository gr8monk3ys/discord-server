"""Offline tests for cogs.partners: a real in-memory SQLite database plus small fakes
for the bot, guild, channels, members, interactions and the invite endpoint.
No network. Time is controlled by patching cogs.partners.now."""

import asyncio
import json
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

import config
import db as dbmod
import style
from cogs import partners as cogmod
from cogs.partners import ApplyModal, PartnerButton, Partners
from logic import partners as P

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, A, B, MOD, KEEPER = 1, 11, 12, 20, 21
T0 = 1_790_000_000
DAY = 24 * 60 * 60
CODE = "cozyCorner1"
NONE = discord.AllowedMentions.none().to_dict()
DESC = "A chill gaming community for night owls, weekend squads and co-op fans."


def run(coro):
    return asyncio.run(coro)


def http_error(cls=discord.HTTPException, status=500):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


def invite_body(**kw) -> bytes:
    data = {"code": CODE, "type": 0, "expires_at": None,
            "guild": {"id": "123456", "name": "Cozy Corner"}, "approximate_member_count": 250}
    data.update(kw)
    return json.dumps(data).encode()


# ---------------------------------------------------------------- fakes
class FakeInvites:
    def __init__(self):
        self.answers = {}
        self.calls = []

    def set(self, code, status=200, raw=None, error=None, **kw):
        self.answers[code] = (status, raw if raw is not None else invite_body(code=code, **kw), error)

    async def fetch(self, code):
        self.calls.append(code)
        status, raw, error = self.answers.get(code, (404, b'{"code": 10006}', None))
        if error is not None:
            raise error
        return status, raw


class FakePartial:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid

    async def edit(self, **kwargs):
        if self.channel.edit_fail is not None:
            raise self.channel.edit_fail
        self.channel.edits.append((self.id, kwargs))

    async def delete(self):
        if self.channel.delete_fail is not None:
            raise self.channel.delete_fail
        self.channel.deleted.append(self.id)


class FakeText:
    _next = 300

    def __init__(self, name):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.mention = f"<#{self.id}>"
        self.sent, self.edits, self.deleted = [], [], []
        self.fail = self.edit_fail = self.delete_fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))
        return SimpleNamespace(id=9000 + len(self.sent), embeds=[kwargs.get("embed")])

    def get_partial_message(self, mid):
        return FakePartial(self, mid)


class Named:
    _next = 5000

    def __init__(self, name):
        Named._next += 1
        self.id = Named._next
        self.name = name
        self.mention = f"<@&{self.id}>"


class FakeMember:
    def __init__(self, uid, guild, roles=(), admin=False):
        self.id = uid
        self.bot = False
        self.guild = guild
        self.name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.roles = [guild.role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.dms = []
        self.dm_fail = None

    async def send(self, content=None, **kwargs):
        if self.dm_fail is not None:
            raise self.dm_fail
        self.dms.append(dict(content=content, **kwargs))


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.name = "Lorenzo's Server"
        self.owner_id = OWNER
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.partners = FakeText(config.PARTNERS_CHANNEL)
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.text_channels = [self.general, self.mod_log, self.partners]
        self.roles = [Named(n) for n in ["@everyone", config.MOD_ROLE, config.KEEPER_ROLE]]
        self.members = {}

    def role(self, name):
        return config.match_by_name(self.roles, name)

    def get_member(self, uid):
        return self.members.get(uid)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.dynamic = set()
        self.cogs = {}
        self.fetched = []

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def get_cog(self, name):
        return self.cogs.get(name)

    def add_dynamic_items(self, *items):
        self.dynamic.update(items)

    def remove_dynamic_items(self, *items):
        self.dynamic.difference_update(items)

    async def fetch_user(self, uid):
        self.fetched.append(uid)
        raise http_error(discord.NotFound, 404)


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls
        self.done = False

    def _finish(self):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self._finish()
        self.calls.append(("send_message", dict(content=content, **kwargs)))

    async def edit_message(self, **kwargs):
        self._finish()
        self.calls.append(("edit_message", kwargs))

    async def defer(self, **kwargs):
        self._finish()
        self.calls.append(("defer", kwargs))

    async def send_modal(self, modal):
        self._finish()
        self.calls.append(("send_modal", {"modal": modal}))

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))


class FakeCard:
    """The review card message a button lives on."""

    def __init__(self, embed):
        self.embeds = [embed] if embed is not None else []
        self.edits = []
        self.fail = None

    async def edit(self, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.edits.append(kwargs)


class FakeInteraction:
    def __init__(self, bot, user, message=None):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.message = message
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]

    def texts(self):
        return [kw.get("content") for k, kw in self.calls if k in ("send_message", "followup")]


class Env:
    def __init__(self, db, guild, bot, cog, invites):
        self.db, self.guild, self.bot, self.cog, self.invites = db, guild, bot, cog, invites
        self.t = T0

    def member(self, uid, **kw):
        if uid not in self.guild.members:
            self.guild.members[uid] = FakeMember(uid, self.guild, **kw)
        return self.guild.members[uid]

    def inter(self, uid, **kw):
        return FakeInteraction(self.bot, self.member(uid), **kw)

    async def rows(self):
        return [dict(r) for r in await self.db.fetchall("SELECT * FROM partners ORDER BY id")]

    async def apply(self, uid, name="Cozy Corner", invite=f"https://discord.gg/{CODE}", desc=DESC):
        """/partner apply -> modal -> submit, as Discord would drive it."""
        inter = self.inter(uid)
        await Partners.apply.callback(self.cog, inter)
        modals = inter.of("send_modal")
        if not modals:
            return inter
        modal = modals[0]["modal"]
        modal.server_name._value, modal.invite._value, modal.description._value = name, invite, desc
        submit = self.inter(uid)
        await modal.on_submit(submit)
        return submit

    def card(self, index=-1):
        return FakeCard(self.guild.mod_log.sent[index]["embed"])

    async def press(self, uid, action, app_id, card=None):
        inter = self.inter(uid, message=card or self.card())
        await self.cog.handle_button(inter, action, app_id)
        return inter

    async def remove(self, uid, app_id):
        inter = self.inter(uid)
        await Partners.remove.callback(self.cog, inter, app_id)
        return inter


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        invites = FakeInvites()
        invites.set(CODE)
        slept = []

        async def pause(seconds):
            slept.append(seconds)

        cog = Partners(bot, invites=invites, pause=pause)
        bot.cogs["Partners"] = cog
        env = Env(db, guild, bot, cog, invites)
        env.slept = slept
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        env.member(MOD, roles=[config.MOD_ROLE])
        env.member(KEEPER, roles=[config.KEEPER_ROLE])
        env.member(A)
        env.member(B)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def test_button_template_round_trips():
    b = PartnerButton("approve", 42)
    assert b.item.custom_id == "partner:approve:42"
    m = re.fullmatch(PartnerButton.__discord_ui_compiled_template__, "partner:deny:7")
    assert m and m["action"] == "deny" and m["id"] == "7"
    assert re.fullmatch(PartnerButton.__discord_ui_compiled_template__, "partner:nuke:7") is None


# ---------------------------------------------------------------- applying
def test_apply_posts_review_card(monkeypatch):
    async def go(env):
        submit = await env.apply(A)
        assert env.invites.calls == [CODE]
        assert "application is in" in submit.texts()[-1]
        (row,) = await env.rows()
        assert row["status"] == "pending" and row["user_id"] == A and row["invite_code"] == CODE
        assert row["description"] == DESC
        (card,) = env.guild.mod_log.sent
        assert card["allowed_mentions"].to_dict() == NONE
        assert card["embed"].title == f"Partner application #{row['id']}"
        assert "250" in card["embed"].description and f"<https://discord.gg/{CODE}>" in card["embed"].description
        ids = [c.item.custom_id for c in card["view"].children]
        assert ids == [f"partner:approve:{row['id']}", f"partner:deny:{row['id']}"]
        assert row["review_message_id"] == 9001
    with_env(go, monkeypatch)


def test_modal_has_three_fields():
    async def go():
        modal = ApplyModal(SimpleNamespace())
        assert modal.description.max_length == P.DESC_MAX
        assert len(modal.children) == 3
    run(go())


def test_description_is_one_paragraph_and_escaped(monkeypatch):
    async def go(env):
        await env.apply(A, name="**Cozy** [x](https://evil.example)",
                        desc="Line one about the server\n\nline two with __style__ and [a](https://evil.example)")
        (row,) = await env.rows()
        assert "\n" not in row["description"]
        text = env.guild.mod_log.sent[0]["embed"].description
        assert "\\*\\*Cozy\\*\\*" in text and "\\_\\_style\\_\\_" in text
        assert "\\[x](" in text and "\\[a](" in text  # escaped, so masked links don't render
    with_env(go, monkeypatch)


@pytest.mark.parametrize("kw, problem", [
    ({"invite": "https://evil.example/abc"}, "invite_format"),
    ({"invite": "not a code"}, "invite_format"),
    ({"desc": "Come hang out with us @everyone, we play everything!"}, "description_mentions"),
    ({"desc": "Ask <@&123> for a role when you join, we play everything!"}, "description_mentions"),
    ({"desc": "We are cool, also join discord.gg/another1 for more fun stuff"}, "description_invite"),
    ({"name": "Cozy @here"}, "name_mentions"),
])
def test_bad_input_is_rejected_before_any_lookup(monkeypatch, kw, problem):
    async def go(env):
        submit = await env.apply(A, **kw)
        assert submit.texts() == [P.reply(problem)]
        assert env.invites.calls == [] and await env.rows() == [] and env.guild.mod_log.sent == []
    with_env(go, monkeypatch)


@pytest.mark.parametrize("answer, problem", [
    (dict(status=404, raw=b'{"code": 10006}'), "not_found"),
    (dict(expires_at="2026-10-08T00:00:00+00:00"), "temporary"),
    (dict(approximate_member_count=5), "small"),
    (dict(guild={"id": str(GUILD_ID), "name": "Us"}), "self"),
    (dict(status=429, raw=b"{}"), "unreachable"),
    (dict(error=asyncio.TimeoutError()), "unreachable"),
])
def test_invite_problems_are_rejected(monkeypatch, answer, problem):
    async def go(env):
        env.invites.set(CODE, **answer)
        submit = await env.apply(A)
        assert submit.texts()[-1] == P.reply(problem)
        assert await env.rows() == [] and env.guild.mod_log.sent == []
    with_env(go, monkeypatch)


def test_bare_code_works(monkeypatch):
    async def go(env):
        await env.apply(A, invite=CODE)
        assert len(await env.rows()) == 1
    with_env(go, monkeypatch)


def test_one_pending_application_per_member(monkeypatch):
    async def go(env):
        await env.apply(A)
        env.invites.set("other123")
        again = await env.apply(A, invite="discord.gg/other123")
        assert again.texts() == [P.reply("pending")] and not again.of("send_modal")
        assert len(await env.rows()) == 1
    with_env(go, monkeypatch)


def test_double_submit_of_an_open_modal_is_caught(monkeypatch):
    async def go(env):
        first, second = env.inter(A), env.inter(A)
        modal = ApplyModal(env.cog)
        modal.server_name._value, modal.invite._value, modal.description._value = "Cozy", CODE, DESC
        await modal.on_submit(first)
        await modal.on_submit(second)
        assert second.texts()[-1] == P.reply("pending")
        assert len(await env.rows()) == 1
    with_env(go, monkeypatch)


def test_same_invite_from_someone_else_is_a_duplicate(monkeypatch):
    async def go(env):
        await env.apply(A)
        submit = await env.apply(B)
        assert submit.texts()[-1] == P.reply("duplicate")
        assert len(await env.rows()) == 1
    with_env(go, monkeypatch)


def test_no_mod_log_drops_the_application(monkeypatch):
    async def go(env):
        env.guild.text_channels.remove(env.guild.mod_log)
        submit = await env.apply(A)
        assert "closed right now" in submit.texts()[-1]
        assert await env.rows() == []
    with_env(go, monkeypatch)


def test_failed_review_card_drops_the_application(monkeypatch):
    async def go(env):
        env.guild.mod_log.fail = http_error()
        with pytest.raises(discord.HTTPException):
            await env.apply(A)
        assert await env.rows() == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- reviewing
def test_only_staff_can_press(monkeypatch):
    async def go(env):
        await env.apply(A)
        inter = await env.press(B, "approve", 1)
        assert inter.texts() == [cogmod.STAFF_ONLY]
        assert (await env.rows())[0]["status"] == "pending"
    with_env(go, monkeypatch)


def test_approve_posts_partner_and_dms(monkeypatch):
    async def go(env):
        await env.apply(A, name="Cozy Corner")
        env.t += 3600
        card = env.card()
        inter = await env.press(MOD, "approve", 1, card=card)
        (row,) = await env.rows()
        assert row["status"] == "approved" and row["decided_by"] == MOD and row["decided_at"] == env.t
        (post,) = env.guild.partners.sent
        assert post["allowed_mentions"].to_dict() == NONE
        e = post["embed"]
        assert e.title == "Cozy Corner" and f"https://discord.gg/{CODE}" in e.description
        assert "250 members" in e.description and DESC in e.description
        assert e.footer.text == style.label("partner", "#1")
        assert row["post_message_id"] == 9001
        (edit,) = card.edits
        assert all(c.item.disabled for c in edit["view"].children)
        assert edit["embed"].footer.text == style.label("partner", "approved")
        assert "Approved and posted" in inter.texts()[-1]
        (dm,) = env.guild.members[A].dms
        assert "Approved" in dm["content"] and "Lorenzo's Server" in dm["content"]
        assert dm["allowed_mentions"].to_dict() == NONE
    with_env(go, monkeypatch)


def test_approve_rechecks_the_invite(monkeypatch):
    async def go(env):
        await env.apply(A)
        env.invites.set(CODE, status=404, raw=b"{}")
        inter = await env.press(MOD, "approve", 1)
        assert P.reply("not_found") in inter.texts()[-1]
        assert (await env.rows())[0]["status"] == "pending"
        assert env.guild.partners.sent == []
    with_env(go, monkeypatch)


def test_approve_without_partners_channel_stays_pending(monkeypatch):
    async def go(env):
        await env.apply(A)
        env.guild.text_channels.remove(env.guild.partners)
        inter = await env.press(KEEPER, "approve", 1)
        assert "no" in inter.texts()[-1].lower() and config.PARTNERS_CHANNEL in inter.texts()[-1]
        assert (await env.rows())[0]["status"] == "pending"
    with_env(go, monkeypatch)


def test_failed_partner_post_reverts_to_pending(monkeypatch):
    async def go(env):
        await env.apply(A)
        env.guild.partners.fail = http_error()
        with pytest.raises(discord.HTTPException):
            await env.press(MOD, "approve", 1)
        row = (await env.rows())[0]
        assert row["status"] == "pending" and row["decided_by"] is None
    with_env(go, monkeypatch)


def test_card_edit_or_dm_failure_doesnt_undo_approval(monkeypatch):
    async def go(env):
        await env.apply(A)
        env.guild.members[A].dm_fail = http_error(discord.Forbidden, 403)
        card = env.card()
        card.fail = http_error()
        inter = await env.press(MOD, "approve", 1, card=card)
        assert (await env.rows())[0]["status"] == "approved"
        assert "Approved and posted" in inter.texts()[-1]
    with_env(go, monkeypatch)


def test_deny_dms_and_starts_cooldown(monkeypatch):
    async def go(env):
        await env.apply(A)
        inter = await env.press(MOD, "deny", 1)
        (row,) = await env.rows()
        assert row["status"] == "denied" and row["decided_at"] == T0
        (edit,) = inter.of("edit_message")
        assert all(c.item.disabled for c in edit["view"].children)
        assert edit["embed"].footer.text == style.label("partner", "denied")
        assert any(f.name == "Denied by" for f in edit["embed"].fields)
        (dm,) = env.guild.members[A].dms
        assert "30 days" in dm["content"]
        env.t = T0 + 10 * DAY
        again = await env.apply(A)
        assert again.texts() == [P.reply("cooldown", 20)]
        env.t = T0 + 30 * DAY
        await env.apply(A)
        assert [r["status"] for r in await env.rows()] == ["denied", "pending"]
    with_env(go, monkeypatch)


def test_deny_for_member_who_left_is_fine(monkeypatch):
    async def go(env):
        await env.apply(A)
        del env.guild.members[A]
        await env.press(MOD, "deny", 1)
        assert env.bot.fetched == [A]
        assert (await env.rows())[0]["status"] == "denied"
    with_env(go, monkeypatch)


def test_pressing_a_decided_card(monkeypatch):
    async def go(env):
        await env.apply(A)
        await env.press(MOD, "deny", 1)
        inter = await env.press(KEEPER, "approve", 1)
        assert inter.texts() == ["This application was already denied."]
        inter = await env.press(KEEPER, "approve", 77)
        assert inter.texts() == ["That application is gone."]
    with_env(go, monkeypatch)


def test_owner_and_admin_are_staff(monkeypatch):
    async def go(env):
        env.member(OWNER)
        env.member(30, admin=True)
        await env.apply(A)
        await env.press(OWNER, "deny", 1)
        assert (await env.rows())[0]["status"] == "denied"
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- removing
def test_remove_deletes_post(monkeypatch):
    async def go(env):
        await env.apply(A)
        await env.press(MOD, "approve", 1)
        denied = await env.remove(B, 1)
        assert denied.texts() == [cogmod.STAFF_ONLY]
        inter = await env.remove(MOD, 1)
        assert "Removed partner #1" in inter.texts()[0]
        assert env.guild.partners.deleted == [9001]
        assert (await env.rows())[0]["status"] == "removed"
        # a removed partner doesn't block a new application for the same server
        env.invites.set(CODE)
        await env.apply(B)
        assert len(await env.rows()) == 2
    with_env(go, monkeypatch)


def test_remove_unknown_or_pending(monkeypatch):
    async def go(env):
        assert "no partner #5" in (await env.remove(MOD, 5)).texts()[0]
        await env.apply(A)
        assert "Deny" in (await env.remove(MOD, 1)).texts()[0]
    with_env(go, monkeypatch)


def test_remove_when_post_cant_be_deleted(monkeypatch):
    async def go(env):
        await env.apply(A)
        await env.press(MOD, "approve", 1)
        env.guild.partners.delete_fail = http_error(discord.Forbidden, 403)
        inter = await env.remove(MOD, 1)
        assert "by hand" in inter.texts()[0]
        assert (await env.rows())[0]["status"] == "removed"
        env.guild.partners.delete_fail = http_error(discord.NotFound, 404)
        await env.db.execute("UPDATE partners SET status = 'approved'")
        assert "by hand" not in (await env.remove(MOD, 1)).texts()[0]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- weekly sweep
async def approved(env, code=CODE, uid=A):
    env.invites.set(code)
    await env.apply(uid, invite=code)
    row = (await env.db.fetchall("SELECT id FROM partners ORDER BY id DESC LIMIT 1"))[0]
    await env.press(MOD, "approve", row["id"])
    return row["id"]


def test_first_sweep_only_marks_the_week_done(monkeypatch):
    async def go(env):
        await approved(env)
        env.invites.set(CODE, status=404, raw=b"{}")
        await env.cog.sweep_loop.coro(env.cog)
        assert (await env.rows())[0]["status"] == "approved"
        assert len(await env.db.fetchall("SELECT * FROM jobs WHERE key LIKE 'partners:%'")) == 1
    with_env(go, monkeypatch)


def test_sweep_marks_dead_invites(monkeypatch):
    async def go(env):
        first = await approved(env)
        second = await approved(env, code="alive1234", uid=B)
        await env.cog.sweep_loop.coro(env.cog)  # first run: marks this week done
        env.invites.set(CODE, status=404, raw=b"{}")
        env.t += 7 * DAY
        sent_before = len(env.guild.mod_log.sent)
        await env.cog.sweep_loop.coro(env.cog)
        rows = {r["id"]: r for r in await env.rows()}
        assert rows[first]["status"] == "dead" and rows[second]["status"] == "approved"
        ((mid, edit),) = env.guild.partners.edits
        assert mid == rows[first]["post_message_id"]
        assert "discord.gg" not in edit["embed"].description and "expired" in edit["embed"].description
        assert edit["embed"].color.value == style.MUTED
        assert "expired" in env.guild.mod_log.sent[sent_before]["embed"].description
        assert env.slept == [cogmod.SWEEP_PAUSE]
        assert len(await env.db.fetchall("SELECT * FROM jobs WHERE key LIKE 'partners:%'")) == 2
        # ran once this week: a later tick does nothing
        env.invites.calls.clear()
        await env.cog.sweep_loop.coro(env.cog)
        assert env.invites.calls == []
    with_env(go, monkeypatch)


def test_shrunk_partner_isnt_dead(monkeypatch):
    async def go(env):
        await approved(env)
        await env.cog.sweep_loop.coro(env.cog)
        env.invites.set(CODE, approximate_member_count=3)
        env.t += 7 * DAY
        await env.cog.sweep_loop.coro(env.cog)
        assert (await env.rows())[0]["status"] == "approved"
    with_env(go, monkeypatch)


def test_unreachable_sweep_retries_next_tick(monkeypatch):
    async def go(env):
        await approved(env)
        await env.cog.sweep_loop.coro(env.cog)
        env.invites.set(CODE, error=aiohttp_error())
        env.t += 7 * DAY
        await env.cog.sweep_loop.coro(env.cog)
        assert len(await env.db.fetchall("SELECT * FROM jobs WHERE key LIKE 'partners:%'")) == 1  # not done
        env.invites.set(CODE, status=404, raw=b"{}")
        env.t += 600
        await env.cog.sweep_loop.coro(env.cog)
        assert (await env.rows())[0]["status"] == "dead"
        assert len(await env.db.fetchall("SELECT * FROM jobs WHERE key LIKE 'partners:%'")) == 2
    with_env(go, monkeypatch)


def aiohttp_error():
    return OSError("connection reset")


def test_dead_post_edit_failure_still_marks_dead(monkeypatch):
    async def go(env):
        await approved(env)
        await env.cog.sweep_loop.coro(env.cog)
        env.invites.set(CODE, status=404, raw=b"{}")
        env.guild.partners.edit_fail = http_error(discord.NotFound, 404)
        env.t += 7 * DAY
        await env.cog.sweep_loop.coro(env.cog)
        assert (await env.rows())[0]["status"] == "dead"
    with_env(go, monkeypatch)


def test_sweep_loop_never_raises(monkeypatch):
    async def go(env):
        async def boom(*a, **k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.db, "fetchone", boom)
        await env.cog.sweep_loop.coro(env.cog)  # logged, not raised
    with_env(go, monkeypatch)


def test_cog_load_registers_buttons(monkeypatch):
    async def go(env):
        monkeypatch.setattr(env.cog.sweep_loop, "start", lambda *a, **k: None)
        monkeypatch.setattr(env.cog.sweep_loop, "cancel", lambda *a, **k: None)
        await env.cog.cog_load()
        assert PartnerButton in env.bot.dynamic
        await env.cog.cog_unload()
        assert PartnerButton not in env.bot.dynamic
    with_env(go, monkeypatch)
