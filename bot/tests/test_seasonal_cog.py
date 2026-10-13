"""Offline tests for cogs.seasonal: a real in-memory SQLite database plus small fakes for the
bot, guild, channels, members, roles and interactions. No network."""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import seasonal as cogmod
from cogs.seasonal import GrabButton, Seasonal
from logic import seasonal as S

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
HOUR = 3600
DAY = 24 * HOUR
EVENTS = S.load_events()
HALLOWEEN = EVENTS[0]
RUN = S.run_for(HALLOWEEN, 2026, TZ)
RID = "halloween:2026"


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=TZ).timestamp())


def old_id(n: int) -> int:
    """A Discord id for an account created in 2020."""
    return discord.utils.time_snowflake(datetime(2020, 1, 1, tzinfo=timezone.utc)) + n


def young_id(at: int, n: int = 0) -> int:
    """A Discord id for an account created 5 days before `at`."""
    return discord.utils.time_snowflake(datetime.fromtimestamp(at - 5 * DAY, timezone.utc)) + n


U1, U2, U3, U4, BOTUSER = old_id(1), old_id(2), old_id(3), old_id(4), old_id(50)
T = ts(2026, 10, 24, 15, 0)  # a Saturday afternoon inside Halloween 2026
TODAY = date(2026, 10, 24)


def run(coro):
    return asyncio.run(coro)


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="nope"), "Unknown Message")


class FakeMessage:
    def __init__(self, channel, mid, kwargs):
        self.channel, self.id, self.kwargs = channel, mid, kwargs

    async def delete(self):
        await self.channel.delete_message(self.id)


class FakeText:
    _next = 300

    def __init__(self, name):
        FakeText._next += 1
        self.id = FakeText._next
        self.name = name
        self.sent = []
        self.deleted = []
        self.fail = None
        self.delete_fail = None
        self.next_message = 7000 + self.id * 100

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.next_message += 1
        self.sent.append(dict(content=content, id=self.next_message, **kwargs))
        return FakeMessage(self, self.next_message, kwargs)

    async def delete_message(self, mid):
        if self.delete_fail is not None:
            raise self.delete_fail
        self.deleted.append(mid)

    def get_partial_message(self, mid):
        return FakeMessage(self, mid, {})


class FakeRole:
    def __init__(self, name, position=1, **perms):
        self.id = 4000 + position
        self.name = name
        self.position = position
        self.permissions = SimpleNamespace(**perms)
        self.members = []


class FakeMember:
    def __init__(self, uid, bot=False):
        self.id = uid
        self.bot = bot
        self.created_at = discord.utils.snowflake_time(uid)
        self.mention = f"<@{uid}>"
        self.display_name = f"user*{uid}"
        self.roles = []
        self.fail_roles = None

    async def add_roles(self, role, reason=None):
        if self.fail_roles is not None:
            raise self.fail_roles
        self.roles.append(role)
        role.members.append(self)

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)
        role.members.remove(self)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.games = FakeText(config.GAMES_CHANNEL)
        self.news = FakeText(config.ANNOUNCEMENTS_CHANNEL)
        self.other = FakeText("🕹️・gaming")
        self.text_channels = [self.general, self.games, self.news, self.other]
        self.afk_channel = None
        self.members = []
        self.champ = FakeRole(config.EVENT_ROLE, position=3)
        self.roles = [self.champ]
        self.me = SimpleNamespace(top_role=FakeRole("Front Desk", position=10))

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.cogs = {}
        self.dynamic = []

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None

    def get_cog(self, name):
        return self.cogs.get(name)

    def add_dynamic_items(self, *items):
        self.dynamic += items


class FakeResponse:
    def __init__(self, calls):
        self.calls = calls
        self.done = False
        self.fail_edit = None

    def _finish(self):
        if self.done:
            raise discord.InteractionResponded(None)
        self.done = True

    async def send_message(self, content=None, **kwargs):
        self._finish()
        self.calls.append(("send_message", dict(content=content, **kwargs)))

    async def edit_message(self, **kwargs):
        if self.fail_edit is not None:
            raise self.fail_edit
        self._finish()
        self.calls.append(("edit_message", kwargs))

    def is_done(self):
        return self.done


