"""Wrapped: a year-in-review card per member each December, and one for the whole server.

- `/wrapped [year]`: the member's card for the year that is open (December 1 to January 15,
  local time), as one tall PNG only they see, with a "Share to channel" button (a
  restart-safe DynamicItem) that posts it publicly with AllowedMentions.none(). A member
  who turned tracking off with /privacy gets an explanation instead of a card.
- `/wrapped-preview [year] [member] [server]` (staff): any card at any time, privately.
- Two posts a year in the announcements channel, on logic/schedule.py's rules (local time,
  caught up once after downtime, never twice): December 1 at 10:00 "Wrapped is open" with
  the server-wide card, and a reminder on December 20. Nothing else is posted.
- Sharing your card earns the "Wrapped <year>" badge (shown on /profile, outside the
  catalogue's N/TOTAL count). No coins are involved anywhere.

Everything on a card is read from the tables the other modules keep, at the moment it is
asked for: nothing about a member's year is stored. What this cog does store: when a member
last shared a card (meta wrapped:shared:YEAR:USER, for the share cooldown), the badge row,
and a level snapshot taken in January (meta wrapped:levels:YEAR, levels only, opted-out
members removed) so next December's card can say how many levels were gained.

Cards are drawn in a thread (Pillow is CPU-bound) and kept in memory keyed by a digest of
what they show, so a card is drawn once until the numbers, the name or the avatar change.
Needs the Server Members intent to name teammates; without it cards just have no
teammates section. The bot needs Send Messages and Attach Files where cards are posted."""

import asyncio
import io
import json
import logging
import time
from collections import OrderedDict

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from errors import reply_error
from logic import community as community_rules
from logic import houses as H
from logic import matchmaking as M
from logic import starboard
from logic import stats as S
from logic import wrapped as W
from logic import wrappedcard as WC

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
FILENAME = "wrapped.png"
SERVER_FILENAME = "wrapped-server.png"
AVATAR_TIMEOUT = 10  # seconds to wait for an avatar before drawing the initial instead
ANNOUNCED_KEY = "wrapped:announced:"  # meta: + year -> when the December 1 post really went out
# A shared card never lands in these (counting would break, the others are read-only or curated).
QUIET_CHANNELS = (config.COUNTING_CHANNEL, config.RULES_CHANNEL, config.ANNOUNCEMENTS_CHANNEL,
                  config.HALL_OF_FAME_CHANNEL, config.CREATORS_CHANNEL, config.WELCOME_CHANNEL,
                  config.TOURNAMENTS_CHANNEL, config.ROLES_CHANNEL, config.PARTNERS_CHANNEL)


def now() -> int:
    return int(time.time())


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    perms = getattr(user, "guild_permissions", None)
    return community_rules.can_handle(guild.owner_id == user.id, bool(getattr(perms, "administrator", False)),
                                      getattr(user, "roles", []))


def esc(text) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text or "")))


class ShareButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"wrapped:share:(?P<year>\d{4}):(?P<user>\d+)"):
    """Under a member's private card: posts that card in the channel. Only its owner can."""

    def __init__(self, year: int, user_id: int):
        super().__init__(discord.ui.Button(label="Share to channel", emoji="📣", style=discord.ButtonStyle.primary,
                                           custom_id=f"wrapped:share:{year}:{user_id}"))
        self.year = year
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["year"]), int(match["user"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.client.get_cog("Wrapped").handle_share(interaction, self.year, self.user_id)
        except Exception:
            log.exception("wrapped: share button for %s failed", self.user_id)
            await reply_error(interaction)


def share_view(year: int, user_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ShareButton(year, user_id))
    return view


class Wrapped(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cache: OrderedDict[tuple, tuple[str, bytes]] = OrderedDict()  # (kind, id, year) -> (digest, png)
        self.last_run: dict[int, int] = {}  # user -> last /wrapped
        self.warned: set[str] = set()  # job periods already logged as having no channel
        self.renders = 0  # cards actually drawn (tests read it)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ShareButton)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()
        self.bot.remove_dynamic_items(ShareButton)

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    async def meta(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    async def optouts(self) -> set[int]:
        return {r["user_id"] for r in await self.db.fetchall("SELECT user_id FROM privacy_optout")}

    # ------------------------------------------------------------ reading a year
    @staticmethod
    def excluded_channels(guild) -> set[int]:
        afk = getattr(guild, "afk_channel", None) if guild else None
        return {afk.id} if afk is not None else set()

    async def allowed(self, guild) -> dict[int, str]:
        """Members who may be named on a card (here, human, tracking on) -> display name."""
        skip = await self.optouts()
        return {m.id: m.display_name for m in getattr(guild, "members", []) if not m.bot and m.id not in skip}

    async def times(self, sql: str, params) -> list[int]:
        return [r["t"] for r in await self.db.fetchall(sql, params) if r["t"] is not None]

    async def squads(self, start: int, end: int, user_id: int | None = None) -> list[W.Squad]:
        """/lfg posts made in the window (only this member's, if given) with their rosters."""
        mine, params = "", [start, end]
        if user_id is not None:
            mine = (" AND (p.host_id = ? OR EXISTS (SELECT 1 FROM lfg_members m"
                    " WHERE m.post_id = p.id AND m.user_id = ?))")
            params += [user_id, user_id]
        rows = await self.db.fetchall(
            "SELECT p.id, p.host_id, p.created_at, m.user_id FROM lfg_posts p"
            " LEFT JOIN lfg_members m ON m.post_id = p.id"
            f" WHERE p.created_at >= ? AND p.created_at < ?{mine} ORDER BY p.id, m.user_id", params)
        posts: dict[int, list] = {}
        for r in rows:
            post = posts.setdefault(r["id"], [r["created_at"], r["host_id"], []])
            if r["user_id"] is not None:
                post[2].append(r["user_id"])
        return [W.Squad(created, tuple(members), host) for created, host, members in posts.values()]

    async def matches(self, start: int, end: int) -> list[W.Squad]:
        rows = await self.db.fetchall("SELECT members, created_at FROM mm_matches WHERE created_at >= ?"
                                      " AND created_at < ? ORDER BY id", (start, end))
        out = []
        for r in rows:
            try:
                out.append(W.Squad(r["created_at"], tuple(M.decode_members(r["members"]))))
            except ValueError:
                continue
        return out

    async def level_snapshot(self, year: int) -> dict[int, int]:
        raw = await self.meta(W.levels_key(year))
        try:
            return {int(u): int(level) for u, level in json.loads(raw).items()} if raw else {}
        except (ValueError, AttributeError, TypeError):
            return {}

    async def house_of(self, user_id: int) -> str | None:
        """The display name of the house the member is in now (logic/houses.py: a row with
        left_at set is a former member), or None. None too when the houses module is off or
        its table can't be read: a card never fails over this."""
        if self.bot.get_cog("Houses") is None:
            return None
        try:
            key = await H.house_of(self.db, user_id)
        except Exception:
            log.debug("wrapped: no readable house for %s", user_id, exc_info=True)
            return None
        return H.BY_KEY[key].name if key is not None else None

    async def member_rows(self, member, year: int) -> W.MemberRows:
        uid = member.id
        start, end = W.year_bounds(year, self.tz)
        span = (uid, start, end)
        first_day, last_day = W.day_range(year)
        voice = await self.db.fetchall(
            'SELECT user_id, channel_id, start, "end" FROM voice_sessions WHERE channel_id IN'
            " (SELECT DISTINCT channel_id FROM voice_sessions WHERE user_id = ?)"
            ' AND start < ? AND ("end" IS NULL OR "end" > ?)', (uid, end, start))
        games = await self.db.fetchall(
            'SELECT user_id, game, start, "end" FROM game_sessions WHERE user_id = ?'
            ' AND start < ? AND ("end" IS NULL OR "end" > ?)', (uid, end, start))
        days = await self.db.fetchall("SELECT day, count FROM message_counts WHERE user_id = ? AND day >= ?"
                                      " AND day <= ?", (uid, first_day, last_day))
        ledger = await self.db.fetchall("SELECT delta, reason, ref, at FROM ledger WHERE user_id = ? AND at >= ?"
                                        " AND at < ?", span)
        badges = await self.db.fetchall("SELECT key, at FROM achievements WHERE user_id = ?", (uid,))
        holders = await self.db.fetchall("SELECT key, COUNT(*) AS n FROM achievements GROUP BY key")
        xp = await self.db.fetchone("SELECT level FROM xp WHERE user_id = ?", (uid,))
        joined = await self.db.fetchone("SELECT MAX(joined_at) AS t FROM joins WHERE user_id = ?", (uid,))
        joined_at = joined["t"] if joined and joined["t"] is not None else None
        if joined_at is None and getattr(member, "joined_at", None) is not None:
            joined_at = int(member.joined_at.timestamp())
        in_year = "AND {0} >= ? AND {0} < ?"
        return W.MemberRows(
            voice=[S.Session(r["user_id"], r["channel_id"], r["start"], r["end"]) for r in voice],
            games=[S.Session(r["user_id"], r["game"], r["start"], r["end"]) for r in games],
            message_days={r["day"]: r["count"] for r in days},
            ledger=[W.Coin(r["delta"], r["reason"], r["ref"], r["at"]) for r in ledger],
            badges=[(r["key"], r["at"]) for r in badges],
            badge_holders={r["key"]: r["n"] for r in holders},
            squads=await self.squads(start, end, uid),
            matches=[m for m in await self.matches(start, end) if uid in m.members],
            gamenights=await self.times(
                f"SELECT starts_at AS t FROM gamenights WHERE host_id = ? {in_year.format('starts_at')}", span),
            tournaments_entered=await self.times(
                f"SELECT joined_at AS t FROM tournament_entries WHERE user_id = ? {in_year.format('joined_at')}", span),
            tournaments_won=await self.times(
                f"SELECT created_at AS t FROM tournaments WHERE winner_id = ? {in_year.format('created_at')}", span),
            clips=await self.times(
                f"SELECT posted_at AS t FROM clips WHERE user_id = ? {in_year.format('posted_at')}", span),
            clip_week_wins=await self.times(
                f"SELECT ends_at AS t FROM clip_polls WHERE winner_id = ? {in_year.format('ends_at')}", span),
            hall_of_fame=await self.times(
                "SELECT at AS t FROM starboard WHERE author_id = ? AND at >= ? AND at < ?"
                " AND board_message_id IS NOT NULL AND board_message_id != ?", (*span, starboard.PENDING)),
            challenges=await self.times(
                f"SELECT at AS t FROM challenge_claims WHERE user_id = ? {in_year.format('at')}", span),
            event_quests=await self.times(
                f"SELECT at AS t FROM event_claims WHERE user_id = ? {in_year.format('at')}", span),
            event_drops=await self.times(
                f"SELECT claimed_at AS t FROM event_drops WHERE claimed_by = ? {in_year.format('claimed_at')}", span),
            words_solved=await self.times(
                "SELECT finished_at AS t FROM word_games WHERE user_id = ? AND solved = 1"
                f" {in_year.format('finished_at')}", span),
            level=xp["level"] if xp else None,
            level_at_start=(await self.level_snapshot(year)).get(uid),
            level_at_end=(await self.level_snapshot(year + 1)).get(uid),
            joined_at=joined_at,
            house=await self.house_of(uid),
        )

    async def member_summary(self, guild, member, year: int) -> tuple[W.Summary, dict[int, str]]:
        """(the member's year, display names of everyone who may be named on it)."""
        rows = await self.member_rows(member, year)
        names = await self.allowed(guild)
        # Overlapping a year of voice sessions is CPU work: keep it off the event loop.
        summary = await asyncio.to_thread(W.summarize, member.id, year, self.tz, rows, now(), set(names),
                                          self.excluded_channels(guild))
        return summary, names

    async def server_rows(self, year: int) -> W.ServerRows:
        start, end = W.year_bounds(year, self.tz)
        span = (start, end)
        first_day, last_day = W.day_range(year)
        voice = await self.db.fetchall('SELECT user_id, channel_id, start, "end" FROM voice_sessions'
                                       ' WHERE start < ? AND ("end" IS NULL OR "end" > ?)', (end, start))
        games = await self.db.fetchall('SELECT user_id, game, start, "end" FROM game_sessions'
                                       ' WHERE start < ? AND ("end" IS NULL OR "end" > ?)', (end, start))
        days = await self.db.fetchall("SELECT user_id, day, count FROM message_counts WHERE day >= ? AND day <= ?",
                                      (first_day, last_day))
        ledger = await self.db.fetchall("SELECT user_id, delta, reason, ref, at FROM ledger WHERE at >= ?"
                                        " AND at < ? AND delta > 0", span)
        badges = await self.db.fetchall("SELECT user_id, key, at FROM achievements WHERE at >= ? AND at < ?", span)
        clips = await self.db.fetchall("SELECT user_id, posted_at FROM clips WHERE posted_at >= ? AND posted_at < ?",
                                       span)
        joins = await self.db.fetchall("SELECT user_id, joined_at FROM joins WHERE joined_at >= ? AND joined_at < ?",
                                       span)
        return W.ServerRows(
            voice=[S.Session(r["user_id"], r["channel_id"], r["start"], r["end"]) for r in voice],
            games=[S.Session(r["user_id"], r["game"], r["start"], r["end"]) for r in games],
            messages=[(r["user_id"], r["day"], r["count"]) for r in days],
            ledger=[W.Coin(r["delta"], r["reason"], r["ref"], r["at"], r["user_id"]) for r in ledger],
            badges=[(r["user_id"], r["key"], r["at"]) for r in badges],
            squads=await self.squads(start, end),
            matches=await self.matches(start, end),
            gamenights=await self.times("SELECT starts_at AS t FROM gamenights WHERE starts_at >= ?"
                                        " AND starts_at < ?", span),
            tournaments=await self.times("SELECT created_at AS t FROM tournaments WHERE created_at >= ?"
                                         " AND created_at < ?", span),
            clips=[(r["user_id"], r["posted_at"]) for r in clips],
            joins=[(r["user_id"], r["joined_at"]) for r in joins],
        )

    async def server_summary(self, guild, year: int) -> tuple[W.ServerSummary, dict[int, str]]:
        rows = await self.server_rows(year)
        names = await self.allowed(guild)
        summary = await asyncio.to_thread(W.summarize_server, year, self.tz, rows, now(), set(names),
                                          self.excluded_channels(guild))
        return summary, names

    # ------------------------------------------------------------ drawing, once
    @staticmethod
    async def image_bytes(asset) -> bytes | None:
        if asset is None:
            return None
        try:
            return await asyncio.wait_for(asset.replace(size=256, format="png").read(), AVATAR_TIMEOUT)
        except Exception:
            return None  # the card draws the initial instead

    async def cached(self, key: tuple, fingerprint: str, spec: WC.CardSpec, asset) -> bytes:
        """The PNG for `spec`, drawn only if this key's fingerprint changed since last time."""
        hit = self.cache.get(key)
        if hit is not None and hit[0] == fingerprint:
            self.cache.move_to_end(key)
            return hit[1]
        png = await asyncio.to_thread(WC.render, spec, await self.image_bytes(asset))
        self.renders += 1
        self.cache[key] = (fingerprint, png)
        self.cache.move_to_end(key)
        while len(self.cache) > W.CACHE_SIZE:
            self.cache.popitem(last=False)
        return png

    async def member_card(self, guild, member, year: int) -> tuple[bytes, W.Summary]:
        summary, names = await self.member_summary(guild, member, year)
        spec = WC.member_spec(summary, member.display_name, member.name, guild.name, names)
        asset = getattr(member, "display_avatar", None)
        fingerprint = W.digest(spec, getattr(asset, "key", None))
        return await self.cached(("member", member.id, year), fingerprint, spec, asset), summary

    async def server_card(self, guild, year: int) -> tuple[bytes, W.ServerSummary]:
        summary, names = await self.server_summary(guild, year)
        spec = WC.server_spec(summary, guild.name, names)
        asset = getattr(guild, "icon", None)
        fingerprint = W.digest(spec, getattr(asset, "key", None))
        return await self.cached(("server", guild.id, year), fingerprint, spec, asset), summary

    def forget(self, user_id: int) -> None:
        for key in [k for k in self.cache if k[0] == "member" and k[1] == user_id]:
            del self.cache[key]

    # ------------------------------------------------------------ /wrapped
    async def send_card(self, interaction: discord.Interaction, member, year: int, share: bool) -> None:
        own = member.id == interaction.user.id
        if not await self.db.tracking_allowed(member.id):
            self.forget(member.id)
            text = W.OPTOUT_SELF if own else W.optout_other(esc(member.display_name))
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        png, summary = await self.member_card(interaction.guild, member, year)
        kwargs = dict(file=discord.File(io.BytesIO(png), filename=FILENAME), ephemeral=True, allowed_mentions=NO_PINGS)
        if share and own:
            kwargs["view"] = share_view(year, member.id)
        whose = "Your" if own else f"{esc(member.display_name)}'s"
        await interaction.followup.send(f"{whose} Wrapped {year}: **{W.persona(summary).title}**", **kwargs)

    @app_commands.command(name="wrapped", description="Your year here on one card (open December 1 to January 15)")
    @app_commands.describe(year="Which year (default: the one that's open now)")
    @app_commands.guild_only()
    async def wrapped(self, interaction: discord.Interaction,
                      year: app_commands.Range[int, W.FIRST_YEAR, 2100] | None = None) -> None:
        t = now()
        year, problem = W.resolve_year(year, t, self.tz)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        uid = interaction.user.id
        left = W.cooldown_left(self.last_run.get(uid), t)
        if left:
            await interaction.response.send_message(
                f"Your card is still warm. Try again in {W.plural(left, 'second')}.", ephemeral=True)
            return
        self.last_run[uid] = t
        if len(self.last_run) > 5000:
            self.last_run = {u: at for u, at in self.last_run.items() if t - at < W.COOLDOWN}
        await self.send_card(interaction, interaction.user, year, share=True)

    @app_commands.command(name="wrapped-preview", description="Staff: preview a Wrapped card at any time")
    @app_commands.describe(year="Which year (default: the open one, else this year so far)",
                           member="Whose card (default: yours)", server="Show the server-wide card instead")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def wrapped_preview(self, interaction: discord.Interaction,
                              year: app_commands.Range[int, W.FIRST_YEAR, 2100] | None = None,
                              member: discord.Member | None = None, server: bool = False) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff can preview Wrapped cards.", ephemeral=True)
            return
        year, problem = W.resolve_year(year, now(), self.tz, staff=True)
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        if server:
            await interaction.response.defer(ephemeral=True, thinking=True)
            png, _ = await self.server_card(interaction.guild, year)
            await interaction.followup.send(f"Server Wrapped {year} (preview)", ephemeral=True,
                                            file=discord.File(io.BytesIO(png), filename=SERVER_FILENAME),
                                            allowed_mentions=NO_PINGS)
            return
        member = member or interaction.user
        if member.bot:
            await interaction.response.send_message("Bots don't get a Wrapped.", ephemeral=True)
            return
        await self.send_card(interaction, member, year, share=True)

    # ------------------------------------------------------------ sharing
    @staticmethod
    def shareable(channel) -> bool:
        """A normal chat channel: not staff, not one of the quiet ones."""
        if channel is None or not hasattr(channel, "send"):
            return False
        parent = getattr(channel, "parent", None) or channel
        if getattr(parent, "name", None) is None:
            return False
        category = getattr(parent, "category", None)
        if category is not None and config.match_by_name([category], config.STAFF_CATEGORY) is not None:
            return False
        return not any(config.match_by_name([parent], name) is not None for name in QUIET_CHANNELS)

    async def handle_share(self, interaction: discord.Interaction, year: int, user_id: int) -> None:
        user = interaction.user
        if user.id != user_id:
            await interaction.response.send_message("That's not your card. Run `/wrapped` for yours.", ephemeral=True)
            return
        t = now()
        _, problem = W.resolve_year(year, t, self.tz, staff=is_staff(user))
        if problem:
            await interaction.response.send_message(
                f"Wrapped {year} has closed, so this card can't be shared any more.", ephemeral=True)
            return
        if not await self.db.tracking_allowed(user_id):
            self.forget(user_id)
            await interaction.response.send_message(W.OPTOUT_SELF, ephemeral=True)
            return
        channel = interaction.channel
        if not self.shareable(channel):
            await interaction.response.send_message(
                "Cards can't be shared in this channel. Run `/wrapped` in a chat channel and share it there.",
                ephemeral=True)
            return
        # Claim the cooldown in one transaction, so a double click can't post twice.
        key = W.share_key(year, user_id)
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
            previous = row["value"] if row else None
            left = W.cooldown_left(int(previous) if previous and previous.isdigit() else None, t, W.SHARE_COOLDOWN)
            if not left:
                await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(t)))
        if left:
            minutes = max(left // 60, 1)
            wait = W.plural(minutes // 60, "hour") if minutes >= 120 else W.plural(minutes, "minute")
            await interaction.response.send_message(
                f"You shared this card not long ago. You can share it again in about {wait}.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            png, summary = await self.member_card(interaction.guild, user, year)
            await channel.send(W.share_text(user.mention, year, W.persona(summary).title),
                               file=discord.File(io.BytesIO(png), filename=FILENAME), allowed_mentions=NO_PINGS)
        except Exception:
            # Give the claim back so they can try again (in another channel, say).
            if previous is None:
                await self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                await self.set_meta(key, previous)
            log.warning("wrapped: couldn't share %s's card in %s", user_id, getattr(channel, "id", "?"),
                        exc_info=True)
            await interaction.followup.send(
                "I couldn't post there (I may be missing Send Messages or Attach Files). Try another channel.",
                ephemeral=True)
            return
        fresh = await self.db.execute("INSERT OR IGNORE INTO achievements (user_id, key, at) VALUES (?, ?, ?)",
                                      (user_id, W.badge_key(year), t))
        badge = f" You earned the 📓 **Wrapped {year}** badge (see `/profile`)." if fresh else ""
        await interaction.followup.send(f"Shared.{badge}", ephemeral=True)

    # ------------------------------------------------------------ the two posts
    @tasks.loop(minutes=5)
    async def tick(self) -> None:
        jobs = (
            ("announcement", lambda: self.run_job(W.ANNOUNCE_JOB, self.post_announcement)),
            ("reminder", lambda: self.run_job(W.REMINDER_JOB, self.post_reminder)),
            ("level snapshot", self.snapshot_levels),
        )
        for name, job in jobs:
            try:
                await job()
            except Exception:
                log.exception("wrapped: %s failed", name)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def due(self, job: W.Annual):
        """Record first_seen and skipped periods; return the period to run now, if any."""
        seen_key = f"first_seen:{job.name}"
        first_seen = await self.meta(seen_key)
        first_seen = int(first_seen) if first_seen else None
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (f"{job.name}:%",))}
        t = now()
        todo = W.plan_annual(job, t, self.tz, done, first_seen)
        async with self.db.transaction() as tx:
            if first_seen is None:
                await tx.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (seen_key, str(t)))
            for period in todo.mark_done:
                await tx.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, t))
        return todo.run

    async def run_job(self, job: W.Annual, action) -> None:
        period = await self.due(job)
        # Marked done only after it worked: a failure retries on the next tick.
        if period is not None and await action(period):
            await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (period.key, now()))

    def announcements(self, guild, period):
        channel = config.match_by_name(guild.text_channels, config.ANNOUNCEMENTS_CHANNEL)
        if channel is None and period.key not in self.warned:
            self.warned.add(period.key)
            log.warning("wrapped: no %s channel for %s; will keep looking", config.ANNOUNCEMENTS_CHANNEL, period.key)
        return channel

    async def post_announcement(self, period) -> bool:
        guild = self.guild()
        channel = self.announcements(guild, period) if guild else None
        if channel is None:
            return False
        png, summary = await self.server_card(guild, period.year)
        try:
            if summary.empty:  # nothing recorded this year: say it's open, skip the empty card
                await channel.send(W.announce_text(period.year, with_card=False), allowed_mentions=NO_PINGS)
            else:
                await channel.send(W.announce_text(period.year),
                                   file=discord.File(io.BytesIO(png), filename=SERVER_FILENAME),
                                   allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("wrapped: couldn't post the %s announcement; retrying", period.year, exc_info=True)
            return False
        await self.set_meta(f"{ANNOUNCED_KEY}{period.year}", str(now()))
        log.info("wrapped: announced %s", period.year)
        return True

    async def post_reminder(self, period) -> bool:
        guild = self.guild()
        channel = self.announcements(guild, period) if guild else None
        if channel is None:
            return False
        announced = await self.meta(f"{ANNOUNCED_KEY}{period.year}")
        if announced and announced.isdigit() and now() - int(announced) < W.REMINDER_GAP:
            log.info("wrapped: the %s announcement was late, so no reminder", period.year)
            return True
        try:
            await channel.send(W.reminder_text(period.year), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("wrapped: couldn't post the %s reminder; retrying", period.year, exc_info=True)
            return False
        return True

    # ------------------------------------------------------------ level snapshot
    async def snapshot_levels(self) -> None:
        """In January, once: everyone's level, so December can say how many were gained.
        Members who opt out later are dropped from every snapshot."""
        t = now()
        skip = await self.optouts()
        year = W.snapshot_due(t, self.tz)
        if year is not None and await self.meta(W.levels_key(year)) is None:
            rows = await self.db.fetchall("SELECT user_id, level FROM xp")
            levels = {str(r["user_id"]): r["level"] for r in rows if r["user_id"] not in skip}
            await self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                                  (W.levels_key(year), json.dumps(levels)))
        if not skip:
            return
        for row in await self.db.fetchall("SELECT key, value FROM meta WHERE key LIKE ?", (W.LEVELS_PREFIX + "%",)):
            try:
                levels = json.loads(row["value"])
            except ValueError:
                continue
            kept = {u: level for u, level in levels.items() if not (u.isdigit() and int(u) in skip)}
            if len(kept) != len(levels):
                await self.set_meta(row["key"], json.dumps(kept))


async def setup(bot) -> None:
    await bot.add_cog(Wrapped(bot))
