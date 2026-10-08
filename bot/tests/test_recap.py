"""Pure rules for the weekly recap, owner digest, milestones and invite contest."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from logic import recap as R
from logic.growth import STAY_SECONDS, Join

TZ = ZoneInfo("America/Los_Angeles")
DAY = 86400


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


# ---------------------------------------------------------------- jobs
def test_jobs_run_monday_morning():
    assert (R.RECAP_JOB.weekday, R.RECAP_JOB.hour, R.RECAP_JOB.minute) == (0, 10, 0)
    assert (R.DIGEST_JOB.weekday, R.DIGEST_JOB.hour, R.DIGEST_JOB.minute) == (0, 10, 5)
    assert (R.CONTEST_JOB.day, R.CONTEST_JOB.hour, R.CONTEST_JOB.minute) == (1, 12, 0)


# ---------------------------------------------------------------- week bounds
def test_week_bounds_is_previous_monday_to_sunday():
    monday = ts(2026, 10, 12, 10, 0)  # a Monday
    start, end, first = R.week_bounds(monday, TZ)
    assert first == date(2026, 10, 5)
    assert start == ts(2026, 10, 5) and end == ts(2026, 10, 12)


def test_week_bounds_across_dst_is_local_midnights():
    monday = ts(2026, 11, 2, 10, 0)  # DST ended Sunday Nov 1
    start, end, first = R.week_bounds(monday, TZ)
    assert first == date(2026, 10, 26)
    assert end - start == 7 * DAY + 3600


def test_day_keys_are_seven_iso_days():
    assert R.day_keys(date(2026, 10, 5)) == [f"2026-10-{d:02}" for d in range(5, 12)]


def test_week_label():
    assert R.week_label(date(2026, 10, 5)) == "Oct 5 – Oct 11"
    assert R.week_label(date(2026, 12, 28)) == "Dec 28 – Jan 3"


# ---------------------------------------------------------------- monthly plan
JOB = R.Monthly("invitecontest", day=1, hour=12, minute=0)


def test_monthly_first_run_only_marks_done():
    now = ts(2026, 10, 7, 9)
    p = R.plan_monthly(JOB, now, TZ, set(), None)
    assert p.run is None
    assert [x.key for x in p.mark_done] == ["invitecontest:2026-09"]


def test_monthly_runs_latest_due_once():
    first_seen = ts(2026, 9, 20)
    now = ts(2026, 10, 1, 12, 1)
    p = R.plan_monthly(JOB, now, TZ, {"invitecontest:2026-08"}, first_seen)
    assert p.run.key == "invitecontest:2026-09"
    assert p.run.month == "2026-09"
    assert p.run.window_start == ts(2026, 9, 1) and p.run.window_end == ts(2026, 10, 1)
    assert p.run.scheduled_at == ts(2026, 10, 1, 12)
    assert p.mark_done == []
    assert R.plan_monthly(JOB, now, TZ, {"invitecontest:2026-09"}, first_seen) == R.MonthPlan(None, [])


def test_monthly_before_noon_on_the_first_is_last_months_run():
    first_seen = ts(2026, 8, 1)
    now = ts(2026, 10, 1, 11, 59)
    p = R.plan_monthly(JOB, now, TZ, {"invitecontest:2026-07"}, first_seen)
    assert p.run.key == "invitecontest:2026-08"


def test_monthly_catch_up_marks_missed_and_runs_latest():
    first_seen = ts(2026, 6, 15)
    now = ts(2026, 10, 3)
    p = R.plan_monthly(JOB, now, TZ, {"invitecontest:2026-07"}, first_seen)
    assert p.run.key == "invitecontest:2026-09"
    assert [x.key for x in p.mark_done] == ["invitecontest:2026-06", "invitecontest:2026-08"]


def test_monthly_latest_before_first_seen_is_marked_done():
    first_seen = ts(2026, 10, 2)
    p = R.plan_monthly(JOB, ts(2026, 10, 5), TZ, set(), first_seen)
    assert p.run is None and [x.key for x in p.mark_done] == ["invitecontest:2026-09"]


def test_monthly_january_judges_december():
    first_seen = ts(2026, 12, 1)
    p = R.plan_monthly(JOB, ts(2027, 1, 1, 13), TZ, {"invitecontest:2026-11"}, first_seen)
    assert p.run.key == "invitecontest:2026-12"


# ---------------------------------------------------------------- rankings
def test_top_orders_by_score_then_id_and_skips_excluded_and_zero():
    scores = {5: 10, 3: 10, 9: 50, 7: 0, 8: 30}
    assert R.top(scores) == [(9, 50), (8, 30), (3, 10)]
    assert R.top(scores, exclude={9}) == [(8, 30), (3, 10), (5, 10)]
    assert R.top({}) == []


def test_squads_formed_counts_posts_that_filled_in_the_window():
    start, end = 1000, 2000
    posts = [
        (2, [900, 1500]),  # filled at 1500: counts
        (3, [900, 950, 999]),  # filled before: no
        (2, [1500]),  # never filled
        (2, [1900, 2100]),  # filled after
        (2, [1000, 1000, 1999]),  # filled at 1000 (start is inclusive)
    ]
    assert R.squads_formed(posts, start, end) == 2


def test_level_ups_compare_snapshots():
    before = {1: 3, 2: 5, 3: 9}
    after = {1: 6, 2: 5, 3: 10, 4: 2, 5: 0}
    ups = R.level_ups(before, after)
    assert ups == [(1, 3, 6), (4, 0, 2), (3, 9, 10)]
    assert R.level_ups(before, after, exclude={1}) == [(4, 0, 2), (3, 9, 10)]


# ---------------------------------------------------------------- recap embed content
def test_empty_recap_has_no_sections():
    assert R.recap_sections(R.Recap()) == []
    assert R.Recap().empty


def test_recap_sections_skip_empty_ones():
    r = R.Recap(joined=4, left=1, messages=1234, voice_seconds=5 * 3600 + 1800,
                chatters=[(1, 500), (2, 300)], voice=[(3, 7200)], badges=6)
    sections = dict(R.recap_sections(r))
    assert sections["Members"] == "**4** joined, **1** left (net **+3**)"
    assert sections["Chat"].startswith("**1,234** messages")
    assert "<@1> 500" in sections["Chat"] and "<@2> 300" in sections["Chat"]
    assert sections["Voice"].startswith("**5.5** hours")
    assert "<@3> 2h 0m" in sections["Voice"]
    assert sections["Badges"] == "**6** new badges earned"
    for missing in ("Squads", "Game nights", "Hall of fame", "Clip of the week", "Tournaments", "Level-ups"):
        assert missing not in sections
    assert not r.empty


def test_recap_sections_highlights():
    r = R.Recap(squads=3, gamenights=1, hall=2, clip_winner=7, champions=[(8, "Friday *Cup*")],
                levelups=[(9, 4, 6)])
    sections = dict(R.recap_sections(r))
    assert sections["Squads"] == "**3** squads formed in LFG"
    assert sections["Game nights"] == "**1** game night held"
    assert sections["Hall of fame"] == "**2** posts made the hall of fame"
    assert sections["Clip of the week"] == "<@7>"
    assert sections["Tournaments"] == "<@8> won **Friday \\*Cup\\***"
    assert sections["Level-ups"] == "<@9> reached level **6**"


def test_net_growth_can_be_negative_or_only_leaves():
    assert dict(R.recap_sections(R.Recap(left=2)))["Members"] == "**0** joined, **2** left (net **-2**)"


def test_fmt_hours():
    assert R.fmt_hours(0) == "0"
    assert R.fmt_hours(3600) == "1"
    assert R.fmt_hours(5400) == "1.5"
    assert R.fmt_hours(100 * 3600 + 100) == "100"


# ---------------------------------------------------------------- milestones
def test_milestones_first_check_marks_reached_silently():
    assert R.milestones_due(60, set(), first=True) == (None, [25, 50])


def test_milestones_post_highest_new_and_mark_all_reached():
    assert R.milestones_due(24, set(), first=False) == (None, [])
    assert R.milestones_due(25, set(), first=False) == (25, [25])
    assert R.milestones_due(105, {25}, first=False) == (100, [50, 100])
    assert R.milestones_due(40, {25, 50}, first=False) == (None, [])
    assert R.milestones_due(5000, set(R.MILESTONES), first=False) == (None, [])


def test_milestone_text_has_the_number():
    assert "100" in R.milestone_text(100)
    assert "1,000" in R.milestone_text(1000)


# ---------------------------------------------------------------- invite contest
START, END = ts(2026, 9, 1), ts(2026, 10, 1)
NOW = ts(2026, 10, 1, 12)


def j(user, inviter, joined, left=None):
    return Join(user, inviter, joined, left)


def test_contest_counts_distinct_invitees_who_stayed_and_are_still_here():
    joins = [
        j(1, 100, START + DAY),
        j(2, 100, START + 2 * DAY),
        j(2, 100, START + 3 * DAY),  # rejoin: still one person
        j(3, 100, START + DAY, left=START + DAY + 60),  # left quickly
        j(4, 200, START + DAY),
        j(5, 200, START - DAY),  # joined last month
        j(6, 300, END - DAY),  # hasn't stayed 3 days yet at the contest
        j(7, 7, START + DAY),  # invited themselves
        j(8, None, START + DAY),  # unknown inviter
        j(9, 400, START + DAY, left=START + 10 * DAY),  # stayed, but has since left
    ]
    assert R.contest_ranking(joins, START, END, NOW) == [(1, 100, 2), (2, 200, 1)]


def test_contest_later_rejoin_after_leaving_counts_latest_row():
    joins = [j(1, 100, START + DAY, left=START + 5 * DAY), j(1, 100, START + 6 * DAY)]
    assert R.contest_ranking(joins, START, END, NOW) == [(1, 100, 1)]
    joins = [j(1, 100, START + DAY), j(1, 100, START - 40 * DAY, left=START - 30 * DAY)]
    assert R.contest_ranking(joins, START, END, NOW) == [(1, 100, 1)]


def test_contest_ties_go_to_who_got_there_first_and_top_three_only():
    joins = [
        j(1, 100, START + 5 * DAY), j(2, 200, START + 2 * DAY), j(3, 300, START + 3 * DAY),
        j(4, 400, START + 1 * DAY), j(5, 500, START + 1 * DAY),
    ]
    assert R.contest_ranking(joins, START, END, NOW) == [(1, 400, 1), (2, 500, 1), (3, 200, 1)]


def test_contest_excludes_given_inviters():
    joins = [j(1, 100, START + DAY), j(2, 200, START + DAY)]
    assert R.contest_ranking(joins, START, END, NOW, exclude={100}) == [(1, 200, 1)]
    assert R.contest_ranking([], START, END, NOW) == []


def test_contest_stay_rule_matches_growth():
    edge = END - STAY_SECONDS
    assert R.contest_ranking([j(1, 100, NOW - STAY_SECONDS)], START, END + DAY, NOW) == [(1, 100, 1)]
    assert R.contest_ranking([j(1, 100, edge + 12 * 3600 + 1)], START, END, NOW) == []


def test_contest_ref_and_prizes():
    assert R.contest_ref("2026-09", 1) == "invitecontest:2026-09:1"
    assert R.CONTEST_PRIZES == (1500, 750, 300)


def test_contest_text_mentions_winners_and_prizes():
    text = R.contest_text("2026-09", [(1, 100, 4), (2, 200, 1)])
    assert "September 2026" in text
    assert "<@100>" in text and "4 invites" in text and "1,500" in text
    assert "<@200>" in text and "1 invite " in text and "750" in text


# ---------------------------------------------------------------- owner digest
def test_member_trend_walks_back_from_now():
    day_ends = [100, 200, 300]
    rows = [(150, None), (250, 280), (350, None), (50, 120)]
    # count at T = now - joins after T + leaves after T
    assert R.member_trend(10, rows, day_ends) == [9, 9, 9]


def test_member_trend_simple():
    # one join at 250, nothing else: count was one lower before it
    assert R.member_trend(5, [(250, None)], [100, 200, 300]) == [4, 4, 5]


def test_digest_nothing_needs_you():
    quiet = R.Digest(member_count=40)
    assert not R.needs_you(quiet)
    fields = dict(R.digest_fields(quiet, date(2026, 10, 5)))
    assert fields["Needs you"] == "Nothing needs you this week."
    for busy in (R.Digest(open_tickets=1), R.Digest(open_reports=2), R.Digest(pending_partners=1),
                 R.Digest(open_suggestions=3), R.Digest(errors=4)):
        assert R.needs_you(busy)


def test_digest_fields():
    d = R.Digest(member_count=45, joined=6, left=1, trend=[40, 41, 41, 43, 44, 44, 45],
                 open_tickets=2, open_reports=0, open_suggestions=None,
                 cases={"warn": 3, "timeout": 1}, errors=None, pending_partners=1)
    fields = dict(R.digest_fields(d, date(2026, 10, 5)))
    assert fields["Members"].startswith("**45** now · **6** joined, **1** left (net **+5**)")
    assert "Mon 40 · Tue 41 · Wed 41 · Thu 43 · Fri 44 · Sat 44 · Sun 45" in fields["Members"]
    assert fields["Mod actions"] == "**4** this week: 1 timeout, 3 warn"
    assert "Errors" not in fields
    needs = fields["Needs you"]
    assert "2 open tickets" in needs and "1 partner application" in needs
    assert "suggestion" not in needs and "report" not in needs


def test_digest_fields_quiet_mod_and_errors():
    fields = dict(R.digest_fields(R.Digest(errors=0, open_suggestions=0), date(2026, 10, 5)))
    assert fields["Mod actions"] == "None this week."
    assert fields["Errors"] == "None since the bot started."
    fields = dict(R.digest_fields(R.Digest(errors=7), date(2026, 10, 5)))
    assert fields["Errors"] == "**7** since the bot started (see /status)."


@pytest.mark.parametrize("n,word,expected", [(1, "ticket", "1 ticket"), (2, "ticket", "2 tickets")])
def test_plural(n, word, expected):
    assert R.plural(n, word) == expected
