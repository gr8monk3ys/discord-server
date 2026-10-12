"""Seasonal events: yearly events (Halloween, Winter Fest) read from assets/events.json,
with drops in chat, a small daily quest, a points board and prizes at the end. Pure: no
Discord, no database. The cog (cogs/seasonal.py) does the I/O.

An event recurs every year between two month-day dates in local time (the end is exclusive and
may fall in the next year). One occurrence is a Run with the id "KEY:YEAR" (the year it starts
in); that id is what `event_drops.event` and `event_claims.event` store, ledger refs are
"event:KEY:YEAR:..." and the badge key is "event_KEY_YEAR".

Points are never counted here or stored as a counter: the cog reads them from the tables
(one per claimed drop, QUEST_POINTS per daily-quest claim).

Drop times for a local day are drawn from an rng seeded with the run id and the date, so every
restart agrees on them: DROPS_MIN..DROPS_MAX a day between DROP_FROM and DROP_UNTIL, at least
DROP_GAP apart."""

import json
import random
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path

from logic.quests import MIN_ACCOUNT_DAYS

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
EVENTS_FILE = Path(__file__).resolve().parents[1] / "assets" / "events.json"
REASON = "event"  # ledger reason for every event payout

# Drops
DROPS_MIN, DROPS_MAX = 6, 10  # per local day
DROP_FROM = time(10, 0)
DROP_UNTIL = time(23, 30)
DROP_GAP = 30 * MINUTE  # never two drops closer than this
DROP_COINS = (25, 75)
DROP_EXPIRY = 10 * MINUTE  # an unclaimed drop is deleted after this
DROP_DAILY_MAX = 12  # drops one member can claim per local day
CHAT_ACTIVE = 20 * MINUTE  # a drop needs a member message in general this recently
SLOT_WINDOW = 20 * MINUTE  # a drop time that finds chat quiet waits this long, then is skipped
# Daily quest
QUEST_POINTS = 3
QUEST_COINS = 100
QUEST_GRACE = 2 * HOUR  # yesterday's quest is still granted this long into the next day
# The end
RANK_COINS = (2000, 1000, 500)
BADGE_POINTS = 10  # at least this many points earns the event's badge
TOP_N = 10
MEDALS = ("🥇", "🥈", "🥉")

KEY_RE = re.compile(r"^[a-z][a-z0-9]{1,19}$")
MD_RE = re.compile(r"^(\d{2})-(\d{2})$")
COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
MAX_LINES = 30
MAX_LINE = 200
MAX_NAME = 40


class EventError(ValueError):
    """events.json is wrong; the message says where."""


@dataclass(frozen=True)
class Event:
    key: str
    name: str
    emoji: str
    start: tuple[int, int]  # (month, day), inclusive, 00:00 local
    end: tuple[int, int]  # (month, day), exclusive, 00:00 local; may be in the next year
    color: int
    intro: str
    drop_emoji: str
    drop_name: str
    drop_lines: tuple[str, ...]
    badge_emoji: str
    badge_name: str

    @property
    def wraps(self) -> bool:
        """Ends in the year after it starts (Dec 15 to Jan 2)."""
        return self.end <= self.start


@dataclass(frozen=True)
class Run:
    """One year's occurrence of an event."""

    event: Event
    year: int  # the year it starts in
    start: int  # unix seconds
    end: int  # unix seconds, exclusive

    @property
    def id(self) -> str:
        return f"{self.event.key}:{self.year}"

    @property
    def badge_key(self) -> str:
        return f"event_{self.event.key}_{self.year}"


# ---------------------------------------------------------------- events.json
def _text(raw, where: str, limit: int = MAX_NAME) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise EventError(f"{where}: expected some text")
    text = " ".join(raw.split())
    if len(text) > limit:
        raise EventError(f"{where}: longer than {limit} characters")
    return text


