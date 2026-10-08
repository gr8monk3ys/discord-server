"""Matchmaking queue: /queue join puts a member in a (game, mode, size) bucket; when a bucket
holds `size` live members they're popped together in one transaction (nobody lands in two
matches), recorded in mm_matches, given a temporary voice channel in the voice category
(user limit = size) and pinged in 🎲・games (else 🤖・bot-commands) with a link to it.

- One queue entry per member: joining another bucket moves you; leaving the server removes it.
- Entries expire after an hour (swept every minute, no DM); the member is told in an ephemeral
  note the next time they use /queue (a meta key mm:expired:<user id>, pruned after a week).
- Match channels are registered with cogs.tempvoice (temp_voice row + its owners map), so once
  people have been in one and it empties it goes 30 s later like any squad channel, and
  /squad name|limit work in it. A match channel nobody ever joins is deleted here after
  10 minutes empty.
- No coins or XP are paid for matches, so fresh alt accounts have nothing to farm here.
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from errors import reply_error
from logic import matchmaking as M
from logic.matchmaking import Entry

log = logging.getLogger(__name__)

MODE_CHOICES = [app_commands.Choice(name=m, value=m) for m in config.MODES]
NOTICE_PREFIX = "mm:expired:"
NOTICE_KEEP = 7 * 24 * 60 * 60
EXPIRED_NOTE = "Heads up: your last queue entry expired after an hour without a match, so it was removed."


def now() -> int:
    return int(time.time())


def ping_only(users=()) -> discord.AllowedMentions:
    """Allow exactly these user pings and nothing else."""
    return discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(u) for u in users] or False)


def esc(text) -> str:
    return discord.utils.escape_markdown(str(text or ""))


def game_label(key: str) -> str:
    game = config.game_by_key(key)
    return f"{game.emoji} {game.role}" if game else esc(key)


def bucket_label(game: str, mode: str, size: int) -> str:
    return f"{game_label(game)} · {mode} · {size} players"


def entry_of(row) -> Entry:
    return Entry(row["user_id"], row["game"], row["mode"], row["size"], row["joined_at"])


class Matchmaking(commands.Cog):
    queue = app_commands.Group(name="queue", description="Get matched with members who want to play the same game",
                               guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.empty_since: dict[int, float] = {}  # match channel id -> first seen empty

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    async def cog_load(self) -> None:
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.sweep.cancel()

    # ------------------------------------------------------------ expiry notes
    async def take_notice(self, user_id: int) -> bool:
        """True (once) if this member's entry expired since they last used /queue."""
        return await self.db.execute("DELETE FROM meta WHERE key = ?", (f"{NOTICE_PREFIX}{user_id}",)) > 0

    async def expire(self, t: int) -> list[int]:
        """Remove expired entries and leave each member a note; returns their ids."""
        cutoff = t - M.QUEUE_TTL
        async with self.db.transaction() as tx:
            rows = await tx.fetchall("SELECT user_id FROM mm_queue WHERE joined_at <= ?", (cutoff,))
            gone = [r["user_id"] for r in rows]
            for uid in gone:
                await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                 (f"{NOTICE_PREFIX}{uid}", str(t)))
            await tx.execute("DELETE FROM mm_queue WHERE joined_at <= ?", (cutoff,))
            await tx.execute("DELETE FROM meta WHERE key LIKE ? AND CAST(value AS INTEGER) <= ?",
                             (f"{NOTICE_PREFIX}%", t - NOTICE_KEEP))
        if gone:
            log.info("matchmaking: %d queue entries expired", len(gone))
        return gone

    # ------------------------------------------------------------ joining and popping
    def present_check(self, guild, caller: int):
        """Who still counts as in the server. Only trusted once the member cache is complete."""
        if guild is None or not getattr(guild, "chunked", False):
            return None
        return lambda uid: uid == caller or guild.get_member(uid) is not None

    async def enqueue(self, user_id: int, game: str, mode: str, size: int, t: int, present=None):
        """Put a member in a bucket and pop it if it's full, all in one transaction.
        Returns (previous entry or None, popped entries or None, match id or None)."""
        async with self.db.transaction() as tx:
            old_row = await tx.fetchone("SELECT * FROM mm_queue WHERE user_id = ?", (user_id,))
            old = entry_of(old_row) if old_row is not None else None
            if old is not None and M.expired(old.joined_at, t):
                old = None  # an expired entry is replaced as if it weren't there
            if old is not None and old.bucket == (game, mode, size):
                joined_at = old.joined_at  # same bucket again: keep their place in line
            else:
                joined_at = t
            await tx.execute("INSERT OR REPLACE INTO mm_queue (user_id, game, mode, size, joined_at) "
                             "VALUES (?, ?, ?, ?, ?)", (user_id, game, mode, size, joined_at))
            rows = await tx.fetchall("SELECT * FROM mm_queue WHERE game = ? AND mode = ? AND size = ?",
                                     (game, mode, size))
            picked = M.pick([entry_of(r) for r in rows], size, t, present)
            match_id = None
            if picked:
                ids = [e.user_id for e in picked]
                await tx.execute(f"DELETE FROM mm_queue WHERE user_id IN ({','.join('?' * len(ids))})", ids)
                cur = await tx.execute("INSERT INTO mm_matches (game, mode, members, created_at) VALUES (?, ?, ?, ?)",
                                       (game, mode, M.encode_members(ids), t))
                match_id = cur.lastrowid
        return old, picked, match_id

    @queue.command(name="join", description="Queue up: you'll be matched when enough members want the same game")
    @app_commands.describe(game="Which game", mode="Ranked or Casual",
                           size="Players per match, you included (default: the game's usual team size)")
    @app_commands.choices(mode=MODE_CHOICES)
    async def join(self, interaction: discord.Interaction, game: app_commands.Range[str, 1, 60],
                   mode: app_commands.Choice[str],
                   size: app_commands.Range[int, M.MIN_SIZE, M.MAX_SIZE] | None = None) -> None:
        try:
            await self.handle_join(interaction, game, getattr(mode, "value", mode), size)
        except Exception:
            log.exception("matchmaking: /queue join failed")
            await reply_error(interaction)

    @join.autocomplete("game")
    async def game_choices(self, interaction, current: str):
        text = (current or "").lower().strip()
        return [app_commands.Choice(name=g.role, value=g.key) for g in config.GAMES
                if text in g.role.lower()][:25]

    async def handle_join(self, interaction, game_text: str, mode_text: str, size: int | None) -> None:
        user = interaction.user
        game = M.resolve_game(game_text)
        if game is None:
            await interaction.response.send_message("Pick a game from the list.", ephemeral=True)
            return
        mode = M.resolve_mode(mode_text)
        if mode is None:
            await interaction.response.send_message(f"Mode must be one of: {', '.join(config.MODES)}.",
                                                    ephemeral=True)
            return
        size = M.resolve_size(game.key, size)
        note = await self.take_notice(user.id)
        t = now()
        guild = interaction.guild or self.guild()
        old, picked, match_id = await self.enqueue(user.id, game.key, mode, size, t,
                                                   self.present_check(guild, user.id))
        label = bucket_label(game.key, mode, size)
        lines = [EXPIRED_NOTE] if note else []
        if picked:
            lines.append(f"Match found: **{label}**. Setting up your voice channel...")
        elif old is not None and old.bucket == (game.key, mode, size):
            lines.append(f"You're already in the **{label}** queue.")
        else:
            if old is not None:
                lines.append(f"Moved you out of **{bucket_label(*old.bucket)}**.")
            lines.append(f"You're in the **{label}** queue. You'll be pinged when {size} players are in; "
                         "the entry expires after an hour. `/queue leave` to drop out.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())
        if picked:
            channel = await self.start_match(guild, match_id, game, mode, size, [e.user_id for e in picked])
            where = (f"Your voice channel: {channel.mention}" if channel is not None
                     else f"Grab a voice channel: join **{esc(config.NEW_SQUAD_VOICE)}** for a fresh one.")
            try:
                await interaction.followup.send(where, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.info("matchmaking: couldn't tell %s the voice channel", user.id)

    # ------------------------------------------------------------ match channel + ping
    async def start_match(self, guild, match_id: int, game: config.Game, mode: str, size: int,
                          members: list[int]):
        """Voice channel and ping for a popped match; returns the channel (or None). Never
        raises: the match row is already saved."""
        channel = None
        try:
            channel = await self.create_channel(guild, game, size, members)
            if channel is not None:
                await self.db.execute("UPDATE mm_matches SET channel_id = ? WHERE id = ?", (channel.id, match_id))
        except Exception:
            log.exception("matchmaking: voice channel for match %s failed", match_id)
        try:
            await self.announce(guild, game, mode, size, members, channel)
        except Exception:
            log.exception("matchmaking: announcing match %s failed", match_id)
        log.info("matchmaking: match %s (%s %s %d) channel %s", match_id, game.key, mode, size,
                 channel.id if channel else None)
        return channel

    async def create_channel(self, guild, game: config.Game, size: int, members: list[int]):
        if guild is None:
            return None
        category = config.match_by_name(guild.categories, config.VOICE_CATEGORY)
        if category is None:
            log.warning("matchmaking: no %r category; match without a voice channel", config.VOICE_CATEGORY)
            return None
        try:
            new = await category.create_voice_channel(
                M.channel_name(game, game.key), user_limit=size, overwrites=category.overwrites,
                reason=f"Matchmaking: {game.role} match")
        except discord.HTTPException:
            log.exception("matchmaking: creating a voice channel failed")
            return None
        self.empty_since[new.id] = now()
        # Register it the way tempvoice tracks its channels: emptied channels get deleted, /squad works.
        await self.db.execute("INSERT OR REPLACE INTO temp_voice (channel_id, owner_id, created_at) VALUES (?, ?, ?)",
                              (new.id, members[0], now()))
        tempvoice = self.bot.get_cog("TempVoice")
        if tempvoice is not None:
            tempvoice.owners[new.id] = members[0]
        return new

    def post_channel(self, guild):
        if guild is None:
            return None
        return (config.match_by_name(guild.text_channels, config.GAMES_CHANNEL)
                or config.match_by_name(guild.text_channels, config.BOT_COMMANDS_CHANNEL))

    async def announce(self, guild, game: config.Game, mode: str, size: int, members: list[int], channel) -> None:
        post = self.post_channel(guild)
        where = (f"Your voice channel: {channel.mention}" if channel is not None
                 else f"Grab a voice channel: join **{esc(config.NEW_SQUAD_VOICE)}** for a fresh one.")
        if post is None:
            log.warning("matchmaking: no %r or %r channel to announce in; DMing the players", config.GAMES_CHANNEL,
                        config.BOT_COMMANDS_CHANNEL)
            text = f"🎮 **Match found** · {bucket_label(game.key, mode, size)}\n{where}"
            for uid in members:
                member = guild.get_member(uid) if guild is not None else None
                try:
                    if member is not None:
                        await member.send(text, allowed_mentions=discord.AllowedMentions.none())
                except (discord.HTTPException, AttributeError):
                    log.info("matchmaking: match DM to %s failed", uid)
            return
        pings = " ".join(f"<@{u}>" for u in members)
        text = f"🎮 **Match found** · {bucket_label(game.key, mode, size)}\n{pings}\n{where}"
        try:
            await post.send(text, allowed_mentions=ping_only(members))
        except discord.HTTPException:
            log.exception("matchmaking: match ping failed")

    # ------------------------------------------------------------ leave / status
    @queue.command(name="leave", description="Leave the matchmaking queue")
    async def leave(self, interaction: discord.Interaction) -> None:
        try:
            note = await self.take_notice(interaction.user.id)
            async with self.db.transaction() as tx:
                row = await tx.fetchone("SELECT * FROM mm_queue WHERE user_id = ?", (interaction.user.id,))
                await tx.execute("DELETE FROM mm_queue WHERE user_id = ?", (interaction.user.id,))
            lines = [EXPIRED_NOTE] if note else []
            if row is not None and not M.expired(row["joined_at"], now()):
                lines.append(f"You left the **{bucket_label(row['game'], row['mode'], row['size'])}** queue.")
            elif not note:
                lines.append("You're not in a queue.")
            await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            log.exception("matchmaking: /queue leave failed")
            await reply_error(interaction)

    @queue.command(name="status", description="See how many people are queued for each game")
    async def status(self, interaction: discord.Interaction) -> None:
        try:
            await self.handle_status(interaction)
        except Exception:
            log.exception("matchmaking: /queue status failed")
            await reply_error(interaction)

    async def handle_status(self, interaction) -> None:
        me = interaction.user.id
        note = await self.take_notice(me)
        t = now()
        entries = [entry_of(r) for r in await self.db.fetchall("SELECT * FROM mm_queue")]
        rows = M.summary(entries, me, t)
        guild = interaction.guild or self.guild()
        lines = [EXPIRED_NOTE] if note else []
        if not rows:
            lines.append("Nobody's queued right now. Start one with `/queue join`.")
        for r in rows:
            line = f"{bucket_label(r.game, r.mode, r.size)}: **{r.waiting}**/{r.size} waiting"
            if r.mine:
                mine = next(e for e in entries if e.user_id == me)
                others = [e for e in sorted(entries, key=lambda e: (e.joined_at, e.user_id))
                          if e.bucket == mine.bucket and e.user_id != me and not M.expired(e.joined_at, t)]
                names = [esc(self.name_of(guild, e.user_id)) for e in others]
                left = max(1, -(-M.expires_in(mine.joined_at, t) // 60))
                line += f" (you're in this one, expires in {left} min"
                line += f"; with {', '.join(names)})" if names else ")"
            lines.append(line)
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900].rsplit("\n", 1)[0] + "\n…"
        await interaction.response.send_message(text, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def name_of(guild, user_id: int) -> str:
        member = guild.get_member(user_id) if guild is not None else None
        return member.display_name if member is not None else "someone"

    # ------------------------------------------------------------ members leaving
    @commands.Cog.listener()
    async def on_member_remove(self, member) -> None:
        try:
            if member.guild.id != self.bot.settings.guild_id:
                return
            await self.db.execute("DELETE FROM mm_queue WHERE user_id = ?", (member.id,))
            await self.db.execute("DELETE FROM meta WHERE key = ?", (f"{NOTICE_PREFIX}{member.id}",))
        except Exception:
            log.exception("matchmaking: removing %s from the queue failed", getattr(member, "id", None))

    # ------------------------------------------------------------ the minute sweep
    @tasks.loop(minutes=1)
    async def sweep(self) -> None:
        for name, job in (("expiry", lambda: self.expire(now())), ("empty channels", self.clean_channels)):
            try:
                await job()
            except Exception:
                log.exception("matchmaking: %s sweep failed", name)

    @sweep.before_loop
    async def before_sweep(self) -> None:
        await self.bot.wait_until_ready()

    async def clean_channels(self) -> int:
        """Delete match channels that have been empty for EMPTY_GRACE; returns how many."""
        guild = self.guild()
        if guild is None:
            return 0
        t = now()
        rows = await self.db.fetchall("SELECT channel_id FROM mm_matches WHERE channel_id IS NOT NULL "
                                      "AND created_at >= ?", (t - M.CHANNEL_WATCH,))
        removed = 0
        for row in rows:
            cid = row["channel_id"]
            channel = guild.get_channel(cid)
            if channel is None:
                self.empty_since.pop(cid, None)
                continue
            people = [u for u in channel.voice_states
                      if (m := guild.get_member(u)) is None or not m.bot]
            if people:
                self.empty_since.pop(cid, None)
                continue
            self.empty_since.setdefault(cid, t)
            if M.empty_for(self.empty_since, cid, t) < M.EMPTY_GRACE:
                continue
            try:
                await self.remove_channel(channel)
            except discord.HTTPException:
                log.exception("matchmaking: deleting match channel %s failed", cid)
                continue
            self.empty_since.pop(cid, None)
            removed += 1
        if removed:
            log.info("matchmaking: deleted %d empty match channels", removed)
        return removed

    async def remove_channel(self, channel) -> None:
        """Delete a match channel through tempvoice when it tracks it (so its state goes too)."""
        tempvoice = self.bot.get_cog("TempVoice")
        if tempvoice is not None and channel.id in tempvoice.owners:
            await tempvoice.remove(channel)
            return
        try:
            await channel.delete(reason="Matchmaking: match channel empty")
        except discord.NotFound:
            pass
        await self.db.execute("DELETE FROM temp_voice WHERE channel_id = ?", (channel.id,))


async def setup(bot) -> None:
    await bot.add_cog(Matchmaking(bot))
