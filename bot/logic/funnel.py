"""Are newcomers sticking? The "Newcomers" field of the Monday owner digest. Staff-facing only.
Pure: no Discord, no database. All times are unix seconds.

Once a week the owner digest (cogs/recap.py) hands this module the rows the other modules
already keep and gets one field back:

- This week (the digest's Monday-Sunday) and the last 28 days: how many joined, how many of
  those have left since, and of the rest of the path: picked roles, said something, joined
  voice.
- One retention line: of the members who joined in the week before, how many are still here
  and how many came back in week 2.
- One plain sentence about this week, picked by the rules in `reading`.

No joins in 28 days: no field at all.

Windows are whole local days (`windows`), the same Monday 00:00 edges as the rest of the
digest, so "joined this week" here is the "joined" of the Members field and a week with a
clock change is still one week.

Where each number comes from, and why that table:

- Joined, left: `joins` (one row per arrival, `left_at` set on the way out). A member is
  counted once per window however often they came and went, their activity is measured from
  their first arrival inside the window, and "left" means not in the server when the window
  ends. Bots are never in that table; the caller still passes their ids and they never count.
- Said something: `message_counts`, which counts messages per member per local day in every
  public channel. The starter quest's `say_hi` step is not used: it is one row per member for
  life and only for the general channel, so it misses a rejoin and every other channel. The
  price is day precision: a day counts from the local day of the join onward.
- Joined voice: `voice_sessions` that started after the join (the AFK and staff channels are
  never recorded there). The quest's `join_voice` step is copied from this table, with the
  time it was noticed instead of the time it happened, so it adds nothing.
- Picked roles: the only record is the quest step `pick_roles`, stamped when the bot noticed
  the role. A row counts when it is dated after the join. Rows are never restamped, so for
  somebody who left and came back the row is from the earlier stay and says nothing about
  this one: for them, and for anyone the hourly quest sweep has not reached, the caller also
  passes who holds a platform, region or game role right now. Somebody who came back, picked
  roles and left again before the digest is therefore not counted.
- Came back in week 2: the same message and voice records, in the calendar week after the
  week they joined (for a Sunday joiner that week starts the next day). Discord's Server
  Insights calls its version first-week retention and counts visits; the bot cannot see a
  visit, only a message or a voice session, so this number is the lower of the two.

Privacy: the field is counts only, never a member or an id. Members who turned tracking off
(`/privacy`) are counted as joined and left, which the server shows everyone anyway, and are
left out of every step after that, numerator and denominator both: the steps are "of the N
with stats on". The bot holds no message or voice rows for them, and their role step, which
it does hold, is ignored here too, so no number moves with anything they did. What the field
does give away is how many newcomers have stats off; with one newcomer that is the newcomer.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from logic import stats as S

FIELD = "Newcomers"
WEEK_DAYS = 7
MONTH_DAYS = 28


@dataclass(frozen=True)
class Join:
    """A row of `joins`."""

    user_id: int
    joined_at: int
    left_at: int | None = None


@dataclass(frozen=True)
class Windows:
    """The local-midnight [start, end) windows one digest looks at."""

    week: tuple[int, int]  # the digest's Monday to Monday
    month: tuple[int, int]  # the 28 local days that end with the week
    cohort: tuple[int, int]  # the week before: whoever joined then has had a second week
    cohort_first: date  # that week's Monday, for the label
    tz: tzinfo


@dataclass(frozen=True)
class Funnel:
    """One window. The steps count trackable members only."""

    joined: int = 0
    left: int = 0  # of those who joined: not in the server when the window ended
    trackable: int = 0  # joined and did not turn tracking off
    roles: int = 0
    text: int = 0
    voice: int = 0
    active: int = 0  # said something or joined voice


@dataclass(frozen=True)
class Retention:
    """Whoever joined in the cohort week, looked at over the week after."""

    joined: int = 0
    here: int = 0  # still in the server when the week after ended
    trackable: int = 0
    back: int = 0  # trackable, and sent a message or was in voice in the week after


@dataclass(frozen=True)
class Review:
    week: Funnel
    month: Funnel
    cohort: Retention
    cohort_first: date


def _midnight(day: date, tz: tzinfo) -> int:
    return int(datetime.combine(day, time(0), tzinfo=tz).timestamp())


def day_key(ts: int, tz: tzinfo) -> str:
    """The local date of a time, as message_counts spells it."""
    return datetime.fromtimestamp(ts, tz).date().isoformat()


def windows(first: date, tz: tzinfo) -> Windows:
    """The windows for the digest whose week starts on the local Monday `first`."""
    end = first + timedelta(days=WEEK_DAYS)
    before = first - timedelta(days=WEEK_DAYS)
    return Windows(
        week=(_midnight(first, tz), _midnight(end, tz)),
        month=(_midnight(end - timedelta(days=MONTH_DAYS), tz), _midnight(end, tz)),
        cohort=(_midnight(before, tz), _midnight(first, tz)),
        cohort_first=before, tz=tz,
    )


def _arrivals(joins: Iterable[Join], start: int, end: int) -> dict[int, int]:
    """user -> their first arrival inside [start, end). One entry per member, rejoins included."""
    out: dict[int, int] = {}
    for j in joins:
        if not start <= j.joined_at < end:
            continue
        if j.user_id not in out or j.joined_at < out[j.user_id]:
            out[j.user_id] = j.joined_at
    return out


def _here(joins: Iterable[Join], at: int) -> set[int]:
    """Who was in the server at `at`: an arrival before it that had not ended by then."""
    return {j.user_id for j in joins if j.joined_at < at and (j.left_at is None or j.left_at >= at)}


def review(joins: Iterable[Join], w: Windows, *, text_days: Iterable[tuple[int, str]] = (),
           voice: Iterable[S.Session] = (), role_steps: Iterable[tuple[int, int]] = (),
           roles_now: Collection[int] = (), optouts: Collection[int] = (), bots: Collection[int] = (),
           excluded_channels: Collection[int] = ()) -> Review:
    """Count the windows.

    text_days: (user, local day) with at least one message, as in message_counts.
    voice: voice sessions (key = channel id); `excluded_channels` never count (the AFK channel).
    role_steps: (user, when the pick_roles quest step was recorded).
    roles_now: members who hold a platform, region or game role at this moment.
    optouts: members with tracking off: joined and left only. bots: never counted at all."""
    joins = [j for j in joins if j.user_id not in bots]
    hidden = set(optouts) | set(bots)
    days: dict[int, set[str]] = {}
    for user, day in text_days:
        if user not in hidden:
            days.setdefault(user, set()).add(day)
    sessions: dict[int, list[S.Session]] = {}
    for s in voice:
        if s.user_id not in hidden and s.key not in excluded_channels:
            sessions.setdefault(s.user_id, []).append(s)
    stamped: dict[int, int] = {}  # user -> their latest pick_roles time (there is one row at most)
    for user, at in role_steps:
        if user not in hidden:
            stamped[user] = max(at, stamped.get(user, at))
    roles_now = set(roles_now) - hidden

    def funnel(start: int, end: int) -> Funnel:
        arrived = _arrivals(joins, start, end)
        here = _here(joins, end)
        last_day = day_key(end, w.tz)  # the first local day outside the window
        roles = text = talked = active = 0
        trackable = [u for u in arrived if u not in hidden]
        for u in trackable:
            since = arrived[u]
            if u in roles_now or stamped.get(u, since - 1) >= since:
                roles += 1
            wrote = any(day_key(since, w.tz) <= d < last_day for d in days.get(u, ()))
            spoke = any(since <= s.start < end for s in sessions.get(u, ()))
            text += wrote
            talked += spoke
            active += wrote or spoke
        return Funnel(joined=len(arrived), left=sum(1 for u in arrived if u not in here),
                      trackable=len(trackable), roles=roles, text=text, voice=talked, active=active)

    def retention() -> Retention:
        arrived = _arrivals(joins, *w.cohort)
        start, end = w.week
        here = _here(joins, end)
        first_day, last_day = day_key(start, w.tz), day_key(end, w.tz)
        trackable = [u for u in arrived if u not in hidden]
        back = 0
        for u in trackable:
            wrote = any(first_day <= d < last_day for d in days.get(u, ()))
            spoke = any(s.start < end and (s.end is None or s.end > start) for s in sessions.get(u, ()))
            back += wrote or spoke
        return Retention(joined=len(arrived), here=sum(1 for u in arrived if u in here),
                         trackable=len(trackable), back=back)

    return Review(week=funnel(*w.week), month=funnel(*w.month), cohort=retention(), cohort_first=w.cohort_first)


# ---------------------------------------------------------------- words
ACTED = "said something or joined voice"


def reading(r: Review) -> str | None:
    """One sentence about this week's newcomers, or None when the numbers say nothing.

    1. Nobody joined: say so.
    2. Everybody who joined has left: that, whatever they did first.
    3. Tracking is off for all of them: nothing can be said about what they did.
    4. None of the trackable ones sent a message or joined voice: say so.
    5. Some did: how many of how many.

    Rules 4 and 5 only ever speak about the members with stats on, and say so when that is
    not all of them."""
    f = r.week
    n, t, a = f.joined, f.trackable, f.active
    if n == 0:
        return "No joins this week: the listings are not bringing anyone in."
    if f.left == n:
        return ("The 1 member who joined this week has already left." if n == 1
                else f"All {n} who joined this week have already left.")
    if t == 0:
        return None
    if t < n:
        did = "none said anything or joined voice" if a == 0 else f"{a} {ACTED}"
        return f"{n} joined this week; of the {t} with stats on, {did}."
    if a == 0:
        return ("1 joined this week and has not said anything or joined voice." if n == 1
                else f"{n} joined this week and none of them said anything or joined voice.")
    if a < n:
        return f"{a} of the {n} who joined this week {ACTED}."
    return f"1 joined this week and {ACTED}." if n == 1 else f"All {n} who joined this week {ACTED}."


def _steps(joined: int, trackable: int, parts: list[str]) -> str:
    if trackable == 0:
        return "no activity numbers (stats are off)"
    text = " · ".join(parts)
    return text if trackable == joined else f"of the {trackable} with stats on: {text}"


def _funnel_line(label: str, f: Funnel) -> str:
    steps = _steps(f.joined, f.trackable, [f"picked roles {f.roles}", f"said something {f.text}",
                                           f"joined voice {f.voice}"])
    return f"{label}: **{f.joined}** joined ({f.left} left since) · {steps}"


def _span(first: date) -> str:
    last = first + timedelta(days=WEEK_DAYS - 1)
    return f"{first.strftime('%b')} {first.day} to {last.strftime('%b')} {last.day}"


def _retention_line(c: Retention, first: date) -> str:
    back = _steps(c.joined, c.trackable, [f"came back in week 2 (a message or voice): {c.back}"])
    return f"Joined {_span(first)}: **{c.joined}** · still here {c.here} · {back}"


def digest_text(r: Review) -> str | None:
    """The owner digest's "Newcomers" field, or None when nobody joined in 28 days."""
    if r.month.joined == 0:
        return None
    lines = []
    sentence = reading(r)
    if sentence is not None:
        lines.append(sentence)
    if r.week.joined:
        lines.append(_funnel_line("This week", r.week))
    lines.append(_funnel_line(f"Last {MONTH_DAYS} days", r.month))
    if r.cohort.joined:
        lines.append(_retention_line(r.cohort, r.cohort_first))
    return "\n".join(lines)
