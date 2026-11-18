"""The 2026-11-17 cut: layout.RETIRED_CHANNELS, layout.GAME_CHANNELS and public_mode's
retire_channels step, against a stand-in for the server as it was before the cut. No Discord.
tests/before_cut holds server/snapshot's channels, Onboarding and roles from that day (names only,
like the snapshot), so these tests keep their meaning once the snapshot is refreshed.

    # print the dry run against that stand-in (from server/):
    python -c "import sys; sys.path[:0] = ['.', 'tests']; import test_retire_channels as t; t.dry_run()"

Like retiring a game, this step takes something away, so the tests pin that it hides and never
deletes, that Onboarding lets go of a channel before it is hidden, and that a game keeps its
answer and its role.
"""

import asyncio
import json
from pathlib import Path

import discord

import focus_mode
import layout
import public_mode
from names import slug
from test_games import LiveGuild
from test_server_scripts import NS

SNAPSHOT = Path(__file__).with_name("before_cut")
ARCHIVE = layout.GAME_ARCHIVE["name"]
GAMES = "What do you play?"
MERGED = [g for g in layout.GAMES if not layout.own_channel(g)]  # listed games that talk in #gaming
MERGED_CHANNELS = [f"{emoji}・{channel}" for emoji, channel, _ in MERGED]
HIDDEN = [*layout.RETIRED_CHANNELS, *MERGED_CHANNELS]


def read(name):
    return json.loads((SNAPSHOT / f"{name}.json").read_text(encoding="utf-8"))


def live_server():
    """The server as it was before the cut: its roles, categories, channels and Onboarding."""
    g = LiveGuild()
    for spec in read("roles"):
        if spec["name"] not in ("@everyone", "Front Desk"):
            g.add_role(spec["name"], position=60 if spec["name"] in ("Keeper", "Moderator") else 5, hoist=spec["hoist"])
    for cat_spec in read("channels")["categories"]:
        hidden = "read_messages" in cat_spec["overwrites"].get("role:@everyone", {}).get("deny", [])
        cat = g.add_category(cat_spec["name"], public_mode.PublicMode(g, False).private_overwrites(
            layout.GAME_ARCHIVE["private_to"]) if hidden and cat_spec["name"] == ARCHIVE else None)
        for spec in cat_spec["channels"]:
            if spec["type"] == "forum":
                g.add_forum(spec["name"], [(t["name"], t["emoji"]) for t in spec["tags"]], cat)
            elif spec["type"] == "voice":
                g.add_voice(spec["name"], cat)
            elif spec["type"] == "stage_voice":
                g.stage_channels.append(NS(id=5000, name=spec["name"], category_id=cat.id,
                                           permissions_for=lambda me: discord.Permissions.all()))
            else:
                g.add_text(spec["name"], cat)
    channels = {c.name: c for c in g.channels}
    roles = {r.name: r for r in g.roles}
    ob = read("onboarding")
    g.ob = NS(enabled=ob["enabled"], default_channels=[channels[n] for n in ob["default_channels"]], prompts=[
        discord.OnboardingPrompt(
            type=discord.OnboardingPromptType.multiple_choice, title=p["title"], required=p["required"],
            single_select=p["single_select"], in_onboarding=p["in_onboarding"],
            options=[discord.OnboardingPromptOption(
                title=o["title"], emoji=o["emoji"] or None, description=o["description"] or None,
                roles=[roles[n] for n in o["roles"]], channels=[channels[n] for n in o["channels"]])
                for o in p["options"]])
        for p in ob["prompts"]])
    return g


async def layout_steps(pm):
    """The steps of public_mode.run() that act on channels, Onboarding and games, in its order. The
    ones left out read things a snapshot does not hold (pins, invites, the welcome screen, AutoMod)."""
    await pm.roles()
    await pm.categories()
    await pm.renamed_channels()
    await pm.new_channels()
    await pm.moved_channels()
    await pm.onboarding()
    await pm.game_tags()
    await pm.restore_games()
    await pm.game_options()
    await pm.game_answer_channels()
    await pm.retire_games()
    await pm.retire_channels()


def run(guild, apply):
    asyncio.run(layout_steps(public_mode.PublicMode(guild, apply)))


def channel(guild, name):
    return next(c for c in guild.channels if slug(c.name) == slug(name) and c not in guild.categories)


def category_of(guild, name):
    return guild.get_channel(channel(guild, name).category_id).name


