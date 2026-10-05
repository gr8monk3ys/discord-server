"""Offline tests for cogs.games.Games: a real in-memory SQLite database plus small
fakes for the bot, channels and interactions. No network: trivia's fetch and all
randomness are injected."""

import asyncio
import itertools
import random
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

import config
import db as dbmod
import economy
from cogs import games as cogmod
from cogs.games import Games, PredictionButton
from logic import blackjack as bj
from logic import games as rules

GUILD_ID = 999
OWNER, CREATOR, A, B, C, D, KEEPER, MOD = 1, 10, 11, 12, 13, 14, 20, 21
CHANNEL = 500
T0 = 1_800_000_000

_ids = itertools.count(900_000)


def run(coro):
    return asyncio.run(coro)


def http_error():
    return discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")


# ---------------------------------------------------------------- fakes
class Named:
    def __init__(self, name):
        self.id = next(_ids)
        self.name = name


class FakeMessage:
    def __init__(self, message_id=None):
        self.id = message_id or next(_ids)
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.messages = {}

    def get_partial_message(self, message_id):
        return self.messages.setdefault(message_id, FakeMessage(message_id))


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=ZoneInfo("America/Los_Angeles"))
        self.guild = SimpleNamespace(id=GUILD_ID, owner_id=OWNER)
        self.cogs = {}
        self.channels = {}
        self.dynamic = []

    def get_cog(self, name):
        return self.cogs.get(name)

    def add_dynamic_items(self, *items):
        self.dynamic.extend(items)

    def remove_dynamic_items(self, *items):
        for item in items:
            self.dynamic.remove(item)

    def get_partial_messageable(self, channel_id):
        return self.channels.setdefault(channel_id, FakeChannel(channel_id))


class FakeResponse:
    def __init__(self, inter):
        self.inter = inter
        self.done = False
        self.fail_send = None

    def _finish(self):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self._finish()
        if self.fail_send is not None:
            raise self.fail_send
        self.inter.calls.append(("send_message", dict(content=content, **kwargs)))

    async def edit_message(self, **kwargs):
        self._finish()
        self.inter.calls.append(("edit_message", kwargs))

    async def defer(self, **kwargs):
        self._finish()
        self.inter.calls.append(("defer", kwargs))

    async def send_modal(self, modal):
        self._finish()
        self.inter.calls.append(("modal", {"modal": modal}))

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, inter):
        self.inter = inter

    async def send(self, content=None, wait=False, **kwargs):
        message = FakeMessage()
        self.inter.calls.append(("followup", dict(content=content, message=message, **kwargs)))
        return message if wait else None


class FakeInteraction:
    def __init__(self, bot, user_id, roles=(), admin=False, channel_id=CHANNEL):
        self.id = next(_ids)
        self.calls = []
        self.client = bot
        self.channel_id = channel_id
        self.user = SimpleNamespace(id=user_id, display_name=f"user{user_id}", mention=f"<@{user_id}>",
                                    roles=[Named(r) for r in roles], guild=bot.guild,
                                    guild_permissions=SimpleNamespace(administrator=admin))
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.original = FakeMessage()

    async def original_response(self):
        return self.original

    async def edit_original_response(self, **kwargs):
        self.calls.append(("edit_original", kwargs))

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]

    def last(self):
        return self.calls[-1][1]

    def text(self):
        return " ".join(kw.get("content") or "" for _, kw in self.calls)


class StackedRng(random.Random):
    """Deals the given ranks in order: player, dealer, player, dealer, then hits."""

    def __init__(self, *ranks):
        super().__init__(0)
        self.ranks = ranks

    def shuffle(self, deck):
        deck[:] = [bj.Card(r) for r in reversed(self.ranks)]


def trivia_payload(correct="Paris", wrong=("Lyon", "Nice", "M&eacute;nton"), question="Capital of France?"):
    return {"response_code": 0, "results": [{
        "type": "multiple", "difficulty": "easy", "category": "Geography", "question": question,
        "correct_answer": correct, "incorrect_answers": list(wrong)}]}


