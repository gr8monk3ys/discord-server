"""/catchup [channel] [hours]: a private AI summary of what happened in a public channel.

The member's recent public messages from that one channel (never staff or private
channels, never bots, never members who turned tracking off) are flattened by
logic.catchup and sent to the Anthropic Messages API, which writes a few bullets. The
answer is shown only to the member who asked (ephemeral, no pings, markdown escaped,
foreign links removed). Nothing from the channel or the answer is stored or logged; only
request and token counts go into catchup_usage for the cost caps.

Needs ANTHROPIC_API_KEY in the environment (server/.env). Without it the command says
summaries aren't set up yet and does nothing else. Cost controls: 3 requests per member
and 60 per server per (Pacific) day, one request at a time per member, a 20 s timeout,
600 output tokens, a ~12,000 character prompt, accounts 30+ days old only.

Staff: /catchup-usage shows today's requests and tokens."""

import asyncio
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
ENV_KEY = "ANTHROPIC_API_KEY"
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


class AiohttpApi:
    """One JSON POST with a total timeout, no redirects, reading at most `max_bytes`."""

    async def post(self, url, *, headers, body, timeout, max_bytes):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(url, headers=headers, data=json.dumps(body).encode("utf-8"),
                                    allow_redirects=False) as resp:
                return resp.status, await resp.content.read(max_bytes)


class Failed(Exception):
    """The API call didn't produce an answer. `billed` is False when no tokens can have
    been spent (connection error or an error status), so the request is handed back."""

    def __init__(self, why: str, billed: bool):
        super().__init__(why)
        self.billed = billed


class Catchup(commands.Cog):
    def __init__(self, bot, api: Api | None = None, environ=None):
        self.bot = bot
        self.api = api or AiohttpApi()
        self.environ = os.environ if environ is None else environ
        # Members with a request in flight. May reset on restart; the daily caps are in SQLite.
        self.busy: set[int] = set()

    @property
    def db(self):
        return self.bot.db

    def api_key(self) -> str:
        return (self.environ.get(ENV_KEY) or "").strip()

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
    async def ask(self, key: str, log_text: str, channel_name: str, hours: int) -> C.Answer:
        try:
            status, body = await asyncio.wait_for(
                self.api.post(C.API_URL, headers=C.request_headers(key),
                              body=C.request_body(log_text, channel_name, hours),
                              timeout=C.TIMEOUT, max_bytes=C.MAX_RESPONSE_BYTES), C.TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            raise Failed("timeout", billed=True) from None  # the request may have been processed
        except Exception as exc:  # DNS, connection reset...
            raise Failed(type(exc).__name__, billed=False) from None
        if status != 200:
            raise Failed(f"status {status}", billed=False)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            data = None
        answer = C.parse_response(data)
        if answer is None:
            raise Failed("unreadable response", billed=True)
        return answer

    @staticmethod
    def render(channel, hours: int, used: int, left_out: int, points: list[str], jump: str | None) -> str:
        head = f"**Catch-up: {channel.mention}, last {hours} h** ({used} message{'s' if used != 1 else ''} read"
        if left_out:
            head += (f"; {left_out} left out from members who turned tracking off")
        head += ")"
        tail = [f"**{C.JUMP_IN}** {esc(jump)}"] if jump else []
        tail.append("-# Written by AI from the channel's recent messages. It can miss things or get them wrong.")
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
        key = self.api_key()
        if not key:
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
            block = await self.reserve(day, member.id)
            if block:
                return await self.say(interaction, MESSAGES[block])
            try:
                answer = await self.ask(key, log_text, channel.name, hours)
            except Failed as exc:
                log.warning("catchup: API call failed (%s)", exc)
                if not exc.billed:
                    await self.refund(day, member.id)
                return await self.say(interaction,
                                      MESSAGES["failed"].format(refund="" if exc.billed else REFUNDED))
            await self.add_tokens(day, member.id, answer)
            log.info("catchup: user %s, %d messages, %d in / %d out tokens", member.id, used,
                     answer.input_tokens, answer.output_tokens)
            points, jump = C.bullets(answer.text, C.source_urls(raw))
            if not points:
                return await self.say(interaction, MESSAGES["empty"])
            if not C.safe_to_show(points, jump):
                log.info("catchup: summary for user %s blocked by the text filter", member.id)
                return await self.say(interaction, MESSAGES["unsafe"])
            await self.say(interaction, self.render(channel, hours, used, left_out, points, jump))
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
            row = await self.db.fetchone(
                "SELECT COALESCE(SUM(requests), 0) AS requests, COUNT(CASE WHEN requests > 0 THEN 1 END) AS members, "
                "COALESCE(SUM(input_tokens), 0) AS tin, COALESCE(SUM(output_tokens), 0) AS tout "
                "FROM catchup_usage WHERE day = ?", (day,))
            week = await self.db.fetchone(
                "SELECT COALESCE(SUM(requests), 0) AS requests, COALESCE(SUM(input_tokens), 0) AS tin, "
                "COALESCE(SUM(output_tokens), 0) AS tout FROM catchup_usage WHERE day > date(?, '-7 days')", (day,))
            setup = "on" if self.api_key() else f"off (no `{ENV_KEY}`)"
            await self.say(interaction, "\n".join([
                f"**/catchup usage, {day}** (summaries: {setup})",
                f"Requests today: **{row['requests']}** of {C.SERVER_DAILY}, from {row['members']} "
                f"member{'s' if row['members'] != 1 else ''} (each can make {C.USER_DAILY} a day)",
                f"Tokens today: **{row['tin']:,}** in, **{row['tout']:,}** out",
                f"Last 7 days: {week['requests']} requests, {week['tin']:,} in, {week['tout']:,} out",
                f"-# Model `{C.MODEL}`. Days are Pacific.",
            ]))
        except Exception:
            log.exception("catchup: usage command failed")


async def setup(bot) -> None:
    await bot.add_cog(Catchup(bot))
