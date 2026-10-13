"""Focus: keeps two event-driven channels in the Onboarding default list only while their
event is on (rules in logic/focus.py). No commands, nothing members interact with.

- tournaments channel: while a tournament is open for signups or running (the tournaments table)
- realms channel: while the Minecraft server is online (Realms.is_online())

Once a minute it looks at those two facts, which costs no Discord call. It reads the
Onboarding settings only at startup, when one of the facts changes, and once an hour; it
edits them only when a channel has to be added or removed, at most once per channel every
15 minutes, and all changes of one pass go out as a single edit. Each change is one line in
the mod log. It never edits a channel or a permission, and the only default channels it adds
or removes are the ones in logic/focus.py EVENT_CHANNELS. A missing channel, Onboarding
being off or a refused edit is logged and retried later.
"""

import logging
import time

import discord
from discord.ext import commands, tasks

import config
import style
from logic import focus as F

log = logging.getLogger(__name__)
NO_PINGS = discord.AllowedMentions.none()
UNSET = object()


def now() -> int:
    return int(time.time())


class Focus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_wants: object = UNSET  # the facts as of the last pass
        self.next_check_at = 0
        self.changed_at: dict[str, int] = {}  # channel name -> when this cog last changed it
        self.said: set[str] = set()

    async def cog_load(self) -> None:
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    @property
    def db(self):
        return self.bot.db

    def guild(self):
        return self.bot.get_guild(self.bot.settings.guild_id)

    def once(self, key: str, text: str, *args) -> None:
        if key not in self.said:
            self.said.add(key)
            log.warning(text, *args)

    # ------------------------------------------------------------ the two facts
    async def live(self, source: str) -> bool | None:
        """True/False, or None when the module that knows isn't loaded or can't say yet."""
        if source == F.TOURNAMENT:
            if self.bot.get_cog("Tournaments") is None:
                return None
            marks = ",".join("?" * len(F.TOURNAMENT_LIVE))
            row = await self.db.fetchone(f"SELECT 1 FROM tournaments WHERE status IN ({marks}) LIMIT 1",
                                         F.TOURNAMENT_LIVE)
            return row is not None
        realms = self.bot.get_cog("Realms")
        return realms.is_online() if realms is not None else None

    async def wants(self) -> dict[str, bool | None]:
        return {name: await self.live(source) for name, source in F.EVENT_CHANNELS.items()}

    # ------------------------------------------------------------ markers
    async def marked(self, name: str) -> bool:
        return await self.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (F.marker_key(name),)) is not None

    async def mark(self, name: str, on: bool) -> None:
        if on:
            await self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                  (F.marker_key(name), str(now())))
        else:
            await self.db.execute("DELETE FROM meta WHERE key = ?", (F.marker_key(name),))

    async def mod_log(self, line: str) -> None:
        guild = self.guild()
        channel = config.match_by_name(guild.text_channels, config.MOD_LOG_CHANNEL) if guild is not None else None
        if channel is None:
            return
        try:
            await channel.send(embed=style.embed(description=line), allowed_mentions=NO_PINGS)
        except Exception:
            log.warning("focus: couldn't write to the mod log", exc_info=True)

    # ------------------------------------------------------------ one pass
    async def run_once(self) -> None:
        """Look at the facts; talk to Discord only when they changed or the hourly check is due."""
        t = now()
        wants = await self.wants()
        if wants == self.last_wants and t < self.next_check_at:
            return
        self.next_check_at = t + await self.reconcile(wants, t)
        self.last_wants = wants

    async def reconcile(self, wants: dict[str, bool | None], t: int) -> int:
        """Make the defaults match. Returns the seconds until the next look is worth it."""
        guild = self.guild()
        if guild is None:
            return F.RETRY
        try:
            ob = await guild.onboarding()
        except discord.HTTPException:
            self.once("read", "focus: couldn't read the Onboarding settings; trying again later")
            return F.RETRY
        self.said.discard("read")
        if not ob.enabled:
            return F.RECHECK
        default_ids = {int(i) for i in ob.default_channel_ids}
        opt_in = [c for c in (config.match_by_name(guild.channels, n) for n in F.opt_in_names()) if c is not None]
        is_focused = F.focused(default_ids, [c.id for c in opt_in])
        actions: dict[int, str] = {}
        todo: list[tuple[str, str, str]] = []  # (channel name, source, action)
        wait = F.RECHECK
        for name, source in F.EVENT_CHANNELS.items():
            channel = config.match_by_name(guild.channels, name)
            if channel is None:
                continue  # not made yet: nothing to show
            action = F.decide(wants.get(name), channel.id in default_ids, await self.marked(name), is_focused)
            if action == F.FORGET:
                await self.mark(name, False)
            elif F.held(action, self.changed_at.get(name), t):
                wait = min(wait, F.HOLD - (t - self.changed_at[name]))
            elif action in (F.ADD, F.REMOVE):
                actions[channel.id] = action
                todo.append((name, source, action))
        new_ids = F.apply_actions(default_ids, actions)
        if new_ids is None:
            return wait
        try:
            await guild.edit_onboarding(default_channels=[discord.Object(id=i) for i in new_ids],
                                        reason="Focus: event-driven default channels")
        except discord.HTTPException as e:
            self.once("edit", "focus: Discord refused the default-channel edit (%s); trying again later",
                      getattr(e, "text", None) or e)
            return F.RETRY
        self.said.discard("edit")
        for name, source, action in todo:
            await self.mark(name, action == F.ADD)
            self.changed_at[name] = t
            log.info("focus: %s %s (%s)", action, name, source)
            await self.mod_log(F.log_line(name, source, action))
        return wait

    @tasks.loop(seconds=F.TICK)
    async def tick(self) -> None:
        try:
            await self.run_once()
        except Exception:
            log.exception("focus: pass failed")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(Focus(bot))
