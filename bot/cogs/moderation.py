"""Module 10: Moderation autopilot.

Staff commands /warn, /timeout, /untimeout, /cases and /purge, each stored as a case
and logged to the mod log. Warnings escalate on their own (3 in 30 days: 1 h timeout,
5: 24 h). Anti-spam times out message floods, repeats and mass mentions and deletes the
burst. Anti-raid raises the verification level and pauses invites for 30 minutes when
many accounts join at once, then puts everything back, even across a restart.

Needs Moderate Members (timeouts), Manage Messages (deleting) and Manage Server (raid
lockdown). Anti-raid needs the Server Members intent and duplicate detection needs
Message Content; without them the rest keeps working."""

import asyncio
import functools
import logging
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.enums import try_enum
from discord.ext import commands, tasks

import config
import style
from logic import moderation as M
from logic.moderation import SpamMessage

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
RAID_KEY = "moderation:raid"
STAFF_ONLY = "Only Moderators and Keepers can use this."
PERMISSION_NAMES = {
    "moderate_members": "Moderate Members",
    "manage_messages": "Manage Messages",
    "manage_guild": "Manage Server",
    "read_message_history": "Read Message History",
}


def now() -> int:
    return int(time.time())


def clock() -> float:
    """Sub-second time for the anti-spam and anti-raid windows."""
    return time.time()


def utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def never_raise(fn):
    """Listeners and loops log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("moderation: %s failed", fn.__name__)
    return wrapper


def who(user) -> str:
    """'<@1> (name · `1`)': a mention that still reads right if the user leaves."""
    name = discord.utils.escape_markdown(getattr(user, "name", None) or str(user.id))
    return f"{user.mention} ({name} · `{user.id}`)"


def missing_reply(perm: str) -> str:
    return (f"I'm missing the **{PERMISSION_NAMES[perm]}** permission. "
            "Give my role that permission in Server Settings, then try again.")


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.spam = M.SpamTracker()
        self.raid = M.RaidTracker()
        self.raid_lock = asyncio.Lock()
        self.noted_missing: set[tuple[str, str]] = set()

    async def cog_load(self) -> None:
        self.raid_restore.start()

    async def cog_unload(self) -> None:
        self.raid_restore.cancel()

    @property
    def db(self):
        return self.bot.db

    def ours(self, guild) -> bool:
        return guild is not None and guild.id == self.bot.settings.guild_id

    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def is_staff(member) -> bool:
        guild = getattr(member, "guild", None)
        if guild is None:
            return False
        return M.is_staff(guild.owner_id == member.id, member.guild_permissions.administrator, member.roles)

    @staticmethod
    def has(guild, perm: str, channel=None) -> bool:
        me = guild.me
        perms = channel.permissions_for(me) if channel is not None else me.guild_permissions
        return bool(getattr(perms, perm, False))

    async def note_missing(self, guild, perm: str, feature: str) -> None:
        """Automatic systems say once per run that a permission is missing, not on every event."""
        if (perm, feature) in self.noted_missing:
            return
        self.noted_missing.add((perm, feature))
        log.warning("%s is off: missing the %s permission", feature, PERMISSION_NAMES[perm])
        await self.post_log(guild, f"⚠️ {feature} needs the **{PERMISSION_NAMES[perm]}** permission, "
                                   "which my role doesn't have. Until it's granted that part is off.", style.MUTED)

    def log_channel(self, guild):
        return (config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL)
                or config.match_by_name(guild.text_channels, config.MOD_CHANNEL))

    async def post_log(self, guild, text: str, color: int = style.FOREST) -> None:
        channel = self.log_channel(guild)
        if channel is None:
            log.warning("no %s or %s channel: %s", config.MOD_LOG_CHANNEL, config.MOD_CHANNEL, text)
            return
        try:
            await channel.send(embed=style.embed(description=text, color=color), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("couldn't write to the mod log", exc_info=True)

    async def add_case(self, user_id: int, mod_id: int | None, kind: str, reason: str,
                       duration: int | None = None) -> int:
        async with self.db.transaction() as t:
            cur = await t.execute(
                "INSERT INTO cases (user_id, mod_id, kind, reason, at, duration) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, mod_id, kind, reason, now(), duration))
            return cur.lastrowid

    async def log_case(self, guild, case_id: int, kind: str, target, mod, reason: str,
                       duration: int | None, extra: str = "") -> None:
        line = M.case_line(case_id, kind, who(target), mod.mention if mod is not None else None, reason, duration)
        color = style.FOREST if kind == "untimeout" else style.MUTED
        await self.post_log(guild, line + (f"\n{extra}" if extra else ""), color)

    @staticmethod
    async def dm(member, text: str) -> bool:
        if getattr(member, "bot", False):
            return False
        try:
            await member.send(text, allowed_mentions=NO_PINGS)
            return True
        except discord.HTTPException:  # DMs closed, blocked, or left the server
            return False

    @staticmethod
    async def apply_timeout(member, seconds: int, reason: str, keep_longer: bool = False) -> None:
        until = utc(now() + seconds)
        current = getattr(member, "timed_out_until", None)
        if keep_longer and current is not None and current > until:
            return
        await member.timeout(until, reason=reason[:512])

    def target_problem(self, actor, target) -> M.Problem | None:
        guild = actor.guild
        me = guild.me
        return M.check_target(
            actor_id=actor.id, target_id=target.id, bot_id=me.id,
            target_is_staff=self.is_staff(target), actor_is_owner=guild.owner_id == actor.id,
            actor_top=actor.top_role.position, target_top=target.top_role.position,
            bot_top=me.top_role.position)

    async def gate(self, interaction: discord.Interaction, target=None, perm: str | None = None,
                   channel=None) -> bool:
        """Staff, permission and target checks shared by the commands; replies when it says no."""
        user = interaction.user
        if not self.is_staff(user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return False
        guild = interaction.guild
        for p in ([perm] if isinstance(perm, str) else (perm or [])):
            if not self.has(guild, p, channel):
                await interaction.response.send_message(missing_reply(p), ephemeral=True)
                return False
        if target is not None:
            problem = self.target_problem(user, target)
            if problem is not None:
                await interaction.response.send_message(M.TARGET_REPLIES[problem], ephemeral=True)
                return False
        return True

    async def done(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ commands
    @app_commands.command(name="warn", description="Warn a member (mods only)")
    @app_commands.default_permissions(moderate_members=True)  # hidden from members' picker
    @app_commands.describe(member="Who to warn", reason="Why (they get this in a DM)")
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member,
                   reason: app_commands.Range[str, 1, M.REASON_MAX]) -> None:
        if not await self.gate(interaction, member):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, mod, reason = interaction.guild, interaction.user, reason.strip()
        async with self.db.transaction() as tx:
            cur = await tx.execute(
                "INSERT INTO cases (user_id, mod_id, kind, reason, at, duration) VALUES (?, ?, 'warn', ?, ?, NULL)",
                (member.id, mod.id, reason, now()))
            case_id = cur.lastrowid
            row = await tx.fetchone("SELECT COUNT(*) AS n FROM cases WHERE user_id = ? AND kind = 'warn' AND at > ?",
                                    (member.id, now() - M.WARN_WINDOW))
        warns = row["n"]
        dm_ok = await self.dm(member, M.dm_text("warn", guild.name, reason, None, warns=warns))
        await self.log_case(guild, case_id, "warn", member, mod, reason, None,
                            f"{warns} warning{'s' if warns != 1 else ''} in 30 days" + ("" if dm_ok else " · DM failed"))
        reply = f"Case #{case_id}: warned {member.mention} ({warns} in 30 days)."
        auto = M.escalation(warns)
        if auto:
            reply += " " + await self.escalate(guild, member, warns, auto)
        await self.done(interaction, reply)

    async def escalate(self, guild, member, warns: int, seconds: int) -> str:
        """The automatic timeout after a warning; returns a note for the mod's reply."""
        reason = f"Automatic: {warns} warnings in 30 days"
        if not self.has(guild, "moderate_members"):
            await self.post_log(guild, f"⚠️ {who(member)} reached {warns} warnings, but I can't time them out: "
                                       "missing **Moderate Members**.", style.MUTED)
            return "The automatic timeout didn't happen: I'm missing **Moderate Members**."
        try:
            await self.apply_timeout(member, seconds, reason, keep_longer=True)
        except discord.HTTPException:
            log.warning("automatic timeout of %s failed", member.id, exc_info=True)
            await self.post_log(guild, f"⚠️ Automatic timeout of {who(member)} failed.", style.MUTED)
            return "The automatic timeout failed (see bot.log)."
        case_id = await self.add_case(member.id, None, "auto_timeout", reason, seconds)
        await self.dm(member, M.dm_text("auto_timeout", guild.name, reason, seconds))
        await self.log_case(guild, case_id, "auto_timeout", member, None, reason, seconds)
        return f"That's {warns} warnings, so case #{case_id}: automatic {M.fmt_duration(seconds)} timeout."

    @app_commands.command(name="timeout", description="Time out a member (mods only)")
    @app_commands.default_permissions(moderate_members=True)  # hidden from members' picker
    @app_commands.describe(member="Who to time out", minutes="How long (max 28 days)",
                           reason="Why (they get this in a DM)")
    @app_commands.guild_only()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member,
                      minutes: app_commands.Range[int, 1, M.TIMEOUT_MAX_MIN],
                      reason: app_commands.Range[str, 1, M.REASON_MAX]) -> None:
        if not await self.gate(interaction, member, "moderate_members"):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, mod, reason, seconds = interaction.guild, interaction.user, reason.strip(), minutes * 60
        try:
            await self.apply_timeout(member, seconds, f"{mod.name}: {reason}")
        except discord.Forbidden:
            await self.done(interaction, "Discord didn't let me time them out (role order or permissions).")
            return
        case_id = await self.add_case(member.id, mod.id, "timeout", reason, seconds)
        dm_ok = await self.dm(member, M.dm_text("timeout", guild.name, reason, seconds))
        await self.log_case(guild, case_id, "timeout", member, mod, reason, seconds, "" if dm_ok else "DM failed")
        await self.done(interaction, f"Case #{case_id}: timed out {member.mention} for {M.fmt_duration(seconds)}.")

    @app_commands.command(name="untimeout", description="Remove a member's timeout (mods only)")
    @app_commands.default_permissions(moderate_members=True)  # hidden from members' picker
    @app_commands.describe(member="Whose timeout to lift", reason="Optional note for the log")
    @app_commands.guild_only()
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member,
                        reason: app_commands.Range[str, 1, M.REASON_MAX] | None = None) -> None:
        if not await self.gate(interaction, member, "moderate_members"):
            return
        if not member.is_timed_out():
            await interaction.response.send_message(f"{member.mention} isn't timed out.", ephemeral=True,
                                                    allowed_mentions=NO_PINGS)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild, mod = interaction.guild, interaction.user
        reason = (reason or "").strip() or "Timeout removed"
        try:
            await member.timeout(None, reason=f"{mod.name}: {reason}"[:512])
        except discord.Forbidden:
            await self.done(interaction, "Discord didn't let me lift the timeout (role order or permissions).")
            return
        case_id = await self.add_case(member.id, mod.id, "untimeout", reason)
        await self.log_case(guild, case_id, "untimeout", member, mod, reason, None)
        await self.done(interaction, f"Case #{case_id}: lifted {member.mention}'s timeout.")

    @app_commands.command(name="cases", description="A member's last moderation cases (mods only)")
    @app_commands.default_permissions(moderate_members=True)  # hidden from members' picker
    @app_commands.describe(member="Whose cases to show")
    @app_commands.guild_only()
    async def cases(self, interaction: discord.Interaction, member: discord.User) -> None:
        if not await self.gate(interaction):
            return
        rows = await self.db.fetchall(
            "SELECT id, kind, reason, at, duration, mod_id FROM cases WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (member.id, M.CASES_SHOWN))
        warn = await self.db.fetchone("SELECT COUNT(*) AS n FROM cases WHERE user_id = ? AND kind = 'warn' AND at > ?",
                                      (member.id, now() - M.WARN_WINDOW))
        total = await self.db.fetchone("SELECT COUNT(*) AS n FROM cases WHERE user_id = ?", (member.id,))
        embed = style.embed(title=f"Cases: {getattr(member, 'name', member.id)}",
                            description=M.cases_text([dict(r) for r in rows]),
                            footer=style.label("cases", f"{total['n']} total", f"{warn['n']} warnings in 30 days"))
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @app_commands.command(name="purge", description="Delete the last messages in this channel (mods only)")
    @app_commands.default_permissions(moderate_members=True)  # hidden from members' picker
    @app_commands.describe(count="How many messages (1 to 100)")
    @app_commands.guild_only()
    async def purge(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, M.PURGE_MAX]) -> None:
        channel = interaction.channel
        if not hasattr(channel, "purge"):
            await interaction.response.send_message("I can't purge this kind of channel.", ephemeral=True)
            return
        if not await self.gate(interaction, perm=["manage_messages", "read_message_history"], channel=channel):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        mod = interaction.user
        deleted = await channel.purge(limit=count, reason=f"/purge by {mod.name}")
        n = len(deleted)
        where = getattr(channel, "mention", f"<#{channel.id}>")
        reason = f"{n} message{'s' if n != 1 else ''} in {where}"
        # No member target: the case is filed under the channel id, so it never mixes with /cases.
        case_id = await self.add_case(channel.id, mod.id, "purge", reason)
        await self.post_log(interaction.guild, f"**Case #{case_id}** {M.KIND_LABELS['purge']}\n"
                                               f"**By** {mod.mention}\n**Deleted** {reason}", style.MUTED)
        await self.done(interaction, f"Deleted {n} message{'s' if n != 1 else ''}.")

    # ------------------------------------------------------------ anti-spam
    @commands.Cog.listener()
    @never_raise
    async def on_message(self, message: discord.Message) -> None:
        author = message.author
        guild = message.guild
        if not self.ours(guild) or author.bot or message.webhook_id is not None:
            return
        if getattr(author, "roles", None) is None or self.is_staff(author):
            return
        mentions = len(set(message.raw_mentions) - {author.id}) + len(set(message.raw_role_mentions))
        hit = self.spam.add(author.id, SpamMessage(at=clock(), message_id=message.id, channel_id=message.channel.id,
                                                   content_key=M.content_key(message.content), mentions=mentions))
        if hit is not None:
            await self.handle_spam(guild, author, hit)

    async def handle_spam(self, guild, member, hit: M.SpamHit) -> None:
        reason = M.SPAM_REASONS[hit.reason]
        duration = None
        if not self.has(guild, "moderate_members"):
            await self.note_missing(guild, "moderate_members", "Anti-spam timeouts")
        elif M.check_target(actor_id=0, target_id=member.id, bot_id=guild.me.id, target_is_staff=False,
                            actor_is_owner=True, actor_top=0, target_top=member.top_role.position,
                            bot_top=guild.me.top_role.position) is None:
            try:
                await self.apply_timeout(member, M.SPAM_TIMEOUT, reason, keep_longer=True)
                duration = M.SPAM_TIMEOUT
            except discord.HTTPException:
                log.warning("anti-spam timeout of %s failed", member.id, exc_info=True)
        deleted, total = await self.delete_burst(guild, hit.burst)
        case_id = await self.add_case(member.id, None, "spam", reason, duration)
        if duration:
            await self.dm(member, M.dm_text("spam", guild.name, reason, duration))
        extra = [f"Deleted {deleted} of {total} message{'s' if total != 1 else ''}"]
        if not duration:
            extra.append("not timed out (missing permission or role order)")
        await self.log_case(guild, case_id, "spam", member, None, reason, duration, " · ".join(extra))

    async def delete_burst(self, guild, burst) -> tuple[int, int]:
        """Bulk delete per channel; if that fails, one by one. Returns (deleted, total)."""
        by_channel: dict[int, list[int]] = {}
        for m in burst:
            by_channel.setdefault(m.channel_id, []).append(m.message_id)
        deleted = 0
        for channel_id, ids in by_channel.items():
            channel = guild.get_channel_or_thread(channel_id)
            if channel is None or not hasattr(channel, "delete_messages"):
                continue
            if not self.has(guild, "manage_messages", channel):
                await self.note_missing(guild, "manage_messages", "Anti-spam message deletion")
                continue
            try:
                await channel.delete_messages([discord.Object(i) for i in ids], reason="Anti-spam")
                deleted += len(ids)
                continue
            except discord.HTTPException:
                log.info("bulk delete in %s failed, deleting one by one", channel_id, exc_info=True)
            for i in ids:
                try:
                    await channel.delete_messages([discord.Object(i)], reason="Anti-spam")
                    deleted += 1
                except discord.HTTPException:
                    pass  # already gone
        return deleted, len(burst)

    # ------------------------------------------------------------ anti-raid
    @commands.Cog.listener()
    @never_raise
    async def on_member_join(self, member: discord.Member) -> None:
        if not self.ours(member.guild) or member.bot:
            return
        young = M.is_young(member.created_at.timestamp(), now())
        if self.raid.add(clock(), young):
            await self.start_raid(member.guild)

    async def load_raid(self) -> tuple[bool, M.RaidState | None]:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (RAID_KEY,))
        return (row is not None, M.RaidState.loads(row["value"]) if row else None)

    async def save_raid(self, state: M.RaidState) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (RAID_KEY, state.dumps()))

    async def clear_raid(self) -> None:
        await self.db.execute("DELETE FROM meta WHERE key = ?", (RAID_KEY,))

    async def start_raid(self, guild) -> None:
        async with self.raid_lock:
            t = now()
            _, active = await self.load_raid()
            if active is not None:
                state = M.extend(active, t)
                await self.save_raid(state)
                if self.has(guild, "manage_guild"):
                    try:
                        await guild.edit(invites_disabled_until=utc(max(state.until, state.prev_invites_until or 0)),
                                         reason="Anti-raid: joins are still coming in")
                    except discord.HTTPException:
                        log.warning("couldn't extend the invite pause", exc_info=True)
                await self.post_log(guild, f"🚨 More raid joins: the lockdown now lasts until <t:{state.until}:t>.",
                                    style.MUTED)
                return

            paused_until = guild.invites_paused_until if guild.invites_paused() else None
            prev_inv = int(paused_until.timestamp()) if paused_until is not None else None
            state = M.lockdown(t, guild.verification_level.value, prev_inv)
            head = f"🚨 **Join raid**: {M.RAID_WEIGHT}+ joins in {M.RAID_WINDOW} s (new accounts count double)."
            if not self.has(guild, "manage_guild"):
                await self.note_missing(guild, "manage_guild", "Anti-raid lockdown")
                await self.post_log(guild, head + "\nI couldn't lock the server down: missing **Manage Server**. "
                                                  "Raise the verification level and pause invites by hand.", style.MUTED)
                return
            # Saved before editing, so a crash in between still restores.
            await self.save_raid(state)
            changes = {"invites_disabled_until": utc(max(state.until, prev_inv or 0))}
            if state.raised:
                changes["verification_level"] = discord.VerificationLevel.high
            try:
                await guild.edit(**changes, reason="Anti-raid: join burst")
            except discord.HTTPException:
                log.exception("anti-raid lockdown failed")
                await self.post_log(guild, head + "\nThe lockdown failed (see bot.log). Check the server by hand.",
                                    style.MUTED)
                return
            done = "Verification raised to **High** and invites paused" if state.raised else "Invites paused"
            await self.post_log(guild, head + f"\n{done} until <t:{state.until}:t>. "
                                              "Everything reverts automatically.", style.MUTED)
            log.warning("anti-raid lockdown until %s", state.until)

    @tasks.loop(seconds=30)
    async def raid_restore(self) -> None:
        try:
            await self.run_restore()
        except Exception:
            log.exception("anti-raid restore failed")

    @raid_restore.before_loop
    async def before_raid_restore(self) -> None:
        await self.bot.wait_until_ready()

    async def run_restore(self) -> None:
        async with self.raid_lock:
            exists, state = await self.load_raid()
            if state is None:
                if exists:
                    log.error("unreadable anti-raid state, dropping it")
                    await self.clear_raid()
                return
            guild = self.guild()
            if guild is None:
                return
            plan = M.restore_plan(state, now(), guild.verification_level.value)
            if plan is None:
                return
            changes = {"invites_disabled_until": utc(plan.invites_until) if plan.invites_until else None}
            if plan.level is not None:
                changes["verification_level"] = try_enum(discord.VerificationLevel, plan.level)
            try:
                await guild.edit(**changes, reason="Anti-raid: lockdown over")
            except discord.Forbidden:
                log.exception("anti-raid restore not allowed; giving up")
                await self.clear_raid()
                await self.post_log(guild, "⚠️ The raid lockdown is over but I couldn't undo it (missing "
                                           "**Manage Server**). Reset verification and invites by hand.", style.MUTED)
                return
            except discord.HTTPException:
                log.warning("anti-raid restore failed, retrying", exc_info=True)
                return
            await self.clear_raid()
            parts = ["invites are open again" if plan.invites_until is None else "the earlier invite pause is back"]
            if plan.level is not None:
                parts.insert(0, f"verification is back to **{changes['verification_level']}**")
            await self.post_log(guild, f"✅ Raid lockdown over: {' and '.join(parts)}.")
            log.info("anti-raid lockdown lifted")


async def setup(bot) -> None:
    await bot.add_cog(Moderation(bot))
