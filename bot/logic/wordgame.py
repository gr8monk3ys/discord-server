"""Daily Word: one five-letter word a day (Pacific), six guesses, coloured-square feedback.
Pure: no Discord, no database. The cog stores each member's game in `word_games`.

The day's answer comes from the date alone: the answer list in a fixed order (sorted by a
seeded hash, so it doesn't depend on Python's RNG or the file's order), indexed by days since
EPOCH. Every restart, and every machine, agrees on the word.

Scoring is two-pass, so repeated letters are coloured the way players expect: greens first,
then yellows left to right only while the answer still has unmatched copies of that letter.
"""

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

from logic import quests as Q

LENGTH = 5
MAX_GUESSES = 6
EPOCH = date(2026, 10, 1)  # Daily Word #1
SEED = "front-desk/daily-word/v1"  # changing this reshuffles every future answer
NAME = "Daily Word"

BASE_COINS = 50
PER_SPARE = 10  # per unused guess on a win
REASON = "word"
DAY = 24 * 60 * 60

HIT, NEAR, MISS = "hit", "near", "miss"
SQUARES = {HIT: "🟩", NEAR: "🟨", MISS: "⬛"}
RANK = {MISS: 0, NEAR: 1, HIT: 2}
ALPHABET = "abcdefghijklmnopqrstuvwxyz"

WORDS_DIR = Path(__file__).resolve().parents[1] / "assets" / "words"


# ---------------------------------------------------------------- word lists
def load_words(path: Path) -> list[str]:
    """Lowercase a-z words of LENGTH letters, first occurrence order, duplicates dropped."""
    seen: dict[str, None] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        w = line.strip().lower()
        if len(w) == LENGTH and all(c in ALPHABET for c in w):
            seen.setdefault(w, None)
    return list(seen)


def order(answers: Iterable[str], seed: str = SEED) -> list[str]:
    """The fixed daily order: independent of input order and of Python's random module."""
    return sorted(set(answers), key=lambda w: hashlib.sha256(f"{seed}:{w}".encode()).hexdigest())


@dataclass(frozen=True)
class Words:
    answers: tuple[str, ...]  # in daily order
    allowed: frozenset[str]  # every accepted guess (includes the answers)

    @classmethod
    def load(cls, directory: Path = WORDS_DIR) -> "Words":
        answers = order(load_words(directory / "answers.txt"))
        allowed = frozenset(load_words(directory / "allowed.txt")) | frozenset(answers)
        return cls(tuple(answers), allowed)


def local_day(ts: int, tz: tzinfo) -> date:
    return datetime.fromtimestamp(ts, tz).date()


def puzzle_number(day: date) -> int:
    return (day - EPOCH).days + 1


def answer_for(day: date, answers: Sequence[str]) -> str:
    return answers[(day - EPOCH).days % len(answers)]


# ---------------------------------------------------------------- scoring
def score(guess: str, answer: str) -> tuple[str, ...]:
    marks = [MISS] * LENGTH
    left: Counter[str] = Counter()
    for i, (g, a) in enumerate(zip(guess, answer)):
        if g == a:
            marks[i] = HIT
        else:
            left[a] += 1
    for i, g in enumerate(guess):
        if marks[i] != HIT and left[g] > 0:
            marks[i] = NEAR
            left[g] -= 1
    return tuple(marks)


def row(marks: Iterable[str]) -> str:
    return "".join(SQUARES[m] for m in marks)


def grid(guesses: Sequence[str], answer: str) -> str:
    """Squares only, no letters: safe to post where others can see."""
    return "\n".join(row(score(g, answer)) for g in guesses)


def board_text(guesses: Sequence[str], answer: str) -> str:
    """The player's own view: squares plus the letters they guessed."""
    return "\n".join(f"{row(score(g, answer))}  `{g.upper()}`" for g in guesses)


def keyboard(guesses: Sequence[str], answer: str) -> dict[str, str]:
    """Best state seen for every guessed letter (green beats yellow beats gray)."""
    best: dict[str, str] = {}
    for g in guesses:
        for letter, mark in zip(g, score(g, answer)):
            if letter not in best or RANK[mark] > RANK[best[letter]]:
                best[letter] = mark
    return best


def keyboard_text(guesses: Sequence[str], answer: str) -> str:
    kb = keyboard(guesses, answer)
    lines = []
    for mark in (HIT, NEAR, MISS):
        letters = [c.upper() for c in ALPHABET if kb.get(c) == mark]
        if letters:
            lines.append(f"{SQUARES[mark]} {' '.join(letters)}")
    lines.append("Unused: " + " ".join(c.upper() for c in ALPHABET if c not in kb))
    return "\n".join(lines)


