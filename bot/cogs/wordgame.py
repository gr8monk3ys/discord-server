"""Daily Word: /word guess, /word today, /word stats, /word leaderboard.

One five-letter word a day (Pacific midnight), the same for everyone, picked from the date by
logic/wordgame.py so restarts agree. Every reply about the game is ephemeral so nobody is
spoiled. Each member's game is one `word_games` row (day, user): guesses as a comma string,
solved, finished_at. A finished game posts a squares-only share line in the games channel
(no letters, no pings) and a win pays 50 coins + 10 per unused guess once per day (ledger ref
word:DAY:USER). Accounts younger than the starter quest's MIN_ACCOUNT_DAYS play but aren't paid.

Playing is something a member does on purpose, not activity tracking, so /privacy off doesn't
stop it; opted-out members are left off the public leaderboard."""

import functools
import logging
import time
from dataclasses import dataclass, field
from datetime import date

import discord
from discord import app_commands
from discord.ext import commands

import config
import economy
import style
from logic import wordgame as W

log = logging.getLogger(__name__)

FOOTER = style.label("daily word", "new word at midnight pacific")


def now() -> int:
    return int(time.time())


def created_ts(member) -> int | None:
    created = getattr(member, "created_at", None)
    return int(created.timestamp()) if created is not None else None


