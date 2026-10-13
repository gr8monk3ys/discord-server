"""The games list (layout.GAMES / RETIRED_GAMES) and public_mode's games steps, against a fake
guild that looks like the server before the list changed. No Discord.

Retiring is the one thing public_mode does that takes something away, so these tests pin both
directions: a retired game is hidden (never deleted) and a game moved back is restored.
"""

import asyncio
import types
from pathlib import Path

import discord
import pytest

import layout
import public_mode
from names import slug
from test_server_scripts import NS, FakeGuild, FakeRole

OLD_GAMES = [  # what the server listed before: every one of these has a role, a channel and an option
    ("🪖", "wardogs", "Wardogs"), ("⛏️", "minecraft", "Minecraft"), ("🪂", "fortnite", "Fortnite"),
    ("🎯", "valorant", "Valorant"), ("💥", "call-of-duty", "Call of Duty"),
    ("🧙", "league-of-legends", "League of Legends"), ("💣", "counter-strike", "Counter-Strike 2"),
    ("🔺", "apex-legends", "Apex Legends"), ("🚗", "gta-online", "GTA Online"), ("🧱", "roblox", "Roblox"),
]
NEW = ["Risk of Rain 2", "Gears of War", "Overwatch", "Marvel Rivals", "Tabletop Simulator"]
RETIRED = ["Fortnite", "Valorant", "Call of Duty", "League of Legends", "Counter-Strike 2", "GTA Online", "Roblox"]
GAMES_CAT = "03 · games"
ARCHIVE = "99 · archive"


def never(what):
    async def refuse(*_, **__):
        raise AssertionError(f"{what} must never happen")
    return refuse


class LiveGuild(FakeGuild):
    """FakeGuild whose created roles, categories and channels join the guild, so a second
    run sees the first one's work."""

    def __init__(self):
        super().__init__()
        self.name = "test"
        self.me.guild_permissions = discord.Permissions.all()
        self.onboarding_edits = []
        self.ob = NS(prompts=[], default_channels=[])

    def add_role(self, name, position=5, hoist=False):
        role = FakeRole(id=300 + len(self.roles), name=name, position=position, managed=False, hoist=hoist,
                        edit=never(f"editing @{name}"), delete=never(f"deleting @{name}"))
        self.roles.append(role)
        return role

    def add_category(self, name, overwrites=None):
        cat = NS(id=1000 + len(self.categories), name=name, overwrites=dict(overwrites or {}), edits=[],
                 delete=never(f"deleting {name}"), permissions_for=lambda me: discord.Permissions.all())
        cat.overwrites_for = lambda target: cat.overwrites.get(target, discord.PermissionOverwrite())

        async def edit(**kw):
            cat.edits.append(kw)
            cat.overwrites = dict(kw.get("overwrites", cat.overwrites))
        cat.edit = edit
        self.categories.append(cat)
        return cat

    def add_text(self, name, category):
        ch = NS(id=2000 + len(self.text_channels), name=name, category_id=category.id if category else None,
                overwrites={}, edits=[], delete=never(f"deleting #{name}"),
                permissions_for=lambda me: discord.Permissions.all())

        async def edit(**kw):
            ch.edits.append(kw)
            if "category" in kw:
                ch.category_id = kw["category"].id
        ch.edit = edit
        self.text_channels.append(ch)
        return ch

    def add_forum(self, name, tags):
        forum = NS(id=3000 + len(self.forums), name=name, category_id=None, edits=[],
                   available_tags=[discord.ForumTag(name=n, emoji=e) for n, e in tags],
                   delete=never(f"deleting #{name}"))

        async def edit(**kw):
            forum.edits.append(kw)
            forum.available_tags = list(kw.get("available_tags", forum.available_tags))
        forum.edit = edit
        self.forums.append(forum)
        return forum

    # what the scripts call
    async def create_role(self, **kw):
        self.calls.append(("role", kw["name"], kw))
        return self.add_role(kw["name"], position=1, hoist=kw["hoist"])

    async def create_category(self, name, **kw):
        self.calls.append(("category", name, kw))
        return self.add_category(name, kw.get("overwrites"))

    async def create_text_channel(self, name, **kw):
        self.calls.append(("text", name, kw))
        return self.add_text(name, kw.get("category"))

    async def create_forum(self, name, **kw):
        self.calls.append(("forum", name, kw))
        return self.add_forum(name, [])

    async def onboarding(self):
        return self.ob

    async def edit_onboarding(self, **kw):
        self.onboarding_edits.append(kw)
        self.ob.prompts = list(kw["prompts"])

    # reading the result
    def category_of(self, channel_slug):
        ch = next(c for c in self.text_channels if slug(c.name) == slug(channel_slug))
        return next((c.name for c in self.categories if c.id == ch.category_id), None)

    def options(self):
        prompt = next(p for p in self.ob.prompts if p.title == public_mode.GAMES_PROMPT)
        return [o.title for o in prompt.options]


