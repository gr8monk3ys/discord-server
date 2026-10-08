"""Offline tests for cogs.challenges: a real in-memory SQLite database plus small fakes for
the bot, guild, channels, members and interactions. No network."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import challenges as cogmod
from cogs.challenges import Challenges, ClaimButton, ProgressButton
from logic import challenges as CH
from logic.starboard import PENDING

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
U1, U2, U3, BOTUSER = 11, 12, 13, 50
HOUR = 3600
DAY = 24 * HOUR


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


T = ts(2026, 10, 7, 15, 0)  # Wednesday of 2026-W41
WEEK = CH.week_of(T, TZ)
PICKS = (CH.BY_KEY["join_squads"], CH.BY_KEY["voice_3h"], CH.BY_KEY["hall_of_fame"])


def run(coro):
    return asyncio.run(coro)


class FakeText:
    _next = 300

    def __init__(self, name):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.category = None
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeMember:
    def __init__(self, uid, bot=False, created_at=None):
        self.id = uid
        self.bot = bot
        self.created_at = created_at or datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.mention = f"<@{uid}>"
        self.display_name = f"user_{uid}*"


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.games = FakeText(config.GAMES_CHANNEL)
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.text_channels = [self.general, self.games]
        self.afk_channel = None
        self.members = []
        self.events = {}  # event_id -> SimpleNamespace(status=...), all "uncached"
        self.fetches = []

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_scheduled_event(self, eid):
        return None

    async def fetch_scheduled_event(self, eid):
        self.fetches.append(eid)
        if eid not in self.events:
            raise discord.NotFound(SimpleNamespace(status=404, reason="nope"), "Unknown Guild Scheduled Event")
        return self.events[eid]


def young_id(at: int, n: int = 0) -> int:
    """A Discord id for an account created 5 days before `at` (under MIN_ACCOUNT_DAYS)."""
    return discord.utils.time_snowflake(datetime.fromtimestamp(at - 5 * DAY, timezone.utc)) + n


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.cogs = {}

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None

    def get_cog(self, name):
        return self.cogs.get(name)


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls
        self.done = False

    def _finish(self):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self._finish()
        self.calls.append(("send_message", dict(content=content, **kwargs)))

    async def edit_message(self, **kwargs):
        self._finish()
        self.calls.append(("edit_message", kwargs))

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, bot, user):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T
        self.games = 0

    def member(self, uid, **kw):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, **kw)
            self.guild.members.append(m)
        return m

    async def post(self, host, at=None, joiners=()):
        at = self.t if at is None else at
        self.games += 1  # one open post per host per game
        await self.db.execute("INSERT INTO lfg_posts (game, host_id, size, when_text, created_at) VALUES (?, ?, 4, 'now', ?)",
                              (f"game{self.games}", host, at))
        pid = (await self.db.fetchone("SELECT MAX(id) AS id FROM lfg_posts"))["id"]
        for uid in (host, *joiners):
            await self.db.execute("INSERT INTO lfg_members (post_id, user_id, joined_at) VALUES (?, ?, ?)", (pid, uid, at))
        return pid

    async def voice(self, uid, start, end, channel=5):
        await self.db.execute('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, ?, ?, ?)',
                              (uid, channel, start, end))

    async def star(self, uid, at=None, board=777, mid=None):
        mid = mid or (uid * 1000 + (at or self.t) % 1000)
        await self.db.execute("INSERT INTO starboard (message_id, channel_id, author_id, board_message_id, stars, at)"
                              " VALUES (?, 1, ?, ?, 5, ?)", (mid, uid, board, self.t if at is None else at))

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def complete_all(self, uid, partner=U3):
        await self.post(77, joiners=(uid,))
        await self.post(78, joiners=(uid,))
        await self.voice(uid, WEEK.start + HOUR, WEEK.start + 5 * HOUR)
        await self.voice(partner, WEEK.start + HOUR, WEEK.start + 5 * HOUR)
        await self.star(uid)

    async def balance(self, uid):
        return await economy.balance(self.db, uid)


def with_env(fn, monkeypatch, picks=PICKS):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Challenges(bot)
        bot.cogs["Challenges"] = cog
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        if picks is not None:
            monkeypatch.setattr(CH, "pick", lambda key: picks)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def pinged_only(sent, uid):
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.roles is False
    assert [u.id for u in am.users] == [uid]


# ---------------------------------------------------------------- measuring
def test_join_squads_counts_others_posts_in_this_week_only(monkeypatch):
    async def body(env):
        await env.post(77, joiners=(U1,))
        await env.post(78, joiners=(U1, U2))
        await env.post(U1)  # own post: not a join
        await env.post(79, at=WEEK.start - 1, joiners=(U1,))  # last week
        got = await env.cog.measure("join_squads", WEEK, None, env.t)
        assert got[U1] == 2 and got[U2] == 1 and 77 not in got
        assert await env.cog.measure("join_squads", WEEK, {U2}, env.t) == {U2: 1}
    with_env(body, monkeypatch)


def test_host_squads_needs_someone_else_to_join(monkeypatch):
    async def body(env):
        await env.post(U1, joiners=(U2,))
        await env.post(U1)  # nobody came
        await env.post(U1, at=WEEK.end, joiners=(U2,))  # next week
        assert await env.cog.measure("host_squads", WEEK, None, env.t) == {U1: 1}
    with_env(body, monkeypatch)


def test_win_game_from_trivia_and_blackjack_ledger(monkeypatch):
    async def body(env):
        t = env.t
        await economy.apply(env.db, U1, 500, "seed", t)
        await economy.apply(env.db, U2, 500, "seed", t)
        # U1: a trivia win
        await economy.apply(env.db, U1, 25, "trivia", t, ref="trivia:1")
        # U2: a push (bet back), then a refund, then a real win
        await economy.apply(env.db, U2, -100, "blackjack", t)
        await economy.apply(env.db, U2, 100, "blackjack", t, ref="blackjack:a")
        await economy.apply(env.db, U2, -50, "blackjack", t)
        await economy.apply(env.db, U2, 50, "blackjack", t, ref="bjrefund:12:1")
        got = await env.cog.measure("win_game", WEEK, None, t)
        assert got == {U1: 1}
        await economy.apply(env.db, U2, -100, "blackjack", t)
        await economy.apply(env.db, U2, 200, "blackjack", t, ref="blackjack:b")
        got = await env.cog.measure("win_game", WEEK, None, t)
        assert got == {U1: 1, U2: 1}
        # outside the week: not counted
        await economy.apply(env.db, U3, 25, "trivia", WEEK.start - 1, ref="trivia:2")
        assert U3 not in await env.cog.measure("win_game", WEEK, None, t)
    with_env(body, monkeypatch)


def test_messages_daily_word_games_tournament_clip_gamenight(monkeypatch):
    async def body(env):
        t = env.t
        for day, n in (("2026-10-04", 50), ("2026-10-05", 30), ("2026-10-11", 20), ("2026-10-12", 9)):
            await env.db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, ?, ?)", (U1, day, n))
        assert await env.cog.measure("messages", WEEK, None, t) == {U1: 50}
        for i in range(3):
            await economy.apply(env.db, U1, 100, "daily", WEEK.start + i * DAY, ref=f"daily:{i}")
        await economy.apply(env.db, U1, 100, "daily", WEEK.start - 1, ref="daily:old")
        assert await env.cog.measure("daily", WEEK, None, t) == {U1: 3}
        for day, solved in (("2026-10-05", 1), ("2026-10-06", 0), ("2026-10-07", 1), ("2026-10-04", 1)):
            await env.db.execute("INSERT INTO word_games (day, user_id, solved) VALUES (?, ?, ?)", (day, U1, solved))
        assert await env.cog.measure("word_games", WEEK, None, t) == {U1: 2}
        await env.db.execute("INSERT INTO tournaments (name, size, status, created_by, created_at)"
                             " VALUES ('cup', 8, 'running', 1, ?)", (t,))
        await env.db.execute("INSERT INTO tournament_entries (tournament_id, user_id, joined_at) VALUES (1, ?, ?)", (U2, t))
        assert await env.cog.measure("tournament", WEEK, None, t) == {U2: 1}
        await env.db.execute("INSERT INTO clips (message_id, user_id, url, posted_at) VALUES (1, ?, 'u', ?)", (U3, t))
        assert await env.cog.measure("post_clip", WEEK, None, t) == {U3: 1}
        env.guild.events[1] = SimpleNamespace(status=discord.EventStatus.scheduled)
        await env.db.execute("INSERT INTO gamenights (event_id, host_id, starts_at) VALUES (1, ?, ?)", (U1, t - HOUR))
        await env.db.execute("INSERT INTO gamenights (event_id, host_id, starts_at) VALUES (2, ?, ?)", (U2, WEEK.end + 1))
        assert await env.cog.measure("gamenight", WEEK, None, t) == {U1: 1}
    with_env(body, monkeypatch)


def test_join_squads_ignores_posts_by_fresh_accounts(monkeypatch):
    """An alt posting squads for its main to join doesn't count."""
    async def body(env):
        alt1, alt2 = young_id(env.t), young_id(env.t, 1)
        await env.post(alt1, joiners=(U1,))
        await env.post(alt2, joiners=(U1,))
        await env.post(77, joiners=(U1,))
        assert await env.cog.measure("join_squads", WEEK, None, env.t) == {U1: 1}
        assert await env.cog.claim(env.member(U1)) == []
    with_env(body, monkeypatch)


