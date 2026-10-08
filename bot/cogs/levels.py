"""Levels: members earn XP by chatting (15-25 per message, once a minute, outside staff,
counting and bot-commands) and by hanging out in voice (10 XP per counted minute: 2+
people, not AFK, from the stats module's voice sessions). Levels follow MEE6's curve; the
LEVEL_ROLES reward role for the highest threshold reached is given (and lower ones taken
away). /rank draws a rank card, /levels shows the top 10, and staff have /xp set|reset.

Voice XP is credited by a sweep every 10 minutes against xp.voice_seconds_counted, so a
restart or a missed sweep never pays a minute twice. The first sweep credits past voice
time quietly (roles yes, posts no). Opted-out members (/privacy off) earn nothing and are
never shown.

Needs Message Content (message length) and Server Members (role changes, the member list
behind /levels and the voice sweep). The bot needs Manage Roles, with the level roles
below its own role."""

import asyncio
import functools
import io
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from cogs.lfg import ping_only
from logic import community as community_rules
from logic import levels as L
from logic import stats as S
from logic.selfroles import has_dangerous_permissions

log = logging.getLogger(__name__)

BACKFILL_KEY = "levels:voice_backfilled"  # meta: set once the first (quiet) voice sweep is done
AVATAR_TIMEOUT = 10
FILENAME = "rank.png"
MAX_SET = 10_000_000
NO_PINGS = discord.AllowedMentions.none()
# Never earn XP here: counting would break, bot-commands is for commands.
NO_XP_CHANNELS = (config.COUNTING_CHANNEL, config.BOT_COMMANDS_CHANNEL)


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("levels: %s failed", fn.__name__)
    return wrapper


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    perms = getattr(user, "guild_permissions", None)
    return community_rules.can_handle(guild.owner_id == user.id, bool(getattr(perms, "administrator", False)),
                                      getattr(user, "roles", []))


def esc(text) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text or "")))


