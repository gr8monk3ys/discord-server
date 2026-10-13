"""Offline tests for cogs.events.Events: a real in-memory SQLite database plus fakes for
the bot, guild, channels, roles, scheduled events and interactions. No network: the
GamerPower fetch is injected. Time is controlled by patching cogs.events.now."""

import asyncio
import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest
from discord import app_commands

import config
import db as dbmod
from cogs import events as cogmod
from cogs.events import FREE_GAMES_JOB, FREE_GAMES_URL, Events
from logic.schedule import occurrence

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
HOST, A, B, BOTUSER = 1, 2, 3, 50
USER_A = 70
MIN = 60
HOUR = 3600


def run(coro):
    return asyncio.run(coro)


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


T0 = ts(2026, 10, 7, 12)  # Wednesday, local noon
OVERWATCH = config.game_by_key("overwatch")


def choice(value, name=None):
    return app_commands.Choice(name=name or value, value=value)


# ---------------------------------------------------------------- fakes
class FakeVoice:
    def __init__(self, cid, name):
        self.id, self.name = cid, name
        self.mention = f"<#{cid}>"


class FakeText:
    def __init__(self, cid, name, fail=False):
        self.id, self.name = cid, name
        self.mention = f"<#{cid}>"
        self.sent = []
        self.fail = fail

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        self.sent.append(dict(content=content, **kwargs))


class FakeRole:
    def __init__(self, rid, name):
        self.id, self.name = rid, name
        self.mention = f"<@&{rid}>"


class FakeUser:
    def __init__(self, uid, bot=False):
        self.id, self.bot = uid, bot
        self.display_name = f"user{uid}"
        self.mention = f"<@{uid}>"

    def __str__(self):
        return self.display_name


class FakeEvent:
    def __init__(self, eid, guild, **kw):
        self.id = eid
        self.guild = guild
        self.kwargs = kw
        self.name = kw["name"]
        self.start_time = kw["start_time"]
        self.channel = kw["channel"]
        self.channel_id = kw["channel"].id
        self.status = discord.EventStatus.scheduled
        self.url = f"https://discord.com/events/{GUILD_ID}/{eid}"
        self.interested = []

    async def users(self, **kw):
        for u in self.interested:
            yield u


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.squad = FakeVoice(101, config.SQUAD_VOICE)
        self.lobby = FakeVoice(100, config.LOBBY_VOICE)
        self.voice_channels = [self.lobby, self.squad]
        self.gaming = FakeText(300, config.GAMING_CHANNEL)
        self.overwatch = FakeText(301, OVERWATCH.channel_name)
        self.text_channels = [self.gaming, self.overwatch]
        self.overwatch_role = FakeRole(700, OVERWATCH.role)
        self.roles = [self.overwatch_role]
        self.events: dict[int, FakeEvent] = {}
        self.cached: set[int] = set()  # event ids in the gateway cache
        self.next_id = 5000
        self.forbidden = False
        self.create_error = None  # any other exception create_scheduled_event should raise
        self.fetches = 0
        self.members: dict[int, FakeUser] = {}  # whoever ran a command, as the member cache has them

    def get_member(self, uid):
        return self.members.get(uid)

    async def create_scheduled_event(self, **kw):
        if self.forbidden:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        if self.create_error is not None:
            raise self.create_error
        self.next_id += 1
        ev = FakeEvent(self.next_id, self, **kw)
        self.events[ev.id] = ev
        self.cached.add(ev.id)
        return ev

    def get_scheduled_event(self, eid):
        return self.events.get(eid) if eid in self.cached else None

    async def fetch_scheduled_event(self, eid, **kw):
        self.fetches += 1
        if eid not in self.events:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown event")
        return self.events[eid]


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.ready = asyncio.Event()

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    async def wait_until_ready(self):
        await self.ready.wait()


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

    async def defer(self, **kwargs):
        self._finish()
        self.calls.append(("defer", kwargs))

    def is_done(self):
        return self.done


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, user, guild):
        self.calls = []
        self.user = user
        self.guild = guild
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def of(self, kind):
        return [kw for k, kw in self.calls if k == kind]

    def texts(self):
        return [kw["content"] for _, kw in self.calls if kw.get("content")]


class FakeFeed:
    """Injected fetch_json: returns queued results (or raises queued exceptions)."""

    def __init__(self, *results):
        self.results = list(results)
        self.urls = []

    async def __call__(self, url):
        self.urls.append(url)
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, BaseException):
            raise r
        return r


class Env:
    def __init__(self, db, guild, bot, cog, feed):
        self.db, self.guild, self.bot, self.cog, self.feed = db, guild, bot, cog, feed
        self.t = T0

    def inter(self, uid=HOST, staff=True):
        user = FakeUser(uid)
        # Staff are exempt from the /gamenight spam limits; most tests aren't about those.
        user.roles = [SimpleNamespace(name=config.KEEPER_ROLE)] if staff else []
        self.guild.members[uid] = user
        return FakeInteraction(user, self.guild)

    async def gamenight(self, game="overwatch", when="9pm", size=None, note=None, uid=HOST, staff=True):
        i = self.inter(uid, staff)
        await self.cog.gamenight.callback(self.cog, i, choice(game), when, size, note)
        return i

    async def rows(self, sql, params=()):
        return [dict(r) for r in await self.db.fetchall(sql, params)]

    async def jobs(self):
        return {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs")}


def with_env(fn, monkeypatch, feed=None):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        f = feed or FakeFeed([])
        cog = Events(bot, fetch_json=f)
        env = Env(db, guild, bot, cog, f)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- /gamenight
def test_gamenight_creates_event_row_and_announcement(monkeypatch):
    async def go(env):
        g = env.guild
        i = await env.gamenight(when="9pm", note="bring comms")
        assert len(g.events) == 1
        ev = next(iter(g.events.values()))
        kw = ev.kwargs
        assert kw["name"] == "Overwatch game night"
        assert kw["entity_type"] is discord.EntityType.voice
        assert kw["channel"] is g.squad
        assert kw["privacy_level"] is discord.PrivacyLevel.guild_only
        assert kw["start_time"] == datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)
        assert kw["start_time"].tzinfo is not None
        assert "user1" in kw["description"] and "bring comms" in kw["description"]

        rows = await env.rows("SELECT * FROM gamenights")
        assert rows == [dict(event_id=ev.id, host_id=HOST, game="overwatch",
                             starts_at=ts(2026, 10, 7, 21), reminded=0)]

        [post] = g.overwatch.sent
        assert g.overwatch_role.mention in post["content"]
        assert ev.url in post["content"]
        am = post["allowed_mentions"]
        assert am.everyone is False and am.users is False
        assert [r.id for r in am.roles] == [g.overwatch_role.id]
        assert any(ev.url in t for t in i.texts())
        assert g.gaming.sent == []
    with_env(go, monkeypatch)


