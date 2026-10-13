"""Tests for jobs.py, the scheduled-job driver, against a real in-memory SQLite database
(migrated like the cog tests' databases). No Discord, no network; the clock is a value the
test passes in.

The last section replays whole timelines through the driver and through a copy of the block
the cogs used to carry inline, and compares every `meta` and `jobs` row."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import db as dbmod
import jobs
from logic import engagement as E
from logic import ops as O
from logic import recap as R
from logic import tournaments as T
from logic import wrapped as W
from logic.schedule import Weekly, plan

TZ = ZoneInfo("America/Los_Angeles")
HOUR = 3600
DAY = 24 * HOUR
MVP = Weekly("mvp", weekday=6, hour=18, minute=0)  # Sundays 18:00 local


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


T0 = ts(2026, 9, 30, 12)  # Wednesday: the latest due run (Sun 9/27, W39) predates the bot
SUN = ts(2026, 10, 4, 18)  # the first run the bot is up for (W40)


def with_db(fn):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        try:
            await fn(db)
        finally:
            await db.close()
    asyncio.run(go())


async def job_rows(db):
    return {r["key"]: r["done_at"] for r in await db.fetchall("SELECT key, done_at FROM jobs")}


async def meta_rows(db):
    return {r["key"]: r["value"] for r in await db.fetchall("SELECT key, value FROM meta")}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Action:
    """Returns the scripted results in turn (an exception is raised), recording each call."""

    def __init__(self, *results, clock=None, takes=0):
        self.results, self.calls, self.clock, self.takes = list(results), [], clock, takes

    async def __call__(self, period):
        self.calls.append(period.key)
        if self.clock is not None:
            self.clock.t += self.takes
        result = self.results.pop(0) if self.results else True
        if isinstance(result, Exception):
            raise result
        return result


# ---------------------------------------------------------------- due
def test_seen_key():
    assert jobs.seen_key("mvp") == "first_seen:mvp"


def test_first_run_marks_the_latest_period_and_runs_nothing():
    async def go(db):
        assert await jobs.due(db, MVP, TZ, T0) is None
        assert await meta_rows(db) == {"first_seen:mvp": str(T0)}
        assert await job_rows(db) == {"mvp:2026-W39": T0}
    with_db(go)


def test_first_seen_is_written_once_and_never_replaced():
    async def go(db):
        await jobs.due(db, MVP, TZ, T0)
        await jobs.due(db, MVP, TZ, T0 + HOUR)
        await jobs.due(db, MVP, TZ, SUN)
        assert await meta_rows(db) == {"first_seen:mvp": str(T0)}
    with_db(go)


def test_due_at_the_scheduled_minute_and_not_before():
    async def go(db):
        await jobs.due(db, MVP, TZ, T0)
        assert await jobs.due(db, MVP, TZ, SUN - 1) is None
        period = await jobs.due(db, MVP, TZ, SUN)
        assert (period.key, period.scheduled_at) == ("mvp:2026-W40", SUN)
        assert period.window_end == SUN and period.window_start == SUN - 7 * DAY
    with_db(go)


def test_due_does_not_mark_the_period_it_returns():
    async def go(db):
        await jobs.due(db, MVP, TZ, T0)
        first = await jobs.due(db, MVP, TZ, SUN)
        again = await jobs.due(db, MVP, TZ, SUN + 300)  # the work failed: the next tick asks again
        assert first == again and "mvp:2026-W40" not in await job_rows(db)
    with_db(go)


def test_done_marks_it_and_it_is_never_due_twice():
    async def go(db):
        await jobs.due(db, MVP, TZ, T0)
        period = await jobs.due(db, MVP, TZ, SUN)
        await jobs.done(db, period, SUN + 7)
        assert (await job_rows(db))["mvp:2026-W40"] == SUN + 7
        for later in (SUN + 8, SUN + 300, SUN + 6 * DAY):
            assert await jobs.due(db, MVP, TZ, later) is None
        await jobs.done(db, period, SUN + 999)  # marking twice keeps the first stamp
        assert (await job_rows(db))["mvp:2026-W40"] == SUN + 7
    with_db(go)


def test_catch_up_after_downtime_runs_only_the_latest():
    async def go(db):
        await jobs.due(db, MVP, TZ, T0)
        back = ts(2026, 10, 21, 12)  # off for three Sundays: 10/4, 10/11, 10/18
        period = await jobs.due(db, MVP, TZ, back)
        assert period.key == "mvp:2026-W42"
        # The two it missed are recorded as skipped, stamped with this tick; the one to run is not.
        assert await job_rows(db) == {"mvp:2026-W39": T0, "mvp:2026-W40": back, "mvp:2026-W41": back}
        await jobs.done(db, period, back + 5)
        assert await jobs.due(db, MVP, TZ, back + 300) is None
    with_db(go)


def test_periods_from_before_the_bot_are_never_run_or_backfilled():
    async def go(db):
        await db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("first_seen:mvp", str(SUN + HOUR)))
        assert await jobs.due(db, MVP, TZ, SUN + 2 * HOUR) is None  # W40 was due an hour before first sight
        assert await job_rows(db) == {"mvp:2026-W40": SUN + 2 * HOUR}
    with_db(go)


def test_an_existing_first_seen_row_is_used_as_is():
    async def go(db):
        await db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("first_seen:mvp", str(T0 - 30 * DAY)))
        period = await jobs.due(db, MVP, TZ, T0)
        assert period.key == "mvp:2026-W39"
        assert (await meta_rows(db))["first_seen:mvp"] == str(T0 - 30 * DAY)
        # Skipped since first sight (Mon 8/31): the Sundays 9/6 (W36), 9/13 and 9/20 (W38).
        assert await job_rows(db) == {f"mvp:2026-W{w}": T0 for w in (36, 37, 38)}
    with_db(go)


@pytest.mark.parametrize("bad", ["", None, "soon"])
def test_a_first_seen_row_that_is_not_a_number_is_an_error_not_a_first_run(bad):
    """Nothing writes such a row (only due() writes the key, as str(now)); if one is put there
    by hand the job stops loudly, with nothing marked, rather than skipping periods quietly."""
    async def go(db):
        await db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("first_seen:mvp", bad))
        with pytest.raises((ValueError, TypeError)):
            await jobs.due(db, MVP, TZ, SUN)
        assert await job_rows(db) == {} and await meta_rows(db) == {"first_seen:mvp": bad}
    with_db(go)


# ---------------------------------------------------------------- similar names
@pytest.mark.parametrize("a,b", [("clipcast", "clipcast-ask"), ("wrapped", "wrappedreminder"),
                                 ("support", "supporterstipend"), ("recap", "recap2")])
def test_jobs_with_similar_names_do_not_see_each_others_rows(a, b):
    async def go(db):
        one, two = Weekly(a, 6, 18, 0), Weekly(b, 6, 18, 0)
        for job in (one, two):
            assert await jobs.due(db, job, TZ, T0) is None
        assert set(await meta_rows(db)) == {f"first_seen:{a}", f"first_seen:{b}"}
        first = await jobs.due(db, one, TZ, SUN)
        await jobs.done(db, first, SUN)
        # `one` is finished for the week; `two` is still due, with its own key.
        assert await jobs.due(db, one, TZ, SUN + 60) is None
        second = await jobs.due(db, two, TZ, SUN + 60)
        assert (first.key, second.key) == (f"{a}:2026-W40", f"{b}:2026-W40")
        await jobs.done(db, second, SUN + 60)
        assert await jobs.due(db, two, TZ, SUN + 120) is None
        assert await job_rows(db) == {f"{a}:2026-W39": T0, f"{b}:2026-W39": T0,
                                      f"{a}:2026-W40": SUN, f"{b}:2026-W40": SUN + 60}
    with_db(go)


def test_rows_of_other_drivers_in_the_jobs_table_are_ignored():
    async def go(db):
        # Seasonal drops and the raffle keep their own keys in `jobs`.
        for key in ("seasonal:seen:spooky-2026", "raffle:2026-W40", "mvpx:2026-W40", "xmvp:2026-W40"):
            await db.execute("INSERT INTO jobs (key, done_at) VALUES (?, ?)", (key, 1))
        await jobs.due(db, MVP, TZ, T0)
        assert (await jobs.due(db, MVP, TZ, SUN)).key == "mvp:2026-W40"
    with_db(go)


# ---------------------------------------------------------------- the other planners
def test_period_keys_are_the_ones_stored_in_the_live_database():
    """The key strings are data: a changed format would re-run or skip real jobs."""
    async def go(db):
        cases = [
            (MVP, plan, "mvp:2026-W39", ts(2026, 10, 4, 18), "mvp:2026-W40"),
            (E.Daily("qotd", 12, 0), E.plan_daily, "qotd:2026-09-30", ts(2026, 10, 1, 12), "qotd:2026-10-01"),
            (O.Daily("dbbackup", 4, 0), O.plan_daily, "dbbackup:2026-09-30", ts(2026, 10, 1, 4), "dbbackup:2026-10-01"),
            (R.Monthly("invitecontest", day=1, hour=12, minute=0), R.plan_monthly, "invitecontest:2026-08",
             ts(2026, 10, 1, 12), "invitecontest:2026-09"),
            (T.AUTO_JOB, T.plan_monthly, "autotourney:2026-09", ts(2026, 10, 5, 12), "autotourney:2026-10"),
            (W.ANNOUNCE_JOB, W.plan_annual, "wrapped:2025", ts(2026, 12, 1, 10), "wrapped:2026"),
        ]
        for job, planner, skipped, at, key in cases:
            assert await jobs.due(db, job, TZ, T0, planner) is None
            period = await jobs.due(db, job, TZ, at, planner)
            assert period is not None and period.key == key, job.name
            await jobs.done(db, period, at)
            assert await jobs.due(db, job, TZ, at + 60, planner) is None
            rows = await job_rows(db)
            assert rows[skipped] == T0 and rows[key] == at, job.name
        assert await meta_rows(db) == {f"first_seen:{job.name}": str(T0) for job, *_ in cases}
    with_db(go)


def test_daily_catch_up_marks_the_missed_days():
    async def go(db):
        job = E.Daily("qotd", 12, 0)
        await jobs.due(db, job, TZ, T0, E.plan_daily)  # Wed 9/30 12:00 exactly: first sight, marked not run
        back = ts(2026, 10, 3, 13)
        period = await jobs.due(db, job, TZ, back, E.plan_daily)
        assert period.key == "qotd:2026-10-03"
        assert await job_rows(db) == {"qotd:2026-09-30": T0, "qotd:2026-10-01": back, "qotd:2026-10-02": back}
    with_db(go)


def test_an_annual_job_that_is_too_late_is_skipped_not_run():
    async def go(db):
        job = W.REMINDER_JOB  # Dec 20, dropped when 14 or more days late
        await jobs.due(db, job, TZ, T0, W.plan_annual)
        late = ts(2027, 1, 5, 9)
        assert await jobs.due(db, job, TZ, late, W.plan_annual) is None
        assert (await job_rows(db))["wrappedreminder:2026"] == late
    with_db(go)


# ---------------------------------------------------------------- run_due
def test_run_due_runs_the_action_once_and_marks_it():
    async def go(db):
        clock, action = Clock(T0), Action()
        assert await jobs.run_due(db, MVP, TZ, clock, action) is False  # first sight
        clock.t = SUN
        assert await jobs.run_due(db, MVP, TZ, clock, action) is True
        for clock.t in (SUN + 60, SUN + 300, SUN + DAY):
            assert await jobs.run_due(db, MVP, TZ, clock, action) is False
        assert action.calls == ["mvp:2026-W40"]
        assert await job_rows(db) == {"mvp:2026-W39": T0, "mvp:2026-W40": SUN}
    with_db(go)


def test_action_that_fails_is_retried_on_the_next_tick_then_never_again():
    async def go(db):
        clock = Clock(T0)
        action = Action(False, None, 0, True)  # three falsy results, then it works
        await jobs.run_due(db, MVP, TZ, clock, action)
        for i in range(3):
            clock.t = SUN + 300 * i
            assert await jobs.run_due(db, MVP, TZ, clock, action) is False
            assert "mvp:2026-W40" not in await job_rows(db)
        clock.t = SUN + 900
        assert await jobs.run_due(db, MVP, TZ, clock, action) is True
        clock.t = SUN + 1200
        assert await jobs.run_due(db, MVP, TZ, clock, action) is False
        assert action.calls == ["mvp:2026-W40"] * 4
        assert (await job_rows(db))["mvp:2026-W40"] == SUN + 900
    with_db(go)


def test_action_that_raises_leaves_the_period_unmarked_and_the_error_reaches_the_caller():
    async def go(db):
        clock = Clock(T0)
        action = Action(RuntimeError("discord is down"), True)
        await jobs.run_due(db, MVP, TZ, clock, action)
        clock.t = SUN
        with pytest.raises(RuntimeError):
            await jobs.run_due(db, MVP, TZ, clock, action)
        assert await job_rows(db) == {"mvp:2026-W39": T0}
        clock.t = SUN + 300
        assert await jobs.run_due(db, MVP, TZ, clock, action) is True
        assert (await job_rows(db))["mvp:2026-W40"] == SUN + 300
    with_db(go)


def test_run_due_stamps_done_when_the_work_finished():
    async def go(db):
        clock = Clock(T0)
        await jobs.run_due(db, MVP, TZ, clock, Action())
        back = ts(2026, 10, 14, 9)  # missed 10/4 (W40); 10/11 (W41) is the one to run
        clock.t = back
        assert await jobs.run_due(db, MVP, TZ, clock, Action(clock=clock, takes=42)) is True
        rows = await job_rows(db)
        assert rows["mvp:2026-W40"] == back  # skipped: stamped when planned
        assert rows["mvp:2026-W41"] == back + 42  # ran: stamped after the action returned
    with_db(go)


def test_run_due_passes_the_planner_through():
    async def go(db):
        clock, action = Clock(T0), Action()
        job = R.Monthly("invitecontest", day=1, hour=12, minute=0)
        await jobs.run_due(db, job, TZ, clock, action, R.plan_monthly)
        clock.t = ts(2026, 10, 1, 12)
        assert await jobs.run_due(db, job, TZ, clock, action, R.plan_monthly) is True
        assert action.calls == ["invitecontest:2026-09"]
    with_db(go)


# ---------------------------------------------------------------- same rows as the inline block
async def legacy_tick(db, job, tz, now, planner, action):
    """The block the cogs carried inline before jobs.py (as in cogs/clips.py and the
    `due`/`finish` pairs), kept here word for word as the reference."""
    seen_key = f"first_seen:{job.name}"
    row = await db.fetchone("SELECT value FROM meta WHERE key = ?", (seen_key,))
    first_seen = int(row["value"]) if row else None
    done = {r["key"] for r in await db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{job.name}:%",))}
    t = now()
    todo = planner(job, t, tz, done, first_seen)
    async with db.transaction() as tx:
        if first_seen is None:
            await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
        for period in todo.mark_done:
            await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
    if todo.run is not None and await action(todo.run):
        await db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (todo.run.key, now()))


TIMELINE_JOBS = [
    (MVP, plan),
    (Weekly("clipcast", 1, 18, 30), plan),
    (Weekly("clipcast-ask", 6, 18, 30), plan),
    (E.Daily("qotd", 12, 0), E.plan_daily),
    (O.Daily("dbbackup", 4, 0), O.plan_daily),
    (R.Monthly("invitecontest", day=1, hour=12, minute=0), R.plan_monthly),
    (T.AUTO_JOB, T.plan_monthly),
    (W.ANNOUNCE_JOB, W.plan_annual),
    (W.REMINDER_JOB, W.plan_annual),
]


def timeline():
    """Tick times from late September to mid January: every 11 hours, with a 3-day and a
    23-day outage, so there are first sights, normal runs, catch-ups and a late annual job."""
    start, end = ts(2026, 9, 28, 7), ts(2027, 1, 12)
    outages = [(ts(2026, 10, 9), ts(2026, 10, 12, 6)), (ts(2026, 11, 20), ts(2026, 12, 13))]
    return [t for t in range(start, end, 11 * HOUR) if not any(a <= t < b for a, b in outages)]


def test_driver_writes_the_same_rows_as_the_inline_block():
    """Every third action call returns False (so there are retries), and each call takes 7 s
    (so a finished period's stamp differs from the tick's)."""
    async def replay(db, tick):
        clock = Clock(0)
        actions = {job.name: Action(*[i % 3 != 0 for i in range(1, 400)], clock=clock, takes=7)
                   for job, _ in TIMELINE_JOBS}
        for t in timeline():
            for job, planner in TIMELINE_JOBS:
                clock.t = t
                await tick(db, job, clock, planner, actions[job.name])
        return await meta_rows(db), await job_rows(db), {n: a.calls for n, a in actions.items()}

    async def old(db, job, clock, planner, action):
        await legacy_tick(db, job, TZ, clock, planner, action)

    async def new(db, job, clock, planner, action):
        await jobs.run_due(db, job, TZ, clock, action, planner)

    async def new_in_two_steps(db, job, clock, planner, action):
        period = await jobs.due(db, job, TZ, clock(), planner)
        if period is not None and await action(period):
            await jobs.done(db, period, clock())

    results = []
    for tick in (old, new, new_in_two_steps):
        async def go(db, tick=tick):
            results.append(await replay(db, tick))
        with_db(go)
    reference, via_run_due, via_due_and_done = results
    assert via_run_due == reference and via_due_and_done == reference
    meta, rows, calls = reference
    assert len(meta) == len(TIMELINE_JOBS) and len(rows) > 150 and all(calls.values())
