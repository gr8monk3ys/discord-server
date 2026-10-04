"""Unit tests for logic.community: welcome text, report rules and mod-log lines."""

from types import SimpleNamespace

import pytest

import config
from logic import community as C

GUILD = 999
VALORANT = config.game_by_key("valorant")
MINECRAFT = config.game_by_key("minecraft")
FORTNITE = config.game_by_key("fortnite")
ROBLOX = config.game_by_key("roblox")
DAY = 24 * 60 * 60


def roles(*names):
    return [SimpleNamespace(name=n) for n in names]


# ---------------------------------------------------------------- welcome: games
def test_picked_games_follow_config_order_and_cap_at_three():
    member_roles = roles("@everyone", "Roblox", "Valorant", "LFG", "Fortnite", "Minecraft")
    assert C.picked_games(member_roles, config.GAMES) == [MINECRAFT, FORTNITE, VALORANT]


def test_picked_games_match_ignoring_emoji_and_case():
    assert C.picked_games(roles("🎯 VALORANT"), config.GAMES) == [VALORANT]


def test_picked_games_none():
    assert C.picked_games(roles("@everyone", "LFG", "Moderator"), config.GAMES) == []


def test_pick_open_post_uses_first_game_with_an_open_post():
    games = [MINECRAFT, FORTNITE, VALORANT]
    assert C.pick_open_post(games, {"valorant": 70, "fortnite": 71}) == (FORTNITE, 71)
    assert C.pick_open_post(games, {"roblox": 5}) is None
    assert C.pick_open_post([], {"valorant": 70}) is None


def test_thread_link():
    assert C.thread_link(GUILD, 123) == "https://discord.com/channels/999/123"


# ---------------------------------------------------------------- welcome: text
def test_opener_is_deterministic_and_varies_by_user():
    assert C.opener(1, "<@1>") == C.opener(1, "<@1>")
    lines = {C.opener(uid, "<@x>") for uid in range(len(C.OPENERS))}
    assert len(lines) == len(C.OPENERS) > 1
    for uid in range(20):
        assert "<@x>" in C.opener(uid, "<@x>")


def test_welcome_with_games_and_open_post_links_the_squad():
    text = C.welcome_text(7, "<@7>", [MINECRAFT, VALORANT], {"valorant": 555}, GUILD, "<#42>")
    assert text.startswith(C.opener(7, "<@7>"))
    assert "Minecraft and Valorant" in text
    assert "Valorant squad looking for people right now: https://discord.com/channels/999/555" in text
    assert "<#42>" not in text


def test_welcome_with_games_but_no_post_points_at_forum():
    text = C.welcome_text(7, "<@7>", [VALORANT], {}, GUILD, "<#42>")
    assert "Valorant" in text
    assert "<#42>" in text and "/lfg" in text
    assert "discord.com/channels" not in text


def test_welcome_without_games_points_at_forum_only():
    text = C.welcome_text(8, "<@8>", [], {"valorant": 555}, GUILD, "<#42>")
    assert "<#42>" in text and "/lfg" in text
    assert "discord.com/channels" not in text  # the post isn't for any game they picked
    assert "into" not in text


def test_welcome_lists_three_games_with_commas():
    text = C.welcome_text(1, "<@1>", [MINECRAFT, FORTNITE, VALORANT], {}, GUILD, "#lfg")
    assert "Minecraft, Fortnite and Valorant" in text


def test_welcome_is_short_and_has_one_mention():
    text = C.welcome_text(3, "<@3>", [MINECRAFT, FORTNITE, VALORANT], {"fortnite": 9}, GUILD, "<#42>")
    assert text.count("<@3>") == 1
    assert len(text) < 400
    assert "@everyone" not in text and "@here" not in text


