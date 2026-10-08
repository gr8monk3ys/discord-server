"""Pure achievement rules: which badges a member's numbers earn."""

from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from logic import achievements as A

PT = ZoneInfo("America/Los_Angeles")
HOUR = 3600


def keys(badges):
    return [b.key for b in badges]


def test_twenty_badges_with_unique_keys_and_emoji():
    assert len(A.BADGES) == 20 == A.TOTAL
    assert len({b.key for b in A.BADGES}) == 20
    assert len({b.emoji for b in A.BADGES}) == 20
    for b in A.BADGES:
        assert b.name and b.description and b.emoji
        assert A.BY_KEY[b.key] is b


def test_nothing_earned_by_default():
    assert A.earned(A.Facts()) == set()


@pytest.mark.parametrize("field,value,expected", [
    ("squads_joined", 1, {"first_squad"}),
    ("squads_joined", 10, {"first_squad", "squad_regular"}),
    ("squads_hosted", 1, {"squad_host"}),
    ("gamenights_hosted", 1, {"gamenight_host"}),
    ("voice_seconds", 10 * HOUR - 1, set()),
    ("voice_seconds", 10 * HOUR, {"voice_10h"}),
    ("voice_seconds", 100 * HOUR, {"voice_10h", "voice_100h"}),
    ("messages", 99, set()),
    ("messages", 100, {"chatty"}),
    ("messages", 1000, {"chatty", "messages_1000"}),
    ("clips", 1, {"first_clip"}),
    ("clip_week_wins", 1, {"clip_week"}),
    ("hall_of_fame", 1, {"hall_of_fame"}),
    ("mvp_wins", 1, {"weekly_mvp"}),
    ("daily_streak", 6, set()),
    ("daily_streak", 7, {"streak_7"}),
    ("daily_streak", 30, {"streak_7", "streak_30"}),
    ("balance", 9_999, set()),
    ("balance", 10_000, {"coins_10k"}),
    ("tournaments_entered", 1, {"tourney_entry"}),
    ("tournament_wins", 1, {"tourney_win"}),
    ("birthday_set", True, {"birthday"}),
    ("recruiter", True, {"recruiter"}),
    ("early_member", True, {"early_member"}),
])
def test_each_threshold(field, value, expected):
    assert A.earned(replace(A.Facts(), **{field: value})) == expected


def test_every_badge_is_reachable():
    everything = A.Facts(squads_joined=10, squads_hosted=1, gamenights_hosted=1, voice_seconds=100 * HOUR,
                         messages=1000, clips=1, clip_week_wins=1, hall_of_fame=1, mvp_wins=1, daily_streak=30,
                         balance=10_000, tournaments_entered=1, tournament_wins=1, birthday_set=True,
                         recruiter=True, early_member=True)
    assert A.earned(everything) == set(A.BY_KEY)


def test_opted_out_never_earns_tracking_badges():
    f = A.Facts(voice_seconds=200 * HOUR, messages=5000, squads_joined=1, tracking=False)
    assert A.earned(f) == {"first_squad"}
    tracking = {b.key for b in A.BADGES if b.tracking}
    assert tracking == {"voice_10h", "voice_100h", "chatty", "messages_1000"}


def test_new_badges_skips_held_and_keeps_catalogue_order():
    f = A.Facts(squads_joined=10, balance=10_000, messages=100)
    assert keys(A.new_badges(f, {"squad_regular"})) == ["first_squad", "chatty", "coins_10k"]
    assert A.new_badges(f, A.earned(f)) == []


def test_new_badges_ignores_unknown_held_keys():
    assert keys(A.new_badges(A.Facts(clips=1), {"retired_badge"})) == ["first_clip"]


def test_early_member_cutoff_is_pacific_midnight_nov_1():
    cutoff = A.early_cutoff(PT)
    assert cutoff == int(datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc).timestamp())
    assert A.is_early(datetime.fromtimestamp(cutoff - 1, timezone.utc), PT)
    assert not A.is_early(datetime.fromtimestamp(cutoff, timezone.utc), PT)
    assert not A.is_early(None, PT)


def test_grid_rows_of_five_in_catalogue_order():
    held = {"early_member", "first_squad", "chatty", "first_clip", "birthday", "streak_7", "balance?"}
    grid = A.grid(held)
    rows = grid.split("\n")
    assert len(rows) == 2
    order = [b.emoji for b in A.BADGES if b.key in held]
    assert rows[0] == " ".join(order[:5]) and rows[1] == " ".join(order[5:])
    assert A.grid(set()) == A.NO_BADGES


def test_progress():
    assert A.progress({"first_squad", "chatty"}) == "2/20"
    assert A.progress({"first_squad", "gone"}) == "1/20"


def test_congrats_one_and_many():
    one = A.congrats("<@5>", [A.BY_KEY["streak_7"]])
    assert "<@5>" in one and "On Fire" in one and A.BY_KEY["streak_7"].emoji in one
    many = A.congrats("<@5>", [A.BY_KEY["first_squad"], A.BY_KEY["chatty"]])
    assert "2 badges" in many and "Squad Up" in many and "Chatty" in many
    assert "/profile" in one and "/profile" in many


def test_season_points_window_and_reasons():
    from logic import shop
    assert A.SEASON_REASONS == shop.SEASON_REASONS
