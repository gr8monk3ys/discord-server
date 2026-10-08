"""Module 4c: the coin shop and monthly seasons.

/shop and /buy spend coins on timed perks (a personal name colour, the Hype role for you
or a friend, a shoutout in general, a 24-hour pinned Spotlight). The Discord side of a purchase happens first; the price is then
taken with economy.apply_tx in the same transaction that writes the perk row, so a
failed purchase never charges (and a purchase that can't be paid is undone). All perk
state lives in the `perks` table and an expiry loop removes perks when they run out,
so restarts lose nothing. The one active Spotlight lives in `meta` (shop:spotlight) and the
same loop unpins it after 24 hours.

/raffle buy|info: a weekly coin raffle and the shop's main sink. Tickets are ledger rows;
Sunday 20:00 local one ticket wins 80% of the pot and the rest is burned. A draw pays
the winner with a ledger ref first and is marked in `jobs` only after the post, so a
Discord error retries without paying twice; draws missed while the bot was off run late.

/season ranks coins earned this local month from activity (not gambling, gifts or
the shop). When a month ends, the top 3 are announced, get the Season Champ role and
season bonuses (ledger refs make that idempotent); the `seasons` row is written only
after the post succeeds, so a Discord error retries on the next tick."""

import asyncio
import contextlib
import json
import logging
import random
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

def esc(text: str | None) -> str:
    """Member text inside bot-authored embeds: no markdown tricks (links and mentions are
    already refused by shoutout_error)."""
    return discord.utils.escape_markdown((text or "").strip())


ITEM_CHOICES = [app_commands.Choice(name=f"{i.name} ({i.price:,} coins)", value=i.key) for i in S.ITEMS.values()]
MEDALS = ("🥇", "🥈", "🥉")