# ---------------------------------------------------------------- welcome: triggers
@pytest.mark.parametrize("before,after,expected", [
    (False, True, True),
    (True, True, False),
    (False, False, False),
    (True, False, False),
])
def test_onboarding_flip(before, after, expected):
    assert C.onboarding_completed(before, after) is expected


@pytest.mark.parametrize("completed,enabled,expected", [
    (True, True, True),     # already done at join: welcome now
    (False, True, False),   # wait for the flip in on_member_update
    (False, False, True),   # no onboarding on this server: welcome on join
    (True, False, True),
])
def test_welcome_on_join(completed, enabled, expected):
    assert C.welcome_on_join(completed, enabled) is expected


# ---------------------------------------------------------------- reports
def test_check_report_ok():
    assert C.check_report(1, 2, False, "spamming slurs in vc") is None


def test_check_report_self_and_bot():
    assert C.check_report(1, 1, False, "reporting myself") is C.ReportProblem.SELF
    assert C.check_report(1, 2, True, "bot did a thing") is C.ReportProblem.BOT


def test_check_target_ignores_reason():
    assert C.check_target(1, 2, False) is None
    assert C.check_target(1, 1, False) is C.ReportProblem.SELF
    assert C.check_target(1, 2, True) is C.ReportProblem.BOT


def test_check_report_reason_length_after_strip():
    assert C.check_report(1, 2, False, "  hey  ") is C.ReportProblem.REASON_SHORT
    assert C.check_report(1, 2, False, "x" * 5) is None
    assert C.check_report(1, 2, False, "x" * 300) is None
    assert C.check_report(1, 2, False, "x" * 301) is C.ReportProblem.REASON_LONG


def test_every_problem_has_a_reply():
    for problem in C.ReportProblem:
        assert C.REPORT_REPLIES[problem]


def test_rate_limit_three_per_window():
    assert not C.rate_limited(0)
    assert not C.rate_limited(2)
    assert C.rate_limited(3)
    assert C.rate_limited(10)
    assert C.RATE_WINDOW == 600


def test_quote_truncates_and_blockquotes():
    assert C.quote(None) is None
    assert C.quote("   ") is None
    assert C.quote("hello") == "> hello"
    assert C.quote("a\nb") == "> a\n> b"
    long = C.quote("x" * 400)
    assert long.endswith("…") and len(long) == len("> ") + 300


@pytest.mark.parametrize("owner,admin,names,expected", [
    (True, False, [], True),
    (False, True, [], True),
    (False, False, ["Moderator"], True),
    (False, False, ["🛡️ keeper"], True),
    (False, False, ["Valorant", "LFG"], False),
])
def test_can_handle(owner, admin, names, expected):
    assert C.can_handle(owner, admin, roles(*names)) is expected


# ---------------------------------------------------------------- mod log
def test_account_age_text():
    now = 1_000_000_000
    assert C.account_age(now - 3 * 3600, now) == "3 hours"
    assert C.account_age(now - 1 * 3600, now) == "1 hour"
    assert C.account_age(now - 30, now) == "under an hour"
    assert C.account_age(now - 2 * DAY, now) == "2 days"
    assert C.account_age(now - 400 * DAY, now) == "1 year"
    assert C.account_age(now - 800 * DAY, now) == "2 years"


def test_new_account_flag():
    now = 1_000_000_000
    assert C.is_new_account(now - 6 * DAY, now)
    assert not C.is_new_account(now - 7 * DAY, now)
    line = C.join_line("<@5>", "newbie", 5, now - 2 * DAY, now)
    assert "new account" in line and "2 days" in line and "`5`" in line
    old = C.join_line("<@5>", "vet", 5, now - 90 * DAY, now)
    assert "new account" not in old and "90 days" in old


def test_automod_line():
    line = C.automod_line("No slurs", "<@5>", "<#7>", "badword")
    assert "No slurs" in line and "<@5>" in line and "<#7>" in line and "`badword`" in line
    assert "matched" not in C.automod_line("No slurs", "<@5>", None, None)
