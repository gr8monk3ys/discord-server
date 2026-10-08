"""Staff applications: /apply staff opens a five-question form. Front Desk checks the
member can apply (30 days in the server, a 90-day-old account, not timed out, no
warnings or timeouts in the last 60 days, not already staff, one open application at
a time, 60 days after a denial), then posts a review card in the mod log with
Approve / Deny / Interview buttons (DynamicItems, so they survive restarts).

Moderators may press Interview (marks it, logs who, DMs the applicant that someone
will reach out). Only Keepers and the owner may Approve or Deny. Approve hands out the
Moderator role only when it sits below Front Desk (it never moves roles); otherwise
it says to give the role by hand. /apply status shows the applicant where they stand;
/apps list (staff) shows open applications. Answers are escaped and sent with no pings.
Applications are explicit submissions, not tracking, so /privacy doesn't apply here."""

import copy
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import style
from errors import reply_error
from logic import community as community_rules
from logic import staffapps as S

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
STAFF_ONLY = "Only Moderators and Keepers can do that."
DECIDE_ONLY = "Only Keepers and the server owner can approve or deny applications."
ALREADY = "Someone already handled this one."


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


def ts(dt) -> int | None:
    return int(dt.timestamp()) if dt is not None else None


def role_names(user) -> list[str]:
    return [r.name for r in getattr(user, "roles", None) or []]


def is_owner(user) -> bool:
    guild = getattr(user, "guild", None)
    return guild is not None and guild.owner_id == user.id


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    perms = getattr(user, "guild_permissions", None)
    return community_rules.can_handle(guild.owner_id == user.id, bool(getattr(perms, "administrator", False)),
                                      getattr(user, "roles", None) or [])


def timed_out(member) -> bool:
    check = getattr(member, "is_timed_out", None)
    try:
        return bool(check()) if callable(check) else False
    except Exception:
        return False


# ---------------------------------------------------------------- UI
class StaffAppButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"staffapp:(?P<action>approve|deny|interview):(?P<id>\d+)"):
    STYLES = {"approve": ("Approve", discord.ButtonStyle.success),
              "deny": ("Deny", discord.ButtonStyle.secondary),
              "interview": ("Interview", discord.ButtonStyle.primary)}

    def __init__(self, action: str, app_id: int, disabled: bool = False):
        text, button_style = self.STYLES[action]
        super().__init__(discord.ui.Button(label=text, style=button_style, disabled=disabled,
                                           custom_id=f"staffapp:{action}:{app_id}"))
        self.action = action
        self.app_id = app_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "StaffApps" = interaction.client.get_cog("StaffApps")
        try:
            await cog.handle_button(interaction, self.action, self.app_id)
        except Exception:
            log.exception("staff app button %s on application %s failed", self.action, self.app_id)
            await reply_error(interaction)


def build_view(app_id: int, status: str = "pending") -> discord.ui.View:
    """Buttons for a card: all live while pending, Interview off once used, all off when decided."""
    view = discord.ui.View(timeout=None)
    for action in ("approve", "deny", "interview"):
        view.add_item(StaffAppButton(action, app_id, disabled=not S.can_move(status, action)))
    return view


class ApplyModal(discord.ui.Modal):
    def __init__(self, cog: "StaffApps"):
        super().__init__(title="Staff application")
        self.cog = cog
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for q in S.QUESTIONS:
            box = discord.ui.TextInput(
                style=discord.TextStyle.paragraph if q.paragraph else discord.TextStyle.short,
                max_length=q.max_length, placeholder=q.placeholder or None)
            self.inputs[q.key] = box
            self.add_item(discord.ui.Label(text=q.label, component=box, description=q.description))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit(interaction, {k: box.value for k, box in self.inputs.items()})

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("staff application failed", exc_info=error)
        await reply_error(interaction)


