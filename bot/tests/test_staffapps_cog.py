"""Offline tests for cogs.staffapps: a real in-memory SQLite database plus small fakes
for the bot, guild, channels, members and interactions. No network. Time is
controlled by patching cogs.staffapps.now."""

import asyncio
import datetime as dt
import json
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
from cogs import staffapps as cogmod
from cogs.staffapps import ApplyModal, StaffAppButton, StaffApps, build_view
from logic import staffapps as S

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, A, B, MOD, KEEPER, ADMIN = 1, 11, 12, 20, 21, 22
T0 = 1_790_000_000
DAY = 24 * 60 * 60
NONE = discord.AllowedMentions.none().to_dict()
ANSWERS = {"age": "yes", "timezone": "Pacific, evenings", "experience": "Ran a 300 member server for a year.",
           "voice": "Split them into separate channels, then talk to each one calmly.",
           "why": "I'm here every day and want to help new people."}


def run(coro):
    return asyncio.run(coro)


def at(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc)


def http_error(cls=discord.HTTPException, status=500):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


# ---------------------------------------------------------------- fakes
class FakeText:
    _next = 300

    def __init__(self, name):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.mention = f"<#{self.id}>"
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))
        return SimpleNamespace(id=9000 + len(self.sent), embeds=[kwargs.get("embed")])


class Role:
    _next = 5000

    def __init__(self, name, position):
        Role._next += 1
        self.id = Role._next
        self.name = name
        self.position = position
        self.mention = f"<@&{self.id}>"


