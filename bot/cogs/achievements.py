"""Achievements: 20 badges earned from what members already do here (squads, voice,
messages, clips, streaks, tournaments...), granted once each and stored in the
`achievements` table, plus /profile and /badges.

Badges are worked out from the existing tables, never from counters of our own, so
a restart or a missed event can't lose one: members are re-checked shortly after
they do something (a message, a slash command or button, leaving voice, a weekly
award) and everyone is swept hourly. The very first sweep grants silently, so
switching the module on doesn't flood general with old achievements. Opted-out
members (/privacy off) never get stat-based badges and their stats aren't shown."""

import asyncio
import functools
import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from cogs.lfg import ping_only
from logic import achievements as A
from logic import growth as G
from logic import shop
from logic import stats as S

log = logging.getLogger(__name__)

BACKFILL_KEY = "achievements:backfilled"  # meta: set once the first (silent) sweep is done
MESSAGE_COOLDOWN = 10 * 60  # re-check a chatter at most this often
INTERACTION_COOLDOWN = 2 * 60  # and a clicker (commands, buttons) at most this often
EVENT_DELAY = 5  # seconds: let the other cogs write their rows first
SWEEP_POST_LIMIT = 10  # congratulation posts per sweep at most; the rest are granted quietly
# Never congratulate in these (counting would break, the others are read-only or curated).
QUIET_CHANNELS = (config.COUNTING_CHANNEL, config.RULES_CHANNEL, config.ANNOUNCEMENTS_CHANNEL,
                  config.HALL_OF_FAME_CHANNEL, config.CREATORS_CHANNEL, config.WELCOME_CHANNEL,
                  config.CLIPS_CHANNEL, config.TOURNAMENTS_CHANNEL)


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Listeners log and carry on: one bad event must never break the others."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            await fn(self, *args)
        except Exception:
            log.exception("achievements: %s failed", fn.__name__)
    return wrapper


def _in(column: str, ids) -> tuple[str, tuple]:
    """An optional `AND column IN (...)` filter."""
    if ids is None:
        return "", ()
    ids = tuple(ids)
    return f" AND {column} IN ({','.join('?' * len(ids))})", ids


