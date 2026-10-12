"""/catchup [channel] [hours]: a private AI summary of what happened in a public channel.

The member's recent public messages from that one channel (never staff or private
channels, never bots, never members who turned tracking off) are flattened by
logic.catchup and sent to one provider, which writes a few bullets: the Anthropic Messages
API, or a local Ollama server (free, and the messages never leave the owner's machine).
The answer is shown only to the member who asked (ephemeral, no pings, markdown escaped,
foreign links removed). Nothing from the channel or the answer is stored or logged; only
request and token counts go into catchup_usage for the caps.

Provider (server/.env): CATCHUP_PROVIDER = "ollama", "anthropic" or empty. Empty picks
Anthropic if ANTHROPIC_API_KEY is set, else Ollama if its server answers, else the command
says summaries aren't set up yet and does nothing else. OLLAMA_URL (default
http://127.0.0.1:11434) must be localhost or a loopback/private-network IP address or it
is refused; OLLAMA_MODEL names the model, which has to be pulled already.

Controls, the same for both providers: 3 requests per member and 60 per server per
(Pacific) day, one request at a time per member, a ~12,000 character prompt, accounts 30+
days old only. Anthropic: a 20 s timeout and 600 output tokens. Ollama: a 90 s timeout,
500 output tokens and one summary at a time server-wide, so it can't pile up on the PC;
if the server is down or the model is missing the reply is the "not set up" one and a
warning is logged at most once an hour.

Staff: /catchup-usage shows today's requests and tokens, and the provider and model."""

import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, Protocol

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from logic import catchup as C
from logic import community as community_rules
from logic import quests

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
HISTORY_TIMEOUT = 30
DISCORD_LIMIT = 2000
TEXT_TYPES = (discord.MessageType.default, discord.MessageType.reply)
CHANNEL_TYPES = (discord.ChannelType.text, discord.ChannelType.news)  # what a TextChannel option offers

MESSAGES = {
    "off": "Summaries aren't set up yet.",
    "guild": "Use this in the server.",
    "young": f"`/catchup` is for Discord accounts at least {quests.MIN_ACCOUNT_DAYS} days old.",
    "hours": f"Pick between {C.MIN_HOURS} and {C.MAX_HOURS} hours.",
    "kind": "I can only catch you up on a regular text channel.",
    "private": "I only summarize public channels, the ones everyone on the server can read.",
    "cant_see": "You can't see that channel.",
    "busy": "Your last catch-up is still being written. One at a time.",
    "queue": "Someone else's catch-up is being written right now. Try again in a minute.",
    "user": f"You've used your {C.USER_DAILY} catch-ups for today. They reset at midnight Pacific.",
    "server": "The server has used all of today's catch-ups. They reset at midnight Pacific.",
    "cant_read": "I can't read that channel's history.",
    "quiet": "It's been quiet in {channel}: nothing much to catch up on in the last {hours} h.",
    "failed": "I couldn't write a summary just now. Try again in a bit.{refund}",
    "empty": "The summary came back empty. Try again later or with a different time window.",
    "unsafe": "I couldn't write a clean summary of that. Try a different channel or time window.",
    "staff": "That's for staff.",
}
REFUNDED = " That one didn't count toward your daily limit."


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    """Model or member text made inert: no markdown, no masked links, no mentions."""
    text = discord.utils.escape_markdown(text or "").replace("[", "\\[").replace("]", "\\]")
    return discord.utils.escape_mentions(text)


def is_staff(user) -> bool:
    guild = getattr(user, "guild", None)
    if guild is None:
        return False
    return community_rules.can_handle(guild.owner_id == user.id, user.guild_permissions.administrator, user.roles)


# ---------------------------------------------------------------- network seam
class Api(Protocol):
    """Tests pass a fake; nothing else here touches the network."""

    async def post(self, url: str, *, headers: dict, body: dict, timeout: float,
                   max_bytes: int) -> tuple[int, bytes]: ...

    async def get(self, url: str, *, timeout: float, max_bytes: int) -> tuple[int, bytes]: ...


