"""Module 4c: the coin shop and monthly seasons. Pure: no Discord, no database.

Shop: the item list and prices, colour and shoutout checks, perk expiry maths.
Seasons: one per local calendar month; points are coins *earned* in the month from
activity reasons only (gambling, transfers and shop spending never count), plus the
standings order and the rollover plan. Coins only ever move through bot/economy.py."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterable

from logic import coins as C

HOUR = 60 * 60
DAY = 24 * HOUR

# ---------------------------------------------------------------- items
SHOP_REASON = "shop"
SEASON_REASON = "season"  # season bonuses: not a season-point reason, so they never snowball

COLOR, HYPE, SHOUTOUT = "color", "hype", "shoutout"


@dataclass(frozen=True)
class Item:
    key: str
    name: str
    price: int
    duration: int  # seconds the perk lasts (shoutout: the cooldown)
    blurb: str


ITEMS = {
    COLOR: Item(COLOR, "Name colour", 2_000, 30 * DAY,
                "A personal role in a colour you pick (`#RRGGBB`) for 30 days."),
    HYPE: Item(HYPE, "Hype", 500, DAY, "The Hype role for 24 hours."),
    SHOUTOUT: Item(SHOUTOUT, "Shoutout", 300, DAY,
                   "Post a message (up to 140 characters) in general. Once per 24 hours."),
}

ROLE_NAME_MAX = 32
SHOUTOUT_MAX = 140


def extend(current_expiry: int | None, now: int, duration: int) -> int:
    """Buying again adds time on top of what's left; an expired perk starts from now."""
    base = current_expiry if current_expiry is not None and current_expiry > now else now
    return base + duration


def role_name(display_name: str) -> str:
    name = " ".join((display_name or "").split())[:ROLE_NAME_MAX].strip()
    return name or "member"


# ---------------------------------------------------------------- colours
HEX = re.compile(r"#?([0-9a-fA-F]{6})")
MIN_LUMINANCE = 0.08  # below this a name is near-invisible on Discord's dark theme
STAFF_DISTANCE = 60  # RGB distance (0..441): closer than this to a staff colour is refused


def parse_color(text: str) -> int | None:
    m = HEX.fullmatch((text or "").strip())
    return int(m.group(1), 16) if m else None


def _rgb(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def luminance(value: int) -> float:
    """WCAG relative luminance, 0 (black) .. 1 (white)."""
    def lin(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(value)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def distance(a: int, b: int) -> float:
    return sum((x - y) ** 2 for x, y in zip(_rgb(a), _rgb(b))) ** 0.5


def check_color(text: str, staff_colors: Iterable[int] = ()) -> tuple[int | None, str | None]:
    """(colour, None) if `text` is an allowed colour, else (None, reason)."""
    value = parse_color(text)
    if value is None:
        return None, "Use a hex colour like `#3BA55D`."
    if luminance(value) < MIN_LUMINANCE:
        return None, "That colour is too dark to read. Pick something brighter."
    if any(s and distance(value, s) < STAFF_DISTANCE for s in staff_colors):
        return None, "That's too close to a staff role's colour. Pick something else."
    return value, None


# ---------------------------------------------------------------- shoutouts
LINK = re.compile(r"https?://|www\.|discord\.gg|discord(?:app)?\.com/invite", re.IGNORECASE)
MENTION = re.compile(r"<@|<#|@everyone|@here", re.IGNORECASE)


def shoutout_error(text: str) -> str | None:
    if text is None or not text.strip():
        return "Write something to shout out."
    if "\n" in text or "\r" in text:
        return "Keep it to one line."
    if len(text.strip()) > SHOUTOUT_MAX:
        return f"Keep it to {SHOUTOUT_MAX} characters."
    if LINK.search(text):
        return "No links in shoutouts."
    if MENTION.search(text):
        return "No mentions in shoutouts."
    return None


def fmt_wait(seconds: int) -> str:
    seconds = max(seconds, 60)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02}m" if h else f"{m}m"


# ---------------------------------------------------------------- seasons
SEASON_REASONS = frozenset({C.DAILY, C.VOICE, C.MESSAGE, C.CLIP, C.LFG, C.MVP, C.CLIP_WEEK, "trivia"})
BONUSES = (1_000, 500, 250)
SEASON_ROLE_REASON = "Season champion"


def month_key(now: int, tz) -> str:
    d = datetime.fromtimestamp(now, tz)
    return f"{d.year:04}-{d.month:02}"


def _first(year: int, month: int) -> date:
    return date(year, month, 1)


def _next(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _split(key: str) -> tuple[int, int]:
    y, m = key.split("-")
    return int(y), int(m)


def previous_key(key: str) -> str:
    y, m = _split(key)
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04}-{m:02}"


def month_bounds(key: str, tz) -> tuple[int, int]:
    """[start, end) of the local month, in unix seconds (DST-correct: midnight to midnight)."""
    y, m = _split(key)
    start = datetime.combine(_first(y, m), time(0), tz)
    end = datetime.combine(_first(*_next(y, m)), time(0), tz)
    return int(start.timestamp()), int(end.timestamp())


def month_name(key: str) -> str:
    y, m = _split(key)
    return _first(y, m).strftime("%B %Y")


def days_left(now: int, tz) -> int:
    """Local days left in this season, counting today."""
    today = datetime.fromtimestamp(now, tz).date()
    return (_first(*_next(today.year, today.month)) - today).days


@dataclass(frozen=True)
class Standing:
    rank: int
    user_id: int
    points: int


def standings(rows: Iterable[tuple[int, int, int]]) -> list[Standing]:
    """rows: (user_id, points, first earning at). Higher points first, then whoever started
    earning earliest in the month, then user id. Ranks are 1, 2, 3... (no shared ranks)."""
    ordered = sorted((r for r in rows if r[1] > 0), key=lambda r: (-r[1], r[2], r[0]))
    return [Standing(i + 1, uid, pts) for i, (uid, pts, _) in enumerate(ordered)]


def find(table: list[Standing], user_id: int) -> Standing | None:
    return next((s for s in table if s.user_id == user_id), None)


def bonus_ref(key: str, rank: int) -> str:
    return f"season:{key}:{rank}"


@dataclass(frozen=True)
class SeasonPlan:
    run: str | None  # finished season to post now
    mark_done: str | None  # record as done without posting (first run ever)


def season_plan(now: int, tz, done_keys: set[str]) -> SeasonPlan:
    """The just-finished month is posted once. On the very first run (no seasons rows at all)
    it is marked done instead, so turning the bot on mid-month doesn't announce a season it
    never watched. Older gaps (bot off for a whole month) are left alone."""
    prev = previous_key(month_key(now, tz))
    if not done_keys:
        return SeasonPlan(None, prev)
    if prev in done_keys:
        return SeasonPlan(None, None)
    return SeasonPlan(prev, None)
