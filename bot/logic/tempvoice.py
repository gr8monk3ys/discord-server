"""Join-to-create voice rules: channel names, per-member cooldown, the rename limit,
who inherits a channel, and who may run /squad commands.
Pure: no Discord, no database, so it's all unit-tested."""

import math
import re
import unicodedata
from collections import deque
from collections.abc import Mapping, Sequence
from enum import Enum

import config
from names import slug

PREFIX = "🎮 "
SUFFIX = "'s squad"
NAME_MAX = 100  # Discord's channel name limit (counted here in UTF-16 units, the strict reading)
CREATE_COOLDOWN = 30  # seconds between channel creations per member
EMPTY_DELAY = 30  # seconds an empty channel lives before it's deleted
RENAME_LIMIT = 2  # Discord allows about 2 renames...
RENAME_WINDOW = 10 * 60  # ...per channel per 10 minutes

# A temp channel must never look like a permanent one to config.match_by_name.
RESERVED = {slug(n) for n in (config.NEW_SQUAD_VOICE, config.SQUAD_VOICE, config.LOBBY_VOICE)}


# ---------------------------------------------------------------- names
def clean(text: str | None) -> str:
    """Drop control characters (keeping zero-width joiners for emoji) and collapse whitespace."""
    if not text:
        return ""
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in text)
    return re.sub(r"\s+", " ", text).strip()


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def fit(text: str, limit: int) -> str:
    """Cut `text` to at most `limit` UTF-16 units, on a code point boundary."""
    while _units(text) > limit:
        text = text[:-1]
    return text.rstrip()


def channel_name(game: str | None, display_name: str) -> str:
    """'🎮 Valorant' while playing, else '🎮 Bob's squad'; always 1-100 characters."""
    game = clean(game)
    if game and slug(game) not in RESERVED:
        return fit(PREFIX + game, NAME_MAX)
    who = clean(display_name) or "Someone"
    room = NAME_MAX - _units(PREFIX) - _units(SUFFIX)
    return PREFIX + fit(who, room) + SUFFIX


def clean_rename(text: str) -> str | None:
    """A /squad name value ready for Discord, or None if it's empty or reserved."""
    name = fit(clean(text), NAME_MAX)
    if not name or slug(name) in RESERVED:
        return None
    return name


# ---------------------------------------------------------------- limits
class Cooldown:
    """One action per key per `seconds` (in memory; a restart resets it)."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.last: dict[int, float] = {}

    def active(self, key: int, now: float) -> bool:
        last = self.last.get(key)
        return last is not None and now - last < self.seconds

    def take(self, key: int, now: float) -> bool:
        """True (and starts the cooldown) if the key is free."""
        if self.active(key, now):
            return False
        self.last[key] = now
        return True


class WindowLimit:
    """At most `limit` events per key in any `window` seconds."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self.events: dict[int, deque[float]] = {}

    def _recent(self, key: int, now: float) -> deque[float]:
        q = self.events.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        return q

    def wait(self, key: int, now: float) -> float:
        """Seconds until another event is allowed (0 = now)."""
        q = self._recent(key, now)
        if len(q) < self.limit:
            return 0
        return q[0] + self.window - now

    def record(self, key: int, now: float) -> None:
        self._recent(key, now).append(now)

    def forget(self, key: int) -> None:
        self.events.pop(key, None)


def minutes(seconds: float) -> str:
    n = max(1, math.ceil(seconds / 60))
    return f"{n} minute" + ("" if n == 1 else "s")


# ---------------------------------------------------------------- ownership
def next_owner(present: Sequence[int], joined_at: Mapping[int, float], exclude: int | None = None) -> int | None:
    """Whoever has been in the channel longest; anyone if join times are unknown."""
    candidates = [u for u in present if u != exclude]
    if not candidates:
        return None
    known = [u for u in candidates if u in joined_at]
    if known:
        return min(known, key=lambda u: joined_at[u])  # min is stable: ties keep `present` order
    return candidates[0]


class Denied(Enum):
    NOT_IN_TEMP = "not_in_temp"
    NOT_OWNER = "not_owner"
    ALREADY_OWNER = "already_owner"
    OWNER_PRESENT = "owner_present"


OWNER_ACTIONS = {"name", "limit"}


def authorize(action: str, *, user_id: int, owner_id: int | None, owner_present: bool) -> Denied | None:
    """Why `user_id` can't run /squad `action` in their current channel (None = allowed).
    `owner_id` is None when they aren't in a temp channel."""
    if action not in OWNER_ACTIONS | {"claim"}:
        raise ValueError(f"unknown action {action!r}")
    if owner_id is None:
        return Denied.NOT_IN_TEMP
    if action in OWNER_ACTIONS:
        return None if user_id == owner_id else Denied.NOT_OWNER
    if user_id == owner_id:
        return Denied.ALREADY_OWNER
    if owner_present:
        return Denied.OWNER_PRESENT
    return None
