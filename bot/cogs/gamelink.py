"""Linked game accounts: /link steam, /link verify, /unlink, /accounts, and a "Game
accounts" section other cogs can show (`profile_lines`).

Linking is opt-in. /link steam reads the member's public Steam community profile (no API
key) and stores a pending claim with a code (FD-XXXXXX). The member puts the code in their
Steam profile summary and runs /link verify within 24 hours; staff can /link approve
instead (logged to the mod log). A Steam account can be verified by one member only.
Verified accounts are re-read at most once a day on a slow loop for the persona name and
the games Steam lists; after three failed reads in a row an account is left alone until
its member runs /link steam again. /unlink deletes the row.

Every Steam URL fetched or shown is rebuilt from a validated id, redirects are followed
only to another Steam profile, replies are capped at 512 KB and 10 s, and persona and game
names are shown escaped with no pings."""

import logging
import time
from typing import Protocol

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from logic import community as community_rules
from logic import gamelink as G

log = logging.getLogger(__name__)

USER_AGENT = "FrontDesk Discord bot"
NO_PINGS = discord.AllowedMentions.none()
PLATFORM_CHOICES = [app_commands.Choice(name=G.NAMES[p], value=p) for p in G.PLATFORMS]
WHERE = "Steam profile summary (Edit Profile > Summary)"

MESSAGES = {
    "input": ("That doesn't look like a Steam profile. Paste your profile link "
              "(`https://steamcommunity.com/id/yourname` or `https://steamcommunity.com/profiles/7656...`), "
              "your custom URL name or your 17-digit SteamID64."),
    "not_found": "I couldn't find that Steam profile. Check the link and try again.",
    "unreachable": "I couldn't reach Steam just now. Try again in a few minutes.",
    "taken": "Someone here already linked that Steam account. If it's yours, ask a mod.",
    "no_pending": "You have no Steam link waiting. Start with `/link steam`.",
    "already": "Your Steam account is already linked and verified.",
    "expired": "Your code expired (codes last 24 hours). Run `/link steam` again for a new one.",
    "cooldown": "One check a minute, please. Try again in {seconds} s.",
    "private": ("Your Steam profile isn't public, so I can't read its summary. Set **My profile** to Public "
                "(Edit Profile > Privacy Settings), save `{code}` in your summary, then run `/link verify` again. "
                "You can set it back afterwards."),
    "no_code": ("I couldn't find `{code}` in your " + WHERE + " yet. Save it there, give Steam a minute to "
                "update, then run `/link verify` again."),
    "none": "No game accounts linked yet. `/link steam` adds your Steam.",
}


def now() -> int:
    return int(time.time())


class Http(Protocol):
    """The network seam. Tests pass a fake; nothing here touches the network directly."""

    async def get(self, url: str, *, max_bytes: int = G.MAX_BODY) -> tuple[int, bytes, bool, str | None]: ...


class AiohttpHttp:
    """One GET with a 10 s limit that never follows a redirect by itself: returns (status,
    body cut at `max_bytes`, whether it was cut, the Location header of a redirect)."""

    async def get(self, url, *, max_bytes=G.MAX_BODY):
        timeout = aiohttp.ClientTimeout(total=G.FETCH_TIMEOUT)
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=False) as resp:
                if 300 <= resp.status < 400:
                    return resp.status, b"", False, resp.headers.get("Location")
                body = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    body += chunk
                    if len(body) > max_bytes:
                        return resp.status, bytes(body[:max_bytes]), True, None
                return resp.status, bytes(body), False, None


class Unreachable(Exception):
    """Steam didn't answer usefully (timeout, 5xx, an odd redirect, garbage)."""


class NotFound(Exception):
    pass


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    return community_rules.can_handle(guild.owner_id == user.id, user.guild_permissions.administrator, user.roles)


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))


