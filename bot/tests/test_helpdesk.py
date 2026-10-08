"""Tests for logic.helpdesk: the /help tree walk, categories, staff hiding, presence and /about text."""

import importlib
from datetime import date

import discord
from discord import app_commands
from discord.ext import commands

import main
from logic import helpdesk as H
from logic import wordgame as W


async def _noop(interaction: discord.Interaction) -> None:
    pass


def cmd(name, description, module="cogs.utility", perms=None, callback=_noop):
    c = app_commands.Command(name=name, description=description, callback=callback)
    c.module = module
    if perms is not None:
        c.default_permissions = perms
    return c


def group(name, description, module, *subs):
    g = app_commands.Group(name=name, description=description)
    g.module = module
    for s in subs:
        g.add_command(s)
    return g


@app_commands.describe(amount="How many coins", member="Who gets them")
async def _give(interaction: discord.Interaction, member: discord.Member, amount: int, note: str | None = None) -> None:
    pass


def sample():
    return [
        cmd("daily", "Claim your daily coins", "cogs.economy"),
        cmd("give", "Give coins to a member", "cogs.economy", callback=_give),
        cmd("warn", "Warn a member (mods only)", "cogs.moderation"),
        cmd("status", "Front Desk health", "cogs.ops", perms=discord.Permissions(moderate_members=True)),
        group("word", "Daily Word", "cogs.wordgame",
              cmd("guess", "Guess today's word", "cogs.wordgame"),
              cmd("stats", "Your Daily Word stats", "cogs.wordgame")),
        group("partner", "Partner servers: apply, or (staff) remove one", "cogs.partners",
              cmd("apply", "Apply to partner", "cogs.partners"),
              cmd("remove", "Staff: take a partner off", "cogs.partners")),
        group("xp", "Staff: adjust XP", "cogs.levels", cmd("set", "Set XP", "cogs.levels")),
        group("suggestion", "Suggestion forum tools (mods)", "cogs.utility",
              cmd("status", "Mark this suggestion done", "cogs.utility")),
        cmd("newthing", "From a cog nobody mapped yet", "cogs.brand_new"),
    ]


def by_name(entries):
    return {e.name: e for e in entries}


# ------------------------------------------------------------ collect


def test_collect_walks_groups_and_keeps_descriptions():
    es = by_name(H.collect(sample()))
    assert es["word"].group and es["word"].subcommands == ("word guess", "word stats")
    assert es["word guess"].description == "Guess today's word"
    assert es["word guess"].category == "games"
    assert es["daily"].category == "coins"
    assert not es["daily"].group


def test_unknown_module_falls_back_to_utility():
    assert by_name(H.collect(sample()))["newthing"].category == "utility"
    assert H.module_category(None) == "utility"
    assert H.module_category("cogs.lfg") == "squad"


def test_staff_flags_from_descriptions_permissions_and_groups():
    es = by_name(H.collect(sample()))
    assert es["warn"].staff  # "(mods only)"
    assert es["status"].staff  # default_permissions
    assert es["partner remove"].staff and not es["partner apply"].staff
    assert not es["partner"].staff  # "(staff)" mid-sentence doesn't hide the group
    assert es["xp"].staff and es["xp set"].staff  # "Staff:" on the group covers its subcommands
    assert es["suggestion status"].staff  # "(mods)" on the group
    assert not es["daily"].staff and not es["word"].staff


def test_visible_hides_staff_for_members():
    es = H.collect(sample())
    member = {e.name for e in H.visible(es, staff=False)}
    assert "warn" not in member and "xp" not in member and "xp set" not in member
    assert "partner" in member and "partner apply" in member and "partner remove" not in member
    assert {e.name for e in H.visible(es, staff=True)} == {e.name for e in es}


def test_params_have_kind_and_required():
    give = by_name(H.collect(sample()))["give"]
    params = {p.name: p for p in give.params}
    assert params["member"].required and params["member"].kind == "member"
    assert params["amount"].kind == "whole number" and params["amount"].description == "How many coins"
    assert not params["note"].required
    assert H.usage(give) == "/give member:<member> amount:<whole number> [note]"


def test_context_menus_are_skipped():
    async def cb(interaction: discord.Interaction, message: discord.Message):
        pass
    menu = app_commands.ContextMenu(name="Report message", callback=cb)
    assert H.collect([menu, cmd("daily", "Claim", "cogs.economy")])[0].name == "daily"
    assert len(H.collect([menu])) == 0


# ------------------------------------------------------------ pages


def test_by_category_follows_category_order_and_skips_empty():
    grouped = H.by_category(H.visible(H.collect(sample()), staff=False))
    assert list(grouped) == [k for k in (c.key for c in H.CATEGORIES) if k in grouped]
    assert "squad" not in grouped
    assert [e.name for e in grouped["games"]] == ["word guess", "word stats"]


def test_mentions_are_clickable_when_ids_are_known():
    assert H.mention("word guess", {"word": 42}) == "</word guess:42>"
    assert H.mention("daily", {}) == "`/daily`"


