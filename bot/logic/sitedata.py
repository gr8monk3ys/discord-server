"""Live data for the landing page: one small JSON document of server-wide aggregates.
Pure: no Discord, no database. All times are unix seconds.

cogs/sitedata.py gathers the numbers every 30 minutes and `build()` turns them into the
document server/publish_site_data.py later pushes to the public `site-data` branch. The page
is public and so is the repo, so this module is where the privacy rules live:

- Aggregates only. `Inputs` has no field for a member name, id or avatar, and the schema
  (server/site_schema.py, shared with the publisher) refuses anything ID-like or mention-like.
- Small-group rule: a number that describes fewer than `MIN_GROUP` people is left out. The
  gatherer passes how many distinct members stand behind each number; `build()` drops the rest.
- Rounding: messages to the nearest 10, voice and game time to whole hours.
- Game names come from layout.py only. A presence name that is not one of the server's games
  is ignored, so nobody's obscure game shows up.
- Event titles: only the ones the bot templates itself, rebuilt here and never copied: "Game
  night" / "<Game> game night", "Town Hall", "Watch party" (what is being watched is the host's
  own words, so it is left off) and "<Game> tournament" / "Tournament" for an open or running
  tournament (its name is free text too). An event with any other title was made by hand and
  is dropped. Events in a channel @everyone cannot see are dropped.
- Houses: the three house names from layout.py and each house's points per member, rounded to
  a whole point. Published only when every house has at least `MIN_GROUP` members who scored
  this week; otherwise the whole table is left out, so no number is ever one or two members'
  activity (and a missing house cannot be worked out from the others).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

import config
import site_schema as schema  # server/site_schema.py; config puts server/ on sys.path
from logic import events, textfilter
from names import slug

MIN_GROUP = 3  # an aggregate about fewer people than this is not published
INTERVAL = 30 * 60  # seconds between refreshes
WINDOW = 7 * 24 * 3600  # "this week" is the last 7 days
EVENT_HORIZON = 14 * 24 * 3600  # upcoming events this far ahead
MESSAGE_STEP = 10
HOUR = 3600
MIN_PREFIX = 5  # a presence name may extend a game key at least this long ("minecraftlauncher")
GAMENIGHT = "Game night"
TOWNHALL = events.TOWNHALL_NAME
WATCHPARTY = "Watch party"
WATCHPARTY_PREFIX = events.watchparty_name("").strip()  # "Watch party:", as the events module spells it
TOURNAMENT = "Tournament"
_SPACE = re.compile(r"\s+")


def due(last_run: int | None, now: int, interval: int = INTERVAL) -> bool:
    """Run when nothing ran yet (startup: catch up once) or the interval has passed."""
    return last_run is None or now - last_run >= interval or now < last_run


def round_to(n: int | float, step: int) -> int:
    """Nearest multiple of `step`, halves up."""
    return int((n + step / 2) // step) * step


def hours(seconds: int | float) -> int:
    return round_to(seconds, HOUR) // HOUR


# ---------------------------------------------------------------- sessions
def clipped(start: int, end: int | None, lo: int, hi: int, now: int) -> int:
    """Seconds of a session (open when `end` is None) that fall inside [lo, hi)."""
    stop = now if end is None else end
    return max(0, min(stop, hi) - max(start, lo))


def session_total(sessions: Iterable[tuple[int, int, int | None]], lo: int, hi: int, now: int) -> tuple[int, int]:
    """(seconds, distinct people) for (user, start, end) sessions clipped to [lo, hi)."""
    total, people = 0, set()
    for user, start, end in sessions:
        secs = clipped(start, end, lo, hi, now)
        if secs > 0:
            total += secs
            people.add(user)
    return total, len(people)


def match_game(name: str | None, games=None) -> config.Game | None:
    """The layout game a presence or tournament game name means, or None.
    'VALORANT' and 'Counter-Strike 2' match exactly; 'Minecraft Launcher' by prefix."""
    games = config.GAMES if games is None else games
    key = slug(name or "")
    if not key:
        return None
    exact = next((g for g in games if g.key == key), None)
    if exact is not None:
        return exact
    prefixed = [g for g in games if len(g.key) >= MIN_PREFIX and key.startswith(g.key)]
    return max(prefixed, key=lambda g: len(g.key)) if prefixed else None


def game_table(sessions: Iterable[tuple[int, str, int, int | None]], lo: int, hi: int, now: int,
               games=None) -> list[tuple[str, int, int]]:
    """(game role name, seconds, distinct players) per layout game from (user, presence name,
    start, end) sessions. Names that are not a layout game are dropped here."""
    seconds: dict[str, int] = {}
    players: dict[str, set[int]] = {}
    for user, name, start, end in sessions:
        game = match_game(name, games)
        secs = clipped(start, end, lo, hi, now)
        if game is None or secs <= 0:
            continue
        seconds[game.role] = seconds.get(game.role, 0) + secs
        players.setdefault(game.role, set()).add(user)
    return [(role, secs, len(players[role])) for role, secs in seconds.items()]


# ---------------------------------------------------------------- event titles
def clean_title(title: str | None) -> str | None:
    """A member-authored title that is safe to publish, or None (dropped if in doubt)."""
    text = _SPACE.sub(" ", title or "").strip()
    if not text or len(text) > schema.MAX_TITLE:
        return None
    ok, _reason = textfilter.check(text, allow_links=False, invite_allowlist=())
    if not ok or schema.text_problem(text):
        return None
    return text


@dataclass(frozen=True)
class EventIn:
    """A scheduled server event as Discord shows it."""

    title: str
    start: int
    active: bool = False  # already started
    public: bool = True  # False: it sits in a channel @everyone cannot see


def classify_event(title: str, tournaments: dict[str, str | None], games=None) -> dict | None:
    """{"title", "kind"[, "game"]} for a scheduled event's title, or None to drop it. The
    title that comes back is always one of the bot's own templates, never the text it was given.
    tournaments: lower-cased name -> game text, for tournaments that are open or running."""
    games = config.GAMES if games is None else games
    plain = _SPACE.sub(" ", title or "").strip()
    folded = plain.casefold()
    if folded == GAMENIGHT.casefold():
        return {"title": GAMENIGHT, "kind": "gamenight"}
    for g in games:  # the bot's own template: rebuilt from the game name, not copied
        if folded == f"{g.role} game night".casefold():
            return {"title": f"{g.role} game night", "kind": "gamenight", "game": g.role}
    if folded == TOWNHALL.casefold():
        return {"title": TOWNHALL, "kind": "event"}
    if folded.startswith(WATCHPARTY_PREFIX.casefold()):  # "Watch party: <the host's words>"
        return {"title": WATCHPARTY, "kind": "event"}
    if folded in tournaments:  # the name is whatever its organiser typed: say which game instead
        game = match_game(tournaments[folded], games)
        if game is None:
            return {"title": TOURNAMENT, "kind": "tournament"}
        return {"title": f"{game.role} tournament", "kind": "tournament", "game": game.role}
    return None


# ---------------------------------------------------------------- the document
@dataclass(frozen=True)
class Inputs:
    """Everything the cog gathered. Every `*_people` is how many distinct members stand
    behind the number next to it; None for a count means "could not tell"."""

    now: int
    member_count: int | None = None
    online: int | None = None
    messages: int = 0
    message_people: int = 0
    voice_seconds: int = 0
    voice_people: int = 0
    events: list = field(default_factory=list)  # [EventIn]
    active_tournaments: dict = field(default_factory=dict)  # lower-cased name -> game text
    games: list = field(default_factory=list)  # [(role name, seconds, players)] from game_table
    tournaments: dict = field(default_factory=dict)  # status -> count ('signup', 'running', 'done')
    matches: int = 0
    match_people: int = 0
    queued: int = 0
    seasonal: tuple | None = None  # (name, emoji, end)
    houses: list | None = None  # [(name, points per member, members who scored)], best first; None: no houses
    starred: int = 0
    starred_people: int = 0
    clips: int = 0
    clip_people: int = 0
    badges: int = 0
    badge_people: int = 0


def _events(inp: Inputs, games) -> list[dict]:
    rows = []
    for e in sorted(inp.events, key=lambda e: (e.start, e.title)):
        if not e.public or not (e.active or inp.now <= e.start <= inp.now + EVENT_HORIZON):
            continue
        item = classify_event(e.title, inp.active_tournaments, games)
        if item is not None:
            rows.append({**item, "start": schema.iso(e.start)})
    return rows[:schema.MAX_EVENTS]


def _games(inp: Inputs, games) -> list[dict]:
    allowed = {g.role for g in games}  # game_table already did this; never trust the caller
    rows = [(name, hours(secs)) for name, secs, players in inp.games
            if players >= MIN_GROUP and name in allowed]
    rows = sorted(((n, h) for n, h in rows if h > 0), key=lambda r: (-r[1], r[0]))
    return [{"name": n, "hours": h} for n, h in rows[:schema.MAX_GAMES]]


def _houses(inp: Inputs) -> list[dict]:
    rows = []
    for name, points, people in inp.houses or []:
        text = clean_title(name) if isinstance(name, str) else None
        if (text is None or len(text) > schema.MAX_NAME or isinstance(points, bool)
                or not isinstance(points, int) or not 0 <= points <= schema.MAX_COUNT):
            return []  # one odd row: publish no standings rather than a partial table
        # The small-group rule, for the whole table: a score built on one or two members'
        # week is theirs, not the house's, and leaving just that house out would say as much.
        if isinstance(people, bool) or not isinstance(people, int) or people < MIN_GROUP:
            return []
        rows.append((text, points))
    if not 2 <= len(rows) <= schema.MAX_HOUSES:
        return []
    # Stable: houses level after rounding keep the order the houses module ranked them in.
    return [{"name": n, "points": p} for n, p in sorted(rows, key=lambda r: -r[1])]


def build(inp: Inputs, games=None) -> dict:
    """The stats.json document. Keys with nothing (or too few people) behind them are absent."""
    games = config.GAMES if games is None else games
    doc: dict = {"v": schema.VERSION, "generated_at": schema.iso(inp.now)}

    if inp.member_count is not None and inp.member_count >= MIN_GROUP:
        doc["members"] = {"total": inp.member_count}
        if inp.online is not None and MIN_GROUP <= inp.online <= inp.member_count:
            doc["members"]["online"] = inp.online

    week = {}
    if inp.message_people >= MIN_GROUP and round_to(inp.messages, MESSAGE_STEP) > 0:
        week["messages"] = round_to(inp.messages, MESSAGE_STEP)
    if inp.voice_people >= MIN_GROUP and hours(inp.voice_seconds) > 0:
        week["voice_hours"] = hours(inp.voice_seconds)
    if week:
        doc["week"] = week

    events = _events(inp, games)
    if events:
        doc["events"] = events
    played = _games(inp, games)
    if played:
        doc["games"] = played

    # How many tournaments exist is about the server, not about any member.
    names = (("open", "signup"), ("running", "running"), ("finished", "done"))
    tournaments = {k: int(inp.tournaments.get(status, 0)) for k, status in names}
    if any(tournaments.values()):
        doc["tournaments"] = tournaments

    mm = {}
    if inp.matches > 0 and inp.match_people >= MIN_GROUP:
        mm["matches_week"] = inp.matches
    if inp.queued >= MIN_GROUP:
        mm["queued"] = inp.queued
    if mm:
        doc["matchmaking"] = mm

    if inp.seasonal is not None:
        name, emoji, end = inp.seasonal
        if end > inp.now and isinstance(name, str) and name.strip():
            doc["seasonal"] = {"name": name.strip()[:schema.MAX_NAME], "ends": schema.iso(end)}
            if isinstance(emoji, str) and emoji.strip():
                doc["seasonal"]["emoji"] = emoji.strip()[:schema.MAX_EMOJI]

    houses = _houses(inp)
    if houses:
        doc["houses"] = houses

    hall = {}
    for key, n, people in (("starred", inp.starred, inp.starred_people), ("clips", inp.clips, inp.clip_people),
                           ("badges", inp.badges, inp.badge_people)):
        if n > 0 and people >= MIN_GROUP:
            hall[key] = n
    if hall:
        doc["hall"] = hall
    return doc


class Unsafe(ValueError):
    """The document failed the schema or the leak check; the message lists where."""


def render(doc: dict, game_names=None) -> str:
    """The file's text. Raises Unsafe instead of returning anything the publisher would refuse."""
    text = schema.dumps(doc)
    _doc, problems = schema.check_text(text, schema.GAME_NAMES if game_names is None else game_names)
    if problems:
        raise Unsafe("; ".join(problems))
    return text


def summary(doc: dict) -> str:
    """One line for staff: which sections made it in (no values needed)."""
    parts = [k for k in schema.TOP_KEYS if k in doc and k not in ("v", "generated_at")]
    return ", ".join(parts) if parts else "nothing yet (every section is empty or below the small-group limit)"
