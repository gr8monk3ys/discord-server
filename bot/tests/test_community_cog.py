"""Offline integration tests for cogs.community.Community: a real in-memory SQLite
database plus lightweight fakes for the bot, guild, channels, members and
interactions. No network. Time is controlled by patching cogs.community.now."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

import config
import db as dbmod
import style
from cogs import community as cogmod
from cogs.community import Community, ReportButton, ReportModal
from logic import community as rules

GUILD_ID = 999
OWNER, A, B, C, MOD, KEEPER, ADMIN, BOTUSER = 1, 11, 12, 13, 20, 21, 22, 50
T0 = 1_790_000_000
DAY = 24 * 60 * 60


def run(coro):
    return asyncio.run(coro)


def http_error(status=500):
    return discord.HTTPException(SimpleNamespace(status=status, reason="boom"), "boom")


# ---------------------------------------------------------------- fakes
class Named:
    _next = 5000

    def __init__(self, name):
        Named._next += 1
        self.id = Named._next
        self.name = name
        self.mention = f"<@&{self.id}>"


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


class FakeMember:
    def __init__(self, uid, guild, bot=False, roles=(), completed=False, admin=False, age_days=365):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.name = f"user{uid}"
        self.display_name = self.name
        self.mention = f"<@{uid}>"
        self.roles = [guild.role("@everyone")] + [guild.role(r) for r in roles]
        self.flags = SimpleNamespace(completed_onboarding=completed)
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.created_at = datetime.fromtimestamp(T0 - age_days * DAY, tz=timezone.utc)

    def finished(self, done=True):
        """A copy with a different Onboarding flag (before/after for on_member_update)."""
        m = FakeMember(self.id, self.guild, self.bot, admin=self.guild_permissions.administrator)
        m.roles = list(self.roles)
        m.flags = SimpleNamespace(completed_onboarding=done)
        return m


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.mod = FakeText(config.MOD_CHANNEL)
        self.text_channels = [self.general, self.mod_log, self.mod]
        self.forum = SimpleNamespace(id=4242, name=config.LFG_FORUM, mention="<#4242>")
        self.forums = [self.forum]
        self.roles = [Named(n) for n in ["@everyone", config.MOD_ROLE, config.KEEPER_ROLE, config.LFG_ROLE]]
        self.roles += [Named(g.role) for g in config.GAMES]
        self.onboarding_on = True
        self.onboarding_calls = 0
        self.onboarding_error = None

    def role(self, name):
        return config.match_by_name(self.roles, name)

    def drop(self, channel):
        self.text_channels.remove(channel)

    async def onboarding(self):
        self.onboarding_calls += 1
        if self.onboarding_error is not None:
            raise self.onboarding_error
        return SimpleNamespace(enabled=self.onboarding_on)


class FakeTree:
    def __init__(self):
        self.commands = []

    def add_command(self, command):
        self.commands.append(command)

    def remove_command(self, name, type=None):
        self.commands = [c for c in self.commands if c.name != name]


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)
        self.tree = FakeTree()
        self.dynamic = set()
        self.cogs = {}

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None

    def add_dynamic_items(self, *items):
        self.dynamic.update(items)

    def remove_dynamic_items(self, *items):
        self.dynamic.difference_update(items)

    def get_cog(self, name):
        return self.cogs.get(name)


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


class FakeInteraction:
    def __init__(self, bot, user, channel=None, message=None):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.channel = channel
        self.message = message
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]

    def replies(self):
        """(text, ephemeral) of every message sent back to the user."""
        return [(kw.get("content"), kw.get("ephemeral")) for k, kw in self.calls
                if k in ("send_message", "followup")]


class FakeMessage:
    def __init__(self, mid, author, channel, content):
        self.id = mid
        self.author = author
        self.channel = channel
        self.content = content
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{channel.id}/{mid}"


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

    async def rows(self, table):
        return [dict(r) for r in await self.db.fetchall(f"SELECT * FROM {table} ORDER BY rowid")]

    async def finish_onboarding(self, member):
        await self.cog.on_member_update(member.finished(False), member.finished(True))

    async def report(self, reporter, target, reason="being rude in voice chat"):
        inter = self.inter(reporter, channel=self.guild.general)
        await Community.report.callback(self.cog, inter, self.member(target), reason)
        return inter

    async def report_message(self, reporter, message, reason="this message is out of line"):
        """Context menu -> modal -> submit, as Discord would drive it."""
        inter = self.inter(reporter, channel=message.channel)
        await self.cog.report_message(inter, message)
        modals = inter.of("send_modal")
        if not modals:
            return inter, None
        modal = modals[0]["modal"]
        modal.reason._value = reason
        submit = self.inter(reporter, channel=message.channel)
        await modal.on_submit(submit)
        return inter, submit

    async def press(self, uid, action, report_id, message=None, **kw):
        inter = self.inter(uid, message=message, **kw)
        await self.cog.handle_report_button(inter, action, report_id)
        return inter

    def restart(self):
        """A new cog on the same database, as after a bot restart."""
        self.cog = Community(self.bot)
        self.bot.cogs["Community"] = self.cog
        return self.cog


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Community(bot)
        bot.cogs["Community"] = cog
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        env.member(MOD, roles=[config.MOD_ROLE])
        env.member(KEEPER, roles=[config.KEEPER_ROLE])
        env.member(ADMIN, admin=True)
        env.member(OWNER)
        env.member(BOTUSER, bot=True)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


async def add_lfg_post(env, game, thread_id, size=4, members=1, closed=False, created=None):
    cur_id = await env.db.fetchone("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM lfg_posts")
    pid = cur_id["n"]
    await env.db.execute(
        "INSERT INTO lfg_posts (id, thread_id, message_id, game, host_id, size, when_text, created_at, closed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'now', ?, ?)",
        (pid, thread_id, thread_id + 1, game, 100 + pid, size, created or env.t, env.t if closed else None))
    for i in range(members):
        await env.db.execute("INSERT INTO lfg_members VALUES (?, ?, ?)", (pid, 100 + pid + i, env.t))
    return pid


def embeds(channel):
    return [s["embed"] for s in channel.sent if s.get("embed") is not None]


def field(embed, name):
    return next((f.value for f in embed.fields if f.name == name), None)


# ---------------------------------------------------------------- load
def test_cog_load_registers_menu_and_buttons_and_unload_removes(monkeypatch):
    async def go(env):
        await env.cog.cog_load()
        assert ReportButton in env.bot.dynamic
        (menu,) = env.bot.tree.commands
        assert menu.name == "Report message" and menu.type is discord.AppCommandType.message
        await env.cog.cog_unload()
        assert env.bot.tree.commands == [] and ReportButton not in env.bot.dynamic
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- welcome
def test_welcome_on_onboarding_flip_once_ever(monkeypatch):
    async def go(env):
        m = env.member(A, roles=["Valorant", "Minecraft"])
        await env.finish_onboarding(m)
        (post,) = env.guild.general.sent
        assert post["content"].startswith(rules.opener(A, m.mention))
        assert "Minecraft and Valorant" in post["content"]
        assert "<#4242>" in post["content"] and "/lfg" in post["content"]
        am = post["allowed_mentions"]
        assert am.everyone is False and am.roles is False and [u.id for u in am.users] == [A]
        assert [r["user_id"] for r in await env.rows("welcomed")] == [A]

        await env.finish_onboarding(m)  # flips again (e.g. re-onboarding)
        env.restart()
        await env.finish_onboarding(m)  # after a restart
        env.guild.onboarding_on = False
        await env.cog.on_member_join(m)  # leaves and rejoins on a server without onboarding
        assert len(env.guild.general.sent) == 1
    with_env(go, monkeypatch)


def test_no_welcome_without_a_flip(monkeypatch):
    async def go(env):
        m = env.member(A)
        await env.cog.on_member_update(m.finished(True), m.finished(True))
        await env.cog.on_member_update(m.finished(False), m.finished(False))
        await env.cog.on_member_update(m.finished(True), m.finished(False))
        assert env.guild.general.sent == [] and await env.rows("welcomed") == []
    with_env(go, monkeypatch)


def test_bots_are_never_welcomed(monkeypatch):
    async def go(env):
        bot_member = env.member(BOTUSER)
        await env.finish_onboarding(bot_member)
        bot_member.flags.completed_onboarding = True
        await env.cog.on_member_join(bot_member)
        assert env.guild.general.sent == [] and await env.rows("welcomed") == []
        assert len(env.guild.mod_log.sent) == 1  # the join is still logged
    with_env(go, monkeypatch)


def test_join_waits_for_onboarding_when_the_server_has_it(monkeypatch):
    async def go(env):
        m = env.member(A, completed=False)
        await env.cog.on_member_join(m)
        assert env.guild.general.sent == []
        await env.finish_onboarding(m)
        assert len(env.guild.general.sent) == 1
    with_env(go, monkeypatch)


def test_join_already_onboarded_is_welcomed_at_join(monkeypatch):
    async def go(env):
        m = env.member(A, completed=True)
        await env.cog.on_member_join(m)
        assert len(env.guild.general.sent) == 1
    with_env(go, monkeypatch)


def test_join_on_server_without_onboarding_welcomes_and_caches_the_check(monkeypatch):
    async def go(env):
        env.guild.onboarding_on = False
        await env.cog.on_member_join(env.member(A))
        await env.cog.on_member_join(env.member(B))
        assert len(env.guild.general.sent) == 2
        assert env.guild.onboarding_calls == 1
        env.t += cogmod.ONBOARDING_TTL
        await env.cog.on_member_join(env.member(C))
        assert env.guild.onboarding_calls == 2
    with_env(go, monkeypatch)


def test_onboarding_check_failure_assumes_onboarding(monkeypatch):
    async def go(env):
        env.guild.onboarding_on = False
        env.guild.onboarding_error = http_error(403)
        m = env.member(A)
        await env.cog.on_member_join(m)
        assert env.guild.general.sent == []
        await env.finish_onboarding(m)
        assert len(env.guild.general.sent) == 1
    with_env(go, monkeypatch)


def test_welcome_links_open_lfg_post_for_a_picked_game(monkeypatch):
    async def go(env):
        await add_lfg_post(env, "fortnite", 771)  # not one of theirs
        await add_lfg_post(env, "valorant", 772, created=env.t - 100)
        await add_lfg_post(env, "valorant", 773)  # newest wins
        m = env.member(A, roles=["Valorant", "Minecraft"])
        await env.finish_onboarding(m)
        text = env.guild.general.sent[0]["content"]
        assert "Valorant squad looking for people right now: https://discord.com/channels/999/773" in text
        assert "<#4242>" not in text
    with_env(go, monkeypatch)


def test_welcome_skips_closed_full_and_unthreaded_posts(monkeypatch):
    async def go(env):
        await add_lfg_post(env, "valorant", 781, closed=True)
        await add_lfg_post(env, "valorant", 782, size=2, members=2)  # full
        pid = await add_lfg_post(env, "valorant", 783)
        await env.db.execute("UPDATE lfg_posts SET thread_id = NULL WHERE id = ?", (pid,))  # still creating
        await env.finish_onboarding(env.member(A, roles=["Valorant"]))
        text = env.guild.general.sent[0]["content"]
        assert "discord.com/channels" not in text and "<#4242>" in text
    with_env(go, monkeypatch)


def test_welcome_without_forum_names_it(monkeypatch):
    async def go(env):
        env.guild.forums = []
        await env.finish_onboarding(env.member(A))
        assert config.LFG_FORUM in env.guild.general.sent[0]["content"]
    with_env(go, monkeypatch)


def test_failed_welcome_send_releases_the_claim_and_never_raises(monkeypatch):
    async def go(env):
        m = env.member(A)
        env.guild.general.fail = http_error()
        await env.finish_onboarding(m)  # must not raise
        assert await env.rows("welcomed") == []
        env.guild.general.fail = RuntimeError("unexpected")
        await env.finish_onboarding(m)  # must not raise either
        env.guild.general.fail = None
        await env.finish_onboarding(m)
        assert len(env.guild.general.sent) == 1
    with_env(go, monkeypatch)


def test_missing_general_channel_skips_without_claiming(monkeypatch):
    async def go(env):
        env.guild.drop(env.guild.general)
        await env.finish_onboarding(env.member(A))
        assert await env.rows("welcomed") == []
    with_env(go, monkeypatch)


def test_other_guilds_ignored(monkeypatch):
    async def go(env):
        stranger = FakeMember(A, env.guild)
        before, after = stranger.finished(False), stranger.finished(True)
        for m in (stranger, before, after):
            m.guild = SimpleNamespace(id=123)
        await env.cog.on_member_update(before, after)
        await env.cog.on_member_join(stranger)
        assert env.guild.general.sent == [] and env.guild.mod_log.sent == []
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /report
def test_report_member_saves_row_posts_embed_and_thanks(monkeypatch):
    async def go(env):
        inter = await env.report(A, B, reason="  being rude in voice chat  ")
        (row,) = await env.rows("reports")
        assert (row["reporter_id"], row["target_id"], row["channel_id"], row["message_id"]) == (
            A, B, env.guild.general.id, None)
        assert row["reason"] == "being rude in voice chat" and row["status"] == "open" and row["at"] == T0

        (sent,) = env.guild.mod_log.sent
        assert row["log_message_id"] == 9001
        embed = sent["embed"]
        assert embed.title == f"Report #{row['id']}"
        assert "being rude in voice chat" in embed.description
        assert "<@12>" in field(embed, "Reported") and "<@11>" in field(embed, "By")
        assert field(embed, "Channel") == env.guild.general.mention
        assert field(embed, "Message") is None
        assert [i.item.custom_id for i in sent["view"].children] == [
            f"report:resolve:{row['id']}", f"report:dismiss:{row['id']}"]
        am = sent["allowed_mentions"]
        assert am.users is False and am.roles is False and am.everyone is False

        assert inter.of("defer")[0]["ephemeral"] is True
        assert inter.replies() == [(rules.THANKS, True)]
        # Reporter identity stays out of everything public.
        assert env.guild.general.sent == []
    with_env(go, monkeypatch)


def test_report_message_opens_modal_then_posts_link_and_quote(monkeypatch):
    async def go(env):
        msg = FakeMessage(4444, env.member(B), env.guild.general, "y" * 500)
        menu_inter, submit = await env.report_message(A, msg)
        assert isinstance(menu_inter.of("send_modal")[0]["modal"], ReportModal)
        (row,) = await env.rows("reports")
        assert row["message_id"] == 4444 and row["channel_id"] == env.guild.general.id and row["target_id"] == B
        embed = embeds(env.guild.mod_log)[0]
        assert msg.jump_url in field(embed, "Message")
        quoted = embed.description.split("\n\n", 1)[1]
        assert quoted.startswith("> y") and quoted.endswith("…") and len(quoted) == 2 + rules.QUOTE_LIMIT
        assert submit.replies() == [(rules.THANKS, True)]
    with_env(go, monkeypatch)


def test_report_message_without_text_has_no_quote(monkeypatch):
    async def go(env):
        msg = FakeMessage(4445, env.member(B), env.guild.general, "")
        await env.report_message(A, msg)
        embed = embeds(env.guild.mod_log)[0]
        assert "\n\n" not in embed.description and field(embed, "Message")
    with_env(go, monkeypatch)


def test_modal_reason_input_limits():
    async def go():
        modal = ReportModal(None, None)
        assert modal.reason.min_length == rules.REASON_MIN and modal.reason.max_length == rules.REASON_MAX
    run(go())


@pytest.mark.parametrize("target,problem", [(A, rules.ReportProblem.SELF), (BOTUSER, rules.ReportProblem.BOT)])
def test_cannot_report_self_or_bot(monkeypatch, target, problem):
    async def go(env):
        inter = await env.report(A, target)
        assert inter.replies() == [(rules.REPORT_REPLIES[problem], True)]
        msg = FakeMessage(1, env.member(target), env.guild.general, "hi")
        menu_inter, submit = await env.report_message(A, msg)
        assert submit is None  # no modal opened
        assert menu_inter.replies() == [(rules.REPORT_REPLIES[problem], True)]
        assert await env.rows("reports") == [] and env.guild.mod_log.sent == []
    with_env(go, monkeypatch)


def test_blank_reason_is_rejected(monkeypatch):
    async def go(env):
        inter = await env.report(A, B, reason="     ok     ")
        assert inter.replies() == [(rules.REPORT_REPLIES[rules.ReportProblem.REASON_SHORT], True)]
        assert await env.rows("reports") == []
    with_env(go, monkeypatch)


def test_rate_limit_three_per_ten_minutes(monkeypatch):
    async def go(env):
        for i in range(3):
            env.t = T0 + i * 60
            inter = await env.report(A, B)
            assert inter.replies() == [(rules.THANKS, True)]
        env.t = T0 + 5 * 60
        limited = rules.REPORT_REPLIES[rules.ReportProblem.RATE_LIMITED]
        assert (await env.report(A, C)).replies() == [(limited, True)]
        msg = FakeMessage(1, env.member(C), env.guild.general, "hi")
        menu_inter, submit = await env.report_message(A, msg)
        assert submit is None and menu_inter.replies() == [(limited, True)]
        assert (await env.report(B, C)).replies() == [(rules.THANKS, True)]  # others unaffected
        assert len(await env.rows("reports")) == 4

        env.t = T0 + 10 * 60 + 1  # the first report has aged out
        assert (await env.report(A, C)).replies() == [(rules.THANKS, True)]
    with_env(go, monkeypatch)


def test_report_falls_back_to_mod_channel(monkeypatch):
    async def go(env):
        env.guild.drop(env.guild.mod_log)
        inter = await env.report(A, B)
        assert len(env.guild.mod.sent) == 1 and inter.replies() == [(rules.THANKS, True)]
    with_env(go, monkeypatch)


def test_report_with_no_mod_channels_is_saved_and_says_so(monkeypatch):
    async def go(env):
        env.guild.drop(env.guild.mod_log)
        env.guild.drop(env.guild.mod)
        inter = await env.report(A, B)
        assert len(await env.rows("reports")) == 1
        assert inter.replies() == [(cogmod.NO_MOD_CHANNEL, True)]
    with_env(go, monkeypatch)


def test_report_post_failure_removes_row_and_raises_for_error_reply(monkeypatch):
    async def go(env):
        env.guild.mod_log.fail = http_error()
        with pytest.raises(discord.HTTPException):
            await env.report(A, B)
        assert await env.rows("reports") == []  # doesn't count against the limit
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- resolve / dismiss
async def make_report(env):
    await env.report(A, B)
    (row,) = await env.rows("reports")
    message = SimpleNamespace(embeds=[env.guild.mod_log.sent[-1]["embed"]])
    return row["id"], message


@pytest.mark.parametrize("uid", [A, B])
def test_regular_members_cannot_handle_reports(monkeypatch, uid):
    async def go(env):
        rid, message = await make_report(env)
        inter = await env.press(uid, "resolve", rid, message)
        assert inter.replies() == [("Only Moderators and Keepers can handle reports.", True)]
        assert inter.of("edit_message") == []
        assert (await env.rows("reports"))[0]["status"] == "open"
    with_env(go, monkeypatch)


@pytest.mark.parametrize("uid", [MOD, KEEPER, ADMIN, OWNER])
def test_mods_keepers_admins_owner_can_handle(monkeypatch, uid):
    async def go(env):
        rid, message = await make_report(env)
        inter = await env.press(uid, "resolve", rid, message)
        assert len(inter.of("edit_message")) == 1
    with_env(go, monkeypatch)


@pytest.mark.parametrize("action,status", [("resolve", "resolved"), ("dismiss", "dismissed")])
def test_handle_updates_status_embed_and_disables_buttons(monkeypatch, action, status):
    async def go(env):
        rid, message = await make_report(env)
        original = message.embeds[0]
        inter = await env.press(MOD, action, rid, message)
        assert (await env.rows("reports"))[0]["status"] == status
        (edit,) = inter.of("edit_message")
        embed = edit["embed"]
        assert field(embed, f"{status.capitalize()} by") == f"<@{MOD}>"
        assert embed.color.value == style.MUTED and status.upper() in embed.footer.text
        assert field(embed, "Reported") == field(original, "Reported")  # original content kept
        assert all(i.item.disabled for i in edit["view"].children)
        assert field(original, f"{status.capitalize()} by") is None  # copied, not mutated

        again = await env.press(KEEPER, "dismiss", rid, message)
        assert again.replies() == [(f"This report was already {status}.", True)]
        assert (await env.rows("reports"))[0]["status"] == status
    with_env(go, monkeypatch)


def test_handle_unknown_report(monkeypatch):
    async def go(env):
        inter = await env.press(MOD, "resolve", 12345, SimpleNamespace(embeds=[]))
        assert inter.replies() == [("That report doesn't exist anymore.", True)]
    with_env(go, monkeypatch)


def test_button_custom_id_round_trips_through_template():
    async def go():
        made = ReportButton("dismiss", 42)
        assert made.item.custom_id == "report:dismiss:42" and made.item.label == "Dismiss"
        pattern = ReportButton.__discord_ui_compiled_template__
        parsed = await ReportButton.from_custom_id(None, None, pattern.fullmatch("report:resolve:7"))
        assert (parsed.action, parsed.report_id, parsed.item.label) == ("resolve", 7, "Resolve")
        assert pattern.fullmatch("report:ban:7") is None
        assert pattern.fullmatch("report:resolve:x") is None
    run(go())


def test_buttons_work_after_restart(monkeypatch):
    async def go(env):
        rid, message = await make_report(env)
        env.restart()  # new cog, same DB, nothing carried in memory
        custom_id = env.guild.mod_log.sent[0]["view"].children[1].item.custom_id
        match = ReportButton.__discord_ui_compiled_template__.fullmatch(custom_id)
        item = await ReportButton.from_custom_id(None, None, match)
        inter = env.inter(MOD, message=message)
        await item.callback(inter)
        assert (await env.rows("reports"))[0]["status"] == "dismissed"
        assert len(inter.of("edit_message")) == 1
    with_env(go, monkeypatch)


def test_button_callback_errors_reply_generically(monkeypatch):
    async def go(env):
        rid, message = await make_report(env)
        await env.db.close()  # the next DB call fails
        item = ReportButton("resolve", rid)
        inter = env.inter(MOD, message=message)
        await item.callback(inter)
        assert inter.replies()[0][1] is True
        await env.db.connect()  # so the fixture's close() is happy
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- mod log
def test_join_log_flags_new_accounts(monkeypatch):
    async def go(env):
        await env.cog.on_member_join(env.member(A, age_days=2))
        await env.cog.on_member_join(env.member(B, age_days=400))
        new, old = embeds(env.guild.mod_log)
        assert "<@11>" in new.description and "2 days" in new.description and "new account" in new.description
        assert "1 year" in old.description and "new account" not in old.description
        for sent in env.guild.mod_log.sent:
            am = sent["allowed_mentions"]
            assert am.users is False and am.roles is False and am.everyone is False
    with_env(go, monkeypatch)


def test_leave_ban_unban_lines(monkeypatch):
    async def go(env):
        m = env.member(A)
        await env.cog.on_member_remove(m)
        await env.cog.on_member_ban(env.guild, m)
        await env.cog.on_member_unban(env.guild, m)
        lines = [e.description for e in embeds(env.guild.mod_log)]
        assert "left" in lines[0] and "banned" in lines[1] and "unbanned" in lines[2]
        assert all("`11`" in line for line in lines)
    with_env(go, monkeypatch)


class FakeExecution:
    def __init__(self, guild, rule="No slurs", keyword="badword", channel_id=77, fail=False):
        self.guild = guild
        self.rule_id = 31
        self.user_id = A
        self.channel_id = channel_id
        self.matched_keyword = keyword
        self.rule = rule
        self.fail = fail

    async def fetch_rule(self):
        if self.fail:
            raise http_error(403)
        return SimpleNamespace(name=self.rule)


def test_automod_lines(monkeypatch):
    async def go(env):
        await env.cog.on_automod_action(FakeExecution(env.guild))
        await env.cog.on_automod_action(FakeExecution(env.guild, keyword=None, channel_id=None, fail=True))
        first, second = [e.description for e in embeds(env.guild.mod_log)]
        assert "No slurs" in first and "<@11>" in first and "<#77>" in first and "`badword`" in first
        assert "rule 31" in second and "matched" not in second
    with_env(go, monkeypatch)


def test_mod_log_missing_is_silently_skipped(monkeypatch):
    async def go(env):
        env.guild.drop(env.guild.mod_log)
        await env.cog.on_member_join(env.member(A, age_days=1))
        await env.cog.on_member_remove(env.member(A))
        await env.cog.on_automod_action(FakeExecution(env.guild))
        assert env.guild.mod.sent == []  # mod-log events don't spill into the mod channel
    with_env(go, monkeypatch)


def test_mod_log_send_failure_never_raises(monkeypatch):
    async def go(env):
        env.guild.mod_log.fail = RuntimeError("weird")
        await env.cog.on_member_ban(env.guild, env.member(A))
        env.guild.mod_log.fail = http_error()
        await env.cog.on_member_unban(env.guild, env.member(A))
    with_env(go, monkeypatch)


def test_report_reason_and_quote_are_markdown_escaped(monkeypatch):
    async def go(env):
        msg = FakeMessage(4445, env.member(B), env.guild.general, "[login](https://phish.example)")
        await env.report_message(A, msg, reason="[Jump to message](https://phish.example/login)")
        desc = embeds(env.guild.mod_log)[0].description
        assert r"**Reason** \[Jump to message]" in desc
        assert r"> \[login]" in desc
    with_env(go, monkeypatch)