def test_gamenight_big_group_uses_lobby(monkeypatch):
    async def go(env):
        await env.gamenight(size=8)
        ev = next(iter(env.guild.events.values()))
        assert ev.kwargs["channel"] is env.guild.lobby
        await env.gamenight(size=5)
        assert env.guild.events[ev.id + 1].kwargs["channel"] is env.guild.squad
    with_env(go, monkeypatch)


def test_gamenight_anything_posts_in_gaming_without_pings(monkeypatch):
    async def go(env):
        await env.gamenight(game=cogmod.ANYTHING, when="tomorrow 8pm")
        ev = next(iter(env.guild.events.values()))
        assert ev.kwargs["name"] == "Game night"
        [post] = env.guild.gaming.sent
        am = post["allowed_mentions"]
        assert am.roles is False and am.users is False and am.everyone is False
        assert (await env.rows("SELECT game FROM gamenights"))[0]["game"] is None
    with_env(go, monkeypatch)


def test_note_with_role_mention_cannot_ping(monkeypatch):
    async def go(env):
        await env.gamenight(game=cogmod.ANYTHING, note="<@&123> @everyone <@456>")
        [post] = env.guild.gaming.sent
        am = post["allowed_mentions"]
        assert am.roles is False and am.users is False and am.everyone is False
        await env.gamenight(note="<@&123> @everyone")
        [post] = env.guild.overwatch.sent
        assert [r.id for r in post["allowed_mentions"].roles] == [env.guild.overwatch_role.id]
    with_env(go, monkeypatch)


@pytest.mark.parametrize("when, word", [
    ("whenever", "examples"), ("tonight 9am", "past"), ("2026-12-25 20:00", "30 days"),
    ("<@&123> 9pm", "examples"),
])
def test_gamenight_bad_when(monkeypatch, when, word):
    async def go(env):
        i = await env.gamenight(when=when)
        [msg] = i.of("send_message")
        assert msg["ephemeral"] is True
        assert word in msg["content"]
        assert "tomorrow 8pm" in msg["content"]
        assert env.guild.events == {}
        assert await env.rows("SELECT * FROM gamenights") == []
        assert env.guild.overwatch.sent == []
    with_env(go, monkeypatch)


def test_gamenight_without_create_events(monkeypatch, caplog):
    async def go(env):
        env.guild.forbidden = True
        i = await env.gamenight()
        assert any("Create Events" in t for t in i.texts())
        assert await env.rows("SELECT * FROM gamenights") == []
        for _ in range(5):
            await env.gamenight()
        env.t += 31 * MIN
        await env.gamenight()
        warnings = [r for r in caplog.records if r.name == "cogs.events" and r.levelno == logging.WARNING]
        assert len(warnings) == 2  # one per half hour, not one per try
        assert all("Create Events" in r.getMessage() for r in warnings)
    with_env(go, monkeypatch)


def test_gamenight_announcement_failure_still_replies(monkeypatch):
    async def go(env):
        env.guild.overwatch.fail = True
        i = await env.gamenight()
        ev = next(iter(env.guild.events.values()))
        assert any(ev.url in t for t in i.texts())
        assert len(await env.rows("SELECT * FROM gamenights")) == 1
    with_env(go, monkeypatch)


def test_gamenight_missing_voice_channel(monkeypatch):
    async def go(env):
        env.guild.voice_channels = [env.guild.lobby]
        i = await env.gamenight(size=3)
        assert config.SQUAD_VOICE in i.of("send_message")[0]["content"]
        assert env.guild.events == {}
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- reminders
async def make_night(env, interested=(), when="9pm", game="overwatch"):
    await env.gamenight(game=game, when=when)
    ev = env.guild.events[env.guild.next_id]
    ev.interested = [FakeUser(u) for u in interested]
    env.guild.overwatch.sent.clear()
    env.guild.gaming.sent.clear()
    return ev


def test_reminder_window_and_once_only(monkeypatch):
    async def go(env):
        ev = await make_night(env, interested=[A, B, BOTUSER])
        ev.interested[-1].bot = True
        start = ts(2026, 10, 7, 21)
        env.t = start - 16 * MIN
        await env.cog.run_reminders()
        assert env.guild.overwatch.sent == []
        env.t = start - 14 * MIN
        await env.cog.run_reminders()
        [post] = env.guild.overwatch.sent
        assert "<@2>" in post["content"] and "<@3>" in post["content"] and "<@50>" not in post["content"]
        assert env.guild.squad.mention in post["content"]
        am = post["allowed_mentions"]
        assert sorted(u.id for u in am.users) == [A, B]
        assert am.roles is False and am.everyone is False
        assert (await env.rows("SELECT reminded FROM gamenights"))[0]["reminded"] == 1
        env.t += MIN
        await env.cog.run_reminders()
        assert len(env.guild.overwatch.sent) == 1
    with_env(go, monkeypatch)


def test_reminder_in_memory_guard_when_db_write_fails(monkeypatch):
    async def go(env):
        await make_night(env, interested=[A])
        env.t = ts(2026, 10, 7, 21) - 5 * MIN
        real = env.cog.mark_reminded
        calls = []

        async def flaky(eid):
            calls.append(eid)
            if len(calls) == 1:
                raise RuntimeError("db locked")
            await real(eid)
        monkeypatch.setattr(env.cog, "mark_reminded", flaky)
        await env.cog.run_reminders()  # sends, then the write fails (logged, not raised)
        await env.cog.run_reminders()  # guard: no second send, the write is retried
        assert len(env.guild.overwatch.sent) == 1
        assert (await env.rows("SELECT reminded FROM gamenights"))[0]["reminded"] == 1
    with_env(go, monkeypatch)


def test_reminder_send_failure_retries(monkeypatch):
    async def go(env):
        await make_night(env, interested=[A])
        env.t = ts(2026, 10, 7, 21) - 5 * MIN
        env.guild.overwatch.fail = True
        await env.cog.run_reminders()
        assert (await env.rows("SELECT reminded FROM gamenights"))[0]["reminded"] == 0
        env.guild.overwatch.fail = False
        env.t += MIN
        await env.cog.run_reminders()
        assert len(env.guild.overwatch.sent) == 1
    with_env(go, monkeypatch)


