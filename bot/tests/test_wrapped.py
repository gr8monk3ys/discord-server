"""Wrapped rules (logic.wrapped): year boundaries, the member and server summaries, personas,
the availability window, cooldowns and the yearly job plan. Pure: no Discord, no database."""

from dataclasses import replace
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from logic import wrapped as W
from logic.stats import Session

TZ = ZoneInfo("America/Los_Angeles")
HOUR = 3600
DAY = 24 * HOUR
ME, PAL, BUDDY, OPTED, GONE = 11, 12, 13, 14, 15
ALLOWED = {ME, PAL, BUDDY}


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


NOW = ts(2027, 1, 5, 12, 0)


def summarize(rows: W.MemberRows, year: int = 2026, allowed=ALLOWED, now: int = NOW, **kw) -> W.Summary:
    return W.summarize(ME, year, TZ, rows, now, allowed, **kw)


def together(start: int, end: int, *users, channel: int = 5) -> list[Session]:
    return [Session(u, channel, start, end) for u in users]


# ---------------------------------------------------------------- the calendar
def test_year_bounds_are_local_midnights():
    start, end = W.year_bounds(2026, TZ)
    assert start == ts(2026, 1, 1) and end == ts(2027, 1, 1)
    # Pacific midnight, not UTC: the year starts eight hours after it does in UTC.
    assert start == int(datetime(2026, 1, 1, 8, tzinfo=timezone.utc).timestamp())
    assert end - start == 365 * DAY  # the two DST changes cancel out


def test_leap_year_has_366_days():
    start, end = W.year_bounds(2028, TZ)
    assert end - start == 366 * DAY
    assert W.month_bounds(2028, 2, TZ) == (ts(2028, 2, 1), ts(2028, 3, 1))
    assert W.month_bounds(2028, 12, TZ) == (ts(2028, 12, 1), ts(2029, 1, 1))
    assert W.day_range(2028) == ("2028-01-01", "2028-12-31")


def test_longest_streak():
    d = date
    assert W.longest_streak([]) == 0
    assert W.longest_streak([d(2026, 3, 1)]) == 1
    assert W.longest_streak([d(2026, 3, 3), d(2026, 3, 1), d(2026, 3, 2), d(2026, 3, 2), d(2026, 3, 9)]) == 3
    assert W.longest_streak([d(2028, 2, 28), d(2028, 2, 29), d(2028, 3, 1)]) == 3  # across the leap day
    assert W.longest_streak([d(2027, 2, 28), d(2027, 3, 1)]) == 2  # and across its absence


# ---------------------------------------------------------------- boundaries in the summary
def test_events_are_cut_at_pacific_new_year():
    last_second, first_second = ts(2026, 12, 31, 23, 59, 59), ts(2027, 1, 1)
    # 00:30 UTC on Jan 1 2026 is still Dec 31 2025 in Los Angeles.
    utc_new_year = int(datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc).timestamp())
    rows = W.MemberRows(clips=[utc_new_year, ts(2026, 1, 1), last_second, first_second],
                        tournaments_entered=[last_second, first_second],
                        challenges=[first_second], words_solved=[ts(2026, 6, 1)])
    s = summarize(rows)
    assert s.clips == 2 and s.tournaments_entered == 1 and s.challenges == 0 and s.words_solved == 1
    assert summarize(rows, year=2025).clips == 1
    assert summarize(rows, year=2027, now=ts(2027, 6, 1)).clips == 1


def test_a_voice_session_across_new_year_is_split_between_the_years():
    start, end = ts(2026, 12, 31, 22), ts(2027, 1, 1, 3)
    rows = W.MemberRows(voice=together(start, end, ME, PAL))
    assert summarize(rows).voice_seconds == 2 * HOUR
    assert summarize(rows, year=2027).voice_seconds == 3 * HOUR


def test_voice_alone_or_in_the_afk_channel_does_not_count():
    rows = W.MemberRows(voice=[Session(ME, 5, ts(2026, 5, 1, 10), ts(2026, 5, 1, 12)),
                               *together(ts(2026, 5, 2, 10), ts(2026, 5, 2, 11), ME, PAL, channel=9)])
    assert summarize(rows).voice_seconds == HOUR
    assert summarize(rows, excluded_channels={9}).voice_seconds == 0