def test_host_squads_ignores_joins_by_fresh_accounts(monkeypatch):
    """Alts joining your own posts don't make them count."""
    async def body(env):
        alt = young_id(env.t)
        await env.post(U1, joiners=(alt,))
        await env.post(U1, joiners=(alt, young_id(env.t, 1)))
        await env.post(U1, joiners=(alt, U2))
        assert await env.cog.measure("host_squads", WEEK, None, env.t) == {U1: 1}
    with_env(body, monkeypatch)


def test_voice_with_only_a_fresh_account_does_not_count(monkeypatch):
    async def body(env):
        s = WEEK.start
        alt = young_id(env.t)
        await env.voice(U1, s, s + 4 * HOUR)
        await env.voice(alt, s, s + 4 * HOUR)
        assert await env.cog.measure("voice_3h", WEEK, None, env.t) == {}
        await env.voice(U2, s, s + HOUR)
        assert await env.cog.measure("voice_3h", WEEK, None, env.t) == {U1: HOUR, U2: HOUR}
    with_env(body, monkeypatch)


def test_gamenight_counts_only_after_it_starts_and_if_not_cancelled(monkeypatch):
    """Scheduling a game night and having it called off (or never reaching its start) earns nothing."""
    async def body(env):
        t = env.t
        rows = ((1, U1, t + HOUR), (2, U2, t - HOUR), (3, U3, t - HOUR), (4, 77, t - 2 * HOUR))
        for eid, host, at in rows:
            await env.db.execute("INSERT INTO gamenights (event_id, host_id, starts_at) VALUES (?, ?, ?)",
                                 (eid, host, at))
        env.guild.events[1] = SimpleNamespace(status=discord.EventStatus.scheduled)  # not started yet
        env.guild.events[2] = SimpleNamespace(status=discord.EventStatus.cancelled)
        # 3: deleted in Discord
        env.guild.events[4] = SimpleNamespace(status=discord.EventStatus.completed)
        assert await env.cog.measure("gamenight", WEEK, None, t) == {77: 1}
        assert 1 not in env.guild.fetches  # future nights aren't even looked up
        assert await env.cog.measure("gamenight", WEEK, {U1}, t + 2 * HOUR) == {U1: 1}
    with_env(body, monkeypatch)


