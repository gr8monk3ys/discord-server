"""Community autopilot touches. Pure: no Discord, no database.

- Chat revival: when 💬・general has been quiet for 3 hours during the day (10:00 to 23:00
  local), one conversation starter, at most once per 6 hours and never twice in a row
  without a member message in between.
- Join anniversaries: who celebrates a whole number of years on the server today (Feb 29
  joiners on Feb 28 in other years), at most ANNIV_MAX a day.
- Boosters: when a boost starts, the refs for the thank-you payout and the monthly stipend.
- Member of the month: voice hours + messages/50 + squads joined*2 over last month (the cog
  counts only joins of other members' posts, and fresh accounts aren't squad hosts or voice company).

Coin rewards skip accounts younger than quests.MIN_ACCOUNT_DAYS (fresh alts can't farm them).
"""

from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, tzinfo

import config
from logic import engagement as E
from logic import shop
from logic.engagement import Daily
from logic.quests import MIN_ACCOUNT_DAYS
from logic.recap import Monthly
from names import slug

HOUR = 60 * 60
DAY = 24 * HOUR

# ---------------------------------------------------------------- jobs
ANNIV_JOB = Daily("anniversaries", 11, 0)
STIPEND_JOB = Monthly("booststipend", day=1, hour=12, minute=0)
MOTM_JOB = Monthly("motm", day=1, hour=12, minute=30)

# ---------------------------------------------------------------- chat revival
QUIET_SECONDS = 3 * HOUR
REVIVAL_GAP = 6 * HOUR
ACTIVE_FROM = 10  # local hour, inclusive
ACTIVE_UNTIL = 23  # local hour, exclusive


def in_active_hours(now: int, tz: tzinfo) -> bool:
    return ACTIVE_FROM <= datetime.fromtimestamp(now, tz).hour < ACTIVE_UNTIL


def should_revive(now: int, tz: tzinfo, last_member_at: int | None, last_revival_at: int | None) -> bool:
    """Post a starter now? Needs a known last member message (None: never guess)."""
    if last_member_at is None or not in_active_hours(now, tz):
        return False
    if now - last_member_at < QUIET_SECONDS:
        return False
    if last_revival_at is not None:
        if now - last_revival_at < REVIVAL_GAP:
            return False
        if last_member_at <= last_revival_at:
            return False  # nobody answered the last one: don't talk to an empty room twice
    return True


def revival_text(question: str) -> str:
    return f"💬 Quiet in here. Here's something to get us talking:\n\n**{question}**"


# ---------------------------------------------------------------- anniversaries
ANNIV_MAX = 10


def years_on(joined: date, day: date) -> int:
    """Whole years on the server if `day` is the join anniversary, else 0."""
    years = day.year - joined.year
    if years < 1:
        return 0
    return years if E.celebrated_on(joined.month, joined.day, day.year) == day else 0


def anniversaries(members: Iterable[tuple[int, date]], day: date, done: Mapping[int, int]) -> list[tuple[int, int]]:
    """(user_id, years) celebrated on `day`, longest-serving first, then lowest id, capped at
    ANNIV_MAX. `done` maps user_id -> last year celebrated (once per member per year)."""
    out = []
    for uid, joined in members:
        years = years_on(joined, day)
        if years and done.get(uid) != day.year:
            out.append((uid, years))
    out.sort(key=lambda p: (-p[1], p[0]))
    return out[:ANNIV_MAX]


def _years(n: int) -> str:
    return f"{n} year" if n == 1 else f"{n} years"


def anniversary_text(people: list[tuple[str, int]]) -> str:
    parts = [f"{mention} ({_years(years)})" for mention, years in people]
    who = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"🎉 Happy server anniversary to {who}! Thanks for hanging out with us."


# ---------------------------------------------------------------- boosters
BOOST_COINS = 1000
STIPEND_COINS = 500
BOOST_REASON = "boost"
STIPEND_REASON = "booststipend"


def boost_started(before_since, after_since) -> bool:
    return before_since is None and after_since is not None


def boost_ref(user_id: int, month: str) -> str:
    return f"boost:{user_id}:{month}"


def stipend_ref(month: str, user_id: int) -> str:
    return f"booststipend:{month}:{user_id}"


def boost_text(mention: str, coins: int) -> str:
    extra = f" **{coins:,} coins** are in your wallet as a thank-you." if coins else ""
    return f"💜 {mention} just boosted the server. Thank you!{extra}"


def old_enough(created_at: int | None, now: int) -> bool:
    return created_at is not None and now - created_at >= MIN_ACCOUNT_DAYS * DAY


STAFF_SLUGS = frozenset(slug(n) for n in (config.KEEPER_ROLE, config.MOD_ROLE))


def is_staff(role_names: Iterable[str]) -> bool:
    return any(slug(n) in STAFF_SLUGS for n in role_names)


# ---------------------------------------------------------------- member of the month
MOTM_COINS = 1000
MOTM_REASON = "motm"
MESSAGES_PER_POINT = 50
SQUAD_POINTS = 2
MIN_SCORE = 1.0  # a quiet month has no member of the month


def motm_ref(month: str) -> str:
    return f"motm:{month}"


def activity_score(voice_seconds: int, messages: int, squads: int) -> float:
    return voice_seconds / HOUR + messages / MESSAGES_PER_POINT + squads * SQUAD_POINTS


def scores(voice: Mapping[int, int], messages: Mapping[int, int], squads: Mapping[int, int]) -> dict[int, float]:
    users = set(voice) | set(messages) | set(squads)
    return {u: activity_score(voice.get(u, 0), messages.get(u, 0), squads.get(u, 0)) for u in users}


def pick_winner(scored: Mapping[int, float], eligible: Callable[[int], bool]) -> int | None:
    """Highest score (ties: lowest id) among eligible members with at least MIN_SCORE."""
    for uid, score in sorted(scored.items(), key=lambda p: (-p[1], p[0])):
        if score < MIN_SCORE:
            return None
        if eligible(uid):
            return uid
    return None


def _n(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def motm_text(mention: str, month: str, voice_seconds: int, messages: int, squads: int, coins: int) -> str:
    stats = [f"{voice_seconds / HOUR:.1f} h in voice", _n(messages, "message"), _n(squads, "squad")]
    prize = f" Enjoy **{coins:,} coins** on us." if coins else ""
    return (f"🌟 **Member of the month for {shop.month_name(month)}:** {mention}!\n"
            f"{' · '.join(stats)}. Thanks for making this place fun.{prize}")
