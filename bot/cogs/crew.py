"""Welcome Crew (rules in logic/crew.py): newcomers get a hello from a person, not only the bot.

- /crew shows who's on the crew, their greetings this month and how to join; its Join / Leave
  buttons (DynamicItems, so they survive restarts) hand out the Welcome Crew role to members
  who pass the bar (established account, 14 days here, no moderation case in 30 days). A crew
  member who picks up a case loses the role at the next hourly check (logged to the mod log).
- When a member arrives (the same moment the Community welcome and the welcome card use: joined
  with Onboarding done or off, or Onboarding just finished) the arrival is recorded, and a
  1-minute tick posts ONE line in general pinging the crew role only (for fresh joins: someone
  who left and came back within 30 days is not announced again): at most once per
  CREW_PING_MINUTES (arrivals in between share the next ping), never in CREW_QUIET_HOURS, never
  during a join raid (the moderation cog's lockdown state, plus the same weighted join window
  counted here) and only while a crew member is online. Arrivals older than 12 hours are dropped.
- A greeting is recorded when the newcomer answers a crew member (see logic/crew.py for what
  counts). One per newcomer ever; the crew member earns coins through economy.py with a ref
  keyed on the newcomer, capped per day, and the Welcoming Committee badge at five greetings.
  Nothing is recorded when either side has turned tracking off. When someone opts out later
  their greetings are deleted (by /privacy at once, and by the hourly check); the bare arrival
  row (id and times) stays so a rejoin is still a rejoin, and the daily coin cap is counted in
  the ledger, so opting out and back in resets neither.
- /crew-stats (staff) and `digest_line()` for the owner digest.

Needs the Server Members intent (arrivals). With the Presence intent off, "is a crew member
online" can't be known and that one check is skipped. No message content is read: replies and
mentions come with the message. Without the Welcome Crew role the module does nothing."""

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
from cogs.lfg import ping_only
from cogs.moderation import RAID_KEY
from cogs.selfroles import assignable
from errors import reply_error
from logic import achievements as A
from logic import community as community_rules
from logic import crew as C
from logic import moderation as M
from logic import shop

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
LAST_PING_KEY = "crew:last_ping"  # meta: when the last crew ping was claimed (unix)
REASON = "Welcome Crew (/crew)"
MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)
ARRIVAL_DEDUPE = 10 * C.MIN  # join and onboarding events for one arrival
JOIN_SLACK = 2 * C.MIN  # a `joins` row this close to the member's joined_at is this join
UPKEEP_EVERY = C.HOUR
STAFF_ONLY = "Only staff can use /crew-stats."
NOT_HERE = "Use this in the server."


def now() -> int:
    return int(time.time())


def clock() -> float:
    """Sub-second time for the join-raid window (same as the moderation cog)."""
    return time.time()


def ts(dt) -> int | None:
    return int(dt.timestamp()) if dt is not None else None


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("crew: %s failed", fn.__name__)
    return wrapper


def who(user) -> str:
    """'<@1> (name · `1`)': a mention that still reads right if the user leaves."""
    name = discord.utils.escape_markdown(getattr(user, "name", None) or str(user.id))
    return f"{user.mention} ({name} · `{user.id}`)"


