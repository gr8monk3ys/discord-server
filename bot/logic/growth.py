"""Growth math: invite attribution, who stayed, recruiters, Disboard bumps.
Pure: no Discord, no database. All times are unix seconds."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass

DAY = 24 * 60 * 60
STAY_SECONDS = 3 * DAY  # a recruit "stayed" if still in the server this long after joining
RECRUITER_THRESHOLD = 3  # stayed invites needed for the Recruiter role
REMINDER_DELAY = 2 * 60 * 60  # Disboard's bump cooldown

BUMP_CONFIRMED = "confirmed"  # the embed says "Bump done"
BUMP_UNVERIFIED = "unverified"  # /bump reply, but the embed isn't readable

# code -> (inviter_id or None, uses). The vanity URL is just another code with no inviter.
Snapshot = dict[str, tuple[int | None, int]]


def nearly_used_up(max_uses: int, uses: int) -> bool:
    """One use (or none) left: if this invite disappears, a join probably used it up."""
    return bool(max_uses) and uses >= max_uses - 1


def attribute_join(old: Snapshot, new: Snapshot, used_up: Collection[str] | None = None) -> str | None:
    """The invite code one new member used, or None if it can't be told.

    - Exactly one code went up, by exactly one (a code new since `old` counts from 0): that code.
    - Nothing went up and exactly one code disappeared: that code (a one-use invite is
      deleted the moment it's used). With `used_up`, only those codes count as
      disappearing: one revoked or expired unused isn't evidence of anything.
    - Anything else (no change, several codes up, one code up by 2+, several gone): None.
    """
    increments = {}
    for code, (_, uses) in new.items():
        before = old[code][1] if code in old else 0
        if uses > before:
            increments[code] = uses - before
    if increments:
        if len(increments) == 1:
            (code, by), = increments.items()
            return code if by == 1 else None
        return None
    gone = [code for code in old if code not in new and (used_up is None or code in used_up)]
    return gone[0] if len(gone) == 1 else None


def inviter_of(code: str | None, old: Snapshot, new: Snapshot) -> int | None:
    if code is None:
        return None
    if code in new:
        return new[code][0]
    if code in old:
        return old[code][0]
    return None


@dataclass(frozen=True)
class Join:
    user_id: int
    inviter_id: int | None
    joined_at: int
    left_at: int | None  # None = still here (as far as we know)


def stayed(join: Join, now: int) -> bool:
    """Still in the server STAY_SECONDS after joining (leaving later doesn't undo it)."""
    end = now if join.left_at is None else join.left_at
    return end - join.joined_at >= STAY_SECONDS


def _counted(joins: Iterable[Join], inviter: int | None = None, since: int | None = None) -> list[Join]:
    return [
        x for x in joins
        if x.inviter_id is not None and x.inviter_id != x.user_id
        and (inviter is None or x.inviter_id == inviter)
        and (since is None or x.joined_at >= since)
    ]


@dataclass(frozen=True)
class Summary:
    total: int  # distinct people invited
    still_here: int  # whose latest join through this inviter hasn't left
    stayed: int  # who stayed at least STAY_SECONDS on some join


def summary(joins: Iterable[Join], inviter: int, now: int) -> Summary:
    by_user: dict[int, list[Join]] = {}
    for x in _counted(joins, inviter):
        by_user.setdefault(x.user_id, []).append(x)
    here = sum(1 for rows in by_user.values() if max(rows, key=lambda r: r.joined_at).left_at is None)
    kept = sum(1 for rows in by_user.values() if any(stayed(r, now) for r in rows))
    return Summary(len(by_user), here, kept)


def stayed_counts(joins: Iterable[Join], now: int, since: int | None = None) -> dict[int, int]:
    """inviter -> distinct people who stayed, counting joins at or after `since`."""
    people: dict[int, set[int]] = {}
    for x in _counted(joins, since=since):
        if stayed(x, now):
            people.setdefault(x.inviter_id, set()).add(x.user_id)
    return {inviter: len(users) for inviter, users in people.items()}


def recruiters(counts: dict[int, int], threshold: int = RECRUITER_THRESHOLD) -> set[int]:
    return {user for user, n in counts.items() if n >= threshold}


def bump_status(author_id: int, command_name: str | None, embed_texts: Iterable[str | None],
                disboard_id: int = 302050872383242240) -> str | None:
    """Is this message Disboard confirming a /bump?

    BUMP_CONFIRMED: readable embed says "Bump done". BUMP_UNVERIFIED: a /bump reply with no
    readable embed (no Message Content intent). None: not a bump, including Disboard's
    cooldown reply. An unknown command name only counts with a readable "Bump done".
    """
    if author_id != disboard_id:
        return None
    if command_name is not None and command_name.lower() != "bump":
        return None
    texts = [t for t in embed_texts if t]
    if texts:
        return BUMP_CONFIRMED if any("bump done" in t.lower() for t in texts) else None
    return BUMP_UNVERIFIED if command_name is not None else None


def reminder_due_at(bumped_at: int) -> int:
    return bumped_at + REMINDER_DELAY


def reminder_ready(due: int | None, now: int) -> bool:
    return due is not None and now >= due