def _month_day(raw, where: str) -> tuple[int, int]:
    m = MD_RE.match(raw) if isinstance(raw, str) else None
    if m is None:
        raise EventError(f"{where}: expected MM-DD")
    month, day = int(m[1]), int(m[2])
    try:
        date(2001, month, day)  # not a leap year: Feb 29 would skip three years in four
    except ValueError:
        raise EventError(f"{where}: {raw} is not a date that exists every year") from None
    return month, day


def parse_event(raw) -> Event:
    if not isinstance(raw, Mapping):
        raise EventError("an event must be an object")
    key = raw.get("key")
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise EventError(f"event key {key!r}: lowercase letters and digits only (2 to 20)")
    drop, badge = raw.get("drop"), raw.get("badge")
    if not isinstance(drop, Mapping) or not isinstance(badge, Mapping):
        raise EventError(f"{key}: needs a drop and a badge object")
    lines = drop.get("lines")
    if not isinstance(lines, list) or not lines or len(lines) > MAX_LINES:
        raise EventError(f"{key}: drop.lines needs 1 to {MAX_LINES} lines")
    color = COLOR_RE.match(raw.get("color")) if isinstance(raw.get("color"), str) else None
    if color is None:
        raise EventError(f"{key}: color must look like #E8741A")
    start, end = _month_day(raw.get("start"), f"{key}.start"), _month_day(raw.get("end"), f"{key}.end")
    if start == end:
        raise EventError(f"{key}: start and end are the same day")
    return Event(
        key=key, name=_text(raw.get("name"), f"{key}.name"), emoji=_text(raw.get("emoji"), f"{key}.emoji", 8),
        start=start, end=end, color=int(color[1], 16), intro=_text(raw.get("intro"), f"{key}.intro", MAX_LINE),
        drop_emoji=_text(drop.get("emoji"), f"{key}.drop.emoji", 8),
        drop_name=_text(drop.get("name"), f"{key}.drop.name"),
        drop_lines=tuple(_text(line, f"{key}.drop.lines", MAX_LINE) for line in lines),
        badge_emoji=_text(badge.get("emoji"), f"{key}.badge.emoji", 8),
        badge_name=_text(badge.get("name"), f"{key}.badge.name"))


def _days(event: Event) -> set[tuple[int, int]]:
    """Every (month, day) the event covers, in a leap year so Feb 29 is included."""
    day = date(2000, *event.start)
    last = date(2001 if event.wraps else 2000, *event.end)
    out = set()
    while day < last:
        out.add((day.month, day.day))
        day += timedelta(days=1)
    return out


def parse_events(data) -> tuple[Event, ...]:
    """Validated events. Raises EventError for a bad shape, a repeated key or two events that
    overlap (only one event runs at a time)."""
    raw = data.get("events") if isinstance(data, Mapping) else None
    if not isinstance(raw, list):
        raise EventError('expected {"events": [...]}')
    events = tuple(parse_event(e) for e in raw)
    seen: dict[tuple[int, int], str] = {}
    keys = set()
    for event in events:
        if event.key in keys:
            raise EventError(f"event key {event.key!r} is used twice")
        keys.add(event.key)
        for md in _days(event):
            if md in seen:
                raise EventError(f"{event.key} and {seen[md]} overlap")
            seen[md] = event.key
    return events


def load_events(path: Path | str = EVENTS_FILE) -> tuple[Event, ...]:
    with open(path, encoding="utf-8") as f:
        return parse_events(json.load(f))


# ---------------------------------------------------------------- runs
def _midnight(year: int, md: tuple[int, int], tz: tzinfo) -> int:
    return int(datetime.combine(date(year, *md), time(0), tz).timestamp())


def run_for(event: Event, year: int, tz: tzinfo) -> Run:
    """The run that starts in `year`."""
    return Run(event, year, _midnight(year, event.start, tz),
               _midnight(year + 1 if event.wraps else year, event.end, tz))


def _runs(events: Iterable[Event], now: int, tz: tzinfo) -> list[Run]:
    year = datetime.fromtimestamp(now, tz).year
    return [run_for(e, y, tz) for e in events for y in (year - 1, year, year + 1)]


