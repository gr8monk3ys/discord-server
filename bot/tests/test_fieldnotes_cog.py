"""Offline tests for cogs.fieldnotes: a real in-memory SQLite database, a fake guild, a fake
HTTP client (GitHub and Letterboxd) and a fake listening-log reader. No network, no files."""

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import style
from cogs import fieldnotes as cogmod
from cogs.fieldnotes import FieldNotes
from logic import fieldnotes as F
from logic import quiet as Q
from logic import schedule

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER_ID = 4242
MEMBER_ID = 777
USER = "owner-name"
LBX_USER = "film_fan_9"

PERIOD = schedule.occurrence(F.JOB, date(2026, 11, 1), TZ)  # Sunday 1 Nov 2026 18:00
INSIDE = int(datetime(2026, 10, 28, 15, tzinfo=TZ).timestamp())
ENV = {F.ENV_GITHUB: USER, F.ENV_SPOTIFY: "C:\\fake\\listening_history.jsonl", F.ENV_LETTERBOXD: LBX_USER}


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- fakes
class FakeMessage:
    def __init__(self, mid, channel):
        self.id = mid
        self.channel = channel
        self.threads = []
        self.thread_fail = None

    async def create_thread(self, *, name, auto_archive_duration):
        if self.thread_fail is not None:
            raise self.thread_fail
        self.threads.append((name, auto_archive_duration))


class FakeText:
    def __init__(self, name):
        self.name = name
        self.mention = f"<#{name}>"
        self.sent = []
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.sent.append(dict(content=content, **kwargs))
        message = FakeMessage(1000 + len(self.sent), self)
        self.messages.append(message)
        return message

    async def fetch_message(self, mid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="gone"), "gone")


class FakeGuild:
    def __init__(self, with_channel=True):
        self.id = GUILD_ID
        self.owner_id = OWNER_ID
        self.notes = FakeText(config.FIELD_NOTES_CHANNEL)
        self.text_channels = [FakeText("💬・general")] + ([self.notes] if with_channel else [])


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None

    def get_channel(self, _cid):
        return None


class FakeHttp:
    """Answers by URL prefix; anything else is a 404. Records every URL asked for."""

    def __init__(self):
        self.routes = {}
        self.urls = []
        self.fail = None

    def route(self, prefix, status, body):
        self.routes[prefix] = (status, body if isinstance(body, bytes) else json.dumps(body).encode())

    async def get(self, url, *, headers, max_bytes):
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        for prefix, (status, body) in self.routes.items():
            if url.startswith(prefix):
                return status, body[:max_bytes], len(body) > max_bytes
        return 404, b"{}", False


def repo(name, visibility="public"):
    return {"name": name, "visibility": visibility, "private": visibility != "public", "owner": {"login": USER}}


def pr(name, number, title, at):
    return {"repository_url": f"https://api.github.com/repos/{USER}/{name}", "number": number, "title": title,
            "pull_request": {"merged_at": iso(at)}}


def feed(*items):
    inner = "".join(items)
    return (f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0" xmlns:letterboxd="https://letterboxd.com">'
            f"<channel><title>Letterboxd</title>{inner}</channel></rss>").encode()


