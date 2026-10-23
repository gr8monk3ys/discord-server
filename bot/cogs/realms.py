"""Monkey SMP: a bridge between this server and the owner's Minecraft server.

Four parts, each on only when its own settings are in the environment (see
logic/realms.py `read_setup`); with none of them the cog loads, starts nothing and every
command says the bridge is not set up.

- Status panel (REALMS_ADDRESS): one message in the realms channel, edited in place when
  what it shows changes. The server is asked with the Server List Ping protocol every two
  minutes; while it does not answer the polls back off and the panel is edited once.
- Whitelist (RCON): `/smp link <name>` for established accounts that have been in the
  server two weeks runs `whitelist add`, `/smp unlink` (or leaving, or a ban) runs
  `whitelist remove`. One name per member, one member per name. A change the server could
  not take yet waits in `realms_links` (state add / remove) and is sent after the next poll
  that reaches the server. The bot only ever removes a name it added itself (`managed`): an
  add answered "already whitelisted" means somebody else put the name there, so the link is
  refused, and a link the server never confirmed is forgotten without a `whitelist remove`.
  Staff link such a name for a member with `/smp adopt`.
- Chat, both ways, in the realms channel. Discord to Minecraft is a `tellraw` over RCON,
  rate limited, never for bots. Minecraft to Discord tails the server log read-only
  (REALMS_LOG_PATH) in a worker thread and posts at most one batched message every few
  seconds, with no pings. An allow-list decides what comes through: a player's words (chat,
  and death and advancement lines, whose text a player or a mod can shape) are relayed only
  when the name has a confirmed, bot-managed link to a member who is in the server, not
  timed out and able to send messages in the realms channel. Everything else stays in the
  game. Join and leave lines are the bot's own words and show for anyone.
- Playtime: a linked member seen in two player-list polls in a row, with at least one other
  player on in both (company, like voice coins: nobody is paid to idle alone), is credited
  the time between them; every full half hour pays a few coins up to a daily cap (refs keyed
  on member, day and slot). `/smp top` ranks the week; tracking opt-out is respected.

This file owns the sockets and the file handle. Nothing here ever starts, stops or
configures the Minecraft server, and the RCON password is never logged or shown: errors
carry fixed text and an exception type name only.
"""

import asyncio
import logging
import os
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy
import style
from cogkit import is_staff
from logic import coins as coin_rules
from logic import quests
from logic import realms as R

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
PANEL_KEY = "realms:panel"  # meta: "channel_id:message_id" of the status panel
HEALTH_NAME = "Monkey SMP bridge"  # field name for /status (see `health_text`)
SLOW_DOWN, NOT_SENT = "⏳", "📴"
# Posted (rarely) where a player's chat would have been; no name, none of their text.
UNLINKED_HINT = "*Some players aren't linked, so their chat stays in the game. `/smp link`*"

MESSAGES = {
    "off": "The Monkey SMP bridge isn't set up yet.",
    "no_whitelist": "Whitelisting through Discord isn't set up yet. Ask a Keeper to add you.",
    "bad_name": "That isn't a Minecraft Java name: 3 to 16 letters, digits or underscores, nothing else.",
    "young": (f"Your Discord account must be {quests.MIN_ACCOUNT_DAYS}+ days old to join "
              "Monkey SMP through Discord. Ask a Keeper to add you."),
    "new_here": (f"Linking opens once you've been in the server for {R.LINK_MEMBER_DAYS} days. "
                 "Until then, ask a Keeper to add you."),
    "claimed": ("`{name}` is already on the Monkey SMP whitelist, and I didn't put it there, so I can't link it. "
                "If it's yours, ask a mod to link it for you."),
    "unlink_unconfirmed": "Unlinked. `{name}` was still waiting to be added, so the whitelist is unchanged.",
    "cooldown": "Give it a moment, then try again.",
    "have_one": "You already linked `{name}`. Run `/smp unlink` first if you want to change it.",
    "taken": "Someone here already linked that Minecraft name. If it's yours, ask a mod.",
    "already": "`{name}` is already linked to you and on the whitelist.",
    "linked": ("Linked. `{name}` is on the Monkey SMP whitelist, and what you say in the game now shows up in the "
               "Monkey SMP channel. `/smp unlink` takes it off again."),
    "pending": ("Saved `{name}`. I can't reach the Minecraft server right now, so you'll be added to the "
                "whitelist as soon as it answers."),
    "unknown": "Minecraft doesn't know a player by that name. Check the spelling and try again.",
    "none": "You haven't linked a Minecraft name. `/smp link` gets you on the whitelist.",
    "unlinked": "Unlinked. `{name}` is off the Monkey SMP whitelist.",
    "unlink_pending": ("Unlinked `{name}`. I can't reach the Minecraft server right now; it comes off the "
                       "whitelist as soon as the server answers."),
    "failed": "Something went wrong on my side. Try again in a minute.",
}


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
    are checked before anything is read (logic/realms.py), each command has one timeout
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


