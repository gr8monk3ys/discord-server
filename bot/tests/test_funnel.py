"""The owner digest's "Newcomers" field (logic/funnel.py): pure rules, fixed fixtures."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from logic import funnel as F
from logic import recap as R
from logic.stats import Session

TZ = ZoneInfo("America/Los_Angeles")
HOUR, DAY = 3600, 86400
A, B, C, D, BOT = 700_001, 700_002, 700_003, 700_004, 700_009
VOICE, AFK = 77, 78


def ts(y, mo, d, h=0, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp())


FIRST = date(2026, 10, 5)  # a Monday: the digest of Monday Oct 12 covers Oct 5 .. Oct 11
W = F.windows(FIRST, TZ)
WEEK, END = ts(2026, 10, 5), ts(2026, 10, 12)
MONTH = ts(2026, 9, 14)
COHORT = ts(2026, 9, 28)


def review(joins, **kw):
    return F.review(joins, W, **kw)


def spoke(user, start, hours=1, channel=VOICE, open_=False):
    return Session(user, channel, start, None if open_ else start + hours * HOUR)


# ---------------------------------------------------------------- windows
def test_windows_are_whole_pacific_days_ending_at_the_digests_monday():
    assert W.week == (WEEK, END) and W.month == (MONTH, END) and W.cohort == (COHORT, WEEK)
    assert W.cohort_first == date(2026, 9, 28)
    assert (END - WEEK, END - MONTH) == (7 * DAY, 28 * DAY)
    # the same week the rest of the digest uses, from the job's own Monday 10:05
    start, end, first = R.week_bounds(ts(2026, 10, 12, 10, 5), TZ)
    assert (start, end) == F.windows(first, TZ).week == W.week


def test_a_week_with_a_clock_change_is_still_one_local_week():
    w = F.windows(date(2026, 10, 26), TZ)  # clocks go back on Sunday Nov 1
    assert w.week == (ts(2026, 10, 26), ts(2026, 11, 2))
    assert w.week[1] - w.week[0] == 7 * DAY + HOUR
    assert w.month == (ts(2026, 10, 5), ts(2026, 11, 2)) and w.cohort == (ts(2026, 10, 19), ts(2026, 10, 26))


def test_window_edges_are_local_midnights():
    joins = [
        F.Join(A, WEEK),  # Monday 00:00:00: the week's first second
        F.Join(B, END - 1),  # Sunday 23:59:59: its last
        F.Join(C, END),  # the next Monday 00:00:00: next week's digest
        F.Join(D, WEEK - 1),  # the Sunday before, 23:59:59: the week before's cohort
    ]
    r = review(joins)
    assert r.week.joined == 2 and r.month.joined == 3 and r.cohort.joined == 1
    edge = review([F.Join(A, MONTH), F.Join(B, MONTH - 1)])
    assert (edge.week.joined, edge.month.joined, edge.cohort.joined) == (0, 1, 0)
    # 23:30 on Sunday in Pacific is already Monday in UTC: the local day decides
    late = ts(2026, 10, 11, 23, 30)
    assert datetime.fromtimestamp(late, ZoneInfo("UTC")).date() == date(2026, 10, 12)
    assert review([F.Join(A, late)]).week.joined == 1


# ---------------------------------------------------------------- the steps
def test_each_step_counts_from_the_join_to_the_end_of_the_window():
    wed = ts(2026, 10, 7, 15)
    joins = [F.Join(A, wed), F.Join(B, wed), F.Join(C, wed)]
    r = review(
        joins,
        text_days=[(A, "2026-10-07"), (B, "2026-10-06"), (C, "2026-10-12")],  # the join day, before, after
        voice=[spoke(A, wed), spoke(B, wed - 1), spoke(C, END)],  # at the join, before it, after the week
        role_steps=[(A, wed), (B, wed - 1)],
    )
    assert r.week == F.Funnel(joined=3, left=0, trackable=3, roles=1, text=1, voice=1, active=1)
    assert r.month == r.week  # the same three people, the same steps


def test_text_and_voice_are_separate_counts_and_active_is_either():
    t = ts(2026, 10, 6, 9)
    r = review([F.Join(A, t), F.Join(B, t), F.Join(C, t), F.Join(D, t)],
               text_days=[(A, "2026-10-06"), (A, "2026-10-08"), (B, "2026-10-09")],
               voice=[spoke(A, t + HOUR), spoke(A, t + DAY), spoke(C, t + 2 * DAY)])
    assert (r.week.text, r.week.voice, r.week.active) == (2, 2, 3)


def test_the_afk_channel_is_not_joining_voice():
    t = ts(2026, 10, 6, 9)
    sessions = [spoke(A, t + HOUR, channel=AFK)]
    assert review([F.Join(A, t)], voice=sessions).week.voice == 1
    assert review([F.Join(A, t)], voice=sessions, excluded_channels={AFK}).week.voice == 0


def test_roles_count_from_a_step_recorded_after_the_join_or_a_role_held_now():
    t = ts(2026, 10, 6, 9)
    joins = [F.Join(A, t), F.Join(B, t), F.Join(C, t)]
    # A: the quest noticed. B: no row yet (the hourly sweep has not run), but has the role.
    r = review(joins, role_steps=[(A, t + 60)], roles_now={B})
    assert r.week.roles == 2
    assert review(joins, role_steps=[(A, t + 60)], roles_now={A, B}).week.roles == 2  # never twice
    # a role picked after the week ended still shows: roles are "has picked", not "picked by Sunday"
    assert review(joins, role_steps=[(C, END + HOUR)]).week.roles == 1


# ---------------------------------------------------------------- joins, leaves, rejoins
def test_a_member_who_joined_and_left_the_same_day_is_one_join_and_one_leave():
    t = ts(2026, 10, 8, 10)
    r = review([F.Join(A, t, t + 3 * HOUR)], text_days=[(A, "2026-10-08")])
    assert r.week == F.Funnel(joined=1, left=1, trackable=1, roles=0, text=1, voice=0, active=1)
    assert F.reading(r) == "The 1 member who joined this week has already left."
    assert F.digest_text(r).splitlines()[1] == (
        "This week: **1** joined (1 left since) · picked roles 0 · said something 1 · joined voice 0")


def test_leaving_after_the_window_ended_is_next_weeks_news():
    t = ts(2026, 10, 8, 10)
    assert review([F.Join(A, t, END + HOUR)]).week.left == 0
    assert review([F.Join(A, t, END)]).week.left == 0  # Monday 00:00:00 is next week
    assert review([F.Join(A, t, END - 1)]).week.left == 1


def test_a_rejoin_is_one_member_measured_from_the_first_arrival_in_the_window():
    first, again = ts(2026, 10, 5, 12), ts(2026, 10, 9, 12)
    joins = [F.Join(A, first, first + DAY), F.Join(A, again)]
    r = review(joins, text_days=[(A, "2026-10-05")], voice=[spoke(A, first + HOUR)])
    assert r.week == F.Funnel(joined=1, left=0, trackable=1, roles=0, text=1, voice=1, active=1)
    assert r.month.joined == 1
    gone = review([F.Join(A, first, first + DAY), F.Join(A, again, again + HOUR)])
    assert (gone.week.joined, gone.week.left) == (1, 1)


def test_a_rejoin_from_an_older_stay_counts_this_stay_only():
    old, again = ts(2026, 7, 1, 12), ts(2026, 10, 7, 12)
    joins = [F.Join(A, old, old + 30 * DAY), F.Join(A, again)]
    stale = dict(text_days=[(A, "2026-10-06")], voice=[spoke(A, old + DAY)], role_steps=[(A, old + HOUR)])
    r = review(joins, **stale)
    # a newcomer again, and nothing from the first stay is this stay's: the role row is never restamped
    assert r.week == F.Funnel(joined=1, left=0, trackable=1)
    assert review(joins, roles_now={A}, **stale).week.roles == 1  # holding a role now is this stay
    # in both the week and the cohort when they came, went and came back across the two
    both = review([F.Join(A, COHORT + HOUR, COHORT + DAY), F.Join(A, again)])
    assert (both.week.joined, both.cohort.joined, both.month.joined) == (1, 1, 1)
    assert both.cohort.here == 1


def test_bots_never_count():
    t = ts(2026, 10, 6, 9)
    r = review([F.Join(A, t), F.Join(BOT, t, t + HOUR)], bots={BOT}, text_days=[(BOT, "2026-10-06")],
               voice=[spoke(BOT, t + HOUR)], roles_now={BOT})
    assert r.week == F.Funnel(joined=1, left=0, trackable=1)
    assert F.digest_text(review([F.Join(BOT, t)], bots={BOT})) is None


# ---------------------------------------------------------------- tracking off
def test_a_newcomer_with_tracking_off_is_a_join_and_a_leave_and_nothing_else():
    t = ts(2026, 10, 6, 9)
    joins = [F.Join(A, t), F.Join(B, t, t + DAY)]
    r = review(joins, optouts={B}, text_days=[(A, "2026-10-06")], roles_now={A})
    assert r.week == F.Funnel(joined=2, left=1, trackable=1, roles=1, text=1, voice=0, active=1)
    assert F.digest_text(r).splitlines()[:2] == [
        "2 joined this week; of the 1 with stats on, 1 said something or joined voice.",
        "This week: **2** joined (1 left since) · of the 1 with stats on: "
        "picked roles 1 · said something 1 · joined voice 0",
    ]


def test_one_of_two_newcomers_opted_out_and_nothing_they_did_moves_the_field():
    t = ts(2026, 10, 6, 9)
    joins = [F.Join(A, t), F.Join(B, t)]
    quiet = review(joins, optouts={B})
    # rows the bot should not hold for B at all, and the role step it does hold
    loud = review(joins, optouts={B}, text_days=[(B, "2026-10-06"), (B, "2026-10-09")],
                  voice=[spoke(B, t + HOUR), spoke(B, t + DAY, open_=True)], role_steps=[(B, t + 60)], roles_now={B})
    assert loud == quiet
    assert F.digest_text(loud) == F.digest_text(quiet) == "\n".join([
        "2 joined this week; of the 1 with stats on, none said anything or joined voice.",
        "This week: **2** joined (0 left since) · of the 1 with stats on: "
        "picked roles 0 · said something 0 · joined voice 0",
        "Last 28 days: **2** joined (0 left since) · of the 1 with stats on: "
        "picked roles 0 · said something 0 · joined voice 0",
    ])
    # and the same in the retention line a week later
    w = F.windows(date(2026, 10, 12), TZ)
    later = dict(text_days=[(B, "2026-10-13")], voice=[spoke(B, ts(2026, 10, 14, 20))])
    assert F.review(joins, w, optouts={B}, **later) == F.review(joins, w, optouts={B})
    assert F.review(joins, w, optouts={B}, **later).cohort == F.Retention(joined=2, here=2, trackable=1, back=0)


def test_when_every_newcomer_has_tracking_off_there_are_no_activity_numbers():
    t = ts(2026, 10, 6, 9)
    r = review([F.Join(A, t), F.Join(B, COHORT + HOUR)], optouts={A, B}, text_days=[(A, "2026-10-06")])
    assert F.reading(r) is None
    assert F.digest_text(r).splitlines() == [
        "This week: **1** joined (0 left since) · no activity numbers (stats are off)",
        "Last 28 days: **2** joined (0 left since) · no activity numbers (stats are off)",
        "Joined Sep 28 to Oct 4: **1** · still here 1 · no activity numbers (stats are off)",
    ]


# ---------------------------------------------------------------- the week before: who came back
def test_the_cohort_is_the_week_before_and_week_2_is_the_digests_week():
    mon, sun = ts(2026, 9, 28, 8), ts(2026, 10, 4, 22)
    joins = [
        F.Join(A, mon),  # wrote in week 2
        F.Join(B, mon),  # was in voice in week 2
        F.Join(C, sun),  # active the night they joined, never again
        F.Join(D, mon, ts(2026, 10, 8, 12)),  # came back on Tuesday, left on Thursday
        F.Join(700_005, ts(2026, 9, 27, 23)),  # joined a week earlier: not this cohort
        F.Join(700_006, WEEK + HOUR),  # joined this week: next Monday's cohort
    ]
    r = review(joins,
               text_days=[(A, "2026-10-05"), (C, "2026-10-04"), (D, "2026-10-06"), (700_005, "2026-10-07")],
               voice=[spoke(B, ts(2026, 10, 10, 20)), spoke(C, sun + HOUR)])
    assert r.cohort == F.Retention(joined=4, here=3, trackable=4, back=3)
    assert F.digest_text(r).splitlines()[-1] == (
        "Joined Sep 28 to Oct 4: **4** · still here 3 · came back in week 2 (a message or voice): 3")


def test_week_2_voice_is_any_session_that_overlaps_the_week():
    mon = ts(2026, 9, 28, 8)
    joins = [F.Join(A, mon)]
    over = Session(A, VOICE, WEEK - HOUR, WEEK + HOUR)  # Sunday night into Monday
    assert review(joins, voice=[over]).cohort.back == 1
    assert review(joins, voice=[Session(A, VOICE, WEEK - 2 * HOUR, WEEK)]).cohort.back == 0  # ended at midnight
    assert review(joins, voice=[Session(A, VOICE, END, END + HOUR)]).cohort.back == 0  # week 3
    assert review(joins, voice=[Session(A, VOICE, END - HOUR, None)]).cohort.back == 1  # still in there
    assert review(joins, text_days=[(A, "2026-10-11")]).cohort.back == 1  # Sunday, the last day
    assert review(joins, text_days=[(A, "2026-10-12")]).cohort.back == 0


def test_no_retention_line_without_a_cohort():
    r = review([F.Join(A, ts(2026, 10, 6, 9))])
    assert r.cohort == F.Retention() and "Joined Sep" not in F.digest_text(r)


# ---------------------------------------------------------------- the sentence
def one_week(**kw):
    f = F.Funnel(**kw)
    return F.Review(week=f, month=f, cohort=F.Retention(), cohort_first=date(2026, 9, 28))


@pytest.mark.parametrize("week, sentence", [
    (dict(), "No joins this week: the listings are not bringing anyone in."),
    (dict(joined=1, left=1, trackable=1, active=1), "The 1 member who joined this week has already left."),
    (dict(joined=3, left=3, trackable=2), "All 3 who joined this week have already left."),
    (dict(joined=2, left=1, trackable=0), None),
    (dict(joined=1, trackable=1), "1 joined this week and has not said anything or joined voice."),
    (dict(joined=3, left=1, trackable=3), "3 joined this week and none of them said anything or joined voice."),
    (dict(joined=3, trackable=2), "3 joined this week; of the 2 with stats on, none said anything or joined voice."),
    (dict(joined=3, trackable=2, active=1), "3 joined this week; of the 2 with stats on, 1 said something or joined voice."),
    (dict(joined=3, trackable=3, active=2), "2 of the 3 who joined this week said something or joined voice."),
    (dict(joined=3, left=2, trackable=3, active=3), "All 3 who joined this week said something or joined voice."),
    (dict(joined=1, trackable=1, active=1), "1 joined this week and said something or joined voice."),
])
def test_the_sentence_follows_the_rules(week, sentence):
    assert F.reading(one_week(**week)) == sentence


def test_every_sentence_is_one_plain_sentence():
    for week in (dict(), dict(joined=1, left=1, trackable=1), dict(joined=4, trackable=4),
                 dict(joined=4, trackable=3, active=2), dict(joined=4, trackable=4, active=4)):
        s = F.reading(one_week(**week))
        assert s.endswith(".") and s.count(".") == 1 and "\n" not in s and s.isascii()


# ---------------------------------------------------------------- the field
def test_no_joins_in_28_days_is_no_field():
    assert F.digest_text(review([])) is None
    assert F.digest_text(review([F.Join(A, MONTH - 1), F.Join(B, END)])) is None  # too old, and too new


def test_a_week_with_no_joins_says_so_and_keeps_the_28_day_line():
    r = review([F.Join(A, ts(2026, 9, 20, 12)), F.Join(B, ts(2026, 9, 21, 12), ts(2026, 9, 22, 12))],
               text_days=[(A, "2026-09-20")], roles_now={A})
    assert F.digest_text(r).splitlines() == [
        "No joins this week: the listings are not bringing anyone in.",
        "Last 28 days: **2** joined (1 left since) · picked roles 1 · said something 1 · joined voice 0",
    ]


def test_the_whole_field_is_counts_and_never_a_member():
    tue = ts(2026, 10, 6, 18)
    joins = [
        F.Join(A, tue), F.Join(B, tue + DAY), F.Join(C, tue + 2 * DAY, tue + 3 * DAY),  # this week
        F.Join(D, ts(2026, 9, 29, 12)), F.Join(700_005, ts(2026, 9, 30, 12), ts(2026, 10, 2, 12)),  # the week before
        F.Join(700_006, ts(2026, 9, 16, 12)),  # three weeks back
    ]
    r = review(joins,
               text_days=[(A, "2026-10-06"), (D, "2026-10-07"), (700_006, "2026-09-16")],
               voice=[spoke(B, tue + DAY + HOUR), spoke(700_006, ts(2026, 9, 17, 20))],
               role_steps=[(A, tue + 60), (D, ts(2026, 9, 29, 13)), (700_006, ts(2026, 9, 16, 13))], roles_now={B})
    text = F.digest_text(r)
    assert text.splitlines() == [
        "2 of the 3 who joined this week said something or joined voice.",
        "This week: **3** joined (1 left since) · picked roles 2 · said something 1 · joined voice 1",
        "Last 28 days: **6** joined (2 left since) · picked roles 4 · said something 3 · joined voice 2",
        "Joined Sep 28 to Oct 4: **2** · still here 1 · came back in week 2 (a message or voice): 1",
    ]
    assert "700" not in text and "<@" not in text and len(text) < 1024
    assert F.FIELD == "Newcomers"
