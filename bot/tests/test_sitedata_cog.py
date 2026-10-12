"""Offline tests for cogs.sitedata: a real in-memory SQLite database, a fake guild, a temp file."""

import asyncio
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import site_schema as schema
from cogs import sitedata as cogmod
from cogs.sitedata import SiteData
from logic import sitedata as SD

T = 1_800_000_000
HOUR = 3600
DAY = 24 * HOUR
TZ = ZoneInfo("America/Los_Angeles")
BASE = 100_000_000_000_000_000  # user ids shaped like real ones, so a leak would be caught


def uid(n: int) -> int:
    return BASE + n


class Role:
    def __init__(self, name):
        self.name = name


class Channel:
    def __init__(self, public=True):
        self.public = public

    def permissions_for(self, _role):
        return SimpleNamespace(view_channel=self.public)


def event(name, start, status=discord.EventStatus.scheduled, channel=None):
    return SimpleNamespace(name=name, start_time=datetime.fromtimestamp(start, timezone.utc), status=status,
                           channel=channel)


def member(n, status=discord.Status.online, bot=False):
    return SimpleNamespace(id=uid(n), status=status, bot=bot, display_name=f"Member Name {n}")


class Guild:
    owner_id = uid(999)
    default_role = Role("@everyone")

    def __init__(self, members=(), events=()):
        self.members = list(members)
        self.member_count = len(self.members)
        self.scheduled_events = list(events)


class User:
    def __init__(self, n, roles=(), admin=False, guild=None):
        self.id = uid(n)
        self.roles = [Role(r) for r in roles]
        self.guild = guild if guild is not None else Guild()
        self.guild_permissions = SimpleNamespace(administrator=admin)


class Interaction:
    def __init__(self, user):
        self.user = user
        self.calls = []
        self.response = SimpleNamespace(send_message=self.send)

    async def send(self, content=None, **kwargs):
        self.calls.append(dict(content=content, **kwargs))


def with_cog(fn, monkeypatch, tmp_path, guild=None, events=(), enabled=True):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        bot = SimpleNamespace(db=db, settings=SimpleNamespace(guild_id=1, tz=TZ),
                              get_guild=lambda _id: guild)
        cog = SiteData(bot, path=tmp_path / "site" / "stats.json", events=events, enabled=enabled)
        monkeypatch.setattr(cogmod, "now", lambda: T)
        try:
            await fn(db, cog)
        finally:
            await db.close()
    asyncio.run(go())


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).date().isoformat()


async def seed_busy_week(db):
    """Five active members; member 5 opted out after the fact."""
    for n, count in ((1, 400), (2, 300), (3, 250), (4, 200), (5, 5000)):
        await db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, ?, ?)", (uid(n), day(T), count))
    await db.execute("INSERT INTO message_counts (user_id, day, count) VALUES (?, ?, ?)",
                     (uid(1), day(T - 10 * DAY), 9999))  # older than the week
    for n in (1, 2, 3, 5):
        await db.execute('INSERT INTO voice_sessions (user_id, channel_id, start, "end") VALUES (?, ?, ?, ?)',
                         (uid(n), uid(700), T - 5 * HOUR, T - HOUR))
    for n in (1, 2, 3):
        await db.execute('INSERT INTO game_sessions (user_id, game, start, "end") VALUES (?, ?, ?, ?)',
                         (uid(n), "VALORANT", T - 6 * HOUR, T - 2 * HOUR))
    for n in (1, 2):  # only two players: below the small-group limit
        await db.execute('INSERT INTO game_sessions (user_id, game, start, "end") VALUES (?, ?, ?, ?)',
                         (uid(n), "Minecraft", T - 30 * HOUR, T - 10 * HOUR))
    await db.execute('INSERT INTO game_sessions (user_id, game, start, "end") VALUES (?, ?, ?, ?)',
                     (uid(3), "Some Private Hobby Game", T - 30 * HOUR, T - 10 * HOUR))
    await db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid(5), T))
    await db.execute("INSERT INTO tournaments (name, game, size, status, created_by, created_at)"
                     " VALUES ('October Cup', 'Valorant', 16, 'signup', ?, ?)", (uid(1), T))
    await db.execute("INSERT INTO tournaments (name, game, size, status, created_by, created_at)"
                     " VALUES ('Old Cup', NULL, 16, 'done', ?, ?)", (uid(1), T))
    await db.execute("INSERT INTO mm_matches (game, mode, members, created_at) VALUES ('valorant', 'Casual', ?, ?)",
                     (f"{uid(1)},{uid(2)}", T - HOUR))
    await db.execute("INSERT INTO mm_matches (game, mode, members, created_at) VALUES ('valorant', 'Casual', ?, ?)",
                     (f"{uid(3)},{uid(4)}", T - 2 * HOUR))
    for i, n in enumerate((1, 2, 3, 3)):
        await db.execute("INSERT INTO starboard (message_id, channel_id, author_id, board_message_id, stars, at)"
                         " VALUES (?, ?, ?, ?, 5, ?)", (uid(800 + i), uid(700), uid(n), uid(900 + i), T))
    await db.execute("INSERT INTO starboard (message_id, channel_id, author_id, stars, at) VALUES (?, ?, ?, 1, ?)",
                     (uid(850), uid(700), uid(4), T))  # not on the board yet
    for n in (1, 2):
        await db.execute("INSERT INTO clips (message_id, user_id, url, posted_at) VALUES (?, ?, 'x', ?)",
                         (uid(600 + n), uid(n), T))
    for n in (1, 2, 3, 4):
        await db.execute("INSERT INTO achievements (user_id, key, at) VALUES (?, 'first', ?)", (uid(n), T))


