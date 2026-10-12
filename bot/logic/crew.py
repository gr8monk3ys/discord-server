"""Welcome Crew: trusted regulars who say hi to newcomers. Pure: no Discord, no database.

- The bar for joining the crew: an established Discord account, 14 days on the server and no
  moderation case against you in the last 30 days.
- The crew ping: one line in general when someone new arrives, at most once per gap (joins in
  between are batched into the next one), never in quiet hours, never during a join raid and
  only while a crew member is online. Arrivals nobody could be pinged for go stale and are dropped.
- The greeting reward, built to be hard to farm: the *newcomer* has to answer the crew member
  (a reply to a message the crew member sent after they arrived, or a mention of a crew member
  who addressed them first), within 48 hours of arriving, on a fresh join (not a rejoin), both
  accounts established, one greeting per newcomer ever (first crew member wins) and a daily
  coin cap per crew member. The ledger ref is keyed on the newcomer's id.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from enum import Enum

import config
from logic import quests as Q

MIN = 60
HOUR = 60 * MIN
DAY = 24 * HOUR

# ---------------------------------------------------------------- joining the crew
MIN_SERVER_DAYS = 14
CLEAN_DAYS = 30  # no case against you in this many days
# Case kinds that are an action against the member (cases.kind; untimeout and purge are not).
CASE_KINDS = ("warn", "timeout", "auto_timeout", "spam")


def join_problem(*, user_id: int, joined_at: int | None, now: int, recent_cases: int) -> str | None:
    """Why this member can't join the crew yet, in words for them, or None."""
    if not Q.established(user_id, now):
        return f"Your Discord account has to be at least {Q.MIN_ACCOUNT_DAYS} days old to join the crew."
    if joined_at is None or now - joined_at < MIN_SERVER_DAYS * DAY:
        left = MIN_SERVER_DAYS if joined_at is None else -(-(MIN_SERVER_DAYS * DAY - (now - joined_at)) // DAY)
        return (f"The crew is for members who've been here {MIN_SERVER_DAYS} days. "
                f"Come back in {left} day{'' if left == 1 else 's'}.")
    if recent_cases > 0:
        return f"The crew needs a clean record for the last {CLEAN_DAYS} days. Try again later."
    return None


# ---------------------------------------------------------------- settings (from the environment)
DEFAULT_GAP_MIN = 30
GAP_RANGE = (5, 24 * 60)  # minutes
DEFAULT_QUIET = (22, 9)  # local hours: from 22:00 up to 09:00


def ping_gap(raw: str | None) -> int:
    """Seconds between crew pings from CREW_PING_MINUTES; anything unusable means the default."""
    try:
        minutes = int((raw or "").strip())
    except ValueError:
        return DEFAULT_GAP_MIN * MIN
    return minutes * MIN if GAP_RANGE[0] <= minutes <= GAP_RANGE[1] else DEFAULT_GAP_MIN * MIN


def quiet_hours(raw: str | None) -> tuple[int, int] | None:
    """(from hour, until hour) from CREW_QUIET_HOURS like "22-9"; "off" means none; anything
    unusable means the default."""
    text = (raw or "").strip().lower()
    if text in ("off", "none"):
        return None
    try:
        start, end = (int(p) for p in text.split("-"))
    except ValueError:
        return DEFAULT_QUIET
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return DEFAULT_QUIET
    return start, end


def is_quiet(now: int, tz: tzinfo, quiet: tuple[int, int] | None) -> bool:
    if quiet is None:
        return False
    hour, (start, end) = datetime.fromtimestamp(now, tz).hour, quiet
    return start <= hour < end if start < end else hour >= start or hour < end


# ---------------------------------------------------------------- the ping
HOLD = 12 * HOUR  # an arrival nobody was pinged for by then is dropped (a late hello reads oddly)
NAMES_SHOWN = 10


class Ping(Enum):
    SEND = "send"
    WAIT = "wait"  # keep the arrivals for a later tick
    DROP = "drop"  # forget them: this looks like a raid


def is_stale(arrived_at: int, now: int) -> bool:
    return now - arrived_at >= HOLD


def ping_decision(*, now: int, tz: tzinfo, last_ping_at: int | None, gap: int,
                  quiet: tuple[int, int] | None, crew_online: bool | None, raid: bool) -> Ping:
    """What to do with waiting arrivals. `crew_online` None: presence is unknown (no Presence
    intent), so that check is skipped."""
    if raid:
        return Ping.DROP
    if is_quiet(now, tz, quiet):
        return Ping.WAIT
    if last_ping_at is not None and now - last_ping_at < gap:
        return Ping.WAIT
    if crew_online is False:
        return Ping.WAIT
    return Ping.SEND


def _listing(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def ping_text(role_mention: str, newcomers: Sequence[str]) -> str:
    """The one line in general. Newcomer mentions render but don't ping (role ping only)."""
    shown = list(newcomers[:NAMES_SHOWN])
    if len(newcomers) > NAMES_SHOWN:
        shown.append(f"{len(newcomers) - NAMES_SHOWN} more")
    verb = "just joined" if len(newcomers) == 1 else "are new here"
    return f"👋 {role_mention} {_listing(shown)} {verb}. Come say hi!"


# ---------------------------------------------------------------- fresh joins
REJOIN_DAYS = 30


def is_fresh(now: int, earlier: Iterable[int | None]) -> bool:
    """A first arrival, not a rejoin: `earlier` holds the times of anything we know about an
    earlier stay (a previous arrival, a previous leave). Any of them in the last REJOIN_DAYS
    makes this a rejoin, which is still pinged for but never rewarded."""
    return not any(t is not None and now - t < REJOIN_DAYS * DAY for t in earlier)


# ---------------------------------------------------------------- the greeting reward
WINDOW = 48 * HOUR  # the newcomer has to answer within this long of arriving
COINS = 40
DAILY_REWARDS = 3  # paid greetings per crew member per local day; more still count, unpaid
REASON = "crew"  # ledger reason
BADGE_KEY = "welcoming"  # logic/achievements.py, granted by the cog
BADGE_AT = 5  # greetings


def ref(newcomer_id: int) -> str:
    """One payout per newcomer, ever, whoever greets them."""
    return f"crew:greet:{newcomer_id}"


def window_open(arrived_at: int, now: int) -> bool:
    return 0 <= now - arrived_at < WINDOW


def counts(*, newcomer_id: int, crew_id: int, arrived_at: int, fresh: bool, now: int) -> bool:
    """Whether this exchange is a greeting at all. Fresh alts on either side never count."""
    return (newcomer_id != crew_id and fresh and window_open(arrived_at, now)
            and Q.established(newcomer_id, now) and Q.established(crew_id, now))


def coins_for(earned_today: int) -> int:
    """The payout given what this crew member already earned from greetings today."""
    return COINS if earned_today + COINS <= DAILY_REWARDS * COINS else 0


def day_bounds(now: int, tz: tzinfo) -> tuple[int, int]:
    """[start, end) of the local day (DST-correct: midnight to midnight)."""
    today = datetime.fromtimestamp(now, tz).date()
    start = datetime.combine(today, time(0), tz)
    end = datetime.combine(today + timedelta(days=1), time(0), tz)
    return int(start.timestamp()), int(end.timestamp())


class Hellos:
    """Which crew member addressed which newcomer, and when. In memory only: after a restart
    a mention alone no longer counts until the crew member speaks to them again (a reply to
    the crew member's message still does)."""

    def __init__(self):
        self.said: dict[tuple[int, int], int] = {}

    def note(self, crew_id: int, newcomer_id: int, at: int) -> None:
        self.said[(crew_id, newcomer_id)] = at

    def since(self, crew_id: int, newcomer_id: int, arrived_at: int) -> bool:
        at = self.said.get((crew_id, newcomer_id))
        return at is not None and at >= arrived_at

    def prune(self, now: int) -> None:
        self.said = {k: at for k, at in self.said.items() if now - at < WINDOW}


def pick_greeter(*, newcomer_id: int, arrived_at: int, crew_ids: Iterable[int], reply_to: int | None,
                 reply_sent_at: int | None, mentioned: Iterable[int], hellos: Hellos) -> int | None:
    """The crew member this newcomer message answers, or None. A reply counts when the message
    replied to was sent by a crew member after the newcomer arrived; a mention counts when
    that crew member addressed the newcomer first. The reply wins over mentions."""
    crew = set(crew_ids) - {newcomer_id}
    if reply_to in crew and reply_sent_at is not None and reply_sent_at >= arrived_at:
        return reply_to
    for uid in mentioned:
        if uid in crew and hellos.since(uid, newcomer_id, arrived_at):
            return uid
    return None


# ---------------------------------------------------------------- /crew and /crew-stats
ROSTER_SHOWN = 20
NOT_SET_UP = f"The {config.CREW_ROLE} isn't set up yet (the role doesn't exist)."


def how_to_join() -> str:
    return (f"**Want in?** Press **Join the crew**. You need a Discord account at least "
            f"{Q.MIN_ACCOUNT_DAYS} days old, {MIN_SERVER_DAYS} days on the server and a clean record for "
            f"{CLEAN_DAYS} days. You'll get a ping when someone new arrives; say hi, and when they answer "
            f"you it counts as a greeting (**{COINS} coins**, up to {DAILY_REWARDS} paid a day).")


def roster_text(members: Sequence[tuple[str, int | None]]) -> str:
    """`members`: (mention, greetings this month or None when hidden by /privacy), already in
    display order."""
    if not members:
        return "Nobody's on the crew yet. Be the first."
    lines = []
    for mention, n in members[:ROSTER_SHOWN]:
        lines.append(mention if not n else f"{mention} · {n} greeting{'' if n == 1 else 's'} this month")
    if len(members) > ROSTER_SHOWN:
        lines.append(f"… and {len(members) - ROSTER_SHOWN} more")
    return "\n".join(lines)


def roster_order(counts_by_id: dict[int, int | None], ids: Iterable[int]) -> list[int]:
    """Most greetings this month first, then lowest id."""
    return sorted(ids, key=lambda u: (-(counts_by_id.get(u) or 0), u))


@dataclass(frozen=True)
class Stats:
    arrivals: int  # newcomers seen in the window
    pinged: int  # of those, how many a crew ping covered
    pings: int  # crew ping messages sent
    greeted: int  # newcomers a crew member had a real exchange with
    coins: int  # paid for those greetings
    top: tuple[tuple[int, int], ...] = ()  # (crew id, greetings), best first


def greeted_share(stats: Stats) -> str:
    return f"{100 * stats.greeted / stats.arrivals:.0f}%" if stats.arrivals else "n/a"


def stats_text(stats: Stats) -> str:
    lines = [f"Newcomers: **{stats.arrivals}** ({stats.pinged} covered by {stats.pings} crew "
             f"ping{'' if stats.pings == 1 else 's'})",
             f"Greeted by the crew: **{stats.greeted}** ({greeted_share(stats)})",
             f"Coins paid: {stats.coins:,}"]
    if stats.top:
        lines.append("Top: " + ", ".join(f"<@{uid}> ({n})" for uid, n in stats.top))
    return "\n".join(lines)


def digest_text(stats: Stats, crew_size: int) -> str | None:
    """One line for the owner digest; None when there's nothing to say."""
    if crew_size == 0 and stats.arrivals == 0:
        return None
    return (f"{stats.arrivals} newcomer{'' if stats.arrivals == 1 else 's'}, {stats.greeted} greeted by the "
            f"crew ({greeted_share(stats)}) · crew of {crew_size}")
