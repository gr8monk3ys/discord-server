"""Economy health numbers for staff (/economy). Pure: no Discord, no database.

The cog hands in per-reason ledger totals for a window and the wallet balances; this module
turns them into minted vs burned, the biggest sources and sinks, and how concentrated the
coins are. "Minted" and "burned" are per-reason *net* amounts: a /give moves coins between
members (net 0), a prediction pays the pool back out (net 0), coinflip and slots net out to
whatever the house won or lost, and a raffle nets to the 20% that is burned. So the numbers
show coins entering and leaving circulation, not how busy the ledger was."""

from dataclasses import dataclass
from typing import Iterable

DAY = 24 * 60 * 60
WINDOW = 7 * DAY
TOP = 5  # sources and sinks shown
RICH = 10  # "top 10 hold X%"

LABELS = {
    "daily": "Daily", "voice": "Voice", "message": "Chat messages", "clip": "Clips", "lfg": "Squads",
    "mvp": "Weekly MVP", "clipweek": "Clip of the week", "give": "Gifts (/give)", "coinflip": "Coinflip",
    "slots": "Slots", "blackjack": "Blackjack", "trivia": "Trivia", "predict": "Predictions",
    "shop": "Shop", "season": "Season bonuses", "quest": "Starter quest", "challenge": "Weekly challenges",
    "word": "Daily Word", "tournament": "Tournaments", "invitecontest": "Invite contest",
    "boost": "Booster thanks", "booststipend": "Booster stipend", "motm": "Member of the month",
    "birthday": "Birthdays", "raffle": "Raffle",
}


def label(reason: str) -> str:
    return LABELS.get(reason, reason)


@dataclass(frozen=True)
class Flow:
    reason: str
    net: int


@dataclass(frozen=True)
class Summary:
    minted: int  # sum of reasons that added coins overall
    burned: int  # sum of reasons that removed coins overall (positive number)
    sources: list[Flow]  # biggest first
    sinks: list[Flow]  # biggest first (most negative net)

    @property
    def net(self) -> int:
        return self.minted - self.burned


def summarize(rows: Iterable[tuple[str, int]], top: int = TOP) -> Summary:
    """rows: (reason, net delta over the window). Several rows for one reason are added up."""
    nets: dict[str, int] = {}
    for reason, net in rows:
        nets[reason] = nets.get(reason, 0) + int(net or 0)
    sources = sorted((Flow(r, n) for r, n in nets.items() if n > 0), key=lambda f: (-f.net, f.reason))
    sinks = sorted((Flow(r, n) for r, n in nets.items() if n < 0), key=lambda f: (f.net, f.reason))
    return Summary(sum(f.net for f in sources), -sum(f.net for f in sinks), sources[:top], sinks[:top])


@dataclass(frozen=True)
class Supply:
    total: int
    holders: int  # wallets above 0
    median: int
    top_share: float  # 0..1 held by the RICH biggest wallets


def supply(balances: Iterable[int], rich: int = RICH) -> Supply:
    held = sorted((b for b in balances if b > 0), reverse=True)
    total = sum(held)
    if not held:
        return Supply(0, 0, 0, 0.0)
    n = len(held)
    median = held[n // 2] if n % 2 else (held[n // 2 - 1] + held[n // 2]) // 2
    return Supply(total, n, median, sum(held[:rich]) / total)


def pct(part: int | float, whole: int | float) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def flow_lines(flows: list[Flow]) -> str:
    if not flows:
        return "none"
    return "\n".join(f"{label(f.reason)}: {'+' if f.net > 0 else '-'}{abs(f.net):,}" for f in flows)


def verdict(s: Summary, total: int) -> str:
    """One line for staff: is the money supply growing fast?"""
    if s.minted == 0 and s.burned == 0:
        return "No coins moved this week."
    growth = s.net / total if total else None
    if s.net <= 0:
        return "Sinks are keeping up: the supply shrank or held steady this week."
    if growth is not None and growth > 0.10:
        return (f"The supply grew {pct(s.net, total)} in a week. Consider raising sink prices or "
                "trimming a payout.")
    return f"Mild inflation: the supply grew {pct(s.net, total) if total else f'{s.net:,} coins'} this week."
