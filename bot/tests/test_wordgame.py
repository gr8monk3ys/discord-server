"""Pure rules for Daily Word (logic/wordgame.py)."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from logic import quests as Q
from logic import wordgame as W

TZ = ZoneInfo("America/Los_Angeles")
G, Y, B = W.HIT, W.NEAR, W.MISS
D0 = date(2026, 10, 7)


def marks(s: str) -> tuple:
    return tuple({"g": G, "y": Y, "b": B}[c] for c in s)


# ---------------------------------------------------------------- word lists
def test_shipped_lists_load_and_agree():
    words = W.Words.load()
    assert len(words.answers) > 1500
    assert len(words.allowed) > 8000
    assert all(len(w) == 5 and w.isascii() and w.isalpha() and w.islower() for w in words.allowed)
    assert set(words.answers) <= words.allowed
    assert len(set(words.answers)) == len(words.answers)
    for bad in ("bitch", "whore", "penis"):
        assert bad not in words.answers
    assert "crane" in words.allowed and "hello" in words.answers


def test_load_words_filters_junk(tmp_path):
    p = tmp_path / "w.txt"
    p.write_text("Crane\nhello\n\nab\ntoolong\nh3llo\ncafé!\n  stair  \nhello\n", encoding="utf-8")
    assert W.load_words(p) == ["crane", "hello", "stair"]


def test_order_is_a_fixed_shuffle_independent_of_input_order():
    words = ["apple", "crane", "stair", "hello", "pious", "zebra"]
    a = W.order(words)
    assert sorted(a) == sorted(words)
    assert a == W.order(list(reversed(words)))
    assert a == W.order(words + ["apple"])  # duplicates collapse
    assert W.order(words, seed="other") != a or len(words) < 3


def test_order_is_shuffled_not_alphabetical():
    words = W.Words.load()
    assert list(words.answers) != sorted(words.answers)


def test_puzzle_number_and_answer_are_deterministic():
    assert W.puzzle_number(W.EPOCH) == 1
    assert W.puzzle_number(W.EPOCH + timedelta(days=9)) == 10
    order = ["aaaaa", "bbbbb", "ccccc"]
    assert W.answer_for(W.EPOCH, order) == "aaaaa"
    assert W.answer_for(W.EPOCH + timedelta(days=1), order) == "bbbbb"
    assert W.answer_for(W.EPOCH + timedelta(days=3), order) == "aaaaa"  # cycles
    words = W.Words.load()
    assert W.answer_for(D0, words.answers) == W.answer_for(D0, W.Words.load().answers)  # restarts agree
    # no repeat for the whole cycle
    days = [W.EPOCH + timedelta(days=i) for i in range(len(words.answers))]
    assert len({W.answer_for(d, words.answers) for d in days}) == len(words.answers)


def test_daily_order_depends_on_a_secret_salt():
    """The answer list, SEED and EPOCH are public, so the daily order also mixes in a private
    salt: without it nobody can compute today's word from the repo."""
    answers = W.Words.load().answers
    days = [D0 + timedelta(days=i) for i in range(10)]
    a = W.daily_order(answers, "a" * 64)
    b = W.daily_order(answers, "b" * 64)
    assert sorted(a) == sorted(answers)
    assert [W.answer_for(d, a) for d in days] != [W.answer_for(d, b) for d in days]
    assert [W.answer_for(d, a) for d in days] != [W.answer_for(d, answers) for d in days]  # not the public order
    # stable for one salt and day (restarts agree), whatever order the list comes in
    assert W.answer_for(D0, W.daily_order(list(reversed(answers)), "a" * 64)) == W.answer_for(D0, a)
    with pytest.raises(ValueError):
        W.daily_order(answers, "")


def test_new_salt_is_random_hex():
    s1, s2 = W.new_salt(), W.new_salt()
    assert len(s1) == 64 and int(s1, 16) >= 0
    assert s1 != s2


def test_local_day_is_pacific():
    # 2026-10-08 06:30 UTC is still the 7th in Los Angeles (UTC-7).
    ts = int(datetime(2026, 10, 8, 6, 30, tzinfo=timezone.utc).timestamp())
    assert W.local_day(ts, TZ) == date(2026, 10, 7)
    ts = int(datetime(2026, 10, 8, 7, 30, tzinfo=timezone.utc).timestamp())
    assert W.local_day(ts, TZ) == date(2026, 10, 8)


