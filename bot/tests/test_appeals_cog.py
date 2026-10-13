"""Offline tests for cogs.appeals: a real in-memory SQLite database plus small fakes for
the bot, guild, the appeals channel, webhook messages and interactions. No network.
Time is controlled by patching cogs.appeals.now; settings come from the environment."""

import asyncio
import json
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
from cogs import appeals as cogmod
from cogs.appeals import AppealButton, Appeals, DenyModal, build_view
from logic import appeals as A
from logic.ops import latest_due_daily

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, MEMBER, MOD, BANMOD, KEEPER, BOT_ID = 1, 11, 20, 23, 21, 77
U1, U2 = 123456789012345678, 223456789012345678
OWNER_ID = 323456789012345678  # a real-looking ID, for mention tests
SECRET = "test-secret-for-appeal-vectors!!"
T0 = 1_790_000_000
DAY = 24 * 60 * 60
NONE = discord.AllowedMentions.none().to_dict()
ANSWERS = ["I spammed the chat.", "I read the rules again.", ""]


def run(coro):
    return asyncio.run(coro)


def http_error(cls=discord.HTTPException, status=500):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


def body(uid=U1, answers=None, code=None, **over):
    data = {"v": 1, "u": str(uid), "c": code or A.appeal_code(SECRET, uid), "a": answers or ANSWERS}
    data.update(over)
    return json.dumps(data)


# ---------------------------------------------------------------- fakes
class FakeCard:
    def __init__(self, mid, channel, embed, view):
        self.id = mid
        self.channel = channel
        self.embeds = [embed] if embed is not None else []
        self.view = view
        self.edits = []
        self.threads = []
        self.webhook_id = None

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]

    async def create_thread(self, name, auto_archive_duration=None):
        if self.channel.thread_fail is not None:
            raise self.channel.thread_fail
        thread = SimpleNamespace(id=self.id + 500_000, name=name, parent_id=self.channel.id)
        self.threads.append(thread)
        return thread


class FakeText:
    _next = 300

    def __init__(self, name):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.sent = []
        self.cards = {}
        self.messages = []  # what history() returns, oldest first
        self.fail = None
        self.thread_fail = None
        self.perms = dict(view_channel=True, read_message_history=True)

    def permissions_for(self, member):
        return SimpleNamespace(**self.perms)

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))
        card = FakeCard(9000 + len(self.sent), self, kwargs.get("embed"), kwargs.get("view"))
        self.cards[card.id] = card
        return card

    async def fetch_message(self, mid):
        if mid not in self.cards:
            raise http_error(discord.NotFound, 404)
        return self.cards[mid]

    def history(self, limit=50):
        newest_first = [m for m in reversed(self.messages) if not m.deleted][:limit]

        async def gen():
            for m in newest_first:
                yield m
        return gen()


class FakeWebhookMessage:
    _next = 70000

    def __init__(self, guild, channel, text, webhook_id=555, content=""):
        FakeWebhookMessage._next += 1
        self.id = FakeWebhookMessage._next
        self.guild = guild
        self.channel = channel
        self.webhook_id = webhook_id
        self.content = content
        self.embeds = [SimpleNamespace(description=text)] if text is not None else []
        self.author = SimpleNamespace(id=555, bot=True)
        self.deleted = False
        self.delete_fail = None

    async def delete(self):
        if self.delete_fail is not None:
            raise self.delete_fail
        self.deleted = True


class Role:
    def __init__(self, name, position):
        self.name, self.position = name, position


class FakeMember:
    def __init__(self, uid, guild, roles=(), admin=False, ban=False):
        self.id = uid
        self.bot = False
        self.guild = guild
        self.name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.roles = [guild.role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=admin, ban_members=ban)