class FakeInteraction:
    def __init__(self, bot, user, message=None):
        self.calls = []
        self.client = bot
        self.user = user
        self.guild = bot.guild
        self.message = message
        self.response = FakeResponse(self.calls)
        self.followup = SimpleNamespace(send=self._followup)

    async def _followup(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T
        self.posts = 0

    def member(self, uid, **kw):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, **kw)
            self.guild.members.append(m)
        return m

    def slots(self, *times, coins=40):
        """Replace today's seeded drop times with these."""
        made = [S.Slot(i, at, coins, HALLOWEEN.drop_lines[0]) for i, at in enumerate(times)]
        self.cog.slots_for = lambda run, day: made
        return made

    def chat(self, at=None):
        self.cog.last_chat = self.t if at is None else at

    async def drop(self, created_at=None, value=40, message_id=555, event=RID, claimed_by=None):
        at = self.t if created_at is None else created_at
        await self.db.execute(
            "INSERT INTO event_drops (event, channel_id, message_id, kind, value, claimed_by, created_at, claimed_at)"
            " VALUES (?, ?, ?, 'drop', ?, ?, ?, ?)",
            (event, self.guild.general.id, message_id, value, claimed_by, at, at if claimed_by else None))
        return (await self.db.fetchone("SELECT MAX(id) AS id FROM event_drops"))["id"]

    async def claimed(self, uid, n, at=None, event=RID):
        for _ in range(n):
            await self.drop(created_at=at, claimed_by=uid, event=event, message_id=None)

    async def row(self, drop_id):
        return await self.db.fetchone("SELECT * FROM event_drops WHERE id = ?", (drop_id,))

    async def grab(self, uid, drop_id, **member_kw):
        i = FakeInteraction(self.bot, self.member(uid, **member_kw),
                            message=FakeMessage(self.guild.general, 555, {}))
        await self.cog.handle_grab(i, drop_id)
        return i

    async def squad(self, host, joiners=(), at=None):
        at = self.t if at is None else at
        self.posts += 1
        await self.db.execute("INSERT INTO lfg_posts (game, host_id, size, when_text, created_at) VALUES (?, ?, 4, 'now', ?)",
                              (f"game{self.posts}", host, at))
        pid = (await self.db.fetchone("SELECT MAX(id) AS id FROM lfg_posts"))["id"]
        for uid in (host, *joiners):
            await self.db.execute("INSERT INTO lfg_members (post_id, user_id, joined_at) VALUES (?, ?, ?)", (pid, uid, at))

    async def voice(self, uid, start, end, channel=5):
        await self.db.execute('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, ?, ?, ?)',
                              (uid, channel, start, end))

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def quest_claims(self, event=RID):
        rows = await self.db.fetchall("SELECT user_id, key FROM event_claims WHERE event = ? ORDER BY user_id, key", (event,))
        return [(r["user_id"], r["key"]) for r in rows]

    async def jobs(self, like="seasonal:%"):
        return {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs WHERE key LIKE ?", (like,))}

    async def refs(self, uid):
        rows = await self.db.fetchall("SELECT ref, delta, reason FROM ledger WHERE user_id = ? ORDER BY id", (uid,))
        return [(r["ref"], r["delta"], r["reason"]) for r in rows]

    async def balance(self, uid):
        return await economy.balance(self.db, uid)

    async def badges(self):
        rows = await self.db.fetchall("SELECT user_id, key FROM achievements ORDER BY user_id")
        return [(r["user_id"], r["key"]) for r in rows]


def with_env(fn, monkeypatch, quest="squad", events=EVENTS):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Seasonal(bot, events=events)
        bot.cogs["Seasonal"] = cog
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        if quest is not None:
            monkeypatch.setattr(S, "quest_for", lambda run, day, tz: S.QUEST_BY_KEY[quest])
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def no_pings(sent):
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.roles is False and am.users is False


def pinged_only(sent, *uids):
    am = sent["allowed_mentions"]
    assert am.everyone is False and am.roles is False
    assert [u.id for u in am.users] == list(uids)


def custom_ids(view):
    return [c.custom_id for c in view.children]


# ---------------------------------------------------------------- setup
def test_cog_loads_shipped_events_and_survives_a_broken_file(monkeypatch):
    async def body(env):
        assert [e.key for e in Seasonal(env.bot).events] == ["halloween", "winter"]
        monkeypatch.setattr(S, "EVENTS_FILE", S.EVENTS_FILE.with_name("missing.json"))
        monkeypatch.setattr(S, "load_events", lambda: (_ for _ in ()).throw(S.EventError("bad")))
        broken = Seasonal(env.bot)
        assert broken.events == ()
        env.bot.cogs["Seasonal"] = broken
        await broken.run_tick()
        assert await broken.run_sweep() == 0
        embed = await broken.event_embed(env.member(U1))
        assert embed.description == "No event is running right now."
    with_env(body, monkeypatch)


def test_grab_button_round_trips_its_custom_id(monkeypatch):
    async def body(env):
        button = GrabButton(42, "🎃")
        assert button.item.custom_id == "seasonal:grab:42" and button.item.label == "Grab"
        match = GrabButton.__discord_ui_compiled_template__.fullmatch("seasonal:grab:42")
        again = await GrabButton.from_custom_id(None, None, match)
        assert again.drop_id == 42
        assert GrabButton.__discord_ui_compiled_template__.fullmatch("seasonal:grab:x") is None
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- chat activity
def test_on_message_tracks_only_member_messages_in_general(monkeypatch):
    async def body(env):
        cog, g = env.cog, env.guild

        def msg(channel, bot=False, guild=g):
            return SimpleNamespace(guild=guild, channel=channel, author=SimpleNamespace(bot=bot, id=U1))

        await cog.on_message(msg(g.other))
        await cog.on_message(msg(g.general, bot=True))
        await cog.on_message(msg(g.general, guild=None))
        await cog.on_message(msg(g.general, guild=SimpleNamespace(id=5)))
        assert cog.last_chat is None
        await cog.on_message(msg(g.general))
        assert cog.last_chat == env.t
        await cog.on_message(SimpleNamespace())  # nonsense never raises
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- opening
def test_opening_is_announced_once_without_pings(monkeypatch):
    async def body(env):
        env.slots()
        await env.cog.run_tick()
        await env.cog.run_tick()
        (sent,) = env.guild.news.sent
        no_pings(sent)
        assert sent["content"] is None and "Halloween is on" in sent["embed"].description
        assert sent["embed"].color.value == HALLOWEEN.color
        assert {"seasonal:seen:" + RID, "seasonal:open:" + RID} <= await env.jobs()
    with_env(body, monkeypatch)


def test_opening_waits_for_the_channel_and_is_not_sent_outside_events(monkeypatch):
    async def body(env):
        env.slots()
        env.guild.text_channels.remove(env.guild.news)
        await env.cog.run_tick()
        assert "seasonal:open:" + RID not in await env.jobs()
        assert "seasonal:seen:" + RID in await env.jobs()
        env.guild.text_channels.append(env.guild.news)
        await env.cog.run_tick()
        assert len(env.guild.news.sent) == 1
        env.t = ts(2026, 11, 20, 12)
        await env.cog.run_tick()
        assert len(env.guild.news.sent) == 1
    with_env(body, monkeypatch)


def test_opening_failure_is_retried(monkeypatch):
    async def body(env):
        env.slots()
        env.guild.news.fail = RuntimeError("boom")
        await env.cog.run_tick()
        assert "seasonal:open:" + RID not in await env.jobs()
        env.guild.news.fail = None
        await env.cog.run_tick()
        assert len(env.guild.news.sent) == 1
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- posting drops
def test_drop_posts_when_due_and_chat_is_awake(monkeypatch):
    async def body(env):
        (slot,) = env.slots(env.t - 60, coins=61)
        env.chat(env.t - 19 * 60)
        assert await env.cog.maybe_drop(env.guild, RUN, env.t) is True
        (sent,) = env.guild.general.sent
        no_pings(sent)
        row = await env.row(1)
        assert (row["event"], row["value"], row["kind"], row["claimed_by"]) == (RID, 61, "drop", None)
        assert row["message_id"] == sent["id"] and row["channel_id"] == env.guild.general.id
        assert custom_ids(sent["view"]) == ["seasonal:grab:1"] and sent["view"].timeout is None
        assert HALLOWEEN.drop_lines[0] in sent["embed"].description
        assert slot.job_key(RUN, TODAY) in await env.jobs()
        # the same slot never posts twice
        assert await env.cog.maybe_drop(env.guild, RUN, env.t + 60) is False
        assert len(env.guild.general.sent) == 1
    with_env(body, monkeypatch)


def test_no_drop_when_chat_is_quiet_or_nothing_is_due(monkeypatch):
    async def body(env):
        env.slots(env.t - 60)
        assert await env.cog.maybe_drop(env.guild, RUN, env.t) is False  # nobody has spoken
        env.chat(env.t - 21 * 60)
        assert await env.cog.maybe_drop(env.guild, RUN, env.t) is False  # too long ago
        assert await env.jobs("seasonal:drop:%") == set()  # the slot is still open
        env.chat(env.t)
        env.slots(env.t + 60)
        assert await env.cog.maybe_drop(env.guild, RUN, env.t) is False  # not yet
        env.slots(env.t - S.SLOT_WINDOW)
        assert await env.cog.maybe_drop(env.guild, RUN, env.t) is False  # waited too long: skipped
        assert env.guild.general.sent == []
    with_env(body, monkeypatch)


def test_never_two_drops_within_thirty_minutes(monkeypatch):
    async def body(env):
        env.chat()
        env.slots(env.t - 60)
        await env.drop(created_at=env.t - 29 * 60, message_id=None)
        assert await env.cog.maybe_drop(env.guild, RUN, env.t) is False
        env.t += 60
        env.chat()
        assert await env.cog.maybe_drop(env.guild, RUN, env.t) is True
    with_env(body, monkeypatch)


def test_failed_post_leaves_no_claimable_row_and_does_not_repeat(monkeypatch):
    async def body(env):
        env.chat()
        env.slots(env.t - 60)
        env.guild.general.fail = RuntimeError("boom")
        await env.cog.run_tick()  # never raises
        assert await env.db.fetchall("SELECT id FROM event_drops") == []
        env.guild.general.fail = None
        await env.cog.run_tick()
        assert env.guild.general.sent == []  # the slot was spent
    with_env(body, monkeypatch)


def test_no_drop_without_general_channel_or_outside_event(monkeypatch):
    async def body(env):
        env.chat()
        env.slots(env.t - 60)
        env.guild.text_channels.remove(env.guild.general)
        assert await env.cog.maybe_drop(env.guild, RUN, env.t) is False
        env.guild.text_channels.append(env.guild.general)
        env.t = ts(2026, 11, 3, 15)
        env.chat()
        env.slots(env.t - 60)
        await env.cog.run_tick()
        assert env.guild.general.sent == []
    with_env(body, monkeypatch)


def test_seeded_slots_drive_a_whole_day(monkeypatch):
    async def body(env):
        slots = S.drop_slots(RUN, TODAY, TZ)
        posted = 0
        env.t = ts(2026, 10, 24, 9, 59)
        while env.t < ts(2026, 10, 25):
            env.chat()
            posted += bool(await env.cog.maybe_drop(env.guild, RUN, env.t))
            env.t += 60
        assert posted == len(slots)
        rows = await env.db.fetchall("SELECT created_at, value FROM event_drops ORDER BY id")
        assert all(0 <= r["created_at"] - s.at < 60 for r, s in zip(rows, slots))
        assert [r["value"] for r in rows] == [s.coins for s in slots]
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- claiming drops
def test_first_grab_wins_and_is_paid_once(monkeypatch):
    async def body(env):
        d = await env.drop(value=55)
        i = await env.grab(U1, d)
        (edit,) = i.of("edit_message")
        assert edit["view"] is None
        no_pings(edit)
        assert edit["embed"].description == f"🎃 <@{U1}> grabbed the pumpkin: **+55 coins** and 1 event point."
        row = await env.row(d)
        assert row["claimed_by"] == U1 and row["claimed_at"] == env.t
        assert await env.refs(U1) == [(f"event:halloween:2026:drop:{d}", 55, "event")]
        # the second presser gets nothing
        j = await env.grab(U2, d)
        assert j.of("send_message")[0]["content"] == cogmod.TAKEN and j.of("send_message")[0]["ephemeral"]
        assert await env.balance(U2) == 0 and (await env.row(d))["claimed_by"] == U1
        # pressing again doesn't pay again
        await env.grab(U1, d)
        assert await env.balance(U1) == 55
        board = await env.cog.board(RID)
        assert [(s.user_id, s.points) for s in board] == [(U1, 1)]
    with_env(body, monkeypatch)


def test_concurrent_grabs_have_exactly_one_winner(monkeypatch):
    async def body(env):
        d = await env.drop(value=30)
        users = [old_id(100 + n) for n in range(8)]
        results = await asyncio.gather(*(env.grab(u, d) for u in users))
        assert sum(1 for i in results if i.of("edit_message")) == 1
        paid = [u for u in users if await env.balance(u)]
        assert paid == [(await env.row(d))["claimed_by"]]
        ledger = await env.db.fetchall("SELECT id FROM ledger")
        assert len(ledger) == 1
    with_env(body, monkeypatch)


def test_fresh_accounts_and_bots_cannot_grab(monkeypatch):
    async def body(env):
        d = await env.drop()
        alt = young_id(env.t)
        i = await env.grab(alt, d)
        assert i.of("send_message")[0]["content"] == cogmod.TOO_NEW and i.of("send_message")[0]["ephemeral"]
        k = await env.grab(BOTUSER, d, bot=True)
        assert k.calls == []
        assert (await env.row(d))["claimed_by"] is None
        assert await env.db.fetchall("SELECT id FROM ledger") == []
    with_env(body, monkeypatch)


def test_daily_cap_of_twelve_per_local_day(monkeypatch):
    async def body(env):
        await env.claimed(U1, 11, at=env.t - HOUR)
        await env.claimed(U1, 5, at=ts(2026, 10, 23, 22))  # yesterday doesn't count
        a, b = await env.drop(), await env.drop()
        assert (await env.grab(U1, a)).of("edit_message")
        i = await env.grab(U1, b)
        assert i.of("send_message")[0]["content"] == cogmod.CAPPED
        assert (await env.row(b))["claimed_by"] is None
        assert (await env.grab(U2, b)).of("edit_message")  # still there for someone else
    with_env(body, monkeypatch)


def test_expired_or_unknown_drop_cannot_be_grabbed(monkeypatch):
    async def body(env):
        d = await env.drop(created_at=env.t - S.DROP_EXPIRY)
        i = await env.grab(U1, d)
        assert i.of("send_message")[0]["content"] == cogmod.GONE
        assert env.guild.general.deleted == [555]  # the leftover message is cleaned up
        assert (await env.row(d))["message_id"] is None and (await env.row(d))["claimed_by"] is None
        j = await env.grab(U1, 9999)
        assert j.of("send_message")[0]["content"] == cogmod.GONE
        assert await env.balance(U1) == 0
        fresh = await env.drop(created_at=env.t - S.DROP_EXPIRY + 1)
        assert (await env.grab(U1, fresh)).of("edit_message")
    with_env(body, monkeypatch)


def test_grab_after_event_text_falls_back_when_event_is_unknown(monkeypatch):
    async def body(env):
        d = await env.drop(event="easter:2026", value=25)
        i = await env.grab(U1, d)
        assert f"<@{U1}> grabbed it" in i.of("edit_message")[0]["embed"].description
        assert await env.refs(U1) == [(f"event:easter:2026:drop:{d}", 25, "event")]
    with_env(body, monkeypatch)


def test_failed_edit_still_tells_the_winner(monkeypatch):
    async def body(env):
        d = await env.drop(value=30)
        i = FakeInteraction(env.bot, env.member(U1))
        i.response.fail_edit = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        await env.cog.handle_grab(i, d)
        assert "You got it" in i.of("send_message")[0]["content"]
        assert await env.balance(U1) == 30
    with_env(body, monkeypatch)


def test_button_callback_reports_errors_instead_of_raising(monkeypatch):
    async def body(env):
        i = FakeInteraction(env.bot, env.member(U1))

        async def boom(*a):
            raise RuntimeError("boom")
        env.cog.handle_grab = boom
        await GrabButton(1).callback(i)
        assert i.of("send_message")[0]["ephemeral"] is True
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- expiry
def test_unclaimed_drops_are_deleted_after_ten_minutes(monkeypatch):
    async def body(env):
        old = await env.drop(created_at=env.t - 600, message_id=901)
        new = await env.drop(created_at=env.t - 599, message_id=902)
        won = await env.drop(created_at=env.t - 900, message_id=903, claimed_by=U1)
        assert await env.cog.expire_drops(env.guild, env.t) == 1
        assert env.guild.general.deleted == [901]
        assert (await env.row(old))["message_id"] is None
        assert (await env.row(new))["message_id"] == 902 and (await env.row(won))["message_id"] == 903
        assert await env.cog.expire_drops(env.guild, env.t) == 0  # nothing left to do
    with_env(body, monkeypatch)


def test_expiry_handles_missing_messages_channels_and_errors(monkeypatch):
    async def body(env):
        a = await env.drop(created_at=env.t - 700, message_id=901)
        env.guild.general.delete_fail = not_found()
        assert await env.cog.expire_drops(env.guild, env.t) == 1  # already gone
        b = await env.drop(created_at=env.t - 700, message_id=902)
        env.guild.general.delete_fail = RuntimeError("discord is down")
        assert await env.cog.expire_drops(env.guild, env.t) == 0  # retried later
        assert (await env.row(b))["message_id"] == 902
        assert await env.cog.expire_drops(env.guild, env.t + cogmod.GIVE_UP_DELETING) == 1  # gives up
        c = await env.drop(created_at=env.t - 700, message_id=903)
        await env.db.execute("UPDATE event_drops SET channel_id = 1 WHERE id = ?", (c,))
        assert await env.cog.expire_drops(env.guild, env.t) == 1  # the channel is gone
        assert (await env.row(a))["message_id"] is None
    with_env(body, monkeypatch)


def test_tick_expires_drops_even_after_the_event(monkeypatch):
    async def body(env):
        await env.drop(created_at=RUN.end - 60, message_id=901)
        env.t = RUN.end + 600
        await env.cog.run_tick()
        assert env.guild.general.deleted == [901]
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- measuring quests
def test_squad_quest_needs_an_established_squadmate_today(monkeypatch):
    async def body(env):
        q = S.QUEST_BY_KEY["squad"]
        alt = young_id(env.t)
        await env.squad(U1, joiners=(U2,))  # both count
        await env.squad(U3)  # alone
        await env.squad(U4, joiners=(alt,))  # only an alt for company
        await env.squad(old_id(9), joiners=(old_id(10),), at=ts(2026, 10, 23, 12))  # yesterday
        got = await env.cog.measure(q, TODAY, None, env.t)
        assert got == {U1: 1, U2: 1, alt: 1}
        assert await env.cog.measure(q, TODAY, {U2}, env.t) == {U2: 1}
    with_env(body, monkeypatch)


def test_win_game_quest_from_the_ledger(monkeypatch):
    async def body(env):
        q, t = S.QUEST_BY_KEY["win_game"], env.t
        for u in (U1, U2, U3):
            await economy.apply(env.db, u, 500, "seed", t - 3 * DAY)
        await economy.apply(env.db, U1, 25, "trivia", t, ref="trivia:1")
        await economy.apply(env.db, U2, -100, "blackjack", t)
        await economy.apply(env.db, U2, 100, "blackjack", t, ref="blackjack:a")  # a push
        await economy.apply(env.db, U3, -100, "blackjack", t)
        await economy.apply(env.db, U3, 200, "blackjack", t, ref="blackjack:b")  # a win
        await economy.apply(env.db, U4, 25, "trivia", t - DAY, ref="trivia:2")  # yesterday
        assert await env.cog.measure(q, TODAY, None, t) == {U1: 1, U3: 1}
    with_env(body, monkeypatch)


def test_chat_word_and_voice_quests(monkeypatch):
    async def body(env):
        t = env.t
        await env.db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, '2026-10-24', 5)", (U1,))
        await env.db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, '2026-10-23', 50)", (U2,))
        assert await env.cog.measure(S.QUEST_BY_KEY["chat"], TODAY, None, t) == {U1: 5}
        await env.db.execute("INSERT INTO word_games (day, user_id, solved) VALUES ('2026-10-24', ?, 1)", (U1,))
        await env.db.execute("INSERT INTO word_games (day, user_id, solved) VALUES ('2026-10-24', ?, 0)", (U2,))
        assert await env.cog.measure(S.QUEST_BY_KEY["word"], TODAY, None, t) == {U1: 1}
        start = ts(2026, 10, 24, 12)
        alt = young_id(t)
        await env.voice(U1, start, start + 40 * 60)
        await env.voice(U2, start, start + 40 * 60)
        await env.voice(U3, start, start + 40 * 60, channel=6)  # with only an alt: alone
        await env.voice(alt, start, start + 40 * 60, channel=6)
        got = await env.cog.measure(S.QUEST_BY_KEY["voice"], TODAY, None, t)
        assert got == {U1: 40 * 60, U2: 40 * 60}
        assert await env.cog.measure(S.QUEST_BY_KEY["voice"], TODAY, {U2}, t) == {U2: 40 * 60}
    with_env(body, monkeypatch)