# ---------------------------------------------------------------- scoring
@pytest.mark.parametrize("guess, answer, expected", [
    ("crane", "crane", "ggggg"),
    ("crane", "tipsy", "bbbbb"),
    ("react", "crane", "yygyb"),
    # repeated letter in the guess, once in the answer: only one is coloured
    ("speed", "abide", "bbyby"),   # first e yellow, second e gray (abide has one e)
    ("geese", "those", "bbbgg"),   # the s and final e are green, the other e's gray
    ("eerie", "ether", "gyybb"),   # e green, second e yellow (ether has 2 e's), last e gray
    ("lever", "eerie", "bgbyy"),
    # green takes priority over an earlier yellow for the same letter
    ("allay", "alloy", "gggbg"),
    ("llama", "hello", "yybbb"),
    ("sassy", "class", "yybgb"),
    ("mamma", "mommy", "gbggb"),
    ("abbey", "kebab", "yygyb"),
    ("tenet", "eaten", "yyygb"),
])
def test_score_two_pass(guess, answer, expected):
    assert W.score(guess, answer) == marks(expected), (guess, answer)


def test_score_worked_by_hand():
    # lever vs eerie: e(1) green; eerie still has e(0), r, i, e(4) unmatched, so e(3) and r(4)
    # are yellow; l and v are gray.
    assert W.score("lever", "eerie") == (B, G, B, Y, Y)
    # Three e's against an answer with three, one in place: green first, then two yellows.
    assert W.score("eeexy", "yxeee") == (Y, Y, G, Y, Y)
    # Three e's against an answer with one, not in place: only the first is yellow.
    assert W.score("eeexy", "abcde") == (Y, B, B, B, B)


def test_score_counts_never_exceed_answer_letters():
    words = ["eerie", "geese", "llama", "mamma", "sassy", "abbey", "kayak", "tenet", "otter", "fluff"]
    for guess in words:
        for answer in words:
            m = W.score(guess, answer)
            for letter in set(guess):
                coloured = sum(1 for g, k in zip(guess, m) if g == letter and k != B)
                assert coloured == min(guess.count(letter), answer.count(letter)), (guess, answer, letter)
            for i, (g, a) in enumerate(zip(guess, answer)):
                assert (m[i] == G) == (g == a)


def test_rows_and_grid_have_no_letters():
    assert W.row(marks("gybbb")) == "🟩🟨⬛⬛⬛"
    grid = W.grid(["crane", "stair"], "stair")
    assert grid == "\n".join([W.row(W.score("crane", "stair")), "🟩🟩🟩🟩🟩"])
    assert not any(c.isalpha() and c.isascii() for c in grid)


def test_keyboard_best_state_per_letter():
    kb = W.keyboard(["react", "stair"], "stair")
    # r: yellow in react, green in stair -> green
    assert kb["r"] == G and kb["a"] == G and kb["t"] == G and kb["s"] == G and kb["i"] == G
    assert kb["e"] == B and kb["c"] == B
    assert "z" not in kb
    # a letter gray in one spot (repeat) but yellow elsewhere stays yellow
    kb = W.keyboard(["speed"], "abide")
    assert kb["e"] == Y and kb["d"] == Y and kb["s"] == B


def test_keyboard_text_groups_letters():
    text = W.keyboard_text(["react"], "crane")
    assert "🟩 A" in text  # a is green in position 2
    assert "🟨 C E R" in text
    assert "⬛ T" in text
    assert "Unused:" in text and "Z" in text.split("Unused:")[1]
    assert "R" not in text.split("Unused:")[1]


def test_keyboard_text_empty():
    assert "Unused: A B C" in W.keyboard_text([], "crane")


# ---------------------------------------------------------------- validation and game state
def test_validate_guess():
    allowed = {"crane", "stair", "hello"}
    assert W.validate(" CRANE ", allowed, []) == ("crane", None)
    assert W.validate("cran", allowed, [])[1] is not None
    assert W.validate("cranes", allowed, [])[1] is not None
    assert W.validate("cr4ne", allowed, [])[1] is not None
    assert W.validate("zzzzz", allowed, [])[1] is not None
    assert W.validate("crane", allowed, ["crane"])[1] is not None  # already guessed
    assert W.validate("stair", allowed, ["crane"] * 6)[1] is not None  # out of guesses
    assert W.validate("stair", allowed, ["crane"]) == ("stair", None)


def test_validate_errors_never_echo_markdown():
    # The error text quotes nothing the member typed, so it needs no escaping.
    err = W.validate("**@everyone**", {"crane"}, [])[1]
    assert "@" not in err and "*" not in err


def test_finished_and_solved():
    assert not W.finished([], "crane")
    assert W.finished(["crane"], "crane") and W.solved(["crane"], "crane")
    five = ["stair"] * 5
    assert not W.finished(five, "crane")
    assert W.finished(five + ["hello"], "crane") and not W.solved(five + ["hello"], "crane")
    assert W.solved(five + ["crane"], "crane")


