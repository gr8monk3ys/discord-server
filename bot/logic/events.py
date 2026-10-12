"""Module 7 rules: game nights and free games. Pure: no Discord calls, no database.

- `parse_when` turns "9pm", "tomorrow 8pm", "fri 21:30", "2026-10-10 20:00" into an aware
  local datetime; `check_when` says whether it's in the allowed window.
- `reminder_state` decides when a game-night reminder goes out.
- `watch_link` / `show_link` vet a watch party's link; `townhall_start` picks the first
  Saturday of the month.
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


def _later(a: datetime, b: datetime) -> bool:
    """a is after b as real instants. Datetimes sharing a tzinfo compare (and subtract) as
    wall-clock times, ignoring fold, so compare in UTC."""
    return a.astimezone(timezone.utc) > b.astimezone(timezone.utc)


def _local(day: date, at: time, tz: tzinfo, now: datetime) -> datetime:
    # zoneinfo resolves wall-clock times with fold=0: on the fall-back day an ambiguous time
    # takes the first (DST) instance; a spring-forward gap time lands an hour later. If that
    # first instance has passed but the repeated one hasn't, take the repeated one.
    when = datetime.combine(day, at, tzinfo=tz)
    if not _later(when, now) and _later(when.replace(fold=1), now):
        return when.replace(fold=1)
    return when


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
        return _local(day, at, tz, now) if at else None

    first, _, rest = words.partition(" ")
    if first in TODAY_WORDS or first in TOMORROW_WORDS or first in WEEKDAYS:
        at = _parse_time(rest.strip())
        if at is None:
            return None
        if first in TODAY_WORDS:
            return _local(today, at, tz, now)
        if first in TOMORROW_WORDS:
            return _local(today + timedelta(days=1), at, tz, now)
        day = today + timedelta(days=(WEEKDAYS[first] - today.weekday()) % 7)
        when = _local(day, at, tz, now)
        return when if _later(when, now) else _local(day + timedelta(days=7), at, tz, now)

    at = _parse_time(words)
    if at is None:
        return None
    when = _local(today, at, tz, now)
    return when if _later(when, now) else _local(today + timedelta(days=1), at, tz, now)


def check_when(when: datetime, now: datetime) -> str | None:
    """None if `when` is usable, else "past" or "too_far"."""
    if not _later(when, now):
        return "past"
    if when - now > MAX_AHEAD:  # calendar days: wall-clock difference is what people mean
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


# ---------------------------------------------------------------- watch parties and the Town Hall
# `hosted_events.kind`. Both are scheduled events that share the game-night reminder loop.
WATCHPARTY, TOWNHALL = "watchparty", "townhall"
# What Discord asks for, in the stage channel, before a bot may schedule a Stage event.
STAGE_EVENT_PERMS = ("manage_channels", "mute_members", "move_members")
# Hosts whose links may unfurl in the announcement. Anything else is shown as plain text
# (wrapped in <>), so an unknown site never gets a preview card under the bot's name.
WATCH_HOSTS = frozenset({
    "youtube.com", "youtu.be", "twitch.tv", "netflix.com", "disneyplus.com", "primevideo.com",
    "max.com", "hulu.com", "crunchyroll.com", "kick.com", "vimeo.com",
})
MAX_LINK = 300
TOWNHALL_NAME = "Town Hall"
TOWNHALL_WEEKDAY = 5  # Saturday
TOWNHALL_DEFAULT = time(12, 0)
TOWNHALL_OFF = {"off", "none", "no", "false", "0", "disabled"}


def watch_link(url: str | None) -> str | None:
    """`url` if it is a plain https link with a host and no credentials, else None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if len(url) > MAX_LINK or safe_url(url) is None:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or "@" in parts.netloc or not parts.hostname or "." not in parts.hostname:
        return None
    return url


def known_watch_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    return any(host == h or host.endswith("." + h) for h in WATCH_HOSTS)


def show_link(url: str) -> str:
    """The link as the bot posts it: bare (Discord may embed it) for a known host, else <url>."""
    return url if known_watch_host(url) else f"<{url}>"


def watchparty_name(title: str) -> str:
    return f"Watch party: {' '.join(title.split())}"[:100]


def watchparty_description(host_name: str, link: str | None) -> str:
    parts = [f"Hosted by {host_name}."]
    if link:
        parts.append(f"Watching: {link}")
    return " ".join(parts)[:1000]


def parse_townhall_time(text: str | None) -> time | None:
    """The TOWNHALL_TIME setting: "12:00", "7pm", ... (Pacific wall-clock). Unset means the
    default; "off" (or anything unreadable) means no Town Hall."""
    if text is None or not text.strip():
        return TOWNHALL_DEFAULT
    words = " ".join(text.lower().split())
    if words in TOWNHALL_OFF:
        return None
    return _parse_time(words)


def townhall_start(scheduled_at: int, tz: tzinfo) -> datetime | None:
    """The weekly job runs on Saturdays at the Town Hall time. The Saturday a week later gets
    a Town Hall when it is the first Saturday of its month; this returns that start (same
    wall-clock time), or None in the other weeks."""
    local = datetime.fromtimestamp(scheduled_at, tz)
    day = local.date() + timedelta(days=7)
    if day.weekday() != TOWNHALL_WEEKDAY or day.day > 7:
        return None
    return datetime.combine(day, local.time().replace(second=0, microsecond=0), tzinfo=tz)


def townhall_description() -> str:
    return ("The monthly open mic: what's working, what isn't, and what the server does next. "
            "Tap Interested for a reminder. Bring a topic or just listen.")


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
    """New, active giveaways with a safe link, in feed order. Anything that isn't a list
    means "nothing this week": when no giveaway matches, GamerPower's /filter answers 201
    with an object ({"status": 0, "status_message": ...}) instead of a list."""
    if not isinstance(data, list):
        return []
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
