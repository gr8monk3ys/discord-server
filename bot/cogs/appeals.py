"""Ban appeals: banned members can't use the server's help desk, so they appeal on the
website (site/appeal). The site's function checks their appeal code and posts a small
JSON payload through a webhook into the staff-only appeals channel; this cog turns it
into a review card.

Trust: only messages in that channel that were sent by a webhook are read, and the code
(an HMAC of the user ID, see logic/appeals.py) is verified again here, so a leaked
webhook URL alone can't put a fake appeal in front of staff. The user must really be
banned, may have one open appeal, must have waited out the cooling-off period and, after
a denial, the re-appeal cooldown. Anything else is deleted and dropped, with at most one
mod-log note per member and reason per day, so resubmitting a valid code can't flood staff.
The raw webhook message is deleted once it has been read.

The card has Accept (unbans, with an audit-log reason) and Deny (optional reason)
buttons, for the same people who may /ban, and a discussion thread. Decisions record
who and when and go to the mod log. The appellant is told by DM when that is possible;
it usually isn't for a banned user, and the staff reply says so. Open appeals nobody
decided for 30 days are closed by a daily job. /appeals list and /appeals code are for staff.

Off (commands say so, nothing is read) until APPEAL_SECRET is set. Needs the Message
Content intent to read the webhook payload and Ban Members to check and lift bans."""

import asyncio
import copy
import logging
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from cogs.moderation import Moderation
from errors import reply_error
from logic import appeals as A
from logic.ops import plan_daily

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
SEEN_KEY = f"first_seen:{A.EXPIRE_JOB.name}"
SWEEP_LIMIT = 50  # newest messages re-read for webhook posts that arrived while the bot was off
HANDLED_MAX = 500
NOT_SET_UP = "Ban appeals aren't set up yet (`APPEAL_SECRET` is missing from `server/.env`)."
STAFF_ONLY = "Only Moderators and Keepers can use this."
DECIDE_ONLY = "Only staff with the **Ban Members** permission can decide a ban appeal."
ALREADY = "Someone already handled this one."
NO_BAN_PERM = ("I'm missing the **Ban Members** permission, so I can't check or lift bans. "
               "Give my role that permission in Server Settings, then try again.")
NO_BAN_APPEAL = ("⚠️ A ban appeal arrived, but I can't check the ban list without the **Ban Members** "
                 "permission. It stays in the channel until my role has it.")


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


def tag(user_id: int) -> str:
    return f"<@{user_id}> (`{user_id}`)"


# ---------------------------------------------------------------- UI
class AppealButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"appeal:(?P<action>accept|deny):(?P<id>\d+)"):
    STYLES = {"accept": ("Accept (unban)", discord.ButtonStyle.success),
              "deny": ("Deny", discord.ButtonStyle.secondary)}

    def __init__(self, action: str, appeal_id: int, disabled: bool = False):
        text, button_style = self.STYLES[action]
        super().__init__(discord.ui.Button(label=text, style=button_style, disabled=disabled,
                                           custom_id=f"appeal:{action}:{appeal_id}"))
        self.action = action
        self.appeal_id = appeal_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Appeals" = interaction.client.get_cog("Appeals")
        try:
            await cog.handle_button(interaction, self.action, self.appeal_id)
        except Exception:
            log.exception("appeal button %s on appeal %s failed", self.action, self.appeal_id)
            await reply_error(interaction)


def build_view(appeal_id: int, status: str = A.OPEN) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("accept", "deny"):
        view.add_item(AppealButton(action, appeal_id, disabled=not A.can_move(status, action)))
    return view


