"""Tonight: one quiet line in general while people are together in voice. Pure: no Discord
I/O, no database, no clock of its own (the cog passes `now`, the rooms and the saved state in).

- A room is a public voice channel with at least MIN_PEOPLE non-bot members in it. The line
  shows counts, never names, and the channel as a mention (no channel or member text is copied).
- The line appears once a room has held for HOLD, is edited at most once per EDIT_GAP and only
  when its text changes, and turns into a past-tense line once the rooms have been empty for
  HOLD (so a group moving between channels does not flip it).
- A session keeps its message for REUSE after it ended: people who come back within that time
  get the same message edited back. At most MAX_PER_DAY new messages per local day.
- Stacking: a new session edits the previous session's line instead of posting when no member
  has written in general since that line. Nothing is posted within REVIVAL_GAP of the chat
  revival (cogs/vibes.py), which in turn stays quiet for REVIVAL_GAP after this line.
- A message a moderator deleted is not posted again for that session.

The whole state is one JSON value in `meta` (STATE_KEY). Nothing about members is stored.
"""

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, tzinfo

import discord

from logic import textfilter
from logic.sitedata import match_game
from names import slug

MINUTE = 60
HOUR = 60 * MINUTE

MIN_PEOPLE = 2
HOLD = 3 * MINUTE  # a room must last this long before the line appears; empty this long to end
EDIT_GAP = 2 * MINUTE  # between two writes to the message (edits, and retries after a failure)
REUSE = 45 * MINUTE  # people back within this time continue the same session and message
MAX_PER_DAY = 3  # new messages per local day
DOWN_GAP = 5 * MINUTE  # no tick for this long: the bot was down, the open stretch ended back then
REVIVAL_GAP = HOUR  # this line and the chat revival never both fire within this time
MAX_GAME_NAME = 40
MAX_ROOMS_SHOWN = 3

STATE_KEY = "tonight:state"  # meta: the State below as JSON
FIRED_KEY = "tonight:fired_at"  # meta: when a session's line last appeared (cogs/vibes.py reads it)


# ---------------------------------------------------------------- game
NONE, ONE, MIXED = "none", "one", "mixed"


@dataclass(frozen=True)
class Pick:
    kind: str  # NONE | ONE | MIXED
    name: str | None = None  # printable, only with ONE


def printable(name: str) -> str | None:
    """A presence name that is not one of the server's games, safe to show, or None.
    Presence names are set by the game or by anything pretending to be one."""
    if len(name) > MAX_GAME_NAME or any(c in name for c in "<>@#`\\|"):
        return None
    ok, _reason = textfilter.check(name, allow_links=False, invite_allowlist=())
    return discord.utils.escape_markdown(name) if ok else None


def game_label(name: str | None) -> tuple[str, str | None] | None:
    """(group key, printable name or None) for a presence name; None when there is none.
    A server game (the stats mapping, logic/sitedata.match_game) shows its own name."""
    text = " ".join((name or "").split())
    if not text:
        return None
    game = match_game(text)
    if game is not None:
        return f"game:{game.key}", game.role
    return f"other:{slug(text) or text.casefold()}", printable(text)


def pick_game(names: Iterable[str | None]) -> Pick:
    """What the room is playing. The game more of them share than any other is named; a tie
    is "a few different things"; a name that cannot be shown is never printed."""
    counts: dict[str, int] = {}
    shown: dict[str, str | None] = {}
    for name in names:
        label = game_label(name)
        if label is None:
            continue
        counts[label[0]] = counts.get(label[0], 0) + 1
        shown.setdefault(label[0], label[1])
    if not counts:
        return Pick(NONE)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ordered) > 1 and ordered[1][1] == ordered[0][1]:
        return Pick(MIXED)
    name = shown[ordered[0][0]]
    if name is not None:
        return Pick(ONE, name)
    return Pick(MIXED) if len(ordered) > 1 else Pick(NONE)


# ---------------------------------------------------------------- text
@dataclass(frozen=True)
class Room:
    channel_id: int
    people: int  # non-bot members in the channel, opted out or not
    games: tuple[str | None, ...] = ()  # presence names of the members whose presence may be read


