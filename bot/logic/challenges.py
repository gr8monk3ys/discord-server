"""Weekly challenges: three goals a week (one per tier), picked deterministically from a
pool of twelve, with coin rewards and a bonus for doing all three. Pure: no Discord, no
database. The cog computes progress from other modules' tables for the week's window
(never from counters) and records claims in `challenge_claims`.

Challenges that come from tracked activity (/privacy off stops recording it, or /daily
and clip posts refuse opted-out members) show as unavailable to opted-out members; the
bonus then needs the other ones. At most one tracking-based challenge is picked a week."""

import random
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from logic.quests import MIN_ACCOUNT_DAYS
from logic.schedule import Weekly
from logic.stats import fmt_duration

DAY = 24 * 60 * 60
HOUR = 60 * 60
REWARDS = {1: 150, 2: 250, 3: 400}
BONUS = 300
BONUS_KEY = "bonus"
REASON = "challenge"
MAX_TRACKING = 1  # tracking-based challenges per week, so opted-out members still have two
GRACE = DAY  # last week's challenges can still be claimed this long into the new week
BOARD_JOB = Weekly("challenges", weekday=0, hour=9, minute=0)  # Mondays 09:00 local


@dataclass(frozen=True)
class Challenge:
    key: str
    tier: int  # 1..3, sets the reward
    emoji: str
    name: str
    how: str  # shown to members
    target: int  # seconds when hours=True, else a count
    hours: bool = False
    tracking: bool = False  # needs tracked activity: unavailable after /privacy off


POOL: tuple[Challenge, ...] = (
    Challenge("join_squads", 1, "🤝", "Squad up", "join 2 squads someone else posted with `/lfg`", 2),
    Challenge("post_clip", 1, "🎬", "Share a clip", "post a clip in the clips channel", 1, tracking=True),
    Challenge("win_game", 1, "🃏", "Beat the house", "win a hand of `/blackjack` or a `/trivia` question", 1),
    Challenge("messages", 1, "💬", "Chatty week", "send 100 messages", 100, tracking=True),
    Challenge("daily", 2, "🪙", "Regular", "claim `/daily` on 5 days", 5, tracking=True),
    Challenge("word_games", 2, "🔤", "Wordsmith", "solve the daily word game (`/word`) 3 times", 3),
    Challenge("tournament", 2, "🏆", "Contender", "enter a tournament", 1),
    Challenge("voice_3h", 2, "🎧", "Hang out", "spend 3 h in voice with others", 3 * HOUR, hours=True, tracking=True),
    Challenge("hall_of_fame", 3, "⭐", "Famous", "get a message into the hall of fame", 1),
    Challenge("gamenight", 3, "🎉", "Host", "host a `/gamenight` this week", 1),
    Challenge("host_squads", 3, "📣", "Rally point", "post 2 squads with `/lfg` that someone joins", 2),
    Challenge("voice_8h", 3, "🎙️", "Voice marathon", "spend 8 h in voice with others", 8 * HOUR, hours=True,
              tracking=True),
)
BY_KEY = {c.key: c for c in POOL}


# ---------------------------------------------------------------- weeks
@dataclass(frozen=True)
class Week:
    key: str  # f"{iso_year}-W{iso_week:02}"
    start: int  # Monday 00:00 local, unix seconds
    end: int  # next Monday 00:00 local
    first_day: date  # Monday
    last_day: date  # Sunday


def _monday(local: date) -> date:
    return local - timedelta(days=local.weekday())


def _week(monday: date, tz: tzinfo) -> Week:
    iso = monday.isocalendar()
    start = int(datetime.combine(monday, time(0), tz).timestamp())
    end = int(datetime.combine(monday + timedelta(days=7), time(0), tz).timestamp())
    return Week(f"{iso.year}-W{iso.week:02}", start, end, monday, monday + timedelta(days=6))


def week_of(now: int, tz: tzinfo) -> Week:
    return _week(_monday(datetime.fromtimestamp(now, tz).date()), tz)


def previous_week(week: Week, tz: tzinfo) -> Week:
    return _week(week.first_day - timedelta(days=7), tz)


def weeks_to_settle(now: int, tz: tzinfo) -> list[Week]:
    """The weeks claims can still be paid for: this one, plus last week during GRACE."""
    week = week_of(now, tz)
    if now - week.start < GRACE:
        return [previous_week(week, tz), week]
    return [week]


