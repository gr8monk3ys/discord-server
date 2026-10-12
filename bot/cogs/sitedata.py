"""Live data for the landing page: every 30 minutes (and on /sitedata refresh, staff only) the
bot writes one small JSON file of server-wide aggregates to bot/data/site/stats.json.

The bot never publishes anything itself: server/publish_site_data.py, run by the owner's
scheduled task, checks the file again and pushes it to the public `site-data` branch, where
site/app.js reads it. What may be in the file, and the small-group rule, live in
logic/sitedata.py; the shape and the leak check in server/site_schema.py.

- Read-only: it reads the tables other modules keep and the guild cache, and writes one file
  (temp file, then os.replace, so the publisher never reads half of it).
- Members who opted out with /privacy are left out of every number that comes from a
  per-member table.
- A section whose source is missing (a table from a module that isn't merged yet, an intent
  that is off, a query that fails) is simply absent; one bad section never blocks the rest.
- SITE_DATA=off (server/.env) turns the job off; the command then says so.
- A document that fails the schema or the leak check is not written; the old file stays.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from logic import community as community_rules
from logic import matchmaking as M
from logic import seasonal as S
from logic import sitedata as SD

log = logging.getLogger(__name__)

OPTED_IN = "NOT IN (SELECT user_id FROM privacy_optout)"
ACTIVE_TOURNAMENTS = ("signup", "running")


def now() -> int:
    return int(time.time())


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    perms = getattr(user, "guild_permissions", None)
    return community_rules.can_handle(guild.owner_id == user.id, bool(getattr(perms, "administrator", False)),
                                      getattr(user, "roles", []))


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def load_events() -> tuple:
    try:
        return S.load_events()
    except Exception:
        log.exception("sitedata: couldn't read %s; no seasonal event on the site", S.EVENTS_FILE.name)
        return ()


class SiteData(commands.Cog):
    group = app_commands.Group(name="sitedata", description="Live numbers for the landing page (staff only)",
                               guild_only=True,
                               default_permissions=discord.Permissions(moderate_members=True))

    def __init__(self, bot, path: Path | None = None, events: tuple | None = None, enabled: bool | None = None):
        self.bot = bot
        self.path = config.SITE_DATA_FILE if path is None else Path(path)
        self.events = load_events() if events is None else tuple(events)
        self.enabled = config.site_data_enabled() if enabled is None else enabled
        self.last_run: int | None = None

    async def cog_load(self) -> None:
        if self.enabled:
            self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    # ------------------------------------------------------------ scheduling
    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        t = now()
        if not SD.due(self.last_run, t):
            return
        self.last_run = t  # a failing run waits for the next interval instead of retrying every minute
        try:
            await self.refresh(t)
        except Exception:
            log.exception("sitedata: refresh failed")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ gathering
    async def section(self, name: str, read, default):
        """One source's numbers; a missing table or a failed query gives `default`."""
        try:
            return await read()
        except Exception:
            log.warning("sitedata: skipped %s", name, exc_info=True)
            return default

    async def one(self, sql: str, params=()) -> tuple[int, int]:
        """(n, people) from a query that selects both."""
        row = await self.db.fetchone(sql, params)
        return (int(row["n"] or 0), int(row["people"] or 0)) if row else (0, 0)

    def presence(self, guild) -> tuple[int | None, int | None]:
        """(members, online). Online needs the member cache with presences; else None."""
        if guild is None:
            return None, None
        total = getattr(guild, "member_count", None)
        humans = [m for m in getattr(guild, "members", []) if not getattr(m, "bot", False)]
        seen = [m for m in humans if getattr(m, "status", None) is not None]
        if len(humans) < 2 or not seen:  # no members intent: only the bot is cached
            return total, None
        return total, sum(1 for m in seen if m.status != discord.Status.offline)

    def scheduled(self, guild) -> list[SD.EventIn]:
        out = []
        for e in getattr(guild, "scheduled_events", None) or []:
            status = getattr(e, "status", None)
            if status not in (discord.EventStatus.scheduled, discord.EventStatus.active):
                continue
            start = getattr(e, "start_time", None)
            if start is None:
                continue
            channel, public = getattr(e, "channel", None), True
            if channel is not None:  # a voice/stage event: only if anyone can see the channel
                try:
                    public = bool(channel.permissions_for(guild.default_role).view_channel)
                except Exception:
                    public = False
            out.append(SD.EventIn(title=str(getattr(e, "name", "") or ""), start=int(start.timestamp()),
                                  active=status is discord.EventStatus.active, public=public))
        return out

    async def chat(self, t: int) -> tuple[int, int]:
        first = (datetime.fromtimestamp(t, self.tz).date() - timedelta(days=6)).isoformat()
        return await self.one("SELECT SUM(count) AS n, COUNT(DISTINCT user_id) AS people FROM message_counts"
                              f" WHERE day >= ? AND user_id {OPTED_IN}", (first,))

    async def sessions(self, table: str, column: str, lo: int, hi: int) -> list:
        return await self.db.fetchall(
            f'SELECT user_id, {column} AS what, start, "end" FROM {table}'
            f' WHERE start < ? AND ("end" IS NULL OR "end" > ?) AND user_id {OPTED_IN}', (hi, lo))

    async def voice(self, lo: int, t: int) -> tuple[int, int]:
        rows = await self.sessions("voice_sessions", "channel_id", lo, t)
        return SD.session_total(((r["user_id"], r["start"], r["end"]) for r in rows), lo, t, t)

    async def played(self, lo: int, t: int) -> list:
        rows = await self.sessions("game_sessions", "game", lo, t)
        return SD.game_table(((r["user_id"], r["what"], r["start"], r["end"]) for r in rows), lo, t, t)

    async def tournaments(self) -> tuple[dict, dict]:
        counts = {r["status"]: r["n"] for r in await self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM tournaments GROUP BY status")}
        marks = ",".join("?" * len(ACTIVE_TOURNAMENTS))
        active = {(r["name"] or "").strip().casefold(): r["game"] for r in await self.db.fetchall(
            f"SELECT name, game FROM tournaments WHERE status IN ({marks})", ACTIVE_TOURNAMENTS)}
        return counts, active

    async def matchmaking(self, lo: int) -> tuple[int, int, int]:
        out = {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}
        rows = await self.db.fetchall("SELECT members FROM mm_matches WHERE created_at >= ?", (lo,))
        people = {u for r in rows for u in M.decode_members(r["members"])} - out
        queued = await self.db.fetchone(f"SELECT COUNT(*) AS n FROM mm_queue WHERE user_id {OPTED_IN}")
        return len(rows), len(people), int(queued["n"]) if queued else 0

    async def houses(self) -> list | None:
        """House standings, if the houses module is merged: a `houses` table with `name` and
        `points` columns. Anything else (no table, other columns) means no standings."""
        if await self.db.fetchone("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'houses'") is None:
            return None
        columns = {r["name"] for r in await self.db.fetchall("PRAGMA table_info(houses)")}
        if not {"name", "points"} <= columns:
            return None
        return [(r["name"], r["points"]) for r in await self.db.fetchall("SELECT name, points FROM houses")]

    async def gather(self, t: int) -> SD.Inputs:
        guild = self.guild()
        lo = t - SD.WINDOW
        members, online = self.presence(guild)
        messages, message_people = await self.section("chat", lambda: self.chat(t), (0, 0))
        voice_seconds, voice_people = await self.section("voice", lambda: self.voice(lo, t), (0, 0))
        games = await self.section("games", lambda: self.played(lo, t), [])
        counts, active = await self.section("tournaments", self.tournaments, ({}, {}))
        matches, match_people, queued = await self.section("matchmaking", lambda: self.matchmaking(lo), (0, 0, 0))
        houses = await self.section("houses", self.houses, None)
        starred, starred_people = await self.section("hall of fame", lambda: self.one(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT author_id) AS people FROM starboard"
            f" WHERE board_message_id IS NOT NULL AND author_id {OPTED_IN}"), (0, 0))
        clips, clip_people = await self.section("clips", lambda: self.one(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT user_id) AS people FROM clips WHERE user_id {OPTED_IN}"), (0, 0))
        badges, badge_people = await self.section("badges", lambda: self.one(
            "SELECT COUNT(*) AS n, COUNT(DISTINCT user_id) AS people FROM achievements"
            f" WHERE user_id {OPTED_IN}"), (0, 0))
        run = S.current(self.events, t, self.tz) if self.events else None
        return SD.Inputs(
            now=t, member_count=members, online=online,
            messages=messages, message_people=message_people,
            voice_seconds=voice_seconds, voice_people=voice_people,
            events=self.scheduled(guild) if guild is not None else [],
            active_tournaments=active, games=games, tournaments=counts,
            matches=matches, match_people=match_people, queued=queued,
            seasonal=(run.event.name, run.event.emoji, run.end) if run is not None else None,
            houses=houses, starred=starred, starred_people=starred_people,
            clips=clips, clip_people=clip_people, badges=badges, badge_people=badge_people,
        )

    async def refresh(self, t: int | None = None) -> dict:
        """Build the document and write it. Raises logic.sitedata.Unsafe (nothing written)
        when the result fails the schema or the leak check."""
        t = now() if t is None else t
        doc = SD.build(await self.gather(t))
        write_atomic(self.path, SD.render(doc))
        return doc

    # ------------------------------------------------------------ command
    @group.command(name="refresh", description="Rebuild the landing page's live numbers now (staff only)")
    async def refresh_command(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can use /sitedata.", ephemeral=True)
            return
        if not self.enabled:
            await interaction.response.send_message(
                "Live site data is off (SITE_DATA=off in server/.env), so nothing was written.", ephemeral=True)
            return
        try:
            doc = await self.refresh()
        except SD.Unsafe:
            log.exception("/sitedata refresh: the document failed its checks")
            await interaction.response.send_message(
                "The numbers failed the privacy check, so nothing was written. Check the logs.", ephemeral=True)
            return
        except Exception:
            log.exception("/sitedata refresh failed")
            await interaction.response.send_message("Couldn't build the site data. Check the logs.", ephemeral=True)
            return
        self.last_run = now()
        await interaction.response.send_message(
            f"Site data rebuilt. Sections: {SD.summary(doc)}.\n"
            "Aggregates only; anything about fewer than "
            f"{SD.MIN_GROUP} members is left out. The page updates after the next publish run.",
            ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(SiteData(bot))
