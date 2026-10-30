"""Monkey SMP: a bridge between this server and the owner's Minecraft server.

Three parts, each on only when its own settings are in the environment (see
logic/monkey.py `read_setup`); with none of them the cog loads, starts nothing and every
command says the bridge is not set up.

- Status panel (MONKEY_ADDRESS): one message in the Monkey SMP channel, edited in place when
  what it shows changes. The server is asked with the Server List Ping protocol every two
  minutes; while it does not answer the polls back off and the panel is edited once.
- Links: `/smp link <name>` records which Minecraft name is whose (one name per member,
  one member per name), for the chat relay and playtime. A link is only that: it never
  gives anyone access to Monkey SMP. Access is a paid pass that staff grant by hand or
  through the store, and this bot never sends `whitelist` or any other command that
  changes the server (`R.sendable`); the one exception is opt-in, REFERRAL_GRANT=rcon, and
  covers only the referral's two wardrobe scores (`R.grant_sendable`, `grant_call`).
  `/smp unlink`, leaving or a ban forget the link and nothing else, so a paying player keeps
  their access. Staff link a name for a member with
  `/smp adopt` and forget one with `/smp remove`.
- Chat, both ways, in the Monkey SMP channel. Discord to Minecraft is a `tellraw` over RCON,
  rate limited, never for bots. Minecraft to Discord tails the server log read-only
  (MONKEY_LOG_PATH) in a worker thread and posts at most one batched message every few
  seconds, with no pings. An allow-list decides what comes through: a player's words (chat,
  and death and advancement lines, whose text a player or a mod can shape) are relayed only
  when the name is linked to a member who is in the server, not timed out and able to
  send messages in the Monkey SMP channel. Everything else stays in the
  game. Join and leave lines are the bot's own words and show for anyone.
- Playtime: a linked member seen in two player-list polls in a row, with at least one other
  player on in both (company, like voice coins: nobody is paid to idle alone), is credited
  the time between them; every full half hour pays a few coins up to a daily cap (refs keyed
  on member, day and slot). `/smp top` ranks the week; tracking opt-out is respected.
- Nudges (SMP_NUDGES=on, off by default; rules in logic/smpnudge.py): a member who ran
  `/smp nudges on` and hasn't been in a player-list poll for a week gets one DM about what
  happened since. The same polls record when they were last seen; nobody else is recorded.

This file owns the sockets and the file handle. Nothing here ever starts, stops or
configures the Minecraft server or changes its whitelist, and the RCON password is never
logged or shown: errors carry fixed text and an exception type name only.
"""

import asyncio
import logging
import os
import time
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from cogkit import is_staff
from logic import botm as B
from logic import coins as coin_rules
from logic import quests
from logic import monkey as R
from logic import smpnudge as N
from logic import smpwatch as W
from logic.crew import is_quiet

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
PANEL_KEY = "monkey:panel"  # meta: "channel_id:message_id" of the status panel
HEALTH_NAME = "Monkey SMP bridge"  # field name for /status (see `health_text`)
SLOW_DOWN, NOT_SENT = "⏳", "📴"
# Posted (rarely) where a player's chat would have been; no name, none of their text.
UNLINKED_HINT = "*Some players aren't linked, so their chat stays in the game. `/smp link`*"

MESSAGES = {
    "off": "The Monkey SMP bridge isn't set up yet.",
    "bad_name": "That isn't a Minecraft Java name: 3 to 16 letters, digits or underscores, nothing else.",
    "young": (f"Your Discord account must be {quests.MIN_ACCOUNT_DAYS}+ days old to link a Minecraft name. "
              "If it can't wait, ask a mod to link it for you."),
    "cooldown": "Give it a moment, then try again.",
    "have_one": "You already linked `{name}`. Run `/smp unlink` first if you want to change it.",
    "taken": "Someone here already linked that Minecraft name. If it's yours, open a ticket in {help}.",
    "already": "`{name}` is already linked to you.",
    "linked": ("Linked `{name}` to your Discord account. What you say in the game now shows up in the Monkey SMP "
               "channel, and your playtime counts here. A link doesn't get you onto the server; {access} "
               "`/smp unlink` forgets the link."),
    "none": "You haven't linked a Minecraft name. `/smp link` connects one for chat and playtime.",
    "unlinked": "Unlinked `{name}`. Your access to Monkey SMP doesn't change.",
    "failed": "Something went wrong on my side. Try again in a minute.",
    "nudges_off_here": "Nudges aren't switched on for this server.",
    "nudges_link": "Link your Minecraft name first (`/smp link`): that's how I can tell when you were last on.",
    "nudges_privacy": ("You've turned tracking off (`/privacy`), so I don't keep when you were last on and "
                       "can't tell when you've been away."),
    "nudges_on": ("Nudges on. If you're off Monkey SMP for a week, I'll DM you once with what you missed, and not "
                  "again until you've played and been away another week. Your DMs from server members need to be "
                  "open. `/smp nudges off` stops them."),
    "nudges_off": "Nudges off. I won't DM you about Monkey SMP.",
    "nudges_were_off": "Nudges were already off.",
}
# Appended to the /smp link reply while nudges are switched on (logic/smpnudge.py).
NUDGE_HINT = " Away for a while? `/smp nudges on` and I'll DM you once with what you missed."
NUDGE_CHOICES = [app_commands.Choice(name="on: DM me once if I'm away a week", value="on"),
                 app_commands.Choice(name="off: no DMs", value="off")]
# How a member gets onto Monkey SMP. {help} is the help channel (a mention when it exists).
ACCESS = f"for that you need a Monkey SMP pass, from <{R.PASS_URL}> or through a ticket in {{help}}."


def now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- transports
class PingError(Exception):
    """The server did not answer a status ping usefully."""


class RconError(Exception):
    """RCON did not work. The text is fixed and never holds the password or a command."""


class RconRefused(RconError):
    def __init__(self):
        super().__init__("the server refused the RCON password")


async def _close(writer) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # a reset while closing is not news
        pass