class FakeUser:
    def __init__(self, uid, dm_fail=None):
        self.id = uid
        self.name = f"ban_*ned*{uid % 100}"
        self.mention = f"<@{uid}>"
        self.dms = []
        self.dm_fail = dm_fail

    async def send(self, content=None, **kwargs):
        if self.dm_fail is not None:
            raise self.dm_fail
        self.dms.append(dict(content=content, **kwargs))


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.name = "Test *Server*"
        self.owner_id = OWNER
        self.appeals = FakeText(config.APPEALS_CHANNEL)
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.text_channels = [self.general, self.mod_log, self.appeals]
        self.roles = [Role("@everyone", 0), Role(config.MOD_ROLE, 5), Role(config.KEEPER_ROLE, 6)]
        self.me = SimpleNamespace(id=BOT_ID, guild_permissions=SimpleNamespace(ban_members=True))
        self.banned = {U1, U2}
        self.ban_check_fail = None
        self.unbans = []
        self.unban_fail = None

    def role(self, name):
        return config.match_by_name(self.roles, name)

    async def fetch_ban(self, obj):
        if self.ban_check_fail is not None:
            raise self.ban_check_fail
        if obj.id not in self.banned:
            raise http_error(discord.NotFound, 404)
        return SimpleNamespace(user=obj)

    async def unban(self, obj, reason=None):
        if self.unban_fail is not None:
            raise self.unban_fail
        if obj.id not in self.banned:
            raise http_error(discord.NotFound, 404)
        self.banned.discard(obj.id)
        self.unbans.append((obj.id, reason))


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.dynamic = set()
        self.users = {}  # uid -> FakeUser; missing means the DM can't be delivered

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def get_cog(self, name):
        return getattr(self, "cog", None)

    def add_dynamic_items(self, *items):
        self.dynamic.update(items)

    def remove_dynamic_items(self, *items):
        self.dynamic.difference_update(items)

    async def wait_until_ready(self):
        await asyncio.sleep(3600)

    async def fetch_user(self, uid):
        if uid not in self.users:
            self.users[uid] = FakeUser(uid, dm_fail=http_error(discord.Forbidden, 403))
        return self.users[uid]


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
        self.members = {}

    def member(self, uid, **kw):
        if uid not in self.members:
            self.members[uid] = FakeMember(uid, self.guild, **kw)
        return self.members[uid]

    def inter(self, uid, **kw):
        return FakeInteraction(self.bot, self.member(uid), **kw)

    def post(self, text, channel=None, **kw):
        """A message arriving in the appeals channel (through the webhook unless told otherwise)."""
        channel = channel or self.guild.appeals
        m = FakeWebhookMessage(self.guild, channel, text, **kw)
        channel.messages.append(m)
        return m

    async def submit(self, uid=U1, **kw):
        m = self.post(body(uid, **kw))
        return m, await self.cog.ingest(m)

    async def rows(self):
        return [dict(r) for r in await self.db.fetchall("SELECT * FROM appeals ORDER BY id")]

    def cards(self):
        return list(self.guild.appeals.cards.values())

    def logs(self):
        return [s["embed"].description for s in self.guild.mod_log.sent]

    async def press(self, uid, action, appeal_id=1, card=None):
        inter = self.inter(uid, message=card or self.cards()[0])
        await self.cog.handle_button(inter, action, appeal_id)
        return inter

    async def deny(self, uid, appeal_id=1, reason=""):
        """Deny button -> modal -> submit, as Discord would drive it."""
        inter = await self.press(uid, "deny", appeal_id)
        modals = inter.of("send_modal")
        if not modals:
            return inter
        modal = modals[0]["modal"]
        modal.reason._value = reason
        submit = self.inter(uid, message=self.cards()[0])
        await modal.on_submit(submit)
        return submit

    async def ban_row(self, uid, at, source="command"):
        await self.db.execute("INSERT OR REPLACE INTO appeal_bans (user_id, banned_at, source) VALUES (?, ?, ?)",
                              (uid, at, source))

    def restart(self):
        self.cog = Appeals(self.bot)
        self.bot.cog = self.cog
        return self.cog


def with_env(fn, monkeypatch, secret=SECRET):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Appeals(bot)
        bot.cog = cog
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        for key in ("APPEAL_SECRET", "APPEAL_COOLING_DAYS", "APPEAL_REAPPEAL_DAYS", "SITE_URL"):
            monkeypatch.delenv(key, raising=False)
        if secret:
            monkeypatch.setenv("APPEAL_SECRET", secret)
        env.member(OWNER)
        env.member(MEMBER)
        env.member(MOD, roles=[config.MOD_ROLE])  # a Moderator without Ban Members
        env.member(BANMOD, roles=[config.MOD_ROLE], ban=True)
        env.member(KEEPER, roles=[config.KEEPER_ROLE], admin=True)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- wiring
def test_button_template_round_trips():
    b = AppealButton("deny", 42)
    assert b.item.custom_id == "appeal:deny:42"
    pattern = AppealButton.__discord_ui_compiled_template__
    m = re.fullmatch(pattern, "appeal:accept:7")
    assert m and m["action"] == "accept" and m["id"] == "7"
    assert re.fullmatch(pattern, "appeal:unban:7") is None and re.fullmatch(pattern, "appeal:accept:x") is None


def test_view_states():
    def disabled(status):
        return [c.item.disabled for c in build_view(1, status).children]
    assert disabled(A.OPEN) == [False, False]
    for status in (A.ACCEPTED, A.DENIED, A.EXPIRED, A.CLOSED):
        assert disabled(status) == [True, True]


def test_cog_load_registers_buttons_and_starts_the_loop():
    async def go():
        bot = FakeBot(None, FakeGuild())
        cog = Appeals(bot)
        await cog.cog_load()
        assert AppealButton in bot.dynamic and cog.tick.is_running()
        await cog.cog_unload()
        assert AppealButton not in bot.dynamic
    run(go())


def test_group_is_hidden_from_members_and_marked_staff_only():
    perms = Appeals.group.default_permissions
    assert perms is not None and perms.moderate_members
    assert all(c.description.endswith("(staff only)") for c in Appeals.group.commands)


