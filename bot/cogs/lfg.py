"""Module 1: Squad-up. /lfg posts in the 🎮・lfg forum with Join / Leave / Close
buttons. Rosters live in SQLite and buttons are DynamicItems, so both survive
restarts."""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import style
from errors import reply_error
from logic import lfg as rules
from logic.lfg import Join, Leave, Roster

log = logging.getLogger(__name__)

GAME_CHOICES = [app_commands.Choice(name=g.role, value=g.key) for g in config.GAMES]
MODE_CHOICES = [app_commands.Choice(name=m, value=m) for m in config.MODES]
PING_ROLES = discord.AllowedMentions(everyone=False, roles=True, users=False)
PING_USERS = discord.AllowedMentions(everyone=False, roles=False, users=True)

BUTTONS = {
    "join": ("Join", discord.ButtonStyle.success),
    "leave": ("Leave", discord.ButtonStyle.secondary),
    "close": ("Close", discord.ButtonStyle.secondary),
}

JOIN_REPLIES = {
    Join.ALREADY_IN: "You're already in this squad.",
    Join.FULL: "This squad is full. If someone leaves, a spot opens up.",
}
LEAVE_REPLIES = {
    Leave.NOT_IN: "You're not in this squad.",
    Leave.HOST: "You're the host, so use **Close** instead.",
}


def now() -> int:
    return int(time.time())


class LfgButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"lfg:(?P<action>join|leave|close):(?P<post>\d+)"):
    def __init__(self, action: str, post_id: int, disabled: bool = False):
        text, button_style = BUTTONS[action]
        super().__init__(discord.ui.Button(label=text, style=button_style, disabled=disabled,
                                           custom_id=f"lfg:{action}:{post_id}"))
        self.action = action
        self.post_id = post_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["post"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Lfg" = interaction.client.get_cog("Lfg")
        try:
            await cog.handle_button(interaction, self.action, self.post_id)
        except Exception:
            log.exception("lfg button %s on post %s failed", self.action, self.post_id)
            await reply_error(interaction)


def build_view(post_id: int, roster: Roster, closed: bool) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(LfgButton("join", post_id, disabled=closed or roster.full))
    view.add_item(LfgButton("leave", post_id, disabled=closed))
    view.add_item(LfgButton("close", post_id, disabled=closed))
    return view


class Lfg(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ready = False
        self.forum: discord.ForumChannel | None = None
        self.game_tags: dict[str, discord.ForumTag] = {}
        self.mode_tags: dict[str, discord.ForumTag] = {}
        self.game_roles: dict[str, discord.Role] = {}
        self.lfg_role: discord.Role | None = None
        self.keeper_role: discord.Role | None = None
        self.voice: dict[str, discord.VoiceChannel] = {}

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(LfgButton)
        self.expiry.start()

    async def cog_unload(self) -> None:
        self.expiry.cancel()
        self.bot.remove_dynamic_items(LfgButton)

    # ------------------------------------------------------------ setup
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self.resolve()

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after) -> None:
        if self.forum is not None and after.id == self.forum.id:
            self.resolve()  # tags may have changed

    def resolve(self) -> None:
        """Find the forum, tags, roles and voice channels by name; log what's missing."""
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        if guild is None:
            self.ready = False
            return
        self.forum = config.match_by_name(guild.forums, config.LFG_FORUM)
        if self.forum is None:
            self.ready = False
            log.warning("LFG disabled: no forum named %s", config.LFG_FORUM)
            return
        missing = []
        tags = self.forum.available_tags
        for game in config.GAMES:
            tag = config.match_by_name(tags, game.role)
            role = config.match_by_name(guild.roles, game.role)
            if tag:
                self.game_tags[game.key] = tag
            else:
                missing.append(f"tag '{game.role}'")
            if role:
                self.game_roles[game.key] = role
            else:
                missing.append(f"role @{game.role}")
        for mode in config.MODES:
            tag = config.match_by_name(tags, mode)
            if tag:
                self.mode_tags[mode] = tag
            else:
                missing.append(f"tag '{mode}'")
        self.lfg_role = config.match_by_name(guild.roles, config.LFG_ROLE)
        self.keeper_role = config.match_by_name(guild.roles, config.KEEPER_ROLE)
        for key, name in (("squad", config.SQUAD_VOICE), ("lobby", config.LOBBY_VOICE)):
            channel = config.match_by_name(guild.voice_channels, name)
            if channel:
                self.voice[key] = channel
        if self.lfg_role is None:
            missing.append(f"role @{config.LFG_ROLE}")
        if missing:
            log.warning("LFG works, but these weren't found (pings/tags skipped): %s", ", ".join(missing))
        self.ready = True

    def is_keeper(self, member: discord.Member) -> bool:
        if member.guild.owner_id == member.id or member.guild_permissions.administrator:
            return True
        return self.keeper_role is not None and self.keeper_role in member.roles

    # ------------------------------------------------------------ data
    async def load_roster(self, tx_or_db, post) -> Roster:
        rows = await tx_or_db.fetchall(
            "SELECT user_id FROM lfg_members WHERE post_id = ? ORDER BY user_id = ? DESC, joined_at, rowid",
            (post["id"], post["host_id"]),
        )
        return Roster(post["host_id"], post["size"], tuple(r["user_id"] for r in rows))

    async def get_post(self, post_id: int):
        return await self.bot.db.fetchone("SELECT * FROM lfg_posts WHERE id = ?", (post_id,))

    # ------------------------------------------------------------ rendering
    def render(self, post, roster: Roster, closed: bool = False) -> discord.Embed:
        game = config.game_by_key(post["game"])
        name = f"{game.emoji} {game.role}" if game else post["game"]
        lines = [f"**When** {post['when_text']}"]
        if post["mode"]:
            lines.append(f"**Mode** {post['mode']}")
        if post["note"]:
            lines.append(f"**Note** {post['note']}")
        lines += ["", f"**Squad {rules.count_label(roster)}**"]
        lines += [f"`{i:02}`  <@{uid}>" + ("  · host" if uid == roster.host_id else "")
                  for i, uid in enumerate(roster.members, start=1)]
        if not closed:
            lines += [f"`{i:02}`  · open" for i in range(len(roster.members) + 1, roster.size + 1)]
        status = "closed" if closed else ("full" if roster.full else "open")
        return style.embed(
            title=name,
            description="\n".join(lines),
            footer=style.label("squad-up", game.role if game else "", status),
            color=style.MUTED if closed else style.FOREST,
        )

    def pings(self, game: config.Game, host: discord.Member, roster: Roster, when: str) -> str:
        mentions = " ".join(r.mention for r in (self.game_roles.get(game.key), self.lfg_role) if r)
        need = roster.size - len(roster.members)
        return f"{mentions} {host.display_name} needs {need} more for **{game.role}** ({when})".strip()

    def tags_for(self, game_key: str, mode: str | None) -> list[discord.ForumTag]:
        return [t for t in (self.game_tags.get(game_key), self.mode_tags.get(mode)) if t]

    async def fetch_thread(self, thread_id: int) -> discord.Thread | None:
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        thread = guild.get_thread(thread_id) if guild else None
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except (discord.NotFound, discord.Forbidden):
                return None
        return thread

    # ------------------------------------------------------------ /lfg
    @app_commands.command(name="lfg", description="Find a squad: posts in the LFG forum with Join buttons")
    @app_commands.describe(
        game="Which game",
        players="Squad size, including you",
        mode="Ranked or Casual (optional)",
        when="When you're playing, e.g. now, 9pm, in 30 min",
        note="Anything else: rank, mic, mode",
    )
    @app_commands.choices(game=GAME_CHOICES, mode=MODE_CHOICES)
    async def lfg(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str],
        players: app_commands.Range[int, rules.MIN_PLAYERS, rules.MAX_PLAYERS],
        mode: app_commands.Choice[str] | None = None,
        when: app_commands.Range[str, 1, 40] = "now",
        note: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        if not self.ready:
            await interaction.response.send_message(
                f"Squad-up isn't available: I couldn't find the {config.LFG_FORUM} forum.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        g = config.game_by_key(game.value)
        mode_value = mode.value if mode else None
        host = interaction.user

        existing = await self.bot.db.fetchone(
            "SELECT * FROM lfg_posts WHERE host_id = ? AND game = ? AND closed_at IS NULL",
            (host.id, g.key),
        )
        if existing:
            await self.update_post(interaction, existing, players, mode_value, when, note)
            return

        created = now()
        async with self.bot.db.transaction() as tx:
            cur = await tx.execute(
                "INSERT INTO lfg_posts (game, host_id, size, mode, when_text, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (g.key, host.id, players, mode_value, when, note, created),
            )
            post_id = cur.lastrowid
            await tx.execute("INSERT INTO lfg_members VALUES (?, ?, ?)", (post_id, host.id, created))
            post = await tx.fetchone("SELECT * FROM lfg_posts WHERE id = ?", (post_id,))

        roster = Roster.new(host.id, players)
        try:
            made = await self.forum.create_thread(
                name=rules.title(g.role, mode_value),
                content=self.pings(g, host, roster, when),
                embed=self.render(post, roster),
                view=build_view(post_id, roster, closed=False),
                applied_tags=self.tags_for(g.key, mode_value),
                allowed_mentions=PING_ROLES,
            )
        except discord.HTTPException:
            await self.bot.db.execute("DELETE FROM lfg_posts WHERE id = ?", (post_id,))
            raise
        await self.bot.db.execute(
            "UPDATE lfg_posts SET thread_id = ?, message_id = ? WHERE id = ?",
            (made.thread.id, made.message.id, post_id),
        )
        await interaction.followup.send(f"Posted: {made.thread.jump_url}", ephemeral=True)

    async def update_post(self, interaction, post, players, mode, when, note) -> None:
        """Re-running /lfg for a game you already have open edits that post."""
        game = config.game_by_key(post["game"])
        roster = await self.load_roster(self.bot.db, post)
        resized = roster.resize(players)
        if resized is None:
            await interaction.followup.send(
                f"Your {game.role} squad already has {len(roster.members)} people, "
                f"so pick at least {len(roster.members)}.", ephemeral=True)
            return
        await self.bot.db.execute(
            "UPDATE lfg_posts SET size = ?, mode = ?, when_text = ?, note = ? WHERE id = ?",
            (players, mode, when, note, post["id"]),
        )
        post = await self.get_post(post["id"])
        thread = await self.fetch_thread(post["thread_id"])
        if thread is None:
            await self.bot.db.execute("UPDATE lfg_posts SET closed_at = ? WHERE id = ?", (now(), post["id"]))
            await interaction.followup.send("Your old post was deleted, so run /lfg again for a new one.",
                                            ephemeral=True)
            return
        message = thread.get_partial_message(post["message_id"])
        await message.edit(embed=self.render(post, resized), view=build_view(post["id"], resized, closed=False))
        if rules.title(game.role, mode) != thread.name:
            # Mode changed: the one case that renames (rare, so the rate limit doesn't bite).
            await thread.edit(name=rules.title(game.role, mode), applied_tags=self.tags_for(game.key, mode))
        await interaction.followup.send(f"Updated your {game.role} post: {thread.jump_url}", ephemeral=True)

    # ------------------------------------------------------------ buttons
    async def handle_button(self, interaction: discord.Interaction, action: str, post_id: int) -> None:
        post = await self.get_post(post_id)
        if post is None or post["closed_at"] is not None:
            await interaction.response.send_message("This squad is closed.", ephemeral=True)
            return
        user = interaction.user

        if action == "close":
            if not rules.can_close(user.id, post["host_id"], self.is_keeper(user)):
                await interaction.response.send_message("Only the host or a Keeper can close this.",
                                                        ephemeral=True)
                return
            await interaction.response.defer()
            await self.close_post(post)
            return

        became_full = False
        async with self.bot.db.transaction() as tx:
            roster = await self.load_roster(tx, post)
            if action == "join":
                roster, result, became_full = roster.join(user.id)
                reply = JOIN_REPLIES.get(result)
                if result is Join.JOINED:
                    await tx.execute("INSERT INTO lfg_members VALUES (?, ?, ?)", (post_id, user.id, now()))
            else:
                roster, result = roster.leave(user.id)
                reply = LEAVE_REPLIES.get(result)
                if result is Leave.LEFT:
                    await tx.execute("DELETE FROM lfg_members WHERE post_id = ? AND user_id = ?",
                                     (post_id, user.id))

        if reply:
            await interaction.response.send_message(reply, ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.render(post, roster),
                                                view=build_view(post_id, roster, closed=False))
        if became_full:
            await self.announce_full(interaction.channel, roster)

    async def announce_full(self, thread, roster: Roster) -> None:
        voice = self.voice.get(rules.voice_hint(roster.size))
        where = f", hop in {voice.mention}" if voice else ""
        mentions = " ".join(f"<@{uid}>" for uid in roster.members)
        await thread.send(f"Squad's full: {mentions}{where}", allowed_mentions=PING_USERS)
        # Module 4 hooks in here to pay each member (ref lfg:<post>:<user>).
        self.bot.dispatch("lfg_squad_full", thread, roster)

    # ------------------------------------------------------------ closing
    async def close_post(self, post) -> None:
        changed = await self.bot.db.execute(
            "UPDATE lfg_posts SET closed_at = ? WHERE id = ? AND closed_at IS NULL", (now(), post["id"]))
        if not changed:
            return  # someone else closed it first
        thread = await self.fetch_thread(post["thread_id"]) if post["thread_id"] else None
        if thread is None:
            return
        roster = await self.load_roster(self.bot.db, post)
        try:
            # Turn the buttons off before archiving: archived threads can't be edited.
            await thread.get_partial_message(post["message_id"]).edit(
                embed=self.render(post, roster, closed=True), view=build_view(post["id"], roster, closed=True))
            await thread.edit(name=rules.closed_title(thread.name), locked=True, archived=True)
        except discord.HTTPException:
            log.warning("couldn't fully close LFG post %s (thread %s)", post["id"], post["thread_id"], exc_info=True)

    @tasks.loop(minutes=5)
    async def expiry(self) -> None:
        try:
            current = now()
            for post in await self.bot.db.fetchall("SELECT * FROM lfg_posts WHERE closed_at IS NULL"):
                if rules.is_expired(post["created_at"], current):
                    await self.close_post(post)
        except Exception:
            log.exception("LFG expiry check failed")

    @expiry.before_loop
    async def before_expiry(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Lfg(bot))
