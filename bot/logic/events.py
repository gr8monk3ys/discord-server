"""Module 7 rules: game nights and free games. Pure: no Discord calls, no database.

- `parse_when` turns "9pm", "tomorrow 8pm", "fri 21:30", "2026-10-10 20:00" into an aware
  local datetime; `check_when` says whether it's in the allowed window.
- `reminder_state` decides when a game-night reminder goes out.
- `select_giveaways` filters the GamerPower feed down to new, active, linkable games.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from urllib.parse import urlsplit

import discord

import config

MAX_AHEAD = timedelta(days=30)
REMIND_BEFORE = 15 * 60  # seconds before the start
REMIND_GRACE = 10 * 60  # still remind up to this long after the start
SQUAD_MAX = 5  # bigger groups go to the Lobby
MAX_FREE_GAMES = 8

WHEN_EXAMPLES = "`9pm`, `9:30pm`, `21:00`, `tonight 9pm`, `tomorrow 8pm`, `fri 9pm`, `2026-10-10 20:00`"

WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
TODAY_WORDS = {"today", "tonight"}
TOMORROW_WORDS = {"tomorrow", "tmrw", "tmr", "tomorrow's"}

_TIME_12 = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)")
_TIME_24 = re.compile(r"(\d{1,2}):(\d{2})")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _parse_time(text: str) -> time | None:
    if m := _TIME_12.fullmatch(text):
        hour, minute = int(m[1]), int(m[2] or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        hour = hour % 12 + (12 if m[3] == "pm" else 0)
        return time(hour, minute)
    if m := _TIME_24.fullmatch(text):
        hour, minute = int(m[1]), int(m[2])
        if hour > 23 or minute > 59:
            return None
        return time(hour, minute)
    return None


def _local(day: date, at: time, tz: tzinfo) -> datetime:
    # zoneinfo resolves wall-clock times with fold=0: on the fall-back day an ambiguous time
    # takes the first (DST) instance; a spring-forward gap time lands an hour later.
    return datetime.combine(day, at, tzinfo=tz)


def parse_when(text: str, now: datetime, tz: tzinfo) -> datetime | None:
    """An aware local datetime for `text`, or None if it isn't one of the supported forms.

    A bare time ("9pm") is the next one: today if still ahead, else tomorrow. A weekday is the
    next such day (today if the time is still ahead). "tonight"/"today" and explicit dates are
    taken literally, so they may be in the past; `check_when` rejects that.
    """
    words = " ".join(text.lower().split())
    now = now.astimezone(tz)
    today = now.date()
    if m := _DATE.match(words):
        rest = words[m.end():].strip()
        try:
            day = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
        at = _parse_time(rest)
        return _local(day, at, tz) if at else None

    first, _, rest = words.partition(" ")
    if first in TODAY_WORDS or first in TOMORROW_WORDS or first in WEEKDAYS:
        at = _parse_time(rest.strip())
        if at is None:
            return None
        if first in TODAY_WORDS:
            return _local(today, at, tz)
        if first in TOMORROW_WORDS:
            return _local(today + timedelta(days=1), at, tz)
        day = today + timedelta(days=(WEEKDAYS[first] - today.weekday()) % 7)
        when = _local(day, at, tz)
        return when if when > now else _local(day + timedelta(days=7), at, tz)

    at = _parse_time(words)
    if at is None:
        return None
    when = _local(today, at, tz)
    return when if when > now else _local(today + timedelta(days=1), at, tz)


def check_when(when: datetime, now: datetime) -> str | None:
    """None if `when` is usable, else "past" or "too_far"."""
    if when <= now:
        return "past"
    if when - now > MAX_AHEAD:
        return "too_far"
    return None


def to_utc(when: datetime) -> datetime:
    return when.astimezone(timezone.utc)


def voice_for(size: int | None) -> str:
    """The voice channel name for a group of `size` (unknown size: Squad)."""
    return config.LOBBY_VOICE if size is not None and size > SQUAD_MAX else config.SQUAD_VOICE


def event_name(game_label: str | None) -> str:
    """"Valorant game night"; plain "Game night" when any game goes (None)."""
    return f"{game_label} game night"[:100] if game_label else "Game night"


def event_description(host_name: str, note: str | None, size: int | None) -> str:
    parts = [f"Hosted by {host_name}."]
    if size:
        parts.append(f"Looking for {size} players.")
    if note:
        parts.append(note)
    return " ".join(parts)[:1000]


# ---------------------------------------------------------------- reminders
def reminder_state(starts_at: int, now: int) -> str:
    """'wait' until 15 min before the start, 'send' from then until 10 min after it,
    'expired' later (too late to be useful: mark it reminded and move on)."""
    if now < starts_at - REMIND_BEFORE:
        return "wait"
    if now <= starts_at + REMIND_GRACE:
        return "send"
    return "expired"


# ---------------------------------------------------------------- free games
class MalformedFeed(ValueError):
    """The API answered with something that isn't a giveaway list."""


@dataclass(frozen=True)
class Giveaway:
    id: int
    title: str
    url: str
    platforms: str | None
    worth: str | None
    end_date: str | None


_URL_BAD_CHARS = re.compile(r"[\s<>()\[\]\"'`\\]")


def safe_url(url) -> str | None:
    """`url` if it's a plain http(s) link with a host, else None. Characters that could break
    out of a Markdown link are refused rather than escaped."""
    if not isinstance(url, str) or not url or len(url) > 500 or _URL_BAD_CHARS.search(url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return url


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or value.upper() == "N/A":
        return None
    return value[:limit]


def select_giveaways(data, seen: set[int]) -> list[Giveaway]:
    """New, active giveaways with a safe link, in feed order. Raises MalformedFeed if `data`
    isn't a list (GamerPower's "nothing right now" object counts as an empty list)."""
    if isinstance(data, dict) and data.get("status") == 0:
        return []
    if not isinstance(data, list):
        raise MalformedFeed(f"expected a list, got {type(data).__name__}")
    out: list[Giveaway] = []
    taken = set(seen)
    for raw in data:
        if not isinstance(raw, dict):
            continue
        gid = raw.get("id")
        if not isinstance(gid, int) or isinstance(gid, bool) or gid in taken:
            continue
        status = raw.get("status")
        if isinstance(status, str) and status.lower() != "active":
            continue
        title = _text(raw.get("title"), 100)
        url = safe_url(raw.get("open_giveaway_url"))
        if title is None or url is None:
            continue
        taken.add(gid)
        out.append(Giveaway(
            id=gid, title=title, url=url,
            platforms=_text(raw.get("platforms"), 60),
            worth=_text(raw.get("worth"), 20),
            end_date=_text(raw.get("end_date"), 10),  # "2026-10-15 23:59:00" -> the date
        ))
    return out


def _md(text: str) -> str:
    return discord.utils.escape_markdown(text.replace("[", "(").replace("]", ")"))


def giveaway_lines(games: list[Giveaway]) -> list[str]:
    """One embed line per game, at most MAX_FREE_GAMES."""
    lines = []
    for g in games[:MAX_FREE_GAMES]:
        bits = [b for b in (g.platforms, f"~~{_md(g.worth)}~~ free" if g.worth else None,
                            f"until {g.end_date}" if g.end_date else None) if b]
        line = f"**[{_md(g.title)}]({g.url})**"
        if bits:
            line += "\n" + " · ".join(_md(b) if b is g.platforms else b for b in bits)
        lines.append(line)
    return lines