def old_server():
    """The server as it was: the ten old games, each with a role, a channel in the games
    category, a tag in both game forums and an option in "What do you play?"."""
    g = LiveGuild()
    g.add_role("Keeper", position=60)
    g.add_role("Moderator", position=55)
    g.add_role("Guest")
    game_roles = {r for _, _, r in (*layout.GAMES, *layout.RETIRED_GAMES)}
    for name in public_mode.BOT_ROLES:  # everything Front Desk hands out exists, except the new games' roles
        if name not in game_roles:
            g.add_role(name, hoist=next(r for r in layout.ROLES if r["name"] == name).get("hoist", False))
    for cat_spec in layout.CATEGORIES:
        g.add_category(cat_spec["name"])
    games = next(c for c in g.categories if c.name == GAMES_CAT)
    # Every layout channel that isn't a game channel already exists, so the only things the
    # scripts can create are the new games' channels.
    game_slugs = {slug(c) for _, c, _ in (*layout.GAMES, *layout.RETIRED_GAMES)}
    for cat_spec in layout.CATEGORIES:
        cat = next(c for c in g.categories if c.name == cat_spec["name"])
        for spec in cat_spec["channels"]:
            if slug(spec["name"]) in game_slugs:
                continue
            kind = spec.get("type", "text")
            if kind == "forum":
                extra = [(n, e) for n, e in spec.get("tags", []) if n not in {r for _, _, r in layout.GAMES}]
                has_game_tags = len(extra) != len(spec.get("tags", []))
                g.add_forum(spec["name"], [*((r, e) for e, _, r in OLD_GAMES), *extra] if has_game_tags else extra)
            elif kind == "voice":
                g.voice_channels.append(NS(id=4000 + len(g.voice_channels), name=spec["name"], category_id=cat.id))
            elif kind == "stage":
                g.stage_channels.append(NS(id=5000, name=spec["name"], category_id=cat.id,
                                           permissions_for=lambda me: discord.Permissions.all()))
            else:
                g.add_text(spec["name"], cat)
    options = []
    for emoji, channel, role_name in OLD_GAMES:
        role = g.add_role(role_name)
        ch = g.add_text(f"{emoji}・{channel}", games)
        options.append(discord.OnboardingPromptOption(title=role_name, emoji=emoji, roles=[role], channels=[ch]))
    options.append(discord.OnboardingPromptOption(title="Just here to hang", emoji="🛋️",
                                                  roles=[next(r for r in g.roles if r.name == "Guest")]))
    g.ob.prompts = [
        discord.OnboardingPrompt(type=discord.OnboardingPromptType.multiple_choice, title="What do you play?",
                                 options=options, single_select=False, required=True),
        discord.OnboardingPrompt(type=discord.OnboardingPromptType.multiple_choice, title="Want squad pings?",
                                 options=[discord.OnboardingPromptOption(title="Ping me for squads", emoji="🎮")]),
    ]
    return g


async def games_steps(pm):
    """The steps that act on the games list, in the order run() uses."""
    await pm.roles()
    await pm.new_channels()
    await pm.game_tags()
    await pm.restore_games()
    await pm.game_options()
    await pm.retire_games()


