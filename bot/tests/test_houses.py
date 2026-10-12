"""logic/houses.py: placement, the scoring formula, seasons, frozen results and the read API
(against a real in-memory SQLite database). No Discord, no network, no real time."""

import asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import layout
from logic import houses as H

TZ = ZoneInfo("America/Los_Angeles")
HOUR = 3600
DAY = 24 * HOUR
FERN, KESTREL, OTTER = "fern", "kestrel", "otter"


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


T = ts(2026, 10, 7, 15, 0)  # Wednesday of 2026-W41
WEEK = H.week_of(T, TZ)


def run(coro):
    return asyncio.run(coro)


def young_id(at: int, n: int = 0) -> int:
    """A Discord id for an account created 5 days before `at` (under MIN_ACCOUNT_DAYS)."""
    return discord.utils.time_snowflake(datetime.fromtimestamp(at - 5 * DAY, timezone.utc)) + n


def by_house(standings):
    return {s.house: s for s in standings}


# ---------------------------------------------------------------- the houses
def test_three_houses_from_the_layout_with_roles_at_the_bottom():
    assert [h.key for h in H.HOUSES] == [FERN, KESTREL, OTTER]
    assert len({h.role for h in H.HOUSES}) == len({h.emoji for h in H.HOUSES}) == len({h.color for h in H.HOUSES}) == 3
    assert all(h.role == config.HOUSE_ROLES[h.key] and h.role.startswith("House ") for h in H.HOUSES)
    # Created last in layout.ROLES: the lowest roles, below Front Desk, with a name colour each.
    assert [r["name"] for r in layout.ROLES[-3:]] == [h.role for h in H.HOUSES]
    assert all(r["color"] and not r.get("hoist") and not r.get("mentionable") for r in layout.ROLES[-3:])


# ---------------------------------------------------------------- placement
def test_place_picks_the_smallest_house():
    assert H.place(1, {FERN: 2, KESTREL: 1, OTTER: 2}) == KESTREL
    assert H.place(1, {FERN: 3, KESTREL: 3}) == OTTER  # a house nobody is in yet counts as 0


def test_place_breaks_ties_the_same_way_every_time():
    sizes = {FERN: 1, KESTREL: 1, OTTER: 1}
    ids = [discord.utils.time_snowflake(datetime(2020, 1, 1, 0, 0, i, tzinfo=timezone.utc)) for i in range(30)]
    picks = [H.place(u, sizes) for u in ids]
    assert picks == [H.place(u, sizes) for u in ids]
    assert set(picks) == {FERN, KESTREL, OTTER}  # the tie-break isn't "always the first house"
    assert H.place(ids[0], {FERN: 1, KESTREL: 5, OTTER: 1}) in (FERN, OTTER)


def test_placing_one_by_one_keeps_houses_balanced():
    membership: dict[int, str] = {}
    for i in range(16):
        uid = discord.utils.time_snowflake(datetime(2021, 3, 1, 0, i, tzinfo=timezone.utc))
        membership[uid] = H.place(uid, H.sizes(membership))
    assert sorted(H.sizes(membership).values()) == [5, 5, 6]


def test_rejoin_cooldown():
    assert H.REJOIN_COOLDOWN >= 30 * DAY
    assert not H.can_rejoin(T, T + H.REJOIN_COOLDOWN - 1)
    assert H.can_rejoin(T, T + H.REJOIN_COOLDOWN)
    assert H.rejoin_at(T) == T + H.REJOIN_COOLDOWN


# ---------------------------------------------------------------- the formula
def test_units_are_clipped_per_source_and_points_per_member():
    assert H.member_points({}) == 0
    assert H.member_points({"active_days": 2, "squads": 1}) == 2 * 3 + 5
    assert H.member_points({"active_days": 400}) == 7 * 3  # a chat day is presence: spam earns nothing more
    assert H.member_points({"voice_hours": 60}) == 10 * 2
    assert H.member_points({"nonsense": 99, "active_days": -4}) == 0
    assert [(s.key, n, p) for s, n, p in H.breakdown({"word_games": 9, "badges": 1})] == [
        ("word_games", 7, 14), ("badges", 1, 3)]


