"""Pure blackjack rules: hand values with soft aces, dealer play, outcomes, payouts."""

import random

import pytest

from logic import blackjack as bj
from logic.blackjack import Card, Game, Outcome


def hand(*ranks):
    return [Card(r) for r in ranks]


def stacked(*ranks):
    """A deck that deals `ranks` in order: player, dealer, player, dealer, then hits."""
    return [Card(r) for r in reversed(ranks)]


# ---------------------------------------------------------------- hand values
@pytest.mark.parametrize("ranks, value, soft", [
    (("2", "3"), 5, False),
    (("K", "Q"), 20, False),
    (("A", "6"), 17, True),
    (("A", "6", "10"), 17, False),  # the ace drops to 1
    (("A", "A"), 12, True),
    (("A", "A", "9"), 21, True),
    (("A", "A", "A", "8"), 21, True),
    (("A", "K"), 21, True),
    (("A", "5", "A", "K"), 17, False),
    (("K", "Q", "5"), 25, False),
    (("J", "A", "Q"), 21, False),
])
def test_hand_value(ranks, value, soft):
    assert bj.hand_value(hand(*ranks)) == (value, soft)


def test_blackjack_is_two_cards_only():
    assert bj.is_blackjack(hand("A", "K"))
    assert bj.is_blackjack(hand("10", "A"))
    assert not bj.is_blackjack(hand("7", "7", "7"))


def test_deck_is_52_unique_cards_and_seeded():
    deck = bj.new_deck(random.Random(1))
    assert len(deck) == 52 and len(set(deck)) == 52
    assert deck == bj.new_deck(random.Random(1))
    assert deck != bj.new_deck(random.Random(2))
    assert str(Card("10", "♥")) == "10♥"


# ---------------------------------------------------------------- dealer
def test_dealer_stands_on_soft_17_and_hits_16():
    assert not bj.dealer_should_hit(hand("A", "6"))
    assert not bj.dealer_should_hit(hand("10", "7"))
    assert bj.dealer_should_hit(hand("10", "6"))
    assert bj.dealer_should_hit(hand("A", "5"))  # soft 16


def test_stand_plays_out_the_dealer():
    # player 10,8 = 18; dealer 6,10 = 16 -> hits a 5 = 21
    game = Game.deal(100, deck=stacked("10", "6", "8", "10", "5"))
    assert not game.finished
    game.stand()
    assert [c.rank for c in game.dealer] == ["6", "10", "5"]
    assert game.result is Outcome.LOSE and game.payout == 0


def test_dealer_soft_17_stands_in_play():
    game = Game.deal(100, deck=stacked("10", "A", "8", "6", "2"))
    game.stand()
    assert len(game.dealer) == 2  # A,6 soft 17: no draw
    assert game.result is Outcome.WIN and game.payout == 200


def test_dealer_bust_pays_player():
    game = Game.deal(50, deck=stacked("10", "10", "2", "6", "K"))
    game.stand()
    assert bj.is_bust(game.dealer)
    assert game.result is Outcome.WIN and game.payout == 100


# ---------------------------------------------------------------- player
def test_hit_and_bust_loses_without_dealer_drawing():
    game = Game.deal(100, deck=stacked("10", "10", "6", "7", "K", "5"))
    game.hit()
    assert game.finished and game.result is Outcome.LOSE and game.payout == 0
    assert len(game.dealer) == 2


def test_hit_to_21_stands_automatically():
    game = Game.deal(100, deck=stacked("10", "10", "6", "8", "5"))
    game.hit()
    assert bj.total(game.player) == 21
    assert game.finished and game.result is Outcome.WIN


def test_soft_hand_hit_does_not_bust():
    game = Game.deal(100, deck=stacked("A", "10", "6", "9", "K"))
    game.hit()  # A,6,K = 17 hard
    assert not game.finished and bj.total(game.player) == 17


def test_push_refunds_the_bet():
    game = Game.deal(100, deck=stacked("10", "10", "8", "8"))
    game.stand()
    assert game.result is Outcome.PUSH and game.payout == 100


@pytest.mark.parametrize("bet, paid", [(100, 250), (10, 25), (11, 27), (15, 37), (5000, 12500)])
def test_blackjack_pays_3_to_2_rounded_down(bet, paid):
    game = Game.deal(bet, deck=stacked("A", "9", "K", "7"))
    assert game.finished and game.result is Outcome.BLACKJACK
    assert game.payout == paid


def test_dealer_blackjack_beats_player_and_both_push():
    game = Game.deal(100, deck=stacked("10", "A", "9", "K"))
    assert game.finished and game.result is Outcome.LOSE
    game = Game.deal(100, deck=stacked("A", "A", "K", "Q"))
    assert game.finished and game.result is Outcome.PUSH and game.payout == 100


def test_finished_game_ignores_more_moves():
    game = Game.deal(100, deck=stacked("10", "10", "6", "7", "K", "5"))
    game.hit()
    cards = list(game.player)
    game.hit()
    game.stand()
    assert game.player == cards and game.result is Outcome.LOSE


def test_idle_timeout_is_two_minutes():
    assert bj.IDLE_SECONDS == 120


def test_payout_table():
    assert bj.payout(Outcome.WIN, 40) == 80
    assert bj.payout(Outcome.PUSH, 40) == 40
    assert bj.payout(Outcome.LOSE, 40) == 0
    assert bj.payout(Outcome.BLACKJACK, 40) == 100


def test_random_games_never_pay_more_than_2_5x():
    rng = random.Random(5)
    for _ in range(2000):
        game = Game.deal(100, rng=rng)
        while not game.finished:
            game.hit() if bj.total(game.player) < 15 else game.stand()
        assert game.payout in (0, 100, 200, 250)