# ---------------------------------------------------------------- ingestion: trust
def test_valid_appeal_becomes_a_card(monkeypatch):
    async def go(env):
        await env.ban_row(U1, T0 - 10 * DAY)
        await env.db.execute("INSERT INTO cases (user_id, mod_id, kind, reason, at) VALUES (?, ?, 'ban', ?, ?)",
                             (U1, MOD, "spam in *general*", T0 - 10 * DAY))
        m, verdict = await env.submit()
        assert verdict == "posted" and m.deleted
        (row,) = await env.rows()
        assert row["status"] == A.OPEN and row["user_id"] == U1 and row["source_message_id"] == m.id
        assert row["created_at"] == T0 and row["touched_at"] == T0
        assert json.loads(row["answers"]) == dict(zip(A.KEYS, ANSWERS))
        (sent,) = env.guild.appeals.sent
        assert sent["allowed_mentions"].to_dict() == NONE
        e = sent["embed"]
        assert e.title == "Ban appeal #1" and f"<@{U1}>" in e.description and f"`{U1}`" in e.description
        assert "with `/ban`" in e.description and f"<t:{T0 - 10 * DAY}:R>" in e.description
        assert "spam in \\*general\\*" in e.description and f"<@{MOD}>" in e.description
        assert "ban\\_\\*ned\\*" in e.description  # the fetched username is escaped too
        assert [f.name for f in e.fields] == [q.label for q in A.QUESTIONS]
        assert [f.value for f in e.fields] == ["I spammed the chat.", "I read the rules again.", "(blank)"]
        assert [c.item.custom_id for c in sent["view"].children] == ["appeal:accept:1", "appeal:deny:1"]
        (card,) = env.cards()
        assert row["card_message_id"] == card.id and row["thread_id"] == card.threads[0].id
        assert card.threads[0].name == "Appeal 1 discussion"
        assert env.logs() == []  # nothing to say in the mod log until someone decides
    with_env(go, monkeypatch)


def test_a_non_webhook_message_is_never_an_appeal(monkeypatch):
    async def go(env):
        forged = env.post(body(), webhook_id=None)  # a person pasting a perfectly valid payload
        assert await env.cog.ingest(forged) is None
        await env.cog.on_message(forged)
        assert not forged.deleted and await env.rows() == [] and env.guild.appeals.sent == []
        assert await env.cog.sweep() == 0 and await env.rows() == []
    with_env(go, monkeypatch)


def test_webhooks_in_other_channels_and_other_servers_are_ignored(monkeypatch):
    async def go(env):
        elsewhere = env.post(body(), channel=env.guild.general)
        assert await env.cog.ingest(elsewhere) is None and not elsewhere.deleted
        other = env.post(body())
        other.guild = SimpleNamespace(id=12345, text_channels=env.guild.text_channels)
        assert await env.cog.ingest(other) is None and not other.deleted
        assert await env.rows() == []
    with_env(go, monkeypatch)


def test_wrong_hmac_is_deleted_and_noted_once(monkeypatch):
    async def go(env):
        for text in (body(code="A" * 16), body(code=A.appeal_code(SECRET, U2)), body(code=A.appeal_code("x" * 20, U1)),
                     "not json", json.dumps({"v": 1}), body(v=9)):
            m = env.post(text)
            assert await env.cog.ingest(m) == "forged"
            assert m.deleted
        assert await env.rows() == [] and env.guild.appeals.sent == []
        (note,) = env.logs()
        assert "failed verification" in note and "webhook" in note
        assert str(U1) not in note  # a forged ID is not put in front of staff
    with_env(go, monkeypatch)


def test_feature_off_reads_nothing(monkeypatch):
    async def go(env):
        m, verdict = await env.submit()
        assert verdict is None and not m.deleted
        assert await env.cog.sweep() == 0
        assert await env.rows() == [] and env.guild.appeals.sent == [] and env.logs() == []
    with_env(go, monkeypatch, secret=None)


def test_unreadable_without_message_content_is_left_alone(monkeypatch):
    async def go(env):
        m = env.post(None)
        assert await env.cog.ingest(m) == "unreadable" and not m.deleted
        assert await env.cog.ingest(env.post(None)) == "unreadable"
        (note,) = env.logs()
        assert "Message Content" in note
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- ingestion: rules
def test_not_banned_is_dropped(monkeypatch):
    async def go(env):
        env.guild.banned.clear()
        m, verdict = await env.submit()
        assert verdict == "not_banned" and m.deleted
        assert await env.rows() == [] and env.guild.appeals.sent == []
        (note,) = env.logs()
        assert "aren't banned" in note and f"<@{U1}>" in note
    with_env(go, monkeypatch)


def test_one_open_appeal_and_spam_is_quiet(monkeypatch):
    async def go(env):
        await env.submit()
        for _ in range(25):  # a banned member hammering the form with their own valid code
            m, verdict = await env.submit(answers=["again", "and again", "!"])
            assert verdict == "open" and m.deleted
        assert len(await env.rows()) == 1 and len(env.guild.appeals.sent) == 1
        (note,) = env.logs()
        assert "already have one open" in note
        env.t += DAY
        await env.submit()
        assert len(env.logs()) == 2  # at most one note a day
        _, verdict = await env.submit(U2)  # someone else is unaffected
        assert verdict == "posted"
    with_env(go, monkeypatch)


