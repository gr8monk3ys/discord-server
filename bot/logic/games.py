"""Module 4b: rules for /slots, /trivia and predictions, no Discord. Every random
choice takes an `rng` so tests can seed it."""

import html
import random
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from itertools import accumulate, product
from urllib.parse import urlencode

# ---------------------------------------------------------------- bets
MIN_BET = 10
MAX_BET = 5000


class BetProblem(Enum):
    TOO_SMALL = "too_small"
    TOO_BIG = "too_big"
    BROKE = "broke"


def bet_problem(bet: int, balance: int, already: int = 0) -> BetProblem | None:
    """10 <= bet <= min(balance, 5000). `already` is a stake already placed on the
    same thing (a prediction top-up), which counts toward the 5000 cap."""
    if bet < MIN_BET:
        return BetProblem.TOO_SMALL
    if bet + already > MAX_BET:
        return BetProblem.TOO_BIG
    if bet > balance:
        return BetProblem.BROKE
    return None


def bet_reply(problem: BetProblem, balance: int, already: int = 0) -> str:
    if problem is BetProblem.TOO_SMALL:
        return f"The smallest bet is {MIN_BET} coins."
    if problem is BetProblem.TOO_BIG:
        if already:
            return f"You can stake at most {MAX_BET:,} coins in total, and you've already put in {already:,}."
        return f"The biggest bet is {MAX_BET:,} coins."
    return f"You only have {balance:,} coins."


# ---------------------------------------------------------------- slots
# One reel, used three times. Weights sum to 24, so there are 24**3 = 13,824 equally
# likely weighted outcomes and the return is computed exactly in expected_return().
REEL = (("🍒", 5), ("🍋", 6), ("🔔", 5), ("⭐", 4), ("💎", 3), ("7️⃣", 1))
SYMBOLS = tuple(s for s, _ in REEL)
WEIGHTS = tuple(w for _, w in REEL)
CUM_WEIGHTS = tuple(accumulate(WEIGHTS))
CHERRY = "🍒"
THREE_OF_A_KIND = {"🍒": 10, "🍋": 12, "🔔": 15, "⭐": 25, "💎": 50, "7️⃣": 250}
TWO_CHERRIES = 3  # exactly two cherries anywhere
TARGET_RTP = 0.95


def spin(rng: random.Random) -> tuple[str, str, str]:
    return tuple(rng.choices(SYMBOLS, cum_weights=CUM_WEIGHTS, k=3))


def multiplier(reels) -> int:
    """Payout as a multiple of the bet (0 = lose). Always a whole number."""
    a, b, c = reels
    if a == b == c:
        return THREE_OF_A_KIND[a]
    if list(reels).count(CHERRY) == 2:
        return TWO_CHERRIES
    return 0


def slots_payout(reels, bet: int) -> int:
    """Coins handed back after the bet was taken (stake included)."""
    return bet * multiplier(reels)


def expected_return() -> Fraction:
    """Exact return to player per coin bet, over the whole reel distribution."""
    total = sum(WEIGHTS) ** 3
    weight = dict(REEL)
    return sum((Fraction(weight[a] * weight[b] * weight[c], total) * multiplier((a, b, c))
                for a, b, c in product(SYMBOLS, repeat=3)), Fraction(0))


# ---------------------------------------------------------------- trivia
TRIVIA_URL = "https://opentdb.com/api.php?amount=1&type=multiple"
TRIVIA_PRIZE = 50
TRIVIA_DAILY_CAP = 250  # coins per member per local day: stops farming with alts
TRIVIA_SECONDS = 20
LABEL_MAX = 80
TRIVIA_CATEGORIES = {  # name shown in /trivia -> Open Trivia DB category id
    "General knowledge": 9,
    "Film": 11,
    "Music": 12,
    "Video games": 15,
    "Science & nature": 17,
    "Sports": 21,
    "Geography": 22,
    "History": 23,
    "Anime & manga": 31,
}