def busy_guild():
    members = [member(1), member(2), member(3, discord.Status.idle), member(4, discord.Status.offline),
               member(5), member(50, bot=True)]
    events = [event("Valorant game night", T + DAY, channel=Channel()),
              event("October Cup", T + 3 * DAY),
              event("Mods only planning", T + 2 * DAY, channel=Channel(public=False)),
              event(f"party for <@{uid(4)}>", T + 2 * DAY),
              event("Game night", T - DAY, status=discord.EventStatus.completed)]
    return Guild(members, events)


def test_refresh_writes_aggregates_only(monkeypatch, tmp_path):
    async def go(db, cog):
        await seed_busy_week(db)
        doc = await cog.refresh()
        text = cog.path.read_text(encoding="utf-8")
        assert json.loads(text) == doc and schema.check_text(text) == (doc, [])
        assert not cog.path.with_name("stats.json.tmp").exists()
        assert doc["generated_at"] == schema.iso(T)
        assert doc["members"] == {"total": 6, "online": 4}
        assert doc["week"] == {"messages": 1150, "voice_hours": 12}  # member 5 opted out
        assert doc["games"] == [{"name": "Valorant", "hours": 12}]
        assert doc["events"] == [
            {"title": "Valorant game night", "kind": "gamenight", "game": "Valorant", "start": schema.iso(T + DAY)},
            {"title": "October Cup", "kind": "tournament", "game": "Valorant", "start": schema.iso(T + 3 * DAY)}]
        assert doc["tournaments"] == {"open": 1, "running": 0, "finished": 1}
        assert doc["matchmaking"] == {"matches_week": 2}
        assert doc["hall"] == {"starred": 4, "badges": 4}  # clips: only two people
        assert "houses" not in doc and "seasonal" not in doc
        # nothing ID-shaped, no member name, no mention anywhere in the file
        assert not re.search(r"\d{15,20}", text)
        assert "Member Name" not in text and "@" not in text
    with_cog(go, monkeypatch, tmp_path, guild=busy_guild())


def test_empty_database_and_no_guild_still_writes_a_valid_file(monkeypatch, tmp_path):
    async def go(db, cog):
        doc = await cog.refresh()
        assert doc == {"v": 1, "generated_at": schema.iso(T)}
        assert schema.check_text(cog.path.read_text(encoding="utf-8")) == (doc, [])
    with_cog(go, monkeypatch, tmp_path, guild=None)


def test_online_needs_the_member_cache(monkeypatch, tmp_path):
    async def go(db, cog):
        guild = Guild([member(50, bot=True)])
        guild.member_count = 15  # no members intent: only the bot is cached
        assert cog.presence(guild) == (15, None)
        assert cog.presence(None) == (None, None)
        assert cog.presence(busy_guild()) == (6, 4)
    with_cog(go, monkeypatch, tmp_path)


def test_houses_are_read_only_when_the_table_fits(monkeypatch, tmp_path):
    async def go(db, cog):
        assert await cog.houses() is None  # the houses module is not merged
        await db.execute("CREATE TABLE houses (id INTEGER PRIMARY KEY, title TEXT)")
        assert await cog.houses() is None  # a table, but not the shape we expect
        await db.execute("DROP TABLE houses")
        await db.execute("CREATE TABLE houses (name TEXT PRIMARY KEY, points INTEGER NOT NULL, role_id INTEGER)")
        for name, points in (("Ember", 120), ("Tide", 340), ("Moss", 90)):
            await db.execute("INSERT INTO houses (name, points, role_id) VALUES (?, ?, ?)", (name, points, uid(1)))
        doc = await cog.refresh()
        assert doc["houses"] == [{"name": "Tide", "points": 340}, {"name": "Ember", "points": 120},
                                 {"name": "Moss", "points": 90}]
        assert not re.search(r"\d{15,20}", cog.path.read_text(encoding="utf-8"))
    with_cog(go, monkeypatch, tmp_path)