def apply(guild):
    asyncio.run(games_steps(public_mode.PublicMode(guild, apply=True)))


def moved_layout(role_name, to):
    """layout.py as it would be after staff move one game's line into GAMES or RETIRED_GAMES:
    the real file with that one line moved, loaded as a module."""
    src = Path(layout.__file__).read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    (i,) = [n for n, line in enumerate(lines) if line.lstrip().startswith("(") and f'"{role_name}"),' in line]
    line = lines.pop(i)
    (head,) = [n for n, text in enumerate(lines) if text.startswith(f"{to} = [")]
    lines.insert(head + 1, line)
    mod = types.ModuleType("layout_moved")
    mod.__file__ = layout.__file__
    exec(compile("".join(lines), layout.__file__, "exec"), mod.__dict__)
    return mod


# ------------------------------------------------------------ the list itself


def test_the_list_is_the_eight_games_in_the_agreed_order_and_the_seven_are_retired():
    assert [role for _, _, role in layout.GAMES] == [
        "Risk of Rain 2", "Gears of War", "Overwatch", "Marvel Rivals", "Minecraft", "Tabletop Simulator",
        "Wardogs", "Apex Legends"]
    assert sorted(role for _, _, role in layout.RETIRED_GAMES) == sorted(RETIRED)
    assert {g for g in OLD_GAMES} == {*layout.RETIRED_GAMES, *(g for g in layout.GAMES if g in OLD_GAMES)}


def test_listed_and_retired_games_never_overlap_and_every_emoji_is_its_own():
    both = [*layout.GAMES, *layout.RETIRED_GAMES]
    for i in (1, 2):  # channel slug, role name
        keys = [slug(g[i]) for g in both]
        assert len(set(keys)) == len(keys)
    for emoji, channel, role in both:
        assert channel == channel.lower() and " " not in channel and 0 < len(role) <= 20  # a forum tag name
    game_emoji = [e for e, _, _ in both]
    assert len(set(game_emoji)) == len(game_emoji)
    game_names = {f"{e}・{c}" for e, c, _ in layout.GAMES}
    others = [s["name"] for c in layout.CATEGORIES for s in c["channels"] if s["name"] not in game_names]
    used = {name.split("・")[0].split(" ")[0] for name in others}
    # ⛏️ is shared on purpose: #minecraft and #realms are the same game.
    assert not (set(game_emoji) - {"⛏️"}) & used
    tags = {e for c in layout.CATEGORIES for s in c["channels"] for n, e in s.get("tags", [])
            if n not in {r for _, _, r in layout.GAMES}}
    assert not {e for e, _, _ in layout.GAMES} & tags  # a game's tag never looks like "Settings" or "Ranked"


def test_retired_games_are_offered_nowhere_but_their_roles_stay_declared():
    retired_roles = {r for _, _, r in layout.RETIRED_GAMES}
    retired_channels = {slug(c) for _, c, _ in layout.RETIRED_GAMES}
    specs = [s for c in layout.CATEGORIES for s in c["channels"]]
    assert not retired_channels & {slug(s["name"]) for s in specs}
    assert not retired_roles & {n for s in specs for n, _ in s.get("tags", [])}
    for prompt in layout.ONBOARDING_PROMPTS:
        assert not retired_roles & {o.get("role") for o in prompt["options"]}
        assert not retired_roles & {o["title"] for o in prompt["options"]}
    assert not retired_channels & {slug(n) for n in layout.ONBOARDING_DEFAULT_CHANNELS}
    assert not retired_roles & set(public_mode.BOT_ROLES)  # the script never creates a retired role
    assert retired_roles <= {r["name"] for r in layout.ROLES}  # left in place, in the same spot
    assert {r for _, _, r in layout.GAMES} <= set(public_mode.BOT_ROLES)
    # The archive is not a layout category: nothing else creates, fills or reorders it.
    assert slug(layout.GAME_ARCHIVE["name"]) not in {slug(c["name"]) for c in layout.CATEGORIES}
    assert set(layout.GAME_ARCHIVE["private_to"]) == {"Keeper", "Moderator"}
    assert len(layout.PUBLIC["description"]) <= 120
    assert layout.PUBLIC["description"].startswith("A small, friendly co-op and PvE server.")
    assert not any(r in layout.PUBLIC["description"] for r in retired_roles)


