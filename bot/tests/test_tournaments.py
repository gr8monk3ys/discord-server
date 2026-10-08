"""Pure tournament rules: seeding, brackets with byes, advancement, the report /
confirm flow and the text bracket."""

import random

import pytest

from logic import tournaments as T
from logic.tournaments import Bracket, Match, Report


# ---------------------------------------------------------------- sizes and seeding
@pytest.mark.parametrize("n,size", [(2, 2), (3, 4), (4, 4), (5, 8), (8, 8), (9, 16), (16, 16), (17, 32), (32, 32)])
def test_bracket_size_is_next_power_of_two(n, size):
    assert T.bracket_size(n) == size


def test_bracket_size_needs_two():
    with pytest.raises(ValueError):
        T.bracket_size(1)


def test_rounds():
    assert [T.rounds(s) for s in (2, 4, 8, 16, 32)] == [1, 2, 3, 4, 5]


def test_seed_positions_standard_order():
    assert T.seed_positions(2) == [1, 2]
    assert T.seed_positions(4) == [1, 4, 2, 3]
    assert T.seed_positions(8) == [1, 8, 4, 5, 2, 7, 3, 6]
    for size in (16, 32):
        pos = T.seed_positions(size)
        assert sorted(pos) == list(range(1, size + 1))
        # every first-round pair sums to size + 1
        assert all(pos[i] + pos[i + 1] == size + 1 for i in range(0, size, 2))


def test_seed_shuffles_with_injected_rng_and_does_not_mutate():
    entrants = list(range(100, 110))
    a = T.seed(entrants, random.Random(7))
    b = T.seed(entrants, random.Random(7))
    assert a == b and sorted(a) == entrants and entrants == list(range(100, 110))
    assert T.seed(entrants, random.Random(8)) != a


def test_valid_sizes():
    assert T.SIZES == (4, 8, 16, 32)


# ---------------------------------------------------------------- bracket building
@pytest.mark.parametrize("n", range(2, 33))
def test_build_bracket_byes_for_every_count(n):
    seeded = list(range(1, n + 1))
    b = Bracket.build(seeded)
    size = T.bracket_size(n)
    first = b.round(1)
    assert len(first) == size // 2
    assert len(b.matches) == size - 1
    assert b.final_round == T.rounds(size)
    byes = [m for m in first if m.status == T.BYE]
    assert len(byes) == size - n
    # no match is two byes, and byes go to the top seeds
    assert all(m.p1 is not None or m.p2 is not None for m in first)
    assert sorted(m.winner for m in byes) == list(range(1, size - n + 1))
    # everyone appears exactly once in round 1
    players = [p for m in first for p in (m.p1, m.p2) if p is not None]
    assert sorted(players) == seeded
    # bye winners already sit in round 2
    if b.final_round > 1:
        second = [p for m in b.round(2) for p in (m.p1, m.p2) if p is not None]
        assert sorted(second) == sorted(m.winner for m in byes)
    for m in first:
        assert m.status in (T.BYE, T.OPEN)
        assert (m.status == T.OPEN) == (m.p1 is not None and m.p2 is not None)
    # a round-2 match is open exactly when both of its feeders were byes
    for m in b.round(2) if b.final_round > 1 else []:
        assert (m.status == T.OPEN) == (m.p1 is not None and m.p2 is not None)


def test_build_three_entrants():
    b = Bracket.build([10, 20, 30])
    m1, m2 = b.round(1)
    assert (m1.p1, m1.p2, m1.status, m1.winner) == (10, None, T.BYE, 10)
    assert (m2.p1, m2.p2, m2.status) == (20, 30, T.OPEN)  # seeds 2 and 3
    final = b.get(2, 0)
    assert (final.p1, final.p2, final.status) == (10, None, T.PENDING)


def test_build_needs_two_and_unique():
    with pytest.raises(ValueError):
        Bracket.build([1])
    with pytest.raises(ValueError):
        Bracket.build([1, 1, 2])


def test_open_matches_lists_playable():
    b = Bracket.build(list(range(1, 6)))  # 5 of 8: 3 byes, 1 real match in round 1
    assert [(m.round, m.slot) for m in b.open_matches()] == [(1, 1), (2, 1)]  # seeds 2 and 3 both had byes


# ---------------------------------------------------------------- advancement
def play_out(b, pick_lower=True):
    """Decide every open match until there's a champion."""
    while b.champion() is None:
        for m in b.open_matches():
            winner = min(m.p1, m.p2) if pick_lower else max(m.p1, m.p2)
            b.decide(m.round, m.slot, winner)
    return b


