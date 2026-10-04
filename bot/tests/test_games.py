"""Pure rules for module 4b: bets, slots, trivia and prediction payouts."""

import random
from fractions import Fraction

import pytest

from logic import games
from logic.games import (BetProblem, Click, PredictionRefusal, Stake, TriviaRound, bet_problem,
                         parse_trivia, split_pool)


# ---------------------------------------------------------------- bet bounds
@pytest.mark.parametrize("bet, balance, expected", [
    (9, 1000, BetProblem.TOO_SMALL),
    (10, 1000, None),
    (10, 10, None),
    (11, 10, BetProblem.BROKE),
    (5000, 5000, None),
    (5001, 10_000, BetProblem.TOO_BIG),
    (5000, 4999, BetProblem.BROKE),
    (0, 0, BetProblem.TOO_SMALL),
])
def test_bet_bounds(bet, balance, expected):
    assert bet_problem(bet, balance) is expected


def test_bet_cap_counts_an_existing_stake():
    assert bet_problem(1000, 10_000, already=4000) is None
    assert bet_problem(1001, 10_000, already=4000) is BetProblem.TOO_BIG


def test_bet_replies_mention_the_limits():
    assert "10" in games.bet_reply(BetProblem.TOO_SMALL, 0)
    assert "5,000" in games.bet_reply(BetProblem.TOO_BIG, 0)
    assert "42" in games.bet_reply(BetProblem.BROKE, 42)


# ---------------------------------------------------------------- slots
def test_slots_expected_return_is_exact_and_about_95_percent():
    rtp = games.expected_return()
    assert isinstance(rtp, Fraction)
    # Worked by hand: 24**3 = 13824 weighted outcomes.
    three = sum(w ** 3 * games.THREE_OF_A_KIND[s] for s, w in games.REEL)
    cherry = dict(games.REEL)[games.CHERRY]
    two_cherries = 3 * cherry ** 2 * (24 - cherry) * games.TWO_CHERRIES
    assert rtp == Fraction(three + two_cherries, 24 ** 3)
    assert 0.94 <= float(rtp) <= 0.96


def test_slots_paytable():
    assert games.multiplier(("7️⃣", "7️⃣", "7️⃣")) == 250
    assert games.multiplier(("🍒", "🍒", "🍒")) == 10
    assert games.multiplier(("🍒", "🍋", "🍒")) == 3
    assert games.multiplier(("🍋", "🍒", "🍒")) == 3
    assert games.multiplier(("🍒", "🍋", "🔔")) == 0
    assert games.multiplier(("🍋", "🍋", "🔔")) == 0
    assert games.slots_payout(("💎", "💎", "💎"), 20) == 1000
    assert games.slots_payout(("💎", "⭐", "💎"), 20) == 0


def test_slots_spin_uses_the_rng():
    assert games.spin(random.Random(7)) == games.spin(random.Random(7))
    assert all(s in games.SYMBOLS for s in games.spin(random.Random(1)))


def test_slots_million_spin_simulation_matches_expected_return():
    rng = random.Random(20261004)
    n = 1_000_000
    flat = rng.choices(games.SYMBOLS, cum_weights=games.CUM_WEIGHTS, k=3 * n)
    returned = sum(games.multiplier(flat[i:i + 3]) for i in range(0, 3 * n, 3))
    rtp = float(games.expected_return())
    assert abs(returned / n - rtp) <= 0.015


def test_spin_draws_match_simulation_draws():
    # The simulation above draws the same way spin() does.
    a = random.Random(3)
    b = random.Random(3)
    assert games.spin(a) == tuple(b.choices(games.SYMBOLS, cum_weights=games.CUM_WEIGHTS, k=3))


# ---------------------------------------------------------------- trivia
def payload(question="What&#039;s 2 + 2?", correct="4", wrong=("3", "5", "&quot;22&quot;"), code=0):
    return {"response_code": code, "results": [{
        "type": "multiple", "difficulty": "easy", "category": "Science &amp; Nature",
        "question": question, "correct_answer": correct, "incorrect_answers": list(wrong)}]}


def test_trivia_parse_unescapes_and_shuffles():
    q = parse_trivia(payload(), random.Random(1))
    assert q.text == "What's 2 + 2?"
    assert q.category == "Science & Nature"
    assert sorted(q.answers) == sorted(["4", "3", "5", '"22"'])
    assert q.correct_answer == "4"
    orders = {parse_trivia(payload(), random.Random(seed)).answers for seed in range(20)}
    assert len(orders) > 1  # really shuffled


