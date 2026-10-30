"""Faction sign-up for Monkey SMP (rules in logic/factions.py).

/faction register opens a form for the faction's name, its members' Minecraft names, its banner and
a charter. Front Desk checks it against the rules and this season's other registrations, then makes
one post in 🚩・smp-forum tagged Faction, with Approve / Reject buttons for staff (DynamicItems, so
they survive restarts). /faction charter lets the member who registered rewrite the charter at any
time; the post is edited in place.

- Member-authored text is escaped and sent with no pings.
- No forum (or FACTIONS_FORUM names one that isn't there): the form says sign-up is closed.
- Nothing here changes roles or the Minecraft server: registration is the paperwork; staff still
  set the faction up in game.
"""

import logging
import os
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import style
from cogkit import is_staff
from errors import reply_error
from logic import factions as F

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
STAFF_ONLY = "Only Moderators and Keepers can approve or reject a faction."


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


# ---------------------------------------------------------------- UI
class FactionButton(discord.ui.DynamicItem[discord.ui.Button],
                    template=r"faction:(?P<action>approve|reject):(?P<id>\d+)"):
    STYLES = {"approve": ("Approve", discord.ButtonStyle.success),
              "reject": ("Reject", discord.ButtonStyle.secondary)}

    def __init__(self, action: str, faction_id: int):
        text, button_style = self.STYLES[action]
        super().__init__(discord.ui.Button(label=text, style=button_style,
                                           custom_id=f"faction:{action}:{faction_id}"))
        self.action = action
        self.faction_id = faction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Factions" = interaction.client.get_cog("Factions")
        try:
            await cog.decide(interaction, self.action, self.faction_id)
        except Exception:
            log.exception("faction button %s on registration %s failed", self.action, self.faction_id)
            await reply_error(interaction)