def test_reminder_nobody_interested_posts_without_pings(monkeypatch):
    async def go(env):
        await make_night(env, game=cogmod.ANYTHING)
        env.t = ts(2026, 10, 7, 21) - 10 * MIN
        await env.cog.run_reminders()
        [post] = env.guild.gaming.sent
        am = post["allowed_mentions"]
        assert am.users is False and am.roles is False and am.everyone is False
    with_env(go, monkeypatch)


def test_reminder_event_name_with_role_mention_cannot_ping(monkeypatch):
    async def go(env):
        ev = await make_night(env, interested=[A])
        ev.name = "<@&700> @everyone"
        env.t = ts(2026, 10, 7, 21) - 10 * MIN
        await env.cog.run_reminders()
        am = env.guild.overwatch.sent[0]["allowed_mentions"]
        assert am.roles is False and am.everyone is False and [u.id for u in am.users] == [A]
    with_env(go, monkeypatch)


def test_reminder_pings_game_night_role_when_it_exists(monkeypatch):
    async def go(env):
        gn = FakeRole(710, config.GAMENIGHT_ROLE)
        env.guild.roles.append(gn)
        ev = await make_night(env, interested=[A])
        ev.name = "<@&700> @everyone"
        env.t = ts(2026, 10, 7, 21) - 10 * MIN
        await env.cog.run_reminders()
        [post] = env.guild.overwatch.sent
        assert post["content"].startswith(gn.mention)
        am = post["allowed_mentions"]
        assert [r.id for r in am.roles] == [gn.id]  # never the game role named in the title
        assert [u.id for u in am.users] == [A] and am.everyone is False
    with_env(go, monkeypatch)


def test_reminder_cancelled_and_deleted_events(monkeypatch):
    async def go(env):
        ev1 = await make_night(env, interested=[A])
        ev2 = await make_night(env, interested=[A], when="9:10pm")
        ev1.status = discord.EventStatus.cancelled
        del env.guild.events[ev2.id]
        env.guild.cached.discard(ev2.id)
        env.t = ts(2026, 10, 7, 21) - 2 * MIN  # both inside their reminder windows
        await env.cog.run_reminders()
        assert env.guild.overwatch.sent == []
        rows = await env.rows("SELECT reminded FROM gamenights")
        assert [r["reminded"] for r in rows] == [1, 1]
    with_env(go, monkeypatch)


def test_reminder_too_late_is_marked_not_sent(monkeypatch):
    async def go(env):
        await make_night(env, interested=[A])
        env.t = ts(2026, 10, 7, 21) + 11 * MIN
        await env.cog.run_reminders()
        assert env.guild.overwatch.sent == []
        assert (await env.rows("SELECT reminded FROM gamenights"))[0]["reminded"] == 1
    with_env(go, monkeypatch)


def test_reminder_follows_rescheduled_event(monkeypatch):
    async def go(env):
        ev = await make_night(env, interested=[A])
        ev.start_time = datetime.fromtimestamp(ts(2026, 10, 7, 22), timezone.utc)
        env.t = ts(2026, 10, 7, 21) - 5 * MIN
        await env.cog.run_reminders()
        assert env.guild.overwatch.sent == []  # moved to 10pm: not yet
        assert (await env.rows("SELECT starts_at FROM gamenights"))[0]["starts_at"] == ts(2026, 10, 7, 22)
        env.t = ts(2026, 10, 7, 22) - 5 * MIN
        await env.cog.run_reminders()
        assert len(env.guild.overwatch.sent) == 1
    with_env(go, monkeypatch)


def test_reminder_uncached_far_event_is_not_fetched(monkeypatch):
    async def go(env):
        ev = await make_night(env, interested=[A])
        env.guild.cached.discard(ev.id)
        env.t = ts(2026, 10, 7, 13)
        await env.cog.run_reminders()
        assert env.guild.fetches == 0
        env.t = ts(2026, 10, 7, 21) - 5 * MIN
        await env.cog.run_reminders()
        assert env.guild.fetches == 1 and len(env.guild.overwatch.sent) == 1
    with_env(go, monkeypatch)


def test_reminder_loop_never_raises(monkeypatch):
    async def go(env):
        async def boom():
            raise RuntimeError("x")
        monkeypatch.setattr(env.cog, "run_reminders", boom)
        await env.cog.reminders.coro(env.cog)
        monkeypatch.setattr(env.cog, "run_weekly", boom)
        await env.cog.weekly.coro(env.cog)
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- free games
def item(i, **kw):
    base = dict(id=i, title=f"Game {i}", worth="$19.99", platforms="PC, Steam",
                end_date="2026-10-15 23:59:00", status="Active", type="Game",
                open_giveaway_url=f"https://www.gamerpower.com/open/game-{i}")
    base.update(kw)
    return base


THU = ts(2026, 10, 8, 18)  # Thursday 18:00, the scheduled time
KEY = occurrence(FREE_GAMES_JOB, date(2026, 10, 8), TZ).key


async def seen_first(env):
    """First startup on Wednesday: marks the previous Thursday done, runs nothing."""
    env.t = T0
    await env.cog.run_weekly()


def test_weekly_first_run_marks_done_without_posting(monkeypatch):
    async def go(env):
        await seen_first(env)
        assert env.feed.urls == []
        assert await env.jobs() == {occurrence(FREE_GAMES_JOB, date(2026, 10, 1), TZ).key}
        assert env.guild.gaming.sent == []
    with_env(go, monkeypatch, feed=FakeFeed([item(1)]))


def test_free_games_posts_filters_and_dedupes(monkeypatch):
    feed = FakeFeed([item(1), item(2, status="Expired"), item(3, open_giveaway_url="javascript:x"),
                     item(4)])

    async def go(env):
        await seen_first(env)
        await env.db.execute("INSERT INTO free_games (id, posted_at) VALUES (4, 0)")
        env.t = THU + MIN
        await env.cog.run_weekly()
        assert env.feed.urls == [FREE_GAMES_URL]
        [post] = env.guild.gaming.sent
        e = post["embed"]
        assert e.title == "Free games this week"
        assert "Game 1" in e.description
        assert "Game 2" not in e.description and "Game 3" not in e.description and "Game 4" not in e.description
        assert "javascript" not in e.description
        am = post["allowed_mentions"]
        assert am.everyone is False and am.roles is False and am.users is False
        assert {r["id"] for r in await env.rows("SELECT id FROM free_games")} == {1, 4}
        assert KEY in await env.jobs()
        env.t += 10 * MIN
        await env.cog.run_weekly()  # done this week: no second fetch
        assert len(env.feed.urls) == 1
    with_env(go, monkeypatch, feed=feed)


