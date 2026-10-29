"""Field notes: the owner's weekly notebook page in 📓・field-notes (rules in logic/fieldnotes.py).

Sundays at 18:00 local, through bot/jobs.py (caught up once after downtime, never twice), one
embed in the Field Notebook look: Built, Heard, Watched and Played, each left out when its
source is not set up, failed or had nothing that week, then one question. A week where every
section is empty posts nothing and is still marked done. Each post opens a thread for replies.

- Built: GitHub's public REST API with no token (search, then the owner's repo listing to keep
  public repos only). Rate limits and errors leave the section out.
- Heard: SpotifyForge's listening log on this machine, read only. Front Desk never handles a
  Spotify token; SpotifyForge's own scheduled `history sync` keeps the log current.
- Watched: the Letterboxd profile's public RSS feed.
- Played: the server owner's own rows in `game_sessions` (cogs/stats.py), unless they turned
  tracking off. Nobody else's data is read for the post.
- The question goes through bot/quietgate.py (kind `fieldnotes`): when the gate says the room
  is not answering, the notes still go out, without the question.
- Staff `/fieldnotes preview`: the week so far, privately, with which sources are set up. It
  changes nothing (no quiet-rooms state, no job rows).

Settings: FIELD_NOTES_GITHUB_USER, FIELD_NOTES_SPOTIFY_HISTORY, FIELD_NOTES_LETTERBOXD_USER,
FIELD_NOTES_PLAYED (server/.env.example). None set: no tick runs and nothing is posted.
"""

import asyncio
import functools
import logging
import os
import time
from typing import Protocol

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import jobs
import quietgate
import style
from cogkit import is_staff
from logic import fieldnotes as F
from logic import stats as S

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
FETCH_TIMEOUT = 10
TOTAL_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; FrontDesk/1.0)"  # the same as cogs/news.py
MAX_REPO_PAGES = 3


def now() -> int:
    return int(time.time())


def never_raise(fn):
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            return await fn(self, *args)
        except Exception:
            log.exception("fieldnotes: %s failed", fn.__name__)
    return wrapper


# ---------------------------------------------------------------- network seam
class Http(Protocol):
    """Tests pass a fake; nothing else here touches the network."""

    async def get(self, url: str, *, headers: dict[str, str], max_bytes: int) -> tuple[int, bytes, bool]: ...


class AiohttpHttp:
    """GET with a 10 s timeout and no redirects, reading at most `max_bytes`."""

    async def get(self, url, *, headers, max_bytes):
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT, **headers}) as session:
            async with session.get(url, allow_redirects=False) as resp:
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    body += chunk
                    if len(body) > max_bytes:
                        return resp.status, bytes(body[:max_bytes]), True
                return resp.status, bytes(body), False


class Unavailable(Exception):
    """A source didn't answer usefully; its section is left out."""