@pytest.mark.parametrize("n", [2, 3, 4, 5, 7, 8, 12, 16, 17, 31, 32])
def test_full_tournament_reaches_one_champion(n):
    b = play_out(Bracket.build(list(range(1, n + 1))))
    assert b.champion() == 1
    final = b.get(b.final_round, 0)
    assert b.runner_up() in (final.p1, final.p2) and b.runner_up() != 1
    assert all(m.status in (T.DONE, T.BYE) for m in b.matches)


def test_decide_moves_winner_to_right_side_of_next_match():
    b = Bracket.build([1, 2, 3, 4])  # positions 1v4, 2v3
    changed = b.decide(1, 0, 4)
    assert [(m.round, m.slot) for m in changed] == [(1, 0), (2, 0)]
    assert b.get(2, 0).p1 == 4 and b.get(2, 0).status == T.PENDING
    b.decide(1, 1, 3)
    final = b.get(2, 0)
    assert (final.p1, final.p2, final.status) == (4, 3, T.OPEN)
    assert b.champion() is None and b.runner_up() is None
    b.decide(2, 0, 3)
    assert b.champion() == 3 and b.runner_up() == 4


def test_decide_rejects_non_player_and_finished():
    b = Bracket.build([1, 2, 3, 4])
    with pytest.raises(ValueError):
        b.decide(1, 0, 2)
    b.decide(1, 0, 1)
    with pytest.raises(ValueError):
        b.decide(1, 0, 4)
    with pytest.raises(ValueError):
        b.decide(2, 0, 1)  # final not open yet


def test_from_rows_round_trip():
    b = Bracket.build([5, 6, 7])
    again = Bracket([Match(**vars(m)) for m in b.matches])
    assert [vars(m) for m in again.matches] == [vars(m) for m in b.matches]


# ---------------------------------------------------------------- report / confirm
def open_match(p1=1, p2=2):
    return Match(round=1, slot=0, p1=p1, p2=p2, status=T.OPEN)


def test_first_report_waits_for_the_other_player():
    outcome, m = T.report(open_match(), user_id=1, pick=1, staff=False)
    assert outcome is Report.RECORDED
    assert (m.status, m.winner, m.reported_by) == (T.REPORTED, 1, 1)


def test_other_player_confirms_same_pick():
    _, m = T.report(open_match(), 1, 2, False)  # P1 honestly says P2 won
    outcome, m = T.report(m, 2, 2, False)
    assert outcome is Report.CONFIRMED and (m.status, m.winner) == (T.DONE, 2)


def test_same_player_can_change_their_report():
    _, m = T.report(open_match(), 1, 1, False)
    outcome, m = T.report(m, 1, 2, False)
    assert outcome is Report.RECORDED and (m.status, m.winner, m.reported_by) == (T.REPORTED, 2, 1)


def test_conflicting_report_flags():
    _, m = T.report(open_match(), 1, 1, False)
    outcome, m = T.report(m, 2, 2, False)
    assert outcome is Report.CONFLICT
    assert (m.status, m.winner, m.reported_by) == (T.CONFLICT, 1, 1)  # first claim kept
    outcome, m2 = T.report(m, 1, 1, False)
    assert outcome is Report.DISPUTED and m2 == m  # players are locked out until staff decide


def test_staff_report_is_final_from_any_state():
    for start in (open_match(),
                  T.report(open_match(), 1, 1, False)[1],
                  T.report(T.report(open_match(), 1, 1, False)[1], 2, 2, False)[1]):
        outcome, m = T.report(start, 99, 2, True)
        assert outcome is Report.STAFF and (m.status, m.winner, m.reported_by) == (T.DONE, 2, 99)


def test_staff_who_plays_is_treated_as_a_player():
    outcome, m = T.report(open_match(), 1, 1, True)
    assert outcome is Report.RECORDED and m.status == T.REPORTED


def test_outsiders_and_closed_matches():
    assert T.report(open_match(), 3, 1, False)[0] is Report.NOT_PLAYER
    pending = Match(round=2, slot=0, p1=1, p2=None, status=T.PENDING)
    assert T.report(pending, 1, 1, False)[0] is Report.NOT_OPEN
    assert T.report(pending, 99, 1, True)[0] is Report.NOT_OPEN
    done = Match(round=1, slot=0, p1=1, p2=2, winner=1, status=T.DONE)
    assert T.report(done, 2, 2, False)[0] is Report.DONE
    assert T.report(done, 99, 2, True)[0] is Report.DONE
    bye = Match(round=1, slot=0, p1=1, p2=None, winner=1, status=T.BYE)
    assert T.report(bye, 1, 1, False)[0] is Report.DONE


def test_report_does_not_mutate_input():
    m = open_match()
    T.report(m, 1, 1, False)
    assert m.status == T.OPEN and m.winner is None


def test_bad_pick():
    with pytest.raises(ValueError):
        T.report(open_match(), 1, 3, False)