def test_messages_only_count_days_inside_the_year():
    rows = W.MemberRows(message_days={"2025-12-31": 50, "2026-01-01": 3, "2026-03-14": 20, "2026-03-15": 20,
                                      "2026-12-31": 7, "2027-01-01": 99, "not-a-day": 5, "2026-02-30": 5})
    s = summarize(rows)
    assert s.messages == 50 and s.active_days == 4
    assert s.busiest_day == ("2026-03-14", 20)  # the earlier day wins a tie
    assert s.busiest_month == 3 and s.busiest_month_messages == 40
    assert s.chat_streak == 2


def test_leap_day_messages_and_streak():
    rows = W.MemberRows(message_days={"2028-02-28": 1, "2028-02-29": 9, "2028-03-01": 1})
    s = summarize(rows, year=2028, now=ts(2028, 12, 31, 12))
    assert s.messages == 11 and s.chat_streak == 3 and s.busiest_day == ("2028-02-29", 9)
    # The same key in a non-leap year is not a date at all.
    assert summarize(W.MemberRows(message_days={"2027-02-29": 9}), year=2027).messages == 0


def test_busiest_month_weighs_messages_and_voice_minutes():
    rows = W.MemberRows(message_days={"2026-02-10": 100},
                        voice=together(ts(2026, 7, 4, 10), ts(2026, 7, 4, 13), ME, PAL))  # 180 minutes
    s = summarize(rows)
    assert s.busiest_month == 7 and s.busiest_month_voice == 3 * HOUR and s.busiest_month_messages == 0
    assert summarize(W.MemberRows()).busiest_month is None


def test_games_top_five_and_short_sessions_ignored():
    t = ts(2026, 4, 1, 12)
    games = [Session(ME, f"Game {i}", t + i * DAY, t + i * DAY + (i + 1) * HOUR) for i in range(6)]
    games.append(Session(ME, "Blip", t, t + 120))  # under five minutes: not a session
    games.append(Session(PAL, "Other", t, t + 50 * HOUR))  # someone else's row is never mine
    s = summarize(W.MemberRows(games=games))
    assert [g for g, _ in s.top_games] == ["Game 5", "Game 4", "Game 3", "Game 2", "Game 1"]
    assert s.game_seconds == 21 * HOUR and s.games_played == 6


def test_member_who_joined_mid_year():
    rows = W.MemberRows(joined_at=ts(2026, 6, 15, 9), message_days={"2026-06-15": 4})
    s = summarize(rows)
    assert s.joined_month == 6 and s.messages == 4
    assert summarize(replace(rows, joined_at=ts(2025, 6, 15))).joined_month is None
    assert summarize(replace(rows, joined_at=None)).joined_month is None
    # Joined on New Year's Eve, late: still this year in Pacific time.
    assert summarize(replace(rows, joined_at=ts(2026, 12, 31, 23, 30))).joined_month == 12


# ---------------------------------------------------------------- teammates
def test_shared_voice_is_per_channel_overlap():
    t = ts(2026, 5, 1, 10)
    sessions = [Session(ME, 5, t, t + 4 * HOUR), Session(PAL, 5, t + HOUR, t + 3 * HOUR),
                Session(BUDDY, 6, t, t + 4 * HOUR),  # another channel: not together
                Session(PAL, 5, t + HOUR, t + 2 * HOUR)]  # duplicate row: not counted twice
    assert W.shared_voice_seconds(sessions, ME, 0, NOW, NOW) == {PAL: 2 * HOUR}


def test_teammates_rank_by_shared_voice_and_squads():
    t = ts(2026, 5, 1, 10)
    rows = W.MemberRows(
        voice=[*together(t, t + 2 * HOUR, ME, PAL), *together(t + DAY, t + DAY + 2 * HOUR, ME, BUDDY)],
        squads=[W.Squad(t, (ME, BUDDY), ME), W.Squad(t + DAY, (PAL, ME, BUDDY), PAL),
                W.Squad(ts(2025, 5, 1), (ME, BUDDY), ME)],  # last year
        matches=[W.Squad(t, (ME, BUDDY))])
    mates = summarize(rows).teammates
    assert [m.user_id for m in mates] == [BUDDY, PAL]
    assert mates[0] == W.Teammate(BUDDY, 2 * HOUR, 3) and mates[0].score == 2 * HOUR + 3 * W.SQUAD_WEIGHT
    assert mates[1] == W.Teammate(PAL, 2 * HOUR, 1)