def test_member_cap_is_below_the_most_one_member_could_score():
    everything = {s.key: 10_000 for s in H.SOURCES}
    assert sum(s.points * s.cap for s in H.SOURCES) > H.MEMBER_CAP
    assert H.member_points(everything) == H.MEMBER_CAP


def test_a_bigger_house_does_not_win_on_headcount():
    # Six members with 10 points each against three members with 30 each.
    membership = {**{i: FERN for i in range(1, 7)}, **{i: KESTREL for i in range(11, 14)},
                  **{i: OTTER for i in range(21, 26)}}
    points = {**{i: 10 for i in range(1, 7)}, **{i: 30 for i in range(11, 14)}}
    s = by_house(H.score_houses(membership, points, "2026-W41"))
    assert s[FERN].total == 60 and s[KESTREL].total == 90
    assert s[FERN].score == 60 * H.SCALE // 6 == 100
    # Kestrel is divided by 5 (largest roster - 1), not by its own 3.
    assert s[KESTREL].score == 90 * H.SCALE // 5 == 180
    assert (s[KESTREL].rank, s[FERN].rank, s[OTTER].rank) == (1, 2, 3)
    assert (s[FERN].members, s[FERN].contributors, s[OTTER].contributors) == (6, 6, 0)


def test_one_hyperactive_member_cannot_carry_a_house():
    membership = {**{i: FERN for i in range(1, 6)}, **{i: KESTREL for i in range(11, 16)},
                  **{i: OTTER for i in range(21, 26)}}
    # Fern: one member far over the cap, four idle. Kestrel: five members at 30.
    points = {1: 5000, **{i: 30 for i in range(11, 16)}}
    s = by_house(H.score_houses(membership, points, "2026-W41"))
    assert s[FERN].total == H.MEMBER_CAP and s[KESTREL].total == 150
    assert s[KESTREL].rank == 1 and s[FERN].rank == 2


def test_a_shrunken_house_is_not_divided_by_its_own_small_roster():
    # One Otter left alone with a perfect week must not beat a full house averaging 40.
    membership = {**{i: FERN for i in range(1, 6)}, **{i: KESTREL for i in range(11, 16)}, 21: OTTER}
    points = {**{i: 40 for i in range(1, 6)}, 21: H.MEMBER_CAP}
    s = by_house(H.score_houses(membership, points, "2026-W41"))
    assert s[OTTER].score == H.MEMBER_CAP * H.SCALE // 4  # divisor: largest roster (5) - 1
    assert s[FERN].score == 400 and s[FERN].rank == 1 and s[OTTER].rank == 2


def test_points_of_people_outside_the_roster_are_ignored():
    s = by_house(H.score_houses({1: FERN, 2: KESTREL}, {1: 10, 99: 100, 2: 0}, "2026-W41"))
    assert s[FERN].total == 10 and s[KESTREL].total == 0 and s[OTTER].total == 0
    assert H.winner(s.values()).house == FERN


def test_ties_fall_to_contributors_then_total_then_a_rotation_by_period():
    membership = {1: FERN, 2: FERN, 11: KESTREL, 12: KESTREL, 21: OTTER, 22: OTTER}
    # Same score; Kestrel has two contributors, Fern one.
    s = H.score_houses(membership, {1: 20, 11: 10, 12: 10}, "2026-W41")
    assert [x.house for x in s][:2] == [KESTREL, FERN]
    # A full tie: the order depends on the period key only, and isn't the same every week.
    even = {1: 10, 11: 10, 21: 10}
    firsts = {H.score_houses(membership, even, f"2026-W{n:02}")[0].house for n in range(1, 30)}
    assert firsts == {FERN, KESTREL, OTTER}
    assert H.score_houses(membership, even, "2026-W07") == H.score_houses(membership, even, "2026-W07")