def current(events: Iterable[Event], now: int, tz: tzinfo) -> Run | None:
    return next((r for r in _runs(events, now, tz) if r.start <= now < r.end), None)


def upcoming(events: Iterable[Event], now: int, tz: tzinfo) -> Run | None:
    """The next run to start after `now`."""
    later = [r for r in _runs(events, now, tz) if r.start > now]
    return min(later, key=lambda r: r.start) if later else None


def last_ended(events: Iterable[Event], now: int, tz: tzinfo) -> list[Run]:
    """Per event, its most recent run that is over, oldest end first."""
    out = []
    for event in events:
        over = [r for r in _runs([event], now, tz) if r.end <= now]
        if over:
            out.append(max(over, key=lambda r: r.end))
    return sorted(out, key=lambda r: r.end)


def parse_run_id(run_id: str) -> tuple[str, int] | None:
    key, _, year = run_id.partition(":")
    return (key, int(year)) if KEY_RE.match(key) and year.isdigit() else None


# ---------------------------------------------------------------- local days
def local_day(ts: int, tz: tzinfo) -> date:
    return datetime.fromtimestamp(ts, tz).date()


def day_bounds(day: date, tz: tzinfo) -> tuple[int, int]:
    return (int(datetime.combine(day, time(0), tz).timestamp()),
            int(datetime.combine(day + timedelta(days=1), time(0), tz).timestamp()))


def in_run(run: Run, day: date, tz: tzinfo) -> bool:
    start, _ = day_bounds(day, tz)
    return run.start <= start < run.end


# ---------------------------------------------------------------- drops
@dataclass(frozen=True)
class Slot:
    index: int
    at: int  # unix seconds
    coins: int
    line: str

    def job_key(self, run: Run, day: date) -> str:
        return f"seasonal:drop:{run.id}:{day.isoformat()}:{self.index}"


def drop_slots(run: Run, day: date, tz: tzinfo, rng: random.Random | None = None) -> list[Slot]:
    """The day's drop times with their coins and flavour line, oldest first. Seeded from the
    run and the date unless an rng is passed, so it is the same list on every call."""
    rng = rng or random.Random(f"seasonal:{run.id}:{day.isoformat()}")
    first = datetime.combine(day, DROP_FROM, tz).timestamp()
    last = datetime.combine(day, DROP_UNTIL, tz).timestamp()
    n = rng.randint(DROPS_MIN, DROPS_MAX)
    # n sorted points in the span left after reserving the gaps, then the gaps are put back.
    free = int(last - first) - DROP_GAP * (n - 1)
    offsets = sorted(rng.randint(0, free) for _ in range(n))
    slots = []
    for i, offset in enumerate(offsets):
        at = int(first) + offset + DROP_GAP * i
        slots.append(Slot(i, at, rng.randint(*DROP_COINS), rng.choice(run.event.drop_lines)))
    return [s for s in slots if run.start <= s.at < run.end]


def due_slot(slots: Iterable[Slot], now: int, done: Iterable[str], run: Run, day: date) -> Slot | None:
    """The slot to post now: its time has come, it is at most SLOT_WINDOW old and not done."""
    done = set(done)
    for slot in slots:
        if slot.at <= now < slot.at + SLOT_WINDOW and slot.job_key(run, day) not in done:
            return slot
    return None


def chat_active(last_message_at: int | None, now: int) -> bool:
    return last_message_at is not None and 0 <= now - last_message_at <= CHAT_ACTIVE


def gap_ok(last_drop_at: int | None, now: int) -> bool:
    return last_drop_at is None or now - last_drop_at >= DROP_GAP


def drop_expired(created_at: int, now: int) -> bool:
    return now - created_at >= DROP_EXPIRY


def drop_ref(run_id: str, drop_id: int) -> str:
    return f"event:{run_id}:drop:{drop_id}"


