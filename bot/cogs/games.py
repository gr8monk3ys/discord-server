"""Module 4b: more games. /slots, /blackjack, /trivia and /predict.

Coins only move through bot/economy.py. A bet is checked and taken in one
transaction when the game starts, so two games can't spend the same coins.
Blackjack hands live in memory with their bet in `blackjack_open`, and are
refunded on startup if the bot restarted mid-hand. Predictions live in the
database and their buttons are DynamicItems, so they survive restarts. Public
posts never ping anyone."""

import asyncio
import functools
import logging
import random
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
import economy
import style
from errors import reply_error
from logic import blackjack as bj
from logic import games as rules
from logic.games import BetProblem, Click, PredictionRefusal, Stake

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()
TRIVIA_DOWN = "Trivia's down right now. Try again in a bit."
BLACKJACK_BUSY = "You already have a blackjack hand going. Finish that one first."
REFUSALS = {
    PredictionRefusal.NOT_OPEN: "Betting on this prediction is closed.",
    PredictionRefusal.CREATOR: "You made this prediction, so you can't bet on it.",
    PredictionRefusal.SWITCH: "You've already bet on the other option, and you can't switch sides.",
}
OUTCOME_TEXT = {
    bj.Outcome.BLACKJACK: "Blackjack! Paid 3:2.",
    bj.Outcome.WIN: "You win.",
    bj.Outcome.PUSH: "Push: your bet is back.",
    bj.Outcome.LOSE: "Dealer wins.",
}
TRIVIA_CHOICES = [app_commands.Choice(name=n, value=i) for n, i in rules.TRIVIA_CATEGORIES.items()]
PRED_BUTTONS = {  # action -> (label, style, row)
    "a": ("Bet A", discord.ButtonStyle.primary, 0),
    "b": ("Bet B", discord.ButtonStyle.primary, 0),
    "lock": ("Lock", discord.ButtonStyle.secondary, 1),
    "resolve_a": ("Resolve A", discord.ButtonStyle.success, 1),
    "resolve_b": ("Resolve B", discord.ButtonStyle.success, 1),
    "cancel": ("Cancel", discord.ButtonStyle.danger, 1),
}


def now() -> int:
    return int(time.time())


def name_of(user) -> str:
    return discord.utils.escape_markdown(getattr(user, "display_name", None) or str(user.id))


async def fetch_json(url: str):
    """GET a JSON document. Raises on network errors and non-2xx replies."""
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json(content_type=None)


