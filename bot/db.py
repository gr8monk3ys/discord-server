"""SQLite storage. One connection; writes that must happen together go through
`transaction()`, which also serialises them so concurrent button clicks can't
interleave."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

# Append only: never edit a migration that has shipped, add a new one.
MIGRATIONS = [
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE jobs (key TEXT PRIMARY KEY, done_at INTEGER NOT NULL);
    CREATE TABLE privacy_optout (user_id INTEGER PRIMARY KEY, at INTEGER NOT NULL);
    CREATE TABLE lfg_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id INTEGER,
        message_id INTEGER,
        game TEXT NOT NULL,
        host_id INTEGER NOT NULL,
        size INTEGER NOT NULL,
        mode TEXT,
        when_text TEXT NOT NULL,
        note TEXT,
        created_at INTEGER NOT NULL,
        closed_at INTEGER
    );
    CREATE INDEX lfg_posts_open ON lfg_posts (closed_at, host_id, game);
    CREATE TABLE lfg_members (
        post_id INTEGER NOT NULL REFERENCES lfg_posts (id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        joined_at INTEGER NOT NULL,
        PRIMARY KEY (post_id, user_id)
    );
    """,
]


class Tx:
    """Statements inside a transaction (the lock is already held)."""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def execute(self, sql: str, params=()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def fetchone(self, sql: str, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params=()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()


class Database:
    def __init__(self, path: Path | str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self) -> None:
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, transactions are explicit.
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.execute("PRAGMA foreign_keys = ON")

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def migrate(self) -> int:
        """Apply pending migrations; returns the schema version."""
        async with self.conn.execute("PRAGMA user_version") as cur:
            (version,) = await cur.fetchone()
        for number, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            async with self.transaction() as tx:
                for statement in filter(str.strip, sql.split(";")):
                    await tx.execute(statement)
                await tx.execute(f"PRAGMA user_version = {number}")
            version = number
        return version

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield Tx(self.conn)
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    # Single statements, serialised with transactions.
    async def execute(self, sql: str, params=()) -> int:
        async with self.lock:
            cur = await self.conn.execute(sql, params)
            return cur.rowcount

    async def fetchone(self, sql: str, params=()):
        async with self.lock:
            return await Tx(self.conn).fetchone(sql, params)

    async def fetchall(self, sql: str, params=()):
        async with self.lock:
            return await Tx(self.conn).fetchall(sql, params)

    async def tracking_allowed(self, user_id: int) -> bool:
        """The one privacy gate: every stats/coins recorder checks this first."""
        row = await self.fetchone("SELECT 1 FROM privacy_optout WHERE user_id = ?", (user_id,))
        return row is None