def test_every_quest_in_the_pool_can_be_measured(monkeypatch):
    async def body(env):
        for q in S.QUESTS:
            assert await env.cog.measure(q, TODAY, None, env.t) == {}
            assert await env.cog.measure(q, TODAY, {U1}, env.t) == {}
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- granting quests
def test_sweep_grants_the_quest_once_with_a_ping_only_them(monkeypatch):
    async def body(env):
        for u in (U1, U2):
            env.member(u)
        await env.squad(U1, joiners=(U2,))
        assert await env.cog.run_sweep() == 2
        assert await env.quest_claims() == [(U1, "quest:2026-10-24"), (U2, "quest:2026-10-24")]
        assert await env.refs(U1) == [(f"event:halloween:2026:quest:2026-10-24:{U1}", 100, "event")]
        assert len(env.guild.games.sent) == 2
        pinged_only(env.guild.games.sent[0], U1)
        assert f"<@{U1}>" in env.guild.games.sent[0]["content"]
        assert [(s.user_id, s.points) for s in await env.cog.board(RID)] == [(U1, 3), (U2, 3)]
        # again: nothing new
        assert await env.cog.run_sweep() == 0
        assert await env.balance(U1) == 100 and len(env.guild.games.sent) == 2
    with_env(body, monkeypatch)


