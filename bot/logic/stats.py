"""Stats math: counted voice time, game time, rankings. Pure: no Discord, no database."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

MIN_GAME_SECONDS = 5 * 60


@dataclass(frozen=True)
class Session:
    """A voice or game session as stored. All times are unix seconds."""

    user_id: int
    key: int | str  # voice: channel_id; game: game name
    start: int
    end: int | None  # None = still open


@dataclass(frozen=True)
class Ranked:
    rank: int  # competition ranking: 1, 2, 2, 4
    user_id: int
    score: int | float


def clip(session: Session, window_start: int, window_end: int, now: int) -> tuple[int, int] | None:
    """Overlap of [start, end or now) with [window_start, window_end); None if empty."""
    end = now if session.end is None else session.end
    lo, hi = max(session.start, window_start), min(end, window_end)
    return (lo, hi) if lo < hi else None


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of half-open intervals, so duplicate rows don't double count."""
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _merged_by_user_key(
    sessions: Iterable[Session], window_start: int, window_end: int, now: int
) -> dict[tuple[int, int | str], list[tuple[int, int]]]:
    raw: dict[tuple[int, int | str], list[tuple[int, int]]] = defaultdict(list)
    for s in sessions:
        span = clip(s, window_start, window_end, now)
        if span:
            raw[(s.user_id, s.key)].append(span)
    return {k: _merge(v) for k, v in raw.items()}


def counted_voice_seconds(
    sessions: Iterable[Session],
    window_start: int,
    window_end: int,
    now: int,
    excluded_channels: set[int] = frozenset(),
    bot_ids: set[int] = frozenset(),
) -> dict[int, int]:
    """Per user, seconds in a voice channel while 2+ non-bot users were in that same channel."""
    kept = (s for s in sessions if s.key not in excluded_channels and s.user_id not in bot_ids)
    events: dict[int | str, list[tuple[int, int, int]]] = defaultdict(list)
    for (user, channel), spans in _merged_by_user_key(kept, window_start, window_end, now).items():
        for lo, hi in spans:
            # Leaves (-1) sort before joins (+1) at the same instant.
            events[channel].append((lo, 1, user))
            events[channel].append((hi, -1, user))

    totals: dict[int, int] = defaultdict(int)
    for channel_events in events.values():
        channel_events.sort(key=lambda e: (e[0], e[1]))
        present: set[int] = set()
        prev = None
        for t, delta, user in channel_events:
            if prev is not None and t > prev and len(present) >= 2:
                for u in present:
                    totals[u] += t - prev
            if delta > 0:
                present.add(user)
            else:
                present.discard(user)
            prev = t
    return {u: secs for u, secs in totals.items() if secs > 0}


def game_seconds(
    sessions: Iterable[Session],
    window_start: int,
    window_end: int,
    now: int,
    min_seconds: int = MIN_GAME_SECONDS,
) -> dict[int, dict[str, int]]:
    """Per user, per game: clipped seconds. Sessions shorter than min_seconds (full length) are ignored."""
    long_enough = (
        s for s in sessions if (now if s.end is None else s.end) - s.start >= min_seconds
    )
    result: dict[int, dict[str, int]] = defaultdict(dict)
    for (user, game), spans in _merged_by_user_key(long_enough, window_start, window_end, now).items():
        result[user][game] = sum(hi - lo for lo, hi in spans)
    return dict(result)


def gaming_totals(per_user: dict[int, dict[str, int]]) -> dict[int, int]:
    return {user: sum(games.values()) for user, games in per_user.items()}


def top_games(per_game: dict[str, int], n: int = 3) -> list[tuple[str, int]]:
    """By seconds desc, then name."""
    return sorted(per_game.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def _ranked(scores: dict[int, int | float]) -> list[Ranked]:
    ordered = sorted(((u, s) for u, s in scores.items() if s > 0), key=lambda us: (-us[1], us[0]))
    out: list[Ranked] = []
    for i, (user, score) in enumerate(ordered):
        r = out[-1].rank if out and out[-1].score == score else i + 1
        out.append(Ranked(r, user, score))
    return out


def rank(
    scores: dict[int, int | float], limit: int = 10, me: int | None = None
) -> tuple[list[Ranked], Ranked | None]:
    """Returns (top `limit`, my entry if I'm ranked but outside the top list, else None)."""
    everyone = _ranked(scores)
    top = everyone[:limit]
    mine = next((r for r in everyone[limit:] if r.user_id == me), None)
    return top, mine


def mvp(boards: dict[str, dict[int, int | float]], tiebreak_board: str = "voice") -> int | None:
    """Lowest rank-sum across boards (absent = last + 1); ties: tiebreak_board score, then user_id."""
    ranks = {name: {r.user_id: r.rank for r in _ranked(scores)} for name, scores in boards.items()}
    users = {u for board in ranks.values() for u in board}
    if not users:
        return None

    def rank_sum(user: int) -> int:
        return sum(board.get(user, len(board) + 1) for board in ranks.values())

    tiebreak = boards.get(tiebreak_board, {})
    return min(users, key=lambda u: (rank_sum(u), -tiebreak.get(u, 0), u))


def fmt_duration(seconds: int) -> str:
    """0 -> "0m", 3600 -> "1h 0m". Rounds down to the minute."""
    hours, minutes = divmod(max(seconds, 0) // 60, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"