def open_rooms(rooms: Iterable[Room]) -> list[Room]:
    """Rooms that count, busiest first."""
    return sorted((r for r in rooms if r.people >= MIN_PEOPLE), key=lambda r: (-r.people, r.channel_id))


def live_text(rooms: list[Room]) -> str:
    """`rooms` as open_rooms() returns them, not empty."""
    main, others = rooms[0], rooms[1:MAX_ROOMS_SHOWN]
    pick = pick_game(main.games)
    playing = {ONE: f", playing {pick.name}", MIXED: ", playing a few different things"}.get(pick.kind, "")
    text = f"{main.people} people are in <#{main.channel_id}> right now{playing}."
    if others:
        text += " Also " + " and ".join(f"{r.people} in <#{r.channel_id}>" for r in others) + "."
        return text + " Click a channel to join."
    return text + " Click the channel to join."


def fmt_length(seconds: int) -> str:
    """"45 min", "2 h", "2 h 10 min". Never less than a minute."""
    hours, minutes = divmod(max(seconds // MINUTE, 1), 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def ended_text(seconds: int, peak: int) -> str:
    return f"That voice session ran {fmt_length(seconds)} with up to {max(peak, MIN_PEOPLE)} people."


# ---------------------------------------------------------------- state
@dataclass(frozen=True)
class State:
    pending_since: int | None = None  # a room has been open since then (no stretch running)
    start: int | None = None  # the session's first stretch began; None: no session
    live: bool = False  # a stretch is running
    stretch: int | None = None  # when the running stretch began
    below_since: int | None = None  # live, but no room has been open since then
    active: int = 0  # seconds in the session's finished stretches
    peak: int = 0  # most people at once this session
    ended_at: int | None = None  # the last stretch ended (the session can still be continued)
    message_id: int | None = None  # this session's line, or the previous session's
    own: bool = False  # the message has shown this session's text
    gone: bool = False  # this session's message was deleted: say nothing more this session
    text: str = ""  # what the message says
    want: str = ""  # what it should say
    tried_at: int = 0  # last write to the message, or failed attempt
    day: str = ""  # local day `posts` counts
    posts: int = 0  # new messages that day
    seen_at: int = 0  # last tick while a stretch or a hold was running


@dataclass(frozen=True)
class Post:
    text: str


@dataclass(frozen=True)
class Edit:
    message_id: int
    text: str


def dump(state: State) -> str:
    return json.dumps(asdict(state), separators=(",", ":"))


def load(raw: str | None) -> State:
    """The saved state; anything unreadable is a fresh one."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return State()
    if not isinstance(data, dict):
        return State()
    kept = {}
    for f in fields(State):
        value = data.get(f.name)
        kind = int if f.default is None else type(f.default)  # the optional fields are all times or ids
        if type(value) is kind:
            kept[f.name] = value
    state = State(**kept)
    if state.live and (state.start is None or state.stretch is None):
        return replace(State(), message_id=state.message_id, text=state.text, day=state.day, posts=state.posts)
    if state.start is not None and not state.live and state.ended_at is None:
        return replace(state, start=None)
    return state


def local_day(now: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(now, tz).date().isoformat()


def posts_today(state: State, now: int, tz: tzinfo) -> int:
    return state.posts if state.day == local_day(now, tz) else 0


def _end_stretch(s: State, at: int) -> State:
    at = max(at, s.stretch or at)
    active = s.active + at - (s.stretch or at)
    return replace(s, live=False, stretch=None, below_since=None, active=active, ended_at=at,
                   want=ended_text(active, s.peak))


def _action(s: State, now: int, tz: tzinfo, buried: bool, quiet_until: int) -> Post | Edit | None:
    if s.gone or not s.want or now - s.tried_at < EDIT_GAP:
        return None
    if s.own:  # this session's line: keep it current
        return Edit(s.message_id, s.want) if s.message_id is not None and s.want != s.text else None
    if not s.live or now < quiet_until:
        return None
    if s.message_id is not None and not buried:
        return Edit(s.message_id, s.want)  # still the newest message in general: reuse it, never stack
    return Post(s.want) if posts_today(s, now, tz) < MAX_PER_DAY else None


def step(state: State, rooms: Iterable[Room], now: int, tz: tzinfo, *, buried: bool = False,
         quiet_until: int = 0) -> tuple[State, Post | Edit | None]:
    """One tick: the new state and at most one thing to do in general.

    `rooms`: the public voice channels as they are now. `buried`: something was posted in general
    after the message in `state.message_id` (by anyone, another bot post included), so editing
    it would put the line where nobody looks. `quiet_until`: a new session's line waits for this
    time (the chat revival's last post plus REVIVAL_GAP). Report the result of the action with
    posted(), edited(), failed() or gone()."""
    rooms = open_rooms(rooms)
    people = sum(r.people for r in rooms)
    s, resume = state, False
    if s.seen_at and now - s.seen_at > DOWN_GAP:  # the bot was down: reconcile with what is true now
        if s.live:
            s = _end_stretch(s, s.below_since if s.below_since is not None else s.seen_at)
            resume = bool(rooms) and now - s.ended_at < REUSE  # still there: carry on without a hold
        s = replace(s, pending_since=None)
    if s.start is not None and not s.live and not resume:
        back_at = s.pending_since if s.pending_since is not None and rooms else now
        if back_at - s.ended_at >= REUSE:  # too long ago to continue: that session is over
            s = replace(s, start=None, ended_at=None, active=0, peak=0)
    if rooms:
        if s.live:
            s = replace(s, below_since=None, peak=max(s.peak, people))
        else:
            since = now if resume or s.pending_since is None else s.pending_since
            if resume or now - since >= HOLD:
                if s.start is None:
                    s = replace(s, start=since, active=0, peak=0, own=False, gone=False)
                s = replace(s, live=True, stretch=since, pending_since=None, ended_at=None,
                            below_since=None, peak=max(s.peak, people))
            else:
                s = replace(s, pending_since=since)
        if s.live:
            s = replace(s, want=live_text(rooms))
    else:
        s = replace(s, pending_since=None)
        if s.live:
            below = now if s.below_since is None else s.below_since
            s = _end_stretch(s, below) if now - below >= HOLD else replace(s, below_since=below)
    s = replace(s, seen_at=now if s.live or s.pending_since is not None else 0)
    return s, _action(s, now, tz, buried, quiet_until)


def stop(state: State, now: int) -> tuple[State, Edit | None]:
    """The module was switched off: close whatever was running and say what the line should
    become, so a "right now" line is not left standing."""
    s = replace(state, pending_since=None)
    if s.live:
        fallback = s.seen_at or now
        s = _end_stretch(s, s.below_since if s.below_since is not None else fallback)
    s = replace(s, seen_at=0)
    due = s.own and not s.gone and s.message_id is not None and s.want and s.want != s.text
    return s, (Edit(s.message_id, s.want) if due else None)


# ---------------------------------------------------------------- results of an action
def posted(state: State, message_id: int, text: str, now: int, tz: tzinfo) -> State:
    return replace(state, message_id=message_id, text=text, own=True, tried_at=now,
                   day=local_day(now, tz), posts=posts_today(state, now, tz) + 1)


def edited(state: State, text: str, now: int) -> State:
    return replace(state, text=text, own=True, tried_at=now)


def failed(state: State, now: int) -> State:
    """Discord refused for now: try again after EDIT_GAP, not every tick."""
    return replace(state, tried_at=now)


def gone(state: State, now: int) -> State:
    """The message no longer exists. This session's own line is not posted again; a previous
    session's line that was only being reused is forgotten, so a new one can be posted."""
    if state.own:
        return replace(state, message_id=None, text="", gone=True, tried_at=now)
    return replace(state, message_id=None, text="")


def fired(before: State, after: State) -> bool:
    """A session's line just appeared (a new message, or the previous line reused)."""
    return after.own and not before.own