def test_opted_out_and_departed_teammates_are_never_named():
    t = ts(2026, 5, 1, 10)
    rows = W.MemberRows(voice=[*together(t, t + 9 * HOUR, ME, OPTED, GONE), *together(t + DAY, t + DAY + HOUR, ME, PAL),
                               *together(t + 2 * DAY, t + 2 * DAY + HOUR, ME, BUDDY)])
    mates = summarize(rows).teammates
    assert {m.user_id for m in mates} == {PAL, BUDDY}  # OPTED had by far the most shared time
    assert summarize(rows, allowed={ME, PAL, BUDDY, OPTED}).teammates[0].user_id == OPTED


def test_teammates_section_needs_two_who_qualify():
    t = ts(2026, 5, 1, 10)
    rows = W.MemberRows(voice=[*together(t, t + 9 * HOUR, ME, OPTED), *together(t + DAY, t + DAY + HOUR, ME, PAL)])
    assert summarize(rows).teammates == ()  # only PAL qualifies: one name would point at the other
    assert summarize(W.MemberRows()).teammates == ()


def test_at_most_three_teammates():
    t = ts(2026, 5, 1, 10)
    others = [21, 22, 23, 24]
    rows = W.MemberRows(matches=[W.Squad(t + i, (ME, *others[: i + 1])) for i in range(4)])
    mates = summarize(rows, allowed={ME, *others}).teammates
    assert [m.user_id for m in mates] == [21, 22, 23]


# ---------------------------------------------------------------- coins, streaks, levels, badges
def coin(delta, reason, at=None, ref=None):
    return W.Coin(delta, reason, ref, ts(2026, 6, 1, 12) if at is None else at)


def test_coins_earned_leaves_out_gifts_stakes_and_refunds():
    ledger = [coin(100, "daily"), coin(25, "message"), coin(500, "quest"), coin(40, "trivia"),
              coin(1000, "give"), coin(-50, "coinflip"), coin(100, "coinflip"), coin(300, "blackjack"),
              coin(-200, "shop"), coin(75, "raffle", ref="raffle:2026-W20:refund:11"),
              coin(999, "daily", at=ts(2027, 1, 1))]
    s = summarize(W.MemberRows(ledger=ledger))
    assert s.coins_earned == 100 + 25 + 500 + 40
    assert s.bets == 1


def test_biggest_payout_is_a_prize_or_game_payout_never_a_refund():
    ledger = [coin(5000, "daily"), coin(300, "blackjack"), coin(900, "blackjack", ref="bjrefund:11:5"),
              coin(1200, "tournament"), coin(2000, "raffle", ref="raffle:2026-W20:refund:11"),
              coin(9000, "slots", at=ts(2025, 12, 31, 23))]
    assert summarize(W.MemberRows(ledger=ledger)).biggest_payout == (1200, "tournament")
    assert summarize(W.MemberRows(ledger=[coin(5, "daily")])).biggest_payout is None
    assert W.label_for("blackjack") == "Blackjack"


def test_daily_streak_is_rebuilt_from_local_claim_days():
    # Claims at 23:30 and then 00:10 local are two different days, a minute apart or not.
    ledger = [coin(100, "daily", at=ts(2026, 3, 1, 23, 30)), coin(100, "daily", at=ts(2026, 3, 2, 0, 10)),
              coin(100, "daily", at=ts(2026, 3, 3, 8)), coin(100, "daily", at=ts(2026, 3, 5, 8)),
              coin(100, "message", at=ts(2026, 3, 4, 8))]
    assert summarize(W.MemberRows(ledger=ledger)).daily_streak == 3


def test_levels_gained_needs_a_start_of_year_snapshot():
    assert summarize(W.MemberRows(level=12)).levels_gained is None  # no history: only "level reached"
    assert summarize(W.MemberRows(level=12)).level == 12
    assert summarize(W.MemberRows(level=12, level_at_start=7)).levels_gained == 5
    # Viewed in January: the next year's snapshot is the level the year ended on.
    assert summarize(W.MemberRows(level=13, level_at_start=7, level_at_end=12)).levels_gained == 5
    assert summarize(W.MemberRows(level=3, level_at_start=7)).levels_gained == 0  # XP removed by staff


