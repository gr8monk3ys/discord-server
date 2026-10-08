"""Creator spotlight: members link their YouTube channel and/or Twitch login with
/creator link, prove it's theirs with /creator verify, and Front Desk announces new
uploads and go-lives in the creators channel.

Ownership: /creator link stores a pending claim with a random code (FD-XXXXXX). The
member puts the code in their YouTube channel description (or a recent upload's
description) or their Twitch bio and runs /creator verify within 24 hours; staff can
/creator approve instead (logged to the mod log). Only verified links are announced, and
links made before verification existed stay quiet until they are verified.

YouTube needs no keys: the public upload feed is polled every 15 minutes. Twitch needs
TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in the environment (an app access token via
client credentials); without them Twitch links are refused with a friendly message.
Uploads already on the channel when it's verified are marked seen, so only new ones post.
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
WHERE = {"youtube": "YouTube channel description (or a recent upload's description)", "twitch": "Twitch bio"}
WHAT = {"youtube": "uploads", "twitch": "go-lives"}
NO_PINGS = discord.AllowedMentions.none()

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
    "no_pending": "You have no {platform} link waiting. Start with `/creator link`.",
    "legacy": ("Your {platform} link was made before verification existed, so it isn't announced yet. "
               "Run `/creator link` with the same channel to get a code."),
    "already": "Your {platform} link is already verified.",
    "expired": "Your code expired (codes last 24 hours). Run `/creator link` again for a new one.",
    "cooldown": "One check a minute, please. Try again in {seconds} s.",
    "no_code": ("I couldn't find `{code}` in your {where} yet. Save it there, give {platform} a minute to "
                "update, then run `/creator verify` again."),
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
        # user id -> time of their last /creator verify check. A rate limit, so it may
        # reset on restart; everything that must last is in SQLite.
        self.last_verify: dict[int, int] = {}

    async def cog_load(self) -> None:
        self.youtube_loop.start()
        self.cleanup_loop.start()
        if self.twitch_creds:
            self.twitch_loop.start()

    async def cog_unload(self) -> None:
        self.youtube_loop.cancel()
        self.twitch_loop.cancel()
        self.cleanup_loop.cancel()

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

    async def fetch_page(self, url: str) -> bytes:
        """A YouTube channel page, cut at MAX_PAGE_BYTES (what we need sits near the top)."""
        try:
            status, body, _ = await self.http.get(url, max_bytes=C.MAX_PAGE_BYTES)
        except Exception as exc:
            raise Unreachable(type(exc).__name__) from None
        if status == 404:
            raise NotFound()
        if status != 200:
            raise Unreachable(f"channel page status {status}")
        return body

    async def resolve_handle(self, handle: str) -> str:
        channel_id = C.extract_channel_id(await self.fetch_page(C.handle_url(handle)))
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

    # ------------------------------------------------------------ proof and seeding
    async def youtube_proof(self, channel_id: str) -> tuple[list[str], list[str]]:
        """(texts the code may be in, upload ids to mark seen) for a YouTube channel: the
        channel page's description (only if the page really is this channel's) and the
        descriptions of the uploads in its feed. Only the channel's owner can write any
        of these."""
        page = await self.fetch_page(C.channel_url(channel_id))
        texts = C.youtube_descriptions(page) if C.extract_channel_id(page) == channel_id else []
        feed = await self.fetch_feed(channel_id)
        return texts + C.feed_descriptions(feed), [v.id for v in feed.videos]

    async def twitch_proof(self, twitch_id: str) -> tuple[list[str], list[str], str | None]:
        """([bio], live stream ids to mark seen, current login) for a Twitch account id."""
        data = await self.helix("users", [("id", twitch_id)])
        bio = C.twitch_bio(data, twitch_id)
        if bio is None:
            raise NotFound()
        user = C.parse_user(data)
        live = C.parse_streams(await self.helix("streams", [("user_id", twitch_id)]))
        return [bio], [s.id for s in live], user[1] if user else None

    async def current_items(self, platform: str, external_id: str) -> list[str]:
        """What's already up (uploads / live streams), to mark seen when a link is verified."""
        if platform == "youtube":
            return [v.id for v in (await self.fetch_feed(external_id)).videos]
        if not self.twitch_creds:
            return []  # nothing is polled without credentials, so there is nothing to seed
        return [s.id for s in C.parse_streams(await self.helix("streams", [("user_id", external_id)]))]

    # ------------------------------------------------------------ storage
    async def links_of(self, user_id: int) -> dict[str, dict]:
        rows = await self.db.fetchall("SELECT * FROM creators WHERE user_id = ?", (user_id,))
        return {r["platform"]: dict(r) for r in rows}

    async def owner_of(self, platform: str, external_id: str) -> int | None:
        """Who has verified this channel. Unverified claims don't block the real owner."""
        row = await self.db.fetchone("SELECT user_id FROM creators WHERE platform = ? AND external_id = ?"
                                     " AND verified = 1", (platform, external_id))
        return row["user_id"] if row else None

    async def pending_of(self, user_id: int, platform: str) -> dict | None:
        row = await self.db.fetchone("SELECT * FROM creator_pending WHERE user_id = ? AND platform = ?",
                                     (user_id, platform))
        return dict(row) if row else None

    async def save_pending(self, user_id: int, platform: str, handle: str, external_id: str) -> str:
        """Store a claim and return its code. Linking the same channel again keeps a live code
        (and its 24 hours), so a member who already pasted it isn't sent back to change it."""
        t = now()
        old = await self.pending_of(user_id, platform)
        if old and old["external_id"] == external_id and not C.expired(old["created_at"], t):
            await self.db.execute("UPDATE creator_pending SET handle = ? WHERE user_id = ? AND platform = ?",
                                  (handle, user_id, platform))
            return old["code"]
        code = C.new_code()
        await self.db.execute("INSERT OR REPLACE INTO creator_pending (user_id, platform, handle, external_id, code,"
                              " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                              (user_id, platform, handle, external_id, code, t))
        return code

    async def promote(self, user_id: int, platform: str, handle: str, external_id: str, seen) -> bool:
        """Make this the member's verified link for the platform, mark what's already up as
        seen and drop the claim, in one transaction. False if someone else verified it first."""
        t = now()
        async with self.db.transaction() as tx:
            other = await tx.fetchone("SELECT 1 FROM creators WHERE platform = ? AND external_id = ?"
                                      " AND verified = 1 AND user_id != ?", (platform, external_id, user_id))
            if other is not None:
                return False
            # anyone else's unverified claim on this channel was never theirs
            await tx.execute("DELETE FROM creators WHERE platform = ? AND external_id = ? AND user_id != ?",
                             (platform, external_id, user_id))
            await tx.execute("INSERT OR REPLACE INTO creators (user_id, platform, handle, external_id, added_at,"
                             " verified) VALUES (?, ?, ?, ?, ?, 1)", (user_id, platform, handle, external_id, t))
            for item in seen:
                await tx.execute("INSERT OR IGNORE INTO creator_seen (platform, item_id, seen_at) VALUES (?, ?, ?)",
                                 (platform, item, t))
            await tx.execute("DELETE FROM creator_pending WHERE user_id = ? AND platform = ?", (user_id, platform))
        return True

    async def clean_pending(self) -> int:
        """Drop claims older than a day; returns how many."""
        return await self.db.execute("DELETE FROM creator_pending WHERE created_at <= ?", (now() - C.CODE_TTL,))

    async def mark_seen(self, platform: str, item_id: str) -> bool:
        """True if this item is new (and is now marked): announce it exactly once."""
        return await self.db.execute("INSERT OR IGNORE INTO creator_seen (platform, item_id, seen_at) VALUES (?, ?, ?)",
                                     (platform, item_id, now())) > 0

    # ------------------------------------------------------------ /creator link
    async def start_claim(self, user_id: int, platform: str, handle: str, external_id: str, shown: str) -> str:
        owner = await self.owner_of(platform, external_id)
        if owner is not None and owner != user_id:
            return MESSAGES["taken"]
        if owner == user_id:
            return MESSAGES["already"].format(platform=NAMES[platform])
        code = await self.save_pending(user_id, platform, handle, external_id)
        return (f"Found {shown}. To show it's yours, put `{code}` anywhere in your {WHERE[platform]}, "
                f"then run `/creator verify` within 24 hours. Once verified you can remove the code, and new "
                f"{WHAT[platform]} will show up in {config.CREATORS_CHANNEL}.")

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
        handle = f"@{ref.value}" if ref.kind == "handle" else channel_id
        return await self.start_claim(user_id, "youtube", handle, channel_id,
                                      f"**{esc(feed.title)}** ({C.channel_url(channel_id)})")

    async def link_twitch(self, user_id: int, text: str) -> str:
        if not self.twitch_creds:
            return MESSAGES["twitch_off"]
        login = C.parse_twitch(text)
        if login is None:
            return MESSAGES["twitch_input"]
        try:
            user = C.parse_user(await self.helix("users", [("login", login)]))
        except Unreachable as exc:
            log.info("creators: twitch link check failed: %s", exc)
            return MESSAGES["unreachable"].format(platform="Twitch")
        if user is None:
            return MESSAGES["not_found"]
        twitch_id, login, display = user
        return await self.start_claim(user_id, "twitch", login, twitch_id,
                                      f"**{esc(display)}** ({C.twitch_url(login)})")

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
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /creator verify
    async def verify_claim(self, user_id: int, platform: str) -> str:
        name = NAMES[platform]
        pending = await self.pending_of(user_id, platform)
        if pending is None:
            current = (await self.links_of(user_id)).get(platform)
            if current and current["verified"]:
                return MESSAGES["already"].format(platform=name)
            return MESSAGES["legacy" if current else "no_pending"].format(platform=name)
        t = now()
        if C.expired(pending["created_at"], t):
            await self.db.execute("DELETE FROM creator_pending WHERE user_id = ? AND platform = ?", (user_id, platform))
            return MESSAGES["expired"]
        if platform == "twitch" and not self.twitch_creds:
            return MESSAGES["twitch_off"]
        wait = C.cooldown_left(self.last_verify.get(user_id), t)
        if wait:
            return MESSAGES["cooldown"].format(seconds=wait)
        self.last_verify[user_id] = t
        handle = pending["handle"]
        try:
            if platform == "youtube":
                texts, seen = await self.youtube_proof(pending["external_id"])
            else:
                texts, seen, login = await self.twitch_proof(pending["external_id"])
                handle = login or handle  # the login may have changed since /creator link
        except NotFound:
            return MESSAGES["not_found"]
        except Unreachable as exc:
            log.info("creators: %s verify check failed: %s", platform, exc)
            return MESSAGES["unreachable"].format(platform=name)
        if not C.code_in(pending["code"], texts):
            return MESSAGES["no_code"].format(code=pending["code"], where=WHERE[platform], platform=name)
        if not await self.promote(user_id, platform, handle, pending["external_id"], seen):
            return MESSAGES["taken"]
        return (f"Verified! Your {name} is linked and new {WHAT[platform]} will show up in "
                f"{config.CREATORS_CHANNEL}. You can remove the code now.")

    @creator.command(name="verify", description="Check for your code in your channel description or Twitch bio")
    @app_commands.describe(platform="The platform you ran /creator link for")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def verify(self, interaction: discord.Interaction, platform: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self.verify_claim(interaction.user.id, platform.value)
        except Exception:
            log.exception("creators: verify failed")
            text = MESSAGES["unreachable"].format(platform=platform.name)
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /creator approve (staff)
    async def mod_log(self, guild, line: str) -> None:
        channel = config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL) if guild is not None else None
        if channel is None:
            log.info("creators: no %s channel, approval not logged there", config.MOD_LOG_CHANNEL)
            return
        try:
            await channel.send(embed=style.embed(description=line), allowed_mentions=NO_PINGS)
        except Exception:
            log.warning("creators: couldn't write to the mod log", exc_info=True)

    @creator.command(name="approve", description="Verify a member's creator link by hand (mods)")
    @app_commands.describe(member="Whose link", platform="Which platform")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def approve(self, interaction: discord.Interaction, member: discord.Member,
                      platform: app_commands.Choice[str]) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only mods can approve creator links.", ephemeral=True)
            return
        pending = await self.pending_of(member.id, platform.value)
        current = (await self.links_of(member.id)).get(platform.value)
        source = pending or (current if current and not current["verified"] else None)
        if source is None:
            text = (f"{member.mention}'s {platform.name} link is already verified." if current else
                    f"{member.mention} has no {platform.name} link waiting.")
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            seen = await self.current_items(platform.value, source["external_id"])
        except (Unreachable, NotFound) as exc:
            log.info("creators: approve seeding failed: %s", type(exc).__name__)
            await interaction.followup.send(MESSAGES["unreachable"].format(platform=platform.name), ephemeral=True)
            return
        if not await self.promote(member.id, platform.value, source["handle"], source["external_id"], seen):
            await interaction.followup.send("Someone else already verified that channel. `/creator remove` theirs "
                                            "first if it isn't really theirs.", ephemeral=True)
            return
        row = {"platform": platform.value, "handle": source["handle"], "external_id": source["external_id"]}
        await self.mod_log(interaction.guild, f"✅ {interaction.user.mention} approved {member.mention}'s creator link · "
                                              f"{self.describe(row)}")
        await interaction.followup.send(f"Approved {member.mention}'s {platform.name} link.", ephemeral=True,
                                        allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ unlink, list, remove
    @creator.command(name="unlink", description="Stop spotlighting one of your channels")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def unlink(self, interaction: discord.Interaction, platform: app_commands.Choice[str]) -> None:
        uid = interaction.user.id
        n = await self.db.execute("DELETE FROM creators WHERE user_id = ? AND platform = ?", (uid, platform.value))
        n += await self.db.execute("DELETE FROM creator_pending WHERE user_id = ? AND platform = ?",
                                   (uid, platform.value))
        text = f"Unlinked your {platform.name}." if n else f"You haven't linked a {platform.name} channel."
        await interaction.response.send_message(text, ephemeral=True)

    def describe(self, row) -> str:
        if row["platform"] == "youtube":
            return f"YouTube: <{C.channel_url(row['external_id'])}> ({esc(row['handle'])})"
        return f"Twitch: <{C.twitch_url(row['handle'])}>"

    async def own_notes(self, user_id: int) -> list[str]:
        """What the viewer still has to do: unverified old links and waiting claims."""
        notes = []
        for r in await self.db.fetchall("SELECT platform FROM creators WHERE user_id = ? AND verified = 0"
                                        " ORDER BY platform", (user_id,)):
            notes.append(f"Your {NAMES.get(r['platform'], r['platform'])} link isn't verified yet, so it isn't "
                         "announced. Run `/creator link` with the same channel to get a code.")
        t = now()
        for r in await self.db.fetchall("SELECT platform, code, created_at FROM creator_pending WHERE user_id = ?"
                                        " ORDER BY platform", (user_id,)):
            if r["platform"] in WHERE and not C.expired(r["created_at"], t):
                notes.append(f"Your {NAMES[r['platform']]} link is waiting: put `{r['code']}` in your "
                             f"{WHERE[r['platform']]} and run `/creator verify`.")
        return notes

    @creator.command(name="list", description="Everyone here who streams or uploads")
    async def list_(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        rows = await self.db.fetchall("SELECT * FROM creators WHERE verified = 1 ORDER BY added_at, user_id, platform")
        lines = [f"<@{r['user_id']}> · {self.describe(r)}" for r in rows
                 if guild is None or guild.get_member(r["user_id"]) is not None]
        if not lines:
            text = "Nobody has linked a channel yet. Be the first with `/creator link`."
        else:
            text, shown = "", 0
            for line in lines:
                if len(text) + len(line) + 1 > 3400:
                    break
                text += line + "\n"
                shown += 1
            if shown < len(lines):
                text += f"…and {len(lines) - shown} more"
        notes = await self.own_notes(interaction.user.id)
        if notes:
            text = text.strip() + "\n\n" + "\n".join(notes)
        embed = style.embed(title="Creators", description=text.strip(), footer=style.label("creators", "/creator link"))
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

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
            await self.db.execute("DELETE FROM creator_pending WHERE user_id = ?", (member.id,))
        else:
            n = await self.db.execute("DELETE FROM creators WHERE user_id = ? AND platform = ?",
                                      (member.id, platform.value))
            await self.db.execute("DELETE FROM creator_pending WHERE user_id = ? AND platform = ?",
                                  (member.id, platform.value))
        text = f"Removed {n} link{'s' if n != 1 else ''} for {member.mention}." if n else "Nothing to remove."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ announcing
    def target(self, guild):
        channel = config.match_by_name(guild.text_channels, config.CREATORS_CHANNEL)
        if channel is None:
            self.warn_once("channel", "creators: no %s channel, not announcing", config.CREATORS_CHANNEL)
        return channel

    async def post(self, channel, content: str, embed: discord.Embed) -> None:
        try:
            await channel.send(content, embed=embed, allowed_mentions=NO_PINGS)
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

    # ------------------------------------------------------------ polling (verified links only)
    async def poll_youtube(self) -> int:
        """Check every verified channel once; returns announcements made."""
        guild = self.guild()
        channel = self.target(guild) if guild is not None else None
        if channel is None:
            return 0
        posted = 0
        rows = await self.db.fetchall("SELECT * FROM creators WHERE platform = 'youtube' AND verified = 1"
                                      " ORDER BY user_id")
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
        rows = await self.db.fetchall("SELECT * FROM creators WHERE platform = 'twitch' AND verified = 1")
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

    @tasks.loop(hours=1)
    async def cleanup_loop(self) -> None:
        try:
            n = await self.clean_pending()
            if n:
                log.info("creators: dropped %d expired claim(s)", n)
            t = now()
            self.last_verify = {u: at for u, at in self.last_verify.items() if C.cooldown_left(at, t)}
        except Exception:
            log.exception("creators: pending cleanup failed")

    @cleanup_loop.before_loop
    async def before_cleanup(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Creators(bot))
