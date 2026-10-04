"""Module 4a: economy rules. Pure: no Discord, no database.

How much each action earns, the daily caps, the /daily streak, and the bet and
gift checks. Coins only ever move through bot/economy.py; this module just decides
the amounts and the ledger refs that make payouts idempotent."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

# ---------------------------------------------------------------- amounts
DAILY_BASE = 100
DAILY_STEP = 20  # per day of streak after the first
DAILY_BONUS_CAP = 140  # so day 8+ pays 240

VOICE_COINS = 2
VOICE_TICK_SECONDS = 5 * 60
VOICE_MIN_HUMANS = 2

MESSAGE_COINS = 1
MESSAGE_DAILY_CAP = 50  # coins (= messages) per local day

CLIP_COINS = 25
CLIP_DAILY_MAX = 3  # paid clips per local day

LFG_COINS = 20
MVP_COINS = 250
CLIP_WEEK_COINS = 500

GIVE_MIN = 1
MIN_BET = 10
MAX_BET = 5_000
SIDES = ("heads", "tails")

# ledger reasons
DAILY, VOICE, MESSAGE, CLIP, LFG, MVP, CLIP_WEEK, GIVE, COINFLIP = (
    "daily", "voice", "message", "clip", "lfg", "mvp", "clipweek", "give", "coinflip")


# ---------------------------------------------------------------- local days
def local_date(now: int, tz) -> date:
    return datetime.fromtimestamp(now, tz).date()


def day_bounds(now: int, tz) -> tuple[int, int]:
    """[start, end) of the local day containing `now`, in unix seconds (23 or 25 h on DST days)."""
    today = local_date(now, tz)
    start = datetime.combine(today, time(0), tz)
    end = datetime.combine(today + timedelta(days=1), time(0), tz)
    return int(start.timestamp()), int(end.timestamp())


# ---------------------------------------------------------------- /daily
def daily_amount(streak: int) -> int:
    return DAILY_BASE + min(DAILY_STEP * (max(streak, 1) - 1), DAILY_BONUS_CAP)


@dataclass(frozen=True)
class DailyClaim:
    day: str  # local date, ISO
    streak: int
    amount: int


def claim_daily(last_daily: str | None, streak: int, now: int, tz) -> DailyClaim | None:
    """The claim for `now`, or None if today's (local) daily is already taken.
    The streak continues if the last claim was yesterday, otherwise starts again at 1."""
    today = local_date(now, tz)
    last = date.fromisoformat(last_daily) if last_daily else None
    if last is not None and last >= today:
        return None
    new_streak = streak + 1 if last == today - timedelta(days=1) else 1
    return DailyClaim(today.isoformat(), new_streak, daily_amount(new_streak))


def current_streak(last_daily: str | None, streak: int, now: int, tz) -> int:
    """The streak as it stands: alive if claimed today or yesterday, else 0."""
    if not last_daily:
        return 0
    return streak if date.fromisoformat(last_daily) >= local_date(now, tz) - timedelta(days=1) else 0


# ---------------------------------------------------------------- voice
def voice_tick(now: int) -> int:
    return now // VOICE_TICK_SECONDS


def voice_earners(channels: Iterable[Iterable[tuple[int, bool]]], opted_out=frozenset()) -> list[int]:
    """Who earns this tick. `channels` holds, per counted voice channel, (user_id, is_bot)
    for everyone in it. A channel pays only with 2+ humans; bots never earn and don't count
    as company; opted-out members count as company but don't earn."""
    earners = []
    for people in channels:
        humans = [uid for uid, is_bot in people if not is_bot]
        if len(humans) >= VOICE_MIN_HUMANS:
            earners += [uid for uid in humans if uid not in opted_out]
    return sorted(set(earners))


# ---------------------------------------------------------------- daily caps
def message_payout(earned_today: int) -> int:
    return MESSAGE_COINS if earned_today + MESSAGE_COINS <= MESSAGE_DAILY_CAP else 0


def clip_payout(earned_today: int) -> int:
    return CLIP_COINS if earned_today // CLIP_COINS < CLIP_DAILY_MAX else 0


# ---------------------------------------------------------------- refs
def daily_ref(day: str, user_id: int) -> str:
    return f"daily:{day}:{user_id}"


def voice_ref(user_id: int, tick: int) -> str:
    return f"voice:{user_id}:{tick}"


def clip_ref(message_id: int) -> str:
    return f"clip:{message_id}"


def lfg_ref(post_id: int, user_id: int) -> str:
    return f"lfg:{post_id}:{user_id}"


def mvp_ref(period_key: str, user_id: int) -> str:
    """The stats job key is 'mvp:2026-W40'; the ref is 'mvp:2026-W40:<user>' either way."""
    period = period_key.split(":", 1)[1] if period_key.startswith("mvp:") else period_key
    return f"mvp:{period}:{user_id}"


def clip_week_ref(week: str) -> str:
    return f"clipweek:{week}"


# ---------------------------------------------------------------- /give
def give_error(giver_id: int, target_id: int, target_is_bot: bool, amount: int) -> str | None:
    if amount < GIVE_MIN:
        return f"You can give {GIVE_MIN} coin or more."
    if target_id == giver_id:
        return "You can't give coins to yourself."
    if target_is_bot:
        return "Bots don't have wallets."
    return None


# ---------------------------------------------------------------- /coinflip
def bet_error(bet: int, balance: int) -> str | None:
    if bet < MIN_BET:
        return f"The smallest bet is {MIN_BET} coins."
    if bet > MAX_BET:
        return f"The biggest bet is {MAX_BET:,} coins."
    if bet > balance:
        return f"You only have {balance:,} coins."
    return None


def flip(rng) -> str:
    return rng.choice(SIDES)


def coinflip_net(bet: int, won: bool) -> int:
    """A win pays 2x the bet back, so +bet overall; a loss is -bet."""
    return bet if won else -bet