def film(title, year, rating, watched, logged):
    pub = datetime.fromtimestamp(logged, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (f"<item><title>{title}</title><pubDate>{pub}</pubDate><letterboxd:watchedDate>{watched}"
            f"</letterboxd:watchedDate><letterboxd:rewatch>No</letterboxd:rewatch><letterboxd:filmTitle>{title}"
            f"</letterboxd:filmTitle><letterboxd:filmYear>{year}</letterboxd:filmYear>"
            f"<letterboxd:memberRating>{rating}</letterboxd:memberRating></item>")


def plays(n=8):
    return [json.dumps({"played_at": iso(INSIDE + i * 300), "track_id": f"t{i % 3}", "name": f"Song {i % 3}",
                        "artists": ["Artist A" if i % 2 else "Artist B"], "duration_ms": 200_000}) + "\n"
            for i in range(n)]


class Env:
    def __init__(self, environ=None, with_channel=True):
        # one event loop for the whole test: the aiosqlite connection belongs to it
        self.loop = asyncio.new_event_loop()
        self.db = dbmod.Database(":memory:")
        self.run(self.db.connect())
        self.run(self.db.migrate())
        self.guild = FakeGuild(with_channel)
        self.bot = FakeBot(self.db, self.guild)
        self.http = FakeHttp()
        self.log_lines = plays()
        self.read_paths = []
        self.read_fail = None
        self.http.route(f"https://api.github.com/users/{USER}/repos", 200,
                        [repo("discord-server"), repo("fleet", "private")])
        self.http.route("https://api.github.com/search/issues", 200, {"items": [
            pr("discord-server", 70, "feat(bot): Season I channels (#70)", INSIDE),
            pr("fleet", 12, "fake private repo change", INSIDE),
        ]})
        self.http.route(f"https://letterboxd.com/{LBX_USER}/rss/", 200,
                        feed(film("Perfect Days", 2023, 3.5, "2026-10-27", INSIDE)))
        self.cog = FieldNotes(self.bot, environ=ENV if environ is None else environ, http=self.http,
                              reader=self.reader)

    def reader(self, path):
        self.read_paths.append(path)
        if self.read_fail is not None:
            raise self.read_fail
        return list(self.log_lines)

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def close(self):
        self.run(self.db.close())
        self.loop.close()


def tick(env, monkeypatch, at):
    monkeypatch.setattr(cogmod, "now", lambda: at)
    return env.run(env.cog.run_once())


def first_post(env, monkeypatch):
    """The first tick only records first_seen; the next week's slot posts."""
    tick(env, monkeypatch, PERIOD.scheduled_at - 3 * 86400)
    return tick(env, monkeypatch, PERIOD.scheduled_at + 60)


# ---------------------------------------------------------------- the weekly post
def test_posts_once_on_sunday_evening_with_every_section(monkeypatch):
    env = Env()
    env.run(env.db.execute('INSERT INTO game_sessions (user_id, game, start, "end") VALUES (?, ?, ?, ?)',
                       (OWNER_ID, "Minecraft", INSIDE, INSIDE + 3 * 3600)))
    assert first_post(env, monkeypatch) is True
    [sent] = env.guild.notes.sent
    embed = sent["embed"]
    assert embed.title == "Field notes, week of 26 Oct"
    assert embed.footer.text == style.label("field notes", "week 44") == "FIELD NOTES · WEEK 44"
    assert embed.colour.value == style.FOREST
    text = embed.description
    for label in ("**Built**", "**Heard**", "**Watched**", "**Played**"):
        assert label in text
    assert "Merged 1 pull request in 1 public repo." in text
    assert "Perfect Days (2023) ★★★½" in text
    assert "Minecraft, about 3 hours." in text
    assert text.endswith(F.question(F.week_of(PERIOD, TZ)))
    assert "fleet" not in text and "private" not in text  # the fake private repo never shows
    assert sent["allowed_mentions"].everyone is False and sent["allowed_mentions"].users is False
    assert env.guild.notes.messages[0].threads == [("Field notes, week 44", 10080)]
    # the question was recorded with the quiet-rooms gate
    state = Q.load(env.run(env.db.meta_get(Q.key(F.QUIET))))
    assert state.posts[-1].message_id == env.guild.notes.messages[0].id
    assert state.posts[-1].at == PERIOD.scheduled_at
    # never twice
    assert tick(env, monkeypatch, PERIOD.scheduled_at + 600) is False
    assert len(env.guild.notes.sent) == 1
    env.close()


def test_first_sighting_only_marks_the_week(monkeypatch):
    env = Env()
    assert tick(env, monkeypatch, PERIOD.scheduled_at + 60) is False
    assert env.guild.notes.sent == []
    env.close()


def test_catches_up_once_after_downtime(monkeypatch):
    env = Env()
    tick(env, monkeypatch, PERIOD.scheduled_at - 3 * 86400)
    assert tick(env, monkeypatch, PERIOD.scheduled_at + 2 * 86400) is True  # down until Tuesday
    assert tick(env, monkeypatch, PERIOD.scheduled_at + 2 * 86400 + 300) is False
    assert len(env.guild.notes.sent) == 1
    env.close()


def test_an_empty_week_posts_nothing_and_is_done(monkeypatch):
    env = Env()
    env.http.route("https://api.github.com/search/issues", 200, {"items": []})
    env.http.route(f"https://letterboxd.com/{LBX_USER}/rss/", 200, feed())
    env.log_lines = []
    assert first_post(env, monkeypatch) is True
    assert env.guild.notes.sent == []
    assert env.run(env.db.meta_get(Q.key(F.QUIET))) is None  # no question was asked
    assert tick(env, monkeypatch, PERIOD.scheduled_at + 600) is False
    env.close()


def test_no_sources_means_no_tick_and_no_requests():
    env = Env(environ={})
    assert not env.cog.settings.enabled
    env.run(env.cog.cog_load())
    assert not env.cog.tick.is_running()
    assert env.http.urls == [] and env.read_paths == []
    env.close()


def test_a_missing_channel_retries_without_reading_anything(monkeypatch):
    env = Env(with_channel=False)
    tick(env, monkeypatch, PERIOD.scheduled_at - 3 * 86400)
    assert tick(env, monkeypatch, PERIOD.scheduled_at + 60) is False
    assert env.http.urls == []
    env.guild.text_channels.append(env.guild.notes)
    assert tick(env, monkeypatch, PERIOD.scheduled_at + 360) is True
    assert len(env.guild.notes.sent) == 1
    env.close()


def test_each_failing_source_is_left_out(monkeypatch):
    env = Env()
    env.http.route(f"https://api.github.com/users/{USER}/repos", 403, {"message": "API rate limit exceeded"})
    env.http.route(f"https://letterboxd.com/{LBX_USER}/rss/", 200, b"<html>not a feed</html>")
    assert first_post(env, monkeypatch) is True
    text = env.guild.notes.sent[0]["embed"].description
    assert "**Built**" not in text and "**Watched**" not in text
    assert "**Heard**" in text
    env.close()


def test_network_errors_and_a_missing_log_post_nothing(monkeypatch):
    env = Env()
    env.http.fail = asyncio.TimeoutError()
    env.read_fail = FileNotFoundError("no log")
    assert first_post(env, monkeypatch) is True
    assert env.guild.notes.sent == []
    env.close()


def test_too_large_responses_are_left_out(monkeypatch):
    env = Env()
    monkeypatch.setattr(F, "MAX_BODY", 10)
    assert env.run(env.cog.built(F.week_of(PERIOD, TZ))) == []
    env.close()


def test_played_is_the_owner_only_and_respects_the_opt_out(monkeypatch):
    env = Env(environ={F.ENV_PLAYED: "on"})
    for uid, game in ((OWNER_ID, "Hades II"), (MEMBER_ID, "Secret Member Game")):
        env.run(env.db.execute('INSERT INTO game_sessions (user_id, game, start, "end") VALUES (?, ?, ?, ?)',
                           (uid, game, INSIDE, INSIDE + 2 * 3600)))
    week = F.week_of(PERIOD, TZ)
    monkeypatch.setattr(cogmod, "now", lambda: PERIOD.scheduled_at)
    assert env.run(env.cog.played(week, env.guild)) == ["Hades II, about 2 hours."]
    env.run(env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (OWNER_ID, INSIDE)))
    assert env.run(env.cog.played(week, env.guild)) == []
    env.close()