class AiohttpApi:
    """One JSON POST (or a plain GET) with a total timeout, no redirects, no proxy from
    the environment, reading at most `max_bytes`."""

    async def post(self, url, *, headers, body, timeout, max_bytes):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(url, headers=headers, data=json.dumps(body).encode("utf-8"),
                                    allow_redirects=False) as resp:
                return resp.status, await resp.content.read(max_bytes)

    async def get(self, url, *, timeout, max_bytes):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, allow_redirects=False) as resp:
                return resp.status, await resp.content.read(max_bytes)


class Failed(Exception):
    """The API call didn't produce an answer. `billed` is False when no tokens can have
    been spent (connection error or an error status), so the request is handed back.
    `unavailable` means the local provider isn't there (server down, model missing)."""

    def __init__(self, why: str, billed: bool, unavailable: bool = False):
        super().__init__(why)
        self.billed = billed
        self.unavailable = unavailable


class Catchup(commands.Cog):
    def __init__(self, bot, api: Api | None = None, environ=None):
        self.bot = bot
        self.api = api or AiohttpApi()
        self.environ = os.environ if environ is None else environ
        # Members with a request in flight. May reset on restart; the daily caps are in SQLite.
        self.busy: set[int] = set()
        # One local (Ollama) summary at a time server-wide; nobody waits on it, a second
        # request is told to try again.
        self.local = asyncio.Lock()
        self.probed: tuple[tuple, int, bool] | None = None  # ((url, model), when, usable)
        self.warned_at: int | None = None
        for problem in self.setup().problems:  # e.g. a refused OLLAMA_URL, said once at startup
            log.warning("catchup: %s", problem)

    @property
    def db(self):
        return self.bot.db

    def setup(self) -> C.Setup:
        return C.read_setup(self.environ)

    def api_key(self) -> str:
        return self.setup().api_key

    async def cog_load(self) -> None:
        """Say at startup which provider is in use (and look for Ollama once, so the
        first /catchup already knows)."""
        try:
            setup = self.setup()
            if setup.provider == C.OLLAMA:
                usable = await self.ollama_ready(setup)
                log.info("catchup: provider ollama, model %s, %s", setup.ollama_model,
                         "ready" if usable else "not available right now")
            else:
                log.info("catchup: provider %s", setup.provider or "none (summaries off)")
        except Exception:
            log.exception("catchup: startup check failed")

    # ------------------------------------------------------------ the local provider
    def warn_unavailable(self, why: str) -> None:
        """One warning an hour at most, however often members try."""
        at = now()
        if self.warned_at is None or not 0 <= at - self.warned_at < C.UNAVAILABLE_LOG_EVERY:
            self.warned_at = at
            log.warning("catchup: Ollama isn't available (%s); /catchup says summaries aren't set up", why)

    def ollama_known(self, setup: C.Setup) -> bool | None:
        """The last probe's answer if it is fresh, else None. No network."""
        if self.probed is None:
            return None
        target, at, usable = self.probed
        if target != (setup.ollama_url, setup.ollama_model) or not 0 <= now() - at < C.OLLAMA_PROBE_TTL:
            return None
        return usable

    async def ollama_ready(self, setup: C.Setup) -> bool:
        """Is the Ollama server answering and is the model pulled? Asks /api/tags (which
        loads nothing) at most once a minute. Never raises."""
        known = self.ollama_known(setup)
        if known is not None:
            return known
        why = None
        try:
            status, body = await asyncio.wait_for(
                self.api.get(setup.ollama_url + C.OLLAMA_TAGS_PATH, timeout=C.OLLAMA_PROBE_TIMEOUT,
                             max_bytes=C.OLLAMA_TAGS_BYTES), C.OLLAMA_PROBE_TIMEOUT)
            if status != 200:
                why = f"status {status}"
            else:
                try:
                    data = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    data = None
                has = C.ollama_has_model(data, setup.ollama_model)
                if has is None:
                    why = "unreadable model list"
                elif not has:
                    why = f"model {setup.ollama_model} isn't pulled"
        except (asyncio.TimeoutError, TimeoutError):
            why = "no answer"
        except Exception as exc:  # connection refused: the server isn't running
            why = type(exc).__name__
        self.probed = ((setup.ollama_url, setup.ollama_model), now(), why is None)
        if why is not None:
            self.warn_unavailable(why)
        return why is None

    def day(self) -> str:
        return C.day_key(now(), self.bot.settings.tz)

    # ------------------------------------------------------------ usage (cost caps)
    async def counts(self, day: str, user_id: int) -> tuple[int, int]:
        mine = await self.db.fetchone("SELECT requests FROM catchup_usage WHERE day = ? AND user_id = ?",
                                      (day, user_id))
        total = await self.db.fetchone("SELECT COALESCE(SUM(requests), 0) AS n FROM catchup_usage WHERE day = ?",
                                       (day,))
        return (mine["requests"] if mine else 0), total["n"]

    async def reserve(self, day: str, user_id: int) -> str | None:
        """Take one of today's requests, or say which cap is hit ("user"/"server")."""
        async with self.db.transaction() as tx:
            mine = await tx.fetchone("SELECT requests FROM catchup_usage WHERE day = ? AND user_id = ?",
                                     (day, user_id))
            total = await tx.fetchone("SELECT COALESCE(SUM(requests), 0) AS n FROM catchup_usage WHERE day = ?",
                                      (day,))
            block = C.quota_block(mine["requests"] if mine else 0, total["n"])
            if block:
                return block
            await tx.execute(
                "INSERT INTO catchup_usage (day, user_id, requests) VALUES (?, ?, 1) "
                "ON CONFLICT (day, user_id) DO UPDATE SET requests = requests + 1", (day, user_id))
        return None

    async def refund(self, day: str, user_id: int) -> None:
        await self.db.execute(
            "UPDATE catchup_usage SET requests = MAX(requests - 1, 0) WHERE day = ? AND user_id = ?", (day, user_id))

    async def add_tokens(self, day: str, user_id: int, answer: C.Answer) -> None:
        await self.db.execute(
            "UPDATE catchup_usage SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ? "
            "WHERE day = ? AND user_id = ?", (answer.input_tokens, answer.output_tokens, day, user_id))

    # ------------------------------------------------------------ channel rules
    @staticmethod
    def channel_problem(channel, member) -> str | None:
        """None if `channel` may be summarized for `member`, else a MESSAGES key."""
        guild = member.guild
        if getattr(channel, "type", None) not in CHANNEL_TYPES or getattr(channel, "guild", None) != guild:
            return "kind"
        everyone = channel.permissions_for(guild.default_role)
        category = getattr(channel, "category", None)
        if (not (everyone.view_channel and everyone.read_message_history)
                or getattr(category, "name", None) == config.STAFF_CATEGORY):
            return "private"
        mine = channel.permissions_for(member)
        if not (mine.view_channel and mine.read_message_history):
            return "cant_see"
        return None

    # ------------------------------------------------------------ reading the channel
    async def collect(self, channel, hours: int) -> tuple[list[str], list[str], int]:
        """(prompt lines oldest first, raw texts, messages left out for opted-out members)."""
        after = datetime.fromtimestamp(now() - hours * 3600, timezone.utc)
        tz = self.bot.settings.tz
        allowed: dict[int, bool] = {}
        entries: list[str] = []
        raw: list[str] = []
        left_out = 0
        async for message in channel.history(limit=C.MAX_MESSAGES, after=after, oldest_first=False):
            author = message.author
            if getattr(author, "bot", False) or getattr(message, "type", TEXT_TYPES[0]) not in TEXT_TYPES:
                continue
            if author.id not in allowed:
                allowed[author.id] = await self.db.tracking_allowed(author.id)
            if not allowed[author.id]:
                left_out += 1
                continue
            content = message.content or ""
            text = C.clean_text(
                content,
                users={u.id: u.display_name for u in getattr(message, "mentions", ())},
                roles={r.id: r.name for r in getattr(message, "role_mentions", ())},
                channels={c.id: c.name for c in getattr(message, "channel_mentions", ())})
            entry = C.line(author.display_name, text, attachments=len(getattr(message, "attachments", ())),
                           stickers=len(getattr(message, "stickers", ())),
                           clock=message.created_at.astimezone(tz).strftime("%a %H:%M"))
            if entry is not None:
                entries.append(entry)
                raw.append(content)
        entries.reverse()
        return entries, raw, left_out

    # ------------------------------------------------------------ the API call
    async def ask(self, setup: C.Setup, log_text: str, channel_name: str, hours: int) -> C.Answer:
        local = setup.provider == C.OLLAMA
        if local:
            url, headers = setup.ollama_url + C.OLLAMA_CHAT_PATH, C.ollama_headers()
            body = C.ollama_request_body(log_text, channel_name, hours, setup.ollama_model)
            timeout, parse = C.OLLAMA_TIMEOUT, C.parse_ollama_response
        else:
            url, headers = C.API_URL, C.request_headers(setup.api_key)
            body = C.request_body(log_text, channel_name, hours)
            timeout, parse = C.TIMEOUT, C.parse_response
        try:
            status, raw = await asyncio.wait_for(
                self.api.post(url, headers=headers, body=body, timeout=timeout,
                              max_bytes=C.MAX_RESPONSE_BYTES), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise Failed("timeout", billed=True) from None  # the request may have been processed
        except Exception as exc:  # DNS, connection reset; for Ollama: the server isn't running
            raise Failed(type(exc).__name__, billed=False, unavailable=local) from None
        if status != 200:  # Ollama answers 404 when the model isn't pulled
            raise Failed(f"status {status}", billed=False, unavailable=local and status == 404)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        answer = parse(data)
        if answer is None:
            raise Failed("unreadable response", billed=True)
        return answer

    @staticmethod
    def render(channel, hours: int, used: int, left_out: int, points: list[str], jump: str | None,
               provider: str = C.ANTHROPIC) -> str:
        head = f"**Catch-up: {channel.mention}, last {hours} h** ({used} message{'s' if used != 1 else ''} read"
        if left_out:
            head += (f"; {left_out} left out from members who turned tracking off")
        head += ")"
        tail = [f"**{C.JUMP_IN}** {esc(jump)}"] if jump else []
        tail.append(f"-# {C.footer(provider)}")
        lines = [f"• {esc(p)}" for p in points]
        while lines and len("\n".join([head, *lines, *tail])) > DISCORD_LIMIT:
            lines.pop()
        return "\n".join([head, *lines, *tail])[:DISCORD_LIMIT]

    # ------------------------------------------------------------ /catchup
    @app_commands.command(name="catchup", description="A private AI summary of what you missed in a channel")
    @app_commands.describe(channel="Which public channel (default: this one)",
                           hours=f"How far back to look, {C.MIN_HOURS}-{C.MAX_HOURS} (default {C.DEFAULT_HOURS})")
    @app_commands.guild_only()
    async def catchup(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None,
                      hours: app_commands.Range[int, C.MIN_HOURS, C.MAX_HOURS] = C.DEFAULT_HOURS) -> None:
        try:
            await self.run(interaction, channel, hours)
        except Exception:
            log.exception("catchup: command failed")
            try:
                await self.say(interaction, MESSAGES["failed"].format(refund=""))
            except Exception:
                pass

    @staticmethod
    async def say(interaction, text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_PINGS)
        else:
            await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)

    async def run(self, interaction, channel, hours: int) -> None:
        setup = self.setup()
        local = setup.provider == C.OLLAMA
        if not setup.provider or (local and self.ollama_known(setup) is False):
            return await self.say(interaction, MESSAGES["off"])
        member = interaction.user
        if interaction.guild is None or getattr(member, "guild", None) is None:
            return await self.say(interaction, MESSAGES["guild"])
        if not C.established(member.id, now()):
            return await self.say(interaction, MESSAGES["young"])
        if not C.valid_hours(hours):
            return await self.say(interaction, MESSAGES["hours"])
        channel = channel or interaction.channel
        problem = self.channel_problem(channel, member)
        if problem:
            return await self.say(interaction, MESSAGES[problem])
        if member.id in self.busy:
            return await self.say(interaction, MESSAGES["busy"])
        day = self.day()
        block = C.quota_block(*await self.counts(day, member.id))
        if block:
            return await self.say(interaction, MESSAGES[block])

        self.busy.add(member.id)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if local and not await self.ollama_ready(setup):
                return await self.say(interaction, MESSAGES["off"])
            try:
                entries, raw, left_out = await asyncio.wait_for(self.collect(channel, hours), HISTORY_TIMEOUT)
            except (discord.HTTPException, asyncio.TimeoutError, TimeoutError) as exc:
                log.warning("catchup: couldn't read channel history (%s)", type(exc).__name__)
                return await self.say(interaction, MESSAGES["cant_read"])
            log_text, used = C.fit_log(entries)
            if used < C.MIN_MESSAGES:
                quiet = MESSAGES["quiet"].format(channel=channel.mention, hours=hours)
                if left_out:
                    quiet += f" ({left_out} left out from members who turned tracking off.)"
                return await self.say(interaction, quiet)
            if local and self.local.locked():  # nobody queues behind the local model
                return await self.say(interaction, MESSAGES["queue"])
            # No await between the check above and taking the lock, so it is never waited on.
            async with (self.local if local else contextlib.nullcontext()):
                block = await self.reserve(day, member.id)
                if block:
                    return await self.say(interaction, MESSAGES[block])
                try:
                    answer = await self.ask(setup, log_text, channel.name, hours)
                except Failed as exc:
                    if not exc.billed:
                        await self.refund(day, member.id)
                    if exc.unavailable:
                        self.probed = ((setup.ollama_url, setup.ollama_model), now(), False)
                        self.warn_unavailable(str(exc))
                        return await self.say(interaction, MESSAGES["off"])
                    log.warning("catchup: API call failed (%s)", exc)
                    return await self.say(interaction,
                                          MESSAGES["failed"].format(refund="" if exc.billed else REFUNDED))
            await self.add_tokens(day, member.id, answer)
            log.info("catchup: user %s, %s, %d messages, %d in / %d out tokens", member.id, setup.provider,
                     used, answer.input_tokens, answer.output_tokens)
            points, jump = C.bullets(answer.text, C.source_urls(raw))
            if not points:
                return await self.say(interaction, MESSAGES["empty"])
            if not C.safe_to_show(points, jump):
                log.info("catchup: summary for user %s blocked by the text filter", member.id)
                return await self.say(interaction, MESSAGES["unsafe"])
            await self.say(interaction, self.render(channel, hours, used, left_out, points, jump, setup.provider))
        finally:
            self.busy.discard(member.id)

    # ------------------------------------------------------------ /catchup-usage (staff)
    @app_commands.command(name="catchup-usage", description="Today's /catchup requests and tokens (staff)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def catchup_usage(self, interaction: discord.Interaction) -> None:
        try:
            if not is_staff(interaction.user):
                return await self.say(interaction, MESSAGES["staff"])
            day = self.day()
            setup = self.setup()
            if not setup.provider:
                state, model = f"off ({setup.why_off})", ""
            elif setup.provider == C.ANTHROPIC:
                state, model = "on, Anthropic API", setup.model
            else:
                if self.ollama_known(setup) is None:  # a network check: answer Discord first
                    await interaction.response.defer(ephemeral=True, thinking=True)
                if await self.ollama_ready(setup):
                    state = "on, Ollama on the server's own machine"
                else:
                    state = "off (Ollama isn't answering or the model isn't pulled)"
                model = setup.model
            row = await self.db.fetchone(
                "SELECT COALESCE(SUM(requests), 0) AS requests, COUNT(CASE WHEN requests > 0 THEN 1 END) AS members, "
                "COALESCE(SUM(input_tokens), 0) AS tin, COALESCE(SUM(output_tokens), 0) AS tout "
                "FROM catchup_usage WHERE day = ?", (day,))
            week = await self.db.fetchone(
                "SELECT COALESCE(SUM(requests), 0) AS requests, COALESCE(SUM(input_tokens), 0) AS tin, "
                "COALESCE(SUM(output_tokens), 0) AS tout FROM catchup_usage WHERE day > date(?, '-7 days')", (day,))
            await self.say(interaction, "\n".join([
                f"**/catchup usage, {day}** (summaries: {state})",
                f"Requests today: **{row['requests']}** of {C.SERVER_DAILY}, from {row['members']} "
                f"member{'s' if row['members'] != 1 else ''} (each can make {C.USER_DAILY} a day)",
                f"Tokens today: **{row['tin']:,}** in, **{row['tout']:,}** out",
                f"Last 7 days: {week['requests']} requests, {week['tin']:,} in, {week['tout']:,} out",
                f"-# Model `{model}`. Days are Pacific." if model else "-# Days are Pacific.",
            ]))
        except Exception:
            log.exception("catchup: usage command failed")


async def setup(bot) -> None:
    await bot.add_cog(Catchup(bot))
