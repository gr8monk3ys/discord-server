import asyncio

import pytest

import db as dbmod
import economy as E


def run(coro):
    return asyncio.run(coro)


async def fresh():
    d = dbmod.Database(":memory:")
    await d.connect()
    await d.migrate()
    return d


def test_credit_debit_and_balance():
    async def go():
        d = await fresh()
        assert (await E.apply(d, 1, 100, "daily", 0)).balance == 100
        assert (await E.apply(d, 1, -30, "coinflip", 0)).balance == 70
        assert await E.balance(d, 1) == 70
        assert await E.balance(d, 2) == 0
        await d.close()
    run(go())


def test_cannot_go_negative():
    async def go():
        d = await fresh()
        await E.apply(d, 1, 10, "daily", 0)
        r = await E.apply(d, 1, -11, "coinflip", 0)
        assert r.status is E.Status.INSUFFICIENT and r.balance == 10
        assert await E.balance(d, 1) == 10
        assert len(await d.fetchall("SELECT * FROM ledger")) == 1
        await d.close()
    run(go())


def test_ref_pays_once():
    async def go():
        d = await fresh()
        assert (await E.apply(d, 1, 250, "mvp", 0, ref="mvp:2026-W40:1")).ok
        r = await E.apply(d, 1, 250, "mvp", 0, ref="mvp:2026-W40:1")
        assert r.status is E.Status.DUPLICATE and await E.balance(d, 1) == 250
        await d.close()
    run(go())


def test_transfer_is_all_or_nothing():
    async def go():
        d = await fresh()
        await E.apply(d, 1, 50, "daily", 0)
        assert (await E.transfer(d, 1, 2, 60, "give", 0)).status is E.Status.INSUFFICIENT
        assert (await E.balance(d, 1), await E.balance(d, 2)) == (50, 0)
        assert (await E.transfer(d, 1, 2, 20, "give", 0)).ok
        assert (await E.balance(d, 1), await E.balance(d, 2)) == (30, 20)
        with pytest.raises(ValueError):
            await E.transfer(d, 1, 1, 5, "give", 0)
        with pytest.raises(ValueError):
            await E.transfer(d, 1, 2, 0, "give", 0)
        await d.close()
    run(go())


def test_concurrent_spends_never_overdraw():
    async def go():
        d = await fresh()
        await E.apply(d, 1, 100, "daily", 0)
        results = await asyncio.gather(*(E.apply(d, 1, -30, "slots", 0) for _ in range(5)))
        assert sum(r.ok for r in results) == 3 and await E.balance(d, 1) == 10
        await d.close()
    run(go())


def test_earned_for_daily_caps():
    async def go():
        d = await fresh()
        for t in (10, 20, 30):
            await E.apply(d, 1, 1, "message", t)
        async with d.transaction() as tx:
            assert await E.earned_tx(tx, 1, "message", 0, 25) == 2
            assert await E.earned_tx(tx, 1, "message", 0, 100) == 3
            assert await E.earned_tx(tx, 1, "daily", 0, 100) == 0
        await d.close()
    run(go())