def test_sweep_skips_alts_bots_leavers_and_unfinished(monkeypatch):
    async def body(env):
        alt = young_id(env.t)
        env.member(alt)
        env.member(BOTUSER, bot=True)
        env.member(U1)
        await env.squad(U1, joiners=(alt, BOTUSER, U2))  # U2 isn't in the server any more
        await env.squad(U3)  # U3 is alone
        env.member(U3)
        assert await env.cog.run_sweep() == 1
        assert await env.quest_claims() == [(U1, "quest:2026-10-24")]
    with_env(body, monkeypatch)


def test_tracking_quests_are_skipped_for_opted_out_members(monkeypatch):
    async def body(env):
        for u in (U1, U2):
            env.member(u)
            await env.db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, '2026-10-24', 9)", (u,))
        await env.optout(U2)
        assert await env.cog.run_sweep() == 1
        assert await env.quest_claims() == [(U1, "quest:2026-10-24")]
        embed = await env.cog.event_embed(env.member(U2))
        assert "stats are off" in embed.description
    with_env(body, monkeypatch, quest="chat")


def test_non_tracking_quests_still_count_for_opted_out_members(monkeypatch):
    async def body(env):
        for u in (U1, U2):
            env.member(u)
        await env.optout(U2)
        await env.squad(U1, joiners=(U2,))
        assert await env.cog.run_sweep() == 2
    with_env(body, monkeypatch, quest="squad")