class Env:
    def __init__(self, db, bot, cog):
        self.db, self.bot, self.cog = db, bot, cog
        self.clock = [T0]
        self.fetched = []
        self.payload = trivia_payload()
        self.wire(cog)

    def wire(self, cog):
        cog.clock = lambda: self.clock[0]
        cog.fetch = self.fetch
        self.bot.cogs["Games"] = cog
        self.cog = cog

    async def fetch(self, url):
        self.fetched.append(url)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def inter(self, user_id, **kw):
        return FakeInteraction(self.bot, user_id, **kw)

    async def fund(self, user_id, amount):
        await economy.apply(self.db, user_id, amount, "seed", T0)

    async def bal(self, user_id):
        return await economy.balance(self.db, user_id)

    async def ledger(self, user_id=None):
        if user_id is None:
            return await self.db.fetchall("SELECT * FROM ledger WHERE reason != 'seed' ORDER BY id")
        return await self.db.fetchall("SELECT * FROM ledger WHERE user_id = ? AND reason != 'seed' ORDER BY id",
                                      (user_id,))

    async def restart(self):
        """A new cog on the same database, as after a bot restart."""
        cog = Games(self.bot, rng=random.Random(1))
        self.wire(cog)
        await cog.cog_load()
        return cog


async def make_env():
    db = dbmod.Database(":memory:")
    await db.connect()
    await db.migrate()
    bot = FakeBot(db)
    cog = Games(bot, rng=random.Random(1))
    env = Env(db, bot, cog)
    await cog.cog_load()
    return env


def with_env(fn):
    async def go():
        env = await make_env()
        try:
            await fn(env)
        finally:
            await env.cog.cog_unload()
            await env.db.close()
    run(go())


def no_pings(kwargs):
    am = kwargs.get("allowed_mentions")
    return (isinstance(am, discord.AllowedMentions) and am.everyone is False and am.users is False
            and am.roles is False)


# ---------------------------------------------------------------- slots
def force_spin(monkeypatch, reels):
    monkeypatch.setattr(cogmod.rules, "spin", lambda rng: tuple(reels))


def test_slots_jackpot_pays_and_posts_without_pings(monkeypatch):
    force_spin(monkeypatch, ["7️⃣"] * 3)

    async def go(env):
        await env.fund(A, 1000)
        inter = env.inter(A)
        await env.cog.play_slots(inter, 20)
        assert await env.bal(A) == 1000 - 20 + 20 * 250
        rows = await env.ledger(A)
        assert [(r["delta"], r["reason"], r["ref"]) for r in rows] == [
            (-20, "slots", None), (5000, "slots", f"slots:{inter.id}")]
        (sent,) = inter.of("send_message")
        assert no_pings(sent) and not sent.get("ephemeral")
        assert "7️⃣" in sent["embed"].description and "5,000" in sent["embed"].description
    with_env(go)


def test_slots_loss_keeps_the_bet(monkeypatch):
    force_spin(monkeypatch, ["🍋", "🔔", "⭐"])

    async def go(env):
        await env.fund(A, 100)
        await env.cog.play_slots(env.inter(A), 30)
        assert await env.bal(A) == 70
    with_env(go)


def test_slots_with_seeded_rng_matches_paytable():
    async def go(env):
        await env.fund(A, 10_000)
        env.cog.rng = random.Random(42)
        expected_rng = random.Random(42)
        balance = 10_000
        for _ in range(50):
            reels = rules.spin(expected_rng)
            balance += rules.slots_payout(reels, 10) - 10
            await env.cog.play_slots(env.inter(A), 10)
        assert await env.bal(A) == balance
    with_env(go)


@pytest.mark.parametrize("balance, bet, words", [(1000, 9, "smallest"), (50, 60, "only have 50"),
                                                 (10_000, 5001, "biggest")])
def test_slots_bet_bounds_refuse_and_change_nothing(balance, bet, words):
    async def go(env):
        await env.fund(A, balance)
        inter = env.inter(A)
        await env.cog.play_slots(inter, bet)
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"] is True and words in sent["content"]
        assert await env.bal(A) == balance and await env.ledger(A) == []
    with_env(go)


def test_concurrent_bets_cannot_spend_the_same_coins(monkeypatch):
    force_spin(monkeypatch, ["🍋", "🔔", "⭐"])

    async def go(env):
        await env.fund(A, 100)
        inters = [env.inter(A) for _ in range(3)]
        await asyncio.gather(env.cog.play_slots(inters[0], 60), env.cog.play_slots(inters[1], 60),
                             env.cog.start_blackjack(inters[2], 60))
        refused = [i for i in inters if i.of("send_message") and i.of("send_message")[0].get("ephemeral")]
        assert len(refused) == 2
        assert await env.bal(A) == 40
        debits = [r for r in await env.ledger(A) if r["delta"] < 0]
        assert len(debits) == 1
    with_env(go)