def option(guild, prompt_title, title):
    prompt = next(p for p in guild.ob.prompts if p.title == prompt_title)
    return next((o for o in prompt.options if o.title == title), None)


# ------------------------------------------------------------ the lists


def test_the_cut_is_the_one_the_owner_approved():
    voice = [s["name"] for c in layout.CATEGORIES if not c.get("private_to") for s in c["channels"]
             if s.get("type") in ("voice", "stage")]
    assert voice == ["🔊 Lobby", "➕ New Squad", "🎙・stage", "💤 AFK"]
    assert layout.GAME_CHANNELS == {"Minecraft"}
    assert len(layout.GAMES) == 8 and len(MERGED) == 7  # every game is still listed; one has a channel
    smp = {s["name"] for c in layout.CATEGORIES if c["name"] == "04 · monkey smp" for s in c["channels"]}
    assert smp & set(layout.ONBOARDING_DEFAULT_CHANNELS) == {"⛏️・monkey-smp"}
    assert smp - {"⛏️・monkey-smp"} <= set(layout.GAME_EXTRA_CHANNELS["Minecraft"])
    public = [r for r in focus_mode.classify() if r["status"] != focus_mode.PRIVATE]
    assert len(public) == 26 and len(layout.ONBOARDING_DEFAULT_CHANNELS) == 11


def test_retired_channels_are_named_nowhere_else_and_exist_on_the_server():
    retired = {slug(n) for n in HIDDEN}
    assert len(retired) == len(HIDDEN)
    assert layout.GAME_CHANNELS <= {role for _, _, role in layout.GAMES}
    named = [s["name"] for c in layout.CATEGORIES for s in c["channels"]]
    named += [*layout.ONBOARDING_DEFAULT_CHANNELS, *layout.ONBOARDING_EVERYTHING, *layout.FOCUS_RESERVE,
              *layout.FOCUS_UNLISTED, *(n for g in layout.FOCUS_GROUPS for n in g["channels"]),
              *(n for p in layout.ONBOARDING_PROMPTS for o in p["options"] for n in o.get("channels", ())),
              *(n for n, _, _ in layout.WELCOME_SCREEN["channels"]), layout.PUBLIC["invite_channel"],
              layout.AUTOMOD_ALERTS, *layout.COMMUNITY.values()]
    assert not retired & {slug(n) for n in named}
    posts = " ".join(p["description"] for p in layout.POSTS.values())
    assert not [n for n in HIDDEN if "{#" + n.split("・")[-1] + "}" in posts]  # no post links a hidden channel
    on_server = {slug(c.name) for c in live_server().channels}
    assert retired <= on_server  # a typo here would hide nothing and say "no channel"
    for emoji, _, role in MERGED:  # the role, the answer and the squad tag stay
        assert role in {r["name"] for r in layout.ROLES}
        spec = next(o for o in layout.ONBOARDING_PROMPTS[0]["options"] if o["title"] == role)
        assert spec["channels"] == [] and spec["role"] == role and "#gaming" in spec["description"]
        assert len(spec["description"]) <= focus_mode.MAX_OPTION_DESCRIPTION
        assert (role, emoji) in next(s["tags"] for c in layout.CATEGORIES for s in c["channels"]
                                     if s["name"] == "🎮・lfg")


# ------------------------------------------------------------ the step


def dry_run():
    guild = live_server()
    run(guild, apply=False)
    return guild


def test_dry_run_prints_every_line_and_changes_nothing(capsys):
    guild = dry_run()
    out = capsys.readouterr().out
    assert guild.calls == [] and guild.onboarding_edits == []
    assert not any(c.edits for c in guild.channels if hasattr(c, "edits"))
    assert "default channels: 🔊 Lobby 2, 🔒 New Private Squad" in out
    assert (f'"{GAMES}" answer Overwatch no longer opens 🦍・overwatch (the answer stays, and now reads: '
            "Get @Overwatch pings. The talk is in #gaming.)") in out
    assert '"What else do you want to see?" answer Events and tournaments (it only opened 🏆・tournaments)' in out
    for name in HIDDEN:
        assert f"  hide    #{name}: move from " in out, name
    assert out.count("  hide    ") == len(HIDDEN)
    assert "create" not in out and "#⛏️・minecraft" not in out and "#💬・general" not in out


