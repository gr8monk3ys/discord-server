"""Weekly job scheduling for a bot that is sometimes off. Pure: no Discord, no database.

A job like "mvp" runs once per local week at a wall-clock time (Sundays 18:00). Each run is a
Period keyed by the ISO week of its local date, with a data window of the 7 local days before
it. The caller stores finished keys (the `jobs` table) and asks `plan()` what to do.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

WEEK = timedelta(days=7)


@dataclass(frozen=True)
class Weekly:
    name: str  # e.g. "mvp", "clips"
    weekday: int  # Monday=0 .. Sunday=6
    hour: int
    minute: int


@dataclass(frozen=True)
class Period:
    key: str  # f"{name}:{iso_year}-W{iso_week:02}" of the local scheduled date
    scheduled_at: int  # unix seconds
    window_start: int  # same wall-clock time 7 local days earlier (167/169 h across DST)
    window_end: int  # == scheduled_at


@dataclass(frozen=True)
class Plan:
    run: Period | None  # the one period to run now
    mark_done: list[Period]  # record as done without running


def _at(job: Weekly, day: date, tz: tzinfo) -> int:
    # zoneinfo resolves wall-clock times (fold=0): an ambiguous time takes the first (DST)
    # instance, a nonexistent one lands an hour later. 18:00 is never either in practice.
    return int(datetime.combine(day, time(job.hour, job.minute), tzinfo=tz).timestamp())


def occurrence(job: Weekly, local_date: date, tz: tzinfo) -> Period:
    """The period scheduled on `local_date`, which must fall on `job.weekday`."""
    if local_date.weekday() != job.weekday:
        raise ValueError(f"{local_date} is not weekday {job.weekday}")
    iso = local_date.isocalendar()
    scheduled = _at(job, local_date, tz)
    return Period(
        key=f"{job.name}:{iso.year}-W{iso.week:02}",
        scheduled_at=scheduled,
        window_start=_at(job, local_date - WEEK, tz),
        window_end=scheduled,
    )


def latest_due(job: Weekly, now: int, tz: tzinfo) -> Period | None:
    """The most recent period with scheduled_at <= now. Always exists for a weekly job."""
    today = datetime.fromtimestamp(now, tz).date()
    day = today - timedelta(days=(today.weekday() - job.weekday) % 7)
    p = occurrence(job, day, tz)
    return p if p.scheduled_at <= now else occurrence(job, day - WEEK, tz)


def periods_between(job: Weekly, start: int, end: int, tz: tzinfo) -> list[Period]:
    """Periods with start < scheduled_at <= end, oldest first."""
    out: list[Period] = []
    p = latest_due(job, end, tz)
    while p is not None and p.scheduled_at > start:
        out.append(p)
        p = occurrence(job, datetime.fromtimestamp(p.scheduled_at, tz).date() - WEEK, tz)
    return out[::-1]


def plan(job: Weekly, now: int, tz: tzinfo, done_keys: set[str], first_seen: int | None) -> Plan:
    """What to do for `job` at `now`, given finished keys and when the bot first saw the job.

    Let L be latest_due(now).
    - L already in done_keys: nothing (run=None, mark_done=[]). Older gaps are left alone,
      since only L could ever be chosen to run again.
    - first_seen is None (very first startup) or L.scheduled_at < first_seen (L predates the
      bot): don't run; mark_done=[L]. Only L is returned: older periods can never be chosen.
    - Otherwise run L (its window anchored to L.scheduled_at, not to now), and mark_done
      every older period with first_seen <= scheduled_at < L.scheduled_at not in done_keys,
      oldest first. Periods before first_seen are never touched.
    """
    latest = latest_due(job, now, tz)
    if latest is None or latest.key in done_keys:
        return Plan(None, [])
    if first_seen is None or latest.scheduled_at < first_seen:
        return Plan(None, [latest])
    missed = periods_between(job, first_seen - 1, latest.scheduled_at - 1, tz)
    return Plan(latest, [p for p in missed if p.key not in done_keys])
