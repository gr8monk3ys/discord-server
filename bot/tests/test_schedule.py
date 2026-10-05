from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from logic import schedule
from logic.schedule import Period, Plan, Weekly

LA = ZoneInfo("America/Los_Angeles")
MVP = Weekly("mvp", 6, 18, 0)  # Sundays 18:00 local
HOUR = 60 * 60
WEEK = 7 * 24 * HOUR


def utc(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def occ(y, m, d) -> Period:
    return schedule.occurrence(MVP, date(y, m, d), LA)


# --- occurrence ---------------------------------------------------------------


@pytest.mark.parametrize(
    "day,key",
    [
        (date(2026, 10, 4), "mvp:2026-W40"),
        (date(2025, 12, 28), "mvp:2025-W52"),
        (date(2026, 1, 4), "mvp:2026-W01"),
        (date(2027, 1, 3), "mvp:2026-W53"),  # January Sunday still in the old ISO year
    ],
)
def test_occurrence_key_uses_iso_week_of_local_date(day, key):
    assert schedule.occurrence(MVP, day, LA).key == key


def test_occurrence_in_pdt():
    p = occ(2026, 10, 4)
    assert p.scheduled_at == utc(2026, 10, 5, 1, 0)  # 18:00 PDT = 01:00Z next day
    assert p.window_end == p.scheduled_at
    assert p.scheduled_at - p.window_start == 7 * 24 * HOUR


def test_occurrence_in_pst():
    p = occ(2026, 12, 6)
    assert p.scheduled_at == utc(2026, 12, 7, 2, 0)  # 18:00 PST = 02:00Z next day
    assert p.scheduled_at - p.window_start == 7 * 24 * HOUR


def test_occurrence_minute_and_other_job():
    clips = Weekly("clips", 6, 18, 5)
    p = schedule.occurrence(clips, date(2026, 10, 4), LA)
    assert p.key == "clips:2026-W40"
    assert p.scheduled_at == utc(2026, 10, 5, 1, 5)


def test_spring_forward_week_is_167_hours():
    p = occ(2026, 3, 8)
    assert p.scheduled_at == utc(2026, 3, 9, 1, 0)  # 18:00 PDT
    assert p.window_start == utc(2026, 3, 2, 2, 0)  # 18:00 PST the Sunday before
    assert p.window_end - p.window_start == 167 * HOUR
    assert datetime.fromtimestamp(p.scheduled_at, LA).hour == 18


def test_fall_back_week_is_169_hours():
    p = occ(2026, 11, 1)
    assert p.scheduled_at == utc(2026, 11, 2, 2, 0)  # 18:00 PST
    assert p.window_start == utc(2026, 10, 26, 1, 0)  # 18:00 PDT the Sunday before
    assert p.window_end - p.window_start == 169 * HOUR
    assert datetime.fromtimestamp(p.scheduled_at, LA).hour == 18


def test_consecutive_windows_tile_without_gaps():
    a, b = occ(2026, 3, 1), occ(2026, 3, 8)
    assert b.window_start == a.window_end


def test_occurrence_rejects_wrong_weekday():
    with pytest.raises(ValueError):
        occ(2026, 10, 5)  # a Monday


# --- latest_due ---------------------------------------------------------------

SUN_W40 = occ(2026, 10, 4)
SUN_W39 = occ(2026, 9, 27)


def test_latest_due_around_the_scheduled_minute():
    t = SUN_W40.scheduled_at
    assert schedule.latest_due(MVP, t - 1, LA) == SUN_W39
    assert schedule.latest_due(MVP, t, LA) == SUN_W40
    assert schedule.latest_due(MVP, t + 1, LA) == SUN_W40


def test_latest_due_midweek_and_late_sunday_local():
    # Thursday 2026-10-01 noon local -> previous Sunday.
    assert schedule.latest_due(MVP, utc(2026, 10, 1, 19, 0), LA) == SUN_W39
    # Sunday 23:30 local is already Monday in UTC; still this Sunday's period.
    assert schedule.latest_due(MVP, utc(2026, 10, 5, 6, 30), LA) == SUN_W40
    # Sunday 10:00 local, before 18:00 -> last week's.
    assert schedule.latest_due(MVP, utc(2026, 10, 4, 17, 0), LA) == SUN_W39


# --- periods_between ----------------------------------------------------------


def test_periods_between_bounds_are_exclusive_inclusive():
    w38, w39, w40 = occ(2026, 9, 20), SUN_W39, SUN_W40
    got = schedule.periods_between(MVP, w38.scheduled_at, w40.scheduled_at, LA)
    assert got == [w39, w40]  # start excluded, end included
    got = schedule.periods_between(MVP, w38.scheduled_at - 1, w40.scheduled_at - 1, LA)
    assert got == [w38, w39]


def test_periods_between_empty():
    t = SUN_W40.scheduled_at
    assert schedule.periods_between(MVP, t, t, LA) == []
    assert schedule.periods_between(MVP, t + 1, t + WEEK - 1, LA) == []
    assert schedule.periods_between(MVP, t, t - 5, LA) == []


def test_periods_between_spans_dst():
    got = schedule.periods_between(MVP, occ(2026, 10, 18).scheduled_at, occ(2026, 11, 15).scheduled_at, LA)
    assert [p.key for p in got] == ["mvp:2026-W43", "mvp:2026-W44", "mvp:2026-W45", "mvp:2026-W46"]


# --- plan ---------------------------------------------------------------------

def test_first_startup_marks_latest_done_without_running():
    now = SUN_W40.scheduled_at + 3 * HOUR
    assert schedule.plan(MVP, now, LA, set(), None) == Plan(None, [SUN_W40])


def test_first_startup_when_already_done_does_nothing():
    now = SUN_W40.scheduled_at + 3 * HOUR
    assert schedule.plan(MVP, now, LA, {SUN_W40.key}, None) == Plan(None, [])


def test_normal_weekly_run():
    now = SUN_W40.scheduled_at + 5 * 60
    first_seen = occ(2026, 9, 20).scheduled_at - HOUR
    done = {SUN_W39.key, occ(2026, 9, 20).key}
    assert schedule.plan(MVP, now, LA, done, first_seen) == Plan(SUN_W40, [])


def test_pc_off_three_weeks_runs_only_latest():
    w37, w38, w39, w40 = occ(2026, 9, 13), occ(2026, 9, 20), SUN_W39, SUN_W40
    now = w40.scheduled_at + 2 * HOUR  # last ran w37, then off
    first_seen = w37.scheduled_at - HOUR
    p = schedule.plan(MVP, now, LA, {w37.key}, first_seen)
    assert p == Plan(w40, [w38, w39])
    assert p.run.window_end == w40.scheduled_at  # anchored to schedule, not now


def test_already_done_does_nothing():
    now = SUN_W40.scheduled_at + 10 * 60
    assert schedule.plan(MVP, now, LA, {SUN_W40.key}, SUN_W39.scheduled_at) == Plan(None, [])


def test_period_before_first_seen_is_not_run():
    # Bot first started Sunday 20:00, after W40's 18:00; its record was lost.
    first_seen = SUN_W40.scheduled_at + 2 * HOUR
    now = first_seen + HOUR
    assert schedule.plan(MVP, now, LA, set(), first_seen) == Plan(None, [SUN_W40])


def test_missed_periods_before_first_seen_are_not_backfilled():
    first_seen = SUN_W39.scheduled_at + HOUR  # bot born after W39
    now = SUN_W40.scheduled_at + HOUR
    assert schedule.plan(MVP, now, LA, set(), first_seen) == Plan(SUN_W40, [])


def test_period_exactly_at_first_seen_counts():
    first_seen = SUN_W39.scheduled_at
    now = SUN_W40.scheduled_at + HOUR
    assert schedule.plan(MVP, now, LA, set(), first_seen) == Plan(SUN_W40, [SUN_W39])
