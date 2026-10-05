import asyncio

import pytest

import db as dbmod


def run(coro):
    return asyncio.run(coro)


async def fresh():
    d = dbmod.Database(":memory:")
    await d.connect()
    await d.migrate()
    return d


def test_migrations_apply_to_empty_db_and_are_idempotent():
    async def go():
        d = await fresh()
        assert await d.migrate() == len(dbmod.MIGRATIONS)  # second run: nothing to do
        tables = {r["name"] for r in await d.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"meta", "jobs", "privacy_optout", "lfg_posts", "lfg_members"} <= tables
        await d.close()
    run(go())


def test_transaction_rolls_back_on_error():
    async def go():
        d = await fresh()
        with pytest.raises(RuntimeError):
            async with d.transaction() as tx:
                await tx.execute("INSERT INTO meta VALUES ('k', 'v')")
                raise RuntimeError("boom")
        assert await d.fetchone("SELECT * FROM meta") is None
        await d.close()
    run(go())


def test_concurrent_transactions_do_not_interleave():
    async def go():
        d = await fresh()
        await d.execute("INSERT INTO meta VALUES ('n', '0')")

        async def bump():
            async with d.transaction() as tx:
                (n,) = await tx.fetchone("SELECT CAST(value AS INTEGER) FROM meta WHERE key='n'")
                await asyncio.sleep(0)  # yield mid-transaction
                await tx.execute("UPDATE meta SET value = ? WHERE key='n'", (str(n + 1),))

        await asyncio.gather(*(bump() for _ in range(20)))
        assert (await d.fetchone("SELECT value FROM meta WHERE key='n'"))["value"] == "20"
        await d.close()
    run(go())


def test_privacy_gate():
    async def go():
        d = await fresh()
        assert await d.tracking_allowed(42)
        await d.execute("INSERT INTO privacy_optout VALUES (42, 0)")
        assert not await d.tracking_allowed(42)
        assert await d.tracking_allowed(7)
        await d.close()
    run(go())