def test_badges_earned_this_year_and_the_rarest():
    at = ts(2026, 6, 1)
    rows = W.MemberRows(badges=[("chatty", at), ("tourney_win", at), ("first_squad", ts(2025, 6, 1)),
                                ("wrapped_2026", at), ("first_clip", at), ("mystery_key", at)],
                        badge_holders={"chatty": 9, "tourney_win": 2, "first_clip": 2, "mystery_key": 1})
    s = summarize(rows)
    assert s.badges == ("chatty", "first_clip", "tourney_win", "mystery_key")  # catalogue order, no share badge
    assert s.rarest_badge == ("first_clip", 2)  # tie: the first in order; unknown keys can't be named
    assert W.badge_name("tourney_win") == "Champion" and W.badge_name("mystery_key") is None
    assert W.badge_name("wrapped_2026") == "Wrapped 2026"
    assert summarize(W.MemberRows()).rarest_badge is None


def test_squads_hosted_joined_and_matches():
    t = ts(2026, 5, 1)
    rows = W.MemberRows(squads=[W.Squad(t, (ME, PAL), ME), W.Squad(t, (PAL, ME), PAL), W.Squad(t, (ME,), ME),
                                W.Squad(ts(2027, 1, 1), (PAL, ME), PAL)],
                        matches=[W.Squad(t, (ME, PAL)), W.Squad(t, (PAL, BUDDY))])
    s = summarize(rows)
    assert (s.squads_hosted, s.squads_joined, s.matches, s.squads) == (2, 1, 1, 4)


# ---------------------------------------------------------------- the empty member
def test_member_with_no_data_is_sparse_not_zeros():
    s = summarize(W.MemberRows())
    assert s.sparse and s.messages == 0 and s.top_games == () and s.teammates == ()
    assert W.persona(s).key == "quiet"
    assert W.persona(summarize(W.MemberRows(joined_at=ts(2026, 11, 2)))).key == "fresh"


def test_sparse_stops_once_there_is_something_to_show():
    base = W.Summary(ME, 2026, messages=19, voice_seconds=HOUR - 1, badges=("chatty", "birthday"))
    assert base.sparse
    for change in (dict(messages=20), dict(voice_seconds=HOUR), dict(game_seconds=HOUR), dict(squads_joined=1),
                   dict(matches=1), dict(tournaments_entered=1), dict(clips=1), dict(words_solved=5),
                   dict(badges=("chatty", "birthday", "first_clip"))):
        assert not replace(base, **change).sparse, change


# ---------------------------------------------------------------- personas
BLANK = W.Summary(ME, 2026)


@pytest.mark.parametrize("key, facts", [
    ("champion", dict(tournaments_won=1)),
    ("captain", dict(squads_hosted=3, gamenights=2)),
    ("director", dict(clips=10)),
    ("director", dict(clip_week_wins=1)),
    ("voice", dict(voice_seconds=50 * HOUR, messages=3000)),
    ("chat", dict(messages=1000)),
    ("chat", dict(voice_seconds=50 * HOUR, messages=3001)),  # more messages than voice minutes
    ("loyalist", dict(game_seconds=20 * HOUR, top_games=(("A", 14 * HOUR), ("B", 6 * HOUR)))),
    ("sampler", dict(games_played=5)),
    ("teamplayer", dict(squads_joined=5, matches=3)),
    ("collector", dict(badges=tuple("abcdef"))),
    ("streak", dict(daily_streak=14)),
    ("streak", dict(chat_streak=14)),
    ("wordsmith", dict(words_solved=20)),
    ("quester", dict(challenges=4, event_quests=3, event_drops=3)),
    ("regular", dict(active_days=30)),
    ("regular", dict(voice_seconds=10 * HOUR)),
    ("regular", dict(voice_seconds=50 * HOUR - 1)),  # just under Voice of the Lobby
    ("fresh", dict(joined_month=11)),
    ("quiet", dict()),
])
def test_each_persona_rule(key, facts):
    assert W.persona(replace(BLANK, **facts)).key == key


@pytest.mark.parametrize("facts", [
    dict(squads_hosted=4), dict(clips=9), dict(voice_seconds=10 * HOUR - 1, messages=999),
    dict(game_seconds=20 * HOUR, top_games=(("A", 13 * HOUR), ("B", 7 * HOUR))),
    dict(game_seconds=19 * HOUR, top_games=(("A", 19 * HOUR),)), dict(games_played=4),
    dict(squads_joined=7), dict(badges=tuple("abcde")), dict(daily_streak=13), dict(words_solved=19),
    dict(challenges=9), dict(active_days=29, voice_seconds=10 * HOUR - 1),
])
def test_just_under_each_threshold_is_the_fallback(facts):
    assert W.persona(replace(BLANK, **facts)).key == "quiet"


