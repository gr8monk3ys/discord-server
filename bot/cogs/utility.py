"""Module 12: Utility. /remind and /reminders, /afk, the suggestions forum (votes and
status tags), the member/online stat channels, and "Contact the mods" tickets.

Everything lives in SQLite (reminders, afk, tickets, and stat-channel/ticket-message
ids in meta), so a restart picks up where it left off. The online counter needs the
Presence intent; without it that channel is left alone. No privileged intent is needed
otherwise: AFK uses message.mentions, which Discord sends without Message Content."""

import asyncio
import functools
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from cogs.lfg import ping_only
from errors import reply_error
from logic import community as community_rules
from logic import utility as U

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
FRONT_DESK_CATEGORY = "01 · front desk"  # stat channels sit at the top of this category
STAT_KEY = "utility:stat:{kind}"  # meta: channel id of the counter
RENAMED_KEY = "utility:stat_renamed:{kind}"  # meta: when it was last renamed
TICKET_MESSAGE_KEY = "utility:ticket_message"  # meta: "channel_id:message_id" of the button post
DUE_BATCH = 25  # reminders delivered per tick at most
STARTER_RETRIES = 3  # a forum post's starter message can lag its thread_create event


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("utility: %s failed", fn.__name__)
    return wrapper


def display(user) -> str:
    return discord.utils.escape_markdown(getattr(user, "display_name", None) or getattr(user, "name", "") or "someone")


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    return community_rules.can_handle(guild.owner_id == user.id, user.guild_permissions.administrator, user.roles)


# ---------------------------------------------------------------- ticket buttons
class TicketOpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:open"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Contact the mods", emoji="🆘",
                                           style=discord.ButtonStyle.primary, custom_id="ticket:open"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Utility" = interaction.client.get_cog("Utility")
        try:
            await cog.open_ticket(interaction)
        except Exception:
            log.exception("opening a ticket for %s failed", interaction.user.id)
            await reply_error(interaction)


class TicketCloseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:close"):
    def __init__(self, disabled: bool = False):
        super().__init__(discord.ui.Button(label="Close ticket", emoji="🔒", disabled=disabled,
                                           style=discord.ButtonStyle.secondary, custom_id="ticket:close"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Utility" = interaction.client.get_cog("Utility")
        try:
            await cog.close_ticket(interaction)
        except Exception:
            log.exception("closing ticket %s failed", getattr(interaction.channel, "id", None))
            await reply_error(interaction)


def open_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TicketOpenButton())
    return view


def close_view(disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TicketCloseButton(disabled=disabled))
    return view


TICKET_POST = ("Need a mod? Press **Contact the mods** and I'll open a private thread that only you "
               "and the mods can see. One open ticket at a time.")


# ---------------------------------------------------------------- the cog
class Utility(commands.Cog):
    suggestion = app_commands.Group(name="suggestion", description="Suggestion forum tools (mods)",
                                    guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.afk_ids: set[int] = set()  # mirror of the afk table, so most messages skip the db
        self.afk_notices: dict[tuple[int, int], int] = {}  # (channel, member) -> last notice

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(TicketOpenButton, TicketCloseButton)
        await self.load_afk()
        # A crash between claiming a ticket and creating its thread leaves a placeholder.
        await self.db.execute("DELETE FROM tickets WHERE thread_id < 0")
        self.reminder_loop.start()
        self.stat_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()
        self.stat_loop.cancel()
        self.bot.remove_dynamic_items(TicketOpenButton, TicketCloseButton)

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def ours(self, guild) -> bool:
        return guild is not None and guild.id == self.bot.settings.guild_id

    async def meta_get(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def meta_set(self, key: str, value) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Fires again after reconnects; every step is idempotent.
        guild = self.guild()
        if guild is None:
            return
        for step in (self.ensure_stat_channels, self.ensure_ticket_message):
            try:
                await step(guild)
            except Exception:
                log.exception("utility: %s failed", step.__name__)

    # ============================================================ reminders
    @app_commands.command(name="remind", description="Remind you about something later, in this channel")
    @app_commands.describe(when='When: "10m", "2h", "1d", "tomorrow 9am", "friday 8pm" (up to 30 days)',
                           what="What to remind you about")
    @app_commands.guild_only()
    async def remind(self, interaction: discord.Interaction, when: app_commands.Range[str, 1, 60],
                     what: app_commands.Range[str, 1, U.REMINDER_TEXT_MAX]) -> None:
        t = now()
        try:
            due = U.parse_when(when, t, self.tz)
        except U.WhenError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        uid = interaction.user.id
        # Only remind in a channel the member can post in themselves; otherwise the bot
        # would post their text somewhere they can't (read-only rules, announcements).
        # channel_id 0 means "deliver by DM".
        channel_id = interaction.channel_id if self.can_post(interaction.channel, interaction.user) else 0
        problem = U.check_reminder(what, 0)
        if problem is None:
            # Count and insert together, so a burst of commands can't pass the limit.
            async with self.db.transaction() as tx:
                row = await tx.fetchone("SELECT COUNT(*) AS n FROM reminders WHERE user_id = ? AND done = 0", (uid,))
                problem = U.check_reminder(what, row["n"])
                if problem is None:
                    cur = await tx.execute(
                        "INSERT INTO reminders (user_id, channel_id, due_at, text, created_at) VALUES (?, ?, ?, ?, ?)",
                        (uid, channel_id, due, what.strip(), t))
                    rid = cur.lastrowid
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.send_message(
            f"⏰ Got it. I'll remind you here <t:{due}:R> (<t:{due}:f>). Reminder `#{rid}`; "
            "see or cancel yours with `/reminders`.", ephemeral=True)

    @app_commands.command(name="reminders", description="See your reminders, or cancel one")
    @app_commands.describe(cancel="A reminder to cancel")
    @app_commands.guild_only()
    async def reminders(self, interaction: discord.Interaction, cancel: int | None = None) -> None:
        uid = interaction.user.id
        if cancel is not None:
            changed = await self.db.execute(
                "UPDATE reminders SET done = 1 WHERE id = ? AND user_id = ? AND done = 0", (cancel, uid))
            text = (f"Cancelled reminder `#{cancel}`." if changed
                    else f"You don't have a waiting reminder `#{cancel}`.")
            await interaction.response.send_message(text, ephemeral=True)
            return
        rows = await self.db.fetchall(
            "SELECT id, due_at, text FROM reminders WHERE user_id = ? AND done = 0 ORDER BY due_at, id", (uid,))
        if not rows:
            await interaction.response.send_message("You have no reminders waiting. Set one with `/remind`.",
                                                    ephemeral=True)
            return
        lines = [U.reminder_line(r["id"], r["due_at"], r["text"]) for r in rows]
        embed = style.embed(title="Your reminders", description="\n".join(lines),
                            footer=style.label("reminders", f"{len(rows)} of {U.REMINDER_MAX_ACTIVE}"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @reminders.autocomplete("cancel")
    async def cancel_choices(self, interaction: discord.Interaction, current: str):
        rows = await self.db.fetchall(
            "SELECT id, due_at, text FROM reminders WHERE user_id = ? AND done = 0 ORDER BY due_at, id",
            (interaction.user.id,))
        t = now()
        needle = (current or "").strip().lstrip("#").lower()
        out = []
        for r in rows:
            label = U.choice_label(r["id"], r["due_at"], t, r["text"])
            if not needle or needle in label.lower():
                out.append(app_commands.Choice(name=label, value=r["id"]))
        return out[:25]

    @tasks.loop(seconds=30)
    async def reminder_loop(self) -> None:
        try:
            await self.deliver_due()
        except Exception:
            log.exception("reminder delivery failed")

    @reminder_loop.before_loop
    async def before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    async def deliver_due(self) -> None:
        t = now()
        rows = await self.db.fetchall(
            "SELECT id, user_id, channel_id, due_at, text, created_at FROM reminders"
            " WHERE done = 0 AND due_at <= ? ORDER BY due_at, id LIMIT ?", (t, DUE_BATCH))
        for r in rows:
            try:
                delivered = await self.deliver(r)
            except Exception:
                log.exception("reminder %s failed", r["id"])
                delivered = False
            # Marked done only after it went out; a temporary failure retries next tick,
            # but not forever.
            if delivered or t - r["due_at"] > U.REMINDER_GIVE_UP:
                if not delivered:
                    log.warning("gave up on reminder %s after a day of failures", r["id"])
                await self.db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (r["id"],))

    @staticmethod
    def can_post(channel, member) -> bool:
        """Can this member post in this channel right now (and isn't timed out)?"""
        try:
            if channel is None or member is None or not hasattr(channel, "permissions_for"):
                return False
            if getattr(member, "is_timed_out", None) and member.is_timed_out():
                return False
            perms = channel.permissions_for(member)
            sends = perms.send_messages_in_threads if isinstance(channel, discord.Thread) else perms.send_messages
            return bool(perms.view_channel and sends)
        except Exception:
            return False

    async def deliver(self, r) -> bool:
        uid = r["user_id"]
        # Member text is shown as typed but can't format, embed links or ping anyone.
        text = U.reminder_message(uid, discord.utils.escape_mentions(discord.utils.escape_markdown(r["text"])),
                                  r["created_at"])
        mentions = ping_only(users=[discord.Object(uid)])
        channel = None
        if r["channel_id"]:
            channel = self.bot.get_channel(r["channel_id"])
        try:
            if r["channel_id"] and channel is None:
                channel = await self.bot.fetch_channel(r["channel_id"])
            guild = getattr(channel, "guild", None)
            member = guild.get_member(uid) if guild is not None else None
            # Re-check at delivery: permissions may have changed or the member may be timed out.
            if channel is not None and self.can_post(channel, member):
                await channel.send(text, allowed_mentions=mentions, suppress_embeds=True)
                return True
        except (discord.NotFound, discord.Forbidden):
            # The channel is gone or closed to the bot: try a DM instead.
            log.info("reminder %s: channel %s unusable, trying a DM", r["id"], r["channel_id"])
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            await user.send(text, allowed_mentions=mentions, suppress_embeds=True)
        except (discord.NotFound, discord.Forbidden):
            log.info("reminder %s: couldn't DM %s either; dropping it", r["id"], uid)
        return True

    # ============================================================ AFK
    async def load_afk(self) -> None:
        self.afk_ids = {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM afk")}

    @app_commands.command(name="afk", description="Mark yourself AFK; I'll tell people who mention you")
    @app_commands.describe(reason="Why you're away (optional)")
    @app_commands.guild_only()
    async def afk(self, interaction: discord.Interaction,
                  reason: app_commands.Range[str, 1, U.AFK_REASON_MAX] | None = None) -> None:
        uid = interaction.user.id
        reason = (reason or "").strip() or None
        await self.db.execute("INSERT OR REPLACE INTO afk (user_id, reason, since) VALUES (?, ?, ?)",
                              (uid, reason, now()))
        self.afk_ids.add(uid)
        await interaction.response.send_message(
            f"💤 You're AFK{': ' + reason if reason else ''}. I'll let people know when they mention you, "
            "and clear it when you next send a message.", ephemeral=True, allowed_mentions=NO_PINGS)

    @commands.Cog.listener()
    @never_raise
    async def on_message(self, message: discord.Message) -> None:
        author = message.author
        if author.bot or not self.ours(message.guild):
            return
        if author.id in self.afk_ids:
            await self.clear_afk(message)
        mentioned = [u for u in message.mentions
                     if u.id in self.afk_ids and u.id != author.id and not getattr(u, "bot", False)]
        if mentioned:
            await self.afk_replies(message, mentioned)

    async def clear_afk(self, message: discord.Message) -> None:
        uid = message.author.id
        self.afk_ids.discard(uid)
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT since FROM afk WHERE user_id = ?", (uid,))
            await tx.execute("DELETE FROM afk WHERE user_id = ?", (uid,))
        if row is None:
            return
        await message.reply(U.welcome_back(display(message.author), row["since"]),
                            allowed_mentions=NO_PINGS, delete_after=30, mention_author=False)

    async def afk_replies(self, message: discord.Message, mentioned) -> None:
        t = now()
        cid = message.channel.id
        self.afk_notices = {k: v for k, v in self.afk_notices.items() if U.afk_due(v, t) is False}
        due = [u for u in mentioned if U.afk_due(self.afk_notices.get((cid, u.id)), t)]
        if not due:
            return
        marks = ", ".join("?" for _ in due)
        rows = await self.db.fetchall(f"SELECT user_id, reason, since FROM afk WHERE user_id IN ({marks})",
                                      tuple(u.id for u in due))
        by_id = {r["user_id"]: r for r in rows}
        lines = []
        for u in due:
            r = by_id.get(u.id)
            if r is None:
                continue
            # The reason is member text in a bot-authored message: escape markdown so a
            # masked link like [free nitro](https://phish) can't render as a trusted link.
            reason = discord.utils.escape_markdown(r["reason"]) if r["reason"] else None
            lines.append(U.afk_notice(display(u), reason, r["since"]))
            self.afk_notices[(cid, u.id)] = t
        if lines:
            await message.reply("\n".join(lines), allowed_mentions=NO_PINGS, mention_author=False,
                                suppress_embeds=True)

    # ============================================================ suggestions
    def is_suggestions(self, channel) -> bool:
        return channel is not None and config.match_by_name([channel], config.SUGGESTIONS_FORUM) is not None

    @staticmethod
    def tags_named(forum, names: list[str]) -> list:
        out = []
        for name in names:
            tag = next((t for t in forum.available_tags if t.name.casefold() == name.casefold()), None)
            if tag is not None and tag not in out:
                out.append(tag)
        return out

    @commands.Cog.listener()
    @never_raise
    async def on_thread_create(self, thread: discord.Thread) -> None:
        forum = thread.parent
        if not self.ours(thread.guild) or not self.is_suggestions(forum):
            return
        if not thread.applied_tags:
            idea = self.tags_named(forum, [U.IDEA_TAG])
            if idea:
                try:
                    await thread.edit(applied_tags=idea)
                except discord.HTTPException:
                    log.warning("couldn't tag suggestion %s as Idea", thread.id, exc_info=True)
            else:
                log.warning("the suggestions forum has no %s tag", U.IDEA_TAG)
        starter = thread.get_partial_message(thread.id)  # a forum post's starter shares its id
        for emoji in U.VOTES:
            for attempt in range(STARTER_RETRIES):
                try:
                    await starter.add_reaction(emoji)
                    break
                except discord.NotFound:
                    if attempt == STARTER_RETRIES - 1:
                        log.warning("suggestion %s: starter message never appeared", thread.id)
                        return
                    await self.pause(2)

    @staticmethod
    async def pause(seconds: float) -> None:
        await asyncio.sleep(seconds)

    @suggestion.command(name="status", description="Mark this suggestion Accepted, Denied or Done")
    @app_commands.describe(status="The decision", note="Optional note posted with it")
    @app_commands.choices(status=[app_commands.Choice(name=v, value=k) for k, v in U.STATUS_TAGS.items()])
    async def suggestion_status(self, interaction: discord.Interaction, status: app_commands.Choice[str],
                                note: app_commands.Range[str, 1, U.STATUS_NOTE_MAX] | None = None) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only Moderators and Keepers can set a suggestion's status.",
                                                    ephemeral=True)
            return
        thread = interaction.channel
        forum = getattr(thread, "parent", None)
        if not hasattr(thread, "applied_tags") or not self.is_suggestions(forum):
            await interaction.response.send_message(
                f"Run this inside a post in {config.SUGGESTIONS_FORUM}.", ephemeral=True)
            return
        names = U.retag([t.name for t in thread.applied_tags], status.value)
        tags = self.tags_named(forum, names)
        if not any(t.name.casefold() == U.STATUS_TAGS[status.value].casefold() for t in tags):
            await interaction.response.send_message(
                f"The suggestions forum has no **{U.STATUS_TAGS[status.value]}** tag. Run setup_server.py.",
                ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        if getattr(thread, "archived", False):
            await thread.edit(archived=False, applied_tags=tags)
        else:
            await thread.edit(applied_tags=tags)
        embed = style.embed(description=U.status_text(status.value, interaction.user.mention, note),
                            color=style.MUTED if status.value == "denied" else style.FOREST,
                            footer=style.label("suggestion", U.STATUS_TAGS[status.value]))
        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)

    # ============================================================ stat channels
    @property
    def online_enabled(self) -> bool:
        return bool(getattr(self.bot.intents, "presences", False))

    def stat_kinds(self) -> list[str]:
        return ["members", "online"] if self.online_enabled else ["members"]

    def count(self, guild, kind: str) -> int:
        if kind == "online":
            return sum(1 for m in guild.members if not m.bot and m.status is not discord.Status.offline)
        if getattr(self.bot.intents, "members", False) and guild.members:
            return sum(1 for m in guild.members if not m.bot)
        return guild.member_count or 0

    async def stat_channel(self, guild, kind: str):
        stored = await self.meta_get(STAT_KEY.format(kind=kind))
        channel = guild.get_channel(int(stored)) if stored else None
        if channel is not None:
            return channel
        category = config.match_by_name(guild.categories, FRONT_DESK_CATEGORY)
        pool = category.voice_channels if category is not None else guild.voice_channels
        channel = next((c for c in pool if U.is_stat_channel(c.name, kind)), None)  # meta lost: adopt it
        if channel is not None:
            await self.meta_set(STAT_KEY.format(kind=kind), channel.id)
        return channel

    @staticmethod
    def locked_overwrites(guild) -> dict:
        return {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
        }

    async def ensure_stat_channels(self, guild) -> None:
        category = config.match_by_name(guild.categories, FRONT_DESK_CATEGORY)
        if category is None:
            log.warning("no %s category: stat channels skipped", FRONT_DESK_CATEGORY)
            return
        for position, kind in enumerate(self.stat_kinds()):
            channel = await self.stat_channel(guild, kind)
            if channel is None:
                name = U.stat_name(kind, self.count(guild, kind))
                channel = await guild.create_voice_channel(
                    name, category=category, position=position, overwrites=self.locked_overwrites(guild),
                    reason="Front Desk stat channel")
                await self.meta_set(STAT_KEY.format(kind=kind), channel.id)
                await self.meta_set(RENAMED_KEY.format(kind=kind), now())
                log.info("created stat channel %s", name)
            elif channel.overwrites_for(guild.default_role).connect is not False:
                await channel.set_permissions(guild.default_role, view_channel=True, connect=False,
                                              reason="Front Desk stat channel is display-only")
        await self.update_stats(guild)

    async def update_stats(self, guild) -> None:
        t = now()
        for kind in self.stat_kinds():
            channel = await self.stat_channel(guild, kind)
            if channel is None:
                continue
            wanted = U.stat_name(kind, self.count(guild, kind))
            renamed = await self.meta_get(RENAMED_KEY.format(kind=kind))
            if not U.rename_due(channel.name, wanted, int(renamed) if renamed else None, t):
                continue
            try:
                await channel.edit(name=wanted, reason="Front Desk stat update")
            except discord.HTTPException:
                log.warning("couldn't rename stat channel %s", kind, exc_info=True)
                continue
            # Recorded after the rename went through, so the next try still waits its turn.
            await self.meta_set(RENAMED_KEY.format(kind=kind), t)

    @tasks.loop(minutes=10)
    async def stat_loop(self) -> None:
        try:
            guild = self.guild()
            if guild is not None:
                await self.update_stats(guild)
        except Exception:
            log.exception("stat channel update failed")

    @stat_loop.before_loop
    async def before_stats(self) -> None:
        await self.bot.wait_until_ready()

    # ============================================================ tickets
    def help_channel(self, guild):
        return config.match_by_name(guild.text_channels, config.HELP_CHANNEL)

    async def ensure_ticket_message(self, guild) -> None:
        channel = self.help_channel(guild)
        if channel is None:
            log.warning("no %s channel: ticket button skipped", config.HELP_CHANNEL)
            return
        stored = await self.meta_get(TICKET_MESSAGE_KEY)
        if stored:
            cid, mid = (int(x) for x in stored.split(":"))
            if cid == channel.id:
                try:
                    await channel.fetch_message(mid)
                    return  # still there; DynamicItem handles its clicks
                except discord.NotFound:
                    pass  # deleted: post a fresh one
        embed = style.embed(title="Contact the mods", description=TICKET_POST, footer=style.label("help", "tickets"))
        sent = await channel.send(embed=embed, view=open_view(), allowed_mentions=NO_PINGS)
        await self.meta_set(TICKET_MESSAGE_KEY, f"{channel.id}:{sent.id}")
        log.info("posted the ticket button in %s", config.HELP_CHANNEL)

    def mod_role(self, guild):
        return (config.match_by_name(guild.roles, config.MOD_ROLE)
                or config.match_by_name(guild.roles, config.KEEPER_ROLE))

    async def thread_alive(self, guild, thread_id: int) -> bool:
        if guild.get_thread(thread_id) is not None:
            return True
        try:
            await self.bot.fetch_channel(thread_id)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return True  # can't tell: assume it's there rather than open a duplicate

    async def open_ticket(self, interaction: discord.Interaction) -> None:
        user, guild = interaction.user, interaction.guild
        channel = self.help_channel(guild) if guild else None
        if channel is None:
            await interaction.response.send_message("Tickets aren't set up right now. Please message a Moderator.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        t = now()
        placeholder = -user.id
        existing = await self.db.fetchone(
            "SELECT thread_id FROM tickets WHERE user_id = ? AND closed_at IS NULL AND thread_id > 0", (user.id,))
        if existing and not await self.thread_alive(guild, existing["thread_id"]):
            await self.db.execute("UPDATE tickets SET closed_at = ? WHERE thread_id = ?", (t, existing["thread_id"]))
            existing = None
        if existing:
            await interaction.followup.send(f"You already have an open ticket: <#{existing['thread_id']}>",
                                            ephemeral=True)
            return
        # Claim first (a placeholder row), so a double click can't open two threads.
        async with self.db.transaction() as tx:
            busy = await tx.fetchone("SELECT 1 FROM tickets WHERE user_id = ? AND closed_at IS NULL", (user.id,))
            if not busy:
                await tx.execute("INSERT INTO tickets (thread_id, user_id, opened_at) VALUES (?, ?, ?)",
                                 (placeholder, user.id, t))
        if busy:
            await interaction.followup.send("Your ticket is already being opened.", ephemeral=True)
            return
        try:
            thread = await channel.create_thread(
                name=U.ticket_name(user.name), type=discord.ChannelType.private_thread, invitable=False,
                auto_archive_duration=10080, reason=f"Ticket for {user.id}")
            await self.db.execute("UPDATE tickets SET thread_id = ? WHERE thread_id = ?", (thread.id, placeholder))
        except Exception:
            await self.db.execute("DELETE FROM tickets WHERE thread_id = ?", (placeholder,))
            raise
        await thread.add_user(user)
        role = self.mod_role(guild)
        text = (f"{user.mention} opened a ticket. {role.mention if role else 'The mods'} will be with you soon.\n"
                "Tell us what's going on here; only you and the mods can see this thread. "
                "Press **Close ticket** when you're done.")
        await thread.send(text, view=close_view(),
                          allowed_mentions=ping_only(users=[user], roles=[role] if role else ()))
        await interaction.followup.send(f"Opened your ticket: {thread.mention}", ephemeral=True)
        log.info("ticket %s opened by %s", thread.id, user.id)

    async def close_ticket(self, interaction: discord.Interaction) -> None:
        thread = interaction.channel
        row = await self.db.fetchone("SELECT user_id, closed_at FROM tickets WHERE thread_id = ?",
                                     (getattr(thread, "id", 0),))
        if row is None or row["closed_at"] is not None:
            await interaction.response.send_message("This ticket is already closed.", ephemeral=True)
            return
        user = interaction.user
        if not U.can_close_ticket(user.id, row["user_id"], is_staff(user)):
            await interaction.response.send_message("Only the member who opened this ticket or a mod can close it.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(f"🔒 Ticket closed by {user.mention}.", allowed_mentions=NO_PINGS)
        await thread.edit(archived=True, locked=True, reason=f"Ticket closed by {user.id}")
        await self.db.execute("UPDATE tickets SET closed_at = ? WHERE thread_id = ? AND closed_at IS NULL",
                              (now(), thread.id))
        log.info("ticket %s closed by %s", thread.id, user.id)

    @commands.Cog.listener()
    @never_raise
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        await self.db.execute("UPDATE tickets SET closed_at = ? WHERE thread_id = ? AND closed_at IS NULL",
                              (now(), payload.thread_id))


async def setup(bot) -> None:
    await bot.add_cog(Utility(bot))