def test_guess_string_round_trip():
    assert W.parse_guesses("") == []
    assert W.parse_guesses("crane,stair") == ["crane", "stair"]
    assert W.join_guesses(["crane", "stair"]) == "crane,stair"


# ---------------------------------------------------------------- coins
def test_reward():
    assert W.reward(True, 1) == 100
    assert W.reward(True, 4) == 70
    assert W.reward(True, 6) == 50
    assert W.reward(False, 6) == 0
    assert W.ref(D0, 42) == "word:2026-10-07:42"
    assert W.REASON == "word"


def test_young_accounts_are_not_paid():
    now = 1_790_000_000
    day = 86400
    assert W.payable(now - Q.MIN_ACCOUNT_DAYS * day, now)
    assert not W.payable(now - Q.MIN_ACCOUNT_DAYS * day + 1, now)
    assert not W.payable(None, now)


# ---------------------------------------------------------------- streaks and stats
def R(offset, solved, n=4):
    return W.Result(D0 + timedelta(days=offset), solved, n)


def test_current_streak():
    assert W.current_streak([], D0) == 0
    assert W.current_streak([R(0, True)], D0) == 1
    assert W.current_streak([R(-2, True), R(-1, True), R(0, True)], D0) == 3
    # today not played yet: the streak through yesterday still counts
    assert W.current_streak([R(-2, True), R(-1, True)], D0) == 2
    # a gap breaks it
    assert W.current_streak([R(-3, True), R(-1, True)], D0) == 1
    assert W.current_streak([R(-3, True), R(-2, True)], D0) == 0
    # a loss breaks it
    assert W.current_streak([R(-2, True), R(-1, False)], D0) == 0
    assert W.current_streak([R(-1, True), R(0, False)], D0) == 0
    # order doesn't matter
    assert W.current_streak([R(0, True), R(-2, True), R(-1, True)], D0) == 3


def test_max_streak_and_window():
    rs = [R(-10, True), R(-9, True), R(-8, False), R(-7, True), R(-6, True), R(-5, True), R(-3, True)]
    assert W.max_streak(rs) == 3
    assert W.max_streak([]) == 0
    # only days inside [start, end] count
    assert W.max_streak(rs, start=D0 + timedelta(days=-6)) == 2
    assert W.max_streak(rs, end=D0 + timedelta(days=-9)) == 2


def test_stats():
    rs = [R(-3, True, 3), R(-2, False, 6), R(-1, True, 4), R(0, True, 4)]
    s = W.stats(rs, D0)
    assert s.played == 4 and s.wins == 3
    assert s.win_pct == 75
    assert s.current == 2 and s.best == 2
    assert s.distribution == [0, 0, 1, 2, 0, 0]
    empty = W.stats([], D0)
    assert empty.played == 0 and empty.win_pct == 0 and empty.distribution == [0] * 6


def test_stats_text_has_bars():
    text = W.stats_text(W.stats([R(0, True, 3)], D0))
    assert "Played **1**" in text and "100%" in text
    assert "3 " in text and "█" in text


def test_month_start():
    assert W.month_start(date(2026, 10, 7)) == date(2026, 10, 1)


def test_leaderboard_ranks_best_streak_then_wins():
    month = date(2026, 10, 1)
    by_user = {
        1: [W.Result(date(2026, 10, d), True, 4) for d in (1, 2, 3)],
        2: [W.Result(date(2026, 10, d), True, 4) for d in (1, 2, 3, 5)],
        3: [W.Result(date(2026, 9, 30), True, 4), W.Result(date(2026, 10, 1), True, 4)],  # Sept doesn't count
        4: [W.Result(date(2026, 10, 1), False, 6)],
    }
    board = W.leaderboard(by_user, month, date(2026, 10, 7))
    assert board == [(2, 3), (1, 3), (3, 1)]


# ---------------------------------------------------------------- share line
def test_share_line_win_and_loss_have_no_letters():
    text = W.share_line("<@5>", 7, ["crane", "stair"], "stair")
    assert text.startswith("<@5> solved Daily Word #7 in 2/6")
    assert "crane" not in text.lower() and "stair" not in text.lower()
    assert W.grid(["crane", "stair"], "stair") in text
    lost = W.share_line("<@5>", 7, ["crane"] * 6, "stair")
    assert "X/6" in lost and "stair" not in lost.lower() and "crane" not in lost.lower()


def test_board_text_shows_letters_for_the_player():
    text = W.board_text(["crane"], "stair")
    assert "CRANE" in text and W.row(W.score("crane", "stair")) in text
