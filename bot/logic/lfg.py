"""Squad-up rules. Pure: no Discord, no database, so it's all unit-tested."""

from dataclasses import dataclass, replace
from enum import Enum

MIN_PLAYERS = 2
MAX_PLAYERS = 10
EXPIRY_SECONDS = 3 * 60 * 60
SQUAD_VOICE_LIMIT = 5  # 🎮 Squad's user limit in layout.py
TITLE_LIMIT = 100  # Discord's thread name limit
CLOSED_PREFIX = "✓ "


class Join(Enum):
    JOINED = "joined"
    ALREADY_IN = "already_in"
    FULL = "full"


class Leave(Enum):
    LEFT = "left"
    NOT_IN = "not_in"
    HOST = "host"  # the host closes the post instead of leaving


@dataclass(frozen=True)
class Roster:
    host_id: int
    size: int
    members: tuple[int, ...]  # host first, then in join order

    @classmethod
    def new(cls, host_id: int, size: int) -> "Roster":
        return cls(host_id, size, (host_id,))

    @property
    def full(self) -> bool:
        return len(self.members) >= self.size

    def join(self, user_id: int) -> tuple["Roster", Join, bool]:
        """Returns (roster, result, became_full)."""
        if user_id in self.members:
            return self, Join.ALREADY_IN, False
        if self.full:
            return self, Join.FULL, False
        joined = replace(self, members=self.members + (user_id,))
        return joined, Join.JOINED, joined.full

    def leave(self, user_id: int) -> tuple["Roster", Leave]:
        if user_id == self.host_id:
            return self, Leave.HOST
        if user_id not in self.members:
            return self, Leave.NOT_IN
        return replace(self, members=tuple(m for m in self.members if m != user_id)), Leave.LEFT

    def resize(self, size: int) -> "Roster | None":
        """None if the size is out of range or below the current headcount."""
        if not MIN_PLAYERS <= size <= MAX_PLAYERS or size < len(self.members):
            return None
        return replace(self, size=size)


def can_close(user_id: int, host_id: int, is_keeper: bool) -> bool:
    return user_id == host_id or is_keeper


def is_expired(created_at: int, now: int) -> bool:
    return now - created_at >= EXPIRY_SECONDS


def count_label(roster: Roster) -> str:
    return f"{len(roster.members)} / {roster.size}"


def title(game_role: str, mode: str | None) -> str:
    """Set once at creation. No time or count in it: thread renames are rate-limited."""
    text = f"{game_role} · {mode}" if mode else game_role
    return text[: TITLE_LIMIT - len(CLOSED_PREFIX)]


def closed_title(current: str) -> str:
    if current.startswith(CLOSED_PREFIX):
        return current[:TITLE_LIMIT]
    return (CLOSED_PREFIX + current)[:TITLE_LIMIT]


def voice_hint(size: int) -> str:
    """Which voice channel a full squad should use: 'squad' fits 5, else 'lobby'."""
    return "squad" if size <= SQUAD_VOICE_LIMIT else "lobby"