def test_free_games_caps_at_eight(monkeypatch):
    async def go(env):
        await seen_first(env)
        env.t = THU + MIN
        await env.cog.run_weekly()
        [post] = env.guild.gaming.sent
        assert post["embed"].description.count("gamerpower.com/open/") == 8
        posted = {r["id"] for r in await env.rows("SELECT id FROM free_games")}
        assert posted == set(range(1, 9))  # the rest can show next week
    with_env(go, monkeypatch, feed=FakeFeed([item(i) for i in range(1, 13)]))


def test_free_games_empty_marks_done_posts_nothing(monkeypatch):
    async def go(env):
        await seen_first(env)
        env.t = THU + MIN
        await env.cog.run_weekly()
        assert env.guild.gaming.sent == []
        assert KEY in await env.jobs()
    with_env(go, monkeypatch, feed=FakeFeed({"status": 0, "status_message": "No active giveaways"}))


def test_free_games_all_seen_marks_done_posts_nothing(monkeypatch):
    async def go(env):
        await seen_first(env)
        await env.db.execute("INSERT INTO free_games (id, posted_at) VALUES (1, 0)")
        env.t = THU + MIN
        await env.cog.run_weekly()
        assert env.guild.gaming.sent == []
        assert KEY in await env.jobs()
    with_env(go, monkeypatch, feed=FakeFeed([item(1)]))


def test_free_games_uses_the_filter_endpoint(monkeypatch):
    # /api/giveaways answers 404 for a platform list; /api/filter takes one.
    assert cogmod.FREE_GAMES_URL == ("https://www.gamerpower.com/api/filter"
                                     "?platform=epic-games-store.steam&type=game")
    assert cogmod.E.safe_url(cogmod.FREE_GAMES_URL) is not None
    feed = FakeFeed([item(1)])

    async def go(env):
        await seen_first(env)
        env.t = THU + MIN
        await env.cog.run_weekly()
        assert feed.urls == [cogmod.FREE_GAMES_URL]
    with_env(go, monkeypatch, feed=feed)


@pytest.mark.parametrize("body", [{"status": 0, "status_message": "No active giveaways available at the moment"},
                                  {"weird": 1}, "html page", None])
def test_free_games_non_list_body_means_none_this_week(monkeypatch, caplog, body):
    async def go(env):
        await seen_first(env)
        env.t = THU + MIN
        await env.cog.run_weekly()
        assert KEY in await env.jobs() and env.guild.gaming.sent == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    with_env(go, monkeypatch, feed=FakeFeed(body))


def test_free_games_next_week_runs_after_a_week_was_given_up(monkeypatch):
    feed = FakeFeed(TimeoutError("404"))

    async def go(env):
        await seen_first(env)
        env.t = THU + MIN
        await env.cog.run_weekly()
        env.t = THU + 24 * HOUR + MIN
        await env.cog.run_weekly()
        assert KEY in await env.jobs() and env.guild.gaming.sent == []  # gave up on that week
        feed.results = [[item(9)]]
        env.t = THU + 6 * 24 * HOUR
        await env.cog.run_weekly()
        assert env.guild.gaming.sent == []  # not due again yet
        env.t = THU + 7 * 24 * HOUR + MIN
        await env.cog.run_weekly()
        assert len(env.guild.gaming.sent) == 1
        assert len(await env.jobs()) == 3  # first sighting, the week given up, this week
    with_env(go, monkeypatch, feed=feed)


@pytest.mark.parametrize("failure", [
    TimeoutError("slow"), ValueError("bad json"),
])
def test_free_games_api_failure_retries_then_succeeds(monkeypatch, failure):
    feed = FakeFeed(failure, [item(7)])

    async def go(env):
        await seen_first(env)
        env.t = THU + MIN
        await env.cog.run_weekly()
        assert env.guild.gaming.sent == []
        assert KEY not in await env.jobs()
        env.t += 5 * MIN
        await env.cog.run_weekly()
        assert len(env.guild.gaming.sent) == 1
        assert KEY in await env.jobs()
    with_env(go, monkeypatch, feed=feed)


def test_free_games_send_failure_retries(monkeypatch):
    async def go(env):
        await seen_first(env)
        env.guild.gaming.fail = True
        env.t = THU + MIN
        await env.cog.run_weekly()
        assert KEY not in await env.jobs()
        assert await env.rows("SELECT id FROM free_games") == []
        env.guild.gaming.fail = False
        env.t += 5 * MIN
        await env.cog.run_weekly()
        assert len(env.guild.gaming.sent) == 1 and KEY in await env.jobs()
    with_env(go, monkeypatch, feed=FakeFeed([item(1)]))


def test_free_games_gives_up_after_a_day(monkeypatch):
    async def go(env):
        await seen_first(env)
        env.t = THU + 23 * HOUR
        await env.cog.run_weekly()
        assert KEY not in await env.jobs()
        env.t = THU + 24 * HOUR + MIN
        await env.cog.run_weekly()
        assert KEY in await env.jobs()
        assert env.guild.gaming.sent == []
    with_env(go, monkeypatch, feed=FakeFeed(TimeoutError("down")))


def test_http_fetch_refuses_non_http_urls():
    with pytest.raises(ValueError):
        run(cogmod.http_get_json("file:///etc/passwd"))


def test_members_get_one_upcoming_gamenight_and_an_hourly_cooldown(monkeypatch):
    """Security: on a public server a member can't spam events (each one pings a role)."""
    async def go(env):
        await env.gamenight(uid=USER_A, staff=False)
        second = await env.gamenight(game="minecraft", uid=USER_A, staff=False)
        assert "just made a game night" in second.texts()[0]
        env.cog.last_created.clear()  # past the cooldown, but the first night is still upcoming
        third = await env.gamenight(game="minecraft", uid=USER_A, staff=False)
        assert "already have a game night" in third.texts()[0]
        rows = await env.rows("SELECT * FROM gamenights WHERE host_id = ?", (USER_A,))
        assert len(rows) == 1
        # Staff aren't limited.
        await env.gamenight(game="minecraft", uid=USER_A + 1, staff=True)
        await env.gamenight(game="wardogs", uid=USER_A + 1, staff=True)
        assert len(await env.rows("SELECT * FROM gamenights WHERE host_id = ?", (USER_A + 1,))) == 2
    with_env(go, monkeypatch)


def test_concurrent_gamenights_from_one_member_create_only_one(monkeypatch):
    """Security: two simultaneous /gamenight submits must not both pass the limits."""
    async def go(env):
        await asyncio.gather(
            env.gamenight(uid=USER_A, staff=False),
            env.gamenight(game="minecraft", uid=USER_A, staff=False),
        )
        rows = await env.rows("SELECT * FROM gamenights WHERE host_id = ?", (USER_A,))
        assert len(rows) == 1
    with_env(go, monkeypatch)


