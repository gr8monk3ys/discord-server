"""Module 4c: the coin shop and monthly seasons. Pure: no Discord, no database.

Shop: the item list and prices, colour, shoutout, gift and spotlight checks, perk expiry maths.
Raffle: the weekly coin raffle (ticket limits, draw times, ledger refs, the winner pick).
Seasons: one per local calendar month; points are coins *earned* in the month from
activity reasons only (gambling, transfers and shop spending never count), plus the
standings order and the rollover plan. Coins only ever move through bot/economy.py."""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from logic import coins as C
from logic import quests as Q
from logic.schedule import Weekly, occurrence

HOUR = 60 * 60
DAY = 24 * HOUR

# ---------------------------------------------------------------- items
SHOP_REASON = "shop"
SEASON_REASON = "season"  # season bonuses: not a season-point reason, so they never snowball

COLOR, HYPE, SHOUTOUT, GIFT, SPOTLIGHT = "color", "hype", "shoutout", "gift", "spotlight"


@dataclass(frozen=True)
class Item:
    key: str
    name: str
    price: int
    duration: int  # seconds the perk lasts (shoutout, spotlight: the buyer's cooldown)
    blurb: str


SPOTLIGHT_PIN = DAY  # how long a spotlight stays pinned

ITEMS = {
    COLOR: Item(COLOR, "Name colour", 2_000, 30 * DAY,
                "A personal role in a colour you pick (`#RRGGBB`) for 30 days."),
    HYPE: Item(HYPE, "Hype", 500, DAY, "The Hype role for 24 hours."),
    SHOUTOUT: Item(SHOUTOUT, "Shoutout", 300, DAY,
                   "Post a message (up to 140 characters) in general. Once per 24 hours."),
    GIFT: Item(GIFT, "Gift Hype", 500, DAY,
               "Give a friend the Hype role for 24 hours (`friend:`). They get a DM saying it was you."),
    SPOTLIGHT: Item(SPOTLIGHT, "Spotlight", 1_500, 7 * DAY,
                    "A shoutout pinned in general for 24 hours. One spotlight at a time, once a week each."),
}


# ---------------------------------------------------------------- gifts
def gift_error(buyer_id: int, friend_id: int | None, friend_is_bot: bool) -> str | None:
    if friend_id is None:
        return "Pick who gets it, like `/buy item:gift friend:@someone`."
    if friend_id == buyer_id:
        return "That's you! Use `/buy item:hype` to get Hype yourself."
    if friend_is_bot:
        return "Bots can't be Hype."
    return None


# ---------------------------------------------------------------- spotlight
SPOTLIGHT_KEY = "shop:spotlight"  # meta row: the one active spotlight, as JSON


@dataclass(frozen=True)
class Spotlight:
    user_id: int
    channel_id: int
    message_id: int
    expires_at: int


def spotlight_dump(s: Spotlight) -> str:
    return json.dumps({"user": s.user_id, "channel": s.channel_id, "message": s.message_id,
                       "expires": s.expires_at}, sort_keys=True)


def spotlight_load(value: str | None) -> Spotlight | None:
    """The stored spotlight, or None if there's none (or the row is unreadable)."""
    if not value:
        return None
    try:
        d = json.loads(value)
        return Spotlight(int(d["user"]), int(d["channel"]), int(d["message"]), int(d["expires"]))
    except (ValueError, KeyError, TypeError):
        return None


def spotlight_busy(current: Spotlight | None, now: int) -> bool:
    return current is not None and current.expires_at > now

ROLE_NAME_MAX = 32
SHOUTOUT_MAX = 140


def extend(current_expiry: int | None, now: int, duration: int) -> int:
    """Buying again adds time on top of what's left; an expired perk starts from now."""
    base = current_expiry if current_expiry is not None and current_expiry > now else now
    return base + duration


COLOUR_ROLE_PREFIX = "Colour · "


def role_name(display_name: str) -> str:
    """Purchased colour roles always carry a fixed text prefix. The bot finds staff roles
    by name (emoji and punctuation ignored), so a member nicknamed "Keeper" must never
    get a role whose name matches Keeper, Moderator, Squad or any other real role."""
    name = " ".join((display_name or "").split())[:ROLE_NAME_MAX].strip()
    return COLOUR_ROLE_PREFIX + (name or "member")


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