# ---------------------------------------------------------------- picks
def pick(week_key: str) -> tuple[Challenge, ...]:
    """One challenge per tier, seeded by the week key (a str seed is stable across runs),
    with at most MAX_TRACKING tracking-based ones."""
    rng = random.Random(f"challenges:{week_key}")
    picked: list[Challenge] = []
    for tier in sorted(REWARDS):
        options = [c for c in POOL if c.tier == tier]
        if sum(c.tracking for c in picked) >= MAX_TRACKING:
            options = [c for c in options if not c.tracking]
        picked.append(rng.choice(options))
    return tuple(picked)


# ---------------------------------------------------------------- progress and claims
def reward(c: Challenge) -> int:
    return REWARDS[c.tier]


def available(c: Challenge, tracking: bool) -> bool:
    return tracking or not c.tracking


def complete(c: Challenge, value: int) -> bool:
    return value >= c.target


def claimable(picks: Iterable[Challenge], progress: Mapping[str, int], claimed: Collection[str],
              tracking: bool) -> list[tuple[str, int]]:
    """(key, coins) to pay now: finished, unclaimed, available challenges, then the bonus once
    every available challenge is finished."""
    out: list[tuple[str, int]] = []
    usable = [c for c in picks if available(c, tracking)]
    for c in usable:
        if c.key not in claimed and complete(c, progress.get(c.key, 0)):
            out.append((c.key, reward(c)))
    if usable and BONUS_KEY not in claimed and all(complete(c, progress.get(c.key, 0)) for c in usable):
        out.append((BONUS_KEY, BONUS))
    return out


def total(items: Iterable[tuple[str, int]]) -> int:
    return sum(coins for _, coins in items)


def ref(week_key: str, user_id: int, key: str) -> str:
    return f"challenge:{week_key}:{user_id}:{key}"


def old_enough(created_at: int | None, now: int) -> bool:
    """Accounts younger than MIN_ACCOUNT_DAYS (likely alts) can't claim."""
    return created_at is not None and now - created_at >= MIN_ACCOUNT_DAYS * DAY


# ---------------------------------------------------------------- text
def _amount(c: Challenge, value: int) -> str:
    if c.hours:
        return fmt_duration(value) if value < c.target else f"{c.target // HOUR}h"
    return f"{min(value, c.target):,}"


def fmt_progress(c: Challenge, value: int) -> str:
    goal = f"{c.target // HOUR}h" if c.hours else f"{c.target:,}"
    return f"{_amount(c, value)} / {goal}"


def board_text(picks: Iterable[Challenge], week: Week) -> str:
    lines = [f"{c.emoji} **{c.name}**: {c.how} · **{reward(c):,} coins**" for c in picks]
    lines += ["", f"Do all three for a **{BONUS:,} coin** bonus.",
              f"Check your progress and claim with `/challenges`. Resets <t:{week.end}:R>."]
    return "\n".join(lines)


def progress_text(picks: Iterable[Challenge], progress: Mapping[str, int], claimed: Collection[str],
                  tracking: bool) -> str:
    lines = []
    picks = list(picks)
    for c in picks:
        if not available(c, tracking):
            lines.append(f"➖ {c.emoji} ~~{c.name}~~: unavailable, stats are off (`/privacy`)")
            continue
        value = progress.get(c.key, 0)
        if c.key in claimed:
            mark = "✅"
        elif complete(c, value):
            mark = "🎁"
        else:
            mark = "▫️"
        lines.append(f"{mark} {c.emoji} **{c.name}**: {c.how} · {fmt_progress(c, value)} · {reward(c):,} coins")
    bonus_mark = "✅" if BONUS_KEY in claimed else "🎁" if any(
        k == BONUS_KEY for k, _ in claimable(picks, progress, claimed, tracking)) else "▫️"
    lines.append(f"{bonus_mark} 🌟 **Bonus**: finish all of them · {BONUS:,} coins")
    return "\n".join(lines)


def claim_text(mention: str, items: Iterable[tuple[str, int]]) -> str:
    items = list(items)
    names = [BY_KEY[k].name if k in BY_KEY else "the bonus" for k, _ in items]
    return (f"🏅 {mention} finished {', '.join(names)} and earned **{total(items):,} coins**. "
            "See this week's challenges with `/challenges`.")
