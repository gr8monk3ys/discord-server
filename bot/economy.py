"""The only way coins move. Every change is one ledger row plus a wallet update in
the same transaction, so a balance can't go negative (CHECK + an explicit check
for a friendly message) and a payout with a `ref` can never be paid twice.

Use the module functions with the Database for single moves, or the `*_tx`
variants inside an existing `db.transaction()` when several moves must happen
together (a /give, a prediction payout).
"""

from dataclasses import dataclass
from enum import Enum


class Status(Enum):
    OK = "ok"
    INSUFFICIENT = "insufficient"  # would go below 0: nothing changed
    DUPLICATE = "duplicate"  # ref already paid: nothing changed


@dataclass(frozen=True)
class Result:
    status: Status
    balance: int  # balance after (or unchanged balance if nothing happened)

    @property
    def ok(self) -> bool:
        return self.status is Status.OK


async def balance_tx(tx, user_id: int) -> int:
    row = await tx.fetchone("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))
    return row["balance"] if row else 0


async def apply_tx(tx, user_id: int, delta: int, reason: str, now: int, ref: str | None = None) -> Result:
    """Add `delta` (may be negative) inside an open transaction."""
    current = await balance_tx(tx, user_id)
    if ref is not None:
        if await tx.fetchone("SELECT 1 FROM ledger WHERE ref = ?", (ref,)):
            return Result(Status.DUPLICATE, current)
    if current + delta < 0:
        return Result(Status.INSUFFICIENT, current)
    # Not an upsert: SQLite checks CHECK(balance >= 0) on the would-be-inserted row,
    # so a negative delta would fail even when the existing balance covers it.
    await tx.execute("INSERT OR IGNORE INTO wallets (user_id, balance) VALUES (?, 0)", (user_id,))
    await tx.execute("UPDATE wallets SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
    await tx.execute("INSERT INTO ledger (user_id, delta, reason, ref, at) VALUES (?, ?, ?, ?, ?)",
                     (user_id, delta, reason, ref, now))
    return Result(Status.OK, current + delta)


async def apply(db, user_id: int, delta: int, reason: str, now: int, ref: str | None = None) -> Result:
    async with db.transaction() as tx:
        return await apply_tx(tx, user_id, delta, reason, now, ref)


async def transfer_tx(tx, from_id: int, to_id: int, amount: int, reason: str, now: int,
                      ref: str | None = None) -> Result:
    """Move coins between members; all or nothing. Returns the sender's result."""
    if amount <= 0 or from_id == to_id:
        raise ValueError("transfer needs a positive amount between two different members")
    sent = await apply_tx(tx, from_id, -amount, reason, now, f"{ref}:from" if ref else None)
    if not sent.ok:
        return sent
    await apply_tx(tx, to_id, amount, reason, now, f"{ref}:to" if ref else None)
    return sent


async def transfer(db, from_id: int, to_id: int, amount: int, reason: str, now: int,
                   ref: str | None = None) -> Result:
    async with db.transaction() as tx:
        return await transfer_tx(tx, from_id, to_id, amount, reason, now, ref)


async def balance(db, user_id: int) -> int:
    row = await db.fetchone("SELECT balance FROM wallets WHERE user_id = ?", (user_id,))
    return row["balance"] if row else 0


async def earned_tx(tx, user_id: int, reason: str, since: int, until: int) -> int:
    """Coins earned for `reason` in [since, until): used for daily caps."""
    row = await tx.fetchone(
        "SELECT COALESCE(SUM(delta), 0) AS n FROM ledger WHERE user_id = ? AND reason = ? AND delta > 0"
        " AND at >= ? AND at < ?", (user_id, reason, since, until))
    return row["n"]