def old_enough(created_at: int | None, now: int) -> bool:
    """Accounts younger than MIN_ACCOUNT_DAYS (likely alts) earn nothing from an event."""
    return created_at is not None and now - created_at >= MIN_ACCOUNT_DAYS * DAY


# ---------------------------------------------------------------- daily quest
@dataclass(frozen=True)
class Quest:
    key: str
    emoji: str
    how: str  # shown to members
    target: int  # seconds for voice, else a count
    tracking: bool = False  # from tracked activity: skipped for /privacy off


QUESTS: tuple[Quest, ...] = (
    Quest("squad", "🤝", "be in an `/lfg` squad with someone else today (join one or post one)", 1),
    Quest("win_game", "🃏", "win a `/trivia` question or a hand of `/blackjack`", 1),
    Quest("chat", "💬", "send 5 messages in chat", 5, tracking=True),
    Quest("voice", "🎧", "spend 30 minutes in voice with others", 30 * MINUTE, tracking=True),
    Quest("word", "🔤", "solve today's word game (`/word`)", 1),
)
QUEST_BY_KEY = {q.key: q for q in QUESTS}


def quest_for(run: Run, day: date, tz: tzinfo) -> Quest:
    """The day's quest: the pool in an order shuffled once per run, cycled by day, so the same
    quest never comes two days running and no quest is skipped."""
    order = list(QUESTS)
    random.Random(f"seasonal:{run.id}:quests").shuffle(order)
    index = (day - local_day(run.start, tz)).days
    return order[index % len(order)]


def quest_claim_key(day: date) -> str:
    return f"quest:{day.isoformat()}"


def quest_ref(run_id: str, day: date, user_id: int) -> str:
    return f"event:{run_id}:quest:{day.isoformat()}:{user_id}"


def quest_done(quest: Quest, value: int) -> bool:
    return value >= quest.target


def quest_days(run: Run, now: int, tz: tzinfo) -> list[date]:
    """The days whose quest can be granted now: today, plus yesterday during QUEST_GRACE (so
    the last hour of a day isn't lost). Only days inside the run."""
    today = local_day(now, tz)
    days = [today]
    if now - day_bounds(today, tz)[0] < QUEST_GRACE:
        days.insert(0, today - timedelta(days=1))
    return [d for d in days if in_run(run, d, tz)]


def last_day(run: Run, tz: tzinfo) -> date:
    return local_day(run.end - 1, tz)


# ---------------------------------------------------------------- points and ranks
@dataclass(frozen=True)
class Standing:
    user_id: int
    points: int
    last_at: int  # when their latest point was earned: the earlier one wins a tie


def standings(drops: Iterable[tuple[int, int, int]], quests: Iterable[tuple[int, int, int]]) -> list[Standing]:
    """Rows are (user_id, how many, latest time). One point per drop, QUEST_POINTS per quest.
    Best first; ties go to whoever got there first, then the lower id."""
    points: dict[int, int] = {}
    last: dict[int, int] = {}
    for rows, each in ((drops, 1), (quests, QUEST_POINTS)):
        for uid, n, at in rows:
            if uid is None or not n:
                continue
            points[uid] = points.get(uid, 0) + int(n) * each
            last[uid] = max(last.get(uid, 0), int(at or 0))
    out = [Standing(uid, p, last[uid]) for uid, p in points.items() if p > 0]
    return sorted(out, key=lambda s: (-s.points, s.last_at, s.user_id))


def rank_ref(run_id: str, rank: int) -> str:
    return f"event:{run_id}:rank:{rank}"


def winners(board: Iterable[Standing], eligible) -> list[Standing]:
    """The top len(RANK_COINS) of the members `eligible(user_id)` accepts (still here, not a bot)."""
    return [s for s in board if eligible(s.user_id)][:len(RANK_COINS)]


def badge_holders(board: Iterable[Standing]) -> list[int]:
    return [s.user_id for s in board if s.points >= BADGE_POINTS]


# ---------------------------------------------------------------- text
def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def drop_text(event: Event, line: str) -> str:
    return f"{event.drop_emoji} **{line}** First to grab it gets coins and an event point."


