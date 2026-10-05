"""Module 12: Utility rules. Pure: no Discord, no database.

Reminders (when-text parsing, limits, wording), AFK notices and their cooldown,
suggestion-forum tags, stat-channel names and rename pacing, and ticket names.
"""

import re
from datetime import date, datetime, time, timedelta, tzinfo

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR

# ---------------------------------------------------------------- reminders
REMINDER_TEXT_MAX = 200
REMINDER_MAX_ACTIVE = 10
REMINDER_MIN_AHEAD = MINUTE
REMINDER_MAX_AHEAD = 30 * DAY
DEFAULT_HOUR = 9  # "tomorrow" with no time means 9am
TONIGHT_HOUR = 20  # "tonight" with no time means 8pm


class WhenError(ValueError):
    """A when-text that can't be used; str(error) is the reply for the member."""


UNITS = {
    "w": 7 * DAY, "wk": 7 * DAY, "wks": 7 * DAY, "week": 7 * DAY, "weeks": 7 * DAY,
    "d": DAY, "day": DAY, "days": DAY,
    "h": HOUR, "hr": HOUR, "hrs": HOUR, "hour": HOUR, "hours": HOUR,
    "m": MINUTE, "min": MINUTE, "mins": MINUTE, "minute": MINUTE, "minutes": MINUTE,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}