# ------------------------------------------------------------ retiring


def test_dry_run_prints_every_channel_it_would_hide_and_changes_nothing(capsys):
    guild = old_server()
    before = {c.name: c.category_id for c in guild.text_channels}
    asyncio.run(games_steps(public_mode.PublicMode(guild, apply=False)))
    assert guild.calls == [] and guild.onboarding_edits == []
    assert {c.name: c.category_id for c in guild.text_channels} == before
    assert all(not c.edits for c in (*guild.text_channels, *guild.forums, *guild.categories))
    out = capsys.readouterr().out
    assert "\nRetired games (hidden, never deleted)\n" in out
    for emoji, channel, role in layout.RETIRED_GAMES:
        assert (f"  hide    #{emoji}・{channel}: move from {GAMES_CAT} into {ARCHIVE} "
                "(only @Keeper, @Moderator see it; messages are kept)\n") in out, role
        assert f"  keep    @{role} (the role stays, and so does every member's copy of it)\n" in out
    assert out.count("  hide    ") == len(layout.RETIRED_GAMES)  # exactly those, nothing else
    assert f"  create  category {ARCHIVE} (hidden from @everyone; visible to @Keeper, @Moderator)\n" in out
    assert ('  remove  "What do you play?" options: Fortnite, Valorant, Call of Duty, League of Legends, '
            "Counter-Strike 2, GTA Online, Roblox (members who picked one keep the role)\n") in out
    assert '  add     "What do you play?" options: ' + ", ".join(NEW) + "\n" in out
    for role in NEW:
        assert f"  create  @{role}\n" in out
    assert "tags: " + ", ".join(NEW) in out
    assert "delete" not in out.lower().replace("never deleted", "")


def test_apply_adds_the_new_games_and_hides_the_retired_ones_without_deleting_anything():
    guild = old_server()
    held = {r.name: r for r in guild.roles}
    apply(guild)  # any delete, or any edit of a role, raises

    # new games: a role at the bottom (no position), a channel in the games category
    made_roles = {name: kw for kind, name, kw in guild.calls if kind == "role"}
    assert set(made_roles) == set(NEW)
    assert all(kw["mentionable"] is True and "position" not in kw for kw in made_roles.values())
    made_text = {name: kw for kind, name, kw in guild.calls if kind == "text"}
    assert {slug(n) for n in made_text} == {slug(c) for _, c, r in layout.GAMES if r in NEW}
    assert all(kw["category"].name == GAMES_CAT and "overwrites" not in kw for kw in made_text.values())

    # the question: listed games in list order, then whatever else was there; retired ones gone
    assert guild.options() == [*(r for _, _, r in layout.GAMES), "Just here to hang"]
    assert all(set(kw) == {"prompts"} for kw in guild.onboarding_edits)  # default channels untouched
    assert [p.title for p in guild.ob.prompts] == ["What do you play?", "Want squad pings?"]
    prompt = guild.ob.prompts[0]
    by_title = {o.title: o for o in prompt.options}
    for emoji, channel, role in layout.GAMES:
        ch = next(c for c in guild.text_channels if slug(c.name) == slug(channel))
        assert by_title[role].channel_ids == {ch.id} and len(by_title[role].role_ids) == 1, role
    assert by_title["Wardogs"].role_ids == {held["Wardogs"].id}  # an option that was there is the same object

    # retired channels: moved into a new private category, permissions synced to it
    archive = next(c for c in guild.categories if c.name == ARCHIVE)
    everyone = archive.overwrites[guild.default_role]
    assert everyone.view_channel is False and archive.overwrites[guild.me].view_channel is True
    seen = {t.name for t, ow in archive.overwrites.items() if ow.view_channel and t is not guild.me}
    assert seen == {"Keeper", "Moderator"}
    for emoji, channel, role in layout.RETIRED_GAMES:
        ch = next(c for c in guild.text_channels if slug(c.name) == slug(channel))
        assert ch.category_id == archive.id, role
        (edit,) = ch.edits
        assert edit["category"] is archive and edit["sync_permissions"] is True and "RETIRED_GAMES" in edit["reason"]
        assert ch.name == f"{emoji}・{channel}"  # not renamed
    for _, channel, role in layout.GAMES:
        assert guild.category_of(channel) == GAMES_CAT, role
        assert not next(c for c in guild.text_channels if slug(c.name) == slug(channel)).edits

    # a member who holds a retired role: the role object is the same one, untouched
    for role in RETIRED:
        assert held[role] in guild.roles
    assert len(guild.text_channels) == len({c.id for c in guild.text_channels})

    # forums: new tags added after the old ones, which all stay (tagged posts keep their tag)
    for forum in guild.forums:
        names = [t.name for t in forum.available_tags]
        if "Wardogs" not in names:
            assert not forum.edits  # a forum that doesn't tag by game
            continue
        assert names[:len(OLD_GAMES)] == [r for _, _, r in OLD_GAMES]
        assert set(NEW) <= set(names) and len(names) <= public_mode.MAX_FORUM_TAGS
        assert len(forum.edits) == 1


