"""Tests for focus_mode.py against layout.py and fake guilds (no Discord)."""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import focus_mode
import layout
from names import slug

# The Onboarding default list on the day the core was cut down, in layout.py's order.
# `focus_mode.py --revert` must put exactly this back.
TODAY = [
    "📌・rules", "📣・announcements", "👋・welcome", "💬・general",
    "🆘・help", "💡・suggestions", "🤣・memes", "📸・clips", "⭐・hall-of-fame", "🔢・counting", "🎲・games",
    "🕹️・gaming", "🎮・lfg", "🎨・art", "💻・code",
    "👗・fashion", "♟️・chess", "🤖・bot-commands", "📺・creators", "🏆・tournaments", "🎭・roles", "🤝・partners",
    "🔊 Lobby", "➕ New Squad", "🎮 Squad", "💤 AFK",
    "📚・guides", "🖥・setups",
    "⛏️・realms",
]
PRIVATE = ["💜・supporters", "🛡️・mod", "🔒・admin", "📋・mod-log", "🔒・appeals", "🔒・squad-chat", "🔒 Squad Only"]


class NS(SimpleNamespace):
    __hash__ = object.__hash__


def grouped() -> list[str]:
    return [n for g in layout.FOCUS_GROUPS for n in g["channels"]]


# ---------------------------------------------------------------- the grouping
def test_revert_list_is_todays_list_exactly():
    assert layout.ONBOARDING_EVERYTHING == TODAY


def test_every_public_channel_is_in_the_core_or_exactly_one_group():
    rows = focus_mode.classify()  # raises if a public channel is left out or listed twice
    assert len(rows) == len(focus_mode.layout_rows())
    core = [slug(n) for n in layout.ONBOARDING_DEFAULT_CHANNELS]
    groups = [slug(n) for n in grouped()]
    unlisted = [slug(n) for n in layout.FOCUS_UNLISTED]
    games = focus_mode.game_channels()
    listed = [*core, *groups, *unlisted]
    assert len(listed) == len(set(listed))  # disjoint: nothing is listed twice
    public = [slug(r["name"]) for r in rows if r["status"] != focus_mode.PRIVATE]
    assert len(public) == len(set(public))
    assert set(public) == set(listed) | games  # total: nothing public is left out
    assert not set(listed) & games  # game channels belong to "What do you play?"
    # Everything that is a default today is still a default or one click away.
    assert {slug(n) for n in TODAY} == set(core) | set(groups)


def test_private_channels_are_untouched():
    rows = {r["name"]: r for r in focus_mode.classify()}
    for name in PRIVATE:
        assert rows[name]["status"] == focus_mode.PRIVATE, name
    private = {slug(r["name"]) for r in rows.values() if r["status"] == focus_mode.PRIVATE}
    assert private == {slug(n) for n in PRIVATE}
    mentioned = {slug(n) for n in (*layout.ONBOARDING_DEFAULT_CHANNELS, *layout.ONBOARDING_EVERYTHING, *grouped(),
                                   *layout.FOCUS_EVENT_CHANNELS, *layout.FOCUS_UNLISTED, *layout.FOCUS_RESERVE)}
    assert not mentioned & private
    assert not focus_mode.FocusMode(NS(channels=[]), apply=True).managed() & private


def test_statuses_and_event_channels():
    rows = {r["name"]: r for r in focus_mode.classify()}
    for name in layout.ONBOARDING_DEFAULT_CHANNELS:
        assert rows[name]["status"] == focus_mode.CORE
    assert {n for n, r in rows.items() if r["status"] == focus_mode.EVENT} == set(layout.FOCUS_EVENT_CHANNELS)
    assert rows["🏆・tournaments"]["group"] == "Events and tournaments"
    assert rows["⛏️・realms"]["group"] == "Minecraft"
    assert rows["🎙・stage"]["status"] == focus_mode.UNLISTED
    assert rows["⛏️・minecraft"]["status"] == focus_mode.GAME


def test_a_forgotten_or_doubled_channel_is_an_error(monkeypatch):
    monkeypatch.setattr(layout, "FOCUS_GROUPS", [g for g in layout.FOCUS_GROUPS if g["title"] != "Off topic"])
    with pytest.raises(ValueError, match="memes is public but is not in"):
        focus_mode.classify()
    monkeypatch.undo()
    monkeypatch.setattr(layout, "FOCUS_GROUPS", [*layout.FOCUS_GROUPS, {
        "title": "Again", "emoji": "🔁", "description": "", "channels": ["🤣・memes"]}])
    with pytest.raises(ValueError, match="memes is listed more than once"):
        focus_mode.classify()
    monkeypatch.undo()
    monkeypatch.setattr(layout, "ONBOARDING_DEFAULT_CHANNELS", [*layout.ONBOARDING_DEFAULT_CHANNELS, "📋・mod-log"])
    with pytest.raises(ValueError, match="mod-log is private"):
        focus_mode.classify()
    monkeypatch.undo()
    monkeypatch.setattr(layout, "FOCUS_RESERVE", ["🫥・nowhere"])
    with pytest.raises(ValueError, match="not a channel in layout.py"):
        focus_mode.classify()


