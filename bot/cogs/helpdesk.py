"""Help desk: /help, /about and the rotating presence.

/help is built from the bot's live command tree every time it's opened, so it never goes stale:
an ephemeral embed with a category select menu, a "Start here" button (the starter quest and the
roles channel), and /help command:<name> for one command's options. Staff commands only show for
members with Timeout Members. The menu is a short-lived View (the reply is ephemeral, so a
restart just means running /help again).

The presence rotates every few minutes: member count, /queue, /help and today's Daily Word."""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from logic import helpdesk as H
from logic import wordgame as W

log = logging.getLogger(__name__)

IDS_TTL = 60 * 60  # re-read command ids (for clickable mentions) at most hourly
IDS_RETRY = 10 * 60
HOME = "home"
NO_PINGS = discord.AllowedMentions.none()


def now() -> int:
    return int(time.time())


def is_staff(user) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms is not None and (getattr(perms, "moderate_members", False)
                                       or getattr(perms, "administrator", False)))


def activity(kind: str, text: str) -> discord.BaseActivity:
    if kind == "playing":
        return discord.Game(name=text)
    kinds = {"watching": discord.ActivityType.watching, "listening": discord.ActivityType.listening}
    return discord.Activity(type=kinds.get(kind, discord.ActivityType.playing), name=text)


# ------------------------------------------------------------ embeds

def home_embed(entries) -> discord.Embed:
    total = len(H.leaves(entries))
    e = style.embed(
        title="Front Desk help",
        description=(f"{total} commands, by category. Pick one below, or run `/help command:<name>` "
                     "for one command's options.\nNew here? Press **Start here**."),
        footer=style.label("help", "overview"))
    for name, value in H.overview_rows(entries):
        e.add_field(name=name, value=value, inline=False)
    return e


def category_embed(key: str, entries, ids=None) -> discord.Embed:
    cat = H.BY_KEY.get(key) or H.BY_KEY[H.FALLBACK]
    return style.embed(title=f"{cat.emoji} {cat.title}", description=H.category_text(key, entries, ids),
                       footer=style.label("help", cat.title))


def detail_embed(entry: H.Entry, ids=None) -> discord.Embed:
    e = style.embed(title=H.usage(entry), description=H.clip("\n".join(H.detail_lines(entry, ids))),
                    footer=style.label("help", entry.name))
    return e


