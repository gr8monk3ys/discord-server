"""Tournament rules (no Discord I/O): random seeding, single-elimination brackets with
byes, advancing winners, the report / confirm flow and the text bracket.

Rounds are numbered from 1; slots from 0. The winner of (round, slot) plays in
(round + 1, slot // 2), as P1 from an even slot and P2 from an odd one. The bracket
is sized to the next power of two of the entrant count, so byes (< half the slots)
always face a real player and go to the top seeds.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Iterable

SIZES = (4, 8, 16, 32)  # signup caps staff can pick
PRIZE_FIRST = 1000
PRIZE_SECOND = 400
NAME_WIDTH = 16  # characters per name in the text bracket

# Match status
PENDING = "pending"  # waiting for one or both players
OPEN = "open"  # both players known, nobody has reported
REPORTED = "reported"  # one player reported; waiting for the other to confirm
CONFLICT = "conflict"  # the players disagree: staff decide
DONE = "done"
BYE = "bye"  # one player, advanced automatically

FINISHED = (DONE, BYE)
PLAYABLE = (OPEN, REPORTED, CONFLICT)


class Report(Enum):
    NOT_PLAYER = "not_player"
    NOT_OPEN = "not_open"  # players aren't both known yet
    DONE = "done"  # already decided
    RECORDED = "recorded"  # waiting for the other player
    CONFIRMED = "confirmed"  # the other player agreed: final
    STAFF = "staff"  # staff decided: final
    CONFLICT = "conflict"  # the other player disagreed: staff flagged
    DISPUTED = "disputed"  # already in conflict: only staff can decide

    @property
    def final(self) -> bool:
        return self in (Report.CONFIRMED, Report.STAFF)


@dataclass
class Match:
    round: int
    slot: int
    p1: int | None = None
    p2: int | None = None
    winner: int | None = None  # the decided winner, or the reported claim while REPORTED/CONFLICT
    reported_by: int | None = None
    status: str = PENDING
    id: int | None = None  # database row id, when loaded from one

    @property
    def players(self) -> tuple[int, ...]:
        return tuple(p for p in (self.p1, self.p2) if p is not None)

    def loser(self) -> int | None:
        if self.status != DONE:
            return None
        return self.p2 if self.winner == self.p1 else self.p1


# ---------------------------------------------------------------- sizes and seeding
def bracket_size(n: int) -> int:
    """Smallest power of two holding `n` entrants (at least 2)."""
    if n < 2:
        raise ValueError("a bracket needs at least two entrants")
    size = 2
    while size < n:
        size *= 2
    return size


def rounds(size: int) -> int:
    return size.bit_length() - 1


def seed_positions(size: int) -> list[int]:
    """Seeds in bracket order (1v8, 4v5, 2v7, 3v6 for 8): top seeds meet as late as possible."""
    order = [1]
    while len(order) < size:
        total = len(order) * 2 + 1
        order = [s for seed in order for s in (seed, total - seed)]
    return order


def seed(entrants: Iterable[int], rng: random.Random) -> list[int]:
    """A random seeding: index 0 is seed 1. Doesn't touch the input."""
    seeded = list(entrants)
    rng.shuffle(seeded)
    return seeded