class CrewButton(discord.ui.DynamicItem[discord.ui.Button], template=r"crew:(?P<action>join|leave)"):
    def __init__(self, action: str):
        label, button_style = (("Join the crew", discord.ButtonStyle.success) if action == "join"
                               else ("Leave", discord.ButtonStyle.secondary))
        super().__init__(discord.ui.Button(label=label, style=button_style, custom_id=f"crew:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Crew" = interaction.client.get_cog("Crew")
        try:
            await cog.handle_button(interaction, self.action)
        except Exception:
            log.exception("crew button %s failed", self.action)
            await reply_error(interaction)


def build_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(CrewButton("join"))
    view.add_item(CrewButton("leave"))
    return view


class Crew(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.raid = M.RaidTracker()  # the moderation cog's rule, counted here too
        self.raid_until = 0.0
        self.hellos = C.Hellos()
        # Fresh arrivals still inside the greeting window: user id -> arrived at. Kept in
        # memory so on_message costs no query for everyone else; rebuilt from the table.
        self.newcomers: dict[int, int] | None = None
        self.last_upkeep = 0

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(CrewButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.bot.remove_dynamic_items(CrewButton)

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
        return config.match_by_name(guild.roles, config.CREW_ROLE) if guild is not None else None

    @staticmethod
    def channel(guild, name: str):
        return config.match_by_name(guild.text_channels, name) if guild is not None else None

    async def meta(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def mod_log(self, guild, line: str) -> None:
        channel = self.channel(guild, config.MOD_LOG_CHANNEL)
        if channel is None:
            return
        try:
            await channel.send(embed=style.embed(description=line, color=style.MUTED), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("crew: couldn't write to the mod log", exc_info=True)

    # ------------------------------------------------------------ arrivals
    async def onboarding_enabled(self, guild) -> bool:
        """Ask the Community cog (it caches the answer); without it ask Discord, and if Discord
        won't say assume Onboarding is on, so the arrival is the moment it's finished."""
        community = self.bot.get_cog("Community")
        if community is not None:
            return await community.onboarding_enabled(guild)
        try:
            return bool((await guild.onboarding()).enabled)
        except discord.HTTPException:
            return True

    @commands.Cog.listener()
    @never_raise
    async def on_member_join(self, member) -> None:
        if member.bot or not self.ours(member.guild):
            return
        t = clock()
        if self.raid.add(t, M.is_young(member.created_at.timestamp(), t)):
            self.raid_until = t + M.RAID_LOCK
        if community_rules.welcome_on_join(member.flags.completed_onboarding,
                                           await self.onboarding_enabled(member.guild)):
            await self.arrived(member)

    @commands.Cog.listener()
    @never_raise
    async def on_member_update(self, before, after) -> None:
        if after.bot or not self.ours(after.guild):
            return
        if community_rules.onboarding_completed(before.flags.completed_onboarding,
                                                after.flags.completed_onboarding):
            await self.arrived(after)

    async def arrived(self, member) -> bool:
        """Record an arrival for the next crew ping. True when this call recorded it."""
        t = now()
        if not await self.db.tracking_allowed(member.id):
            return False
        joined = ts(getattr(member, "joined_at", None)) or t
        async with self.db.transaction() as tx:
            prior = await tx.fetchone("SELECT at FROM crew_arrivals WHERE user_id = ?", (member.id,))
            if prior is not None and t - prior["at"] < ARRIVAL_DEDUPE:
                return False  # the join and the onboarding event both saw it
            # Earlier stays, from the growth cog's join log: when each ended (or began).
            rows = await tx.fetchall("SELECT COALESCE(left_at, joined_at) AS t FROM joins"
                                     " WHERE user_id = ? AND joined_at < ?", (member.id, joined - JOIN_SLACK))
            fresh = C.is_fresh(t, [prior["at"] if prior else None, *(r["t"] for r in rows)])
            await tx.execute("INSERT OR REPLACE INTO crew_arrivals (user_id, at, fresh, pinged_at, skipped)"
                             " VALUES (?, ?, ?, NULL, 0)", (member.id, t, int(fresh)))
        if fresh and self.newcomers is not None:
            self.newcomers[member.id] = t
        return True

    async def open_newcomers(self) -> dict[int, int]:
        if self.newcomers is None:
            rows = await self.db.fetchall(
                "SELECT user_id, at FROM crew_arrivals WHERE fresh = 1 AND at > ?"
                " AND user_id NOT IN (SELECT newcomer_id FROM crew_greetings)", (now() - C.WINDOW,))
            self.newcomers = {r["user_id"]: r["at"] for r in rows}
        return self.newcomers

    # ------------------------------------------------------------ the tick
    @tasks.loop(minutes=1)
    async def tick(self) -> None:
        for name, job in (("crew ping", self.flush), ("upkeep", self.upkeep)):
            try:
                await job()
            except Exception:
                log.exception("crew: %s failed", name)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def raid_active(self) -> bool:
        t = now()
        if t < self.raid_until:
            return True
        state = M.RaidState.loads(await self.meta(RAID_KEY))
        return state is not None and t < state.until

    def crew_online(self, role) -> bool | None:
        """None when presence isn't available (the intent is off): the check is skipped."""
        intents = getattr(self.bot, "intents", None)
        if intents is None or not intents.presences:
            return None
        return any(not m.bot and m.status is not discord.Status.offline for m in role.members)

    async def skip(self, user_ids) -> None:
        ids = list(user_ids)
        if ids:
            await self.db.execute(f"UPDATE crew_arrivals SET skipped = 1 WHERE pinged_at IS NULL"
                                  f" AND user_id IN ({','.join('?' * len(ids))})", ids)

    async def flush(self) -> bool:
        """Ping the crew for waiting arrivals if the rules allow it now. True when a ping was sent."""
        guild = self.guild()
        role = self.role(guild)
        if role is None:
            return False  # not set up: arrivals just go stale
        rows = await self.db.fetchall("SELECT user_id, at, fresh FROM crew_arrivals WHERE pinged_at IS NULL"
                                      " AND skipped = 0 ORDER BY at, user_id")
        if not rows:
            return False
        t = now()
        # Only a fresh join pings: an account that leaves and comes back can't ring the bell again.
        waiting = [r["user_id"] for r in rows
                   if r["fresh"] and not C.is_stale(r["at"], t) and guild.get_member(r["user_id"])]
        await self.skip({r["user_id"] for r in rows} - set(waiting))  # a rejoin, stale, or they left already
        if not waiting:
            return False
        last = await self.meta(LAST_PING_KEY)
        gap, quiet = config.crew_env()
        decision = C.ping_decision(now=t, tz=self.tz, last_ping_at=int(last) if last else None,
                                   gap=C.ping_gap(gap), quiet=C.quiet_hours(quiet),
                                   crew_online=self.crew_online(role), raid=await self.raid_active())
        if decision is C.Ping.DROP:
            await self.skip(waiting)
            log.info("crew: join raid, %d arrival(s) dropped without a ping", len(waiting))
            return False
        channel = self.channel(guild, config.GENERAL_CHANNEL)
        if decision is C.Ping.WAIT or channel is None:
            return False
        # Claimed before sending: a failing channel costs one ping, never a ping a minute.
        async with self.db.transaction() as tx:
            await tx.execute(f"UPDATE crew_arrivals SET pinged_at = ? WHERE pinged_at IS NULL AND skipped = 0"
                             f" AND user_id IN ({','.join('?' * len(waiting))})", (t, *waiting))
            await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (LAST_PING_KEY, str(t)))
        try:
            await channel.send(C.ping_text(role.mention, [f"<@{uid}>" for uid in waiting]),
                               allowed_mentions=ping_only(roles=[role]))
        except discord.HTTPException:
            log.warning("crew: couldn't post the crew ping", exc_info=True)
            return False
        log.info("crew: pinged for %d arrival(s)", len(waiting))
        return True

    async def recent_cases(self, user_id: int, t: int) -> int:
        row = await self.db.fetchone(
            f"SELECT COUNT(*) AS n FROM cases WHERE user_id = ? AND at > ?"
            f" AND kind IN ({','.join('?' * len(C.CASE_KINDS))})", (user_id, t - C.CLEAN_DAYS * C.DAY, *C.CASE_KINDS))
        return row["n"]

    async def upkeep(self) -> None:
        """Hourly: forget finished windows, delete what opted-out members left behind, and take
        the role back from crew members who picked up a moderation case."""
        t = now()
        if t - self.last_upkeep < UPKEEP_EVERY:
            return
        self.last_upkeep = t
        self.hellos.prune(t)
        if self.newcomers is not None:
            self.newcomers = {u: at for u, at in self.newcomers.items() if C.window_open(at, t)}
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM crew_greetings WHERE crew_id IN (SELECT user_id FROM privacy_optout)"
                             " OR newcomer_id IN (SELECT user_id FROM privacy_optout)")
            # The arrival row stays: it is what makes a later join a rejoin (no second ping, no
            # reward). It just never leads to a ping, and /crew-stats does not count it.
            await tx.execute("UPDATE crew_arrivals SET skipped = 1 WHERE pinged_at IS NULL"
                             " AND user_id IN (SELECT user_id FROM privacy_optout)")
        guild = self.guild()
        role = self.role(guild)
        if role is None or not assignable(guild, role):
            return
        for member in list(role.members):
            if member.bot or not await self.recent_cases(member.id, t):
                continue
            try:
                await member.remove_roles(role, reason="Welcome Crew: a moderation case in the last 30 days")
            except discord.HTTPException:
                log.warning("crew: couldn't take the role from %s", member.id, exc_info=True)
                continue
            await self.mod_log(guild, f"👋 {who(member)} is off the {config.CREW_ROLE}: "
                                      f"a moderation case in the last {C.CLEAN_DAYS} days.")

    # ------------------------------------------------------------ greetings
    @commands.Cog.listener()
    @never_raise
    async def on_message(self, message) -> None:
        author, guild = message.author, message.guild
        if author.bot or not self.ours(guild) or getattr(message, "webhook_id", None) is not None:
            return
        if getattr(message, "type", discord.MessageType.default) not in MESSAGE_TYPES:
            return
        role = self.role(guild)
        if role is None:
            return
        newcomers = await self.open_newcomers()
        replied = getattr(getattr(message, "reference", None), "resolved", None)
        reply_author = getattr(replied, "author", None)  # None for a deleted or uncached message
        mentioned = [m for m in getattr(message, "mentions", []) if not m.bot and m.id != author.id]
        t = now()
        if role in getattr(author, "roles", []):
            # A crew member addressing a newcomer: what makes a later mention of them count.
            for target in filter(None, [reply_author, *mentioned]):
                arrived_at = newcomers.get(target.id)
                if arrived_at is not None and C.window_open(arrived_at, t):
                    self.hellos.note(author.id, target.id, t)
        arrived_at = newcomers.get(author.id)
        if arrived_at is None:
            return
        greeter = C.pick_greeter(
            newcomer_id=author.id, arrived_at=arrived_at, crew_ids=[m.id for m in role.members if not m.bot],
            reply_to=getattr(reply_author, "id", None), reply_sent_at=ts(getattr(replied, "created_at", None)),
            mentioned=[m.id for m in mentioned], hellos=self.hellos)
        if greeter is not None:
            await self.greeted(guild, author.id, greeter, message.channel)

    async def greeted(self, guild, newcomer_id: int, crew_id: int, channel=None) -> bool:
        """Record the greeting and pay for it. True when this call recorded one."""
        t = now()
        start, end = C.day_bounds(t, self.tz)
        badge = False
        async with self.db.transaction() as tx:
            # The opt-out is read inside the write transaction (see cogs/stats.py).
            if await tx.fetchone("SELECT 1 FROM privacy_optout WHERE user_id IN (?, ?)", (newcomer_id, crew_id)):
                return False
            arrival = await tx.fetchone("SELECT at, fresh FROM crew_arrivals WHERE user_id = ?", (newcomer_id,))
            if arrival is None or not C.counts(newcomer_id=newcomer_id, crew_id=crew_id, arrived_at=arrival["at"],
                                               fresh=bool(arrival["fresh"]), now=t):
                return False
            if await tx.fetchone("SELECT 1 FROM crew_greetings WHERE newcomer_id = ?", (newcomer_id,)):
                return False  # someone got there first
            coins = C.coins_for(await economy.earned_tx(tx, crew_id, C.REASON, start, end))
            if coins and not (await economy.apply_tx(tx, crew_id, coins, C.REASON, t, ref=C.ref(newcomer_id))).ok:
                coins = 0  # this newcomer was paid for before (the row was deleted by an opt-out)
            await tx.execute("INSERT INTO crew_greetings (newcomer_id, crew_id, at, coins) VALUES (?, ?, ?, ?)",
                             (newcomer_id, crew_id, t, coins))
            total = await tx.fetchone("SELECT COUNT(*) AS n FROM crew_greetings WHERE crew_id = ?", (crew_id,))
            if total["n"] >= C.BADGE_AT and C.BADGE_KEY in A.BY_KEY:
                cur = await tx.execute("INSERT OR IGNORE INTO achievements (user_id, key, at) VALUES (?, ?, ?)",
                                       (crew_id, C.BADGE_KEY, t))
                badge = bool(cur.rowcount)
        if self.newcomers is not None:
            self.newcomers.pop(newcomer_id, None)
        log.info("crew: %s greeted %s (%d coins)", crew_id, newcomer_id, coins)
        if badge:
            await self.congratulate(guild, crew_id, channel)
        return True

    async def congratulate(self, guild, crew_id: int, channel) -> None:
        target = channel if hasattr(channel, "send") else self.channel(guild, config.GENERAL_CHANNEL)
        if target is None:
            return
        try:
            await target.send(A.congrats(f"<@{crew_id}>", [A.BY_KEY[C.BADGE_KEY]]),
                              allowed_mentions=ping_only(users=[discord.Object(crew_id)]))
        except discord.HTTPException:
            log.warning("crew: couldn't congratulate %s", crew_id, exc_info=True)

    # ------------------------------------------------------------ /crew
    async def month_counts(self, t: int) -> dict[int, int]:
        start, end = shop.month_bounds(shop.month_key(t, self.tz), self.tz)
        rows = await self.db.fetchall("SELECT crew_id, COUNT(*) AS n FROM crew_greetings WHERE at >= ? AND at < ?"
                                      " GROUP BY crew_id", (start, end))
        return {r["crew_id"]: r["n"] for r in rows}

    async def roster_embed(self, guild, role) -> discord.Embed:
        t = now()
        hidden = {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}
        counts = {u: n for u, n in (await self.month_counts(t)).items() if u not in hidden}
        members = {m.id: m for m in role.members if not m.bot}
        lines = [(members[uid].mention, counts.get(uid)) for uid in C.roster_order(counts, members)]
        embed = style.embed(title=config.CREW_ROLE, description=C.roster_text(lines),
                            footer=style.label("crew", f"{len(members)} on the crew"))
        embed.add_field(name="How it works", value=C.how_to_join(), inline=False)
        return embed

    @app_commands.command(name="crew", description="The Welcome Crew: who's on it, greetings this month, how to join")
    @app_commands.guild_only()
    async def crew(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not self.ours(guild):
            await interaction.response.send_message(NOT_HERE, ephemeral=True)
            return
        role = self.role(guild)
        if role is None:
            await interaction.response.send_message(C.NOT_SET_UP, ephemeral=True)
            return
        await interaction.response.send_message(embed=await self.roster_embed(guild, role), view=build_view(),
                                                allowed_mentions=NO_PINGS)

    async def handle_button(self, interaction: discord.Interaction, action: str) -> None:
        guild, member = interaction.guild, interaction.user

        async def say(text: str) -> None:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)

        if not self.ours(guild) or not hasattr(member, "roles"):
            return await say(NOT_HERE)
        role = self.role(guild)
        if role is None:
            return await say(C.NOT_SET_UP)
        if not assignable(guild, role):  # above Front Desk, managed, or edited to carry mod powers
            return await say(f"I can't hand out `{config.CREW_ROLE}` right now. Ask a mod.")
        have = role in member.roles
        if action == "leave":
            if not have:
                return await say("You're not on the crew.")
        elif have:
            return await say("You're already on the crew. Thanks for saying hi to people!")
        else:
            t = now()
            problem = C.join_problem(user_id=member.id, joined_at=ts(getattr(member, "joined_at", None)), now=t,
                                     recent_cases=await self.recent_cases(member.id, t))
            if problem:
                return await say(problem)
        try:
            if action == "leave":
                await member.remove_roles(role, reason=REASON)
            else:
                await member.add_roles(role, reason=REASON)
        except discord.HTTPException:
            log.warning("crew: couldn't change the role for %s", member.id, exc_info=True)
            return await say("I couldn't change that role right now. Ask a mod.")
        await say("You've left the crew. Thanks for helping out." if action == "leave" else
                  f"Welcome to the crew! You'll get a {role.mention} ping when someone new arrives. "
                  "Say hi; when they answer you, it counts.")

    # ------------------------------------------------------------ staff numbers
    async def stats(self, start: int, end: int) -> C.Stats:
        a = await self.db.fetchone(
            "SELECT COUNT(*) AS arrivals, COUNT(CASE WHEN pinged_at > 0 THEN 1 END) AS pinged,"
            " COUNT(DISTINCT CASE WHEN pinged_at > 0 THEN pinged_at END) AS pings"
            " FROM crew_arrivals WHERE at >= ? AND at < ?"
            " AND user_id NOT IN (SELECT user_id FROM privacy_optout)", (start, end))
        g = await self.db.fetchone("SELECT COUNT(*) AS greeted, COALESCE(SUM(coins), 0) AS coins"
                                   " FROM crew_greetings WHERE at >= ? AND at < ?", (start, end))
        top = await self.db.fetchall(
            "SELECT crew_id, COUNT(*) AS n FROM crew_greetings WHERE at >= ? AND at < ?"
            " AND crew_id NOT IN (SELECT user_id FROM privacy_optout)"
            " GROUP BY crew_id ORDER BY n DESC, crew_id LIMIT 3", (start, end))
        return C.Stats(arrivals=a["arrivals"], pinged=a["pinged"], pings=a["pings"], greeted=g["greeted"],
                       coins=g["coins"], top=tuple((r["crew_id"], r["n"]) for r in top))

    def crew_size(self) -> int:
        role = self.role(self.guild())
        return sum(1 for m in role.members if not m.bot) if role is not None else 0

    async def digest_line(self, start: int, end: int) -> str | None:
        """For the owner digest (cogs/recap.py): one line about [start, end), or None when the
        crew isn't set up and nobody arrived. Reads only; safe to call from another cog."""
        if self.role(self.guild()) is None:
            return None
        return C.digest_text(await self.stats(start, end), self.crew_size())

    @app_commands.command(name="crew-stats", description="Welcome Crew numbers: newcomers, pings, greetings (staff only)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def crew_stats(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message(STAFF_ONLY, ephemeral=True)
            return
        if self.role(self.guild()) is None:
            await interaction.response.send_message(C.NOT_SET_UP, ephemeral=True)
            return
        t = now()
        embed = style.embed(title=f"{config.CREW_ROLE} · numbers",
                            footer=style.label("crew", "staff", f"{self.crew_size()} on the crew"))
        for name, days in (("Last 7 days", 7), ("Last 30 days", 30)):
            embed.add_field(name=name, value=C.stats_text(await self.stats(t - days * C.DAY, t + 1)), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)


async def setup(bot) -> None:
    await bot.add_cog(Crew(bot))