# ---------------------------------------------------------------- blackjack
async def deal(env, user, bet, *ranks):
    env.cog.rng = StackedRng(*ranks)
    inter = env.inter(user)
    await env.cog.start_blackjack(inter, bet)
    return inter, env.cog.blackjack_hands.get(user)


async def open_rows(env):
    return await env.db.fetchall("SELECT * FROM blackjack_open")


def test_blackjack_start_takes_bet_and_records_open_row():
    async def go(env):
        await env.fund(A, 500)
        inter, view = await deal(env, A, 100, "10", "10", "8", "6")
        assert await env.bal(A) == 400
        (row,) = await open_rows(env)
        assert (row["user_id"], row["bet"], row["started_at"]) == (A, 100, T0)
        (sent,) = inter.of("send_message")
        assert no_pings(sent) and sent["view"] is view
        assert "??" in sent["embed"].description  # hole card hidden
        assert view.timeout == 120
    with_env(go)


def test_blackjack_stand_win_pays_double_once():
    async def go(env):
        await env.fund(A, 500)
        inter, view = await deal(env, A, 100, "10", "10", "9", "7")  # 19 vs 17
        press = env.inter(A)
        await env.cog.blackjack_move(press, view, "stand")
        assert view.game.result is bj.Outcome.WIN
        assert await env.bal(A) == 600
        assert await open_rows(env) == []
        assert A not in env.cog.blackjack_hands
        edited = press.of("edit_message")[0]
        assert no_pings(edited) and all(item.disabled for item in view.children)
        # A late timeout or second press can't pay again.
        await view.on_timeout()
        again = env.inter(A)
        await env.cog.blackjack_move(again, view, "stand")
        assert "over" in again.of("send_message")[0]["content"]
        assert await env.bal(A) == 600
        payouts = [r for r in await env.ledger(A) if r["delta"] > 0]
        assert [(r["delta"], r["ref"]) for r in payouts] == [(200, f"blackjack:{inter.id}")]
    with_env(go)


def test_blackjack_bust_loses_and_closes_row():
    async def go(env):
        await env.fund(A, 500)
        _, view = await deal(env, A, 100, "10", "10", "6", "7", "K")
        await env.cog.blackjack_move(env.inter(A), view, "hit")
        assert view.game.result is bj.Outcome.LOSE
        assert await env.bal(A) == 400 and await open_rows(env) == []
    with_env(go)


def test_blackjack_push_refunds():
    async def go(env):
        await env.fund(A, 500)
        _, view = await deal(env, A, 100, "10", "10", "8", "8")
        await env.cog.blackjack_move(env.inter(A), view, "stand")
        assert view.game.result is bj.Outcome.PUSH
        assert await env.bal(A) == 500
    with_env(go)


def test_blackjack_natural_pays_3_to_2_rounded_down_immediately():
    async def go(env):
        await env.fund(A, 500)
        inter, view = await deal(env, A, 15, "A", "9", "K", "7")
        assert view is None  # settled on the deal, nothing to play
        assert await env.bal(A) == 500 - 15 + 15 + 22
        assert await open_rows(env) == []
        (sent,) = inter.of("send_message")
        assert all(item.disabled for item in sent["view"].children)
    with_env(go)


def test_blackjack_only_the_player_can_press():
    async def go(env):
        await env.fund(A, 500)
        _, view = await deal(env, A, 100, "10", "10", "6", "7", "K")
        other = env.inter(B)
        await env.cog.blackjack_move(other, view, "hit")
        assert other.of("send_message")[0]["ephemeral"] is True
        assert len(view.game.player) == 2 and not view.game.finished
        checked = env.inter(B)
        assert await view.interaction_check(checked) is False
        assert await view.interaction_check(env.inter(A)) is True
    with_env(go)


def test_blackjack_one_hand_at_a_time():
    async def go(env):
        await env.fund(A, 500)
        await deal(env, A, 100, "10", "10", "8", "6")
        inter, _ = await deal(env, A, 100, "10", "10", "8", "6")
        assert "already have a blackjack hand" in inter.of("send_message")[0]["content"]
        assert await env.bal(A) == 400
    with_env(go)