def test_refused_request_gives_the_cooldown_back(monkeypatch):
    async def go(env):
        await env.gamenight(uid=USER_A, staff=False)
        env.cog.last_created.clear()
        await env.gamenight(game="minecraft", uid=USER_A, staff=False)  # refused: one already upcoming
        assert USER_A not in env.cog.last_created
    with_env(go, monkeypatch)


@pytest.mark.parametrize("how", ["deleted", "cancelled"])
def test_cancelled_or_deleted_night_frees_the_members_slot(monkeypatch, how):
    """A member whose far-off night is gone in Discord can schedule another right away."""
    async def go(env):
        await env.gamenight(when="2026-10-27 20:00", uid=USER_A, staff=False)
        ev = env.guild.events[env.guild.next_id]
        if how == "deleted":
            del env.guild.events[ev.id]
            env.guild.cached.discard(ev.id)
        else:
            ev.status = discord.EventStatus.cancelled
        env.cog.last_created.clear()  # past the cooldown
        i = await env.gamenight(game="minecraft", uid=USER_A, staff=False)
        assert not any("already have a game night" in t for t in i.texts())
        rows = await env.rows("SELECT event_id, reminded FROM gamenights WHERE host_id = ? ORDER BY event_id",
                              (USER_A,))
        assert rows == [dict(event_id=ev.id, reminded=1), dict(event_id=ev.id + 1, reminded=0)]
    with_env(go, monkeypatch)


def test_uncached_live_night_still_counts(monkeypatch):
    async def go(env):
        await env.gamenight(when="2026-10-27 20:00", uid=USER_A, staff=False)
        env.guild.cached.clear()  # alive in Discord, just not in the cache
        env.cog.last_created.clear()
        i = await env.gamenight(game="minecraft", uid=USER_A, staff=False)
        assert "already have a game night" in i.texts()[0]
        assert env.guild.fetches == 1
    with_env(go, monkeypatch)


@pytest.mark.parametrize("status", [400, 503])
def test_discord_error_creating_event_replies_and_gives_the_cooldown_back(monkeypatch, status):
    async def go(env):
        env.guild.create_error = discord.HTTPException(SimpleNamespace(status=status, reason="nope"), "nope")
        i = await env.gamenight(uid=USER_A, staff=False)
        [msg] = i.of("followup")
        assert msg["ephemeral"] is True and "try again" in msg["content"]
        assert USER_A not in env.cog.last_created
        assert await env.rows("SELECT * FROM gamenights") == []
        env.guild.create_error = None
        retry = await env.gamenight(uid=USER_A, staff=False)
        assert len(env.guild.events) == 1
        assert not any("just made a game night" in t for t in retry.texts())
    with_env(go, monkeypatch)


def test_gamenight_note_markdown_is_escaped(monkeypatch):
    async def go(env):
        await env.gamenight(note="[Claim Nitro](phish)")  # a real URL is refused by textfilter
        [post] = env.guild.overwatch.sent
        assert r"> \[Claim Nitro]" in post["content"]
        ev = next(iter(env.guild.events.values()))
        assert r"\[Claim Nitro]" in ev.kwargs["description"]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- textfilter
@pytest.mark.parametrize("note, reason", [
    ("join discord.gg/raid", "invite"), ("free nitro at https://phish.example", "link"), ("you c.u.n.t", "word"),
])
def test_gamenight_blocked_note_is_refused_before_anything_is_made(monkeypatch, caplog, note, reason):
    from logic import textfilter
    caplog.set_level("INFO", logger="logic.textfilter")

    async def go(env):
        i = await env.gamenight(note=note)
        (reply,) = i.of("send_message")
        assert reply["ephemeral"] is True and reply["content"] == textfilter.MESSAGES[reason]
        assert note not in reply["content"]
        assert not env.guild.events and not env.guild.overwatch.sent
        assert await env.rows("SELECT * FROM gamenights") == []
    with_env(go, monkeypatch)
    line = caplog.records[-1].getMessage()
    assert str(HOST) in line and reason in line and note not in line


# ---------------------------------------------------------------- /watchparty
ME = SimpleNamespace(id=BOTUSER)


class FakeStage(FakeVoice):
    def __init__(self, perms=None):
        super().__init__(102, config.STAGE_CHANNEL)
        self.perms = discord.Permissions.all() if perms is None else perms

    def permissions_for(self, member):
        assert member is ME
        return self.perms


def add_stage(env, perms=None):
    env.guild.me = ME
    env.guild.stage = FakeStage(perms)
    env.guild.stage_channels = [env.guild.stage]
    return env.guild.stage


async def watchparty(env, title="Dune Part Two", when="9pm", link=None, uid=HOST, staff=True):
    i = env.inter(uid, staff)
    await env.cog.watchparty.callback(env.cog, i, title, when, link)
    return i


def young_account(days=3):
    from logic import quests as Q
    return ((T0 - days * 86400) * 1000 - Q.DISCORD_EPOCH_MS) << 22


def test_watchparty_creates_a_stage_event_row_and_announcement(monkeypatch):
    async def go(env):
        stage = add_stage(env)
        i = await watchparty(env, link="https://www.youtube.com/watch?v=abc123")
        [ev] = env.guild.events.values()
        kw = ev.kwargs
        assert kw["name"] == "Watch party: Dune Part Two" and kw["channel"] is stage
        assert kw["entity_type"] is discord.EntityType.stage_instance
        assert kw["privacy_level"] is discord.PrivacyLevel.guild_only
        assert kw["start_time"] == datetime.fromtimestamp(ts(2026, 10, 7, 21), timezone.utc)
        assert kw["description"] == "Hosted by user1. Watching: https://www.youtube.com/watch?v=abc123"
        assert await env.rows("SELECT * FROM hosted_events") == [dict(
            event_id=ev.id, host_id=HOST, kind="watchparty", starts_at=ts(2026, 10, 7, 21), reminded=0)]
        assert await env.rows("SELECT * FROM gamenights") == []  # game-night stats never count it
        [post] = env.guild.gaming.sent  # announced where "Anything" game nights are
        text = post["content"]
        assert text.startswith("**Watch party: Dune Part Two** <t:")
        assert stage.mention in text and "hosted by <@1>" in text and ev.url in text
        assert "Watching: https://www.youtube.com/watch?v=abc123" in text  # a known host may embed
        am = post["allowed_mentions"]
        assert am.roles is False and am.users is False and am.everyone is False  # nobody is pinged
        assert "/predict" not in text  # the games module isn't loaded here
        [reply] = i.of("followup")
        assert reply["ephemeral"] is True and ev.url in reply["content"] and stage.mention in reply["content"]
    with_env(go, monkeypatch)


