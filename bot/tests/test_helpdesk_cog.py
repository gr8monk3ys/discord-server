"""Offline tests for cogs.helpdesk: fakes for the bot, tree, guild and interactions. No network."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

import config
from cogs import helpdesk as cogmod
from cogs.helpdesk import HOME, HelpView, Helpdesk
from logic import helpdesk as H
from logic import wordgame as W

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999


def run(coro):
    return asyncio.run(coro)


async def _noop(interaction: discord.Interaction) -> None:
    pass


def cmd(name, description, module):
    c = app_commands.Command(name=name, description=description, callback=_noop)
    c.module = module
    return c


def group(name, description, module, *subs):
    g = app_commands.Group(name=name, description=description)
    g.module = module
    for s in subs:
        g.add_command(s)
    return g


def tree_commands():
    return [
        cmd("daily", "Claim your daily coins", "cogs.economy"),
        cmd("warn", "Warn a member (mods only)", "cogs.moderation"),
        cmd("lfg", "Find a squad", "cogs.lfg"),
        group("word", "Daily Word", "cogs.wordgame", cmd("guess", "Guess today's word", "cogs.wordgame")),
    ]


class FakeTree:
    def __init__(self, commands, ids=None, fail=None):
        self.commands = commands
        self.ids = ids or {}
        self.fail = fail
        self.fetches = 0

    def get_commands(self):
        return list(self.commands)

    async def fetch_commands(self, guild=None):
        self.fetches += 1
        if self.fail is not None:
            raise self.fail
        return [SimpleNamespace(name=n, id=i) for n, i in self.ids.items()]


class FakeText:
    def __init__(self, name, cid):
        self.name = name
        self.id = cid
        self.mention = f"<#{cid}>"
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{cid}"


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.name = "Test Server"
        self.description = "Squads every night"
        self.icon = None
        self.member_count = 321
        self.members = [SimpleNamespace(status=discord.Status.online), SimpleNamespace(status=discord.Status.offline)]
        self.text_channels = [FakeText(config.ROLES_CHANNEL, 501), FakeText(config.GENERAL_CHANNEL, 502)]
        self.voice_channels = [object(), object()]
        self.forums = [object()]
        self.stage_channels = []
        self.premium_subscription_count = 7
        self.premium_tier = 1
        self.created_at = datetime(2025, 10, 1, tzinfo=timezone.utc)


class FakeQuests:
    def __init__(self, fail=False):
        self.checked = []
        self.fail = fail

    async def check(self, member):
        self.checked.append(member.id)

    async def quest_embed(self, member):
        if self.fail:
            raise RuntimeError("boom")
        return discord.Embed(title="Starter quest · 2/5")


class FakeBot:
    def __init__(self, guild, tree=None, quests=None, counts=None):
        self.guild = guild
        self.tree = tree or FakeTree(tree_commands())
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.guild_ref = discord.Object(id=GUILD_ID)
        self.quests = quests
        self.counts = counts
        self.presences = []

    def get_guild(self, gid):
        return self.guild if gid == GUILD_ID else None

    def get_cog(self, name):
        return self.quests if name == "Quests" else None

    async def change_presence(self, activity=None):
        self.presences.append(activity)

    async def fetch_guild(self, gid, with_counts=False):
        if self.counts is None:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "down")
        return SimpleNamespace(approximate_member_count=self.counts[0], approximate_presence_count=self.counts[1])


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls

    async def send_message(self, content=None, **kw):
        self.calls.append(("send", dict(content=content, **kw)))

    async def edit_message(self, **kw):
        self.calls.append(("edit", kw))


def user(staff=False, uid=11):
    perms = discord.Permissions(moderate_members=staff)
    return SimpleNamespace(id=uid, guild_permissions=perms, mention=f"<@{uid}>")


class FakeInteraction:
    def __init__(self, user, guild):
        self.user = user
        self.guild = guild
        self.calls = []
        self.response = FakeResponse(self.calls)

    def sent(self):
        return [kw for k, kw in self.calls if k == "send"]


def env(**kw):
    guild = FakeGuild()
    bot = FakeBot(guild, **kw)
    return guild, bot, Helpdesk(bot)


def fields(embed):
    return {f.name: f.value for f in embed.fields}


# ------------------------------------------------------------ /help


def test_help_home_is_ephemeral_with_menu_and_hides_staff():
    async def go():
        guild, bot, cog = env()
        inter = FakeInteraction(user(), guild)
        await cog.help.callback(cog, inter, None)
        [kw] = inter.sent()
        assert kw["ephemeral"] is True
        assert isinstance(kw["view"], HelpView)
        text = " ".join([kw["embed"].description, *fields(kw["embed"]).keys(), *fields(kw["embed"]).values()])
        assert "/warn" not in text and "/daily" in text
        values = [o.value for o in kw["view"].pick.options]
        assert values[0] == HOME and "utility" not in values  # /warn is the only utility command
    run(go())


def test_help_shows_staff_commands_to_staff():
    async def go():
        guild, bot, cog = env()
        inter = FakeInteraction(user(staff=True), guild)
        await cog.help.callback(cog, inter, None)
        [kw] = inter.sent()
        assert "utility" in [o.value for o in kw["view"].pick.options]
        assert "`/warn`" in " ".join(fields(kw["embed"]).values())
    run(go())


def test_help_command_detail_uses_clickable_mentions():
    async def go():
        guild, bot, cog = env(tree=FakeTree(tree_commands(), ids={"word": 77}))
        inter = FakeInteraction(user(), guild)
        await cog.help.callback(cog, inter, "word")
        [kw] = inter.sent()
        assert kw["ephemeral"] is True and "view" not in kw
        assert kw["embed"].title == "/word <guess>"
        assert "</word guess:77>" in kw["embed"].description
    run(go())


def test_help_unknown_or_hidden_command_falls_back_to_menu():
    async def go():
        guild, bot, cog = env()
        for query in ("nope", "warn"):  # members can't look up staff commands either
            inter = FakeInteraction(user(), guild)
            await cog.help.callback(cog, inter, query)
            [kw] = inter.sent()
            assert kw["content"].startswith(f"No command called `/{query}`")
            assert isinstance(kw["view"], HelpView)
    run(go())


def test_help_autocomplete_filters_staff():
    async def go():
        guild, bot, cog = env()
        member = await cog.help_autocomplete(FakeInteraction(user(), guild), "w")
        staff = await cog.help_autocomplete(FakeInteraction(user(staff=True), guild), "w")
        assert [c.value for c in member] == ["word", "word guess"]
        assert "warn" in [c.value for c in staff]
        assert all(len(c.name) <= 100 for c in staff)
    run(go())


def test_select_switches_category():
    async def go():
        guild, bot, cog = env()
        entries = cog.entries_for(user())
        view = HelpView(cog, entries)
        view.pick._values = ["games"]
        inter = FakeInteraction(user(), guild)
        await view.pick.callback(inter)
        [(kind, kw)] = inter.calls
        assert kind == "edit" and kw["view"] is view
        assert kw["embed"].title == "🎲 Games"
        assert "`/word guess`" in kw["embed"].description
        assert [o.value for o in view.pick.options if o.default] == ["games"]
        view.pick._values = [HOME]
        await view.pick.callback(inter)
        assert inter.calls[-1][1]["embed"].title == "Front Desk help"
    run(go())


# ------------------------------------------------------------ Start here


def test_start_here_shows_quest_and_roles_link():
    async def go():
        quests = FakeQuests()
        guild, bot, cog = env(quests=quests)
        view = HelpView(cog, cog.entries_for(user()))
        inter = FakeInteraction(user(uid=42), guild)
        await view.start_here.callback(inter)
        [kw] = inter.sent()
        assert kw["ephemeral"] is True
        assert kw["embed"].title == "Starter quest · 2/5"
        assert "<#501>" in kw["content"]
        urls = [i.url for i in kw["view"].children]
        assert urls == [f"https://discord.com/channels/{GUILD_ID}/501", H.SITE_URL]
        assert quests.checked == [42]
        assert kw["allowed_mentions"].users is False
    run(go())


def test_start_here_without_quests_or_roles_channel():
    async def go():
        guild, bot, cog = env(quests=FakeQuests(fail=True))
        guild.text_channels = []
        inter = FakeInteraction(user(), guild)
        await cog.send_start_here(inter)
        [kw] = inter.sent()
        assert "/quest" in kw["embed"].description
        assert "/roles" in kw["content"]
        assert [i.url for i in kw["view"].children] == [H.SITE_URL]
    run(go())


# ------------------------------------------------------------ command ids


def test_command_ids_are_cached_and_failures_fall_back(monkeypatch):
    async def go():
        t = [1000]
        monkeypatch.setattr(cogmod, "now", lambda: t[0])
        tree = FakeTree(tree_commands(), ids={"daily": 5})
        guild, bot, cog = env(tree=tree)
        assert await cog.command_ids() == {"daily": 5}
        t[0] += 60
        await cog.command_ids()
        assert tree.fetches == 1  # cached
        t[0] += cogmod.IDS_TTL
        tree.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "down")
        assert await cog.command_ids() == {"daily": 5}  # keeps the last good ids
        assert tree.fetches == 2
        t[0] += 60
        await cog.command_ids()
        assert tree.fetches == 2  # waits IDS_RETRY before trying again
        t[0] += cogmod.IDS_RETRY
        await cog.command_ids()
        assert tree.fetches == 3
    run(go())


# ------------------------------------------------------------ presence


def test_presence_rotates_through_the_lines(monkeypatch):
    async def go():
        ts = int(datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc).timestamp())
        monkeypatch.setattr(cogmod, "now", lambda: ts)
        guild, bot, cog = env()
        for _ in range(5):
            await cog.rotate()
        a = bot.presences
        assert a[0].type == discord.ActivityType.watching and a[0].name == "321 members"
        assert isinstance(a[1], discord.Game) and a[1].name == "/queue to find a squad"
        assert a[2].type == discord.ActivityType.listening and a[2].name == "/help"
        number = W.puzzle_number(W.local_day(ts, TZ))
        assert number == 8 and a[3].name == f"Daily Word #{number}"
        assert a[4].name == "321 members"
    run(go())


def test_presence_loop_swallows_errors():
    async def go():
        guild, bot, cog = env()

        async def boom(activity=None):
            raise RuntimeError("gateway")
        bot.change_presence = boom
        await cog.presence.coro(cog)  # logs, doesn't raise
    run(go())


# ------------------------------------------------------------ /about


def test_about_uses_fetched_counts(monkeypatch):
    async def go():
        monkeypatch.setattr(discord.utils, "utcnow", lambda: datetime(2026, 10, 8, tzinfo=timezone.utc))
        guild, bot, cog = env(counts=(400, 123))
        inter = FakeInteraction(user(), guild)
        await cog.about.callback(cog, inter)
        [kw] = inter.sent()
        assert "ephemeral" not in kw
        e = kw["embed"]
        f = fields(e)
        assert e.title == "Test Server" and e.description == "Squads every night"
        assert f["Members"] == "400" and f["Online now"] == "123"
        assert f["Boosts"] == "7 (level 1)"
        assert f["Channels"] == "3 text · 2 voice"
        assert f["Around for"].startswith("1 year, 7 days (since <t:")
        assert H.SITE_URL in f["Links"] and H.INVITE_URL in f["Links"]
        assert [i.url for i in kw["view"].children] == [H.SITE_URL, H.INVITE_URL]
    run(go())


def test_about_falls_back_to_cached_counts():
    async def go():
        guild, bot, cog = env(counts=None)
        inter = FakeInteraction(user(), guild)
        await cog.about.callback(cog, inter)
        f = fields(inter.sent()[0]["embed"])
        assert f["Members"] == "321" and f["Online now"] == "1"
    run(go())


def test_about_is_guild_only_and_help_is_registered():
    assert cogmod.Helpdesk.about.guild_only is True
    names = {c.name for c in cogmod.Helpdesk.__cog_app_commands__}
    assert names == {"help", "about"}


def test_setup_adds_the_cog():
    async def go():
        added = []

        class Bot:
            async def add_cog(self, cog):
                added.append(cog)
        await cogmod.setup(Bot())
        assert isinstance(added[0], Helpdesk)
    run(go())


def test_activity_kinds():
    assert isinstance(cogmod.activity("playing", "x"), discord.Game)
    assert cogmod.activity("watching", "x").type == discord.ActivityType.watching
    assert cogmod.activity("listening", "x").type == discord.ActivityType.listening


def test_is_staff():
    assert cogmod.is_staff(user(staff=True))
    assert not cogmod.is_staff(user())
    assert cogmod.is_staff(SimpleNamespace(guild_permissions=discord.Permissions(administrator=True)))
    assert not cogmod.is_staff(SimpleNamespace())  # a DM user has no guild permissions