class DenyModal(discord.ui.Modal):
    def __init__(self, cog: "Appeals", appeal_id: int):
        super().__init__(title=f"Deny appeal #{appeal_id}")
        self.cog = cog
        self.appeal_id = appeal_id
        self.reason = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False,
                                           max_length=A.REASON_MAX)
        self.add_item(discord.ui.Label(text="Reason (optional)", component=self.reason,
                                       description="Sent to them if I can DM them, and kept on the card."))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.deny(interaction, self.appeal_id, self.reason.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("denying appeal %s failed", self.appeal_id, exc_info=error)
        await reply_error(interaction)


# ---------------------------------------------------------------- the cog
class Appeals(commands.Cog):
    group = app_commands.Group(name="appeals", description="Ban appeals (staff only)", guild_only=True,
                               default_permissions=discord.Permissions(moderate_members=True))

    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()  # one webhook message at a time
        self.handled: set[int] = set()  # webhook messages dealt with this run (in case deleting failed)
        self.notes = A.DropNotes()
        self.warned: set[str] = set()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(AppealButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.bot.remove_dynamic_items(AppealButton)

    @property
    def db(self):
        return self.bot.db

    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    @staticmethod
    def channel(guild):
        return config.match_by_name(guild.text_channels, config.APPEALS_CHANNEL) if guild is not None else None

    # ------------------------------------------------------------ helpers
    async def mod_log(self, guild, line: str, color: int = style.MUTED) -> None:
        channel = config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL) if guild is not None else None
        if channel is None:
            return
        try:
            await channel.send(embed=style.embed(description=line, color=color), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("appeals: couldn't write to the mod log", exc_info=True)

    async def warn_once(self, guild, key: str, line: str) -> None:
        """A missing permission or intent is said once per run, not on every message."""
        if key in self.warned:
            return
        self.warned.add(key)
        log.warning("appeals: %s", key)
        await self.mod_log(guild, line)

    async def fetch_user(self, user_id: int):
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    async def dm(self, user_id: int, text: str) -> bool:
        user = await self.fetch_user(user_id)
        if user is None:
            return False
        try:
            await user.send(text, allowed_mentions=NO_PINGS)
            return True
        except discord.HTTPException:  # the usual case: a banned user shares no server with the bot
            return False

    @staticmethod
    def can_ban(guild) -> bool:
        return bool(getattr(guild.me.guild_permissions, "ban_members", False))

    @staticmethod
    async def is_banned(guild, user_id: int) -> bool:
        try:
            await guild.fetch_ban(discord.Object(id=user_id))
            return True
        except discord.NotFound:
            return False

    @staticmethod
    def terms() -> tuple[str, int]:
        """(join link, re-appeal seconds) for the outcome DM, with defaults if the secret was removed."""
        s = config.appeal_settings()
        return (s.join_url, s.reappeal) if s is not None else (f"{config.SITE_URL}/join", 90 * A.DAY)

    async def finish(self, message) -> None:
        """Done with a webhook message: remember it and delete it (answers shouldn't sit there raw)."""
        if len(self.handled) >= HANDLED_MAX:
            self.handled.clear()
        self.handled.add(message.id)
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            await self.warn_once(message.guild, "can't delete webhook messages",
                                 f"⚠️ I couldn't delete a raw appeal message in {config.APPEALS_CHANNEL}. "
                                 "I need **Manage Messages** there.")

    # ------------------------------------------------------------ the card
    async def card_embed(self, appeal_id: int, user_id: int, answers: dict, ban, earlier: list, t: int):
        user = await self.fetch_user(user_id)
        name = f" {esc(user.name)}" if user is not None and getattr(user, "name", None) else ""
        lines = [f"**User:** <@{user_id}>{name} (`{user_id}`)"]
        if ban is not None:
            how = "with `/ban`" if ban["source"] == "command" else "in Discord's own menu"
            lines.append(f"**Banned:** <t:{ban['banned_at']}:R> {how}")
        else:
            lines.append("**Banned:** before I kept track of bans")
        case = await self.db.fetchone(
            "SELECT reason, mod_id FROM cases WHERE user_id = ? AND kind = 'ban' ORDER BY id DESC LIMIT 1", (user_id,))
        if case is not None:
            by = f" · by <@{case['mod_id']}>" if case["mod_id"] else ""
            lines.append(f"**Ban reason:** {esc(case['reason'])[:400]}{by}")
        if earlier:
            denied = sum(1 for r in earlier if r["status"] == A.DENIED)
            lines.append(f"**Earlier appeals:** {len(earlier)} ({denied} denied)")
        e = style.embed(title=f"Ban appeal #{appeal_id}", description="\n".join(lines),
                        footer=style.label("ban appeal", A.OPEN))
        for q in A.QUESTIONS:
            shown, hidden = A.screen_answer(answers.get(q.key, ""))
            value = A.fit_field(esc(shown)) if shown is not None else f"*Hidden: it contained {hidden}.*"
            e.add_field(name=q.label, value=value, inline=False)
        return e

    async def card_of(self, guild, row, message=None):
        if message is not None:
            return message
        channel = self.channel(guild)
        if channel is None or not row["card_message_id"]:
            return None
        try:
            return await channel.fetch_message(row["card_message_id"])
        except discord.HTTPException:
            return None

    async def close_card(self, message, appeal_id: int, status: str, field: str, value: str) -> None:
        if message is None:
            return
        embeds = getattr(message, "embeds", None) or []
        e = (discord.Embed.from_dict(copy.deepcopy(embeds[0].to_dict())) if embeds
             else discord.Embed(title=f"Ban appeal #{appeal_id}"))
        e.color = style.FOREST if status == A.ACCEPTED else style.MUTED
        e.add_field(name=field, value=value[:A.FIELD_MAX], inline=False)
        e.set_footer(text=style.label("ban appeal", status))
        try:
            await message.edit(embed=e, view=build_view(appeal_id, status))
        except discord.HTTPException:
            log.warning("appeals: couldn't update card %s", appeal_id, exc_info=True)

    # ------------------------------------------------------------ ingestion
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            if message.webhook_id is not None:
                await self.ingest(message)
            else:
                await self.touch(message)
        except Exception:
            log.exception("appeals: handling a message failed")

    async def touch(self, message) -> None:
        """Staff talking in an appeal's thread keeps it from being auto-closed."""
        if getattr(message.author, "bot", False) or getattr(message.channel, "parent_id", None) is None:
            return
        channel = self.channel(message.guild)
        if channel is not None and message.channel.parent_id == channel.id:
            await self.db.execute("UPDATE appeals SET touched_at = ? WHERE thread_id = ? AND status = ?",
                                  (now(), message.channel.id, A.OPEN))

    async def ingest(self, message) -> str | None:
        """Read one webhook message. Returns what happened (for the log and the tests):
        posted, forged, duplicate, a drop reason from logic.appeals, unreadable, no_permission,
        retry, or None when the message isn't ours to read."""
        guild = message.guild
        if guild is None or guild.id != self.bot.settings.guild_id:
            return None
        channel = self.channel(guild)
        if channel is None or message.channel.id != channel.id:
            return None
        if message.webhook_id is None:  # staff talking, or my own cards: never an appeal
            return None
        settings = config.appeal_settings()
        if settings is None or message.id in self.handled:
            return None
        async with self.lock:
            if message.id in self.handled:
                return None
            verdict = await self.read(guild, channel, message, settings)
        log.info("appeals: webhook message %s -> %s", message.id, verdict)  # never the payload
        return verdict

    async def read(self, guild, channel, message, settings) -> str:
        embeds = getattr(message, "embeds", None) or []
        if not embeds and not (getattr(message, "content", None) or ""):
            await self.warn_once(guild, "can't read webhook messages",
                                 "⚠️ Ban appeals are arriving but I can't read them: turn on the **Message "
                                 "Content** intent for Front Desk in the Developer Portal and restart me.")
            return "unreadable"
        t = now()
        payload = A.parse_payload(embeds[0].description if embeds else None)
        if payload is None or not A.code_matches(settings.secret, payload.user_id, payload.code):
            await self.finish(message)
            if self.notes.should_note(0, "forged", t):
                await self.mod_log(guild, A.drop_note("forged", ""))
            return "forged"
        uid = payload.user_id
        if await self.db.fetchone("SELECT 1 FROM appeals WHERE source_message_id = ?", (message.id,)) is not None:
            await self.finish(message)
            return "duplicate"
        if not self.can_ban(guild):
            await self.warn_once(guild, "missing Ban Members", NO_BAN_APPEAL)
            return "no_permission"
        try:
            banned = await self.is_banned(guild, uid)
        except discord.Forbidden:
            await self.warn_once(guild, "missing Ban Members", NO_BAN_APPEAL)
            return "no_permission"
        except discord.HTTPException:
            log.warning("appeals: couldn't check the ban list, will retry", exc_info=True)
            return "retry"
        earlier = [dict(r) for r in await self.db.fetchall("SELECT * FROM appeals WHERE user_id = ? ORDER BY id",
                                                           (uid,))]
        ban = await self.db.fetchone("SELECT banned_at, source FROM appeal_bans WHERE user_id = ?", (uid,))
        problem = A.ingest_problem(banned=banned, rows=earlier, banned_at=ban["banned_at"] if ban else None,
                                   now=t, cooling=settings.cooling, reappeal=settings.reappeal)
        if problem is not None:
            reason, days = problem
            await self.finish(message)
            if self.notes.should_note(uid, reason, t):
                await self.mod_log(guild, A.drop_note(reason, tag(uid), days))
            return reason

        try:
            async with self.db.transaction() as tx:
                cur = await tx.execute(
                    "INSERT INTO appeals (user_id, answers, created_at, touched_at, source_message_id) "
                    "VALUES (?, ?, ?, ?, ?)", (uid, A.dump_answers(payload.answers), t, t, message.id))
                appeal_id = cur.lastrowid
        except sqlite3.IntegrityError:  # the one-open-appeal index: someone else just filed it
            await self.finish(message)
            return "open"
        try:
            card = await channel.send(embed=await self.card_embed(appeal_id, uid, payload.answers, ban, earlier, t),
                                      view=build_view(appeal_id), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            await self.db.execute("DELETE FROM appeals WHERE id = ?", (appeal_id,))
            log.warning("appeals: couldn't post the card, will retry", exc_info=True)
            return "retry"
        thread_id = None
        try:
            thread = await card.create_thread(name=f"Appeal {appeal_id} discussion", auto_archive_duration=10080)
            thread_id = thread.id
        except (discord.HTTPException, AttributeError):
            log.info("appeals: no discussion thread for appeal %s", appeal_id)
        await self.db.execute("UPDATE appeals SET card_message_id = ?, thread_id = ? WHERE id = ?",
                              (card.id, thread_id, appeal_id))
        await self.finish(message)
        return "posted"

    async def sweep(self) -> int:
        """Webhook messages that arrived while the bot was off (or before a permission was fixed)."""
        guild = self.guild()
        channel = self.channel(guild)
        if channel is None or config.appeal_settings() is None:
            return 0
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            return 0
        try:
            messages = [m async for m in channel.history(limit=SWEEP_LIMIT)]
        except discord.HTTPException:
            log.warning("appeals: couldn't read the appeals channel", exc_info=True)
            return 0
        done = 0
        for message in reversed(messages):  # oldest first
            if message.webhook_id is not None and await self.ingest(message) is not None:
                done += 1
        return done

    # ------------------------------------------------------------ decisions
    async def handle_button(self, interaction: discord.Interaction, action: str, appeal_id: int) -> None:
        if not Moderation.may_ban(interaction.user):
            await interaction.response.send_message(DECIDE_ONLY, ephemeral=True)
            return
        row = await self.db.fetchone("SELECT * FROM appeals WHERE id = ?", (appeal_id,))
        if row is None:
            await interaction.response.send_message("That appeal is gone.", ephemeral=True)
            return
        if not A.can_move(row["status"], action):
            word = A.STATUS_WORDS.get(row["status"], row["status"])
            await interaction.response.send_message(f"This appeal is already {word}.", ephemeral=True)
            return
        if action == "deny":
            await interaction.response.send_modal(DenyModal(self, appeal_id))
        else:
            await self.accept(interaction, row)

    async def move(self, appeal_id: int, status: str, staff_id: int | None, reason: str | None, t: int) -> bool:
        """Atomic: False if someone else decided first."""
        return bool(await self.db.execute(
            "UPDATE appeals SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ? "
            "WHERE id = ? AND status = ?", (status, staff_id, t, reason, appeal_id, A.OPEN)))

    async def accept(self, interaction: discord.Interaction, row) -> None:
        staff, guild, uid, appeal_id = interaction.user, interaction.guild, row["user_id"], row["id"]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.can_ban(guild):
            await interaction.followup.send(NO_BAN_PERM, ephemeral=True)
            return
        t = now()
        if not await self.move(appeal_id, A.ACCEPTED, staff.id, None, t):
            await interaction.followup.send(ALREADY, ephemeral=True)
            return
        extra = ""
        try:
            await guild.unban(discord.Object(id=uid), reason=f"Ban appeal #{appeal_id} accepted by {staff.name}"[:512])
        except discord.NotFound:
            extra = " They were already unbanned."
        except discord.HTTPException:
            log.warning("appeals: unban for appeal %s failed", appeal_id, exc_info=True)
            await self.db.execute(
                "UPDATE appeals SET status = ?, decided_by = NULL, decided_at = NULL WHERE id = ? AND status = ?",
                (A.OPEN, appeal_id, A.ACCEPTED))
            await interaction.followup.send("Discord didn't let me unban them (permissions). The appeal is still "
                                            "open.", ephemeral=True)
            return
        join_url, reappeal = self.terms()
        dm_ok = await self.dm(uid, A.outcome_dm(A.ACCEPTED, esc(guild.name), "", join_url, reappeal))
        told = "I told them by DM." if dm_ok else A.NO_DM_NOTE
        await self.close_card(await self.card_of(guild, row, interaction.message), appeal_id, A.ACCEPTED,
                              "Accepted by", f"{staff.mention} · <t:{t}:f>")
        await self.mod_log(guild, f"✅ Ban appeal #{appeal_id} from {tag(uid)}: accepted by {staff.mention}, ban "
                                  f"lifted.{extra} {told}", style.FOREST)
        await interaction.followup.send(f"Accepted: the ban is lifted.{extra} {told}", ephemeral=True)

    async def deny(self, interaction: discord.Interaction, appeal_id: int, reason: str | None) -> None:
        staff, guild = interaction.user, interaction.guild
        if not Moderation.may_ban(staff):
            await interaction.response.send_message(DECIDE_ONLY, ephemeral=True)
            return
        row = await self.db.fetchone("SELECT * FROM appeals WHERE id = ?", (appeal_id,))
        reason = A.clean_answer(reason, A.REASON_MAX)
        t = now()
        if row is None or not await self.move(appeal_id, A.DENIED, staff.id, reason or None, t):
            await interaction.response.send_message(ALREADY, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = row["user_id"]
        join_url, reappeal = self.terms()
        dm_ok = await self.dm(uid, A.outcome_dm(A.DENIED, esc(guild.name), esc(reason), join_url, reappeal))
        told = "I told them by DM." if dm_ok else A.NO_DM_NOTE
        said = f"\n**Reason** {esc(reason)}" if reason else ""
        await self.close_card(await self.card_of(guild, row, interaction.message), appeal_id, A.DENIED,
                              "Denied by", f"{staff.mention} · <t:{t}:f>{said}")
        await self.mod_log(guild, f"✖️ Ban appeal #{appeal_id} from {tag(uid)}: denied by {staff.mention}. "
                                  f"They can appeal again in {A.plural(reappeal // A.DAY, 'day')}. {told}{said}")
        await interaction.followup.send(f"Denied. {told}", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user) -> None:
        """Unbanned by hand while an appeal was open: nothing left to decide."""
        try:
            if guild.id != self.bot.settings.guild_id:
                return
            t = now()
            rows = await self.db.fetchall("SELECT * FROM appeals WHERE user_id = ? AND status = ?", (user.id, A.OPEN))
            for row in rows:
                if await self.move(row["id"], A.CLOSED, None, "Unbanned outside the appeal", t):
                    await self.close_card(await self.card_of(guild, row), row["id"], A.CLOSED, "Closed",
                                          f"They were unbanned outside the appeal · <t:{t}:f>")
                    await self.mod_log(guild, f"♻️ Ban appeal #{row['id']} from {tag(user.id)} closed: they were "
                                              "unbanned outside the appeal.")
        except Exception:
            log.exception("appeals: on_member_unban failed")

    # ------------------------------------------------------------ /appeals
    @group.command(name="list", description="Open ban appeals and recent decisions (staff only)")
    async def appeals_list(self, interaction: discord.Interaction) -> None:
        if not Moderation.is_staff(interaction.user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        open_rows = await self.db.fetchall("SELECT * FROM appeals WHERE status = ? ORDER BY id", (A.OPEN,))
        recent = await self.db.fetchall(
            "SELECT * FROM appeals WHERE status != ? ORDER BY decided_at DESC, id DESC LIMIT ?",
            (A.OPEN, A.RECENT_SHOWN))
        guild = interaction.guild
        channel = self.channel(guild)

        def link(r) -> str:
            if channel is None or not r["card_message_id"]:
                return ""
            return f"https://discord.com/channels/{guild.id}/{channel.id}/{r['card_message_id']}"

        text = A.list_text(open_rows, recent, link)
        if config.appeal_settings() is None:
            text = f"{NOT_SET_UP}\n\n{text}"
        elif channel is None:
            text = f"There's no {config.APPEALS_CHANNEL} channel yet (run `public_mode.py`).\n\n{text}"
        e = style.embed(title="Ban appeals", description=text,
                        footer=style.label("ban appeals", f"{len(open_rows)} open"))
        await interaction.response.send_message(embed=e, ephemeral=True, allowed_mentions=NO_PINGS)

    @group.command(name="code", description="Show a banned user's appeal code to pass on (staff only)")
    @app_commands.describe(user="The banned user (their ID works)")
    async def appeals_code(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not Moderation.is_staff(interaction.user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        settings = config.appeal_settings()
        if settings is None:
            await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
            return
        ban = await self.db.fetchone("SELECT banned_at FROM appeal_bans WHERE user_id = ?", (user.id,))
        notice = A.ban_notice(page_url=settings.page_url, user_id=user.id,
                              code=A.appeal_code(settings.secret, user.id),
                              banned_at=ban["banned_at"] if ban else now() - settings.cooling,
                              cooling=settings.cooling, reappeal=settings.reappeal)
        await interaction.response.send_message(
            f"What {user.mention} needs to appeal. The code only works for their user ID, and only while "
            f"they're banned.\n\n{notice}", ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ scheduled work
    @tasks.loop(minutes=5)
    async def tick(self) -> None:
        try:
            await self.sweep()
            period = await self.due()
            if period is not None:
                await self.expire(now())
                await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now()))
        except Exception:
            log.exception("appeals: tick failed")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def due(self):
        """logic/schedule.py semantics: the latest due day only; the first run just marks it done."""
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (SEEN_KEY,))
        first_seen = int(row["value"]) if row else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?",
                                                         (f"{A.EXPIRE_JOB.name}:%",))}
        t = now()
        todo = plan_daily(A.EXPIRE_JOB, t, self.bot.settings.tz, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (SEEN_KEY, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        return todo.run

    async def expire(self, t: int) -> int:
        """Close open appeals nobody touched for 30 days, and blank old answers."""
        guild = self.guild()
        rows = await self.db.fetchall("SELECT * FROM appeals WHERE status = ? AND touched_at <= ? ORDER BY id",
                                      (A.OPEN, t - A.STALE))
        closed = 0
        for row in rows:
            if not await self.move(row["id"], A.EXPIRED, None, f"No decision in {A.STALE_DAYS} days", t):
                continue
            closed += 1
            if guild is None:
                continue
            await self.close_card(await self.card_of(guild, row), row["id"], A.EXPIRED, "Closed",
                                  f"No decision in {A.STALE_DAYS} days · <t:{t}:f>")
            await self.mod_log(guild, f"⌛ Ban appeal #{row['id']} from {tag(row['user_id'])} closed: nobody decided "
                                      f"it in {A.STALE_DAYS} days. They stay banned and can appeal again.")
        await self.db.execute("UPDATE appeals SET answers = '' WHERE status != ? AND decided_at <= ? AND answers != ''",
                              (A.OPEN, t - A.ANSWER_RETENTION))
        return closed


async def setup(bot) -> None:
    await bot.add_cog(Appeals(bot))