def test_sweep_grants_yesterday_during_the_grace_only(monkeypatch):
    async def body(env):
        env.member(U1), env.member(U2)
        await env.squad(U1, joiners=(U2,), at=ts(2026, 10, 23, 23, 30))
        env.t = ts(2026, 10, 24, 1, 0)
        assert await env.cog.run_sweep() == 2
        assert await env.quest_claims() == [(U1, "quest:2026-10-23"), (U2, "quest:2026-10-23")]
        await env.db.execute("DELETE FROM event_claims")
        await env.db.execute("DELETE FROM ledger")
        env.t = ts(2026, 10, 24, 3, 0)
        assert await env.cog.run_sweep() == 0
    with_env(body, monkeypatch)


def test_sweep_announces_a_few_and_pays_everyone(monkeypatch):
    async def body(env):
        users = [old_id(200 + n) for n in range(cogmod.MAX_ANNOUNCE + 4)]
        for u in users:
            env.member(u)
        await env.squad(users[0], joiners=users[1:])
        assert await env.cog.run_sweep() == len(users)
        assert len(env.guild.games.sent) == cogmod.MAX_ANNOUNCE
        assert all([await env.balance(u) == 100 for u in users])
    with_env(body, monkeypatch)


def test_sweep_without_games_channel_or_event_or_guild(monkeypatch):
    async def body(env):
        env.member(U1), env.member(U2)
        await env.squad(U1, joiners=(U2,))
        env.guild.text_channels.remove(env.guild.games)
        assert await env.cog.run_sweep() == 2  # paid quietly
        env.t = ts(2026, 11, 20, 12)
        assert await env.cog.run_sweep() == 0
        env.bot.guild = None
        assert await env.cog.run_sweep() == 0
        await env.cog.run_tick()
    with_env(body, monkeypatch)


