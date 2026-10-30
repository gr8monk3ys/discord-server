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

The recruitment board (rules in logic/recruit.py), off unless RECRUIT_FORUM names a forum: the leader
of an approved faction writes a pitch, tags and a timezone with /faction pitch, which makes (or edits)
the faction's one post there, and opens or closes it with /faction recruit on|off. The post's "Request
to join" button (a DynamicItem) DMs the leader, or pings them once in the post when their DMs are shut.
Solo players say they're looking with /faction lfg on (the server's /lfg is the game squad finder), and
leaders and staff list them with /faction seekers.
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
from logic import recruit as RC

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
STAFF_ONLY = "Only Moderators and Keepers can approve or reject a faction."
BOARD_OFF = "The faction recruitment board isn't open right now."
NOT_LEADER = ("Only the leader of an approved faction can do that, and you don't lead one this season. "
              "Register with `/faction register`; staff approve it in the forum.")
STATE_CHOICES = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]


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


class JoinButton(discord.ui.DynamicItem[discord.ui.Button], template=r"recruit:join:(?P<id>\d+)"):
    def __init__(self, faction_id: int):
        super().__init__(discord.ui.Button(label="Request to join", style=discord.ButtonStyle.success,
                                           emoji="📯", custom_id=f"recruit:join:{faction_id}"))
        self.faction_id = faction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Factions" = interaction.client.get_cog("Factions")
        try:
            await cog.request_join(interaction, self.faction_id)
        except Exception:
            log.exception("recruit: join request for faction %s failed", self.faction_id)
            await reply_error(interaction)


def board_view(faction_id: int, recruiting: bool) -> discord.ui.View | None:
    """The board post's button, only while the faction is recruiting."""
    if not recruiting:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(JoinButton(faction_id))
    return view


class PitchModal(discord.ui.Modal):
    def __init__(self, cog: "Factions", faction_id: int, current=None):
        super().__init__(title="Your recruitment pitch")
        self.cog = cog
        self.faction_id = faction_id
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for key, label, paragraph, limit, placeholder, description in (
            ("pitch", "Pitch", True, RC.PITCH_LEN[1],
             "River traders with a big farm. We want a builder who likes redstone.",
             "A sentence or two: what you're like and who you want"),
            ("tags", "Playstyle tags", False, 80, "builders, traders, chill",
             f"Up to {RC.MAX_TAGS}, commas between, from: " + ", ".join(RC.PLAYSTYLES)),
            ("timezone", "Timezone", False, RC.TIMEZONE_LEN[1], "US Pacific, evenings",
             "When you're usually on"),
        ):
            default = None
            if current is not None:
                default = ", ".join(RC.load_tags(current["tags"])) if key == "tags" else current[key]
            text, item = box(label, paragraph, limit, placeholder, description[:100], default=default or None)
            if key == "tags":
                text.required = False
            self.inputs[key] = text
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_pitch(interaction, self.faction_id, {k: v.value for k, v in self.inputs.items()})

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("recruit: pitch failed", exc_info=error)
        await reply_error(interaction)


