"""Growth: invite tracking (who brought whom, who stayed, the Recruiter role) and
Disboard bump reminders. /invites, /recruiters, /bumpers, /bumpping.

Join tracking needs the Server Members intent; reading invites needs Manage Server.
Without either the cog still loads: no intent = no join events, no Manage Server =
joins recorded with no inviter."""

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from errors import reply_error
from logic import growth as G
from logic import stats as S

log = logging.getLogger(__name__)

WINDOW = 30 * G.DAY  # /recruiters and /bumpers look back this far
BUMP_DUE_KEY = "bump_due"
REMINDER_TEXT = "Time to bump! Run `/bump`"


def now() -> int:
    return int(time.time())


def embed_texts(message) -> list[str]:
    out = []
    for e in getattr(message, "embeds", None) or []:
        out += [e.title, e.description]
        out += [t for f in getattr(e, "fields", []) for t in (f.name, f.value)]
    return [t for t in out if t]


def bump_command(message) -> tuple[str | None, object | None]:
    """(command name, user) of the slash command this message answers.

    interaction_metadata has the user but (in discord.py 2.7) no command name; the
    deprecated message.interaction has both, so read its backing field to avoid the warning."""
    meta = getattr(message, "interaction_metadata", None)
    legacy = getattr(message, "_interaction", None)
    name = getattr(meta, "name", None) or getattr(legacy, "name", None)
    user = getattr(meta, "user", None) or getattr(legacy, "user", None)
    return name, user


def can_manage(guild, role) -> bool:
    me = getattr(guild, "me", None)
    return me is not None and me.top_role > role


