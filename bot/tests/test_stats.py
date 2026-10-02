from logic import stats
from logic.stats import Ranked, Session

A, B, C, BOT = 1, 2, 3, 99
VC1, VC2, AFK = 100, 200, 300
NOW = 10_000


def v(user, start, end, channel=VC1):
    return Session(user, channel, start, end)


def voice(sessions, ws=0, we=NOW, now=NOW, **kw):
    return stats.counted_voice_seconds(sessions, ws, we, now, **kw)


# --- clip ---


def test_clip_inside_and_outside_window():
    assert stats.clip(v(A, 10, 20), 0, 100, NOW) == (10, 20)
    assert stats.clip(v(A, 10, 20), 15, 100, NOW) == (15, 20)
    assert stats.clip(v(A, 10, 20), 0, 12, NOW) == (10, 12)
    assert stats.clip(v(A, 10, 20), 20, 100, NOW) is None
    assert stats.clip(v(A, 10, 20), 0, 10, NOW) is None


def test_clip_open_session_ends_at_now():
    assert stats.clip(v(A, 10, None), 0, 100, 50) == (10, 50)
    assert stats.clip(v(A, 60, None), 0, 100, 50) is None


# --- counted voice ---


def test_alone_in_channel_counts_zero():
    assert voice([v(A, 0, 1000)]) == {}


def test_partial_overlap_counts_only_overlap_for_both():
    assert voice([v(A, 0, 100), v(B, 60, 300)]) == {A: 40, B: 40}


def test_three_users_one_leaves_remaining_two_keep_counting():
    sessions = [v(A, 0, 300), v(B, 0, 300), v(C, 0, 100)]
    assert voice(sessions) == {A: 300, B: 300, C: 100}


def test_last_two_down_to_one_stops_counting():
    sessions = [v(A, 0, 300), v(B, 0, 200), v(C, 0, 100)]
    assert voice(sessions) == {A: 200, B: 200, C: 100}


def test_excluded_channel_ignored():
    sessions = [v(A, 0, 100, AFK), v(B, 0, 100, AFK)]
    assert voice(sessions, excluded_channels={AFK}) == {}
    assert voice(sessions) == {A: 100, B: 100}


def test_bot_does_not_make_you_not_alone():
    sessions = [v(A, 0, 100), v(BOT, 0, 100)]
    assert voice(sessions, bot_ids={BOT}) == {}
    # With a real second person, the bot still earns nothing.
    sessions.append(v(B, 50, 100))
    assert voice(sessions, bot_ids={BOT}) == {A: 50, B: 50}


def test_users_in_different_channels_are_alone():
    assert voice([v(A, 0, 100, VC1), v(B, 0, 100, VC2)]) == {}


def test_user_hopping_channels():
    sessions = [
        v(A, 0, 100, VC1),
        v(A, 100, 200, VC2),
        v(B, 0, 200, VC1),
        v(C, 0, 200, VC2),
    ]
    assert voice(sessions) == {A: 200, B: 100, C: 100}


def test_duplicate_overlapping_rows_not_double_counted():
    sessions = [v(A, 0, 100), v(A, 50, 150), v(A, 0, 100), v(B, 0, 200)]
    assert voice(sessions) == {A: 150, B: 150}


def test_duplicate_rows_alone_still_alone():
    assert voice([v(A, 0, 100), v(A, 0, 100)]) == {}


def test_open_sessions_clipped_at_now():
    assert voice([v(A, 0, None), v(B, 100, None)], now=500, we=1000) == {A: 400, B: 400}


def test_window_clipping_both_sides():
    sessions = [v(A, 0, 1000), v(B, 0, 1000)]
    assert voice(sessions, ws=200, we=700) == {A: 500, B: 500}


def test_session_entirely_outside_window():
    sessions = [v(A, 0, 100), v(B, 0, 100)]
    assert voice(sessions, ws=100, we=200) == {}


# --- games ---


def g(user, game, start, end):
    return Session(user, game, start, end)


def test_game_seconds_basic_and_min_length():
    sessions = [g(A, "Valorant", 0, 600), g(A, "Chess", 0, 299), g(B, "Chess", 0, 300)]
    assert stats.game_seconds(sessions, 0, NOW, NOW) == {
        A: {"Valorant": 600},
        B: {"Chess": 300},
    }


def test_game_min_length_uses_full_not_clipped_length():
    # 1000s session, only 100s inside the window: still counts.
    assert stats.game_seconds([g(A, "X", 0, 1000)], 900, NOW, NOW) == {A: {"X": 100}}
    # Short session fully inside: ignored.
    assert stats.game_seconds([g(A, "X", 950, 1000)], 900, NOW, NOW) == {}


