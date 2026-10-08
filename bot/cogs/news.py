"""Game news: every 30 minutes Front Desk reads each game's official feed (listed in
assets/news_sources.json) and posts new items as embeds in that game's channel.

Steam games use the public Steam news API (no key), official announcements only; other
games use an official RSS/Atom feed when one exists, and games without one get no news.
At most 2 posts per game per poll, nothing older than 7 days, and nothing twice: posted
and skipped items go in news_seen. The first time a source is read its current items are
only marked seen, so turning this on doesn't flood the channels. Posts carry no pings,
escaped titles, plain-text summaries and links rebuilt from validated parts.

Staff check a feed with /news test, which shows its latest item only to them."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Protocol

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from logic import community as community_rules
from logic import news as N

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 10  # aiohttp's own limit
TOTAL_TIMEOUT = 15  # hard cap around any fetch, whatever the client does
# minecraft.net stalls on agents that look like bots, so this one reads like a browser's.
USER_AGENT = "Mozilla/5.0 (compatible; FrontDesk/1.0)"
NO_PINGS = discord.AllowedMentions.none()
STAFF_ONLY = "Only Moderators and Keepers can do that."
EMBED_TITLE = 256


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


def fit(text: str, limit: int) -> str:
    """Cut escaped text to `limit` without leaving a dangling escape backslash."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip("\\") + "…"


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    return community_rules.can_handle(guild.owner_id == user.id, user.guild_permissions.administrator, user.roles)


# ---------------------------------------------------------------- network seam
class Http(Protocol):
    """Tests pass a fake; nothing else here touches the network."""

    async def get(self, url: str, *, max_bytes: int) -> tuple[int, bytes, bool]: ...


class AiohttpHttp:
    """GET with a 10 s timeout, no redirects (the host we validated is the host we read),
    reading at most `max_bytes` (the flag says the body was cut short)."""

    follow_redirects = False

    async def get(self, url, *, max_bytes):
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(url, allow_redirects=self.follow_redirects) as resp:
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    body += chunk
                    if len(body) > max_bytes:
                        return resp.status, bytes(body[:max_bytes]), True
                return resp.status, bytes(body), False


class Unreachable(Exception):
    """The feed didn't answer usefully (timeout, non-200, too big, garbage)."""


def _game(role: str):
    return next((g for g in config.GAMES if g.role == role), None)