def test_watchparty_unknown_host_link_is_shown_without_an_embed(monkeypatch):
    async def go(env):
        add_stage(env)
        await watchparty(env, link="https://example.org/stream?x=1")
        [post] = env.guild.gaming.sent
        assert "Watching: <https://example.org/stream?x=1>" in post["content"]
    with_env(go, monkeypatch)


def test_watchparty_mentions_predictions_when_the_games_module_is_loaded(monkeypatch):
    async def go(env):
        add_stage(env)
        env.bot.get_cog = lambda name: object() if name == "Games" else None
        await watchparty(env)
        assert "`/predict create`" in env.guild.gaming.sent[0]["content"]
        env.bot.get_cog = lambda name: None
        await watchparty(env, title="Finals", when="tomorrow 8pm")
        assert "/predict" not in env.guild.gaming.sent[1]["content"]
    with_env(go, monkeypatch)


@pytest.mark.parametrize("stage_perms", [None, discord.Permissions(view_channel=True, manage_channels=True,
                                                                  move_members=True)])
def test_watchparty_falls_back_to_the_lobby_voice_channel(monkeypatch, stage_perms):
    """No stage channel, or one the bot may not schedule on (Mute Members missing)."""
    async def go(env):
        if stage_perms is not None:
            add_stage(env, stage_perms)
        await watchparty(env)
        [ev] = env.guild.events.values()
        assert ev.kwargs["channel"] is env.guild.lobby and ev.kwargs["entity_type"] is discord.EntityType.voice
        assert env.guild.lobby.mention in env.guild.gaming.sent[0]["content"]
    with_env(go, monkeypatch)


def test_watchparty_with_nowhere_to_host(monkeypatch):
    async def go(env):
        env.guild.voice_channels = []
        i = await watchparty(env)
        assert "nowhere to host" in i.texts()[0] and not env.guild.events
    with_env(go, monkeypatch)


@pytest.mark.parametrize("link", [
    "http://example.org/stream", "javascript:alert(1)", "example.org", "https://example.org/a b",
    "https://user:pw@example.org/x", "[click](https://example.org)",
])
def test_watchparty_refuses_links_that_are_not_plain_https(monkeypatch, link):
    async def go(env):
        add_stage(env)
        i = await watchparty(env, link=link)
        (reply,) = i.of("send_message")
        assert reply["ephemeral"] is True and reply["content"] == cogmod.BAD_LINK
        assert not env.guild.events and not env.guild.gaming.sent
        assert await env.rows("SELECT * FROM hosted_events") == []
    with_env(go, monkeypatch)


@pytest.mark.parametrize("title, link, reason", [
    ("join discord.gg/raid", None, "invite"),
    ("free nitro at https://phish.example", None, "link"),  # the title may not carry a link
    ("watch at phish.com", None, "link"),
    ("you c.u.n.t", None, "word"),
    ("Movie night", "https://discord.gg/raid", "invite"),  # the link field may, but never an invite
])
def test_watchparty_blocked_text_is_refused_before_anything_is_made(monkeypatch, title, link, reason):
    from logic import textfilter

    async def go(env):
        add_stage(env)
        i = await watchparty(env, title=title, link=link)
        (reply,) = i.of("send_message")
        assert reply["ephemeral"] is True and reply["content"] == textfilter.MESSAGES[reason]
        assert not env.guild.events and not env.guild.gaming.sent
    with_env(go, monkeypatch)


def test_watchparty_title_cannot_ping_or_format(monkeypatch):
    async def go(env):
        add_stage(env)
        await watchparty(env, title="**big** <@&700> @everyone")
        [post] = env.guild.gaming.sent
        assert r"\*\*big\*\*" in post["content"]
        am = post["allowed_mentions"]
        assert am.roles is False and am.users is False and am.everyone is False
    with_env(go, monkeypatch)


@pytest.mark.parametrize("when, word", [("soonish", "couldn't read"), ("today 9am", "in the past"),
                                        ("2027-01-01 20:00", "more than 30 days")])
def test_watchparty_bad_when(monkeypatch, when, word):
    async def go(env):
        add_stage(env)
        i = await watchparty(env, when=when)
        assert word in i.texts()[0] and not env.guild.events
    with_env(go, monkeypatch)


def test_watchparty_shares_the_game_night_limits_and_needs_an_established_account(monkeypatch):
    """Security: the same anti-spam limits as /gamenight, counted together, plus an age floor."""
    async def go(env):
        add_stage(env)
        alt = young_account()
        refused = await watchparty(env, uid=alt, staff=False)
        assert refused.texts()[0] == cogmod.TOO_NEW and not env.guild.events
        assert alt not in env.cog.last_created

        await watchparty(env, uid=USER_A, staff=False)
        assert len(env.guild.events) == 1
        second = await watchparty(env, title="Another", uid=USER_A, staff=False)
        assert "just scheduled an event" in second.texts()[0]
        night = await env.gamenight(uid=USER_A, staff=False)  # the cooldown covers both commands
        assert "just made a game night" in night.texts()[0]
        env.cog.last_created.clear()
        third = await watchparty(env, title="Another", uid=USER_A, staff=False)
        assert "already have an event coming up" in third.texts()[0]
        assert USER_A not in env.cog.last_created  # the refused request gave the cooldown back
        night = await env.gamenight(uid=USER_A, staff=False)  # ...and the upcoming limit does too
        assert "already have a game night" in night.texts()[0]
        assert len(env.guild.events) == 1

        # A game night blocks a watch party the same way.
        await env.gamenight(uid=USER_A + 1, staff=False)
        env.cog.last_created.clear()
        blocked = await watchparty(env, uid=USER_A + 1, staff=False)
        assert "already have an event coming up" in blocked.texts()[0]
        # Staff aren't limited, whatever the account's age.
        await watchparty(env, uid=alt, staff=True)
        await watchparty(env, title="Two", when="tomorrow 8pm", uid=alt, staff=True)
        assert len(await env.rows("SELECT * FROM hosted_events WHERE host_id = ?", (alt,))) == 2
    with_env(go, monkeypatch)


def test_concurrent_watchparties_from_one_member_create_only_one(monkeypatch):
    async def go(env):
        add_stage(env)
        await asyncio.gather(watchparty(env, uid=USER_A, staff=False),
                             watchparty(env, title="Other", uid=USER_A, staff=False),
                             env.gamenight(uid=USER_A, staff=False))
        made = (await env.rows("SELECT event_id FROM gamenights WHERE host_id = ?", (USER_A,))
                + await env.rows("SELECT event_id FROM hosted_events WHERE host_id = ?", (USER_A,)))
        assert len(made) == 1 and len(env.guild.events) == 1
    with_env(go, monkeypatch)