def test_a_quiet_room_drops_only_the_question(monkeypatch):
    env = Env()

    async def no(*_a, **_k):
        return False
    monkeypatch.setattr(cogmod.quietgate, "allowed", no)
    assert first_post(env, monkeypatch) is True
    text = env.guild.notes.sent[0]["embed"].description
    assert F.REPLY_HINT not in text and "**Built**" in text
    assert env.run(env.db.meta_get(Q.key(F.QUIET))) is None
    env.close()


def test_a_thread_failure_still_finishes_the_week(monkeypatch):
    env = Env()
    original = env.guild.notes.send

    async def send(*a, **k):
        message = await original(*a, **k)
        message.thread_fail = discord.HTTPException(SimpleNamespace(status=403, reason="no"), "no")
        return message
    env.guild.notes.send = send
    assert first_post(env, monkeypatch) is True
    assert tick(env, monkeypatch, PERIOD.scheduled_at + 600) is False
    env.close()


# ---------------------------------------------------------------- /fieldnotes preview
class FakeResponse:
    def __init__(self, calls):
        self.calls = calls

    async def send_message(self, content=None, **kwargs):
        self.calls.append(("send_message", dict(content=content, **kwargs)))

    async def defer(self, **kwargs):
        self.calls.append(("defer", kwargs))


class FakeFollowup:
    def __init__(self, calls):
        self.calls = calls

    async def send(self, content=None, **kwargs):
        self.calls.append(("followup", dict(content=content, **kwargs)))


class FakeInteraction:
    def __init__(self, guild, user):
        self.calls = []
        self.guild = guild
        self.user = user
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)


def member(guild, uid):
    return SimpleNamespace(id=uid, guild=guild, roles=[], guild_permissions=SimpleNamespace(administrator=False))


def test_preview_is_private_for_staff_and_changes_nothing(monkeypatch):
    env = Env()
    monkeypatch.setattr(cogmod, "now", lambda: INSIDE + 86400)
    i = FakeInteraction(env.guild, member(env.guild, OWNER_ID))
    env.run(FieldNotes.preview.callback(env.cog, i))
    [(kind, sent)] = [c for c in i.calls if c[0] == "followup"]
    assert sent["ephemeral"] is True
    assert "Sources: Built on, Heard on, Watched on, Played on." in sent["content"]
    assert sent["embed"].title == "Field notes, week of 26 Oct"
    assert "**Built**" in sent["embed"].description
    assert env.guild.notes.sent == []
    assert env.run(env.db.meta_get(Q.key(F.QUIET))) is None
    assert env.run(env.db.fetchall("SELECT key FROM jobs")) == []
    env.close()


def test_preview_refuses_non_staff():
    env = Env()
    i = FakeInteraction(env.guild, member(env.guild, MEMBER_ID))
    env.run(FieldNotes.preview.callback(env.cog, i))
    [(kind, sent)] = i.calls
    assert kind == "send_message" and sent["ephemeral"] is True
    assert env.http.urls == []
    env.close()


def test_preview_with_an_empty_week_says_so(monkeypatch):
    env = Env(environ={})
    monkeypatch.setattr(cogmod, "now", lambda: INSIDE)
    i = FakeInteraction(env.guild, member(env.guild, OWNER_ID))
    env.run(FieldNotes.preview.callback(env.cog, i))
    [(_, sent)] = [c for c in i.calls if c[0] == "followup"]
    assert "every section is empty" in sent["content"] and "embed" not in sent
    env.close()