class News(commands.Cog):
    news = app_commands.Group(name="news", description="Game news feeds (staff)", guild_only=True,
                              default_permissions=discord.Permissions(moderate_members=True))

    def __init__(self, bot, http: Http | None = None, sources: list[N.Source] | None = None):
        self.bot = bot
        self.http = http or AiohttpHttp()
        self.sources = sources if sources is not None else self.load_sources()
        self.warned: set[str] = set()

    @staticmethod
    def load_sources() -> list[N.Source]:
        try:
            return N.read_sources({g.role for g in config.GAMES})
        except (OSError, ValueError):
            log.exception("news: couldn't read %s; no game news", N.SOURCES_FILE.name)
            return []

    async def cog_load(self) -> None:
        self.news_loop.start()

    async def cog_unload(self) -> None:
        self.news_loop.cancel()

    @property
    def db(self):
        return self.bot.db

    def warn_once(self, key: str, msg: str, *args) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning(msg, *args)

    # ------------------------------------------------------------ fetching
    async def fetch(self, source: N.Source) -> list[N.Item]:
        url = N.steam_api_url(source.appid) if source.kind == "steam" else source.url
        try:
            status, body, cut = await asyncio.wait_for(self.http.get(url, max_bytes=N.MAX_BODY), TOTAL_TIMEOUT)
        except Exception as exc:  # timeout, DNS, connection reset...
            raise Unreachable(type(exc).__name__) from None
        if status != 200 or cut:
            raise Unreachable(f"status {status}{' (too large)' if cut else ''}")
        try:
            if source.kind == "steam":
                return N.parse_steam(body, source.appid)
            return N.parse_rss(body, source.host)
        except ValueError as exc:
            raise Unreachable(str(exc)) from None

    # ------------------------------------------------------------ seen state
    async def seen_ids(self, source: N.Source) -> set[str]:
        rows = await self.db.fetchall("SELECT item_id FROM news_seen WHERE source = ?", (source.key,))
        return {r["item_id"] for r in rows}

    async def mark_seen(self, source: N.Source, item_id: str) -> bool:
        """True if this item is new (and is now marked): post it exactly once."""
        return await self.db.execute("INSERT OR IGNORE INTO news_seen (source, item_id, seen_at) VALUES (?, ?, ?)",
                                     (source.key, item_id, now())) > 0

    async def seed(self, source: N.Source, items: list[N.Item]) -> None:
        async with self.db.transaction() as tx:
            for item_id in [N.SEED_MARK, *(i.id for i in items)]:
                await tx.execute("INSERT OR IGNORE INTO news_seen (source, item_id, seen_at) VALUES (?, ?, ?)",
                                 (source.key, item_id, now()))

    async def prune(self) -> None:
        await self.db.execute("DELETE FROM news_seen WHERE seen_at < ? AND item_id != ?",
                              (now() - N.SEEN_KEEP, N.SEED_MARK))

    # ------------------------------------------------------------ rendering
    @staticmethod
    def item_embed(game_role: str, item: N.Item) -> discord.Embed:
        embed = style.embed(title=fit(esc(item.title), EMBED_TITLE), description=esc(item.summary) or None,
                            footer=style.label("news", game_role))
        embed.url = item.link
        embed.timestamp = datetime.fromtimestamp(item.published, tz=timezone.utc)
        return embed

    # ------------------------------------------------------------ polling
    async def poll_source(self, guild, source: N.Source) -> int:
        game = _game(source.game)
        channel = config.match_by_name(guild.text_channels, game.channel_name) if game else None
        if channel is None:
            self.warn_once(source.key, "news: no channel for %s, skipping its news", source.game)
            return 0
        try:
            items = await self.fetch(source)
        except Unreachable as exc:
            log.info("news: %s feed failed: %s", source.game, exc)
            return 0
        seen = await self.seen_ids(source)
        if not seen:  # first read: today's backlog is not news
            await self.seed(source, items)
            log.info("news: seeded %s with %d item(s)", source.game, len(items))
            return 0
        post, quiet = N.select(items, seen, now())
        for item in quiet:
            await self.mark_seen(source, item.id)
        posted = 0
        for item in post:
            if not await self.mark_seen(source, item.id):
                continue
            try:
                await channel.send(embed=self.item_embed(source.game, item), allowed_mentions=NO_PINGS)
                posted += 1
            except Exception:
                log.exception("news: posting %s news failed", source.game)
        return posted

    async def poll(self) -> int:
        """Read every source once; returns posts made."""
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        if guild is None:
            return 0
        await self.prune()
        posted = 0
        for source in self.sources:
            try:
                posted += await self.poll_source(guild, source)
            except Exception:
                log.exception("news: %s poll failed", source.game)
        return posted

    @tasks.loop(minutes=N.POLL_MINUTES)
    async def news_loop(self) -> None:
        try:
            await self.poll()
        except Exception:
            log.exception("news: poll failed")

    @news_loop.before_loop
    async def before_news(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ /news test
    def source_for(self, name: str) -> N.Source | None:
        target = config.slug(name or "")
        return next((s for s in self.sources if config.slug(s.game) == target), None)

    @news.command(name="test", description="Show a game's latest news item (only you see it)")
    @app_commands.describe(game="A game with a news feed")
    async def news_test(self, interaction: discord.Interaction, game: str) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        source = self.source_for(game)
        if source is None:
            await interaction.response.send_message("That game has no news feed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            item = N.latest(await self.fetch(source))
        except Unreachable as exc:
            log.info("news: /news test %s failed: %s", source.game, exc)
            await interaction.followup.send(f"I couldn't reach the {esc(source.game)} feed just now.",
                                            ephemeral=True, allowed_mentions=NO_PINGS)
            return
        if item is None:
            await interaction.followup.send(f"Nothing official in the {esc(source.game)} feed right now.",
                                            ephemeral=True, allowed_mentions=NO_PINGS)
            return
        await interaction.followup.send(embed=self.item_embed(source.game, item), ephemeral=True,
                                        allowed_mentions=NO_PINGS)

    @news_test.autocomplete("game")
    async def game_autocomplete(self, interaction: discord.Interaction, current: str):
        needle = (current or "").lower()
        return [app_commands.Choice(name=s.game, value=s.game) for s in self.sources
                if needle in s.game.lower()][:25]


async def setup(bot) -> None:
    await bot.add_cog(News(bot))
