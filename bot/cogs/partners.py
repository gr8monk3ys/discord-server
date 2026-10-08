"""Partner program: /partner apply opens a form (server name, permanent invite,
one-paragraph description). Front Desk checks the invite with Discord's public
invite endpoint (it must exist, never expire, have at least 20 members and not be
this server), then posts a review card in the mod log with Approve / Deny buttons
(DynamicItems, so they survive restarts). Approve posts the partner in the partners
channel and DMs the applicant; Deny DMs them too (one try, DMs may be closed).

A weekly sweep (Mondays at noon, Pacific) re-checks approved partners and marks dead
invites: their post is edited to say so and the link is removed. Staff remove a
partner with /partner remove. Applications are explicit submissions, not tracking,
so /privacy doesn't apply here. Every link posted is rebuilt from the validated
invite code; names and descriptions are escaped and sent with no pings."""

import asyncio
import copy
import logging
import time
from typing import Protocol

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from errors import reply_error
from logic import community as community_rules
from logic import partners as P
from logic import textfilter
from logic.schedule import plan

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
FETCH_TIMEOUT = 10
MAX_BODY = 64 * 1024
USER_AGENT = "FrontDesk Discord bot"
SWEEP_PAUSE = 2  # seconds between invite checks in the sweep (the endpoint is rate limited)
SEEN_KEY = f"first_seen:{P.SWEEP_JOB.name}"
STAFF_ONLY = "Only Moderators and Keepers can do that."


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    return community_rules.can_handle(guild.owner_id == user.id, user.guild_permissions.administrator, user.roles)


# ---------------------------------------------------------------- network seam
class Invites(Protocol):
    """Tests pass a fake; nothing else here touches the network."""

    async def fetch(self, code: str) -> tuple[int, bytes]: ...


class AiohttpInvites:
    """GET the public invite endpoint (no auth) with a 10 s timeout and no redirects."""

    async def fetch(self, code: str) -> tuple[int, bytes]:
        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
            async with session.get(P.api_url(code), params={"with_counts": "true"},
                                   allow_redirects=False) as resp:
                return resp.status, await resp.content.read(MAX_BODY)


# ---------------------------------------------------------------- UI
class PartnerButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"partner:(?P<action>approve|deny):(?P<id>\d+)"):
    def __init__(self, action: str, app_id: int, disabled: bool = False):
        text, button_style = (("Approve", discord.ButtonStyle.success) if action == "approve"
                              else ("Deny", discord.ButtonStyle.secondary))
        super().__init__(discord.ui.Button(label=text, style=button_style, disabled=disabled,
                                           custom_id=f"partner:{action}:{app_id}"))
        self.action = action
        self.app_id = app_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Partners" = interaction.client.get_cog("Partners")
        try:
            await cog.handle_button(interaction, self.action, self.app_id)
        except Exception:
            log.exception("partner button %s on application %s failed", self.action, self.app_id)
            await reply_error(interaction)


def build_view(app_id: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PartnerButton("approve", app_id, disabled=disabled))
    view.add_item(PartnerButton("deny", app_id, disabled=disabled))
    return view