def test_blackjack_idle_timeout_stands():
    async def go(env):
        await env.fund(A, 500)
        inter, view = await deal(env, A, 100, "10", "10", "9", "7")
        await view.on_timeout()
        assert view.game.result is bj.Outcome.WIN
        assert await env.bal(A) == 600 and await open_rows(env) == []
        (edited,) = inter.of("edit_original")
        assert "Idle" in edited["embed"].description and no_pings(edited)
    with_env(go)


def test_blackjack_timeout_never_raises():
    async def go(env):
        await env.fund(A, 500)
        inter, view = await deal(env, A, 100, "10", "10", "9", "7")

        async def broken(**kwargs):
            raise http_error()
        inter.edit_original_response = broken
        await view.on_timeout()  # logged, not raised
        assert await env.bal(A) == 600
    with_env(go)


def test_blackjack_restart_refunds_open_bets_once():
    async def go(env):
        await env.fund(A, 500)
        await env.fund(B, 500)
        _, old_view = await deal(env, A, 100, "10", "10", "9", "7")
        env.clock[0] += 5
        await deal(env, B, 50, "10", "10", "9", "7")
        assert await env.bal(A) == 400 and await env.bal(B) == 450

        await env.restart()
        assert await open_rows(env) == []
        assert await env.bal(A) == 500 and await env.bal(B) == 500
        refs = sorted(r["ref"] for r in await env.ledger() if r["delta"] > 0)
        assert refs == [f"bjrefund:{A}:{T0}", f"bjrefund:{B}:{T0 + 5}"]

        await env.restart()  # nothing left to refund
        assert await env.bal(A) == 500
        # The pre-restart hand finishing late pays nothing more.
        await env.cog.blackjack_move(env.inter(A), old_view, "stand")
        assert await env.bal(A) == 500
        assert "already refunded" in old_view.note
    with_env(go)


def test_blackjack_refund_on_load_never_raises():
    async def go(env):
        await env.db.close()  # every query now fails
        cog = Games(env.bot)
        await cog.refund_open_blackjack()  # logged, not raised
        await env.db.connect()
    with_env(go)


def test_blackjack_send_failure_refunds_bet():
    async def go(env):
        await env.fund(A, 500)
        env.cog.rng = StackedRng("10", "10", "8", "6")
        inter = env.inter(A)
        inter.response.fail_send = http_error()
        with pytest.raises(discord.HTTPException):
            await env.cog.start_blackjack(inter, 100)
        assert await env.bal(A) == 500 and await open_rows(env) == []
        assert A not in env.cog.blackjack_hands
    with_env(go)


# ---------------------------------------------------------------- trivia
async def ask(env, user=A, category=None, channel_id=CHANNEL):
    inter = env.inter(user, channel_id=channel_id)
    await env.cog.start_trivia(inter, category)
    view = env.cog.trivia_rounds.get(channel_id)
    return inter, view


def test_trivia_posts_question_with_four_unescaped_buttons():
    async def go(env):
        env.payload = trivia_payload(wrong=("Lyon", "Nice", "M&eacute;nton " + "x" * 200),
                                     question="What&#039;s the capital of France?")
        inter, view = await ask(env, category=22)
        assert env.fetched == ["https://opentdb.com/api.php?amount=1&type=multiple&category=22"]
        (posted,) = inter.of("followup")
        assert no_pings(posted) and posted["view"] is view
        assert "What's the capital of France?" in posted["embed"].description
        labels = [item.label for item in view.children]
        assert len(labels) == 4 and all(len(label) <= 80 for label in labels)
        assert any(label.startswith("Ménton") for label in labels)
        assert view.round.question.correct_answer == "Paris"
        assert view.message is posted["message"]
    with_env(go)


def test_trivia_first_correct_click_wins_once_per_person():
    async def go(env):
        _, view = await ask(env)
        right = view.round.question.correct
        wrong = (right + 1) % 4

        a = env.inter(A)
        await env.cog.trivia_click(a, view=view, index=wrong)
        assert "not it" in a.of("send_message")[0]["content"]
        a2 = env.inter(A)
        await env.cog.trivia_click(a2, view=view, index=right)
        assert "already answered" in a2.of("send_message")[0]["content"]

        b = env.inter(B)
        await env.cog.trivia_click(b, view=view, index=right)
        (edited,) = b.of("edit_message")
        assert no_pings(edited) and "user12" in edited["embed"].description
        assert await env.bal(B) == 50 and await env.bal(A) == 0
        (row,) = await env.ledger(B)
        assert row["ref"] == f"trivia:{view.message.id}"

        c = env.inter(C)
        await env.cog.trivia_click(c, view=view, index=right)
        assert "closed" in c.of("send_message")[0]["content"]
        assert await env.bal(C) == 0
        assert CHANNEL not in env.cog.trivia_rounds  # free for the next question
    with_env(go)


