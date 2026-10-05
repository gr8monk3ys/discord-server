"""Offline integration tests for cogs.moderation.Moderation: a real in-memory SQLite
database plus lightweight fakes for the bot, guild, channels, members, messages and
interactions. No network. Time is controlled by patching cogs.moderation.now/clock."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import discord

import config
import db as dbmod
from cogs import moderation as cogmod
from cogs.moderation import Moderation
from logic import moderation as M

GUILD_ID = 999
OWNER, A, B, C, MOD, KEEPER, ADMIN, OTHERBOT, BOT_ID = 1, 11, 12, 13, 20, 21, 22, 50, 77
T0 = 1_790_000_000
MIN = 60
HOUR = 60 * MIN
DAY = 24 * HOUR
ALL_PERMS = dict(moderate_members=True, manage_messages=True, manage_guild=True, read_message_history=True)


def run(coro):
    return asyncio.run(coro)


def http_error(cls=discord.HTTPException, status=500):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


# ---------------------------------------------------------------- fakes
class Role:
    _next = 5000

    def __init__(self, name, position):
        Role._next += 1
        self.id = Role._next
        self.name = name
        self.position = position


class FakeMember:
    def __init__(self, uid, guild, bot=False, roles=(), admin=False, age_days=365, perms=None):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.name = f"user{uid}"
        self.mention = f"<@{uid}>"
        self.roles = [guild.role("@everyone")] + [guild.role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=admin, **(perms or {}))
        self.created_at = datetime.fromtimestamp(T0 - age_days * DAY, tz=timezone.utc)
        self.timed_out_until = None
        self.timeouts = []
        self.dms = []
        self.dm_fail = None
        self.timeout_fail = None

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: r.position)

    async def timeout(self, until, reason=None):
        if self.timeout_fail is not None:
            raise self.timeout_fail
        self.timeouts.append((until, reason))
        self.timed_out_until = until

    def is_timed_out(self):
        return self.timed_out_until is not None and self.timed_out_until.timestamp() > cogmod.now()

    async def send(self, content=None, **kwargs):
        if self.dm_fail is not None:
            raise self.dm_fail
        self.dms.append(dict(content=content, **kwargs))


class FakeText:
    _next = 300

    def __init__(self, name, perms=None):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.mention = f"<#{self.id}>"
        self.sent = []
        self.perms = dict(ALL_PERMS if perms is None else perms)
        self.bulk = []  # lists of ids per delete_messages call
        self.bulk_fail = None
        self.history = 30  # messages available to purge
        self.purged = []

    def permissions_for(self, member):
        return SimpleNamespace(**self.perms)

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))
        return SimpleNamespace(id=9000 + len(self.sent))

    async def delete_messages(self, objs, reason=None):
        ids = [o.id for o in objs]
        if self.bulk_fail is not None and len(ids) > 1:
            raise self.bulk_fail
        self.bulk.append(ids)

    async def purge(self, limit=100, reason=None):
        n = min(limit, self.history)
        self.history -= n
        self.purged.append((limit, reason))
        return [SimpleNamespace(id=i) for i in range(n)]


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.name = "Test Server"
        self.owner_id = OWNER
        self.roles = [Role("@everyone", 0), Role(config.LFG_ROLE, 1), Role("Regular", 2),
                      Role(config.MOD_ROLE, 5), Role(config.KEEPER_ROLE, 6), Role("Bigshot", 8),
                      Role("Front Desk", 10), Role("Above Bot", 12)]
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.gaming = FakeText("🕹️・gaming")
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.mod = FakeText(config.MOD_CHANNEL)
        self.text_channels = [self.general, self.gaming, self.mod_log, self.mod]
        self.me = FakeMember(BOT_ID, self, bot=True, roles=["Front Desk"], perms=dict(ALL_PERMS))
        self.verification_level = discord.VerificationLevel.low
        self.paused_until = None
        self.edits = []
        self.edit_fail = None

    def role(self, name):
        return config.match_by_name(self.roles, name)

    def get_channel_or_thread(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    @property
    def invites_paused_until(self):
        return self.paused_until

    def invites_paused(self):
        return self.paused_until is not None and self.paused_until.timestamp() > cogmod.now()

    async def edit(self, reason=None, **kw):
        if self.edit_fail is not None:
            raise self.edit_fail
        self.edits.append(kw)
        if "verification_level" in kw:
            self.verification_level = kw["verification_level"]
        if "invites_disabled_until" in kw:
            self.paused_until = kw["invites_disabled_until"]


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None


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

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, bot, user, channel):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.channel = channel
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def replies(self):
        return [kw for k, kw in self.calls if k in ("send_message", "followup")]

    def text(self):
        return " ".join(kw.get("content") or "" for kw in self.replies())


class FakeMessage:
    _next = 70000

    def __init__(self, author, channel, content="hi", mentions=(), role_mentions=(), webhook_id=None):
        FakeMessage._next += 1
        self.id = FakeMessage._next
        self.author = author
        self.guild = author.guild
        self.channel = channel
        self.content = content
        self.raw_mentions = list(mentions)
        self.raw_role_mentions = list(role_mentions)
        self.webhook_id = webhook_id


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0
        self.c = float(T0)
        self.members = {}

    def member(self, uid, **kw):
        if uid not in self.members:
            self.members[uid] = FakeMember(uid, self.guild, **kw)
        return self.members[uid]

    def inter(self, uid, channel=None):
        return FakeInteraction(self.bot, self.member(uid), channel or self.guild.general)

    def tick(self, seconds):
        self.t += int(seconds)
        self.c += seconds

    async def cases(self, user_id=None):
        sql = "SELECT * FROM cases" + (" WHERE user_id = ?" if user_id else "") + " ORDER BY id"
        return [dict(r) for r in await self.db.fetchall(sql, (user_id,) if user_id else ())]

    async def warn(self, mod, target, reason="being rude"):
        inter = self.inter(mod)
        await Moderation.warn.callback(self.cog, inter, self.member(target), reason)
        return inter

    async def say(self, uid, content="hi", channel=None, **kw):
        m = FakeMessage(self.member(uid), channel or self.guild.general, content, **kw)
        await self.cog.on_message(m)
        return m

    async def join(self, uid, age_days=365, bot=False):
        await self.cog.on_member_join(FakeMember(uid, self.guild, bot=bot, age_days=age_days))

    def restart(self):
        self.cog = Moderation(self.bot)
        return self.cog


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        env = Env(db, guild, bot, Moderation(bot))
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        monkeypatch.setattr(cogmod, "clock", lambda: env.c)
        env.member(MOD, roles=[config.MOD_ROLE])
        env.member(KEEPER, roles=[config.KEEPER_ROLE])
        env.member(ADMIN, admin=True)
        env.member(OWNER)
        env.member(OTHERBOT, bot=True)
        for uid in (A, B, C):
            env.member(uid, roles=[config.LFG_ROLE])
        env.members[BOT_ID] = guild.me
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def logs(channel):
    return [s["embed"].description for s in channel.sent]


NONE = discord.AllowedMentions.none().to_dict()


def silent(kw):
    return kw.get("allowed_mentions") is not None and kw["allowed_mentions"].to_dict() == NONE


def no_pings(channel):
    return all(silent(s) for s in channel.sent)


# ---------------------------------------------------------------- staff gate
def test_non_staff_are_refused_everywhere(monkeypatch):
    async def go(env):
        calls = [
            lambda i: Moderation.warn.callback(env.cog, i, env.member(B), "x"),
            lambda i: Moderation.timeout.callback(env.cog, i, env.member(B), 5, "x"),
            lambda i: Moderation.untimeout.callback(env.cog, i, env.member(B)),
            lambda i: Moderation.cases.callback(env.cog, i, env.member(B)),
            lambda i: Moderation.purge.callback(env.cog, i, 5),
        ]
        for call in calls:
            inter = env.inter(A)
            await call(inter)
            assert inter.replies() == [dict(content=cogmod.STAFF_ONLY, ephemeral=True)]
        assert await env.cases() == [] and env.guild.mod_log.sent == []
        assert env.guild.general.purged == []
    with_env(go, monkeypatch)


def test_owner_admin_keeper_mod_are_staff(monkeypatch):
    async def go(env):
        for uid in (OWNER, ADMIN, KEEPER, MOD):
            inter = env.inter(uid)
            await Moderation.cases.callback(env.cog, inter, env.member(A))
            assert inter.replies()[0]["ephemeral"] is True and inter.replies()[0].get("embed") is not None
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- targets
def test_never_act_on_staff_the_bot_or_yourself(monkeypatch):
    async def go(env):
        for target, problem in [(KEEPER, M.Problem.STAFF), (ADMIN, M.Problem.STAFF), (OWNER, M.Problem.STAFF),
                                (BOT_ID, M.Problem.BOT_SELF), (MOD, M.Problem.SELF)]:
            inter = await env.warn(MOD, target)
            assert inter.replies() == [dict(content=M.TARGET_REPLIES[problem], ephemeral=True)]
        assert await env.cases() == []
    with_env(go, monkeypatch)


def test_hierarchy_target_must_be_below_actor_and_bot(monkeypatch):
    async def go(env):
        env.member(B).roles.append(env.guild.role("Bigshot"))  # 8: above Moderator (5), below the bot (10)
        env.member(C).roles.append(env.guild.role("Above Bot"))  # 12
        inter = await env.warn(MOD, B)
        assert inter.replies()[0]["content"] == M.TARGET_REPLIES[M.Problem.ABOVE_YOU]
        inter = await env.warn(OWNER, C)  # the owner outranks everyone, but the bot can't act
        assert inter.replies()[0]["content"] == M.TARGET_REPLIES[M.Problem.ABOVE_BOT]
        inter = await env.warn(OWNER, B)
        assert "Case #1" in inter.text()
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /warn
def test_warn_stores_case_dms_and_logs_without_pings(monkeypatch):
    async def go(env):
        inter = await env.warn(MOD, A, "  spamming memes  ")
        (case,) = await env.cases()
        assert (case["user_id"], case["mod_id"], case["kind"], case["reason"], case["at"], case["duration"]) == (
            A, MOD, "warn", "spamming memes", T0, None)
        assert inter.calls[0][0] == "defer"
        (reply,) = inter.replies()
        assert reply["content"] == f"Case #1: warned <@{A}> (1 in 30 days)." and reply["ephemeral"] and silent(reply)
        (dm,) = env.member(A).dms
        assert "warning" in dm["content"] and "spamming memes" in dm["content"] and "Test Server" in dm["content"]
        (line,) = logs(env.guild.mod_log)
        assert "Case #1" in line and f"<@{A}>" in line and f"<@{MOD}>" in line and "spamming memes" in line
        assert no_pings(env.guild.mod_log)
        assert env.member(A).timeouts == []
    with_env(go, monkeypatch)


def test_warn_dm_failure_is_ignored(monkeypatch):
    async def go(env):
        env.member(A).dm_fail = http_error(discord.Forbidden, 403)
        inter = await env.warn(MOD, A)
        assert "Case #1" in inter.text()
        assert "DM failed" in logs(env.guild.mod_log)[0]
    with_env(go, monkeypatch)


def test_mod_log_falls_back_to_mod_channel(monkeypatch):
    async def go(env):
        env.guild.text_channels.remove(env.guild.mod_log)
        await env.warn(MOD, A)
        assert len(env.guild.mod.sent) == 1 and no_pings(env.guild.mod)
    with_env(go, monkeypatch)


def test_three_warnings_in_30_days_auto_timeout_one_hour_five_gives_a_day(monkeypatch):
    async def go(env):
        a = env.member(A)
        await env.warn(MOD, A)
        env.tick(DAY)
        await env.warn(KEEPER, A)
        assert a.timeouts == []
        env.tick(DAY)
        inter = await env.warn(MOD, A)
        assert "automatic 1 h timeout" in inter.text()
        auto = [c for c in await env.cases(A) if c["kind"] == "auto_timeout"]
        assert [(c["mod_id"], c["duration"]) for c in auto] == [(None, HOUR)]
        until, reason = a.timeouts[-1]
        assert until.timestamp() == env.t + HOUR and "3 warnings" in reason
        assert any("timed out" in d["content"] for d in a.dms)
        assert any("Automatic timeout" in line for line in logs(env.guild.mod_log))
        env.tick(2 * HOUR)
        await env.warn(MOD, A)  # 4th: another hour
        env.tick(2 * HOUR)
        await env.warn(MOD, A)  # 5th: a day
        auto = [c["duration"] for c in await env.cases(A) if c["kind"] == "auto_timeout"]
        assert auto == [HOUR, HOUR, DAY]
        assert a.timeouts[-1][0].timestamp() == env.t + DAY
    with_env(go, monkeypatch)


def test_old_warnings_fall_out_of_the_window(monkeypatch):
    async def go(env):
        await env.warn(MOD, A)
        await env.warn(MOD, A)
        env.tick(31 * DAY)
        inter = await env.warn(MOD, A)
        assert "(1 in 30 days)" in inter.text()
        assert env.member(A).timeouts == []
    with_env(go, monkeypatch)


def test_auto_timeout_never_shortens_a_longer_timeout(monkeypatch):
    async def go(env):
        a = env.member(A)
        a.timed_out_until = datetime.fromtimestamp(T0 + 5 * DAY, tz=timezone.utc)
        for _ in range(3):
            await env.warn(MOD, A)
        assert a.timeouts == []
    with_env(go, monkeypatch)


def test_escalation_without_moderate_members_still_warns(monkeypatch):
    async def go(env):
        env.guild.me.guild_permissions.moderate_members = False
        for _ in range(3):
            inter = await env.warn(MOD, A)
        assert "Moderate Members" in inter.text()
        assert [c["kind"] for c in await env.cases(A)] == ["warn"] * 3
        assert env.member(A).timeouts == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /timeout, /untimeout
def test_timeout_and_untimeout(monkeypatch):
    async def go(env):
        a = env.member(A)
        inter = env.inter(MOD)
        await Moderation.timeout.callback(env.cog, inter, a, 90, "flaming")
        assert inter.text() == f"Case #1: timed out <@{A}> for 1 h 30 min."
        assert a.timeouts[0][0].timestamp() == T0 + 90 * MIN
        assert "1 h 30 min" in a.dms[0]["content"] and "flaming" in a.dms[0]["content"]
        (case,) = await env.cases()
        assert (case["kind"], case["duration"], case["mod_id"]) == ("timeout", 90 * MIN, MOD)

        inter = env.inter(KEEPER)
        await Moderation.untimeout.callback(env.cog, inter, a)
        assert a.timeouts[-1][0] is None and not a.is_timed_out()
        assert inter.text() == f"Case #2: lifted <@{A}>'s timeout."
        assert [c["kind"] for c in await env.cases()] == ["timeout", "untimeout"]
        assert len(logs(env.guild.mod_log)) == 2 and no_pings(env.guild.mod_log)

        inter = env.inter(KEEPER)
        await Moderation.untimeout.callback(env.cog, inter, a)
        assert inter.text() == f"<@{A}> isn't timed out."
    with_env(go, monkeypatch)


def test_timeout_missing_permission_names_it(monkeypatch):
    async def go(env):
        env.guild.me.guild_permissions.moderate_members = False
        for call in (lambda i: Moderation.timeout.callback(env.cog, i, env.member(A), 5, "x"),
                     lambda i: Moderation.untimeout.callback(env.cog, i, env.member(A))):
            inter = env.inter(MOD)
            await call(inter)
            (reply,) = inter.replies()
            assert "**Moderate Members**" in reply["content"] and reply["ephemeral"]
        assert await env.cases() == []
    with_env(go, monkeypatch)


def test_timeout_forbidden_by_discord(monkeypatch):
    async def go(env):
        env.member(A).timeout_fail = http_error(discord.Forbidden, 403)
        inter = env.inter(MOD)
        await Moderation.timeout.callback(env.cog, inter, env.member(A), 5, "x")
        assert "didn't let me" in inter.text()
        assert await env.cases() == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /cases
def test_cases_shows_last_ten_newest_first(monkeypatch):
    async def go(env):
        for i in range(12):
            await env.cog.add_case(A, MOD, "warn", f"reason {i}")
            env.tick(MIN)
        await env.cog.add_case(B, MOD, "warn", "someone else")
        inter = env.inter(MOD)
        await Moderation.cases.callback(env.cog, inter, env.member(A))
        (reply,) = inter.replies()
        text = reply["embed"].description
        assert reply["ephemeral"] and silent(reply)
        assert "#12" in text and "#3" in text and "`#2`" not in text and "someone else" not in text
        assert text.index("#12") < text.index("#3")
        assert "12 TOTAL" in reply["embed"].footer.text

        inter = env.inter(MOD)
        await Moderation.cases.callback(env.cog, inter, env.member(C))
        assert inter.replies()[0]["embed"].description == M.NO_CASES
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /purge
def test_purge_deletes_and_logs(monkeypatch):
    async def go(env):
        inter = env.inter(MOD, env.guild.gaming)
        await Moderation.purge.callback(env.cog, inter, 25)
        assert env.guild.gaming.purged == [(25, f"/purge by user{MOD}")]
        assert inter.text() == "Deleted 25 messages."
        (case,) = await env.cases()
        assert (case["user_id"], case["kind"], case["mod_id"]) == (env.guild.gaming.id, "purge", MOD)
        assert "25 messages" in logs(env.guild.mod_log)[0] and no_pings(env.guild.mod_log)
    with_env(go, monkeypatch)


def test_purge_missing_manage_messages(monkeypatch):
    async def go(env):
        env.guild.gaming.perms["manage_messages"] = False
        inter = env.inter(MOD, env.guild.gaming)
        await Moderation.purge.callback(env.cog, inter, 5)
        assert "**Manage Messages**" in inter.text() and env.guild.gaming.purged == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- anti-spam
def test_flood_times_out_deletes_burst_and_files_a_case(monkeypatch):
    async def go(env):
        sent = []
        for i in range(6):
            sent.append(await env.say(A, f"msg {i}", channel=env.guild.general if i % 2 else env.guild.gaming))
            env.tick(0.5)
        a = env.member(A)
        assert a.timeouts[0][0].timestamp() == T0 + 10 * MIN  # the 6th arrived at T0 + 2.5 (int clock T0)
        gen = [m.id for m in sent if m.channel is env.guild.general]
        gam = [m.id for m in sent if m.channel is env.guild.gaming]
        assert env.guild.general.bulk == [gen] and env.guild.gaming.bulk == [gam]
        (case,) = await env.cases()
        assert (case["user_id"], case["mod_id"], case["kind"], case["duration"]) == (A, None, "spam", 10 * MIN)
        (line,) = logs(env.guild.mod_log)
        assert "Deleted 6 of 6" in line and no_pings(env.guild.mod_log)
        assert "10 min" in a.dms[0]["content"]
    with_env(go, monkeypatch)


def test_identical_messages_and_mass_mentions(monkeypatch):
    async def go(env):
        for _ in range(3):
            await env.say(A, "join my server discord.gg/x")
            env.tick(10)
        assert [c["reason"] for c in await env.cases(A)] == [M.SPAM_REASONS[M.SpamReason.DUPLICATE]]
        m = await env.say(B, "yo", mentions=[A, C, MOD, B], role_mentions=[5001, 5002])  # self doesn't count
        assert [c["reason"] for c in await env.cases(B)] == [M.SPAM_REASONS[M.SpamReason.MENTIONS]]
        assert env.guild.general.bulk[-1] == [m.id]
        await env.say(C, "hey", mentions=[A, B, MOD, KEEPER])
        assert await env.cases(C) == []
    with_env(go, monkeypatch)


def test_staff_bots_and_webhooks_are_exempt(monkeypatch):
    async def go(env):
        for uid in (MOD, KEEPER, ADMIN, OWNER, OTHERBOT):
            for _ in range(8):
                await env.say(uid, "same")
        for _ in range(8):
            await env.say(A, "same", webhook_id=123)
        assert await env.cases() == []
    with_env(go, monkeypatch)


def test_bulk_delete_failure_falls_back_to_one_by_one(monkeypatch):
    async def go(env):
        env.guild.general.bulk_fail = http_error()
        sent = [await env.say(A, f"m{i}") for i in range(6)]
        assert env.guild.general.bulk == [[m.id] for m in sent]
        assert "Deleted 6 of 6" in logs(env.guild.mod_log)[0]
    with_env(go, monkeypatch)


def test_anti_spam_without_permissions_notes_once_and_still_files_cases(monkeypatch):
    async def go(env):
        env.guild.me.guild_permissions.moderate_members = False
        env.guild.general.perms["manage_messages"] = False
        for uid in (A, B):
            for i in range(6):
                await env.say(uid, f"m{i}")
        assert [c["duration"] for c in await env.cases()] == [None, None]
        assert env.member(A).timeouts == [] and env.guild.general.bulk == []
        notes = [line for line in logs(env.guild.mod_log) if "permission, which" in line]
        assert len(notes) == 2  # one per missing permission, not per event
        assert any("Moderate Members" in n for n in notes) and any("Manage Messages" in n for n in notes)
    with_env(go, monkeypatch)


def test_anti_spam_skips_timeout_above_the_bot(monkeypatch):
    async def go(env):
        env.member(A).roles.append(env.guild.role("Above Bot"))
        for i in range(6):
            await env.say(A, f"m{i}")
        assert env.member(A).timeouts == []
        assert "not timed out" in logs(env.guild.mod_log)[0]
    with_env(go, monkeypatch)


def test_on_message_never_raises(monkeypatch):
    async def go(env):
        await env.db.close()
        for i in range(6):
            await env.say(A, f"m{i}")  # case insert fails: logged, not raised
        await env.db.connect()
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- anti-raid
async def raid(env, n=8, age_days=365, start=1000):
    for i in range(n):
        await env.join(start + i, age_days=age_days)
        env.tick(2)


def test_raid_locks_down_and_restores_after_restart(monkeypatch):
    async def go(env):
        await raid(env, 7)
        assert env.guild.edits == []
        await env.join(2000)
        (edit,) = env.guild.edits
        assert edit["verification_level"] is discord.VerificationLevel.high
        assert edit["invites_disabled_until"].timestamp() == env.t + M.RAID_LOCK
        alert = logs(env.guild.mod_log)[-1]
        assert "Join raid" in alert and "High" in alert and no_pings(env.guild.mod_log)
        row = await env.db.fetchone("SELECT value FROM meta WHERE key = ?", (cogmod.RAID_KEY,))
        state = M.RaidState.loads(row["value"])
        assert state.prev_level == 1 and state.raised

        cog = env.restart()
        env.tick(M.RAID_LOCK - 10)
        await cog.run_restore()
        assert len(env.guild.edits) == 1
        env.tick(10)
        await cog.run_restore()
        restore = env.guild.edits[-1]
        assert restore == {"invites_disabled_until": None, "verification_level": discord.VerificationLevel.low}
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (cogmod.RAID_KEY,)) is None
        assert "Raid lockdown over" in logs(env.guild.mod_log)[-1]
        await cog.run_restore()
        assert len(env.guild.edits) == 2
    with_env(go, monkeypatch)


def test_young_accounts_count_double(monkeypatch):
    async def go(env):
        await raid(env, 4, age_days=2)
        assert len(env.guild.edits) == 1
    with_env(go, monkeypatch)


def test_slow_joins_and_bots_do_not_trigger(monkeypatch):
    async def go(env):
        for i in range(30):
            await env.join(1000 + i)
            env.tick(10)
        for i in range(20):
            await env.join(3000 + i, bot=True)
        assert env.guild.edits == []
    with_env(go, monkeypatch)


def test_raid_keeps_a_stricter_level_and_a_mod_change(monkeypatch):
    async def go(env):
        env.guild.verification_level = discord.VerificationLevel.highest
        await raid(env)
        assert "verification_level" not in env.guild.edits[0]
        env.tick(M.RAID_LOCK)
        await env.cog.run_restore()
        assert env.guild.edits[-1] == {"invites_disabled_until": None}
        assert env.guild.verification_level is discord.VerificationLevel.highest

    with_env(go, monkeypatch)


def test_restore_leaves_a_level_a_mod_changed_meanwhile(monkeypatch):
    async def go(env):
        await raid(env)
        env.guild.verification_level = discord.VerificationLevel.medium
        env.tick(M.RAID_LOCK)
        await env.cog.run_restore()
        assert env.guild.edits[-1] == {"invites_disabled_until": None}
        assert env.guild.verification_level is discord.VerificationLevel.medium
    with_env(go, monkeypatch)


def test_second_wave_extends_the_lockdown(monkeypatch):
    async def go(env):
        await raid(env)
        first = M.RaidState.loads((await env.db.fetchone("SELECT value FROM meta WHERE key = ?",
                                                         (cogmod.RAID_KEY,)))["value"])
        env.tick(10 * MIN)
        await raid(env, start=5000)
        again = M.RaidState.loads((await env.db.fetchone("SELECT value FROM meta WHERE key = ?",
                                                         (cogmod.RAID_KEY,)))["value"])
        assert again.until > first.until and again.prev_level == first.prev_level == 1
        assert env.guild.edits[-1]["invites_disabled_until"].timestamp() == again.until
        assert "verification_level" not in env.guild.edits[-1]
        assert "lockdown now lasts" in logs(env.guild.mod_log)[-1]
    with_env(go, monkeypatch)


def test_raid_without_manage_server_alerts_and_saves_nothing(monkeypatch):
    async def go(env):
        env.guild.me.guild_permissions.manage_guild = False
        await raid(env)
        assert env.guild.edits == []
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (cogmod.RAID_KEY,)) is None
        assert any("couldn't lock the server down" in line for line in logs(env.guild.mod_log))
    with_env(go, monkeypatch)


def test_restore_retries_on_errors_and_gives_up_when_forbidden(monkeypatch):
    async def go(env):
        await raid(env)
        env.tick(M.RAID_LOCK)
        env.guild.edit_fail = http_error()
        await env.cog.run_restore()
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (cogmod.RAID_KEY,)) is not None
        env.guild.edit_fail = http_error(discord.Forbidden, 403)
        await env.cog.run_restore()
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (cogmod.RAID_KEY,)) is None
        assert "couldn't undo it" in logs(env.guild.mod_log)[-1]
    with_env(go, monkeypatch)


def test_restore_puts_back_an_earlier_longer_invite_pause(monkeypatch):
    async def go(env):
        earlier = datetime.fromtimestamp(T0 + 5 * HOUR, tz=timezone.utc)
        env.guild.paused_until = earlier
        await raid(env)
        assert env.guild.edits[0]["invites_disabled_until"] == earlier  # never shortened
        env.tick(M.RAID_LOCK)
        await env.cog.run_restore()
        assert env.guild.edits[-1]["invites_disabled_until"] == earlier
    with_env(go, monkeypatch)


def test_on_member_join_never_raises(monkeypatch):
    async def go(env):
        env.guild.edit_fail = RuntimeError("boom")
        await raid(env)  # lockdown raises a non-HTTP error: logged, not raised
    with_env(go, monkeypatch)
