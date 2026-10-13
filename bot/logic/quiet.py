"""Quiet rooms: the bot stops asking questions nobody answers, and starts again when someone
is there. Pure: no Discord I/O, no database, no clock (the caller passes `now`).

A "kind" is one recurring prompt that asks members for a response: the question of the day,
the daily poll, the chat revival. Informational posts (free games, recaps, results, welcomes,
the voice line) are not kinds and never come here. One rule, the same for every kind:

- A prompt is posted unless the last N (3) prompts of its kind were ALL ignored.
- Ignored means no human responded. The driver (bot/quietgate.py) reads the signals; whatever
  it could not establish counts as answered, so an API hiccup never silences the bot.
- A sleeping kind wakes by itself: a member message in the channel after the last prompt lets
  the next scheduled one through, and after PROBE_AFTER (7 days) one probe goes out anyway.
  A probe nobody answers leaves the kind asleep for another 7 days.
- Staff are told once when a kind goes to sleep and once when it wakes (not for probes).
- QUIET=off (server/.env): every prompt is posted, as before this rule existed.

State: one JSON value per kind in `meta` (`quiet:<kind>`): the last N prompts as
[time, message id, answered] and whether the kind is asleep. Nothing about members.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace

HOUR = 60 * 60
DAY = 24 * HOUR

N = 3  # this many prompts in a row with no response: the kind goes to sleep
PROBE_AFTER = 7 * DAY  # asleep this long since the last prompt: one more goes out anyway
# The same clock time seven local days on is an hour short of 7 * DAY across a clock change.
SLACK = 2 * HOUR


# ---------------------------------------------------------------- kinds
@dataclass(frozen=True)
class Kind:
    key: str  # the meta key is quiet:<key>; live data, never rename
    label: str  # as staff read it in the mod log and /status


QOTD = Kind("qotd", "Question of the day")
POLL = Kind("thisorthat", "This or that poll")
REVIVAL = Kind("revival", "Conversation starter")
KINDS = (QOTD, POLL, REVIVAL)


def key(kind: Kind) -> str:
    return f"quiet:{kind.key}"


def enabled(environ: Mapping[str, str]) -> bool:
    """QUIET=off restores always-post. Anything else (unset included) is on."""
    return str(environ.get("QUIET") or "on").strip().lower() not in {"off", "0", "false", "no"}


# ---------------------------------------------------------------- state
@dataclass(frozen=True)
class Post:
    at: int  # unix seconds; for a scheduled job its scheduled time, so probes land on the slot
    message_id: int | None  # None: the send did not hand a message back
    answered: bool = False


@dataclass(frozen=True)
class State:
    posts: tuple[Post, ...] = ()  # oldest first, at most N
    asleep: bool = False


def dump(state: State) -> str:
    return json.dumps({"posts": [[p.at, p.message_id, int(p.answered)] for p in state.posts],
                       "asleep": int(state.asleep)}, separators=(",", ":"))


def load(raw: str | None) -> State:
    """The saved state; anything unreadable is an empty one (the kind starts over, awake)."""
    try:
        data = json.loads(raw) if raw else {}
        posts = tuple(Post(int(at), None if mid is None else int(mid), bool(answered))
                      for at, mid, answered in data.get("posts", []))
        return State(posts[-N:], bool(data.get("asleep")))
    except (ValueError, TypeError, AttributeError):
        return State()


def posted(state: State, at: int, message_id: int | None) -> State:
    """The state after a prompt went out. Nobody has answered it yet."""
    return replace(state, posts=(*state.posts, Post(at, message_id))[-N:])


# ---------------------------------------------------------------- the rule
@dataclass(frozen=True)
class Evidence:
    """What is known about the newest prompt. None means "could not tell"."""
    answered: bool | None = None  # a human responded to it (vote, reaction, thread message, reply)
    member_since: bool | None = None  # a member wrote in the channel after it


def heard(state: State, last_member_at: int | None) -> Evidence:
    """Evidence for a kind whose response is simply "someone talked": the cog knows when a
    member last wrote in the channel (None: it does not know)."""
    if not state.posts or last_member_at is None:
        return Evidence()
    spoke = last_member_at > state.posts[-1].at
    return Evidence(spoke, spoke)


ACTIVE, WOKEN, PROBE, ASLEEP = "active", "woken", "probe", "asleep"  # Decision.reason
PAUSED, RESUMED = "paused", "resumed"  # Decision.change


@dataclass(frozen=True)
class Decision:
    post: bool
    reason: str  # ACTIVE | WOKEN | PROBE (post) or ASLEEP (skip)
    state: State  # what to save: the newest prompt's answer folded in, asleep updated
    change: str | None = None  # PAUSED | RESUMED when staff should be told, else None


def probe_at(state: State) -> int | None:
    """When a sleeping kind posts its next probe if nobody shows up first."""
    return state.posts[-1].at + PROBE_AFTER if state.posts else None


def decide(state: State, now: int, evidence: Evidence = Evidence()) -> Decision:
    """Post the next prompt of this kind now?

    `evidence` is about the newest prompt in `state`. An answer, once seen, is kept; "could not
    tell" is recorded as answered. Fewer than N prompts on record: always post."""
    posts = state.posts
    if posts and not posts[-1].answered and evidence.answered is not False:
        posts = (*posts[:-1], replace(posts[-1], answered=True))
    if len(posts) < N or any(p.answered for p in posts[-N:]):
        reason = ACTIVE
    elif evidence.member_since is not False:
        reason = WOKEN
    elif now - posts[-1].at >= PROBE_AFTER - SLACK:
        reason = PROBE
    else:
        reason = ASLEEP
    if reason == ASLEEP:
        return Decision(False, reason, State(posts, True), None if state.asleep else PAUSED)
    if reason == PROBE:  # one try; the kind stays as it was until someone answers
        return Decision(True, reason, State(posts, state.asleep))
    return Decision(True, reason, State(posts, False), RESUMED if state.asleep else None)


# ---------------------------------------------------------------- what staff read
def _days(seconds: int) -> str:
    n = max(1, -(-seconds // DAY))  # rounded up, at least one
    return f"{n} day" if n == 1 else f"{n} days"


def paused_line(kind: Kind, state: State, now: int) -> str:
    left = (probe_at(state) or now) - now
    return (f"{kind.label} is paused: the last {N} went unanswered. It resumes when someone posts "
            f"in the channel, and tries again in {_days(left)}.")


def resumed_line(kind: Kind, reason: str) -> str:
    why = "someone posted in the channel" if reason == WOKEN else "the last one got a response"
    return f"{kind.label} is back: {why}."


def status_line(kind: Kind, state: State) -> str:
    """One line for /status about a sleeping kind."""
    at = probe_at(state)
    return f"{kind.label}: paused, tries again <t:{at}:R>" if at is not None else f"{kind.label}: paused"
