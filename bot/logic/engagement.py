"""Module 9 rules: engagement autopilot. Pure: no Discord calls, no database.

- `Daily` / `plan_daily`: the daily twin of logic/schedule.py's weekly planner, with the same
  rules (run only the latest due period, the very first run only marks it done, the caller
  marks a run done only after it worked).
- Question and poll banks: loading, validating and picking with no repeats until exhausted.
- Counting: what a message in the counting channel does to the count.
- Birthdays: validation, Feb 29 handling, who is celebrated on a day, the next few.
- Auto game night: the Friday-evening window and which game to pick.
"""

import random
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path

import config

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
QOTD_FILE = DATA_DIR / "qotd.txt"
POLLS_FILE = DATA_DIR / "polls.txt"

POLL_ANSWER_MAX = 55  # Discord's limit for a poll answer
QUESTION_MAX = 300  # Discord's limit for a poll question; questions stay well under it


# ---------------------------------------------------------------- daily schedule
@dataclass(frozen=True)
class Daily:
    name: str  # e.g. "qotd"
    hour: int
    minute: int


@dataclass(frozen=True)
class DailyPeriod:
    key: str  # f"{name}:{local date iso}"
    day: date  # the local date it belongs to
    scheduled_at: int  # unix seconds


@dataclass(frozen=True)
class DailyPlan:
    run: DailyPeriod | None  # the one period to run now
    mark_done: list[DailyPeriod]  # record as done without running


def daily_occurrence(job: Daily, local_date: date, tz: tzinfo) -> DailyPeriod:
    at = int(datetime.combine(local_date, time(job.hour, job.minute), tzinfo=tz).timestamp())
    return DailyPeriod(f"{job.name}:{local_date.isoformat()}", local_date, at)


def latest_due_daily(job: Daily, now: int, tz: tzinfo) -> DailyPeriod:
    today = datetime.fromtimestamp(now, tz).date()
    p = daily_occurrence(job, today, tz)
    return p if p.scheduled_at <= now else daily_occurrence(job, today - timedelta(days=1), tz)


def plan_daily(job: Daily, now: int, tz: tzinfo, done_keys: set[str], first_seen: int | None) -> DailyPlan:
    """Same contract as schedule.plan, for a job that runs every local day.

    - The latest due period L already done: nothing.
    - First startup (first_seen None) or L predates the bot: don't run, mark L done.
    - Otherwise run L, and mark done the missed days between first_seen and L (oldest first).
    """
    latest = latest_due_daily(job, now, tz)
    if latest.key in done_keys:
        return DailyPlan(None, [])
    if first_seen is None or latest.scheduled_at < first_seen:
        return DailyPlan(None, [latest])
    missed: list[DailyPeriod] = []
    day = latest.day - timedelta(days=1)
    while True:
        p = daily_occurrence(job, day, tz)
        if p.scheduled_at < first_seen:
            break
        if p.key not in done_keys:
            missed.append(p)
        day -= timedelta(days=1)
    return DailyPlan(latest, missed[::-1])


# ---------------------------------------------------------------- banks
def read_lines(text: str) -> list[str]:
    """Non-empty lines, stripped; lines starting with '#' are comments."""
    return [s for line in text.splitlines() if (s := line.strip()) and not s.startswith("#")]


def load_questions(path: Path = QOTD_FILE) -> list[str]:
    return read_lines(path.read_text(encoding="utf-8"))


def parse_pair(line: str) -> tuple[str, str] | None:
    """'Pizza | Tacos' -> ('Pizza', 'Tacos'); None if it isn't exactly two non-empty sides."""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) != 2 or not all(parts) or parts[0].lower() == parts[1].lower():
        return None
    return parts[0], parts[1]


def load_pairs(path: Path = POLLS_FILE) -> list[tuple[str, str]]:
    """Every valid pair, in file order (a bad line is skipped, so ids stay stable per file)."""
    out = []
    for line in read_lines(path.read_text(encoding="utf-8")):
        pair = parse_pair(line)
        if pair is not None and all(len(side) <= POLL_ANSWER_MAX for side in pair):
            out.append(pair)
    return out


@dataclass(frozen=True)
class Pick:
    index: int
    reset: bool  # the bank was exhausted: forget every used id, then record this one


def pick_next(size: int, used: Iterable[int], rng: random.Random, last: int | None = None) -> Pick | None:
    """A random unused index in range(size). When all are used, reshuffle: start over,
    avoiding `last` (the most recent pick) so the same item never runs twice in a row."""
    if size <= 0:
        return None
    used_set = {u for u in used if 0 <= u < size}
    fresh = [i for i in range(size) if i not in used_set]
    if fresh:
        return Pick(rng.choice(fresh), False)
    pool = [i for i in range(size) if i != last] or list(range(size))
    return Pick(rng.choice(pool), True)