def trivia_url(category_id: int | None = None) -> str:
    if category_id is None:
        return TRIVIA_URL
    return f"{TRIVIA_URL}&{urlencode({'category': category_id})}"


def trivia_payout(earned_today: int) -> int:
    """The prize, or 0 once it would take today's trivia winnings past the cap."""
    return TRIVIA_PRIZE if earned_today + TRIVIA_PRIZE <= TRIVIA_DAILY_CAP else 0


@dataclass(frozen=True)
class Question:
    text: str
    category: str
    difficulty: str
    answers: tuple[str, ...]  # shuffled, unescaped, full length
    correct: int  # index into answers

    @property
    def correct_answer(self) -> str:
        return self.answers[self.correct]


def parse_trivia(payload, rng: random.Random) -> Question | None:
    """The first question of an Open Trivia DB reply, or None if it's unusable."""
    try:
        if payload.get("response_code") != 0:
            return None
        item = payload["results"][0]
        correct = html.unescape(item["correct_answer"]).strip()
        wrong = [html.unescape(w).strip() for w in item["incorrect_answers"]]
        text = html.unescape(item["question"]).strip()
    except (AttributeError, KeyError, IndexError, TypeError):
        return None
    if not text or not correct or len(wrong) != 3 or not all(wrong):
        return None
    answers = [correct, *wrong]
    rng.shuffle(answers)
    return Question(
        text=text,
        category=html.unescape(str(item.get("category", ""))),
        difficulty=str(item.get("difficulty", "")),
        answers=tuple(answers),
        correct=answers.index(correct),
    )


def button_label(text: str, limit: int = LABEL_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Click(Enum):
    WIN = "win"
    WRONG = "wrong"
    ALREADY = "already"  # this person already used their one click
    CLOSED = "closed"  # someone already won, or time ran out


@dataclass
class TriviaRound:
    question: Question
    clicked: set[int] = field(default_factory=set)
    winner: int | None = None
    closed: bool = False

    def click(self, user_id: int, index: int) -> Click:
        if self.closed:
            return Click.CLOSED
        if user_id in self.clicked:
            return Click.ALREADY
        self.clicked.add(user_id)
        if index != self.question.correct:
            return Click.WRONG
        self.winner = user_id
        self.closed = True
        return Click.WIN

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------- predictions
QUESTION_MAX = 200
OPTION_MAX = 60
OPTIONS = ("a", "b")


class PredictionRefusal(Enum):
    NOT_OPEN = "not_open"
    CREATOR = "creator"
    SWITCH = "switch"


def prediction_refusal(status: str, is_creator: bool, existing_option: str | None,
                       option: str) -> PredictionRefusal | None:
    """Who may bet on what. Amount bounds are checked separately (bet_problem)."""
    if status != "open":
        return PredictionRefusal.NOT_OPEN
    if is_creator:
        return PredictionRefusal.CREATOR
    if existing_option is not None and existing_option != option:
        return PredictionRefusal.SWITCH
    return None


@dataclass(frozen=True)
class Stake:
    user_id: int
    option: str
    amount: int


def split_pool(stakes, winner: str) -> dict[int, int]:
    """Who gets what when `winner` wins. `stakes` must be in bet order (earliest first).

    Winners split the whole pool in proportion to their stakes, rounded down; the
    rounding remainder goes to the biggest winning stake (ties: earliest bet). If
    nobody backed the winner, everyone gets their stake back."""
    stakes = list(stakes)
    winners = [s for s in stakes if s.option == winner]
    if not winners:
        return refunds(stakes)
    pool = sum(s.amount for s in stakes)
    backed = sum(s.amount for s in winners)
    paid = {s.user_id: pool * s.amount // backed for s in winners}
    remainder = pool - sum(paid.values())
    biggest = max(winners, key=lambda s: s.amount)  # max keeps the first of equals: earliest
    paid[biggest.user_id] += remainder
    return paid


def refunds(stakes) -> dict[int, int]:
    return {s.user_id: s.amount for s in stakes}