def test_the_same_webhook_message_is_only_read_once(monkeypatch):
    async def go(env):
        m = env.post(body())
        m.delete_fail = http_error(discord.Forbidden, 403)  # no Manage Messages: it stays in the channel
        assert await env.cog.ingest(m) == "posted"
        assert await env.cog.ingest(m) is None
        assert await env.cog.sweep() == 0
        assert any("Manage Messages" in line for line in env.logs())
        # Decided, then the bot restarts and finds the old message again: still one appeal.
        await env.press(KEEPER, "accept")
        env.guild.banned.add(U1)
        env.restart()
        assert await env.cog.ingest(m) == "duplicate"
        assert len(await env.rows()) == 1 and len(env.guild.appeals.sent) == 1
    with_env(go, monkeypatch)


def test_cooling_off_period(monkeypatch):
    async def go(env):
        await env.ban_row(U1, T0 - 2 * DAY, "manual")
        m, verdict = await env.submit()
        assert verdict == "cooling" and m.deleted and await env.rows() == []
        assert "5 more days" in env.logs()[0]
        env.t = T0 + 5 * DAY
        _, verdict = await env.submit()
        assert verdict == "posted"
        assert "in Discord's own menu" in env.guild.appeals.sent[0]["embed"].description
    with_env(go, monkeypatch)


def test_cooling_off_is_configurable_and_unknown_bans_skip_it(monkeypatch):
    async def go(env):
        await env.ban_row(U1, T0)
        monkeypatch.setenv("APPEAL_COOLING_DAYS", "0")
        assert (await env.submit())[1] == "posted"
        monkeypatch.setenv("APPEAL_COOLING_DAYS", "30")
        assert (await env.submit(U2))[1] == "posted"  # no ban record for U2
        assert "before I kept track" in env.guild.appeals.sent[1]["embed"].description
    with_env(go, monkeypatch)


def test_reappeal_cooldown_after_a_denial(monkeypatch):
    async def go(env):
        await env.submit()
        await env.deny(KEEPER, reason="Not yet.")
        env.t += 30 * DAY
        m, verdict = await env.submit()
        assert verdict == "cooldown" and m.deleted and len(await env.rows()) == 1
        assert "60 more days" in env.logs()[-1]
        env.t = T0 + 90 * DAY
        assert (await env.submit())[1] == "posted"
        rows = await env.rows()
        assert [r["status"] for r in rows] == [A.DENIED, A.OPEN]
        assert "**Earlier appeals:** 1 (1 denied)" in env.guild.appeals.sent[-1]["embed"].description
    with_env(go, monkeypatch)


def test_hostile_text_is_escaped_hidden_and_capped(monkeypatch):
    async def go(env):
        nasty = f"@everyone <@{OWNER_ID}> <@&{OWNER_ID}> **bold** `code` ||spoiler|| # heading\n> quote"
        m, verdict = await env.submit(answers=[nasty, "read [this](https://evil.example/x) now", "z" * 5000])
        assert verdict == "forged"  # the oversized payload is not parsed at all
        _, verdict = await env.submit(answers=[nasty, "read [this](https://evil.example/x) now", "z" * 290])
        assert verdict == "posted"
        (sent,) = env.guild.appeals.sent
        assert sent["allowed_mentions"].to_dict() == NONE
        what, why, extra = [f.value for f in sent["embed"].fields]
        assert "@everyone" not in what.replace("@\u200beveryone", "")
        assert f"<@{OWNER_ID}>" not in what and f"<@&{OWNER_ID}>" not in what
        assert "\\*\\*bold\\*\\*" in what and "\\`code\\`" in what and "\\|\\|spoiler\\|\\|" in what
        assert why == "*Hidden: it contained a link.*" and "evil.example" not in str(sent["embed"].to_dict())
        assert len(extra) <= A.FIELD_MAX
        _, verdict = await env.submit(U2, answers=["join discord.gg/abcdefg", "x" * 2000, "\u202e\u0000ok\tfine"])
        assert verdict == "posted"
        fields = [f.value for f in env.guild.appeals.sent[1]["embed"].fields]
        assert fields[0] == "*Hidden: it contained an invite link.*"
        assert len(fields[1]) == 600 and fields[2] == "ok fine"
    with_env(go, monkeypatch)


def test_missing_ban_permission_keeps_the_appeal_for_later(monkeypatch):
    async def go(env):
        env.guild.me.guild_permissions.ban_members = False
        m, verdict = await env.submit()
        assert verdict == "no_permission" and not m.deleted and await env.rows() == []
        assert (await env.submit(U2))[1] == "no_permission"
        (note,) = env.logs()
        assert "Ban Members" in note
        env.guild.me.guild_permissions.ban_members = True
        assert await env.cog.sweep() == 2  # picked up by the next sweep, oldest first
        assert [r["user_id"] for r in await env.rows()] == [U1, U2] and m.deleted
    with_env(go, monkeypatch)


