"""Offline tests for cogs.wordgame: a real in-memory SQLite database plus small fakes for the
bot, guild, channel, members and interactions. No network."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import wordgame as cogmod
from cogs.wordgame import WordGame
from logic import quests as Q
from logic import wordgame as W

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
U1, U2, U3 = 11, 12, 13
DAY = 24 * 3600
# 2026-10-07 12:00 Pacific
T0 = int(datetime(2026, 10, 7, 12, 0, tzinfo=TZ).timestamp())
D0 = date(2026, 10, 7)
ANSWER = "stair"
ALLOWED = frozenset({"stair", "crane", "hello", "pious", "zebra", "about", "eerie", "lever", "tiger", "brick"})


def run(coro):
    return asyncio.run(coro)


class FakeText:
    def __init__(self, name):
        self.id = 301
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeMember:
    def __init__(self, uid, created_at=None):
        self.id = uid
        self.bot = False
        self.created_at = created_at or datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.name = f"user{uid}"
        self.display_name = f"**user_{uid}**"
        self.mention = f"<@{uid}>"


class FakeGuild:
    def __init__(self, with_games=True):
        self.id = GUILD_ID
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.games = FakeText(config.GAMES_CHANNEL)
        self.text_channels = [self.general] + ([self.games] if with_games else [])


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls

    async def send_message(self, content=None, **kwargs):
        self.calls.append(dict(content=content, **kwargs))


class FakeInteraction:
    def __init__(self, user, guild):
        self.calls = []
        self.user = user
        self.guild = guild
        self.response = FakeResponse(self.calls)


class Env:
    def __init__(self, db, guild, cog):
        self.db, self.guild, self.cog = db, guild, cog
        self.t = T0
        self.members = {}

    def member(self, uid, **kw):
        if uid not in self.members:
            self.members[uid] = FakeMember(uid, **kw)
        return self.members[uid]

    async def guess(self, uid, word):
        inter = FakeInteraction(self.member(uid), self.guild)
        await self.cog.guess.callback(self.cog, inter, word)
        assert len(inter.calls) == 1
        return inter.calls[0]

    async def call(self, command, uid):
        inter = FakeInteraction(self.member(uid), self.guild)
        await command.callback(self.cog, inter)
        assert len(inter.calls) == 1
        return inter.calls[0]

    async def row(self, uid, day=D0):
        return await self.db.fetchone("SELECT * FROM word_games WHERE day = ? AND user_id = ?",
                                      (day.isoformat(), uid))

    async def add_game(self, uid, day, solved, n=4):
        guesses = ["crane"] * (n - 1) + ([ANSWER] if solved else ["crane"])
        await self.db.execute(
            "INSERT INTO word_games (day, user_id, guesses, solved, finished_at) VALUES (?, ?, ?, ?, ?)",
            (day.isoformat(), uid, ",".join(guesses), int(solved), T0))


def with_env(fn, monkeypatch, with_games=True):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild(with_games)
        bot = FakeBot(db, guild)
        # Every day's answer is "stair" so tests don't depend on the shuffle.
        cog = WordGame(bot, W.Words(answers=(ANSWER,), allowed=ALLOWED))
        env = Env(db, guild, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def no_letters(text):
    return not any(c.isascii() and c.isalpha() for c in text.split("\n", 1)[1])


# ---------------------------------------------------------------- loading
def test_cog_loads_the_shipped_lists():
    cog = WordGame(SimpleNamespace(db=None, settings=SimpleNamespace(guild_id=GUILD_ID, tz=TZ)))
    assert len(cog.words.answers) > 1500
    assert cog.answer(D0) == W.answer_for(D0, W.Words.load().answers)


# ---------------------------------------------------------------- guessing
def test_guess_is_ephemeral_and_recorded(monkeypatch):
    async def go(env):
        reply = await env.guess(U1, "CRANE")
        assert reply["ephemeral"] is True
        embed = reply["embed"]
        assert "1/6" in embed.title and "#7" in embed.title
        assert "CRANE" in embed.description and W.row(W.score("crane", ANSWER)) in embed.description
        assert "Unused:" in embed.description
        r = await env.row(U1)
        assert r["guesses"] == "crane" and r["solved"] == 0 and r["finished_at"] is None
        assert env.guild.games.sent == []
    with_env(go, monkeypatch)


def test_invalid_guesses_dont_use_a_turn(monkeypatch):
    async def go(env):
        for bad in ("cran", "crane!", "zzzzz", "toolong"):
            reply = await env.guess(U1, bad)
            assert reply["ephemeral"] is True and reply["content"]
        assert await env.row(U1) is None
        await env.guess(U1, "crane")
        reply = await env.guess(U1, "crane")
        assert "already" in reply["content"]
        assert reply["embed"] is not discord.utils.MISSING  # shows the board with the error
        assert (await env.row(U1))["guesses"] == "crane"
    with_env(go, monkeypatch)


def test_win_pays_once_and_shares_without_letters_or_pings(monkeypatch):
    async def go(env):
        await env.guess(U1, "crane")
        await env.guess(U1, "hello")
        reply = await env.guess(U1, "stair")
        assert "solved in 3/6" in reply["embed"].title
        assert "+80 coins" in reply["embed"].description
        assert await economy.balance(env.db, U1) == 80
        r = await env.row(U1)
        assert r["solved"] == 1 and r["finished_at"] == T0
        assert await env.db.fetchone("SELECT 1 FROM ledger WHERE ref = ?", (W.ref(D0, U1),))
        [post] = env.guild.games.sent
        assert post["content"].startswith(f"<@{U1}> solved Daily Word #7 in 3/6")
        assert no_letters(post["content"])
        for w in ("crane", "hello", "stair"):
            assert w not in post["content"].lower()
        am = post["allowed_mentions"]
        assert am.everyone is False and am.users is False and am.roles is False
        # the game is over: further guesses are refused, nothing more is paid or posted
        reply = await env.guess(U1, "pious")
        assert "finished" in reply["content"]
        assert await economy.balance(env.db, U1) == 80
        assert len(env.guild.games.sent) == 1
    with_env(go, monkeypatch)


def test_first_guess_win_pays_100(monkeypatch):
    async def go(env):
        await env.guess(U1, "stair")
        assert await economy.balance(env.db, U1) == 100
    with_env(go, monkeypatch)


def test_loss_reveals_word_privately_and_pays_nothing(monkeypatch):
    async def go(env):
        for w in ("crane", "hello", "pious", "zebra", "about"):
            reply = await env.guess(U1, w)
            assert env.guild.games.sent == []
        reply = await env.guess(U1, "eerie")
        assert reply["ephemeral"] is True
        assert "X/6" in reply["embed"].title and "STAIR" in reply["embed"].description
        assert await economy.balance(env.db, U1) == 0
        r = await env.row(U1)
        assert r["solved"] == 0 and r["finished_at"] == T0
        [post] = env.guild.games.sent
        assert "X/6" in post["content"] and "stair" not in post["content"].lower()
        assert no_letters(post["content"])
    with_env(go, monkeypatch)


def test_young_account_plays_but_is_not_paid(monkeypatch):
    async def go(env):
        young = datetime.fromtimestamp(T0 - (Q.MIN_ACCOUNT_DAYS - 1) * DAY, tz=timezone.utc)
        env.member(U2, created_at=young)
        reply = await env.guess(U2, "stair")
        assert "older than a month" in reply["embed"].description
        assert await economy.balance(env.db, U2) == 0
        assert len(env.guild.games.sent) == 1
    with_env(go, monkeypatch)


def test_already_paid_ref_is_not_paid_again(monkeypatch):
    async def go(env):
        await economy.apply(env.db, U1, 100, W.REASON, T0, ref=W.ref(D0, U1))
        await env.guess(U1, "stair")
        assert await economy.balance(env.db, U1) == 100
    with_env(go, monkeypatch)


def test_new_day_new_game_at_pacific_midnight(monkeypatch):
    async def go(env):
        await env.guess(U1, "stair")
        env.t = int(datetime(2026, 10, 7, 23, 59, tzinfo=TZ).timestamp())
        assert "finished" in (await env.guess(U1, "crane"))["content"]
        env.t = int(datetime(2026, 10, 8, 0, 1, tzinfo=TZ).timestamp())
        reply = await env.guess(U1, "crane")
        assert "#8" in reply["embed"].title and "1/6" in reply["embed"].title
        assert (await env.row(U1, date(2026, 10, 8)))["guesses"] == "crane"
    with_env(go, monkeypatch)


def test_share_without_games_channel_is_quiet(monkeypatch):
    async def go(env):
        reply = await env.guess(U1, "stair")
        assert "+100 coins" in reply["embed"].description
        assert env.guild.general.sent == []
    with_env(go, monkeypatch, with_games=False)


def test_share_failure_never_raises(monkeypatch):
    async def go(env):
        env.guild.games.fail = RuntimeError("boom")
        reply = await env.guess(U1, "stair")
        assert reply["ephemeral"] is True
        assert await economy.balance(env.db, U1) == 100
    with_env(go, monkeypatch)


def test_concurrent_guesses_never_exceed_six(monkeypatch):
    async def go(env):
        words = ["crane", "hello", "pious", "zebra", "about", "eerie", "lever", "tiger"]
        await asyncio.gather(*(env.cog.play(env.member(U1), w, env.t) for w in words))
        r = await env.row(U1)
        assert len(r["guesses"].split(",")) == 6 and r["finished_at"] == T0
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /word today
def test_today_shows_rules_then_board(monkeypatch):
    async def go(env):
        reply = await env.call(env.cog.today_cmd, U1)
        assert reply["ephemeral"] is True and "0/6" in reply["embed"].title
        assert "🟩" in reply["embed"].description
        await env.guess(U1, "crane")
        reply = await env.call(env.cog.today_cmd, U1)
        assert "CRANE" in reply["embed"].description and "1/6" in reply["embed"].title
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /word stats
def test_stats(monkeypatch):
    async def go(env):
        await env.add_game(U1, D0 - timedelta(days=3), True, 3)
        await env.add_game(U1, D0 - timedelta(days=2), False, 6)
        await env.add_game(U1, D0 - timedelta(days=1), True, 4)
        await env.guess(U1, "crane")  # today in progress: not counted as played
        reply = await env.call(env.cog.stats_cmd, U1)
        assert reply["ephemeral"] is True
        d = reply["embed"].description
        assert "Played **3**" in d and "67%" in d and "Streak **1**" in d and "best **1**" in d
    with_env(go, monkeypatch)


def test_stats_empty(monkeypatch):
    async def go(env):
        d = (await env.call(env.cog.stats_cmd, U1))["embed"].description
        assert "Played **0**" in d and "0%" in d
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /word leaderboard
def test_leaderboard_this_month_excludes_optouts(monkeypatch):
    async def go(env):
        for d in (1, 2, 3):
            await env.add_game(U1, date(2026, 10, d), True)
        for d in (5, 6):
            await env.add_game(U2, date(2026, 10, d), True)
        await env.add_game(U2, date(2026, 9, 30), True)  # last month: not counted
        for d in (1, 2, 3, 4, 5):
            await env.add_game(U3, date(2026, 10, d), True)
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (U3, T0))
        assert await env.cog.board(D0) == [(U1, 3), (U2, 2)]
        reply = await env.call(env.cog.leaderboard_cmd, U1)
        assert reply.get("ephemeral") is not True
        assert reply["allowed_mentions"].users is False
        d = reply["embed"].description
        assert d.index(f"<@{U1}>") < d.index(f"<@{U2}>") and f"<@{U3}>" not in d
        assert "October 2026" in reply["embed"].title
    with_env(go, monkeypatch)


def test_leaderboard_empty(monkeypatch):
    async def go(env):
        reply = await env.call(env.cog.leaderboard_cmd, U1)
        assert "No streaks yet" in reply["embed"].description
    with_env(go, monkeypatch)


def test_in_progress_games_dont_count_on_the_board(monkeypatch):
    async def go(env):
        await env.guess(U1, "crane")
        assert await env.cog.board(D0) == []
    with_env(go, monkeypatch)