def test_a_second_run_changes_nothing(capsys):
    guild = old_server()
    apply(guild)
    guild.calls.clear()
    guild.onboarding_edits.clear()
    for c in (*guild.text_channels, *guild.forums, *guild.categories):
        c.edits.clear()
    capsys.readouterr()
    apply(guild)
    assert guild.calls == [] and guild.onboarding_edits == []
    assert all(not c.edits for c in (*guild.text_channels, *guild.forums, *guild.categories))
    out = capsys.readouterr().out
    assert "hide" not in out and "restore" not in out and "  remove  " not in out
    assert out.count(f"(already in {ARCHIVE})") == len(layout.RETIRED_GAMES)
    assert "ok      every listed game is an option" in out


def test_nothing_is_hidden_while_onboarding_cannot_be_read(capsys):
    guild = old_server()

    class Resp:
        status, reason = 403, "Forbidden"

    async def onboarding():
        raise discord.HTTPException(Resp(), "Missing Access")
    guild.onboarding = onboarding
    asyncio.run(public_mode.PublicMode(guild, apply=True).retire_games())
    assert guild.calls == [] and all(not c.edits for c in guild.text_channels)
    assert "nothing hidden" in capsys.readouterr().out


def test_the_question_is_never_left_empty(capsys):
    guild = old_server()
    prompt = guild.ob.prompts[0]
    prompt.options = [o for o in prompt.options if o.title in RETIRED]
    asyncio.run(public_mode.PublicMode(guild, apply=True).retire_games())
    assert guild.onboarding_edits == [] and all(not c.edits for c in guild.text_channels)
    assert "would be left without options" in capsys.readouterr().out


def test_a_game_listed_twice_by_mistake_is_never_hidden(monkeypatch, capsys):
    guild = old_server()
    monkeypatch.setattr(layout, "RETIRED_GAMES", [*layout.RETIRED_GAMES, ("⛏️", "minecraft", "Minecraft")])
    asyncio.run(public_mode.PublicMode(guild, apply=True).retire_games())
    assert guild.category_of("minecraft") == GAMES_CAT
    assert "the layout still lists it" in capsys.readouterr().out


def test_an_archive_that_is_not_private_is_locked_before_anything_moves_in():
    guild = old_server()
    open_archive = guild.add_category(ARCHIVE)
    asyncio.run(public_mode.PublicMode(guild, apply=True).retire_games())
    (edit,) = open_archive.edits
    assert edit["overwrites"][guild.default_role].view_channel is False
    assert not [c for c in guild.calls if c[0] == "category"]
    assert guild.category_of("valorant") == ARCHIVE