def test_ban_list_errors_retry_later(monkeypatch):
    async def go(env):
        env.guild.ban_check_fail = http_error()
        m, verdict = await env.submit()
        assert verdict == "retry" and not m.deleted
        env.guild.ban_check_fail = http_error(discord.Forbidden, 403)
        assert await env.cog.ingest(m) == "no_permission"
        env.guild.ban_check_fail = None
        assert await env.cog.ingest(m) == "posted"
    with_env(go, monkeypatch)


def test_card_send_failure_leaves_no_row_and_retries(monkeypatch):
    async def go(env):
        env.guild.appeals.fail = http_error()
        m, verdict = await env.submit()
        assert verdict == "retry" and not m.deleted and await env.rows() == []
        env.guild.appeals.fail = None
        assert await env.cog.sweep() == 1
        assert len(await env.rows()) == 1 and m.deleted
    with_env(go, monkeypatch)


def test_no_thread_is_fine(monkeypatch):
    async def go(env):
        env.guild.appeals.thread_fail = http_error(discord.Forbidden, 403)
        assert (await env.submit())[1] == "posted"
        (row,) = await env.rows()
        assert row["thread_id"] is None and row["card_message_id"]
    with_env(go, monkeypatch)


def test_sweep_reads_what_arrived_while_offline(monkeypatch):
    async def go(env):
        env.post("staff chatter", webhook_id=None)
        first, second = env.post(body(U1)), env.post(body(U2))
        env.post(body(U1, answers=["dup", "dup", ""]))
        assert await env.cog.sweep() == 3
        assert [r["user_id"] for r in await env.rows()] == [U1, U2]
        assert first.deleted and second.deleted
        assert await env.cog.sweep() == 0
        env.guild.appeals.perms["read_message_history"] = False
        env.post(body(U1))
        assert await env.cog.sweep() == 0
    with_env(go, monkeypatch)


def test_on_message_routes_webhooks_and_never_raises(monkeypatch):
    async def go(env):
        m = env.post(body())
        await env.cog.on_message(m)
        assert len(await env.rows()) == 1

        async def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "ingest", boom)
        await env.cog.on_message(env.post(body(U2)))  # logged, not raised
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- buttons: who
def test_only_staff_with_ban_members_may_decide(monkeypatch):
    async def go(env):
        await env.submit()
        for uid in (MEMBER, MOD):  # a member, and a Moderator who can't ban
            for action in ("accept", "deny"):
                inter = await env.press(uid, action)
                assert "Ban Members" in inter.texts()[-1] and inter.of("send_modal") == []
        banner = env.member(50, ban=True)  # the permission without a staff role isn't enough either
        assert "Ban Members" in (await env.press(banner.id, "accept")).texts()[-1]
        (row,) = await env.rows()
        assert row["status"] == A.OPEN and env.guild.unbans == []
        # A modal opened before the permission was taken away is refused on submit too.
        modal = DenyModal(env.cog, 1)
        modal.reason._value = "x"
        submit = env.inter(MOD, message=env.cards()[0])
        await modal.on_submit(submit)
        assert "Ban Members" in submit.texts()[-1]
        assert (await env.rows())[0]["status"] == A.OPEN
    with_env(go, monkeypatch)


def test_missing_appeal(monkeypatch):
    async def go(env):
        await env.submit()
        assert "gone" in (await env.press(KEEPER, "accept", 77)).texts()[-1]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- accept
def test_accept_unbans_records_and_logs(monkeypatch):
    async def go(env):
        await env.submit()
        env.t += 3600
        inter = await env.press(BANMOD, "accept")
        assert env.guild.unbans == [(U1, f"Ban appeal #1 accepted by user{BANMOD}")]
        (row,) = await env.rows()
        assert (row["status"], row["decided_by"], row["decided_at"]) == (A.ACCEPTED, BANMOD, T0 + 3600)
        (card,) = env.cards()
        (edit,) = card.edits
        assert all(c.item.disabled for c in edit["view"].children)
        field = edit["embed"].fields[-1]
        assert field.name == "Accepted by" and f"<@{BANMOD}>" in field.value and f"<t:{T0 + 3600}:f>" in field.value
        assert edit["embed"].footer.text == "BAN APPEAL · ACCEPTED"
        line = env.logs()[-1]
        assert "accepted by" in line and f"<@{BANMOD}>" in line and f"`{U1}`" in line and "trying the invite" in line
        assert env.guild.mod_log.sent[-1]["allowed_mentions"].to_dict() == NONE
        reply = inter.texts()[-1]
        assert "ban is lifted" in reply and "trying the invite" in reply  # the DM bounced, as it usually does
        assert inter.of("followup")[-1]["ephemeral"]
    with_env(go, monkeypatch)


