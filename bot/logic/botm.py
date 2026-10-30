"""Base of the Month for Monkey SMP: the rules. Pure: no Discord I/O, no database, no clock.

The SMP repo's docs/SEASONS.md, "Base of the Month": once a month the owners pick one base (a
faction's or a solo player's), post it in #monkey-smp, and its builders get the Base of the Month
role for the month. The same faction can't win two months running.

/botm award (staff) names up to MAX_WINNERS builders. The role moves: last month's holders lose it,
the new ones get it. Running it again in the same month corrects that month's award (the check
against "two months running" then looks at the month before).

State: one JSON value in `meta` (STATE_KEY): the month of the last award, its winners, and the
winners of the award before it. Only member IDs, nothing else about them.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

MAX_WINNERS = 4  # a faction is up to four players
LINE_MAX = 300
STATE_KEY = "botm:state"  # meta; live data, never rename


@dataclass(frozen=True)
class State:
    month: str = ""  # "2027-01"
    winners: tuple[int, ...] = ()
    before: tuple[int, ...] = field(default=())  # the winners of the award before `month`


def month_key(local: datetime) -> str:
    return f"{local.year:04d}-{local.month:02d}"


def month_label(local: datetime) -> str:
    return local.strftime("%B %Y")


def load(text) -> State:
    try:
        data = json.loads(text or "{}")
        return State(month=str(data.get("month") or ""),
                     winners=tuple(int(i) for i in data.get("winners") or ()),
                     before=tuple(int(i) for i in data.get("before") or ()))
    except (ValueError, TypeError, AttributeError):
        return State()


def dump(state: State) -> str:
    return json.dumps({"month": state.month, "winners": list(state.winners), "before": list(state.before)})


def last_winners(state: State, month: str) -> tuple[int, ...]:
    """Who won the month before `month` (for the two-months-running rule)."""
    return state.before if state.month == month else state.winners


def repeat_winners(state: State, month: str, winners) -> list[int]:
    previous = set(last_winners(state, month))
    return [w for w in winners if w in previous]


def award(state: State, month: str, winners) -> State:
    return State(month=month, winners=tuple(dict.fromkeys(winners)), before=last_winners(state, month))


def announcement(mentions: list[str], label: str, line: str = "") -> str:
    """`mentions` are the winners' mentions; `line` is the builders' own words, already escaped."""
    who = mentions[0] if len(mentions) == 1 else ", ".join(mentions[:-1]) + " and " + mentions[-1]
    text = f"🏰 **Base of the Month, {label}:** {who}! The Base of the Month role is yours for the month."
    if line:
        text += f"\n> {line}"
    return text