def test_trivia_api_failure_pays_nothing_and_frees_channel():
    async def go(env):
        env.payload = OSError("network down")
        inter, view = await ask(env)
        assert view is None
        assert "Trivia's down right now" in inter.of("followup")[0]["content"]
        assert CHANNEL not in env.cog.trivia_rounds
        env.payload = {"response_code": 5, "results": []}  # rate limited
        inter, _ = await ask(env)
        assert "Trivia's down right now" in inter.of("followup")[0]["content"]
        assert await env.ledger() == []
    with_env(go)


def test_trivia_one_question_per_channel():
    async def go(env):
        await ask(env)
        second, _ = await ask(env, user=B)
        assert "already a trivia question" in second.of("send_message")[0]["content"]
        assert len(env.fetched) == 1
        other, view = await ask(env, user=B, channel_id=CHANNEL + 1)  # other channels are fine
        assert view is not None
    with_env(go)


def test_trivia_times_out_after_the_limit():
    async def go(env):
        env.cog.trivia_seconds = 0.01
        _, view = await ask(env)
        await asyncio.sleep(0.1)
        assert view.round.closed and view.round.winner is None
        (edit,) = view.message.edits
        assert "Time's up" in edit["embed"].description and "Paris" in edit["embed"].description
        assert no_pings(edit)
        assert CHANNEL not in env.cog.trivia_rounds
        late = env.inter(A)
        await env.cog.trivia_click(late, view=view, index=view.round.question.correct)
        assert "closed" in late.of("send_message")[0]["content"]
        assert await env.ledger() == []
    with_env(go)


def test_trivia_winnings_are_capped_per_local_day():
    """Farming with alts: once a member has won the daily cap, a right answer is
    still announced but pays nothing; the next local day pays again."""
    async def win(env):
        _, view = await ask(env)
        inter = env.inter(B)
        await env.cog.trivia_click(inter, view=view, index=view.round.question.correct)
        return inter.of("edit_message")[0]["embed"].description

    async def go(env):
        rounds = rules.TRIVIA_DAILY_CAP // rules.TRIVIA_PRIZE
        assert rounds == 5
        for _ in range(rounds):
            assert f"+{rules.TRIVIA_PRIZE} coins" in await win(env)
        assert await env.bal(B) == rules.TRIVIA_DAILY_CAP
        capped = await win(env)
        assert "Paris" in capped and "No coins" in capped and "+" not in capped
        assert await env.bal(B) == rules.TRIVIA_DAILY_CAP
        assert CHANNEL not in env.cog.trivia_rounds
        env.clock[0] += 24 * 60 * 60
        assert f"+{rules.TRIVIA_PRIZE} coins" in await win(env)
        assert await env.bal(B) == rules.TRIVIA_DAILY_CAP + rules.TRIVIA_PRIZE
    with_env(go)


def test_trivia_click_after_deadline_is_closed_even_before_timer():
    async def go(env):
        _, view = await ask(env)
        env.clock[0] += rules.TRIVIA_SECONDS
        inter = env.inter(A)
        await env.cog.trivia_click(inter, view=view, index=view.round.question.correct)
        assert "closed" in inter.of("send_message")[0]["content"]
        assert await env.bal(A) == 0
    with_env(go)


# ---------------------------------------------------------------- predictions
async def create(env, question="Who wins?", a="Red", b="Blue", user=CREATOR):
    inter = env.inter(user)
    await env.cog.create_prediction(inter, question, a, b)
    row = await env.db.fetchone("SELECT * FROM predictions ORDER BY id DESC LIMIT 1")
    return inter, row


async def bet(env, pid, user, option, amount):
    inter = env.inter(user)
    await env.cog.place_bet(inter, pid, option, str(amount))
    return inter


async def press(env, pid, user, action, **kw):
    inter = env.inter(user, **kw)
    await env.cog.handle_prediction_button(inter, action, pid)
    return inter


async def bets(env, pid):
    rows = await env.db.fetchall("SELECT user_id, option, amount FROM prediction_bets WHERE prediction_id = ?"
                                 " ORDER BY at, rowid", (pid,))
    return [(r["user_id"], r["option"], r["amount"]) for r in rows]


