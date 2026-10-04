"""Module 5: Community. Welcomes new members once they finish Onboarding, takes
/report and "Report message" reports into the mod log with Resolve / Dismiss
buttons (DynamicItems, so they survive restarts), and keeps a compact mod log of
joins, leaves, bans and AutoMod actions.

Join/leave/onboarding events need the Server Members intent; without it the cog
still loads and reports, bans and AutoMod logging keep working."""

import copy
import functools
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import style
from errors import reply_error
from logic import community as rules
from logic.community import ReportProblem

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
ONBOARDING_TTL = 10 * 60  # re-check whether the server uses Onboarding at most this often
STATUS_BY_ACTION = {"resolve": "resolved", "dismiss": "dismissed"}
NO_MOD_CHANNEL = ("Thanks. Your report is saved, but I couldn't reach the mod channel, "
                  "so please also message a Moderator.")


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("community: %s failed", fn.__name__)
    return wrapper


def who(user) -> str:
    """'<@1> (name · `1`)': a mention that still reads right if the user leaves."""
    name = discord.utils.escape_markdown(getattr(user, "name", None) or str(user.id))
    return f"{user.mention} ({name} · `{user.id}`)"


# ---------------------------------------------------------------- reports UI
class ReportButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"report:(?P<action>resolve|dismiss):(?P<id>\d+)"):
    def __init__(self, action: str, report_id: int, disabled: bool = False):
        text, button_style = (("Resolve", discord.ButtonStyle.success) if action == "resolve"
                              else ("Dismiss", discord.ButtonStyle.secondary))
        super().__init__(discord.ui.Button(label=text, style=button_style, disabled=disabled,
                                           custom_id=f"report:{action}:{report_id}"))
        self.action = action
        self.report_id = report_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Community" = interaction.client.get_cog("Community")
        try:
            await cog.handle_report_button(interaction, self.action, self.report_id)
        except Exception:
            log.exception("report button %s on report %s failed", self.action, self.report_id)
            await reply_error(interaction)


def build_view(report_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReportButton("resolve", report_id, disabled=disabled))
    view.add_item(ReportButton("dismiss", report_id, disabled=disabled))
    return view


