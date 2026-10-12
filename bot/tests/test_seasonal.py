"""Pure rules of seasonal events: events.json, runs, drop times, quests, points, text."""

import copy
import json
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from logic import seasonal as S
from logic.quests import MIN_ACCOUNT_DAYS

TZ = ZoneInfo("America/Los_Angeles")
EVENTS = S.load_events()
HALLOWEEN = next(e for e in EVENTS if e.key == "halloween")
WINTER = next(e for e in EVENTS if e.key == "winter")


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


def raw_events() -> dict:
    with open(S.EVENTS_FILE, encoding="utf-8") as f:
        return json.load(f)


RUN = S.run_for(HALLOWEEN, 2026, TZ)


# ---------------------------------------------------------------- events.json
def test_shipped_events_are_halloween_and_winter():
    assert [e.key for e in EVENTS] == ["halloween", "winter"]
    assert HALLOWEEN.start == (10, 20) and HALLOWEEN.end == (11, 1) and not HALLOWEEN.wraps
    assert WINTER.start == (12, 15) and WINTER.end == (1, 2) and WINTER.wraps
    assert HALLOWEEN.drop_emoji == "🎃" and WINTER.drop_emoji == "❄️"
    for e in EVENTS:
        assert 0 <= e.color <= 0xFFFFFF and len(e.drop_lines) >= 5 and e.badge_name
        assert all("@" not in line and "http" not in line for line in e.drop_lines)


def test_shipped_text_passes_the_text_filter():
    from logic import textfilter
    for e in EVENTS:
        for text in (e.name, e.intro, e.drop_name, e.badge_name, *e.drop_lines):
            assert textfilter.check(text, allow_links=False) == (True, None)


@pytest.mark.parametrize("change, match", [
    (lambda e: e.update(key="Hallo ween"), "key"),
    (lambda e: e.update(key="a_b"), "key"),
    (lambda e: e.update(start="13-01"), "start"),
    (lambda e: e.update(start="02-29"), "every year"),
    (lambda e: e.update(end="10-20"), "same day"),
    (lambda e: e.update(color="orange"), "color"),
    (lambda e: e.update(name=""), "name"),
    (lambda e: e.update(name="x" * 100), "longer"),
    (lambda e: e["drop"].update(lines=[]), "lines"),
    (lambda e: e["drop"].update(lines=["ok", 5]), "lines"),
    (lambda e: e.pop("badge"), "badge"),
    (lambda e: e.update(start="12-20"), "overlap"),  # halloween moved into winter
])
def test_bad_events_are_rejected(change, match):
    data = raw_events()
    change(data["events"][0])
    with pytest.raises(S.EventError, match=match):
        S.parse_events(data)


def test_bad_shapes_and_duplicate_keys():
    for bad in (None, [], {"events": "x"}, {"events": ["x"]}):
        with pytest.raises(S.EventError):
            S.parse_events(bad)
    data = raw_events()
    twin = copy.deepcopy(data["events"][0])
    twin.update(start="03-01", end="03-05")
    data["events"].append(twin)
    with pytest.raises(S.EventError, match="twice"):
        S.parse_events(data)


def test_whitespace_in_text_is_collapsed():
    data = raw_events()
    data["events"][0]["name"] = "  Spooky \n Season "
    assert S.parse_events(data)[0].name == "Spooky Season"


# ---------------------------------------------------------------- runs
def test_run_bounds_and_ids():
    assert RUN.start == ts(2026, 10, 20) and RUN.end == ts(2026, 11, 1)
    assert RUN.id == "halloween:2026" and RUN.badge_key == "event_halloween_2026"
    winter = S.run_for(WINTER, 2026, TZ)
    assert winter.start == ts(2026, 12, 15) and winter.end == ts(2027, 1, 2)
    assert winter.id == "winter:2026"
    assert S.parse_run_id("winter:2026") == ("winter", 2026)
    assert S.parse_run_id("winter") is None and S.parse_run_id("Bad Key:2026") is None


