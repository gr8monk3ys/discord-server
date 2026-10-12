"""Vote hub: /vote shows (privately) where members can support the server: Disboard with
the member's own bump count and when the next bump is ready, discord.me, top.gg ("coming
soon" until logic.votehub.TOPGG_URL is set) and the short share link.

Weekly Supporter thanks: Sundays 17:00 local the top three Disboard bumpers of the past
week are thanked in the games channel and paid 300/200/100 coins (ledger refs
support:WEEK:PLACE, so a retry or restart never pays twice). Nobody bumped = no post.
Accounts younger than MIN_ACCOUNT_DAYS are thanked without coins.

Nothing is stored here: bumps are read from the growth cog's `bumps` table and its pending
reminder time (meta key cogs.growth.BUMP_DUE_KEY). No privileged intents."""

import functools
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from cogs.growth import BUMP_DUE_KEY, WINDOW
from errors import reply_error
from logic import growth as G
from logic import votehub as V
from logic.schedule import latest_due, plan

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
SEEN_KEY = f"first_seen:{V.SUPPORT_JOB.name}"


def now() -> int:
    return int(time.time())


def never_raise(fn):
    """Loops log and carry on."""
    @functools.wraps(fn)
    async def wrapper(self, *args):
        try:
            return await fn(self, *args)
        except Exception:
            log.exception("votehub: %s failed", fn.__name__)
    return wrapper


def links_view() -> discord.ui.View:
    """Link buttons under /vote (they open the browser; nothing to handle)."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="discord.me", url=V.DISCORD_ME_URL))
    topgg = V.link_url(V.TOPGG_URL)
    if topgg:
        view.add_item(discord.ui.Button(label="top.gg", url=topgg))
    view.add_item(discord.ui.Button(label="Share link", url=V.SHARE_URL))
    return view


class VoteHub(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.warned: set[str] = set()  # periods already logged as having nowhere to post

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

    # ------------------------------------------------------------ /vote
    async def ready_at(self) -> int | None:
        """When Disboard takes the next bump, from the growth cog's data."""
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (BUMP_DUE_KEY,))
        try:
            due = int(row["value"]) if row else None
        except (TypeError, ValueError):
            due = None
        last = await self.db.fetchone("SELECT MAX(at) AS at FROM bumps")
        return V.next_bump_at(due, last["at"] if last else None, G.REMINDER_DELAY)

    async def my_bumps(self, user_id: int, t: int) -> tuple[int, int]:
        """(bumps since the last Supporter thanks, bumps in the past 30 days)."""
        week_start = latest_due(V.SUPPORT_JOB, t, self.tz).scheduled_at
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(at >= ?), 0) AS week, COUNT(*) AS month FROM bumps WHERE user_id = ? AND at >= ?",
            (week_start, user_id, t - WINDOW))
        return int(row["week"]), int(row["month"])

    async def hub_embed(self, user_id: int, guild) -> discord.Embed:
        t = now()
        week, month = await self.my_bumps(user_id, t)
        channel = config.match_by_name(guild.text_channels, config.BOT_COMMANDS_CHANNEL) if guild else None
        embed = style.embed(title="Support the server",
                            description="A minute of your time puts the server in front of more players.\n"
                                        + V.rewards_text(),
                            footer=style.label("vote", "thanks for helping"))
        embed.add_field(name="Disboard", inline=False,
                        value=V.disboard_text(channel.mention if channel else None, await self.ready_at(), t,
                                              week, month))
        embed.add_field(name="discord.me", value=V.discord_me_text(), inline=False)
        embed.add_field(name="top.gg", value=V.topgg_text(), inline=False)
        embed.add_field(name="Share", value=V.share_text(), inline=False)
        return embed

    @app_commands.command(name="vote", description="Where you can support the server: bumps, votes and the share link")
    async def vote(self, interaction: discord.Interaction) -> None:
        try:
            embed = await self.hub_embed(interaction.user.id, interaction.guild)
            await interaction.response.send_message(embed=embed, view=links_view(), ephemeral=True,
                                                    allowed_mentions=NO_PINGS)
        except Exception:
            log.exception("votehub: /vote failed")
            await reply_error(interaction)

    # ------------------------------------------------------------ weekly Supporter thanks
    @tasks.loop(minutes=5)
    async def tick(self) -> None:
        await self.run_support()

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    @never_raise
    async def run_support(self) -> bool:
        """Thank and pay last week's bumpers once (the first run only marks the week done).
        True when the week was handled now."""
        job = V.SUPPORT_JOB
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (SEEN_KEY,))
        first_seen = int(row["value"]) if row else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{job.name}:%",))}
        t = now()
        todo = plan(job, t, self.tz, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (SEEN_KEY, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        if todo.run is None:
            return False
        # Marked done only after it worked: no channel yet or an error retries on the next tick
        # (the refs keep a retry from paying twice).
        if await self.post_support(todo.run):
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (todo.run.key, now()))
            return True
        return False

    async def podium(self, period) -> list[V.Place]:
        rows = await self.db.fetchall(
            "SELECT user_id, COUNT(*) AS n, MAX(at) AS last FROM bumps WHERE at >= ? AND at < ? GROUP BY user_id",
            (period.window_start, period.window_end))
        return V.places([(r["user_id"], r["n"], r["last"]) for r in rows], period.scheduled_at)

    def warn_once(self, period, why: str) -> None:
        if period.key not in self.warned:
            self.warned.add(period.key)
            log.warning("supporter thanks %s: %s", period.key, why)

    async def post_support(self, period) -> bool:
        """True when the week is finished (posted, or nobody bumped)."""
        podium = await self.podium(period)
        if not podium:
            log.info("supporter thanks %s: nobody bumped, nothing to post", period.key)
            return True
        guild = self.guild()
        channel = config.match_by_name(guild.text_channels, config.GAMES_CHANNEL) if guild else None
        if channel is None:
            self.warn_once(period, f"no {config.GAMES_CHANNEL} channel yet")
            return False
        t = now()
        async with self.db.transaction() as tx:
            for p in podium:
                if p.coins:
                    await economy.apply_tx(tx, p.user_id, p.coins, V.REASON, t, V.ref(period.key, p.rank))
        embed = style.embed(title=f"Supporters · {V.week_of(period.key)}", description=V.thanks_text(podium),
                            footer=style.label("supporters", "top Disboard bumpers of the week"))
        try:
            await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        except discord.Forbidden:
            self.warn_once(period, f"can't post in {config.GAMES_CHANNEL}")
            return False
        log.info("supporter thanks %s: posted %d place(s)", period.key, len(podium))
        return True


async def setup(bot) -> None:
    await bot.add_cog(VoteHub(bot))
