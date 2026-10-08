"""Matchmaking queue rules: default party sizes, queue expiry, which members a full bucket
pops, the /queue status summary and match channel names.
Pure: no Discord, no database, so it's all unit-tested."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import config
from logic.tempvoice import NAME_MAX, clean, fit
from names import slug

MIN_SIZE = 2
MAX_SIZE = 5
SIZES = tuple(range(MIN_SIZE, MAX_SIZE + 1))
FALLBACK_SIZE = 2  # games without a usual team size: a duo
QUEUE_TTL = 60 * 60  # a queue entry expires after an hour
EMPTY_GRACE = 10 * 60  # a match channel nobody is in is deleted after this long
CHANNEL_WATCH = 7 * 24 * 60 * 60  # match channels older than this are left to tempvoice/staff

# Usual team size per game key (config.Game.key, the role slug).
DEFAULT_SIZES: dict[str, int] = {
    "valorant": 5,
    "counterstrike2": 5,
    "leagueoflegends": 5,
    "fortnite": 4,
    "apexlegends": 4,
    "callofduty": 4,
}


@dataclass(frozen=True)
class Entry:
    user_id: int
    game: str
    mode: str
    size: int
    joined_at: int

    @property
    def bucket(self) -> tuple[str, str, int]:
        return (self.game, self.mode, self.size)


def default_size(game_key: str) -> int:
    return DEFAULT_SIZES.get(game_key, FALLBACK_SIZE)


def resolve_size(game_key: str, size: int | None) -> int:
    """The member's choice when given (clamped to 2-5), else the game's usual size."""
    if size is None:
        return default_size(game_key)
    return max(MIN_SIZE, min(MAX_SIZE, int(size)))


def resolve_game(text: str | None) -> config.Game | None:
    """A config.Game from an autocomplete value (its key) or a typed name; None if unknown."""
    if not text:
        return None
    key = slug(text)
    return config.game_by_key(key) or next((g for g in config.GAMES if slug(g.channel) == key), None)


def resolve_mode(text: str | None) -> str | None:
    if not text:
        return None
    return next((m for m in config.MODES if m.lower() == text.strip().lower()), None)


def expired(joined_at: int, now: int) -> bool:
    return now - joined_at >= QUEUE_TTL


def expires_in(joined_at: int, now: int) -> int:
    """Seconds left before the entry expires (0 if it already has)."""
    return max(0, joined_at + QUEUE_TTL - now)


def pick(entries: Iterable[Entry], size: int, now: int, present=None) -> list[Entry] | None:
    """The `size` longest-waiting live entries of one bucket, or None if there aren't enough.
    Expired entries and members `present` says have left (when given) never get picked."""
    live = [e for e in entries if not expired(e.joined_at, now) and (present is None or present(e.user_id))]
    if len(live) < size:
        return None
    live.sort(key=lambda e: (e.joined_at, e.user_id))
    return live[:size]


def encode_members(user_ids: Sequence[int]) -> str:
    return ",".join(str(u) for u in user_ids)


def decode_members(text: str | None) -> list[int]:
    return [int(p) for p in (text or "").split(",") if p.strip()]


@dataclass(frozen=True)
class BucketCount:
    game: str
    mode: str
    size: int
    waiting: int
    mine: bool


def summary(entries: Iterable[Entry], me: int, now: int) -> list[BucketCount]:
    """Live entries counted per bucket, in game order (config.GAMES) then mode then size.
    `mine` marks the bucket `me` is waiting in (the only one whose names are shown)."""
    counts: dict[tuple[str, str, int], int] = {}
    my_bucket = None
    for e in entries:
        if expired(e.joined_at, now):
            continue
        counts[e.bucket] = counts.get(e.bucket, 0) + 1
        if e.user_id == me:
            my_bucket = e.bucket
    order = {g.key: i for i, g in enumerate(config.GAMES)}
    modes = {m: i for i, m in enumerate(config.MODES)}
    keys = sorted(counts, key=lambda b: (order.get(b[0], len(order)), b[0], modes.get(b[1], len(modes)), b[2]))
    return [BucketCount(g, m, s, counts[(g, m, s)], (g, m, s) == my_bucket) for g, m, s in keys]


def channel_name(game: config.Game | None, game_key: str) -> str:
    """'🎯 Valorant match', always 1-100 characters."""
    if game is None:
        label = clean(game_key) or "Game"
        return fit(f"🎮 {label} match", NAME_MAX)
    suffix = " match"
    head = f"{game.emoji} "
    room = NAME_MAX - len((head + suffix).encode("utf-16-le")) // 2
    return head + fit(clean(game.role) or "Game", room) + suffix


def empty_for(empty_since: Mapping[int, float], channel_id: int, now: float) -> float:
    """Seconds a channel has been seen empty (0 if it isn't known to be empty)."""
    since = empty_since.get(channel_id)
    return 0 if since is None else max(0, now - since)
