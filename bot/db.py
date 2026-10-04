"""SQLite storage. One connection; writes that must happen together go through
`transaction()`, which also serialises them so concurrent button clicks can't
interleave."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

# Append only: never edit a migration that has shipped, add a new one.
# Each migration is a list of single SQL statements (no splitting on ";").
MIGRATIONS = [
    [
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE jobs (key TEXT PRIMARY KEY, done_at INTEGER NOT NULL)",
        "CREATE TABLE privacy_optout (user_id INTEGER PRIMARY KEY, at INTEGER NOT NULL)",
        """CREATE TABLE lfg_posts (
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
    )""",
        "CREATE INDEX lfg_posts_open ON lfg_posts (closed_at, host_id, game)",
        """CREATE TABLE lfg_members (
        post_id INTEGER NOT NULL REFERENCES lfg_posts (id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        joined_at INTEGER NOT NULL,
        PRIMARY KEY (post_id, user_id)
    )""",
    ],
    [
        # Backstop for "one open post per host per game".
        "CREATE UNIQUE INDEX lfg_one_open_per_game ON lfg_posts (host_id, game) WHERE closed_at IS NULL",
    ],
    [
        # Module 2: stats. Counts and durations only, never message text.
        """CREATE TABLE voice_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        start INTEGER NOT NULL,
        "end" INTEGER
    )""",
        'CREATE INDEX voice_sessions_open ON voice_sessions (user_id, "end")',
        'CREATE INDEX voice_sessions_time ON voice_sessions (start, "end")',
        """CREATE TABLE game_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        game TEXT NOT NULL,
        start INTEGER NOT NULL,
        "end" INTEGER
    )""",
        'CREATE INDEX game_sessions_open ON game_sessions (user_id, "end")',
        'CREATE INDEX game_sessions_time ON game_sessions (start, "end")',
        """CREATE TABLE message_counts (
        user_id INTEGER NOT NULL,
        day TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    )""",
    ],
    [
        # Growth: invite tracking and Disboard bumps.
        """CREATE TABLE invite_uses (
        code TEXT PRIMARY KEY,
        inviter_id INTEGER,
        uses INTEGER NOT NULL DEFAULT 0
    )""",
        """CREATE TABLE joins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        joined_at INTEGER NOT NULL,
        inviter_id INTEGER,
        invite_code TEXT,
        left_at INTEGER
    )""",
        "CREATE INDEX joins_user ON joins (user_id, joined_at)",
        "CREATE INDEX joins_inviter ON joins (inviter_id, joined_at)",
        """CREATE TABLE bumps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        at INTEGER NOT NULL
    )""",
        # Community: welcomes and reports.
        "CREATE TABLE welcomed (user_id INTEGER PRIMARY KEY, at INTEGER NOT NULL)",
        """CREATE TABLE reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reporter_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        channel_id INTEGER,
        message_id INTEGER,
        reason TEXT NOT NULL,
        at INTEGER NOT NULL,
        log_message_id INTEGER,
        status TEXT NOT NULL DEFAULT 'open'
    )""",
    ],
    [
        # Module 6: hall of fame.
        """CREATE TABLE starboard (
        message_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL,
        board_message_id INTEGER,
        stars INTEGER NOT NULL DEFAULT 0,
        at INTEGER NOT NULL
    )""",
        # Module 3: clip of the week.
        """CREATE TABLE clips (
        message_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        url TEXT NOT NULL,
        posted_at INTEGER NOT NULL
    )""",
        "CREATE INDEX clips_time ON clips (posted_at)",
        """CREATE TABLE clip_polls (
        week TEXT PRIMARY KEY,
        message_id INTEGER NOT NULL,
        ends_at INTEGER NOT NULL,
        winner_id INTEGER,
        done INTEGER NOT NULL DEFAULT 0
    )""",
        # Module 5: join-to-create voice.
        """CREATE TABLE temp_voice (
        channel_id INTEGER PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    )""",
        # Module 7: game nights and free games.
        """CREATE TABLE gamenights (
        event_id INTEGER PRIMARY KEY,
        host_id INTEGER NOT NULL,
        game TEXT,
        starts_at INTEGER NOT NULL,
        reminded INTEGER NOT NULL DEFAULT 0
    )""",
        "CREATE TABLE free_games (id INTEGER PRIMARY KEY, posted_at INTEGER NOT NULL)",
    ],
    [
        # Module 4: economy. Every coin movement goes through bot/economy.py.
        """CREATE TABLE wallets (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
        daily_streak INTEGER NOT NULL DEFAULT 0,
        last_daily TEXT
    )""",
        """CREATE TABLE ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        reason TEXT NOT NULL,
        ref TEXT UNIQUE,
        at INTEGER NOT NULL
    )""",
        "CREATE INDEX ledger_user ON ledger (user_id, reason, at)",
        """CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id INTEGER,
        message_id INTEGER,
        creator_id INTEGER NOT NULL,
        question TEXT NOT NULL,
        option_a TEXT NOT NULL,
        option_b TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        winner TEXT,
        created_at INTEGER NOT NULL
    )""",
        """CREATE TABLE prediction_bets (
        prediction_id INTEGER NOT NULL REFERENCES predictions (id),
        user_id INTEGER NOT NULL,
        option TEXT NOT NULL,
        amount INTEGER NOT NULL,
        at INTEGER NOT NULL,
        PRIMARY KEY (prediction_id, user_id)
    )""",
        """CREATE TABLE blackjack_open (
        user_id INTEGER PRIMARY KEY,
        bet INTEGER NOT NULL,
        started_at INTEGER NOT NULL
    )""",
    ],
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
        for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
            async with self.transaction() as tx:
                for statement in statements:
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