def test_no_points_means_no_winner_and_no_cup_points():
    s = H.score_houses({1: FERN, 2: KESTREL}, {}, "2026-W41")
    assert H.winner(s) is None
    assert H.cup_points(s) == {FERN: 0, KESTREL: 0, OTTER: 0}
    assert H.winner([]) is None


def test_cup_points_only_go_to_houses_that_scored():
    s = H.score_houses({1: FERN, 2: KESTREL, 3: OTTER}, {1: 30, 2: 10}, "2026-W41")
    assert H.cup_points(s) == {FERN: 3, KESTREL: 2, OTTER: 0}


def test_payees_need_points_and_company():
    membership = {1: FERN, 2: FERN, 3: FERN, 4: KESTREL}
    points = {1: H.MIN_PAID_POINTS, 2: H.MIN_PAID_POINTS - 1, 3: 50, 4: 99}
    assert H.payees(membership, points, FERN) == [1, 3]  # 2 is under the bar, 4 is another house
    assert H.payees(membership, {1: 100}, FERN) == []  # alone: the house wins, nobody is paid
    assert H.payees(membership, points, None) == []
    assert H.MIN_PAYEES >= 2
    assert H.reward_ref("2026-W41", 7) == "housecup:2026-W41:7"


# ---------------------------------------------------------------- seasons
def test_seasons_are_calendar_quarters():
    assert H.season_of(date(2026, 1, 1)) == "2026-Q1" and H.season_of(date(2026, 3, 31)) == "2026-Q1"
    assert H.season_of(date(2026, 10, 1)) == "2026-Q4" and H.season_of(date(2026, 12, 31)) == "2026-Q4"
    assert H.week_season(WEEK) == "2026-Q4"
    # A week belongs to the quarter of its Sunday: Mon Sep 28 - Sun Oct 4 is already Q4.
    assert H.week_season(H.week_of(ts(2026, 9, 29, 12, 0), TZ)) == "2026-Q4"
    assert [H.season_closing(f"2026-{m:02}") for m in (3, 6, 9, 12)] == ["2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]
    assert all(H.season_closing(f"2026-{m:02}") is None for m in (1, 2, 4, 5, 7, 8, 10, 11))
    assert H.season_label("2026-Q4") == "Q4 2026"


def test_season_tally_sums_cup_points_and_a_new_season_starts_empty():
    members = {1: FERN, 2: KESTREL, 3: OTTER}
    weeks = [H.score_houses(members, {1: 30, 2: 10}, "2026-W41"),
             H.score_houses(members, {2: 30, 3: 20, 1: 10}, "2026-W42"),
             H.score_houses(members, {1: 30, 2: 10}, "2026-W43"),
             H.score_houses(members, {}, "2026-W44")]
    tally = {t.house: t for t in H.season_tally(weeks, "2026-Q4")}
    assert (tally[FERN].cup, tally[FERN].wins) == (3 + 1 + 3, 2)
    assert (tally[KESTREL].cup, tally[KESTREL].wins) == (2 + 3 + 2, 1)
    assert (tally[OTTER].cup, tally[OTTER].wins) == (2, 0)
    assert tally[FERN].rank == 1  # level on cup points: more weeks won decides
    assert H.champion(tally.values()).house == FERN
    empty = H.season_tally([], "2027-Q1")
    assert all(t.cup == t.wins == t.score == 0 for t in empty) and H.champion(empty) is None


def test_season_job_waits_for_the_latest_weekly_close():
    # Friday 2027-01-01: the last weekly close before it is Monday 2026-12-28.
    assert H.season_job_week(ts(2027, 1, 1, 10, 20), TZ) == "housecup:2026-W53"
    # A quarter that starts on a Monday: the close of that same morning.
    assert H.season_job_week(ts(2029, 1, 1, 10, 20), TZ) == "housecup:2029-W01"
    assert (H.WEEKLY_JOB.weekday, H.WEEKLY_JOB.hour, H.WEEKLY_JOB.minute) == (0, 10, 10)


# ---------------------------------------------------------------- frozen results and text
def test_week_and_season_results_round_trip():
    s = H.score_houses({1: FERN, 2: KESTREL}, {1: 30, 2: 10}, "2026-W41")
    assert H.load_week(H.dump_week(s, [1, 5])) == (s, [1, 5])
    tally = H.season_tally([s], "2026-Q4")
    assert H.load_season(H.dump_season(tally)) == tally
    assert H.load_week("not json") == ([], []) and H.load_week('{"standings": [[1]]}') == ([], [])
    assert H.load_season("[]") == [] and H.load_season("{") == []
    # A house that no longer exists is dropped rather than crashing a reader.
    assert H.load_week('{"standings": [["gone", 1, 1, 1, 1, 1]], "paid": []}') == ([], [])


def test_standings_text_names_houses_only():
    s = H.score_houses({1: FERN, 2: FERN, 3: KESTREL}, {1: 31, 3: 4}, "2026-W41")
    text = H.standings_text(s)
    assert "<@" not in text and "@" not in text
    lines = text.splitlines()
    assert lines[0].startswith("🥇 🌿 **Fern** · **15.5** per member") and "1 of 2 members chipped in" in lines[0]
    assert lines[2].startswith("▫️")  # no medal for a house without points
    assert H.fmt_score(155) == "15.5"


def test_winner_trophy_contribution_and_history_text():
    s = H.score_houses({1: FERN, 2: FERN, 3: KESTREL}, {1: 31, 2: 12, 3: 4}, "2026-W41")
    paid = H.winner_text(WEEK, s, [1, 2])
    assert "House Fern takes the House Cup for Oct 5 – Oct 11" in paid and "<@1> <@2>" in paid
    assert f"**{H.WIN_REWARD} coins** each" in paid
    assert "No coins this week" in H.winner_text(WEEK, s, [])
    tally = H.season_tally([s], "2026-Q4")
    assert "House Fern wins the Q4 2026 season trophy" in H.trophy_text("2026-Q4", tally)
    assert "Nothing yet" in H.contribution_text({})
    mine = H.contribution_text({"active_days": 3, "squads": 1})
    assert "**9** · 3 days chatted" in mine and "Total: **14** of 100 points" in mine
    assert "(capped from" in H.contribution_text({s.key: 99 for s in H.SOURCES})
    assert "No House Cup has been decided yet" in H.history_text([], [])
    text = H.history_text([("2026-Q4", FERN), ("2026-Q3", None)], [("2026-W41", KESTREL), ("2026-W40", None)])
    assert "🏆 **House Cup · Q4 2026**: 🌿 House Fern" in text and "Q3 2026**: no winner" in text
    assert "`2026-W41` 🪶 Kestrel" in text and "`2026-W40` no winner" in text
    assert str(H.MEMBER_CAP) in H.how_text()


# ---------------------------------------------------------------- reading the database
async def fresh():
    d = dbmod.Database(":memory:")
    await d.connect()
    await d.migrate()
    return d


async def join(d, uid, house, left_at=None):
    await d.execute("INSERT INTO house_members (user_id, house, joined_at, left_at) VALUES (?, ?, ?, ?)",
                    (uid, house, T - DAY, left_at))


async def chat(d, uid, days, count=H.ACTIVE_DAY_MESSAGES, week=WEEK):
    for i in range(days):
        day = date.fromordinal(week.first_day.toordinal() + i).isoformat()
        await d.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, ?, ?)", (uid, day, count))


