"""Parked modules: in MODULES, not loaded, and nothing else points members at them."""
import importlib
import re
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from discord.ext import commands

import config
import main
from logic import helpdesk as H
from logic import seasonal as S

ROOT = Path(__file__).resolve().parents[2]


def test_parked_modules_are_real_and_core_ones_are_not_parked():
    names = {ext for ext, _ in main.MODULES}
    assert main.PARKED <= names
    core = {"cogs.ops", "cogs.heartbeat", "cogs.moderation", "cogs.community", "cogs.tempvoice",
            "cogs.selfroles", "cogs.stats", "cogs.sitedata", "cogs.monkey", "cogs.helpdesk", "cogs.economy"}
    assert not core & main.PARKED


def test_loaded_modules_keep_build_order_and_skip_the_parked():
    loaded = main.loaded_modules({})
    assert [m for m in main.MODULES if m[0] not in main.PARKED] == loaded
    assert len(loaded) == len(main.MODULES) - len(main.PARKED)


def test_parked_modules_env_replaces_the_list():
    assert main.parked({}) == main.PARKED
    assert main.parked({"PARKED_MODULES": "  "}) == main.PARKED
    assert main.parked({"PARKED_MODULES": "none"}) == frozenset()
    assert main.parked({"PARKED_MODULES": "games, cogs.wordgame, nosuch"}) == {"cogs.games", "cogs.wordgame"}
    assert len(main.loaded_modules({"PARKED_MODULES": "none"})) == len(main.MODULES)


def test_intents_come_from_loaded_modules_only(monkeypatch):
    monkeypatch.setenv("PARKED_MODULES", ",".join(ext for ext, needs in main.MODULES if "presences" in needs))
    assert main.build_intents().presences is False
    monkeypatch.setenv("PARKED_MODULES", "none")
    assert main.build_intents().presences is True


def test_presence_leaves_out_parked_lines():
    assert len(H.presence_lines(5, 1)) == 4
    texts = [text for _, text in H.presence_lines(5, 1, queue=False, word=False)]
    assert texts == ["5 members", "/help"]
    assert H.presence_at(3, 5, 1, queue=False, word=False) == ("listening", "/help")


def test_daily_quest_skips_quests_of_parked_modules():
    tz = ZoneInfo("America/Los_Angeles")
    run = S.run_for(next(e for e in S.load_events() if e.key == "halloween"), 2026, tz)
    first = S.local_day(run.start, tz)
    days = [first + timedelta(days=n) for n in range(10)]
    missing = frozenset({"Games", "WordGame"})
    assert {S.quest_for(run, d, tz, missing).key for d in days} == {"squad", "chat", "voice"}
    assert {S.quest_for(run, d, tz).key for d in days} == set(S.QUEST_BY_KEY)


# ------------------------------------------------------------ nothing public promises a parked command


def slash_commands(extensions) -> set[str]:
    """Top-level slash command names the given extensions register ("word" for /word guess)."""
    names = set()
    for ext in extensions:
        mod = importlib.import_module(ext)
        for cog in vars(mod).values():
            if isinstance(cog, type) and issubclass(cog, commands.Cog) and cog.__module__ == mod.__name__:
                names |= {cmd.name for cmd in cog.__cog_app_commands__}
    return names


def parked_commands_in(text: str) -> set[str]:
    """The slash commands of parked modules that `text` names, like "/word" in "/word guess"."""
    loaded = slash_commands(ext for ext, _ in main.loaded_modules({}))
    parked = slash_commands(main.PARKED) - loaded
    # A slash that starts a word: not a path ("@/lib/word", "img/word.png") and not a longer name ("/wordle").
    return {name for name in re.findall(r"(?<![\w@.:/])/([a-z][a-z-]*)(?![\w-])", text) if name in parked}


def public_text() -> dict[str, str]:
    """Everything a stranger or a member reads that this repo writes: the site's pages and
    components, the listing kit, and what the server scripts post and set in Discord."""
    layout = config.layout
    out = {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
           for p in sorted((ROOT / "site" / "src").rglob("*.tsx")) if ".test." not in p.name}
    out["docs/LISTING.md"] = (ROOT / "docs" / "LISTING.md").read_text(encoding="utf-8")
    out["server/layout.py (posts, topics, welcome screen, Onboarding)"] = "\n".join([
        *(post["description"] for post in layout.POSTS.values()),
        *(spec.get("topic", "") for cat in layout.CATEGORIES for spec in cat["channels"]),
        layout.WELCOME_SCREEN["description"], layout.PUBLIC["description"],
        *(g["description"] for g in layout.FOCUS_GROUPS),
        *(o.get("description", "") for p in layout.ONBOARDING_PROMPTS for o in p["options"]),
    ])
    return out


def test_the_scan_finds_a_parked_command_and_leaves_paths_and_loaded_ones_alone():
    assert {"word", "partner", "house"} <= slash_commands(main.PARKED)
    assert parked_commands_in("a daily word puzzle (<code>/word guess</code>)") == {"word"}
    assert parked_commands_in("Apply with /partner apply. `/house me` shows yours.") == {"partner", "house"}
    assert parked_commands_in('import x from "@/lib/word"; href="/img/house.png" /wordle https://a.test/word') == set()
    assert parked_commands_in("Post with /lfg, check /rank, open the /shop.") == set()  # loaded modules


def test_no_public_text_names_a_slash_command_of_a_parked_module():
    found = {where: sorted(names) for where, text in public_text().items() if (names := parked_commands_in(text))}
    assert not found, (
        f"these name slash commands that parked modules (bot/main.py PARKED) would register: {found}. "
        "Take the mention out of the text, or un-park the module by deleting its line from PARKED.")