class FakeMember:
    def __init__(self, uid, guild, roles=(), admin=False, joined=T0 - 100 * DAY, created=T0 - 900 * DAY):
        self.id = uid
        self.bot = False
        self.guild = guild
        self.name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.roles = [guild.role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.joined_at = at(joined) if joined is not None else None
        self.created_at = at(created)
        self.timed_out = False
        self.dms = []
        self.dm_fail = None
        self.added = []
        self.add_fail = None

    def is_timed_out(self):
        return self.timed_out

    async def send(self, content=None, **kwargs):
        if self.dm_fail is not None:
            raise self.dm_fail
        self.dms.append(dict(content=content, **kwargs))

    async def add_roles(self, *roles, reason=None):
        if self.add_fail is not None:
            raise self.add_fail
        self.added.extend(roles)
        self.roles.extend(roles)

    def __str__(self):
        return self.name


class FakeGuild:
    def __init__(self, mod_position=3):
        self.id = GUILD_ID
        self.name = "Lorenzo's *Server*"
        self.owner_id = OWNER
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.text_channels = [self.general, self.mod_log]
        self.roles = [Role("@everyone", 0), Role(config.MOD_ROLE, mod_position), Role(config.KEEPER_ROLE, 6),
                      Role("Front Desk", 5)]
        self.me = SimpleNamespace(top_role=self.role("Front Desk"))
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

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def get_cog(self, name):
        return self.cogs.get(name)

    def add_dynamic_items(self, *items):
        self.dynamic.update(items)

    def remove_dynamic_items(self, *items):
        self.dynamic.difference_update(items)

    async def fetch_user(self, uid):
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
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0

    def member(self, uid, **kw):
        if uid not in self.guild.members:
            self.guild.members[uid] = FakeMember(uid, self.guild, **kw)
        return self.guild.members[uid]

    def inter(self, uid, **kw):
        return FakeInteraction(self.bot, self.member(uid), **kw)

    async def rows(self):
        return [dict(r) for r in await self.db.fetchall("SELECT * FROM staff_apps ORDER BY id")]

    async def apply(self, uid, **answers):
        """/apply staff -> modal -> submit, as Discord would drive it."""
        inter = self.inter(uid)
        await StaffApps.apply_staff.callback(self.cog, inter)
        modals = inter.of("send_modal")
        if not modals:
            return inter
        modal = modals[0]["modal"]
        for key, value in {**ANSWERS, **answers}.items():
            modal.inputs[key]._value = value
        submit = self.inter(uid)
        await modal.on_submit(submit)
        return submit

    def card(self, index=0):
        return FakeCard(self.guild.mod_log.sent[index]["embed"])

    async def press(self, uid, action, app_id, card=None):
        inter = self.inter(uid, message=card or self.card())
        await self.cog.handle_button(inter, action, app_id)
        return inter

    async def status(self, uid):
        inter = self.inter(uid)
        await StaffApps.apply_status.callback(self.cog, inter)
        return inter.texts()[-1]

    async def add_case(self, uid, kind, t):
        await self.db.execute("INSERT INTO cases (user_id, mod_id, kind, reason, at) VALUES (?, ?, ?, 'x', ?)",
                              (uid, MOD, kind, t))


def with_env(fn, monkeypatch, mod_position=3):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild(mod_position)
        bot = FakeBot(db, guild)
        cog = StaffApps(bot)
        bot.cogs["StaffApps"] = cog
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        env.member(OWNER)
        env.member(MOD, roles=[config.MOD_ROLE])
        env.member(KEEPER, roles=[config.KEEPER_ROLE])
        env.member(ADMIN, admin=True)
        env.member(A)
        env.member(B)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- wiring
def test_button_template_round_trips():
    b = StaffAppButton("interview", 42)
    assert b.item.custom_id == "staffapp:interview:42"
    pattern = StaffAppButton.__discord_ui_compiled_template__
    m = re.fullmatch(pattern, "staffapp:approve:7")
    assert m and m["action"] == "approve" and m["id"] == "7"
    assert re.fullmatch(pattern, "staffapp:nuke:7") is None


def test_view_states():
    def disabled(status):
        return [c.item.disabled for c in build_view(1, status).children]
    assert disabled("pending") == [False, False, False]
    assert disabled("interview") == [False, False, True]
    assert disabled("approved") == [True, True, True]
    assert disabled("denied") == [True, True, True]


def test_cog_load_registers_dynamic_items():
    async def go():
        bot = FakeBot(None, FakeGuild())
        cog = StaffApps(bot)
        await cog.cog_load()
        assert StaffAppButton in bot.dynamic
        await cog.cog_unload()
        assert StaffAppButton not in bot.dynamic
    run(go())


def test_modal_has_five_labelled_fields():
    async def go():
        modal = ApplyModal(SimpleNamespace())
        assert len(modal.children) == 5
        assert list(modal.inputs) == list(S.KEYS)
        assert modal.inputs["age"].style == discord.TextStyle.short
        assert modal.inputs["why"].style == discord.TextStyle.paragraph
    run(go())


def test_apps_group_is_staff_gated():
    perms = StaffApps.apps_group.default_permissions
    assert perms is not None and perms.moderate_members
    assert StaffApps.apply_group.default_permissions is None


# ---------------------------------------------------------------- applying
def test_apply_posts_review_card(monkeypatch):
    async def go(env):
        submit = await env.apply(A)
        assert "Thanks for applying" in submit.texts()[-1]
        (row,) = await env.rows()
        assert row["status"] == "pending" and row["user_id"] == A and row["review_message_id"] == 9001
        assert json.loads(row["answers"]) == ANSWERS
        (card,) = env.guild.mod_log.sent
        assert card["allowed_mentions"].to_dict() == NONE
        e = card["embed"]
        assert e.title == f"Staff application #{row['id']}"
        assert f"<@{A}>" in e.description and "900 days" in e.description and "100 days" in e.description
        assert [f.name for f in e.fields] == [q.label for q in S.QUESTIONS]
        ids = [c.item.custom_id for c in card["view"].children]
        assert ids == [f"staffapp:{a}:{row['id']}" for a in ("approve", "deny", "interview")]
    with_env(go, monkeypatch)


def test_answers_are_escaped(monkeypatch):
    async def go(env):
        await env.apply(A, why="**bold** [x](https://evil.example) @everyone <@1>")
        value = env.guild.mod_log.sent[0]["embed"].fields[-1].value
        assert "\\*\\*bold\\*\\*" in value and "\\[x](" in value
        assert "@everyone" not in value.replace("@​everyone", "")
    with_env(go, monkeypatch)


def test_age_must_be_yes(monkeypatch):
    async def go(env):
        submit = await env.apply(A, age="no")
        assert "18 or older" in submit.texts()[-1]
        assert await env.rows() == [] and env.guild.mod_log.sent == []
    with_env(go, monkeypatch)


def test_blank_answer_rejected(monkeypatch):
    async def go(env):
        submit = await env.apply(A, voice="   ")
        assert "every question" in submit.texts()[-1]
        assert await env.rows() == []
    with_env(go, monkeypatch)


def test_new_member_cant_apply(monkeypatch):
    async def go(env):
        env.member(B).joined_at = at(T0 - 5 * DAY)
        inter = await env.apply(B)
        assert inter.of("send_modal") == []
        assert "25 more days" in inter.texts()[-1]
    with_env(go, monkeypatch)


def test_new_account_cant_apply(monkeypatch):
    async def go(env):
        env.member(B).created_at = at(T0 - 30 * DAY)
        inter = await env.apply(B)
        assert "90 days old" in inter.texts()[-1]
    with_env(go, monkeypatch)


def test_timed_out_cant_apply(monkeypatch):
    async def go(env):
        env.member(B).timed_out = True
        assert "timed out" in (await env.apply(B)).texts()[-1]
    with_env(go, monkeypatch)


def test_recent_warning_blocks_old_one_doesnt(monkeypatch):
    async def go(env):
        await env.add_case(B, "warn", T0 - 10 * DAY)
        assert "60 days" in (await env.apply(B)).texts()[-1]
        await env.add_case(A, "warn", T0 - 61 * DAY)
        await env.add_case(A, "purge", T0 - DAY)  # not a mark against the member
        await env.apply(A)
        assert len(await env.rows()) == 1
    with_env(go, monkeypatch)


def test_recent_timeout_case_blocks(monkeypatch):
    async def go(env):
        await env.add_case(B, "auto_timeout", T0 - 5 * DAY)
        assert "60 days" in (await env.apply(B)).texts()[-1]
    with_env(go, monkeypatch)


def test_staff_cant_apply(monkeypatch):
    async def go(env):
        for uid in (MOD, KEEPER, OWNER, ADMIN):
            assert "already on the staff" in (await env.apply(uid)).texts()[-1]
        assert await env.rows() == []
    with_env(go, monkeypatch)


def test_one_open_application(monkeypatch):
    async def go(env):
        await env.apply(A)
        inter = await env.apply(A)
        assert "already have an application" in inter.texts()[-1]
        assert len(await env.rows()) == 1
    with_env(go, monkeypatch)


def test_double_submit_makes_one_row(monkeypatch):
    async def go(env):
        inter = env.inter(A)
        await StaffApps.apply_staff.callback(env.cog, inter)
        modal = inter.of("send_modal")[0]["modal"]
        for k, v in ANSWERS.items():
            modal.inputs[k]._value = v
        first, second = env.inter(A), env.inter(A)
        await modal.on_submit(first)
        await modal.on_submit(second)
        assert len(await env.rows()) == 1
        assert "already have an application" in second.texts()[-1]
    with_env(go, monkeypatch)


def test_no_mod_log_drops_application(monkeypatch):
    async def go(env):
        env.guild.text_channels = [env.guild.general]
        submit = await env.apply(A)
        assert "closed right now" in submit.texts()[-1]
        assert await env.rows() == []
    with_env(go, monkeypatch)


def test_card_send_failure_removes_row(monkeypatch):
    async def go(env):
        env.guild.mod_log.fail = http_error()
        try:
            await env.apply(A)
        except discord.HTTPException:
            pass
        assert await env.rows() == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- buttons: who
def test_moderator_cant_approve_or_deny(monkeypatch):
    async def go(env):
        await env.apply(A)
        for action in ("approve", "deny"):
            inter = await env.press(MOD, action, 1)
            assert "Only Keepers" in inter.texts()[-1]
        (row,) = await env.rows()
        assert row["status"] == "pending"
    with_env(go, monkeypatch)


def test_member_cant_press_anything(monkeypatch):
    async def go(env):
        await env.apply(A)
        assert "Only Moderators" in (await env.press(B, "interview", 1)).texts()[-1]
        assert "Only Keepers" in (await env.press(B, "approve", 1)).texts()[-1]
        assert "Only Keepers" in (await env.press(ADMIN, "approve", 1)).texts()[-1]
    with_env(go, monkeypatch)


def test_missing_application(monkeypatch):
    async def go(env):
        env.guild.mod_log.sent.append({"embed": None})
        inter = await env.press(KEEPER, "deny", 77)
        assert "gone" in inter.texts()[-1]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- interview
def test_moderator_interview(monkeypatch):
    async def go(env):
        await env.apply(A)
        env.t += 3600
        inter = await env.press(MOD, "interview", 1)
        (edit,) = inter.of("edit_message")
        assert edit["embed"].fields[-1].name == "Interview by" and edit["embed"].fields[-1].value == f"<@{MOD}>"
        assert [c.item.disabled for c in edit["view"].children] == [False, False, True]
        (row,) = await env.rows()
        assert row["status"] == "interview" and row["decided_by"] is None
        log_line = env.guild.mod_log.sent[-1]
        assert "interview" in log_line["embed"].description and f"<@{MOD}>" in log_line["embed"].description
        assert log_line["allowed_mentions"].to_dict() == NONE
        (dm,) = env.member(A).dms
        assert "reach out" in dm["content"] and "Lorenzo's \\*Server\\*" in dm["content"]
        # twice: refused
        again = await env.press(MOD, "interview", 1)
        assert "already" in again.texts()[-1]
    with_env(go, monkeypatch)


def test_interview_then_status_and_still_open(monkeypatch):
    async def go(env):
        await env.apply(A)
        await env.press(MOD, "interview", 1)
        assert "interview" in await env.status(A)
        assert "already have an application" in (await env.apply(A)).texts()[-1]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- deny
def test_keeper_deny_then_cooldown(monkeypatch):
    async def go(env):
        await env.apply(A)
        inter = await env.press(KEEPER, "deny", 1)
        (edit,) = inter.of("edit_message")
        assert all(c.item.disabled for c in edit["view"].children)
        assert edit["embed"].fields[-1].name == "Denied by"
        (row,) = await env.rows()
        assert row["status"] == "denied" and row["decided_by"] == KEEPER and row["decided_at"] == T0
        assert "Thanks for applying" in env.member(A).dms[-1]["content"]
        assert "denied" in env.guild.mod_log.sent[-1]["embed"].description
        env.t += 10 * DAY
        assert "50 days" in (await env.apply(A)).texts()[-1]
        assert "50 days" in await env.status(A)
        env.t = T0 + 60 * DAY
        await env.apply(A)
        assert len(await env.rows()) == 2
    with_env(go, monkeypatch)


def test_deny_after_interview_and_dm_closed(monkeypatch):
    async def go(env):
        await env.apply(A)
        await env.press(MOD, "interview", 1)
        env.member(A).dm_fail = http_error(discord.Forbidden, 403)
        await env.press(OWNER, "deny", 1)
        (row,) = await env.rows()
        assert row["status"] == "denied" and row["decided_by"] == OWNER
    with_env(go, monkeypatch)


def test_decided_cant_be_pressed_again(monkeypatch):
    async def go(env):
        await env.apply(A)
        await env.press(KEEPER, "deny", 1)
        inter = await env.press(KEEPER, "approve", 1)
        assert "already not accepted" in inter.texts()[-1]
        assert env.member(A).added == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- approve
def test_keeper_approve_gives_role_below_bot(monkeypatch):
    async def go(env):
        await env.apply(A)
        card = env.card()
        inter = await env.press(KEEPER, "approve", 1, card=card)
        assert env.member(A).added == [env.guild.role(config.MOD_ROLE)]
        (row,) = await env.rows()
        assert row["status"] == "approved" and row["decided_by"] == KEEPER
        assert "Gave them" in inter.texts()[-1]
        (edit,) = card.edits
        assert all(c.item.disabled for c in edit["view"].children)
        assert edit["embed"].fields[-1].name == "Approved by"
        assert "approved by" in env.guild.mod_log.sent[-1]["embed"].description
        assert "approved" in env.member(A).dms[-1]["content"]
        assert "approved" in await env.status(A)
        # now staff: can't apply again
        assert "already on the staff" in (await env.apply(A)).texts()[-1]
    with_env(go, monkeypatch)


def test_approve_role_above_bot_is_manual(monkeypatch):
    async def go(env):
        await env.apply(A)
        inter = await env.press(OWNER, "approve", 1)
        assert env.member(A).added == []
        assert "by hand" in inter.texts()[-1]
        assert "by hand" in env.guild.mod_log.sent[-1]["embed"].description
        (row,) = await env.rows()
        assert row["status"] == "approved"
        assert env.member(A).dms
    with_env(go, monkeypatch, mod_position=7)


def test_approve_role_equal_to_bot_is_manual(monkeypatch):
    async def go(env):
        await env.apply(A)
        await env.press(KEEPER, "approve", 1)
        assert env.member(A).added == []
    with_env(go, monkeypatch, mod_position=5)


def test_approve_add_roles_failure_still_approves(monkeypatch):
    async def go(env):
        await env.apply(A)
        env.member(A).add_fail = http_error(discord.Forbidden, 403)
        inter = await env.press(KEEPER, "approve", 1)
        assert "couldn't give" in inter.texts()[-1]
        (row,) = await env.rows()
        assert row["status"] == "approved"
    with_env(go, monkeypatch)


def test_approve_no_mod_role(monkeypatch):
    async def go(env):
        env.guild.roles = [r for r in env.guild.roles if r.name != config.MOD_ROLE]
        await env.apply(A)
        inter = await env.press(KEEPER, "approve", 1)
        assert "no Moderator role" in inter.texts()[-1]
    with_env(go, monkeypatch)


def test_approve_applicant_left(monkeypatch):
    async def go(env):
        await env.apply(A)
        del env.guild.members[A]
        inter = await env.press(KEEPER, "approve", 1)
        assert "aren't in the server" in inter.texts()[-1]
        (row,) = await env.rows()
        assert row["status"] == "pending"
    with_env(go, monkeypatch)


def test_approve_card_edit_failure_is_tolerated(monkeypatch):
    async def go(env):
        await env.apply(A)
        card = env.card()
        card.fail = http_error()
        inter = await env.press(KEEPER, "approve", 1, card=card)
        assert "Approved" in inter.texts()[-1]
    with_env(go, monkeypatch)


def test_race_lost_reports_already(monkeypatch):
    async def go(env):
        await env.apply(A)
        row = (await env.rows())[0]
        await env.db.execute("UPDATE staff_apps SET status = 'denied' WHERE id = 1")
        inter = env.inter(KEEPER, message=env.card())
        await env.cog.approve(inter, row)
        assert "already handled" in inter.texts()[-1]
        assert env.member(A).added == []
    with_env(go, monkeypatch)


def test_button_callback_errors_are_caught(monkeypatch):
    async def go(env):
        async def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "handle_button", boom)
        inter = env.inter(KEEPER)
        await StaffAppButton("approve", 1).callback(inter)
        assert "went wrong" in inter.texts()[-1]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- status and list
