"""Offline tests for cogs.selfroles: a real in-memory SQLite database plus fakes for the
bot, guild, channel, roles, members and interactions. No network."""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import config
import db as dbmod
from cogs import selfroles as cogmod
from cogs.selfroles import PANEL_KEY, GameSelect, RoleButton, SelfRoles
from logic import selfroles as S

GUILD_ID = 999
BOT_ID = 50


def run(coro):
    return asyncio.run(coro)


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="x"), "x")


class FakeRole:
    _next = 1000

    def __init__(self, name, position=5, managed=False, perms=None, rid=None):
        FakeRole._next += 1
        self.id = rid or FakeRole._next
        self.name = name
        self.position = position
        self.managed = managed
        self.permissions = discord.Permissions(**(perms or {}))
        self.mention = f"<@&{self.id}>"

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id

    def __hash__(self):
        return self.id


class FakeMessage:
    def __init__(self, mid, channel, author_id=BOT_ID, components=()):
        self.id = mid
        self.channel = channel
        self.author = SimpleNamespace(id=author_id)
        self.components = list(components)
        self.edits = []
        self.gone = False

    async def edit(self, **kwargs):
        if self.gone:
            raise http_error(discord.NotFound, 404)
        if self.channel.edit_error is not None:
            raise self.channel.edit_error
        self.edits.append(kwargs)