def never_raise(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception:
            log.exception("wordgame: %s failed", fn.__name__)
            return None
    return wrapper


@dataclass
class Outcome:
    day: date
    answer: str
    guesses: list[str] = field(default_factory=list)
    error: str | None = None
    finished_now: bool = False  # this guess ended the game
    coins: int = 0
    unpaid_young: bool = False  # won, but the account is too new to be paid

    @property
    def number(self) -> int:
        return W.puzzle_number(self.day)

    @property
    def done(self) -> bool:
        return W.finished(self.guesses, self.answer)

    @property
    def won(self) -> bool:
        return W.solved(self.guesses, self.answer)


class WordGame(commands.Cog):
    word = app_commands.Group(name="word", description=f"{W.NAME}: one five-letter word a day",
                              guild_only=True)

    def __init__(self, bot, words: W.Words | None = None):
        self.bot = bot
        self.words = words or W.Words.load()

    @property
    def db(self):
        return self.bot.db

    def guild(self) -> discord.Guild | None:
        return self.bot.get_guild(self.bot.settings.guild_id)

    def today(self, t: int | None = None) -> date:
        return W.local_day(now() if t is None else t, self.bot.settings.tz)

    def answer(self, day: date) -> str:
        return W.answer_for(day, self.words.answers)

    # ------------------------------------------------------------ state
    async def game(self, day: date, user_id: int) -> list[str]:
        row = await self.db.fetchone("SELECT guesses FROM word_games WHERE day = ? AND user_id = ?",
                                     (day.isoformat(), user_id))
        return W.parse_guesses(row["guesses"]) if row else []

    async def play(self, member, raw: str, t: int) -> Outcome:
        """Validate and record one guess; on a win, pay once. All in one transaction so two
        quick guesses can't both land as guess six or pay twice."""
        day = self.today(t)
        out = Outcome(day=day, answer=self.answer(day))
        uid = member.id
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT guesses FROM word_games WHERE day = ? AND user_id = ?",
                                    (day.isoformat(), uid))
            out.guesses = W.parse_guesses(row["guesses"]) if row else []
            if out.done:
                out.error = "You've finished today's word. A new one drops at midnight Pacific."
                return out
            guess, error = W.validate(raw, self.words.allowed, out.guesses)
            if error:
                out.error = error
                return out
            out.guesses.append(guess)
            out.finished_now = out.done
            await tx.execute(
                "INSERT INTO word_games (day, user_id, guesses, solved, finished_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (day, user_id) DO UPDATE SET guesses = excluded.guesses,"
                " solved = excluded.solved, finished_at = excluded.finished_at",
                (day.isoformat(), uid, W.join_guesses(out.guesses), int(out.won), t if out.done else None))
            if out.won:
                if W.payable(created_ts(member), t):
                    amount = W.reward(True, len(out.guesses))
                    result = await economy.apply_tx(tx, uid, amount, W.REASON, t, ref=W.ref(day, uid))
                    out.coins = amount if result.ok else 0
                else:
                    out.unpaid_young = True
        if out.finished_now:
            log.info("wordgame: %s finished #%s (%s)", uid, out.number,
                     f"{len(out.guesses)}/{W.MAX_GUESSES}" if out.won else "X")
        return out

    # ------------------------------------------------------------ views
    def board_embed(self, out: Outcome) -> discord.Embed:
        n = len(out.guesses)
        if out.done and out.won:
            title = f"{W.NAME} #{out.number} · solved in {n}/{W.MAX_GUESSES}"
        elif out.done:
            title = f"{W.NAME} #{out.number} · X/{W.MAX_GUESSES}"
        else:
            title = f"{W.NAME} #{out.number} · {n}/{W.MAX_GUESSES}"
        parts = []
        if out.guesses:
            parts.append(W.board_text(out.guesses, out.answer))
            parts.append(W.keyboard_text(out.guesses, out.answer))
        else:
            parts.append(f"Guess the {W.LENGTH}-letter word in {W.MAX_GUESSES} tries with `/word guess`.\n"
                         "🟩 right spot · 🟨 in the word, wrong spot · ⬛ not in the word")
        if out.done:
            if out.won and out.finished_now:
                if out.coins:
                    parts.append(f"Nice! **+{out.coins} coins**.")
                elif out.unpaid_young:
                    parts.append("Nice! (Coins are for accounts older than a month.)")
                else:
                    parts.append("Nice!")
            elif not out.won:
                parts.append(f"Out of guesses. The word was **{out.answer.upper()}**.")
            parts.append("See you tomorrow.")
        return style.embed(title=title, description="\n\n".join(parts), footer=FOOTER)

    # ------------------------------------------------------------ share
    @never_raise
    async def share(self, member, out: Outcome) -> bool:
        guild = self.guild()
        channel = config.match_by_name(guild.text_channels, config.GAMES_CHANNEL) if guild else None
        if channel is None:
            log.info("wordgame: no %s channel to share in", config.GAMES_CHANNEL)
            return False
        mention = f"<@{member.id}>"
        await channel.send(W.share_line(mention, out.number, out.guesses, out.answer),
                           allowed_mentions=discord.AllowedMentions.none())
        return True

    # ------------------------------------------------------------ commands
    @word.command(name="guess", description="Guess today's word (only you see the board)")
    @app_commands.describe(word="A five-letter word")
    async def guess(self, interaction: discord.Interaction, word: app_commands.Range[str, 1, 32]) -> None:
        member = interaction.user
        out = await self.play(member, word, now())
        if out.error:
            await interaction.response.send_message(
                out.error, embed=self.board_embed(out) if out.guesses else discord.utils.MISSING, ephemeral=True)
            return
        await interaction.response.send_message(embed=self.board_embed(out), ephemeral=True)
        if out.finished_now:
            await self.share(member, out)

    @word.command(name="today", description="Your board for today's word (only you see it)")
    async def today_cmd(self, interaction: discord.Interaction) -> None:
        day = self.today()
        out = Outcome(day=day, answer=self.answer(day), guesses=await self.game(day, interaction.user.id))
        await interaction.response.send_message(embed=self.board_embed(out), ephemeral=True)

    async def results(self, user_id: int) -> list[W.Result]:
        rows = await self.db.fetchall(
            "SELECT day, guesses, solved FROM word_games WHERE user_id = ? AND finished_at IS NOT NULL",
            (user_id,))
        return [W.Result(date.fromisoformat(r["day"]), bool(r["solved"]), len(W.parse_guesses(r["guesses"])))
                for r in rows]

    @word.command(name="stats", description="Your Daily Word stats (only you see them)")
    async def stats_cmd(self, interaction: discord.Interaction) -> None:
        s = W.stats(await self.results(interaction.user.id), self.today())
        await interaction.response.send_message(
            embed=style.embed(title=f"{W.NAME} · your stats", description=W.stats_text(s), footer=FOOTER),
            ephemeral=True)

    async def board(self, today: date) -> list[tuple[int, int]]:
        start = W.month_start(today)
        rows = await self.db.fetchall(
            "SELECT day, user_id, guesses, solved FROM word_games WHERE day >= ? AND day <= ?"
            " AND finished_at IS NOT NULL AND user_id NOT IN (SELECT user_id FROM privacy_optout)",
            (start.isoformat(), today.isoformat()))
        by_user: dict[int, list[W.Result]] = {}
        for r in rows:
            by_user.setdefault(r["user_id"], []).append(
                W.Result(date.fromisoformat(r["day"]), bool(r["solved"]), len(W.parse_guesses(r["guesses"]))))
        return W.leaderboard(by_user, start, today)

    @word.command(name="leaderboard", description="Best Daily Word streaks this month")
    async def leaderboard_cmd(self, interaction: discord.Interaction) -> None:
        today = self.today()
        top = await self.board(today)
        if top:
            text = "\n".join(f"`{i:02}`  <@{uid}>  🔥 {best} day{'s' if best != 1 else ''}"
                             for i, (uid, best) in enumerate(top, start=1))
        else:
            text = "No streaks yet this month. Start one with `/word guess`."
        embed = style.embed(title=f"{W.NAME} · best streaks · {today:%B %Y}", description=text, footer=FOOTER)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot) -> None:
    await bot.add_cog(WordGame(bot))