def test_prediction_button_custom_id_round_trips():
    async def go():
        made = PredictionButton("resolve_a", 42)
        assert made.item.custom_id == "pred:resolve_a:42"
        pattern = PredictionButton.__discord_ui_compiled_template__
        parsed = await PredictionButton.from_custom_id(None, None, pattern.fullmatch("pred:b:7"))
        assert (parsed.action, parsed.prediction_id) == ("b", 7)
        assert pattern.fullmatch("pred:resolve_c:1") is None
        assert pattern.fullmatch("pred:a:x") is None
    run(go())


def test_create_posts_with_buttons_and_no_pings():
    async def go(env):
        inter, row = await create(env)
        assert row["status"] == "open" and row["creator_id"] == CREATOR and row["channel_id"] == CHANNEL
        assert row["message_id"] == inter.original.id
        (sent,) = inter.of("send_message")
        assert no_pings(sent)
        pid = row["id"]
        ids = [i.item.custom_id for i in sent["view"].children]
        assert ids == [f"pred:{a}:{pid}" for a in ("a", "b", "lock", "resolve_a", "resolve_b", "cancel")]
        assert sent["view"].children[0].item.label == "Bet A: Red"
        assert PredictionButton in env.bot.dynamic
    with_env(go)


def test_create_refuses_identical_options():
    async def go(env):
        inter, row = await create(env, a="Red", b="red")
        assert row is None and inter.of("send_message")[0]["ephemeral"] is True
    with_env(go)


def test_bet_button_opens_modal_and_creator_cannot_bet():
    async def go(env):
        _, row = await create(env)
        opened = await press(env, row["id"], A, "a")
        (modal,) = opened.of("modal")
        assert modal["modal"].option == "a" and modal["modal"].prediction_id == row["id"]
        mine = await press(env, row["id"], CREATOR, "b")
        assert "can't bet" in mine.of("send_message")[0]["content"]
        await env.fund(CREATOR, 500)
        direct = await bet(env, row["id"], CREATOR, "a", 50)
        assert "can't bet" in direct.of("send_message")[0]["content"]
        assert await env.bal(CREATOR) == 500
    with_env(go)


def test_betting_rules_stake_taken_top_up_no_switching():
    async def go(env):
        _, row = await create(env)
        pid = row["id"]
        await env.fund(A, 1000)
        ok = await bet(env, pid, A, "a", 100)
        assert "100 coins on **Red**" in ok.of("send_message")[0]["content"]
        assert await env.bal(A) == 900
        await bet(env, pid, A, "a", "1,50")  # topping up adds to the same option
        assert await bets(env, pid) == [(A, "a", 250)] and await env.bal(A) == 750
        switch = await bet(env, pid, A, "b", 50)
        assert "can't switch" in switch.of("send_message")[0]["content"]
        assert (await press(env, pid, A, "b")).of("send_message")  # button refuses before the modal
        assert await bets(env, pid) == [(A, "a", 250)] and await env.bal(A) == 750
        # The posted totals were refreshed (without pings).
        edits = env.bot.channels[CHANNEL].messages[row["message_id"]].edits
        assert edits and "250" in edits[-1]["embed"].description and no_pings(edits[-1])
    with_env(go)


@pytest.mark.parametrize("raw, words", [("5", "smallest"), ("abc", "not a number"), ("600", "only have 500"),
                                        ("5001", "biggest")])
def test_bet_amount_bounds(raw, words):
    async def go(env):
        _, row = await create(env)
        await env.fund(A, 500)
        inter = await bet(env, row["id"], A, "a", raw)
        assert words in inter.of("send_message")[0]["content"]
        assert await env.bal(A) == 500 and await bets(env, row["id"]) == []
    with_env(go)


def test_bet_total_capped_at_max_bet():
    async def go(env):
        _, row = await create(env)
        await env.fund(A, 10_000)
        await bet(env, row["id"], A, "a", 4000)
        over = await bet(env, row["id"], A, "a", 1001)
        assert "at most 5,000" in over.of("send_message")[0]["content"]
        assert await env.bal(A) == 6000
    with_env(go)


def test_concurrent_prediction_and_slots_bets_cannot_overspend(monkeypatch):
    force_spin(monkeypatch, ["🍋", "🔔", "⭐"])

    async def go(env):
        _, row = await create(env)
        await env.fund(A, 100)
        p = env.inter(A)
        s = env.inter(A)
        await asyncio.gather(env.cog.place_bet(p, row["id"], "a", "70"), env.cog.play_slots(s, 70))
        assert await env.bal(A) == 30
        assert len([r for r in await env.ledger(A) if r["delta"] < 0]) == 1
    with_env(go)