def test_current_is_start_inclusive_end_exclusive():
    assert S.current(EVENTS, ts(2026, 10, 19, 23, 59, 59), TZ) is None
    assert S.current(EVENTS, ts(2026, 10, 20), TZ).id == "halloween:2026"
    assert S.current(EVENTS, ts(2026, 10, 31, 23, 59, 59), TZ).id == "halloween:2026"
    assert S.current(EVENTS, ts(2026, 11, 1), TZ) is None


def test_winter_run_spans_new_year_and_keeps_its_start_year():
    assert S.current(EVENTS, ts(2026, 12, 31, 23), TZ).id == "winter:2026"
    assert S.current(EVENTS, ts(2027, 1, 1, 12), TZ).id == "winter:2026"
    assert S.current(EVENTS, ts(2027, 1, 2), TZ) is None


def test_events_recur_every_year():
    assert S.current(EVENTS, ts(2031, 10, 25), TZ).id == "halloween:2031"
    assert S.current(EVENTS, ts(2040, 1, 1), TZ).id == "winter:2039"


def test_upcoming():
    assert S.upcoming(EVENTS, ts(2026, 10, 11), TZ).id == "halloween:2026"
    assert S.upcoming(EVENTS, ts(2026, 10, 25), TZ).id == "winter:2026"
    assert S.upcoming(EVENTS, ts(2027, 1, 1), TZ).id == "halloween:2027"
    assert S.upcoming((), ts(2026, 10, 11), TZ) is None


def test_last_ended_is_one_per_event_oldest_first():
    got = S.last_ended(EVENTS, ts(2026, 11, 5), TZ)
    assert [r.id for r in got] == ["winter:2025", "halloween:2026"]
    got = S.last_ended(EVENTS, ts(2027, 1, 2), TZ)
    assert [r.id for r in got] == ["halloween:2026", "winter:2026"]


def test_day_helpers_follow_local_time_across_dst():
    day = date(2026, 11, 1)  # clocks go back: a 25-hour day
    start, end = S.day_bounds(day, TZ)
    assert end - start == 25 * S.HOUR
    assert S.local_day(start, TZ) == day and S.local_day(start - 1, TZ) == date(2026, 10, 31)
    assert S.in_run(RUN, date(2026, 10, 20), TZ) and S.in_run(RUN, date(2026, 10, 31), TZ)
    assert not S.in_run(RUN, date(2026, 11, 1), TZ) and not S.in_run(RUN, date(2026, 10, 19), TZ)
    assert S.last_day(RUN, TZ) == date(2026, 10, 31)


# ---------------------------------------------------------------- drops
def all_days(run):
    day = S.local_day(run.start, TZ)
    while S.in_run(run, day, TZ):
        yield day
        day += timedelta(days=1)


def test_drop_slots_are_stable_and_within_the_rules():
    for run in (RUN, S.run_for(WINTER, 2026, TZ)):
        for day in all_days(run):
            slots = S.drop_slots(run, day, TZ)
            assert slots == S.drop_slots(run, day, TZ)  # seeded: the same after a restart
            assert S.DROPS_MIN <= len(slots) <= S.DROPS_MAX
            assert [s.index for s in slots] == list(range(len(slots)))
            for a, b in zip(slots, slots[1:]):
                assert b.at - a.at >= S.DROP_GAP
            for s in slots:
                local = datetime.fromtimestamp(s.at, TZ)
                assert local.date() == day and S.DROP_FROM <= local.time() <= S.DROP_UNTIL
                assert S.DROP_COINS[0] <= s.coins <= S.DROP_COINS[1]
                assert s.line in run.event.drop_lines


