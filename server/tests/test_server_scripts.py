"""Tests for setup_server, public_mode and snapshot_server against fake guilds (no Discord)."""

import asyncio
from types import SimpleNamespace

import discord

import layout
import public_mode
import setup_server
import snapshot_server
from names import slug


class NS(SimpleNamespace):
    __hash__ = object.__hash__


class FakeRole(NS):
    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position

    def is_default(self):
        return self.name == "@everyone"


def aiter_of(items):
    async def gen():
        for i in items:
            yield i
    return gen()


class FakeGuild:
    """Records create_* calls; returns simple namespaces for what it creates."""

    def __init__(self, roles=()):
        self.default_role = FakeRole(id=1, name="@everyone", position=0, managed=False)
        self.me = NS(id=2, name="Front Desk", top_role=FakeRole(id=3, name="Front Desk", position=50))
        self.roles = [self.default_role, self.me.top_role, *roles]
        self.categories, self.text_channels, self.voice_channels, self.forums = [], [], [], []
        self.calls = []
        self._next = 100

    @property
    def channels(self):
        return [*self.categories, *self.text_channels, *self.voice_channels, *self.forums]

    def get_channel(self, cid):
        return next((c for c in self.channels if c.id == cid), None)

    def _make(self, kind, name, **kw):
        self._next += 1
        self.calls.append((kind, name, kw))
        cat = kw.get("category")
        return NS(id=self._next, name=name, category_id=cat.id if cat else None,
                  overwrites=kw.get("overwrites", {}), **({"edit": _async_none} if kind == "forum" else {}))

    async def create_category(self, name, **kw):
        return self._make("category", name, **kw)

    async def create_text_channel(self, name, **kw):
        return self._make("text", name, **kw)

    async def create_voice_channel(self, name, **kw):
        return self._make("voice", name, **kw)

    async def create_forum(self, name, **kw):
        return self._make("forum", name, **kw)

    async def create_role(self, **kw):
        self.calls.append(("role", kw["name"], kw))
        return FakeRole(id=0, position=1, **kw)


async def _async_none(**_):
    return None


# ------------------------------------------------------------ finding 14


def test_uncached_member_overwrite_targets_are_fetched_by_name():
    # discord.py 2.7 builds uncached member targets as Object(type=User), never Member.
    alice = discord.Object(id=555, type=discord.User)
    role_target = discord.Object(id=777, type=discord.Role)
    fetched = []

    async def fetch_member(mid):
        fetched.append(mid)
        return NS(name="alice")

    guild = NS(
        channels=[NS(overwrites={alice: None, role_target: None})],
        fetch_member=fetch_member,
    )
    names = asyncio.run(snapshot_server.member_names_for_overwrites(guild))
    assert names == {555: "alice"}
    assert fetched == [555]


# ------------------------------------------------------------ finding 15


def test_make_posts_finds_pinned_post_after_joins_push_it_out_of_history(monkeypatch):
    guild = FakeGuild()
    me = guild.me
    post_specs = [s for s in setup_server.layout_channels() if s.get("post")]
    m = setup_server.Makeover(guild, apply=True)
    pinned = {}
    for spec in post_specs:
        ch = guild._make("text", spec["name"])
        guild.text_channels.append(ch)
        m.channels[slug(spec["name"])] = ch
        old = NS(author=me, embeds=[object()], pinned=True)
        pinned[ch.id] = old
        joins = [NS(author=NS(id=900 + i), embeds=[]) for i in range(60)]
        ch.pins = lambda limit=50, old=old: aiter_of([old])
        ch.history = lambda limit=50, joins=joins: aiter_of(joins[:limit])

        async def send(**_):
            raise AssertionError("posted a duplicate")
        ch.send = send

    refreshed = []

    async def refresh_post(key, msg, post):
        refreshed.append(msg)
    monkeypatch.setattr(m, "refresh_post", refresh_post)

    asyncio.run(m.make_posts())
    assert len(refreshed) == len(post_specs)
    assert all(msg in pinned.values() for msg in refreshed)


def test_find_post_skips_pin_notices_in_history():
    guild = FakeGuild()
    me = guild.me
    post = NS(author=me, embeds=[object()], pinned=False)
    notice = NS(author=me, embeds=[])  # "Front Desk pinned a message", newer than the post
    ch = NS(pins=lambda limit=50: aiter_of([]), history=lambda limit=50: aiter_of([notice, post]))
    assert asyncio.run(setup_server.Makeover(guild, apply=True).find_post(ch)) is post


# ------------------------------------------------------------ finding 16


def _hidden(overwrites, guild):
    ow = overwrites.get(guild.default_role)
    return ow is not None and ow.view_channel is False


def test_setup_creates_private_to_spaces_hidden_from_everyone():
    keeper = FakeRole(id=10, name="Keeper", position=5, managed=False)
    squad = FakeRole(id=11, name="Squad", position=4, managed=False)
    mod = FakeRole(id=12, name="Moderator", position=6, managed=False)
    guild = FakeGuild(roles=[keeper, squad, mod])
    m = setup_server.Makeover(guild, apply=True)
    m.roles = {"Keeper": keeper, "Squad": squad}
    asyncio.run(m.make_channels())

    created = {name: kw for _, name, kw in guild.calls}
    private_names = set()
    for cat_spec in layout.CATEGORIES:
        if cat_spec.get("private_to"):
            private_names.add(cat_spec["name"])
        for spec in cat_spec["channels"]:
            if spec.get("private_to") or cat_spec.get("private_to"):
                private_names.add(spec["name"])
    assert {"06 · squad", "🔒・squad-chat", "🔒 Squad Only", "📋・mod-log"} <= private_names
    for name in private_names:
        ow = created[name].get("overwrites", {})
        assert _hidden(ow, guild), name
        assert ow[guild.me].view_channel is True
    assert created["06 · squad"]["overwrites"][squad].view_channel is True
    assert created["📋・mod-log"]["overwrites"][mod].view_channel is True
    assert not _hidden(created["💬・general"].get("overwrites", {}), guild)
    assert not _hidden(created["02 · the lobby"].get("overwrites", {}), guild)


# ------------------------------------------------------------ finding 17


def test_public_mode_creates_bot_roles_with_layout_hoist():
    guild = FakeGuild()
    guild.me.guild_permissions = NS(**{p: True for p in public_mode.NEEDED})
    asyncio.run(public_mode.PublicMode(guild, apply=True).roles())
    made = {name: kw for kind, name, kw in guild.calls if kind == "role"}
    assert made["Season Champ"]["hoist"] is True
    assert made["Hype"]["hoist"] is True
    assert made["Recruiter"]["hoist"] is False


def test_public_mode_hoists_existing_unhoisted_role():
    edits = []

    async def edit(**kw):
        edits.append(kw)

    hype = FakeRole(id=20, name="Hype", position=4, hoist=False, edit=edit)
    guild = FakeGuild(roles=[
        hype,
        *(FakeRole(id=21 + i, name=n, position=3, hoist=n in ("Season Champ", "Birthday"))
          for i, n in enumerate(public_mode.BOT_ROLES) if n != "Hype"),
    ])
    asyncio.run(public_mode.PublicMode(guild, apply=True).roles())
    assert edits == [{"hoist": True, "reason": "Public server"}]
    assert not [c for c in guild.calls if c[0] == "role"]