def test_lock_needs_creator_or_staff_and_stops_bets():
    async def go(env):
        _, row = await create(env)
        pid = row["id"]
        nope = await press(env, pid, A, "lock")
        assert "Only whoever made" in nope.of("send_message")[0]["content"]
        locked = await press(env, pid, CREATOR, "lock")
        (edited,) = locked.of("edit_message")
        assert no_pings(edited)
        view = edited["view"]
        assert [i.item.disabled for i in view.children] == [True, True, True, False, False, False]
        assert (await env.db.fetchone("SELECT status FROM predictions WHERE id = ?", (pid,)))["status"] == "locked"
        await env.fund(A, 500)
        late = await bet(env, pid, A, "a", 50)
        assert "closed" in late.of("send_message")[0]["content"] and await env.bal(A) == 500
        again = await press(env, pid, CREATOR, "lock")
        assert "already locked" in again.of("send_message")[0]["content"]
    with_env(go)


def test_creator_cannot_cancel_once_locked_but_staff_can():
    """Alt abuse: once locked the outcome may be known, so the creator cancelling
    would rescue an alt's losing stake from the other side's winnings."""
    async def go(env):
        _, row = await create(env)
        pid = row["id"]
        await seed_bets(env, pid)
        assert (await press(env, pid, CREATOR, "lock")).of("edit_message")
        refused = await press(env, pid, CREATOR, "cancel")
        msg = refused.of("send_message")[0]
        assert "only a mod" in msg["content"] and msg["ephemeral"] is True
        status = await env.db.fetchone("SELECT status FROM predictions WHERE id = ?", (pid,))
        assert status["status"] == "locked" and await env.bal(A) == 900
        done = await press(env, pid, KEEPER, "cancel", roles=[config.KEEPER_ROLE])
        assert done.of("edit_message")
        assert all([await env.bal(u) == 1000 for u in (A, B, C, D)])
    with_env(go)


def test_creator_cancel_loses_race_with_lock():
    """Even if the creator's Cancel read the prediction as open, settling it must
    not cancel a prediction that got locked in the meantime."""
    async def go(env):
        _, row = await create(env)
        pid = row["id"]
        await env.db.execute("UPDATE predictions SET status = 'locked' WHERE id = ?", (pid,))
        assert await env.cog.settle_prediction(pid, None, only_open=True) is None
        status = await env.db.fetchone("SELECT status FROM predictions WHERE id = ?", (pid,))
        assert status["status"] == "locked"
    with_env(go)


def test_creator_cannot_resolve_but_can_lock_and_cancel():
    async def go(env):
        _, row = await create(env)
        pid = row["id"]
        await seed_bets(env, pid)
        for action in ("resolve_a", "resolve_b"):
            refused = await press(env, pid, CREATOR, action)
            assert "Only a mod can pick the winner" in refused.of("send_message")[0]["content"]
            assert refused.of("send_message")[0]["ephemeral"] is True
        status = await env.db.fetchone("SELECT status FROM predictions WHERE id = ?", (pid,))
        assert status["status"] == "open" and await env.bal(A) == 900
        # Other members can't resolve either.
        member = await press(env, pid, A, "resolve_a")
        assert "Only a mod can pick the winner" in member.of("send_message")[0]["content"]
        cancelled = await press(env, pid, CREATOR, "cancel")  # still open
        assert cancelled.of("edit_message")
        assert all([await env.bal(u) == 1000 for u in (A, B, C, D)])
    with_env(go)


def test_creator_can_lock():
    async def go(env):
        _, row = await create(env)
        assert (await press(env, row["id"], CREATOR, "lock")).of("edit_message")
    with_env(go)


def test_other_members_cannot_cancel():
    async def go(env):
        _, row = await create(env)
        nope = await press(env, row["id"], A, "cancel")
        assert "Only whoever made" in nope.of("send_message")[0]["content"]
    with_env(go)


@pytest.mark.parametrize("who, kw", [(KEEPER, {"roles": [config.KEEPER_ROLE]}), (MOD, {"roles": [config.MOD_ROLE]}),
                                     (OWNER, {}), (C, {"admin": True})])