@pytest.mark.parametrize("bad", [
    None, {}, {"response_code": 1, "results": []}, {"response_code": 0, "results": []},
    payload(code=5), payload(wrong=("only", "two")), {"response_code": 0, "results": [{"question": "x"}]},
    "not json",
])
def test_trivia_parse_rejects_bad_payloads(bad):
    assert parse_trivia(bad, random.Random(1)) is None


def test_trivia_url_with_category():
    assert games.trivia_url() == "https://opentdb.com/api.php?amount=1&type=multiple"
    assert games.trivia_url(15) == "https://opentdb.com/api.php?amount=1&type=multiple&category=15"
    assert games.TRIVIA_CATEGORIES["Video games"] == 15


def test_button_labels_are_cut_to_80():
    assert games.button_label("short") == "short"
    long = "x" * 200
    assert len(games.button_label(long)) == 80 and games.button_label(long).endswith("…")
    assert games.button_label("y" * 80) == "y" * 80


def round_with(correct_index=2):
    q = games.Question("q", "c", "easy", ("a", "b", "c", "d"), correct_index)
    return TriviaRound(q)


def test_trivia_one_click_per_person_and_first_correct_wins():
    r = round_with(2)
    assert r.click(1, 0) is Click.WRONG
    assert r.click(1, 2) is Click.ALREADY  # no second guess
    assert r.click(2, 2) is Click.WIN
    assert r.winner == 2 and r.closed
    assert r.click(3, 2) is Click.CLOSED  # too late


def test_trivia_closed_round_takes_no_clicks():
    r = round_with()
    r.close()
    assert r.click(1, 2) is Click.CLOSED and r.winner is None


# ---------------------------------------------------------------- predictions
def test_prediction_bet_rules():
    assert games.prediction_refusal("open", False, None, "a") is None
    assert games.prediction_refusal("open", False, "a", "a") is None  # topping up
    assert games.prediction_refusal("open", False, "a", "b") is PredictionRefusal.SWITCH
    assert games.prediction_refusal("open", True, None, "a") is PredictionRefusal.CREATOR
    for status in ("locked", "resolved", "cancelled"):
        assert games.prediction_refusal(status, False, None, "a") is PredictionRefusal.NOT_OPEN


def test_split_pool_proportional_with_remainder_to_biggest_stake():
    stakes = [Stake(1, "a", 100), Stake(2, "a", 200), Stake(3, "b", 100), Stake(4, "b", 33)]
    paid = split_pool(stakes, "a")
    # pool 433, backed 300: 1 -> 144 (144.33), 2 -> 288 (288.67), remainder 1 -> user 2
    assert paid == {1: 144, 2: 289}
    assert sum(paid.values()) == 433


def test_split_pool_remainder_tie_goes_to_earliest_bet():
    stakes = [Stake(5, "b", 50), Stake(1, "a", 100), Stake(2, "a", 100), Stake(3, "a", 100), Stake(4, "b", 51)]
    paid = split_pool(stakes, "a")
    # pool 401 over three equal stakes: 133 each, remainder 2 to the earliest (user 1)
    assert paid == {1: 135, 2: 133, 3: 133}
    assert sum(paid.values()) == 401


def test_split_pool_nobody_on_winning_side_refunds_everyone():
    stakes = [Stake(1, "a", 100), Stake(2, "a", 70)]
    assert split_pool(stakes, "b") == {1: 100, 2: 70}


def test_split_pool_single_winner_takes_everything():
    stakes = [Stake(1, "a", 10), Stake(2, "b", 990)]
    assert split_pool(stakes, "a") == {1: 1000}


def test_split_pool_no_bets_at_all():
    assert split_pool([], "a") == {}


def test_split_pool_always_pays_exactly_the_pool():
    rng = random.Random(9)
    for _ in range(500):
        stakes = [Stake(u, rng.choice("ab"), rng.randint(10, 5000)) for u in range(rng.randint(1, 12))]
        for winner in "ab":
            paid = split_pool(stakes, winner)
            assert sum(paid.values()) == sum(s.amount for s in stakes)
            assert all(v >= 0 for v in paid.values())


def test_refunds():
    assert games.refunds([Stake(1, "a", 10), Stake(2, "b", 20)]) == {1: 10, 2: 20}