# ---------------------------------------------------------------- payouts
def test_payout_refs_and_amounts():
    assert T.payouts(7, champion=1, runner_up=2) == [
        (1, 1000, "tourney:7:first"), (2, 400, "tourney:7:second")]
    assert T.payouts(7, champion=1, runner_up=None) == [(1, 1000, "tourney:7:first")]


# ---------------------------------------------------------------- text
def test_round_names():
    assert T.round_name(3, 3) == "Final"
    assert T.round_name(2, 3) == "Semifinals"
    assert T.round_name(1, 3) == "Quarterfinals"
    assert T.round_name(1, 5) == "Round 1"


def test_clean_name_strips_code_breakers_and_truncates():
    assert T.clean_name("a`b`c") == "abc"
    assert T.clean_name("x" * 40) == "x" * (T.NAME_WIDTH - 1) + "…"
    assert T.clean_name("   ") == "?"
    assert T.clean_name("line\nbreak") == "line break"


def test_render_bracket_shows_states():
    b = Bracket.build([1, 2, 3])
    names = {1: "alice", 2: "bob", 3: "carol"}.get
    text = T.render_bracket(b, names)
    assert text.startswith("```") and text.endswith("```")
    assert "Semifinals" in text and "Final" in text
    assert "(bye)" in text and "TBD" in text
    b.decide(1, 1, 2)
    text = T.render_bracket(b, names)
    assert "> bob" in text
    b.decide(2, 0, 2)
    assert "Champion: bob" in T.render_bracket(b, names)


def test_render_bracket_fits_an_embed_at_32():
    b = play_out(Bracket.build(list(range(1, 33))))
    text = T.render_bracket(b, lambda uid: "W" * 40)
    assert len(text) <= 4096


def test_render_marks_reported_and_disputed():
    b = Bracket.build([1, 2])
    m = b.get(1, 0)
    m.status, m.winner, m.reported_by = T.REPORTED, 1, 1
    assert "awaiting confirmation" in T.render_bracket(b, str)
    m.status = T.CONFLICT
    assert "disputed" in T.render_bracket(b, str)


def test_render_unknown_name_falls_back():
    b = Bracket.build([1, 2])
    assert "?" in T.render_bracket(b, lambda uid: None)


# ---------------------------------------------------------------- the monthly auto tournament
from datetime import date, datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import config  # noqa: E402
from logic.quests import MIN_ACCOUNT_DAYS  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
HOUR, DAY = 3600, 86400


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


@pytest.mark.parametrize("y,m,d", [(2026, 10, 5), (2026, 11, 2), (2026, 12, 7), (2027, 2, 1), (2026, 6, 1)])
def test_first_monday(y, m, d):
    assert T.first_monday(y, m) == date(y, m, d)


def test_monthly_occurrence_key_time_and_window():
    p = T.monthly_occurrence(T.AUTO_JOB, 2026, 10, TZ)
    assert p.key == "autotourney:2026-10"
    assert p.scheduled_at == ts(2026, 10, 5, 12)
    assert p.window_end == p.scheduled_at
    assert p.window_start == ts(2026, 9, 5, 12)  # 30 local days back


def test_latest_monthly_before_and_after_the_first_monday():
    assert T.latest_monthly(T.AUTO_JOB, ts(2026, 10, 5, 11, 59), TZ).key == "autotourney:2026-09"
    assert T.latest_monthly(T.AUTO_JOB, ts(2026, 10, 5, 12), TZ).key == "autotourney:2026-10"
    assert T.latest_monthly(T.AUTO_JOB, ts(2026, 10, 31, 23), TZ).key == "autotourney:2026-10"
    assert T.latest_monthly(T.AUTO_JOB, ts(2027, 1, 1), TZ).key == "autotourney:2026-12"


def test_plan_monthly_first_run_only_marks_done():
    p = T.plan_monthly(T.AUTO_JOB, ts(2026, 10, 7), TZ, set(), None)
    assert p.run is None and [x.key for x in p.mark_done] == ["autotourney:2026-10"]


def test_plan_monthly_period_before_first_seen_is_marked_not_run():
    p = T.plan_monthly(T.AUTO_JOB, ts(2026, 10, 7), TZ, set(), ts(2026, 10, 6))
    assert p.run is None and [x.key for x in p.mark_done] == ["autotourney:2026-10"]


def test_plan_monthly_runs_latest_and_marks_missed():
    first_seen = ts(2026, 8, 20)
    p = T.plan_monthly(T.AUTO_JOB, ts(2026, 11, 3), TZ, set(), first_seen)
    assert p.run.key == "autotourney:2026-11"
    assert [x.key for x in p.mark_done] == ["autotourney:2026-09", "autotourney:2026-10"]


