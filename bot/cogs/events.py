"""Module 7: Game nights and free games.

- /gamenight creates a native Discord scheduled event (Discord's own "Interested" button is
  the RSVP), records it in `gamenights` and announces it in the game's channel.
- A one-minute loop reminds everyone marked Interested about 15 minutes before the start.
- Thursdays 18:00 local, the bot posts new free-to-keep PC games from GamerPower in
  🕹️・gaming. `free_games` stops a giveaway from being posted twice.

Needs the Manage Events permission. No privileged intents.
"""

import asyncio
import logging
import time
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from cogs.lfg import GAME_CHOICES, ping_only
from logic import events as E
from logic.schedule import Weekly, plan

log = logging.getLogger(__name__)

DAY = 24 * 60 * 60
ANYTHING = "anything"
GAMENIGHT_CHOICES = GAME_CHOICES + [app_commands.Choice(name="Anything", value=ANYTHING)]
FREE_GAMES_JOB = Weekly("freegames", weekday=3, hour=18, minute=0)  # Thursdays 18:00 local
FREE_GAMES_URL = "https://www.gamerpower.com/api/giveaways?platform=epic-games-store.steam&type=game"
FREE_GAMES_GIVE_UP = DAY  # API down this long after the scheduled time: skip the week
FETCH_TIMEOUT = 10
MAX_REMINDER_PINGS = 60  # keeps the message well under 2000 characters
ENDED = (discord.EventStatus.cancelled, discord.EventStatus.completed)

WHEN_PROBLEMS = {
    None: "I couldn't read that time.",
    "past": "That time is in the past.",
    "too_far": "That's more than 30 days away.",
}


def now() -> int:
    return int(time.time())


async def http_get_json(url: str):
    """GET `url` and decode JSON. Raises on timeouts, HTTP errors and bad JSON."""
    if E.safe_url(url) is None:
        raise ValueError(f"refusing to fetch {url!r}")
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # aiohttp only follows http(s) redirects.
        async with session.get(url, max_redirects=3,
                               headers={"User-Agent": "FrontDesk Discord bot"}) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


# Anti-spam on a public server: each event pings a game role, so regular members get
# one upcoming game night at a time and one creation per hour. Mods are exempt.
MAX_UPCOMING_PER_HOST = 1
CREATE_COOLDOWN = 60 * 60


def is_staff(member) -> bool:
    guild = getattr(member, "guild", None)
    if guild is not None and getattr(guild, "owner_id", None) == member.id:
        return True
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return True
    names = {getattr(r, "name", "") for r in getattr(member, "roles", [])}
    return bool(names & {config.KEEPER_ROLE, config.MOD_ROLE})


