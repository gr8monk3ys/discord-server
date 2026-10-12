"""Supporter tier (rules in logic/supporter.py): a cosmetic thank-you with no payment code.

Ko-fi's Discord integration or Discord Server Subscriptions gives a member the Supporter role;
this cog only reacts to the role (found by name, like every other role):

- Gaining it: one thank-you in general (pinging only that member; again only if they come
  back after 60 days, and at most 3 public thank-yous per 10 minutes so a bulk role sync can't
  flood the channel), this month's stipend, and a line in the mod log.
- While they hold it: a flair on /profile (cogs/achievements.py asks `profile_flair`), the
  private lounge channel (plain Discord permissions on the role, set up by public_mode.py) and
  a small coin stipend on the 1st at 12:15 local (ref supporterstipend:YYYY-MM:USER, established
  accounts only; logic/schedule.py semantics).
- Losing it: perks stop quietly. The flair and the lounge follow the role, so there is nothing
  to take back; coins already paid stay; the `supporters` row is closed and noted in the mod log.
- A 5-minute reconcile catches role changes the bot missed while it was down. The very first
  one only records who already holds the role (no thank-you posts for history).
- /support shows the perks and a link button when SUPPORT_URL is set and allowed; otherwise it
  says supporting isn't open yet. Staff /supporter grant|revoke is the manual path.

Needs the Server Members intent (role changes). Without the Supporter role nothing happens."""

import functools
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from cogs.lfg import ping_only
from cogs.selfroles import assignable
from logic import community as community_rules
from logic import quests as Q
from logic import recap as R
from logic import shop
from logic import supporter as S

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
STARTED_KEY = "supporter:started"  # meta: set once the first (silent) reconcile is done
STAFF_ONLY = "Only Moderators and Keepers can use this."
NO_ROLE = (f"There's no `{config.SUPPORTER_ROLE}` role on the server yet. Create it with "
           "`server/public_mode.py --apply`, then try again.")


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("supporter: %s failed", fn.__name__)
    return wrapper


def who(user) -> str:
    """'<@1> (name · `1`)': a mention that still reads right if the user leaves."""
    name = discord.utils.escape_markdown(getattr(user, "name", None) or str(user.id))
    return f"{user.mention} ({name} · `{user.id}`)"


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    perms = getattr(user, "guild_permissions", None)
    return community_rules.can_handle(guild.owner_id == user.id, bool(getattr(perms, "administrator", False)),
                                      getattr(user, "roles", []))


def is_supporter(member) -> bool:
    return config.match_by_name(getattr(member, "roles", []), config.SUPPORTER_ROLE) is not None


