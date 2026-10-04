"""Offline tests for cogs.ops.Ops: a real SQLite file database plus fakes for the bot,
guild and channels. No network. Time is controlled by patching cogs.ops.now."""

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

import config
import db as dbmod
import snapshot_lib
from cogs import ops as cogmod
from cogs.ops import Ops
from logic import ops as O

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, A, MOD, KEEPER = 1, 11, 20, 21
MIN = 60
HOUR = 60 * MIN
FAKE_TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4OQ" + ".GhAbCd." + "abcdefghijklmnopqrstuvwxyz0123456789AB"


def local(y, m, d, hh=0, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp())


T0 = local(2026, 10, 4, 12)  # a Sunday


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- fakes
class Named:
    _next = 5000

    def __init__(self, name, position=1, perms=None, color=0):
        Named._next += 1
        self.id = Named._next
        self.name = name
        self.position = position
        self.color = discord.Colour(color)
        self.hoist = False
        self.mentionable = False
        self.managed = False
        self.permissions = perms or discord.Permissions.none()


class FakeChan:
    _next = 7000

    def __init__(self, name, kind=discord.ChannelType.text, category=None, position=0, topic=None):
        FakeChan._next += 1
        self.id = FakeChan._next
        self.name = name
        self.type = kind
        self.category = category
        self.position = position
        self.topic = topic
        self.overwrites = {}
        self.permissions_synced = True
        self.nsfw = False
        self.slowmode_delay = 0


class FakeText:
    def __init__(self, name):
        self.id = 300
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.mod_log = FakeText(config.MOD_LOG_CHANNEL)
        self.text_channels = [self.mod_log]
        self.roles = [Named("@everyone", 0), Named(config.MOD_ROLE, 2), Named(config.KEEPER_ROLE, 3,
                                                                              discord.Permissions(administrator=True))]
        info = FakeChan("info", discord.ChannelType.category, position=0)
        chat = FakeChan("chat", discord.ChannelType.category, position=1)
        self.channels = [info, chat, FakeChan("rules", category=info, topic="read me"),
                         FakeChan("general", category=chat, topic="hi"),
                         FakeChan("Lobby", discord.ChannelType.voice, category=chat)]
        for ch in self.channels:
            if ch.type is discord.ChannelType.voice:
                ch.user_limit, ch.bitrate = 0, 64000

    def role(self, name):
        return config.match_by_name(self.roles, name)

    def channel(self, name):
        return next(c for c in self.channels if c.name == name)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.latency = 0.042
        self.extensions = {"cogs.lfg": None, "cogs.stats": None, "cogs.ops": None}
        self.ready = asyncio.Event()  # never set: the tick loop stays parked

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None

    async def wait_until_ready(self):
        await self.ready.wait()


class FakeMember:
    def __init__(self, uid, guild, roles=(), admin=False):
        self.id = uid
        self.guild = guild
        self.roles = [guild.role("@everyone")] + [guild.role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=admin)


class FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.calls = []
        self.response = SimpleNamespace(send_message=self._send)

    async def _send(self, content=None, **kwargs):
        self.calls.append(dict(content=content, **kwargs))