async def ping_server(host: str, port: int, timeout: float = R.PING_TIMEOUT) -> dict:
    """One Server List Ping: handshake, status request, the JSON reply. A fresh connection
    each time, everything inside one timeout, and a reply over MAX_STATUS_BYTES is refused
    before it is read."""
    async def exchange() -> dict:
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(R.handshake(host, port) + R.STATUS_REQUEST)
            await writer.drain()
            prefix = b""
            while True:
                prefix += await reader.readexactly(1)
                try:
                    length, _ = R.read_varint(prefix)
                    break
                except R.Incomplete:
                    continue
            if not 1 <= length <= R.MAX_STATUS_BYTES:
                raise ValueError("status length out of range")
            return R.parse_status(await reader.readexactly(length))
        finally:
            await _close(writer)

    try:
        return await asyncio.wait_for(exchange(), timeout)
    except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError, ValueError) as exc:
        raise PingError(type(exc).__name__) from None


class Rcon:
    """A Source RCON client: one connection kept open, one command at a time. Packet sizes
    are checked before anything is read (logic/monkey.py), each command has one timeout
    for connecting, signing in and the reply, and a failure closes the connection."""

    def __init__(self, host: str, port: int, password: str, timeout: float = R.RCON_TIMEOUT):
        self.host, self.port, self.timeout = host, port, timeout
        self._password = password
        self.lock = asyncio.Lock()
        self.reader = None
        self.writer = None
        self.next_id = 0

    def __repr__(self) -> str:
        return f"<Rcon {self.host}:{self.port}>"

    async def close(self) -> None:
        writer, self.reader, self.writer = self.writer, None, None
        if writer is not None:
            await _close(writer)

    def _id(self) -> int:
        self.next_id = self.next_id % 0x7FFFFFF0 + 1
        return self.next_id

    async def _read(self) -> tuple[int, int, str]:
        length = R.rcon_length(await self.reader.readexactly(4))
        return R.parse_rcon(await self.reader.readexactly(length))

    async def _connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        request = self._id()
        self.writer.write(R.rcon_packet(request, R.RCON_AUTH, self._password))
        await self.writer.drain()
        for _ in range(R.RCON_MAX_SKIP):
            reply_id, kind, _ = await self._read()
            if kind != R.RCON_AUTH_RESPONSE:
                continue  # Source servers send an empty reply before the verdict
            if reply_id == R.RCON_AUTH_FAILED:
                raise RconRefused()
            if reply_id == request:
                return
        raise ValueError("no sign-in reply")

    async def _exchange(self, text: str) -> str:
        request = self._id()
        self.writer.write(R.rcon_packet(request, R.RCON_COMMAND, text))
        await self.writer.drain()
        for _ in range(R.RCON_MAX_SKIP):
            reply_id, kind, body = await self._read()
            if reply_id == request and kind == R.RCON_RESPONSE:
                return body
        raise ValueError("no reply")

    async def _run(self, text: str) -> str:
        if self.writer is not None and not self.writer.is_closing() and not self.reader.at_eof():
            try:
                return await self._exchange(text)
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    raise
            except OSError:
                pass
            # The kept connection had gone stale (the server restarted) before it answered
            # anything: sign in again and send the command once more.
        await self.close()
        await self._connect()
        return await self._exchange(text)

    async def command(self, text: str) -> str:
        """Run one command and return the server's answer. Raises RconError."""
        try:
            R.rcon_packet(0, R.RCON_COMMAND, text)
        except ValueError:
            raise RconError("that command cannot be sent") from None
        async with self.lock:
            try:
                return await asyncio.wait_for(self._run(text), self.timeout)
            except RconRefused:
                await self.close()
                raise
            except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError, ValueError) as exc:
                await self.close()
                raise RconError(f"no answer ({type(exc).__name__})") from None


def read_log(path: str, state: R.TailState) -> tuple[R.TailState, bytes, bool] | None:
    """One pass over the server log: open read-only, read at most TAIL_CHUNK bytes from
    where the last pass stopped, close. None when the file cannot be read right now. The
    handle is never kept, so the server can rotate the file. Runs in a worker thread."""
    try:
        with open(path, "rb") as handle:
            info = os.fstat(handle.fileno())
            start, reset = R.tail_start(state, info.st_ino, info.st_size)
            data = b""
            if start < info.st_size:
                handle.seek(start)
                data = handle.read(R.TAIL_CHUNK)
    except OSError:
        return None
    return R.TailState(info.st_ino, start + len(data)), data, reset


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


def parse_ref(raw: str | None) -> tuple[int, int] | None:
    try:
        channel_id, message_id = (raw or "").split(":")
        return int(channel_id), int(message_id)
    except ValueError:
        return None