# ---------------------------------------------------------------- Discord's limits
def test_the_core_and_the_question_fit_discords_limits():
    assert focus_mode.limit_problems() == []
    rows = {slug(r["name"]): r for r in focus_mode.layout_rows()}
    core = [rows[slug(n)] for n in layout.ONBOARDING_DEFAULT_CHANNELS]
    assert focus_mode.MIN_DEFAULTS <= len(core) <= 9  # Discord's floor; and still a short list
    assert len(focus_mode.postable(core)) >= focus_mode.MIN_POSTABLE
    assert all(r["type"] in ("text", "forum", "voice") for r in core)
    # If Discord counts only plain text channels, the reserve makes up the difference.
    reserve = [rows[slug(n)] for n in layout.FOCUS_RESERVE]
    assert len(focus_mode.surely_postable([*core, *reserve])) >= focus_mode.MIN_POSTABLE
    assert len(layout.FOCUS_PROMPT) <= focus_mode.MAX_PROMPT_TITLE
    assert 0 < len(layout.FOCUS_GROUPS) <= focus_mode.MAX_OPTIONS
    for g in layout.FOCUS_GROUPS:
        assert 0 < len(g["title"]) <= focus_mode.MAX_OPTION_TITLE
        assert len(g["description"]) <= focus_mode.MAX_OPTION_DESCRIPTION
        assert g["emoji"] and 0 < len(g["channels"]) <= focus_mode.MAX_OPTION_CHANNELS
        assert "—" not in g["title"] + g["description"]


def test_limit_problems_are_reported(monkeypatch):
    monkeypatch.setattr(layout, "ONBOARDING_DEFAULT_CHANNELS", ["📌・rules", "📣・announcements", "💬・general"])
    problems = " ".join(focus_mode.limit_problems())
    assert "needs at least 7" in problems and "needs 5" in problems


def test_plan_has_one_line_per_channel_in_the_words_the_owner_asked_for():
    lines = focus_mode.plan_lines()
    assert len(lines) == len(focus_mode.layout_rows())
    by = {line.split("  ")[0].strip(): line for line in lines}
    assert by["💬・general"].endswith("core")
    assert by["🤣・memes"].endswith('opt-in group "Off topic"')
    assert "event-driven" in by["🏆・tournaments"] and "a tournament is open or running" in by["🏆・tournaments"]
    assert by["📋・mod-log"].endswith("private (unchanged)")
    assert "What do you play?" in by["⛏️・minecraft"]


# ---------------------------------------------------------------- applying it (fake guild)
def make_guild(defaults=TODAY, prompts=(), enabled=True, extra_default=None):
    names = [r["name"] for r in focus_mode.layout_rows()]
    channels = [NS(id=100 + i, name=n) for i, n in enumerate(names)]
    by = {c.name: c for c in channels}
    current = [by[n] for n in defaults]
    if extra_default is not None:
        channels.append(extra_default)
        current.append(extra_default)
    guild = NS(name="test", channels=channels, edits=[], refuse=0,
               me=NS(guild_permissions=discord.Permissions.all()))
    ob = NS(enabled=enabled, default_channels=current, prompts=list(prompts))

    async def onboarding():
        return ob

    async def edit_onboarding(**kw):
        if guild.refuse:
            guild.refuse -= 1
            raise discord.HTTPException(NS(status=400, reason="Bad Request"), "not enough chattable channels")
        guild.edits.append(kw)
        if "default_channels" in kw:
            ob.default_channels = list(kw["default_channels"])
        if "prompts" in kw:
            ob.prompts = list(kw["prompts"])
    guild.onboarding, guild.edit_onboarding, guild.ob, guild.by = onboarding, edit_onboarding, ob, by
    return guild


def other_prompt(title="What do you play?"):
    return discord.OnboardingPrompt(type=discord.OnboardingPromptType.multiple_choice, title=title,
                                    options=[discord.OnboardingPromptOption(title="x", roles=[discord.Object(9)])])


def run(guild, apply, revert=False):
    return asyncio.run(focus_mode.FocusMode(guild, apply, revert).run())


def test_dry_run_prints_the_whole_plan_and_changes_nothing(capsys):
    guild = make_guild(prompts=[other_prompt()])
    assert run(guild, apply=False) is True
    assert guild.edits == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    for line in focus_mode.plan_lines():
        assert line in out
    assert "default channels: 29 -> 9" in out
    assert 'add     question "What else do you want to see?"' in out
    assert "🤣 Off topic: 🤣・memes, 🎨・art, 💻・code, 👗・fashion, ♟️・chess" in out


