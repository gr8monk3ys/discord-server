"""Houses: three teams inside the server, a weekly House Cup and a season trophy.

- Membership. Every ten minutes established, non-bot members who allow tracking and have
  never had a house are placed in the smallest one (logic/houses.py `place`). `/house join`
  does the same on request, `/house leave` steps out and starts a 30-day wait before joining
  again, and nobody picks a house, so they stay balanced. Staff move a member with
  `/house-admin move`. Each house has a role (config.HOUSE_ROLES); the same sweep keeps it in
  step, and everything works without the roles (they are skipped until public_mode.py made
  them, and never given if they sit above the bot or carry moderator permissions). A sweep
  changes one member's roles at a time with a pause between. The first sweep that places
  anyone while no member has a house yet also posts a one-time introduction in the
  announcements channel (no pings; meta `houses:intro` keeps it to once).
- Scoring. Nothing is counted here and no other cog is hooked: logic/houses.py derives a
  week's points from the tables the other modules keep (formula in its docstring).
- `/house me` (your house, this week, your contribution, private), `/house standings`,
  `/house history` (House Cup trophies and recent weekly winners). Standings name houses only
  and are sent with AllowedMentions.none().
- Weekly close, Mondays 10:10 local in the announcements channel (ten minutes after the
  recap): last week's standings are frozen in house_results with the membership at that
  moment, then the winning house's contributors are paid WIN_REWARD coins each (ledger ref
  housecup:WEEK:USER) and the result is posted. The freeze happens before paying, so a retry
  pays the same people; the refs make it safe to repeat.
- Season close, the 1st of January/April/July/October at 10:20: once the last weekly close
  is settled, the quarter's tally (3/2/1 cup points per week) is frozen as the trophy.

Scheduled jobs follow logic/schedule.py semantics. Server Members intent: placement and the
"still in the server" check need the member cache; without it nobody is auto-placed and
rosters fall back to everyone with a house row.
"""

import asyncio
import functools
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from cogkit import is_staff
from logic import houses as H
from logic import quests as Q
from logic import recap as R
from logic.schedule import plan
from logic.selfroles import has_dangerous_permissions

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
HOUSE_CHOICES = [app_commands.Choice(name=f"{h.emoji} {h.name}", value=h.key) for h in H.HOUSES]
NO_HOUSE = "You're not in a house yet. `/house join` puts you in the one that needs you most."
TOO_NEW = (f"Houses open once your Discord account is {Q.MIN_ACCOUNT_DAYS} days old. "
           "Come back then and run `/house join`.")
STAFF_ONLY = "Only staff can move members between houses."
ROLE_PAUSE = 1.0  # seconds between two role changes in one sweep
INTRO_KEY = "houses:intro"  # meta: "due" while the one-time introduction is owed, then when it went out
INTRO_DUE = "due"
pause = asyncio.sleep


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Loops log and carry on."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            return await fn(self, *args)
        except Exception:
            log.exception("houses: %s failed", fn.__name__)
    return wrapper