class FakeText:
    def __init__(self, cid, name, guild):
        self.id, self.name, self.guild = cid, name, guild
        self.sent = []
        self.messages: dict[int, FakeMessage] = {}
        self.next_id = 7000
        self.send_error = None
        self.edit_error = None
        self.history_items = []

    async def send(self, content=None, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.next_id += 1
        msg = FakeMessage(self.next_id, self)
        self.messages[msg.id] = msg
        self.sent.append(dict(content=content, **kwargs))
        return msg

    def get_partial_message(self, mid):
        msg = self.messages.get(mid)
        if msg is None:
            msg = FakeMessage(mid, self)
            msg.gone = True
        return msg

    async def history(self, limit=50):
        for m in self.history_items[:limit]:
            yield m


class FakeMember:
    def __init__(self, uid, roles=()):
        self.id = uid
        self.roles = list(roles)
        self.added, self.removed = [], []
        self.fail = None

    async def add_roles(self, *roles, reason=None):
        if self.fail is not None:
            raise self.fail
        self.added.extend(roles)
        self.roles.extend(roles)

    async def remove_roles(self, *roles, reason=None):
        if self.fail is not None:
            raise self.fail
        self.removed.extend(roles)
        self.roles = [r for r in self.roles if r not in roles]


class FakeGuild:
    def __init__(self, names=None):
        self.id = GUILD_ID
        self.default = FakeRole("@everyone", position=0, rid=GUILD_ID)
        self.top = FakeRole("Front Desk", position=50)
        self.me = SimpleNamespace(id=BOT_ID, top_role=self.top,
                                  guild_permissions=discord.Permissions(manage_roles=True))
        if names is None:
            names = [n for s in S.sections() for n in s.names]
        self.roles = [self.default, self.top, *(FakeRole(n) for n in names)]
        self.channel = FakeText(400, config.ROLES_CHANNEL, self)
        self.text_channels = [self.channel]

    def role(self, name):
        return next(r for r in self.roles if r.name == name)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None


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
    def __init__(self, user, guild):
        self.calls = []
        self.user = user
        self.guild = guild
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def last(self):
        return self.calls[-1][1]


def with_cog(fn, guild=None):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        g = guild or FakeGuild()
        cog = SelfRoles(FakeBot(db, g))
        try:
            await fn(cog, g, db)
        finally:
            await db.close()
    run(go())


def custom_ids(view):
    return [c.custom_id for c in view.children] if view else []


def idx(section, name):
    return S.section(section).names.index(name)


# ---------------------------------------------------------------- panel
def test_panel_posts_once_then_edits_in_place():
    async def go(cog, g, db):
        assert await cog.refresh_panel(g) == "posted"
        [post] = g.channel.sent
        assert post["allowed_mentions"].roles is False and post["allowed_mentions"].everyone is False
        ids = custom_ids(post["view"])
        assert "selfroles:platform:0" in ids and "selfroles:games" in ids
        row = await db.fetchone("SELECT value FROM meta WHERE key = ?", (PANEL_KEY,))
        mid = next(iter(g.channel.messages))
        assert row["value"] == f"{g.channel.id}:{mid}"
        # Same roles: nothing to do. New cog (a restart): edits the same message.
        assert await cog.refresh_panel(g) == "skipped"
        cog.last_signature = None
        assert await cog.refresh_panel(g) == "edited"
        assert len(g.channel.sent) == 1
        assert len(g.channel.messages[mid].edits) == 1
    with_cog(go)


def test_panel_edits_when_roles_change_and_hides_missing_roles():
    async def go(cog, g, db):
        await cog.refresh_panel(g)
        g.roles = [r for r in g.roles if r.name != "Xbox"]
        assert await cog.refresh_panel(g) == "edited"
        msg = next(iter(g.channel.messages.values()))
        ids = custom_ids(msg.edits[-1]["view"])
        assert f"selfroles:platform:{idx('platform', 'Xbox')}" not in ids
        # Indexes stay stable when a role is hidden.
        assert f"selfroles:platform:{idx('platform', 'Switch')}" in ids
        assert "Xbox" not in msg.edits[-1]["embed"].description
    with_cog(go)


def test_panel_reposts_when_old_message_deleted():
    async def go(cog, g, db):
        await cog.refresh_panel(g)
        old = next(iter(g.channel.messages))
        g.channel.messages.clear()
        cog.last_signature = None
        assert await cog.refresh_panel(g) == "posted"
        row = await db.fetchone("SELECT value FROM meta WHERE key = ?", (PANEL_KEY,))
        assert row["value"] != f"{g.channel.id}:{old}"
    with_cog(go)


def test_panel_found_in_history_when_meta_is_missing():
    async def go(cog, g, db):
        comp = SimpleNamespace(children=[SimpleNamespace(custom_id="selfroles:region:1")])
        mine = FakeMessage(4242, g.channel, components=[comp])
        g.channel.messages[4242] = mine
        other = FakeMessage(4243, g.channel, author_id=1, components=[comp])
        g.channel.history_items = [other, mine]
        assert await cog.refresh_panel(g) == "edited"
        assert g.channel.sent == [] and len(mine.edits) == 1 and other.edits == []
        row = await db.fetchone("SELECT value FROM meta WHERE key = ?", (PANEL_KEY,))
        assert row["value"] == f"{g.channel.id}:4242"
    with_cog(go)


def test_panel_without_channel_or_on_errors_never_raises():
    async def go(cog, g, db):
        g.text_channels = []
        assert await cog.refresh_panel(g) == "missing"
        g.text_channels = [g.channel]
        g.channel.send_error = http_error(discord.Forbidden, 403)
        assert await cog.refresh_panel(g) == "failed"
        g.channel.send_error = None
        await cog.refresh_panel(g)
        g.channel.edit_error = http_error(discord.HTTPException, 500)
        cog.last_signature = None
        assert await cog.refresh_panel(g) == "failed"
        assert len(g.channel.sent) == 1  # a failed edit never reposts
        g.text_channels = None  # on_ready swallows anything
        await cog.on_ready()
    with_cog(go)


def test_roles_above_bot_managed_or_dangerous_are_hidden():
    async def go(cog, g, db):
        g.role("PC").position = 60
        g.role("Xbox").managed = True
        g.role("NA").permissions = discord.Permissions(administrator=True)
        g.role("EU").permissions = discord.Permissions(manage_roles=True)
        avail = cogmod.available(g)
        names = {S.section(k).names[i] for k, v in avail.items() for i in v}
        assert not names & {"PC", "Xbox", "NA", "EU"}
        assert {"PlayStation", "Asia", config.GAMENIGHT_ROLE} <= names
    with_cog(go)


def test_no_manage_roles_means_no_panel_buttons():
    async def go(cog, g, db):
        g.me.guild_permissions = discord.Permissions.none()
        await cog.refresh_panel(g)
        assert g.channel.sent[0]["view"] is None
    with_cog(go)


def test_view_fits_discord_limits():
    async def go(cog, g, db):
        view = cogmod.build_view(cogmod.available(g))
        assert len(view.children) <= 25
        rows = view.to_components()
        assert len(rows) <= 5
        assert all(len(r["components"]) <= 5 for r in rows)
        assert rows[-1]["components"][0]["type"] == discord.ComponentType.select.value
        assert [c["custom_id"] for c in rows[0]["components"]][0] == "selfroles:platform:0"
        select = next(c.item for c in view.children if isinstance(c.item, discord.ui.Select))
        assert [o.value for o in select.options] == [str(i) for i in range(len(config.GAMES))]
    with_cog(go)


# ---------------------------------------------------------------- buttons
def test_button_toggles_role_on_and_off():
    async def go(cog, g, db):
        m = FakeMember(1)
        i = FakeInteraction(m, g)
        await cog.handle_button(i, "platform", idx("platform", "PC"))
        assert m.added == [g.role("PC")]
        assert i.calls[0] == ("defer", dict(ephemeral=True, thinking=True))
        assert i.last()["content"] == "Added PC." and i.last()["ephemeral"] is True
        i = FakeInteraction(m, g)
        await cog.handle_button(i, "platform", idx("platform", "PC"))
        assert m.removed == [g.role("PC")] and g.role("PC") not in m.roles
        assert i.last()["content"] == "Removed PC."
    with_cog(go)


def test_region_is_single_choice():
    async def go(cog, g, db):
        m = FakeMember(1, roles=[g.role("NA"), g.role("PC")])
        i = FakeInteraction(m, g)
        await cog.handle_button(i, "region", idx("region", "EU"))
        assert m.removed == [g.role("NA")] and m.added == [g.role("EU")]
        assert g.role("PC") in m.roles
        assert i.last()["content"] == "Added EU. Removed NA."
    with_cog(go)


def test_ping_button_toggles_game_night():
    async def go(cog, g, db):
        m = FakeMember(1)
        await cog.handle_button(FakeInteraction(m, g), "pings", idx("pings", config.GAMENIGHT_ROLE))
        assert m.added == [g.role(config.GAMENIGHT_ROLE)]
    with_cog(go)


@pytest.mark.parametrize("key, index", [("platform", 99), ("games", 0), ("keeper", 0)])
def test_unknown_buttons_do_nothing(key, index):
    async def go(cog, g, db):
        m = FakeMember(1)
        i = FakeInteraction(m, g)
        await cog.handle_button(i, key, index)
        assert m.added == [] and m.removed == []
        assert "isn't available" in i.last()["content"]
    with_cog(go)


def test_button_for_role_above_bot_is_refused():
    async def go(cog, g, db):
        g.role("PC").position = 99
        m = FakeMember(1)
        i = FakeInteraction(m, g)
        await cog.handle_button(i, "platform", idx("platform", "PC"))
        assert m.added == [] and "isn't available" in i.last()["content"]
    with_cog(go)


def test_button_outside_the_server_is_refused():
    async def go(cog, g, db):
        m = FakeMember(1)
        i = FakeInteraction(m, None)
        await cog.handle_button(i, "platform", 0)
        assert m.added == [] and "in the server" in i.last()["content"]
    with_cog(go)


def test_forbidden_role_change_replies_politely():
    async def go(cog, g, db):
        m = FakeMember(1)
        m.fail = http_error(discord.Forbidden, 403)
        i = FakeInteraction(m, g)
        await cog.handle_button(i, "platform", 0)
        assert "can't change" in i.last()["content"] and i.last()["ephemeral"] is True
    with_cog(go)


def test_custom_id_template_only_matches_known_sections():
    t = RoleButton.__discord_ui_compiled_template__
    assert t.fullmatch("selfroles:platform:3")
    assert not t.fullmatch("selfroles:keeper:3")
    assert not t.fullmatch("selfroles:platform:123")
    assert GameSelect.__discord_ui_compiled_template__.fullmatch("selfroles:games")


def test_button_callback_error_goes_to_reply_error():
    async def go(cog, g, db):
        async def boom(*a):
            raise RuntimeError("x")
        cog.handle_button = boom
        m = FakeMember(1)
        i = FakeInteraction(m, g)
        i.client = SimpleNamespace(get_cog=lambda name: cog)
        await RoleButton("platform", 0).callback(i)
        assert i.last()["ephemeral"] is True
    with_cog(go)


# ---------------------------------------------------------------- games select
def test_select_toggles_each_picked_game():
    async def go(cog, g, db):
        a, b, c = (config.GAMES[k].role for k in range(3))
        m = FakeMember(1, roles=[g.role(b)])
        i = FakeInteraction(m, g)
        await cog.handle_select(i, ["0", "1", "nope", "999"])
        assert m.added == [g.role(a)] and m.removed == [g.role(b)]
        assert i.last()["content"] == f"Added {a}. Removed {b}."
    with_cog(go)


def test_select_with_only_junk_values_changes_nothing():
    async def go(cog, g, db):
        m = FakeMember(1)
        i = FakeInteraction(m, g)
        await cog.handle_select(i, ["-1", "abc"])
        assert m.added == [] and "aren't available" in i.last()["content"]
    with_cog(go)


def test_select_callback_reads_values_from_the_item():
    async def go(cog, g, db):
        got = []

        async def handle(inter, values):
            got.append(values)
        cog.handle_select = handle
        item = GameSelect([discord.SelectOption(label="x", value="2")])
        item.item._values = ["2"]
        i = FakeInteraction(FakeMember(1), g)
        i.client = SimpleNamespace(get_cog=lambda name: cog)
        i.data = {"values": ["2"]}
        await item.callback(i)
        assert got == [["2"]]
    with_cog(go)


# ---------------------------------------------------------------- /roles
def test_roles_command_shows_panel_ephemerally():
    async def go(cog, g, db):
        i = FakeInteraction(FakeMember(1), g)
        await cog.roles.callback(cog, i)
        [(kind, kw)] = i.calls
        assert kind == "send_message" and kw["ephemeral"] is True
        assert "selfroles:region:0" in custom_ids(kw["view"])
        assert g.channel.sent == []
    with_cog(go)


def test_roles_command_with_no_roles():
    async def go(cog, g, db):
        i = FakeInteraction(FakeMember(1), g)
        await cog.roles.callback(cog, i)
        assert "No self-assign roles" in i.last()["content"]
    with_cog(go, guild=FakeGuild(names=[]))


def test_role_events_debounce_one_refresh(monkeypatch):
    async def go(cog, g, db):
        monkeypatch.setattr(cogmod, "REFRESH_DELAY", 0)
        cog.schedule_refresh()
        first = cog.refresh_task
        cog.schedule_refresh()
        assert cog.refresh_task is first
        await first
        assert len(g.channel.sent) == 1
    with_cog(go)