class Levels(commands.Cog):
    xp = app_commands.Group(name="xp", description="Staff: adjust a member's XP", guild_only=True,
                            default_permissions=discord.Permissions(moderate_members=True))

    def __init__(self, bot, rng: random.Random | None = None):
        self.bot = bot
        self.rng = rng or random.Random()
        self.limiter = L.Limiter()
        self.sweep_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.voice_sweep.start()

    async def cog_unload(self) -> None:
        self.voice_sweep.cancel()

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def ours(self, guild) -> bool:
        return guild is not None and guild.id == self.bot.settings.guild_id

    @staticmethod
    def earns_in(channel) -> bool:
        """Text channels (and their threads) outside staff, counting and bot-commands."""
        if channel is None:
            return False
        parent = getattr(channel, "parent", None) or channel
        category = getattr(parent, "category", None)
        if category is not None and config.match_by_name([category], config.STAFF_CATEGORY) is not None:
            return False
        return not any(config.match_by_name([parent], name) is not None for name in NO_XP_CHANNELS)

    async def optouts(self, tx=None) -> set[int]:
        rows = await (tx or self.db).fetchall("SELECT user_id FROM privacy_optout")
        return {r["user_id"] for r in rows}

    # ------------------------------------------------------------ chat XP
    async def add_message_xp(self, user_id: int, t: int) -> tuple[int, int] | None:
        """Credit one message; (old level, new level), or None if nothing was earned."""
        async with self.db.transaction() as tx:
            # The opt-out check is part of the write, like the stats recorders.
            if await tx.fetchone("SELECT 1 FROM privacy_optout WHERE user_id = ?", (user_id,)):
                return None
            row = await tx.fetchone("SELECT xp, level, last_msg_at FROM xp WHERE user_id = ?", (user_id,))
            if row is not None and not L.off_cooldown(row["last_msg_at"], t):
                return None
            old_xp, old_level = (row["xp"], row["level"]) if row else (0, 0)
            new_xp = old_xp + L.message_xp(self.rng)
            new_level = L.level_for(new_xp)
            await tx.execute(
                "INSERT INTO xp (user_id, xp, level, last_msg_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level,"
                " last_msg_at = excluded.last_msg_at",
                (user_id, new_xp, new_level, t))
        return old_level, new_level

    @commands.Cog.listener()
    @never_raise
    async def on_message(self, message) -> None:
        author = message.author
        if author.bot or not self.ours(message.guild):
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return  # join notices, boosts, pins
        if not self.earns_in(message.channel) or not L.long_enough(message.content):
            return
        result = await self.add_message_xp(author.id, now())
        if result is not None and result[1] > result[0] and hasattr(author, "roles"):
            await self.level_up(author, result[1], message.channel)

    # ------------------------------------------------------------ level ups
    def manageable(self, guild, role) -> bool:
        me = getattr(guild, "me", None)
        top = getattr(me, "top_role", None)
        return top is not None and top.position > role.position and not getattr(role, "managed", False)

    async def sync_roles(self, member, level: int) -> str | None:
        """Give the reward role for `level` and take lower/higher level roles away.
        Returns the name of a role that was newly added, if any. Never raises."""
        guild = member.guild
        add_name, remove_names = L.role_changes(level, [r.name for r in member.roles])
        added = None
        try:
            removing = [r for r in member.roles if r.name in remove_names]
            keep = [r for r in removing if not self.manageable(guild, r)]
            for r in keep:
                log.warning("levels: can't remove %s from %s: it isn't below the bot's role", r.name, member.id)
            removing = [r for r in removing if r not in keep]
            if removing:
                await member.remove_roles(*removing, reason=f"Levels: now level {level}")
            if add_name is not None:
                role = config.match_by_name(guild.roles, add_name)
                if role is None:
                    log.info("levels: no %s role on the server; skipped it for %s", add_name, member.id)
                elif not self.manageable(guild, role):
                    log.warning("levels: can't give %s: it isn't below the bot's role", role.name)
                elif has_dangerous_permissions(role):
                    log.warning("levels: won't give %s: it has moderator permissions", role.name)
                else:
                    await member.add_roles(role, reason=f"Levels: reached level {level}")
                    added = role.name
        except Exception:
            log.exception("levels: updating level roles for %s failed", member.id)
        return added

    async def level_up(self, member, level: int, channel, announce: bool = True) -> bool:
        """Roles, then (rate-limited) one short post. True if posted."""
        added = await self.sync_roles(member, level)
        # Levels below the first reward role (1-4 come within a newcomer's first hour) stay quiet.
        if L.reward_role(level) is None:
            return False
        if not announce or channel is None or not self.limiter.allow(member.id, channel.id, now()):
            return False
        try:
            await channel.send(L.level_up_text(member.mention, level, added),
                               allowed_mentions=ping_only(users=[member]))
            return True
        except Exception:
            log.exception("levels: announcing %s's level %s failed", member.id, level)
            return False

    # ------------------------------------------------------------ voice XP
    @tasks.loop(minutes=10)
    async def voice_sweep(self) -> None:
        try:
            await self.run_voice_sweep()
        except Exception:
            log.exception("levels voice sweep failed")

    @voice_sweep.before_loop
    async def before_voice_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def run_voice_sweep(self) -> int:
        """Credit new counted voice minutes; returns how many members levelled up."""
        guild = self.guild()
        if guild is None:
            return 0
        async with self.sweep_lock:
            t = now()
            rows = await self.db.fetchall('SELECT user_id, channel_id, start, "end" FROM voice_sessions')
            afk = getattr(guild, "afk_channel", None)
            excluded = {afk.id} if afk is not None else set()
            sessions = [S.Session(r["user_id"], r["channel_id"], r["start"], r["end"]) for r in rows]
            # Sorting every join/leave is CPU work: keep it off the event loop.
            totals = await asyncio.to_thread(S.counted_voice_seconds, sessions, 0, t, t,
                                             excluded_channels=excluded)
            first = await self.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (BACKFILL_KEY,)) is None
            leveled: list[tuple[int, int]] = []
            async with self.db.transaction() as tx:
                skip = await self.optouts(tx)
                # Their sessions are deleted by /privacy off, so their baseline starts over.
                await tx.execute("UPDATE xp SET voice_seconds_counted = 0 WHERE voice_seconds_counted != 0"
                                 " AND user_id IN (SELECT user_id FROM privacy_optout)")
                existing = {r["user_id"]: r for r in await tx.fetchall(
                    "SELECT user_id, xp, level, voice_seconds_counted FROM xp")}
                candidates = (set(totals) | {u for u, r in existing.items() if r["voice_seconds_counted"]}) - skip
                for uid in sorted(candidates):
                    row = existing.get(uid)
                    old_xp, old_level, counted = (row["xp"], row["level"], row["voice_seconds_counted"]) if row \
                        else (0, 0, 0)
                    gain, new_counted = L.voice_credit(totals.get(uid, 0), counted)
                    if gain == 0 and new_counted == counted:
                        continue
                    new_xp = old_xp + gain
                    new_level = L.level_for(new_xp)
                    await tx.execute(
                        "INSERT INTO xp (user_id, xp, level, voice_seconds_counted) VALUES (?, ?, ?, ?)"
                        " ON CONFLICT (user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level,"
                        " voice_seconds_counted = excluded.voice_seconds_counted",
                        (uid, new_xp, new_level, new_counted))
                    if new_level > old_level:
                        leveled.append((uid, new_level))
                if first:
                    await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (BACKFILL_KEY, str(t)))
        channel = config.match_by_name(guild.text_channels, config.BOT_COMMANDS_CHANNEL)
        count = 0
        for uid, level in leveled:
            member = guild.get_member(uid)
            if member is None or member.bot:
                continue
            count += 1
            await self.level_up(member, level, channel, announce=not first)
        if first:
            log.info("levels: first voice sweep done, past voice time credited quietly (%d levelled)", count)
        return count

    # ------------------------------------------------------------ reading
    async def scores(self) -> dict[int, int]:
        """XP of current members who haven't opted out."""
        rows = await self.db.fetchall(
            "SELECT user_id, xp FROM xp WHERE xp > 0 AND user_id NOT IN (SELECT user_id FROM privacy_optout)")
        guild = self.guild()
        out = {}
        for r in rows:
            member = guild.get_member(r["user_id"]) if guild else None
            if member is not None and not member.bot:
                out[r["user_id"]] = r["xp"]
        return out

    async def xp_of(self, user_id: int) -> int:
        row = await self.db.fetchone("SELECT xp FROM xp WHERE user_id = ?", (user_id,))
        return row["xp"] if row else 0

    async def avatar_bytes(self, member) -> bytes | None:
        try:
            asset = member.display_avatar.replace(size=256, format="png")
            return await asyncio.wait_for(asset.read(), AVATAR_TIMEOUT)
        except Exception:
            log.info("levels: no avatar for %s; drawing the initial instead", member.id)
            return None

    # ------------------------------------------------------------ /rank
    @app_commands.command(name="rank", description="Your level card (or someone else's)")
    @app_commands.describe(member="Whose card (default: you)")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        if member.bot:
            await interaction.response.send_message("Bots don't level up.", ephemeral=True)
            return
        if not await self.db.tracking_allowed(member.id):
            who = "You have" if member.id == interaction.user.id else f"{esc(member.display_name)} has"
            await interaction.response.send_message(f"{who} stats turned off, so there's no level to show.",
                                                    ephemeral=True, allowed_mentions=NO_PINGS)
            return
        await interaction.response.defer(thinking=True)
        total = await self.xp_of(member.id)
        level, into, needed = L.progress(total)
        position = L.rank_position(await self.scores(), member.id)
        avatar = await self.avatar_bytes(member)
        png = await asyncio.to_thread(L.render_rank_card, member.display_name, member.name, avatar,
                                      level=level, rank=position, into=into, needed=needed, total=total)
        await interaction.followup.send(file=discord.File(io.BytesIO(png), filename=FILENAME),
                                        allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /levels
    @app_commands.command(name="levels", description="The top 10 by level")
    @app_commands.guild_only()
    async def levels(self, interaction: discord.Interaction) -> None:
        scores = await self.scores()
        board = L.top(scores, 10)
        if not board:
            text = "Nobody has XP yet. Chat or hang out in voice to get on the board."
        else:
            rows = [f"`{r:02}`  <@{uid}>  Lv {L.level_for(xp)} · {xp:,} XP" for r, uid, xp in board]
            me = interaction.user.id
            mine = L.rank_position(scores, me)
            if mine is not None and all(uid != me for _, uid, _ in board):
                rows += ["…", f"`{mine:02}`  <@{me}>  Lv {L.level_for(scores[me])} · {scores[me]:,} XP"]
            text = "\n".join(rows)
        embed = style.embed(title="Levels · top 10", description=text,
                            footer=style.label("levels", "chat and voice XP"))
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /xp (staff)
    async def mod_log(self, guild, line: str) -> None:
        channel = config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL)
        if channel is None:
            return
        try:
            await channel.send(embed=style.embed(description=line), allowed_mentions=NO_PINGS)
        except Exception:
            log.warning("levels: couldn't write to the mod log", exc_info=True)

    async def set_xp(self, member, amount: int) -> tuple[int, int]:
        """Set a member's XP (voice baseline and chat cooldown kept): (old XP, new level)."""
        level = L.level_for(amount)
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT xp FROM xp WHERE user_id = ?", (member.id,))
            await tx.execute(
                "INSERT INTO xp (user_id, xp, level) VALUES (?, ?, ?)"
                " ON CONFLICT (user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level",
                (member.id, amount, level))
        return (row["xp"] if row else 0), level

    async def staff_target(self, interaction, member) -> bool:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can change XP.", ephemeral=True)
            return False
        if member.bot:
            await interaction.response.send_message("Bots don't level up.", ephemeral=True)
            return False
        return True

    @xp.command(name="set", description="Staff: set a member's XP")
    @app_commands.describe(member="Whose XP", amount="New total XP")
    async def xp_set(self, interaction: discord.Interaction, member: discord.Member,
                     amount: app_commands.Range[int, 0, MAX_SET]) -> None:
        if not await self.staff_target(interaction, member):
            return
        amount = max(0, min(int(amount), MAX_SET))
        old, level = await self.set_xp(member, amount)
        # Reply first (3 s limit), then the slower role edits and the log line.
        await interaction.response.send_message(f"{member.mention} now has {amount:,} XP (level {level}).",
                                                ephemeral=True, allowed_mentions=NO_PINGS)
        await self.sync_roles(member, level)
        await self.mod_log(interaction.guild, f"🎚️ {interaction.user.mention} set {member.mention}'s XP to "
                                              f"{amount:,} (level {level}, was {old:,})")

    @xp.command(name="reset", description="Staff: reset a member's XP to zero")
    @app_commands.describe(member="Whose XP")
    async def xp_reset(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not await self.staff_target(interaction, member):
            return
        old, level = await self.set_xp(member, 0)
        await interaction.response.send_message(f"{member.mention}'s XP is reset to 0.",
                                                ephemeral=True, allowed_mentions=NO_PINGS)
        await self.sync_roles(member, level)
        await self.mod_log(interaction.guild, f"🎚️ {interaction.user.mention} reset {member.mention}'s XP "
                                              f"(was {old:,})")


async def setup(bot) -> None:
    await bot.add_cog(Levels(bot))