@pytest.mark.parametrize("status", [403, 500])
def test_watchparty_discord_error_replies_and_gives_the_cooldown_back(monkeypatch, status):
    async def go(env):
        add_stage(env)
        if status == 403:
            env.guild.forbidden = True
        else:
            env.guild.create_error = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
        i = await watchparty(env, uid=USER_A, staff=False)
        [reply] = i.of("followup")
        assert ("Create Events" in reply["content"]) == (status == 403)
        assert USER_A not in env.cog.last_created
        assert await env.rows("SELECT * FROM hosted_events") == [] and not env.guild.gaming.sent
    with_env(go, monkeypatch)


def test_a_members_watchparty_reminder_pings_interested_people_but_not_the_role(monkeypatch):
    """The title is the host's own words and any member can make one: it must not be a way to
    ping everyone with the Game Night role."""
    async def go(env):
        add_stage(env)
        gn = FakeRole(710, config.GAMENIGHT_ROLE)
        env.guild.roles.append(gn)

        async def reminder(**who):
            await watchparty(env, title="free nitro at my place", **who)
            ev = list(env.guild.events.values())[-1]
            ev.interested = [FakeUser(A)]
            env.guild.gaming.sent.clear()
            env.t = ts(2026, 10, 7, 21) - 10 * MIN
            await env.cog.run_reminders()
            [post] = env.guild.gaming.sent
            return post
        post = await reminder(uid=USER_A, staff=False)
        assert gn.mention not in post["content"] and "Watch party: free nitro at my place" in post["content"]
        assert not post["allowed_mentions"].roles and post["allowed_mentions"].everyone is False
        assert [u.id for u in post["allowed_mentions"].users] == [A]  # Interested people still hear about it
        # a host who has left by then is nobody's staff either
        env.t = T0
        await env.db.execute("DELETE FROM hosted_events")
        env.cog.last_created.clear()
        env.cog.reminded.clear()
        env.guild.events.clear()
        late = FakeEvent(9001, env.guild, name="Watch party: gone", start_time=datetime.fromtimestamp(
            ts(2026, 10, 7, 21), timezone.utc), channel=env.guild.lobby)
        env.guild.events[late.id] = late
        env.guild.cached.add(late.id)
        await env.db.execute("INSERT INTO hosted_events (event_id, host_id, kind, starts_at) VALUES (9001, 4242,"
                             " 'watchparty', ?)", (ts(2026, 10, 7, 21),))
        env.guild.gaming.sent.clear()
        env.t = ts(2026, 10, 7, 21) - 10 * MIN
        await env.cog.run_reminders()
        [post] = env.guild.gaming.sent
        assert gn.mention not in post["content"] and not post["allowed_mentions"].roles
    with_env(go, monkeypatch)


def test_a_staff_watchparty_reminder_pings_interested_and_the_game_night_role(monkeypatch):
    async def go(env):
        add_stage(env)
        gn = FakeRole(710, config.GAMENIGHT_ROLE)
        env.guild.roles.append(gn)
        await watchparty(env)
        [ev] = env.guild.events.values()
        ev.interested = [FakeUser(A)]
        env.guild.gaming.sent.clear()
        env.t = ts(2026, 10, 7, 21) - 10 * MIN
        await env.cog.run_reminders()
        await env.cog.run_reminders()
        [post] = env.guild.gaming.sent
        assert post["content"].startswith(gn.mention) and "Watch party: Dune Part Two" in post["content"]
        assert [r.id for r in post["allowed_mentions"].roles] == [gn.id]
        assert [u.id for u in post["allowed_mentions"].users] == [A]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- Town Hall
SAT_BEFORE = ts(2026, 10, 31, 12)  # the Saturday a week before November's first Saturday
TOWNHALL_AT = ts(2026, 11, 7, 12)


def townhall_guild(env, perms=None):
    add_stage(env, perms)
    env.bot.user = ME
    env.guild.announcements = FakeText(302, config.ANNOUNCEMENTS_CHANNEL)
    env.guild.text_channels.append(env.guild.announcements)
    env.guild.forums = [SimpleNamespace(name=config.SUGGESTIONS_FORUM, mention="<#400>")]
    env.cog.townhall_job = cogmod.Weekly("townhall", weekday=5, hour=12, minute=0)


async def townhall_seen(env):
    """The bot's first run just records that it has seen the job; the next Saturday is the first real one."""
    await env.cog.run_townhall()
    assert not env.guild.events


def townhall_rows(env):
    return env.rows("SELECT * FROM hosted_events WHERE kind = 'townhall'")


def test_townhall_is_created_a_week_ahead_once_and_announced_once(monkeypatch):
    async def go(env):
        townhall_guild(env)
        await townhall_seen(env)
        for day in (10, 17, 24):  # these Saturdays are not a week before a first Saturday
            env.t = ts(2026, 10, day, 12, 1)
            await env.cog.run_townhall()
        assert not env.guild.events and not env.guild.announcements.sent
        env.t = SAT_BEFORE - 60
        await env.cog.run_townhall()
        assert not env.guild.events
        env.t = SAT_BEFORE + 60
        await env.cog.run_townhall()
        [ev] = env.guild.events.values()
        kw = ev.kwargs
        assert kw["name"] == "Town Hall" and kw["channel"] is env.guild.stage
        assert kw["entity_type"] is discord.EntityType.stage_instance
        assert kw["start_time"] == datetime.fromtimestamp(TOWNHALL_AT, timezone.utc)
        assert await townhall_rows(env) == [dict(event_id=ev.id, host_id=BOTUSER, kind="townhall", starts_at=TOWNHALL_AT,
                                                 reminded=0)]
        [post] = env.guild.announcements.sent
        embed = post["embed"]
        assert embed.title.startswith("Town Hall") and "Agenda" in embed.description
        assert "<#400>" in embed.description and ev.url in embed.description and "<#102>" in embed.description
        assert embed.footer.text == "TOWN HALL · NOVEMBER 2026"
        am = post["allowed_mentions"]
        assert not am.roles and not am.users and am.everyone is False
        # Every later tick that week, and the weeks after: nothing more.
        for later in (SAT_BEFORE + 600, SAT_BEFORE + 3 * 86400, ts(2026, 11, 7, 12, 5), ts(2026, 11, 14, 12, 5)):
            env.t = later
            await env.cog.run_townhall()
        assert len(env.guild.events) == 1 and len(env.guild.announcements.sent) == 1
        # December's is scheduled on November 28.
        env.t = ts(2026, 11, 28, 12, 5)
        await env.cog.run_townhall()
        assert len(env.guild.events) == 2
        assert (await townhall_rows(env))[1]["starts_at"] == ts(2026, 12, 5, 12)
    with_env(go, monkeypatch)