def build_view(faction_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("approve", "reject"):
        view.add_item(FactionButton(action, faction_id))
    return view


def box(label: str, paragraph: bool, max_length: int, placeholder: str, description: str | None = None,
        default: str | None = None):
    text = discord.ui.TextInput(style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
                                max_length=max_length, placeholder=placeholder, default=default)
    return text, discord.ui.Label(text=label, component=text, description=description)


class RegisterModal(discord.ui.Modal):
    def __init__(self, cog: "Factions"):
        super().__init__(title="Register a faction")
        self.cog = cog
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for key, label, paragraph, limit, placeholder, description in (
            ("name", "Faction name", False, F.NAME_LEN[1], "The Red Monkeys", None),
            ("members", "Members' Minecraft Java names", False, 80, "Steve, Alex",
             f"Up to {F.MAX_MEMBERS}, yours too, separated by commas"),
            ("banner", "Banner", True, F.BANNER_LEN[1], "Red field, white monkey skull, black border",
             "Describe it: the official flag is made from it"),
            ("charter", "Charter", True, F.CHARTER_LEN[1],
             "We hold the river and trade with anyone. We raid the Red Monkeys and nobody else.",
             "One paragraph: who you are, what you're after, how you treat others"),
        ):
            text, item = box(label, paragraph, limit, placeholder, description)
            self.inputs[key] = text
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit(interaction, {k: v.value for k, v in self.inputs.items()})

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("faction registration failed", exc_info=error)
        await reply_error(interaction)


class CharterModal(discord.ui.Modal):
    def __init__(self, cog: "Factions", faction_id: int, current: str):
        super().__init__(title="Rewrite your charter")
        self.cog = cog
        self.faction_id = faction_id
        self.charter, item = box("Charter", True, F.CHARTER_LEN[1], "One paragraph", default=current)
        self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.rewrite(interaction, self.faction_id, self.charter.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("faction charter rewrite failed", exc_info=error)
        await reply_error(interaction)


# ---------------------------------------------------------------- the cog
class Factions(commands.Cog):
    group = app_commands.Group(name="faction", description="Monkey SMP factions", guild_only=True)

    def __init__(self, bot, environ=None):
        self.bot = bot
        self.settings = F.read_settings(os.environ if environ is None else environ, config.SMP_FORUM)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(FactionButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(FactionButton)

    @property
    def db(self):
        return self.bot.db

    def forum(self, guild):
        return config.match_by_name(getattr(guild, "forums", None) or [], self.settings.forum) if guild else None

    async def open_rows(self, tx=None):
        return await (tx or self.db).fetchall(
            "SELECT * FROM factions WHERE season = ? AND status != 'rejected' ORDER BY id", (self.settings.season,))

    async def mine(self, user_id: int):
        return await self.db.fetchone(
            "SELECT * FROM factions WHERE season = ? AND leader_id = ? AND status != 'rejected'",
            (self.settings.season, user_id))

    @staticmethod
    def post_embed(row, members, status: str) -> discord.Embed:
        e = style.embed(title=esc(row["name"]), description=esc(row["charter"]),
                        footer=style.label("faction", F.STATUS_WORDS.get(status, status)),
                        color=style.MUTED if status == "rejected" else style.FOREST)
        e.add_field(name="Members", value=", ".join(esc(m) for m in members), inline=False)
        e.add_field(name="Banner", value=esc(row["banner"]), inline=False)
        e.add_field(name="Registered by", value=f"<@{row['leader_id']}>", inline=False)
        return e

    # ------------------------------------------------------------ /faction register
    @group.command(name="register", description="Register your Monkey SMP faction: name, members, banner, charter")
    async def register(self, interaction: discord.Interaction) -> None:
        if self.forum(interaction.guild) is None:
            await interaction.response.send_message("Faction sign-up isn't open right now.", ephemeral=True)
            return
        row = await self.mine(interaction.user.id)
        if row is not None:
            await interaction.response.send_message(
                f"You already registered **{esc(row['name'])}** this season "
                f"({F.STATUS_WORDS.get(row['status'], row['status'])}). One post per faction; use "
                "`/faction charter` to rewrite the charter.", ephemeral=True)
            return
        await interaction.response.send_modal(RegisterModal(self))

    async def submit(self, interaction: discord.Interaction, raw: dict) -> None:
        entry, problem = F.read_entry(raw, self.settings.reserved)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        forum = self.forum(interaction.guild)
        if forum is None:
            await interaction.response.send_message("Faction sign-up isn't open right now.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = interaction.user
        faction_id = None
        async with self.db.transaction() as tx:
            others = await self.open_rows(tx)
            if any(r["leader_id"] == user.id for r in others):
                problem = "You already registered a faction this season."
            else:
                problem = F.clash(entry, others)
            if problem is None:
                cur = await tx.execute(
                    "INSERT INTO factions (season, name, name_key, leader_id, members, banner, charter, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.settings.season, entry.name, entry.key, user.id, F.dump_members(entry.members),
                     entry.banner, entry.charter, now()))
                faction_id = cur.lastrowid
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        row = await self.db.fetchone("SELECT * FROM factions WHERE id = ?", (faction_id,))
        tag = config.match_by_name(getattr(forum, "available_tags", None) or [], config.FACTION_TAG)
        try:
            made = await forum.create_thread(name=entry.name, embed=self.post_embed(row, entry.members, "pending"),
                                             view=build_view(faction_id), applied_tags=[tag] if tag else [],
                                             allowed_mentions=NO_PINGS)
        except BaseException:
            await self.db.execute("DELETE FROM factions WHERE id = ?", (faction_id,))
            raise
        await self.db.execute("UPDATE factions SET thread_id = ?, message_id = ? WHERE id = ?",
                              (made.thread.id, made.message.id, faction_id))
        await interaction.followup.send(f"Posted: {made.thread.jump_url}\nStaff will approve it there.",
                                        ephemeral=True)

    # ------------------------------------------------------------ /faction charter
    @group.command(name="charter", description="Rewrite your faction's charter")
    async def charter(self, interaction: discord.Interaction) -> None:
        row = await self.mine(interaction.user.id)
        if row is None:
            await interaction.response.send_message(
                "Only the member who registered a faction can rewrite its charter, and you haven't registered "
                "one this season.", ephemeral=True)
            return
        await interaction.response.send_modal(CharterModal(self, row["id"], row["charter"]))

    async def rewrite(self, interaction: discord.Interaction, faction_id: int, raw: str) -> None:
        text = F.squash(raw)
        problem = F.charter_problem(text)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        changed = await self.db.execute(
            "UPDATE factions SET charter = ? WHERE id = ? AND leader_id = ? AND status != 'rejected'",
            (text, faction_id, interaction.user.id))
        if not changed:
            await interaction.response.send_message("That registration is closed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.db.fetchone("SELECT * FROM factions WHERE id = ?", (faction_id,))
        await self.edit_post(interaction.guild, row, view_open=row["status"] == "pending")
        await interaction.followup.send("Charter updated.", ephemeral=True)

    async def edit_post(self, guild, row, view_open: bool) -> bool:
        try:
            thread = guild.get_thread(row["thread_id"]) or await guild.fetch_channel(row["thread_id"])
            message = await thread.fetch_message(row["message_id"])
            await message.edit(embed=self.post_embed(row, F.load_members(row["members"]), row["status"]),
                               view=build_view(row["id"]) if view_open else None)
            return True
        except (discord.HTTPException, AttributeError):
            log.warning("factions: couldn't edit the post of registration %s", row["id"], exc_info=True)
            return False

    # ------------------------------------------------------------ staff buttons
    async def decide(self, interaction: discord.Interaction, action: str, faction_id: int) -> None:
        staff = interaction.user
        if not is_staff(staff):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        status = "approved" if action == "approve" else "rejected"
        changed = await self.db.execute(
            "UPDATE factions SET status = ?, decided_by = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (status, staff.id, now(), faction_id))
        row = await self.db.fetchone("SELECT * FROM factions WHERE id = ?", (faction_id,))
        if row is None:
            await interaction.response.send_message("That registration is gone.", ephemeral=True)
            return
        if not changed:
            word = F.STATUS_WORDS.get(row["status"], row["status"])
            await interaction.response.send_message(f"Someone already handled this one ({word}).", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=self.post_embed(row, F.load_members(row["members"]), status), view=None)
        leader = f"<@{row['leader_id']}>"
        if status == "approved":
            text = f"{leader} **{esc(row['name'])}** is registered for the season. Approved by {staff.mention}."
        else:
            text = (f"{leader} staff ({staff.mention}) didn't approve this registration; they'll say why here. "
                    "Fix it and send `/faction register` again.")
        channel = getattr(interaction, "channel", None)
        if channel is not None:
            try:
                await channel.send(text, allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(row["leader_id"])]))
            except discord.HTTPException:
                log.warning("factions: couldn't post the decision on registration %s", faction_id, exc_info=True)


async def setup(bot) -> None:
    await bot.add_cog(Factions(bot))