def test_sweep_survives_a_failing_announcement(monkeypatch):
    async def body(env):
        env.member(U1), env.member(U2)
        await env.squad(U1, joiners=(U2,))
        env.guild.games.fail = RuntimeError("boom")
        assert await env.cog.run_sweep() == 2
        assert await env.balance(U1) == 100
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- /event
def test_event_embed_shows_points_quest_board_and_time_left(monkeypatch):
    async def body(env):
        me = env.member(U1)
        await env.claimed(U1, 2)
        await env.claimed(U2, 5)
        embed = await env.cog.event_embed(me)
        text = embed.description
        assert embed.title == "🎃 Halloween" and embed.color.value == HALLOWEEN.color
        assert f"Ends <t:{RUN.end}:R>." in text and "**2 points** (#2)" in text
        assert f"🥇 <@{U2}> · 5 points" in text and f"🥈 <@{U1}> · 2 points" in text
        assert "▫️" in text and "Today's quest" in text
        assert embed.fields[0].name == "How it works"
    with_env(body, monkeypatch)


def test_event_command_replies_publicly_without_pings_and_pays_a_finished_quest(monkeypatch):
    async def body(env):
        me = env.member(U1)
        env.member(U2)
        await env.squad(U2, joiners=(U1,))
        i = FakeInteraction(env.bot, me)
        await Seasonal.event.callback(env.cog, i)
        (sent,) = i.of("send_message")
        no_pings(sent)
        assert "ephemeral" not in sent
        assert "✅" in sent["embed"].description and "**3 points** (#1)" in sent["embed"].description
        assert await env.balance(U1) == 100 and await env.balance(U2) == 0  # only the caller is settled here
        assert await env.cog.run_sweep() == 1  # the sweep pays the other one, not U1 again
        assert await env.balance(U1) == 100
    with_env(body, monkeypatch)