# ---------------------------------------------------------------- weekly raffle
# Tickets cost coins (a sink); Sunday 20:00 local one ticket is drawn and its owner gets
# RAFFLE_SHARE % of the pot. The rest is burned: never paid out to anyone. Tickets are
# ledger rows (reason "raffle", ref "raffle:2026-W41:buy:<user>:<interaction>"), so the
# pot, each member's count and the winner all come from the ledger; the `jobs` table
# marks a draw as posted.
RAFFLE_REASON = "raffle"
TICKET_PRICE = 100
MAX_TICKETS = 10  # per member per draw
RAFFLE_SHARE = 80  # percent of the pot the winner gets; the rest is burned
MIN_ENTRANTS = 2  # fewer people than this: everyone is refunded in full
RAFFLE_JOB = Weekly("raffle", weekday=6, hour=20, minute=0)  # Sundays 20:00 local
DRAW_GRACE = 60  # seconds after the draw time before drawing, so in-flight purchases land


def next_draw(now: int, tz):
    """The draw a ticket bought at `now` goes into: the first Sunday 20:00 strictly after now."""
    today = datetime.fromtimestamp(now, tz).date()
    day = today + timedelta(days=(RAFFLE_JOB.weekday - today.weekday()) % 7)
    p = occurrence(RAFFLE_JOB, day, tz)
    return p if p.scheduled_at > now else occurrence(RAFFLE_JOB, day + timedelta(days=7), tz)


def draw_at(key: str, tz) -> int:
    """When draw `key` ("raffle:2026-W41") happens, in unix seconds."""
    year, week = key.split(":", 1)[1].split("-W")
    return occurrence(RAFFLE_JOB, date.fromisocalendar(int(year), int(week), 7), tz).scheduled_at


def ticket_ref(key: str, user_id: int, purchase_id: int) -> str:
    return f"{key}:buy:{user_id}:{purchase_id}"


def tickets_like(key: str) -> str:
    """LIKE pattern for every ticket ref of draw `key` (keys hold no % or _)."""
    return f"{key}:buy:%"


def win_ref(key: str) -> str:
    return f"{key}:win"


def refund_ref(key: str, user_id: int) -> str:
    return f"{key}:refund:{user_id}"


def key_of(ref: str | None) -> str | None:
    """'raffle:2026-W41' from a ticket ref, else None."""
    parts = (ref or "").split(":")
    if len(parts) >= 3 and parts[0] == RAFFLE_JOB.name and parts[2] == "buy":
        return f"{parts[0]}:{parts[1]}"
    return None


def can_enter(user_id: int, now: int) -> bool:
    """Fresh alt accounts can't buy tickets (same age rule as every other reward)."""
    return Q.established(user_id, now)


def ticket_error(owned: int, buying: int) -> str | None:
    if buying < 1:
        return "Buy at least 1 ticket."
    if owned >= MAX_TICKETS:
        return f"You already have the maximum {MAX_TICKETS} tickets for this draw."
    if owned + buying > MAX_TICKETS:
        return f"You can hold {MAX_TICKETS} tickets per draw. You have {owned}, so buy {MAX_TICKETS - owned} or fewer."
    return None


def prize(pot: int) -> int:
    return pot * RAFFLE_SHARE // 100


def due(keys: Iterable[str], done: set[str], now: int, tz) -> list[str]:
    """Draws that have tickets, are past their time (plus grace) and haven't been posted: oldest first."""
    out = {k for k in keys if k not in done and draw_at(k, tz) + DRAW_GRACE <= now}
    return sorted(out, key=lambda k: draw_at(k, tz))


def pick_winner(tickets: Iterable[tuple[int, int]], rng) -> int:
    """One ticket at random: (user_id, count) rows, so 3 tickets = 3 chances. Rows are sorted
    first so the result depends only on the rng, not on query order."""
    pool = [uid for uid, n in sorted(tickets) for _ in range(max(n, 0))]
    if not pool:
        raise ValueError("no tickets")
    return rng.choice(pool)


def fmt_chance(mine: int, total: int) -> str:
    return f"{100 * mine / total:.0f}%" if total else "0%"