def test_category_text_lists_commands():
    es = H.visible(H.collect(sample()), staff=False)
    text = H.category_text("coins", es, {"daily": 7})
    assert "</daily:7> Claim your daily coins" in text
    assert "`/give` Give coins" in text
    assert H.category_text("squad", es).endswith("Nothing here yet.")


def test_staff_lines_are_tagged():
    es = H.collect(sample())
    assert "· staff" in H.category_text("utility", es)


def test_overview_rows_count_commands():
    rows = dict(H.overview_rows(H.visible(H.collect(sample()), staff=False)))
    games = next(v for k, v in rows.items() if "Games" in k)
    assert "`/word guess`" in games
    assert any("Games · 2 commands" in k for k in rows)


def test_clip_cuts_on_a_line():
    text = "\n".join(f"line {i:04}" for i in range(1000))
    out = H.clip(text, 100)
    assert len(out) <= 100 and out.endswith("\n…")


def test_find_and_suggest():
    es = H.collect(sample())
    assert H.find(es, "/Word  Guess").name == "word guess"
    assert H.find(es, "dai").name == "daily"  # unique prefix
    assert H.find(es, "word").name == "word"
    assert H.find(es, "nope") is None
    assert H.find(es, "") is None
    names = [e.name for e in H.suggest(es, "st")]
    assert names[0] == "status"
    assert "word stats" in names  # contains
    assert len(H.suggest(es, "")) == min(len(es), H.MAX_CHOICES)


def test_detail_lines():
    es = by_name(H.collect(sample()))
    word = "\n".join(H.detail_lines(es["word"], {"word": 9}))
    assert "Subcommands:" in word and "</word guess:9>" in word
    give = "\n".join(H.detail_lines(es["give"]))
    assert "`amount` (whole number, required): How many coins" in give
    assert "No options" in "\n".join(H.detail_lines(es["daily"]))
    assert "Staff only." in H.detail_lines(es["warn"])
    assert H.usage(es["word"]) == "/word <guess|stats>"


# ------------------------------------------------------------ the real tree


def real_commands():
    out = []
    for ext, _ in [*main.MODULES, ("cogs.helpdesk", set())]:
        mod = importlib.import_module(ext)
        for v in vars(mod).values():
            if isinstance(v, type) and issubclass(v, commands.Cog) and v is not commands.Cog \
                    and v.__module__ == mod.__name__:
                out += list(v.__cog_app_commands__)
    return out


def test_every_real_module_with_commands_has_a_category():
    # Unmapped modules still show (under Utility); this just keeps the categories meaningful.
    unmapped = {c.module.split(".")[-1] for c in real_commands()} - set(H.MODULE_CATEGORY)
    assert not unmapped, f"add these to MODULE_CATEGORY: {unmapped}"


def test_real_tree_staff_commands_are_hidden_from_members():
    es = H.collect(real_commands())
    member = {e.name for e in H.visible(es, staff=False)}
    for staff in ("warn", "timeout", "purge", "cases", "status", "economy", "xp", "xp set",
                  "tournament create", "creator approve", "partner remove", "suggestion status"):
        assert staff not in member, staff
    for public in ("help", "about", "daily", "quest", "queue join", "word guess", "partner apply",
                   "tournament bracket", "roles", "lfg"):
        assert public in member, public


def test_real_tree_fits_discord_limits():
    es = H.collect(real_commands())
    for key in H.by_category(es):
        assert len(H.category_text(key, es, {e.root: 10**18 for e in es})) <= 4096
    rows = H.overview_rows(es)
    assert len(rows) <= 25
    assert all(len(v) <= 1024 for _, v in rows)


# ------------------------------------------------------------ presence


def test_presence_rotation():
    lines = H.presence_lines(1234, 8)
    assert lines == [("watching", "1,234 members"), ("playing", "/queue to find a squad"),
                     ("listening", "/help"), ("playing", "Daily Word #8")]
    assert H.presence_at(0, 1, 1) == ("watching", "1 member")
    assert H.presence_at(5, 10, 3) == ("playing", "/queue to find a squad")


def test_daily_word_number_comes_from_wordgame():
    assert W.puzzle_number(W.EPOCH) == 1


# ------------------------------------------------------------ /about


def test_age_text():
    assert H.age_text(date(2026, 1, 15), date(2026, 1, 15)) == "today"
    assert H.age_text(date(2026, 1, 15), date(2026, 1, 20)) == "5 days"
    assert H.age_text(date(2026, 1, 15), date(2026, 2, 15)) == "1 month"
    assert H.age_text(date(2026, 1, 15), date(2026, 3, 20)) == "2 months, 5 days"
    assert H.age_text(date(2024, 10, 1), date(2026, 10, 8)) == "2 years, 7 days"
    assert H.age_text(date(2025, 1, 31), date(2025, 3, 1)) == "1 month, 1 day"  # Feb has 28 days
    assert H.age_text(date(2025, 5, 1), date(2026, 7, 2)) == "1 year, 2 months"  # two largest units


def test_boost_text():
    assert H.boost_text(14, 2) == "14 (level 2)"
    assert H.boost_text(0, 0) == "0 (no level yet)"


def test_links():
    assert H.SITE_URL.startswith("https://") and H.INVITE_URL.startswith("https://discord.gg/")