def test_accept_tells_them_when_a_dm_is_possible(monkeypatch):
    async def go(env):
        env.bot.users[U1] = FakeUser(U1)
        await env.submit()
        inter = await env.press(OWNER, "accept")
        (dm,) = env.bot.users[U1].dms
        assert "accepted" in dm["content"] and "Test \\*Server\\*" in dm["content"] and "/join>" in dm["content"]
        assert dm["allowed_mentions"].to_dict() == NONE
        assert "told them by DM" in inter.texts()[-1] and "told them by DM" in env.logs()[-1]
    with_env(go, monkeypatch)


def test_accept_when_already_unbanned(monkeypatch):
    async def go(env):
        await env.submit()
        env.guild.banned.discard(U1)
        inter = await env.press(KEEPER, "accept")
        assert "already unbanned" in inter.texts()[-1]
        assert (await env.rows())[0]["status"] == A.ACCEPTED
    with_env(go, monkeypatch)


def test_accept_failure_reopens_the_appeal(monkeypatch):
    async def go(env):
        await env.submit()
        env.guild.unban_fail = http_error(discord.Forbidden, 403)
        inter = await env.press(KEEPER, "accept")
        assert "still open" in inter.texts()[-1]
        (row,) = await env.rows()
        assert row["status"] == A.OPEN and row["decided_by"] is None and env.cards()[0].edits == []
        env.guild.unban_fail = None
        env.guild.me.guild_permissions.ban_members = False
        assert "Ban Members" in (await env.press(KEEPER, "accept")).texts()[-1]
        assert (await env.rows())[0]["status"] == A.OPEN
    with_env(go, monkeypatch)


def test_decided_appeals_cannot_be_decided_again(monkeypatch):
    async def go(env):
        await env.submit()
        await env.press(KEEPER, "accept")
        again = await env.press(KEEPER, "deny")
        assert "already accepted" in again.texts()[-1] and again.of("send_modal") == []
        assert len(env.guild.unbans) == 1
    with_env(go, monkeypatch)


def test_race_lost_reports_already(monkeypatch):
    async def go(env):
        await env.submit()
        row = (await env.rows())[0]
        await env.db.execute("UPDATE appeals SET status = 'denied' WHERE id = 1")
        inter = env.inter(KEEPER, message=env.cards()[0])
        await env.cog.accept(inter, row)
        assert "already handled" in inter.texts()[-1] and env.guild.unbans == []
        late = env.inter(KEEPER, message=env.cards()[0])
        await env.cog.deny(late, 1, "late")
        assert "already handled" in late.texts()[-1]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- deny
def test_deny_with_reason(monkeypatch):
    async def go(env):
        env.bot.users[U1] = FakeUser(U1)
        await env.submit()
        submit = await env.deny(BANMOD, reason="  Too **soon** @everyone  ")
        (row,) = await env.rows()
        assert (row["status"], row["decided_by"], row["decided_at"]) == (A.DENIED, BANMOD, T0)
        assert row["decision_reason"] == "Too **soon** @everyone"
        assert env.guild.unbans == [] and U1 in env.guild.banned
        (edit,) = env.cards()[0].edits
        field = edit["embed"].fields[-1]
        assert field.name == "Denied by" and "Too \\*\\*soon\\*\\*" in field.value
        assert all(c.item.disabled for c in edit["view"].children)
        line = env.logs()[-1]
        assert "denied by" in line and "90 days" in line and "@everyone" not in line.replace("@\u200beveryone", "")
        (dm,) = env.bot.users[U1].dms
        assert "denied" in dm["content"] and "Too \\*\\*soon\\*\\*" in dm["content"] and "90 days" in dm["content"]
        assert "Denied" in submit.texts()[-1]
    with_env(go, monkeypatch)


def test_deny_without_reason_and_without_dm(monkeypatch):
    async def go(env):
        await env.submit()
        submit = await env.deny(KEEPER)
        (row,) = await env.rows()
        assert row["status"] == A.DENIED and row["decision_reason"] is None
        assert "trying the invite" in submit.texts()[-1]
        assert "Reason" not in env.cards()[0].edits[0]["embed"].fields[-1].value
    with_env(go, monkeypatch)


def test_button_callback_errors_are_caught(monkeypatch):
    async def go(env):
        async def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "handle_button", boom)
        inter = env.inter(KEEPER)
        await AppealButton("accept", 1).callback(inter)
        assert "went wrong" in inter.texts()[-1]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- unbanned by hand, expiry
def test_manual_unban_closes_the_open_appeal(monkeypatch):
    async def go(env):
        await env.submit()
        await env.cog.on_member_unban(env.guild, SimpleNamespace(id=U1))
        (row,) = await env.rows()
        assert row["status"] == A.CLOSED and row["decided_by"] is None
        assert env.cards()[0].edits[0]["embed"].fields[-1].name == "Closed"
        assert "unbanned outside the appeal" in env.logs()[-1]
        await env.cog.on_member_unban(env.guild, SimpleNamespace(id=U1))  # nothing left to close
        assert len(env.logs()) == 1
        await env.cog.on_member_unban(SimpleNamespace(id=5), SimpleNamespace(id=U2))  # another server
    with_env(go, monkeypatch)


