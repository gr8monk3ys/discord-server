"""Vote hub: the places members can support the server, and the weekly Supporter thanks.
Pure: no Discord, no database.

/vote lists Disboard (bumped with /bump in the bot channel; the growth cog records who
bumped and when), discord.me, top.gg and the short share link. Sundays at 17:00 local the top
three Disboard bumpers of the past week are thanked in the games channel and paid
300/200/100 coins, each with ledger ref support:WEEK:PLACE so nothing is paid twice.

Anti-farming: Disboard allows one bump per server every two hours whoever runs it, so alts
can't add bumps; they could only collect the prize, and accounts younger than
MIN_ACCOUNT_DAYS are thanked without coins.
"""

from dataclasses import dataclass
from typing import Callable, Iterable

from logic.quests import MIN_ACCOUNT_DAYS, established
from logic.schedule import Weekly

DISCORD_ME_URL = "https://discord.me/chill-gamers-hq"
TOPGG_URL = ""  # the server's top.gg page; empty until the listing is approved
SHARE_URL = "https://discord.lscaturchio.xyz/join"

SUPPORT_JOB = Weekly("support", weekday=6, hour=17, minute=0)
REWARDS = (300, 200, 100)  # coins for 1st, 2nd, 3rd
REASON = "support"  # ledger reason
MEDALS = ("🥇", "🥈", "🥉")
COMING_SOON = "Coming soon."


@dataclass(frozen=True)
class Place:
    rank: int  # 1-based
    user_id: int
    bumps: int
    coins: int  # 0 when the account is too new to be paid


def link_url(url: str | None) -> str | None:
    """`url` if it is a plain https link we'd put on a button, else None (empty = not listed yet)."""
    url = (url or "").strip()
    if not url.startswith("https://") or any(c.isspace() or c in "<>()[]" for c in url) or len(url) > 200:
        return None
    return url


def next_bump_at(due: int | None, last_bump: int | None, cooldown: int) -> int | None:
    """When Disboard takes the next bump: the pending reminder time if there is one, else the
    last recorded bump plus the cooldown (the later of the two). None when no bump is known."""
    times = [t for t in (due, last_bump + cooldown if last_bump is not None else None) if t is not None]
    return max(times) if times else None


def bump_ready_text(ready_at: int | None, now: int) -> str:
    if ready_at is None or ready_at <= now:
        return "Ready now."
    return f"Ready <t:{ready_at}:R>."


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def my_bumps_text(week: int, month: int) -> str:
    if month <= 0:
        return "You haven't bumped yet."
    return f"Your bumps: {week} this week · {month} in the past 30 days."


def disboard_text(channel: str | None, ready_at: int | None, now: int, week: int, month: int) -> str:
    """channel: a channel mention, or None when the bot channel can't be found."""
    where = f"in {channel}" if channel else "in the bot channel"
    return "\n".join((f"Run `/bump` {where} every 2 hours. {bump_ready_text(ready_at, now)}",
                      my_bumps_text(week, month)))


def discord_me_text() -> str:
    return f"[Open our page]({DISCORD_ME_URL}) and press Bump (sign in with Discord there)."


def topgg_text(url: str | None = None) -> str:
    url = link_url(TOPGG_URL if url is None else url)
    return f"[Vote for us]({url}) every 12 hours." if url else COMING_SOON


def share_text() -> str:
    return f"Send a friend the short link: <{SHARE_URL}>"


def rewards_text() -> str:
    coins = "/".join(str(c) for c in REWARDS)
    return (f"Every Sunday the week's top {len(REWARDS)} Disboard bumpers get {coins} coins "
            f"(Discord accounts {MIN_ACCOUNT_DAYS}+ days old).")


def rank_bumpers(rows: Iterable[tuple[int, int, int]], limit: int = len(REWARDS)) -> list[tuple[int, int]]:
    """rows: (user_id, bumps, last_bump_at). Most bumps first; a tie goes to whoever got there
    first (earlier last bump), then the lower id. Returns up to `limit` (user_id, bumps)."""
    ranked = sorted((r for r in rows if r[1] > 0), key=lambda r: (-r[1], r[2], r[0]))
    return [(uid, n) for uid, n, _ in ranked[:limit]]


def places(rows: Iterable[tuple[int, int, int]], at: int,
           is_established: Callable[[int, int], bool] = established) -> list[Place]:
    """The week's podium. Everyone on it is thanked; only accounts established at `at` get coins."""
    return [Place(i + 1, uid, n, REWARDS[i] if is_established(uid, at) else 0)
            for i, (uid, n) in enumerate(rank_bumpers(rows))]


def week_of(period_key: str) -> str:
    """'support:2026-W41' -> '2026-W41'."""
    return period_key.split(":", 1)[1]


def ref(period_key: str, rank: int) -> str:
    """Ledger ref support:WEEK:PLACE, e.g. support:2026-W41:1."""
    return f"{SUPPORT_JOB.name}:{week_of(period_key)}:{rank}"


def thanks_text(podium: list[Place]) -> str:
    lines = ["Thanks for keeping the server easy to find on Disboard this week.", ""]
    for p in podium:
        prize = f"+{p.coins} coins" if p.coins else f"coins need a {MIN_ACCOUNT_DAYS}+ day old account"
        lines.append(f"{MEDALS[p.rank - 1]} <@{p.user_id}>  {plural(p.bumps, 'bump')} · {prize}")
    lines += ["", "Want in? `/vote` shows every way to support the server."]
    return "\n".join(lines)
