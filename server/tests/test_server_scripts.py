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


# ------------------------------------------------------------ onboarding prompts (self-assign roles)


def _onboarding_guild(prompts=(), role_names=None):
    names = role_names if role_names is not None else [
        o["role"] for p in public_mode.ONBOARDING_PROMPTS for o in p["options"]]
    guild = FakeGuild(roles=[FakeRole(id=200 + i, name=n, position=5) for i, n in enumerate(names)])
    ob = NS(prompts=list(prompts))
    edits = []

    async def onboarding():
        return ob

    async def edit_onboarding(**kw):
        edits.append(kw)
    guild.onboarding = onboarding
    guild.edit_onboarding = edit_onboarding
    return guild, edits


def _existing_prompt(title):
    return discord.OnboardingPrompt(type=discord.OnboardingPromptType.multiple_choice, title=title,
                                    options=[discord.OnboardingPromptOption(title="x", roles=[discord.Object(9)])])


def test_onboarding_prompts_added_after_existing_ones():
    old = [_existing_prompt("What do you play?"), _existing_prompt("Want squad pings?")]
    guild, edits = _onboarding_guild(old)
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    [kw] = edits
    assert set(kw) == {"prompts"}  # default channels and the enabled flag are left alone
    prompts = kw["prompts"]
    assert prompts[:2] == old  # never removed or reordered
    new = {p.title: p for p in prompts[2:]}
    assert list(new) == ["What do you play on?", "Where are you?", "Want pings?"]
    assert new["What do you play on?"].single_select is False
    assert new["Where are you?"].single_select is True
    assert all(p.required is False for p in new.values())
    by_name = {r.name: r.id for r in guild.roles}
    assert [o.role_ids for o in new["What do you play on?"].options] == [
        {by_name[n]} for n in ("PC", "PlayStation", "Xbox", "Switch", "Mobile")]
    assert [o.role_ids for o in new["Want pings?"].options] == [{by_name["Game Night"]}]


def test_onboarding_prompts_dry_run_changes_nothing(capsys):
    guild, edits = _onboarding_guild()
    asyncio.run(public_mode.PublicMode(guild, apply=False).onboarding_prompts())
    assert edits == []
    assert "Where are you?" in capsys.readouterr().out


def test_onboarding_prompts_rerun_is_a_noop():
    have = [_existing_prompt(p["title"]) for p in public_mode.ONBOARDING_PROMPTS]
    guild, edits = _onboarding_guild(have)
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    assert edits == []


def test_onboarding_prompts_skip_missing_roles():
    guild, edits = _onboarding_guild(role_names=["PC", "Xbox"])
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    [kw] = edits
    [p] = kw["prompts"]  # region and ping roles don't exist yet: those prompts wait for a re-run
    assert p.title == "What do you play on?"
    assert [o.title for o in p.options] == ["PC", "Xbox"]


def test_onboarding_prompts_respect_discord_limit():
    full = [_existing_prompt(f"q{i}") for i in range(public_mode.MAX_PROMPTS - 1)]
    guild, edits = _onboarding_guild(full)
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    assert edits == []


def test_onboarding_prompts_only_platform_is_pre_join():
    guild, edits = _onboarding_guild([_existing_prompt("What do you play?")])
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    new = {p.title: p for p in edits[0]["prompts"][1:]}
    assert new["What do you play on?"].in_onboarding is True
    assert new["Where are you?"].in_onboarding is False
    assert new["Want pings?"].in_onboarding is False


def test_onboarding_prompts_fall_back_to_post_join_when_discord_caps_questions():
    guild, edits = _onboarding_guild([_existing_prompt("What do you play?")])

    class Resp:
        status, reason = 400, "Bad Request"

    async def edit_onboarding(**kw):
        edits.append(kw)
        if len(edits) == 1:
            raise discord.HTTPException(Resp(), {"code": 50035, "message": "Invalid Form Body",
                                                 "errors": {"prompts": {"_errors": [{"code": "x",
                                                 "message": "Too many questions in onboarding."}]}}})
    guild.edit_onboarding = edit_onboarding
    asyncio.run(public_mode.PublicMode(guild, apply=True).onboarding_prompts())
    assert len(edits) == 2
    assert edits[1]["prompts"][0].title == "What do you play?"
    assert all(p.in_onboarding is False for p in edits[1]["prompts"][1:])
