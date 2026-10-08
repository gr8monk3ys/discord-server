"""Weekly recap, owner digest, member milestones and the monthly invite contest.
Pure: no Discord, no database. All times are unix seconds.

- Weekly jobs reuse logic/schedule.py (`Weekly`, `plan`). The recap and digest cover the
  previous local Monday 00:00 to this Monday 00:00 (`week_bounds`), not the job's own
  Monday-10:00 window.
- `Monthly` / `plan_monthly`: the monthly twin of schedule.plan, same rules (run only the
  latest due period, the very first run only marks it done, the caller marks a run done after
  it worked). A period is keyed by the month it judges ("invitecontest:2026-09").
- The invite contest reuses logic/growth.py's `stayed` rule.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

import discord

from logic import growth as G
from logic import shop
from logic import stats as S
from logic.schedule import Weekly

RECAP_JOB = Weekly("recap", weekday=0, hour=10, minute=0)  # Mondays 10:00 local
DIGEST_JOB = Weekly("ownerdigest", weekday=0, hour=10, minute=5)  # Mondays 10:05 local
MILESTONES = (25, 50, 100, 250, 500, 1000)
CONTEST_PRIZES = (1500, 750, 300)
CONTEST_REASON = "invitecontest"
TOP_N = 3
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _signed(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


# ---------------------------------------------------------------- weeks
def _midnight(day: date, tz: tzinfo) -> int:
    return int(datetime.combine(day, time(0), tzinfo=tz).timestamp())


def week_bounds(scheduled_at: int, tz: tzinfo) -> tuple[int, int, date]:
    """[start, end) of the local Monday-Sunday week before `scheduled_at`, and its Monday."""
    today = datetime.fromtimestamp(scheduled_at, tz).date()
    this_monday = today - timedelta(days=today.weekday())
    first = this_monday - timedelta(days=7)
    return _midnight(first, tz), _midnight(this_monday, tz), first


def day_keys(first: date) -> list[str]:
    """The 7 local dates (ISO strings, as message_counts stores them) from `first`."""
    return [(first + timedelta(days=i)).isoformat() for i in range(7)]


def day_ends(first: date, tz: tzinfo) -> list[int]:
    """Local midnight at the end of each of the 7 days."""
    return [_midnight(first + timedelta(days=i + 1), tz) for i in range(7)]


def week_label(first: date) -> str:
    last = first + timedelta(days=6)
    return f"{first.strftime('%b')} {first.day} – {last.strftime('%b')} {last.day}"


# ---------------------------------------------------------------- monthly schedule
@dataclass(frozen=True)
class Monthly:
    name: str
    day: int  # day of the month (1..28)
    hour: int
    minute: int


@dataclass(frozen=True)
class MonthPeriod:
    key: str  # f"{name}:{month judged}"
    month: str  # "YYYY-MM": the month before the scheduled date
    scheduled_at: int
    window_start: int  # that month's local bounds
    window_end: int


@dataclass(frozen=True)
class MonthPlan:
    run: MonthPeriod | None
    mark_done: list[MonthPeriod]


CONTEST_JOB = Monthly("invitecontest", day=1, hour=12, minute=0)  # the 1st, 12:00 local


def _month_occurrence(job: Monthly, year: int, month: int, tz: tzinfo) -> MonthPeriod:
    at = int(datetime.combine(date(year, month, job.day), time(job.hour, job.minute), tzinfo=tz).timestamp())
    judged = shop.previous_key(f"{year:04}-{month:02}")
    start, end = shop.month_bounds(judged, tz)
    return MonthPeriod(f"{job.name}:{judged}", judged, at, start, end)


def _prev(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def latest_due_monthly(job: Monthly, now: int, tz: tzinfo) -> MonthPeriod:
    d = datetime.fromtimestamp(now, tz).date()
    p = _month_occurrence(job, d.year, d.month, tz)
    return p if p.scheduled_at <= now else _month_occurrence(job, *_prev(d.year, d.month), tz)


def plan_monthly(job: Monthly, now: int, tz: tzinfo, done_keys: set[str], first_seen: int | None) -> MonthPlan:
    """Same contract as schedule.plan, for a job that runs once a local month."""
    latest = latest_due_monthly(job, now, tz)
    if latest.key in done_keys:
        return MonthPlan(None, [])
    if first_seen is None or latest.scheduled_at < first_seen:
        return MonthPlan(None, [latest])
    missed: list[MonthPeriod] = []
    d = datetime.fromtimestamp(latest.scheduled_at, tz).date()
    y, m = _prev(d.year, d.month)
    while True:
        p = _month_occurrence(job, y, m, tz)
        if p.scheduled_at < first_seen:
            break
        if p.key not in done_keys:
            missed.append(p)
        y, m = _prev(y, m)
    return MonthPlan(latest, missed[::-1])


# ---------------------------------------------------------------- recap
def top(scores: dict[int, int | float], n: int = TOP_N, exclude: Collection[int] = ()) -> list[tuple[int, int | float]]:
    """Highest scores first, then lowest user id; zero scores and `exclude` are left out."""
    rows = [(u, s) for u, s in scores.items() if s > 0 and u not in exclude]
    return sorted(rows, key=lambda r: (-r[1], r[0]))[:n]


def squads_formed(posts: Iterable[tuple[int, list[int]]], start: int, end: int) -> int:
    """posts: (size, member joined_at times). A squad formed when its size-th member joined."""
    n = 0
    for size, joined in posts:
        times = sorted(joined)
        if size >= 1 and len(times) >= size and start <= times[size - 1] < end:
            n += 1
    return n


def level_ups(before: dict[int, int], after: dict[int, int], exclude: Collection[int] = (),
              n: int = TOP_N) -> list[tuple[int, int, int]]:
    """(user, old level, new level) for members who went up, most levels gained first."""
    ups = [(u, before.get(u, 0), lvl) for u, lvl in after.items()
           if lvl > before.get(u, 0) and u not in exclude]
    return sorted(ups, key=lambda r: (-(r[2] - r[1]), -r[2], r[0]))[:n]


def fmt_hours(seconds: int) -> str:
    hours = round(seconds / 3600, 1)
    return f"{hours:,.0f}" if hours == int(hours) or hours >= 100 else f"{hours:,.1f}"


@dataclass(frozen=True)
class Recap:
    joined: int = 0
    left: int = 0
    messages: int = 0
    voice_seconds: int = 0
    chatters: list = field(default_factory=list)  # [(user, messages)]
    voice: list = field(default_factory=list)  # [(user, seconds)]
    squads: int = 0
    gamenights: int = 0
    hall: int = 0
    clip_winner: int | None = None
    champions: list = field(default_factory=list)  # [(user, tournament name)]
    badges: int = 0
    levelups: list = field(default_factory=list)  # [(user, old, new)]

    @property
    def empty(self) -> bool:
        return not recap_sections(self)


def recap_sections(r: Recap) -> list[tuple[str, str]]:
    """(embed field name, value) for every section that has data, in display order."""
    out: list[tuple[str, str]] = []
    if r.joined or r.left:
        out.append(("Members", f"**{r.joined}** joined, **{r.left}** left (net **{_signed(r.joined - r.left)}**)"))
    if r.messages:
        text = f"**{r.messages:,}** messages"
        if r.chatters:
            text += "\nTop: " + " · ".join(f"<@{u}> {n:,}" for u, n in r.chatters)
        out.append(("Chat", text))
    if r.voice_seconds >= 60:
        text = f"**{fmt_hours(r.voice_seconds)}** hours in voice"
        if r.voice:
            text += "\nTop: " + " · ".join(f"<@{u}> {S.fmt_duration(s)}" for u, s in r.voice)
        out.append(("Voice", text))
    if r.squads:
        out.append(("Squads", f"**{r.squads}** {'squad' if r.squads == 1 else 'squads'} formed in LFG"))
    if r.gamenights:
        out.append(("Game nights", f"**{r.gamenights}** game {'night' if r.gamenights == 1 else 'nights'} held"))
    if r.hall:
        out.append(("Hall of fame",
                    f"**{r.hall}** {'post' if r.hall == 1 else 'posts'} made the hall of fame"))
    if r.clip_winner is not None:
        out.append(("Clip of the week", f"<@{r.clip_winner}>"))
    if r.champions:
        out.append(("Tournaments", "\n".join(f"<@{u}> won **{discord.utils.escape_markdown(name)}**"
                                             for u, name in r.champions)))
    if r.badges:
        out.append(("Badges", f"**{r.badges}** new {'badge' if r.badges == 1 else 'badges'} earned"))
    if r.levelups:
        out.append(("Level-ups", "\n".join(f"<@{u}> reached level **{new}**" for u, _, new in r.levelups)))
    return out


# ---------------------------------------------------------------- milestones
def milestones_due(count: int, done: Collection[int], first: bool) -> tuple[int | None, list[int]]:
    """(milestone to celebrate or None, milestones to record as done).
    The very first check only records what's already reached; later, several reached at once
    get one post, for the highest."""
    reached = [m for m in MILESTONES if m <= count and m not in done]
    if not reached or first:
        return None, reached
    return max(reached), reached


def milestone_text(n: int) -> str:
    return (f"🎉 We just hit **{n:,} members**! Thanks to everyone who hangs out, squads up and "
            f"brings friends along. Here's to the next milestone.")


# ---------------------------------------------------------------- invite contest
def contest_ranking(joins: Iterable[G.Join], start: int, end: int, now: int,
                    exclude: Collection[int] = (), n: int = TOP_N) -> list[tuple[int, int, int]]:
    """(rank, inviter, people) for the top `n` inviters of [start, end).

    A person counts for an inviter when their latest join came through that inviter (not
    themselves) inside the window, they are still in the server, and they stayed by
    growth.stayed. Ties go to whoever reached their total first, then the lower user id."""
    latest: dict[int, G.Join] = {}
    for x in joins:
        cur = latest.get(x.user_id)
        if cur is None or x.joined_at >= cur.joined_at:
            latest[x.user_id] = x
    people: dict[int, list[int]] = {}
    for x in latest.values():
        if (x.inviter_id is None or x.inviter_id == x.user_id or x.inviter_id in exclude
                or x.left_at is not None or not start <= x.joined_at < end or not G.stayed(x, now)):
            continue
        people.setdefault(x.inviter_id, []).append(x.joined_at)
    ordered = sorted(people.items(), key=lambda kv: (-len(kv[1]), max(kv[1]), kv[0]))[:n]
    return [(i + 1, inviter, len(times)) for i, (inviter, times) in enumerate(ordered)]


def contest_ref(month: str, rank: int) -> str:
    return f"{CONTEST_REASON}:{month}:{rank}"


def contest_text(month: str, winners: list[tuple[int, int, int]]) -> str:
    medals = ("🥇", "🥈", "🥉")
    lines = [f"📨 **Invite contest: {shop.month_name(month)}**",
             "The members who brought in the most people who stuck around:"]
    for rank, uid, count in winners:
        lines.append(f"{medals[rank - 1]} <@{uid}> with {plural(count, 'invite')} "
                     f"(+{CONTEST_PRIZES[rank - 1]:,} coins)")
    lines.append("Invite your friends this month for a shot at next month's prizes.")
    return "\n".join(lines)


# ---------------------------------------------------------------- owner digest
def member_trend(member_count: int, rows: Iterable[tuple[int, int | None]], ends: list[int]) -> list[int]:
    """Estimated member count at each time in `ends`, walking back from `member_count` now.
    rows: (joined_at, left_at) from the joins table."""
    rows = list(rows)
    out = []
    for t in ends:
        later_joins = sum(1 for joined, _ in rows if joined > t)
        later_leaves = sum(1 for _, left in rows if left is not None and left > t)
        out.append(max(member_count - later_joins + later_leaves, 0))
    return out


@dataclass(frozen=True)
class Digest:
    member_count: int = 0
    joined: int = 0
    left: int = 0
    trend: list = field(default_factory=list)  # 7 estimated end-of-day counts, Mon..Sun
    open_tickets: int = 0
    open_reports: int = 0
    open_suggestions: int | None = None  # None: couldn't tell
    cases: dict = field(default_factory=dict)  # kind -> count this week
    errors: int | None = None  # ERROR log records since start; None: ops not loaded
    pending_partners: int = 0


def _needs(d: Digest) -> list[str]:
    items = []
    if d.open_tickets:
        items.append(plural(d.open_tickets, "open ticket"))
    if d.open_reports:
        items.append(plural(d.open_reports, "open report"))
    if d.pending_partners:
        items.append(plural(d.pending_partners, "partner application"))
    if d.open_suggestions:
        items.append(plural(d.open_suggestions, "suggestion") + " without a status")
    if d.errors:
        items.append(plural(d.errors, "error"))
    return items


def needs_you(d: Digest) -> bool:
    return bool(_needs(d))


def digest_fields(d: Digest, first: date) -> list[tuple[str, str]]:
    members = (f"**{d.member_count:,}** now · **{d.joined}** joined, **{d.left}** left "
               f"(net **{_signed(d.joined - d.left)}**)")
    if d.trend:
        members += "\n" + " · ".join(f"{WEEKDAYS[(first.weekday() + i) % 7]} {n:,}" for i, n in enumerate(d.trend))
    out = [("Members", members)]
    total = sum(d.cases.values())
    if total:
        kinds = ", ".join(f"{n} {kind}" for kind, n in sorted(d.cases.items()))
        out.append(("Mod actions", f"**{total}** this week: {kinds}"))
    else:
        out.append(("Mod actions", "None this week."))
    queue = [f"Tickets open: **{d.open_tickets}**", f"Reports open: **{d.open_reports}**",
             f"Partner applications: **{d.pending_partners}**"]
    if d.open_suggestions is not None:
        queue.append(f"Suggestions without a status: **{d.open_suggestions}**")
    out.append(("Queue", "\n".join(queue)))
    if d.errors is not None:
        out.append(("Errors", f"**{d.errors}** since the bot started (see /status)." if d.errors
                    else "None since the bot started."))
    needs = _needs(d)
    out.append(("Needs you", " · ".join(needs) if needs else "Nothing needs you this week."))
    return out