# ---------------------------------------------------------------- game state
def validate(raw: str, allowed: Iterable[str], guesses: Sequence[str]) -> tuple[str, str | None]:
    """(normalised guess, error). Errors never quote the input, so they need no escaping."""
    guess = raw.strip().lower()
    if len(guesses) >= MAX_GUESSES:
        return guess, "You've used all your guesses today. New word at midnight Pacific."
    if len(guess) != LENGTH or not all(c in ALPHABET for c in guess):
        return guess, f"Guesses are {LENGTH}-letter words, letters A to Z only."
    if guess not in allowed:
        return guess, "That's not in the word list. Try another word (it doesn't use up a guess)."
    if guess in guesses:
        return guess, "You already tried that one today."
    return guess, None


def solved(guesses: Sequence[str], answer: str) -> bool:
    return bool(guesses) and guesses[-1] == answer


def finished(guesses: Sequence[str], answer: str) -> bool:
    return solved(guesses, answer) or len(guesses) >= MAX_GUESSES


def parse_guesses(text: str | None) -> list[str]:
    return [g for g in (text or "").split(",") if g]


def join_guesses(guesses: Iterable[str]) -> str:
    return ",".join(guesses)


# ---------------------------------------------------------------- coins
def reward(won: bool, used: int) -> int:
    return BASE_COINS + PER_SPARE * (MAX_GUESSES - used) if won else 0


def ref(day: date, user_id: int) -> str:
    return f"word:{day.isoformat()}:{user_id}"


def payable(created_at: int | None, now: int) -> bool:
    """Fresh alt accounts aren't paid (same rule as the starter quest)."""
    return created_at is not None and now - created_at >= Q.MIN_ACCOUNT_DAYS * DAY


# ---------------------------------------------------------------- streaks and stats
@dataclass(frozen=True)
class Result:
    day: date
    solved: bool
    guesses: int


def current_streak(results: Iterable[Result], today: date) -> int:
    """Consecutive solved days ending today, or yesterday while today's game isn't finished."""
    by_day = {r.day: r.solved for r in results}
    day = today if today in by_day else today - timedelta(days=1)
    n = 0
    while by_day.get(day):
        n += 1
        day -= timedelta(days=1)
    return n


def max_streak(results: Iterable[Result], start: date | None = None, end: date | None = None) -> int:
    """Longest run of consecutive solved days within [start, end]."""
    days = sorted(r.day for r in results if r.solved and (start is None or r.day >= start)
                  and (end is None or r.day <= end))
    best = run = 0
    prev = None
    for d in days:
        run = run + 1 if prev is not None and d - prev == timedelta(days=1) else 1
        best = max(best, run)
        prev = d
    return best


@dataclass(frozen=True)
class Stats:
    played: int
    wins: int
    current: int
    best: int
    distribution: list[int]  # wins by guess count, index 0 = solved in 1

    @property
    def win_pct(self) -> int:
        return round(100 * self.wins / self.played) if self.played else 0


def stats(results: Sequence[Result], today: date) -> Stats:
    dist = [0] * MAX_GUESSES
    for r in results:
        if r.solved and 1 <= r.guesses <= MAX_GUESSES:
            dist[r.guesses - 1] += 1
    return Stats(played=len(results), wins=sum(1 for r in results if r.solved),
                 current=current_streak(results, today), best=max_streak(results), distribution=dist)


def stats_text(s: Stats, width: int = 12) -> str:
    head = f"Played **{s.played}** · Won **{s.win_pct}%** · Streak **{s.current}** (best **{s.best}**)"
    top = max(s.distribution) or 1
    bars = []
    for i, n in enumerate(s.distribution, start=1):
        bar = "█" * max(1, round(width * n / top)) if n else "▏"
        bars.append(f"{i} {bar} {n}")
    return head + "\n\n" + "\n".join(bars)


def month_start(day: date) -> date:
    return day.replace(day=1)


def leaderboard(by_user: Mapping[int, Iterable[Result]], start: date, end: date,
                limit: int = 10) -> list[tuple[int, int]]:
    """(user_id, best streak in [start, end]) for members with one, best first; ties go to
    more wins in the window, then the lower id (stable)."""
    rows = []
    for uid, results in by_user.items():
        results = list(results)
        best = max_streak(results, start, end)
        if best:
            wins = sum(1 for r in results if r.solved and start <= r.day <= end)
            rows.append((-best, -wins, uid))
    rows.sort()
    return [(uid, -b) for b, _, uid in rows[:limit]]


# ---------------------------------------------------------------- posts
def share_line(mention: str, number: int, guesses: Sequence[str], answer: str) -> str:
    """Spoiler-free result for the games channel: squares only, never letters."""
    if solved(guesses, answer):
        head = f"{mention} solved {NAME} #{number} in {len(guesses)}/{MAX_GUESSES}"
    else:
        head = f"{mention} played {NAME} #{number}: X/{MAX_GUESSES}"
    return f"{head}\n{grid(guesses, answer)}"
