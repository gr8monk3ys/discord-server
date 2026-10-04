import random
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from logic import engagement as E
from logic.engagement import CountState, Daily

LA = ZoneInfo("America/Los_Angeles")
HOUR = 3600
DAY = 24 * HOUR
QOTD = Daily("qotd", 12, 0)


def local(y, m, d, h=0, mi=0) -> int:
    return int(datetime(y, m, d, h, mi, tzinfo=LA).timestamp())


# ---------------------------------------------------------------- daily planner
def test_daily_occurrence_key_and_time():
    p = E.daily_occurrence(QOTD, date(2026, 10, 4), LA)
    assert p.key == "qotd:2026-10-04"
    assert p.scheduled_at == int(datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc).timestamp())


def test_latest_due_before_and_after_the_time():
    assert E.latest_due_daily(QOTD, local(2026, 10, 4, 11, 59), LA).key == "qotd:2026-10-03"
    assert E.latest_due_daily(QOTD, local(2026, 10, 4, 12, 0), LA).key == "qotd:2026-10-04"
    assert E.latest_due_daily(QOTD, local(2026, 10, 4, 23, 59), LA).key == "qotd:2026-10-04"


def test_latest_due_across_dst():
    # 2026-11-01 is the fall-back day; noon local still resolves to the right day.
    p = E.latest_due_daily(QOTD, local(2026, 11, 1, 12, 30), LA)
    assert p.key == "qotd:2026-11-01" and datetime.fromtimestamp(p.scheduled_at, LA).hour == 12
    p = E.latest_due_daily(QOTD, local(2026, 3, 8, 12, 0), LA)
    assert p.key == "qotd:2026-03-08"


def test_first_startup_marks_latest_done_without_running():
    now = local(2026, 10, 4, 13)
    plan = E.plan_daily(QOTD, now, LA, set(), None)
    assert plan.run is None and [p.key for p in plan.mark_done] == ["qotd:2026-10-04"]


def test_first_startup_before_todays_time_marks_yesterday():
    plan = E.plan_daily(QOTD, local(2026, 10, 4, 9), LA, set(), None)
    assert plan.run is None and [p.key for p in plan.mark_done] == ["qotd:2026-10-03"]


def test_latest_predating_first_seen_is_marked_not_run():
    first_seen = local(2026, 10, 4, 13)
    plan = E.plan_daily(QOTD, local(2026, 10, 4, 14), LA, set(), first_seen)
    assert plan.run is None and [p.key for p in plan.mark_done] == ["qotd:2026-10-04"]


def test_runs_latest_once_due():
    first_seen = local(2026, 10, 3, 13)
    plan = E.plan_daily(QOTD, local(2026, 10, 4, 12, 1), LA, {"qotd:2026-10-03"}, first_seen)
    assert plan.run.key == "qotd:2026-10-04" and plan.mark_done == []


def test_done_latest_does_nothing():
    plan = E.plan_daily(QOTD, local(2026, 10, 4, 15), LA, {"qotd:2026-10-04"}, local(2026, 10, 1))
    assert plan.run is None and plan.mark_done == []


def test_catch_up_runs_only_latest_and_marks_missed():
    first_seen = local(2026, 10, 1, 8)  # before the 1st's noon
    plan = E.plan_daily(QOTD, local(2026, 10, 4, 18), LA, {"qotd:2026-10-02"}, first_seen)
    assert plan.run.key == "qotd:2026-10-04"
    assert [p.key for p in plan.mark_done] == ["qotd:2026-10-01", "qotd:2026-10-03"]


def test_catch_up_never_touches_days_before_first_seen():
    first_seen = local(2026, 10, 2, 13)  # after the 2nd's noon
    plan = E.plan_daily(QOTD, local(2026, 10, 4, 18), LA, set(), first_seen)
    assert [p.key for p in plan.mark_done] == ["qotd:2026-10-03"]


# ---------------------------------------------------------------- banks
def test_read_lines_skips_blanks_and_comments():
    assert E.read_lines("# header\n\n  one  \ntwo\n   \n#x\n") == ["one", "two"]


