"""Module 7: Game nights, watch parties, the Town Hall and free games.

- /gamenight creates a native Discord scheduled event (Discord's own "Interested" button is
  the RSVP), records it in `gamenights` and announces it in the game's channel.
  (Watch parties and the Town Hall are recorded in `hosted_events` instead, so game-night
  counts elsewhere never include them.)
- /watchparty does the same for watching something together: a Stage event in 🎙・stage when
  the bot may schedule there, else a voice event in the Lobby. Same limits as game nights
  (one upcoming event per host, one creation an hour) plus an account-age floor.
- A one-minute loop reminds everyone marked Interested about 15 minutes before the start.
- A Town Hall (Stage event) on the first Saturday of each month, created a week ahead and
  announced once in 📣・announcements with a prompt for agenda items. Skipped, with a log line,
  when the stage channel or the permission to schedule on it is missing; cogs/ops.py's
  permission self-check reports the same gap in the mod log. TOWNHALL_TIME sets the time
  (default 12:00 Pacific; "off" disables it).
- Thursdays 18:00 local, the bot posts new free-to-keep PC games from GamerPower in
  🕹️・gaming. `free_games` stops a giveaway from being posted twice.

Needs the Create Events and Manage Events permissions; Stage events also need Manage
Channels, Mute Members and Move Members in the stage channel. No privileged intents.
"""

import asyncio
import logging
import os
import time
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from cogkit import is_staff
from cogs.lfg import GAME_CHOICES, ping_only
from logic import events as E
from logic import quests as Q
from logic import textfilter
from logic.schedule import Weekly, plan

log = logging.getLogger(__name__)

DAY = 24 * 60 * 60
ANYTHING = "anything"
GAMENIGHT_CHOICES = GAME_CHOICES + [app_commands.Choice(name="Anything", value=ANYTHING)]
FREE_GAMES_JOB = Weekly("freegames", weekday=3, hour=18, minute=0)  # Thursdays 18:00 local
# Several platforms at once only work on /filter; /giveaways answers 404 for a platform list.
FREE_GAMES_URL = "https://www.gamerpower.com/api/filter?platform=epic-games-store.steam&type=game"
FREE_GAMES_GIVE_UP = DAY  # API down this long after the scheduled time: skip the week
FETCH_TIMEOUT = 10
MAX_REMINDER_PINGS = 60  # keeps the message well under 2000 characters
EVENT_WARN_EVERY = 30 * 60  # one "missing Create Events" warning per half hour, however often it's tried
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


# Game nights live in `gamenights`; watch parties and the Town Hall in `hosted_events`. The
# reminder loop and the per-host limit read both.
EVENT_TABLES = ("gamenights", "hosted_events")
DUE_EVENTS = (
    "SELECT event_id, host_id, game, starts_at, 'gamenight' AS kind FROM gamenights WHERE reminded = 0"
    " UNION ALL SELECT event_id, host_id, NULL, starts_at, kind FROM hosted_events WHERE reminded = 0"
    " ORDER BY starts_at")
TOWNHALL_GIVE_UP = DAY  # Discord erroring this long after the scheduled time: skip the month
TOWNHALL_ENV = "TOWNHALL_TIME"
TOO_NEW = (f"Your Discord account is under {Q.MIN_ACCOUNT_DAYS} days old, so you can't host a watch "
           "party yet. Ask a mod to set one up, or join someone else's.")
BAD_LINK = ("That link won't work. Use a plain `https://` link (no spaces or brackets), "
            "or leave it out and put the details in the title.")
PREDICT_TIP = "Got a take on how it goes? `/predict create` opens a prediction people can bet coins on."