def test_untouched_appeals_close_after_30_days(monkeypatch):
    async def go(env):
        await env.submit(U1)
        env.t += 10 * DAY
        await env.submit(U2)
        assert await env.cog.expire(T0 + 30 * DAY - 1) == 0
        assert await env.cog.expire(T0 + 30 * DAY) == 1
        rows = await env.rows()
        assert [r["status"] for r in rows] == [A.EXPIRED, A.OPEN]
        assert rows[0]["decided_by"] is None and rows[0]["decided_at"] == T0 + 30 * DAY
        card = env.cards()[0]
        assert card.edits[0]["embed"].fields[-1].name == "Closed"
        assert all(c.item.disabled for c in card.edits[0]["view"].children)
        assert "nobody decided" in env.logs()[-1] and f"<@{U1}>" in env.logs()[-1]
        # Not their fault: they may appeal again straight away.
        env.t = T0 + 31 * DAY
        assert (await env.submit(U1))[1] == "posted"
    with_env(go, monkeypatch)


def test_staff_talking_in_the_thread_keeps_it_open(monkeypatch):
    async def go(env):
        await env.submit()
        (row,) = await env.rows()
        env.t += 20 * DAY
        thread = SimpleNamespace(id=row["thread_id"], parent_id=env.guild.appeals.id)
        note = SimpleNamespace(webhook_id=None, guild=env.guild, channel=thread, author=env.member(MOD))
        await env.cog.on_message(note)
        assert (await env.rows())[0]["touched_at"] == T0 + 20 * DAY
        assert await env.cog.expire(T0 + 35 * DAY) == 0
        assert await env.cog.expire(T0 + 50 * DAY) == 1
        # The bot's own messages and other threads don't count.
        await env.cog.on_message(SimpleNamespace(webhook_id=None, guild=env.guild, channel=env.guild.general,
                                                 author=env.member(MOD)))
    with_env(go, monkeypatch)


def test_old_answers_are_blanked_but_the_record_stays(monkeypatch):
    async def go(env):
        await env.submit(U1)
        await env.deny(KEEPER, reason="no")
        await env.submit(U2)
        await env.cog.expire(T0 + A.ANSWER_RETENTION - 1)
        assert (await env.rows())[0]["answers"] != ""
        await env.cog.expire(T0 + A.ANSWER_RETENTION)
        first, second = await env.rows()
        assert first["answers"] == "" and first["status"] == A.DENIED and first["user_id"] == U1
        assert second["status"] == A.EXPIRED and second["answers"] != ""  # decided later: kept for now
    with_env(go, monkeypatch)


def test_daily_job_follows_the_schedule_rules(monkeypatch):
    async def go(env):
        assert await env.cog.due() is None  # first sighting only marks the day done
        assert await env.cog.due() is None
        env.t += DAY
        period = await env.cog.due()
        assert period is not None and period.key.startswith("appeals_expire:")
        await env.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, env.t))
        assert await env.cog.due() is None  # never twice
    with_env(go, monkeypatch)


async def day_done(env) -> bool:
    """Whether the expiry day that is due at env.t is marked finished."""
    key = latest_due_daily(A.EXPIRE_JOB, env.t, TZ).key
    return await env.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", (key,)) is not None


def test_a_failing_sweep_does_not_stop_the_daily_expiry(monkeypatch):
    async def go(env):
        await env.submit(U1)
        await Appeals.tick.coro(env.cog)  # first sighting: the day is marked, nothing runs
        assert await day_done(env) and (await env.rows())[0]["status"] == A.OPEN

        calls = []

        async def boom():
            calls.append(env.t)
            raise RuntimeError("sweep is broken")
        monkeypatch.setattr(env.cog, "sweep", boom)
        env.t = T0 + 31 * DAY
        await Appeals.tick.coro(env.cog)  # logged, not raised
        assert calls == [env.t]  # the sweep was still tried, first
        (row,) = await env.rows()
        assert row["status"] == A.EXPIRED and row["decided_at"] == env.t
        card = env.cards()[0]
        assert len(card.edits) == 1 and card.edits[0]["embed"].fields[-1].name == "Closed"
        assert len(env.logs()) == 1 and "nobody decided" in env.logs()[0]
        assert await day_done(env)

        env.t += 300
        await Appeals.tick.coro(env.cog)  # the same day again: nothing more
        assert len(card.edits) == 1 and len(env.logs()) == 1 and len(calls) == 2
    with_env(go, monkeypatch)


def test_a_failing_expiry_does_not_stop_the_sweep_and_is_retried(monkeypatch):
    async def go(env):
        await Appeals.tick.coro(env.cog)  # first sighting
        real_expire = env.cog.expire

        async def boom(t):
            raise RuntimeError("expiry is broken")
        monkeypatch.setattr(env.cog, "expire", boom)
        env.post(body(U1))  # arrived while the bot was off
        env.t = T0 + DAY
        await Appeals.tick.coro(env.cog)  # logged, not raised
        assert [r["user_id"] for r in await env.rows()] == [U1]  # the sweep did its work
        assert not await day_done(env)  # the day is not marked: it is tried again

        monkeypatch.setattr(env.cog, "expire", real_expire)
        env.t += 300
        await Appeals.tick.coro(env.cog)
        assert await day_done(env)
    with_env(go, monkeypatch)