def test_plan_monthly_done_is_nothing():
    p = T.plan_monthly(T.AUTO_JOB, ts(2026, 11, 3), TZ, {"autotourney:2026-11"}, ts(2026, 1, 1))
    assert p.run is None and p.mark_done == []


def test_auto_start_is_the_following_saturday_evening():
    assert T.auto_starts_at(date(2026, 10, 5), TZ) == ts(2026, 10, 10, 19)
    assert T.auto_starts_at(date(2026, 11, 2), TZ) == ts(2026, 11, 7, 19)  # across the DST change


def test_auto_game_prefers_the_top_game_else_rotates():
    games = config.GAMES
    top = games[2]
    assert T.auto_game(top, 2026, 10, games) is top
    picks = [T.auto_game(None, 2026, m, games) for m in range(1, len(games) + 1)]
    assert len({g.key for g in picks}) == len(games)  # every game once a cycle
    assert T.auto_game(None, 2027, 1, games) is T.auto_game(None, 2027, 1, games)  # deterministic
    assert T.auto_game(None, 2026, 12, games) is not T.auto_game(None, 2027, 1, games)  # keeps rotating


def test_auto_name_fits_the_column():
    name = T.auto_name(config.GAMES[0], date(2026, 10, 5))
    assert "October 2026" in name and config.GAMES[0].role in name and len(name) <= 60
    assert T.auto_name(None, date(2026, 10, 5)) == "Monthly Cup · October 2026"


def test_too_late_to_create():
    start = ts(2026, 10, 10, 19)
    assert not T.too_late_to_create(start - 2 * DAY, start)
    assert T.too_late_to_create(start - T.CREATE_MARGIN + 1, start)


def test_can_join_needs_an_old_enough_account():
    t = ts(2026, 10, 7)
    assert T.can_join(t - MIN_ACCOUNT_DAYS * DAY, t)
    assert not T.can_join(t - MIN_ACCOUNT_DAYS * DAY + 1, t)
    assert not T.can_join(None, t)


def test_auto_start_outcome():
    assert T.start_outcome(T.AUTO_MIN_ENTRANTS) == "start"
    assert T.start_outcome(T.AUTO_MIN_ENTRANTS - 1) == "cancel"
    assert T.start_outcome(0) == "cancel"


def test_reminder_due_window():
    start = ts(2026, 10, 10, 19)
    created = start - 3 * DAY
    assert not T.reminder_due(start, created, start - T.REMIND_BEFORE - 1, False)
    assert T.reminder_due(start, created, start - T.REMIND_BEFORE, False)
    assert T.reminder_due(start, created, start - 1, False)
    assert not T.reminder_due(start, created, start, False)  # it's starting: no reminder
    assert not T.reminder_due(start, created, start - 60, True)  # once
    assert not T.reminder_due(None, created, start - 60, False)
    assert not T.reminder_due(start, start - 30 * 60, start - 60, False)  # made inside the hour


def test_stale_action_open_match():
    o = 1_000_000
    assert T.stale_action(T.OPEN, o, o + T.NUDGE_AFTER - 1, False, False) is None
    assert T.stale_action(T.OPEN, o, o + T.NUDGE_AFTER, False, False) == T.Stale.NUDGE
    assert T.stale_action(T.OPEN, o, o + T.NUDGE_AFTER + 5, True, False) is None
    assert T.stale_action(T.OPEN, o, o + T.STALE_AFTER, True, False) == T.Stale.FLAG
    assert T.stale_action(T.OPEN, o, o + T.STALE_AFTER, False, False) == T.Stale.FLAG  # missed the 24 h
    assert T.stale_action(T.OPEN, o, o + 10 * DAY, True, True) is None  # flagged once, staff decide


def test_stale_action_lone_report_is_flagged_never_accepted():
    """A report the opponent never confirmed doesn't decide the match by itself (a player
    could claim a win against someone who's away); staff are flagged once instead."""
    o = 1_000_000
    assert T.stale_action(T.REPORTED, o, o + T.STALE_AFTER - 1, False, False) is None
    assert T.stale_action(T.REPORTED, o, o + T.NUDGE_AFTER, False, False) is None  # no nudge
    assert T.stale_action(T.REPORTED, o, o + T.STALE_AFTER, False, False) == T.Stale.FLAG
    assert T.stale_action(T.REPORTED, o, o + 10 * DAY, False, True) is None  # flagged once
    assert not hasattr(T.Stale, "ACCEPT")


@pytest.mark.parametrize("status", [T.CONFLICT, T.DONE, T.BYE, T.PENDING])
def test_stale_action_ignores_other_states(status):
    assert T.stale_action(status, 0, 100 * DAY, False, False) is None
