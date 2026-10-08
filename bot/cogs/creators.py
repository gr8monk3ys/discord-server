"""Creator spotlight: members link their YouTube channel and/or Twitch login with
/creator link, and Front Desk announces new uploads and go-lives in the creators
channel.

YouTube needs no keys: the public upload feed is polled every 15 minutes. Twitch needs
TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in the environment (an app access token via
client credentials); without them Twitch links are refused with a friendly message.
Uploads already on the channel when it's linked are marked seen, so only new ones post.
Every link in a post is rebuilt from a validated id, never copied from a feed."""

import json
import logging
import os
import time
from typing import Protocol

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from logic import community as community_rules
from logic import creators as C

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 10
USER_AGENT = "FrontDesk Discord bot"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
NAMES = {"youtube": "YouTube", "twitch": "Twitch"}
PLATFORM_CHOICES = [app_commands.Choice(name=NAMES[p], value=p) for p in C.PLATFORMS]

MESSAGES = {
    "platform": "Pick YouTube or Twitch.",
    "limit": f"You can link up to {C.MAX_LINKS} platforms. `/creator unlink` one first.",
    "youtube_input": ("That doesn't look like a YouTube channel. Paste a link like "
                      "`https://www.youtube.com/@yourhandle` or `https://www.youtube.com/channel/UC...`."),
    "twitch_input": "That doesn't look like a Twitch channel. Paste `https://twitch.tv/yourname` or just the name.",
    "twitch_off": "Twitch alerts aren't set up yet. YouTube works now; Twitch is coming.",
    "not_found": "I couldn't find that channel. Check the link and try again.",
    "unreachable": "I couldn't reach {platform} just now. Try again in a few minutes.",
    "taken": "Someone here already linked that channel. If it's yours, ask a mod.",
}


def now() -> int:
    return int(time.time())


class Http(Protocol):
    """The network seam. Tests pass a fake; nothing here touches the network directly."""

    async def get(self, url: str, *, params=None, headers=None,
                  max_bytes: int = C.MAX_FEED_BYTES) -> tuple[int, bytes, bool]: ...

    async def post(self, url: str, *, data=None) -> tuple[int, bytes]: ...


class AiohttpHttp:
    """GET/POST with a 10 s timeout. GET reads at most `max_bytes` (the flag says the
    body was cut short)."""

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
                                     headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"})

    async def get(self, url, *, params=None, headers=None, max_bytes=C.MAX_FEED_BYTES):
        async with self._session() as session:
            async with session.get(url, params=params, headers=headers, max_redirects=3) as resp:
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    body += chunk
                    if len(body) > max_bytes:
                        return resp.status, bytes(body[:max_bytes]), True
                return resp.status, bytes(body), False

    async def post(self, url, *, data=None):
        async with self._session() as session:
            async with session.post(url, data=data, max_redirects=0) as resp:
                return resp.status, await resp.content.read(64 * 1024)


class Unreachable(Exception):
    """The platform didn't answer usefully (timeout, 5xx, garbage)."""


class NotFound(Exception):
    pass


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    return community_rules.can_handle(guild.owner_id == user.id, user.guild_permissions.administrator, user.roles)


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