class Events(commands.Cog):
    def __init__(self, bot, fetch_json=None):
        self.bot = bot
        self.last_created: dict[int, int] = {}  # host id -> unix time of their last /gamenight
        self.fetch_json = fetch_json or http_get_json  # injectable: tests never hit the network
        self.reminded: set[int] = set()  # sent this run, in case the DB write after it failed

    async def cog_load(self) -> None:
        self.reminders.start()
        self.weekly.start()

    async def cog_unload(self) -> None:
        self.reminders.cancel()
        self.weekly.cancel()

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def release_claim(self, host_id: int, claimed_at: int, previous: int | None) -> None:
        """Undo a cooldown claim, unless a newer request already replaced it."""
        if self.last_created.get(host_id) == claimed_at:
            if previous is None:
                self.last_created.pop(host_id, None)
            else:
                self.last_created[host_id] = previous

    @staticmethod
    def text_channel_for(guild, game: config.Game | None):
        """The game's channel, or 🕹️・gaming for "Anything" (or if the game channel is gone)."""
        if game is not None:
            channel = config.match_by_name(guild.text_channels, game.channel_name)
            if channel is not None:
                return channel
        return config.match_by_name(guild.text_channels, config.GAMING_CHANNEL)

    # ------------------------------------------------------------ /gamenight
    @app_commands.command(name="gamenight", description="Schedule a game night: a server event people can mark Interested")
    @app_commands.describe(
        game="Which game (or Anything)",
        when="Server time (Pacific), e.g. 9pm, tomorrow 8pm, fri 9pm, 2026-10-10 20:00",
        size="How many players (over 5 uses the Lobby)",
        note="Anything else people should know",
    )
    @app_commands.choices(game=GAMENIGHT_CHOICES)
    async def gamenight(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str],
        when: app_commands.Range[str, 1, 40],
        size: app_commands.Range[int, 2, 50] | None = None,
        note: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        local_now = datetime.fromtimestamp(now(), self.tz)
        start = E.parse_when(when, local_now, self.tz)
        problem = "unparsed" if start is None else E.check_when(start, local_now)
        if problem is not None:
            text = WHEN_PROBLEMS.get(problem, WHEN_PROBLEMS[None])
            await interaction.response.send_message(
                f"{text} Pick a time in the next 30 days. Some examples: {E.WHEN_EXAMPLES}", ephemeral=True)
            return
        voice_name = E.voice_for(size)
        voice = config.match_by_name(guild.voice_channels, voice_name)
        if voice is None:
            await interaction.response.send_message(
                f"I couldn't find the {voice_name} voice channel, so I can't host it there.", ephemeral=True)
            return

        host = interaction.user
        if not is_staff(host):
            t = now()
            last = self.last_created.get(host.id)
            if last is not None and t - last < CREATE_COOLDOWN:
                mins = (CREATE_COOLDOWN - (t - last)) // 60 + 1
                await interaction.response.send_message(
                    f"You just made a game night. You can make another in {mins} min.", ephemeral=True)
                return
            # Claim the cooldown now, with no await since the check above, so concurrent
            # submits can't both pass; give it back if this request is refused.
            self.last_created[host.id] = t
            if await self.upcoming_for(guild, host.id, t) >= MAX_UPCOMING_PER_HOST:
                self.release_claim(host.id, t, last)
                await interaction.response.send_message(
                    "You already have a game night coming up. Run that one first, or ask a mod "
                    "if you need two.", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        g = config.game_by_key(game.value)  # None for Anything
        try:
            event = await guild.create_scheduled_event(
                name=E.event_name(g.role if g else None),
                description=E.event_description(discord.utils.escape_markdown(host.display_name),
                                                discord.utils.escape_markdown(note) if note else note, size),
                start_time=E.to_utc(start),
                entity_type=discord.EntityType.voice,
                channel=voice,
                privacy_level=discord.PrivacyLevel.guild_only,
                reason=f"/gamenight by {host} ({host.id})",
            )
        except discord.HTTPException as exc:
            # Nothing was created, so give the cooldown back whatever Discord said.
            if not is_staff(host):
                self.release_claim(host.id, t, last)
            if isinstance(exc, discord.Forbidden):
                log.warning("can't create scheduled events: missing Manage Events")
                text = "I can't create events here: I need the Manage Events permission."
            else:
                log.warning("creating a game night failed", exc_info=True)
                text = "Discord wouldn't create the event. Check the time and try again in a moment."
            await interaction.followup.send(text, ephemeral=True)
            return
        starts_at = int(start.timestamp())
        await self.db.execute(
            "INSERT OR REPLACE INTO gamenights (event_id, host_id, game, starts_at, reminded) VALUES (?, ?, ?, ?, 0)",
            (event.id, host.id, g.key if g else None, starts_at),
        )
        announced = await self.announce(guild, g, event, voice, host, note)
        extra = "" if announced else "\n(I couldn't post the announcement, so share the link yourself.)"
        await interaction.followup.send(
            f"Game night is set for {discord.utils.format_dt(start, 'F')}. "
            f"People can tap Interested to get a reminder: {event.url}{extra}", ephemeral=True)

    async def upcoming_for(self, guild, host_id: int, t: int) -> int:
        """How many of `host_id`'s game nights are still upcoming. Rows for events that were
        cancelled or deleted in Discord are marked reminded (as `remind` would) and not counted."""
        rows = await self.db.fetchall(
            "SELECT event_id FROM gamenights WHERE host_id = ? AND starts_at > ? AND reminded = 0",
            (host_id, t))
        n = 0
        for row in rows:
            eid = row["event_id"]
            event = guild.get_scheduled_event(eid)
            if event is None:
                try:
                    event = await guild.fetch_scheduled_event(eid)
                except discord.NotFound:
                    await self.mark_reminded(eid)
                    continue
                except discord.HTTPException:
                    log.warning("couldn't check game night %s; counting it", eid, exc_info=True)
                    n += 1
                    continue
            if event.status in ENDED:
                await self.mark_reminded(eid)
                continue
            n += 1
        return n

    async def announce(self, guild, g, event, voice, host, note) -> bool:
        channel = self.text_channel_for(guild, g)
        if channel is None:
            log.warning("no %s channel for the game night announcement", config.GAMING_CHANNEL)
            return False
        role = config.match_by_name(guild.roles, g.role) if g else None
        start = event.start_time
        lines = [
            f"{role.mention + ' ' if role else ''}**{discord.utils.escape_markdown(event.name)}** "
            f"{discord.utils.format_dt(start, 'F')} ({discord.utils.format_dt(start, 'R')}) "
            f"in {voice.mention}, hosted by {host.mention}.",
        ]
        if note:
            lines.append(f"> {discord.utils.escape_markdown(note)}")
        lines.append(f"Tap Interested for a reminder: {event.url}")
        try:
            # Only the game role may ping: note/names are user text and can't widen it.
            await channel.send("\n".join(lines), allowed_mentions=ping_only(roles=[role] if role else ()))
        except discord.HTTPException:
            log.warning("couldn't announce game night %s", event.id, exc_info=True)
            return False
        return True

    # ------------------------------------------------------------ reminders
    @tasks.loop(minutes=1)
    async def reminders(self) -> None:
        try:
            await self.run_reminders()
        except Exception:
            log.exception("game night reminders failed")

    @reminders.before_loop
    async def before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    async def run_reminders(self) -> None:
        guild = self.guild()
        if guild is None:
            return
        t = now()
        for row in await self.db.fetchall("SELECT * FROM gamenights WHERE reminded = 0 ORDER BY starts_at"):
            try:
                await self.remind(guild, row, t)
            except Exception:
                log.exception("reminder for game night %s failed", row["event_id"])

    async def mark_reminded(self, event_id: int) -> None:
        await self.db.execute("UPDATE gamenights SET reminded = 1 WHERE event_id = ?", (event_id,))

    async def remind(self, guild, row, t: int) -> None:
        eid = row["event_id"]
        if eid in self.reminded:  # sent already; only the DB write is missing
            await self.mark_reminded(eid)
            return
        event = guild.get_scheduled_event(eid)
        if event is None:
            # Not cached: only ask the API once it could be due (by the stored start time).
            if E.reminder_state(row["starts_at"], t) == "wait":
                return
            try:
                event = await guild.fetch_scheduled_event(eid)
            except discord.NotFound:
                log.info("game night %s was deleted; no reminder", eid)
                await self.mark_reminded(eid)
                return
        if event.status in ENDED:
            log.info("game night %s is %s; no reminder", eid, event.status.name)
            await self.mark_reminded(eid)
            return
        starts_at = int(event.start_time.timestamp())
        if starts_at != row["starts_at"]:  # rescheduled in Discord
            await self.db.execute("UPDATE gamenights SET starts_at = ? WHERE event_id = ?", (starts_at, eid))
        state = E.reminder_state(starts_at, t)
        if state == "wait":
            return
        if state == "expired":
            await self.mark_reminded(eid)
            return

        game = config.game_by_key(row["game"]) if row["game"] else None
        channel = self.text_channel_for(guild, game)
        if channel is None:
            log.warning("no channel for the game night %s reminder", eid)
            await self.mark_reminded(eid)
            return
        users = [u async for u in event.users() if not u.bot]
        shown = users[:MAX_REMINDER_PINGS]
        where = f" in <#{event.channel_id}>" if event.channel_id else ""
        text = (f"**{discord.utils.escape_markdown(event.name)}** starts "
                f"{discord.utils.format_dt(event.start_time, 'R')}{where}.")
        if shown:
            text += " " + " ".join(f"<@{u.id}>" for u in shown)
            if len(users) > len(shown):
                text += f" and {len(users) - len(shown)} more"
        # Opt-in Game Night role (self-assign panel): pinged for every game night that starts.
        gamenight_role = config.match_by_name(guild.roles, config.GAMENIGHT_ROLE)
        if gamenight_role is not None:
            text = f"{gamenight_role.mention} {text}"
        await channel.send(text, allowed_mentions=ping_only(
            roles=[gamenight_role] if gamenight_role else (), users=[discord.Object(u.id) for u in shown]))
        self.reminded.add(eid)
        await self.mark_reminded(eid)

    # ------------------------------------------------------------ free games
    @tasks.loop(minutes=5)
    async def weekly(self) -> None:
        try:
            await self.run_weekly()
        except Exception:
            log.exception("free games job failed")

    @weekly.before_loop
    async def before_weekly(self) -> None:
        await self.bot.wait_until_ready()

    async def run_weekly(self) -> None:
        job = FREE_GAMES_JOB
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
            # Marked done only after it worked: an API or Discord error retries on the next
            # tick, until a day has passed. free_games ids stop a retry from reposting.
            try:
                ok = await self.post_free_games(todo.run)
            except Exception:
                log.exception("free games %s failed; retrying", todo.run.key)
                ok = False
            if not ok and now() - todo.run.scheduled_at >= FREE_GAMES_GIVE_UP:
                log.warning("free games %s: giving up for this week", todo.run.key)
                ok = True
            if ok:
                await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (todo.run.key, now()))

    async def post_free_games(self, period) -> bool:
        """True when this week is handled (posted, or nothing new); False to retry."""
        try:
            data = await self.fetch_json(FREE_GAMES_URL)
            seen = {r["id"] for r in await self.db.fetchall("SELECT id FROM free_games")}
            games = E.select_giveaways(data, seen)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("free games %s: GamerPower fetch failed (%s); retrying", period.key, exc)
            return False
        if not games:
            log.info("free games %s: nothing new", period.key)
            return True
        guild = self.guild()
        channel = config.match_by_name(guild.text_channels, config.GAMING_CHANNEL) if guild else None
        if channel is None:
            log.warning("free games %s: no %s channel, nothing posted", period.key, config.GAMING_CHANNEL)
            return True
        shown = games[:E.MAX_FREE_GAMES]
        embed = style.embed(title="Free games this week", description="\n\n".join(E.giveaway_lines(shown)),
                            footer=style.label("free to keep", period.key.split(":")[1]))
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        t = now()
        async with self.db.transaction() as tx:
            for g in shown:
                await tx.execute("INSERT OR IGNORE INTO free_games (id, posted_at) VALUES (?, ?)", (g.id, t))
        log.info("free games %s: posted %d", period.key, len(shown))
        return True


async def setup(bot) -> None:
    await bot.add_cog(Events(bot))
