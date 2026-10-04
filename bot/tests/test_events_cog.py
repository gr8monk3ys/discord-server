"""Offline tests for cogs.events.Events: a real in-memory SQLite database plus fakes for
the bot, guild, channels, roles, scheduled events and interactions. No network: the
GamerPower fetch is injected. Time is controlled by patching cogs.events.now."""

import asyncio
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
VALORANT = config.game_by_key("valorant")


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
        self.valorant = FakeText(301, VALORANT.channel_name)
        self.text_channels = [self.gaming, self.valorant]
        self.valorant_role = FakeRole(700, VALORANT.role)
        self.roles = [self.valorant_role]
        self.events: dict[int, FakeEvent] = {}
        self.cached: set[int] = set()  # event ids in the gateway cache
        self.next_id = 5000
        self.forbidden = False
        self.fetches = 0

    async def create_scheduled_event(self, **kw):
        if self.forbidden:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
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
        return FakeInteraction(user, self.guild)

    async def gamenight(self, game="valorant", when="9pm", size=None, note=None, uid=HOST, staff=True):
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
        assert kw["name"] == "Valorant game night"
        assert kw["entity_type"] is discord.EntityType.voice
        assert kw["channel"] is g.squad
        assert kw["privacy_level"] is discord.PrivacyLevel.guild_only
        assert kw["start_time"] == datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)
        assert kw["start_time"].tzinfo is not None
        assert "user1" in kw["description"] and "bring comms" in kw["description"]

        rows = await env.rows("SELECT * FROM gamenights")
        assert rows == [dict(event_id=ev.id, host_id=HOST, game="valorant",
                             starts_at=ts(2026, 10, 7, 21), reminded=0)]

        [post] = g.valorant.sent
        assert g.valorant_role.mention in post["content"]
        assert ev.url in post["content"]
        am = post["allowed_mentions"]
        assert am.everyone is False and am.users is False
        assert [r.id for r in am.roles] == [g.valorant_role.id]
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
        [post] = env.guild.valorant.sent
        assert [r.id for r in post["allowed_mentions"].roles] == [env.guild.valorant_role.id]
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
        assert env.guild.valorant.sent == []
    with_env(go, monkeypatch)


def test_gamenight_without_manage_events(monkeypatch):
    async def go(env):
        env.guild.forbidden = True
        i = await env.gamenight()
        assert any("Manage Events" in t for t in i.texts())
        assert await env.rows("SELECT * FROM gamenights") == []
    with_env(go, monkeypatch)


def test_gamenight_announcement_failure_still_replies(monkeypatch):
    async def go(env):
        env.guild.valorant.fail = True
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
async def make_night(env, interested=(), when="9pm", game="valorant"):
    await env.gamenight(game=game, when=when)
    ev = env.guild.events[env.guild.next_id]
    ev.interested = [FakeUser(u) for u in interested]
    env.guild.valorant.sent.clear()
    env.guild.gaming.sent.clear()
    return ev


def test_reminder_window_and_once_only(monkeypatch):
    async def go(env):
        ev = await make_night(env, interested=[A, B, BOTUSER])
        ev.interested[-1].bot = True
        start = ts(2026, 10, 7, 21)
        env.t = start - 16 * MIN
        await env.cog.run_reminders()
        assert env.guild.valorant.sent == []
        env.t = start - 14 * MIN
        await env.cog.run_reminders()
        [post] = env.guild.valorant.sent
        assert "<@2>" in post["content"] and "<@3>" in post["content"] and "<@50>" not in post["content"]
        assert env.guild.squad.mention in post["content"]
        am = post["allowed_mentions"]
        assert sorted(u.id for u in am.users) == [A, B]
        assert am.roles is False and am.everyone is False
        assert (await env.rows("SELECT reminded FROM gamenights"))[0]["reminded"] == 1
        env.t += MIN
        await env.cog.run_reminders()
        assert len(env.guild.valorant.sent) == 1
    with_env(go, monkeypatch)


def test_reminder_in_memory_guard_when_db_write_fails(monkeypatch):
    async def go(env):
        ev = await make_night(env, interested=[A])
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
        assert len(env.guild.valorant.sent) == 1
        assert (await env.rows("SELECT reminded FROM gamenights"))[0]["reminded"] == 1
    with_env(go, monkeypatch)


def test_reminder_send_failure_retries(monkeypatch):
    async def go(env):
        await make_night(env, interested=[A])
        env.t = ts(2026, 10, 7, 21) - 5 * MIN
        env.guild.valorant.fail = True
        await env.cog.run_reminders()
        assert (await env.rows("SELECT reminded FROM gamenights"))[0]["reminded"] == 0
        env.guild.valorant.fail = False
        env.t += MIN
        await env.cog.run_reminders()
        assert len(env.guild.valorant.sent) == 1
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
        am = env.guild.valorant.sent[0]["allowed_mentions"]
        assert am.roles is False and am.everyone is False and [u.id for u in am.users] == [A]
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
        assert env.guild.valorant.sent == []
        rows = await env.rows("SELECT reminded FROM gamenights")
        assert [r["reminded"] for r in rows] == [1, 1]
    with_env(go, monkeypatch)


def test_reminder_too_late_is_marked_not_sent(monkeypatch):
    async def go(env):
        await make_night(env, interested=[A])
        env.t = ts(2026, 10, 7, 21) + 11 * MIN
        await env.cog.run_reminders()
        assert env.guild.valorant.sent == []
        assert (await env.rows("SELECT reminded FROM gamenights"))[0]["reminded"] == 1
    with_env(go, monkeypatch)


def test_reminder_follows_rescheduled_event(monkeypatch):
    async def go(env):
        ev = await make_night(env, interested=[A])
        ev.start_time = datetime.fromtimestamp(ts(2026, 10, 7, 22), timezone.utc)
        env.t = ts(2026, 10, 7, 21) - 5 * MIN
        await env.cog.run_reminders()
        assert env.guild.valorant.sent == []  # moved to 10pm: not yet
        assert (await env.rows("SELECT starts_at FROM gamenights"))[0]["starts_at"] == ts(2026, 10, 7, 22)
        env.t = ts(2026, 10, 7, 22) - 5 * MIN
        await env.cog.run_reminders()
        assert len(env.guild.valorant.sent) == 1
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
        assert env.guild.fetches == 1 and len(env.guild.valorant.sent) == 1
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


@pytest.mark.parametrize("failure", [
    TimeoutError("slow"), ValueError("bad json"), {"weird": 1}, "html page",
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
        await env.gamenight(game="roblox", uid=USER_A + 1, staff=True)
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