def _json(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


class Creators(commands.Cog):
    creator = app_commands.Group(name="creator", description="Get your YouTube uploads and Twitch streams spotlighted",
                                 guild_only=True)

    def __init__(self, bot, http: Http | None = None, environ=None):
        self.bot = bot
        self.http = http or AiohttpHttp()
        environ = os.environ if environ is None else environ
        cid = (environ.get("TWITCH_CLIENT_ID") or "").strip()
        secret = (environ.get("TWITCH_CLIENT_SECRET") or "").strip()
        self.twitch_creds = (cid, secret) if cid and secret else None
        self.token: str | None = None
        self.token_expires = 0
        self.warned: set[str] = set()

    async def cog_load(self) -> None:
        self.youtube_loop.start()
        if self.twitch_creds:
            self.twitch_loop.start()

    async def cog_unload(self) -> None:
        self.youtube_loop.cancel()
        self.twitch_loop.cancel()

    @property
    def db(self):
        return self.bot.db

    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    def warn_once(self, key: str, msg: str, *args) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning(msg, *args)

    # ------------------------------------------------------------ YouTube I/O
    async def fetch_feed(self, channel_id: str) -> C.Feed:
        try:
            status, body, cut = await self.http.get(C.feed_url(channel_id), max_bytes=C.MAX_FEED_BYTES)
        except Exception as exc:
            raise Unreachable(type(exc).__name__) from None
        if status == 404:
            raise NotFound()
        if status != 200 or cut:
            raise Unreachable(f"feed status {status}{' (too large)' if cut else ''}")
        try:
            return C.parse_feed(body)
        except ValueError as exc:
            raise Unreachable(str(exc)) from None

    async def resolve_handle(self, handle: str) -> str:
        try:
            status, body, _ = await self.http.get(C.handle_url(handle), max_bytes=C.MAX_PAGE_BYTES)
        except Exception as exc:
            raise Unreachable(type(exc).__name__) from None
        if status == 404:
            raise NotFound()
        if status != 200:
            raise Unreachable(f"channel page status {status}")
        channel_id = C.extract_channel_id(body)
        if channel_id is None:
            raise NotFound()
        return channel_id

    # ------------------------------------------------------------ Twitch I/O
    async def twitch_token(self) -> str:
        if self.token and now() < self.token_expires:
            return self.token
        cid, secret = self.twitch_creds
        try:
            status, body = await self.http.post(TOKEN_URL, data={"client_id": cid, "client_secret": secret,
                                                                 "grant_type": "client_credentials"})
        except Exception as exc:
            raise Unreachable(type(exc).__name__) from None
        data = _json(body) if status == 200 else None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise Unreachable(f"token status {status}")  # never log the reply: it may echo credentials
        self.token, self.token_expires = token, C.token_expires_at(now(), data.get("expires_in"))
        return token

    async def helix(self, path: str, params) -> object:
        token = await self.twitch_token()
        headers = {"Client-Id": self.twitch_creds[0], "Authorization": f"Bearer {token}"}
        try:
            status, body, cut = await self.http.get(f"{HELIX}/{path}", params=params, headers=headers,
                                                    max_bytes=C.MAX_FEED_BYTES)
        except Exception as exc:
            raise Unreachable(type(exc).__name__) from None
        if status == 401:
            self.token = None  # expired or revoked: get a fresh one next time
        if status != 200 or cut:
            raise Unreachable(f"helix {path} status {status}")
        data = _json(body)
        if data is None:
            raise Unreachable(f"helix {path} sent bad JSON")
        return data

    # ------------------------------------------------------------ storage
    async def links_of(self, user_id: int) -> dict[str, dict]:
        rows = await self.db.fetchall("SELECT * FROM creators WHERE user_id = ?", (user_id,))
        return {r["platform"]: dict(r) for r in rows}

    async def owner_of(self, platform: str, external_id: str) -> int | None:
        row = await self.db.fetchone("SELECT user_id FROM creators WHERE platform = ? AND external_id = ?",
                                     (platform, external_id))
        return row["user_id"] if row else None

    async def save_link(self, user_id: int, platform: str, handle: str, external_id: str, seen) -> None:
        t = now()
        async with self.db.transaction() as tx:
            await tx.execute("INSERT OR REPLACE INTO creators (user_id, platform, handle, external_id, added_at)"
                             " VALUES (?, ?, ?, ?, ?)", (user_id, platform, handle, external_id, t))
            for item in seen:
                await tx.execute("INSERT OR IGNORE INTO creator_seen (platform, item_id, seen_at) VALUES (?, ?, ?)",
                                 (platform, item, t))

    async def mark_seen(self, platform: str, item_id: str) -> bool:
        """True if this item is new (and is now marked): announce it exactly once."""
        return await self.db.execute("INSERT OR IGNORE INTO creator_seen (platform, item_id, seen_at) VALUES (?, ?, ?)",
                                     (platform, item_id, now())) > 0

    # ------------------------------------------------------------ /creator
    async def link_youtube(self, user_id: int, text: str) -> str:
        ref = C.parse_youtube(text)
        if ref is None:
            return MESSAGES["youtube_input"]
        try:
            channel_id = ref.value if ref.kind == "channel" else await self.resolve_handle(ref.value)
            feed = await self.fetch_feed(channel_id)
        except NotFound:
            return MESSAGES["not_found"]
        except Unreachable as exc:
            log.info("creators: youtube link check failed: %s", exc)
            return MESSAGES["unreachable"].format(platform="YouTube")
        owner = await self.owner_of("youtube", channel_id)
        if owner is not None and owner != user_id:
            return MESSAGES["taken"]
        handle = f"@{ref.value}" if ref.kind == "handle" else channel_id
        await self.save_link(user_id, "youtube", handle, channel_id, [v.id for v in feed.videos])
        return (f"Linked **{esc(feed.title)}** ({C.channel_url(channel_id)}). New uploads will show up in "
                f"{config.CREATORS_CHANNEL}.")

    async def link_twitch(self, user_id: int, text: str) -> str:
        if not self.twitch_creds:
            return MESSAGES["twitch_off"]
        login = C.parse_twitch(text)
        if login is None:
            return MESSAGES["twitch_input"]
        try:
            user = C.parse_user(await self.helix("users", [("login", login)]))
            if user is None:
                return MESSAGES["not_found"]
            twitch_id, login, display = user
            live = C.parse_streams(await self.helix("streams", [("user_id", twitch_id)]))
        except Unreachable as exc:
            log.info("creators: twitch link check failed: %s", exc)
            return MESSAGES["unreachable"].format(platform="Twitch")
        owner = await self.owner_of("twitch", twitch_id)
        if owner is not None and owner != user_id:
            return MESSAGES["taken"]
        await self.save_link(user_id, "twitch", login, twitch_id, [s.id for s in live])
        return (f"Linked **{esc(display)}** ({C.twitch_url(login)}). Go-lives will show up in "
                f"{config.CREATORS_CHANNEL}.")

    @creator.command(name="link", description="Link your YouTube channel or Twitch to get spotlighted")
    @app_commands.describe(platform="YouTube or Twitch", channel="Your channel link, @handle or Twitch name")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def link(self, interaction: discord.Interaction, platform: app_commands.Choice[str],
                   channel: app_commands.Range[str, 1, C.MAX_INPUT]) -> None:
        uid = interaction.user.id
        problem = C.link_problem(list(await self.links_of(uid)), platform.value)
        if problem:
            await interaction.response.send_message(MESSAGES[problem], ephemeral=True)
            return
        if platform.value == "twitch" and not self.twitch_creds:
            await interaction.response.send_message(MESSAGES["twitch_off"], ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if platform.value == "youtube":
            text = await self.link_youtube(uid, channel)
        else:
            text = await self.link_twitch(uid, channel)
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @creator.command(name="unlink", description="Stop spotlighting one of your channels")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def unlink(self, interaction: discord.Interaction, platform: app_commands.Choice[str]) -> None:
        n = await self.db.execute("DELETE FROM creators WHERE user_id = ? AND platform = ?",
                                  (interaction.user.id, platform.value))
        text = f"Unlinked your {platform.name}." if n else f"You haven't linked a {platform.name} channel."
        await interaction.response.send_message(text, ephemeral=True)

    def describe(self, row) -> str:
        if row["platform"] == "youtube":
            return f"YouTube: <{C.channel_url(row['external_id'])}> ({esc(row['handle'])})"
        return f"Twitch: <{C.twitch_url(row['handle'])}>"

    @creator.command(name="list", description="Everyone here who streams or uploads")
    async def list_(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        rows = await self.db.fetchall("SELECT * FROM creators ORDER BY added_at, user_id, platform")
        lines = [f"<@{r['user_id']}> · {self.describe(r)}" for r in rows
                 if guild is None or guild.get_member(r["user_id"]) is not None]
        if not lines:
            text = "Nobody has linked a channel yet. Be the first with `/creator link`."
        else:
            text, shown = "", 0
            for line in lines:
                if len(text) + len(line) + 1 > 3800:
                    break
                text += line + "\n"
                shown += 1
            if shown < len(lines):
                text += f"…and {len(lines) - shown} more"
        embed = style.embed(title="Creators", description=text.strip(), footer=style.label("creators", "/creator link"))
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @creator.command(name="remove", description="Remove a member's linked channel (mods)")
    @app_commands.describe(member="Whose link", platform="Which one (default: both)")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def remove(self, interaction: discord.Interaction, member: discord.Member,
                     platform: app_commands.Choice[str] | None = None) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only mods can remove someone else's link.", ephemeral=True)
            return
        if platform is None:
            n = await self.db.execute("DELETE FROM creators WHERE user_id = ?", (member.id,))
        else:
            n = await self.db.execute("DELETE FROM creators WHERE user_id = ? AND platform = ?",
                                      (member.id, platform.value))
        text = f"Removed {n} link{'s' if n != 1 else ''} for {member.mention}." if n else "Nothing to remove."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ announcing
    def target(self, guild):
        channel = config.match_by_name(guild.text_channels, config.CREATORS_CHANNEL)
        if channel is None:
            self.warn_once("channel", "creators: no %s channel, not announcing", config.CREATORS_CHANNEL)
        return channel

    async def post(self, channel, content: str, embed: discord.Embed) -> None:
        try:
            await channel.send(content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            log.exception("creators: posting an announcement failed")

    def upload_embed(self, user_id: int, video: C.Video, channel_title: str) -> discord.Embed:
        embed = style.embed(title=esc(video.title)[:256], description=f"New upload from <@{user_id}>",
                            footer=style.label("youtube", channel_title[:60]))
        embed.url = C.watch_url(video.id)
        embed.set_image(url=f"https://i.ytimg.com/vi/{video.id}/hqdefault.jpg")
        return embed

    def live_embed(self, user_id: int, stream: C.Stream) -> discord.Embed:
        desc = f"<@{user_id}> is live on Twitch"
        if stream.game:
            desc += f"\nPlaying **{esc(stream.game)}**"
        embed = style.embed(title=esc(stream.title)[:256], description=desc, footer=style.label("twitch", stream.login))
        embed.url = C.twitch_url(stream.login)
        return embed

    # ------------------------------------------------------------ polling
    async def poll_youtube(self) -> int:
        """Check every linked channel once; returns announcements made."""
        guild = self.guild()
        channel = self.target(guild) if guild is not None else None
        if channel is None:
            return 0
        posted = 0
        rows = await self.db.fetchall("SELECT * FROM creators WHERE platform = 'youtube' ORDER BY user_id")
        for row in rows:
            uid = row["user_id"]
            if guild.get_member(uid) is None:
                continue
            try:
                feed = await self.fetch_feed(row["external_id"])
            except (Unreachable, NotFound) as exc:
                log.info("creators: feed for %s failed: %s", row["external_id"], type(exc).__name__)
                continue
            except Exception:
                log.exception("creators: feed for %s failed", row["external_id"])
                continue
            ids = [v.id for v in feed.videos]
            seen = {r["item_id"] for r in await self.db.fetchall(
                f"SELECT item_id FROM creator_seen WHERE platform = 'youtube' AND item_id IN ({','.join('?' * len(ids))})",
                ids)} if ids else set()
            fresh = C.unseen(feed.videos, seen)
            for video in feed.videos:  # extras beyond the per-poll cap are marked seen quietly
                if video.id not in seen and video not in fresh:
                    await self.mark_seen("youtube", video.id)
            for video in fresh:
                if await self.mark_seen("youtube", video.id):
                    await self.post(channel, f"📺 New video from <@{uid}>", self.upload_embed(uid, video, feed.title))
                    posted += 1
        return posted

    async def poll_twitch(self) -> int:
        if not self.twitch_creds:
            return 0
        guild = self.guild()
        channel = self.target(guild) if guild is not None else None
        if channel is None:
            return 0
        rows = await self.db.fetchall("SELECT * FROM creators WHERE platform = 'twitch'")
        owners = {r["external_id"]: r["user_id"] for r in rows if guild.get_member(r["user_id"]) is not None}
        posted = 0
        for batch in C.streams_batches(sorted(owners)):
            try:
                streams = C.parse_streams(await self.helix("streams", batch))
            except Unreachable as exc:
                log.info("creators: twitch poll failed: %s", exc)
                return posted
            for stream in streams:
                uid = owners.get(stream.user_id)
                if uid is not None and await self.mark_seen("twitch", stream.id):
                    await self.post(channel, f"🔴 <@{uid}> is live", self.live_embed(uid, stream))
                    posted += 1
        return posted

    @tasks.loop(minutes=15)
    async def youtube_loop(self) -> None:
        try:
            await self.poll_youtube()
        except Exception:
            log.exception("creators: youtube poll failed")

    @youtube_loop.before_loop
    async def before_youtube(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=3)
    async def twitch_loop(self) -> None:
        try:
            await self.poll_twitch()
        except Exception:
            log.exception("creators: twitch poll failed")

    @twitch_loop.before_loop
    async def before_twitch(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Creators(bot))
