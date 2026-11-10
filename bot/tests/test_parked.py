"""Parked modules: in MODULES, not loaded, and nothing else points members at them."""
from datetime import timedelta
from zoneinfo import ZoneInfo

import main
from logic import helpdesk as H
from logic import seasonal as S


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