def test_forum_tags_stop_at_discords_limit(capsys):
    guild = old_server()
    forum = next(f for f in guild.forums if "Wardogs" in {t.name for t in f.available_tags})
    forum.available_tags += [discord.ForumTag(name=f"extra {i}") for i in range(public_mode.MAX_FORUM_TAGS)]
    asyncio.run(public_mode.PublicMode(guild, apply=True).game_tags())
    assert not forum.edits
    assert "would pass Discord's limit of 20" in capsys.readouterr().out


# ------------------------------------------------------------ the one-line move, both ways


def test_moving_a_line_back_into_games_restores_the_game(monkeypatch, capsys):
    guild = old_server()
    apply(guild)  # Valorant is now archived, with no option
    assert guild.category_of("valorant") == ARCHIVE and "Valorant" not in guild.options()
    valorant = next(c for c in guild.text_channels if slug(c.name) == "valorant")
    valorant.edits.clear()
    guild.calls.clear()

    again = moved_layout("Valorant", "GAMES")
    assert [r for _, _, r in again.GAMES][0] == "Valorant" and "Valorant" not in {r for _, _, r in again.RETIRED_GAMES}
    monkeypatch.setattr(public_mode, "layout", again)

    capsys.readouterr()
    asyncio.run(games_steps(public_mode.PublicMode(guild, apply=False)))
    out = capsys.readouterr().out
    assert (f"  restore #🎯・valorant: move from {ARCHIVE} back into {GAMES_CAT} "
            "(public again, with that category's permissions)\n") in out
    assert '  add     "What do you play?" options: Valorant\n' in out
    assert "hide" not in out and not valorant.edits

    asyncio.run(games_steps(public_mode.PublicMode(guild, apply=True)))
    assert guild.category_of("valorant") == GAMES_CAT
    (edit,) = valorant.edits
    assert edit["category"].name == GAMES_CAT and edit["sync_permissions"] is True
    assert guild.options() == ["Valorant", *(r for _, _, r in layout.GAMES), "Just here to hang"]
    option = next(o for o in guild.ob.prompts[0].options if o.title == "Valorant")
    assert option.channel_ids == {valorant.id}
    assert option.role_ids == {next(r.id for r in guild.roles if r.name == "Valorant")}  # the role it always had
    assert not [c for c in guild.calls if c[0] in ("role", "text", "category")]  # nothing recreated
    for role in RETIRED:
        if role != "Valorant":
            assert guild.category_of(next(c for _, c, r in layout.RETIRED_GAMES if r == role)) == ARCHIVE
    for forum in guild.forums:  # its tags were never removed, so none is added twice
        names = [t.name for t in forum.available_tags]
        assert names.count("Valorant") <= 1


def test_moving_a_line_into_retired_games_retires_a_listed_game(monkeypatch):
    guild = old_server()
    apply(guild)
    wardogs = next(c for c in guild.text_channels if slug(c.name) == "wardogs")
    role = next(r for r in guild.roles if r.name == "Wardogs")

    monkeypatch.setattr(public_mode, "layout", moved_layout("Wardogs", "RETIRED_GAMES"))
    asyncio.run(games_steps(public_mode.PublicMode(guild, apply=True)))
    assert guild.category_of("wardogs") == ARCHIVE
    assert wardogs.edits[-1]["sync_permissions"] is True
    assert "Wardogs" not in guild.options() and len(guild.options()) == len(layout.GAMES)
    assert role in guild.roles  # kept; editing or deleting it would have raised

    # and back again: the same channel, the same role, public once more
    monkeypatch.setattr(public_mode, "layout", layout)
    asyncio.run(games_steps(public_mode.PublicMode(guild, apply=True)))
    assert guild.category_of("wardogs") == GAMES_CAT
    assert guild.options() == [*(r for _, _, r in layout.GAMES), "Just here to hang"]
    assert len([c for c in guild.text_channels if slug(c.name) == "wardogs"]) == 1


@pytest.mark.parametrize("step", ["game_tags", "restore_games", "game_options", "retire_games"])
def test_every_games_step_is_safe_on_a_server_with_nothing(step):
    guild = LiveGuild()
    asyncio.run(getattr(public_mode.PublicMode(guild, apply=True), step)())
    assert guild.calls == [] and guild.onboarding_edits == []
