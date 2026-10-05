"""Module 4c: the coin shop and monthly seasons.

/shop and /buy spend coins on timed perks (a personal name colour, the Hype role, a
shoutout in general). The Discord side of a purchase happens first; the price is then
taken with economy.apply_tx in the same transaction that writes the perk row, so a
failed purchase never charges (and a purchase that can't be paid is undone). All perk
state lives in the `perks` table and an expiry loop removes perks when they run out,
so restarts lose nothing.

/season ranks coins earned this local month from activity (not gambling, gifts or
the shop). When a month ends, the top 3 are announced, get the Season Champ role and
season bonuses (ledger refs make that idempotent); the `seasons` row is written only
after the post succeeds, so a Discord error retries on the next tick."""

import asyncio
import json
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import economy as E
import style
from cogs.lfg import ping_only
from logic import shop as S

log = logging.getLogger(__name__)

ITEM_CHOICES = [app_commands.Choice(name=f"{i.name} ({i.price:,} coins)", value=i.key) for i in S.ITEMS.values()]
MEDALS = ("🥇", "🥈", "🥉")


def now() -> int:
    return int(time.time())


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}  # per member: one purchase/expiry at a time

    async def cog_load(self) -> None:
        self.expiry.start()
        self.seasons.start()

    async def cog_unload(self) -> None:
        self.expiry.cancel()
        self.seasons.cancel()

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def lock(self, user_id: int) -> asyncio.Lock:
        return self.locks.setdefault(user_id, asyncio.Lock())

    @staticmethod
    def manageable(guild, role) -> bool:
        me = getattr(guild, "me", None)
        return role is not None and me is not None and me.top_role > role

    @staticmethod
    def staff_colors(guild) -> list[int]:
        roles = [config.match_by_name(guild.roles, n) for n in (config.KEEPER_ROLE, config.MOD_ROLE)]
        return [r.colour.value for r in roles if r is not None]

    @staticmethod
    async def member(guild, user_id: int):
        m = guild.get_member(user_id)
        if m is None:
            try:
                m = await guild.fetch_member(user_id)
            except discord.NotFound:
                return None
        return m

    async def perk(self, user_id: int, kind: str):
        return await self.db.fetchone("SELECT * FROM perks WHERE user_id = ? AND kind = ?", (user_id, kind))

    async def charge(self, user_id: int, item: S.Item, expires_at: int, role_id: int | None, ref: str) -> E.Result:
        """Take the price and store the perk together: both or neither."""
        async with self.db.transaction() as tx:
            result = await E.apply_tx(tx, user_id, -item.price, S.SHOP_REASON, now(), ref)
            if result.ok:
                await tx.execute(
                    "INSERT INTO perks (user_id, kind, role_id, expires_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (user_id, kind) DO UPDATE SET role_id = excluded.role_id,"
                    " expires_at = excluded.expires_at",
                    (user_id, item.key, role_id, expires_at))
            return result

    # ------------------------------------------------------------ /shop
    @app_commands.command(name="shop", description="What you can buy with coins")
    async def shop(self, interaction: discord.Interaction) -> None:
        lines = [f"**{i.name}** · `{i.price:,}` coins · `/buy item:{i.key}`\n{i.blurb}" for i in S.ITEMS.values()]
        balance = await E.balance(self.db, interaction.user.id)
        embed = style.embed(title="Shop", description="\n\n".join(lines),
                            footer=style.label("shop", f"you have {balance:,} coins"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ /buy
    @app_commands.command(name="buy", description="Spend coins on a perk")
    @app_commands.describe(item="What to buy", color="Name colour: a hex code like #3BA55D",
                           message="Shoutout: what to say in general (140 characters, no links or mentions)")
    @app_commands.choices(item=ITEM_CHOICES)
    async def buy(self, interaction: discord.Interaction, item: app_commands.Choice[str],
                  color: str | None = None, message: str | None = None) -> None:
        guild = self.guild()
        member = interaction.user
        if guild is None or not hasattr(member, "roles"):  # a DM: no member, no roles
            await interaction.response.send_message("Use /buy in the server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ref = f"shop:{interaction.id}"
        async with self.lock(member.id):
            if item.value == S.COLOR:
                reply = await self.buy_color(guild, member, color, ref)
            elif item.value == S.HYPE:
                reply = await self.buy_hype(guild, member, ref)
            else:
                reply = await self.buy_shoutout(guild, member, message, ref)
        await interaction.followup.send(reply, ephemeral=True)

    async def afford(self, user_id: int, item: S.Item) -> str | None:
        balance = await E.balance(self.db, user_id)
        if balance < item.price:
            return f"{item.name} costs {item.price:,} coins. You have {balance:,}."
        return None

    @staticmethod
    def paid(item: S.Item, result: E.Result) -> str:
        return f"-{item.price:,} coins, {result.balance:,} left."

    @staticmethod
    def broke(item: S.Item, result: E.Result) -> str:
        if result.status is E.Status.DUPLICATE:
            return "That purchase already went through."
        return f"{item.name} costs {item.price:,} coins. You have {result.balance:,}. You weren't charged."

    async def buy_color(self, guild, member, color: str | None, ref: str) -> str:
        item = S.ITEMS[S.COLOR]
        if not color:
            return "Pick a colour too, like `/buy item:color color:#3BA55D`."
        value, problem = S.check_color(color, self.staff_colors(guild))
        if problem:
            return problem
        if problem := await self.afford(member.id, item):
            return problem
        target = self.color_slot(guild)
        if target is None:
            return ("There's no safe spot for colour roles (they must sit below Front Desk and the staff "
                    "roles), so name colours aren't for sale right now. Ask a Keeper. You weren't charged.")
        row = await self.perk(member.id, S.COLOR)
        role = guild.get_role(row["role_id"]) if row and row["role_id"] else None
        created, old_colour, had = role is None, None, False
        reason = f"/buy color by {member} ({member.id})"
        try:
            if role is None:
                role = await guild.create_role(name=S.role_name(member.display_name),
                                               colour=discord.Colour(value), permissions=discord.Permissions.none(),
                                               hoist=False, mentionable=False, reason=reason)
            else:
                old_colour = role.colour
                await role.edit(name=S.role_name(member.display_name), colour=discord.Colour(value), reason=reason)
            if role.position != target:
                await role.edit(position=target, reason=reason)
            had = role in member.roles
            if not had:
                await member.add_roles(role, reason=reason)
        except discord.HTTPException:
            log.exception("colour role setup failed for %s", member.id)
            await self.undo_color(role if created else None, role if old_colour is not None else None, old_colour)
            return "Couldn't set up your colour role (Front Desk needs Manage Roles). You weren't charged."
        result = await self.charge(member.id, item, S.extend(row["expires_at"] if row else None, now(),
                                                             item.duration), role.id, ref)
        if not result.ok:
            if result.status is E.Status.INSUFFICIENT:
                await self.undo_color(role if created else None, role if old_colour is not None else None,
                                      old_colour)
                if not created and not had:
                    await self.quietly(member.remove_roles(role, reason="purchase not paid"))
            return self.broke(item, result)
        perk = await self.perk(member.id, S.COLOR)
        return (f"Your name colour is now `#{value:06X}` until <t:{perk['expires_at']}:f>. "
                + self.paid(item, result))

    @staticmethod
    def color_slot(guild) -> int | None:
        """Where colour roles go: directly below the lowest of Front Desk's top role and the
        staff roles, so the colour shows over ordinary roles but a bought role never sits
        above (or looks ranked like) staff. None if no position above @everyone is left."""
        me = getattr(guild, "me", None)
        if me is None:
            return None
        staff = [config.match_by_name(guild.roles, n) for n in (config.MOD_ROLE, config.KEEPER_ROLE)]
        target = min([me.top_role.position] + [r.position for r in staff if r is not None]) - 1
        return target if target >= 1 else None

    async def undo_color(self, created_role, edited_role, old_colour) -> None:
        if created_role is not None:
            await self.quietly(created_role.delete(reason="colour purchase failed"))
        elif edited_role is not None and old_colour is not None:
            await self.quietly(edited_role.edit(colour=old_colour, reason="colour purchase failed"))

    @staticmethod
    async def quietly(coro) -> None:
        try:
            await coro
        except discord.HTTPException:
            log.exception("shop clean-up failed")

    async def buy_hype(self, guild, member, ref: str) -> str:
        item = S.ITEMS[S.HYPE]
        role = config.match_by_name(guild.roles, config.HYPE_ROLE)
        if role is None:
            return f"There's no `{config.HYPE_ROLE}` role on the server yet, so Hype isn't for sale. Ask a Keeper."
        if not self.manageable(guild, role):
            return (f"Front Desk's role has to be above `{config.HYPE_ROLE}` to hand it out, so Hype isn't for "
                    "sale right now. Ask a Keeper.")
        if problem := await self.afford(member.id, item):
            return problem
        had = role in member.roles
        try:
            if not had:
                await member.add_roles(role, reason="/buy hype")
        except discord.HTTPException:
            log.exception("hype role failed for %s", member.id)
            return "Couldn't give you the Hype role. You weren't charged."
        row = await self.perk(member.id, S.HYPE)
        result = await self.charge(member.id, item, S.extend(row["expires_at"] if row else None, now(),
                                                             item.duration), role.id, ref)
        if not result.ok:
            if result.status is E.Status.INSUFFICIENT and not had:
                await self.quietly(member.remove_roles(role, reason="purchase not paid"))
            return self.broke(item, result)
        perk = await self.perk(member.id, S.HYPE)
        return f"You're **Hype** until <t:{perk['expires_at']}:f>. " + self.paid(item, result)

    async def buy_shoutout(self, guild, member, message: str | None, ref: str) -> str:
        item = S.ITEMS[S.SHOUTOUT]
        if problem := S.shoutout_error(message):
            return problem
        t = now()
        row = await self.perk(member.id, S.SHOUTOUT)
        if row is not None and row["expires_at"] > t:
            return f"One shoutout per 24 hours. You can post again in {S.fmt_wait(row['expires_at'] - t)}."
        if problem := await self.afford(member.id, item):
            return problem
        channel = config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL)
        if channel is None:
            return f"There's no {config.GENERAL_CHANNEL} channel to post in. You weren't charged."
        embed = style.embed(description=f"📣 {member.mention} says: {message.strip()}",
                            footer=style.label("shoutout", "/buy item:shoutout"))
        try:
            sent = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("shoutout post failed for %s", member.id)
            return "Couldn't post your shoutout. You weren't charged."
        result = await self.charge(member.id, item, t + item.duration, None, ref)
        if not result.ok:
            if result.status is E.Status.INSUFFICIENT:
                await self.quietly(sent.delete())
            return self.broke(item, result)
        return f"Posted in {channel.mention}. " + self.paid(item, result)

    # ------------------------------------------------------------ expiry
    @tasks.loop(minutes=5)
    async def expiry(self) -> None:
        try:
            await self.run_expiry()
        except Exception:
            log.exception("perk expiry failed")

    @expiry.before_loop
    async def before_expiry(self) -> None:
        await self.bot.wait_until_ready()

    async def run_expiry(self) -> None:
        guild = self.guild()
        if guild is None:
            return
        rows = await self.db.fetchall("SELECT * FROM perks WHERE expires_at <= ?", (now(),))
        for row in rows:
            try:
                async with self.lock(row["user_id"]):
                    # Re-read under the lock: a rebuy since the SELECT moved expires_at
                    # on, and its role must not be taken away.
                    row = await self.perk(row["user_id"], row["kind"])
                    if row is None or row["expires_at"] > now():
                        continue
                    await self.expire(guild, row)
                    # Only the row we expired.
                    await self.db.execute("DELETE FROM perks WHERE user_id = ? AND kind = ? AND expires_at = ?",
                                          (row["user_id"], row["kind"], row["expires_at"]))
            except Exception:
                log.exception("expiring %s for %s failed; retrying next tick", row["kind"], row["user_id"])

    async def expire(self, guild, row) -> None:
        kind, role_id = row["kind"], row["role_id"]
        if kind == S.COLOR:
            role = guild.get_role(role_id) if role_id else None
            if role is not None:
                try:
                    await role.delete(reason="Name colour expired")
                except discord.NotFound:
                    pass
        elif kind == S.HYPE:
            role = (guild.get_role(role_id) if role_id else None) or config.match_by_name(guild.roles,
                                                                                         config.HYPE_ROLE)
            if role is None:
                return
            member = await self.member(guild, row["user_id"])
            if member is not None and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Hype expired")
                except discord.NotFound:
                    pass
        # shoutout rows are just a cooldown: nothing on Discord to undo

    # ------------------------------------------------------------ seasons
    async def standings(self, key: str) -> list[S.Standing]:
        start, end = S.month_bounds(key, self.tz)
        reasons = sorted(S.SEASON_REASONS)
        rows = await self.db.fetchall(
            "SELECT user_id, SUM(delta) AS pts, MIN(at) AS first FROM ledger WHERE delta > 0"
            f" AND reason IN ({', '.join('?' * len(reasons))}) AND at >= ? AND at < ? GROUP BY user_id",
            (*reasons, start, end))
        return S.standings((r["user_id"], r["pts"], r["first"]) for r in rows)

    @app_commands.command(name="season", description="This month's season standings")
    async def season(self, interaction: discord.Interaction) -> None:
        t = now()
        key = S.month_key(t, self.tz)
        table = await self.standings(key)
        mine = S.find(table, interaction.user.id)
        if not table:
            text = "Nobody's earned season points yet this month."
        else:
            rows = [f"`{s.rank:02}`  <@{s.user_id}>  {s.points:,}" for s in table[:10]]
            if mine and mine.rank > 10:
                rows += ["…", f"`{mine.rank:02}`  <@{mine.user_id}>  {mine.points:,}"]
            text = "\n".join(rows)
        if mine is None:
            text += "\n\nYou haven't earned season points this month yet."
        days = S.days_left(t, self.tz)
        text += (f"\n\nPoints are coins earned from activity (daily, voice, messages, clips, squads, MVP, trivia). "
                 f"Top 3 win {', '.join(f'{b:,}' for b in S.BONUSES)} coins and the `{config.SEASON_ROLE}` role.")
        embed = style.embed(title=f"Season · {S.month_name(key)}", description=text,
                            footer=style.label("season", f"{days} day{'s' if days != 1 else ''} left"))
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @tasks.loop(minutes=5)
    async def seasons(self) -> None:
        try:
            await self.run_seasons()
        except Exception:
            log.exception("season rollover failed")

    @seasons.before_loop
    async def before_seasons(self) -> None:
        await self.bot.wait_until_ready()

    async def run_seasons(self) -> None:
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM seasons")}
        plan = S.season_plan(now(), self.tz, done)
        if plan.mark_done:
            await self.db.execute("INSERT OR IGNORE INTO seasons (key, posted_at, top) VALUES (?, ?, NULL)",
                                  (plan.mark_done, now()))
        if plan.run:
            await self.finish_season(plan.run)

    async def finish_season(self, key: str) -> None:
        guild = self.guild()
        if guild is None:
            return  # not connected: try again next tick
        top = (await self.standings(key))[:3]
        if not top:
            log.info("season %s: nobody earned points, nothing posted", key)
            await self.mark_season(key, [])
            return
        t = now()
        async with self.db.transaction() as tx:  # refs make a retry a no-op
            for s, bonus in zip(top, S.BONUSES):
                await E.apply_tx(tx, s.user_id, bonus, S.SEASON_REASON, t, S.bonus_ref(key, s.rank))
        winners = [s.user_id for s in top]
        await self.swap_season_role(guild, winners)
        channel = (config.match_by_name(guild.text_channels, config.ANNOUNCEMENTS_CHANNEL)
                   or config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL))
        if channel is None:
            log.warning("season %s: no announcements or general channel, results not posted", key)
        else:
            lines = [f"{MEDALS[s.rank - 1]}  <@{s.user_id}>  {s.points:,} points  ·  +{bonus:,} coins"
                     for s, bonus in zip(top, S.BONUSES)]
            embed = style.embed(title=f"Season results · {S.month_name(key)}", description="\n".join(lines),
                                footer=style.label("season", key))
            await channel.send(
                content=f"🏆 {S.month_name(key)} is over! Congrats " + ", ".join(f"<@{u}>" for u in winners),
                embed=embed, allowed_mentions=ping_only(users=[discord.Object(u) for u in winners]))
        await self.mark_season(key, winners)
        log.info("season %s posted: %s", key, winners)

    async def mark_season(self, key: str, winners: list[int]) -> None:
        await self.db.execute("INSERT OR IGNORE INTO seasons (key, posted_at, top) VALUES (?, ?, ?)",
                              (key, now(), json.dumps(winners)))

    async def swap_season_role(self, guild, winners: list[int]) -> None:
        role = config.match_by_name(guild.roles, config.SEASON_ROLE)
        if not self.manageable(guild, role):
            log.info("season role %s missing or above Front Desk: skipped", config.SEASON_ROLE)
            return
        for member in list(role.members):
            if member.id not in winners:
                try:
                    await member.remove_roles(role, reason=S.SEASON_ROLE_REASON)
                except discord.HTTPException:
                    log.exception("removing season role from %s failed", member.id)
        for uid in winners:
            try:
                member = await self.member(guild, uid)
                if member is not None and role not in member.roles:
                    await member.add_roles(role, reason=S.SEASON_ROLE_REASON)
            except discord.HTTPException:
                log.exception("giving season role to %s failed", uid)


async def setup(bot) -> None:
    await bot.add_cog(Shop(bot))