def never_raise(fn):
    """Timers and timeouts log and carry on."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception:
            log.exception("games: %s failed", fn.__name__)
    return wrapper


# ---------------------------------------------------------------- blackjack UI
class BlackjackView(discord.ui.View):
    """Hit / Stand for one hand. Discord's view timeout restarts on every press, so
    `on_timeout` fires after IDLE_SECONDS without one: that's a Stand."""

    def __init__(self, cog: "Games", user, game: bj.Game, started_at: int, origin: discord.Interaction):
        super().__init__(timeout=bj.IDLE_SECONDS)
        self.cog = cog
        self.user_id = user.id
        self.player_name = name_of(user)
        self.game = game
        self.started_at = started_at
        self.origin = origin  # the /blackjack interaction: its message is the table
        self.ref = f"blackjack:{origin.id}"
        self.lock = asyncio.Lock()
        self.note: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your hand. Start your own with /blackjack.",
                                                    ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.blackjack_move(interaction, self, "hit")

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.blackjack_move(interaction, self, "stand")

    def disable(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_timeout(self) -> None:
        await self.cog.blackjack_timeout(self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log.error("blackjack button failed", exc_info=error)
        await reply_error(interaction)


# ---------------------------------------------------------------- trivia UI
class TriviaView(discord.ui.View):
    def __init__(self, cog: "Games", round_: rules.TriviaRound, channel_id: int, deadline: int):
        super().__init__(timeout=None)  # the 20 s limit is a hard deadline, not an idle timeout
        self.cog = cog
        self.round = round_
        self.channel_id = channel_id
        self.deadline = deadline
        self.message = None
        self.timer: asyncio.Task | None = None
        for index, answer in enumerate(round_.question.answers):
            button = discord.ui.Button(label=rules.button_label(answer), style=discord.ButtonStyle.secondary)
            button.callback = functools.partial(cog.trivia_click, view=self, index=index)
            self.add_item(button)

    def disable(self) -> None:
        for index, item in enumerate(self.children):
            item.disabled = True
            if index == self.round.question.correct:
                item.style = discord.ButtonStyle.success

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        log.error("trivia button failed", exc_info=error)
        await reply_error(interaction)


# ---------------------------------------------------------------- prediction UI
class PredictionButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"pred:(?P<action>a|b|lock|resolve_a|resolve_b|cancel):(?P<id>\d+)"):
    def __init__(self, action: str, prediction_id: int, label: str | None = None, disabled: bool = False):
        text, button_style, row = PRED_BUTTONS[action]
        super().__init__(discord.ui.Button(label=rules.button_label(label or text), style=button_style,
                                           disabled=disabled, row=row,
                                           custom_id=f"pred:{action}:{prediction_id}"))
        self.action = action
        self.prediction_id = prediction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Games" = interaction.client.get_cog("Games")
        try:
            await cog.handle_prediction_button(interaction, self.action, self.prediction_id)
        except Exception:
            log.exception("prediction button %s on %s failed", self.action, self.prediction_id)
            await reply_error(interaction)


def build_prediction_view(row) -> discord.ui.View:
    status = row["status"]
    pid = row["id"]
    betting_closed = status != "open"
    finished = status in ("resolved", "cancelled")
    view = discord.ui.View(timeout=None)
    view.add_item(PredictionButton("a", pid, f"Bet A: {row['option_a']}", disabled=betting_closed))
    view.add_item(PredictionButton("b", pid, f"Bet B: {row['option_b']}", disabled=betting_closed))
    view.add_item(PredictionButton("lock", pid, disabled=betting_closed))
    for action in ("resolve_a", "resolve_b", "cancel"):
        view.add_item(PredictionButton(action, pid, disabled=finished))
    return view


class BetModal(discord.ui.Modal):
    def __init__(self, cog: "Games", prediction_id: int, option: str, option_text: str, already: int):
        super().__init__(title=rules.button_label(f"Bet on {option_text}", 45))
        self.cog = cog
        self.prediction_id = prediction_id
        self.option = option
        hint = (f"Adds to the {already:,} you've staked." if already
                else f"{rules.MIN_BET} to {rules.MAX_BET:,} coins, taken now.")
        self.amount = discord.ui.TextInput(placeholder=str(rules.MIN_BET), min_length=1, max_length=7)
        self.add_item(discord.ui.Label(text="Coins", description=hint, component=self.amount))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.place_bet(interaction, self.prediction_id, self.option, self.amount.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("prediction bet modal failed", exc_info=error)
        await reply_error(interaction)


# ---------------------------------------------------------------- the cog
class Games(commands.Cog):
    predict = app_commands.Group(name="predict", description="Bet coins on how something turns out",
                                 guild_only=True)

    def __init__(self, bot, rng: random.Random | None = None, fetch=None, clock=None):
        self.bot = bot
        self.rng = rng or random.Random()
        self.fetch = fetch or fetch_json
        self.clock = clock or now
        self.trivia_seconds = rules.TRIVIA_SECONDS
        self.blackjack_hands: dict[int, BlackjackView] = {}  # user id -> live hand
        self.trivia_rounds: dict[int, TriviaView | None] = {}  # channel id -> live round (None: loading)
        self.timers: set[asyncio.Task] = set()

    @property
    def db(self):
        return self.bot.db

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(PredictionButton)
        await self.refund_open_blackjack()

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(PredictionButton)
        for view in list(self.blackjack_hands.values()):
            view.stop()  # the next cog_load refunds their bets
        for task in list(self.timers):
            task.cancel()

    def start_timer(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.timers.add(task)
        task.add_done_callback(self.timers.discard)
        return task

    # ------------------------------------------------------------ bets
    async def take_bet(self, tx, user_id: int, bet: int, reason: str, already: int = 0) -> str | None:
        """Check the bet against the balance and take it, inside the caller's
        transaction. Returns the refusal text, or None when the coins are taken."""
        balance = await economy.balance_tx(tx, user_id)
        problem = rules.bet_problem(bet, balance, already)
        if problem is not None:
            return rules.bet_reply(problem, balance, already)
        taken = await economy.apply_tx(tx, user_id, -bet, reason, self.clock())
        if not taken.ok:
            return rules.bet_reply(BetProblem.BROKE, taken.balance)
        return None

    # ------------------------------------------------------------ /slots
    @app_commands.command(name="slots", description="Spin three reels for coins")
    @app_commands.describe(bet=f"Coins to bet ({rules.MIN_BET} to {rules.MAX_BET:,})")
    @app_commands.guild_only()
    async def slots(self, interaction: discord.Interaction,
                    bet: app_commands.Range[int, rules.MIN_BET, rules.MAX_BET]) -> None:
        await self.play_slots(interaction, bet)

    async def play_slots(self, interaction: discord.Interaction, bet: int) -> None:
        user = interaction.user
        async with self.db.transaction() as tx:
            refused = await self.take_bet(tx, user.id, bet, "slots")
            if refused is None:
                reels = rules.spin(self.rng)
                won = rules.slots_payout(reels, bet)
                if won:
                    await economy.apply_tx(tx, user.id, won, "slots", self.clock(), ref=f"slots:{interaction.id}")
                balance = await economy.balance_tx(tx, user.id)
        if refused:
            await interaction.response.send_message(refused, ephemeral=True)
            return
        line = " │ ".join(reels)
        result = (f"**{name_of(user)}** bet {bet:,} and won **{won:,}** coins." if won
                  else f"**{name_of(user)}** bet {bet:,} and lost.")
        embed = style.embed(title="🎰 Slots", description=f"# {line}\n{result}",
                            footer=style.label("slots", f"balance {balance:,}"),
                            color=style.FOREST if won else style.MUTED)
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)

    # ------------------------------------------------------------ /blackjack
    @app_commands.command(name="blackjack", description="Play a hand of blackjack against the dealer")
    @app_commands.describe(bet=f"Coins to bet ({rules.MIN_BET} to {rules.MAX_BET:,})")
    @app_commands.guild_only()
    async def blackjack(self, interaction: discord.Interaction,
                        bet: app_commands.Range[int, rules.MIN_BET, rules.MAX_BET]) -> None:
        await self.start_blackjack(interaction, bet)

    async def start_blackjack(self, interaction: discord.Interaction, bet: int) -> None:
        user = interaction.user
        started = self.clock()
        game = None
        async with self.db.transaction() as tx:
            if await tx.fetchone("SELECT 1 FROM blackjack_open WHERE user_id = ?", (user.id,)):
                refused = BLACKJACK_BUSY
            else:
                refused = await self.take_bet(tx, user.id, bet, "blackjack")
            if refused is None:
                game = bj.Game.deal(bet, rng=self.rng)
                if game.finished:  # a natural on the deal: settled right here
                    if game.payout:
                        await economy.apply_tx(tx, user.id, game.payout, "blackjack", self.clock(),
                                               ref=f"blackjack:{interaction.id}")
                else:
                    await tx.execute("INSERT INTO blackjack_open (user_id, bet, started_at) VALUES (?, ?, ?)",
                                     (user.id, bet, started))
        if refused:
            await interaction.response.send_message(refused, ephemeral=True)
            return
        view = BlackjackView(self, user, game, started, interaction)
        if game.finished:
            view.disable()
            view.stop()
        else:
            self.blackjack_hands[user.id] = view
        try:
            await interaction.response.send_message(embed=self.render_blackjack(view), view=view,
                                                    allowed_mentions=NO_PINGS)
        except Exception:
            if not game.finished:  # no table to play on: hand the bet back
                view.stop()
                self.blackjack_hands.pop(user.id, None)
                await self.refund_blackjack(user.id, started)
            raise

    async def blackjack_move(self, interaction: discord.Interaction, view: BlackjackView, action: str) -> None:
        if interaction.user.id != view.user_id:
            await interaction.response.send_message("This isn't your hand. Start your own with /blackjack.",
                                                    ephemeral=True)
            return
        async with view.lock:
            if view.game.finished:
                await interaction.response.send_message("This hand is over.", ephemeral=True)
                return
            if action == "hit":
                view.game.hit()
            else:
                view.game.stand()
            if view.game.finished:
                await self.finish_blackjack(view)
            await interaction.response.edit_message(embed=self.render_blackjack(view), view=view,
                                                    allowed_mentions=NO_PINGS)

    @never_raise
    async def blackjack_timeout(self, view: BlackjackView) -> None:
        async with view.lock:
            if view.game.finished:
                return
            view.game.stand()
            view.note = "Idle for 2 minutes, so that's a Stand."
            await self.finish_blackjack(view)
        await view.origin.edit_original_response(embed=self.render_blackjack(view), view=view,
                                                 allowed_mentions=NO_PINGS)

    async def finish_blackjack(self, view: BlackjackView) -> None:
        """Close the open bet and pay out, once. If the row is already gone (the bet
        was refunded on a reload), nothing is paid."""
        view.disable()
        view.stop()
        if self.blackjack_hands.get(view.user_id) is view:
            del self.blackjack_hands[view.user_id]
        async with self.db.transaction() as tx:
            cur = await tx.execute("DELETE FROM blackjack_open WHERE user_id = ? AND started_at = ?",
                                   (view.user_id, view.started_at))
            if cur.rowcount and view.game.payout:
                await economy.apply_tx(tx, view.user_id, view.game.payout, "blackjack", self.clock(), ref=view.ref)
            elif not cur.rowcount:
                view.note = "This hand's bet was already refunded."

    async def refund_blackjack(self, user_id: int, started_at: int) -> None:
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM blackjack_open WHERE user_id = ? AND started_at = ?",
                                    (user_id, started_at))
            if row is not None:
                await self.refund_row(tx, row)

    async def refund_row(self, tx, row) -> None:
        await economy.apply_tx(tx, row["user_id"], row["bet"], "blackjack", self.clock(),
                               ref=f"bjrefund:{row['user_id']}:{row['started_at']}")
        await tx.execute("DELETE FROM blackjack_open WHERE user_id = ?", (row["user_id"],))

    async def refund_open_blackjack(self) -> None:
        """Startup: any hand still open was cut off by a restart. Give the bets back."""
        try:
            async with self.db.transaction() as tx:
                rows = await tx.fetchall("SELECT * FROM blackjack_open")
                for row in rows:
                    await self.refund_row(tx, row)
            if rows:
                log.info("refunded %d blackjack bet(s) left open by a restart", len(rows))
        except Exception:
            log.exception("couldn't refund open blackjack bets")

    def render_blackjack(self, view: BlackjackView) -> discord.Embed:
        game = view.game
        cards = lambda hand: " ".join(f"`{c}`" for c in hand)  # noqa: E731
        if game.finished:
            dealer = f"{cards(game.dealer)}  ({bj.total(game.dealer)})"
        else:
            dealer = f"`{game.dealer[0]}` `??`"
        lines = [f"**Dealer** {dealer}",
                 f"**{view.player_name}** {cards(game.player)}  ({bj.total(game.player)})"]
        if game.finished:
            lines += ["", OUTCOME_TEXT[game.result]
                      + (f" +{game.payout:,} coins." if game.payout and game.result is not bj.Outcome.PUSH else "")]
        if view.note:
            lines.append(view.note)
        status = game.result.value if game.finished else "your move"
        color = style.MUTED if game.finished and not game.payout else style.FOREST
        return style.embed(title="🃏 Blackjack", description="\n".join(lines),
                           footer=style.label("blackjack", f"bet {game.bet:,}", status), color=color)

    # ------------------------------------------------------------ /trivia
    @app_commands.command(name="trivia", description="A quick question: the first right answer wins coins")
    @app_commands.describe(category="Pick a topic (optional)")
    @app_commands.choices(category=TRIVIA_CHOICES)
    @app_commands.guild_only()
    async def trivia(self, interaction: discord.Interaction,
                     category: app_commands.Choice[int] | None = None) -> None:
        await self.start_trivia(interaction, category.value if category else None)

    async def start_trivia(self, interaction: discord.Interaction, category_id: int | None) -> None:
        channel_id = interaction.channel_id
        if channel_id in self.trivia_rounds:
            await interaction.response.send_message("There's already a trivia question going in this channel.",
                                                    ephemeral=True)
            return
        self.trivia_rounds[channel_id] = None  # claimed while the question loads
        try:
            await interaction.response.defer(thinking=True)
            question = None
            try:
                question = rules.parse_trivia(await self.fetch(rules.trivia_url(category_id)), self.rng)
            except Exception:
                log.warning("trivia fetch failed", exc_info=True)
            if question is None:
                self.trivia_rounds.pop(channel_id, None)
                await interaction.followup.send(TRIVIA_DOWN, allowed_mentions=NO_PINGS)
                return
            view = TriviaView(self, rules.TriviaRound(question), channel_id, self.clock() + self.trivia_seconds)
            view.message = await interaction.followup.send(embed=self.render_trivia(view), view=view,
                                                           allowed_mentions=NO_PINGS, wait=True)
            self.trivia_rounds[channel_id] = view
            view.timer = self.start_timer(self.trivia_timer(view))
        except Exception:
            self.trivia_rounds.pop(channel_id, None)
            raise

    async def trivia_click(self, interaction: discord.Interaction, view: TriviaView, index: int) -> None:
        round_ = view.round
        if not round_.closed and self.clock() >= view.deadline:
            round_.close()  # the timer hasn't fired yet, but time's up
        result = round_.click(interaction.user.id, index)
        if result is Click.WIN:
            self.end_trivia(view)
            await economy.apply(self.db, interaction.user.id, rules.TRIVIA_PRIZE, "trivia", self.clock(),
                                ref=f"trivia:{view.message.id}")
            await interaction.response.edit_message(embed=self.render_trivia(view, winner=interaction.user),
                                                    view=view, allowed_mentions=NO_PINGS)
            return
        text = {
            Click.WRONG: "Nope, that's not it. One guess each, so you're out for this one.",
            Click.ALREADY: "You've already answered this one.",
            Click.CLOSED: "This question is closed.",
        }[result]
        await interaction.response.send_message(text, ephemeral=True)

    async def trivia_timer(self, view: TriviaView) -> None:
        await asyncio.sleep(self.trivia_seconds)
        await self.expire_trivia(view)

    @never_raise
    async def expire_trivia(self, view: TriviaView) -> None:
        if view.round.closed and view.round.winner is not None:
            return  # already won and shown
        view.round.close()
        if not self.end_trivia(view):
            return
        await view.message.edit(embed=self.render_trivia(view), view=view, allowed_mentions=NO_PINGS)

    def end_trivia(self, view: TriviaView) -> bool:
        """Stop the round and free the channel. False if it was already ended."""
        if view.is_finished():
            return False
        view.disable()
        view.stop()
        if view.timer is not None and view.timer is not asyncio.current_task():
            view.timer.cancel()
        if self.trivia_rounds.get(view.channel_id) is view:
            del self.trivia_rounds[view.channel_id]
        return True

    def render_trivia(self, view: TriviaView, winner=None) -> discord.Embed:
        q = view.round.question
        lines = [f"**{discord.utils.escape_markdown(q.text)}**", ""]
        lines += [f"`{chr(65 + i)}` {discord.utils.escape_markdown(a)}" for i, a in enumerate(q.answers)]
        if winner is not None:
            lines += ["", f"✅ **{name_of(winner)}** got it: {discord.utils.escape_markdown(q.correct_answer)}."
                          f" +{rules.TRIVIA_PRIZE} coins."]
            status, color = "won", style.MUTED
        elif view.round.closed:
            lines += ["", f"⏱️ Time's up. The answer was **{discord.utils.escape_markdown(q.correct_answer)}**."]
            status, color = "closed", style.MUTED
        else:
            lines += ["", f"First right answer in {self.trivia_seconds} seconds wins {rules.TRIVIA_PRIZE} coins."
                          " One guess each."]
            status, color = "open", style.FOREST
        return style.embed(title="❓ Trivia", description="\n".join(lines),
                           footer=style.label("trivia", q.category, q.difficulty, status), color=color)

    # ------------------------------------------------------------ /predict
    @predict.command(name="create", description="Post a prediction others can bet coins on")
    @app_commands.describe(question="What's being predicted", option_a="The first outcome",
                           option_b="The second outcome")
    async def predict_create(self, interaction: discord.Interaction,
                             question: app_commands.Range[str, 1, rules.QUESTION_MAX],
                             option_a: app_commands.Range[str, 1, rules.OPTION_MAX],
                             option_b: app_commands.Range[str, 1, rules.OPTION_MAX]) -> None:
        await self.create_prediction(interaction, question, option_a, option_b)

    async def create_prediction(self, interaction: discord.Interaction, question: str,
                                option_a: str, option_b: str) -> None:
        question, option_a, option_b = question.strip(), option_a.strip(), option_b.strip()
        if not (question and option_a and option_b) or option_a.casefold() == option_b.casefold():
            await interaction.response.send_message("Give a question and two different options.", ephemeral=True)
            return
        async with self.db.transaction() as tx:
            cur = await tx.execute(
                "INSERT INTO predictions (channel_id, creator_id, question, option_a, option_b, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (interaction.channel_id, interaction.user.id, question, option_a, option_b, self.clock()))
            row = await tx.fetchone("SELECT * FROM predictions WHERE id = ?", (cur.lastrowid,))
        try:
            await interaction.response.send_message(embed=self.render_prediction(row, []),
                                                    view=build_prediction_view(row), allowed_mentions=NO_PINGS)
            message = await interaction.original_response()
        except Exception:
            await self.db.execute("DELETE FROM predictions WHERE id = ?", (row["id"],))  # no bets yet
            raise
        await self.db.execute("UPDATE predictions SET message_id = ? WHERE id = ?", (message.id, row["id"]))

    async def load_prediction(self, prediction_id: int, tx=None):
        source = tx or self.db
        row = await source.fetchone("SELECT * FROM predictions WHERE id = ?", (prediction_id,))
        bets = await source.fetchall(
            "SELECT * FROM prediction_bets WHERE prediction_id = ? ORDER BY at, rowid", (prediction_id,))
        return row, bets

    def is_staff(self, user) -> bool:
        """Owner, administrators, Keepers and Moderators."""
        guild = getattr(user, "guild", None)
        if guild is not None and guild.owner_id == user.id:
            return True
        perms = getattr(user, "guild_permissions", None)
        if perms is not None and perms.administrator:
            return True
        roles = list(getattr(user, "roles", []))
        return any(config.match_by_name(roles, name) is not None for name in (config.KEEPER_ROLE, config.MOD_ROLE))

    async def handle_prediction_button(self, interaction: discord.Interaction, action: str,
                                       prediction_id: int) -> None:
        user = interaction.user
        row = await self.db.fetchone("SELECT * FROM predictions WHERE id = ?", (prediction_id,))
        if row is None:
            await interaction.response.send_message("That prediction doesn't exist anymore.", ephemeral=True)
            return
        if action in rules.OPTIONS:
            bet = await self.db.fetchone(
                "SELECT option, amount FROM prediction_bets WHERE prediction_id = ? AND user_id = ?",
                (prediction_id, user.id))
            refusal = rules.prediction_refusal(row["status"], user.id == row["creator_id"],
                                               bet["option"] if bet else None, action)
            if refusal is not None:
                await interaction.response.send_message(REFUSALS[refusal], ephemeral=True)
                return
            await interaction.response.send_modal(
                BetModal(self, prediction_id, action, row[f"option_{action}"], bet["amount"] if bet else 0))
            return

        # Lock and Cancel are safe for the creator (cancel refunds everyone). Picking the
        # winner moves other members' coins, so only staff may: a creator could bet
        # through an alt and then resolve in its favour.
        if action in ("resolve_a", "resolve_b"):
            if not self.is_staff(user):
                await interaction.response.send_message(
                    "Only a mod can pick the winner (a Keeper or Moderator), so nobody resolves their own bet."
                    " You can still Lock or Cancel your prediction.", ephemeral=True)
                return
        elif user.id != row["creator_id"] and not self.is_staff(user):
            await interaction.response.send_message(
                "Only whoever made this prediction, a Keeper or a Moderator can do that.", ephemeral=True)
            return
        if action == "lock":
            changed = await self.db.execute(
                "UPDATE predictions SET status = 'locked' WHERE id = ? AND status = 'open'", (prediction_id,))
            if not changed:
                await interaction.response.send_message(f"This prediction is already {row['status']}.",
                                                        ephemeral=True)
                return
        else:
            winner = {"resolve_a": "a", "resolve_b": "b", "cancel": None}[action]
            paid = await self.settle_prediction(prediction_id, winner)
            if paid is None:
                current = await self.db.fetchone("SELECT status FROM predictions WHERE id = ?", (prediction_id,))
                await interaction.response.send_message(f"This prediction is already {current['status']}.",
                                                        ephemeral=True)
                return
        row, bets = await self.load_prediction(prediction_id)
        await interaction.response.edit_message(embed=self.render_prediction(row, bets),
                                                view=build_prediction_view(row), allowed_mentions=NO_PINGS)

    async def place_bet(self, interaction: discord.Interaction, prediction_id: int, option: str, raw: str) -> None:
        user = interaction.user
        try:
            amount = int(raw.strip().replace(",", "").replace("_", ""))
        except ValueError:
            await interaction.response.send_message("That's not a number of coins.", ephemeral=True)
            return
        reply = None
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM predictions WHERE id = ?", (prediction_id,))
            bet = await tx.fetchone("SELECT * FROM prediction_bets WHERE prediction_id = ? AND user_id = ?",
                                    (prediction_id, user.id))
            if row is None:
                reply = "That prediction doesn't exist anymore."
            else:
                refusal = rules.prediction_refusal(row["status"], user.id == row["creator_id"],
                                                   bet["option"] if bet else None, option)
                already = bet["amount"] if bet else 0
                reply = REFUSALS[refusal] if refusal else await self.take_bet(tx, user.id, amount, "predict", already)
            if reply is None:
                if bet:  # topping up keeps the original bet time (it decides remainder ties)
                    await tx.execute("UPDATE prediction_bets SET amount = amount + ? WHERE prediction_id = ?"
                                     " AND user_id = ?", (amount, prediction_id, user.id))
                else:
                    await tx.execute("INSERT INTO prediction_bets (prediction_id, user_id, option, amount, at)"
                                     " VALUES (?, ?, ?, ?, ?)", (prediction_id, user.id, option, amount, self.clock()))
                staked = already + amount
        if reply:
            await interaction.response.send_message(reply, ephemeral=True)
            return
        await interaction.response.send_message(
            f"You're in: {staked:,} coins on **{discord.utils.escape_markdown(row[f'option_{option}'])}**.",
            ephemeral=True)
        await self.refresh_prediction(prediction_id)

    async def settle_prediction(self, prediction_id: int, winner: str | None) -> dict[int, int] | None:
        """Resolve (winner 'a'/'b') or cancel (None) and pay everyone in one
        transaction. None if it was already settled. Refs `pred:<id>:<user>` make a
        second payout impossible even if the status check were bypassed."""
        status = "resolved" if winner else "cancelled"
        async with self.db.transaction() as tx:
            cur = await tx.execute(
                "UPDATE predictions SET status = ?, winner = ? WHERE id = ? AND status IN ('open', 'locked')",
                (status, winner, prediction_id))
            if not cur.rowcount:
                return None
            _, bets = await self.load_prediction(prediction_id, tx)
            stakes = [Stake(b["user_id"], b["option"], b["amount"]) for b in bets]
            paid = rules.split_pool(stakes, winner) if winner else rules.refunds(stakes)
            at = self.clock()
            for user_id, amount in paid.items():
                if amount > 0:
                    await economy.apply_tx(tx, user_id, amount, "predict", at, ref=f"pred:{prediction_id}:{user_id}")
        return paid

    @never_raise
    async def refresh_prediction(self, prediction_id: int) -> None:
        """Update the posted totals after a bet (the modal reply can't edit it)."""
        row, bets = await self.load_prediction(prediction_id)
        if row is None or row["message_id"] is None or row["channel_id"] is None:
            return
        message = self.bot.get_partial_messageable(row["channel_id"]).get_partial_message(row["message_id"])
        try:
            await message.edit(embed=self.render_prediction(row, bets), view=build_prediction_view(row),
                               allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            log.warning("couldn't update prediction %s", prediction_id, exc_info=True)

    def render_prediction(self, row, bets) -> discord.Embed:
        stakes = [Stake(b["user_id"], b["option"], b["amount"]) for b in bets]
        lines = []
        for option in rules.OPTIONS:
            side = [s for s in stakes if s.option == option]
            coins = sum(s.amount for s in side)
            mark = "🏆 " if row["winner"] == option else ""
            people = f"{len(side)} {'person' if len(side) == 1 else 'people'}"
            lines.append(f"{mark}**{option.upper()}** · {discord.utils.escape_markdown(row[f'option_{option}'])}"
                         f" — {coins:,} coins from {people}")
        pool = sum(s.amount for s in stakes)
        lines += ["", f"**Pool** {pool:,} coins"]
        status = row["status"]
        if status == "resolved":
            paid = rules.split_pool(stakes, row["winner"])
            if any(s.option == row["winner"] for s in stakes):
                top = sorted(paid.items(), key=lambda kv: -kv[1])[:10]
                lines += ["", "**Paid out**"] + [f"<@{uid}> +{amount:,}" for uid, amount in top]
            elif stakes:
                lines += ["", "Nobody backed the winner, so everyone got their coins back."]
        elif status == "cancelled":
            lines += ["", "Cancelled. Everyone got their coins back."]
        elif status == "locked":
            lines += ["", "Bets are locked. Waiting for the result."]
        else:
            lines += ["", f"Bet with the buttons ({rules.MIN_BET} to {rules.MAX_BET:,} coins). "
                          "One side each; the winners split the whole pool. A mod picks the winner."]
        return style.embed(title=f"🔮 {row['question']}", description="\n".join(lines),
                           footer=style.label("prediction", status),
                           color=style.FOREST if status in ("open", "locked") else style.MUTED)


async def setup(bot) -> None:
    await bot.add_cog(Games(bot))