def now() -> int:
    return int(time.time())


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}  # per member: one purchase/expiry at a time
        self.spotlight_lock = asyncio.Lock()  # one server-wide slot: one buyer at a time
        self.rng = random.SystemRandom()  # raffle draws: not predictable from the ticket list

    async def cog_load(self) -> None:
        self.expiry.start()
        self.seasons.start()
        self.raffles.start()

    async def cog_unload(self) -> None:
        self.expiry.cancel()
        self.seasons.cancel()
        self.raffles.cancel()

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

    async def charge(self, user_id: int, item: S.Item, expires_at: int, role_id: int | None, ref: str,
                     holder: int | None = None, kind: str | None = None,
                     spotlight: S.Spotlight | None = None) -> E.Result:
        """Take the price from `user_id` and store the perk (for `holder`, default the payer, as
        `kind`, default the item) together: both or neither. A Spotlight purchase also claims
        the server-wide slot in the same transaction."""
        async with self.db.transaction() as tx:
            result = await E.apply_tx(tx, user_id, -item.price, S.SHOP_REASON, now(), ref)
            if result.ok:
                await tx.execute(
                    "INSERT INTO perks (user_id, kind, role_id, expires_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (user_id, kind) DO UPDATE SET role_id = excluded.role_id,"
                    " expires_at = excluded.expires_at",
                    (holder if holder is not None else user_id, kind or item.key, role_id, expires_at))
                if spotlight is not None:
                    await tx.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                     (S.SPOTLIGHT_KEY, S.spotlight_dump(spotlight)))
            return result

    # ------------------------------------------------------------ /shop
    @app_commands.command(name="shop", description="What you can buy with coins")
    async def shop(self, interaction: discord.Interaction) -> None:
        lines = [f"**{i.name}** · `{i.price:,}` coins · `/buy item:{i.key}`\n{i.blurb}" for i in S.ITEMS.values()]
        lines.append(f"**Raffle ticket** · `{S.TICKET_PRICE:,}` coins · `/raffle buy`\nUp to {S.MAX_TICKETS} a week. "
                     f"Sundays 20:00 one ticket wins {S.RAFFLE_SHARE}% of the pot; the rest is burned.")
        balance = await E.balance(self.db, interaction.user.id)
        embed = style.embed(title="Shop", description="\n\n".join(lines),
                            footer=style.label("shop", f"you have {balance:,} coins"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ /buy
    @app_commands.command(name="buy", description="Spend coins on a perk")
    @app_commands.describe(item="What to buy", color="Name colour: a hex code like #3BA55D",
                           message="Shoutout or Spotlight: what to say in general (140 characters, no links "
                                   "or mentions)",
                           friend="Gift Hype: who gets it")
    @app_commands.choices(item=ITEM_CHOICES)
    async def buy(self, interaction: discord.Interaction, item: app_commands.Choice[str],
                  color: str | None = None, message: str | None = None,
                  friend: discord.Member | None = None) -> None:
        guild = self.guild()
        member = interaction.user
        if guild is None or not hasattr(member, "roles"):  # a DM: no member, no roles
            await interaction.response.send_message("Use /buy in the server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ref = f"shop:{interaction.id}"
        # A gift touches the friend's Hype row too: lock both, in id order, so A gifting B
        # while B gifts A can't deadlock.
        holders = {member.id} | ({friend.id} if item.value == S.GIFT and friend is not None else set())
        async with contextlib.AsyncExitStack() as stack:
            for uid in sorted(holders):
                await stack.enter_async_context(self.lock(uid))
            if item.value == S.COLOR:
                reply = await self.buy_color(guild, member, color, ref)
            elif item.value == S.HYPE:
                reply = await self.buy_hype(guild, member, ref)
            elif item.value == S.GIFT:
                reply = await self.buy_gift(guild, member, friend, ref)
            elif item.value == S.SPOTLIGHT:
                async with self.spotlight_lock:
                    reply = await self.buy_spotlight(guild, member, message, ref)
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
        role, problem = self.hype_role(guild)
        if problem:
            return problem
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
        embed = style.embed(description=f"📣 {member.mention} says: {esc(message)}",
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

    def hype_role(self, guild):
        """(role, None) if Front Desk can hand out Hype, else (None, why not)."""
        role = config.match_by_name(guild.roles, config.HYPE_ROLE)
        if role is None:
            return None, f"There's no `{config.HYPE_ROLE}` role on the server yet, so Hype isn't for sale. Ask a Keeper."
        if not self.manageable(guild, role):
            return None, (f"Front Desk's role has to be above `{config.HYPE_ROLE}` to hand it out, so Hype isn't "
                          "for sale right now. Ask a Keeper.")
        return role, None

    async def buy_gift(self, guild, member, friend, ref: str) -> str:
        item = S.ITEMS[S.GIFT]
        if problem := S.gift_error(member.id, getattr(friend, "id", None), bool(getattr(friend, "bot", False))):
            return problem
        if not hasattr(friend, "roles") or guild.get_member(friend.id) is None:
            return "They need to be in the server to get Hype."
        role, problem = self.hype_role(guild)
        if problem:
            return problem
        if problem := await self.afford(member.id, item):
            return problem
        # The caller holds the friend's lock too: the Hype row is theirs.
        had = role in friend.roles
        try:
            if not had:
                await friend.add_roles(role, reason=f"Hype gifted by {member} ({member.id})")
        except discord.HTTPException:
            log.exception("gift hype role failed for %s", friend.id)
            return "Couldn't give them the Hype role. You weren't charged."
        row = await self.perk(friend.id, S.HYPE)
        result = await self.charge(member.id, item, S.extend(row["expires_at"] if row else None, now(),
                                                             item.duration), role.id, ref,
                                   holder=friend.id, kind=S.HYPE)
        if not result.ok:
            if result.status is E.Status.INSUFFICIENT and not had:
                await self.quietly(friend.remove_roles(role, reason="gift not paid"))
            return self.broke(item, result)
        perk = await self.perk(friend.id, S.HYPE)
        try:
            await friend.send(f"🎁 {member.mention} gifted you **Hype** in the server, until <t:{perk['expires_at']}:f>.",
                              allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass  # DMs closed: the gift still went through
        return f"{friend.mention} is **Hype** until <t:{perk['expires_at']}:f>. " + self.paid(item, result)

    async def spotlight_row(self):
        return await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (S.SPOTLIGHT_KEY,))

    async def buy_spotlight(self, guild, member, message: str | None, ref: str) -> str:
        """Callers hold self.spotlight_lock: there's one slot for the whole server."""
        item = S.ITEMS[S.SPOTLIGHT]
        if problem := S.shoutout_error(message):
            return problem
        t = now()
        row = await self.spotlight_row()
        current = S.spotlight_load(row["value"] if row else None)
        if S.spotlight_busy(current, t):
            return f"Someone's in the spotlight until <t:{current.expires_at}:t>. Try again after that."
        perk = await self.perk(member.id, S.SPOTLIGHT)
        if perk is not None and perk["expires_at"] > t:
            return f"One spotlight a week each. You can buy another in {S.fmt_wait(perk['expires_at'] - t)}."
        if problem := await self.afford(member.id, item):
            return problem
        channel = config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL)
        if channel is None:
            return f"There's no {config.GENERAL_CHANNEL} channel to post in. You weren't charged."
        if row is not None:  # the last spotlight is over but the loop hasn't unpinned it yet
            await self.expire_spotlight(guild)
        embed = style.embed(title="🔦 Spotlight", description=f"{member.mention}: {esc(message)}",
                            footer=style.label("spotlight", "/buy item:spotlight"))
        try:
            sent = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("spotlight post failed for %s", member.id)
            return "Couldn't post your spotlight. You weren't charged."
        try:
            await sent.pin(reason=f"Spotlight bought by {member.id}")
        except discord.HTTPException:
            log.exception("spotlight pin failed for %s", member.id)
            await self.quietly(sent.delete())
            return ("Couldn't pin your spotlight (Front Desk needs Manage Messages, or general has too many pins). "
                    "You weren't charged.")
        slot = S.Spotlight(member.id, channel.id, sent.id, t + S.SPOTLIGHT_PIN)
        result = await self.charge(member.id, item, t + item.duration, None, ref, spotlight=slot)
        if not result.ok:
            await self.quietly(sent.delete())
            return self.broke(item, result)
        return f"Pinned in {channel.mention} until <t:{slot.expires_at}:f>. " + self.paid(item, result)

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
        try:
            async with self.spotlight_lock:
                await self.expire_spotlight(guild)
        except Exception:
            log.exception("spotlight expiry failed; retrying next tick")
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

    async def expire_spotlight(self, guild) -> None:
        """Unpin the spotlight once it's over and free the slot. Callers hold spotlight_lock.
        A Discord error other than NotFound keeps the row, so the next tick retries."""
        row = await self.spotlight_row()
        if row is None:
            return
        slot = S.spotlight_load(row["value"])
        if slot is not None:
            if slot.expires_at > now():
                return
            channel = next((c for c in guild.text_channels if c.id == slot.channel_id), None)
            if channel is not None:
                try:
                    await channel.get_partial_message(slot.message_id).unpin(reason="Spotlight is over")
                except discord.NotFound:
                    pass  # a mod deleted it already
        # an unreadable row is just dropped
        await self.db.execute("DELETE FROM meta WHERE key = ? AND value = ?", (S.SPOTLIGHT_KEY, row["value"]))

    # ------------------------------------------------------------ /raffle
    raffle = app_commands.Group(name="raffle", description="The weekly coin raffle", guild_only=True)

    @staticmethod
    async def tickets_tx(tx, key: str, user_id: int | None = None) -> list[tuple[int, int]]:
        """(user_id, tickets) for draw `key`, optionally for one member only."""
        sql = ("SELECT user_id, -SUM(delta) AS spent FROM ledger WHERE reason = ? AND delta < 0 AND ref LIKE ?"
               + (" AND user_id = ?" if user_id is not None else "") + " GROUP BY user_id ORDER BY user_id")
        params = (S.RAFFLE_REASON, S.tickets_like(key)) + ((user_id,) if user_id is not None else ())
        return [(r["user_id"], r["spent"] // S.TICKET_PRICE) for r in await tx.fetchall(sql, params)]

    @raffle.command(name="buy", description=f"Buy raffle tickets ({S.TICKET_PRICE} coins each)")
    @app_commands.describe(tickets=f"How many (you can hold {S.MAX_TICKETS} per week)")
    async def raffle_buy(self, interaction: discord.Interaction,
                         tickets: app_commands.Range[int, 1, S.MAX_TICKETS] = 1) -> None:
        user = interaction.user
        if self.guild() is None or not hasattr(user, "roles"):
            await interaction.response.send_message("Use /raffle in the server.", ephemeral=True)
            return
        if not S.can_enter(user.id, now()):
            await interaction.response.send_message(
                "Your Discord account is too new for the raffle. Come back when it's a month old.", ephemeral=True)
            return
        async with self.lock(user.id):
            t = now()
            draw = S.next_draw(t, self.tz)
            async with self.db.transaction() as tx:
                owned = sum(n for _, n in await self.tickets_tx(tx, draw.key, user.id))
                problem = S.ticket_error(owned, tickets)
                result = None
                if problem is None:
                    result = await E.apply_tx(tx, user.id, -tickets * S.TICKET_PRICE, S.RAFFLE_REASON, t,
                                              S.ticket_ref(draw.key, user.id, interaction.id))
                pot = sum(n for _, n in await self.tickets_tx(tx, draw.key)) * S.TICKET_PRICE
        if problem is None and not result.ok:
            if result.status is E.Status.DUPLICATE:
                problem = "That purchase already went through."
            else:
                problem = (f"{tickets} ticket{'s' if tickets != 1 else ''} cost{'' if tickets != 1 else 's'} "
                           f"{tickets * S.TICKET_PRICE:,} coins. You have {result.balance:,}.")
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        mine = owned + tickets
        await interaction.response.send_message(
            f"🎟️ You have **{mine}** ticket{'s' if mine != 1 else ''} for the draw on <t:{draw.scheduled_at}:F> "
            f"(<t:{draw.scheduled_at}:R>). The prize is {S.prize(pot):,} coins so far. "
            f"-{tickets * S.TICKET_PRICE:,} coins, {result.balance:,} left.", ephemeral=True)

    @raffle.command(name="info", description="This week's raffle: the prize, your tickets, the draw time")
    async def raffle_info(self, interaction: discord.Interaction) -> None:
        draw = S.next_draw(now(), self.tz)
        async with self.db.transaction() as tx:
            rows = await self.tickets_tx(tx, draw.key)
        total = sum(n for _, n in rows)
        mine = sum(n for uid, n in rows if uid == interaction.user.id)
        pot = total * S.TICKET_PRICE
        text = (f"**Prize:** {S.prize(pot):,} coins ({S.RAFFLE_SHARE}% of a {pot:,}-coin pot; the rest is burned)\n"
                f"**Entrants:** {len(rows)} · **Tickets:** {total}\n"
                f"**Yours:** {mine} ({S.fmt_chance(mine, total)} chance)\n"
                f"**Draw:** <t:{draw.scheduled_at}:F> (<t:{draw.scheduled_at}:R>)\n\n"
                f"Tickets are {S.TICKET_PRICE} coins, up to {S.MAX_TICKETS} a week: `/raffle buy`. "
                f"With fewer than {S.MIN_ENTRANTS} entrants everyone is refunded.")
        embed = style.embed(title="Weekly raffle", description=text, footer=style.label("raffle", draw.key))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tasks.loop(minutes=5)
    async def raffles(self) -> None:
        try:
            await self.run_raffles()
        except Exception:
            log.exception("raffle draw failed")

    @raffles.before_loop
    async def before_raffles(self) -> None:
        await self.bot.wait_until_ready()

    async def run_raffles(self) -> None:
        t = now()
        rows = await self.db.fetchall("SELECT DISTINCT ref FROM ledger WHERE reason = ? AND delta < 0 AND at >= ?",
                                      (S.RAFFLE_REASON, t - 60 * S.DAY))
        keys = {k for k in (S.key_of(r["ref"]) for r in rows) if k}
        done = {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?",
                                                         (f"{S.RAFFLE_JOB.name}:%",))}
        for key in S.due(keys, done, t, self.tz):
            await self.draw(key)

    async def draw(self, key: str) -> None:
        guild = self.guild()
        if guild is None:
            return  # not connected: try again next tick
        t = now()
        async with self.db.transaction() as tx:  # refs make a retry a no-op
            rows = await self.tickets_tx(tx, key)
            total = sum(n for _, n in rows)
            pot = total * S.TICKET_PRICE
            winner = None
            if len(rows) < S.MIN_ENTRANTS:
                for uid, n in rows:
                    await E.apply_tx(tx, uid, n * S.TICKET_PRICE, S.RAFFLE_REASON, t, S.refund_ref(key, uid))
            else:
                paid = await tx.fetchone("SELECT user_id FROM ledger WHERE ref = ?", (S.win_ref(key),))
                winner = paid["user_id"] if paid else S.pick_winner(rows, self.rng)
                if paid is None:
                    await E.apply_tx(tx, winner, S.prize(pot), S.RAFFLE_REASON, t, S.win_ref(key))
        channel = config.match_by_name(guild.text_channels, config.GENERAL_CHANNEL)
        if winner is not None and channel is None:
            log.warning("raffle %s: no general channel, result not posted", key)
        elif winner is not None:
            embed = style.embed(
                title="🎟️ Raffle results",
                description=(f"<@{winner}> won **{S.prize(pot):,} coins** with {dict(rows)[winner]} of {total} "
                             f"tickets ({len(rows)} entrants). {pot - S.prize(pot):,} coins were burned.\n\n"
                             f"Next week's draw is open: `/raffle buy`."),
                footer=style.label("raffle", key))
            await channel.send(content=f"🎉 Congrats <@{winner}>!", embed=embed,
                               allowed_mentions=ping_only(users=[discord.Object(winner)]))
        await self.db.execute("INSERT OR IGNORE INTO jobs (key, done_at) VALUES (?, ?)", (key, now()))
        log.info("raffle %s drawn: winner %s, pot %s, entrants %s", key, winner, pot, len(rows))

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