class Env:
    def __init__(self, db, guild, bot, tmp):
        self.db, self.guild, self.bot, self.tmp = db, guild, bot, tmp
        self.t = T0
        self.cog = Ops(bot)

    async def restart(self):
        await self.cog.cog_unload()
        self.cog = Ops(self.bot)
        await self.cog.cog_load()
        return self.cog

    async def meta(self, key):
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def jobs(self):
        return {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs")}

    def posts(self):
        return [s["embed"] for s in self.guild.mod_log.sent]


def with_env(fn, monkeypatch, tmp_path):
    async def go():
        db = dbmod.Database(tmp_path / "front_desk.db")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        env = Env(db, guild, bot, tmp_path)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        env.cog = Ops(bot)  # started_at uses the patched clock
        monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
        monkeypatch.setattr(cogmod, "FALLBACK_DIR", tmp_path / "fallback")
        monkeypatch.setattr(cogmod, "SNAPSHOT_DIR", tmp_path / "snapshot")
        try:
            await fn(env)
        finally:
            await env.cog.cog_unload()
            await db.close()
    run(go())


async def settle(env):
    """Let call_soon_threadsafe callbacks run, then wait for the alert tasks they started."""
    for _ in range(3):
        await asyncio.sleep(0)
    while env.cog.tasks:
        await asyncio.gather(*list(env.cog.tasks))


def no_pings(post):
    am = post["allowed_mentions"]
    return am.everyone is False and am.roles is False and am.users is False


# ---------------------------------------------------------------- load / unload
def test_cog_load_installs_root_handler_and_unload_removes(monkeypatch, tmp_path):
    async def go(env):
        await env.cog.cog_load()
        assert env.cog.handler in logging.getLogger().handlers
        assert env.cog.tick.is_running()
        await env.cog.cog_unload()
        assert all(not isinstance(h, cogmod.ErrorCounter) for h in logging.getLogger().handlers)
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- health: error alerts
def test_more_than_five_errors_posts_one_redacted_alert(monkeypatch, tmp_path):
    async def go(env):
        await env.cog.cog_load()
        bad = logging.getLogger("cogs.fake")
        for i in range(5):
            bad.error("lfg post failed %d", i)
        await settle(env)
        assert env.posts() == []
        bad.error("login with %s failed\nsecond line", FAKE_TOKEN)
        await settle(env)
        (post,) = env.guild.mod_log.sent
        text = post["embed"].description
        assert "cogs.fake" in text and "6 errors" in text and "[redacted]" in text
        assert FAKE_TOKEN not in text and "second line" not in text
        assert no_pings(post)
        for _ in range(5):
            bad.error("still failing")
        logging.getLogger("cogs.fake").warning("only a warning")
        await settle(env)
        assert len(env.posts()) == 1  # once per hour per logger
        assert await env.meta("ops:alert:cogs.fake") == str(env.t)
    with_env(go, monkeypatch, tmp_path)


def test_alert_cooldown_survives_restart_and_expires(monkeypatch, tmp_path):
    async def go(env):
        await env.cog.cog_load()
        bad = logging.getLogger("cogs.fake2")
        for _ in range(6):
            bad.error("x")
        await settle(env)
        assert len(env.posts()) == 1
        env.t += 30 * MIN
        await env.restart()
        for _ in range(6):
            bad.error("x")
        await settle(env)
        assert len(env.posts()) == 1
        env.t += 31 * MIN
        for _ in range(6):
            bad.error("x")
        await settle(env)
        assert len(env.posts()) == 2
    with_env(go, monkeypatch, tmp_path)


def test_alert_post_failure_does_not_raise_or_log_error(monkeypatch, tmp_path, caplog):
    async def go(env):
        await env.cog.cog_load()
        env.guild.mod_log.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
        for _ in range(6):
            logging.getLogger("cogs.fake3").error("x")
        await settle(env)
        ops_records = [r for r in caplog.records if r.name == "cogs.ops"]
        assert ops_records and all(r.levelno == logging.WARNING for r in ops_records)
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- health: back online
@pytest.mark.parametrize("gap,expected", [(31 * MIN, True), (10 * MIN, False), (None, False)])
def test_back_online_note(monkeypatch, tmp_path, gap, expected):
    async def go(env):
        if gap is not None:
            await env.db.execute("INSERT INTO meta (key, value) VALUES ('heartbeat', ?)", (str(env.t - gap),))
        await env.cog.cog_load()
        await env.cog.post_online_note()
        posts = env.posts()
        assert bool(posts) is expected
        if expected:
            assert "back online" in posts[0].description and "31m" in posts[0].description
            assert await env.meta("ops:online_note") == str(env.t)
    with_env(go, monkeypatch, tmp_path)


def test_back_online_at_most_every_six_hours(monkeypatch, tmp_path):
    async def go(env):
        await env.db.execute("INSERT INTO meta (key, value) VALUES ('heartbeat', ?)", (str(env.t - HOUR),))
        await env.cog.cog_load()
        await env.cog.post_online_note()
        env.t += 2 * HOUR  # down again (heartbeat still old)
        await env.restart()
        await env.cog.post_online_note()
        assert len(env.posts()) == 1
        env.t += 5 * HOUR
        await env.restart()
        await env.cog.post_online_note()
        assert len(env.posts()) == 2
    with_env(go, monkeypatch, tmp_path)


def test_back_online_retries_after_discord_error(monkeypatch, tmp_path):
    async def go(env):
        await env.db.execute("INSERT INTO meta (key, value) VALUES ('heartbeat', ?)", (str(env.t - HOUR),))
        await env.cog.cog_load()
        env.guild.mod_log.fail = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
        await env.cog.tick.coro(env.cog)  # never raises
        assert env.cog.pending_online is not None
        env.guild.mod_log.fail = None
        await env.cog.post_online_note()
        assert len(env.posts()) == 1 and env.cog.pending_online is None
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- backups
def test_backup_job_first_run_marks_then_backs_up_next_day(monkeypatch, tmp_path):
    async def go(env):
        await env.db.execute("INSERT INTO wallets (user_id, balance) VALUES (?, ?)", (A, 123))
        await env.cog.run_backup_job()
        assert await env.jobs() == {"dbbackup:2026-10-04"}
        assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").iterdir())
        env.t = local(2026, 10, 5, 4, 3)
        await env.cog.run_backup_job()
        path = tmp_path / "backups" / "front_desk-2026-10-05.db"
        assert path.exists() and "dbbackup:2026-10-05" in await env.jobs()
        con = sqlite3.connect(path)
        try:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert con.execute("SELECT balance FROM wallets WHERE user_id = ?", (A,)).fetchone()[0] == 123
        finally:
            con.close()
        info = json.loads(await env.meta("ops:last_backup"))
        assert info["path"] == str(path) and info["fallback"] is False and info["at"] == env.t
        assert not list((tmp_path / "backups").glob("*.tmp"))
        await env.cog.run_backup_job()  # same day again: nothing new
        assert len(list((tmp_path / "backups").iterdir())) == 1
    with_env(go, monkeypatch, tmp_path)


def test_backup_keeps_newest_fourteen(monkeypatch, tmp_path):
    async def go(env):
        folder = tmp_path / "backups"
        folder.mkdir()
        for d in range(1, 21):
            (folder / f"front_desk-2026-09-{d:02}.db").write_bytes(b"old")
        (folder / "keep-me.txt").write_text("not a backup")
        await env.cog.backup(datetime(2026, 10, 5).date())
        names = sorted(p.name for p in folder.iterdir())
        backups = [n for n in names if n.startswith("front_desk-")]
        assert len(backups) == 14 and "front_desk-2026-10-05.db" in backups
        assert "front_desk-2026-09-07.db" not in backups and "front_desk-2026-09-08.db" in backups
        assert "keep-me.txt" in names
    with_env(go, monkeypatch, tmp_path)


def test_backup_falls_back_when_primary_unusable(monkeypatch, tmp_path, caplog):
    async def go(env):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("file")
        monkeypatch.setattr(config, "BACKUP_DIR", str(blocker / "front-desk"))
        info = await env.cog.backup(datetime(2026, 10, 5).date())
        assert (tmp_path / "fallback" / "front_desk-2026-10-05.db").exists() and info["fallback"] is True
        assert any("unusable" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    with_env(go, monkeypatch, tmp_path)


def test_backup_failure_retries_and_notes_once_per_day(monkeypatch, tmp_path):
    async def go(env):
        await env.cog.run_backup_job()  # first sighting
        env.t = local(2026, 10, 5, 4, 5)

        def boom(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")
        monkeypatch.setattr(cogmod, "make_backup", boom)
        await env.cog.tick.coro(env.cog)  # logs, never raises
        env.t += 5 * MIN
        await env.cog.tick.coro(env.cog)
        assert "dbbackup:2026-10-05" not in await env.jobs()
        assert len(env.posts()) == 1 and "backup failed" in env.posts()[0].description
        monkeypatch.setattr(cogmod, "make_backup", real_make_backup)
        env.t += 5 * MIN
        await env.cog.run_backup_job()
        assert "dbbackup:2026-10-05" in await env.jobs()
    with_env(go, monkeypatch, tmp_path)


real_make_backup = cogmod.make_backup


def test_make_backup_rejects_corrupt_copy(monkeypatch, tmp_path):
    src = tmp_path / "src.db"
    con = sqlite3.connect(src)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    dest = tmp_path / "out"
    dest.mkdir()
    real_connect = sqlite3.connect

    class Checker:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *a):
            if "integrity_check" in sql:
                return SimpleNamespace(fetchone=lambda: ("*** in database main ***",))
            return self.conn.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    calls = {"n": 0}

    def connect(path, *a, **k):
        calls["n"] += 1
        conn = real_connect(path, *a, **k)
        return Checker(conn) if calls["n"] == 3 else conn  # the verification connection
    monkeypatch.setattr(cogmod.sqlite3, "connect", connect)
    with pytest.raises(RuntimeError):
        cogmod.make_backup(src, dest, "front_desk-2026-10-05.db")
    assert list(dest.iterdir()) == []


# ---------------------------------------------------------------- drift
async def save_snapshot(env):
    files = await env.cog.live_snapshot(env.guild)
    folder = env.tmp / "snapshot"
    folder.mkdir(exist_ok=True)
    for name, data in files.items():
        (folder / f"{name}.json").write_text(snapshot_lib.dumps(data), encoding="utf-8")
    return folder


def test_drift_reports_changes_on_sunday(monkeypatch, tmp_path):
    async def go(env):
        folder = await save_snapshot(env)
        before = {p.name: p.read_bytes() for p in folder.iterdir()}
        env.t = local(2026, 10, 3, 12)  # Saturday: first sighting marks last week's run
        await env.cog.run_drift_job()
        assert env.posts() == []
        env.guild.roles.append(Named("Raider @everyone", 1))
        env.guild.channel("general").topic = "changed"
        env.guild.channels.remove(env.guild.channel("rules"))
        env.guild.role(config.MOD_ROLE).permissions = discord.Permissions(kick_members=True)
        env.t = local(2026, 10, 4, 3, 2)
        await env.cog.run_drift_job()
        (post,) = env.guild.mod_log.sent
        text = post["embed"].description
        assert "+ Raider @​everyone" in text
        assert f"~ {config.MOD_ROLE} (permissions)" in text
        assert "~ general (topic)" in text and "- rules" in text
        assert no_pings(post)
        assert {p.name: p.read_bytes() for p in folder.iterdir()} == before  # repo untouched
        await env.cog.run_drift_job()
        assert len(env.posts()) == 1
    with_env(go, monkeypatch, tmp_path)


def test_drift_no_changes(monkeypatch, tmp_path):
    async def go(env):
        await save_snapshot(env)
        await env.cog.post_drift("drift:2026-W40")
        assert "No changes" in env.posts()[0].description
    with_env(go, monkeypatch, tmp_path)


def test_drift_missing_snapshot_is_reported_not_raised(monkeypatch, tmp_path):
    async def go(env):
        await env.cog.post_drift("drift:2026-W40")
        assert "Skipped" in env.posts()[0].description
    with_env(go, monkeypatch, tmp_path)


def test_live_snapshot_matches_snapshot_lib_shape(monkeypatch, tmp_path):
    async def go(env):
        files = await env.cog.live_snapshot(env.guild)
        assert set(files) == {"roles", "channels"}
        assert [r["name"] for r in files["roles"]] == [config.KEEPER_ROLE, config.MOD_ROLE, "@everyone"]
        cats = files["channels"]["categories"]
        assert [c["name"] for c in cats] == ["info", "chat"]
        assert [c["name"] for c in cats[1]["channels"]] == ["general", "Lobby"]
        assert snapshot_lib.find_leaks(files) == []
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- /status
def test_status_staff_only_and_ephemeral(monkeypatch, tmp_path):
    async def go(env):
        await env.cog.cog_load()
        user = FakeInteraction(FakeMember(A, env.guild))
        await Ops.status.callback(env.cog, user)
        assert user.calls == [dict(content="Only staff can use /status.", ephemeral=True)]

        logging.getLogger("cogs.fake4").error("x")
        await env.db.execute("INSERT INTO meta (key, value) VALUES ('ops:last_backup', ?)",
                             (json.dumps({"at": env.t - HOUR, "size": 2048, "path": "p", "fallback": False}),))
        env.t += 2 * HOUR
        for uid, kw in ((MOD, {"roles": [config.MOD_ROLE]}), (KEEPER, {"roles": [config.KEEPER_ROLE]}),
                        (OWNER, {})):
            inter = FakeInteraction(FakeMember(uid, env.guild, **kw))
            await Ops.status.callback(env.cog, inter)
            (call,) = inter.calls
            assert call["ephemeral"] is True
            fields = {f.name: f.value for f in call["embed"].fields}
            assert fields["Uptime"] == "2h 0m" and fields["Latency"] == "42 ms"
            assert fields["Last backup"].startswith(f"<t:{T0 - HOUR}:R>") and "2.0 KB" in fields["Last backup"]
            assert fields["Modules (3)"] == "lfg, ops, stats"
            assert "`cogs.fake4` 0 / 1" in fields["Errors"]
            assert fields["Database"].endswith("KB") or fields["Database"].endswith("MB")
    with_env(go, monkeypatch, tmp_path)


def test_status_without_errors_or_backups(monkeypatch, tmp_path):
    async def go(env):
        env.bot.latency = float("nan")
        inter = FakeInteraction(FakeMember(OWNER, env.guild))
        await Ops.status.callback(env.cog, inter)
        fields = {f.name: f.value for f in inter.calls[0]["embed"].fields}
        assert fields["Latency"] == "n/a" and fields["Last backup"] == "none yet"
        assert fields["Errors"] == "none since start"
    with_env(go, monkeypatch, tmp_path)


def test_ops_job_names_do_not_collide_with_stats():
    assert {cogmod.BACKUP_JOB.name, cogmod.DRIFT_JOB.name}.isdisjoint({"mvp"})
    assert O.backup_name(datetime(2026, 1, 2).date()).startswith("front_desk-")