def test_game_open_session_uses_now():
    assert stats.game_seconds([g(A, "X", 0, None)], 0, NOW, 400) == {A: {"X": 400}}
    assert stats.game_seconds([g(A, "X", 0, None)], 0, NOW, 200) == {}


def test_game_overlaps_not_double_counted():
    sessions = [g(A, "X", 0, 600), g(A, "X", 300, 900), g(A, "Y", 0, 600)]
    assert stats.game_seconds(sessions, 0, NOW, NOW) == {A: {"X": 900, "Y": 600}}


def test_gaming_totals():
    assert stats.gaming_totals({A: {"X": 100, "Y": 50}, B: {"X": 7}}) == {A: 150, B: 7}


def test_top_games_ordering():
    per_game = {"b": 100, "a": 100, "c": 300, "d": 50}
    assert stats.top_games(per_game) == [("c", 300), ("a", 100), ("b", 100)]
    assert stats.top_games(per_game, n=1) == [("c", 300)]
    assert stats.top_games({}) == []


# --- rank ---


def test_rank_competition_ties():
    top, mine = stats.rank({10: 50, 11: 40, 12: 40, 13: 30})
    assert top == [Ranked(1, 10, 50), Ranked(2, 11, 40), Ranked(2, 12, 40), Ranked(4, 13, 30)]
    assert mine is None


def test_rank_tie_display_order_by_user_id():
    top, _ = stats.rank({12: 5, 11: 5})
    assert [r.user_id for r in top] == [11, 12]
    assert {r.rank for r in top} == {1}


def test_rank_limit_and_me():
    scores = {u: 100 - u for u in range(1, 21)}
    top, mine = stats.rank(scores, limit=10, me=15)
    assert len(top) == 10 and top[-1] == Ranked(10, 10, 90)
    assert mine == Ranked(15, 15, 85)
    # Inside the top list: no separate entry.
    assert stats.rank(scores, limit=10, me=3)[1] is None
    # Not ranked at all.
    assert stats.rank(scores, limit=10, me=999)[1] is None


def test_rank_excludes_zero_and_negative():
    top, mine = stats.rank({1: 0, 2: 5, 3: -1}, me=1)
    assert top == [Ranked(1, 2, 5)]
    assert mine is None


def test_rank_empty():
    assert stats.rank({}) == ([], None)


# --- mvp ---


def test_mvp_normal():
    boards = {
        "voice": {A: 100, B: 50, C: 10},
        "messages": {A: 30, B: 40, C: 1},
    }
    assert stats.mvp(boards) == A  # A: 1+2=3, B: 2+1=3 -> tie -> voice A


def test_mvp_lowest_rank_sum():
    boards = {
        "voice": {A: 100, B: 50},
        "messages": {B: 40, A: 10},
        "gaming": {B: 5, A: 1},
    }
    assert stats.mvp(boards) == B  # A: 1+2+2=5, B: 2+1+1=4


def test_mvp_absent_from_board_penalty():
    boards = {
        "voice": {A: 100, B: 90},
        "messages": {B: 5, C: 4},
        "gaming": {B: 1},
    }
    # A: 1 + 3 + 2 = 6; B: 2 + 1 + 1 = 4; C: 3 + 2 + 2 = 7
    assert stats.mvp(boards) == B


def test_mvp_zero_score_counts_as_absent():
    boards = {"voice": {A: 10, B: 0}, "messages": {B: 0}}
    assert stats.mvp(boards) == A


def test_mvp_tiebreak_lowest_user_id():
    boards = {"voice": {A: 10, B: 10}}
    assert stats.mvp(boards) == A
    boards = {"voice": {}, "messages": {B: 5, C: 5}}
    assert stats.mvp(boards) == B


def test_mvp_custom_tiebreak_board():
    boards = {"voice": {A: 10, B: 5}, "messages": {B: 9, A: 1}}
    assert stats.mvp(boards) == A
    assert stats.mvp(boards, tiebreak_board="messages") == B


def test_mvp_empty():
    assert stats.mvp({}) is None
    assert stats.mvp({"voice": {}, "messages": {A: 0}}) is None


# --- fmt_duration ---


def test_fmt_duration():
    assert stats.fmt_duration(0) == "0m"
    assert stats.fmt_duration(59) == "0m"
    assert stats.fmt_duration(61) == "1m"
    assert stats.fmt_duration(3599) == "59m"
    assert stats.fmt_duration(3600) == "1h 0m"
    assert stats.fmt_duration(90061) == "25h 1m"