def test_gamenight_unverifiable_is_not_counted_yet(monkeypatch):
    async def body(env):
        await env.db.execute("INSERT INTO gamenights (event_id, host_id, starts_at) VALUES (1, ?, ?)",
                             (U1, env.t - HOUR))

        async def down(eid):
            raise discord.HTTPException(SimpleNamespace(status=503, reason="down"), "down")
        env.guild.fetch_scheduled_event = down
        assert await env.cog.measure("gamenight", WEEK, None, env.t) == {}
    with_env(body, monkeypatch)


def test_tournament_entry_counts_only_once_it_starts(monkeypatch):
    """Signing up, getting paid by the sweep, then leaving must not work."""
    async def body(env):
        t = env.t
        for tid, status in ((1, "signup"), (2, "running"), (3, "done"), (4, "cancelled")):
            await env.db.execute("INSERT INTO tournaments (id, name, size, status, created_by, created_at)"
                                 " VALUES (?, 'cup', 8, ?, 1, ?)", (tid, status, t))
        for tid, uid in ((1, U1), (2, U2), (3, U3), (4, 77)):
            await env.db.execute("INSERT INTO tournament_entries (tournament_id, user_id, joined_at) VALUES (?, ?, ?)",
                                 (tid, uid, t))
        assert await env.cog.measure("tournament", WEEK, None, t) == {U2: 1, U3: 1}
    with_env(body, monkeypatch)