def test_apply_is_one_edit_core_defaults_plus_the_question_last():
    first = other_prompt()
    guild = make_guild(prompts=[first, other_prompt("Where are you?")])
    assert run(guild, apply=True) is True
    (edit,) = guild.edits  # one atomic edit: the question and the shorter list together
    assert [c.name for c in edit["default_channels"]] == layout.ONBOARDING_DEFAULT_CHANNELS
    assert edit["prompts"][0] is first and len(edit["prompts"]) == 3  # the others untouched, in order
    q = edit["prompts"][-1]
    assert q.title == layout.FOCUS_PROMPT
    assert q.in_onboarding is False and q.required is False and q.single_select is False
    assert [o.title for o in q.options] == [g["title"] for g in layout.FOCUS_GROUPS]
    for option, group in zip(q.options, layout.FOCUS_GROUPS):
        assert option.channel_ids == {guild.by[n].id for n in group["channels"]}
        assert not option.role_ids  # no roles are handed out or changed
    assert set(edit) == {"prompts", "default_channels", "reason"}  # never enabled or mode


def test_rerun_is_a_noop(capsys):
    guild = make_guild()
    run(guild, apply=True)
    capsys.readouterr()
    assert run(guild, apply=True) is True
    assert len(guild.edits) == 1
    out = capsys.readouterr().out
    assert "ok      default channels (9)" in out and 'ok      question "What else do you want to see?"' in out


def test_revert_restores_todays_defaults_exactly_and_keeps_the_question():
    guild = make_guild()
    run(guild, apply=True)
    assert run(guild, apply=False, revert=True) is True and len(guild.edits) == 1  # dry run first
    assert run(guild, apply=True, revert=True) is True
    revert = guild.edits[-1]
    assert set(revert) == {"default_channels", "reason"}  # the questions are not touched
    assert sorted(c.name for c in revert["default_channels"]) == sorted(TODAY)
    assert len(revert["default_channels"]) == len(TODAY)
    assert any(p.title == layout.FOCUS_PROMPT for p in guild.ob.prompts)
    assert run(guild, apply=True, revert=True) is True and len(guild.edits) == 2  # and it is a no-op after


def test_revert_never_drops_a_default_and_focus_keeps_defaults_layout_does_not_know():
    stray = NS(id=900, name="📎・made-by-hand")
    guild = make_guild(extra_default=stray)
    run(guild, apply=True)
    assert [c.name for c in guild.edits[0]["default_channels"]] == [stray.name, *layout.ONBOARDING_DEFAULT_CHANNELS]
    run(guild, apply=True, revert=True)
    assert {c.name for c in guild.edits[1]["default_channels"]} == {stray.name, *TODAY}


def test_discord_refusing_the_short_list_adds_the_reserve_one_at_a_time(capsys):
    guild = make_guild()
    guild.refuse = 2
    assert run(guild, apply=True) is True
    (edit,) = guild.edits  # the refused tries changed nothing
    assert [c.name for c in edit["default_channels"]] == [*layout.ONBOARDING_DEFAULT_CHANNELS, *layout.FOCUS_RESERVE]
    assert "Discord needed 🤣・memes, 🤖・bot-commands as well" in capsys.readouterr().out
    guild = make_guild()
    guild.refuse = 3  # more refusals than reserve channels: give up, nothing changed
    with pytest.raises(discord.HTTPException):
        run(guild, apply=True)
    assert guild.edits == [] and len(guild.ob.default_channels) == len(TODAY)


def test_stops_without_changing_anything_when_it_is_not_safe(capsys):
    guild = make_guild(enabled=False)
    assert run(guild, apply=True) is False and guild.edits == []
    guild = make_guild()
    guild.channels.remove(guild.by["📸・clips"])  # a core channel is missing
    assert run(guild, apply=True) is False and guild.edits == []
    assert "don't exist yet: 📸・clips" in capsys.readouterr().out
    full = [other_prompt(f"q{i}") for i in range(focus_mode.MAX_PROMPTS)]
    guild = make_guild(prompts=full)  # no room for the question: the defaults stay
    assert run(guild, apply=True) is False and guild.edits == []
    guild = make_guild()
    guild.me.guild_permissions = discord.Permissions.none()
    assert run(guild, apply=True) is False and guild.edits == []


def test_a_changed_group_updates_the_question_in_place():
    guild = make_guild()
    run(guild, apply=True)
    old = guild.ob.prompts[-1]
    old.options[0].channel_ids = set()  # as if a group lost its channel on the server
    run(guild, apply=True)
    assert len(guild.edits) == 2 and set(guild.edits[1]) == {"prompts", "reason"}
    assert len(guild.ob.prompts) == 1 and guild.ob.prompts[0].options[0].channel_ids