# ---------------------------------------------------------------- the cog
class Factions(commands.Cog):
    group = app_commands.Group(name="faction", description="Monkey SMP factions", guild_only=True)

    def __init__(self, bot, environ=None):
        self.bot = bot
        environ = os.environ if environ is None else environ
        self.settings = F.read_settings(environ, config.SMP_FORUM)
        self.recruit = RC.read_settings(environ)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(FactionButton, JoinButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(FactionButton, JoinButton)

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

    # ------------------------------------------------------------ the recruitment board
    def board(self, guild):
        if not self.recruit.on or guild is None:
            return None
        return config.match_by_name(getattr(guild, "forums", None) or [], self.recruit.forum)

    async def led_approved(self, user_id: int):
        return await self.db.fetchone(
            "SELECT * FROM factions WHERE season = ? AND leader_id = ? AND status = 'approved'",
            (self.settings.season, user_id))

    async def linked(self, user_id: int) -> str | None:
        row = await self.db.fetchone("SELECT name FROM monkey_links WHERE user_id = ? AND state = 'ok'", (user_id,))
        return row["name"] if row else None

    async def on_a_roster(self, user_id: int) -> bool:
        """Whether the member leads, or (by their linked Minecraft name) is listed on, an open registration."""
        rows = await self.open_rows()
        if any(r["leader_id"] == user_id for r in rows):
            return True
        name = await self.linked(user_id)
        if name is None:
            return False
        return any(name.lower() in {m.lower() for m in F.load_members(r["members"])} for r in rows)

    async def forget_member(self, user_id: int) -> None:
        """Leaving or a ban takes a member off the seekers list and forgets their join requests."""
        await self.db.execute("DELETE FROM smp_seekers WHERE user_id = ?", (user_id,))
        await self.db.execute("DELETE FROM recruit_requests WHERE user_id = ?", (user_id,))

    @commands.Cog.listener()
    async def on_member_remove(self, member) -> None:
        try:
            if member.guild.id == self.bot.settings.guild_id:
                await self.forget_member(member.id)
        except Exception:
            log.exception("factions: handling a leave failed")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user) -> None:
        try:
            if guild.id == self.bot.settings.guild_id:
                await self.forget_member(user.id)
        except Exception:
            log.exception("factions: handling a ban failed")

    @staticmethod
    def is_open(row, rec) -> bool:
        return bool(rec["recruiting"]) and not RC.roster_full(F.load_members(row["members"]))

    def board_embed(self, row, rec) -> discord.Embed:
        members = F.load_members(row["members"])
        recruiting = self.is_open(row, rec)
        e = style.embed(title=esc(row["name"]), description=esc(rec["pitch"]),
                        footer=style.label("recruitment board", "recruiting" if recruiting else "closed"),
                        color=style.FOREST if recruiting else style.MUTED)
        tags = RC.load_tags(rec["tags"])
        e.add_field(name="Playstyle", value=", ".join(tags) if tags else "Not set", inline=True)
        e.add_field(name="Timezone", value=esc(rec["timezone"]), inline=True)
        e.add_field(name="Status", value=RC.status_line(recruiting, members), inline=False)
        e.add_field(name="Leader", value=f"<@{row['leader_id']}>", inline=False)
        return e

    @staticmethod
    def board_tags(forum, recruiting: bool) -> list:
        tag = config.match_by_name(getattr(forum, "available_tags", None) or [], config.RECRUITING_TAG)
        return [tag] if tag is not None and recruiting else []

    async def show_on_board(self, guild, faction_id: int) -> str | None:
        """Makes or edits the faction's board post. The post's link, or None when it couldn't."""
        forum = self.board(guild)
        row = await self.db.fetchone("SELECT * FROM factions WHERE id = ?", (faction_id,))
        rec = await self.db.fetchone("SELECT * FROM faction_recruit WHERE faction_id = ?", (faction_id,))
        if forum is None or row is None or rec is None:
            return None
        recruiting = self.is_open(row, rec)
        embed, view = self.board_embed(row, rec), board_view(faction_id, recruiting)
        if rec["thread_id"]:
            try:
                thread = guild.get_thread(rec["thread_id"]) or await guild.fetch_channel(rec["thread_id"])
                message = await thread.fetch_message(rec["message_id"])
                await message.edit(embed=embed, view=view)
                try:
                    await thread.edit(applied_tags=self.board_tags(forum, recruiting))
                except (discord.HTTPException, AttributeError, TypeError):
                    log.info("recruit: couldn't retag the board post of faction %s", faction_id)
                return thread.jump_url
            except (discord.HTTPException, AttributeError, KeyError):
                log.info("recruit: board post of faction %s is gone; making a new one", faction_id)
        try:
            made = await forum.create_thread(name=row["name"], embed=embed, view=view,
                                             applied_tags=self.board_tags(forum, recruiting),
                                             allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("recruit: couldn't post faction %s on the board", faction_id, exc_info=True)
            return None
        await self.db.execute("UPDATE faction_recruit SET thread_id = ?, message_id = ? WHERE faction_id = ?",
                              (made.thread.id, made.message.id, faction_id))
        return made.thread.jump_url

    # /faction pitch
    @group.command(name="pitch", description="Your faction's recruitment board post: pitch, playstyle, timezone")
    async def pitch(self, interaction: discord.Interaction) -> None:
        if self.board(interaction.guild) is None:
            await interaction.response.send_message(BOARD_OFF, ephemeral=True)
            return
        row = await self.led_approved(interaction.user.id)
        if row is None:
            await interaction.response.send_message(NOT_LEADER, ephemeral=True)
            return
        current = await self.db.fetchone("SELECT * FROM faction_recruit WHERE faction_id = ?", (row["id"],))
        await interaction.response.send_modal(PitchModal(self, row["id"], current))

    async def save_pitch(self, interaction: discord.Interaction, faction_id: int, raw: dict) -> None:
        entry, problem = RC.read_pitch(raw)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        if self.board(interaction.guild) is None:
            await interaction.response.send_message(BOARD_OFF, ephemeral=True)
            return
        row = await self.led_approved(interaction.user.id)
        if row is None or row["id"] != faction_id:
            await interaction.response.send_message(NOT_LEADER, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        opened = RC.can_open(row["status"], F.load_members(row["members"])) is None
        async with self.db.transaction() as tx:
            old = await tx.fetchone("SELECT 1 FROM faction_recruit WHERE faction_id = ?", (faction_id,))
            if old is None:
                await tx.execute(
                    "INSERT INTO faction_recruit (faction_id, pitch, tags, timezone, recruiting, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (faction_id, entry.pitch, RC.dump_tags(entry.tags), entry.timezone, int(opened), now()))
            else:
                await tx.execute(
                    "UPDATE faction_recruit SET pitch = ?, tags = ?, timezone = ?, updated_at = ? WHERE faction_id = ?",
                    (entry.pitch, RC.dump_tags(entry.tags), entry.timezone, now(), faction_id))
        url = await self.show_on_board(interaction.guild, faction_id)
        if url is None:
            await interaction.followup.send("Saved, but I couldn't post on the board. Ask staff to check it.",
                                            ephemeral=True)
            return
        extra = " It's open for requests; `/faction recruit off` closes it." if old is None and opened else ""
        await interaction.followup.send(f"Your board post: {url}{extra}", ephemeral=True)

    # /faction recruit on|off
    @group.command(name="recruit", description="Open or close your faction on the recruitment board")
    @app_commands.describe(choice="on: players can ask to join; off: closed")
    @app_commands.choices(choice=STATE_CHOICES)
    async def recruit_toggle(self, interaction: discord.Interaction, choice: app_commands.Choice[str]) -> None:
        if self.board(interaction.guild) is None:
            await interaction.response.send_message(BOARD_OFF, ephemeral=True)
            return
        row = await self.led_approved(interaction.user.id)
        if row is None:
            await interaction.response.send_message(NOT_LEADER, ephemeral=True)
            return
        on = choice.value == "on"
        if on:
            problem = RC.can_open(row["status"], F.load_members(row["members"]))
            if problem:
                await interaction.response.send_message(problem, ephemeral=True)
                return
        changed = await self.db.execute(
            "UPDATE faction_recruit SET recruiting = ?, updated_at = ? WHERE faction_id = ?", (int(on), now(), row["id"]))
        if not changed:
            await interaction.response.send_message("Write your pitch first with `/faction pitch`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        url = await self.show_on_board(interaction.guild, row["id"])
        word = "open for requests" if on else "closed"
        await interaction.followup.send(f"**{esc(row['name'])}** is {word}." + (f" {url}" if url else ""),
                                        ephemeral=True)

    # /faction lfg on|off
    @group.command(name="lfg", description="Solo? Tell faction leaders you're looking for a faction")
    @app_commands.describe(choice="on: list me; off: take me off the list",
                           note="A line about you: what you like doing, how often you play",
                           timezone="When you're usually on, e.g. US Eastern, evenings")
    @app_commands.choices(choice=STATE_CHOICES)
    async def lfg(self, interaction: discord.Interaction, choice: app_commands.Choice[str],
                  note: app_commands.Range[str, 0, RC.NOTE_LEN[1]] = "",
                  timezone: app_commands.Range[str, 0, RC.TIMEZONE_LEN[1]] = "") -> None:
        if self.board(interaction.guild) is None:
            await interaction.response.send_message(BOARD_OFF, ephemeral=True)
            return
        user = interaction.user
        if choice.value == "off":
            gone = await self.db.execute("DELETE FROM smp_seekers WHERE user_id = ?", (user.id,))
            await interaction.response.send_message(
                "You're off the list." if gone else "You weren't on the list.", ephemeral=True)
            return
        if await self.on_a_roster(user.id):
            await interaction.response.send_message(
                "You're already on a faction's registration this season.", ephemeral=True)
            return
        entry, problem = RC.read_seeker(note, timezone)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        t = now()
        await self.db.execute(
            "INSERT INTO smp_seekers (user_id, note, timezone, since, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET note = excluded.note, timezone = excluded.timezone,"
            " updated_at = excluded.updated_at", (user.id, entry[0], entry[1], t, t))
        await interaction.response.send_message(
            f"You're on the list for {RC.SEEKER_DAYS} days: faction leaders see it with `/faction seekers`. Run this "
            "again to stay on, `/faction lfg off` to come off, and press **Request to join** on a board post to ask "
            "a faction yourself.", ephemeral=True)

    # /faction seekers
    @group.command(name="seekers", description="Faction leaders: solo players looking for a faction")
    async def seekers(self, interaction: discord.Interaction) -> None:
        if self.board(interaction.guild) is None:
            await interaction.response.send_message(BOARD_OFF, ephemeral=True)
            return
        leads = await self.led_approved(interaction.user.id) is not None
        if not RC.can_browse(is_staff=is_staff(interaction.user), leads_approved=leads):
            await interaction.response.send_message(NOT_LEADER, ephemeral=True)
            return
        rows = await self.db.fetchall(
            "SELECT s.*, l.name AS mc FROM smp_seekers s LEFT JOIN monkey_links l"
            " ON l.user_id = s.user_id AND l.state = 'ok' WHERE s.updated_at >= ? ORDER BY s.updated_at DESC LIMIT ?",
            (now() - RC.SEEKER_DAYS * 86400, RC.MAX_SEEKERS_SHOWN))
        if not rows:
            await interaction.response.send_message("Nobody's looking right now.", ephemeral=True)
            return
        lines = [RC.seeker_line(f"<@{r['user_id']}>", r["mc"], r["note"], r["timezone"], r["since"], esc)
                 for r in rows]
        e = style.embed(title="Looking for a faction", description="\n".join(lines)[:4000],
                        footer=style.label("recruitment board", f"{len(rows)} looking"))
        await interaction.response.send_message(embed=e, ephemeral=True, allowed_mentions=NO_PINGS)

    # the "Request to join" button
    async def request_join(self, interaction: discord.Interaction, faction_id: int) -> None:
        guild = interaction.guild
        if self.board(guild) is None:
            await interaction.response.send_message(BOARD_OFF, ephemeral=True)
            return
        row = await self.db.fetchone("SELECT * FROM factions WHERE id = ?", (faction_id,))
        rec = await self.db.fetchone("SELECT * FROM faction_recruit WHERE faction_id = ?", (faction_id,))
        if row is None or rec is None or row["status"] != "approved" or row["season"] != self.settings.season:
            await interaction.response.send_message("That faction isn't on the board any more.", ephemeral=True)
            return
        user = interaction.user
        t = now()
        last = await self.db.fetchone("SELECT at FROM recruit_requests WHERE faction_id = ? AND user_id = ?",
                                      (faction_id, user.id))
        problem = RC.request_problem(recruiting=self.is_open(row, rec), is_leader=row["leader_id"] == user.id,
                                     on_a_roster=await self.on_a_roster(user.id),
                                     last_request_at=last["at"] if last else None, now=t)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.db.execute("INSERT OR REPLACE INTO recruit_requests (faction_id, user_id, at) VALUES (?, ?, ?)",
                              (faction_id, user.id, t))
        seeker = await self.db.fetchone("SELECT note, timezone FROM smp_seekers WHERE user_id = ?", (user.id,))
        thread = guild.get_thread(rec["thread_id"]) if rec["thread_id"] else None
        text = RC.request_dm(user.mention, await self.linked(user.id), seeker["note"] if seeker else "",
                             seeker["timezone"] if seeker else "", row["name"], getattr(thread, "jump_url", None), esc)
        if await self.dm(row["leader_id"], text):
            await interaction.followup.send("Sent to the faction's leader. They'll DM you if it's a fit.",
                                            ephemeral=True)
            return
        if thread is not None:
            try:
                await thread.send(f"<@{row['leader_id']}> {user.mention} would like to join. Your DMs are closed, so "
                                  "it's here: DM them to talk it over.",
                                  allowed_mentions=discord.AllowedMentions(
                                      everyone=False, roles=False, users=[discord.Object(row["leader_id"])]))
                await interaction.followup.send("The leader's DMs are closed, so I pinged them on the board post.",
                                                ephemeral=True)
                return
            except discord.HTTPException:
                log.warning("recruit: couldn't ping the leader of faction %s", faction_id, exc_info=True)
        await self.db.execute("DELETE FROM recruit_requests WHERE faction_id = ? AND user_id = ?", (faction_id, user.id))
        await interaction.followup.send("I couldn't reach the leader. Try DMing them yourself.", ephemeral=True)

    async def dm(self, user_id: int, text: str) -> bool:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(text, allowed_mentions=NO_PINGS)
            return True
        except (discord.HTTPException, AttributeError):
            return False


async def setup(bot) -> None:
    await bot.add_cog(Factions(bot))
