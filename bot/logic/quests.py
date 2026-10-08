"""Starter quest: five first steps that get a new member settled in, a one-time coin
reward for finishing them, and one nudge DM a day after joining. Pure: no Discord, no
database. The cog records steps in `quest_steps` from events and an hourly sweep.

Two steps (saying hi, joining voice) are detected from activity that /privacy off
stops tracking; opted-out members skip them and finish with the other three."""

from collections.abc import Collection, Iterable
from dataclasses import dataclass

import config
from names import slug

DAY = 24 * 60 * 60
REWARD = 500
REASON = "quest"
BADGE_KEY = "starter"  # granted on completion when logic/achievements.py has it
NEW_MEMBER_DAYS = 30  # finish within this many days of joining to be paid
MIN_ACCOUNT_DAYS = 30  # accounts younger than this are not paid (alt farming via /give)
DISCORD_EPOCH_MS = 1420070400000  # snowflake ids count milliseconds from here
NUDGE_AFTER = DAY  # one DM this long after joining...
NUDGE_UNTIL = 7 * DAY  # ...unless they joined longer ago than this (bot was down)
NUDGE_BELOW = 3  # ...and only when fewer steps than this are done


def account_created(user_id: int) -> int:
    """When a Discord account was created (unix seconds), read from its snowflake id. Works
    for members who have left, and in SQL as ((id >> 22) + DISCORD_EPOCH_MS) / 1000."""
    return ((user_id >> 22) + DISCORD_EPOCH_MS) // 1000


def established(user_id: int, at: int) -> bool:
    """The account was at least MIN_ACCOUNT_DAYS old at `at`. Rewards that other people's
    actions earn someone (joins, stars, voice company) only count established accounts, so
    fresh alts can't farm them."""
    return at - account_created(user_id) >= MIN_ACCOUNT_DAYS * DAY


def established_sql(user_col: str, at_col: str) -> str:
    """established() as an SQLite expression over two column names (ours, never user input)."""
    return f"({at_col} - (({user_col} >> 22) + {DISCORD_EPOCH_MS}) / 1000 >= {MIN_ACCOUNT_DAYS * DAY})"


@dataclass(frozen=True)
class Step:
    key: str
    emoji: str
    name: str
    how: str  # shown to members
    tracking: bool = False  # detected from tracked activity: skipped for /privacy off


STEPS: tuple[Step, ...] = (
    Step("pick_roles", "🎭", "Pick your roles", "grab a platform, region or game role"),
    Step("say_hi", "👋", "Say hi", f"send a message in {config.GENERAL_CHANNEL}", tracking=True),
    Step("join_squad", "🤝", "Join a squad", "join or post one with `/lfg`"),
    Step("claim_daily", "🪙", "Claim your daily", "run `/daily`"),
    Step("join_voice", "🎧", "Hop in voice", "join any voice channel", tracking=True),
)
BY_KEY = {s.key: s for s in STEPS}

PICK_ROLE_SLUGS = frozenset(slug(n) for n in (*config.PLATFORM_ROLES, *config.REGION_ROLES,
                                              *(g.role for g in config.GAMES)))


def required(tracking: bool) -> list[str]:
    return [s.key for s in STEPS if tracking or not s.tracking]


def progress(done: Collection[str], tracking: bool) -> tuple[int, int]:
    need = required(tracking)
    return sum(1 for k in need if k in done), len(need)


def is_complete(done: Collection[str], tracking: bool) -> bool:
    got, total = progress(done, tracking)
    return got == total


def has_picked_roles(role_names: Iterable[str]) -> bool:
    return any(slug(n) in PICK_ROLE_SLUGS for n in role_names)


def eligible_for_reward(joined_at: int | None, started_at: int | None, now: int, *,
                        created_at: int | None) -> bool:
    """Joined after the module went live, finished within NEW_MEMBER_DAYS of joining, and the
    Discord account is at least MIN_ACCOUNT_DAYS old (fresh alts can't farm the reward)."""
    if joined_at is None or started_at is None or created_at is None:
        return False
    return (joined_at >= started_at and now - joined_at <= NEW_MEMBER_DAYS * DAY
            and now - created_at >= MIN_ACCOUNT_DAYS * DAY)


def should_nudge(joined_at: int | None, started_at: int | None, now: int, done_count: int) -> bool:
    if joined_at is None or started_at is None or joined_at < started_at:
        return False
    return NUDGE_AFTER <= now - joined_at <= NUDGE_UNTIL and done_count < NUDGE_BELOW


def ref(user_id: int) -> str:
    return f"quest:{user_id}"


def checklist(done: Collection[str], tracking: bool) -> str:
    lines = []
    for s in STEPS:
        if s.tracking and not tracking:
            lines.append(f"➖ {s.emoji} ~~{s.name}~~: skipped, stats are off (`/privacy`)")
        else:
            mark = "✅" if s.key in done else "▫️"
            lines.append(f"{mark} {s.emoji} **{s.name}**: {s.how}")
    return "\n".join(lines)


def congrats(mention: str, coins: int, badge: bool) -> str:
    extra = " and the Starter badge" if badge else ""
    return (f"🎉 {mention} finished the starter quest and earned **{coins:,} coins**{extra}. "
            "Welcome in!")


def nudge_text(done: Collection[str], tracking: bool, rewarded: bool = True) -> str:
    got, total = progress(done, tracking)
    prize = f" Finish them all for **{REWARD:,} coins**." if rewarded else ""
    return (f"Hey, welcome to the server! Here's your starter quest ({got}/{total} done).{prize}\n\n"
            f"{checklist(done, tracking)}\n\nCheck your progress any time with `/quest`.")
