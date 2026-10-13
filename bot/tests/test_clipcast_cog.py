"""Offline tests for cogs.clipcast: a real in-memory SQLite database, small fakes for the bot,
guild, channels, members, messages and interactions, and a scripted fake HTTP client in
place of Discord's CDN and the YouTube API. No real network (one test talks to an aiohttp
server on 127.0.0.1). Time is controlled by patching cogs.clipcast.now."""

import asyncio
import json
import re
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import discord
import pytest
from discord import app_commands

import config
import db as dbmod
import economy
from cogs import clipcast as cogmod
from cogs.clipcast import AiohttpHttp, Clipcast, ConsentButton, Response, build_view
from cogs.clips import thread_key
from logic import clipcast as L
from logic import quests as Q

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
OWNER, A, B, C, D, E, F, G, H, I, MOD = 1, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20
HOUR = 3600
DAY = 24 * HOUR
WEEK = 7 * DAY
ENV = {"YOUTUBE_CLIENT_ID": "client-id", "YOUTUBE_CLIENT_SECRET": "s3cr3t-value",
       "YOUTUBE_REFRESH_TOKEN": "r3fr3sh-value"}
SECRETS = ("s3cr3t-value", "r3fr3sh-value", "tok1", "tok2")
SESSION = f"{L.UPLOAD_URL}?uploadType=resumable&upload_id=sess"
PAYLOAD = b"\x00\x00\x00\x18ftypmp42" + b"v" * 500


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


T0 = ts(2026, 9, 30, 12)  # Wednesday: both jobs' latest due runs predate the bot
THU = ts(2026, 10, 1, 10)
ASK_AT = ts(2026, 10, 4, 18, 30)  # Sunday
PUB_AT = ts(2026, 10, 6, 18, 30)  # Tuesday
YOUNG = (((THU - 10 * DAY) * 1000 - Q.DISCORD_EPOCH_MS) << 22) + 7  # an account made 10 days before


def http_error(cls=discord.HTTPException, status=500):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


def body(obj) -> bytes:
    return json.dumps(obj).encode()


def google_error(reason, code=403):
    return body({"error": {"code": code, "message": "do not log me", "errors": [{"reason": reason}]}})


# ---------------------------------------------------------------- fake network
class FakeHttp:
    """Scripted replies per kind of request (token, init, put, probe, delete); anything not
    scripted gets the happy-path default. Records every call."""

    def __init__(self):
        self.calls = []
        self.downloads = []
        self.queue = {k: [] for k in ("token", "init", "put", "probe", "delete")}
        self.tokens = 0
        self.videos = 0
        self.payload = PAYLOAD
        self.download_status = 200
        self.download_type = "video/mp4"
        self.download_error = None
        self.on_put = None  # async hook run while the file is "in flight"
        self.files_seen = []

    def script(self, kind, *replies):
        self.queue[kind].extend(replies)

    def kinds(self):
        return [c.kind for c in self.calls]

    def of(self, kind):
        return [c for c in self.calls if c.kind == kind]

    def default(self, kind):
        if kind == "token":
            self.tokens += 1
            return Response(200, {}, body({"access_token": f"tok{self.tokens}", "expires_in": 3600}))
        if kind == "init":
            return Response(200, {"location": SESSION}, b"")
        if kind == "put":
            self.videos += 1
            return Response(200, {}, body({"id": f"vid{self.videos:08d}", "kind": "youtube#video"}))
        if kind == "probe":
            return Response(308, {}, b"")
        return Response(204, {}, b"")

    async def request(self, method, url, *, params=None, headers=None, data=None, json=None, file=None, timeout=30):
        if url == L.TOKEN_URL:
            kind = "token"
        elif method == "POST":
            kind = "init"
        elif method == "PUT":
            kind = "put" if file is not None else "probe"
        else:
            kind = "delete"
        sent = file.read_bytes() if file is not None else None  # the temp file must exist right now
        self.calls.append(SimpleNamespace(kind=kind, method=method, url=url, params=params, headers=headers or {},
                                          data=data, json=json, sent=sent))
        if kind == "put" and self.on_put is not None:
            await self.on_put()
        reply = self.queue[kind].pop(0) if self.queue[kind] else self.default(kind)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def download(self, url, dest, max_bytes):
        self.downloads.append(url)
        if self.download_error is not None:
            raise self.download_error
        dest.write_bytes(self.payload)
        self.files_seen.append(dest)
        return self.download_status, self.download_type, len(self.payload)


# ---------------------------------------------------------------- fake Discord
class FakeAttachment:
    def __init__(self, mid, filename="clip.mp4", content_type="video/mp4", size=len(PAYLOAD)):
        self.filename, self.content_type, self.size = filename, content_type, size
        self.url = f"https://cdn.discordapp.com/attachments/300/{mid}/{filename}?ex=1&hm=2"


class FakeReaction:
    def __init__(self, user_ids, extra=0):
        self.user_ids = list(user_ids)
        self.count = len(self.user_ids) + extra  # `extra`: reactions from people the test doesn't name

    async def users(self):
        for uid in self.user_ids:
            yield SimpleNamespace(id=uid, bot=False)


class FakeMessage:
    def __init__(self, mid, content="", attachments=(), reactions=()):
        self.id = mid
        self.content = content
        self.attachments = list(attachments)
        self.reactions = list(reactions)

    def to_reference(self, fail_if_not_exists=True):
        return ("ref", self.id)


class FakeChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.sent = []
        self.messages = {}
        self.deleted = []
        self.fail = None
        self.fetch_fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        mid = self.id * 1000 + len(self.sent) + 1
        self.sent.append(dict(content=content, id=mid, **kwargs))
        return SimpleNamespace(id=mid)

    async def fetch_message(self, mid):
        if self.fetch_fail is not None:
            raise self.fetch_fail
        if mid not in self.messages:
            raise http_error(discord.NotFound, 404)
        return self.messages[mid]

    def get_partial_message(self, mid):
        async def delete():
            self.deleted.append(mid)
        return SimpleNamespace(id=mid, delete=delete)