def claimed_text(event: Event, mention: str, coins: int) -> str:
    return f"{event.drop_emoji} {mention} grabbed the {event.drop_name}: **+{coins:,} coins** and 1 event point."


def board_text(board: list[Standing], n: int = TOP_N) -> str:
    if not board:
        return "Nobody has points yet. Grab a drop in chat to get on the board."
    lines = []
    for i, s in enumerate(board[:n]):
        place = MEDALS[i] if i < len(MEDALS) else f"`{i + 1:>2}.`"
        lines.append(f"{place} <@{s.user_id}> · {plural(s.points, 'point')}")
    return "\n".join(lines)


def my_points(board: Iterable[Standing], user_id: int) -> tuple[int, int | None]:
    """(points, 1-based place or None when not on the board)."""
    for i, s in enumerate(board):
        if s.user_id == user_id:
            return s.points, i + 1
    return 0, None


def quest_line(quest: Quest, *, tracking: bool, claimed: bool, old: bool = True) -> str:
    reward = f"+{QUEST_POINTS} points and {QUEST_COINS:,} coins"
    if quest.tracking and not tracking:
        return f"➖ {quest.emoji} ~~{quest.how}~~: skipped today, stats are off (`/privacy`)"
    if claimed:
        return f"✅ {quest.emoji} {quest.how}: done ({reward})"
    if not old:
        return (f"▫️ {quest.emoji} {quest.how}. Rewards unlock once your Discord account is "
                f"{MIN_ACCOUNT_DAYS}+ days old.")
    return f"▫️ {quest.emoji} {quest.how} ({reward}, paid within the hour)"


def how_it_works(event: Event) -> str:
    return (f"{event.drop_emoji} **Drops**: {event.drop_name}s show up in chat when it's busy. First to press "
            f"Grab gets {DROP_COINS[0]}-{DROP_COINS[1]} coins and 1 point.\n"
            f"📅 **Daily quest**: one small task a day for {QUEST_POINTS} points and {QUEST_COINS:,} coins.\n"
            f"🏆 **Prizes**: the top 3 win {' / '.join(f'{c:,}' for c in RANK_COINS)} coins, "
            f"first place wears the Event Champ role, and {BADGE_POINTS}+ points earns the "
            f"{event.badge_emoji} **{event.badge_name}** badge.")


def opening_text(run: Run) -> str:
    e = run.event
    return (f"{e.emoji} **{e.name} is on!** {e.intro}\n\n{how_it_works(e)}\n\n"
            f"It ends <t:{run.end}:F> (<t:{run.end}:R>). Check `/event` for the board and today's quest.")


def quest_text(event: Event, mention: str, quest: Quest) -> str:
    return (f"{event.emoji} {mention} finished today's {event.name} quest "
            f"(+{QUEST_POINTS} points, +{QUEST_COINS:,} coins).")


def finale_text(run: Run, top: list[Standing], badges: int, role_given: bool) -> str:
    e = run.event
    lines = [f"{e.emoji} **{e.name} {run.year} is over.** Thanks for playing!", ""]
    for i, s in enumerate(top):
        lines.append(f"{MEDALS[i]} <@{s.user_id}> · {plural(s.points, 'point')} · **+{RANK_COINS[i]:,} coins**")
    if role_given and top:
        lines.append("")
        lines.append(f"<@{top[0].user_id}> holds the Event Champ role until the next event.")
    if badges:
        lines.append(f"{plural(badges, 'member')} earned the {e.badge_emoji} **{e.badge_name}** badge "
                     f"({BADGE_POINTS}+ points).")
    return "\n".join(lines)


def idle_text(nxt: Run | None) -> str:
    if nxt is None:
        return "No event is running right now."
    e = nxt.event
    return (f"No event is running right now. Next up: {e.emoji} **{e.name}**, starting "
            f"<t:{nxt.start}:F> (<t:{nxt.start}:R>).")