class Events(commands.Cog):
    def __init__(self, bot, fetch_json=None, environ=None):
        self.bot = bot
        self.last_created: dict[int, int] = {}  # host id -> unix time of their last /gamenight or /watchparty
        self.fetch_json = fetch_json or http_get_json  # injectable: tests never hit the network
        self.reminded: set[int] = set()  # sent this run, in case the DB write after it failed
        self.event_warned_at = 0  # last "missing Create Events" warning
        # The Town Hall job runs on Saturdays at the Town Hall time and schedules the one a week out.
        raw = (os.environ if environ is None else environ).get(TOWNHALL_ENV)
        at = E.parse_townhall_time(raw)
        if at is None and raw and raw.strip().lower() not in E.TOWNHALL_OFF:
            log.warning("%s isn't a time I can read (try 12:00 or 7pm); the Town Hall is off", TOWNHALL_ENV)
        self.townhall_job = (Weekly("townhall", weekday=E.TOWNHALL_WEEKDAY, hour=at.hour, minute=at.minute)
                             if at is not None else None)

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
        # Bots skip AutoMod, so the note is checked here before the event or announcement exists.
        if blocked := textfilter.screen("/gamenight", interaction.user.id, [when, note], allow_links=False):
            await interaction.response.send_message(blocked, ephemeral=True)
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
                if now() - self.event_warned_at >= EVENT_WARN_EVERY:
                    self.event_warned_at = now()
                    log.warning("can't create scheduled events: Front Desk is missing the Create Events "
                                "permission (Server Settings -> Roles -> Front Desk)")
                text = ("I can't create events here: I need the Create Events permission "
                        "(Server Settings -> Roles -> Front Desk). Ask a Keeper.")
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
            "SELECT event_id FROM gamenights WHERE host_id = ? AND starts_at > ? AND reminded = 0"
            " UNION ALL SELECT event_id FROM hosted_events WHERE host_id = ? AND starts_at > ? AND reminded = 0",
            (host_id, t, host_id, t))
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

    # ------------------------------------------------------------ /watchparty
    def stage_for_events(self, guild):
        """(stage channel, None) when the bot may schedule a Stage event there, else
        (None, why not). Reads the cache only."""
        stage = config.match_by_name(getattr(guild, "stage_channels", []), config.STAGE_CHANNEL)
        if stage is None:
            return None, f"there is no {config.STAGE_CHANNEL} channel (run server/public_mode.py)"
        me = getattr(guild, "me", None)
        if me is None:
            return None, "the server isn't fully loaded yet"
        perms = stage.permissions_for(me)
        missing = [p for p in ("view_channel", *E.STAGE_EVENT_PERMS) if not getattr(perms, p, False)]
        if missing:
            return None, (f"Front Desk is missing {', '.join(p.replace('_', ' ').title() for p in missing)} in "
                          f"{config.STAGE_CHANNEL} (Server Settings -> Roles -> Front Desk)")
        return stage, None

    def predictions_on(self) -> bool:
        get_cog = getattr(self.bot, "get_cog", None)
        return get_cog is not None and get_cog("Games") is not None

    @app_commands.command(name="watchparty",
                          description="Schedule a watch party: a server event people can mark Interested")
    @app_commands.describe(
        title="What you're watching",
        when="Server time (Pacific), e.g. 9pm, tomorrow 8pm, fri 9pm, 2026-10-10 20:00",
        link="Optional https:// link to the stream or video",
    )
    async def watchparty(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 80],
        when: app_commands.Range[str, 1, 40],
        link: app_commands.Range[str, 1, E.MAX_LINK] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        host = interaction.user
        # Bots skip AutoMod: the title is checked before the event or the announcement exists.
        # The title may not carry a link at all; the link field may, but never an invite.
        blocked = (textfilter.screen("/watchparty", host.id, [title, when], allow_links=False)
                   or textfilter.screen("/watchparty", host.id, [link]))
        if blocked:
            await interaction.response.send_message(blocked, ephemeral=True)
            return
        url = None
        if link is not None:
            url = E.watch_link(link)
            if url is None:
                await interaction.response.send_message(BAD_LINK, ephemeral=True)
                return
        local_now = datetime.fromtimestamp(now(), self.tz)
        start = E.parse_when(when, local_now, self.tz)
        problem = "unparsed" if start is None else E.check_when(start, local_now)
        if problem is not None:
            text = WHEN_PROBLEMS.get(problem, WHEN_PROBLEMS[None])
            await interaction.response.send_message(
                f"{text} Pick a time in the next 30 days. Some examples: {E.WHEN_EXAMPLES}", ephemeral=True)
            return
        # The stage when the bot may schedule there, else the Lobby as an ordinary voice event.
        place, why = self.stage_for_events(guild)
        entity = discord.EntityType.stage_instance
        if place is None:
            place = config.match_by_name(guild.voice_channels, config.LOBBY_VOICE)
            entity = discord.EntityType.voice
            if place is None:
                await interaction.response.send_message(
                    f"I couldn't find {config.STAGE_CHANNEL} or the {config.LOBBY_VOICE} voice channel, "
                    "so there's nowhere to host it.", ephemeral=True)
                return
            log.info("watch party goes to %s: %s", config.LOBBY_VOICE, why)

        staff = is_staff(host)
        t, last = now(), self.last_created.get(host.id)
        if not staff:
            if not Q.established(host.id, t):
                await interaction.response.send_message(TOO_NEW, ephemeral=True)
                return
            if last is not None and t - last < CREATE_COOLDOWN:
                mins = (CREATE_COOLDOWN - (t - last)) // 60 + 1
                await interaction.response.send_message(
                    f"You just scheduled an event. You can make another in {mins} min.", ephemeral=True)
                return
            # Same claim as /gamenight (no await since the check), and the same one-upcoming
            # limit: game nights and watch parties share both.
            self.last_created[host.id] = t
            if await self.upcoming_for(guild, host.id, t) >= MAX_UPCOMING_PER_HOST:
                self.release_claim(host.id, t, last)
                await interaction.response.send_message(
                    "You already have an event coming up. Run that one first, or ask a mod "
                    "if you need two.", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            event = await guild.create_scheduled_event(
                name=E.watchparty_name(title),
                description=E.watchparty_description(discord.utils.escape_markdown(host.display_name), url),
                start_time=E.to_utc(start),
                entity_type=entity,
                channel=place,
                privacy_level=discord.PrivacyLevel.guild_only,
                reason=f"/watchparty by {host} ({host.id})",
            )
        except discord.HTTPException as exc:
            if not staff:
                self.release_claim(host.id, t, last)
            if isinstance(exc, discord.Forbidden):
                if now() - self.event_warned_at >= EVENT_WARN_EVERY:
                    self.event_warned_at = now()
                    log.warning("can't create a watch party in %s: Front Desk is missing a permission "
                                "(Create Events, or the Stage permissions in the stage channel)", place.name)
                text = ("I can't create events here: I need the Create Events permission "
                        "(Server Settings -> Roles -> Front Desk). Ask a Keeper.")
            else:
                log.warning("creating a watch party failed", exc_info=True)
                text = "Discord wouldn't create the event. Check the time and try again in a moment."
            await interaction.followup.send(text, ephemeral=True)
            return
        await self.db.execute(
            "INSERT OR REPLACE INTO hosted_events (event_id, host_id, kind, starts_at, reminded)"
            " VALUES (?, ?, ?, ?, 0)", (event.id, host.id, E.WATCHPARTY, int(start.timestamp())))
        announced = await self.announce_watchparty(guild, event, place, host, url)
        extra = "" if announced else "\n(I couldn't post the announcement, so share the link yourself.)"
        await interaction.followup.send(
            f"Watch party is set for {discord.utils.format_dt(start, 'F')} in {place.mention}. "
            f"People can tap Interested to get a reminder: {event.url}{extra}", ephemeral=True)

    async def announce_watchparty(self, guild, event, place, host, url: str | None) -> bool:
        """Announced where "Anything" game nights are: 🕹️・gaming. Nobody is pinged here (a game
        night's announcement only pings its game role, and a watch party has none); the
        reminder pings the people who tapped Interested, and the opt-in Game Night role only
        when the host is staff."""
        channel = self.text_channel_for(guild, None)
        if channel is None:
            log.warning("no %s channel for the watch party announcement", config.GAMING_CHANNEL)
            return False
        start = event.start_time
        lines = [
            f"**{discord.utils.escape_markdown(event.name)}** "
            f"{discord.utils.format_dt(start, 'F')} ({discord.utils.format_dt(start, 'R')}) "
            f"in {place.mention}, hosted by {host.mention}.",
        ]
        if url:
            lines.append(f"Watching: {E.show_link(url)}")
        lines.append(f"Tap Interested for a reminder: {event.url}")
        if self.predictions_on():
            lines.append(PREDICT_TIP)
        try:
            await channel.send("\n".join(lines), allowed_mentions=ping_only())
        except discord.HTTPException:
            log.warning("couldn't announce watch party %s", event.id, exc_info=True)
            return False
        return True

    # ------------------------------------------------------------ Town Hall
    async def run_townhall(self) -> None:
        job = self.townhall_job
        if job is None:
            return
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
            # Marked done only once handled: a Discord error retries on the next tick, for a day.
            try:
                ok = await self.schedule_townhall(todo.run)
            except Exception:
                log.exception("town hall %s failed; retrying", todo.run.key)
                ok = False
            if not ok and now() - todo.run.scheduled_at >= TOWNHALL_GIVE_UP:
                log.warning("town hall %s: giving up for this month", todo.run.key)
                ok = True
            if ok:
                await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (todo.run.key, now()))

    async def schedule_townhall(self, period) -> bool:
        """True when this week is handled (created, not a Town Hall week, or skipped for a
        reason that retrying won't fix); False to retry."""
        guild = self.guild()
        if guild is None:
            return False
        start = E.townhall_start(period.scheduled_at, self.tz)
        if start is None:
            return True  # the Saturday after this one isn't the first of its month
        starts_at = int(start.timestamp())
        if starts_at <= now():
            log.info("town hall %s: too late to schedule it", period.key)
            return True
        if await self.db.fetchone("SELECT 1 FROM hosted_events WHERE kind = ? AND starts_at = ?",
                                  (E.TOWNHALL, starts_at)):
            return True  # created on an earlier try
        stage, why = self.stage_for_events(guild)
        if stage is None:
            log.warning("town hall %s skipped: %s", period.key, why)
            return True
        try:
            event = await guild.create_scheduled_event(
                name=E.TOWNHALL_NAME,
                description=E.townhall_description(),
                start_time=E.to_utc(start),
                entity_type=discord.EntityType.stage_instance,
                channel=stage,
                privacy_level=discord.PrivacyLevel.guild_only,
                reason="Monthly Town Hall",
            )
        except discord.Forbidden:
            log.warning("town hall %s skipped: Discord refused the event. Front Desk needs Create Events "
                        "(Server Settings -> Roles -> Front Desk)", period.key)
            return True
        except discord.HTTPException:
            log.warning("town hall %s: Discord wouldn't create the event; retrying", period.key, exc_info=True)
            return False
        me = getattr(self.bot, "user", None)
        await self.db.execute(
            "INSERT OR REPLACE INTO hosted_events (event_id, host_id, kind, starts_at, reminded)"
            " VALUES (?, ?, ?, ?, 0)", (event.id, me.id if me else 0, E.TOWNHALL, starts_at))
        await self.announce_townhall(guild, event, stage)
        log.info("town hall %s: created %s", period.key, event.id)
        return True

    async def announce_townhall(self, guild, event, stage) -> bool:
        """Once, in announcements, with the agenda prompt. The event exists either way, so a
        failed post is logged and not retried."""
        channel = config.match_by_name(guild.text_channels, config.ANNOUNCEMENTS_CHANNEL)
        if channel is None:
            log.warning("no %s channel for the Town Hall announcement", config.ANNOUNCEMENTS_CHANNEL)
            return False
        start = event.start_time
        ideas = config.match_by_name(getattr(guild, "forums", []), config.SUGGESTIONS_FORUM)
        where = ideas.mention if ideas is not None else "the suggestions forum"
        embed = style.embed(
            title="Town Hall: what should we talk about?",
            description="\n".join([
                f"{discord.utils.format_dt(start, 'F')} ({discord.utils.format_dt(start, 'R')}) in {stage.mention}.",
                "",
                "**Agenda**",
                f"Post what you want covered in {where} with \"Town Hall\" in the title: a rule that "
                "bugs you, a game we should add, an event idea, anything.",
                "Posts with the most 👍 go first. No topic is fine too: come and listen.",
                "",
                f"Tap Interested for a reminder: {event.url}",
            ]),
            footer=style.label("town hall", f"{start.astimezone(self.tz):%B %Y}"))
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("couldn't announce town hall %s", event.id, exc_info=True)
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
        for row in await self.db.fetchall(DUE_EVENTS):
            try:
                await self.remind(guild, row, t)
            except Exception:
                log.exception("reminder for game night %s failed", row["event_id"])

    async def mark_reminded(self, event_id: int) -> None:
        # Event ids are Discord snowflakes: one id is only ever in one of the two tables.
        for table in EVENT_TABLES:
            await self.db.execute(f"UPDATE {table} SET reminded = 1 WHERE event_id = ?", (event_id,))

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
            for table in EVENT_TABLES:
                await self.db.execute(f"UPDATE {table} SET starts_at = ? WHERE event_id = ?", (starts_at, eid))
        state = E.reminder_state(starts_at, t)
        if state == "wait":
            return
        if state == "expired":
            await self.mark_reminded(eid)
            return

        townhall = row["kind"] == E.TOWNHALL
        game = config.game_by_key(row["game"]) if row["game"] else None
        # The Town Hall is reminded where it was announced; everything else in a game channel.
        channel = (config.match_by_name(guild.text_channels, config.ANNOUNCEMENTS_CHANNEL) if townhall
                   else self.text_channel_for(guild, game))
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
        # Not for the Town Hall: that isn't what people opted in to. And for a watch party only
        # when staff host it: its title is the host's own words, and any member can make one,
        # so a member's watch party reminds the people who tapped Interested and nobody else.
        host = guild.get_member(row["host_id"]) if row["kind"] == E.WATCHPARTY else None
        quiet = townhall or (row["kind"] == E.WATCHPARTY and (host is None or not is_staff(host)))
        gamenight_role = None if quiet else config.match_by_name(guild.roles, config.GAMENIGHT_ROLE)
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
        try:
            await self.run_townhall()
        except Exception:
            log.exception("town hall job failed")

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
            if not isinstance(data, list):
                log.info("free games %s: GamerPower sent no list (%s), so nothing matches this week",
                         period.key, type(data).__name__)
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