class Achievements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.delay = EVENT_DELAY
        self.pending: dict[int, asyncio.Task] = {}
        self.last_message_check: dict[int, int] = {}
        self.last_interaction_check: dict[int, int] = {}

    async def cog_load(self) -> None:
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.sweep.cancel()
        for task in self.pending.values():
            task.cancel()
        self.pending.clear()

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

    # ------------------------------------------------------------ reading facts
    async def counts(self, sql: str, ids) -> dict[int, int]:
        """`sql` is "SELECT <col> AS uid, <n> AS n ... WHERE ...": the id filter extends the WHERE."""
        col = re.match(r"SELECT (\S+) AS uid", sql).group(1)
        extra, params = _in(col, ids)
        rows = await self.db.fetchall(sql + extra + " GROUP BY uid", params)
        return {r["uid"]: r["n"] for r in rows if r["uid"] is not None}

    async def voice_seconds(self, ids) -> dict[int, int]:
        """Counted voice (2+ people, AFK excluded), all time, using everyone's sessions
        in the channels these members used (company counts)."""
        if ids is None:
            sql, params = 'SELECT user_id, channel_id, start, "end" FROM voice_sessions', ()
        else:
            extra, params = _in("user_id", ids)
            sql = ('SELECT user_id, channel_id, start, "end" FROM voice_sessions WHERE channel_id IN'
                   f" (SELECT DISTINCT channel_id FROM voice_sessions WHERE 1 = 1{extra})")
        rows = await self.db.fetchall(sql, params)
        guild = self.guild()
        afk = getattr(guild, "afk_channel", None) if guild else None
        excluded = {afk.id} if afk is not None else set()
        t = now()
        sessions = [S.Session(r["user_id"], r["channel_id"], r["start"], r["end"]) for r in rows]
        # Sorting every join/leave is CPU work: keep it off the event loop.
        return await asyncio.to_thread(S.counted_voice_seconds, sessions, 0, t, t, excluded_channels=excluded)

    async def recruiters(self, ids=None) -> set[int]:
        """Recruiters among `ids` (None: everyone). Only these inviters' joins are read."""
        extra, params = _in("inviter_id", ids)
        rows = await self.db.fetchall(
            f"SELECT user_id, inviter_id, joined_at, left_at FROM joins WHERE 1 = 1{extra}", params)
        joins = [G.Join(r["user_id"], r["inviter_id"], r["joined_at"], r["left_at"]) for r in rows]
        return G.recruiters(G.stayed_counts(joins, now()))

    async def gather(self, ids=None) -> dict[int, A.Facts]:
        """Facts for these members (None: everyone who appears anywhere, plus the guild's members)."""
        q = {
            "squads_joined": "SELECT m.user_id AS uid, COUNT(*) AS n FROM lfg_members m"
                             " JOIN lfg_posts p ON p.id = m.post_id WHERE m.user_id != p.host_id",
            "squads_hosted": "SELECT host_id AS uid, COUNT(*) AS n FROM lfg_posts WHERE 1 = 1",
            "gamenights_hosted": "SELECT host_id AS uid, COUNT(*) AS n FROM gamenights WHERE 1 = 1",
            "messages": "SELECT user_id AS uid, SUM(count) AS n FROM message_counts WHERE 1 = 1",
            "clips": "SELECT user_id AS uid, COUNT(*) AS n FROM clips WHERE 1 = 1",
            "clip_week_wins": "SELECT winner_id AS uid, COUNT(*) AS n FROM clip_polls WHERE winner_id IS NOT NULL",
            "hall_of_fame": "SELECT author_id AS uid, COUNT(*) AS n FROM starboard"
                            " WHERE board_message_id IS NOT NULL AND board_message_id != 0",
            "mvp_wins": "SELECT user_id AS uid, COUNT(*) AS n FROM ledger WHERE reason = 'mvp' AND delta > 0",
            "daily_streak": "SELECT user_id AS uid, MAX(daily_streak) AS n FROM wallets WHERE 1 = 1",
            "balance": "SELECT user_id AS uid, MAX(balance) AS n FROM wallets WHERE 1 = 1",
            "tournaments_entered": "SELECT user_id AS uid, COUNT(*) AS n FROM tournament_entries WHERE 1 = 1",
            "tournament_wins": "SELECT winner_id AS uid, COUNT(*) AS n FROM tournaments WHERE winner_id IS NOT NULL",
            "birthday_set": "SELECT user_id AS uid, COUNT(*) AS n FROM birthdays WHERE 1 = 1",
        }
        values = {name: await self.counts(sql, ids) for name, sql in q.items()}
        values["voice_seconds"] = await self.voice_seconds(ids)
        extra, params = _in("user_id", ids)
        optout = {r["user_id"] for r in await self.db.fetchall(
            f"SELECT user_id FROM privacy_optout WHERE 1 = 1{extra}", params)}
        recruiters = await self.recruiters(ids)
        guild = self.guild()

        everyone = set(ids) if ids is not None else (
            {u for d in values.values() for u in d} | recruiters
            | {m.id for m in (guild.members if guild else []) if not m.bot})
        facts = {}
        for uid in everyone:
            member = guild.get_member(uid) if guild else None
            tracking = uid not in optout
            kw = {name: int(d.get(uid, 0) or 0) for name, d in values.items()}
            if not tracking:  # /privacy off deletes these, but never count leftovers
                kw["voice_seconds"] = kw["messages"] = 0
            kw["birthday_set"] = kw["birthday_set"] > 0
            facts[uid] = A.Facts(**kw, recruiter=uid in recruiters, tracking=tracking,
                                 early_member=A.is_early(getattr(member, "joined_at", None), self.tz))
        return facts

    async def held(self, user_id: int) -> set[str]:
        rows = await self.db.fetchall("SELECT key FROM achievements WHERE user_id = ?", (user_id,))
        return {r["key"] for r in rows}

    # ------------------------------------------------------------ granting
    async def grant(self, user_id: int, badges: list[A.Badge]) -> list[A.Badge]:
        """Insert once each; returns the ones that were new."""
        t = now()
        fresh = []
        async with self.db.transaction() as tx:
            for b in badges:
                cur = await tx.execute("INSERT OR IGNORE INTO achievements (user_id, key, at) VALUES (?, ?, ?)",
                                       (user_id, b.key, t))
                if cur.rowcount:
                    fresh.append(b)
        return fresh

    async def backfilled(self) -> bool:
        return await self.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (BACKFILL_KEY,)) is not None

    def target_channel(self, guild, channel=None):
        """Where it happened, unless that's a quiet or staff channel; else general."""
        if channel is not None and hasattr(channel, "send"):
            parent = getattr(channel, "parent", None) or channel
            category = getattr(parent, "category", None)
            staff = category is not None and config.match_by_name([category], config.STAFF_CATEGORY) is not None
            quiet = any(config.match_by_name([parent], name) is not None for name in QUIET_CHANNELS)
            if not staff and not quiet and getattr(parent, "name", None) is not None:
                return channel
        return config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL)

    async def announce(self, guild, user_id: int, badges: list[A.Badge], channel=None) -> bool:
        target = self.target_channel(guild, channel)
        if target is None:
            log.info("achievements: no %s channel to congratulate in", config.GENERAL_CHANNEL)
            return False
        try:
            await target.send(A.congrats(f"<@{user_id}>", badges),
                              allowed_mentions=ping_only(users=[discord.Object(user_id)]))
            return True
        except Exception:
            log.exception("achievements: congratulating %s failed", user_id)
            return False

    async def evaluate(self, user_ids, channel=None, announce: bool = True, extra: dict | None = None) -> int:
        """Grant whatever these members have earned. `extra`: uid -> badge keys earned by an
        event the tables don't record (none today, kept for awards like MVP). Returns posts made."""
        guild = self.guild()
        if guild is None:
            return 0
        ids = None if user_ids is None else {u for u in user_ids}
        facts = await self.gather(ids)
        announce = announce and await self.backfilled()
        held_by: dict[int, set[str]] | None = None
        if ids is None:  # the sweep: one read for everyone instead of one per member
            held_by = {}
            for r in await self.db.fetchall("SELECT user_id, key FROM achievements"):
                held_by.setdefault(r["user_id"], set()).add(r["key"])
        posts = 0
        for uid in sorted(facts):
            member = guild.get_member(uid)
            if member is None or member.bot:
                continue  # left (or never in) the server: nothing to grant
            held = held_by.get(uid, set()) if held_by is not None else await self.held(uid)
            new = A.new_badges(facts[uid], held)
            for key in (extra or {}).get(uid, ()):
                if key in A.BY_KEY and key not in held and A.BY_KEY[key] not in new:
                    new.append(A.BY_KEY[key])
            if not new:
                continue
            fresh = await self.grant(uid, new)
            loud = [b for b in fresh if b.key not in A.QUIET]
            if loud and announce and (user_ids is not None or posts < SWEEP_POST_LIMIT):
                posts += await self.announce(guild, uid, loud, channel)
            if fresh:
                log.info("achievements: %s earned %s", uid, ", ".join(b.key for b in fresh))
        return posts

    # ------------------------------------------------------------ triggers
    def soon(self, user_id: int, channel=None) -> None:
        """Re-check one member shortly (other cogs record the same event first)."""
        if user_id in self.pending:
            return

        async def later():
            try:
                await asyncio.sleep(self.delay)
                await self.evaluate({user_id}, channel)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("achievements: checking %s failed", user_id)
            finally:
                self.pending.pop(user_id, None)

        self.pending[user_id] = asyncio.create_task(later())

    async def drain(self) -> None:
        """Wait for scheduled checks (tests, shutdown)."""
        while self.pending:
            await asyncio.gather(*list(self.pending.values()), return_exceptions=True)

    @commands.Cog.listener()
    @never_raise
    async def on_message(self, message) -> None:
        if message.author.bot or not self.ours(message.guild):
            return
        uid, t = message.author.id, now()
        if t - self.last_message_check.get(uid, 0) < MESSAGE_COOLDOWN:
            return
        self.last_message_check[uid] = t
        self.soon(uid, message.channel)

    @commands.Cog.listener()
    @never_raise
    async def on_interaction(self, interaction) -> None:
        # Fires as the interaction arrives; the delay lets the command or button finish.
        user = interaction.user
        if user is None or getattr(user, "bot", False) or not self.ours(interaction.guild):
            return
        t = now()
        if t - self.last_interaction_check.get(user.id, 0) < INTERACTION_COOLDOWN:
            return  # the hourly sweep catches anything earned in between
        self.last_interaction_check[user.id] = t
        self.soon(user.id, interaction.channel)

    @commands.Cog.listener()
    @never_raise
    async def on_voice_state_update(self, member, before, after) -> None:
        if member.bot or not self.ours(member.guild) or before.channel == after.channel or before.channel is None:
            return
        self.soon(member.id)

    @commands.Cog.listener()
    @never_raise
    async def on_weekly_mvp(self, period_key, winner_id) -> None:
        # The economy cog pays the MVP (the ledger row this badge reads) on the same
        # event; grant straight away too, in case that payout is switched off.
        await self.evaluate({winner_id}, extra={winner_id: ["weekly_mvp"]})

    @commands.Cog.listener()
    @never_raise
    async def on_clip_of_the_week(self, week, winner_id) -> None:
        await self.evaluate({winner_id}, extra={winner_id: ["clip_week"]})

    @commands.Cog.listener()
    @never_raise
    async def on_tournament_won(self, *args) -> None:
        # Read from the tournaments table, so the payload shape doesn't matter: re-check
        # every member the event names (ids or member objects).
        guild = self.guild()
        for arg in args:
            uid = getattr(arg, "id", arg)
            if isinstance(uid, int) and guild is not None and guild.get_member(uid) is not None:
                self.soon(uid)

    # ------------------------------------------------------------ hourly sweep
    @tasks.loop(hours=1)
    async def sweep(self) -> None:
        try:
            await self.run_sweep()
        except Exception:
            log.exception("achievements sweep failed")

    @sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def run_sweep(self) -> int:
        if self.guild() is None:
            return 0
        first = not await self.backfilled()
        posts = await self.evaluate(None, announce=not first)
        if first:
            await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (BACKFILL_KEY, str(now())))
            log.info("achievements: first sweep done, existing badges granted quietly")
        return posts

    # ------------------------------------------------------------ /profile
    async def season_points(self, user_id: int) -> int:
        start, end = shop.month_bounds(shop.month_key(now(), self.tz), self.tz)
        reasons = sorted(A.SEASON_REASONS)
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(delta), 0) AS n FROM ledger WHERE user_id = ? AND delta > 0 AND at >= ? AND at < ?"
            f" AND reason IN ({','.join('?' * len(reasons))})", (user_id, start, end, *reasons))
        return row["n"]

    async def top_game(self, user_id: int) -> str | None:
        rows = await self.db.fetchall('SELECT user_id, game, start, "end" FROM game_sessions WHERE user_id = ?',
                                      (user_id,))
        t = now()
        per_game = S.game_seconds([S.Session(r["user_id"], r["game"], r["start"], r["end"]) for r in rows], 0, t, t)
        top = S.top_games(per_game.get(user_id, {}), n=1)
        return f"{discord.utils.escape_markdown(top[0][0])} ({S.fmt_duration(top[0][1])})" if top else None

    async def profile_embed(self, member) -> discord.Embed:
        uid = member.id
        held = await self.held(uid)
        name = discord.utils.escape_markdown(getattr(member, "display_name", None) or "member")
        embed = style.embed(title=name, footer=style.label("profile", f"{A.progress(held)} badges"))
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Coins", value=f"{await economy.balance(self.db, uid):,}")
        embed.add_field(name="Season points", value=f"{await self.season_points(uid):,} this month")
        if await self.db.tracking_allowed(uid):
            voice = (await self.voice_seconds({uid})).get(uid, 0)
            row = await self.db.fetchone("SELECT COALESCE(SUM(count), 0) AS n FROM message_counts WHERE user_id = ?",
                                         (uid,))
            embed.add_field(name="Voice", value=f"{voice / 3600:,.1f} h")
            embed.add_field(name="Messages", value=f"{row['n']:,}")
            embed.add_field(name="Top game", value=await self.top_game(uid) or "none yet")
        else:
            embed.add_field(name="Stats", value="Turned off with `/privacy`.", inline=False)
        embed.add_field(name=f"Badges · {A.progress(held)}", value=A.grid(held), inline=False)
        return embed

    @app_commands.command(name="profile", description="Coins, stats and badges for you or someone else")
    @app_commands.describe(member="Whose profile (default: you)")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        if member.bot:
            await interaction.response.send_message("Bots don't collect badges.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(embed=await self.profile_embed(member),
                                        allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="badges", description="Every badge and how to earn it")
    @app_commands.guild_only()
    async def badges(self, interaction: discord.Interaction) -> None:
        held = await self.held(interaction.user.id)
        lines = [f"{'✅' if b.key in held else '▫️'} {b.emoji} **{b.name}**: {b.description}" for b in A.BADGES]
        embed = style.embed(title=f"Badges · {A.progress(held)}", description="\n".join(lines),
                            footer=style.label("badges", "stat badges need /privacy tracking on"))
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Achievements(bot))
