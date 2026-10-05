"""Module 11: Operations autopilot.

- Daily SQLite online backup at 04:00 local into config.BACKUP_DIR (falls back to
  bot/data/backups when D: is missing or read-only), keeping the newest 14, each one
  checked with PRAGMA integrity_check before it replaces anything.
- Health: a root logging handler counts ERROR records per logger; more than 5 in
  10 minutes posts one alert to the mod log, at most once per hour per logger. After
  more than 30 minutes down, a "back online" note (at most once per 6 h).
- Weekly config drift check, Sundays 03:00: the live roles and channels, built with
  server/snapshot_lib.py the way snapshot_server.py builds them, compared with the
  JSON in server/snapshot/. Read only: nothing is written into the repo.
- /status for staff: uptime, latency, database size, last backup, modules, errors.
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import snapshot_lib  # server/ is on sys.path via config
import style
from cogs.lfg import ping_only
from logic import community as community_rules
from logic import ops as O
from logic.schedule import Weekly, plan

log = logging.getLogger(__name__)

BACKUP_JOB = O.Daily("dbbackup", hour=4, minute=0)  # daily 04:00 local
DRIFT_JOB = Weekly("drift", weekday=6, hour=3, minute=0)  # Sundays 03:00 local
FALLBACK_DIR = config.DATA_DIR / "backups"
SNAPSHOT_DIR = config.SERVER_DIR / "snapshot"
NO_PINGS = ping_only()
ALERT_META = "ops:alert:"  # + logger name -> unix time of the last alert
ONLINE_META = "ops:online_note"
BACKUP_META = "ops:last_backup"
BACKUP_FAIL_META = "ops:backup_fail_note"  # local date of the last failure note


def now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- backup (runs in a thread)
def choose_backup_dir(primary: str | Path, fallback: Path) -> tuple[Path, str | None]:
    """The primary dir if it can be created and written, else the fallback (and why)."""
    try:
        p = Path(primary)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return p, None
    except OSError as e:
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback, f"{type(e).__name__}: {e}"


def make_backup(src: Path, dest_dir: Path, name: str, keep: int = O.KEEP_BACKUPS) -> dict:
    """Online backup of `src` to dest_dir/name via a temp file; verified before it replaces
    anything, then older backups beyond `keep` are deleted. Blocking: call in a thread."""
    final = dest_dir / name
    tmp = dest_dir / (name + ".tmp")
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(str(src), timeout=30)
    try:
        dest = sqlite3.connect(str(tmp))
        try:
            source.backup(dest)
            # A standalone file: no -wal/-shm siblings needed to read it.
            dest.execute("PRAGMA journal_mode = DELETE")
        finally:
            dest.close()
    finally:
        source.close()
    check = sqlite3.connect(str(tmp))
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if not result or result[0] != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed integrity_check: {result[0] if result else 'no result'}")
    os.replace(tmp, final)
    pruned = []
    for old in O.backups_to_prune(os.listdir(dest_dir), keep=keep):
        try:
            (dest_dir / old).unlink()
            pruned.append(old)
        except OSError:
            log.warning("couldn't delete old backup %s", old, exc_info=True)
    return {"path": str(final), "size": final.stat().st_size, "pruned": pruned}


def read_snapshot(directory: Path) -> dict:
    out = {}
    for name in ("roles", "channels"):
        out[name] = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
    return out


# ---------------------------------------------------------------- health handler
class ErrorCounter(logging.Handler):
    """Root handler: hands ERROR+ records to the cog. Never raises."""

    def __init__(self, cog: "Ops"):
        super().__init__(level=logging.ERROR)
        self.cog = cog

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.cog.on_error_record(record)
        except Exception:
            pass  # logging must never fail the code that logged


# ---------------------------------------------------------------- the cog
class Ops(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.started_at = now()
        self.monitor = O.ErrorMonitor()
        self.lock = threading.Lock()  # records may arrive from any thread
        self.handler: ErrorCounter | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pending_online: int | None = None  # seconds down, waiting to be posted
        self.tasks: set[asyncio.Task] = set()

    async def cog_load(self) -> None:
        self.loop = asyncio.get_running_loop()
        # cog_load runs in setup_hook, before stats' heartbeat loop (which waits for
        # ready) overwrites the last heartbeat.
        try:
            await self.check_downtime()
        except Exception:
            log.exception("ops: downtime check failed")
        try:
            rows = await self.db.fetchall("SELECT key, value FROM meta WHERE key LIKE ?", (ALERT_META + "%",))
            with self.lock:
                for r in rows:
                    self.monitor.last_alert[r["key"][len(ALERT_META):]] = int(r["value"])
        except Exception:
            log.exception("ops: loading alert history failed")
        self.handler = ErrorCounter(self)
        logging.getLogger().addHandler(self.handler)
        self.tick.start()

    async def cog_unload(self) -> None:
        if self.handler is not None:
            logging.getLogger().removeHandler(self.handler)
            self.handler = None
        self.tick.cancel()
        for task in list(self.tasks):
            task.cancel()

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    def mod_log_channel(self):
        guild = self.guild()
        return config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL) if guild else None

    async def meta(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    async def post(self, embed: discord.Embed) -> bool:
        """One embed in the mod log, no pings. False if there's no mod log."""
        channel = self.mod_log_channel()
        if channel is None:
            log.info("ops: no %s channel, nothing posted", config.MOD_LOG_CHANNEL)
            return False
        await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        return True

    # ------------------------------------------------------------ health: errors
    def on_error_record(self, record: logging.LogRecord) -> None:
        t = now()
        with self.lock:
            alert = self.monitor.record(record.name, t)
            count = self.monitor.count(record.name, t)
        if not alert or self.loop is None or self.loop.is_closed():
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        line = O.first_line(message)
        self.loop.call_soon_threadsafe(self._spawn_alert, record.name, count, line, t)

    def _spawn_alert(self, name: str, count: int, line: str, t: int) -> None:
        task = asyncio.ensure_future(self.post_alert(name, count, line, t))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def post_alert(self, name: str, count: int, line: str, t: int) -> None:
        # Failures here log at WARNING: an ERROR would feed the counter it reports on.
        try:
            await self.set_meta(ALERT_META + name, str(t))
            embed = style.embed(
                title="Front Desk is hitting errors",
                description=(f"`{name.replace('`', '')}`: {count} errors in the last "
                             f"{O.WINDOW // 60} minutes.\nLatest: {discord.utils.escape_markdown(line)}"),
                footer=style.label("health", "details in bot.log"),
                color=style.MUTED,
            )
            await self.post(embed)
        except Exception:
            log.warning("ops: couldn't post the error alert for %s", name, exc_info=True)

    # ------------------------------------------------------------ health: back online
    async def check_downtime(self) -> None:
        hb, last = await self.meta("heartbeat"), await self.meta(ONLINE_META)
        heartbeat = int(hb) if hb else None
        t = now()
        if O.should_note_online(heartbeat, t, int(last) if last else None):
            self.pending_online = t - heartbeat

    async def post_online_note(self) -> None:
        if self.pending_online is None:
            return
        down = self.pending_online
        embed = style.embed(description=f"Front Desk is back online after about {O.fmt_uptime(down)} away.",
                            footer=style.label("health"))
        # Kept pending on a Discord error so the next tick retries.
        await self.post(embed)
        self.pending_online = None
        await self.set_meta(ONLINE_META, str(now()))

    # ------------------------------------------------------------ scheduled jobs
    @tasks.loop(minutes=5)
    async def tick(self) -> None:
        for step in (self.post_online_note, self.run_backup_job, self.run_drift_job):
            try:
                await step()
            except Exception:
                log.exception("ops: %s failed", step.__name__)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def job_state(self, name: str) -> tuple[int | None, set[str], int]:
        seen_key = f"first_seen:{name}"
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (seen_key,))
        first_seen = int(row["value"]) if row else None
        done = {r["key"] for r in await self.db.fetchall(
            "SELECT key FROM jobs WHERE substr(key, 1, ?) = ?", (len(name) + 1, f"{name}:"))}
        t = now()
        if first_seen is None:
            await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
        return first_seen, done, t

    async def mark_done(self, keys) -> None:
        t = now()
        async with self.db.transaction() as tx:
            for key in keys:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (key, t))

    async def run_backup_job(self) -> None:
        first_seen, done, t = await self.job_state(BACKUP_JOB.name)
        todo = O.plan_daily(BACKUP_JOB, t, self.tz, done, first_seen)
        await self.mark_done(p.key for p in todo.mark_done)
        if todo.run is None:
            return
        try:
            await self.backup(todo.run.local_date)
        except Exception:
            await self.note_backup_failure(todo.run.local_date.isoformat())
            raise
        await self.mark_done([todo.run.key])  # only after success: a failure retries next tick

    async def backup(self, local_date) -> dict:
        src = self.db.path
        if not isinstance(src, Path) and str(src) == ":memory:":
            raise RuntimeError("in-memory database can't be backed up")
        dest_dir, why = await asyncio.to_thread(choose_backup_dir, config.BACKUP_DIR, FALLBACK_DIR)
        if why:
            log.warning("backup dir %s unusable (%s); using %s", config.BACKUP_DIR, why, dest_dir)
        info = await asyncio.to_thread(make_backup, Path(src), dest_dir, O.backup_name(local_date))
        info.update(at=now(), fallback=bool(why))
        await self.set_meta(BACKUP_META, json.dumps(info))
        log.info("backup ok: %s (%s), pruned %d", info["path"], O.fmt_bytes(info["size"]), len(info["pruned"]))
        return info

    async def note_backup_failure(self, day: str) -> None:
        """One mod-log line per day when backups fail (retries every tick would never
        reach the error alert's 5-in-10-minutes threshold)."""
        try:
            if await self.meta(BACKUP_FAIL_META) == day:
                return
            if await self.post(style.embed(description="Tonight's database backup failed. Details in bot.log; "
                                                       "it retries every 5 minutes.",
                                           footer=style.label("backup", day), color=style.MUTED)):
                await self.set_meta(BACKUP_FAIL_META, day)
        except Exception:
            log.warning("ops: couldn't post the backup failure note", exc_info=True)

    # ------------------------------------------------------------ weekly drift check
    async def run_drift_job(self) -> None:
        first_seen, done, t = await self.job_state(DRIFT_JOB.name)
        todo = plan(DRIFT_JOB, t, self.tz, done, first_seen)
        await self.mark_done(p.key for p in todo.mark_done)
        if todo.run is None:
            return
        await self.post_drift(todo.run.key)
        await self.mark_done([todo.run.key])

    @staticmethod
    async def member_names(guild) -> dict:
        """Names for members with their own overwrites that discord.py couldn't resolve,
        as snapshot_server.member_names_for_overwrites does."""
        ids = set()
        for ch in guild.channels:
            for target in ch.overwrites:
                if isinstance(target, discord.Object) and target.type in (discord.Member, discord.User):
                    ids.add(target.id)
        names = {}
        for member_id in sorted(ids):
            try:
                names[member_id] = (await guild.fetch_member(member_id)).name
            except discord.HTTPException:
                pass
        return names

    async def live_snapshot(self, guild) -> dict:
        """roles + channels exactly as snapshot_server.snapshot() builds them."""
        member_names = await self.member_names(guild)
        names = {r.id: r.name for r in guild.roles}
        names.update({c.id: c.name for c in guild.channels})
        files = {
            "roles": snapshot_lib.roles_list(guild.roles),
            "channels": snapshot_lib.channels_snapshot(guild.channels, member_names),
        }
        return snapshot_lib.scrub(files, names)

    async def post_drift(self, key: str) -> None:
        guild = self.guild()
        if guild is None:
            raise RuntimeError("guild not available for the drift check")
        week = key.split(":", 1)[1]
        try:
            committed = await asyncio.to_thread(read_snapshot, SNAPSHOT_DIR)
        except (OSError, ValueError) as e:
            log.warning("drift check: can't read %s: %s", SNAPSHOT_DIR, e)
            await self.post(style.embed(
                title="Weekly config check",
                description="Skipped: the saved snapshot in server/snapshot/ couldn't be read.",
                footer=style.label("drift", week), color=style.MUTED))
            return
        live = await self.live_snapshot(guild)
        roles = O.diff_named(committed["roles"], live["roles"])
        channels = O.diff_named(list(O.flatten_channels(committed["channels"]).values()),
                                list(O.flatten_channels(live["channels"]).values()))
        text = O.drift_summary(roles, channels)
        if not text:
            text = "No changes since the saved snapshot."
        else:
            text = ("Changes since the saved snapshot. If they're intended, run "
                    "`python server/snapshot_server.py` and commit.\n\n" + text)
        await self.post(style.embed(title="Weekly config check", description=text,
                                    footer=style.label("drift", week)))
        log.info("drift check %s: roles %s, channels %s", key, bool(roles), bool(channels))

    # ------------------------------------------------------------ /status
    @staticmethod
    def is_staff(user) -> bool:
        guild = getattr(user, "guild", None)
        if guild is None:
            return False
        return community_rules.can_handle(guild.owner_id == user.id, user.guild_permissions.administrator,
                                          user.roles)

    def db_size(self) -> int:
        path = self.db.path
        if not isinstance(path, Path):
            return 0
        total = 0
        for p in (path, path.with_name(path.name + "-wal")):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    async def last_backup_text(self) -> str:
        raw = await self.meta(BACKUP_META)
        if not raw:
            return "none yet"
        try:
            info = json.loads(raw)
        except ValueError:
            return "unknown"
        where = " (fallback folder)" if info.get("fallback") else ""
        return f"<t:{int(info['at'])}:R>, {O.fmt_bytes(int(info['size']))}{where}"

    @app_commands.command(name="status", description="Front Desk health: uptime, backups, errors (staff only)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def status(self, interaction: discord.Interaction) -> None:
        if not self.is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use /status.", ephemeral=True)
            return
        t = now()
        latency = getattr(self.bot, "latency", float("nan"))
        latency_text = f"{latency * 1000:.0f} ms" if latency == latency and latency != float("inf") else "n/a"
        modules = sorted(name.removeprefix("cogs.") for name in getattr(self.bot, "extensions", {}))
        with self.lock:
            recent = self.monitor.counts(t)
            totals = dict(self.monitor.totals)
        if totals:
            rows = [f"`{n.replace('`', '')}` {recent.get(n, 0)} / {c}"
                    for n, c in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]
            errors = "last 10 min / since start\n" + "\n".join(rows)
        else:
            errors = "none since start"
        embed = style.embed(title="Front Desk status", footer=style.label("status"))
        embed.add_field(name="Uptime", value=O.fmt_uptime(t - self.started_at))
        embed.add_field(name="Latency", value=latency_text)
        embed.add_field(name="Database", value=O.fmt_bytes(self.db_size()))
        embed.add_field(name="Last backup", value=await self.last_backup_text(), inline=False)
        embed.add_field(name=f"Modules ({len(modules)})", value=", ".join(modules) or "none", inline=False)
        embed.add_field(name="Errors", value=errors[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Ops(bot))
