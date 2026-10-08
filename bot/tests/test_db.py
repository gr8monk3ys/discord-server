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



def test_hot_query_indexes_exist_and_are_used():
    async def go():
        d = await fresh()
        try:
            names = {r["name"] for r in await d.fetchall("SELECT name FROM sqlite_master WHERE type='index'")}
            assert {"ledger_reason_at", "ledger_at", "message_counts_day", "voice_sessions_end",
                    "game_sessions_end", "voice_sessions_channel"} <= names

            async def plan(sql):
                return " ".join(r["detail"] for r in await d.fetchall("EXPLAIN QUERY PLAN " + sql))
            assert "ledger_reason_at" in await plan("SELECT user_id FROM ledger WHERE reason = 'raffle' AND at >= 0")
            assert "message_counts_day" in await plan("SELECT user_id FROM message_counts WHERE day >= '2026-01-01'")
            assert "voice_sessions_end" in await plan('SELECT user_id FROM voice_sessions WHERE "end" IS NULL')
            assert "game_sessions_end" in await plan('SELECT user_id FROM game_sessions WHERE "end" IS NULL')
        finally:
            await d.close()
    run(go())


def test_file_database_uses_wal_with_normal_sync(tmp_path):
    async def go():
        d = dbmod.Database(tmp_path / "bot.sqlite3")
        await d.connect()
        try:
            assert (await d.fetchone("PRAGMA journal_mode"))[0] == "wal"
            assert (await d.fetchone("PRAGMA synchronous"))[0] == 1  # NORMAL
        finally:
            await d.close()
    run(go())
