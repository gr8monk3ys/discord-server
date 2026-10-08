"""Self-assign roles: a panel in 🎭・roles (platform, region, play time, pings, games) and
/roles, which shows the same panel just to you.

The bot posts the panel once and edits it in place on every start (its message id is in
meta, with a history scan as a fallback), so the channel never fills with copies. Buttons
and the games select are DynamicItems, so they keep working after a restart. Their
custom_ids hold only a section key and an index into the lists in config; the role is
always looked up from that list, and only roles below Front Desk with no staff powers can
be toggled. Roles that don't exist (yet) are left off the panel.

No privileged intents: the member and their roles come with the interaction."""

import asyncio
import functools
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import style
from errors import reply_error
from logic import selfroles as rules

log = logging.getLogger(__name__)

PANEL_KEY = "selfroles:panel"  # meta: "channel_id:message_id" of the panel post
PREFIX = "selfroles:"
REFRESH_DELAY = 10  # seconds: role edits come in bursts, so the panel is edited once after them
HISTORY_SCAN = 50  # how far back to look for an old panel when the meta row is missing
NO_PINGS = discord.AllowedMentions.none()
REASON = "Self-assign (#roles panel)"
GAME_EMOJI = {g.role: g.emoji for g in config.GAMES}


def never_raise(fn):
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        try:
            await fn(self, *args, **kwargs)
        except Exception:
            log.exception("selfroles: %s failed", fn.__name__)
    return wrapper