def test_drop_slots_differ_by_day_and_take_an_injected_rng():
    a = S.drop_slots(RUN, date(2026, 10, 20), TZ)
    b = S.drop_slots(RUN, date(2026, 10, 21), TZ)
    assert [s.at - ts(2026, 10, 20) for s in a] != [s.at - ts(2026, 10, 21) for s in b]
    x = S.drop_slots(RUN, date(2026, 10, 20), TZ, random.Random(5))
    assert x == S.drop_slots(RUN, date(2026, 10, 20), TZ, random.Random(5))
    counts = {len(S.drop_slots(RUN, date(2026, 10, 20), TZ, random.Random(i))) for i in range(200)}
    assert counts == set(range(S.DROPS_MIN, S.DROPS_MAX + 1))


def test_drop_slots_outside_the_run_are_empty():
    assert S.drop_slots(RUN, date(2026, 11, 1), TZ) == []
    assert S.drop_slots(RUN, date(2026, 10, 19), TZ) == []


def test_due_slot_window_and_done_keys():
    day = date(2026, 10, 20)
    slots = S.drop_slots(RUN, day, TZ)
    first = slots[0]
    assert S.due_slot(slots, first.at - 1, (), RUN, day) is None
    assert S.due_slot(slots, first.at, (), RUN, day) == first
    assert S.due_slot(slots, first.at + S.SLOT_WINDOW - 1, (), RUN, day) == first
    assert S.due_slot(slots, first.at + S.SLOT_WINDOW, (), RUN, day) is None  # chat stayed quiet: skipped
    assert S.due_slot(slots, first.at, {first.job_key(RUN, day)}, RUN, day) is None
    assert first.job_key(RUN, day) == "seasonal:drop:halloween:2026:2026-10-20:0"


def test_chat_gap_expiry_and_refs():
    assert not S.chat_active(None, 1000)
    assert S.chat_active(1000, 1000 + S.CHAT_ACTIVE) and not S.chat_active(1000, 1001 + S.CHAT_ACTIVE)
    assert not S.chat_active(2000, 1000)  # a clock that went backwards
    assert S.gap_ok(None, 5) and S.gap_ok(0, S.DROP_GAP) and not S.gap_ok(0, S.DROP_GAP - 1)
    assert not S.drop_expired(0, S.DROP_EXPIRY - 1) and S.drop_expired(0, S.DROP_EXPIRY)
    assert S.DROP_EXPIRY == 600 and S.DROP_DAILY_MAX == 12 and S.DROP_COINS == (25, 75)
    assert S.drop_ref("halloween:2026", 7) == "event:halloween:2026:drop:7"
    assert S.rank_ref("halloween:2026", 1) == "event:halloween:2026:rank:1"
    assert S.quest_ref("winter:2026", date(2027, 1, 1), 5) == "event:winter:2026:quest:2027-01-01:5"
    assert S.quest_claim_key(date(2026, 10, 20)) == "quest:2026-10-20"


def test_old_enough():
    now = 10_000_000
    assert S.old_enough(now - MIN_ACCOUNT_DAYS * S.DAY, now)
    assert not S.old_enough(now - MIN_ACCOUNT_DAYS * S.DAY + 1, now)
    assert not S.old_enough(None, now)


# ---------------------------------------------------------------- quests
def test_quest_for_is_stable_and_cycles_through_the_pool():
    days = list(all_days(RUN))
    picks = [S.quest_for(RUN, d, TZ) for d in days]
    assert picks == [S.quest_for(RUN, d, TZ) for d in days]
    assert all(a != b for a, b in zip(picks, picks[1:]))
    assert set(picks[:len(S.QUESTS)]) == set(S.QUESTS)
    assert any(not q.tracking for q in S.QUESTS) and any(q.tracking for q in S.QUESTS)
    assert S.QUEST_POINTS == 3 and S.QUEST_COINS == 100


