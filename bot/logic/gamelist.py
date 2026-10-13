"""Is the games list (server/layout.py GAMES) still what the server plays? Staff-facing only.
Pure: no Discord, no database. All times are unix seconds.

Once a week the owner digest (cogs/recap.py) compares the last 28 days of `game_sessions`
with the list and gets one field out of this module:

- "Worth adding": a game that is not on the list, played by at least MIN_PLAYERS distinct
  members for at least MIN_SECONDS combined. A retired game that people are playing again
  shows up here too, marked as retired.
- "Worth retiring": a listed game nobody played in the window.

Nothing here changes the list. Staff decide, and edit layout.py (see RETIRED_GAMES there).

Privacy: the result is counts and game names, never who played. The caller leaves out members
who opted out of tracking and bots (`exclude`); a game below MIN_PLAYERS is never named, so
one member's obscure game cannot show up. Presence names are free text written by whoever
published the game, so before one is shown it is cut to one short line, checked with the text
filter (a refused name is counted, not shown) and markdown-escaped.

When nobody played anything in the window there is nothing to compare (tracking was off, or
the bot is new), so there is no field at all instead of "retire everything".
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import config
from logic import stats as S
from logic import textfilter
from logic.engagement import game_for
from logic.gamelink import escape
from names import slug

DAY = 24 * 3600
HOUR = 3600
WINDOW = 28 * DAY
MIN_PLAYERS = 3  # distinct members before an unlisted game is named
MIN_SECONDS = 5 * HOUR  # their combined play time
MAX_SHOWN = 5  # "worth adding" names listed; the rest are counted
MAX_NAME = 60
FIELD = "Games list"
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Candidate:
    """A game people play that the list doesn't have."""

    name: str  # as the game's publisher spells it (unfiltered), or the role name of a retired game
    players: int
    seconds: int
    retired: bool = False  # it is in RETIRED_GAMES: listing it again is moving one line back


@dataclass(frozen=True)
class Review:
    played: bool  # anyone played anything at all in the window
    add: tuple[Candidate, ...]  # most hours first
    retire: tuple[str, ...]  # role names of listed games nobody played, in list order


def _group(name: str) -> str:
    """Spellings of one game fold together: 'HELLDIVERS™ 2' and 'Helldivers 2'."""
    return slug(name) or _SPACE.sub(" ", name).strip().casefold()


def review(sessions: Iterable[S.Session], start: int, end: int, now: int, *, exclude: Iterable[int] = (),
           games=None, retired=None) -> Review:
    """Compare game sessions (key = presence name) clipped to [start, end) with the list.
    `exclude`: user ids that never count (opted out of tracking, bots)."""
    games = list(config.GAMES if games is None else games)
    retired = list(config.RETIRED_GAMES if retired is None else retired)
    skip = set(exclude)
    per_user = S.game_seconds([s for s in sessions if s.user_id not in skip and isinstance(s.key, str)],
                              start, end, now)

    listed_played: set[str] = set()
    seconds: dict[str, int] = {}
    players: dict[str, set[int]] = {}
    spellings: dict[str, dict[str, int]] = {}  # group -> {name as stored: seconds}
    old: dict[str, config.Game] = {}  # group -> the retired game it is
    played = False
    for user, by_name in per_user.items():
        for name, secs in by_name.items():
            if secs <= 0:
                continue
            played = True
            game = game_for(name, games)
            if game is not None:
                listed_played.add(game.key)
                continue
            was = game_for(name, retired)
            group = f"retired:{was.key}" if was is not None else _group(name)
            if not group:
                continue
            if was is not None:
                old[group] = was
            seconds[group] = seconds.get(group, 0) + secs
            players.setdefault(group, set()).add(user)
            by_spelling = spellings.setdefault(group, {})
            by_spelling[name] = by_spelling.get(name, 0) + secs

    add = []
    for group, secs in seconds.items():
        if len(players[group]) < MIN_PLAYERS or secs < MIN_SECONDS:
            continue
        if group in old:
            add.append(Candidate(old[group].role, len(players[group]), secs, retired=True))
        else:  # the spelling with the most time behind it; ties go to the first alphabetically
            name = min(spellings[group], key=lambda n: (-spellings[group][n], n))
            add.append(Candidate(name, len(players[group]), secs))
    add.sort(key=lambda c: (-c.seconds, -c.players, c.name))
    return Review(played=played, add=tuple(add),
                  retire=tuple(g.role for g in games if g.key not in listed_played))


def safe_name(name: str) -> str | None:
    """A presence name fit to show staff: one line, cut, markdown inert. None when it is empty
    or the text filter refuses it (an invite, a link, a slur)."""
    text = _SPACE.sub(" ", "".join(ch if ch.isprintable() else " " for ch in name or "")).strip()
    if not text:
        return None
    if len(text) > MAX_NAME:
        text = text[:MAX_NAME - 1].rstrip() + "…"
    ok, _reason = textfilter.check(text, allow_links=False, invite_allowlist=())
    return escape(text) if ok else None


def _hours(seconds: int) -> str:
    return f"{round(seconds / HOUR):,} h"


def digest_text(r: Review) -> str | None:
    """The owner digest's "Games list" field, or None when there was no play to compare with."""
    if not r.played:
        return None
    days = WINDOW // DAY
    lines = []
    shown, hidden = [], 0
    for c in r.add:
        name = c.name if c.retired else safe_name(c.name)  # a retired game's name is our own, from layout.py
        if name is None:
            hidden += 1
        elif len(shown) < MAX_SHOWN:
            shown.append(f"**{name}** ({'retired, ' if c.retired else ''}{c.players} players, {_hours(c.seconds)})")
        else:
            hidden += 1
    if hidden:
        shown.append(f"{hidden} more not shown")
    if shown:
        lines.append("Worth adding: " + " · ".join(shown))
    if r.retire:
        lines.append(f"Worth retiring: {', '.join(r.retire)} (nobody played in {days} days)")
    if not lines:
        return f"No changes suggested. The list matches the last {days} days of play."
    lines.append("Nothing changes by itself: edit GAMES in server/layout.py.")
    return "\n".join(lines)