def test_townhall_catches_up_after_downtime_but_never_schedules_in_the_past(monkeypatch):
    async def go(env):
        townhall_guild(env)
        await townhall_seen(env)
        env.t = ts(2026, 11, 3, 9)  # off since before the 31st, back on Tuesday: still worth creating
        await env.cog.run_townhall()
        assert len(env.guild.events) == 1 and (await townhall_rows(env))[0]["starts_at"] == TOWNHALL_AT
        # Off through all of late November: the Dec 5 Town Hall's own week is over, so it is skipped.
        env.t = ts(2026, 12, 5, 12, 30)
        await env.cog.run_townhall()
        assert len(env.guild.events) == 1
    with_env(go, monkeypatch)


def test_townhall_is_not_created_twice_when_the_job_row_was_lost(monkeypatch):
    async def go(env):
        townhall_guild(env)
        await townhall_seen(env)
        env.t = SAT_BEFORE + 60
        await env.cog.run_townhall()
        await env.db.execute("DELETE FROM jobs WHERE key LIKE 'townhall:%'")  # as if the write after it failed
        await env.cog.run_townhall()
        assert len(env.guild.events) == 1 and len(env.guild.announcements.sent) == 1
    with_env(go, monkeypatch)


@pytest.mark.parametrize("case, word", [
    ("no stage", "no 🎙・stage channel"),
    ("no permission", "missing Mute Members"),
    ("forbidden", "Create Events"),
])
def test_townhall_is_skipped_cleanly_with_the_reason_logged_once(monkeypatch, caplog, case, word):
    async def go(env):
        perms = discord.Permissions(view_channel=True, manage_channels=True, move_members=True)
        townhall_guild(env, perms if case == "no permission" else None)
        if case == "no stage":
            env.guild.stage_channels = []
        if case == "forbidden":
            env.guild.forbidden = True
        await townhall_seen(env)
        for minute in (1, 6, 11):  # three ticks: one warning, not three
            env.t = SAT_BEFORE + minute * MIN
            await env.cog.run_townhall()
        assert not env.guild.events and not env.guild.announcements.sent
        assert await townhall_rows(env) == []
    with caplog.at_level(logging.WARNING, logger="cogs.events"):
        with_env(go, monkeypatch)
    warnings = [r.getMessage() for r in caplog.records if "town hall" in r.getMessage()]
    assert len(warnings) == 1 and "skipped" in warnings[0] and word in warnings[0]


def test_townhall_discord_error_retries_then_gives_up_after_a_day(monkeypatch):
    async def go(env):
        townhall_guild(env)
        await townhall_seen(env)
        env.guild.create_error = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
        env.t = SAT_BEFORE + 60
        await env.cog.run_townhall()
        assert not env.guild.events and not any(k.startswith("townhall:2026-W44") for k in await env.jobs())
        env.guild.create_error = None
        env.t += 300
        await env.cog.run_townhall()
        assert len(env.guild.events) == 1

        # The next month Discord stays down for a day: that month is dropped, with no retry storm after.
        env.guild.create_error = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "x")
        env.t = ts(2026, 11, 28, 12, 5)
        await env.cog.run_townhall()
        env.t = ts(2026, 11, 29, 12, 5)
        await env.cog.run_townhall()
        env.guild.create_error = None
        env.t += 300
        await env.cog.run_townhall()
        assert len(env.guild.events) == 1
    with_env(go, monkeypatch)


def test_townhall_announcement_failure_keeps_the_event(monkeypatch):
    async def go(env):
        townhall_guild(env)
        env.guild.announcements.fail = True
        await townhall_seen(env)
        env.t = SAT_BEFORE + 60
        await env.cog.run_townhall()
        await env.cog.run_townhall()
        assert len(env.guild.events) == 1 and len(await townhall_rows(env)) == 1
    with_env(go, monkeypatch)


def test_townhall_time_setting(monkeypatch, caplog):
    async def go(env):
        job = Events(env.bot, environ={}).townhall_job
        assert (job.name, job.weekday, job.hour, job.minute) == ("townhall", 5, 12, 0)
        job = Events(env.bot, environ={"TOWNHALL_TIME": "7:30pm"}).townhall_job
        assert (job.hour, job.minute) == (19, 30)
        townhall_guild(env)
        env.cog.townhall_job = Events(env.bot, environ={"TOWNHALL_TIME": "off"}).townhall_job
        assert env.cog.townhall_job is None
        env.t = SAT_BEFORE + 60
        await env.cog.run_townhall()  # off: no job rows, no event
        assert not env.guild.events and await env.jobs() == set()
        assert not caplog.records
        assert Events(env.bot, environ={"TOWNHALL_TIME": "lunchtime"}).townhall_job is None
        assert "TOWNHALL_TIME" in caplog.records[-1].getMessage()
    with caplog.at_level(logging.WARNING, logger="cogs.events"):
        with_env(go, monkeypatch)


def test_townhall_reminder_goes_to_announcements_without_the_game_night_ping(monkeypatch):
    async def go(env):
        townhall_guild(env)
        gn = FakeRole(710, config.GAMENIGHT_ROLE)
        env.guild.roles.append(gn)
        await townhall_seen(env)
        env.t = SAT_BEFORE + 60
        await env.cog.run_townhall()
        [ev] = env.guild.events.values()
        ev.interested = [FakeUser(A)]
        env.guild.announcements.sent.clear()
        env.t = TOWNHALL_AT - 10 * MIN
        await env.cog.run_reminders()
        [post] = env.guild.announcements.sent
        assert post["content"].startswith("**Town Hall** starts") and "<@2>" in post["content"]
        assert gn.mention not in post["content"] and post["allowed_mentions"].roles is False
        assert not env.guild.gaming.sent
    with_env(go, monkeypatch)


def test_weekly_loop_runs_both_jobs_and_never_raises(monkeypatch):
    async def go(env):
        ran = []

        async def boom():
            ran.append("free")
            raise RuntimeError("x")

        async def also():
            ran.append("townhall")
            raise RuntimeError("y")
        monkeypatch.setattr(env.cog, "run_weekly", boom)
        monkeypatch.setattr(env.cog, "run_townhall", also)
        await env.cog.weekly.coro(env.cog)
        assert ran == ["free", "townhall"]  # one job failing doesn't stop the other
    with_env(go, monkeypatch)
