"""Module 5: Join-to-create voice. Joining ➕ New Squad makes a temporary voice
channel named after your game (or you) and moves you in. You own it bot-side
(no Discord permissions are granted): /squad name, /squad limit, /squad claim.
It's deleted 30 seconds after it empties; startup removes any left behind."""

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.stats import playing
from logic import tempvoice as T

log = logging.getLogger(__name__)

DENIED = {
    T.Denied.NOT_IN_TEMP: (f"Run this from inside your squad channel. Join **{config.NEW_SQUAD_VOICE}** "
                           "to get one."),
    T.Denied.NOT_OWNER: "Only the channel owner (<@{owner}>) can do that. If they've left, use `/squad claim`.",
    T.Denied.ALREADY_OWNER: "You already own this channel.",
    T.Denied.OWNER_PRESENT: "<@{owner}> owns this channel and is still in it.",
}


def now() -> float:
    return time.time()


class TempVoice(commands.Cog):
    squad = app_commands.Group(name="squad", description="Manage your temporary voice channel", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.empty_delay: float = T.EMPTY_DELAY
        self.owners: dict[int, int] = {}  # channel_id -> owner_id (mirror of temp_voice)
        self.joined: dict[int, dict[int, float]] = {}  # channel_id -> user_id -> joined at
        self.pending: dict[int, asyncio.Task] = {}  # channel_id -> delayed delete
        self.cooldown = T.Cooldown(T.CREATE_COOLDOWN)
        self.renames = T.WindowLimit(T.RENAME_LIMIT, T.RENAME_WINDOW)
        self.warned_missing = False

    async def cog_load(self) -> None:
        rows = await self.db.fetchall("SELECT channel_id, owner_id FROM temp_voice")
        self.owners = {r["channel_id"]: r["owner_id"] for r in rows}

    async def cog_unload(self) -> None:
        for task in self.pending.values():
            task.cancel()
        self.pending.clear()

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def places(self, guild: discord.Guild):
        """(category, hub), or None (logged once) if either is missing."""
        category = config.match_by_name(guild.categories, config.VOICE_CATEGORY)
        hubs = [c for c in guild.voice_channels if c.id not in self.owners]
        hub = config.match_by_name(hubs, config.NEW_SQUAD_VOICE)
        if category is None or hub is None:
            if not self.warned_missing:
                log.warning("temp voice off: no %r channel or %r category",
                            config.NEW_SQUAD_VOICE, config.VOICE_CATEGORY)
                self.warned_missing = True
            return None
        self.warned_missing = False
        return category, hub

    def humans(self, channel) -> list[int]:
        """User ids in a voice channel, minus known bots (unknown members count as people)."""
        out = []
        for user_id in channel.voice_states:
            member = channel.guild.get_member(user_id)
            if member is None or not member.bot:
                out.append(user_id)
        return out

    # ------------------------------------------------------------ state changes
    async def forget(self, channel_id: int) -> None:
        """Drop all state for a channel that's gone (or going)."""
        self.cancel_pending(channel_id)
        self.owners.pop(channel_id, None)
        self.joined.pop(channel_id, None)
        self.renames.forget(channel_id)
        await self.db.execute("DELETE FROM temp_voice WHERE channel_id = ?", (channel_id,))

    async def remove(self, channel) -> None:
        """Delete a temp channel and its row. A channel already gone just loses its row."""
        try:
            await channel.delete(reason="Temp voice: empty")
        except discord.NotFound:
            pass
        await self.forget(channel.id)

    async def set_owner(self, channel, owner_id: int, announce: str | None = None) -> None:
        self.owners[channel.id] = owner_id
        await self.db.execute("UPDATE temp_voice SET owner_id = ? WHERE channel_id = ?", (owner_id, channel.id))
        if announce:
            try:
                await channel.send(announce.format(owner=owner_id),
                                   allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                                            users=[discord.Object(owner_id)]))
            except discord.HTTPException:
                pass

    def cancel_pending(self, channel_id: int) -> None:
        task = self.pending.pop(channel_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def schedule_delete(self, channel_id: int) -> None:
        self.cancel_pending(channel_id)
        self.pending[channel_id] = asyncio.create_task(self.delete_if_still_empty(channel_id))

    async def delete_if_still_empty(self, channel_id: int) -> None:
        try:
            await asyncio.sleep(self.empty_delay)
            if self.pending.get(channel_id) is asyncio.current_task():
                del self.pending[channel_id]
            guild = self.guild()
            channel = guild.get_channel(channel_id) if guild else None
            if channel is None:
                await self.forget(channel_id)
            elif channel_id in self.owners and not self.humans(channel):
                await self.remove(channel)
                log.info("deleted empty temp voice %s", channel_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("temp voice cleanup failed for %s", channel_id)

    # ------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after) -> None:
        try:
            if member.guild.id != self.bot.settings.guild_id:
                return
            b, a = before.channel, after.channel
            if (b and b.id) == (a and a.id):
                return  # mute/deafen/stream changes
            if b is not None and b.id in self.owners:
                await self.left_temp(member, b)
            if a is not None and a.id in self.owners:
                self.joined_temp(member, a)
            if a is not None and not member.bot:
                await self.maybe_create(member, a)
        except Exception:
            log.exception("temp voice: voice update for %s failed", member.id)

    def joined_temp(self, member, channel) -> None:
        if member.bot:
            return
        self.joined.setdefault(channel.id, {}).setdefault(member.id, now())
        self.cancel_pending(channel.id)

    async def left_temp(self, member, channel) -> None:
        self.joined.get(channel.id, {}).pop(member.id, None)
        present = self.humans(channel)
        if not present:
            self.schedule_delete(channel.id)
        elif self.owners.get(channel.id) == member.id:
            heir = T.next_owner(present, self.joined.get(channel.id, {}), exclude=member.id)
            if heir is not None:
                await self.set_owner(channel, heir, "<@{owner}> owns this channel now (`/squad name`, `/squad limit`).")

    async def maybe_create(self, member, channel) -> None:
        places = self.places(member.guild)
        if places is None:
            return
        category, hub = places
        if channel.id != hub.id:
            return
        if not self.cooldown.take(member.id, now()):
            # Rejoined the hub right away: back to the channel they already own, if any.
            owned = [c for cid, o in self.owners.items() if o == member.id
                     and (c := member.guild.get_channel(cid)) is not None]
            if owned:
                try:
                    await member.move_to(owned[0], reason="Temp voice: back to your channel")
                except discord.HTTPException:
                    pass
            return
        name = T.channel_name(playing(member), member.display_name)
        new = await category.create_voice_channel(
            name, position=hub.position, overwrites=category.overwrites,
            reason=f"Temp voice for {member} ({member.id})")
        # Row first: if the bot dies right now, startup cleanup still finds the channel.
        self.owners[new.id] = member.id
        await self.db.execute("INSERT OR REPLACE INTO temp_voice (channel_id, owner_id, created_at) VALUES (?, ?, ?)",
                              (new.id, member.id, int(now())))
        try:
            await member.move_to(new, reason="Temp voice: your new channel")
        except discord.HTTPException:
            log.info("temp voice: %s left the hub before the move; removing %s", member.id, new.id)
            await self.remove(new)
            return
        self.joined.setdefault(new.id, {}).setdefault(member.id, now())
        log.info("temp voice %s (%s) for %s", new.id, name, member.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel) -> None:
        try:
            if channel.id in self.owners:
                await self.forget(channel.id)
        except Exception:
            log.exception("temp voice: forgetting deleted channel %s failed", channel.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Fires on first connect and after reconnects that rebuilt the cache.
        try:
            await self.cleanup()
        except Exception:
            log.exception("temp voice startup cleanup failed")

    async def cleanup(self) -> None:
        """Delete empty temp channels, drop rows for channels that are gone, and
        hand off channels whose owner left while the bot wasn't watching."""
        guild = self.guild()
        if guild is None:
            return
        self.places(guild)  # warn early if the hub is missing
        removed = 0
        for channel_id, owner_id in list(self.owners.items()):
            channel = guild.get_channel(channel_id)
            if channel is None:
                await self.forget(channel_id)
                continue
            present = self.humans(channel)
            if not present:
                await self.remove(channel)
                removed += 1
                continue
            seen = self.joined.setdefault(channel_id, {})
            for user_id in present:
                seen.setdefault(user_id, now())
            for gone in set(seen) - set(present):
                del seen[gone]
            if owner_id not in present:
                heir = T.next_owner(present, seen)
                await self.set_owner(channel, heir)
        if removed:
            log.info("temp voice: removed %d empty channels at startup", removed)

    # ------------------------------------------------------------ /squad
    async def my_channel(self, interaction: discord.Interaction, action: str):
        """The caller's temp channel if they may run `action` there; otherwise replies and returns None."""
        voice = getattr(interaction.user, "voice", None)
        channel = voice.channel if voice else None
        owner = self.owners.get(channel.id) if channel else None
        denied = T.authorize(action, user_id=interaction.user.id, owner_id=owner,
                             owner_present=channel is not None and owner in channel.voice_states)
        if denied is not None:
            await interaction.response.send_message(DENIED[denied].format(owner=owner), ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return None
        return channel

    @squad.command(name="name", description="Rename your squad channel")
    @app_commands.describe(text="The new name")
    async def squad_name(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 100]) -> None:
        channel = await self.my_channel(interaction, "name")
        if channel is None:
            return
        name = T.clean_rename(text)
        if name is None:
            await interaction.response.send_message("Pick a different name.", ephemeral=True)
            return
        wait = self.renames.wait(channel.id, now())
        if wait > 0:
            await interaction.response.send_message(
                f"Discord only lets a channel be renamed twice every 10 minutes. Try again in {T.minutes(wait)}.",
                ephemeral=True)
            return
        self.renames.record(channel.id, now())  # before the await: two quick clicks can't both pass
        await interaction.response.defer(ephemeral=True, thinking=True)
        await channel.edit(name=name, reason=f"/squad name by {interaction.user.id}")
        await interaction.followup.send(f"Renamed to **{discord.utils.escape_markdown(name)}**.", ephemeral=True)

    @squad.command(name="limit", description="Set how many people can join your squad channel (0 = no limit)")
    @app_commands.describe(size="Max people, 0 for no limit")
    async def squad_limit(self, interaction: discord.Interaction, size: app_commands.Range[int, 0, 99]) -> None:
        channel = await self.my_channel(interaction, "limit")
        if channel is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await channel.edit(user_limit=size, reason=f"/squad limit by {interaction.user.id}")
        await interaction.followup.send("Limit removed." if size == 0 else f"Limit set to {size}.", ephemeral=True)

    @squad.command(name="claim", description="Take over this squad channel if its owner has left")
    async def squad_claim(self, interaction: discord.Interaction) -> None:
        channel = await self.my_channel(interaction, "claim")
        if channel is None:
            return
        await self.set_owner(channel, interaction.user.id)
        await interaction.response.send_message("This channel is yours now (`/squad name`, `/squad limit`).",
                                                ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(TempVoice(bot))