def test_personas_are_picked_in_priority_order_and_deterministically():
    everything = replace(BLANK, tournaments_won=1, squads_hosted=9, clips=50, messages=5000, joined_month=3)
    assert W.persona(everything).key == "champion"
    assert W.persona(replace(everything, tournaments_won=0)).key == "captain"
    assert W.persona(replace(everything, tournaments_won=0, squads_hosted=0)).key == "director"
    assert W.persona(everything) is W.persona(everything)


def test_persona_catalogue():
    keys = [p.key for p in W.PERSONAS]
    assert 10 <= len(keys) <= 15 and len(set(keys)) == len(keys)
    assert len({p.title for p in W.PERSONAS}) == len(keys)
    assert W.PERSONAS[-1].key == "quiet" and W.PERSONAS[-1].check(BLANK)  # always a fallback
    for p in W.PERSONAS:
        assert p.title and p.line and p.rule
        assert len(p.title) <= 24 and len(p.line) <= 70  # fits the card
        assert "@" not in p.line and "http" not in p.line


# ---------------------------------------------------------------- availability
@pytest.mark.parametrize("when, year", [
    ((2026, 11, 30, 23, 59), None), ((2026, 12, 1, 0, 0), 2026), ((2026, 12, 31, 23, 59), 2026),
    ((2027, 1, 1, 0, 0), 2026), ((2027, 1, 15, 23, 59), 2026), ((2027, 1, 16, 0, 0), None),
    ((2027, 6, 1, 12, 0), None), ((2027, 12, 1, 0, 0), 2027),
])
def test_open_year(when, year):
    assert W.open_year(ts(*when), TZ) == year


def test_window_edges_match_open_year():
    assert W.opens_at(2026, TZ) == ts(2026, 12, 1) and W.closes_at(2026, TZ) == ts(2027, 1, 16)
    assert W.open_year(W.opens_at(2026, TZ) - 1, TZ) is None and W.open_year(W.opens_at(2026, TZ), TZ) == 2026
    assert W.open_year(W.closes_at(2026, TZ) - 1, TZ) == 2026 and W.open_year(W.closes_at(2026, TZ), TZ) is None


def test_members_get_only_the_open_year():
    december, january, june = ts(2026, 12, 10), ts(2027, 1, 10), ts(2027, 6, 10)
    assert W.resolve_year(None, december, TZ) == (2026, None)
    assert W.resolve_year(2026, january, TZ) == (2026, None)
    assert W.resolve_year(None, january, TZ) == (2026, None)  # January shows the year just ended
    year, why = W.resolve_year(2027, january, TZ)
    assert year is None and "Only Wrapped 2026" in why
    assert W.resolve_year(None, june, TZ) == (None, W.CLOSED_TEXT)
    assert W.resolve_year(2026, june, TZ) == (None, W.CLOSED_TEXT)


def test_staff_can_preview_any_real_year_any_time():
    june = ts(2027, 6, 10)
    assert W.resolve_year(None, june, TZ, staff=True) == (2027, None)  # this year so far
    assert W.resolve_year(2026, june, TZ, staff=True) == (2026, None)
    assert W.resolve_year(None, ts(2027, 1, 10), TZ, staff=True) == (2026, None)
    for bad in (2028, W.FIRST_YEAR - 1):
        year, why = W.resolve_year(bad, june, TZ, staff=True)
        assert year is None and str(bad) in why


# ---------------------------------------------------------------- cooldown, digest, keys
def test_cooldown_left():
    assert W.cooldown_left(None, 1000) == 0
    assert W.cooldown_left(1000, 1000) == W.COOLDOWN
    assert W.cooldown_left(1000, 1000 + W.COOLDOWN - 1) == 1
    assert W.cooldown_left(1000, 1000 + W.COOLDOWN) == 0
    assert W.cooldown_left(1000, 1000 + HOUR, W.SHARE_COOLDOWN) == W.SHARE_COOLDOWN - HOUR


def test_digest_changes_only_when_the_card_would():
    a = replace(BLANK, messages=5)
    assert W.digest(a, "Alex") == W.digest(replace(BLANK, messages=5), "Alex")
    assert W.digest(a, "Alex") != W.digest(replace(a, messages=6), "Alex")
    assert W.digest(a, "Alex") != W.digest(a, "Alexa")
    assert len(W.digest(a)) == 64