class Houses(commands.Cog):
    house = app_commands.Group(name="house", description="Your house, the weekly House Cup and the season trophy",
                               guild_only=True)
    admin = app_commands.Group(name="house-admin", description="Staff: move a member to a house", guild_only=True,
                               default_permissions=discord.Permissions(moderate_members=True))

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.tick.start()
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.sweep.cancel()

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    # ------------------------------------------------------------ what the guild knows
    @staticmethod
    def present(guild) -> set[int] | None:
        """Ids of the humans in the server, or None when the member cache can't say."""
        members = list(getattr(guild, "members", None) or []) if guild is not None else []
        if not members or not getattr(guild, "chunked", True):
            return None
        return {m.id for m in members if not m.bot}

    def scoring_context(self, guild) -> dict:
        """Keyword arguments for logic/houses.py live() and standings()."""
        afk = getattr(guild, "afk_channel", None) if guild else None
        bots = {m.id for m in getattr(guild, "members", ()) if m.bot} if guild else set()
        return {"present": self.present(guild), "excluded_channels": {afk.id} if afk else set(), "bot_ids": bots}

    # ------------------------------------------------------------ roles
    @staticmethod
    def manageable(guild, role) -> bool:
        top = getattr(getattr(guild, "me", None), "top_role", None)
        return top is not None and top.position > role.position and not getattr(role, "managed", False)

    async def sync_role(self, member, house: str | None) -> bool:
        """Give `member` their house's role and take the other house roles away (all of them
        when `house` is None). Missing or unusable roles are skipped. Never raises. Returns
        True when it asked Discord for a change, so a sweep can space those out."""
        guild = getattr(member, "guild", None)
        if guild is None:
            return False
        asked = False
        try:
            have = list(getattr(member, "roles", []))
            wanted = config.match_by_name(guild.roles, H.BY_KEY[house].role) if house in H.BY_KEY else None
            names = {h.role for h in H.HOUSES}
            extra = [r for r in have if r.name in names and r is not wanted and self.manageable(guild, r)]
            if extra:
                asked = True
                await member.remove_roles(*extra, reason="Houses: not this member's house")
            if wanted is None or wanted in have:
                return asked
            if not self.manageable(guild, wanted):
                log.warning("houses: can't give %s: it isn't below the bot's role", wanted.name)
            elif has_dangerous_permissions(wanted):
                log.warning("houses: won't give %s: it has moderator permissions", wanted.name)
            else:
                asked = True
                await member.add_roles(wanted, reason="Houses: sorted into this house")
        except Exception:
            log.exception("houses: updating the house role for %s failed", member.id)
        return asked

    # ------------------------------------------------------------ membership
    async def place(self, user_id: int, source: str, t: int, present: set[int] | None,
                    house: str | None = None) -> str:
        """Put a member in a house (the smallest one unless staff named it) and return its key.
        One transaction, so two placements can't both see the same smallest house."""
        async with self.db.transaction() as tx:
            rows = await tx.fetchall("SELECT user_id, house FROM house_members WHERE left_at IS NULL AND user_id != ?",
                                     (user_id,))
            if house is None:
                house = H.place(user_id, H.sizes({r["user_id"]: r["house"] for r in rows
                                                  if present is None or r["user_id"] in present}))
            await tx.execute(
                "INSERT INTO house_members (user_id, house, joined_at, left_at, source) VALUES (?, ?, ?, NULL, ?)"
                " ON CONFLICT (user_id) DO UPDATE SET house = excluded.house, joined_at = excluded.joined_at,"
                " left_at = NULL, source = excluded.source", (user_id, house, t, source))
        log.info("houses: %s placed in %s (%s)", user_id, house, source)
        return house

    @tasks.loop(minutes=10)
    async def sweep(self) -> None:
        await self.run_sweep()

    @sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    @never_raise
    async def run_sweep(self) -> int:
        """Place established members who never had a house, then bring the house roles in step.
        Returns how many members were placed."""
        guild = self.guild()
        present = self.present(guild)
        if guild is None or present is None:
            return 0
        t = now()
        known = {r["user_id"]: (r["house"] if r["left_at"] is None else None)
                 for r in await self.db.fetchall("SELECT user_id, house, left_at FROM house_members")}
        optouts = {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}
        placed = 0
        members = sorted((m for m in guild.members if not m.bot), key=lambda m: m.id)
        for m in members:
            # A row with left_at set means they left on purpose: only /house join brings them back.
            if m.id in known or m.id in optouts or not Q.established(m.id, t):
                continue
            known[m.id] = await self.place(m.id, "auto", t, present)
            placed += 1
        if placed and placed == len(known):  # nobody had a house before this sweep: say what this is
            await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (INTRO_KEY, INTRO_DUE))
        await self.post_intro(guild, t)
        for m in members:
            # The first sweep gives everyone a role at once: one change at a time with a pause
            # between, well inside Discord's limit for role changes, instead of a burst.
            if await self.sync_role(m, known.get(m.id)):
                await pause(ROLE_PAUSE)
        return placed

    async def post_intro(self, guild, t: int) -> bool:
        """The one-time "what are houses" post, if it is still owed. Recorded in meta before it
        is sent (and put back if Discord refuses), so it can never go out twice."""
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (INTRO_KEY,))
        if row is None or row["value"] != INTRO_DUE:
            return False
        await self.db.execute("UPDATE meta SET value = ? WHERE key = ?", (str(t), INTRO_KEY))
        channel = self.announcements(guild)
        if channel is None:
            return False  # no announcements channel: skipped for good, quietly
        embed = style.embed(title="Houses are here", description=H.intro_text(),
                            footer=style.label("houses", "welcome"))
        try:
            await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("houses: couldn't post the introduction; retrying on the next sweep", exc_info=True)
            await self.db.execute("UPDATE meta SET value = ? WHERE key = ?", (INTRO_DUE, INTRO_KEY))
            return False
        log.info("houses: introduction posted")
        return True

    # ------------------------------------------------------------ scheduling
    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        await self.run_jobs()

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def run_jobs(self) -> None:
        await self.run_weekly()
        await self.run_season()

    async def due(self, name: str, make_plan):
        """Record first_seen and skipped periods; return the period to run now, if any."""
        seen_key = f"first_seen:{name}"
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (seen_key,))
        first_seen = int(row["value"]) if row else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{name}:%",))}
        t = now()
        todo = make_plan(t, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        return todo.run

    async def finish(self, period, action) -> bool:
        # Marked done only after it worked: a failure retries on the next tick.
        if await action(period):
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now()))
            return True
        return False

    @never_raise
    async def run_weekly(self) -> bool:
        job = H.WEEKLY_JOB
        period = await self.due(job.name, lambda t, done, seen: plan(job, t, self.tz, done, seen))
        return period is not None and await self.finish(period, self.close_week)

    @never_raise
    async def run_season(self) -> bool:
        job = H.SEASON_JOB
        period = await self.due(job.name, lambda t, done, seen: R.plan_monthly(job, t, self.tz, done, seen))
        return period is not None and await self.finish(period, self.close_season)

    def announcements(self, guild):
        return config.match_by_name(guild.text_channels, config.ANNOUNCEMENTS_CHANNEL)

    # ------------------------------------------------------------ weekly close
    async def freeze_week(self, guild, week: H.Week, t: int) -> tuple[list[H.Standing], list[int]]:
        """The week's frozen result, judging it now (with today's membership) if it's new."""
        done = await H.frozen_week(self.db, week.key)
        if done is not None:
            return done
        current = await H.live(self.db, week, t, **self.scoring_context(guild))
        first = H.winner(current.standings)
        paid = H.payees(current.membership, current.points, first.house if first else None)
        await self.db.execute(
            "INSERT OR IGNORE INTO house_results (kind, period, season, winner, data, closed_at)"
            " VALUES ('week', ?, ?, ?, ?, ?)",
            (week.key, H.week_season(week), first.house if first else None, H.dump_week(current.standings, paid), t))
        return await H.frozen_week(self.db, week.key)

    async def close_week(self, period) -> bool:
        guild = self.guild()
        if guild is None:
            return False
        week = H.previous_week(H.week_of(period.scheduled_at, self.tz), self.tz)
        t = now()
        standings, paid = await self.freeze_week(guild, week, t)
        first = H.winner(standings)
        if first is None:
            log.info("house cup %s: nobody scored, nothing posted", week.key)
            return True
        for uid in paid:
            # Refs make this safe to repeat after a failed announcement.
            await economy.apply(self.db, uid, H.WIN_REWARD, H.REASON, t, ref=H.reward_ref(week.key, uid))
        channel = self.announcements(guild)
        if channel is None:
            log.warning("house cup %s: decided, but no %s channel to announce it", week.key,
                        config.ANNOUNCEMENTS_CHANNEL)
            return True
        house = H.BY_KEY[first.house]
        embed = style.embed(title="House Cup · the week's winner", description=H.winner_text(week, standings, paid),
                            footer=style.label("house cup", week.key), color=house.color)
        try:
            await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("house cup %s: couldn't post; retrying", week.key, exc_info=True)
            return False
        log.info("house cup %s: %s wins, %d paid", week.key, first.house, len(paid))
        return True

    # ------------------------------------------------------------ season close
    async def close_season(self, period) -> bool:
        season = H.season_closing(period.month)
        if season is None:
            return True  # not a quarter end: nothing to judge this month
        guild = self.guild()
        if guild is None:
            return False
        # The quarter's last week closes on a Monday that can be this very day: wait for it.
        last_week = H.season_job_week(period.scheduled_at, self.tz)
        if not await self.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", (last_week,)):
            return False
        t = now()
        row = await self.db.fetchone("SELECT 1 FROM house_results WHERE kind = 'season' AND period = ?", (season,))
        tally = await H.season_standings(self.db, season)
        first = H.champion(tally)
        if row is None:
            await self.db.execute(
                "INSERT OR IGNORE INTO house_results (kind, period, season, winner, data, closed_at)"
                " VALUES ('season', ?, ?, ?, ?, ?)",
                (season, season, first.house if first else None, H.dump_season(tally), t))
        if first is None:
            log.info("house season %s: no weeks were won, no trophy", season)
            return True
        channel = self.announcements(guild)
        if channel is None:
            log.warning("house season %s: decided, but no %s channel to announce it", season,
                        config.ANNOUNCEMENTS_CHANNEL)
            return True
        embed = style.embed(title=f"House Cup · {H.season_label(season)}", description=H.trophy_text(season, tally),
                            footer=style.label("house cup", season), color=H.BY_KEY[first.house].color)
        try:
            await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("house season %s: couldn't post; retrying", season, exc_info=True)
            return False
        log.info("house season %s: %s takes the trophy", season, first.house)
        return True

    # ------------------------------------------------------------ embeds
    async def board(self, guild, t: int) -> tuple[H.Week, H.Live, list[H.Tally]]:
        week = H.week_of(t, self.tz)
        current = await H.live(self.db, week, t, **self.scoring_context(guild))
        return week, current, await H.season_standings(self.db, H.week_season(week))

    def standings_embed(self, week: H.Week, current: H.Live, tally, color: int = style.FOREST) -> discord.Embed:
        embed = style.embed(title=f"House Cup · {week.key}",
                            description=f"{H.standings_text(current.standings)}\n\nThe week closes <t:{week.end}:R>.",
                            footer=style.label("house cup", week.key), color=color)
        embed.add_field(name=f"Season · {H.season_label(H.week_season(week))}", value=H.season_text(tally), inline=False)
        return embed

    # ------------------------------------------------------------ /house
    @house.command(name="me", description="Your house, this week's standings and what you've brought in")
    async def me(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        t = now()
        key = await H.house_of(self.db, member.id)
        if key is None:
            await interaction.response.send_message(NO_HOUSE, ephemeral=True)
            return
        house = H.BY_KEY[key]
        week, current, tally = await self.board(interaction.guild, t)
        embed = self.standings_embed(week, current, tally, color=house.color)
        embed.title = f"{house.title} · {week.key}"
        if not await self.db.tracking_allowed(member.id):
            mine = "Your stats are off (`/privacy`), so your activity isn't counted for the house."
        elif not Q.established(member.id, t):
            mine = f"Your activity starts counting once your Discord account is {Q.MIN_ACCOUNT_DAYS} days old."
        else:
            mine = H.contribution_text(current.units.get(member.id, {}))
        embed.add_field(name="Your contribution this week", value=mine[:1024], inline=False)
        embed.add_field(name="How it works", value=H.how_text(), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @house.command(name="standings", description="This week's House Cup standings and the season table")
    async def standings(self, interaction: discord.Interaction) -> None:
        week, current, tally = await self.board(interaction.guild, now())
        embed = self.standings_embed(week, current, tally)
        embed.add_field(name="How it works", value=H.how_text(), inline=False)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

    @house.command(name="join", description="Get sorted into a house (the one that needs you most)")
    async def join(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        t = now()
        if getattr(member, "bot", False) or not Q.established(member.id, t):
            await interaction.response.send_message(TOO_NEW, ephemeral=True)
            return
        row = await self.db.fetchone("SELECT house, left_at FROM house_members WHERE user_id = ?", (member.id,))
        if row is not None and row["left_at"] is None and row["house"] in H.BY_KEY:
            await self.sync_role(member, row["house"])
            await interaction.response.send_message(f"You're already in {H.BY_KEY[row['house']].title}.",
                                                    ephemeral=True)
            return
        if row is not None and row["left_at"] is not None and not H.can_rejoin(row["left_at"], t):
            await interaction.response.send_message(
                f"You left your house recently. You can join again <t:{H.rejoin_at(row['left_at'])}:R>.",
                ephemeral=True)
            return
        house = H.BY_KEY[await self.place(member.id, "join", t, self.present(interaction.guild))]
        await self.sync_role(member, house.key)
        note = ("" if await self.db.tracking_allowed(member.id)
                else " Your stats are off (`/privacy`), so your activity won't score until you turn them on.")
        await interaction.response.send_message(
            f"Welcome to {house.title}. Everything you do here now counts for your house; "
            f"see how it's going with `/house me`.{note}", ephemeral=True)

    @house.command(name="leave", description="Leave your house (you can't join one again for 30 days)")
    async def leave(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        t = now()
        changed = await self.db.execute("UPDATE house_members SET left_at = ? WHERE user_id = ? AND left_at IS NULL",
                                        (t, member.id))
        if not changed:
            await interaction.response.send_message("You're not in a house.", ephemeral=True)
            return
        await self.sync_role(member, None)
        log.info("houses: %s left their house", member.id)
        await interaction.response.send_message(
            f"You've left your house. You can `/house join` again <t:{H.rejoin_at(t)}:R>.", ephemeral=True)

    @house.command(name="history", description="House Cup trophies and the recent weekly winners")
    async def history(self, interaction: discord.Interaction) -> None:
        seasons = await self.db.fetchall("SELECT period, winner FROM house_results WHERE kind = 'season'"
                                         " ORDER BY period DESC LIMIT 8")
        weeks = await self.db.fetchall("SELECT period, winner FROM house_results WHERE kind = 'week'"
                                       " ORDER BY period DESC LIMIT ?", (H.HISTORY_WEEKS,))
        text = H.history_text([(r["period"], r["winner"]) for r in seasons], [(r["period"], r["winner"]) for r in weeks])
        embed = style.embed(title="House Cup history", description=text[:4000], footer=style.label("house cup", "history"))
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /house-admin
    @admin.command(name="move", description="Staff: put a member in a house (or move them to another one)")
    @app_commands.describe(member="Who to move", house="The house they go to")
    @app_commands.choices(house=HOUSE_CHOICES)
    async def move(self, interaction: discord.Interaction, member: discord.Member,
                   house: app_commands.Choice[str]) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        target = H.BY_KEY.get(house.value)
        if target is None or getattr(member, "bot", False):
            await interaction.response.send_message("Bots don't get a house." if target else "No such house.",
                                                    ephemeral=True)
            return
        await self.place(member.id, "staff", now(), self.present(interaction.guild), house=target.key)
        await self.sync_role(member, target.key)
        log.info("houses: %s moved %s to %s", interaction.user.id, member.id, target.key)
        await interaction.response.send_message(
            f"{member.mention} is now in {target.title}. This week's points count for the house they're in "
            "when the week closes.", ephemeral=True, allowed_mentions=NO_PINGS)


async def setup(bot) -> None:
    await bot.add_cog(Houses(bot))