def test_seasonal_event_from_events_json(monkeypatch, tmp_path):
    from logic import seasonal as S

    start = datetime.fromtimestamp(T, TZ)
    raw = next(e for e in S.load_events())
    current = S.Event(**{**raw.__dict__, "start": (start.month, start.day),
                         "end": ((start.month % 12) + 1, 1)})

    async def go(db, cog):
        doc = await cog.refresh()
        run = S.current((current,), T, TZ)
        assert run is not None
        assert doc["seasonal"]["name"] == current.name and doc["seasonal"]["ends"] == schema.iso(run.end)
    with_cog(go, monkeypatch, tmp_path, events=(current,))


def test_a_broken_source_only_loses_its_own_section(monkeypatch, tmp_path):
    async def go(db, cog):
        await seed_busy_week(db)
        await db.execute("DROP TABLE mm_matches")
        await db.execute("DROP TABLE tournaments")
        doc = await cog.refresh()
        assert "matchmaking" not in doc
        assert doc["week"]["messages"] == 1150
        assert doc["events"][1] == {"title": "October Cup", "kind": "event", "start": schema.iso(T + 3 * DAY)}
    with_cog(go, monkeypatch, tmp_path, guild=busy_guild())


def test_unsafe_document_is_not_written_and_the_old_file_stays(monkeypatch, tmp_path):
    async def go(db, cog):
        await cog.refresh()
        before = cog.path.read_text(encoding="utf-8")
        monkeypatch.setattr(SD, "build", lambda inputs: {"v": 1, "generated_at": schema.iso(T), "members":
                                                         {"total": uid(1)}})
        try:
            await cog.refresh()
        except SD.Unsafe as e:
            assert str(uid(1)) not in str(e)
        else:
            raise AssertionError("an ID-shaped number was written")
        assert cog.path.read_text(encoding="utf-8") == before
        i = Interaction(User(1, roles=[config.MOD_ROLE]))
        await SiteData.refresh_command.callback(cog, i)
        assert "privacy check" in i.calls[0]["content"] and i.calls[0]["ephemeral"]
    with_cog(go, monkeypatch, tmp_path)


def test_command_is_staff_only_and_ephemeral(monkeypatch, tmp_path):
    async def go(db, cog):
        i = Interaction(User(1))
        await SiteData.refresh_command.callback(cog, i)
        assert "Only staff" in i.calls[0]["content"] and i.calls[0]["ephemeral"]
        assert not cog.path.exists()
        await seed_busy_week(db)
        i = Interaction(User(2, roles=[config.MOD_ROLE]))
        await SiteData.refresh_command.callback(cog, i)
        reply = i.calls[0]
        assert reply["ephemeral"] and "Site data rebuilt" in reply["content"]
        assert "week" in reply["content"] and "1150" not in reply["content"]  # sections, not numbers
        assert cog.path.exists() and cog.last_run == T
    with_cog(go, monkeypatch, tmp_path, guild=busy_guild())


def test_command_is_hidden_from_members_by_default():
    perms = SiteData.group.default_permissions
    assert perms is not None and perms.value == discord.Permissions(moderate_members=True).value
    assert SiteData.group.guild_only


def test_switched_off_is_clean(monkeypatch, tmp_path):
    async def go(db, cog):
        await cog.cog_load()
        assert not cog.tick.is_running()
        i = Interaction(User(2, roles=[config.MOD_ROLE]))
        await SiteData.refresh_command.callback(cog, i)
        assert "off" in i.calls[0]["content"] and i.calls[0]["ephemeral"]
        assert not cog.path.exists()
    with_cog(go, monkeypatch, tmp_path, enabled=False)


def test_site_data_setting(monkeypatch):
    monkeypatch.delenv("SITE_DATA", raising=False)
    assert config.site_data_enabled()
    for value in ("off", "0", "False", " no "):
        monkeypatch.setenv("SITE_DATA", value)
        assert not config.site_data_enabled()
    monkeypatch.setenv("SITE_DATA", "on")
    assert config.site_data_enabled()
    assert config.SITE_DATA_FILE.parent.parent == config.DATA_DIR  # bot/data is gitignored


def test_tick_runs_once_per_interval(monkeypatch, tmp_path):
    async def go(db, cog):
        runs = []

        async def refresh(t=None):
            runs.append(t)
            raise RuntimeError("boom")  # a failing run must not retry every minute

        monkeypatch.setattr(cog, "refresh", refresh)
        await SiteData.tick.coro(cog)
        await SiteData.tick.coro(cog)
        monkeypatch.setattr(cogmod, "now", lambda: T + SD.INTERVAL)
        await SiteData.tick.coro(cog)
        assert runs == [T, T + SD.INTERVAL]
    with_cog(go, monkeypatch, tmp_path)


def test_registered_in_modules():
    import main
    assert ("cogs.sitedata", {"members", "presences"}) in main.MODULES