def test_apply_takes_them_off_onboarding_then_hides_them_and_deletes_nothing():
    guild = live_server()
    kept = [c for c in guild.channels if slug(c.name) not in {slug(n) for n in HIDDEN} and hasattr(c, "edits")]
    roles = list(guild.roles)
    run(guild, apply=True)  # any delete, or any edit of a role, raises

    assert guild.calls == []  # nothing created: the archive was already there
    (edit,) = guild.onboarding_edits  # one edit, before anything moved
    gone = {channel(guild, n).id for n in HIDDEN}
    assert not gone & {c.id for c in edit["default_channels"]}
    assert not gone & {i for p in guild.ob.prompts for o in p.options for i in o.channel_ids}
    for name in HIDDEN:
        ch = channel(guild, name)
        assert category_of(guild, name) == ARCHIVE, name
        (move,) = ch.edits
        assert move["sync_permissions"] is True and ch.name == name
    assert not any(c.edits for c in kept) and guild.roles == roles

    # Games: the answer, its role and its place stay; only the channel is off it.
    for _, _, role in MERGED:
        answer = option(guild, GAMES, role)
        assert answer.channel_ids == set() and len(answer.role_ids) == 1
        assert answer.description == f"Get @{role} pings. The talk is in #gaming."
    minecraft = option(guild, GAMES, "Minecraft")
    assert channel(guild, "⛏️・minecraft").id in minecraft.channel_ids and len(minecraft.channel_ids) == 7
    assert category_of(guild, "⛏️・minecraft") == "03 · games"
    assert [o.title for o in guild.ob.prompts[0].options] == [*(r for _, _, r in layout.GAMES), "Just here to hang"]
    # The other questions: an answer that only opened retired channels leaves, the rest are trimmed.
    assert [p.title for p in guild.ob.prompts] == [p["title"] for p in read("onboarding")["prompts"]]
    assert option(guild, layout.FOCUS_PROMPT, "Events and tournaments") is None
    assert option(guild, layout.FOCUS_PROMPT, "More voice rooms").channel_ids == {channel(guild, "💤 AFK").id}
    assert option(guild, "Want pings?", "Game nights") is not None


def test_a_second_run_changes_nothing(capsys):
    guild = live_server()
    run(guild, apply=True)
    moved = {c.name: len(c.edits) for c in guild.channels if hasattr(c, "edits")}
    capsys.readouterr()
    run(guild, apply=True)
    out = capsys.readouterr().out
    assert len(guild.onboarding_edits) == 1 and guild.calls == []
    assert moved == {c.name: len(c.edits) for c in guild.channels if hasattr(c, "edits")}
    assert "hide" not in out and f"  ok      🦍・overwatch (already in {ARCHIVE})" in out


def test_focus_mode_then_finishes_the_onboarding_side():
    guild = live_server()
    run(guild, apply=True)
    guild.me.guild_permissions = discord.Permissions.all()
    assert asyncio.run(focus_mode.FocusMode(guild, apply=True).run()) is True
    assert [c.name for c in guild.ob.default_channels] == layout.ONBOARDING_DEFAULT_CHANNELS
    question = next(p for p in guild.ob.prompts if p.title == layout.FOCUS_PROMPT)
    assert [o.title for o in question.options] == [g["title"] for g in layout.FOCUS_GROUPS]


def test_nothing_is_hidden_while_onboarding_cannot_be_read(capsys):
    guild = live_server()

    async def broken():
        raise discord.HTTPException(NS(status=500, reason="Server Error"), "try later")
    guild.onboarding = broken
    asyncio.run(public_mode.PublicMode(guild, apply=True).retire_channels())
    assert "nothing hidden" in capsys.readouterr().out
    assert not any(c.edits for c in guild.channels if hasattr(c, "edits")) and guild.onboarding_edits == []


def test_a_channel_the_layout_still_lists_is_never_hidden(monkeypatch, capsys):
    monkeypatch.setattr(layout, "RETIRED_CHANNELS", [*layout.RETIRED_CHANNELS, "💬・general"])
    guild = live_server()
    asyncio.run(public_mode.PublicMode(guild, apply=True).retire_channels())
    assert "! skip    💬・general: the layout still lists it" in capsys.readouterr().out
    assert category_of(guild, "💬・general") == "02 · the lobby" and not channel(guild, "💬・general").edits


def test_the_step_is_safe_on_a_server_with_nothing():
    guild = LiveGuild()
    asyncio.run(public_mode.PublicMode(guild, apply=True).retire_channels())
    assert guild.calls == [] and guild.onboarding_edits == []