def test_quest_done_and_days_to_settle():
    voice = S.QUEST_BY_KEY["voice"]
    assert not S.quest_done(voice, 30 * 60 - 1) and S.quest_done(voice, 30 * 60)
    assert S.quest_days(RUN, ts(2026, 10, 25, 15), TZ) == [date(2026, 10, 25)]
    assert S.quest_days(RUN, ts(2026, 10, 25, 1, 30), TZ) == [date(2026, 10, 24), date(2026, 10, 25)]
    assert S.quest_days(RUN, ts(2026, 10, 25, 2), TZ) == [date(2026, 10, 25)]
    assert S.quest_days(RUN, ts(2026, 10, 20, 0, 30), TZ) == [date(2026, 10, 20)]  # no day before the start


# ---------------------------------------------------------------- points
def test_standings_points_and_tie_breaks():
    board = S.standings(drops=[(1, 4, 100), (2, 1, 50), (3, 7, 300), (None, 9, 1)],
                        quests=[(1, 1, 200), (2, 2, 90), (4, 0, 5)])
    assert [(s.user_id, s.points, s.last_at) for s in board] == [(2, 7, 90), (1, 7, 200), (3, 7, 300)]
    assert S.my_points(board, 1) == (7, 2) and S.my_points(board, 99) == (0, None)


def test_winners_and_badge_holders():
    board = S.standings([(1, 12, 1), (2, 10, 2), (3, 9, 3), (4, 8, 4), (5, 7, 5)], [])
    assert [s.user_id for s in S.winners(board, lambda u: u != 2)] == [1, 3, 4]
    assert S.winners(board, lambda u: False) == []
    assert S.badge_holders(board) == [1, 2]
    assert S.RANK_COINS == (2000, 1000, 500) and S.BADGE_POINTS == 10


# ---------------------------------------------------------------- text
def test_board_text_has_mentions_and_ten_rows_at_most():
    board = S.standings([(i, 30 - i, i) for i in range(1, 15)], [])
    text = S.board_text(board)
    assert text.count("\n") == 9 and text.startswith("🥇 <@1> · 29 points")
    assert "<@11>" not in text
    assert "Nobody" in S.board_text([])
    assert S.plural(1, "point") == "1 point" and S.plural(2, "point") == "2 points"


def test_quest_line_states():
    chat, squad = S.QUEST_BY_KEY["chat"], S.QUEST_BY_KEY["squad"]
    assert S.quest_line(chat, tracking=False, claimed=False).startswith("➖")
    assert "/privacy" in S.quest_line(chat, tracking=False, claimed=False)
    assert S.quest_line(squad, tracking=False, claimed=False).startswith("▫️")
    assert S.quest_line(squad, tracking=True, claimed=True).startswith("✅")
    assert str(MIN_ACCOUNT_DAYS) in S.quest_line(squad, tracking=True, claimed=False, old=False)


def test_announcement_texts():
    text = S.opening_text(RUN)
    assert "Halloween is on" in text and f"<t:{RUN.end}:R>" in text and "/event" in text
    assert "2,000 / 1,000 / 500" in text and "Pumpkin Hunter" in text
    assert "@everyone" not in text and "@here" not in text
    drop = S.drop_text(HALLOWEEN, HALLOWEEN.drop_lines[0])
    assert drop.startswith("🎃") and HALLOWEEN.drop_lines[0] in drop
    assert S.claimed_text(HALLOWEEN, "<@5>", 40) == "🎃 <@5> grabbed the pumpkin: **+40 coins** and 1 event point."
    assert "<@5>" in S.quest_text(HALLOWEEN, "<@5>", S.QUESTS[0])
    top = S.standings([(1, 12, 1), (2, 10, 2)], [])
    end = S.finale_text(RUN, top, badges=2, role_given=True)
    assert "🥇 <@1> · 12 points · **+2,000 coins**" in end and "🥈 <@2>" in end and "🥉" not in end
    assert "Event Champ" in end and "2 members earned" in end
    assert "Event Champ" not in S.finale_text(RUN, top, badges=0, role_given=False)
    assert "Halloween" in S.idle_text(RUN) and f"<t:{RUN.start}:R>" in S.idle_text(RUN)
    assert S.idle_text(None) == "No event is running right now."