def test_keys():
    assert W.share_key(2026, 42) == "wrapped:shared:2026:42"
    assert W.badge_key(2026) == "wrapped_2026" and W.badge_key(2026).startswith(W.WRAPPED_BADGE_PREFIX)
    assert W.levels_key(2027) == "wrapped:levels:2027" and W.levels_key(2027).startswith(W.LEVELS_PREFIX)


def test_level_snapshot_is_only_taken_in_january():
    assert W.snapshot_due(ts(2027, 1, 1, 0, 5), TZ) == 2027
    assert W.snapshot_due(ts(2027, 1, 31, 23), TZ) == 2027
    assert W.snapshot_due(ts(2027, 2, 1), TZ) is None and W.snapshot_due(ts(2026, 12, 31, 23), TZ) is None


# ---------------------------------------------------------------- the yearly jobs
def test_annual_job_runs_once_at_its_local_time():
    job = W.ANNOUNCE_JOB
    first_seen = ts(2026, 10, 12)
    before, at = ts(2026, 12, 1, 9, 59), ts(2026, 12, 1, 10, 0)
    plan = W.plan_annual(job, before, TZ, set(), first_seen)
    assert plan.run is None and [p.key for p in plan.mark_done] == ["wrapped:2025"]  # predates the bot
    plan = W.plan_annual(job, at, TZ, {"wrapped:2025"}, first_seen)
    assert plan.run.key == "wrapped:2026" and plan.run.year == 2026 and plan.run.scheduled_at == at
    assert plan.mark_done == []
    assert W.plan_annual(job, at + 60, TZ, {"wrapped:2026"}, first_seen) == W.Plan(None, [])


def test_annual_job_catches_up_once_but_not_too_late():
    job, first_seen = W.ANNOUNCE_JOB, ts(2026, 10, 12)
    assert W.plan_annual(job, ts(2026, 12, 9, 3), TZ, set(), first_seen).run.key == "wrapped:2026"
    assert W.plan_annual(job, ts(2027, 1, 14), TZ, set(), first_seen).run.key == "wrapped:2026"
    late = W.plan_annual(job, ts(2027, 1, 16), TZ, set(), first_seen)  # Wrapped has closed
    assert late.run is None and [p.key for p in late.mark_done] == ["wrapped:2026"]
    reminder = W.REMINDER_JOB
    assert W.plan_annual(reminder, ts(2026, 12, 20, 10), TZ, set(), first_seen).run.key == "wrappedreminder:2026"
    assert W.plan_annual(reminder, ts(2027, 1, 2), TZ, set(), first_seen).run is not None
    assert W.plan_annual(reminder, ts(2027, 1, 4), TZ, set(), first_seen).run is None


def test_annual_job_never_runs_a_period_from_before_the_bot_saw_it():
    job = W.ANNOUNCE_JOB
    for first_seen in (None, ts(2026, 12, 5)):  # very first start, or switched on after December 1
        plan = W.plan_annual(job, ts(2026, 12, 6), TZ, set(), first_seen)
        assert plan.run is None and [p.key for p in plan.mark_done] == ["wrapped:2026"]
    assert W.latest_due_annual(job, ts(2026, 6, 1), TZ).year == 2025


def test_post_texts():
    text = W.announce_text(2026)
    assert "Wrapped 2026" in text and "/wrapped" in text and "January 15" in text and "@" not in text
    assert text.endswith("went:") and not W.announce_text(2026, with_card=False).endswith("went:")
    assert "Wrapped 2026" in W.reminder_text(2026) and "@everyone" not in W.reminder_text(2026)
    assert W.share_text("<@1>", 2026, "The Champion") == "📓 <@1>'s Wrapped 2026: **The Champion**"
    assert "/privacy" in W.OPTOUT_SELF and "Nova" in W.optout_other("Nova")