def test_roster_only_holds_members_who_count():
    async def go():
        d = await fresh()
        young = young_id(T)
        await join(d, 1, FERN)
        await join(d, 2, KESTREL)
        await join(d, 3, OTTER, left_at=T - HOUR)  # left their house
        await join(d, 4, OTTER)  # opted out
        await join(d, young, FERN)  # fresh account
        await join(d, 6, "gone")  # a house that no longer exists
        await d.execute("INSERT INTO privacy_optout (user_id, at) VALUES (4, ?)", (T,))
        assert await H.roster(d, T) == {1: FERN, 2: KESTREL}
        assert await H.roster(d, T, present={2, 3, 4}) == {2: KESTREL}  # 1 is no longer in the server
        assert await H.house_of(d, 1) == FERN and await H.house_of(d, 3) is None and await H.house_of(d, 99) is None
        await d.close()
    run(go())


def test_units_come_from_the_other_modules_tables_for_that_week_only():
    async def go():
        d = await fresh()
        mid = WEEK.start + 2 * DAY
        await chat(d, 1, 3)
        await chat(d, 1, 1, count=H.ACTIVE_DAY_MESSAGES - 1, week=H.week_of(T + 7 * DAY, TZ))  # next week, too few
        await d.execute("INSERT INTO message_counts (user_id, day, count) VALUES (2, ?, ?)",
                        (WEEK.last_day.isoformat(), H.ACTIVE_DAY_MESSAGES - 1))  # not an active day
        # Voice: 1 and 2 together for 2.5 h, 3 alone for 5 h, 1 with a fresh account for 4 h.
        young = young_id(T)
        for uid, ch, start, end in ((1, 5, mid, mid + 9000), (2, 5, mid, mid + 9000), (3, 6, mid, mid + 5 * HOUR),
                                    (1, 7, mid + 4 * HOUR, mid + 8 * HOUR), (young, 7, mid + 4 * HOUR, mid + 8 * HOUR)):
            await d.execute('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, ?, ?, ?)',
                            (uid, ch, start, end))
        # Squads: 1 joins 2's post (counts), 2 "joins" their own (doesn't), 3 joins a fresh account's post (doesn't).
        for pid, host in ((1, 2), (2, young)):
            await d.execute("INSERT INTO lfg_posts (id, game, host_id, size, when_text, created_at)"
                            " VALUES (?, 'g', ?, 4, 'now', ?)", (pid, host, mid))
        for pid, uid in ((1, 2), (1, 1), (2, 3)):
            await d.execute("INSERT INTO lfg_members (post_id, user_id, joined_at) VALUES (?, ?, ?)", (pid, uid, mid))
        await d.execute("INSERT INTO word_games (day, user_id, solved) VALUES (?, 1, 1)", (WEEK.first_day.isoformat(),))
        await d.execute("INSERT INTO word_games (day, user_id, solved) VALUES (?, 2, 0)", (WEEK.first_day.isoformat(),))
        await d.execute("INSERT INTO challenge_claims (week, user_id, key, at) VALUES ('2026-W40', 1, 'bonus', ?)", (mid,))
        await d.execute("INSERT INTO challenge_claims (week, user_id, key, at) VALUES ('2026-W41', 1, 'x', ?)",
                        (WEEK.end + 5,))  # claimed next week
        await d.execute("INSERT INTO tournaments (id, name, size, status, created_by, created_at)"
                        " VALUES (1, 'Cup', 4, 'running', 9, ?)", (mid,))
        await d.execute("INSERT INTO tournaments (id, name, size, status, created_by, created_at)"
                        " VALUES (2, 'Later', 4, 'signup', 9, ?)", (mid,))
        for tid in (1, 2):
            await d.execute("INSERT INTO tournament_entries (tournament_id, user_id, joined_at) VALUES (?, 1, ?)",
                            (tid, mid))
        await d.execute("INSERT INTO ledger (user_id, delta, reason, ref, at) VALUES (2, 500, 'tournament',"
                        " 'tourney:1:first', ?)", (mid,))
        await d.execute("INSERT INTO starboard (message_id, channel_id, author_id, board_message_id, stars, at)"
                        " VALUES (1, 1, 3, 777, 5, ?)", (mid,))
        await d.execute("INSERT INTO starboard (message_id, channel_id, author_id, board_message_id, stars, at)"
                        " VALUES (2, 1, 3, 0, 5, ?)", (mid,))  # still pending
        await d.execute("INSERT INTO achievements (user_id, key, at) VALUES (3, 'a', ?)", (mid,))
        await d.execute("INSERT INTO achievements (user_id, key, at) VALUES (3, 'old', ?)", (WEEK.start - 1,))
        await d.execute("INSERT INTO quest_steps (user_id, step, at) VALUES (2, 'hi', ?)", (mid,))
        got = await H.units(d, WEEK, T + 6 * DAY)
        assert got[1] == {"active_days": 3, "voice_hours": 2, "squads": 1, "word_games": 1, "challenges": 1,
                          "tournament_entries": 1}
        assert got[2] == {"voice_hours": 2, "tournament_wins": 1, "quest_steps": 1}
        assert got[3] == {"hall_of_fame": 1, "badges": 1}
        assert young not in got
        # An excluded (AFK) channel and bots don't count as company either.
        assert "voice_hours" not in (await H.units(d, WEEK, T + 6 * DAY, excluded_channels={5})).get(2, {})
        assert "voice_hours" not in (await H.units(d, WEEK, T + 6 * DAY, bot_ids={2})).get(1, {})
        await d.close()
    run(go())