class ApplyModal(discord.ui.Modal):
    def __init__(self, cog: "Partners"):
        super().__init__(title="Partner application")
        self.cog = cog
        self.server_name = discord.ui.TextInput(max_length=P.NAME_MAX, placeholder="Your server's name")
        self.invite = discord.ui.TextInput(max_length=P.INVITE_MAX, placeholder="https://discord.gg/yourcode")
        self.description = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, min_length=P.DESC_MIN, max_length=P.DESC_MAX,
            placeholder="One paragraph: what your server is about and who it's for. No pings or links.")
        self.add_item(discord.ui.Label(text="Server name", component=self.server_name))
        self.add_item(discord.ui.Label(text="Permanent invite link or code", component=self.invite,
                                       description="Server Settings > Invites: set Expire after to Never"))
        self.add_item(discord.ui.Label(text="Description", component=self.description))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit(interaction, self.server_name.value, self.invite.value, self.description.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("partner application failed", exc_info=error)
        await reply_error(interaction)


# ---------------------------------------------------------------- the cog
class Partners(commands.Cog):
    partner = app_commands.Group(name="partner", description="Partner servers: apply, or (staff) remove one",
                                 guild_only=True)

    def __init__(self, bot, invites: Invites | None = None, pause=None):
        self.bot = bot
        self.invites = invites or AiohttpInvites()
        self.pause = pause or asyncio.sleep

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PartnerButton)
        self.sweep_loop.start()

    async def cog_unload(self) -> None:
        self.sweep_loop.cancel()
        self.bot.remove_dynamic_items(PartnerButton)

    @property
    def db(self):
        return self.bot.db

    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    @staticmethod
    def channel(guild, name: str):
        return config.match_by_name(guild.text_channels, name) if guild is not None else None

    async def check(self, code: str) -> P.InviteCheck:
        try:
            status, body = await self.invites.fetch(code)
        except Exception as exc:  # timeout, DNS, connection reset...
            log.info("partners: invite check failed: %s", type(exc).__name__)
            return P.InviteCheck("unreachable")
        return P.check_invite(status, body, self.bot.settings.guild_id)

    async def rows_of(self, user_id: int, tx=None):
        return await (tx or self.db).fetchall("SELECT * FROM partners WHERE user_id = ?", (user_id,))

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

    async def mod_log(self, guild, line: str) -> None:
        channel = self.channel(guild, config.MOD_LOG_CHANNEL)
        if channel is None:
            return
        try:
            await channel.send(embed=style.embed(description=line, color=style.MUTED), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("partners: couldn't write to the mod log", exc_info=True)

    # ------------------------------------------------------------ rendering
    @staticmethod
    def post_embed(row, members: int | None, dead: bool = False) -> discord.Embed:
        return style.embed(
            title=esc(row["server_name"]),
            description=P.post_lines(esc(row["server_name"]), esc(row["description"]), members,
                                     row["invite_code"], dead=dead),
            footer=style.label("partner", f"#{row['id']}", "invite expired" if dead else ""),
            color=style.MUTED if dead else style.FOREST,
        )

    @staticmethod
    def review_embed(app_id: int, user_id: int, name: str, check: P.InviteCheck, code: str,
                     desc: str) -> discord.Embed:
        body = P.review_lines(app_id, f"<@{user_id}> (`{user_id}`)", esc(name), esc(check.guild_name or ""),
                              check.members, code, esc(desc))
        return style.embed(title=f"Partner application #{app_id}", description=body,
                           footer=style.label("partner", "pending"))

    @staticmethod
    def decided_embed(message, app_id: int, status: str, staff) -> discord.Embed:
        embeds = getattr(message, "embeds", None) or []
        e = (discord.Embed.from_dict(copy.deepcopy(embeds[0].to_dict())) if embeds
             else discord.Embed(title=f"Partner application #{app_id}"))
        e.color = style.MUTED
        e.add_field(name=f"{status.capitalize()} by", value=staff.mention, inline=False)
        e.set_footer(text=style.label("partner", status))
        return e

    # ------------------------------------------------------------ /partner apply
    @partner.command(name="apply", description="Apply to partner your server with this one")
    async def apply(self, interaction: discord.Interaction) -> None:
        problem = P.application_problem(await self.rows_of(interaction.user.id), now())
        if problem:
            await interaction.response.send_message(P.reply(*problem), ephemeral=True)
            return
        await interaction.response.send_modal(ApplyModal(self))

    async def submit(self, interaction: discord.Interaction, name: str, invite_text: str, description: str) -> None:
        user = interaction.user
        name, desc = P.clean_text(name), P.clean_text(description)
        code = P.parse_invite(invite_text or "")
        problem = P.name_problem(name) or P.description_problem(desc) or (None if code else "invite_format")
        if problem:
            await interaction.response.send_message(P.reply(problem), ephemeral=True)
            return
        # Bots skip AutoMod: the name may not hold links; the description may link a site,
        # but never another server's invite (their own goes in the invite field).
        blocked = (textfilter.screen("partner name", user.id, [name], allow_links=False)
                   or textfilter.screen("partner description", user.id, [desc], invite_allowlist=()))
        if blocked:
            await interaction.response.send_message(blocked, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        check = await self.check(code)
        if check.problem:
            await interaction.followup.send(P.reply(check.problem), ephemeral=True)
            return

        # Rules re-checked with the insert, so a double submit can't make two applications.
        app_id, problem = None, None
        async with self.db.transaction() as tx:
            problem = P.application_problem(await self.rows_of(user.id, tx), now())
            if problem is None:
                open_rows = await tx.fetchall("SELECT status, invite_code FROM partners WHERE status IN (?, ?)",
                                              P.OPEN)
                if P.duplicate(open_rows, code):
                    problem = ("duplicate", 0)
            if problem is None:
                cur = await tx.execute(
                    "INSERT INTO partners (user_id, server_name, invite_code, description, created_at)"
                    " VALUES (?, ?, ?, ?, ?)", (user.id, name, code, desc, now()))
                app_id = cur.lastrowid
        if problem:
            await interaction.followup.send(P.reply(*problem), ephemeral=True)
            return

        log_channel = self.channel(interaction.guild, config.MOD_LOG_CHANNEL)
        if log_channel is None:
            await self.db.execute("DELETE FROM partners WHERE id = ?", (app_id,))
            log.error("partner application dropped: no %s channel", config.MOD_LOG_CHANNEL)
            await interaction.followup.send("Applications are closed right now. Try again later.", ephemeral=True)
            return
        try:
            sent = await log_channel.send(embed=self.review_embed(app_id, user.id, name, check, code, desc),
                                          view=build_view(app_id), allowed_mentions=NO_PINGS)
        except BaseException:
            await self.db.execute("DELETE FROM partners WHERE id = ?", (app_id,))
            raise
        await self.db.execute("UPDATE partners SET review_message_id = ? WHERE id = ?", (sent.id, app_id))
        await interaction.followup.send("Thanks! Your application is in. The mods will review it and I'll DM you "
                                        "the result.", ephemeral=True)

    # ------------------------------------------------------------ review buttons
    async def handle_button(self, interaction: discord.Interaction, action: str, app_id: int) -> None:
        staff = interaction.user
        if not is_staff(staff):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        row = await self.db.fetchone("SELECT * FROM partners WHERE id = ?", (app_id,))
        if row is None or row["status"] != "pending":
            text = f"This application was already {row['status']}." if row else "That application is gone."
            await interaction.response.send_message(text, ephemeral=True)
            return
        if action == "deny":
            await self.deny(interaction, row)
        else:
            await self.approve(interaction, row)

    async def deny(self, interaction, row) -> None:
        staff = interaction.user
        changed = await self.db.execute(
            "UPDATE partners SET status = 'denied', decided_by = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (staff.id, now(), row["id"]))
        if not changed:
            await interaction.response.send_message("Someone already decided this one.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.decided_embed(interaction.message, row["id"], "denied", staff),
                                                view=build_view(row["id"], disabled=True))
        await self.dm(row["user_id"], P.dm_text("denied", esc(row["server_name"]), esc(interaction.guild.name)))

    async def approve(self, interaction, row) -> None:
        staff = interaction.user
        await interaction.response.defer(ephemeral=True, thinking=True)
        check = await self.check(row["invite_code"])  # it may have died since they applied
        if check.problem:
            await interaction.followup.send(f"Can't approve yet: {P.reply(check.problem)}", ephemeral=True)
            return
        channel = self.channel(interaction.guild, config.PARTNERS_CHANNEL)
        if channel is None:
            await interaction.followup.send(f"There's no {config.PARTNERS_CHANNEL} channel to post in.",
                                            ephemeral=True)
            return
        changed = await self.db.execute(
            "UPDATE partners SET status = 'approved', decided_by = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (staff.id, now(), row["id"]))
        if not changed:
            await interaction.followup.send("Someone already decided this one.", ephemeral=True)
            return
        try:
            post = await channel.send(embed=self.post_embed(row, check.members), allowed_mentions=NO_PINGS)
        except BaseException:
            await self.db.execute("UPDATE partners SET status = 'pending', decided_by = NULL, decided_at = NULL"
                                  " WHERE id = ?", (row["id"],))
            raise
        await self.db.execute("UPDATE partners SET post_message_id = ? WHERE id = ?", (post.id, row["id"]))
        try:
            await interaction.message.edit(embed=self.decided_embed(interaction.message, row["id"], "approved", staff),
                                           view=build_view(row["id"], disabled=True))
        except discord.HTTPException:
            log.warning("partners: couldn't update review card %s", row["id"], exc_info=True)
        await interaction.followup.send(f"Approved and posted in {channel.mention}.", ephemeral=True)
        await self.dm(row["user_id"], P.dm_text("approved", esc(row["server_name"]), esc(interaction.guild.name)))

    # ------------------------------------------------------------ /partner remove
    @partner.command(name="remove", description="Staff: take a partner off the partners channel")
    @app_commands.describe(id="The partner number (#id on the post)")
    async def remove(self, interaction: discord.Interaction, id: app_commands.Range[int, 1]) -> None:
        staff = interaction.user
        if not is_staff(staff):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        row = await self.db.fetchone("SELECT * FROM partners WHERE id = ?", (id,))
        if row is None or row["status"] not in ("approved", "dead"):
            await interaction.response.send_message(f"There's no partner #{id}. (Pending applications: use Deny.)",
                                                    ephemeral=True)
            return
        await self.db.execute("UPDATE partners SET status = 'removed', decided_by = ?, decided_at = ? WHERE id = ?",
                              (staff.id, now(), id))
        gone = await self.delete_post(interaction.guild, row["post_message_id"])
        note = "" if gone else " I couldn't delete its post, so remove that by hand."
        await interaction.response.send_message(f"Removed partner #{id} ({esc(row['server_name'])}).{note}",
                                                ephemeral=True, allowed_mentions=NO_PINGS)

    async def delete_post(self, guild, message_id: int | None) -> bool:
        channel = self.channel(guild, config.PARTNERS_CHANNEL)
        if message_id is None:
            return True
        if channel is None:
            return False
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning("partners: couldn't delete partner post %s", message_id, exc_info=True)
            return False
        return True

    # ------------------------------------------------------------ weekly sweep
    @tasks.loop(minutes=10)
    async def sweep_loop(self) -> None:
        try:
            period = await self.due()
            if period is not None and await self.sweep():
                # Marked done only after every invite got a real answer: otherwise retry next tick.
                await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now()))
        except Exception:
            log.exception("partners: sweep failed")

    @sweep_loop.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def due(self):
        """logic/schedule.py semantics: latest due week only; the first run just marks it done."""
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (SEEN_KEY,))
        first_seen = int(row["value"]) if row else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?",
                                                         (f"{P.SWEEP_JOB.name}:%",))}
        t = now()
        todo = plan(P.SWEEP_JOB, t, self.bot.settings.tz, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (SEEN_KEY, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        return todo.run

    async def sweep(self) -> bool:
        """Re-check every approved partner; True if every check got a real answer."""
        rows = await self.db.fetchall("SELECT * FROM partners WHERE status = 'approved' ORDER BY id")
        complete = True
        for i, row in enumerate(rows):
            if i:
                await self.pause(SWEEP_PAUSE)
            try:
                check = await self.check(row["invite_code"])
                if not check.definitive:
                    complete = False
                elif P.is_dead(check):
                    await self.mark_dead(row)
            except Exception:
                complete = False
                log.exception("partners: couldn't sweep partner %s", row["id"])
        return complete

    async def mark_dead(self, row) -> None:
        changed = await self.db.execute("UPDATE partners SET status = 'dead' WHERE id = ? AND status = 'approved'",
                                        (row["id"],))
        if not changed:
            return
        guild = self.guild()
        channel = self.channel(guild, config.PARTNERS_CHANNEL)
        if channel is not None and row["post_message_id"] is not None:
            try:
                await channel.get_partial_message(row["post_message_id"]).edit(embed=self.post_embed(row, None, dead=True))
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.warning("partners: couldn't mark post %s expired", row["post_message_id"], exc_info=True)
        await self.mod_log(guild, f"🤝 Partner #{row['id']} ({esc(row['server_name'])}): the invite expired, "
                                  "so its post now says so.")


async def setup(bot) -> None:
    await bot.add_cog(Partners(bot))