class FakeMember:
    def __init__(self, uid, guild, roles=()):
        self.id = uid
        self.bot = False
        self.guild = guild
        self.display_name = f"user{uid}"
        self.roles = [SimpleNamespace(name=r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=False)
        self.dms = []
        self.dm_fail = None

    async def send(self, content=None, **kwargs):
        if self.dm_fail is not None:
            raise self.dm_fail
        self.dms.append(dict(content=content, **kwargs))


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.name = "Front *Porch*"
        self.owner_id = OWNER
        self.clips = FakeChannel(300, config.CLIPS_CHANNEL)
        self.mod_log = FakeChannel(301, config.MOD_LOG_CHANNEL)
        self.thread = FakeChannel(310, "a clip thread")
        self.thread.parent = self.clips
        self.text_channels = [self.mod_log, self.clips]
        self.members = {}
        self.fetch_fail = None

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        raise self.fetch_fail or http_error(discord.NotFound, 404)

    def get_channel_or_thread(self, cid):
        return next((c for c in (self.clips, self.mod_log, self.thread) if c.id == cid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.dynamic = set()

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def get_cog(self, name):
        return self.cog if name == "Clipcast" else None

    def add_dynamic_items(self, *items):
        self.dynamic.update(items)

    def remove_dynamic_items(self, *items):
        self.dynamic.difference_update(items)


class FakeInteraction:
    def __init__(self, bot, user, guild=None):
        self.client = bot
        self.user = user
        self.guild = guild
        self.replies = []
        self.kwargs = []
        self.deferred = False
        outer = self

        class Reply:
            def is_done(self):
                return bool(outer.replies) or outer.deferred

            async def send_message(self, content=None, **kw):
                outer.replies.append(content)
                outer.kwargs.append(kw)

            async def defer(self, **kw):
                outer.deferred = True

        class Followup:
            async def send(self, content=None, **kw):
                outer.replies.append(content)
                outer.kwargs.append(kw)

        self.response, self.followup = Reply(), Followup()

    @property
    def text(self):
        return self.replies[-1]


class Env:
    def __init__(self, db, guild, bot, cog, http, tmp):
        self.db, self.guild, self.bot, self.cog, self.http, self.tmp = db, guild, bot, cog, http, tmp
        self.t = T0
        self.next_mid = 700_000_000_000_000_000

    def member(self, uid, roles=()):
        if uid not in self.guild.members:
            self.guild.members[uid] = FakeMember(uid, self.guild, roles)
        return self.guild.members[uid]

    async def clip(self, uid, content="", reactors=(), extra=0, at=None, link=None, channel=None, **att):
        """A clip as the clips cog stored it, with its message still in the channel."""
        self.next_mid += 1
        mid = self.next_mid
        self.member(uid)
        channel = channel or self.guild.clips
        attachments = [] if link else [FakeAttachment(mid, **att)]
        text = f"{content} {link}".strip() if link else content
        channel.messages[mid] = FakeMessage(mid, text, attachments, [FakeReaction(reactors, extra)])
        at = THU + (mid % 1000) * 60 if at is None else at
        await self.db.execute("INSERT INTO clips (message_id, user_id, url, posted_at) VALUES (?, ?, ?, ?)",
                              (mid, uid, link or attachments[0].url, at))
        if channel is not self.guild.clips:
            await self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (thread_key(mid), str(channel.id)))
        return mid

    async def tick(self, at):
        self.t = at
        await self.cog.tick()

    async def start(self):
        """First sighting of both jobs (marks them done), as on the day the module ships."""
        await self.tick(T0)

    async def press(self, uid, choice, mid, user=None):
        inter = FakeInteraction(self.bot, user or self.member(uid))
        await self.cog.handle_button(inter, choice, mid)
        return inter.text

    async def row(self, mid):
        r = await self.db.fetchone("SELECT * FROM clipcast WHERE message_id = ?", (mid,))
        return dict(r) if r else None

    async def status(self, mid):
        r = await self.row(mid)
        return r["status"] if r else None

    async def put_row(self, mid, uid, status, **kw):
        """A clipcast row written directly, for states the flows can't easily reach."""
        cols = dict(message_id=mid, user_id=uid, channel_id=self.guild.clips.id, posted_at=THU, week="2026-W40",
                    status=status, updated_at=self.t)
        cols.update(kw)
        await self.db.execute(f"INSERT INTO clipcast ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                              tuple(cols.values()))

    async def approved(self, uid, credit=L.NAME, **clip_kw):
        """A clip its author already said yes to."""
        mid = await self.clip(uid, **clip_kw)
        await self.put_row(mid, uid, L.APPROVED, credit=credit, credit_name=f"user{uid}" if credit == L.NAME else None,
                           answered_by=uid, answered_at=self.t, answered_via=L.VIA_BUTTON, backers=1)
        return mid

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def meta(self, key):
        return await self.cog.get_meta(key)

    def leftovers(self):
        return list(self.tmp.glob("*")) if self.tmp.exists() else []

    def interaction(self, uid, roles=()):
        return FakeInteraction(self.bot, self.member(uid, roles), self.guild)

    async def command(self, name, uid, *args, roles=()):
        inter = self.interaction(uid, roles)
        await getattr(self.cog, name).callback(self.cog, inter, *args)
        return inter


def with_env(fn, monkeypatch, tmp_path, environ=ENV):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        http = FakeHttp()
        tmp = tmp_path / "clipcast"
        cog = Clipcast(bot, http=http, environ=environ, tmp_dir=tmp)
        bot.cog = cog
        env = Env(db, guild, bot, cog, http, tmp)
        env.member(OWNER)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    asyncio.run(go())


def choice(value):
    return app_commands.Choice(name=value, value=value)


def only_pings(mentions, uid) -> bool:
    """Exactly this one member may be pinged: no @everyone, no roles, nobody else."""
    d = mentions.to_dict()
    return d["parse"] == [] and d["users"] == [uid] and "roles" not in d


def no_secrets(text):
    return not any(s in str(text) for s in SECRETS)


# ---------------------------------------------------------------- wiring
def test_button_template_round_trips():
    b = ConsentButton(L.ANON, 42, "Yes, anonymously")
    assert b.item.custom_id == "clipcast:anon:42"
    pattern = ConsentButton.__discord_ui_compiled_template__
    m = re.fullmatch(pattern, "clipcast:name:700000000000000001")
    assert m and m["choice"] == "name" and m["id"] == "700000000000000001"
    assert re.fullmatch(pattern, "clipcast:maybe:7") is None
    ids = [c.item.custom_id for c in build_view(7, "Sam").children]
    assert ids == ["clipcast:name:7", "clipcast:anon:7", "clipcast:no:7"]
    assert [c.item.label for c in build_view(7, "Sam").children][0] == "Yes, credit me as Sam"
    assert [c.item.custom_id for c in build_view(7, None).children] == ["clipcast:anon:7", "clipcast:no:7"]


def test_cog_load_registers_the_buttons(monkeypatch, tmp_path):
    async def go(env):
        await env.cog.cog_load()
        assert ConsentButton in env.bot.dynamic and env.cog.ticker.is_running()
        await env.cog.cog_unload()
        assert ConsentButton not in env.bot.dynamic
    with_env(go, monkeypatch, tmp_path)


def test_staff_subcommands_say_so_and_the_module_is_listed():
    import main
    from logic import helpdesk, ops
    assert ("cogs.clipcast", {"members", "message_content"}) in main.MODULES
    assert helpdesk.staff_text(Clipcast.status.description) and helpdesk.staff_text(Clipcast.skip.description)
    assert not helpdesk.staff_text(Clipcast.consent.description) and not helpdesk.staff_text(Clipcast.remove.description)
    assert helpdesk.MODULE_CATEGORY["clipcast"] == "creators"
    assert {n.channel for n in ops.CHANNEL_NEEDS if n.cog == "clipcast"} == {config.CLIPS_CHANNEL,
                                                                             config.MOD_LOG_CHANNEL}
    assert L.BADGE_KEY in __import__("logic.achievements", fromlist=["BY_KEY"]).BY_KEY


def test_settings_come_from_config(monkeypatch, tmp_path):
    assert config.clipcast_env({"YOUTUBE_CLIENT_ID": " x ", "OTHER": "y"}) == {
        "YOUTUBE_CLIENT_ID": "x", "YOUTUBE_CLIENT_SECRET": "", "YOUTUBE_REFRESH_TOKEN": "", "YOUTUBE_PRIVACY": ""}

    async def go(env):
        assert env.cog.setup.configured and env.cog.setup.privacy == "unlisted"
    with_env(go, monkeypatch, tmp_path)

    async def off(env):
        assert not env.cog.setup.configured
    with_env(off, monkeypatch, tmp_path, environ={})


# ---------------------------------------------------------------- Sunday: shortlist and ask
def test_first_sight_of_the_jobs_asks_and_posts_nothing(monkeypatch, tmp_path):
    async def go(env):
        await env.approved(A)
        await env.clip(B, at=T0 - DAY)
        await env.start()
        assert env.http.calls == [] and env.http.downloads == []
        assert env.guild.members[B].dms == [] and env.guild.mod_log.sent == []
        jobs = {r["key"] for r in await env.db.fetchall("SELECT key FROM jobs")}
        assert jobs == {"clipcast-ask:2026-W39", "clipcast:2026-W40"}
    with_env(go, monkeypatch, tmp_path)


def test_ask_job_shortlists_the_top_three_and_asks_their_authors(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.clip(A, "warmup", reactors=[B], extra=4)
        b1 = await env.clip(B, "nice <@123456789012345678>", reactors=[A, C])
        c1 = await env.clip(C, reactors=[YOUNG])
        d1 = await env.clip(D)  # fourth by ranking: not asked
        a2 = await env.clip(A, "insane 1v4", reactors=[B, C], extra=7)  # A's best
        link = await env.clip(E, extra=50, link="https://medal.tv/games/x/clips/abc")
        young = await env.clip(YOUNG, extra=40)
        opted = await env.clip(F, extra=30)
        await env.optout(F)
        rude = await env.clip(G, "join discord.gg/abcdef", extra=20)
        huge = await env.clip(H, extra=15, size=L.MAX_BYTES + 1)
        never = await env.clip(I, extra=12)
        await env.db.execute("INSERT INTO clipcast_prefs (user_id, pref, set_at) VALUES (?, ?, ?)", (I, L.NEVER, T0))
        mkv = await env.clip(MOD, extra=11, filename="clip.mkv", content_type="video/x-matroska")

        await env.tick(ASK_AT)
        rows = {r["message_id"]: dict(r) for r in await env.db.fetchall("SELECT * FROM clipcast")}
        assert set(rows) == {a2, b1, c1}
        for mid in (a1, d1, link, young, opted, rude, huge, never, mkv):
            assert mid not in rows
        for mid, uid in ((a2, A), (b1, B), (c1, C)):
            r = rows[mid]
            assert (r["status"], r["user_id"], r["asked_at"], r["asked_via"], r["week"]) == \
                   (L.ASKED, uid, ASK_AT, "dm", "2026-W40")
            assert r["answered_by"] is None and r["answered_at"] is None and r["credit"] is None
            (dm,) = env.guild.members[uid].dms
            assert f"/channels/{GUILD_ID}/300/{mid}" in dm["content"] and "Front \\*Porch\\*" in dm["content"]
            assert [c.item.custom_id for c in dm["view"].children] == [f"clipcast:{k}:{mid}" for k in L.CHOICES]
            assert dm["view"].children[0].item.label == f"Yes, credit me as user{uid}"
            assert dm["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        assert rows[a2]["reactions"] == 9 and rows[a2]["backers"] == 2
        assert rows[c1]["backers"] == 0  # a reaction from a fresh account doesn't back a clip
        assert env.guild.members[D].dms == [] and env.guild.clips.sent == []
        assert env.http.calls == [] and env.http.downloads == []  # asking never touches the network

        # The run is recorded: later ticks and a direct re-run ask nobody twice.
        await env.tick(ASK_AT + 600)
        assert [len(env.guild.members[u].dms) for u in (A, B, C, D)] == [1, 1, 1, 0]
    with_env(go, monkeypatch, tmp_path)


def test_ask_falls_back_to_a_reply_in_the_clips_channel(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.clip(A, channel=env.guild.thread)
        b1 = await env.clip(B)
        env.member(A).dm_fail = http_error(discord.Forbidden, 403)
        env.member(B).dm_fail = http_error(discord.Forbidden, 403)
        env.guild.clips.fail = http_error()
        await env.tick(ASK_AT)
        (sent,) = env.guild.thread.sent  # asked where the clip is
        assert sent["content"].startswith(f"🎬 <@{A}>") and sent["reference"] == ("ref", a1)
        assert only_pings(sent["allowed_mentions"], A)
        assert [c.item.custom_id for c in sent["view"].children][0] == f"clipcast:name:{a1}"
        assert (await env.row(a1))["asked_via"] == "channel" and (await env.row(a1))["channel_id"] == 310
        assert (await env.row(b1))["asked_via"] == "unsent" and await env.status(b1) == L.ASKED
    with_env(go, monkeypatch, tmp_path)


def test_a_display_name_that_fails_the_filter_is_only_offered_anonymous(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.clip(A)
        env.member(A).display_name = "free nitro at example.com"
        await env.tick(ASK_AT)
        (dm,) = env.member(A).dms
        assert [c.item.custom_id for c in dm["view"].children] == [f"clipcast:anon:{a1}", f"clipcast:no:{a1}"]
        assert "can't be used as a credit" in await env.press(A, L.NAME, a1)  # a stale or forged click
        assert await env.status(a1) == L.ASKED
        assert "with no name on it" in await env.press(A, L.ANON, a1)
        assert (await env.row(a1))["credit_name"] is None and await env.status(a1) == L.APPROVED
    with_env(go, monkeypatch, tmp_path)


def test_unanswered_questions_expire_at_the_next_ask(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.clip(A)
        b1 = await env.clip(B)
        await env.tick(ASK_AT)
        await env.press(B, L.NAME, b1)
        later = await env.clip(C, at=ASK_AT + DAY)
        await env.tick(ASK_AT + WEEK)
        assert await env.status(a1) == L.EXPIRED
        assert await env.status(b1) == L.PUBLISHED  # answered in time: went out on Tuesday
        assert await env.status(later) == L.ASKED
        assert "closed (expired without an answer)" in await env.press(A, L.NAME, a1)
        assert await env.status(a1) == L.EXPIRED
        assert len(env.member(A).dms) == 1  # the same clip is never asked about again
    with_env(go, monkeypatch, tmp_path)


def test_no_clips_channel_means_nothing_happens(monkeypatch, tmp_path):
    async def go(env):
        env.guild.text_channels = [env.guild.mod_log]
        await env.start()
        await env.clip(A)
        await env.tick(ASK_AT)
        assert await env.db.fetchall("SELECT 1 FROM clipcast") == [] and env.member(A).dms == []
    with_env(go, monkeypatch, tmp_path)


ASK_KEY, PUB_KEY = "clipcast-ask:2026-W40", "clipcast:2026-W41"  # the runs at ASK_AT and PUB_AT


def take_away(env, what):
    """Make a channel ("clips", "mod_log") or the whole server ("guild") unavailable.
    Returns the call that brings it back."""
    if what == "guild":
        env.bot.get_guild = lambda gid: None
        return lambda: delattr(env.bot, "get_guild")
    channels = env.guild.text_channels
    env.guild.text_channels = [c for c in channels if c is not getattr(env.guild, what)]
    return lambda: setattr(env.guild, "text_channels", channels)


async def job_keys(env):
    return {r["key"] for r in await env.db.fetchall("SELECT key FROM jobs")}


@pytest.mark.parametrize("missing", ["clips", "guild"])
def test_the_ask_waits_for_the_clips_channel_then_asks_once(monkeypatch, tmp_path, missing):
    async def go(env):
        await env.start()
        a1 = await env.clip(A, reactors=[B])
        bring_back = take_away(env, missing)
        for late in (0, 300):  # the Sunday tick and the next: nowhere to look
            await env.tick(ASK_AT + late)
        assert ASK_KEY not in await job_keys(env)  # not done: the week's shortlist is still owed
        assert await env.db.fetchall("SELECT 1 FROM clipcast") == [] and env.member(A).dms == []

        bring_back()
        await env.tick(ASK_AT + 600)
        assert ASK_KEY in await job_keys(env)
        (dm,) = env.member(A).dms
        assert f"/channels/{GUILD_ID}/300/{a1}" in dm["content"]
        row = await env.row(a1)
        assert (row["status"], row["asked_at"], row["asked_via"], row["week"]) == \
               (L.ASKED, ASK_AT + 600, "dm", "2026-W40")
        for late in (900, DAY):  # later ticks: nobody is asked twice
            await env.tick(ASK_AT + late)
        assert len(env.member(A).dms) == 1 and env.guild.clips.sent == []
    with_env(go, monkeypatch, tmp_path)


def test_an_ask_that_is_no_longer_the_latest_week_is_dropped(monkeypatch, tmp_path):
    """A week is retried only while it is the latest one, so a channel that was gone for
    weeks comes back to one round of questions, about the latest week."""
    async def go(env):
        await env.start()
        old = await env.clip(A)
        bring_back = take_away(env, "clips")
        await env.tick(ASK_AT)
        new = await env.clip(B, at=ASK_AT + DAY)
        await env.tick(ASK_AT + WEEK)  # the next Sunday: still gone, and last week is given up
        keys = await job_keys(env)
        assert ASK_KEY in keys and "clipcast-ask:2026-W41" not in keys
        bring_back()
        await env.tick(ASK_AT + WEEK + 300)
        assert "clipcast-ask:2026-W41" in await job_keys(env)
        assert env.member(A).dms == [] and len(env.member(B).dms) == 1
        assert await env.status(old) is None and await env.status(new) == L.ASKED
    with_env(go, monkeypatch, tmp_path)


def test_a_week_with_nothing_to_ask_or_post_is_done(monkeypatch, tmp_path):
    """Nothing to do is a finished week, not one to keep retrying."""
    async def go(env):
        await env.start()
        await env.tick(ASK_AT)
        assert ASK_KEY in await job_keys(env)
        await env.tick(PUB_AT)
        assert PUB_KEY in await job_keys(env)
        assert (await env.meta(cogmod.RUN_KEY)) == {"at": PUB_AT, "text": "no approved clips"}
        assert env.guild.mod_log.sent == [] and env.guild.clips.sent == [] and env.http.calls == []
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- the consent buttons
def test_only_the_author_answers_and_the_answer_is_recorded(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.clip(A)
        await env.tick(ASK_AT)
        assert "Only the member who posted" in await env.press(B, L.NAME, a1)
        assert "Only the member who posted" in await env.press(OWNER, L.NAME, a1)  # not even the owner
        assert await env.status(a1) == L.ASKED and (await env.row(a1))["answered_by"] is None

        env.t = ASK_AT + 100
        env.member(A).display_name = "Sam *the* Great"
        text = await env.press(A, L.NAME, a1)
        assert "credited as **Sam \\*the\\* Great**" in text
        r = await env.row(a1)
        assert (r["status"], r["credit"], r["credit_name"]) == (L.APPROVED, L.NAME, "Sam *the* Great")
        assert (r["answered_by"], r["answered_at"], r["answered_via"]) == (A, ASK_AT + 100, L.VIA_BUTTON)

        env.t = ASK_AT + 200  # changed their mind
        assert "stays in Discord" in await env.press(A, L.NO, a1)
        r = await env.row(a1)
        assert (r["status"], r["credit"], r["credit_name"]) == (L.DECLINED, None, None)
        assert (r["answered_by"], r["answered_at"]) == (A, ASK_AT + 200)

        env.t = ASK_AT + 300  # and back again, anonymously
        await env.press(A, L.ANON, a1)
        r = await env.row(a1)
        assert (r["status"], r["credit"], r["credit_name"], r["answered_at"]) == (L.APPROVED, L.ANON, None, ASK_AT + 300)
        assert await env.press(A, L.NAME, 123456) == "That clip isn't on the list any more."
    with_env(go, monkeypatch, tmp_path)


def test_answers_from_outside_the_server_or_with_tracking_off_are_refused(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.clip(A)
        b1 = await env.clip(B)
        await env.tick(ASK_AT)
        gone = env.guild.members.pop(A)
        assert "in the server" in await env.press(A, L.NAME, a1, user=gone)
        assert await env.status(a1) == L.ASKED
        await env.optout(B)
        assert "tracking off" in await env.press(B, L.NAME, b1)
        assert await env.status(b1) == L.WITHDRAWN
        assert "tracking off" in await env.press(B, L.ANON, b1) and await env.status(b1) == L.WITHDRAWN
        await env.db.execute("DELETE FROM privacy_optout WHERE user_id = ?", (B,))
        assert "closed (withdrawn)" in await env.press(B, L.NAME, b1)  # turning it back on revives nothing
    with_env(go, monkeypatch, tmp_path)


def test_a_closed_clip_takes_no_answer(monkeypatch, tmp_path):
    async def go(env):
        for i, status in enumerate((L.WITHDRAWN, L.EXPIRED, L.SKIPPED, L.UPLOADING, L.PUBLISHED, L.HANDED, L.UNSURE,
                                    L.FAILED, L.REMOVED, L.REMOVAL)):
            mid = 900_000_000_000_000_000 + i
            await env.put_row(mid, A, status)
            env.member(A)
            assert "closed" in await env.press(A, L.NAME, mid), status
            assert await env.status(mid) == status
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- standing preference
def test_standing_always_approves_without_asking(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        env.t = T0 + 50
        inter = await env.command("consent", A, choice(L.ALWAYS), choice(L.ANON))
        assert "anonymously" in inter.text and inter.kwargs[-1]["ephemeral"]
        await env.command("consent", B, choice(L.ALWAYS), None)
        a1 = await env.clip(A)
        b1 = await env.clip(B)
        await env.tick(ASK_AT)
        ra, rb = await env.row(a1), await env.row(b1)
        assert (ra["status"], ra["credit"], ra["credit_name"]) == (L.APPROVED, L.ANON, None)
        assert (rb["status"], rb["credit"], rb["credit_name"]) == (L.APPROVED, L.NAME, f"user{B}")
        # who said yes and when: the author, at the moment they set the preference
        assert (ra["answered_by"], ra["answered_at"], ra["answered_via"]) == (A, T0 + 50, L.VIA_STANDING)
        assert env.member(A).dms == [] and env.member(B).dms == []
        assert L.may_upload(ra) and L.may_upload(rb)
    with_env(go, monkeypatch, tmp_path)


def test_standing_never_declines_what_is_waiting_and_stops_the_questions(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.clip(A)
        a_ok = await env.approved(A)
        posted = 900_000_000_000_000_001
        await env.put_row(posted, A, L.PUBLISHED, video_id="vid00000009")
        await env.tick(ASK_AT)
        env.t = ASK_AT + 60
        inter = await env.command("consent", A, choice(L.NEVER), None)
        assert "2 clip(s) waiting were taken off the list" in inter.text and "/clipcast remove" in inter.text
        for mid in (a1, a_ok):
            r = await env.row(mid)
            assert (r["status"], r["credit"], r["answered_by"], r["answered_at"], r["answered_via"]) == \
                   (L.DECLINED, None, A, ASK_AT + 60, L.VIA_STANDING)
        assert await env.status(posted) == L.PUBLISHED  # takedown is /clipcast remove, never a side effect
        await env.clip(A, at=ASK_AT + DAY)
        await env.tick(ASK_AT + WEEK)
        assert len(env.member(A).dms) == 1  # only the first week's question
        inter = await env.command("consent", A, choice(L.ASK), None)
        pref = await env.db.fetchone("SELECT * FROM clipcast_prefs WHERE user_id = ?", (A,))
        assert pref["pref"] == L.ASK and pref["credit"] is None
    with_env(go, monkeypatch, tmp_path)


def test_switching_back_to_ask_reopens_what_the_standing_yes_approved(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        await env.command("consent", A, choice(L.ALWAYS), None)
        auto = await env.clip(A)  # approved by the standing yes on Sunday
        pressed = await env.approved(A)  # a yes A gave with the button: that one stands
        other = await env.clip(B)
        await env.command("consent", B, choice(L.ALWAYS), None)
        posted = 900_000_000_000_000_001
        await env.put_row(posted, A, L.PUBLISHED, video_id="vid00000009", credit=L.NAME, answered_by=A,
                          answered_at=T0, answered_via=L.VIA_STANDING)
        await env.tick(ASK_AT)
        assert await env.status(auto) == await env.status(other) == L.APPROVED and env.member(A).dms == []
        env.t = ASK_AT + 60
        inter = await env.command("consent", A, choice(L.ASK), None)
        assert "1 clip(s) your standing yes had already approved won't be posted unless you say yes" in inter.text
        row = await env.row(auto)
        assert (row["status"], row["credit"], row["credit_name"]) == (L.ASKED, None, None)
        assert (row["answered_by"], row["answered_at"], row["answered_via"]) == (None, None, None)
        assert (row["asked_at"], row["asked_via"]) == (ASK_AT + 60, "dm") and not L.may_upload(row)
        (dm,) = env.member(A).dms  # the question is really asked, with its buttons
        assert "shortlist" in dm["content"] and dm["view"] is not None
        # untouched: their own button answer, what is already posted, and anybody else's clips
        assert await env.status(pressed) == L.APPROVED and (await env.row(pressed))["answered_via"] == L.VIA_BUTTON
        assert await env.status(posted) == L.PUBLISHED and await env.status(other) == L.APPROVED
        # the reopened clip is posted only after a real yes
        assert "Thanks!" in await env.press(A, "anon", auto)
        row = await env.row(auto)
        assert (row["status"], row["answered_via"], row["answered_at"]) == (L.APPROVED, L.VIA_BUTTON, ASK_AT + 60)
        # saying "ask" again changes nothing more and sends nothing more
        inter = await env.command("consent", A, choice(L.ASK), None)
        assert "standing yes" not in inter.text and len(env.member(A).dms) == 1
    with_env(go, monkeypatch, tmp_path)


def test_a_reopened_clip_that_cannot_be_looked_at_stays_unanswered(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        await env.command("consent", A, choice(L.ALWAYS), None)
        auto = await env.clip(A)
        await env.tick(ASK_AT)
        del env.guild.clips.messages[auto]  # deleted in the meantime
        await env.command("consent", A, choice(L.ASK), None)
        row = await env.row(auto)
        assert row["status"] == L.ASKED and row["asked_via"] == "unsent" and env.member(A).dms == []
        assert not L.may_upload(row)
    with_env(go, monkeypatch, tmp_path)


def test_standing_always_needs_tracking_on_and_tracking_off_resets_it(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        await env.optout(A)
        inter = await env.command("consent", A, choice(L.ALWAYS), None)
        assert "tracking off" in inter.text
        assert await env.db.fetchone("SELECT 1 FROM clipcast_prefs WHERE user_id = ?", (A,)) is None
        await env.command("consent", B, choice(L.ALWAYS), None)
        await env.optout(B)
        await env.cog.sweep()
        assert (await env.db.fetchone("SELECT pref FROM clipcast_prefs WHERE user_id = ?", (B,)))["pref"] == L.ASK
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- withdrawal
async def one_of_each(env):
    """For member A: a clip being asked about, an approved one, and one already posted."""
    asked = await env.clip(A)
    await env.put_row(asked, A, L.ASKED, asked_at=env.t)
    ok = await env.approved(A)
    posted = await env.clip(A)
    await env.put_row(posted, A, L.PUBLISHED, video_id="vid00000009", credit=L.ANON, answered_by=A, answered_at=env.t)
    other = await env.approved(B)
    return asked, ok, posted, other


def test_deleting_the_clip_withdraws_consent(monkeypatch, tmp_path):
    async def go(env):
        asked, ok, posted, other = await one_of_each(env)
        await env.cog.on_raw_message_delete(SimpleNamespace(message_id=asked))
        await env.cog.on_raw_bulk_message_delete(SimpleNamespace(message_ids={ok, posted, 5}))
        assert [await env.status(m) for m in (asked, ok, posted, other)] == \
               [L.WITHDRAWN, L.WITHDRAWN, L.PUBLISHED, L.APPROVED]
        assert (await env.row(ok))["note"] == "clip deleted"
        assert not L.may_upload(await env.row(ok))
    with_env(go, monkeypatch, tmp_path)


def test_leaving_or_a_ban_withdraws_consent_and_the_standing_answer(monkeypatch, tmp_path):
    async def go(env):
        asked, ok, posted, other = await one_of_each(env)
        await env.command("consent", A, choice(L.ALWAYS), None)
        await env.command("consent", B, choice(L.ALWAYS), None)
        await env.cog.on_raw_member_remove(SimpleNamespace(guild_id=GUILD_ID + 1, user=SimpleNamespace(id=A)))
        assert await env.status(ok) == L.APPROVED  # another server's event
        await env.cog.on_raw_member_remove(SimpleNamespace(guild_id=GUILD_ID, user=SimpleNamespace(id=A)))
        assert [await env.status(m) for m in (asked, ok, posted, other)] == \
               [L.WITHDRAWN, L.WITHDRAWN, L.PUBLISHED, L.APPROVED]
        assert (await env.row(ok))["note"] == "left the server"
        assert await env.db.fetchone("SELECT 1 FROM clipcast_prefs WHERE user_id = ?", (A,)) is None

        await env.cog.on_member_ban(SimpleNamespace(id=GUILD_ID + 1), SimpleNamespace(id=B))
        assert await env.status(other) == L.APPROVED
        await env.cog.on_member_ban(env.guild, SimpleNamespace(id=B))
        assert await env.status(other) == L.WITHDRAWN and (await env.row(other))["note"] == "banned"
        assert await env.db.fetchone("SELECT 1 FROM clipcast_prefs WHERE user_id = ?", (B,)) is None
    with_env(go, monkeypatch, tmp_path)


def test_privacy_off_withdraws_consent_on_the_next_tick(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        asked, ok, posted, other = await one_of_each(env)
        await env.optout(A)
        await env.tick(T0 + 300)
        assert [await env.status(m) for m in (asked, ok, posted, other)] == \
               [L.WITHDRAWN, L.WITHDRAWN, L.PUBLISHED, L.APPROVED]
        assert (await env.row(ok))["note"] == "privacy"
    with_env(go, monkeypatch, tmp_path)


def test_everything_is_checked_again_right_before_posting(monkeypatch, tmp_path):
    """Things that changed while the bot wasn't looking (no event seen)."""
    async def go(env):
        await env.start()
        left = await env.approved(A)
        deleted = await env.approved(B)
        opted = await env.approved(C)
        edited = await env.approved(D, content="clean")
        swapped = await env.approved(E)
        fine = await env.approved(F)
        del env.guild.members[A]
        del env.guild.clips.messages[deleted]
        env.guild.clips.messages[edited].content = "now with discord.gg/abcdef"
        env.guild.clips.messages[swapped].attachments = []
        await env.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (C, T0))
        env.t = PUB_AT
        await env.cog.publish_week(SimpleNamespace(key="clipcast:2026-W41"))
        assert [await env.status(m) for m in (left, deleted, opted, edited, swapped, fine)] == \
               [L.WITHDRAWN, L.WITHDRAWN, L.WITHDRAWN, L.SKIPPED, L.SKIPPED, L.PUBLISHED]
        assert (await env.row(left))["note"] == "left the server"
        assert (await env.row(deleted))["note"] == "clip deleted"
        assert (await env.row(edited))["note"] == "caption failed the text filter"
        assert len(env.http.downloads) == 1 and env.http.kinds() == ["token", "init", "put"]
    with_env(go, monkeypatch, tmp_path)


def test_a_discord_hiccup_leaves_consent_alone_and_posts_nothing(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        b1 = await env.approved(B)
        del env.guild.members[A]
        env.guild.fetch_fail = http_error()  # can't tell whether A is still here
        env.guild.clips.fetch_fail = http_error()  # can't read B's clip
        await env.tick(PUB_AT)
        assert await env.status(a1) == L.APPROVED and await env.status(b1) == L.APPROVED
        assert env.http.calls == [] and env.http.downloads == []
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- Tuesday: upload
def test_approved_clips_are_uploaded_once_and_celebrated(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        b1 = await env.clip(B, "calm <b>save</b> https://cdn.discordapp.com/x.mp4", reactors=[A])
        a1 = await env.clip(A, "insane 1v4", reactors=[B, C], extra=3)
        c1 = await env.clip(C, reactors=[A])
        await env.tick(ASK_AT)
        await env.press(A, L.NAME, a1)
        await env.press(B, L.ANON, b1)
        await env.press(C, L.NO, c1)
        await env.tick(PUB_AT)

        assert env.http.kinds() == ["token", "init", "put", "init", "put"]  # one token, each clip once
        token, init_b, put_b, init_a, put_a = env.http.calls
        assert token.url == L.TOKEN_URL and token.data == {
            "client_id": "client-id", "client_secret": "s3cr3t-value", "refresh_token": "r3fr3sh-value",
            "grant_type": "refresh_token"}
        assert env.http.downloads == [env.guild.clips.messages[b1].attachments[0].url,
                                      env.guild.clips.messages[a1].attachments[0].url]
        for init in (init_a, init_b):
            assert init.method == "POST" and init.url == L.UPLOAD_URL
            assert init.params == {"uploadType": "resumable", "part": "snippet,status"}
            assert init.headers == {"Authorization": "Bearer tok1", "X-Upload-Content-Length": str(len(PAYLOAD)),
                                    "X-Upload-Content-Type": "video/mp4"}
            assert init.json["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
            assert L.JOIN_URL in init.json["snippet"]["description"]
        assert init_a.json["snippet"]["title"] == "insane 1v4 | Clip of the week from Front *Porch* #Shorts"
        assert init_a.json["snippet"]["description"].startswith(f"Clip by user{A}, posted with their permission.")
        assert init_b.json["snippet"]["title"] == "calm bsave/b | Clip of the week from Front *Porch* #Shorts"
        assert init_b.json["snippet"]["description"].startswith("Clip by one of our members")
        assert f"user{B}" not in json.dumps(init_b.json)  # anonymous means anonymous
        for put in (put_a, put_b):
            assert put.url == SESSION and put.sent == PAYLOAD
            assert put.headers == {"Authorization": "Bearer tok1", "Content-Length": str(len(PAYLOAD)),
                                   "Content-Type": "video/mp4"}
        assert {urlsplit(c.url).hostname for c in env.http.calls} == {"oauth2.googleapis.com", "www.googleapis.com"}

        ra, rb = await env.row(a1), await env.row(b1)
        assert (rb["status"], rb["video_id"], rb["published_at"], rb["attempts"]) == (L.PUBLISHED, "vid00000001", PUB_AT, 1)
        assert (ra["status"], ra["video_id"]) == (L.PUBLISHED, "vid00000002")
        assert await env.status(c1) == L.DECLINED
        assert env.leftovers() == [] and len(env.http.files_seen) == 2  # temp files are gone

        # the public "as seen on" replies and the badge; the coins are only promised for later
        notice_b, notice_a = env.guild.clips.sent
        assert notice_a["content"] == (f"📺 As seen on the server's YouTube: https://youtu.be/vid00000002\n"
                                       f"<@{A}> earned the 📺 **On the Channel** badge for it. "
                                       "And 100 coins follow once it has stayed up for 7 days.")
        assert notice_a["reference"] == ("ref", a1) and notice_b["reference"] == ("ref", b1)
        assert only_pings(notice_a["allowed_mentions"], A)
        assert ra["notice_message_id"] == notice_a["id"]
        assert await economy.balance(env.db, A) == 0 and await economy.balance(env.db, B) == 0
        assert await env.db.fetchall("SELECT ref FROM ledger WHERE reason = 'clipcast'") == []
        assert (ra["payout_at"], rb["payout_at"]) == (None, None)
        held = await env.db.fetchall("SELECT user_id FROM achievements WHERE key = 'clipcast' ORDER BY user_id")
        assert [r["user_id"] for r in held] == [A, B]
        assert len(env.guild.mod_log.sent) == 2
        assert "as **unlisted**: https://youtu.be/vid00000001 (anonymous)" in env.guild.mod_log.sent[0]["content"]
        assert (await env.meta(cogmod.RUN_KEY)) == {"at": PUB_AT, "text": "2 posted"}

        # Nothing is ever uploaded a second time: not on later ticks, not next week.
        before = len(env.http.calls)
        await env.tick(PUB_AT + 600)
        await env.tick(PUB_AT + WEEK)
        assert len(env.http.calls) == before and len(env.http.downloads) == 2
        # a week on, still up: now the coins, once each
        assert await economy.balance(env.db, A) == 100 and await economy.balance(env.db, B) == 100
        refs = {r["ref"] for r in await env.db.fetchall("SELECT ref FROM ledger WHERE reason = 'clipcast'")}
        assert refs == {f"clipcast:{a1}", f"clipcast:{b1}"}
        assert all(no_secrets(m["content"]) for m in env.guild.mod_log.sent + env.guild.clips.sent)
    with_env(go, monkeypatch, tmp_path)


def test_at_most_three_a_run_one_per_author_by_the_poll_ranking(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        low = await env.approved(A, extra=1)
        a_best = await env.approved(A, extra=9)
        b1 = await env.approved(B, extra=5)
        c1 = await env.approved(C, extra=4)
        d1 = await env.approved(D, extra=0)
        await env.tick(PUB_AT)
        assert [await env.status(m) for m in (low, a_best, b1, c1, d1)] == \
               [L.APPROVED, L.PUBLISHED, L.PUBLISHED, L.PUBLISHED, L.APPROVED]
        assert len(env.http.of("put")) == 3
        await env.tick(PUB_AT + WEEK)  # the rest go out next week, still once each
        assert await env.status(low) == L.PUBLISHED and await env.status(d1) == L.PUBLISHED
        assert len(env.http.of("put")) == 5
    with_env(go, monkeypatch, tmp_path)


def test_nothing_is_uploaded_without_a_recorded_yes(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        users = iter(range(100, 200))
        for status in (L.ASKED, L.DECLINED, L.WITHDRAWN, L.EXPIRED, L.SKIPPED, L.UPLOADING, L.PUBLISHED, L.HANDED,
                       L.UNSURE, L.FAILED, L.REMOVED, L.REMOVAL):
            uid = next(users)
            mid = await env.clip(uid, extra=50)
            await env.put_row(mid, uid, status, credit=L.NAME, credit_name="x", answered_by=uid, answered_at=T0)
        # "approved" rows whose consent record is incomplete or someone else's
        broken = []
        for kw in (dict(credit=None, answered_by="self", answered_at=T0),
                   dict(credit=L.NAME, answered_by=None, answered_at=T0),
                   dict(credit=L.NAME, answered_by="self", answered_at=None),
                   dict(credit=L.NAME, answered_by=OWNER, answered_at=T0),  # a staff member's yes is not the author's
                   dict(credit="sure", answered_by="self", answered_at=T0),
                   dict(credit=L.NAME, answered_by="self", answered_at=T0, video_id="vid00000042")):
            uid = next(users)
            mid = await env.clip(uid, extra=50)
            kw["answered_by"] = uid if kw["answered_by"] == "self" else kw["answered_by"]
            await env.put_row(mid, uid, L.APPROVED, **kw)
            broken.append(mid)
        never_asked = await env.clip(next(users), extra=99)  # a clip with no clipcast row at all

        await env.tick(PUB_AT)
        assert env.http.calls == [] and env.http.downloads == []
        assert env.guild.clips.sent == []
        # ...and the upload entry point itself refuses each of them, whatever the caller does
        for mid in [*broken, never_asked]:
            seen = await env.cog.inspect(env.guild, 300, mid)
            assert await env.cog.publish_one(env.guild, mid, seen) == "skipped"
        assert env.http.calls == [] and env.http.downloads == []
        assert env.leftovers() == []
    with_env(go, monkeypatch, tmp_path)


def test_a_private_upload_gets_no_public_link(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        await env.tick(PUB_AT)
        assert await env.status(a1) == L.PUBLISHED
        assert env.http.of("init")[0].json["status"]["privacyStatus"] == "private"
        assert env.guild.clips.sent == []
        assert "Members aren't shown the link while it's private" in env.guild.mod_log.sent[0]["content"]
        assert await economy.balance(env.db, A) == 0
        await env.tick(PUB_AT + L.PAYOUT_DAYS * DAY)  # a private upload that stays up pays like any other
        assert await economy.balance(env.db, A) == 100 and env.guild.clips.sent == []
    with_env(go, monkeypatch, tmp_path, environ={**ENV, "YOUTUBE_PRIVACY": "private"})


# ---------------------------------------------------------------- the delayed payout
async def clip_ledger(env, uid):
    rows = await env.db.fetchall("SELECT delta, reason, ref FROM ledger WHERE user_id = ? ORDER BY id", (uid,))
    return [(r["delta"], r["reason"], r["ref"]) for r in rows]


PAY_AT = PUB_AT + 7 * DAY


def test_a_clip_pays_once_after_it_has_stayed_up_a_week(monkeypatch, tmp_path):
    async def go(env):
        assert L.PAYOUT_DAYS == 7
        await env.start()
        a1 = await env.approved(A)
        await env.tick(PUB_AT)
        assert await env.status(a1) == L.PUBLISHED and await clip_ledger(env, A) == []
        await env.tick(PAY_AT - 60)  # a minute short of a week
        assert await clip_ledger(env, A) == [] and (await env.row(a1))["payout_at"] is None
        await env.tick(PAY_AT)
        assert await clip_ledger(env, A) == [(L.COINS, L.REASON, f"clipcast:{a1}")]
        assert (await env.row(a1))["payout_at"] == PAY_AT and await env.status(a1) == L.PUBLISHED
        for later in (PAY_AT + 300, PAY_AT + DAY, PAY_AT + 5 * WEEK):  # every later tick: nothing more
            await env.tick(later)
        assert await env.cog.run_payouts() == 0 and await economy.balance(env.db, A) == L.COINS
        # taking it down after it paid is the author's right and costs nothing: it had its week
        inter = await env.command("remove", A, str(a1))
        assert inter.text == "Done. The video is deleted from YouTube." and await economy.balance(env.db, A) == L.COINS
        assert not hasattr(L, "refund_ref") and not hasattr(env.cog, "reverse_payout")
    with_env(go, monkeypatch, tmp_path)


def test_a_clip_removed_on_day_six_never_pays(monkeypatch, tmp_path):
    """Post, collect, remove is not a coin tap: there is nothing to collect until it has stayed."""
    async def go(env):
        await env.start()
        by_author, by_staff, failed_delete = [await env.approved(uid) for uid in (A, B, C)]
        await env.tick(PUB_AT)
        env.t = PUB_AT + 6 * DAY
        assert (await env.command("remove", A, str(by_author))).text == "Done. The video is deleted from YouTube."
        await env.command("remove", MOD, str(by_staff), roles=[config.MOD_ROLE])
        env.http.script("delete", Response(403, {}, google_error("insufficientPermissions")))
        await env.command("remove", C, str(failed_delete))  # asked for, even though the owner must finish it
        assert [await env.status(m) for m in (by_author, by_staff, failed_delete)] == [L.REMOVED, L.REMOVED, L.REMOVAL]
        for at in (PAY_AT, PAY_AT + WEEK, PAY_AT + 10 * WEEK):
            await env.tick(at)
        for uid in (A, B, C):
            assert await clip_ledger(env, uid) == [] and await economy.balance(env.db, uid) == 0
        assert not await env.db.fetchall("SELECT 1 FROM ledger WHERE reason = ?", (L.REASON,))
    with_env(go, monkeypatch, tmp_path)


def test_a_restart_between_publish_and_payout_still_pays_exactly_once(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        await env.tick(PUB_AT)
        # the bot restarts: a new cog, nothing kept in memory, the same database
        env.cog = env.bot.cog = Clipcast(env.bot, http=env.http, environ=ENV, tmp_dir=env.tmp)
        await env.tick(PAY_AT + 3 * DAY)  # and it was down when the week ran out
        assert await clip_ledger(env, A) == [(L.COINS, L.REASON, f"clipcast:{a1}")]
        env.cog = env.bot.cog = Clipcast(env.bot, http=env.http, environ=ENV, tmp_dir=env.tmp)
        await env.tick(PAY_AT + 4 * DAY)
        # even a payout row lost by hand cannot make it pay again: the ledger ref decides
        await env.db.execute("UPDATE clipcast SET payout_at = NULL WHERE message_id = ?", (a1,))
        assert await env.cog.run_payouts() == 0
        assert await clip_ledger(env, A) == [(L.COINS, L.REASON, f"clipcast:{a1}")]
        assert len(env.http.of("put")) == 1  # and nothing was uploaded twice along the way
    with_env(go, monkeypatch, tmp_path)


def test_an_empty_wallet_changes_nothing(monkeypatch, tmp_path):
    """The hole in a clawback: spend or give the coins away first. Nothing is ever taken back
    now, so what the author's balance is on any day is irrelevant."""
    async def go(env):
        await env.start()
        await economy.apply(env.db, A, 250, "daily", T0)
        early, kept = await env.approved(A), await env.approved(B)
        await env.tick(PUB_AT)
        await economy.transfer(env.db, A, B, 250, "give", PUB_AT + 60)  # A empties their wallet
        assert await economy.balance(env.db, A) == 0
        env.t = PUB_AT + DAY
        await env.command("remove", A, str(early))
        await env.tick(PAY_AT)
        assert await economy.balance(env.db, A) == 0  # removed early: never paid, nothing to recover
        assert (L.COINS, L.REASON, f"clipcast:{kept}") in await clip_ledger(env, B)
        assert all(delta > 0 for (delta, reason, _) in await clip_ledger(env, B) if reason == L.REASON)
        assert not await env.db.fetchall("SELECT 1 FROM ledger WHERE ref LIKE '%refund%'")
    with_env(go, monkeypatch, tmp_path)


def test_the_payout_needs_a_backer_and_an_author_still_here_established_and_tracked(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        no_backer, opted_out, left = [await env.approved(uid) for uid in (A, B, C)]
        await env.db.execute("UPDATE clipcast SET backers = 0 WHERE message_id = ?", (no_backer,))
        await env.tick(PUB_AT)
        notices = {n["reference"][1]: n["content"] for n in env.guild.clips.sent}
        assert "coins" not in notices[no_backer] and "badge" in notices[no_backer]
        assert "100 coins follow once it has stayed up for 7 days" in notices[opted_out]
        # between publish and payout: one opts out, one leaves (the row is edited directly so
        # the clip itself stays published and only the payout rule is under test)
        await env.optout(B)
        del env.guild.members[C]
        env.t = PAY_AT
        assert await env.cog.run_payouts() == 0
        for uid, mid in ((A, no_backer), (B, opted_out), (C, left)):
            assert await clip_ledger(env, uid) == [], uid
            assert (await env.row(mid))["payout_at"] == PAY_AT  # settled: not looked at again
        # coming back, or opting back in, later does not reopen it
        await env.db.execute("DELETE FROM privacy_optout")
        env.member(C)
        env.t = PAY_AT + DAY
        assert await env.cog.run_payouts() == 0 and await economy.balance(env.db, B) == 0
    with_env(go, monkeypatch, tmp_path)


def test_the_payout_waits_when_discord_cannot_say_whether_the_author_is_here(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        await env.tick(PUB_AT)
        member = env.guild.members.pop(A)  # not cached, and the API is having a moment
        env.guild.fetch_fail = http_error(discord.HTTPException, 503)
        env.t = PAY_AT
        assert await env.cog.run_payouts() == 0 and (await env.row(a1))["payout_at"] is None
        env.guild.members[A] = member
        env.t = PAY_AT + 300
        assert await env.cog.run_payouts() == 1 and await economy.balance(env.db, A) == L.COINS
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- upload failures
def test_quota_stops_the_run_and_the_clip_goes_next_week_once(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        b1 = await env.approved(B)
        env.http.script("init", Response(403, {}, google_error("quotaExceeded")))
        await env.tick(PUB_AT)
        assert env.http.kinds() == ["token", "init"]  # the second clip isn't even tried
        assert len(env.http.downloads) == 1 and env.leftovers() == []
        for mid in (a1, b1):
            r = await env.row(mid)
            assert (r["status"], r["attempts"], r["video_id"], r["upload_started_at"]) == (L.APPROVED, 0, None, None)
        error = await env.meta(cogmod.ERROR_KEY)
        assert error == {"at": PUB_AT, "text": "upload start: HTTP 403 (quotaExceeded)"}
        assert (await env.meta(cogmod.RUN_KEY))["text"] == "1 quota"
        (note,) = env.guild.mod_log.sent
        assert "1 quota" in note["content"] and "do not log me" not in note["content"] and no_secrets(note["content"])
        assert env.guild.clips.sent == [] and await economy.balance(env.db, A) == 0

        await env.tick(PUB_AT + 600)  # same week: the job is done, nothing retries
        assert env.http.kinds() == ["token", "init"]
        await env.tick(PUB_AT + WEEK)
        assert env.http.kinds() == ["token", "init", "token", "init", "put", "init", "put"]
        assert await env.status(a1) == L.PUBLISHED and await env.status(b1) == L.PUBLISHED
        assert len(env.http.of("put")) == 2
    with_env(go, monkeypatch, tmp_path)


def test_a_401_refreshes_the_token_and_retries_once(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        env.http.script("init", Response(401, {}, google_error("authError", 401)))
        await env.tick(PUB_AT)
        assert env.http.kinds() == ["token", "init", "token", "init", "put"]
        assert [c.headers["Authorization"] for c in env.http.of("init")] == ["Bearer tok1", "Bearer tok2"]
        assert env.http.of("put")[0].headers["Authorization"] == "Bearer tok2"
        assert await env.status(a1) == L.PUBLISHED and len(env.http.of("put")) == 1
    with_env(go, monkeypatch, tmp_path)


def test_a_second_401_gives_up_cleanly(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        b1 = await env.approved(B)
        env.http.script("init", Response(401, {}, b""), Response(401, {}, google_error("authError", 401)))
        await env.tick(PUB_AT)
        assert env.http.kinds() == ["token", "init", "token", "init"]
        assert await env.status(a1) == L.APPROVED and (await env.row(a1))["attempts"] == 0
        assert await env.status(b1) == L.APPROVED
        assert (await env.meta(cogmod.ERROR_KEY))["text"] == "upload start: HTTP 401 (authError)"
        assert (await env.meta(cogmod.RUN_KEY))["text"] == "1 auth"
    with_env(go, monkeypatch, tmp_path)


def test_a_refused_refresh_token_uploads_nothing_and_leaks_nothing(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        env.http.script("token", Response(400, {}, body({"error": "invalid_grant",
                                                         "error_description": "r3fr3sh-value was revoked"})))
        await env.tick(PUB_AT)
        assert env.http.kinds() == ["token"]
        assert await env.status(a1) == L.APPROVED
        error = await env.meta(cogmod.ERROR_KEY)
        assert error["text"] == "Google refused the refresh token: HTTP 400 (invalid_grant)"
        inter = await env.command("status", OWNER)
        shown = inter.kwargs[-1]["embed"].description
        assert "invalid\\_grant" in shown and no_secrets(shown) and "revoked" not in shown
        assert all(no_secrets(m["content"]) for m in env.guild.mod_log.sent)
    with_env(go, monkeypatch, tmp_path)


def test_token_endpoint_trouble_is_a_clean_failure(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        env.http.script("token", asyncio.TimeoutError())
        await env.tick(PUB_AT)
        r = await env.row(a1)
        assert (r["status"], r["attempts"], r["note"]) == (L.APPROVED, 1, "token: TimeoutError")
        assert env.http.kinds() == ["token"]
    with_env(go, monkeypatch, tmp_path)


def test_an_interrupted_upload_is_retried_only_when_google_says_it_never_finished(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        for week in range(3):
            env.http.script("put", Response(503, {}, b""))  # the probe's default answer is 308: incomplete
            await env.tick(PUB_AT + week * WEEK)
            r = await env.row(a1)
            assert r["attempts"] == week + 1 and r["video_id"] is None
            assert r["status"] == (L.APPROVED if week < 2 else L.FAILED)
            assert r["note"] == "upload interrupted before it finished"
        probe = env.http.of("probe")[0]
        assert probe.url == SESSION and probe.headers["Content-Range"] == f"bytes */{len(PAYLOAD)}"
        assert env.http.kinds().count("put") == 3 and env.http.kinds().count("probe") == 3
        await env.tick(PUB_AT + 3 * WEEK)  # parked: no fourth try
        assert env.http.kinds().count("put") == 3 and await env.status(a1) == L.FAILED
        assert env.guild.clips.sent == [] and env.leftovers() == []
    with_env(go, monkeypatch, tmp_path)


def test_an_upload_that_finished_behind_an_error_is_found_not_repeated(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        env.http.script("put", asyncio.TimeoutError())
        env.http.script("probe", Response(201, {}, body({"id": "vidFOUND001"})))
        await env.tick(PUB_AT)
        r = await env.row(a1)
        assert (r["status"], r["video_id"]) == (L.PUBLISHED, "vidFOUND001")
        assert env.http.kinds() == ["token", "init", "put", "probe"]
        assert "https://youtu.be/vidFOUND001" in env.guild.clips.sent[0]["content"]
    with_env(go, monkeypatch, tmp_path)


def test_an_upload_nobody_can_vouch_for_is_parked_never_retried(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)  # put fails, and so does the status check
        b1 = await env.approved(B)  # put says 200 but the body has no usable id
        c1 = await env.approved(C)  # the session vanished
        env.http.script("put", OSError("connection reset"), Response(200, {}, b"<html>ok</html>"),
                        Response(500, {}, b""))
        env.http.script("probe", asyncio.TimeoutError(), Response(404, {}, b""))
        await env.tick(PUB_AT)
        assert [await env.status(m) for m in (a1, b1, c1)] == [L.UNSURE] * 3
        assert "status check failed (TimeoutError)" in (await env.row(a1))["note"]
        assert (await env.row(b1))["note"] == "upload: finished without a readable video id"
        assert (await env.row(c1))["note"] == "upload interrupted; status check: HTTP 404"
        assert (await env.meta(cogmod.RUN_KEY))["text"] == "3 unsure"
        alerts = [m for m in env.guild.mod_log.sent if "can't tell whether" in m["content"]]
        assert len(alerts) == 3 and alerts[0]["content"].startswith(f"<@{OWNER}> ")
        assert only_pings(alerts[0]["allowed_mentions"], OWNER)
        assert env.guild.clips.sent == [] and await economy.balance(env.db, A) == 0 and env.leftovers() == []

        puts = len(env.http.of("put"))
        await env.tick(PUB_AT + WEEK)
        await env.tick(PUB_AT + 2 * WEEK)
        assert len(env.http.of("put")) == puts and await env.status(a1) == L.UNSURE
        # the author can still ask for it to come down; staff close it with /clipcast skip
        inter = await env.command("skip", OWNER, str(b1))
        assert inter.text == "Skipped. It won't be posted." and await env.status(b1) == L.SKIPPED
    with_env(go, monkeypatch, tmp_path)


def test_a_refused_upload_request_is_a_clean_failure(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        b1 = await env.approved(B)
        env.http.script("init", Response(400, {}, google_error("invalidTitle", 400)))
        env.http.script("put", Response(400, {}, google_error("badContent", 400)))
        await env.tick(PUB_AT)
        ra, rb = await env.row(a1), await env.row(b1)
        assert (ra["status"], ra["attempts"], ra["note"]) == (L.APPROVED, 1, "upload start: HTTP 400 (invalidTitle)")
        assert (rb["status"], rb["attempts"], rb["note"]) == (L.APPROVED, 1, "upload: HTTP 400 (badContent)")
        assert env.http.of("probe") == [] and (await env.meta(cogmod.RUN_KEY))["text"] == "2 failed"
    with_env(go, monkeypatch, tmp_path)


def test_quota_during_the_transfer_also_stops_the_run(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        await env.approved(B)
        env.http.script("put", Response(403, {}, google_error("quotaExceeded")))
        await env.tick(PUB_AT)
        assert env.http.kinds() == ["token", "init", "put"]
        assert (await env.row(a1))["attempts"] == 0 and await env.status(a1) == L.APPROVED
    with_env(go, monkeypatch, tmp_path)


def test_the_token_never_goes_to_an_unexpected_upload_address(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        env.http.script("init", Response(200, {"Location": "https://evil.example/upload/youtube/v3/videos?x=1"}, b""))
        await env.tick(PUB_AT)
        assert env.http.kinds() == ["token", "init"]
        r = await env.row(a1)
        assert (r["status"], r["note"]) == (L.APPROVED, "upload start: unexpected upload address")
        assert all(urlsplit(c.url).hostname.endswith("googleapis.com") for c in env.http.calls)
    with_env(go, monkeypatch, tmp_path)


def test_a_bad_download_never_reaches_youtube(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        env.http.download_type = "text/html"
        await env.tick(PUB_AT)
        assert (await env.row(a1))["note"] == "download was not a video"
        env.http.download_type, env.http.download_status = "video/mp4", 404
        await env.tick(PUB_AT + WEEK)
        assert (await env.row(a1))["note"] == "download failed (HTTP 404)"
        env.http.download_status, env.http.download_error = 200, asyncio.TimeoutError()
        await env.tick(PUB_AT + 2 * WEEK)
        r = await env.row(a1)
        assert (r["status"], r["attempts"], r["note"]) == (L.FAILED, 3, "download failed (TimeoutError)")
        assert env.http.calls == [] and env.leftovers() == []
    with_env(go, monkeypatch, tmp_path)


def test_an_oversized_download_is_dropped(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        monkeypatch.setattr(L, "MAX_BYTES", len(PAYLOAD) - 1)  # the CDN sent more than the attachment claimed
        env.guild.clips.messages[a1].attachments[0].size = 10
        await env.tick(PUB_AT)
        assert (await env.row(a1))["note"] == "larger than the upload limit"
        assert env.http.calls == [] and env.leftovers() == []
    with_env(go, monkeypatch, tmp_path)


def test_an_upload_cut_off_by_a_restart_is_parked_for_staff(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.clip(A)
        await env.put_row(a1, A, L.UPLOADING, credit=L.NAME, answered_by=A, answered_at=T0, upload_started_at=T0,
                          attempts=1)
        await env.tick(T0 + 300)
        assert await env.status(a1) == L.UNSURE
        (alert,) = env.guild.mod_log.sent
        assert "the bot stopped in the middle of the upload" in alert["content"]
        await env.tick(PUB_AT)
        assert env.http.calls == [] and len(env.guild.mod_log.sent) == 1
    with_env(go, monkeypatch, tmp_path)


def test_consent_withdrawn_mid_upload_brings_the_video_straight_back_down(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)

        async def delete_the_clip():
            await env.cog.on_raw_message_delete(SimpleNamespace(message_id=a1))
        env.http.on_put = delete_the_clip
        await env.tick(PUB_AT)
        assert env.http.kinds() == ["token", "init", "put", "delete"]
        delete = env.http.of("delete")[0]
        assert (delete.method, delete.url, delete.params) == ("DELETE", L.VIDEOS_URL, {"id": "vid00000001"})
        r = await env.row(a1)
        assert (r["status"], r["video_id"]) == (L.REMOVED, "vid00000001")
        assert env.guild.clips.sent == [] and await economy.balance(env.db, A) == 0
        assert (await env.meta(cogmod.RUN_KEY))["text"] == "1 withdrawn"
        assert "consent was withdrawn during the upload" in env.guild.mod_log.sent[-1]["content"]
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- no YouTube credentials
def test_without_credentials_the_picks_go_to_the_mod_log_once(monkeypatch, tmp_path):
    async def go(env):
        assert not env.cog.setup.configured
        await env.start()
        a1 = await env.clip(A, reactors=[B])
        b1 = await env.clip(B, reactors=[A])
        c1 = await env.clip(C)
        await env.tick(ASK_AT)
        env.member(A).display_name = "Sam_*x*"
        env.t = ASK_AT + 50
        await env.press(A, L.NAME, a1)
        await env.press(B, L.ANON, b1)
        await env.tick(PUB_AT)
        (post,) = env.guild.mod_log.sent
        embed = post["embed"]
        assert embed.title == "This week's clipcast picks · 2026-W41"
        text = embed.description
        assert f"/channels/{GUILD_ID}/300/{a1} · <@{A}> · credited as Sam\\_\\*x\\* · yes <t:{ASK_AT + 50}:R>" in text
        assert f"/channels/{GUILD_ID}/300/{b1} · <@{B}> · anonymous" in text
        assert str(c1) not in text  # asked, never answered: not listed
        assert post["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        assert await env.status(a1) == L.HANDED and await env.status(b1) == L.HANDED
        assert env.http.calls == [] and env.http.downloads == [] and env.guild.clips.sent == []
        assert (await env.meta(cogmod.RUN_KEY))["text"] == "2 listed for posting by hand"
        await env.tick(PUB_AT + WEEK)
        assert len(env.guild.mod_log.sent) == 1  # listed once
    with_env(go, monkeypatch, tmp_path, environ={})


def test_without_credentials_and_without_consent_it_stays_silent(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.clip(A)
        b1 = await env.clip(B)
        await env.tick(ASK_AT)
        await env.press(A, L.NO, a1)
        await env.tick(PUB_AT)
        assert env.guild.mod_log.sent == [] and env.guild.clips.sent == []
        assert await env.status(a1) == L.DECLINED and await env.status(b1) == L.ASKED
        assert env.http.calls == [] and env.http.downloads == []
    with_env(go, monkeypatch, tmp_path, environ={"YOUTUBE_CLIENT_ID": "only-one-of-three"})


def test_without_a_mod_log_the_picks_wait(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        env.guild.text_channels = [env.guild.clips]
        await env.tick(PUB_AT)
        assert await env.status(a1) == L.APPROVED
        env.guild.text_channels = [env.guild.clips, env.guild.mod_log]
        await env.tick(PUB_AT + WEEK)
        assert await env.status(a1) == L.HANDED and len(env.guild.mod_log.sent) == 1
    with_env(go, monkeypatch, tmp_path, environ={})


@pytest.mark.parametrize("missing", ["mod_log", "forbidden"])
def test_picks_that_could_not_be_listed_are_listed_once_the_mod_log_is_back(monkeypatch, tmp_path, missing):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        if missing == "forbidden":  # the channel is there but Discord refuses the message
            env.guild.mod_log.fail = http_error(discord.Forbidden, 403)
        else:
            bring_back = take_away(env, "mod_log")
        for late in (0, 300):  # the Tuesday tick and the next: nowhere to list them
            await env.tick(PUB_AT + late)
        assert PUB_KEY not in await job_keys(env)  # not done: the list is still owed
        assert await env.status(a1) == L.APPROVED and env.guild.mod_log.sent == []
        assert (await env.meta(cogmod.RUN_KEY))["text"] == "picks not listed: no mod log"

        if missing == "forbidden":
            env.guild.mod_log.fail = None
        else:
            bring_back()
        await env.tick(PUB_AT + 600)
        assert PUB_KEY in await job_keys(env) and await env.status(a1) == L.HANDED
        (post,) = env.guild.mod_log.sent
        assert f"/channels/{GUILD_ID}/300/{a1}" in post["embed"].description
        for late in (900, WEEK):  # later ticks, and next week's run: listed once
            await env.tick(PUB_AT + late)
        assert len(env.guild.mod_log.sent) == 1 and env.http.calls == []
    with_env(go, monkeypatch, tmp_path, environ={})


def test_the_posting_run_waits_for_the_server_then_uploads_and_pays_once(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        bring_back = take_away(env, "guild")
        for late in (0, 300):  # the Tuesday tick and the next: no server to look at
            await env.tick(PUB_AT + late)
        assert PUB_KEY not in await job_keys(env)  # not done: the week's run is still owed
        assert await env.status(a1) == L.APPROVED and env.http.calls == [] and env.http.downloads == []

        bring_back()
        await env.tick(PUB_AT + 600)
        row = await env.row(a1)
        assert (row["status"], row["video_id"], row["published_at"], row["attempts"]) == \
               (L.PUBLISHED, "vid00000001", PUB_AT + 600, 1)
        assert PUB_KEY in await job_keys(env) and env.http.kinds() == ["token", "init", "put"]
        assert len(env.guild.clips.sent) == 1  # the "as seen on" reply
        # Later ticks upload nothing more, and the week it stays up pays once.
        for late in (900, DAY, 600 + 7 * DAY, 900 + 7 * DAY, 5 * WEEK):
            await env.tick(PUB_AT + late)
        assert len(env.http.of("put")) == 1 and len(env.http.downloads) == 1 and len(env.guild.clips.sent) == 1
        assert await clip_ledger(env, A) == [(L.COINS, L.REASON, f"clipcast:{a1}")]
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- /clipcast remove
def test_remove_before_posting_just_withdraws(monkeypatch, tmp_path):
    async def go(env):
        a1 = await env.approved(A)
        inter = await env.command("remove", A, f"https://discord.com/channels/{GUILD_ID}/300/{a1}")
        assert inter.text == "Done. It won't be posted." and await env.status(a1) == L.WITHDRAWN
        assert env.http.calls == [] and env.guild.mod_log.sent == []
        inter = await env.command("remove", A, str(a1))
        assert "Nothing to take down" in inter.text
    with_env(go, monkeypatch, tmp_path)


def test_remove_takes_a_posted_clip_down_and_tells_staff(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        await env.tick(PUB_AT)
        notice_id = (await env.row(a1))["notice_message_id"]
        env.guild.mod_log.sent.clear()

        inter = await env.command("remove", B, str(a1))  # not theirs
        assert inter.text == "That isn't one of your clips on the clipcast list."
        assert await env.status(a1) == L.PUBLISHED and env.http.of("delete") == []

        inter = await env.command("remove", A, str(a1))
        assert inter.deferred and inter.text == "Done. The video is deleted from YouTube."
        (delete,) = env.http.of("delete")
        assert delete.params == {"id": "vid00000001"} and delete.headers["Authorization"] == "Bearer tok1"
        assert await env.status(a1) == L.REMOVED
        assert env.guild.clips.deleted == [notice_id]  # the "as seen on" reply goes too
        (alert,) = env.guild.mod_log.sent
        assert f"by <@{A}> has to come down (requested by <@{A}>). I deleted the video from YouTube." in alert["content"]
        assert alert["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    with_env(go, monkeypatch, tmp_path)


def test_remove_alerts_the_owner_when_the_api_cannot_delete(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        await env.tick(PUB_AT)
        env.guild.mod_log.sent.clear()
        env.http.script("delete", Response(403, {}, google_error("insufficientPermissions")))
        inter = await env.command("remove", A, str(a1))
        assert "the owner has been told" in inter.text and await env.status(a1) == L.REMOVAL
        (alert,) = env.guild.mod_log.sent
        assert alert["content"].startswith(f"<@{OWNER}> 🧹")
        assert "delete: HTTP 403 (insufficientPermissions)" in alert["content"]
        assert "delete https://youtu.be/vid00000001 by hand" in alert["content"]
        assert only_pings(alert["allowed_mentions"], OWNER)
        # asking again tries again; a video that is already gone counts as done
        env.http.script("delete", Response(404, {}, b""))
        await env.command("remove", A, str(a1))
        assert await env.status(a1) == L.REMOVED
    with_env(go, monkeypatch, tmp_path)


def test_remove_of_a_hand_posted_clip_goes_to_the_owner(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        await env.tick(PUB_AT)
        assert await env.status(a1) == L.HANDED
        env.guild.mod_log.sent.clear()
        inter = await env.command("remove", MOD, str(a1), roles=[config.MOD_ROLE])  # staff may act for a member
        assert "the owner has been told" in inter.text and await env.status(a1) == L.REMOVAL
        assert "if it was posted by hand, take it down" in env.guild.mod_log.sent[0]["content"]
        assert env.http.calls == []
    with_env(go, monkeypatch, tmp_path, environ={})


def test_remove_during_the_upload_is_honoured_when_it_returns(monkeypatch, tmp_path):
    async def go(env):
        a1 = await env.clip(A)
        await env.put_row(a1, A, L.UPLOADING, credit=L.NAME, answered_by=A, answered_at=T0)
        inter = await env.command("remove", A, str(a1))
        assert "take it straight back down" in inter.text and a1 in env.cog.late
        assert await env.status(a1) == L.UPLOADING
    with_env(go, monkeypatch, tmp_path)


def test_remove_rejects_things_that_are_not_clips(monkeypatch, tmp_path):
    async def go(env):
        for bad in ("my last clip", "12", "https://youtu.be/abc"):
            inter = await env.command("remove", A, bad)
            assert inter.text == cogmod.NOT_A_CLIP and inter.kwargs[-1]["ephemeral"]
        inter = await env.command("remove", A, "700000000000000999")
        assert inter.text == "That isn't one of your clips on the clipcast list."
    with_env(go, monkeypatch, tmp_path)


def test_remove_autocomplete_lists_only_the_members_own_removable_clips(monkeypatch, tmp_path):
    async def go(env):
        mine = await env.approved(A)
        await env.put_row(900_000_000_000_000_001, A, L.PUBLISHED, video_id="vid00000009")
        await env.put_row(900_000_000_000_000_002, A, L.DECLINED)
        await env.approved(B)
        inter = env.interaction(A)
        found = await env.cog.remove_autocomplete(inter, "")
        assert {c.value for c in found} == {str(mine), "900000000000000001"}
        assert all(c.name.startswith("Clip from Oct 01") for c in found)
        assert [c.value for c in await env.cog.remove_autocomplete(inter, "approved")] == [str(mine)]
        assert [c.value for c in await env.cog.remove_autocomplete(inter, "0000001")] == [str(mine), "900000000000000001"]
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- staff commands
def test_status_is_staff_only_and_shows_no_secrets(monkeypatch, tmp_path):
    async def go(env):
        inter = await env.command("status", A)
        assert inter.text == cogmod.STAFF_ONLY and inter.kwargs[-1]["ephemeral"]

        a1 = await env.approved(A)
        asked = await env.clip(B)
        await env.put_row(asked, B, L.ASKED)
        stuck = await env.clip(C)
        await env.put_row(stuck, C, L.UNSURE, note="upload *interrupted*")
        await env.put_row(900_000_000_000_000_001, D, L.PUBLISHED, video_id="vid00000009")
        inter = await env.command("status", MOD, roles=[config.MOD_ROLE])
        kw = inter.kwargs[-1]
        text = kw["embed"].description
        assert kw["ephemeral"] and kw["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        assert "**YouTube:** connected; uploads are **unlisted**" in text
        assert ("**Queue:** 1 waiting for an answer; 1 approved, waiting to be posted; "
                "1 upload result unknown") in text
        assert "**Posted so far:** 1" in text and "**Last run:** never" in text and "**Last error:** none" in text
        assert f"/300/{a1} · <@{A}> · approved, waiting to be posted" in text
        assert f"/300/{stuck} · <@{C}> · upload result unknown (upload \\*interrupted\\*)" in text
        assert no_secrets(text) and "client-id" not in text
    with_env(go, monkeypatch, tmp_path)


def test_status_says_what_is_missing_by_name_only(monkeypatch, tmp_path):
    async def go(env):
        inter = await env.command("status", OWNER)
        text = inter.kwargs[-1]["embed"].description
        assert "not set up (missing `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`)" in text
        assert "listed in the mod log to post by hand" in text and "half-configured" not in text
    with_env(go, monkeypatch, tmp_path, environ={"YOUTUBE_CLIENT_ID": "half-configured"})


def test_skip_keeps_a_clip_off_youtube(monkeypatch, tmp_path):
    async def go(env):
        await env.start()
        a1 = await env.approved(A)
        b1 = await env.clip(B, extra=9)  # not shortlisted yet
        inter = await env.command("skip", A, str(a1))
        assert inter.text == cogmod.STAFF_ONLY and await env.status(a1) == L.APPROVED

        inter = await env.command("skip", MOD, f"https://discord.com/channels/{GUILD_ID}/300/{a1}",
                                  roles=[config.MOD_ROLE])
        assert inter.text == "Skipped. It won't be posted."
        r = await env.row(a1)
        assert (r["status"], r["note"]) == (L.SKIPPED, f"skipped by {MOD}")
        inter = await env.command("skip", OWNER, str(b1))
        assert inter.text == "Skipped. It won't be shortlisted." and await env.status(b1) == L.SKIPPED
        assert (await env.command("skip", OWNER, "700000000000000999")).text == "That message isn't a clip I know about."
        assert (await env.command("skip", OWNER, "that one")).text == cogmod.NOT_A_CLIP

        await env.tick(ASK_AT)
        await env.tick(PUB_AT)
        assert env.member(B).dms == [] and env.http.calls == [] and env.http.downloads == []
        assert "closed (skipped by staff)" in await env.press(A, L.NAME, a1)

        posted = 900_000_000_000_000_001
        await env.put_row(posted, C, L.PUBLISHED, video_id="vid00000009")
        inter = await env.command("skip", OWNER, str(posted))
        assert "Nothing to skip: it's posted. Use `/clipcast remove`" in inter.text
        assert await env.status(posted) == L.PUBLISHED
    with_env(go, monkeypatch, tmp_path)


# ---------------------------------------------------------------- the real HTTP client, on loopback
def test_aiohttp_client_streams_files_caps_sizes_and_follows_no_redirects(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def go():
        got = {}

        async def upload(request):
            got["body"] = await request.read()
            got["type"] = request.headers.get("Content-Type")
            got["length"] = request.headers.get("Content-Length")
            got["agent"] = request.headers.get("User-Agent")
            # 308 with a Location: a client that followed redirects would leave for /elsewhere
            return web.Response(status=308, headers={"Location": "/elsewhere", "Range": "bytes=0-9"})

        async def elsewhere(request):
            got["followed"] = True
            return web.Response(text="wrong turn")

        async def token(request):
            got["form"] = dict(await request.post())
            return web.json_response({"access_token": "t"}, headers={"X-Thing": "1"})

        async def video(request):
            return web.Response(body=b"v" * 5000, content_type="video/mp4")

        async def flood(request):
            return web.Response(body=b"x" * (cogmod.MAX_REPLY * 3))

        async def moved(request):
            raise web.HTTPFound("/video")

        app = web.Application()
        app.router.add_put("/upload", upload)
        app.router.add_route("*", "/elsewhere", elsewhere)
        app.router.add_post("/token", token)
        app.router.add_get("/video", video)
        app.router.add_get("/flood", flood)
        app.router.add_get("/moved", moved)
        server = TestServer(app, host="127.0.0.1")
        await server.start_server()
        try:
            http = AiohttpHttp()
            source = tmp_path / "clip.mp4"
            source.write_bytes(PAYLOAD)
            resp = await http.request("PUT", str(server.make_url("/upload")), file=source,
                                      headers={"Content-Type": "video/mp4", "Content-Length": str(len(PAYLOAD))})
            assert resp.status == 308 and "followed" not in got
            assert cogmod.header(resp, "location") == "/elsewhere" and cogmod.header(resp, "Missing") == ""
            assert got["body"] == PAYLOAD and got["type"] == "video/mp4" and got["length"] == str(len(PAYLOAD))
            assert got["agent"] == cogmod.USER_AGENT

            resp = await http.request("POST", str(server.make_url("/token")), data={"grant_type": "refresh_token"})
            assert resp.status == 200 and json.loads(resp.body) == {"access_token": "t"}
            assert got["form"] == {"grant_type": "refresh_token"}

            resp = await http.request("GET", str(server.make_url("/flood")))
            assert len(resp.body) == cogmod.MAX_REPLY

            dest = tmp_path / "out.mp4"
            assert await http.download(str(server.make_url("/video")), dest, 10_000) == (200, "video/mp4", 5000)
            assert dest.read_bytes() == b"v" * 5000
            small = tmp_path / "small.mp4"
            status, _, size = await http.download(str(server.make_url("/video")), small, 1000)
            assert status == 200 and size > 1000 and small.stat().st_size <= 1000  # stopped early
            redirected = tmp_path / "redirected.mp4"
            status, _, size = await http.download(str(server.make_url("/moved")), redirected, 10_000)
            assert status == 302 and size == 0 and not redirected.exists()
        finally:
            await server.close()
    asyncio.run(go())