class HelpView(discord.ui.View):
    """Category select + Start here, for one person's ephemeral /help."""

    def __init__(self, cog: "Helpdesk", entries, ids=None, current: str = HOME):
        super().__init__(timeout=H.VIEW_TIMEOUT)
        self.cog = cog
        self.entries = entries
        self.ids = ids or {}
        self.pick.options = self.options(current)

    def options(self, current: str) -> list[discord.SelectOption]:
        counts = {k: len(v) for k, v in H.by_category(self.entries).items()}
        opts = [discord.SelectOption(label="Overview", value=HOME, emoji="🏠", default=current == HOME,
                                     description="Every category at a glance")]
        for cat in H.CATEGORIES:
            if cat.key in counts:
                opts.append(discord.SelectOption(
                    label=cat.title, value=cat.key, emoji=cat.emoji, default=current == cat.key,
                    description=f"{counts[cat.key]} commands · {cat.blurb}"[:100]))
        return opts[:H.MAX_CHOICES]

    def embed_for(self, key: str) -> discord.Embed:
        return home_embed(self.entries) if key == HOME else category_embed(key, self.entries, self.ids)

    @discord.ui.select(placeholder="Pick a category", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        key = select.values[0]
        self.pick.options = self.options(key)
        await interaction.response.edit_message(embed=self.embed_for(key), view=self)

    @discord.ui.button(label="Start here", emoji="🧭", style=discord.ButtonStyle.success)
    async def start_here(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.send_start_here(interaction)


# ------------------------------------------------------------ cog

class Helpdesk(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tick = 0
        self.ids: dict[str, int] = {}
        self.ids_checked = 0
        self.ids_ok = False

    async def cog_load(self) -> None:
        self.presence.start()

    async def cog_unload(self) -> None:
        self.presence.cancel()

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def entries_for(self, user) -> list[H.Entry]:
        return H.visible(H.collect(self.bot.tree.get_commands()), is_staff(user))

    async def command_ids(self) -> dict[str, int]:
        """Top-level command name -> id, for clickable mentions. Best effort; falls back to `/name`."""
        age = now() - self.ids_checked
        if age < (IDS_TTL if self.ids_ok else IDS_RETRY) and self.ids_checked:
            return self.ids
        self.ids_checked = now()
        target = getattr(self.bot, "guild_ref", None) or discord.Object(id=self.bot.settings.guild_id)
        try:
            fetched = await self.bot.tree.fetch_commands(guild=target)
        except (discord.HTTPException, AttributeError, TypeError):
            log.warning("helpdesk: couldn't fetch command ids; /help shows plain names", exc_info=True)
            self.ids_ok = False
            return self.ids
        self.ids = {c.name: c.id for c in fetched}
        self.ids_ok = True
        return self.ids

    # ------------------------------------------------------------ /help
    @app_commands.command(name="help", description="Every command, by category (or details on one)")
    @app_commands.describe(command="One command to explain, like: word guess")
    async def help(self, interaction: discord.Interaction, command: str | None = None) -> None:
        entries = self.entries_for(interaction.user)
        ids = await self.command_ids()
        if command:
            entry = H.find(entries, command)
            if entry is not None:
                await interaction.response.send_message(embed=detail_embed(entry, ids), ephemeral=True)
                return
            shown = H.normalize(command).replace("`", "")[:80]  # inside a code span, so no other escaping
            note = f"No command called `/{shown}`. Here's everything:"
        else:
            note = None
        await interaction.response.send_message(content=note, embed=home_embed(entries),
                                                view=HelpView(self, entries, ids), ephemeral=True,
                                                allowed_mentions=NO_PINGS)

    @help.autocomplete("command")
    async def help_autocomplete(self, interaction: discord.Interaction, current: str):
        entries = self.entries_for(interaction.user)
        return [app_commands.Choice(name=f"/{e.name} · {e.description}"[:100], value=e.name[:100])
                for e in H.suggest(entries, current)]

    async def send_start_here(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        quests = self.bot.get_cog("Quests")
        embed = None
        if quests is not None:
            try:
                await quests.check(member)
            except Exception:
                log.exception("helpdesk: quest refresh for %s failed", member.id)
            try:
                embed = await quests.quest_embed(member)
            except Exception:
                log.exception("helpdesk: quest card for %s failed", member.id)
        if embed is None:
            embed = style.embed(title="Starter quest",
                                description="Run `/quest` to see your first steps on the server.",
                                footer=style.label("quest"))
        guild = interaction.guild or self.guild()
        roles = config.match_by_name(guild.text_channels, config.ROLES_CHANNEL) if guild else None
        view = discord.ui.View(timeout=H.VIEW_TIMEOUT)
        if roles is not None:
            content = f"**Start here:** pick your games and pings in {roles.mention}, then work through your quest."
            view.add_item(discord.ui.Button(label="Pick your roles", emoji="🎭", url=roles.jump_url))
        else:
            content = "**Start here:** run `/roles` to pick your games and pings, then work through your quest."
        view.add_item(discord.ui.Button(label="Website", emoji="🌐", url=H.SITE_URL))
        await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=True,
                                                allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /about
    @app_commands.command(name="about", description="About this server: members, boosts, age and links")
    @app_commands.guild_only()
    async def about(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        members = guild.member_count or len(guild.members)
        online = None
        try:
            full = await self.bot.fetch_guild(guild.id, with_counts=True)
            members = full.approximate_member_count or members
            online = full.approximate_presence_count
        except (discord.HTTPException, AttributeError):
            log.warning("helpdesk: couldn't fetch member counts; using the cache", exc_info=True)
        if online is None:
            online = sum(1 for m in guild.members if getattr(m, "status", discord.Status.offline)
                         != discord.Status.offline)
        created = guild.created_at
        today = discord.utils.utcnow().date()
        ts = int(created.timestamp())
        text = len(guild.text_channels) + len(getattr(guild, "forums", []))
        voice = len(guild.voice_channels) + len(getattr(guild, "stage_channels", []))
        e = style.embed(title=guild.name, description=guild.description or None, footer=style.label("about"))
        if guild.icon:
            e.set_thumbnail(url=guild.icon.url)
        e.add_field(name="Members", value=f"{members:,}", inline=True)
        e.add_field(name="Online now", value=f"{online:,}", inline=True)
        e.add_field(name="Boosts", value=H.boost_text(guild.premium_subscription_count or 0,
                                                       int(guild.premium_tier or 0)), inline=True)
        e.add_field(name="Channels", value=f"{text} text · {voice} voice", inline=True)
        e.add_field(name="Around for", value=f"{H.age_text(created.date(), today)} (since <t:{ts}:D>)", inline=True)
        e.add_field(name="Links", value=f"[Website]({H.SITE_URL}) · [Invite a friend]({H.INVITE_URL})", inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Website", emoji="🌐", url=H.SITE_URL))
        view.add_item(discord.ui.Button(label="Invite a friend", emoji="✉️", url=H.INVITE_URL))
        await interaction.response.send_message(embed=e, view=view, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ presence
    async def rotate(self) -> None:
        guild = self.guild()
        members = (guild.member_count or len(guild.members)) if guild is not None else 0
        word_no = W.puzzle_number(W.local_day(now(), self.bot.settings.tz))
        kind, text = H.presence_at(self.tick, members, word_no)
        self.tick += 1
        await self.bot.change_presence(activity=activity(kind, text))

    @tasks.loop(minutes=H.PRESENCE_MINUTES)
    async def presence(self) -> None:
        try:
            await self.rotate()
        except Exception:
            log.exception("helpdesk: presence update failed")

    @presence.before_loop
    async def before_presence(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Helpdesk(bot))