def parse_ref(raw: str | None) -> tuple[int, int] | None:
    try:
        channel_id, message_id = (raw or "").split(":")
        return int(channel_id), int(message_id)
    except ValueError:
        return None


class Realms(commands.Cog):
    realms = app_commands.Group(name="smp", description="Monkey SMP, our Minecraft server", guild_only=True)

    def __init__(self, bot, environ=None, pinger=None, rcon=None, reader=None):
        self.bot = bot
        self.setup = R.read_setup(config.realms_environ(environ))
        self.pinger = pinger or ping_server
        if rcon is None and self.setup.rcon_on:
            rcon = Rcon(*self.setup.rcon, self.setup.password)
        self.rcon = rcon
        self.reader = reader or read_log
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
        self.waiting = 0  # whitelist changes not yet confirmed by the server
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
        self.sync_lock = asyncio.Lock()
        self.said: set[str] = set()  # log lines already written once (see `once`)

    async def cog_load(self) -> None:
        for problem in self.setup.problems:
            log.warning("realms: %s", problem)
        if not self.setup.any_on:
            log.info("realms: no REALMS_* settings, the bridge is off")
        else:
            log.info("realms: on (%s)", ", ".join(self.setup.parts()))
        try:
            await self.forget_opted_out()
        except Exception:
            log.exception("realms: clearing opted-out playtime failed")
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
        for name in (config.REALMS_CHANNEL, *config.REALMS_CHANNEL_WAS):
            found = config.match_by_name(guild.text_channels, name)
            if found is not None:
                return found
        return None

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
            log.warning("realms: couldn't write to the mod log", exc_info=True)

    def health_text(self) -> str:
        """The self-check line for /status: "off", "ok (...)" or "unreachable since ...".
        Reads cached state only, so it is safe to call from any cog at any time."""
        return R.health(self.setup, down_since=self.down_since, rcon_down_since=self.rcon_down_since,
                        rcon_refused=self.rcon_refused, log_missing=self.log_missing, waiting=self.waiting)

    # ------------------------------------------------------------ RCON
    async def rcon_call(self, command: str) -> str:
        """Run a command, keeping track of whether RCON is answering. Raises RconError."""
        if self.rcon is None or not self.setup.rcon_on:
            raise RconError("RCON is not set up")
        try:
            reply = await self.rcon.command(command)
        except RconError as exc:
            self.rcon_refused = isinstance(exc, RconRefused)
            self.rcon_down_since = self.rcon_down_since or now()
            self.rcon_retry_at = now() + R.RCON_RETRY
            self.once("rcon", logging.WARNING, "realms: RCON is not answering: %s", exc)
            raise
        if self.rcon_down_since is not None:
            log.info("realms: RCON is answering again")
        self.rcon_down_since, self.rcon_refused = None, False
        self.said.discard("rcon")
        return reply

    # ------------------------------------------------------------ polling
    async def poll_once(self) -> None:
        """Ask the server how it is and act on the answer: playtime, waiting whitelist
        changes, members who left, the panel."""
        t = now()
        self.next_poll_at = t + R.POLL_EVERY  # replaced below; set first so a bug cannot spin
        status, names, reached = None, None, False
        if self.setup.status_on:
            try:
                status = R.read_status(await self.pinger(*self.setup.address))
                reached = True
            except PingError as exc:
                self.once("ping", logging.INFO, "realms: the server did not answer a status ping (%s)", exc)
        if self.setup.rcon_on and (reached or not self.setup.status_on):
            try:
                names = R.parse_list(await self.rcon_call(R.LIST_COMMAND))
                reached = True
            except RconError:
                pass
        if names is None and status is not None:
            names = list(status.names)  # the ping's sample: complete on a small server

        if reached:
            if self.down_since is not None:
                log.info("realms: the server is back")
            self.failures, self.first_failure_at, self.down_since = 0, None, None
            self.said.discard("ping")
            self.status, self.names = status, names or []
        else:
            self.failures += 1
            self.first_failure_at = self.first_failure_at or t
            if self.failures >= R.OFFLINE_AFTER and self.down_since is None:
                self.down_since = self.first_failure_at
                self.status, self.names = None, []
                log.info("realms: the server is not answering; polling less often until it does")
        self.next_poll_at = t + R.poll_delay(self.failures)

        steps = [("playtime", self.credit_playtime(names if reached else None, t))]
        if reached and self.setup.rcon_on and self.rcon_down_since is None:
            steps.append(("whitelist sync", self.sync_whitelist()))
        if t - self.last_sweep_at >= R.SWEEP_EVERY:
            self.last_sweep_at = t
            steps.append(("member sweep", self.sweep_departed()))
        if self.setup.status_on and (reached or self.down_since is not None):
            steps.append(("panel", self.refresh_panel()))
        for what, step in steps:
            try:
                await step
            except Exception:
                log.exception("realms: %s failed", what)

    @tasks.loop(seconds=R.TICK)
    async def poll_loop(self) -> None:
        try:
            if now() >= self.next_poll_at:
                await self.poll_once()
        except Exception:
            log.exception("realms: poll failed")

    @poll_loop.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ status panel
    async def refresh_panel(self) -> str:
        """Post the panel or edit it in place. Returns missing / skipped / edited / posted / failed."""
        channel = self.channel()
        if channel is None:
            self.once("channel", logging.INFO, "realms: no %s channel yet (server/public_mode.py creates it)",
                      config.REALMS_CHANNEL)
            return "missing"
        signature = R.panel_signature(self.status, self.names, self.down_since)
        ref = parse_ref(await self.meta_get(PANEL_KEY))
        here = ref is not None and ref[0] == channel.id
        if here and signature == self.last_signature:
            return "skipped"
        text = R.panel_text(self.status, self.names, self.down_since)
        if self.setup.rcon_on:
            text += "\n\nWant in? `/smp link` with your Minecraft name."
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
                self.once("panel", logging.WARNING, "realms: editing the status panel failed")
                return "failed"
        try:
            sent = await channel.send(embed=embed, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            self.once("panel", logging.WARNING, "realms: posting the status panel failed")
            return "failed"
        await self.meta_set(PANEL_KEY, f"{channel.id}:{sent.id}")
        self.last_signature = signature
        self.said.discard("panel")
        try:
            await sent.pin(reason="Monkey SMP status panel")  # chat shares the channel
        except Exception:
            log.info("realms: couldn't pin the status panel")
        return "posted"

    # ------------------------------------------------------------ playtime and coins
    async def forget_opted_out(self) -> int:
        """Playtime is activity tracking: rows of members who turned tracking off go."""
        return await self.db.execute("DELETE FROM realms_playtime WHERE user_id IN (SELECT user_id FROM privacy_optout)")

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
        for row in await self.db.fetchall("SELECT user_id, name_key FROM realms_links WHERE state = 'ok'"):
            uid = row["user_id"]
            if row["name_key"] not in present or guild is None or guild.get_member(uid) is None:
                continue
            async with self.db.transaction() as tx:
                # The opt-out check is part of the write (same reason as cogs/stats.py).
                if await tx.fetchone("SELECT 1 FROM privacy_optout WHERE user_id = ?", (uid,)) is not None:
                    continue
                old = await tx.fetchone("SELECT seconds FROM realms_playtime WHERE user_id = ? AND day = ?", (uid, day))
                before = old["seconds"] if old else 0
                after = before + seconds
                await tx.execute("INSERT INTO realms_playtime (user_id, day, seconds) VALUES (?, ?, ?)"
                                 " ON CONFLICT (user_id, day) DO UPDATE SET seconds = excluded.seconds",
                                 (uid, day, after))
                if not R.established(uid, t):
                    continue
                for slot in range(R.slots(before), R.slots(after)):
                    result = await economy.apply_tx(tx, uid, R.SLOT_COINS, R.REASON, t, ref=R.ref(uid, day, slot))
                    paid += R.SLOT_COINS if result.ok else 0
        return paid

    # ------------------------------------------------------------ whitelist
    async def sync_one(self, key: str) -> str:
        """Send one waiting whitelist change. Returns ok / removed / unknown / claimed /
        pending / gone. "claimed": the name was on the whitelist before this bot ever added
        it (by hand, or for somebody else), so the link is refused and the row deleted."""
        async with self.sync_lock:
            row = await self.db.fetchone("SELECT * FROM realms_links WHERE name_key = ?", (key,))
            if row is None:
                return "gone"
            if row["state"] == "ok":
                return "ok"
            action = "add" if row["state"] == "add" else "remove"
            if action == "remove" and not row["managed"]:
                # Never sent for a name this bot did not put there itself.
                await self.db.execute("DELETE FROM realms_links WHERE name_key = ? AND state = 'remove'", (key,))
                return "gone"
            try:
                reply = await self.rcon_call(R.whitelist_command(action, row["name"]))
            except (RconError, ValueError):
                return "pending"
            result = R.whitelist_result(action, reply)
            t = now()
            if result == "error":
                await self.db.execute("UPDATE realms_links SET attempts = attempts + 1, updated_at = ?"
                                      " WHERE name_key = ? AND state = ?", (t, key, row["state"]))
                log.warning("realms: the server gave an unexpected answer to a whitelist %s", action)
                return "pending"
            # "already whitelisted" is fine only for a name this bot added before (it was being
            # removed and got claimed again before the server took the removal).
            if action == "add" and (result == "ok" or (result == "already" and row["managed"])):
                await self.db.execute("UPDATE realms_links SET state = 'ok', managed = 1, attempts = 0, updated_at = ?"
                                      " WHERE name_key = ? AND state = 'add'", (t, key))
                return "ok"
            # an add for a name Minecraft does not know or that somebody else whitelisted, or a
            # finished removal: the row goes
            await self.db.execute("DELETE FROM realms_links WHERE name_key = ? AND state = ?", (key, row["state"]))
            if action == "add" and result == "already":
                await self.mod_log(
                    f"⛏️ <@{row['user_id']}> tried to link `{row['name']}`, which was already on the Monkey SMP "
                    "whitelist (added by hand, or somebody else's). Not linked, and the whitelist is unchanged. "
                    "If the name is theirs, `/smp adopt` links it.")
                return "claimed"
            return "unknown" if action == "add" else "removed"

    async def sync_whitelist(self) -> None:
        """Send waiting changes, oldest first, a few per poll; stop when RCON stops answering."""
        t = now()
        rows = await self.db.fetchall("SELECT name_key, attempts, updated_at FROM realms_links"
                                      " WHERE state IN ('add', 'remove') ORDER BY updated_at, name_key")
        left = len(rows)
        for row in [r for r in rows if R.retry_due(r["attempts"], r["updated_at"], t)][:R.SYNC_BATCH]:
            if await self.sync_one(row["name_key"]) != "pending":
                left -= 1
            if self.rcon_down_since is not None:
                break
        self.waiting = left

    async def mark_remove(self, user_id: int) -> dict | None:
        """Turn the member's link into a waiting removal; None when they have none. A link
        the server never confirmed is just forgotten ("unconfirmed" in the result): as far as
        the bot knows that name is not on the whitelist, or not by its doing."""
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM realms_links WHERE user_id = ? AND state != 'remove'", (user_id,))
            if row is None:
                return None
            if not row["managed"]:
                await tx.execute("DELETE FROM realms_links WHERE name_key = ?", (row["name_key"],))
                return {**dict(row), "unconfirmed": True}
            await tx.execute("UPDATE realms_links SET state = 'remove', attempts = 0, updated_at = ?"
                             " WHERE name_key = ?", (now(), row["name_key"]))
        self.waiting += 1
        return dict(row)

    async def remove_now(self, row: dict) -> bool:
        """Try the removal at once; False when it has to wait for the server."""
        if row.get("unconfirmed"):
            return True  # nothing was ever added: nothing to send
        if not self.setup.rcon_on:
            return False
        done = await self.sync_one(row["name_key"]) in ("removed", "gone")
        if done:
            self.waiting = max(0, self.waiting - 1)
        return done

    async def drop_member(self, user_id: int, why: str) -> bool:
        """Take a member's name off the whitelist (they left, were banned, or staff said
        so). True when they had one."""
        row = await self.mark_remove(user_id)
        if row is None:
            return False
        done = await self.remove_now(row)
        if row.get("unconfirmed"):
            what = "was still waiting to be added, so the whitelist is unchanged"
        else:
            what = ("is off the Monkey SMP whitelist" if done else
                    "comes off the Monkey SMP whitelist when the server answers")
        await self.mod_log(f"⛏️ <@{user_id}> {why}: `{row['name']}` {what}")
        return True

    async def sweep_departed(self) -> int:
        """Links whose member is gone (they left while the bot was off). Only with a
        complete member list, so a cold cache can never unlink everyone."""
        guild = self.guild()
        if guild is None or not getattr(guild, "chunked", False):
            return 0
        gone = 0
        for row in await self.db.fetchall("SELECT user_id FROM realms_links WHERE state != 'remove'"):
            if guild.get_member(row["user_id"]) is None:
                gone += await self.drop_member(row["user_id"], "is no longer here")
        return gone

    @commands.Cog.listener()
    async def on_member_remove(self, member) -> None:
        try:
            if member.guild.id == self.bot.settings.guild_id:
                await self.drop_member(member.id, "left")
        except Exception:
            log.exception("realms: handling a leave failed")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user) -> None:
        try:
            if guild.id == self.bot.settings.guild_id:
                await self.drop_member(user.id, "was banned")
        except Exception:
            log.exception("realms: handling a ban failed")

    async def link_member(self, member, name: str) -> str:
        """/smp link: returns the reply. The name is never echoed unless it is valid."""
        if not self.setup.rcon_on:
            return MESSAGES["no_whitelist" if self.setup.any_on else "off"]
        if not R.valid_name(name):
            return MESSAGES["bad_name"]
        t = now()
        if not R.established(member.id, t):
            return MESSAGES["young"]
        joined = getattr(member, "joined_at", None)
        if not R.member_long_enough(int(joined.timestamp()) if joined is not None else None, t):
            return MESSAGES["new_here"]
        last = self.last_link.get(member.id)
        if last is not None and t - last < R.LINK_COOLDOWN:
            return MESSAGES["cooldown"]
        self.last_link = {u: at for u, at in self.last_link.items() if t - at < R.LINK_COOLDOWN}
        self.last_link[member.id] = t
        key = R.name_key(name)
        async with self.db.transaction() as tx:
            mine = await tx.fetchone("SELECT * FROM realms_links WHERE user_id = ? AND state != 'remove'",
                                     (member.id,))
            if mine is not None and mine["name_key"] != key:
                return MESSAGES["have_one"].format(name=mine["name"])
            if mine is not None and mine["state"] == "ok":
                return MESSAGES["already"].format(name=mine["name"])
            row = await tx.fetchone("SELECT * FROM realms_links WHERE name_key = ?", (key,))
            if row is not None and row["state"] != "remove" and row["user_id"] != member.id:
                return MESSAGES["taken"]
            if row is None:
                await tx.execute("INSERT INTO realms_links (name_key, name, user_id, state, attempts, linked_at,"
                                 " updated_at) VALUES (?, ?, ?, 'add', 0, ?, ?)", (key, name, member.id, t, t))
            elif mine is None:  # a name whose removal was still waiting: it is claimed again
                await tx.execute("UPDATE realms_links SET name = ?, user_id = ?, state = 'add', attempts = 0,"
                                 " linked_at = ?, updated_at = ? WHERE name_key = ?", (name, member.id, t, t, key))
        result = await self.sync_one(key)
        if result == "unknown":
            return MESSAGES["unknown"]
        if result == "claimed":  # sync_one deleted the row and told the mod log
            return MESSAGES["claimed"].format(name=name)
        await self.mod_log(f"⛏️ <@{member.id}> linked the Minecraft name `{name}`"
                           + ("" if result == "ok" else " (waiting for the server)"))
        return MESSAGES["linked" if result == "ok" else "pending"].format(name=name)

    async def adopt_member(self, mod, member, name: str) -> str:
        """/smp adopt: staff vouch that a name already on the whitelist (put there by hand)
        is this member's. The link is recorded as the bot's own, so unlinking, leaving or a
        ban takes the name off like any other. Returns the reply."""
        if not self.setup.rcon_on:
            return MESSAGES["no_whitelist" if self.setup.any_on else "off"]
        if not R.valid_name(name):
            return MESSAGES["bad_name"]
        if getattr(member, "bot", False):
            return "Bots don't play on Monkey SMP."
        key, t = R.name_key(name), now()
        mine = await self.db.fetchone("SELECT name, name_key FROM realms_links WHERE user_id = ? AND state != 'remove'",
                                      (member.id,))
        if mine is not None and mine["name_key"] != key:
            return f"{member.mention} already has `{mine['name']}` linked. `/smp remove` it first."
        row = await self.db.fetchone("SELECT user_id, state FROM realms_links WHERE name_key = ?", (key,))
        if row is not None and row["state"] != "remove" and row["user_id"] != member.id:
            return f"`{name}` is linked to <@{row['user_id']}>. `/smp remove` that link first."
        try:
            # Makes sure the name is on the whitelist; "already whitelisted" is the expected answer.
            result = R.whitelist_result("add", await self.rcon_call(R.whitelist_command("add", name)))
        except RconError:
            return "I can't reach the Minecraft server right now, so nothing was linked. Try again in a minute."
        if result == "unknown":
            return MESSAGES["unknown"]
        if result not in ("ok", "already"):
            return MESSAGES["failed"]
        await self.db.execute(
            "INSERT INTO realms_links (name_key, name, user_id, state, attempts, linked_at, updated_at, managed)"
            " VALUES (?, ?, ?, 'ok', 0, ?, ?, 1) ON CONFLICT (name_key) DO UPDATE SET name = excluded.name,"
            " user_id = excluded.user_id, state = 'ok', attempts = 0, linked_at = excluded.linked_at,"
            " updated_at = excluded.updated_at, managed = 1", (key, name, member.id, t, t))
        log.info("realms: %s adopted a whitelisted name for %s", mod.id, member.id)
        await self.mod_log(f"⛏️ <@{mod.id}> linked the Minecraft name `{name}` to <@{member.id}> (`/smp adopt`: "
                           "it was on the whitelist already, and is now managed like any other link)")
        return f"Linked `{name}` to {member.mention}. It comes off the whitelist if they unlink, leave or are banned."

    # ------------------------------------------------------------ commands
    @realms.command(name="link", description="Get on the Monkey SMP whitelist with your Minecraft name")
    @app_commands.describe(minecraft_name="Your Minecraft Java name (3 to 16 letters, digits or _)")
    async def link(self, interaction: discord.Interaction, minecraft_name: app_commands.Range[str, 1, 32]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self.link_member(interaction.user, minecraft_name)
        except Exception:
            log.exception("realms: link failed")
            text = MESSAGES["failed"]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    @realms.command(name="unlink", description="Take your Minecraft name off the Monkey SMP whitelist")
    async def unlink(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            row = await self.mark_remove(interaction.user.id)
            if row is None:
                text = MESSAGES["none"]
            else:
                done = await self.remove_now(row)
                which = "unlink_unconfirmed" if row.get("unconfirmed") else "unlinked" if done else "unlink_pending"
                text = MESSAGES[which].format(name=row["name"])
                await self.mod_log(f"⛏️ <@{interaction.user.id}> unlinked the Minecraft name `{row['name']}`")
        except Exception:
            log.exception("realms: unlink failed")
            text = MESSAGES["failed"]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    @realms.command(name="remove", description="Take a member's Minecraft name off the whitelist (mods)")
    @app_commands.describe(member="Whose link to remove")
    async def remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only mods can remove someone else's link.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        had = await self.drop_member(member.id, f"was unlinked by <@{interaction.user.id}>")
        text = f"Removed {member.mention}'s Minecraft link." if had else f"{member.mention} has no Minecraft link."
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    @realms.command(name="adopt", description="Link a name that is already on the whitelist to a member (mods)")
    @app_commands.describe(member="Whose Minecraft name it is", minecraft_name="The name as it is on the whitelist")
    async def adopt(self, interaction: discord.Interaction, member: discord.Member,
                    minecraft_name: app_commands.Range[str, 1, 32]) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only mods can link a name for someone else.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await self.adopt_member(interaction.user, member, minecraft_name)
        except Exception:
            log.exception("realms: adopt failed")
            text = MESSAGES["failed"]
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)

    async def played(self, user_id: int, since_day: str) -> int:
        row = await self.db.fetchone("SELECT COALESCE(SUM(seconds), 0) AS n FROM realms_playtime"
                                     " WHERE user_id = ? AND day >= ?", (user_id, since_day))
        return row["n"]

    @realms.command(name="status", description="Is Monkey SMP up, who is on, and your own link and playtime")
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
        row = await self.db.fetchone("SELECT name, state FROM realms_links WHERE user_id = ? AND state != 'remove'",
                                     (uid,))
        if row is None:
            lines.append("\n**You**: no Minecraft name linked. `/smp link` gets you on the whitelist; only "
                         "linked players' chat is passed on from the game to this channel.")
        else:
            wait = "" if row["state"] == "ok" else " (waiting for the server to take the whitelist change)"
            lines.append(f"\n**You**: `{row['name']}`{wait}")
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
                            footer=style.label("realms", "status"))
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PINGS)

    @realms.command(name="top", description="Who played the most on Monkey SMP this week")
    async def top(self, interaction: discord.Interaction) -> None:
        if not self.setup.any_on:
            await interaction.response.send_message(MESSAGES["off"], ephemeral=True)
            return
        guild = interaction.guild or self.guild()
        since = R.week_start(now(), self.tz).isoformat()
        rows = await self.db.fetchall(
            "SELECT user_id, SUM(seconds) AS n FROM realms_playtime WHERE day >= ?"
            " AND user_id NOT IN (SELECT user_id FROM privacy_optout) GROUP BY user_id HAVING n > 0"
            " ORDER BY n DESC, user_id LIMIT 50", (since,))
        here = [r for r in rows if guild is not None and guild.get_member(r["user_id"]) is not None][:R.TOP_SIZE]
        text = "\n".join(f"`{i:>2}` <@{r['user_id']}> · {R.fmt_duration(r['n'])}" for i, r in enumerate(here, 1))
        embed = style.embed(title="Monkey SMP · playtime this week",
                            description=text or "Nobody has played this week yet.",
                            footer=style.label("realms", "top"))
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

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
        names = (config.REALMS_CHANNEL, *config.REALMS_CHANNEL_WAS)
        if config.slug(getattr(message.channel, "name", "")) not in {config.slug(n) for n in names}:
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
            self.once("relay", logging.ERROR, "realms: relaying a message to Minecraft failed")

    # ------------------------------------------------------------ Minecraft -> Discord
    async def speaker(self, name: str) -> int | None:
        """The member a Minecraft name speaks for: only a link the server confirmed and this
        bot manages (state 'ok', managed). A name nobody linked, a claim still waiting to be
        added, a link on its way off the whitelist: all None, and None is never relayed."""
        row = await self.db.fetchone(
            "SELECT user_id FROM realms_links WHERE name_key = ? AND state = 'ok' AND managed = 1", (R.name_key(name),))
        return row["user_id"] if row is not None else None

    async def may_relay(self, name: str) -> bool:
        """The allow-list in front of everything a player can put words into: relayed only
        when the name has a confirmed link to a member who is in the server right now, not
        timed out, and able to send messages in the realms channel. Everything else stays in
        the game, so a second or unlinked Minecraft account is no way around a mute, and
        claiming somebody's name changes nothing for them."""
        user_id = await self.speaker(name)
        return user_id is not None and await self.may_speak(user_id)

    async def may_speak(self, user_id: int) -> bool:
        """Is this member in the server and allowed to send messages in the realms channel?
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
            log.debug("realms: couldn't work out what %s may do in the realms channel", user_id, exc_info=True)
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
                log.info("realms: the server log can't be read right now; chat from Minecraft is waiting")
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
            self.once("send", logging.WARNING, "realms: posting Minecraft chat failed")
            return False
        self.said.discard("send")
        return True

    @tasks.loop(seconds=R.FLUSH_EVERY)
    async def relay_loop(self) -> None:
        try:
            await self.relay_once()
        except Exception:
            self.once("tail", logging.ERROR, "realms: reading the server log failed")

    @relay_loop.before_loop
    async def before_relay(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Realms(bot))