def read_lines(path) -> list[str]:
    """The listening log, read only. Raises OSError when it is missing or unreadable."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.readlines()


class FieldNotes(commands.Cog):
    fieldnotes = app_commands.Group(name="fieldnotes", description="Staff: the weekly field notes",
                                    guild_only=True, default_permissions=discord.Permissions(moderate_members=True))

    def __init__(self, bot, environ=None, http: Http | None = None, reader=read_lines):
        self.bot = bot
        self.settings = F.read_settings(os.environ if environ is None else environ)
        self.http = http or AiohttpHttp()
        self.reader = reader
        self.warned: set[str] = set()

    async def cog_load(self) -> None:
        if self.settings.enabled:
            self.tick.start()
        else:
            log.info("fieldnotes: no FIELD_NOTES_* source set; nothing will be posted")

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

    def channel(self, guild):
        return config.match_by_name(guild.text_channels, config.FIELD_NOTES_CHANNEL) if guild is not None else None

    def warn_once(self, key: str, msg: str, *args) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning(msg, *args)

    # ------------------------------------------------------------ sources
    async def fetch(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        try:
            status, body, cut = await asyncio.wait_for(
                self.http.get(url, headers=headers or {}, max_bytes=F.MAX_BODY), TOTAL_TIMEOUT)
        except Exception as exc:  # timeout, DNS, connection reset...
            raise Unavailable(type(exc).__name__) from None
        if status != 200 or cut:
            raise Unavailable(f"status {status}{' (too large)' if cut else ''}")
        return body

    async def built(self, week: F.Week) -> list[str]:
        user = self.settings.github_user
        if not user:
            return []
        gh = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            public: set[str] = set()
            for page in range(1, MAX_REPO_PAGES + 1):
                names, listed = F.public_repos(await self.fetch(F.repos_url(user, page), gh), user)
                public |= names
                if listed < 100:
                    break
            prs = F.merged_prs(await self.fetch(F.search_url(user, week), gh), user, public, week)
        except (Unavailable, ValueError) as exc:
            log.info("fieldnotes: GitHub left out (%s)", exc)
            return []
        return F.built_lines(user, prs)

    async def heard(self, week: F.Week) -> list[str]:
        path = self.settings.spotify_history
        if path is None:
            return []
        try:
            lines = await asyncio.to_thread(self.reader, path)
        except OSError as exc:
            log.info("fieldnotes: listening log left out (%s)", type(exc).__name__)
            return []
        plays, _newest = F.parse_history(lines, week)
        return F.heard_lines(plays)

    async def watched(self, week: F.Week) -> list[str]:
        user = self.settings.letterboxd_user
        if not user:
            return []
        try:
            films = F.parse_rss(await self.fetch(F.rss_url(user)))
        except (Unavailable, ValueError) as exc:
            log.info("fieldnotes: Letterboxd left out (%s)", exc)
            return []
        return F.watched_lines(F.watched_this_week(films, week))

    async def played(self, week: F.Week, guild) -> list[str]:
        owner = getattr(guild, "owner_id", None)
        if not self.settings.played or owner is None or not await self.db.tracking_allowed(owner):
            return []
        rows = await self.db.fetchall(
            'SELECT user_id, game AS k, start, "end" FROM game_sessions'
            ' WHERE user_id = ? AND start < ? AND ("end" IS NULL OR "end" > ?)', (owner, week.end, week.start))
        sessions = [S.Session(r["user_id"], r["k"], r["start"], r["end"]) for r in rows]
        per_game = S.game_seconds(sessions, week.start, week.end, now()).get(owner, {})
        return F.played_lines(per_game)

    async def sections(self, week: F.Week, guild) -> F.Sections:
        async def safe(name, coro):
            try:
                return tuple(await coro)
            except Exception:
                log.exception("fieldnotes: %s failed; left out", name)
                return ()
        built, heard, watched, played = await asyncio.gather(
            safe("built", self.built(week)), safe("heard", self.heard(week)),
            safe("watched", self.watched(week)), safe("played", self.played(week, guild)))
        return F.Sections(built, heard, watched, played)

    @staticmethod
    def embed(week: F.Week, sections: F.Sections, ask: str | None) -> discord.Embed:
        return style.embed(title=week.title, description=F.body(sections, ask),
                           footer=style.label("field notes", f"week {week.iso_week}"))

    # ------------------------------------------------------------ the weekly post
    @tasks.loop(minutes=5)
    async def tick(self) -> None:
        await self.run_once()

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    @never_raise
    async def run_once(self) -> bool:
        return await jobs.run_due(self.db, F.JOB, self.tz, now, self.post)

    async def post(self, period) -> bool:
        """True when the week is handled: posted, or nothing to say. False (retry next tick)
        while the server or the channel is missing."""
        guild = self.guild()
        channel = self.channel(guild)
        if channel is None:
            self.warn_once(period.key, "fieldnotes %s waits: no %s channel (run server/public_mode.py)",
                           period.key, config.FIELD_NOTES_CHANNEL)
            return False
        week = F.week_of(period, self.tz)
        sections = await self.sections(week, guild)
        if sections.empty:
            log.info("fieldnotes %s: nothing this week, no post", period.key)
            return True
        ask = F.question(week) if await quietgate.allowed(self.bot, F.QUIET, channel, period.scheduled_at) else None
        message = await channel.send(embed=self.embed(week, sections, ask), allowed_mentions=NO_PINGS)
        if ask:
            await quietgate.posted(self.db, F.QUIET, message, period.scheduled_at)
        try:
            await message.create_thread(name=F.thread_name(week), auto_archive_duration=10080)
        except discord.HTTPException:
            log.warning("fieldnotes %s: couldn't open the thread", period.key, exc_info=True)
        log.info("posted field notes %s", period.key)
        return True

    # ------------------------------------------------------------ /fieldnotes preview
    @fieldnotes.command(name="preview", description="Staff: preview this week's field notes so far, privately")
    async def preview(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can preview the field notes.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        week = F.preview_week(F.JOB, now(), self.tz)
        sections = await self.sections(week, interaction.guild)
        sources = ", ".join(f"{name} {'on' if on else 'off'}" for name, on in self.settings.sources())
        channel = self.channel(interaction.guild)
        where = channel.mention if channel is not None else f"#{config.FIELD_NOTES_CHANNEL} (not created yet)"
        note = (f"Preview of the week so far. It posts Sundays at 18:00 in {where}.\nSources: {sources}.")
        if sections.empty:
            await interaction.followup.send(f"{note}\nNothing to post yet: every section is empty.",
                                            ephemeral=True, allowed_mentions=NO_PINGS)
            return
        await interaction.followup.send(note, embed=self.embed(week, sections, F.question(week)),
                                        ephemeral=True, allowed_mentions=NO_PINGS)


async def setup(bot) -> None:
    await bot.add_cog(FieldNotes(bot))