# ---------------------------------------------------------------- the cog
class StaffApps(commands.Cog):
    apply_group = app_commands.Group(name="apply", description="Apply to join the staff team",
                                     guild_only=True)
    apps_group = app_commands.Group(name="apps", description="Staff applications (staff only)", guild_only=True,
                                    default_permissions=discord.Permissions(moderate_members=True))

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(StaffAppButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(StaffAppButton)

    @property
    def db(self):
        return self.bot.db

    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    @staticmethod
    def log_channel(guild):
        return config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL) if guild is not None else None

    # ------------------------------------------------------------ eligibility
    async def rows_of(self, user_id: int, tx=None):
        return await (tx or self.db).fetchall("SELECT * FROM staff_apps WHERE user_id = ? ORDER BY id", (user_id,))

    async def recent_cases(self, user_id: int, t: int) -> int:
        marks = ",".join("?" * len(S.RECORD_KINDS))
        row = await self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM cases WHERE user_id = ? AND kind IN ({marks}) AND at > ?",
            (user_id, *S.RECORD_KINDS, t - S.RECORD_WINDOW))
        return row["n"] if row else 0

    async def member_problem(self, member, t: int):
        return S.member_problem(joined_at=ts(getattr(member, "joined_at", None)),
                                created_at=ts(member.created_at) or t, timed_out=timed_out(member),
                                recent_cases=await self.recent_cases(member.id, t),
                                is_staff=is_staff(member), now=t)

    async def problem(self, member, t: int):
        return await self.member_problem(member, t) or S.history_problem(await self.rows_of(member.id), t)

    # ------------------------------------------------------------ helpers
    async def dm(self, user_id: int, text: str) -> bool:
        guild = self.guild()
        try:
            user = guild.get_member(user_id) if guild is not None else None
            if user is None:
                user = await self.bot.fetch_user(user_id)
            await user.send(text, allowed_mentions=NO_PINGS)
            return True
        except discord.HTTPException:  # DMs closed, blocked, or the user is gone
            return False

    async def mod_log(self, guild, line: str, color: int = style.MUTED) -> None:
        channel = self.log_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=style.embed(description=line, color=color), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("staffapps: couldn't write to the mod log", exc_info=True)

    @staticmethod
    def review_embed(app_id: int, member, answers: dict, t: int) -> discord.Embed:
        lines = [f"**Applicant:** <@{member.id}> (`{member.id}`)",
                 f"**Account age:** {S.days_ago(ts(member.created_at), t)}",
                 f"**In the server:** {S.days_ago(ts(getattr(member, 'joined_at', None)), t)}"]
        e = style.embed(title=f"Staff application #{app_id}", description="\n".join(lines),
                        footer=style.label("staff app", "pending"))
        for q in S.QUESTIONS:
            e.add_field(name=q.label, value=S.fit_field(esc(answers.get(q.key, ""))), inline=False)
        return e

    @staticmethod
    def updated_embed(message, app_id: int, status: str, field: str, staff) -> discord.Embed:
        embeds = getattr(message, "embeds", None) or []
        e = (discord.Embed.from_dict(copy.deepcopy(embeds[0].to_dict())) if embeds
             else discord.Embed(title=f"Staff application #{app_id}"))
        e.color = style.FOREST if status == "interview" else style.MUTED
        e.add_field(name=field, value=staff.mention, inline=False)
        e.set_footer(text=style.label("staff app", status))
        return e

    # ------------------------------------------------------------ /apply staff, /apply status
    @apply_group.command(name="staff", description="Apply to become a Moderator")
    async def apply_staff(self, interaction: discord.Interaction) -> None:
        problem = await self.problem(interaction.user, now())
        if problem:
            await interaction.response.send_message(S.reply(*problem), ephemeral=True)
            return
        await interaction.response.send_modal(ApplyModal(self))

    async def submit(self, interaction: discord.Interaction, raw: dict) -> None:
        member = interaction.user
        answers = {q.key: S.clean_answer(raw.get(q.key), q.max_length) for q in S.QUESTIONS}
        t = now()
        problem = S.answers_problem(answers)
        if problem:
            await interaction.response.send_message(S.reply(problem), ephemeral=True)
            return
        problem = await self.member_problem(member, t)
        if problem:
            await interaction.response.send_message(S.reply(*problem), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        # History re-checked with the insert, so a double submit can't make two applications.
        app_id = None
        async with self.db.transaction() as tx:
            problem = S.history_problem(await self.rows_of(member.id, tx), t)
            if problem is None:
                cur = await tx.execute("INSERT INTO staff_apps (user_id, answers, created_at) VALUES (?, ?, ?)",
                                       (member.id, S.dump_answers(answers), t))
                app_id = cur.lastrowid
        if problem:
            await interaction.followup.send(S.reply(*problem), ephemeral=True)
            return

        channel = self.log_channel(interaction.guild)
        if channel is None:
            await self.db.execute("DELETE FROM staff_apps WHERE id = ?", (app_id,))
            log.error("staff application dropped: no %s channel", config.MOD_LOG_CHANNEL)
            await interaction.followup.send("Applications are closed right now. Try again later.", ephemeral=True)
            return
        try:
            sent = await channel.send(embed=self.review_embed(app_id, member, answers, t),
                                      view=build_view(app_id), allowed_mentions=NO_PINGS)
        except BaseException:
            await self.db.execute("DELETE FROM staff_apps WHERE id = ?", (app_id,))
            raise
        await self.db.execute("UPDATE staff_apps SET review_message_id = ? WHERE id = ?", (sent.id, app_id))
        await interaction.followup.send("Thanks for applying! The staff team will review it and I'll DM you. "
                                        "Check on it any time with `/apply status`.", ephemeral=True)

    @apply_group.command(name="status", description="Where your staff application stands")
    async def apply_status(self, interaction: discord.Interaction) -> None:
        t = now()
        rows = await self.rows_of(interaction.user.id)
        latest = rows[-1] if rows else None
        text = S.status_text(latest, t)
        if latest is None:
            problem = await self.member_problem(interaction.user, t)
            if problem:
                text += "\n" + S.reply(*problem)
        await interaction.response.send_message(text, ephemeral=True)

    # ------------------------------------------------------------ /apps list
    @apps_group.command(name="list", description="Open staff applications (staff only)")
    async def apps_list(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        rows = await self.db.fetchall("SELECT * FROM staff_apps WHERE status IN (?, ?) ORDER BY id", S.OPEN)
        guild = interaction.guild
        channel = self.log_channel(guild)

        def link(r) -> str:
            if channel is None or guild is None or not r["review_message_id"]:
                return ""
            return f"https://discord.com/channels/{guild.id}/{channel.id}/{r['review_message_id']}"

        e = style.embed(title="Open staff applications", description=S.list_lines(rows, link),
                        footer=style.label("staff apps", f"{len(rows)} open"))
        await interaction.response.send_message(embed=e, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ review buttons
    async def handle_button(self, interaction: discord.Interaction, action: str, app_id: int) -> None:
        staff = interaction.user
        if not S.allowed(action, is_owner=is_owner(staff), role_names=role_names(staff)):
            text = STAFF_ONLY if action == "interview" else DECIDE_ONLY
            await interaction.response.send_message(text, ephemeral=True)
            return
        row = await self.db.fetchone("SELECT * FROM staff_apps WHERE id = ?", (app_id,))
        if row is None:
            await interaction.response.send_message("That application is gone.", ephemeral=True)
            return
        if not S.can_move(row["status"], action):
            word = S.STATUS_WORDS.get(row["status"], row["status"])
            await interaction.response.send_message(f"This application is already {word}.", ephemeral=True)
            return
        if action == "interview":
            await self.interview(interaction, row)
        elif action == "deny":
            await self.deny(interaction, row)
        else:
            await self.approve(interaction, row)

    async def move(self, row, action: str, staff) -> bool:
        """Atomic status change; False if someone else got there first."""
        t = now()
        new = S.target(action)
        froms = [s for s in S.OPEN if S.can_move(s, action)]
        marks = ",".join("?" * len(froms))
        if action == "interview":
            sql, params = (f"UPDATE staff_apps SET status = ? WHERE id = ? AND status IN ({marks})",
                           (new, row["id"], *froms))
        else:
            sql, params = (f"UPDATE staff_apps SET status = ?, decided_by = ?, decided_at = ?"
                           f" WHERE id = ? AND status IN ({marks})", (new, staff.id, t, row["id"], *froms))
        return bool(await self.db.execute(sql, params))

    async def interview(self, interaction, row) -> None:
        staff = interaction.user
        if not await self.move(row, "interview", staff):
            await interaction.response.send_message(ALREADY, ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=self.updated_embed(interaction.message, row["id"], "interview", "Interview by", staff),
            view=build_view(row["id"], "interview"))
        await self.mod_log(interaction.guild, f"🎙️ Staff application #{row['id']} (<@{row['user_id']}>): "
                                              f"moved to interview by {staff.mention}.")
        await self.dm(row["user_id"], S.dm_text("interview", esc(interaction.guild.name)))

    async def deny(self, interaction, row) -> None:
        staff = interaction.user
        if not await self.move(row, "deny", staff):
            await interaction.response.send_message(ALREADY, ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=self.updated_embed(interaction.message, row["id"], "denied", "Denied by", staff),
            view=build_view(row["id"], "denied"))
        await self.mod_log(interaction.guild, f"✖️ Staff application #{row['id']} (<@{row['user_id']}>): "
                                              f"denied by {staff.mention}.")
        await self.dm(row["user_id"], S.dm_text("denied", esc(interaction.guild.name)))

    async def approve(self, interaction, row) -> None:
        staff = interaction.user
        guild = interaction.guild
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = guild.get_member(row["user_id"])
        if member is None:
            await interaction.followup.send("They aren't in the server any more, so I can't approve this. "
                                            "Use Deny to close it.", ephemeral=True)
            return
        role = config.match_by_name(guild.roles, config.MOD_ROLE)
        me = getattr(guild, "me", None)
        bot_top = me.top_role.position if me is not None and getattr(me, "top_role", None) is not None else 0
        why_not = S.role_grantable(role_position=role.position if role is not None else None, bot_top=bot_top)
        if not await self.move(row, "approve", staff):
            await interaction.followup.send(ALREADY, ephemeral=True)
            return

        granted = False
        if why_not is None:
            try:
                await member.add_roles(role, reason=f"Staff application #{row['id']} approved by {staff}")
                granted = True
            except discord.HTTPException:
                log.warning("staffapps: couldn't give the %s role for application %s", config.MOD_ROLE, row["id"],
                            exc_info=True)
        if granted:
            note = f"Gave them the {config.MOD_ROLE} role."
        elif why_not == "no_role":
            note = f"There's no {config.MOD_ROLE} role, so give them their role by hand."
        elif why_not == "above_bot":
            note = f"The {config.MOD_ROLE} role sits above Front Desk, so give it to them by hand."
        else:
            note = f"I couldn't give them the {config.MOD_ROLE} role, so give it to them by hand."

        try:
            await interaction.message.edit(
                embed=self.updated_embed(interaction.message, row["id"], "approved", "Approved by", staff),
                view=build_view(row["id"], "approved"))
        except discord.HTTPException:
            log.warning("staffapps: couldn't update review card %s", row["id"], exc_info=True)
        await self.mod_log(guild, f"✅ Staff application #{row['id']} (<@{row['user_id']}>): approved by "
                                  f"{staff.mention}. {note}", style.FOREST)
        await interaction.followup.send(f"Approved. {note}", ephemeral=True)
        await self.dm(row["user_id"], S.dm_text("approved", esc(guild.name)))


async def setup(bot) -> None:
    await bot.add_cog(StaffApps(bot))