class Growth(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.join_lock = asyncio.Lock()  # one invite diff at a time
        self.invites_blocked = False  # logged the missing Manage Server once
        self.limits: dict[str, tuple[int, int]] = {}  # code -> (max_uses, uses) from the last fetch

    async def cog_load(self) -> None:
        self.bump_reminder.start()
        self.recruiter_sweep.start()

    async def cog_unload(self) -> None:
        self.bump_reminder.cancel()
        self.recruiter_sweep.cancel()

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def ours(self, guild) -> bool:
        return guild is not None and getattr(guild, "id", None) == self.bot.settings.guild_id

    # ------------------------------------------------------------ invite cache
    async def snapshot(self, guild) -> G.Snapshot | None:
        """Every invite's (inviter, uses) right now, or None without Manage Server."""
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            if not self.invites_blocked:
                log.warning("can't read invites (needs Manage Server): joins are recorded without an inviter")
                self.invites_blocked = True
            return None
        if self.invites_blocked:
            log.info("invite tracking is working again")
            self.invites_blocked = False
        snap = {i.code: (i.inviter.id if i.inviter else None, i.uses or 0) for i in invites}
        self.limits = {i.code: (i.max_uses or 0, i.uses or 0) for i in invites}
        if getattr(guild, "vanity_url_code", None):
            try:
                vanity = await guild.vanity_invite()
            except discord.HTTPException:
                log.debug("couldn't read the vanity invite", exc_info=True)
            else:
                if vanity is not None:
                    snap[vanity.code] = (None, vanity.uses or 0)
        return snap

    async def cached(self) -> G.Snapshot:
        rows = await self.db.fetchall("SELECT code, inviter_id, uses FROM invite_uses")
        return {r["code"]: (r["inviter_id"], r["uses"]) for r in rows}

    @staticmethod
    async def store(tx, snap: G.Snapshot) -> None:
        await tx.execute("DELETE FROM invite_uses")
        for code, (inviter, uses) in snap.items():
            await tx.execute("INSERT INTO invite_uses (code, inviter_id, uses) VALUES (?, ?, ?)",
                             (code, inviter, uses))

    async def sync_invites(self) -> None:
        guild = self.guild()
        if guild is None:
            return
        async with self.join_lock:
            snap = await self.snapshot(guild)
            if snap is None:
                return
            async with self.db.transaction() as tx:
                await self.store(tx, snap)
        log.info("invite cache: %d invites", len(snap))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        try:
            await self.sync_invites()
        except Exception:
            log.exception("invite sync failed")

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        try:
            if not self.ours(invite.guild):
                return
            inviter = invite.inviter.id if invite.inviter else None
            self.limits[invite.code] = (invite.max_uses or 0, invite.uses or 0)
            await self.db.execute(
                "INSERT INTO invite_uses (code, inviter_id, uses) VALUES (?, ?, ?)"
                " ON CONFLICT (code) DO UPDATE SET inviter_id = excluded.inviter_id, uses = excluded.uses",
                (invite.code, inviter, invite.uses or 0))
        except Exception:
            log.exception("on_invite_create failed")

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        try:
            if not self.ours(invite.guild):
                return
            max_uses, uses = self.limits.pop(invite.code, (0, 0))
            if max_uses and uses >= max_uses - 1:
                # Probably used up by a join that's about to arrive: keep it so the
                # join's diff can see it disappear. The next diff drops it.
                log.debug("invite %s used up; kept for the join diff", invite.code)
                return
            await self.db.execute("DELETE FROM invite_uses WHERE code = ?", (invite.code,))
        except Exception:
            log.exception("on_invite_delete failed")

    # ------------------------------------------------------------ joins and leaves
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            recorded = await self.record_join(member)
        except Exception:
            log.exception("recording join of %s failed", getattr(member, "id", "?"))
            return
        if recorded:
            try:
                await self.award_recruiters()
            except Exception:
                log.exception("recruiter check failed")

    async def record_join(self, member) -> bool:
        if member.bot or not self.ours(member.guild):
            return False
        async with self.join_lock:
            old = await self.cached()
            new = await self.snapshot(member.guild)
            code = G.attribute_join(old, new) if new is not None else None
            inviter = G.inviter_of(code, old, new or {})
            t = now()
            async with self.db.transaction() as tx:
                # A leave we missed (bot offline) shouldn't leave them "here" twice.
                await tx.execute("UPDATE joins SET left_at = ? WHERE user_id = ? AND left_at IS NULL", (t, member.id))
                await tx.execute("INSERT INTO joins (user_id, joined_at, inviter_id, invite_code) VALUES (?, ?, ?, ?)",
                                 (member.id, t, inviter, code))
                if new is not None:
                    await self.store(tx, new)
        log.info("join %s via %s (inviter %s)", member.id, code or "unknown invite", inviter)
        return True

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            if member.bot or not self.ours(member.guild):
                return
            await self.db.execute(
                "UPDATE joins SET left_at = ? WHERE id = (SELECT id FROM joins WHERE user_id = ? AND left_at IS NULL"
                " ORDER BY joined_at DESC, id DESC LIMIT 1)",
                (now(), member.id))
        except Exception:
            log.exception("recording leave of %s failed", getattr(member, "id", "?"))

    async def joins(self, inviter: int | None = None) -> list[G.Join]:
        sql = "SELECT user_id, inviter_id, joined_at, left_at FROM joins WHERE inviter_id IS NOT NULL"
        params = ()
        if inviter is not None:
            sql += " AND inviter_id = ?"
            params = (inviter,)
        rows = await self.db.fetchall(sql, params)
        return [G.Join(r["user_id"], r["inviter_id"], r["joined_at"], r["left_at"]) for r in rows]

    # ------------------------------------------------------------ Recruiter role
    async def award_recruiters(self) -> int:
        """Give the Recruiter role to everyone over the threshold. Never removes it."""
        guild = self.guild()
        role = config.match_by_name(guild.roles, config.RECRUITER_ROLE) if guild else None
        if role is None:
            return 0
        if not can_manage(guild, role):
            log.info("can't give %s: it's not below my top role", config.RECRUITER_ROLE)
            return 0
        given = 0
        for uid in sorted(G.recruiters(G.stayed_counts(await self.joins(), now()))):
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except discord.HTTPException:
                    continue  # left the server
            if member.bot or role in member.roles:
                continue
            try:
                await member.add_roles(role, reason="3+ invited members stayed")
                given += 1
                log.info("gave %s to %s", config.RECRUITER_ROLE, uid)
            except discord.HTTPException:
                log.warning("couldn't give %s to %s", config.RECRUITER_ROLE, uid, exc_info=True)
        return given

    @tasks.loop(hours=24)
    async def recruiter_sweep(self) -> None:
        # Recruits only count as "stayed" days after joining, so check on a timer too.
        try:
            await self.award_recruiters()
        except Exception:
            log.exception("recruiter sweep failed")

    @recruiter_sweep.before_loop
    async def before_recruiter_sweep(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ Disboard bumps
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self.check_bump(message)
        except Exception:
            log.exception("bump check failed")

    async def check_bump(self, message) -> bool:
        if message.author.id != config.DISBOARD_BOT_ID or not self.ours(message.guild):
            return False
        name, user = bump_command(message)
        status = G.bump_status(message.author.id, name, embed_texts(message), config.DISBOARD_BOT_ID)
        if status is None:
            return False
        if status == G.BUMP_UNVERIFIED:
            log.debug("counted a /bump reply without reading its embed (no Message Content intent?)")
        t = now()
        async with self.db.transaction() as tx:
            if user is not None:
                await tx.execute("INSERT INTO bumps (user_id, at) VALUES (?, ?)", (user.id, t))
            await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                             (BUMP_DUE_KEY, str(G.reminder_due_at(t))))
        log.info("bump by %s; reminder at +%ds", getattr(user, "id", "unknown"), G.REMINDER_DELAY)
        return True

    @tasks.loop(minutes=1)
    async def bump_reminder(self) -> None:
        try:
            await self.send_due_reminder()
        except Exception:
            log.exception("bump reminder failed")

    @bump_reminder.before_loop
    async def before_bump_reminder(self) -> None:
        await self.bot.wait_until_ready()

    async def clear_due(self, value: str) -> None:
        # Only the value we acted on: a bump recorded meanwhile keeps its own reminder.
        await self.db.execute("DELETE FROM meta WHERE key = ? AND value = ?", (BUMP_DUE_KEY, value))

    async def send_due_reminder(self) -> bool:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (BUMP_DUE_KEY,))
        if row is None or not G.reminder_ready(int(row["value"]), now()):
            return False
        guild = self.guild()
        if guild is None:
            return False
        channel = config.match_by_name(guild.text_channels, config.BOT_COMMANDS_CHANNEL)
        if channel is None:
            log.warning("bump reminder skipped: no %s channel", config.BOT_COMMANDS_CHANNEL)
            await self.clear_due(row["value"])
            return False
        role = config.match_by_name(guild.roles, config.BUMPER_ROLE)
        content = f"{role.mention} {REMINDER_TEXT}" if role else REMINDER_TEXT
        mentions = discord.AllowedMentions(everyone=False, users=False, roles=[role] if role else False)
        try:
            await channel.send(content, allowed_mentions=mentions)
        except discord.Forbidden:
            log.warning("bump reminder skipped: can't post in %s", config.BOT_COMMANDS_CHANNEL)
            await self.clear_due(row["value"])
            return False
        # Other HTTP errors propagate: the key stays and the next tick retries.
        await self.clear_due(row["value"])
        log.info("posted bump reminder")
        return True

    # ------------------------------------------------------------ commands
    @app_commands.command(name="invites", description="How many people someone has brought in, and who stayed")
    @app_commands.describe(member="Whose invites (default: you)")
    async def invites(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        s = G.summary(await self.joins(member.id), member.id, now())
        days = G.STAY_SECONDS // G.DAY
        text = (f"`INVITED`  {s.total}   `STILL HERE`  {s.still_here}   `STAYED {days}D+`  {s.stayed}"
                if s.total else "No tracked invites yet.")
        embed = style.embed(title=member.display_name, description=text,
                            footer=style.label("invites", f"stayed = still here {days} days after joining"))
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def board_text(self, scores: dict[int, int], me: int, unit: str) -> str:
        top, mine = S.rank(scores, limit=10, me=me)
        if not top:
            return "Nobody's on the board yet."
        rows = [f"`{r.rank:02}`  <@{r.user_id}>  {r.score} {unit}" for r in top]
        if mine:
            rows += ["…", f"`{mine.rank:02}`  <@{mine.user_id}>  {mine.score} {unit}"]
        return "\n".join(rows)

    @app_commands.command(name="recruiters", description="Who brought in the most people who stayed (past 30 days)")
    async def recruiters(self, interaction: discord.Interaction) -> None:
        t = now()
        scores = G.stayed_counts(await self.joins(), t, since=t - WINDOW)
        embed = style.embed(title="Recruiters · Past 30 days",
                            description=self.board_text(scores, interaction.user.id, "stayed"),
                            footer=style.label("recruiters", f"stayed = {G.STAY_SECONDS // G.DAY}+ days"))
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="bumpers", description="Who bumped the server on Disboard most (past 30 days)")
    async def bumpers(self, interaction: discord.Interaction) -> None:
        rows = await self.db.fetchall("SELECT user_id, COUNT(*) AS n FROM bumps WHERE at >= ? GROUP BY user_id",
                                      (now() - WINDOW,))
        scores = {r["user_id"]: r["n"] for r in rows}
        embed = style.embed(title="Bumpers · Past 30 days",
                            description=self.board_text(scores, interaction.user.id, "bumps"),
                            footer=style.label("bumpers", "run /bump every 2 hours"))
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="bumpping", description="Get pinged when the server can be bumped on Disboard")
    @app_commands.choices(ping=[app_commands.Choice(name="on: ping me when it's time to bump", value="on"),
                                app_commands.Choice(name="off: stop pinging me", value="off")])
    async def bumpping(self, interaction: discord.Interaction, ping: app_commands.Choice[str]) -> None:
        guild = interaction.guild
        role = config.match_by_name(guild.roles, config.BUMPER_ROLE) if guild else None
        if role is None:
            await reply_error(interaction, f"There's no {config.BUMPER_ROLE} role on this server yet. Ask a mod.")
            return
        cant = (f"I can't hand out the {config.BUMPER_ROLE} role: it's above my highest role. "
                "Ask a mod to move my role up.")
        if not can_manage(guild, role):
            await reply_error(interaction, cant)
            return
        member = interaction.user
        try:
            if ping.value == "on":
                if role not in member.roles:
                    await member.add_roles(role, reason="/bumpping on")
                text = "You'll get pinged in the bot channel when the server can be bumped again."
            else:
                if role in member.roles:
                    await member.remove_roles(role, reason="/bumpping off")
                text = "Bump pings are off."
        except discord.Forbidden:
            await reply_error(interaction, cant)
            return
        await interaction.response.send_message(text, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Growth(bot))