def test_hall_of_fame_skips_pending_posts(monkeypatch):
    async def body(env):
        await env.star(U1)
        await env.star(U2, board=PENDING)
        assert await env.cog.measure("hall_of_fame", WEEK, None, env.t) == {U1: 1}
    with_env(body, monkeypatch)


def test_voice_counts_only_time_with_others_not_afk(monkeypatch):
    async def body(env):
        s = WEEK.start
        await env.voice(U1, s, s + 4 * HOUR)
        await env.voice(U2, s + HOUR, s + 2 * HOUR)
        await env.voice(U3, s, s + 9 * HOUR, channel=9)  # alone
        got = await env.cog.measure("voice_3h", WEEK, None, env.t)
        assert got == {U1: HOUR, U2: HOUR}
        env.guild.afk_channel = SimpleNamespace(id=5)
        assert await env.cog.measure("voice_3h", WEEK, None, env.t) == {}
    with_env(body, monkeypatch)


def test_progress_drops_tracking_data_for_opted_out(monkeypatch):
    async def body(env):
        await env.complete_all(U1)
        await env.optout(U1)
        mine = (await env.cog.progress(WEEK, {U1})).get(U1)
        assert mine == {"join_squads": 2, "hall_of_fame": 1}
        assert await env.cog.progress(WEEK, ()) == {}
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- claiming
def test_claim_pays_each_once_with_refs_and_claim_rows(monkeypatch):
    async def body(env):
        m = env.member(U1)
        await env.post(77, joiners=(U1,))
        await env.post(78, joiners=(U1,))
        assert await env.cog.claim(m) == [("join_squads", 150)]
        assert await env.balance(U1) == 150
        assert await env.cog.claim(m) == []
        row = await env.db.fetchone("SELECT reason FROM ledger WHERE ref = ?", (CH.ref(WEEK.key, U1, "join_squads"),))
        assert row["reason"] == CH.REASON
        await env.complete_all(U1)
        assert await env.cog.claim(m) == [("voice_3h", 250), ("hall_of_fame", 400), ("bonus", 300)]
        assert await env.balance(U1) == 1100
        rows = await env.db.fetchall("SELECT key FROM challenge_claims WHERE week = ? AND user_id = ?", (WEEK.key, U1))
        assert {r["key"] for r in rows} == {"join_squads", "voice_3h", "hall_of_fame", "bonus"}
    with_env(body, monkeypatch)


def test_claim_records_claim_row_when_ref_already_paid(monkeypatch):
    async def body(env):
        m = env.member(U1)
        await env.complete_all(U1)
        await economy.apply(env.db, U1, 150, CH.REASON, env.t, ref=CH.ref(WEEK.key, U1, "join_squads"))
        paid = await env.cog.claim(m)
        assert ("join_squads", 150) not in paid
        assert await env.balance(U1) == 150 + 250 + 400 + 300
        claims = await env.cog.claims(WEEK.key, {U1})
        assert "join_squads" in claims[U1]
    with_env(body, monkeypatch)