class GameLink(commands.Cog):
    link = app_commands.Group(name="link", description="Link a game account to your profile", guild_only=True)

    def __init__(self, bot, http: Http | None = None):
        self.bot = bot
        self.http = http or AiohttpHttp()
        # user id -> time of their last /link verify check. A rate limit, so it may reset on
        # restart; everything that must last is in SQLite.
        self.last_verify: dict[int, int] = {}

    async def cog_load(self) -> None:
        self.upkeep_loop.start()

    async def cog_unload(self) -> None:
        self.upkeep_loop.cancel()

    @property
    def db(self):
        return self.bot.db

    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    # ------------------------------------------------------------ Steam I/O
    async def fetch_profile(self, url: str) -> G.Profile:
        """The profile behind one of logic.gamelink's XML URLs. A redirect is followed only
        when logic.gamelink.redirect_target() accepts it, at most MAX_REDIRECTS times."""
        for _ in range(G.MAX_REDIRECTS + 1):
            try:
                status, body, cut, location = await self.http.get(url, max_bytes=G.MAX_BODY)
            except Exception as exc:
                raise Unreachable(type(exc).__name__) from None
            if 300 <= status < 400:
                url = G.redirect_target(location)
                if url is None:
                    raise Unreachable("redirect off the profile")
                continue
            if status == 404:
                raise NotFound()
            if status != 200 or cut:
                raise Unreachable(f"profile status {status}{' (too large)' if cut else ''}")
            try:
                return G.parse_profile(body)
            except G.ProfileNotFound:
                raise NotFound() from None
            except ValueError as exc:
                raise Unreachable(str(exc)) from None
        raise Unreachable("too many redirects")

    async def fetch_by_id(self, steam_id: str) -> G.Profile:
        """The profile for a SteamID64; anything that isn't that account is Unreachable."""
        profile = await self.fetch_profile(G.id_xml_url(steam_id))
        if profile.steam_id != steam_id:
            raise Unreachable("another account answered")
        return profile

    # ------------------------------------------------------------ storage
    async def account_of(self, user_id: int, platform: str) -> dict | None:
        row = await self.db.fetchone("SELECT * FROM game_accounts WHERE user_id = ? AND platform = ?",
                                     (user_id, platform))
        return dict(row) if row else None

    async def owner_of(self, platform: str, account_id: str) -> int | None:
        row = await self.db.fetchone("SELECT user_id FROM game_accounts WHERE platform = ? AND account_id = ?"
                                     " AND verified = 1", (platform, account_id))
        return row["user_id"] if row else None

    async def pending_of(self, user_id: int, platform: str) -> dict | None:
        row = await self.db.fetchone("SELECT * FROM game_account_pending WHERE user_id = ? AND platform = ?",
                                     (user_id, platform))
        return dict(row) if row else None

    async def save_pending(self, user_id: int, platform: str, account_id: str, display: str) -> str:
        """Store a claim and return its code. Linking the same account again keeps a live code
        (and its 24 hours), so a member who already pasted it isn't sent back to change it."""
        t = now()
        old = await self.pending_of(user_id, platform)
        if old and old["account_id"] == account_id and not G.expired(old["created_at"], t):
            await self.db.execute("UPDATE game_account_pending SET display = ? WHERE user_id = ? AND platform = ?",
                                  (display, user_id, platform))
            return old["code"]
        code = G.new_code()
        await self.db.execute("INSERT OR REPLACE INTO game_account_pending (user_id, platform, account_id, display,"
                              " code, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                              (user_id, platform, account_id, display, code, t))
        return code

    async def promote(self, user_id: int, platform: str, profile: G.Profile) -> bool:
        """Make this the member's verified account for the platform and drop the claim, in
        one transaction. False if another member verified the account first."""
        t = now()
        async with self.db.transaction() as tx:
            other = await tx.fetchone("SELECT 1 FROM game_accounts WHERE platform = ? AND account_id = ?"
                                      " AND verified = 1 AND user_id != ?", (platform, profile.steam_id, user_id))
            if other is not None:
                return False
            await tx.execute("DELETE FROM game_accounts WHERE platform = ? AND account_id = ? AND user_id != ?",
                             (platform, profile.steam_id, user_id))
            await tx.execute("INSERT OR REPLACE INTO game_accounts (user_id, platform, account_id, display, verified,"
                             " data, linked_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                             (user_id, platform, profile.steam_id, profile.persona, G.fresh_data(profile), t, t))
            await tx.execute("DELETE FROM game_account_pending WHERE user_id = ? AND platform = ?",
                             (user_id, platform))
        return True

    async def store_refresh(self, row, profile: G.Profile) -> None:
        await self.db.execute("UPDATE game_accounts SET display = ?, data = ?, updated_at = ? WHERE user_id = ?"
                              " AND platform = ? AND account_id = ?",
                              (profile.persona, G.fresh_data(profile, row["data"]), now(), row["user_id"],
                               row["platform"], row["account_id"]))

    async def clean_pending(self) -> int:
        """Drop claims older than a day; returns how many."""
        return await self.db.execute("DELETE FROM game_account_pending WHERE created_at <= ?", (now() - G.CODE_TTL,))

    # ------------------------------------------------------------ the public section
    async def profile_lines(self, user_id: int) -> list[str]:
        """The "Game accounts" lines for a member's verified accounts (empty when they have
        linked nothing). Safe to put in an embed: names are escaped, links are rebuilt.
        Never raises, so another cog's /profile can call it without a guard."""
        try:
            rows = await self.db.fetchall("SELECT platform, account_id, data FROM game_accounts WHERE user_id = ?"
                                          " AND verified = 1 ORDER BY platform", (user_id,))
            lines: list[str] = []
            for row in rows:
                lines += G.account_lines(row["platform"], row["account_id"], row["data"], esc)
            return lines
        except Exception:
            log.exception("gamelink: building profile lines failed")
            return []

    async def own_notes(self, user_id: int) -> list[str]:
        """What the viewer still has to do: waiting claims and paused refreshes."""
        notes = []
        t = now()
        for r in await self.db.fetchall("SELECT platform, code, created_at FROM game_account_pending WHERE user_id = ?"
                                        " ORDER BY platform", (user_id,)):
            if r["platform"] in G.NAMES and not G.expired(r["created_at"], t):
                notes.append(G.pending_line(r["platform"], r["code"]))
        for r in await self.db.fetchall("SELECT platform, data FROM game_accounts WHERE user_id = ? AND verified = 1"
                                        " ORDER BY platform", (user_id,)):
            if r["platform"] in G.NAMES and G.gave_up(r["data"]):
                notes.append(f"I couldn't read your {G.NAMES[r['platform']]} profile a few times in a row, so it "
                             f"stopped updating. Run `/link {r['platform']}` again to restart it.")
        return notes

    # ------------------------------------------------------------ /link steam
    async def link_steam(self, user_id: int, text: str) -> str:
        ref = G.parse_steam(text)
        if ref is None:
            return MESSAGES["input"]
        try:
            profile = await self.fetch_profile(G.xml_url(ref))
            if ref.kind == "id" and profile.steam_id != ref.value:
                raise Unreachable("another account answered")
        except NotFound:
            return MESSAGES["not_found"]
        except Unreachable as exc:
            log.info("gamelink: steam link check failed: %s", exc)
            return MESSAGES["unreachable"]
        owner = await self.owner_of("steam", profile.steam_id)
        if owner is not None and owner != user_id:
            return MESSAGES["taken"]
        if owner == user_id:
            # linking again is how a member restarts a paused refresh
            row = await self.account_of(user_id, "steam")
            await self.store_refresh(row, profile)
            return MESSAGES["already"] + " I just refreshed it."
        code = await self.save_pending(user_id, "steam", profile.steam_id, profile.persona)
        shown = f"**{esc(G.safe_persona(profile.persona))}** (<{G.profile_url(profile.steam_id)}>)"
        text = (f"Found {shown}. To show it's yours, put `{code}` anywhere in your {WHERE}, then run "
                f"`/link verify` within 24 hours. Once verified you can remove the code.")
        if not profile.public:
            text += ("\nYour profile isn't public right now: set **My profile** to Public until you've verified, "
                     "or I can't see the summary. Hours only show while **Game details** is Public too.")
        return text

    @link.command(name="steam", description="Link your Steam account to your profile")
    @app_commands.describe(profile="Your Steam profile link, custom URL name or SteamID64")
    async def steam(self, interaction: discord.Interaction,
                    profile: app_commands.Range[str, 1, G.MAX_INPUT]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self.link_steam(interaction.user.id, profile)
        except Exception:
            log.exception("gamelink: link failed")
            text = MESSAGES["unreachable"]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /link verify
    async def verify_claim(self, user_id: int, platform: str) -> str:
        pending = await self.pending_of(user_id, platform)
        if pending is None:
            current = await self.account_of(user_id, platform)
            return MESSAGES["already" if current and current["verified"] else "no_pending"]
        t = now()
        if G.expired(pending["created_at"], t):
            await self.db.execute("DELETE FROM game_account_pending WHERE user_id = ? AND platform = ?",
                                  (user_id, platform))
            return MESSAGES["expired"]
        wait = G.cooldown_left(self.last_verify.get(user_id), t)
        if wait:
            return MESSAGES["cooldown"].format(seconds=wait)
        self.last_verify[user_id] = t
        try:
            profile = await self.fetch_by_id(pending["account_id"])
        except NotFound:
            return MESSAGES["not_found"]
        except Unreachable as exc:
            log.info("gamelink: steam verify check failed: %s", exc)
            return MESSAGES["unreachable"]
        if not profile.public:
            return MESSAGES["private"].format(code=pending["code"])
        if not G.code_in(pending["code"], [profile.summary]):
            return MESSAGES["no_code"].format(code=pending["code"])
        if not await self.promote(user_id, platform, profile):
            return MESSAGES["taken"]
        return ("Verified! Your Steam account is linked and shows on your `/profile`. You can remove the code now. "
                "`/unlink` removes it again any time.")

    @link.command(name="verify", description="Check for your code in your Steam profile summary")
    @app_commands.describe(platform="The platform you linked")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def verify(self, interaction: discord.Interaction, platform: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self.verify_claim(interaction.user.id, platform.value)
        except Exception:
            log.exception("gamelink: verify failed")
            text = MESSAGES["unreachable"]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ staff: approve, remove
    async def mod_log(self, guild, line: str) -> None:
        channel = config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL) if guild is not None else None
        if channel is None:
            log.info("gamelink: no %s channel, not logged there", config.MOD_LOG_CHANNEL)
            return
        try:
            await channel.send(embed=style.embed(description=line), allowed_mentions=NO_PINGS)
        except Exception:
            log.warning("gamelink: couldn't write to the mod log", exc_info=True)

    @link.command(name="approve", description="Verify a member's game account by hand (mods)")
    @app_commands.describe(member="Whose account", platform="Which platform")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def approve(self, interaction: discord.Interaction, member: discord.Member,
                      platform: app_commands.Choice[str]) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only mods can approve game accounts.", ephemeral=True)
            return
        pending = await self.pending_of(member.id, platform.value)
        if pending is None:
            current = await self.account_of(member.id, platform.value)
            text = (f"{member.mention}'s {platform.name} account is already verified." if current else
                    f"{member.mention} has no {platform.name} link waiting. They start with `/link steam`.")
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            profile = await self.fetch_by_id(pending["account_id"])
        except (Unreachable, NotFound) as exc:
            log.info("gamelink: approve read failed: %s", type(exc).__name__)
            await interaction.followup.send(MESSAGES["unreachable"], ephemeral=True)
            return
        except Exception:
            log.exception("gamelink: approve failed")
            await interaction.followup.send(MESSAGES["unreachable"], ephemeral=True)
            return
        if not await self.promote(member.id, platform.value, profile):
            await interaction.followup.send("Someone else already verified that account. `/link remove` theirs "
                                            "first if it isn't really theirs.", ephemeral=True)
            return
        await self.mod_log(interaction.guild, f"✅ {interaction.user.mention} approved {member.mention}'s game account"
                                              f" · {platform.name}: <{G.profile_url(profile.steam_id)}>")
        await interaction.followup.send(f"Approved {member.mention}'s {platform.name} account.", ephemeral=True,
                                        allowed_mentions=NO_PINGS)

    @link.command(name="remove", description="Remove a member's linked game account (mods)")
    @app_commands.describe(member="Whose account", platform="Which platform")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    async def remove(self, interaction: discord.Interaction, member: discord.Member,
                     platform: app_commands.Choice[str]) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only mods can remove someone else's account.", ephemeral=True)
            return
        n = await self.forget(member.id, platform.value)
        if n:
            await self.mod_log(interaction.guild, f"🗑️ {interaction.user.mention} removed {member.mention}'s "
                                                  f"{platform.name} game account")
        text = f"Removed {member.mention}'s {platform.name} account." if n else "Nothing to remove."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /unlink, /accounts
    async def forget(self, user_id: int, platform: str) -> int:
        """Delete the member's account row and any waiting claim; returns rows deleted."""
        n = await self.db.execute("DELETE FROM game_accounts WHERE user_id = ? AND platform = ?", (user_id, platform))
        n += await self.db.execute("DELETE FROM game_account_pending WHERE user_id = ? AND platform = ?",
                                   (user_id, platform))
        return n

    @app_commands.command(name="unlink", description="Remove a linked game account from your profile")
    @app_commands.describe(platform="Which account")
    @app_commands.choices(platform=PLATFORM_CHOICES)
    @app_commands.guild_only()
    async def unlink(self, interaction: discord.Interaction, platform: app_commands.Choice[str]) -> None:
        n = await self.forget(interaction.user.id, platform.value)
        self.last_verify.pop(interaction.user.id, None)
        text = (f"Unlinked your {platform.name} account. Everything I stored about it is deleted." if n else
                f"You haven't linked a {platform.name} account.")
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="accounts", description="Linked game accounts for you or someone else")
    @app_commands.describe(member="Whose accounts (default: you)")
    @app_commands.guild_only()
    async def accounts(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        own = member.id == interaction.user.id
        lines = [] if getattr(member, "bot", False) else await self.profile_lines(member.id)
        if lines:
            text = "\n".join(lines)
        elif own:
            text = MESSAGES["none"]
        else:
            text = f"{member.mention} hasn't linked a game account."
        if own:
            notes = await self.own_notes(member.id)
            if notes:
                text += "\n\n" + "\n".join(notes)
        name = discord.utils.escape_markdown(getattr(member, "display_name", None) or "member")
        embed = style.embed(title=f"Game accounts · {name}"[:256], description=text[:4000],
                            footer=style.label("game accounts", "/link steam"))
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ daily refresh
    async def refresh_due(self) -> int:
        """Re-read up to REFRESH_BATCH verified accounts that are due (oldest first);
        returns how many were updated. Members who left are skipped, not deleted."""
        guild = self.guild()
        if guild is None:
            return 0
        t = now()
        rows = await self.db.fetchall("SELECT * FROM game_accounts WHERE platform = 'steam' AND verified = 1"
                                      " AND updated_at <= ? ORDER BY updated_at, user_id", (t - G.REFRESH_EVERY,))
        due = [dict(r) for r in rows if guild.get_member(r["user_id"]) is not None
               and G.due(r["data"], r["updated_at"], t)][:G.REFRESH_BATCH]
        done = 0
        for row in due:
            try:
                profile = await self.fetch_by_id(row["account_id"])
            except Exception as exc:
                if not isinstance(exc, (Unreachable, NotFound)):
                    log.exception("gamelink: refresh failed")
                data = G.failed_data(row["data"], now())
                await self.db.execute("UPDATE game_accounts SET data = ? WHERE user_id = ? AND platform = ?"
                                      " AND account_id = ?", (data, row["user_id"], row["platform"], row["account_id"]))
                if G.gave_up(data):
                    log.info("gamelink: an account failed %d refreshes in a row, paused until re-linked", G.MAX_FAILS)
                continue
            await self.store_refresh(row, profile)
            done += 1
        return done

    @tasks.loop(minutes=30)
    async def upkeep_loop(self) -> None:
        try:
            n = await self.clean_pending()
            if n:
                log.info("gamelink: dropped %d expired claim(s)", n)
            t = now()
            self.last_verify = {u: at for u, at in self.last_verify.items() if G.cooldown_left(at, t)}
            await self.refresh_due()
        except Exception:
            log.exception("gamelink: upkeep failed")

    @upkeep_loop.before_loop
    async def before_upkeep(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(GameLink(bot))