class MonkeySmp(commands.Cog):
    smp = app_commands.Group(name="smp", description="Monkey SMP, our Minecraft server", guild_only=True)

    def __init__(self, bot, environ=None, pinger=None, rcon=None, reader=None):
        self.bot = bot
        self.setup = R.read_setup(config.monkey_environ(environ))
        self.pinger = pinger or ping_server
        if rcon is None and self.setup.rcon_on:
            rcon = Rcon(*self.setup.rcon, self.setup.password)
        self.rcon = rcon
        self.reader = reader or read_log
        environ = os.environ if environ is None else environ
        self.nudges_on = N.enabled(environ)
        self.join_address = W.read_settings(environ).address  # the public address players type
        # Everything below is working state that may reset on restart; what must last
        # (links, playtime, the panel's message id) is in SQLite.
        self.next_poll_at = 0
        self.failures = 0
        self.first_failure_at: int | None = None
        self.down_since: int | None = None
        self.status: R.Status | None = None
        self.names: list[str] = []
        self.last_names: set[str] | None = None  # lower-case names in the last good poll
        self.last_names_at: int | None = None
        self.rcon_down_since: int | None = None
        self.rcon_refused = False
        self.rcon_retry_at = 0
        self.last_signature: tuple | None = None
        self.last_sweep_at = 0
        self.tail = R.TailState()
        self.splitter = R.LineSplitter()
        self.batcher = R.Batcher()
        self.log_missing = False
        self.limiter = R.RateLimit()
        self.noticed: dict[tuple[int, str], int] = {}
        self.last_link: dict[int, int] = {}
        self.fetched: dict[int, tuple[int, object | None, int]] = {}  # uid -> (asked at, member, good for)
        self.hinted: dict[str, int] = {}  # name key -> when "link to be heard here" was last said for it
        self.said: set[str] = set()  # log lines already written once (see `once`)
        self.last_nudge_at = 0
        self.blind_at: int | None = None  # last poll whose player list may have left someone out

    async def cog_load(self) -> None:
        for problem in self.setup.problems:
            log.warning("monkey: %s", problem)
        if not self.setup.any_on:
            log.info("monkey: no MONKEY_* settings, the bridge is off")
        else:
            log.info("monkey: on (%s)", ", ".join(self.setup.parts()))
        if self.nudges_on:
            log.info("monkey: nudges on (%s=on)", N.ENV_ON)
        try:
            await self.forget_opted_out()
        except Exception:
            log.exception("monkey: clearing opted-out playtime failed")
        try:
            await self.settle_old_links()
        except Exception:
            log.exception("monkey: tidying links left from the whitelist days failed")
        if self.setup.status_on or self.setup.rcon_on:
            self.poll_loop.start()
        if self.setup.log_on:
            self.relay_loop.start()

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()
        self.relay_loop.cancel()
        if self.rcon is not None:
            await self.rcon.close()

    # ------------------------------------------------------------ small helpers
    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    def channel(self):
        guild = self.guild()
        if guild is None:
            return None
        return config.match_by_names(guild.text_channels, (config.MONKEY_CHANNEL, *config.MONKEY_CHANNEL_WAS))

    def once(self, key: str, level: int, text: str, *args) -> None:
        """Log a condition when it starts, not every time it is seen again."""
        if key not in self.said:
            self.said.add(key)
            log.log(level, text, *args, exc_info=level >= logging.ERROR)

    async def meta_get(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def meta_set(self, key: str, value: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    async def mod_log(self, line: str) -> None:
        guild = self.guild()
        channel = config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL) if guild is not None else None
        if channel is None:
            return
        try:
            await channel.send(embed=style.embed(description=line), allowed_mentions=NO_PINGS)
        except Exception:
            log.warning("monkey: couldn't write to the mod log", exc_info=True)

    def help_ref(self) -> str:
        """The help channel as a mention, or its plain name when it can't be found."""
        guild = self.guild()
        channel = config.match_by_name(guild.text_channels, config.HELP_CHANNEL) if guild is not None else None
        return channel.mention if channel is not None else "#help"

    def access_text(self) -> str:
        return ACCESS.format(help=self.help_ref())

    def health_text(self) -> str:
        """The self-check line for /status: "off", "ok (...)" or "unreachable since ...".
        Reads cached state only, so it is safe to call from any cog at any time."""
        return R.health(self.setup, down_since=self.down_since, rcon_down_since=self.rcon_down_since,
                        rcon_refused=self.rcon_refused, log_missing=self.log_missing)

    # ------------------------------------------------------------ RCON
    async def rcon_call(self, command: str) -> str:
        """Run a command, keeping track of whether RCON is answering. Raises RconError.
        Only `list` and `tellraw` get past here (`R.sendable`): never a whitelist change."""
        if self.rcon is None or not self.setup.rcon_on:
            raise RconError("RCON is not set up")
        if not R.sendable(command):
            raise RconError("that command is not one this bot sends")
        return await self._rcon_send(command)

    async def grant_call(self, command: str) -> str:
        """The referral unlock (cogs/referral.py, only with REFERRAL_GRANT=rcon): a wardrobe score on one
        player's name. Only the shapes `R.grant_sendable` allows get past here. Raises RconError."""
        if self.rcon is None or not self.setup.rcon_on:
            raise RconError("RCON is not set up")
        if not R.grant_sendable(command):
            raise RconError("that command is not one this bot sends")
        return await self._rcon_send(command)

    async def _rcon_send(self, command: str) -> str:
        try:
            reply = await self.rcon.command(command)
        except RconError as exc:
            self.rcon_refused = isinstance(exc, RconRefused)
            self.rcon_down_since = self.rcon_down_since or now()
            self.rcon_retry_at = now() + R.RCON_RETRY
            self.once("rcon", logging.WARNING, "monkey: RCON is not answering: %s", exc)
            raise
        if self.rcon_down_since is not None:
            log.info("monkey: RCON is answering again")
        self.rcon_down_since, self.rcon_refused = None, False
        self.said.discard("rcon")
        return reply

    # ------------------------------------------------------------ polling
    async def poll_once(self) -> None:
        """Ask the server how it is and act on the answer: playtime, members who left,
        the panel."""
        t = now()
        self.next_poll_at = t + R.POLL_EVERY  # replaced below; set first so a bug cannot spin
        status, names, reached = None, None, False
        if self.setup.status_on:
            try:
                status = R.read_status(await self.pinger(*self.setup.address))
                reached = True
            except PingError as exc:
                self.once("ping", logging.INFO, "monkey: the server did not answer a status ping (%s)", exc)
        if self.setup.rcon_on and (reached or not self.setup.status_on):
            try:
                names = R.parse_list(await self.rcon_call(R.LIST_COMMAND))
                reached = True
            except RconError:
                pass
        complete = names is not None
        if names is None and status is not None:
            names = list(status.names)  # the ping's sample: complete on a small server
            complete = len(names) >= status.online

        if reached:
            if self.down_since is not None:
                log.info("monkey: the server is back")
            self.failures, self.first_failure_at, self.down_since = 0, None, None
            self.said.discard("ping")
            self.status, self.names = status, names or []
        else:
            self.failures += 1
            self.first_failure_at = self.first_failure_at or t
            if self.failures >= R.OFFLINE_AFTER and self.down_since is None:
                self.down_since = self.first_failure_at
                self.status, self.names = None, []
                log.info("monkey: the server is not answering; polling less often until it does")
        self.next_poll_at = t + R.poll_delay(self.failures)

        steps = [("playtime", self.credit_playtime(names if reached else None, t))]
        if self.nudges_on and reached:
            steps.append(("last seen", self.record_seen(names or [], complete, t)))
            if t - self.last_nudge_at >= N.CHECK_EVERY:
                self.last_nudge_at = t
                steps.append(("nudges", self.send_nudges(t)))
        if t - self.last_sweep_at >= R.SWEEP_EVERY:
            self.last_sweep_at = t
            steps.append(("member sweep", self.sweep_departed()))
        if self.setup.status_on and (reached or self.down_since is not None):
            steps.append(("panel", self.refresh_panel()))
        for what, step in steps:
            try:
                await step
            except Exception:
                log.exception("monkey: %s failed", what)

    @tasks.loop(seconds=R.TICK)
    async def poll_loop(self) -> None:
        try:
            if now() >= self.next_poll_at:
                await self.poll_once()
        except Exception:
            log.exception("monkey: poll failed")

    @poll_loop.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ status panel
    async def refresh_panel(self) -> str:
        """Post the panel or edit it in place. Returns missing / skipped / edited / posted / failed."""
        channel = self.channel()
        if channel is None:
            self.once("channel", logging.INFO, "monkey: no %s channel yet (server/public_mode.py creates it)",
                      config.MONKEY_CHANNEL)
            return "missing"
        signature = R.panel_signature(self.status, self.names, self.down_since)
        ref = parse_ref(await self.meta_get(PANEL_KEY))
        here = ref is not None and ref[0] == channel.id
        if here and signature == self.last_signature:
            return "skipped"
        text = R.panel_text(self.status, self.names, self.down_since)
        text += (f"\n\nWant to play? You need a Monkey SMP pass: {R.PASS_URL}, or open a ticket in "
                 f"{self.help_ref()}. `/smp link` connects your Minecraft name to chat and playtime here.")
        embed = style.embed(title="Monkey SMP", description=text[:4000], footer=style.label("monkey smp", "minecraft"),
                            color=style.FOREST if self.status is not None else style.MUTED)
        if here:
            try:
                await channel.get_partial_message(ref[1]).edit(embed=embed, allowed_mentions=NO_PINGS)
                self.last_signature = signature
                self.said.discard("panel")
                return "edited"
            except discord.NotFound:
                pass  # deleted: post a fresh one
            except discord.HTTPException:
                self.once("panel", logging.WARNING, "monkey: editing the status panel failed")
                return "failed"
        try:
            sent = await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            self.once("panel", logging.WARNING, "monkey: posting the status panel failed")
            return "failed"
        await self.meta_set(PANEL_KEY, f"{channel.id}:{sent.id}")
        self.last_signature = signature
        self.said.discard("panel")
        try:
            await sent.pin(reason="Monkey SMP status panel")  # chat shares the channel
        except Exception:
            log.info("monkey: couldn't pin the status panel")
        return "posted"

    # ------------------------------------------------------------ playtime and coins
    async def forget_opted_out(self) -> int:
        """Playtime is activity tracking: rows of members who turned tracking off go, and so
        does their nudge row (it holds when they were last seen)."""
        await self.db.execute("DELETE FROM monkey_nudges WHERE user_id IN (SELECT user_id FROM privacy_optout)")
        return await self.db.execute("DELETE FROM monkey_playtime WHERE user_id IN (SELECT user_id FROM privacy_optout)")

    async def credit_playtime(self, names, t: int) -> int:
        """Credit the time since the last good poll to linked members seen in both, and pay
        each newly completed half hour. `names` is None for a failed poll, which breaks the
        chain. Returns the coins paid."""
        previous, previous_at = self.last_names, self.last_names_at
        if names is None:
            self.last_names, self.last_names_at = None, None
            return 0
        current = {R.name_key(n) for n in names}
        self.last_names, self.last_names_at = current, t
        await self.forget_opted_out()
        seconds = R.credit_seconds(previous_at, t)
        if previous is None or not seconds:
            return 0
        present = current & previous
        if len(present) < R.MIN_PLAYERS:  # alone (or the company only just arrived): nothing counts
            return 0
        guild = self.guild()
        day = R.day_key(t, self.tz)
        paid = 0
        for row in await self.db.fetchall("SELECT user_id, name_key FROM monkey_links WHERE state = 'ok'"):
            uid = row["user_id"]
            if row["name_key"] not in present or guild is None or guild.get_member(uid) is None:
                continue
            async with self.db.transaction() as tx:
                # The opt-out check is part of the write (same reason as cogs/stats.py).
                if await tx.fetchone("SELECT 1 FROM privacy_optout WHERE user_id = ?", (uid,)) is not None:
                    continue
                old = await tx.fetchone("SELECT seconds FROM monkey_playtime WHERE user_id = ? AND day = ?", (uid, day))
                before = old["seconds"] if old else 0
                after = before + seconds
                await tx.execute("INSERT INTO monkey_playtime (user_id, day, seconds) VALUES (?, ?, ?)"
                                 " ON CONFLICT (user_id, day) DO UPDATE SET seconds = excluded.seconds",
                                 (uid, day, after))
                if not R.established(uid, t):
                    continue
                for slot in range(R.slots(before), R.slots(after)):
                    result = await economy.apply_tx(tx, uid, R.SLOT_COINS, R.REASON, t, ref=R.ref(uid, day, slot))
                    paid += R.SLOT_COINS if result.ok else 0
        return paid

    # ------------------------------------------------------------ links
    async def settle_old_links(self) -> int:
        """Rows left from when linking ran the whitelist: a removal that was still waiting is
        forgotten, and an add that was still waiting is a plain link like any other. Nothing
        is sent to the server. Returns the rows changed (0 once done; safe on every load)."""
        async with self.db.transaction() as tx:
            gone = await tx.execute("DELETE FROM monkey_links WHERE state = 'remove'")
            kept = await tx.execute("UPDATE monkey_links SET state = 'ok', attempts = 0 WHERE state != 'ok'")
            changed = max(gone.rowcount, 0) + max(kept.rowcount, 0)
        if changed:
            log.info("monkey: tidied %s link row(s) left from the whitelist days", changed)
        return changed

    async def forget_link(self, user_id: int) -> dict | None:
        """Delete the member's link and return it; None when they had none. That is all:
        their access to Monkey SMP is staff's business, not the bot's."""
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM monkey_links WHERE user_id = ?", (user_id,))
            if row is None:
                return None
            await tx.execute("DELETE FROM monkey_links WHERE user_id = ?", (user_id,))
            await tx.execute("DELETE FROM monkey_nudges WHERE user_id = ?", (user_id,))
        return dict(row)

    async def drop_member(self, user_id: int, why: str) -> bool:
        """Forget a member's link (they left, were banned, or staff said so). True when they
        had one."""
        row = await self.forget_link(user_id)
        if row is None:
            return False
        await self.mod_log(f"<@{user_id}> {why}: forgot their Minecraft link `{row['name']}` "
                           "(their Monkey SMP access is unchanged)")
        return True

    async def sweep_departed(self) -> int:
        """Links whose member is gone (they left while the bot was off). Only with a
        complete member list, so a cold cache can never unlink everyone."""
        guild = self.guild()
        if guild is None or not getattr(guild, "chunked", False):
            return 0
        gone = 0
        for row in await self.db.fetchall("SELECT user_id FROM monkey_links"):
            if guild.get_member(row["user_id"]) is None:
                gone += await self.drop_member(row["user_id"], "is no longer here")
        return gone

    @commands.Cog.listener()
    async def on_member_remove(self, member) -> None:
        try:
            if member.guild.id == self.bot.settings.guild_id:
                await self.drop_member(member.id, "left")
        except Exception:
            log.exception("monkey: handling a leave failed")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user) -> None:
        try:
            if guild.id == self.bot.settings.guild_id:
                await self.drop_member(user.id, "was banned")
        except Exception:
            log.exception("monkey: handling a ban failed")

    async def save_link(self, member_id: int, name: str) -> str | None:
        """Record `name` as the member's. None when it was saved; otherwise why not:
        "already" (this name is theirs), "have_one" (they linked another) or "taken"."""
        key, t = R.name_key(name), now()
        async with self.db.transaction() as tx:
            mine = await tx.fetchone("SELECT name_key FROM monkey_links WHERE user_id = ?", (member_id,))
            if mine is not None:
                return "already" if mine["name_key"] == key else "have_one"
            if await tx.fetchone("SELECT 1 FROM monkey_links WHERE name_key = ?", (key,)) is not None:
                return "taken"
            await tx.execute("INSERT INTO monkey_links (name_key, name, user_id, state, attempts, linked_at,"
                             " updated_at) VALUES (?, ?, ?, 'ok', 0, ?, ?)", (key, name, member_id, t, t))
        return None

    async def linked_name(self, user_id: int) -> str | None:
        row = await self.db.fetchone("SELECT name FROM monkey_links WHERE user_id = ?", (user_id,))
        return row["name"] if row is not None else None

    async def link_member(self, member, name: str) -> str:
        """/smp link: returns the reply. The name is never echoed unless it is valid."""
        if not self.setup.any_on:
            return MESSAGES["off"]
        if not R.valid_name(name):
            return MESSAGES["bad_name"]
        t = now()
        # A link decides whose in-game words are posted here and whose playtime it is, so a
        # throwaway account can't claim a name (the same bar as coins and other rewards).
        if not R.established(member.id, t):
            return MESSAGES["young"]
        last = self.last_link.get(member.id)
        if last is not None and t - last < R.LINK_COOLDOWN:
            return MESSAGES["cooldown"]
        self.last_link = {u: at for u, at in self.last_link.items() if t - at < R.LINK_COOLDOWN}
        self.last_link[member.id] = t
        problem = await self.save_link(member.id, name)
        if problem is not None:
            return MESSAGES[problem].format(name=await self.linked_name(member.id) or name, help=self.help_ref())
        await self.mod_log(f"<@{member.id}> linked the Minecraft name `{name}`")
        return MESSAGES["linked"].format(name=name, access=self.access_text()) + (NUDGE_HINT if self.nudges_on else "")

    async def adopt_member(self, mod, member, name: str) -> str:
        """/smp adopt: staff link a name for a member, without the account-age bar or the
        cooldown (someone who can't run `/smp link` yet, or a name sorted out in a ticket).
        Link-only, like `/smp link`: it gives no access to Monkey SMP. Returns the reply."""
        if not self.setup.any_on:
            return MESSAGES["off"]
        if not R.valid_name(name):
            return MESSAGES["bad_name"]
        if getattr(member, "bot", False):
            return "Bots don't play on Monkey SMP."
        problem = await self.save_link(member.id, name)
        if problem == "have_one":
            return f"{member.mention} already has `{await self.linked_name(member.id)}` linked. `/smp remove` it first."
        if problem == "taken":
            row = await self.db.fetchone("SELECT user_id FROM monkey_links WHERE name_key = ?", (R.name_key(name),))
            return f"`{name}` is linked to <@{row['user_id']}>. `/smp remove` that link first."
        if problem == "already":
            return f"`{name}` is already linked to {member.mention}."
        log.info("monkey: %s linked a name for %s", mod.id, member.id)
        await self.mod_log(f"<@{mod.id}> linked the Minecraft name `{name}` to <@{member.id}> (`/smp adopt`)")
        return (f"Linked `{name}` to {member.mention}. A link gives no access to Monkey SMP; that is a pass, "
                "granted separately.")

    # ------------------------------------------------------------ nudges (logic/smpnudge.py)
    async def record_seen(self, names, complete: bool, t: int) -> int:
        """Stamp the members in this poll's player list as seen now: only members who opted in to
        nudges, are linked, and haven't turned tracking off. A list that may be missing names (a
        status sample shorter than the count) holds nudges back for N.AWAY. Returns rows stamped."""
        if not complete:
            self.blind_at = t
        keys = sorted({R.name_key(n) for n in names})
        if not keys:
            return 0
        marks = ", ".join("?" * len(keys))
        return await self.db.execute(
            "UPDATE monkey_nudges SET last_seen_at = ? WHERE user_id IN"
            f" (SELECT user_id FROM monkey_links WHERE state = 'ok' AND name_key IN ({marks}))"
            " AND user_id NOT IN (SELECT user_id FROM privacy_optout)", (t, *keys))

    async def nudge_candidates(self) -> list[N.Candidate]:
        rows = await self.db.fetchall(
            "SELECT n.user_id, n.last_seen_at, n.nudged_for, n.dm_closed_at,"
            " l.user_id IS NOT NULL AS linked, p.user_id IS NULL AS tracking FROM monkey_nudges n"
            " LEFT JOIN monkey_links l ON l.user_id = n.user_id AND l.state = 'ok'"
            " LEFT JOIN privacy_optout p ON p.user_id = n.user_id")
        return [N.Candidate(r["user_id"], r["last_seen_at"], r["nudged_for"], r["dm_closed_at"],
                            bool(r["linked"]), bool(r["tracking"])) for r in rows]

    async def nudge_digest(self, user_id: int, since: int, t: int) -> N.Digest:
        """What happened on Monkey SMP since `since`, from what the bot already keeps: other members'
        playtime (counts only, no names), approved factions, Base of the Month, upcoming events."""
        guild = self.guild()
        played = await self.db.fetchone(
            "SELECT COUNT(DISTINCT user_id) AS players, COALESCE(SUM(seconds), 0) AS seconds FROM monkey_playtime"
            " WHERE day > ? AND user_id != ? AND user_id NOT IN (SELECT user_id FROM privacy_optout)",
            (R.day_key(since, self.tz), user_id))
        factions = await self.db.fetchall(
            "SELECT name FROM factions WHERE status = 'approved' AND decided_at > ? ORDER BY decided_at, id",
            (since,))
        botm = B.load(await self.db.meta_get(B.STATE_KEY))
        seen_month = B.month_key(datetime.fromtimestamp(since, self.tz))
        events = []
        for event in getattr(guild, "scheduled_events", None) or []:
            start = getattr(event, "start_time", None)
            if start is not None and getattr(event, "status", None) == discord.EventStatus.scheduled:
                events.append((esc(event.name), int(start.timestamp())))
        channel = self.channel()
        return N.Digest(
            away_days=max((t - since) // N.DAY, 1),
            players=played["players"], hours=played["seconds"] // 3600,
            factions=tuple(esc(r["name"]) for r in factions[:N.SHOWN]), faction_count=len(factions),
            botm_month=N.month_label(botm.month) if botm.winners and botm.month > seen_month else None,
            events=tuple(N.smp_events(events, t)),
            online_now=self.status.online if self.status is not None else len(self.names),
            address=self.join_address,
            channel=channel.mention if channel is not None else f"#{config.MONKEY_CHANNEL}")

    async def send_nudges(self, t: int) -> int:
        """One pass: DM the members whose nudge is due (logic/smpnudge.py). Returns DMs sent."""
        if is_quiet(t, self.tz, N.QUIET) or self.down_since is not None:
            return 0
        if self.blind_at is not None and t - self.blind_at < N.AWAY:
            self.once("nudge-blind", logging.INFO, "monkey: nudges wait: the player list may be incomplete "
                      "(no RCON, and a status sample shorter than the count)")
            return 0
        guild = self.guild()
        if guild is None:
            return 0
        sent = 0
        for c in N.pick(await self.nudge_candidates(), t):
            member = guild.get_member(c.user_id) or await self.fetched_member(guild, c.user_id)
            if member is None:
                continue
            # Claim the lapse first, so a crash mid-send can never mean a second DM for it.
            claimed = await self.db.execute(
                "UPDATE monkey_nudges SET nudged_for = last_seen_at, nudged_at = ? WHERE user_id = ?"
                " AND last_seen_at = ? AND dm_closed_at IS NULL", (t, c.user_id, c.last_seen_at))
            if not claimed:
                continue
            try:
                text = N.message(await self.nudge_digest(c.user_id, c.last_seen_at, t))
                await member.send(text[:2000], allowed_mentions=NO_PINGS)
            except discord.Forbidden:
                # DMs closed (or the bot is blocked): record it and stop trying.
                await self.db.execute("UPDATE monkey_nudges SET dm_closed_at = ? WHERE user_id = ?", (t, c.user_id))
                continue
            except Exception:
                # Not their doing (Discord had a moment): give the lapse back for the next pass.
                await self.db.execute("UPDATE monkey_nudges SET nudged_for = ?, nudged_at = NULL WHERE user_id = ?",
                                      (c.nudged_for, c.user_id))
                log.warning("monkey: a nudge DM failed; trying again next pass", exc_info=True)
                continue
            sent += 1
        if sent:
            log.info("monkey: sent %s nudge(s)", sent)
        return sent

    async def set_nudges(self, member, on: bool) -> str:
        """/smp nudges on|off: returns the reply."""
        if not self.nudges_on:
            return MESSAGES["nudges_off_here"]
        if not on:
            gone = await self.db.execute("DELETE FROM monkey_nudges WHERE user_id = ?", (member.id,))
            return MESSAGES["nudges_off"] if gone else MESSAGES["nudges_were_off"]
        if not await self.db.tracking_allowed(member.id):
            return MESSAGES["nudges_privacy"]
        if await self.linked_name(member.id) is None:
            return MESSAGES["nudges_link"]
        t = now()
        last = await self.db.fetchone("SELECT MAX(day) AS day FROM monkey_playtime WHERE user_id = ?", (member.id,))
        seen, nudged = N.opt_in_state(last["day"] if last else None, t, self.tz)
        # Turning it on again keeps what is known and clears a DM refusal (they may have opened DMs).
        await self.db.execute(
            "INSERT INTO monkey_nudges (user_id, opted_at, last_seen_at, nudged_for) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET dm_closed_at = NULL", (member.id, t, seen, nudged))
        return MESSAGES["nudges_on"]

    # ------------------------------------------------------------ commands
    @smp.command(name="link", description="Connect your Minecraft name for Monkey SMP chat and playtime")
    @app_commands.describe(minecraft_name="Your Minecraft Java name (3 to 16 letters, digits or _)")
    async def link(self, interaction: discord.Interaction, minecraft_name: app_commands.Range[str, 1, 32]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self.link_member(interaction.user, minecraft_name)
        except Exception:
            log.exception("monkey: link failed")
            text = MESSAGES["failed"]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    @smp.command(name="unlink", description="Forget your linked Minecraft name (your server access stays)")
    async def unlink(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self.forget_link(interaction.user.id)
            if row is None:
                text = MESSAGES["none"]
            else:
                text = MESSAGES["unlinked"].format(name=row["name"])
                await self.mod_log(f"<@{interaction.user.id}> unlinked the Minecraft name `{row['name']}`")
        except Exception:
            log.exception("monkey: unlink failed")
            text = MESSAGES["failed"]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    @smp.command(name="remove", description="Forget a member's linked Minecraft name (mods)")
    @app_commands.describe(member="Whose link to remove")
    async def remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only mods can remove someone else's link.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        had = await self.drop_member(member.id, f"was unlinked by <@{interaction.user.id}>")
        text = f"Removed {member.mention}'s Minecraft link." if had else f"{member.mention} has no Minecraft link."
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    @smp.command(name="adopt", description="Link a Minecraft name to a member (mods)")
    @app_commands.describe(member="Whose Minecraft name it is", minecraft_name="Their Minecraft Java name")
    async def adopt(self, interaction: discord.Interaction, member: discord.Member,
                    minecraft_name: app_commands.Range[str, 1, 32]) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only mods can link a name for someone else.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self.adopt_member(interaction.user, member, minecraft_name)
        except Exception:
            log.exception("monkey: adopt failed")
            text = MESSAGES["failed"]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    async def played(self, user_id: int, since_day: str) -> int:
        row = await self.db.fetchone("SELECT COALESCE(SUM(seconds), 0) AS n FROM monkey_playtime"
                                     " WHERE user_id = ? AND day >= ?", (user_id, since_day))
        return row["n"]

    @smp.command(name="status", description="Is Monkey SMP up, who is on, and your own link and playtime")
    async def status_command(self, interaction: discord.Interaction) -> None:
        if not self.setup.any_on:
            await interaction.response.send_message(MESSAGES["off"], ephemeral=True)
            return
        uid, t = interaction.user.id, now()
        if not self.setup.status_on:
            lines = ["The status check isn't set up."]
        elif self.status is None and self.down_since is None:
            lines = ["Checking the server. Ask again in a minute."]
        else:
            lines = [R.panel_text(self.status, self.names, self.down_since)]
        name = await self.linked_name(uid)
        if name is None:
            lines.append("\n**You**: no Minecraft name linked. `/smp link` connects one; only linked players' "
                         "chat is passed on from the game to this channel.")
        else:
            lines.append(f"\n**You**: `{name}`")
            if await self.db.tracking_allowed(uid):
                today = R.day_key(t, self.tz)
                start, end = coin_rules.day_bounds(t, self.tz)
                earned = await self.db.fetchone(
                    "SELECT COALESCE(SUM(delta), 0) AS n FROM ledger WHERE user_id = ? AND reason = ? AND at >= ?"
                    " AND at < ?", (uid, R.REASON, start, end))
                lines.append(f"Played today {R.fmt_duration(await self.played(uid, today))}, this week "
                             f"{R.fmt_duration(await self.played(uid, R.week_start(t, self.tz).isoformat()))}. "
                             f"Coins today: {earned['n']} of {R.SLOT_COINS * R.DAILY_SLOTS} "
                             f"({R.SLOT_COINS} per half hour). Time counts while someone else is on too.")
            else:
                lines.append("Playtime isn't counted for you (`/privacy`).")
        if is_staff(interaction.user):
            lines.append(f"\n**{HEALTH_NAME}**: {self.health_text()}")
        embed = style.embed(title="Monkey SMP", description="\n".join(lines)[:4000],
                            footer=style.label("monkey-smp", "status"))
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @smp.command(name="top", description="Who played the most on Monkey SMP this week")
    async def top(self, interaction: discord.Interaction) -> None:
        if not self.setup.any_on:
            await interaction.response.send_message(MESSAGES["off"], ephemeral=True)
            return
        guild = interaction.guild or self.guild()
        since = R.week_start(now(), self.tz).isoformat()
        rows = await self.db.fetchall(
            "SELECT user_id, SUM(seconds) AS n FROM monkey_playtime WHERE day >= ?"
            " AND user_id NOT IN (SELECT user_id FROM privacy_optout) GROUP BY user_id HAVING n > 0"
            " ORDER BY n DESC, user_id LIMIT 50", (since,))
        here = [r for r in rows if guild is not None and guild.get_member(r["user_id"]) is not None][:R.TOP_SIZE]
        text = "\n".join(f"`{i:>2}` <@{r['user_id']}> · {R.fmt_duration(r['n'])}" for i, r in enumerate(here, 1))
        embed = style.embed(title="Monkey SMP · playtime this week",
                            description=text or "Nobody has played this week yet.",
                            footer=style.label("monkey-smp", "top"))
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

    @smp.command(name="nudges", description="Get one DM about what you missed if you're off Monkey SMP for a week")
    @app_commands.describe(choice="on: one DM after a week away; off: none")
    @app_commands.choices(choice=NUDGE_CHOICES)
    async def nudges(self, interaction: discord.Interaction, choice: app_commands.Choice[str]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self.set_nudges(interaction.user, choice.value == "on")
        except Exception:
            log.exception("monkey: nudges setting failed")
            text = MESSAGES["failed"]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ Discord -> Minecraft
    async def notice(self, message, emoji: str) -> None:
        """Tell a member their message was not relayed, with a reaction, at most once a
        minute per member and reason (a reaction is an API call too)."""
        t = now()
        key = (message.author.id, emoji)
        if t - self.noticed.get(key, -R.NOTICE_COOLDOWN) < R.NOTICE_COOLDOWN:
            return
        self.noticed = {k: at for k, at in self.noticed.items() if t - at < R.NOTICE_COOLDOWN}
        self.noticed[key] = t
        try:
            await message.add_reaction(emoji)
        except Exception:
            pass

    async def relay_to_minecraft(self, message) -> str:
        """Relay one Discord message. Returns what happened (for tests and reading logs)."""
        if not self.setup.rcon_on:
            return "off"
        author = message.author
        if getattr(author, "bot", False) or getattr(message, "webhook_id", None) is not None:
            return "bot"  # includes this bot's own relay of Minecraft chat: no loops
        guild = getattr(message, "guild", None)
        if guild is None or guild.id != self.bot.settings.guild_id:
            return "elsewhere"
        bridge = self.channel()  # the one channel the bridge uses, by ID: a same-named channel or thread doesn't count
        if bridge is None or getattr(message.channel, "id", None) != bridge.id:
            return "elsewhere"
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return "system"
        text = R.discord_text(getattr(message, "clean_content", None) or message.content,
                              len(getattr(message, "attachments", []) or []))
        if not text:
            return "empty"
        t = now()
        if not self.limiter.allow(author.id, t):
            await self.notice(message, SLOW_DOWN)
            return "limited"
        if self.rcon_down_since is not None and t < self.rcon_retry_at:
            await self.notice(message, NOT_SENT)
            return "down"
        try:
            await self.rcon_call(R.tellraw_command(getattr(author, "display_name", None) or "someone", text))
        except RconError:
            await self.notice(message, NOT_SENT)
            return "down"
        return "sent"

    @commands.Cog.listener()
    async def on_message(self, message) -> None:
        try:
            await self.relay_to_minecraft(message)
        except Exception:
            self.once("relay", logging.ERROR, "monkey: relaying a message to Minecraft failed")

    # ------------------------------------------------------------ Minecraft -> Discord
    async def speaker(self, name: str) -> int | None:
        """The member a Minecraft name speaks for: the member who linked it. A name nobody
        linked is None, and None is never relayed. (Only 'ok' rows count: anything else is a
        leftover from the whitelist days that `settle_old_links` has not tidied yet.)"""
        row = await self.db.fetchone(
            "SELECT user_id FROM monkey_links WHERE name_key = ? AND state = 'ok'", (R.name_key(name),))
        return row["user_id"] if row is not None else None

    async def may_relay(self, name: str) -> bool:
        """The allow-list in front of everything a player can put words into: relayed only
        when the name is linked to a member who is in the server right now, not
        timed out, and able to send messages in the Monkey SMP channel. Everything else stays in
        the game, so a second or unlinked Minecraft account is no way around a mute, and
        claiming somebody's name changes nothing for them."""
        user_id = await self.speaker(name)
        return user_id is not None and await self.may_speak(user_id)

    async def may_speak(self, user_id: int) -> bool:
        """Is this member in the server and allowed to send messages in the Monkey SMP channel?
        (discord.py's permissions_for takes Send Messages from a timed-out member, and covers
        channel overwrites and a muted role too.) Anything that can't be confirmed is a no."""
        try:
            guild, channel = self.guild(), self.channel()
            if guild is None or channel is None:
                return False
            # The gateway keeps cached members current, timeouts included: when it has the
            # member, that is the answer. Otherwise the API is asked.
            member = guild.get_member(user_id) or await self.fetched_member(guild, user_id)
            if member is None or member.is_timed_out():
                return False
            return bool(channel.permissions_for(member).send_messages)
        except Exception:
            log.debug("monkey: couldn't work out what %s may do in the Monkey SMP channel", user_id, exc_info=True)
            return False

    async def fetched_member(self, guild, user_id: int):
        """A member the cache doesn't have, from the API, so a chatty player doesn't cost a
        request per line. "Not a member" is remembered for R.MEMBER_GONE_FOR. A member that
        was found is reused for R.MEMBER_FRESH_FOR at most, then fetched again: a fetched
        object never learns about a timeout given after it was fetched. No answer from
        Discord is not a yes, and is asked again after the same short wait."""
        t = now()
        known = self.fetched.get(user_id)
        if known is not None and 0 <= t - known[0] < known[2]:
            return known[1]
        try:
            member, keep = await guild.fetch_member(user_id), R.MEMBER_FRESH_FOR
        except discord.NotFound:
            member, keep = None, R.MEMBER_GONE_FOR
        except Exception:
            member, keep = None, R.MEMBER_FRESH_FOR  # unknown, not gone: don't sit on it for long
        self.fetched = {u: v for u, v in self.fetched.items() if 0 <= t - v[0] < v[2]}
        self.fetched[user_id] = (t, member, keep)
        return member

    def unlinked_hint(self, names) -> str | None:
        """One bot-worded line, at most once per R.HINT_EVERY per name, saying why some chat
        didn't come through. It carries no name and none of the player's text."""
        t = now()
        self.hinted = {k: at for k, at in self.hinted.items() if 0 <= t - at < R.HINT_EVERY}
        fresh = {R.name_key(n) for n in names} - set(self.hinted)
        if not fresh:
            return None
        self.hinted.update(dict.fromkeys(fresh, t))
        return UNLINKED_HINT

    async def relay_once(self) -> None:
        """Read what the server logged since the last pass and post one batched message."""
        result = await asyncio.to_thread(self.reader, self.setup.log_path, self.tail)
        if result is None:
            if not self.log_missing:
                self.log_missing = True
                log.info("monkey: the server log can't be read right now; chat from Minecraft is waiting")
        else:
            self.log_missing = False
            self.tail, data, reset = result
            if reset:
                self.splitter.reset()
            unlinked: set[str] = set()  # names whose words stayed in the game for want of a link
            for line in self.splitter.feed(data):
                event = R.parse_line(line)
                if event is None:
                    continue
                if event.kind in (R.JOIN, R.LEAVE):
                    self.next_poll_at = min(self.next_poll_at, now() + R.TICK)  # freshen the panel soon
                # Join and leave lines are the bot's own words around a validated name: shown for
                # anyone, so the channel still says who is on. Chat, deaths and advancements
                # carry text a player can shape (what they typed, a renamed item, a modded
                # advancement title): those go through the allow-list.
                if event.kind not in (R.JOIN, R.LEAVE) and not await self.may_relay(event.name):
                    if await self.speaker(event.name) is None:
                        unlinked.add(event.name)
                    continue
                text = R.discord_line(event)
                if text:
                    self.batcher.add(text)
            hint = self.unlinked_hint(unlinked)
            if hint is not None:
                self.batcher.add(hint)  # after this pass's lines: on its own, or the last one
        await self.flush_once()

    async def flush_once(self) -> bool:
        channel = self.channel()
        if channel is None:
            self.batcher = R.Batcher()  # nowhere to post: don't hoard
            return False
        text = self.batcher.take()
        if text is None:
            return False
        try:
            await channel.send(text[:2000], allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            self.once("send", logging.WARNING, "monkey: posting Minecraft chat failed")
            return False
        self.said.discard("send")
        return True

    @tasks.loop(seconds=R.FLUSH_EVERY)
    async def relay_loop(self) -> None:
        try:
            await self.relay_once()
        except Exception:
            self.once("tail", logging.ERROR, "monkey: reading the server log failed")

    @relay_loop.before_loop
    async def before_relay(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(MonkeySmp(bot))
