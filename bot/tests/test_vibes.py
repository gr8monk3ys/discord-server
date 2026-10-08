"""Pure rules for community autopilot touches: chat revival, join anniversaries, boosters
and member of the month."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import config
from logic import quests as Q
from logic import vibes as V

TZ = ZoneInfo("America/Los_Angeles")
HOUR = 3600
DAY = 24 * HOUR


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


# ---------------------------------------------------------------- jobs
def test_job_times():
    assert (V.ANNIV_JOB.hour, V.ANNIV_JOB.minute) == (11, 0)
    assert (V.STIPEND_JOB.day, V.STIPEND_JOB.hour, V.STIPEND_JOB.minute) == (1, 12, 0)
    assert (V.MOTM_JOB.day, V.MOTM_JOB.hour, V.MOTM_JOB.minute) == (1, 12, 30)
    assert len({V.ANNIV_JOB.name, V.STIPEND_JOB.name, V.MOTM_JOB.name}) == 3


# ---------------------------------------------------------------- chat revival
def test_active_hours_are_10_to_23_local():
    assert not V.in_active_hours(ts(2026, 10, 7, 9, 59), TZ)
    assert V.in_active_hours(ts(2026, 10, 7, 10, 0), TZ)
    assert V.in_active_hours(ts(2026, 10, 7, 22, 59), TZ)
    assert not V.in_active_hours(ts(2026, 10, 7, 23, 0), TZ)
    assert not V.in_active_hours(ts(2026, 10, 7, 3, 0), TZ)


def test_revive_after_three_quiet_hours():
    t = ts(2026, 10, 7, 15, 0)
    assert V.should_revive(t, TZ, last_member_at=t - 3 * HOUR, last_revival_at=None)
    assert not V.should_revive(t, TZ, last_member_at=t - 3 * HOUR + 1, last_revival_at=None)


def test_no_revival_outside_active_hours():
    t = ts(2026, 10, 7, 23, 30)
    assert not V.should_revive(t, TZ, last_member_at=t - 10 * HOUR, last_revival_at=None)
    t = ts(2026, 10, 7, 8, 0)
    assert not V.should_revive(t, TZ, last_member_at=t - 10 * HOUR, last_revival_at=None)


def test_revival_at_most_once_per_six_hours():
    t = ts(2026, 10, 7, 20, 0)
    last_rev = t - 6 * HOUR + 1
    member = last_rev + 60  # a member spoke after it, then went quiet for 5+ hours
    assert not V.should_revive(t, TZ, last_member_at=member, last_revival_at=last_rev)
    assert V.should_revive(t + 1, TZ, last_member_at=member, last_revival_at=last_rev)


def test_never_twice_in_a_row_without_a_member_message():
    t = ts(2026, 10, 7, 22, 0)
    last_rev = t - 8 * HOUR
    assert not V.should_revive(t, TZ, last_member_at=last_rev - 60, last_revival_at=last_rev)
    assert not V.should_revive(t, TZ, last_member_at=last_rev, last_revival_at=last_rev)
    assert V.should_revive(t, TZ, last_member_at=last_rev + 1, last_revival_at=last_rev)


def test_unknown_last_message_never_revives():
    assert not V.should_revive(ts(2026, 10, 7, 15), TZ, last_member_at=None, last_revival_at=None)


def test_revival_text_carries_the_question():
    text = V.revival_text("What's your comfort game?")
    assert "What's your comfort game?" in text


# ---------------------------------------------------------------- anniversaries
def test_years_on_anniversary():
    assert V.years_on(date(2024, 10, 7), date(2026, 10, 7)) == 2
    assert V.years_on(date(2025, 10, 7), date(2026, 10, 7)) == 1
    assert V.years_on(date(2026, 10, 7), date(2026, 10, 7)) == 0  # joined today
    assert V.years_on(date(2025, 10, 8), date(2026, 10, 7)) == 0
    assert V.years_on(date(2027, 10, 7), date(2026, 10, 7)) == 0  # nonsense future


def test_leap_day_joiners_celebrate_feb_28():
    assert V.years_on(date(2024, 2, 29), date(2025, 2, 28)) == 1
    assert V.years_on(date(2024, 2, 29), date(2025, 3, 1)) == 0
    assert V.years_on(date(2024, 2, 29), date(2028, 2, 28)) == 0
    assert V.years_on(date(2024, 2, 29), date(2028, 2, 29)) == 4


def test_anniversaries_skips_done_and_caps():
    day = date(2026, 10, 7)
    members = [(1, date(2025, 10, 7)), (2, date(2023, 10, 7)), (3, date(2024, 10, 7)), (4, date(2025, 10, 8)),
               (5, date(2024, 10, 7))]
    assert V.anniversaries(members, day, done={}) == [(2, 3), (3, 2), (5, 2), (1, 1)]
    assert V.anniversaries(members, day, done={3: 2026, 1: 2025}) == [(2, 3), (5, 2), (1, 1)]
    many = [(i, date(2025, 10, 7)) for i in range(1, 30)]
    got = V.anniversaries(many, day, done={})
    assert len(got) == V.ANNIV_MAX == 10
    assert [u for u, _ in got] == list(range(1, 11))


def test_anniversary_text():
    one = V.anniversary_text([("<@1>", 1)])
    assert "<@1>" in one and "1 year" in one and "years" not in one
    two = V.anniversary_text([("<@1>", 3), ("<@2>", 1)])
    assert "<@1> (3 years)" in two and "<@2> (1 year)" in two


# ---------------------------------------------------------------- boosters
def test_boost_started():
    t = datetime(2026, 10, 7)
    assert V.boost_started(None, t)
    assert not V.boost_started(t, t)
    assert not V.boost_started(t, None)
    assert not V.boost_started(None, None)


def test_refs():
    assert V.boost_ref(42, "2026-10") == "boost:42:2026-10"
    assert V.stipend_ref("2026-11", 42) == "booststipend:2026-11:42"
    assert V.motm_ref("2026-10") == "motm:2026-10"


def test_boost_text_mentions_and_coins():
    text = V.boost_text("<@9>", V.BOOST_COINS)
    assert "<@9>" in text and "1,000" in text
    assert "coins" not in V.boost_text("<@9>", 0)


def test_old_enough_reuses_quest_minimum():
    t = ts(2026, 10, 7)
    assert V.MIN_ACCOUNT_DAYS == Q.MIN_ACCOUNT_DAYS
    assert V.old_enough(t - Q.MIN_ACCOUNT_DAYS * DAY, t)
    assert not V.old_enough(t - Q.MIN_ACCOUNT_DAYS * DAY + 1, t)
    assert not V.old_enough(None, t)


def test_staff_roles():
    assert V.is_staff([config.KEEPER_ROLE])
    assert V.is_staff(["@everyone", "🛡️ " + config.MOD_ROLE])
    assert not V.is_staff(["@everyone", "Regular", "LFG"])


# ---------------------------------------------------------------- member of the month
def test_activity_score():
    assert V.activity_score(voice_seconds=2 * HOUR, messages=100, squads=3) == 2 + 2 + 6
    assert V.activity_score(0, 25, 0) == 0.5


def test_combined_scores():
    s = V.scores(voice={1: 3600}, messages={1: 50, 2: 500}, squads={3: 1})
    assert s == {1: 2.0, 2: 10.0, 3: 2.0}


def test_pick_member_of_month():
    s = {1: 5.0, 2: 9.0, 3: 9.0, 4: 0.0}
    assert V.pick_winner(s, eligible=lambda u: True) == 2  # tie: lowest id
    assert V.pick_winner(s, eligible=lambda u: u != 2) == 3
    assert V.pick_winner(s, eligible=lambda u: u == 4) is None  # zero scores never win
    assert V.pick_winner({}, eligible=lambda u: True) is None
    assert V.pick_winner({1: V.MIN_SCORE - 0.01}, eligible=lambda u: True) is None


def test_motm_text():
    text = V.motm_text("<@7>", "2026-10", voice_seconds=5 * HOUR + 1800, messages=1234, squads=4,
                       coins=V.MOTM_COINS)
    assert "<@7>" in text and "October 2026" in text
    assert "5.5 h" in text and "1,234" in text and "4 squads" in text and "1,000" in text
    one = V.motm_text("<@7>", "2026-01", voice_seconds=0, messages=1, squads=1, coins=0)
    assert "1 squad" in one and "1 message" in one and "January 2026" in one and "coins" not in one