# ---------------------------------------------------------------- bracket
class Bracket:
    def __init__(self, matches: Iterable[Match]):
        self.by_key: dict[tuple[int, int], Match] = {(m.round, m.slot): m for m in matches}
        self.final_round = max(r for r, _ in self.by_key) if self.by_key else 0

    @classmethod
    def build(cls, seeded: list[int]) -> "Bracket":
        """All matches for `seeded` (index 0 = seed 1), with byes already advanced."""
        if len(set(seeded)) != len(seeded):
            raise ValueError("an entrant appears twice")
        size = bracket_size(len(seeded))
        total = rounds(size)
        player = {i + 1: uid for i, uid in enumerate(seeded)}
        positions = seed_positions(size)
        matches = []
        for slot in range(size // 2):
            p1, p2 = player.get(positions[2 * slot]), player.get(positions[2 * slot + 1])
            matches.append(Match(round=1, slot=slot, p1=p1, p2=p2, status=OPEN))
        for r in range(2, total + 1):
            matches += [Match(round=r, slot=s) for s in range(size >> r)]
        bracket = cls(matches)
        for m in bracket.round(1):
            if m.p1 is None or m.p2 is None:
                m.status, m.winner = BYE, m.p1 if m.p1 is not None else m.p2
                bracket._advance(m)
        return bracket

    @property
    def matches(self) -> list[Match]:
        return [self.by_key[k] for k in sorted(self.by_key)]

    def round(self, number: int) -> list[Match]:
        return [m for m in self.matches if m.round == number]

    def get(self, round_: int, slot: int) -> Match:
        return self.by_key[(round_, slot)]

    def open_matches(self) -> list[Match]:
        return [m for m in self.matches if m.status in PLAYABLE]

    def decide(self, round_: int, slot: int, winner: int) -> list[Match]:
        """Make `winner` the winner of (round, slot) and move them on.
        Returns the matches that changed (this one, then the next one if any)."""
        m = self.get(round_, slot)
        if m.status not in PLAYABLE:
            raise ValueError(f"match {round_}/{slot} isn't being played ({m.status})")
        if winner not in m.players:
            raise ValueError(f"{winner} isn't in match {round_}/{slot}")
        m.winner, m.status = winner, DONE
        nxt = self._advance(m)
        return [m] + ([nxt] if nxt else [])

    def _advance(self, m: Match) -> Match | None:
        if m.round >= self.final_round:
            return None
        nxt = self.get(m.round + 1, m.slot // 2)
        if m.slot % 2 == 0:
            nxt.p1 = m.winner
        else:
            nxt.p2 = m.winner
        if nxt.p1 is not None and nxt.p2 is not None and nxt.status == PENDING:
            nxt.status = OPEN
        return nxt

    def final(self) -> Match | None:
        return self.by_key.get((self.final_round, 0))

    def champion(self) -> int | None:
        f = self.final()
        return f.winner if f is not None and f.status in FINISHED else None

    def runner_up(self) -> int | None:
        f = self.final()
        return f.loser() if f is not None else None


# ---------------------------------------------------------------- reporting
def report(match: Match, user_id: int, pick: int, staff: bool) -> tuple[Report, Match]:
    """Apply a "P1 won" (pick 1) / "P2 won" (pick 2) press. Returns the outcome and the
    match as it should be stored (a copy; the input is untouched).

    A player's report waits for the other player; the same pick from them makes it final,
    a different pick puts the match in CONFLICT for staff. Staff are final from any open
    state, except in their own match, where they're just a player."""
    if pick not in (1, 2):
        raise ValueError("pick is 1 or 2")
    if match.status in FINISHED:
        return Report.DONE, match
    if match.status == PENDING or match.p1 is None or match.p2 is None:
        return Report.NOT_OPEN, match
    claimed = match.p1 if pick == 1 else match.p2
    is_player = user_id in match.players
    if staff and not is_player:
        return Report.STAFF, replace(match, winner=claimed, reported_by=user_id, status=DONE)
    if not is_player:
        return Report.NOT_PLAYER, match
    if match.status == CONFLICT:
        return Report.DISPUTED, match
    if match.status == OPEN or match.reported_by == user_id:
        return Report.RECORDED, replace(match, winner=claimed, reported_by=user_id, status=REPORTED)
    if claimed == match.winner:
        return Report.CONFIRMED, replace(match, status=DONE)
    return Report.CONFLICT, replace(match, status=CONFLICT)


# ---------------------------------------------------------------- payouts
def payouts(tournament_id: int, champion: int, runner_up: int | None) -> list[tuple[int, int, str]]:
    """(user, coins, ledger ref) rows; the refs make paying twice impossible."""
    rows = [(champion, PRIZE_FIRST, f"tourney:{tournament_id}:first")]
    if runner_up is not None:
        rows.append((runner_up, PRIZE_SECOND, f"tourney:{tournament_id}:second"))
    return rows


# ---------------------------------------------------------------- text
def round_name(number: int, total: int) -> str:
    left = total - number
    return {0: "Final", 1: "Semifinals", 2: "Quarterfinals"}.get(left, f"Round {number}")


def clean_name(name: str | None) -> str:
    """Safe inside a code block and one column wide."""
    text = " ".join(str(name or "").replace("`", "").split())
    if not text:
        return "?"
    if len(text) > NAME_WIDTH:
        text = text[:NAME_WIDTH - 1] + "…"
    return text


def render_bracket(bracket: Bracket, name_of: Callable[[int], str | None]) -> str:
    """The bracket as a monospace block, one line per match."""
    def name(uid):
        return "TBD" if uid is None else clean_name(name_of(uid))

    lines = []
    for r in range(1, bracket.final_round + 1):
        if lines:
            lines.append("")
        lines.append(round_name(r, bracket.final_round))
        for m in bracket.round(r):
            left = f"{m.slot + 1:>2} {name(m.p1):<{NAME_WIDTH}} v {name(m.p2) if m.status != BYE else '(bye)':<{NAME_WIDTH}}"
            if m.status == BYE and m.p1 is None:
                left = f"{m.slot + 1:>2} {'(bye)':<{NAME_WIDTH}} v {name(m.p2):<{NAME_WIDTH}}"
            note = {
                DONE: f"> {name(m.winner)}",
                BYE: f"> {name(m.winner)}",
                REPORTED: "awaiting confirmation",
                CONFLICT: "disputed",
            }.get(m.status, "")
            lines.append(f"{left} {note}".rstrip())
    champ = bracket.champion()
    if champ is not None:
        lines += ["", f"Champion: {name(champ)}"]
    return "```\n" + "\n".join(lines) + "\n```"