def test_the_expiry_waits_for_the_server_then_closes_each_appeal_once(monkeypatch):
    """Without the server the card can't be closed and staff can't be told, so the day is
    not marked done and the next tick does it."""
    async def go(env):
        await env.submit(U1)
        await Appeals.tick.coro(env.cog)  # first sighting

        env.bot.get_guild = lambda gid: None  # the server is unavailable at the daily tick
        env.t = T0 + 31 * DAY
        for _ in range(2):
            await Appeals.tick.coro(env.cog)
            env.t += 300
        assert not await day_done(env)  # not done: the day is still owed
        assert (await env.rows())[0]["status"] == A.OPEN
        card = env.cards()[0]
        assert card.edits == [] and env.logs() == []

        del env.bot.get_guild  # back
        await Appeals.tick.coro(env.cog)
        assert await day_done(env)
        (row,) = await env.rows()
        assert row["status"] == A.EXPIRED and row["decided_at"] == env.t
        assert len(card.edits) == 1 and all(c.item.disabled for c in card.edits[0]["view"].children)
        assert len(env.logs()) == 1 and f"<@{U1}>" in env.logs()[0]

        for later in (300, DAY):  # later ticks, and the next day's run: nothing twice
            env.t += later
            await Appeals.tick.coro(env.cog)
        assert len(card.edits) == 1 and len(env.logs()) == 1
    with_env(go, monkeypatch)


def test_a_day_with_nothing_to_expire_is_done(monkeypatch):
    async def go(env):
        await Appeals.tick.coro(env.cog)  # first sighting
        env.t = T0 + DAY
        assert not await day_done(env)
        await Appeals.tick.coro(env.cog)
        assert await day_done(env) and env.logs() == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /appeals
def test_appeals_list(monkeypatch):
    async def go(env):
        await env.submit(U1)
        await env.deny(KEEPER, reason="no")
        await env.submit(U2)
        inter = env.inter(MOD)
        await Appeals.appeals_list.callback(env.cog, inter)
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"] and sent["allowed_mentions"].to_dict() == NONE
        text = sent["embed"].description
        assert f"`#2` <@{U2}>" in text and f"`#1` <@{U1}> · denied" in text and f"<@{KEEPER}>" in text
        assert f"https://discord.com/channels/{GUILD_ID}/{env.guild.appeals.id}/9002" in text
        assert sent["embed"].footer.text == "BAN APPEALS · 1 OPEN"
    with_env(go, monkeypatch)


def test_appeals_commands_are_staff_only(monkeypatch):
    async def go(env):
        inter = env.inter(MEMBER)
        await Appeals.appeals_list.callback(env.cog, inter)
        assert "Only Moderators" in inter.texts()[-1]
        inter = env.inter(MEMBER)
        await Appeals.appeals_code.callback(env.cog, inter, FakeUser(U1))
        assert "Only Moderators" in inter.texts()[-1] and A.appeal_code(SECRET, U1) not in str(inter.calls)
    with_env(go, monkeypatch)


def test_appeals_code_shows_what_to_pass_on(monkeypatch):
    async def go(env):
        await env.ban_row(U1, T0 - DAY, "manual")
        inter = env.inter(MOD)
        await Appeals.appeals_code.callback(env.cog, inter, FakeUser(U1))
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"] and sent["allowed_mentions"].to_dict() == NONE
        text = sent["content"]
        assert A.fmt_code(A.appeal_code(SECRET, U1)) in text and f"`{U1}`" in text and "/appeal>" in text
        assert f"<t:{T0 + 6 * DAY}:D>" in text
    with_env(go, monkeypatch)


def test_not_set_up_is_said_plainly(monkeypatch):
    async def go(env):
        inter = env.inter(KEEPER)
        await Appeals.appeals_code.callback(env.cog, inter, FakeUser(U1))
        assert "aren't set up" in inter.texts()[-1]
        inter = env.inter(KEEPER)
        await Appeals.appeals_list.callback(env.cog, inter)
        text = inter.of("send_message")[0]["embed"].description
        assert "aren't set up" in text and "No open appeals." in text
    with_env(go, monkeypatch, secret=None)


def test_list_says_when_the_channel_is_missing(monkeypatch):
    async def go(env):
        env.guild.text_channels = [env.guild.mod_log]
        inter = env.inter(KEEPER)
        await Appeals.appeals_list.callback(env.cog, inter)
        assert "public_mode.py" in inter.of("send_message")[0]["embed"].description
        assert await env.cog.sweep() == 0
        assert await env.cog.ingest(env.post(body())) is None
    with_env(go, monkeypatch)
