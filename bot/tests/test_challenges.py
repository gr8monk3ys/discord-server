"""Pure rules for weekly challenges (logic/challenges.py)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from logic import challenges as CH
from logic import quests as Q
from logic import schedule

TZ = ZoneInfo("America/Los_Angeles")
DAY = 24 * 3600
HOUR = 3600


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


# ---------------------------------------------------------------- pool
def test_pool_has_twelve_with_four_per_tier_and_unique_keys():
    assert len(CH.POOL) == 12
    assert len(CH.BY_KEY) == 12
    for tier in (1, 2, 3):
        assert len([c for c in CH.POOL if c.tier == tier]) == 4
    assert CH.BONUS_KEY not in CH.BY_KEY
    assert all(c.target > 0 for c in CH.POOL)


def test_rewards_by_tier_and_bonus():
    assert CH.REWARDS == {1: 150, 2: 250, 3: 400}
    assert CH.BONUS == 300
    assert CH.reward(CH.BY_KEY["join_squads"]) == 150
    assert CH.reward(CH.BY_KEY["voice_3h"]) == 250
    assert CH.reward(CH.BY_KEY["hall_of_fame"]) == 400


def test_tracking_flags():
    tracked = {c.key for c in CH.POOL if c.tracking}
    assert tracked == {"messages", "post_clip", "daily", "voice_3h", "voice_8h"}


def test_reuses_quest_account_age():
    assert CH.MIN_ACCOUNT_DAYS == Q.MIN_ACCOUNT_DAYS


# ---------------------------------------------------------------- weeks
def test_week_of_is_iso_week_monday_to_monday_local():
    # Wednesday 2026-10-07 is ISO 2026-W41
    w = CH.week_of(ts(2026, 10, 7, 15, 0), TZ)
    assert w.key == "2026-W41"
    assert w.start == ts(2026, 10, 5, 0, 0)
    assert w.end == ts(2026, 10, 12, 0, 0)
    assert w.first_day == date(2026, 10, 5) and w.last_day == date(2026, 10, 11)
    # Monday 00:00 starts the new week; Sunday 23:59 is the old one.
    assert CH.week_of(ts(2026, 10, 12, 0, 0), TZ).key == "2026-W42"
    assert CH.week_of(ts(2026, 10, 11, 23, 59), TZ).key == "2026-W41"


def test_week_across_dst_end_is_169_hours():
    w = CH.week_of(ts(2026, 11, 3, 12, 0), TZ)  # DST ends Sunday 2026-11-01
    assert w.key == "2026-W45"
    w = CH.week_of(ts(2026, 10, 28, 12, 0), TZ)  # the week containing the change
    assert w.end - w.start == 7 * DAY + HOUR


def test_week_key_matches_board_job_period_key():
    t = ts(2026, 10, 5, 9, 0)
    period = schedule.latest_due(CH.BOARD_JOB, t, TZ)
    assert period.key == f"{CH.BOARD_JOB.name}:{CH.week_of(t, TZ).key}"
    assert CH.BOARD_JOB.weekday == 0 and (CH.BOARD_JOB.hour, CH.BOARD_JOB.minute) == (9, 0)


def test_previous_week_and_year_boundary():
    w = CH.week_of(ts(2026, 10, 7, 12), TZ)
    p = CH.previous_week(w, TZ)
    assert p.key == "2026-W40" and p.end == w.start
    # 2027-01-01 (Fri) belongs to ISO 2026-W53
    assert CH.week_of(ts(2027, 1, 1, 12), TZ).key == "2026-W53"
    assert CH.previous_week(CH.week_of(ts(2027, 1, 5, 12), TZ), TZ).key == "2026-W53"


def test_weeks_to_settle_includes_last_week_during_grace():
    w = CH.week_of(ts(2026, 10, 12, 0, 30), TZ)
    keys = [x.key for x in CH.weeks_to_settle(ts(2026, 10, 12, 0, 30), TZ)]
    assert keys == ["2026-W41", "2026-W42"]
    later = w.start + CH.GRACE + 1
    assert [x.key for x in CH.weeks_to_settle(later, TZ)] == ["2026-W42"]


# ---------------------------------------------------------------- picks
def test_pick_is_deterministic_one_per_tier():
    a = CH.pick("2026-W41")
    assert a == CH.pick("2026-W41")
    assert [c.tier for c in a] == [1, 2, 3]
    assert len({c.key for c in a}) == 3


def test_pick_varies_and_never_more_than_one_tracking():
    seen = set()
    for year in (2026, 2027):
        for week in range(1, 53):
            picks = CH.pick(f"{year}-W{week:02}")
            assert sum(c.tracking for c in picks) <= CH.MAX_TRACKING
            seen.update(c.key for c in picks)
    assert seen == set(CH.BY_KEY)  # every challenge comes up eventually


# ---------------------------------------------------------------- progress and claims
def picks3():
    return (CH.BY_KEY["join_squads"], CH.BY_KEY["voice_3h"], CH.BY_KEY["hall_of_fame"])


def test_available_and_complete():
    v = CH.BY_KEY["voice_3h"]
    assert CH.available(v, True) and not CH.available(v, False)
    assert CH.available(CH.BY_KEY["join_squads"], False)
    assert not CH.complete(v, 3 * HOUR - 1) and CH.complete(v, 3 * HOUR)


def test_claimable_pays_complete_unclaimed_and_bonus_when_all_done():
    p = picks3()
    progress = {"join_squads": 2, "voice_3h": 3 * HOUR, "hall_of_fame": 0}
    assert CH.claimable(p, progress, set(), True) == [("join_squads", 150), ("voice_3h", 250)]
    assert CH.claimable(p, progress, {"join_squads"}, True) == [("voice_3h", 250)]
    progress["hall_of_fame"] = 1
    assert CH.claimable(p, progress, {"join_squads", "voice_3h"}, True) == [("hall_of_fame", 400), ("bonus", 300)]
    assert CH.claimable(p, progress, {"join_squads", "voice_3h", "hall_of_fame", "bonus"}, True) == []


def test_claimable_opted_out_skips_tracking_and_bonus_needs_the_rest():
    p = picks3()
    progress = {"join_squads": 5, "voice_3h": 99 * HOUR, "hall_of_fame": 1}
    assert CH.claimable(p, progress, set(), False) == [("join_squads", 150), ("hall_of_fame", 400), ("bonus", 300)]
    assert CH.claimable(p, {"join_squads": 5}, set(), False) == [("join_squads", 150)]


def test_total_and_ref():
    assert CH.total([("a", 150), ("bonus", 300)]) == 450
    assert CH.ref("2026-W41", 11, "voice_3h") == "challenge:2026-W41:11:voice_3h"


def test_old_enough():
    now = 1_790_000_000
    assert CH.old_enough(now - CH.MIN_ACCOUNT_DAYS * DAY, now)
    assert not CH.old_enough(now - CH.MIN_ACCOUNT_DAYS * DAY + 1, now)
    assert not CH.old_enough(None, now)


# ---------------------------------------------------------------- text
def test_fmt_progress_hours_and_counts():
    assert CH.fmt_progress(CH.BY_KEY["voice_3h"], 90 * 60) == "1h 30m / 3h"
    assert CH.fmt_progress(CH.BY_KEY["voice_3h"], 10 * HOUR) == "3h / 3h"
    assert CH.fmt_progress(CH.BY_KEY["messages"], 42) == "42 / 100"
    assert CH.fmt_progress(CH.BY_KEY["messages"], 420) == "100 / 100"


def test_board_text_lists_three_and_bonus():
    p = picks3()
    text = CH.board_text(p, CH.week_of(ts(2026, 10, 5, 9), TZ))
    for c in p:
        assert c.name in text and f"{CH.reward(c):,}" in text
    assert "300" in text and "/challenges" in text
    assert f"<t:{ts(2026, 10, 12, 0, 0)}:R>" in text


def test_progress_text_marks():
    p = picks3()
    progress = {"join_squads": 2, "voice_3h": HOUR, "hall_of_fame": 1}
    text = CH.progress_text(p, progress, {"join_squads"}, True)
    lines = text.splitlines()
    assert lines[0].startswith("✅")  # claimed
    assert lines[1].startswith("▫️") and "1h 0m / 3h" in lines[1]
    assert lines[2].startswith("🎁")  # ready to claim
    assert "bonus" in text.lower()
    off = CH.progress_text(p, progress, set(), False)
    assert "~~" in off.splitlines()[1] and "/privacy" in off


def test_claim_text_pings_mention_and_total():
    text = CH.claim_text("<@11>", [("join_squads", 150), ("bonus", 300)])
    assert text.startswith("🏅 <@11>") and "450" in text and "/challenges" in text
    assert "\n" not in text


@pytest.mark.parametrize("key", sorted(CH.BY_KEY))
def test_every_challenge_has_text(key):
    c = CH.BY_KEY[key]
    assert c.name and c.how and c.emoji
