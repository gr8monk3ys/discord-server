"""Monkey SMP referrals (rules in logic/referral.py): `/referral` and the referral unlock.

Off unless REFERRALS=on in server/.env; then `/referral` gives a member with a linked Minecraft name
their personal code, and cogs/tebex.py hands this cog each signed `payment.completed`,
`payment.refunded` and `payment.dispute.opened` webhook.

- A purchase carrying a known code, for a package that counts, from a buyer who was never referred
  before and isn't the code's owner, is recorded once (`referral_claims`). Then the unlock: posted in
  🛡️・mod for staff to run over RCON (REFERRAL_GRANT=staff, the default), or sent through the Monkey SMP
  bridge (REFERRAL_GRANT=rcon), which takes only the unlock's own commands. Whatever RCON didn't do is
  posted for staff. The code's owner gets a DM.
- If neither RCON nor the staff post got the unlock out, the claim is dropped and the answer is 500, so
  Tebex retries later (cogs/tebex.py forgets the webhook id too).
- A self-referral, or a code whose owner has no Minecraft name, gets a staff note. Repeats and unknown
  codes are quiet.
- A refund or a chargeback on a referral's transaction posts the commands that would undo it; nothing
  is taken back by the bot.
"""

import logging
import os
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
from logic import referral as L

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
OFF = "Referrals aren't open yet."
NEED_LINK = ("Link your Minecraft name first with `/smp link`: the referral skin goes on that name, so it needs "
             "to know which one is yours.")
CODE_TRIES = 5


def now() -> int:
    return int(time.time())


def esc(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text or ""))