def test_staff_can_resolve(who, kw):
    async def go(env):
        _, row = await create(env)
        inter = await press(env, row["id"], who, "resolve_a", **kw)
        assert inter.of("edit_message")
    with_env(go)


async def seed_bets(env, pid):
    for user, option, amount in ((A, "a", 100), (B, "a", 200), (C, "b", 100), (D, "b", 33)):
        await env.fund(user, 1000)
        env.clock[0] += 1
        await bet(env, pid, user, option, amount)


def test_resolve_splits_pool_with_remainder_and_cannot_pay_twice():
    async def go(env):
        _, row = await create(env)
        pid = row["id"]
        await seed_bets(env, pid)
        await press(env, pid, CREATOR, "lock")
        done = await press(env, pid, KEEPER, "resolve_a", roles=[config.KEEPER_ROLE])
        (edited,) = done.of("edit_message")
        assert no_pings(edited) and all(i.item.disabled for i in edited["view"].children)
        # pool 433 over 300 backing A: 144 and 288, remainder 1 to the biggest stake (B)
        assert await env.bal(A) == 900 + 144
        assert await env.bal(B) == 800 + 289
        assert await env.bal(C) == 900 and await env.bal(D) == 967
        refs = sorted(r["ref"] for r in await env.ledger() if r["delta"] > 0)
        assert refs == sorted([f"pred:{pid}:{A}", f"pred:{pid}:{B}"])

        twice = await press(env, pid, MOD, "resolve_b", roles=[config.MOD_ROLE])
        assert "already resolved" in twice.of("send_message")[0]["content"]
        # Even if the status guard were bypassed, the refs stop a second payout.
        await env.db.execute("UPDATE predictions SET status = 'locked' WHERE id = ?", (pid,))
        await env.cog.settle_prediction(pid, "a")
        assert await env.bal(A) == 1044 and await env.bal(B) == 1089
    with_env(go)


def test_resolve_with_nobody_on_winning_side_refunds_all():
    async def go(env):
        _, row = await create(env)
        pid = row["id"]
        for user in (A, B):
            await env.fund(user, 300)
            await bet(env, pid, user, "a", 120)
        await press(env, pid, MOD, "resolve_b", roles=[config.MOD_ROLE])
        assert await env.bal(A) == 300 and await env.bal(B) == 300
        status = await env.db.fetchone("SELECT status, winner FROM predictions WHERE id = ?", (pid,))
        assert (status["status"], status["winner"]) == ("resolved", "b")
    with_env(go)


def test_cancel_refunds_everyone():
    async def go(env):
        _, row = await create(env)
        pid = row["id"]
        await seed_bets(env, pid)
        done = await press(env, pid, KEEPER, "cancel", roles=[config.KEEPER_ROLE])
        assert "Cancelled" in done.of("edit_message")[0]["embed"].description
        for user in (A, B, C, D):
            assert await env.bal(user) == 1000
        again = await press(env, pid, MOD, "resolve_a", roles=[config.MOD_ROLE])
        assert "already cancelled" in again.of("send_message")[0]["content"]
        assert all([await env.bal(u) == 1000 for u in (A, B, C, D)])
    with_env(go)


def test_predictions_survive_restart():
    async def go(env):
        _, row = await create(env)
        pid = row["id"]
        await seed_bets(env, pid)
        await env.restart()
        # A button press after the restart is routed through the DynamicItem to the new cog.
        pattern = PredictionButton.__discord_ui_compiled_template__
        item = await PredictionButton.from_custom_id(None, None, pattern.fullmatch(f"pred:resolve_b:{pid}"))
        inter = env.inter(KEEPER, roles=[config.KEEPER_ROLE])
        await item.callback(inter)
        assert inter.of("edit_message")
        # pool 433 over 133 backing B: C 100 -> 325 (325.56), D 33 -> 107 (107.44), remainder 1 to C
        assert await env.bal(C) == 900 + 326 and await env.bal(D) == 967 + 107
    with_env(go)


def test_prediction_button_errors_reply_instead_of_raising():
    async def go(env):
        inter = env.inter(A)
        env.bot.cogs["Games"] = None  # handle_prediction_button blows up
        await PredictionButton("lock", 1).callback(inter)
        assert "didn't work" in inter.of("send_message")[0]["content"]
    with_env(go)


def test_unknown_prediction():
    async def go(env):
        inter = await press(env, 12345, A, "a")
        assert "doesn't exist" in inter.of("send_message")[0]["content"]
    with_env(go)