def test_young_accounts_and_bots_cannot_claim(monkeypatch):
    async def body(env):
        young = env.member(U1, created_at=datetime.fromtimestamp(env.t - 5 * DAY, timezone.utc))
        robot = env.member(BOTUSER, bot=True)
        await env.complete_all(U1)
        await env.complete_all(BOTUSER)
        assert await env.cog.claim(young) == []
        assert await env.cog.claim(robot) == []
        assert await env.cog.run_sweep() == 0
        assert await env.balance(U1) == 0
    with_env(body, monkeypatch)


def test_opted_out_member_claims_the_rest_and_the_bonus(monkeypatch):
    async def body(env):
        m = env.member(U1)
        await env.complete_all(U1)
        await env.optout(U1)
        assert await env.cog.claim(m) == [("join_squads", 150), ("hall_of_fame", 400), ("bonus", 300)]
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- sweep
def test_sweep_auto_claims_and_pings_only_that_member(monkeypatch):
    async def body(env):
        env.member(U1)
        env.member(U2)
        env.member(U3)
        await env.post(77, joiners=(U1,))
        await env.post(78, joiners=(U1,))
        assert await env.cog.run_sweep() == 1
        (sent,) = env.guild.games.sent
        assert sent["content"].startswith(f"🏅 <@{U1}>") and "150" in sent["content"]
        pinged_only(sent, U1)
        assert await env.cog.run_sweep() == 0
        assert len(env.guild.games.sent) == 1
        assert await env.balance(U1) == 150
    with_env(body, monkeypatch)


def test_sweep_announces_at_most_a_few(monkeypatch):
    async def body(env):
        ids = list(range(100, 100 + cogmod.MAX_ANNOUNCE + 3))
        for uid in ids:
            env.member(uid)
            await env.star(uid)
        assert await env.cog.run_sweep() == len(ids)
        assert len(env.guild.games.sent) == cogmod.MAX_ANNOUNCE
        for uid in ids:
            assert await env.balance(uid) == 400
    with_env(body, monkeypatch)


def test_sweep_pays_without_games_channel(monkeypatch):
    async def body(env):
        env.guild.text_channels = [env.guild.general]
        env.member(U1)
        await env.star(U1)
        assert await env.cog.run_sweep() == 1
        assert await env.balance(U1) == 400
    with_env(body, monkeypatch)


def test_sweep_settles_last_week_during_grace(monkeypatch):
    async def body(env):
        env.member(U1)
        await env.star(U1, at=WEEK.end - 60)  # Sunday 23:59
        env.t = WEEK.end + HOUR  # Monday 01:00
        assert await env.cog.run_sweep() == 1
        assert await env.db.fetchone("SELECT 1 FROM ledger WHERE ref = ?", (CH.ref(WEEK.key, U1, "hall_of_fame"),))
        env.member(U2)
        await env.star(U2, at=WEEK.end - 60, mid=5)
        env.t = WEEK.end + CH.GRACE + 1
        assert await env.cog.run_sweep() == 0
    with_env(body, monkeypatch)


