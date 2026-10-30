"""Base of the Month for Monkey SMP (rules in logic/botm.py).

/botm award (staff only) names this month's builders (up to four), with an optional line from them
and up to three screenshots. Front Desk moves the Base of the Month role from last month's holders
to them and announces it in ⛏️・monkey-smp, pinging only the winners.

- The role is made when it is missing (no permissions, not mentionable). A new role lands at the
  bottom of the list; Front Desk never moves roles, so one an admin dragged above Front Desk, or
  one given moderator permissions, is refused with a note instead.
- Screenshots: staff take them in BlueMap's free view. No coordinates or map links (SEASONS.md).
"""

import logging
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogkit import is_staff
from logic import botm as B
from logic.selfroles import has_dangerous_permissions

log = logging.getLogger(__name__)

STAFF_ONLY = "Only Moderators and Keepers can award Base of the Month."
REASON = "Base of the Month"


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


class Botm(commands.Cog):
    group = app_commands.Group(name="botm", description="Base of the Month (staff only)", guild_only=True,
                               default_permissions=discord.Permissions(manage_roles=True))

    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def role(self, guild):
        """(role, problem). Makes the role if it is missing; never moves it."""
        role = config.match_by_name(guild.roles, config.BOTM_ROLE)
        if role is None:
            try:
                role = await guild.create_role(name=config.BOTM_ROLE, permissions=discord.Permissions.none(),
                                               mentionable=False, reason=f"{REASON}: the role was missing")
            except discord.HTTPException:
                log.warning("botm: couldn't create the role", exc_info=True)
                return None, (f"I couldn't create the `{config.BOTM_ROLE}` role (Front Desk needs Manage "
                              "Roles). Make it by hand, below Front Desk, then run this again.")
        if has_dangerous_permissions(role):
            return None, f"`{config.BOTM_ROLE}` has moderator permissions, so I won't hand it out."
        me = getattr(guild, "me", None)
        if me is None or not me.top_role > role:
            return None, (f"`{config.BOTM_ROLE}` sits above Front Desk, so I can't hand it out. Drag it below "
                          "Front Desk in Server Settings -> Roles (don't move Front Desk itself).")
        return role, None

    @group.command(name="award", description="Award Base of the Month and announce it in the Monkey SMP channel")
    @app_commands.describe(builder="A builder of the winning base", builder2="Another builder",
                           builder3="Another builder", builder4="Another builder",
                           line="A line from the builders about the build",
                           picture="A screenshot from the live map (no coordinates)",
                           picture2="Another screenshot", picture3="Another screenshot")
    async def award(self, interaction: discord.Interaction, builder: discord.Member,
                    builder2: discord.Member | None = None, builder3: discord.Member | None = None,
                    builder4: discord.Member | None = None, line: str | None = None,
                    picture: discord.Attachment | None = None, picture2: discord.Attachment | None = None,
                    picture3: discord.Attachment | None = None) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        winners = list({m.id: m for m in (builder, builder2, builder3, builder4) if m is not None}.values())
        if any(getattr(m, "bot", False) for m in winners):
            await interaction.response.send_message("Bots don't build bases.", ephemeral=True)
            return
        text = " ".join((line or "").split())
        if len(text) > B.LINE_MAX:
            await interaction.response.send_message(f"Keep the line under {B.LINE_MAX} characters.", ephemeral=True)
            return
        pictures = [p for p in (picture, picture2, picture3) if p is not None]
        if any(not str(getattr(p, "content_type", "") or "").startswith("image/") for p in pictures):
            await interaction.response.send_message("The pictures have to be images.", ephemeral=True)
            return
        guild = interaction.guild
        channel = config.match_by_names(guild.text_channels, (config.MONKEY_CHANNEL, *config.MONKEY_CHANNEL_WAS))
        if channel is None:
            await interaction.response.send_message(f"There's no {config.MONKEY_CHANNEL} channel to announce it in.",
                                                    ephemeral=True)
            return

        local = datetime.fromtimestamp(now(), self.bot.settings.tz)
        month = B.month_key(local)
        state = B.load(await self.db.meta_get(B.STATE_KEY))
        repeat = B.repeat_winners(state, month, [m.id for m in winners])
        if repeat:
            await interaction.response.send_message(
                f"<@{repeat[0]}> won last month, and the same builders can't win two months running.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        role, problem = await self.role(guild)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        notes = await self.move_role(guild, role, state, winners)
        state = B.award(state, month, [m.id for m in winners])
        await self.db.meta_set(B.STATE_KEY, B.dump(state))

        files = []
        for p in pictures:
            try:
                files.append(await p.to_file())
            except discord.HTTPException:
                notes.append(f"couldn't read {esc(p.filename)}")
        body = B.announcement([m.mention for m in winners], B.month_label(local), esc(text))
        try:
            sent = await channel.send(body, files=files,
                                      allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                                               users=winners))
        except discord.HTTPException:
            log.warning("botm: couldn't post the announcement", exc_info=True)
            await interaction.followup.send("The role moved, but I couldn't post in the Monkey SMP channel.",
                                            ephemeral=True)
            return
        reply = f"Announced: {sent.jump_url}"
        if notes:
            reply += "\nNote: " + "; ".join(notes) + "."
        await interaction.followup.send(reply, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def move_role(self, guild, role, state: B.State, winners) -> list[str]:
        """Last holders lose the role, winners get it. Returns notes about what failed."""
        notes: list[str] = []
        keep = {m.id for m in winners}
        holders = {m.id: m for m in getattr(role, "members", None) or []}
        for uid in state.winners:  # the members cache may be cold
            if uid not in holders and (m := guild.get_member(uid)) is not None:
                holders[uid] = m
        for uid, member in holders.items():
            if uid in keep or role not in getattr(member, "roles", []):
                continue
            try:
                await member.remove_roles(role, reason=REASON)
            except discord.HTTPException:
                notes.append(f"couldn't take the role from {member.mention}")
        for member in winners:
            if role in getattr(member, "roles", []):
                continue
            try:
                await member.add_roles(role, reason=REASON)
            except discord.HTTPException:
                notes.append(f"couldn't give the role to {member.mention}")
        return notes


async def setup(bot) -> None:
    await bot.add_cog(Botm(bot))