def test_live_standings_skip_opted_out_and_fresh_members():
    async def go():
        d = await fresh()
        young = young_id(T)
        for uid, house in ((1, FERN), (2, KESTREL), (3, KESTREL), (young, OTTER)):
            await join(d, uid, house)
            await chat(d, uid, 4)
        await chat(d, 50, 7)  # no house: scores for nobody
        await d.execute("INSERT INTO privacy_optout (user_id, at) VALUES (3, ?)", (T,))
        current = await H.live(d, WEEK, T)
        s = by_house(current.standings)
        assert current.membership == {1: FERN, 2: KESTREL} and current.points == {1: 12, 2: 12}
        assert s[FERN].total == 12 and s[KESTREL].total == 12 and s[OTTER].total == 0
        assert s[KESTREL].members == 1 and s[OTTER].members == 0  # 3 and the fresh account aren't in a roster
        assert await H.standings(d, WEEK, T) == current.standings
        await d.close()
    run(go())


def test_standings_prefer_the_frozen_result_and_seasons_add_up():
    async def go():
        d = await fresh()
        await join(d, 1, FERN)
        await join(d, 2, KESTREL)
        await chat(d, 1, 4)
        frozen = H.score_houses({1: FERN, 2: KESTREL}, {2: 40}, WEEK.key)  # what was true when it closed
        await d.execute("INSERT INTO house_results (kind, period, season, winner, data, closed_at)"
                        " VALUES ('week', ?, '2026-Q4', ?, ?, ?)", (WEEK.key, KESTREL, H.dump_week(frozen, []), T))
        assert await H.standings(d, WEEK, T) == frozen  # not the live table, where Fern leads
        assert (await H.live(d, WEEK, T)).standings[0].house == FERN
        tally = await H.season_standings(d, "2026-Q4")
        assert (tally[0].house, tally[0].cup, tally[0].wins) == (KESTREL, 3, 1)
        assert all(t.cup == 0 for t in await H.season_standings(d, "2027-Q1"))  # a new season starts at zero
        # Once the season is closed its frozen trophy table wins over the weeks.
        closed = H.season_tally([frozen, frozen], "2026-Q4")
        await d.execute("INSERT INTO house_results (kind, period, season, winner, data, closed_at)"
                        " VALUES ('season', '2026-Q4', '2026-Q4', ?, ?, ?)", (KESTREL, H.dump_season(closed), T))
        assert await H.season_standings(d, "2026-Q4") == closed
        await d.close()
    run(go())