@pytest.mark.parametrize("line,expected", [
    ("Pizza | Tacos", ("Pizza", "Tacos")),
    ("  Cats|Dogs  ", ("Cats", "Dogs")),
    ("Just one", None),
    ("A | B | C", None),
    (" | B", None),
    ("Same | same", None),
])
def test_parse_pair(line, expected):
    assert E.parse_pair(line) == expected


def test_pick_prefers_unused():
    rng = random.Random(1)
    for _ in range(50):
        p = E.pick_next(5, {0, 1, 3}, rng)
        assert p.index in (2, 4) and not p.reset


def test_pick_walks_whole_bank_without_repeats():
    rng = random.Random(7)
    used: set[int] = set()
    seen = []
    for _ in range(10):
        p = E.pick_next(10, used, rng)
        assert not p.reset
        used.add(p.index)
        seen.append(p.index)
    assert sorted(seen) == list(range(10))


def test_pick_resets_when_exhausted_and_avoids_last():
    rng = random.Random(3)
    for _ in range(50):
        p = E.pick_next(4, {0, 1, 2, 3}, rng, last=2)
        assert p.reset and p.index != 2


def test_pick_ignores_stale_ids_and_handles_tiny_banks():
    rng = random.Random(0)
    assert E.pick_next(0, set(), rng) is None
    assert E.pick_next(1, {0}, rng, last=0) == E.Pick(0, True)
    # ids from a longer old bank don't count as used
    assert E.pick_next(2, {5, 9}, rng).reset is False


def test_qotd_bank_is_big_unique_and_clean():
    qs = E.load_questions()
    assert len(qs) >= 365
    assert len({q.lower() for q in qs}) == len(qs), "duplicate questions"
    for q in qs:
        assert q.endswith("?"), q
        assert 10 <= len(q) <= 200, q
        assert "@" not in q and "http" not in q.lower(), q


def test_poll_bank_is_big_unique_and_fits_discord():
    raw = E.read_lines(E.POLLS_FILE.read_text(encoding="utf-8"))
    pairs = E.load_pairs()
    assert len(pairs) == len(raw), "every line must be a valid 'A | B' pair"
    assert len(pairs) >= 200
    keys = {tuple(sorted(s.lower() for s in p)) for p in pairs}
    assert len(keys) == len(pairs), "duplicate pairs"
    for a, b in pairs:
        assert len(a) <= E.POLL_ANSWER_MAX and len(b) <= E.POLL_ANSWER_MAX
        assert "@" not in a + b


# ---------------------------------------------------------------- counting
@pytest.mark.parametrize("text,expected", [
    ("1", 1), (" 42 ", 42), ("007", 7), ("12\n", 12),
    ("", None), (None, None), ("-3", None), ("+3", None), ("4.0", None), ("1 2", None),
    ("5!", None), ("five", None), ("1e3", None), ("１", None), ("9" * 16, None),
])
def test_parse_count(text, expected):
    assert E.parse_count(text) == expected


def test_count_happy_path_and_new_best():
    s = CountState()
    step = E.count_step(s, 1, 1)
    assert step.ok and step.state == CountState(1, 1, 1) and step.new_best
    step = E.count_step(step.state, 2, 2)
    assert step.ok and step.state == CountState(2, 2, 2) and step.new_best


def test_count_below_best_is_not_new_best():
    step = E.count_step(CountState(3, 1, 10), 2, 4)
    assert step.ok and not step.new_best and step.state == CountState(4, 2, 10)


def test_wrong_number_resets_and_keeps_best():
    step = E.count_step(CountState(5, 1, 9), 2, 7)
    assert step.kind == "wrong" and step.state == CountState(0, None, 9) and step.broke_at == 5


def test_double_count_resets_even_with_right_number():
    step = E.count_step(CountState(5, 1, 9), 1, 6)
    assert step.kind == "double" and step.state == CountState(0, None, 9) and step.broke_at == 5