def test_event_embed_for_a_fresh_account_explains_the_wait(monkeypatch):
    async def body(env):
        alt = env.member(young_id(env.t))
        env.member(U2)
        await env.squad(U2, joiners=(alt.id,))
        embed = await env.cog.event_embed(alt)
        assert "days old" in embed.description and await env.balance(alt.id) == 0
    with_env(body, monkeypatch)


def test_event_embed_outside_events_names_the_next_one(monkeypatch):
    async def body(env):
        env.t = ts(2026, 10, 11, 12)
        embed = await env.cog.event_embed(env.member(U1))
        assert "Next up: 🎃 **Halloween**" in embed.description and f"<t:{RUN.start}:R>" in embed.description
        env.t = ts(2026, 11, 5, 12)
        assert "Winter Fest" in (await env.cog.event_embed(env.member(U1))).description
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- the end
async def finished_event(env):
    """U1 16 points, U2 10, U3 9, U4 2 (rows only, no drop coins); the event was seen while it ran."""
    for u in (U1, U2, U3, U4):
        env.member(u)
    await env.claimed(U1, 16, at=RUN.end - 5 * DAY)
    await env.claimed(U2, 10, at=RUN.end - 4 * DAY)
    await env.claimed(U3, 9, at=RUN.end - 3 * DAY)
    await env.claimed(U4, 2, at=RUN.end - 2 * DAY)
    await env.cog.mark("seasonal:seen:" + RID, RUN.start)
    await env.cog.mark("seasonal:open:" + RID, RUN.start)
    env.t = RUN.end + cogmod.FINALE_DELAY


def test_finale_pays_badges_crowns_and_announces_once(monkeypatch):
    async def body(env):
        await finished_event(env)
        previous = env.member(old_id(77))
        await previous.add_roles(env.guild.champ)
        await env.cog.run_tick()
        assert [await env.balance(u) for u in (U1, U2, U3, U4)] == [2000, 1000, 500, 0]
        assert (f"event:halloween:2026:rank:1", 2000, "event") in await env.refs(U1)
        assert (f"event:halloween:2026:rank:3", 500, "event") in await env.refs(U3)
        assert await env.badges() == [(U1, "event_halloween_2026"), (U2, "event_halloween_2026")]
        assert env.guild.champ.members == [env.member(U1)] and previous.roles == []
        (sent,) = env.guild.news.sent
        pinged_only(sent, U1, U2, U3)
        assert sent["content"] == f"<@{U1}> <@{U2}> <@{U3}>"
        text = sent["embed"].description
        assert "Halloween 2026 is over" in text and f"🥇 <@{U1}> · 16 points · **+2,000 coins**" in text
        assert "Event Champ" in text and "2 members earned" in text
        assert "seasonal:finale:" + RID in await env.jobs()
        # later ticks and a restart change nothing
        await env.cog.run_tick()
        again = Seasonal(env.bot, events=EVENTS)
        await again.run_tick()
        assert len(env.guild.news.sent) == 1 and await env.balance(U1) == 2000
    with_env(body, monkeypatch)


def test_finale_waits_for_the_last_drops_to_expire(monkeypatch):
    async def body(env):
        await finished_event(env)
        env.t = RUN.end + cogmod.FINALE_DELAY - 1
        await env.cog.run_tick()
        assert env.guild.news.sent == [] and await env.badges() == []
    with_env(body, monkeypatch)


def test_finale_is_skipped_for_events_the_bot_never_saw(monkeypatch):
    async def body(env):
        env.member(U1)
        await env.claimed(U1, 20, at=RUN.end - DAY)
        env.t = RUN.end + DAY
        await env.cog.run_tick()
        assert env.guild.news.sent == [] and await env.balance(U1) == 0
        assert await env.badges() == []
    with_env(body, monkeypatch)


