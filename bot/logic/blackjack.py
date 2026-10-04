"""Module 4b: blackjack rules, no Discord. One fresh 52-card deck per hand, dealer
stands on every 17 (soft 17 included), blackjack pays 3:2 rounded down, a push
refunds the bet, no split or double. Randomness comes in through `rng`."""

import random
from dataclasses import dataclass, field
from enum import Enum

RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SUITS = ("♠", "♥", "♦", "♣")
DEALER_STANDS = 17
IDLE_SECONDS = 120  # a hand left alone this long is treated as Stand


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str = "♠"

    def __str__(self) -> str:
        return f"{self.rank}{self.suit}"


def new_deck(rng: random.Random) -> list[Card]:
    deck = [Card(r, s) for s in SUITS for r in RANKS]
    rng.shuffle(deck)
    return deck


def card_points(card: Card) -> int:
    if card.rank == "A":
        return 1
    if card.rank in ("J", "Q", "K"):
        return 10
    return int(card.rank)


def hand_value(cards) -> tuple[int, bool]:
    """(best total, soft). Soft means an ace is being counted as 11."""
    total = sum(card_points(c) for c in cards)
    if any(c.rank == "A" for c in cards) and total + 10 <= 21:
        return total + 10, True
    return total, False


def total(cards) -> int:
    return hand_value(cards)[0]


def is_blackjack(cards) -> bool:
    return len(cards) == 2 and total(cards) == 21


def is_bust(cards) -> bool:
    return total(cards) > 21


def dealer_should_hit(cards) -> bool:
    return total(cards) < DEALER_STANDS  # stands on soft 17 too


class Outcome(Enum):
    BLACKJACK = "blackjack"
    WIN = "win"
    PUSH = "push"
    LOSE = "lose"


def outcome(player, dealer) -> Outcome:
    if is_blackjack(player) and is_blackjack(dealer):
        return Outcome.PUSH
    if is_blackjack(player):
        return Outcome.BLACKJACK
    if is_blackjack(dealer) or is_bust(player):
        return Outcome.LOSE
    if is_bust(dealer) or total(player) > total(dealer):
        return Outcome.WIN
    if total(player) == total(dealer):
        return Outcome.PUSH
    return Outcome.LOSE


def payout(result: Outcome, bet: int) -> int:
    """Coins handed back (the bet was already taken): stake included."""
    if result is Outcome.BLACKJACK:
        return bet + bet * 3 // 2
    if result is Outcome.WIN:
        return 2 * bet
    if result is Outcome.PUSH:
        return bet
    return 0


@dataclass
class Game:
    bet: int
    deck: list[Card]
    player: list[Card] = field(default_factory=list)
    dealer: list[Card] = field(default_factory=list)
    result: Outcome | None = None

    @classmethod
    def deal(cls, bet: int, rng: random.Random | None = None, deck: list[Card] | None = None) -> "Game":
        """Deal player, dealer, player, dealer. `deck` is drawn from the end (pop)."""
        game = cls(bet, list(deck) if deck is not None else new_deck(rng or random.Random()))
        for _ in range(2):
            game.player.append(game.deck.pop())
            game.dealer.append(game.deck.pop())
        if is_blackjack(game.player) or is_blackjack(game.dealer):
            game.result = outcome(game.player, game.dealer)
        return game

    @property
    def finished(self) -> bool:
        return self.result is not None

    @property
    def payout(self) -> int:
        return payout(self.result, self.bet) if self.finished else 0

    def hit(self) -> None:
        if self.finished:
            return
        self.player.append(self.deck.pop())
        if is_bust(self.player):
            self.result = Outcome.LOSE
        elif total(self.player) == 21:
            self.stand()  # nothing better to do: play it out

    def stand(self) -> None:
        if self.finished:
            return
        while dealer_should_hit(self.dealer):
            self.dealer.append(self.deck.pop())
        self.result = outcome(self.player, self.dealer)
