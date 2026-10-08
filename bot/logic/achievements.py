"""Achievements: the badge catalogue and which badges a member's numbers earn.
Pure: no Discord, no database. The cog gathers a `Facts` per member from the
existing tables (squads, voice, messages, clips, wallets...) and grants whatever
`new_badges` returns, once each."""

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time

from logic import shop

HOUR = 60 * 60
EARLY_BEFORE = date(2026, 11, 1)  # joined before this local date = founding member
SEASON_REASONS = shop.SEASON_REASONS  # season points = coins earned this month for these reasons
NO_BADGES = "No badges yet."
PER_ROW = 5


@dataclass(frozen=True)
class Facts:
    """One member's numbers, as the cog read them. tracking=False: opted out of stats."""

    squads_joined: int = 0  # lfg squads joined (not ones they host)
    squads_hosted: int = 0
    gamenights_hosted: int = 0
    voice_seconds: int = 0  # counted voice (2+ people, not AFK), all time
    messages: int = 0
    clips: int = 0
    clip_week_wins: int = 0
    hall_of_fame: int = 0
    mvp_wins: int = 0
    daily_streak: int = 0
    balance: int = 0
    tournaments_entered: int = 0
    tournament_wins: int = 0
    birthday_set: bool = False
    recruiter: bool = False
    early_member: bool = False
    tracking: bool = True


@dataclass(frozen=True)
class Badge:
    key: str
    emoji: str
    name: str
    description: str  # how it's earned, shown to members
    check: Callable[[Facts], bool]
    tracking: bool = False  # needs stat tracking: never granted to opted-out members


BADGES: tuple[Badge, ...] = (
    Badge("first_squad", "🤝", "Squad Up", "Join someone's /lfg squad", lambda f: f.squads_joined >= 1),
    Badge("squad_regular", "🎮", "Regular", "Join 10 /lfg squads", lambda f: f.squads_joined >= 10),
    Badge("squad_host", "📣", "Rally Point", "Host an /lfg squad", lambda f: f.squads_hosted >= 1),
    Badge("gamenight_host", "🌙", "Game Night Host", "Host a /gamenight", lambda f: f.gamenights_hosted >= 1),
    Badge("voice_10h", "🎧", "Tuned In", "10 hours in voice with others", lambda f: f.voice_seconds >= 10 * HOUR,
          tracking=True),
    Badge("voice_100h", "📻", "On Air", "100 hours in voice with others", lambda f: f.voice_seconds >= 100 * HOUR,
          tracking=True),
    Badge("chatty", "💬", "Chatty", "Send 100 messages", lambda f: f.messages >= 100, tracking=True),
    Badge("messages_1000", "🗯️", "Conversationalist", "Send 1,000 messages", lambda f: f.messages >= 1000,
          tracking=True),
    Badge("first_clip", "🎬", "Director", "Post a clip in the clips channel", lambda f: f.clips >= 1),
    Badge("clip_week", "🏆", "Clip of the Week", "Win the Clip of the Week vote", lambda f: f.clip_week_wins >= 1),
    Badge("hall_of_fame", "⭐", "Hall of Famer", "Get a message into the hall of fame", lambda f: f.hall_of_fame >= 1),
    Badge("weekly_mvp", "👑", "MVP", "Be the weekly MVP", lambda f: f.mvp_wins >= 1),
    Badge("streak_7", "🔥", "On Fire", "Reach a 7-day /daily streak", lambda f: f.daily_streak >= 7),
    Badge("streak_30", "☄️", "Unstoppable", "Reach a 30-day /daily streak", lambda f: f.daily_streak >= 30),
    Badge("coins_10k", "💰", "High Roller", "Hold 10,000 coins at once", lambda f: f.balance >= 10_000),
    Badge("tourney_entry", "⚔️", "Contender", "Enter a tournament", lambda f: f.tournaments_entered >= 1),
    Badge("tourney_win", "🥇", "Champion", "Win a tournament", lambda f: f.tournament_wins >= 1),
    Badge("birthday", "🎂", "Party Planner", "Set your birthday with /birthday", lambda f: f.birthday_set),
    Badge("recruiter", "🧲", "Recruiter", "Invite 3 people who stay", lambda f: f.recruiter),
    Badge("early_member", "🌱", "Founding Member", "Joined before November 2026", lambda f: f.early_member),
)
BY_KEY = {b.key: b for b in BADGES}
TOTAL = len(BADGES)


def earned(facts: Facts) -> set[str]:
    return {b.key for b in BADGES if (facts.tracking or not b.tracking) and b.check(facts)}


def new_badges(facts: Facts, held: Collection[str]) -> list[Badge]:
    """Badges these numbers earn that the member doesn't hold yet, in catalogue order."""
    got = earned(facts)
    return [b for b in BADGES if b.key in got and b.key not in held]


def early_cutoff(tz) -> int:
    return int(datetime.combine(EARLY_BEFORE, time(0), tz).timestamp())


def is_early(joined_at: datetime | None, tz) -> bool:
    return joined_at is not None and joined_at.timestamp() < early_cutoff(tz)


def held_badges(held: Iterable[str]) -> list[Badge]:
    held = set(held)
    return [b for b in BADGES if b.key in held]


def grid(held: Iterable[str], per_row: int = PER_ROW) -> str:
    emoji = [b.emoji for b in held_badges(held)]
    if not emoji:
        return NO_BADGES
    return "\n".join(" ".join(emoji[i:i + per_row]) for i in range(0, len(emoji), per_row))


def progress(held: Iterable[str]) -> str:
    return f"{len(held_badges(held))}/{TOTAL}"


def congrats(mention: str, badges: list[Badge]) -> str:
    if len(badges) == 1:
        b = badges[0]
        return f"🎉 {mention} unlocked {b.emoji} **{b.name}**: {b.description}. See yours with `/profile`."
    names = ", ".join(f"{b.emoji} **{b.name}**" for b in badges)
    return f"🎉 {mention} unlocked {len(badges)} badges: {names}. See yours with `/profile`."