class ReportModal(discord.ui.Modal):
    def __init__(self, cog: "Community", message: discord.Message):
        super().__init__(title="Report message")
        self.cog = cog
        self.message = message
        self.reason = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            min_length=rules.REASON_MIN,
            max_length=rules.REASON_MAX,
            placeholder="What's wrong with this message? Only the mods see this.",
        )
        self.add_item(discord.ui.Label(text="Reason", component=self.reason))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        m = self.message
        await self.cog.submit_report(interaction, m.author, m.channel, m, self.reason.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("report modal failed", exc_info=error)
        await reply_error(interaction)


# ---------------------------------------------------------------- the cog
class Community(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.onboarding_cache: tuple[bool, int] | None = None  # (enabled, checked_at)
        self.report_menu = app_commands.ContextMenu(name="Report message", callback=self.report_message)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ReportButton)
        self.bot.tree.add_command(self.report_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.report_menu.name, type=self.report_menu.type)
        self.bot.remove_dynamic_items(ReportButton)

    @property
    def db(self):
        return self.bot.db

    def ours(self, guild) -> bool:
        return guild is not None and guild.id == self.bot.settings.guild_id

    @staticmethod
    def text_channel(guild, name: str):
        return config.match_by_name(guild.text_channels, name)

    # ------------------------------------------------------------ mod log
    async def mod_log(self, guild, line: str, color: int = style.FOREST) -> None:
        """One compact line in the mod log; silently skipped if there's no mod log."""
        channel = self.text_channel(guild, config.MOD_LOG_CHANNEL)
        if channel is None:
            return
        try:
            await channel.send(embed=style.embed(description=line, color=color), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("couldn't write to the mod log", exc_info=True)

    @commands.Cog.listener()
    @never_raise
    async def on_member_join(self, member: discord.Member) -> None:
        if not self.ours(member.guild):
            return
        current = now()
        created = int(member.created_at.timestamp())
        await self.mod_log(member.guild, rules.join_line(member.mention, member.name, member.id, created, current))
        if member.bot:
            return
        if rules.welcome_on_join(member.flags.completed_onboarding, await self.onboarding_enabled(member.guild)):
            await self.welcome(member)

    @commands.Cog.listener()
    @never_raise
    async def on_member_remove(self, member: discord.Member) -> None:
        if self.ours(member.guild):
            await self.mod_log(member.guild, f"📤 {who(member)} left", style.MUTED)

    @commands.Cog.listener()
    @never_raise
    async def on_member_ban(self, guild: discord.Guild, user) -> None:
        if self.ours(guild):
            await self.mod_log(guild, f"🔨 {who(user)} was banned", style.MUTED)

    @commands.Cog.listener()
    @never_raise
    async def on_member_unban(self, guild: discord.Guild, user) -> None:
        if self.ours(guild):
            await self.mod_log(guild, f"♻️ {who(user)} was unbanned")

    @commands.Cog.listener()
    @never_raise
    async def on_automod_action(self, execution: discord.AutoModAction) -> None:
        guild = execution.guild
        if not self.ours(guild):
            return
        try:
            rule = (await execution.fetch_rule()).name
        except discord.HTTPException:
            rule = f"rule {execution.rule_id}"
        channel = f"<#{execution.channel_id}>" if execution.channel_id else None
        line = rules.automod_line(discord.utils.escape_markdown(rule), f"<@{execution.user_id}>",
                                  channel, execution.matched_keyword)
        await self.mod_log(guild, line)

    # ------------------------------------------------------------ welcome
    @commands.Cog.listener()
    @never_raise
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or not self.ours(after.guild):
            return
        if rules.onboarding_completed(before.flags.completed_onboarding, after.flags.completed_onboarding):
            await self.welcome(after)

    async def onboarding_enabled(self, guild) -> bool:
        """Whether the server runs Onboarding (cached). If Discord won't say, assume
        it does: the member is then welcomed when they finish it."""
        current = now()
        if self.onboarding_cache and current - self.onboarding_cache[1] < ONBOARDING_TTL:
            return self.onboarding_cache[0]
        try:
            enabled = bool((await guild.onboarding()).enabled)
        except discord.HTTPException:
            log.warning("couldn't check whether Onboarding is on; assuming it is", exc_info=True)
            return True
        self.onboarding_cache = (enabled, current)
        return enabled

    async def open_posts(self, game_keys: list[str]) -> dict[str, int]:
        """game key -> thread id of the newest open LFG post with a free spot."""
        if not game_keys:
            return {}
        marks = ", ".join("?" for _ in game_keys)
        rows = await self.db.fetchall(
            f"SELECT game, thread_id FROM lfg_posts p WHERE closed_at IS NULL AND thread_id IS NOT NULL"
            f" AND game IN ({marks})"
            " AND (SELECT COUNT(*) FROM lfg_members m WHERE m.post_id = p.id) < size"
            " ORDER BY created_at, id",
            tuple(game_keys),
        )
        return {r["game"]: r["thread_id"] for r in rows}  # later (newer) rows win

    async def welcome(self, member: discord.Member) -> None:
        guild = member.guild
        general = self.text_channel(guild, config.GENERAL_CHANNEL)
        if general is None:
            log.warning("no %s channel: skipped welcoming %s", config.GENERAL_CHANNEL, member.id)
            return
        # Claim first, so two events racing can't both post.
        claimed = await self.db.execute("INSERT OR IGNORE INTO welcomed (user_id, at) VALUES (?, ?)",
                                        (member.id, now()))
        if not claimed:
            return
        games = rules.picked_games(member.roles, config.GAMES)
        forum = config.match_by_name(guild.forums, config.LFG_FORUM)
        text = rules.welcome_text(member.id, member.mention, games, await self.open_posts([g.key for g in games]),
                                  guild.id, forum.mention if forum else config.LFG_FORUM)
        try:
            await general.send(text, allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[member], replied_user=False))
        except Exception:
            await self.db.execute("DELETE FROM welcomed WHERE user_id = ?", (member.id,))
            raise

    # ------------------------------------------------------------ reports
    def can_handle(self, user) -> bool:
        guild = getattr(user, "guild", None)
        if guild is None:
            return False
        return rules.can_handle(guild.owner_id == user.id, user.guild_permissions.administrator, user.roles)

    async def recent_reports(self, reporter_id: int, tx=None) -> int:
        row = await (tx or self.db).fetchone(
            "SELECT COUNT(*) AS n FROM reports WHERE reporter_id = ? AND at > ?",
            (reporter_id, now() - rules.RATE_WINDOW))
        return row["n"]

    @app_commands.command(name="report", description="Quietly report a member to the mods")
    @app_commands.describe(member="Who you're reporting", reason="What happened (only the mods see this)")
    @app_commands.guild_only()
    async def report(self, interaction: discord.Interaction, member: discord.Member,
                     reason: app_commands.Range[str, rules.REASON_MIN, rules.REASON_MAX]) -> None:
        await self.submit_report(interaction, member, interaction.channel, None, reason)

    async def report_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """'Report message' context menu: check who's reported, then ask for a reason."""
        author = message.author
        problem = rules.check_target(interaction.user.id, author.id, author.bot)
        if problem is None and rules.rate_limited(await self.recent_reports(interaction.user.id)):
            problem = ReportProblem.RATE_LIMITED
        if problem:
            await interaction.response.send_message(rules.REPORT_REPLIES[problem], ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal(self, message))

    async def submit_report(self, interaction: discord.Interaction, target, channel,
                            message: discord.Message | None, reason: str) -> None:
        reporter = interaction.user
        problem = rules.check_report(reporter.id, target.id, target.bot, reason)
        report_id = None
        if problem is None:
            reason = reason.strip()
            # Count and insert together, so a burst of submits can't slip past the limit.
            async with self.db.transaction() as tx:
                if rules.rate_limited(await self.recent_reports(reporter.id, tx)):
                    problem = ReportProblem.RATE_LIMITED
                else:
                    cur = await tx.execute(
                        "INSERT INTO reports (reporter_id, target_id, channel_id, message_id, reason, at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (reporter.id, target.id, getattr(channel, "id", None),
                         message.id if message else None, reason, now()))
                    report_id = cur.lastrowid
        if problem:
            await interaction.response.send_message(rules.REPORT_REPLIES[problem], ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        log_channel = (self.text_channel(guild, config.MOD_LOG_CHANNEL)
                       or self.text_channel(guild, config.MOD_CHANNEL))
        if log_channel is None:
            log.error("report %s saved, but there's no %s or %s channel", report_id,
                      config.MOD_LOG_CHANNEL, config.MOD_CHANNEL)
            await interaction.followup.send(NO_MOD_CHANNEL, ephemeral=True)
            return
        embed = self.render_report(report_id, reporter, target, channel, message, reason)
        try:
            sent = await log_channel.send(embed=embed, view=build_view(report_id), allowed_mentions=NO_PINGS)
        except Exception:
            await self.db.execute("DELETE FROM reports WHERE id = ?", (report_id,))
            raise
        await self.db.execute("UPDATE reports SET log_message_id = ? WHERE id = ?", (sent.id, report_id))
        await interaction.followup.send(rules.THANKS, ephemeral=True)

    @staticmethod
    def render_report(report_id: int, reporter, target, channel, message, reason: str) -> discord.Embed:
        lines = [f"**Reason** {reason}"]
        quoted = rules.quote(message.content) if message is not None else None
        if quoted:
            lines += ["", quoted]
        e = style.embed(title=f"Report #{report_id}", description="\n".join(lines),
                        footer=style.label("report", "open"))
        e.add_field(name="Reported", value=who(target), inline=True)
        e.add_field(name="By", value=who(reporter), inline=True)
        if channel is not None:
            e.add_field(name="Channel", value=getattr(channel, "mention", f"<#{channel.id}>"), inline=True)
        if message is not None:
            e.add_field(name="Message", value=f"[Jump to message]({message.jump_url})", inline=True)
        return e

    async def handle_report_button(self, interaction: discord.Interaction, action: str, report_id: int) -> None:
        user = interaction.user
        if not self.can_handle(user):
            await interaction.response.send_message("Only Moderators and Keepers can handle reports.",
                                                    ephemeral=True)
            return
        status = STATUS_BY_ACTION[action]
        changed = await self.db.execute("UPDATE reports SET status = ? WHERE id = ? AND status = 'open'",
                                        (status, report_id))
        if not changed:
            row = await self.db.fetchone("SELECT status FROM reports WHERE id = ?", (report_id,))
            text = f"This report was already {row['status']}." if row else "That report doesn't exist anymore."
            await interaction.response.send_message(text, ephemeral=True)
            return
        message = interaction.message
        embeds = getattr(message, "embeds", None) or []
        embed = discord.Embed.from_dict(copy.deepcopy(embeds[0].to_dict())) if embeds else discord.Embed(title=f"Report #{report_id}")
        embed.color = style.MUTED
        embed.add_field(name=f"{status.capitalize()} by", value=user.mention, inline=False)
        embed.set_footer(text=style.label("report", status))
        await interaction.response.edit_message(embed=embed, view=build_view(report_id, disabled=True))


async def setup(bot) -> None:
    await bot.add_cog(Community(bot))