class Supporter(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.burst = S.Burst()
        self.manual: set[int] = set()  # members a /supporter command is changing right now
        self.warned_url = False

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

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

    @staticmethod
    def role(guild):
        return config.match_by_name(guild.roles, config.SUPPORTER_ROLE) if guild is not None else None

    @staticmethod
    def channel(guild, name: str):
        return config.match_by_name(guild.text_channels, name) if guild is not None else None

    async def meta(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def mod_log(self, guild, line: str, color: int = style.FOREST) -> None:
        channel = self.channel(guild, config.MOD_LOG_CHANNEL)
        if channel is None:
            return
        try:
            await channel.send(embed=style.embed(description=line, color=color), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("supporter: couldn't write to the mod log", exc_info=True)

    # ------------------------------------------------------------ gaining and losing the role
    @commands.Cog.listener()
    @never_raise
    async def on_member_update(self, before, after) -> None:
        if after.bot or not self.ours(after.guild):
            return
        had, has = is_supporter(before), is_supporter(after)
        if has and not had:
            await self.gained(after)
        elif had and not has:
            await self.lost(after)

    async def pay_stipend(self, user_id: int, month: str, t: int) -> int:
        """This month's stipend, once (the ref), for established accounts. Returns coins paid."""
        if not Q.established(user_id, t):
            return 0
        result = await economy.apply(self.db, user_id, S.STIPEND_COINS, S.STIPEND_REASON, t,
                                     ref=S.stipend_ref(month, user_id))
        return S.STIPEND_COINS if result.ok else 0

    async def gained(self, member, quiet: bool = False) -> bool:
        """Open the member's row, pay this month's stipend and thank them (once; `quiet`
        records the thank-you without posting). True when the row was opened by this call."""
        t = now()
        source = "manual" if member.id in self.manual else "role"
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT thanked_at, ended_at FROM supporters WHERE user_id = ?", (member.id,))
            if row is not None and row["ended_at"] is None:
                return False  # already counted (the event, the reconcile and a grant can all see it)
            thank = S.should_thank(row["thanked_at"] if row else None, t)
            thanked_at = t if thank else row["thanked_at"]
            await tx.execute(
                "INSERT INTO supporters (user_id, since, source, thanked_at, ended_at) VALUES (?, ?, ?, ?, NULL)"
                " ON CONFLICT (user_id) DO UPDATE SET since = excluded.since, source = excluded.source,"
                " thanked_at = excluded.thanked_at, ended_at = NULL", (member.id, t, source, thanked_at))
        coins = await self.pay_stipend(member.id, shop.month_key(t, self.tz), t)
        log.info("supporter: %s gained the role (%s, %d coins)", member.id, source, coins)
        guild = member.guild
        if source == "role" and not quiet:  # a manual grant writes its own line, naming the mod
            await self.mod_log(guild, f"💜 {who(member)} is now a {config.SUPPORTER_ROLE}.")
        channel = self.channel(guild, config.GENERAL_CHANNEL)
        if thank and not quiet and channel is not None and self.burst.allow(t):
            try:
                await channel.send(S.thanks_text(member.mention, coins), allowed_mentions=ping_only(users=[member]))
            except discord.HTTPException:
                log.warning("supporter: couldn't thank %s", member.id, exc_info=True)
        return True

    async def lost(self, member_or_id) -> bool:
        """Close the row. Nothing public: perks keyed on the role are already gone."""
        uid = getattr(member_or_id, "id", member_or_id)
        if not await self.db.execute("UPDATE supporters SET ended_at = ? WHERE user_id = ? AND ended_at IS NULL",
                                     (now(), uid)):
            return False
        log.info("supporter: %s no longer has the role", uid)
        if uid not in self.manual:
            name = who(member_or_id) if hasattr(member_or_id, "mention") else f"<@{uid}> (`{uid}`)"
            await self.mod_log(self.guild(), f"💜 {name} is no longer a {config.SUPPORTER_ROLE}.", style.MUTED)
        return True

    async def profile_flair(self, member) -> str | None:
        """The /profile line for a current supporter, else None (called by cogs/achievements.py)."""
        if not is_supporter(member):
            return None
        row = await self.db.fetchone("SELECT since FROM supporters WHERE user_id = ? AND ended_at IS NULL",
                                     (member.id,))
        return S.flair(row["since"] if row else None)

    # ------------------------------------------------------------ the tick
    @tasks.loop(minutes=5)
    async def tick(self) -> None:
        for name, job in (("reconcile", self.reconcile), ("stipend", self.run_stipend)):
            try:
                await job()
            except Exception:
                log.exception("supporter: %s failed", name)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def reconcile(self) -> None:
        """Bring `supporters` in line with who holds the role (changes missed while down)."""
        guild = self.guild()
        role = self.role(guild)
        # An unchunked member cache (no Server Members intent) would look like everyone lost it.
        if role is None or not getattr(guild, "chunked", True):
            return
        first = await self.meta(STARTED_KEY) is None
        holders = {m.id: m for m in role.members if not m.bot}
        active = {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM supporters"
                                                               " WHERE ended_at IS NULL")}
        for uid in sorted(set(holders) - active):
            await self.gained(holders[uid], quiet=first)
        for uid in sorted(active - set(holders)):
            await self.lost(guild.get_member(uid) or uid)
        if first:
            await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (STARTED_KEY, str(now())))

    async def run_stipend(self) -> None:
        job, t = S.STIPEND_JOB, now()
        seen_key = f"first_seen:{job.name}"
        first_seen = await self.meta(seen_key)
        first_seen = int(first_seen) if first_seen else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{job.name}:%",))}
        todo = R.plan_monthly(job, t, self.tz, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        # Marked done only after it worked: a failure retries on the next tick, and the refs
        # make that retry a no-op for anyone already paid.
        if todo.run is not None and await self.stipend(todo.run):
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (todo.run.key, now()))

    async def stipend(self, period: R.MonthPeriod) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        role = self.role(guild)
        month, t, paid = shop.month_key(period.scheduled_at, self.tz), now(), 0  # the month being paid for
        for m in list(role.members) if role is not None else []:
            if not m.bot:
                paid += bool(await self.pay_stipend(m.id, month, t))
        log.info("supporter stipend %s: paid %d", month, paid)
        return True

    # ------------------------------------------------------------ /support
    def support_url(self) -> str | None:
        raw = config.support_url_env()
        url = S.support_url(raw)
        if url is None and raw.strip() and not self.warned_url:
            self.warned_url = True  # said once, never the value itself
            log.warning("SUPPORT_URL is set but isn't an https link on %s: /support stays closed",
                        ", ".join(sorted(S.ALLOWED_HOSTS)))
        return url

    @app_commands.command(name="support", description="Support the server: the perks and where to chip in")
    async def support(self, interaction: discord.Interaction) -> None:
        url = self.support_url()
        if url is None:
            await interaction.response.send_message(S.NOT_OPEN, ephemeral=True)
            return
        embed = style.embed(title="Support the server", description=S.perks_text(),
                            footer=style.label("support", "cosmetic perks only"))
        view = discord.ui.View()
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Support the server", url=url))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /supporter grant|revoke
    supporter = app_commands.Group(name="supporter", description="Staff: give or take the Supporter role by hand",
                                   guild_only=True,
                                   default_permissions=discord.Permissions(moderate_members=True))

    async def manual_target(self, interaction: discord.Interaction, member):
        """The role to change for `member`, after the staff and role checks; replies and returns
        None when the command can't go ahead."""
        problem = None
        role = self.role(interaction.guild)
        if not is_staff(interaction.user):
            problem = STAFF_ONLY
        elif getattr(member, "bot", False) or not hasattr(member, "roles"):
            problem = "Pick a member of the server (not a bot)."
        elif role is None:
            problem = NO_ROLE
        elif not assignable(interaction.guild, role):
            problem = (f"I can't change `{config.SUPPORTER_ROLE}`: it has to be a plain role below Front Desk "
                       "(a Server Subscriptions role is managed by Discord).")
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return None
        return role

    async def change(self, interaction: discord.Interaction, member, grant: bool) -> None:
        role = await self.manual_target(interaction, member)
        if role is None:
            return
        if (role in member.roles) == grant:
            await interaction.response.send_message(
                f"{member.mention} {'already has' if grant else 'does not have'} `{config.SUPPORTER_ROLE}`.",
                ephemeral=True, allowed_mentions=NO_PINGS)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        mod = interaction.user
        reason = f"/supporter {'grant' if grant else 'revoke'} by {mod.name} ({mod.id})"
        self.manual.add(member.id)  # the role event may arrive first: it must not log as automatic
        try:
            if grant:
                await member.add_roles(role, reason=reason)
                await self.gained(member)
            else:
                await member.remove_roles(role, reason=reason)
                await self.lost(member)
        except discord.HTTPException:
            log.warning("supporter: %s failed", reason, exc_info=True)
            await interaction.followup.send("Discord didn't let me change that role (role order or permissions).",
                                            ephemeral=True)
            return
        finally:
            self.manual.discard(member.id)
        verb = "granted to" if grant else "revoked from"
        await self.mod_log(interaction.guild, f"💜 {config.SUPPORTER_ROLE} {verb} {who(member)}\n**By** {mod.mention}",
                           style.FOREST if grant else style.MUTED)
        await interaction.followup.send(f"{config.SUPPORTER_ROLE} {verb} {member.mention}.", ephemeral=True,
                                        allowed_mentions=NO_PINGS)

    @supporter.command(name="grant", description="Give a member the Supporter role by hand")
    @app_commands.describe(member="Who becomes a Supporter")
    async def grant(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self.change(interaction, member, grant=True)

    @supporter.command(name="revoke", description="Take the Supporter role away again")
    @app_commands.describe(member="Whose Supporter role to remove")
    async def revoke(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self.change(interaction, member, grant=False)


async def setup(bot) -> None:
    await bot.add_cog(Supporter(bot))