def test_sweep_never_raises(monkeypatch):
    async def body(env):
        env.member(U1)

        async def boom(*a, **k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(env.cog, "progress", boom)
        assert await env.cog.run_sweep() is None
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- board
def test_board_first_run_marks_done_then_posts_next_monday_once(monkeypatch):
    async def body(env):
        assert await env.cog.run_board() is False  # Wednesday, first ever run
        assert env.guild.games.sent == []
        assert await env.db.fetchone("SELECT 1 FROM jobs WHERE key = ?", (f"challenges:{WEEK.key}",))
        env.t = WEEK.end + 9 * HOUR - 60  # next Monday 08:59
        assert await env.cog.run_board() is False
        env.t = WEEK.end + 9 * HOUR
        assert await env.cog.run_board() is True
        (sent,) = env.guild.games.sent
        embed = sent["embed"]
        nxt = CH.week_of(env.t, TZ)
        assert nxt.key in embed.title
        for c in PICKS:
            assert c.name in embed.description
        assert sent["allowed_mentions"].everyone is False
        (item,) = sent["view"].children
        assert item.custom_id == "challenges:progress"
        assert await env.cog.run_board() is False
        assert len(env.guild.games.sent) == 1
    with_env(body, monkeypatch)


def test_board_waits_for_the_channel(monkeypatch):
    async def body(env):
        await env.cog.run_board()
        env.guild.text_channels = [env.guild.general]
        env.t = WEEK.end + 10 * HOUR
        assert await env.cog.run_board() is False
        assert await env.cog.run_board() is False
        env.guild.text_channels.append(env.guild.games)
        env.t += 3 * DAY
        assert await env.cog.run_board() is True
        assert len(env.guild.games.sent) == 1
    with_env(body, monkeypatch)


def test_board_real_picks_render(monkeypatch):
    async def body(env):
        embed = env.cog.board_embed(WEEK)
        for c in CH.pick(WEEK.key):
            assert c.name in embed.description
    with_env(body, monkeypatch, picks=None)


# ---------------------------------------------------------------- /challenges and buttons
def test_show_is_ephemeral_with_live_claim_button(monkeypatch):
    async def body(env):
        m = env.member(U1)
        inter = FakeInteraction(env.bot, m)
        await env.cog.show(inter)
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"] is True
        (button,) = sent["view"].children
        assert button.custom_id == f"challenges:claim:{U1}" and button.item.disabled
        await env.star(U1)
        inter = FakeInteraction(env.bot, m)
        await env.cog.show(inter)
        (sent,) = inter.of("send_message")
        assert not sent["view"].children[0].item.disabled
        assert "🎁" in sent["embed"].description
    with_env(body, monkeypatch)


def test_show_young_account_explains_and_disables(monkeypatch):
    async def body(env):
        m = env.member(U1, created_at=datetime.fromtimestamp(env.t - DAY, timezone.utc))
        await env.star(U1)
        inter = FakeInteraction(env.bot, m)
        await env.cog.show(inter)
        (sent,) = inter.of("send_message")
        assert sent["view"].children[0].item.disabled
        assert "days old" in sent["embed"].description
    with_env(body, monkeypatch)


def test_claim_button_pays_and_edits(monkeypatch):
    async def body(env):
        m = env.member(U1)
        await env.star(U1)
        inter = FakeInteraction(env.bot, m)
        await ClaimButton(U1).callback(inter)
        (edit,) = inter.of("edit_message")
        assert "400" in edit["embed"].description
        assert edit["view"].children[0].item.disabled
        assert await env.balance(U1) == 400
        again = FakeInteraction(env.bot, m)
        await ClaimButton(U1).callback(again)
        assert "Nothing new" in again.of("edit_message")[0]["embed"].description
        assert await env.balance(U1) == 400
    with_env(body, monkeypatch)


def test_claim_button_refuses_other_members(monkeypatch):
    async def body(env):
        env.member(U1)
        other = env.member(U2)
        await env.star(U1)
        inter = FakeInteraction(env.bot, other)
        await ClaimButton(U1).callback(inter)
        (sent,) = inter.of("send_message")
        assert sent["content"] == cogmod.NOT_YOURS and sent["ephemeral"]
        assert await env.balance(U1) == 0
    with_env(body, monkeypatch)


def test_progress_button_shows_progress(monkeypatch):
    async def body(env):
        m = env.member(U1)
        inter = FakeInteraction(env.bot, m)
        await ProgressButton().callback(inter)
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"] and WEEK.key in sent["embed"].title
    with_env(body, monkeypatch)


def test_button_errors_get_the_error_reply(monkeypatch):
    async def body(env):
        m = env.member(U1)

        async def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "claim", boom)
        inter = FakeInteraction(env.bot, m)
        await ClaimButton(U1).callback(inter)
        (sent,) = inter.of("send_message")
        assert sent["ephemeral"]
    with_env(body, monkeypatch)


def test_dynamic_items_round_trip():
    async def body():
        match = ClaimButton.__discord_ui_compiled_template__.fullmatch(f"challenges:claim:{U2}")
        item = await ClaimButton.from_custom_id(None, None, match)
        assert item.user_id == U2
        assert ProgressButton.__discord_ui_compiled_template__.fullmatch("challenges:progress")
        assert isinstance(await ProgressButton.from_custom_id(None, None, None), ProgressButton)
    run(body())
