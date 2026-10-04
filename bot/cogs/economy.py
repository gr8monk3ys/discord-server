"""Module 4a: Economy core. Coins for showing up (/daily, voice, messages, clips, full
LFG squads, weekly MVP, clip of the week) and the basic commands: /balance, /give,
/coinflip and /richest.

Every coin moves through bot/economy.py; refs make each payout idempotent. Earning
respects the /privacy opt-out and bots never earn. Needs the Message Content intent to
spot clips (shared with the clips module); voice uses the default voice-state intent."""

import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import economy as E
import style
from cogs.clips import Clips
from cogs.stats import Stats
from logic import clips as clip_rules
from logic import coins as C
from logic import stats as S

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
SIDE_CHOICES = [app_commands.Choice(name=s.title(), value=s) for s in C.SIDES]
OPTED_OUT = ("You've turned tracking off with `/privacy`, so you don't earn coins. "
             "Run `/privacy tracking:on` to start again.")


def now() -> int:
    return int(time.time())


def coins(n: int) -> str:
    return f"{n:,} coin" + ("" if n == 1 else "s")


class Economy(commands.Cog):
    def __init__(self, bot, rng=None):
        self.bot = bot
        self.rng = rng or random.SystemRandom()

    async def cog_load(self) -> None:
        self.voice_loop.start()

    async def cog_unload(self) -> None:
        self.voice_loop.cancel()

    @property
    def db(self):
        return self.bot.db

    @property
    def tz(self):
        return self.bot.settings.tz

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def is_bot(self, user_id: int) -> bool:
        guild = self.guild()
        member = guild.get_member(user_id) if guild else None
        return bool(member and member.bot)

    async def reply(self, interaction, text: str, ephemeral: bool = True, title: str | None = None,
                    footer: str | None = None) -> None:
        await interaction.response.send_message(
            embed=style.embed(title=title, description=text, footer=footer),
            ephemeral=ephemeral, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ voice
    @tasks.loop(minutes=5)
    async def voice_loop(self) -> None:
        try:
            await self.pay_voice()
        except Exception:
            log.exception("voice coins tick failed")

    @voice_loop.before_loop
    async def before_voice(self) -> None:
        await self.bot.wait_until_ready()

    def tracked_voice(self, guild, channel) -> bool:
        afk = guild.afk_channel.id if guild.afk_channel else None
        return channel.id != afk and not Stats.in_staff(channel)

    async def pay_voice(self) -> int:
        """Pay everyone currently counting in voice for this 5-minute tick. Returns how many were paid."""
        guild = self.guild()
        if guild is None:
            return 0
        t = now()
        rooms = []
        for channel in guild.voice_channels:
            if not self.tracked_voice(guild, channel):
                continue
            people = []
            for user_id, state in channel.voice_states.items():
                member = guild.get_member(user_id)
                deafened = bool(getattr(state, "self_deaf", False) or getattr(state, "deaf", False))
                people.append((user_id, bool(member and member.bot), deafened))
            rooms.append(people)
        candidates = C.voice_earners(rooms)
        if not candidates:
            return 0
        earners = [uid for uid in candidates if await self.db.tracking_allowed(uid)]
        tick = C.voice_tick(t)
        since, until = C.day_bounds(t, self.bot.settings.tz)
        paid = 0
        async with self.db.transaction() as tx:
            for uid in earners:
                amount = C.capped(C.VOICE_COINS, await E.earned_tx(tx, uid, C.VOICE, since, until),
                                  C.VOICE_DAILY_CAP)
                if amount and (await E.apply_tx(tx, uid, amount, C.VOICE, t, C.voice_ref(uid, tick))).ok:
                    paid += 1
        return paid

    # ------------------------------------------------------------ messages and clips
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self.earn_from_message(message)
        except Exception:
            log.exception("message coins failed")

    async def earn_from_message(self, message) -> None:
        if message.author.bot or message.guild is None or message.guild.id != self.bot.settings.guild_id:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if Stats.in_staff(message.channel):
            return
        uid = message.author.id
        if not await self.db.tracking_allowed(uid):
            return
        is_clip = Clips.in_clips(message.channel) and clip_rules.clip_url(
            message.content, [(a.url, a.content_type) for a in message.attachments]) is not None
        t = now()
        since, until = C.day_bounds(t, self.tz)
        async with self.db.transaction() as tx:
            n = C.message_payout(await E.earned_tx(tx, uid, C.MESSAGE, since, until))
            if n:
                await E.apply_tx(tx, uid, n, C.MESSAGE, t)
            if is_clip:
                n = C.clip_payout(await E.earned_tx(tx, uid, C.CLIP, since, until))
                if n:
                    await E.apply_tx(tx, uid, n, C.CLIP, t, C.clip_ref(message.id))

    # ------------------------------------------------------------ events from other modules
    async def pay_event(self, user_id: int, amount: int, reason: str, ref: str) -> bool:
        if self.is_bot(user_id) or not await self.db.tracking_allowed(user_id):
            return False
        return (await E.apply(self.db, user_id, amount, reason, now(), ref)).ok

    @commands.Cog.listener()
    async def on_lfg_squad_full(self, post_id: int, roster) -> None:
        t = now()
        since, until = C.day_bounds(t, self.bot.settings.tz)
        for uid in roster.members:
            try:
                if self.is_bot(uid) or not await self.db.tracking_allowed(uid):
                    continue
                # Cap check and payment in one transaction, so simultaneous squads can't both pass.
                async with self.db.transaction() as tx:
                    earned = await E.earned_tx(tx, uid, C.LFG, since, until)
                    if C.capped(C.LFG_COINS, earned, C.LFG_DAILY_CAP):
                        await E.apply_tx(tx, uid, C.LFG_COINS, C.LFG, t, C.lfg_ref(post_id, uid))
            except Exception:
                log.exception("lfg coins failed for %s on post %s", uid, post_id)

    @commands.Cog.listener()
    async def on_weekly_mvp(self, period_key: str, winner_id: int) -> None:
        try:
            await self.pay_event(winner_id, C.MVP_COINS, C.MVP, C.mvp_ref(period_key, winner_id))
        except Exception:
            log.exception("mvp coins failed for %s", period_key)

    @commands.Cog.listener()
    async def on_clip_of_the_week(self, week: str, user_id: int) -> None:
        try:
            await self.pay_event(user_id, C.CLIP_WEEK_COINS, C.CLIP_WEEK, C.clip_week_ref(week))
        except Exception:
            log.exception("clip of the week coins failed for %s", week)

    # ------------------------------------------------------------ /daily
    @app_commands.command(name="daily", description="Claim your daily coins (streaks pay more)")
    async def daily(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        if not await self.db.tracking_allowed(uid):
            await self.reply(interaction, OPTED_OUT)
            return
        t = now()
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT daily_streak, last_daily FROM wallets WHERE user_id = ?", (uid,))
            claim = C.claim_daily(row["last_daily"] if row else None, row["daily_streak"] if row else 0, t, self.tz)
            result = None
            if claim is not None:
                result = await E.apply_tx(tx, uid, claim.amount, C.DAILY, t, C.daily_ref(claim.day, uid))
                if result.ok:
                    await tx.execute("UPDATE wallets SET daily_streak = ?, last_daily = ? WHERE user_id = ?",
                                     (claim.streak, claim.day, uid))
        if claim is None or not result.ok:
            await self.reply(interaction, "You've already claimed today's coins. Come back after midnight.")
            return
        streak = f"Day {claim.streak} streak." + ("" if claim.amount < C.daily_amount(99) else " Max bonus.")
        await self.reply(interaction, f"+{coins(claim.amount)}. {streak}\nBalance: {coins(result.balance)}.",
                         ephemeral=False, title="Daily coins", footer=style.label("coins", "daily"))

    # ------------------------------------------------------------ /balance
    async def ranks(self) -> dict[int, int]:
        rows = await self.db.fetchall("SELECT user_id, balance FROM wallets WHERE balance > 0")
        return {r["user_id"]: r["balance"] for r in rows}

    @app_commands.command(name="balance", description="Coins, daily streak and rank for you or someone else")
    @app_commands.describe(member="Whose wallet (default: you)")
    async def balance(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        member = member or interaction.user
        if member.bot:
            await self.reply(interaction, "Bots don't have wallets.")
            return
        row = await self.db.fetchone("SELECT balance, daily_streak, last_daily FROM wallets WHERE user_id = ?",
                                     (member.id,))
        bal = row["balance"] if row else 0
        streak = C.current_streak(row["last_daily"], row["daily_streak"], now(), self.tz) if row else 0
        top, mine = S.rank(await self.ranks(), limit=0, me=member.id)
        rank = f"#{mine.rank}" if mine else "unranked"
        text = f"`BALANCE`  {coins(bal)}\n`STREAK`  {streak} day{'' if streak == 1 else 's'}\n`RANK`  {rank}"
        await self.reply(interaction, text, ephemeral=False, title=member.display_name,
                         footer=style.label("coins", "balance"))

    # ------------------------------------------------------------ /give
    @app_commands.command(name="give", description="Give some of your coins to another member")
    @app_commands.describe(member="Who gets the coins", amount="How many")
    async def give(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        giver = interaction.user
        error = C.give_error(giver.id, member.id, member.bot, amount)
        if error:
            await self.reply(interaction, error)
            return
        result = await E.transfer(self.db, giver.id, member.id, amount, C.GIVE, now())
        if not result.ok:
            await self.reply(interaction, f"You only have {coins(result.balance)}.")
            return
        await self.reply(interaction, f"<@{giver.id}> gave <@{member.id}> {coins(amount)}.",
                         ephemeral=False, footer=style.label("coins", "give"))

    # ------------------------------------------------------------ /coinflip
    @app_commands.command(name="coinflip", description="Bet coins on a coin flip; a win pays double")
    @app_commands.describe(bet=f"{C.MIN_BET} to {C.MAX_BET:,} coins", side="Heads or tails")
    @app_commands.choices(side=SIDE_CHOICES)
    async def coinflip(self, interaction: discord.Interaction, bet: int, side: app_commands.Choice[str]) -> None:
        uid = interaction.user.id
        t = now()
        async with self.db.transaction() as tx:
            # Checked inside the transaction: two flips at once can't spend the same coins.
            error = C.bet_error(bet, await E.balance_tx(tx, uid))
            if error is None:
                landed = C.flip(self.rng)
                won = landed == side.value
                taken = await E.apply_tx(tx, uid, -bet, C.COINFLIP, t)
                if not taken.ok:  # can't happen after bet_error, but never pay out on a failed take
                    error = f"You only have {coins(taken.balance)}."
                else:
                    after = (await E.apply_tx(tx, uid, 2 * bet, C.COINFLIP, t)).balance if won else taken.balance
        if error:
            await self.reply(interaction, error)
            return
        net = C.coinflip_net(bet, won)
        verdict = f"You won {coins(net)}!" if won else f"You lost {coins(bet)}."
        await self.reply(interaction, f"It's **{landed}**. {verdict}\nBalance: {coins(after)}.", ephemeral=False,
                         title="Coin flip", footer=style.label("coins", "coinflip", side.value))

    # ------------------------------------------------------------ /richest
    @app_commands.command(name="richest", description="The 10 biggest wallets")
    async def richest(self, interaction: discord.Interaction) -> None:
        top, mine = S.rank(await self.ranks(), limit=10, me=interaction.user.id)
        if not top:
            text = "Nobody has any coins yet. Try `/daily`."
        else:
            rows = [f"`{r.rank:02}`  <@{r.user_id}>  {coins(r.score)}" for r in top]
            if mine:
                rows += ["…", f"`{mine.rank:02}`  <@{mine.user_id}>  {coins(mine.score)}"]
            text = "\n".join(rows)
        await self.reply(interaction, text, ephemeral=False, title="Richest", footer=style.label("coins", "richest"))


async def setup(bot) -> None:
    await bot.add_cog(Economy(bot))