class RoleButton(discord.ui.DynamicItem[discord.ui.Button],
                 template=PREFIX + r"(?P<section>platform|region|playtime|pings):(?P<idx>\d{1,2})"):
    def __init__(self, key: str, index: int, label: str = "?", row: int | None = None):
        super().__init__(discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary,
                                           custom_id=f"{PREFIX}{key}:{index}"))
        self.row = row
        self.key = key
        self.index = index

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["section"], int(match["idx"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "SelfRoles" = interaction.client.get_cog("SelfRoles")
        try:
            await cog.handle_button(interaction, self.key, self.index)
        except Exception:
            log.exception("selfroles button %s:%s failed", self.key, self.index)
            await reply_error(interaction)


class GameSelect(discord.ui.DynamicItem[discord.ui.Select], template=PREFIX + r"games"):
    def __init__(self, options=None):
        options = options or [discord.SelectOption(label="(none)", value="-1")]
        super().__init__(discord.ui.Select(custom_id=f"{PREFIX}games", placeholder="Games: pick to add or remove",
                                           min_values=1, max_values=len(options), options=options))
        # Not placed by row: a Select's `row` slot shadows the one DynamicItem reads, so the
        # view places it automatically in the first free row (it always comes last).

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "SelfRoles" = interaction.client.get_cog("SelfRoles")
        try:
            values = list(getattr(self.item, "values", None) or (interaction.data or {}).get("values", []))
            await cog.handle_select(interaction, values)
        except Exception:
            log.exception("selfroles games select failed")
            await reply_error(interaction)


def is_dangerous(role) -> bool:
    perms = getattr(role, "permissions", None)
    return perms is not None and any(getattr(perms, p, False) for p in rules.DANGEROUS)


def assignable(guild, role) -> bool:
    me = getattr(guild, "me", None)
    if me is None or role is None:
        return False
    if not getattr(me.guild_permissions, "manage_roles", False):
        return False
    return rules.can_assign(position=role.position, bot_top=me.top_role.position,
                            managed=bool(getattr(role, "managed", False)),
                            is_default=role.id == guild.id, dangerous=is_dangerous(role))


def available(guild) -> dict[str, dict[int, discord.Role]]:
    """section key -> {index: role} for every listed role that exists and can be handed out."""
    out = {}
    for sec in rules.sections():
        found = {}
        for i, name in enumerate(sec.names):
            role = config.match_by_name(guild.roles, name)
            if role is not None and assignable(guild, role):
                found[i] = role
        out[sec.key] = found
    return out


def build_view(avail: dict[str, dict[int, discord.Role]]) -> discord.ui.View | None:
    view = discord.ui.View(timeout=None)
    row = 0
    for sec in rules.sections():
        found = avail.get(sec.key) or {}
        if not found:
            continue
        if sec.select:
            options = [discord.SelectOption(label=sec.names[i][:100], value=str(i),
                                            emoji=GAME_EMOJI.get(sec.names[i]))
                       for i in sorted(found)]
            view.add_item(GameSelect(options))
        else:
            for i in sorted(found):
                view.add_item(RoleButton(sec.key, i, label=sec.names[i], row=row))
        row += 1
    return view if row else None


def build_embed(avail: dict[str, dict[int, discord.Role]]) -> discord.Embed:
    lines = ["Tap a button to add a role, tap it again to take it off. Only you see the reply.", ""]
    for sec in rules.sections():
        found = avail.get(sec.key) or {}
        if not found:
            continue
        roles = " ".join(found[i].mention for i in sorted(found))
        lines.append(f"**{sec.title}**  {discord.utils.escape_markdown(sec.hint)}")
        lines.append(roles)
        lines.append("")
    return style.embed(title="Pick your roles", description="\n".join(lines).strip(),
                       footer=style.label("roles", "self-assign"))


def signature(avail) -> tuple:
    return tuple((k, tuple(sorted((i, r.id) for i, r in v.items()))) for k, v in sorted(avail.items()))


class SelfRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_signature = None  # what the panel showed after the last successful edit
        self.refresh_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RoleButton, GameSelect)

    async def cog_unload(self) -> None:
        if self.refresh_task is not None:
            self.refresh_task.cancel()
        self.bot.remove_dynamic_items(RoleButton, GameSelect)

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    async def meta_get(self, key: str) -> str | None:
        row = await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    async def meta_set(self, key: str, value: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------ panel upkeep
    @commands.Cog.listener()
    @never_raise
    async def on_ready(self) -> None:
        guild = self.guild()
        if guild is not None:
            await self.refresh_panel(guild)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role) -> None:
        self.schedule_refresh()

    @commands.Cog.listener()
    async def on_guild_role_update(self, before, after) -> None:
        self.schedule_refresh()

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role) -> None:
        self.schedule_refresh()

    def schedule_refresh(self) -> None:
        try:
            if self.refresh_task is not None and not self.refresh_task.done():
                return
            self.refresh_task = asyncio.get_running_loop().create_task(self.delayed_refresh())
        except Exception:
            log.exception("selfroles: scheduling a panel refresh failed")

    @never_raise
    async def delayed_refresh(self) -> None:
        await asyncio.sleep(REFRESH_DELAY)
        guild = self.guild()
        if guild is not None:
            await self.refresh_panel(guild)

    async def find_old_panel(self, channel):
        """The bot's own earlier panel in the channel, if the meta row was lost."""
        me = getattr(channel.guild, "me", None)
        async for msg in channel.history(limit=HISTORY_SCAN):
            if me is None or msg.author.id != me.id:
                continue
            for row in getattr(msg, "components", []) or []:
                for child in getattr(row, "children", []) or []:
                    if str(getattr(child, "custom_id", "") or "").startswith(PREFIX):
                        return msg
        return None

    async def refresh_panel(self, guild) -> str:
        """Post or edit the panel. Returns what happened: missing / skipped / edited / posted / failed."""
        channel = config.match_by_name(guild.text_channels, config.ROLES_CHANNEL)
        if channel is None:
            log.warning("selfroles: no %s channel, panel skipped", config.ROLES_CHANNEL)
            return "missing"
        avail = available(guild)
        sig = signature(avail)
        view = build_view(avail)
        if view is None:
            log.warning("selfroles: none of the self-assign roles exist below Front Desk yet")
        embed = build_embed(avail)
        kwargs = dict(embed=embed, view=view, allowed_mentions=NO_PINGS)

        ref = rules.parse_panel_ref(await self.meta_get(PANEL_KEY))
        if ref is not None and ref[0] == channel.id and sig == self.last_signature:
            return "skipped"
        target = None
        if ref is not None and ref[0] == channel.id:
            target = channel.get_partial_message(ref[1])
        else:
            try:
                target = await self.find_old_panel(channel)
            except discord.HTTPException:
                log.warning("selfroles: couldn't read %s history", config.ROLES_CHANNEL, exc_info=True)
        if target is not None:
            try:
                await target.edit(**kwargs)
                await self.meta_set(PANEL_KEY, rules.panel_ref(channel.id, target.id))
                self.last_signature = sig
                return "edited"
            except discord.NotFound:
                pass  # deleted: post a fresh one
            except discord.HTTPException:
                log.warning("selfroles: editing the panel failed", exc_info=True)
                return "failed"
        try:
            sent = await channel.send(**kwargs)
        except discord.HTTPException:
            log.warning("selfroles: posting the panel failed", exc_info=True)
            return "failed"
        await self.meta_set(PANEL_KEY, rules.panel_ref(channel.id, sent.id))
        self.last_signature = sig
        log.info("selfroles: posted the panel in %s", config.ROLES_CHANNEL)
        return "posted"

    # ------------------------------------------------------------ /roles
    @app_commands.command(name="roles", description="Pick your platform, region, pings and game roles")
    @app_commands.guild_only()
    async def roles(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or guild.id != self.bot.settings.guild_id:
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        avail = available(guild)
        view = build_view(avail)
        if view is None:
            await interaction.response.send_message("No self-assign roles are set up yet. Ask a mod.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(embed=build_embed(avail), view=view, ephemeral=True,
                                                allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ clicks
    def context(self, interaction):
        guild = interaction.guild
        member = interaction.user
        if guild is None or guild.id != self.bot.settings.guild_id or not hasattr(member, "roles"):
            return None, None
        return guild, member

    async def handle_button(self, interaction: discord.Interaction, key: str, index: int) -> None:
        guild, member = self.context(interaction)
        if guild is None:
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        name = rules.name_at(key, index)
        sec = rules.section(key)
        found = available(guild).get(key, {})
        if name is None or index not in found:
            await interaction.response.send_message("That role isn't available right now.", ephemeral=True)
            return
        by_name = {sec.names[i]: r for i, r in found.items()}
        have = {n for n, r in by_name.items() if r in member.roles}
        change = rules.toggle(sec, name, have, available=set(by_name))
        await self.apply(interaction, member, change, by_name)

    async def handle_select(self, interaction: discord.Interaction, values) -> None:
        guild, member = self.context(interaction)
        if guild is None:
            await interaction.response.send_message("Use this in the server.", ephemeral=True)
            return
        sec = rules.section("games")
        found = available(guild).get("games", {})
        by_name = {sec.names[i]: r for i, r in found.items()}
        picked = [n for n in rules.select_values(sec, values) if n in by_name]
        if not picked:
            await interaction.response.send_message("Those roles aren't available right now.", ephemeral=True)
            return
        have = {n for n, r in by_name.items() if r in member.roles}
        await self.apply(interaction, member, rules.select(sec, picked, have), by_name)

    async def apply(self, interaction, member, change: rules.Change, by_name) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if change.remove:
                await member.remove_roles(*(by_name[n] for n in change.remove), reason=REASON)
            if change.add:
                await member.add_roles(*(by_name[n] for n in change.add), reason=REASON)
        except discord.Forbidden:
            log.warning("selfroles: missing permission to change %s", change)
            await interaction.followup.send("I can't change that role right now. Ask a mod.", ephemeral=True)
            return
        await interaction.followup.send(rules.describe(change), ephemeral=True, allowed_mentions=NO_PINGS)


async def setup(bot) -> None:
    await bot.add_cog(SelfRoles(bot))
