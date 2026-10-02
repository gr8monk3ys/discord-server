"""Module 2: Stats and leaderboards. Records voice time, message counts and game
time (counts and durations only, never message text), answers /stats and
/leaderboard, posts the weekly MVP, and handles /privacy."""

import logging
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from logic import stats as S
from logic.schedule import Weekly, plan

log = logging.getLogger(__name__)

DAY = 24 * 60 * 60
PERIODS = {"week": 7 * DAY, "month": 30 * DAY, "all": None}
MVP_JOB = Weekly("mvp", weekday=6, hour=18, minute=0)  # Sundays 18:00 local
BOARD_TITLES = {"voice": "Voice time", "messages": "Messages", "gaming": "Game time"}


def now() -> int:
    return int(time.time())


def playing(member: discord.Member) -> str | None:
    """The game a member is playing right now, from their presence."""
    for activity in member.activities:
        if activity.type is discord.ActivityType.playing and activity.name:
            return activity.name[:100]
    return None


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        # Runs in setup_hook, before the gateway connects: no voice/presence event can
        # close a crash-left session at "now" (which would count the downtime).
        await self.close_stale_sessions()
        self.heartbeat.start()
        self.weekly.start()

    async def cog_unload(self) -> None:
        self.heartbeat.cancel()
        self.weekly.cancel()

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    @property
    def gaming_enabled(self) -> bool:
        return self.bot.intents.presences

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def afk_id(self) -> int | None:
        g = self.guild()
        return g.afk_channel.id if g and g.afk_channel else None

    @staticmethod
    def in_staff(channel) -> bool:
        """Staff channels (and threads in them) are never tracked."""
        parent = getattr(channel, "parent", None) or channel
        category = getattr(parent, "category", None)
        return category is not None and config.match_by_name([category], config.STAFF_CATEGORY) is not None

    def tracked_voice(self, channel) -> bool:
        return channel is not None and channel.id != self.afk_id() and not self.in_staff(channel)

    async def optouts(self) -> set[int]:
        return {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}

    def local_day(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, self.tz).date().isoformat()

    # ------------------------------------------------------------ startup recovery
    async def close_stale_sessions(self) -> None:
        """Close sessions left open by a crash or shutdown at the last heartbeat."""
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = 'heartbeat'")
        last = int(row["value"]) if row else now()
        async with self.db.transaction() as tx:
            await tx.execute('UPDATE voice_sessions SET "end" = MAX(start, ?) WHERE "end" IS NULL', (last,))
            await tx.execute('UPDATE game_sessions SET "end" = MAX(start, ?) WHERE "end" IS NULL', (last,))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Fires on first connect and again after any reconnect that rebuilt the cache.
        try:
            await self.reconcile()
        except Exception:
            log.exception("stats reconcile failed")

    def current_voice(self, guild: discord.Guild) -> dict[int, int]:
        """user_id -> channel_id for every human in a tracked voice channel right now."""
        present = {}
        for channel in guild.voice_channels:
            if not self.tracked_voice(channel):
                continue
            for user_id in channel.voice_states:
                member = guild.get_member(user_id)
                if member is None or not member.bot:
                    present[user_id] = channel.id
        return present

    def current_games(self, guild: discord.Guild) -> dict[int, str]:
        if not self.gaming_enabled:
            return {}
        return {m.id: g for m in guild.members if not m.bot and (g := playing(m))}

    async def reconcile(self) -> None:
        """Make open sessions match reality: close rows for people who left (or
        switched) while the bot wasn't watching, open rows for people already there."""
        guild = self.guild()
        if guild is None:
            return
        t = now()
        skip = await self.optouts()
        voice = {u: c for u, c in self.current_voice(guild).items() if u not in skip}
        games = {u: g for u, g in self.current_games(guild).items() if u not in skip}
        async with self.db.transaction() as tx:
            for table, col, current in (("voice_sessions", "channel_id", voice), ("game_sessions", "game", games)):
                rows = await tx.fetchall(f'SELECT id, user_id, {col} AS k FROM {table} WHERE "end" IS NULL')
                still_open = set()
                for r in rows:
                    if current.get(r["user_id"]) == r["k"] and r["user_id"] not in still_open:
                        still_open.add(r["user_id"])
                    else:
                        await tx.execute(f'UPDATE {table} SET "end" = ? WHERE id = ?', (t, r["id"]))
                for user_id, key in current.items():
                    if user_id not in still_open:
                        await tx.execute(f"INSERT INTO {table} (user_id, {col}, start) VALUES (?, ?, ?)",
                                         (user_id, key, t))
        log.info("stats synced: %d in voice, gaming %s", len(voice),
                 f"on ({len(games)} playing)" if self.gaming_enabled else "off: Presence intent not enabled")

    @tasks.loop(minutes=1)
    async def heartbeat(self) -> None:
        try:
            await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('heartbeat', ?)", (str(now()),))
        except Exception:
            log.exception("heartbeat failed")

    @heartbeat.before_loop
    async def before_heartbeat(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ recording

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        if member.bot or member.guild.id != self.bot.settings.guild_id:
            return
        if before.channel == after.channel:
            return  # mute/deafen/stream changes
        if not await self.db.tracking_allowed(member.id):
            return
        t = now()
        async with self.db.transaction() as tx:
            await tx.execute('UPDATE voice_sessions SET "end" = ? WHERE user_id = ? AND "end" IS NULL', (t, member.id))
            if self.tracked_voice(after.channel):
                await tx.execute("INSERT INTO voice_sessions (user_id, channel_id, start) VALUES (?, ?, ?)",
                                 (member.id, after.channel.id, t))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or message.guild.id != self.bot.settings.guild_id:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return  # join notices, boosts, pins
        if self.in_staff(message.channel):
            return
        if not await self.db.tracking_allowed(message.author.id):
            return
        await self.db.execute(
            "INSERT INTO message_counts (user_id, day, count) VALUES (?, ?, 1)"
            " ON CONFLICT (user_id, day) DO UPDATE SET count = count + 1",
            (message.author.id, self.local_day(now())),
        )

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        if after.bot or after.guild.id != self.bot.settings.guild_id:
            return
        old, new = playing(before), playing(after)
        if old == new:
            return
        if not await self.db.tracking_allowed(after.id):
            return
        t = now()
        async with self.db.transaction() as tx:
            await tx.execute('UPDATE game_sessions SET "end" = ? WHERE user_id = ? AND "end" IS NULL', (t, after.id))
            # Short sessions (alt-tabbing, launchers) are noise: drop them when they close.
            await tx.execute('DELETE FROM game_sessions WHERE user_id = ? AND "end" = ? AND "end" - start < ?',
                             (after.id, t, S.MIN_GAME_SECONDS))
            if new:
                await tx.execute("INSERT INTO game_sessions (user_id, game, start) VALUES (?, ?, ?)",
                                 (after.id, new, t))

    # ------------------------------------------------------------ queries
    async def sessions(self, table: str, key: str, start: int, end: int) -> list[S.Session]:
        rows = await self.db.fetchall(
            f'SELECT user_id, {key} AS k, start, "end" FROM {table} WHERE start < ? AND ("end" IS NULL OR "end" > ?)',
            (end, start),
        )
        return [S.Session(r["user_id"], r["k"], r["start"], r["end"]) for r in rows]

    async def voice_scores(self, start: int, end: int) -> dict[int, int]:
        excluded = {self.afk_id()} - {None}
        return S.counted_voice_seconds(await self.sessions("voice_sessions", "channel_id", start, end),
                                       start, end, now(), excluded_channels=excluded)

    async def game_scores(self, start: int, end: int) -> dict[int, dict[str, int]]:
        return S.game_seconds(await self.sessions("game_sessions", "game", start, end), start, end, now())

    async def message_scores(self, start: int, end: int) -> dict[int, int]:
        rows = await self.db.fetchall(
            # (start day, end day]: a 7-day window covers exactly 7 local days, and
            # back-to-back weekly windows never count the same day twice.
            "SELECT user_id, SUM(count) AS n FROM message_counts WHERE day > ? AND day <= ? GROUP BY user_id",
            (self.local_day(start), self.local_day(end)),
        )
        return {r["user_id"]: r["n"] for r in rows}

    async def boards(self, start: int, end: int) -> dict[str, dict[int, int]]:
        boards = {
            "voice": await self.voice_scores(start, end),
            "messages": await self.message_scores(start, end),
        }
        if self.gaming_enabled:
            boards["gaming"] = S.gaming_totals(await self.game_scores(start, end))
        return boards

    def window(self, period: str) -> tuple[int, int]:
        t = now()
        span = PERIODS[period]
        return (0 if span is None else t - span), t

    @staticmethod
    def show(board: str, score: int) -> str:
        return f"{score:,}" if board == "messages" else S.fmt_duration(score)

    # ------------------------------------------------------------ /stats
    @app_commands.command(name="stats", description="Voice time, messages and top games for you or someone else")
    @app_commands.describe(member="Whose stats (default: you)")
    async def stats(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        if not await self.db.tracking_allowed(member.id):
            await interaction.response.send_message(f"{member.display_name} has stats turned off.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        lines = []
        for label_text, period in (("Past 7 days", "week"), ("All time", "all")):
            start, end = self.window(period)
            voice = (await self.voice_scores(start, end)).get(member.id, 0)
            msgs = (await self.message_scores(start, end)).get(member.id, 0)
            lines.append(f"**{label_text}**")
            lines.append(f"`VOICE`  {S.fmt_duration(voice)}   `MESSAGES`  {msgs:,}")
            if self.gaming_enabled:
                games = (await self.game_scores(start, end)).get(member.id, {})
                top = S.top_games(games)
                lines.append("`TOP GAMES`  " + (", ".join(f"{g} ({S.fmt_duration(s)})" for g, s in top) or "none yet"))
            lines.append("")
        embed = style.embed(title=member.display_name, description="\n".join(lines).strip(),
                            footer=style.label("stats", "counted voice = with 2+ people, not AFK"))
        embed.set_thumbnail(url=member.display_avatar.url)
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------ /leaderboard
    @app_commands.command(name="leaderboard", description="Who's been most active")
    @app_commands.describe(board="What to rank", period="Time window")
    @app_commands.choices(
        board=[app_commands.Choice(name=v, value=k) for k, v in BOARD_TITLES.items()],
        period=[app_commands.Choice(name=n, value=v) for n, v in
                (("Past 7 days", "week"), ("Past 30 days", "month"), ("All time", "all"))],
    )
    async def leaderboard(self, interaction: discord.Interaction, board: app_commands.Choice[str],
                          period: app_commands.Choice[str] | None = None) -> None:
        period_value = period.value if period else "week"
        if board.value == "gaming" and not self.gaming_enabled:
            await interaction.response.send_message(
                "Game time needs the Presence intent, which isn't switched on for Front Desk yet.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        boards = await self.boards(*self.window(period_value))
        top, mine = S.rank(boards.get(board.value, {}), limit=10, me=interaction.user.id)
        if not top:
            text = "Nobody's on the board yet."
        else:
            rows = [f"`{r.rank:02}`  <@{r.user_id}>  {self.show(board.value, r.score)}" for r in top]
            if mine:
                rows += ["…", f"`{mine.rank:02}`  <@{mine.user_id}>  {self.show(board.value, mine.score)}"]
            text = "\n".join(rows)
        period_name = period.name if period else "Past 7 days"
        embed = style.embed(title=f"{BOARD_TITLES[board.value]} · {period_name}", description=text,
                            footer=style.label("leaderboard", board.value, period_value))
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------ /privacy
    @app_commands.command(name="privacy", description="Turn stat tracking off (deletes your stats) or back on")
    @app_commands.choices(tracking=[app_commands.Choice(name="off: stop tracking and delete my stats", value="off"),
                                    app_commands.Choice(name="on: track my stats again", value="on")])
    async def privacy(self, interaction: discord.Interaction, tracking: app_commands.Choice[str]) -> None:
        uid = interaction.user.id
        if tracking.value == "off":
            async with self.db.transaction() as tx:
                await tx.execute("INSERT OR IGNORE INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, now()))
                for table in ("voice_sessions", "game_sessions", "message_counts"):
                    await tx.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
            text = ("Done. Front Desk no longer tracks your voice time, messages or games, and your past stats "
                    "are deleted. Run `/privacy tracking:on` any time to start again.")
        else:
            await self.db.execute("DELETE FROM privacy_optout WHERE user_id = ?", (uid,))
            await self.reconcile()  # already in voice or playing: start counting now
            text = "Tracking is back on, starting now."
        await interaction.response.send_message(text, ephemeral=True)

    # ------------------------------------------------------------ weekly MVP
    @tasks.loop(minutes=5)
    async def weekly(self) -> None:
        try:
            await self.run_weekly()
        except Exception:
            log.exception("weekly MVP job failed")

    @weekly.before_loop
    async def before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    async def run_weekly(self) -> None:
        job = MVP_JOB
        seen_key = f"first_seen:{job.name}"
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (seen_key,))
        first_seen = int(row["value"]) if row else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{job.name}:%",))}
        t = now()
        todo = plan(job, t, self.tz, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        if todo.run is not None:
            # Marked done only after posting: a Discord error retries on the next tick.
            # Payouts hooked to weekly_mvp use ledger refs, so a retry can't pay twice.
            await self.post_mvp(todo.run)
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (todo.run.key, now()))

    async def post_mvp(self, period) -> None:
        boards = await self.boards(period.window_start, period.window_end)
        winner = S.mvp(boards)
        guild = self.guild()
        channel = config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL) if guild else None
        if winner is None or channel is None:
            log.info("weekly MVP %s: %s", period.key, "quiet week, nothing posted" if winner is None
                     else f"no {config.GENERAL_CHANNEL} channel")
            return
        lines = [f"**MVP**  <@{winner}>", ""]
        for name, scores in boards.items():
            top, _ = S.rank(scores, limit=1)
            if top:
                lines.append(f"`{name.upper():<8}`  <@{top[0].user_id}>  {self.show(name, top[0].score)}")
        embed = style.embed(title="This week on the server", description="\n".join(lines),
                            footer=style.label("weekly", period.key.split(":")[1]))
        await channel.send(content=f"MVP this week: <@{winner}>", embed=embed,
                           allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                                    users=[discord.Object(winner)]))
        self.bot.dispatch("weekly_mvp", period.key, winner)
        log.info("posted weekly MVP %s: %s", period.key, winner)


async def setup(bot) -> None:
    await bot.add_cog(Stats(bot))