_UNIT = "|".join(sorted(UNITS, key=len, reverse=True))
_PART = rf"(\d{{1,5}})\s*({_UNIT})(?![a-z])"
_RELATIVE = re.compile(rf"^(?:in\s+)?(?:{_PART}[\s,]*(?:and\s+)?)+$")
_PARTS = re.compile(_PART)
_TIME = re.compile(r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")
WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
DAY_WORDS = {"today", "tonight", "tomorrow", "tmrw", "tmr", *WEEKDAYS}

HELP = 'Try "10m", "2h", "1d 6h", "tomorrow", "tomorrow 9am", "9pm" or "friday 18:30".'


def _clock(text: str, has_day: bool) -> tuple[int, int] | None:
    """'9am' / '9:30 pm' / '21:00' / 'noon' -> (hour, minute). A bare '9' only counts
    next to a day word ('tomorrow 9'), since on its own it's probably a typo."""
    if text in ("noon", "midday"):
        return 12, 0
    if text == "midnight":
        return 0, 0
    m = _TIME.match(text)
    if not m:
        return None
    hour, minute, ampm = int(m[1]), int(m[2] or 0), m[3]
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif hour > 23 or (m[2] is None and not has_day):
        return None
    return hour, minute


def _split_day(text: str) -> tuple[str | None, str]:
    """Pull a leading or trailing day word off: 'tomorrow at 9am' -> ('tomorrow', 'at 9am')."""
    words = text.split()
    if words and words[0] == "next" and len(words) > 1 and words[1] in WEEKDAYS:
        words = words[1:]  # "next friday" == "friday"
    if words and words[0] in DAY_WORDS:
        return words[0], " ".join(words[1:])
    if words and words[-1] in DAY_WORDS:
        return words[-1], " ".join(words[:-1])
    return None, text


def _local(day: date, hour: int, minute: int, tz: tzinfo) -> int:
    return int(datetime.combine(day, time(hour, minute), tzinfo=tz).timestamp())


def _absolute(text: str, now: int, tz: tzinfo) -> int | None:
    day_word, rest = _split_day(text)
    rest = rest.strip()
    if day_word is None and not rest:
        return None
    if rest:
        clock = _clock(rest, has_day=day_word is not None)
        if clock is None:
            return None
    else:
        clock = (TONIGHT_HOUR if day_word == "tonight" else DEFAULT_HOUR, 0)
    today = datetime.fromtimestamp(now, tz).date()
    if day_word in ("today", "tonight"):
        return _local(today, *clock, tz)
    if day_word in ("tomorrow", "tmrw", "tmr"):
        return _local(today + timedelta(days=1), *clock, tz)
    if day_word in WEEKDAYS:
        ahead = (WEEKDAYS[day_word] - today.weekday()) % 7
        due = _local(today + timedelta(days=ahead), *clock, tz)
        return due if due > now else _local(today + timedelta(days=ahead + 7), *clock, tz)
    # A time on its own: the next time the clock shows it.
    due = _local(today, *clock, tz)
    return due if due > now else _local(today + timedelta(days=1), *clock, tz)


def parse_when(text: str, now: int, tz: tzinfo) -> int:
    """When a reminder is due (unix seconds). Raises WhenError with a member-facing reason."""
    clean = " ".join((text or "").lower().replace(",", " ").split())
    if not clean:
        raise WhenError(f"Tell me when. {HELP}")
    if _RELATIVE.match(clean):
        due = now + sum(int(n) * UNITS[u] for n, u in _PARTS.findall(clean))
    else:
        due = _absolute(clean, now, tz)
        if due is None:
            raise WhenError(f"I couldn't read \"{text.strip()[:40]}\" as a time. {HELP}")
    if due - now < REMINDER_MIN_AHEAD:
        raise WhenError("That's less than a minute away. Pick a time at least a minute from now.")
    if due - now > REMINDER_MAX_AHEAD:
        raise WhenError("Reminders can be at most 30 days ahead.")
    return due


def check_reminder(text: str, active: int) -> str | None:
    """Why a new reminder can't be saved, or None."""
    if not (text or "").strip():
        return "What should I remind you about?"
    if len(text.strip()) > REMINDER_TEXT_MAX:
        return f"Keep it to {REMINDER_TEXT_MAX} characters."
    if active >= REMINDER_MAX_ACTIVE:
        return (f"You already have {REMINDER_MAX_ACTIVE} reminders waiting. "
                "Cancel one with `/reminders cancel` first.")
    return None


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def reminder_message(user_id: int, text: str, created_at: int) -> str:
    return f"⏰ <@{user_id}>, you asked me to remind you: {text} (set <t:{created_at}:R>)"


def reminder_line(rid: int, due_at: int, text: str) -> str:
    return f"`#{rid}`  <t:{due_at}:R>  {short(text, 80)}"


def choice_label(rid: int, due_at: int, now: int, text: str) -> str:
    """Autocomplete label (plain text, max 100): '#12 · in 2h 5m · buy milk'."""
    return short(f"#{rid} · in {span(due_at - now)} · {text}", 100)


def span(seconds: int) -> str:
    seconds = max(0, seconds)
    d, rem = divmod(seconds, DAY)
    h, rem = divmod(rem, HOUR)
    m = rem // MINUTE
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{max(m, 1)}m"


# Delivery that keeps failing gives up after this long, so one dead channel can't
# make the loop retry forever.
REMINDER_GIVE_UP = DAY


# ---------------------------------------------------------------- AFK
AFK_REASON_MAX = 100
AFK_COOLDOWN = 10 * MINUTE  # at most one notice per AFK member per channel


def afk_notice(name: str, reason: str | None, since: int) -> str:
    reason = (reason or "").strip()
    head = f"{name} is AFK: {reason}" if reason else f"{name} is AFK"
    return f"💤 {head} (since <t:{since}:R>)"


def welcome_back(name: str, since: int) -> str:
    return f"👋 Welcome back, {name}. I removed your AFK (you were away since <t:{since}:R>)."


def afk_due(last_notice: int | None, now: int) -> bool:
    return last_notice is None or now - last_notice >= AFK_COOLDOWN


# ---------------------------------------------------------------- suggestions
IDEA_TAG = "Idea"
STATUS_TAGS = {"accepted": "Accepted", "denied": "Denied", "done": "Done"}
STATUS_EMOJI = {"accepted": "✅", "denied": "❌", "done": "🎉"}
STATUS_NOTE_MAX = 500
MAX_TAGS = 5  # Discord's limit per forum post
VOTES = ("👍", "👎")


def _same(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def retag(current: list[str], status: str) -> list[str]:
    """Tag names after a mod sets `status`: the old status/Idea tag goes, any other tags
    stay (up to Discord's limit), and the new status tag comes first."""
    ours = [IDEA_TAG, *STATUS_TAGS.values()]
    keep = [t for t in current if not any(_same(t, o) for o in ours)]
    return [STATUS_TAGS[status], *keep][:MAX_TAGS]


def status_text(status: str, mod_mention: str, note: str | None) -> str:
    line = f"{STATUS_EMOJI[status]} **{STATUS_TAGS[status]}** by {mod_mention}"
    note = (note or "").strip()
    return f"{line}\n> {note}" if note else line


# ---------------------------------------------------------------- stat channels
STAT_FORMATS = {"members": "👥 Members: {n}", "online": "🟢 Online: {n}"}
STAT_PREFIXES = {"members": "members:", "online": "online:"}
RENAME_EVERY = 10 * MINUTE  # Discord allows 2 channel renames per 10 minutes


def stat_name(kind: str, n: int) -> str:
    return STAT_FORMATS[kind].format(n=f"{n:,}")


def is_stat_channel(name: str, kind: str) -> bool:
    """Whether an existing channel is our `kind` counter, whatever emoji or number it shows."""
    letters = re.sub(r"^[^a-z]+", "", name.lower())
    return letters.startswith(STAT_PREFIXES[kind])


def rename_due(current: str, wanted: str, last_renamed: int | None, now: int) -> bool:
    if current == wanted:
        return False
    return last_renamed is None or now - last_renamed >= RENAME_EVERY


# ---------------------------------------------------------------- tickets
TICKET_PREFIX = "ticket-"


def ticket_name(username: str) -> str:
    clean = re.sub(r"[^a-z0-9_.-]+", "-", (username or "").lower()).strip("-.") or "member"
    return (TICKET_PREFIX + clean)[:100]


def can_close_ticket(user_id: int, owner_id: int, staff: bool) -> bool:
    return staff or user_id == owner_id