def test_status_none(monkeypatch):
    async def go(env):
        assert "haven't applied" in await env.status(A)
        env.member(B).joined_at = at(T0 - DAY)
        text = await env.status(B)
        assert "haven't applied" in text and "29 more days" in text
    with_env(go, monkeypatch)


def test_status_pending(monkeypatch):
    async def go(env):
        await env.apply(A)
        assert "pending" in await env.status(A)
    with_env(go, monkeypatch)


def test_apps_list(monkeypatch):
    async def go(env):
        await env.apply(A)
        await env.apply(B)
        await env.press(KEEPER, "deny", 1)
        inter = env.inter(MOD)
        await StaffApps.apps_list.callback(env.cog, inter)
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"] and sent["allowed_mentions"].to_dict() == NONE
        text = sent["embed"].description
        assert "#2" in text and "#1" not in text
        assert f"https://discord.com/channels/{GUILD_ID}/{env.guild.mod_log.id}/9002" in text
    with_env(go, monkeypatch)


def test_apps_list_staff_only(monkeypatch):
    async def go(env):
        inter = env.inter(A)
        await StaffApps.apps_list.callback(env.cog, inter)
        assert "Only Moderators" in inter.texts()[-1]
    with_env(go, monkeypatch)


def test_apps_list_empty(monkeypatch):
    async def go(env):
        inter = env.inter(KEEPER)
        await StaffApps.apps_list.callback(env.cog, inter)
        assert inter.of("send_message")[0]["embed"].description == "No open applications."
    with_env(go, monkeypatch)