# ---------------------------------------------------------------- counting
_PLAIN_INT = re.compile(r"[0-9]{1,15}")  # ASCII only: \d would accept other scripts' digits


def parse_count(text: str | None) -> int | None:
    """The number in a message that is just a plain integer ('42'); None for anything else."""
    if text is None:
        return None
    s = text.strip()
    return int(s) if _PLAIN_INT.fullmatch(s) else None


@dataclass(frozen=True)
class CountState:
    current: int = 0
    last_user: int | None = None
    best: int = 0


@dataclass(frozen=True)
class Step:
    kind: str  # "ok", "wrong" (not the next number) or "double" (same person twice in a row)
    state: CountState
    broke_at: int  # the count before this message (what got reset on a failure)
    new_best: bool = False  # this count set a new best run

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def count_step(state: CountState, user_id: int, n: int) -> Step:
    if state.last_user is not None and user_id == state.last_user:
        return Step("double", CountState(0, None, state.best), state.current)
    if n != state.current + 1:
        return Step("wrong", CountState(0, None, state.best), state.current)
    new_best = n > state.best
    return Step("ok", CountState(n, user_id, max(n, state.best)), state.current, new_best)


def champion(contributions: Mapping[int, int], current: int | None = None) -> int | None:
    """Who counted most in the run. A tie keeps the current champion, else the lowest id."""
    if not contributions:
        return None
    top = max(contributions.values())
    if top <= 0:
        return None
    tied = sorted(u for u, n in contributions.items() if n == top)
    return current if current in tied else tied[0]


# ---------------------------------------------------------------- birthdays
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)  # Feb 29 is a valid birthday


def valid_birthday(month: int, day: int) -> bool:
    return 1 <= month <= 12 and 1 <= day <= DAYS_IN_MONTH[month - 1]


def is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def celebrated_on(month: int, day: int, year: int) -> date:
    """The date a birthday is celebrated in `year`: Feb 29 moves to Feb 28 in other years."""
    if month == 2 and day == 29 and not is_leap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def birthday_text(month: int, day: int) -> str:
    return f"{MONTHS[month - 1]} {day}"


def birthdays_on(rows: Iterable[tuple[int, int, int]], local_date: date) -> list[int]:
    """User ids celebrated on `local_date`, from (user_id, month, day) rows."""
    return sorted(u for u, m, d in rows
                  if valid_birthday(m, d) and celebrated_on(m, d, local_date.year) == local_date)


def upcoming(rows: Iterable[tuple[int, int, int]], today: date, n: int = 5) -> list[tuple[int, date]]:
    """The next `n` birthdays from `today` (today included): (user_id, date), soonest first."""
    out = []
    for u, m, d in rows:
        if not valid_birthday(m, d):
            continue
        when = celebrated_on(m, d, today.year)
        if when < today:
            when = celebrated_on(m, d, today.year + 1)
        out.append((when, u))
    out.sort()
    return [(u, when) for when, u in out[:n]]


# ---------------------------------------------------------------- auto game night
GAMENIGHT_HOUR = 21  # Friday 21:00 local
EVENING_FROM = time(17, 0)  # a game night "that Friday evening" starts between 17:00 Friday
EVENING_UNTIL = time(3, 0)  # and 03:00 Saturday
TOO_LATE = 30 * 60  # don't create the event less than this long before it starts


def friday_evening(friday: date, tz: tzinfo) -> tuple[int, int, int]:
    """(evening_start, evening_end, gamenight_start) as unix seconds for that local Friday."""
    def at(d: date, t: time) -> int:
        return int(datetime.combine(d, t, tzinfo=tz).timestamp())
    return (at(friday, EVENING_FROM), at(friday + timedelta(days=1), EVENING_UNTIL),
            at(friday, time(GAMENIGHT_HOUR, 0)))


def game_for(name: str, games: Iterable[config.Game] = config.GAMES) -> config.Game | None:
    """The server game a presence name belongs to: 'VALORANT' -> Valorant,
    'Call of Duty® HQ' -> Call of Duty. None for games the server has no channel for."""
    s = config.slug(name)
    if not s:
        return None
    for g in games:
        if s.startswith(config.slug(g.role)) or s.startswith(config.slug(g.channel)):
            return g
    return None


def top_game(seconds_by_name: Mapping[str, int], games: Iterable[config.Game] = config.GAMES) -> config.Game | None:
    """The server game with the most play time (presence names folded into server games).
    Ties go to the game listed first in layout.py. None if nobody played a server game."""
    games = list(games)
    totals: dict[str, int] = {}
    for name, secs in seconds_by_name.items():
        g = game_for(name, games)
        if g is not None and secs > 0:
            totals[g.key] = totals.get(g.key, 0) + secs
    if not totals:
        return None
    best = max(totals.values())
    return next(g for g in games if totals.get(g.key) == best)