# ---------------------------------------------------------------- the server's year
def server_rows() -> W.ServerRows:
    t = ts(2026, 5, 1, 10)
    return W.ServerRows(
        voice=[*together(t, t + 3 * HOUR, ME, PAL, OPTED), *together(ts(2025, 5, 1), ts(2025, 5, 2), ME, PAL)],
        games=[Session(ME, "Alpha", t, t + 2 * HOUR), Session(PAL, "Alpha", t, t + HOUR),
               Session(OPTED, "Beta", t, t + 10 * HOUR)],
        messages=[(ME, "2026-05-01", 60), (PAL, "2026-05-02", 400), (OPTED, "2026-05-02", 900),
                  (PAL, "2025-05-02", 5000)],
        ledger=[W.Coin(100, "daily", None, t + i * DAY, ME) for i in range(4)]
        + [W.Coin(100, "daily", None, t, PAL), W.Coin(700, "give", None, t, PAL)],
        badges=[(ME, "chatty", t), (ME, "first_clip", t), (PAL, "chatty", t), (ME, "wrapped_2026", t),
                (PAL, "birthday", ts(2025, 1, 1))],
        squads=[W.Squad(t, (ME, PAL), ME), W.Squad(t, (ME, BUDDY), ME), W.Squad(t, (PAL,), PAL),
                W.Squad(ts(2027, 1, 1), (ME, PAL), ME)],
        matches=[W.Squad(t, (ME, PAL))],
        gamenights=[t, ts(2025, 1, 1)], tournaments=[t],
        clips=[(PAL, t), (PAL, t + 1), (ME, t), (ME, ts(2025, 1, 1))],
        joins=[(BUDDY, t), (BUDDY, t + DAY), (GONE, t), (ME, ts(2025, 1, 1))])


def test_server_totals_are_cut_to_the_year():
    s = W.summarize_server(2026, TZ, server_rows(), NOW, ALLOWED)
    assert s.messages == 1360 and s.voice_seconds == 9 * HOUR and s.game_seconds == 13 * HOUR
    assert s.top_games == (("Beta", 10 * HOUR), ("Alpha", 3 * HOUR))
    assert (s.squads, s.gamenights, s.tournaments, s.clips, s.badges) == (4, 1, 1, 3, 3)
    assert s.coins_earned == 500 and s.new_members == 2 and s.active_members == 3
    assert s.busiest_month == 5 and not s.empty


def test_server_shout_outs_only_name_allowed_members_and_only_the_most():
    s = W.summarize_server(2026, TZ, server_rows(), NOW, ALLOWED)
    by_key = {sup.key: sup for sup in s.superlatives}
    assert all(OPTED not in sup.user_ids and GONE not in sup.user_ids for sup in s.superlatives)
    assert by_key["chat"].user_ids == (PAL,) and by_key["chat"].detail == "400 messages"  # OPTED had 900
    assert by_key["gaming"].user_ids == (ME,)  # OPTED had ten hours
    assert by_key["voice"].user_ids == (ME, PAL) and by_key["voice"].detail == "3 h"  # a tie is shared
    assert by_key["host"].user_ids == (ME,) and by_key["host"].detail == "2 squads hosted"  # empty squads don't count
    assert by_key["clips"].user_ids == (PAL,)
    assert by_key["badges"].user_ids == (ME,) and by_key["badges"].detail == "2 badges"
    assert by_key["streak"].user_ids == (ME,) and by_key["streak"].detail == "4 days"
    for sup in s.superlatives:
        assert "least" not in sup.title.lower() and "fewest" not in sup.title.lower()


def test_server_shout_outs_need_a_real_number_and_skip_wide_ties():
    t = ts(2026, 5, 1, 10)
    rows = W.ServerRows(messages=[(ME, "2026-05-01", 49), (PAL, "2026-05-01", 10)],
                        clips=[(ME, t), (PAL, t), (BUDDY, t), (ME, t + 1), (PAL, t + 1), (BUDDY, t + 1)])
    s = W.summarize_server(2026, TZ, rows, NOW, ALLOWED)
    assert s.superlatives == ()  # 49 messages is under the bar; three people tied on clips
    assert W.summarize_server(2026, TZ, rows, NOW, set()).superlatives == ()
    assert W.summarize_server(2026, TZ, W.ServerRows(), NOW, ALLOWED).empty


def test_fmt_hours():
    assert W.fmt_hours(0) == "0" and W.fmt_hours(1800) == "0.5" and W.fmt_hours(3 * HOUR) == "3"
    assert W.fmt_hours(int(7.26 * HOUR)) == "7.3" and W.fmt_hours(10 * HOUR) == "10"
    assert W.fmt_hours(1204 * HOUR + 1700) == "1,204"
    assert W.plural(1, "match", "matches") == "1 match" and W.plural(2, "match", "matches") == "2 matches"
    assert W.plural(1200, "day") == "1,200 days"