class Referrals(commands.Cog):
    def __init__(self, bot, environ=None):
        self.bot = bot
        self.settings, self.problems = L.read_settings(os.environ if environ is None else environ)

    async def cog_load(self) -> None:
        for problem in self.problems:
            log.warning("referral: %s", problem)
        if self.settings is None:
            log.info("referral: off (%s is not on)", L.ENV_ON)

    @property
    def on(self) -> bool:
        return self.settings is not None

    @property
    def db(self):
        return self.bot.db

    async def linked(self, user_id: int, tx=None) -> str | None:
        row = await (tx or self.db).fetchone(
            "SELECT name FROM monkey_links WHERE user_id = ? AND state = 'ok'", (user_id,))
        return row["name"] if row else None

    # ------------------------------------------------------------ /referral
    @app_commands.command(name="referral",
                          description="Your Monkey SMP referral code: a friend's first pass gets you both a skin")
    @app_commands.guild_only()
    async def referral(self, interaction: discord.Interaction) -> None:
        if not self.on:
            await interaction.response.send_message(OFF, ephemeral=True)
            return
        user = interaction.user
        name = await self.linked(user.id)
        if name is None:
            await interaction.response.send_message(NEED_LINK, ephemeral=True)
            return
        code = await self.code_for(user.id, name)
        await interaction.response.send_message(L.code_text(code, name, esc), ephemeral=True)

    async def code_for(self, user_id: int, name: str, make=L.new_code) -> str:
        """The member's code, made the first time; the name it is kept with is refreshed."""
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT code FROM referral_codes WHERE user_id = ?", (user_id,))
            if row is not None:
                await tx.execute("UPDATE referral_codes SET name = ? WHERE user_id = ?", (name, user_id))
                return row["code"]
            for _ in range(CODE_TRIES):
                code = make()
                if await tx.fetchone("SELECT 1 FROM referral_codes WHERE code = ?", (code,)) is None:
                    await tx.execute("INSERT INTO referral_codes (user_id, code, name, created_at) VALUES (?, ?, ?, ?)",
                                     (user_id, code, name, now()))
                    return code
        raise RuntimeError("couldn't make an unused referral code")

    # ------------------------------------------------------------ webhooks (from cogs/tebex.py)
    async def handle(self, kind: str, subject: dict) -> bool:
        """True when the webhook is dealt with (answer 200); False to have Tebex retry it."""
        if not self.on:
            return True
        if kind == L.PAYMENT_COMPLETED:
            return await self.completed(subject)
        if kind in (L.PAYMENT_REFUNDED, L.CHARGEBACK):
            return await self.reversed(kind, subject)
        return True

    async def completed(self, subject: dict) -> bool:
        p = L.purchase(subject, self.settings)
        if not p.codes or not p.transaction:
            return True
        async with self.db.transaction() as tx:
            owner = None
            for code in p.codes:
                owner = await tx.fetchone("SELECT user_id, code, name FROM referral_codes WHERE code = ?", (code,))
                if owner is not None:
                    break
            referrer = None
            buyer_owner = None
            buyer_claimed = transaction_claimed = False
            if owner is not None:
                referrer = await self.linked(owner["user_id"], tx) or owner["name"]
                if p.buyer:
                    row = await tx.fetchone("SELECT user_id FROM monkey_links WHERE name_key = ?", (p.buyer.lower(),))
                    buyer_owner = row["user_id"] if row else None
                    buyer_claimed = await tx.fetchone(
                        "SELECT 1 FROM referral_claims WHERE buyer_key = ?", (p.buyer.lower(),)) is not None
                transaction_claimed = await tx.fetchone(
                    "SELECT 1 FROM referral_claims WHERE transaction_id = ?", (p.transaction,)) is not None
            outcome = L.decide(p, self.settings, code_owner=owner["user_id"] if owner else None,
                               referrer_name=referrer, buyer_owner=buyer_owner, buyer_claimed=buyer_claimed,
                               transaction_claimed=transaction_claimed)
            if outcome == L.OK:
                await tx.execute(
                    "INSERT INTO referral_claims (transaction_id, code, referrer_id, referrer_name, buyer_name,"
                    " buyer_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (p.transaction, owner["code"], owner["user_id"], referrer, p.buyer, p.buyer.lower(), now()))
        if outcome != L.OK:
            log.info("referral: a purchase's code didn't count (%s)", outcome)
            note = L.problem_text(outcome, p.buyer, esc)
            if note:
                await self.post(note)
            return True
        if await self.grant(owner["user_id"], referrer, p.buyer):
            await self.dm(owner["user_id"], L.referrer_dm(p.buyer, esc))
            return True
        await self.db.execute("DELETE FROM referral_claims WHERE transaction_id = ?", (p.transaction,))
        return False

    async def grant(self, referrer_id: int, referrer: str, buyer: str) -> bool:
        """The unlock: over RCON when the owner chose it, the rest (or all of it) posted for staff."""
        commands_ = L.grant_commands(referrer, buyer)
        sent = 0
        if self.settings.grant == L.GRANT_RCON:
            bridge = self.bot.get_cog("MonkeySmp")
            for command in commands_:
                try:
                    await bridge.grant_call(command)
                except Exception as exc:  # RconError, or no bridge loaded
                    log.warning("referral: RCON stopped after %s of %s (%s)", sent, len(commands_), type(exc).__name__)
                    break
                sent += 1
        posted = await self.post(L.staff_text(referrer_id, referrer, buyer, sent, commands_, esc))
        return posted or sent == len(commands_)

    async def reversed(self, kind: str, subject: dict) -> bool:
        transaction = subject.get("transaction_id") if isinstance(subject, dict) else None
        if not isinstance(transaction, str) or not transaction:
            return True
        changed = await self.db.execute(
            "UPDATE referral_claims SET status = 'refunded', refunded_at = ? WHERE transaction_id = ? AND status = 'granted'",
            (now(), transaction.strip()[:80]))
        if not changed:
            return True
        row = await self.db.fetchone("SELECT * FROM referral_claims WHERE transaction_id = ?", (transaction.strip()[:80],))
        if await self.post(L.refund_text(row["referrer_id"], row["referrer_name"], row["buyer_name"], kind, esc)):
            return True
        await self.db.execute("UPDATE referral_claims SET status = 'granted', refunded_at = NULL WHERE id = ?",
                              (row["id"],))
        return False

    # ------------------------------------------------------------ Discord
    async def post(self, text: str) -> bool:
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        channel = config.match_by_name(guild.text_channels, config.MOD_CHANNEL) if guild is not None else None
        if channel is None:
            log.warning("referral: no %s channel", config.MOD_CHANNEL)
            return False
        try:
            await channel.send(text, allowed_mentions=NO_PINGS)
        except discord.HTTPException as exc:
            log.warning("referral: couldn't post in %s (%s)", config.MOD_CHANNEL, type(exc).__name__)
            return False
        return True

    async def dm(self, user_id: int, text: str) -> bool:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(text, allowed_mentions=NO_PINGS)
            return True
        except (discord.HTTPException, AttributeError):
            return False

    async def forget_code(self, user_id: int) -> None:
        """Leaving or a ban retires the member's code: purchases with it count for nobody from then on.
        Claims already made stay, as the record of a cosmetic that was given."""
        await self.db.execute("DELETE FROM referral_codes WHERE user_id = ?", (user_id,))

    @commands.Cog.listener()
    async def on_member_remove(self, member) -> None:
        try:
            if member.guild.id == self.bot.settings.guild_id:
                await self.forget_code(member.id)
        except Exception:
            log.exception("referral: handling a leave failed")

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user) -> None:
        try:
            if guild.id == self.bot.settings.guild_id:
                await self.forget_code(user.id)
        except Exception:
            log.exception("referral: handling a ban failed")


async def setup(bot) -> None:
    await bot.add_cog(Referrals(bot))