def test_after_reset_anyone_can_start_at_one():
    step = E.count_step(CountState(0, None, 9), 1, 1)
    assert step.ok and step.state.current == 1


def test_champion():
    assert E.champion({}) is None
    assert E.champion({1: 3, 2: 5}) == 2
    assert E.champion({1: 5, 2: 5}) == 1
    assert E.champion({1: 5, 2: 5}, current=2) == 2
    assert E.champion({1: 5, 2: 4}, current=2) == 1


# ---------------------------------------------------------------- birthdays
@pytest.mark.parametrize("m,d,ok", [
    (1, 31, True), (2, 29, True), (2, 30, False), (4, 31, False), (12, 31, True),
    (0, 1, False), (13, 1, False), (6, 0, False),
])
def test_valid_birthday(m, d, ok):
    assert E.valid_birthday(m, d) is ok


def test_leap_day_celebrated_feb_28_in_common_years():
    assert E.celebrated_on(2, 29, 2027) == date(2027, 2, 28)
    assert E.celebrated_on(2, 29, 2028) == date(2028, 2, 29)
    assert E.celebrated_on(2, 29, 2100) == date(2100, 2, 28)
    assert E.celebrated_on(2, 29, 2000) == date(2000, 2, 29)


def test_birthdays_on():
    rows = [(1, 10, 4), (2, 2, 29), (3, 2, 28), (4, 10, 5)]
    assert E.birthdays_on(rows, date(2026, 10, 4)) == [1]
    assert E.birthdays_on(rows, date(2027, 2, 28)) == [2, 3]
    assert E.birthdays_on(rows, date(2028, 2, 28)) == [3]
    assert E.birthdays_on(rows, date(2028, 2, 29)) == [2]


def test_upcoming_wraps_year_and_includes_today():
    rows = [(1, 1, 5), (2, 10, 4), (3, 12, 25), (4, 10, 3), (5, 11, 1), (6, 10, 20), (7, 2, 29)]
    got = E.upcoming(rows, date(2026, 10, 4), n=5)
    assert got == [(2, date(2026, 10, 4)), (6, date(2026, 10, 20)), (5, date(2026, 11, 1)),
                   (3, date(2026, 12, 25)), (1, date(2027, 1, 5))]
    assert E.upcoming(rows, date(2026, 12, 26), n=3) == [(1, date(2027, 1, 5)), (7, date(2027, 2, 28)),
                                                         (4, date(2027, 10, 3))]


def test_birthday_text():
    assert E.birthday_text(2, 29) == "February 29"


# ---------------------------------------------------------------- auto game night
def test_friday_evening_window():
    start, end, gn = E.friday_evening(date(2026, 10, 9), LA)
    assert start == local(2026, 10, 9, 17)
    assert end == local(2026, 10, 10, 3)
    assert gn == local(2026, 10, 9, 21)


@pytest.mark.parametrize("name,role", [
    ("VALORANT", "Valorant"), ("Call of Duty® HQ", "Call of Duty"), ("Counter-Strike 2", "Counter-Strike 2"),
    ("Minecraft Launcher", "Minecraft"), ("League of Legends", "League of Legends"),
    ("Some Indie Game", None), ("", None), ("®", None),
])
def test_game_for(name, role):
    g = E.game_for(name)
    assert (g.role if g else None) == role


def test_top_game_folds_names_and_ignores_unknown():
    secs = {"VALORANT": 3 * HOUR, "Call of Duty®: Black Ops": 2 * HOUR, "Call of Duty HQ": 2 * HOUR,
            "Some Huge Indie Game": 50 * HOUR}
    assert E.top_game(secs).role == "Call of Duty"
    assert E.top_game({"Unlisted": HOUR}) is None
    assert E.top_game({}) is None


def test_top_game_tie_goes_to_layout_order():
    order = [g.role for g in config.GAMES]
    a, b = order[0], order[1]
    assert E.top_game({b: HOUR, a: HOUR}).role == a
