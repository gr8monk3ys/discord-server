"""Monkey SMP "we miss you" nudges: the rules. Pure: no Discord I/O, no database, no clock of its
own (the cog passes `now`).

A member who ran `/smp nudges on` and has not been on Monkey SMP for AWAY gets one DM from Front
Desk: what happened since they last played, and how to hop back in.

- Off for the whole server unless SMP_NUDGES=on is in server/.env (`enabled`). While it is off
  `/smp nudges` saves nothing, no last-seen time is recorded and nothing is sent.
- Last seen: the bridge's player-list poll (cogs/monkey.py, every POLL_EVERY while the server
  answers: RCON `list`, or the status ping's sample). Only members who opted in are recorded, only
  while they are linked, and never for a member who turned tracking off (`/privacy`). A member who
  opts in with playtime already on record starts from the end of their last day played
  (`opt_in_state`).
- One nudge per lapse: `nudged_for` is the last-seen time the nudge was sent for. Playing again moves
  last-seen past it, which is the only way another can become due (`due`).
- DMs closed: the first refusal is recorded (`dm_closed_at`) and nothing is tried again until the
  member runs `/smp nudges on` again.
- Sent at most MAX_PER_PASS per CHECK_EVERY, never in QUIET hours, and only while the server is
  answering (a nudge to a server that is down helps nobody).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR

ENV_ON = "SMP_NUDGES"
ON_WORDS = {"on", "1", "true", "yes"}
AWAY = 7 * DAY
CHECK_EVERY = HOUR
MAX_PER_PASS = 5
QUIET = (21, 10)  # local hours: no DMs from 9 PM up to 10 AM
SHOWN = 3  # faction names / events listed in one DM
EVENT_WINDOW = 7 * DAY  # upcoming events this far ahead are mentioned
EVENT_WORDS = ("smp", "minecraft", "monkey")  # a scheduled event about Monkey SMP names one of these


def enabled(environ) -> bool:
    """SMP_NUDGES=on (or 1/true/yes) turns nudges on. Anything else, or nothing, is off."""
    return str(environ.get(ENV_ON) or "").strip().lower() in ON_WORDS


# ---------------------------------------------------------------- who is due
@dataclass(frozen=True)
class Candidate:
    user_id: int
    last_seen_at: int | None  # None: never seen on the server since opting in
    nudged_for: int | None  # the last_seen_at the last nudge was sent for
    dm_closed_at: int | None  # set when Discord refused a DM
    linked: bool  # still has a Minecraft name linked
    tracking: bool  # has not turned tracking off (/privacy)


def due(c: Candidate, now: int) -> bool:
    if not c.linked or not c.tracking or c.dm_closed_at is not None or c.last_seen_at is None:
        return False
    if now - c.last_seen_at < AWAY:
        return False
    return c.nudged_for is None or c.nudged_for < c.last_seen_at


def pick(candidates, now: int, limit: int = MAX_PER_PASS) -> list[Candidate]:
    """Who gets a nudge this pass: due members, the longest away first (ties by ID), at most `limit`."""
    ready = sorted((c for c in candidates if due(c, now)), key=lambda c: (c.last_seen_at, c.user_id))
    return ready[:max(limit, 0)]


def day_end(day: str, tz: tzinfo) -> int:
    """The end of a local day ("2026-10-05") as a timestamp: the latest they could have been on."""
    start = datetime.fromisoformat(day).replace(tzinfo=tz)
    return int((start + timedelta(days=1)).timestamp())


def opt_in_state(last_played_day: str | None, now: int, tz: tzinfo) -> tuple[int | None, int | None]:
    """(last_seen_at, nudged_for) for a member opting in. Seeded from their playtime record when they
    have one. A member already away for AWAY is plainly thinking about the server right now (they
    just ran an /smp command), so that lapse counts as nudged: the next nudge waits until they have
    played and been away again."""
    if last_played_day is None:
        return None, None
    seen = min(day_end(last_played_day, tz), now)
    return seen, (seen if now - seen >= AWAY else None)


# ---------------------------------------------------------------- what to tell them
def smp_events(events, now: int, window: int = EVENT_WINDOW) -> list[tuple[str, int]]:
    """Upcoming scheduled events about Monkey SMP: (name, start) within `window`, soonest first."""
    found = [(name, start) for name, start in events
             if now <= start <= now + window and any(w in (name or "").lower() for w in EVENT_WORDS)]
    return sorted(found, key=lambda e: (e[1], e[0]))[:SHOWN]


def month_label(month: str) -> str | None:
    """"2026-11" -> "November 2026"; None for anything else."""
    try:
        return datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Digest:
    """What happened while they were away. Text fields arrive already escaped for Discord."""
    away_days: int
    players: int = 0  # other members who played since
    hours: int = 0  # hours those members played, together
    factions: tuple[str, ...] = ()  # approved since, the first SHOWN
    faction_count: int = 0
    botm_month: str | None = None  # "November 2026" when a Base of the Month was picked since
    events: tuple[tuple[str, int], ...] = ()  # upcoming (name, start)
    online_now: int | None = None
    address: str = ""
    channel: str = "#monkey-smp"


def message(d: Digest) -> str:
    """The DM. Short: a hello, up to four facts, how to get back in, how to stop these."""
    days = f"{d.away_days} day" + ("" if d.away_days == 1 else "s")
    lines = [f"Hey, Front Desk here. It's been {days} since you were on Monkey SMP. Since then:"]
    facts = []
    if d.players:
        who = "1 other player" if d.players == 1 else f"{d.players} other players"
        facts.append(f"- {who} put in {d.hours} hour{'' if d.hours == 1 else 's'}"
                     if d.hours else f"- {who} dropped in")
    if d.faction_count:
        names = ", ".join(d.factions)
        more = d.faction_count - len(d.factions)
        facts.append(f"- New faction{'' if d.faction_count == 1 else 's'}: {names}"
                     + (f" and {more} more" if more > 0 else ""))
    if d.botm_month:
        facts.append(f"- Base of the Month for {d.botm_month} was picked (it's in {d.channel})")
    for name, start in d.events:
        facts.append(f"- Coming up: {name}, <t:{start}:R>")
    if facts:
        lines += facts
    else:
        lines[0] = f"Hey, Front Desk here. It's been {days} since you were on Monkey SMP."
        lines.append("It's been a quiet stretch, so it's a good time to get ahead.")
    lines.append("In game, `/trigger standings`, `/trigger diplomacy` and `/trigger firsts` show where "
                 "things stand.")
    now_on = ""
    if d.online_now:
        now_on = f" {d.online_now} {'is' if d.online_now == 1 else 'are'} on right now."
    join = f"Hop back in at `{d.address}`." if d.address else "Hop back in any time."
    lines.append(f"{join}{now_on} Chat and the status panel are in {d.channel}.")
    lines.append("This is the only nudge until you've played again. `/smp nudges off` stops them.")
    return "\n".join(lines)
