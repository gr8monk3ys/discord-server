"""The driver for scheduled jobs: the one place that reads and writes a job's rows.

A job is a `Weekly` / `Daily` / `Monthly` / `Annual` value with a `name`; its planner
(`logic.schedule.plan` or one of its daily, monthly or annual counterparts) is pure and decides which period
is due. This module does the database half, the same way for every job:

- `meta` row `first_seen:<name>`: when the bot first saw the job (unix seconds, as text).
  Written once, never replaced.
- `jobs` rows `<name>:<period>`: periods that are finished, or were skipped on purpose.

`due()` asks the planner and records what it says to skip. The caller does the work and then
calls `done()`, so a period is only marked after its work succeeded: a failure retries on the
next tick and a success never repeats. `run_due()` is the two together for the usual case of
an action that returns True when the period is handled.

Plain module: no Discord imports, and no clock of its own (the caller passes `now`).

Job names: finished periods are read with `key LIKE '<name>:%'`, so the colon keeps `clipcast`
from seeing `clipcast-ask` rows. LIKE also treats `_` and `%` in a name as wildcards and
ignores ASCII case, so a new name must not differ from an existing one only by those.
"""

from collections.abc import Awaitable, Callable

from logic import schedule


def seen_key(name: str) -> str:
    """The `meta` key that holds when the bot first saw the job called `name`."""
    return f"first_seen:{name}"


async def due(db, job, tz, now: int, planner=schedule.plan):
    """Record first_seen and skipped periods; return the period to run now, if any.

    `planner(job, now, tz, done_keys, first_seen)` returns an object with `run` (a period or
    None) and `mark_done` (periods to record without running). The first_seen row and the
    skipped periods are written in one transaction, stamped `now`. The returned period is
    NOT marked: call `done()` once its work succeeded."""
    key = seen_key(job.name)
    row = await db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
    first_seen = int(row["value"]) if row else None
    done_keys = {r["key"] for r in await db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{job.name}:%",))}
    todo = planner(job, now, tz, done_keys, first_seen)
    async with db.transaction() as tx:
        if first_seen is None:
            await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, str(now)))
        for period in todo.mark_done:
            await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now))
    return todo.run


async def done(db, period, now: int) -> None:
    """Mark `period` finished, so no later tick runs it again."""
    await db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now))


async def run_due(db, job, tz, clock: Callable[[], int], action: Callable[..., Awaitable],
                  planner=schedule.plan) -> bool:
    """Run `action(period)` if a period is due and mark it done when the action returns truthy.

    A falsy return or an exception leaves the period unmarked, so the next tick tries again
    (an exception is not caught here). `clock` is called once to plan and once more after the
    action, so `done_at` is when the work finished. True when a period was run and marked."""
    period = await due(db, job, tz, clock(), planner)
    if period is None or not await action(period):
        return False
    await done(db, period, clock())
    return True