def test_finale_catches_up_after_downtime(monkeypatch):
    async def body(env):
        await finished_event(env)
        env.t = RUN.end + 9 * DAY  # the bot was off for over a week
        await env.cog.run_tick()
        assert len(env.guild.news.sent) == 1 and await env.balance(U1) == 2000
    with_env(body, monkeypatch)


def test_finale_retries_the_announcement_without_paying_twice(monkeypatch):
    async def body(env):
        await finished_event(env)
        env.guild.text_channels.remove(env.guild.news)
        await env.cog.run_tick()
        assert "seasonal:finale:" + RID not in await env.jobs()
        assert await env.balance(U1) == 2000  # paid already
        env.guild.text_channels.append(env.guild.news)
        env.guild.news.fail = RuntimeError("boom")
        await env.cog.run_tick()
        env.guild.news.fail = None
        await env.cog.run_tick()
        assert len(env.guild.news.sent) == 1
        assert await env.balance(U1) == 2000 and len(await env.badges()) == 2
        assert env.guild.champ.members == [env.member(U1)]
    with_env(body, monkeypatch)


def test_finale_skips_members_who_left_and_bots_for_prizes(monkeypatch):
    async def body(env):
        await finished_event(env)
        env.guild.members.remove(env.member(U1))
        await env.cog.run_tick()
        assert await env.balance(U1) == 0  # no prize
        assert await env.balance(U2) == 2000 and await env.balance(U4) == 500
        assert (U1, "event_halloween_2026") in await env.badges()  # the badge is theirs if they come back
        pinged_only(env.guild.news.sent[0], U2, U3, U4)
        assert env.guild.champ.members == [env.member(U2)]
    with_env(body, monkeypatch)


def test_finale_with_an_empty_board_is_quiet_and_done(monkeypatch):
    async def body(env):
        await env.cog.mark("seasonal:seen:" + RID, RUN.start)
        env.t = RUN.end + HOUR
        await env.cog.run_tick()
        assert env.guild.news.sent == [] and "seasonal:finale:" + RID in await env.jobs()
    with_env(body, monkeypatch)


def test_finale_settles_the_last_days_quest_first(monkeypatch):
    async def body(env):
        await finished_event(env)
        await env.claimed(U4, 6, at=RUN.end - 2 * DAY)  # U4: 8 points, behind U2 and U3
        await env.squad(U4, joiners=(old_id(60),), at=RUN.end - HOUR)  # +3 on the last evening: second place
        await env.cog.run_tick()
        assert (U4, "quest:2026-10-31") in await env.quest_claims()
        assert await env.balance(U4) == 100 + 1000 and await env.balance(U2) == 500 and await env.balance(U3) == 0
        assert (U4, "event_halloween_2026") in await env.badges()
        assert env.guild.games.sent == []  # no quest line after the event
    with_env(body, monkeypatch)


def test_role_is_not_given_when_unsafe_or_out_of_reach(monkeypatch):
    async def body(env):
        await finished_event(env)
        env.guild.champ.permissions = SimpleNamespace(manage_messages=True)
        await env.cog.run_tick()
        assert env.guild.champ.members == []
        assert "Event Champ" not in env.guild.news.sent[0]["embed"].description
        assert await env.balance(U1) == 2000  # still paid and announced
        env.guild.champ.permissions = SimpleNamespace()
        env.guild.champ.position = 10  # not below the bot
        assert await env.cog.crown(env.guild, RUN, U1) is False
        env.guild.roles = []
        assert await env.cog.crown(env.guild, RUN, U1) is False
        assert env.guild.champ.members == []
    with_env(body, monkeypatch)


def test_role_errors_do_not_stop_the_finale(monkeypatch):
    async def body(env):
        await finished_event(env)
        env.member(U1).fail_roles = discord.HTTPException(SimpleNamespace(status=403, reason="x"), "no")
        await env.cog.run_tick()
        assert len(env.guild.news.sent) == 1 and "Event Champ" not in env.guild.news.sent[0]["embed"].description
    with_env(body, monkeypatch)


def test_winter_finale_uses_its_start_year(monkeypatch):
    async def body(env):
        winter = S.run_for(EVENTS[1], 2026, TZ)
        env.member(U1)
        await env.claimed(U1, 10, at=winter.end - DAY, event="winter:2026")
        await env.cog.mark("seasonal:seen:winter:2026", winter.start)
        env.t = winter.end + HOUR
        await env.cog.run_tick()
        assert ("event:winter:2026:rank:1", 2000, "event") in await env.refs(U1)
        assert await env.badges() == [(U1, "event_winter_2026")]
        assert "Winter Fest 2026 is over" in env.guild.news.sent[0]["embed"].description
    with_env(body, monkeypatch)


def test_tick_steps_fail_independently(monkeypatch):
    async def body(env):
        env.slots()

        async def boom(*a):
            raise RuntimeError("boom")
        env.cog.expire_drops = boom
        await env.cog.run_tick()
        assert len(env.guild.news.sent) == 1  # the opening still went out
    with_env(body, monkeypatch)
