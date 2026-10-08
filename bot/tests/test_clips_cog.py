"""Offline tests for cogs.clips.Clips: a real in-memory SQLite database plus small fakes
for the bot, guild, channels, roles and messages. No network. Time is controlled by
patching cogs.clips.now."""

import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
from cogs import clips as cogmod
from cogs.clips import CLIP_JOB, Clips
from logic.schedule import occurrence

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
A, B, C, D, BOTUSER = 1, 2, 3, 4, 50
HOUR = 3600
DAY = 24 * HOUR


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


T0 = ts(2026, 9, 30, 12)  # Wednesday: the latest due run (Sun 9/27) predates the bot
POLL_AT = ts(2026, 10, 4, 18, 5)  # Sunday 18:05
WEEK = "2026-W40"


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")


# ---------------------------------------------------------------- fakes
class FakeRole:
    def __init__(self, rid, name, position, guild):
        self.id, self.name, self.position, self.guild = rid, name, position, guild

    def __gt__(self, other):
        return self.position > other.position

    @property
    def members(self):
        return [m for m in self.guild.members if self in m.roles]


class FakeMember:
    def __init__(self, uid, guild, bot=False):
        self.id = uid
        self.bot = bot
        self.guild = guild
        self.display_name = f"user{uid}"
        self.roles = []

    async def add_roles(self, role, reason=None):
        self.roles.append(role)
        self.guild.role_log.append(("add", self.id))

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)
        self.guild.role_log.append(("remove", self.id))


class FakeMessage:
    def __init__(self, mid, reactions=(), poll=None, content=None):
        self.id = mid
        self.reactions = [SimpleNamespace(count=n) for n in reactions]
        self.poll = poll
        self.content = content


class FakeChannel:
    def __init__(self, cid, name):
        self.id = cid
        self.name = name
        self.sent = []
        self.messages = {}
        self.next_id = cid * 1000

    async def send(self, content=None, **kwargs):
        self.next_id += 1
        msg = FakeMessage(self.next_id, poll=kwargs.get("poll"), content=content)
        self.messages[msg.id] = msg
        self.sent.append(dict(content=content, id=msg.id, **kwargs))
        return msg

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise not_found()
        return self.messages[mid]


class FakeThread(FakeChannel):
    def __init__(self, cid, parent):
        super().__init__(cid, "a clip thread")
        self.parent = parent


class FakeAnswer:
    """`votes`: a count (that many distinct established voters) or a list of voter ids."""

    def __init__(self, number, votes):
        self.id = number
        ids = votes if isinstance(votes, list) else [10_000 + 100 * number + i for i in range(votes)]
        self.vote_count = len(ids)
        self._voters = [SimpleNamespace(id=v, bot=False) for v in ids]

    async def voters(self, limit=None):
        for v in self._voters:
            yield v


class FakePoll:
    def __init__(self, votes, finalized=True):
        self.answers = [FakeAnswer(k, v) for k, v in votes.items()]
        self.finalized = finalized

    def is_finalized(self):
        return self.finalized


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.clips = FakeChannel(300, config.CLIPS_CHANNEL)
        self.mod = FakeChannel(301, config.MOD_CHANNEL)
        self.general = FakeChannel(302, config.GENERAL_CHANNEL)
        self.thread = FakeThread(310, self.clips)
        self.text_channels = [self.mod, self.general, self.clips]
        self.members = []
        self.role_log = []
        self.clip_role = FakeRole(70, config.CLIP_ROLE, 5, self)
        self.roles = [self.clip_role]
        self.me = SimpleNamespace(top_role=FakeRole(71, "Front Desk", 10, self))

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    async def fetch_member(self, uid):
        raise not_found()

    def get_channel_or_thread(self, cid):
        return next((c for c in self.text_channels + [self.thread] if c.id == cid), None)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)
        self.dispatched = []

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None

    def dispatch(self, event, *args):
        self.dispatched.append((event, args))


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0
        self.next_mid = 5000

    def member(self, uid, **kw):
        m = self.guild.get_member(uid)
        if m is None:
            m = FakeMember(uid, self.guild, **kw)
            self.guild.members.append(m)
        return m

    async def post(self, uid, content="", attachments=(), channel=None, at=None, guild="default", reactions=()):
        """A message arriving; also stored in its channel so it can be fetched later."""
        if at is not None:
            self.t = at
        self.next_mid += 1
        channel = channel or self.guild.clips
        msg = SimpleNamespace(
            id=self.next_mid, author=self.member(uid), channel=channel, content=content,
            guild=self.guild if guild == "default" else guild,
            attachments=[SimpleNamespace(url=u, content_type=ct) for u, ct in attachments],
            created_at=datetime.fromtimestamp(self.t, timezone.utc))
        channel.messages[msg.id] = FakeMessage(msg.id, reactions=reactions)
        await self.cog.on_message(msg)
        return msg.id

    async def clips(self):
        return [dict(r) for r in await self.db.fetchall("SELECT * FROM clips ORDER BY posted_at, message_id")]

    async def polls(self):
        return [dict(r) for r in await self.db.fetchall("SELECT * FROM clip_polls ORDER BY week")]

    async def jobs(self):
        return {r["key"] for r in await self.db.fetchall("SELECT key FROM jobs")}

    async def optout(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    async def first_run(self):
        self.t = T0
        await self.cog.run_weekly()

    def set_votes(self, votes, finalized=True):
        """Replace the posted poll message with a fetched one carrying results."""
        mid = self.poll_id
        self.guild.clips.messages[mid] = FakeMessage(mid, poll=FakePoll(votes, finalized))


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        cog = Clips(bot)
        env = Env(db, guild, bot, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    asyncio.run(go())


def clip_key(y, m, d):
    return occurrence(CLIP_JOB, date(y, m, d), TZ).key


async def week_of_clips(env, uids, reactions=None):
    """One clip per uid, posted an hour apart starting Thursday 10/1."""
    ids = []
    for i, uid in enumerate(uids):
        r = (reactions[i],) if reactions else ()
        ids.append(await env.post(uid, f"https://youtu.be/clip{i}", at=ts(2026, 10, 1, 10) + i * HOUR, reactions=r))
    return ids


# ---------------------------------------------------------------- collecting
def test_collects_links_and_video_attachments(monkeypatch):
    async def go(env):
        m1 = await env.post(A, "check this https://medal.tv/games/x/clips/abc")
        m2 = await env.post(B, "", attachments=[("https://cdn/pic.png", "image/png"), ("https://cdn/v.mp4", "video/mp4")])
        m3 = await env.post(C, "https://clips.twitch.tv/Funny", channel=env.guild.thread)
        rows = await env.clips()
        assert [(r["message_id"], r["user_id"], r["url"], r["posted_at"]) for r in rows] == [
            (m1, A, "https://medal.tv/games/x/clips/abc", T0),
            (m2, B, "https://cdn/v.mp4", T0),
            (m3, C, "https://clips.twitch.tv/Funny", T0),
        ]
        row = await env.db.fetchone("SELECT value FROM meta WHERE key = ?", (f"clip_channel:{m3}",))
        assert int(row["value"]) == env.guild.thread.id
    with_env(go, monkeypatch)


def test_ignores_non_clips_other_channels_bots_dms(monkeypatch):
    async def go(env):
        env.member(BOTUSER, bot=True)
        await env.post(A, "gg everyone")
        await env.post(A, "", attachments=[("https://cdn/pic.png", "image/png")])
        await env.post(A, "https://www.twitch.tv/someone")  # channel link, not a clip
        await env.post(A, "https://youtu.be/x", channel=env.guild.general)
        await env.post(BOTUSER, "https://youtu.be/x")
        await env.post(A, "https://youtu.be/x", guild=None)
        await env.post(A, "https://youtu.be/x", guild=SimpleNamespace(id=12345))
        assert await env.clips() == []
    with_env(go, monkeypatch)


def test_privacy_gate(monkeypatch):
    async def go(env):
        await env.optout(A)
        await env.post(A, "https://youtu.be/x")
        assert await env.clips() == []
        await env.post(B, "https://youtu.be/y")
        assert [r["user_id"] for r in await env.clips()] == [B]
    with_env(go, monkeypatch)


def test_delete_removes_row(monkeypatch):
    async def go(env):
        m1 = await env.post(A, "https://youtu.be/x")
        m2 = await env.post(B, "https://youtu.be/y", channel=env.guild.thread)
        m3 = await env.post(C, "https://youtu.be/z")
        await env.cog.on_raw_message_delete(SimpleNamespace(message_id=m1))
        await env.cog.on_raw_message_delete(SimpleNamespace(message_id=424242))  # not a clip: fine
        assert [r["message_id"] for r in await env.clips()] == [m2, m3]
        await env.cog.on_raw_bulk_message_delete(SimpleNamespace(message_ids={m2, m3}))
        assert await env.clips() == []
        assert await env.db.fetchone("SELECT 1 FROM meta WHERE key = ?", (f"clip_channel:{m2}",)) is None
    with_env(go, monkeypatch)


def test_listener_never_raises(monkeypatch):
    async def go(env):
        await env.db.close()  # every query now fails
        await env.cog.on_message(SimpleNamespace(
            id=1, author=env.member(A), guild=env.guild, channel=env.guild.clips, content="https://youtu.be/x",
            attachments=[], created_at=datetime.now(timezone.utc)))
        await env.cog.on_raw_message_delete(SimpleNamespace(message_id=1))
        await Clips.weekly.coro(env.cog)
        await Clips.winners.coro(env.cog)
        await env.db.connect()  # so teardown can close it
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- weekly poll
def test_weekly_first_run_marks_done_and_posts_nothing(monkeypatch):
    async def go(env):
        await env.post(A, "https://youtu.be/a", at=ts(2026, 9, 26, 10))
        await env.post(B, "https://youtu.be/b", at=ts(2026, 9, 26, 11))
        await env.first_run()
        assert await env.jobs() == {clip_key(2026, 9, 27)}
        assert env.guild.clips.sent == [] and await env.polls() == []
    with_env(go, monkeypatch)


def test_poll_posted_once_with_answers_links_and_storage(monkeypatch):
    async def go(env):
        await env.first_run()
        before = await env.post(D, "https://youtu.be/old", at=ts(2026, 9, 27, 18, 0))  # previous window
        ids = await week_of_clips(env, [A, B, C])
        env.member(B).display_name = "B" * 80
        late = await env.post(D, "https://youtu.be/late", at=POLL_AT)  # next window
        env.t = ts(2026, 10, 4, 18, 4)
        await env.cog.run_weekly()
        assert env.guild.clips.sent == []
        env.t = POLL_AT
        await env.cog.run_weekly()
        env.t = POLL_AT + 300
        await env.cog.run_weekly()  # no repost
        assert len(env.guild.clips.sent) == 1
        sent = env.guild.clips.sent[0]
        poll = sent["poll"]
        assert poll.question == "Clip of the week?"
        assert poll.duration.total_seconds() == 24 * HOUR
        texts = [a.text for a in poll.answers]
        assert texts[0] == "#1 · user1" and texts[2] == "#3 · user3"
        assert len(texts[1]) == 55 and texts[1].startswith("#2 · BBB")
        for n, mid in enumerate(ids, start=1):
            assert f"`#{n}` https://discord.com/channels/{GUILD_ID}/{env.guild.clips.id}/{mid}" in sent["content"]
        assert str(before) not in sent["content"] and str(late) not in sent["content"]
        am = sent["allowed_mentions"]
        assert (am.everyone, am.users, am.roles) == (False, False, False)
        assert await env.polls() == [dict(week=WEEK, message_id=sent["id"], ends_at=POLL_AT + 24 * HOUR,
                                          winner_id=None, done=0)]
        assert clip_key(2026, 10, 4) in await env.jobs()
    with_env(go, monkeypatch)


def test_thread_clip_jump_link_uses_thread(monkeypatch):
    async def go(env):
        await env.first_run()
        await env.post(A, "https://youtu.be/a", at=ts(2026, 10, 1, 10))
        mid = await env.post(B, "https://youtu.be/b", channel=env.guild.thread, at=ts(2026, 10, 1, 11))
        env.t = POLL_AT
        await env.cog.run_weekly()
        assert f"/{env.guild.thread.id}/{mid}" in env.guild.clips.sent[0]["content"]
    with_env(go, monkeypatch)


def test_fewer_than_two_clips_no_post_but_done(monkeypatch):
    async def go(env):
        await env.first_run()
        await week_of_clips(env, [A])
        env.t = POLL_AT
        await env.cog.run_weekly()
        assert env.guild.clips.sent == [] and await env.polls() == []
        assert clip_key(2026, 10, 4) in await env.jobs()
    with_env(go, monkeypatch)


def test_more_than_ten_picks_top_ten_by_reactions(monkeypatch):
    async def go(env):
        await env.first_run()
        uids = list(range(101, 113))  # 12 clips
        reactions = [5, 9, 0, 5, 7, 5, 1, 3, 2, 8, 0, 5]
        ids = await week_of_clips(env, uids, reactions)
        env.t = POLL_AT
        await env.cog.run_weekly()
        sent = env.guild.clips.sent[0]
        assert len(sent["poll"].answers) == 10
        dropped = {ids[2], ids[10]}
        kept = [m for m in ids if m not in dropped]
        positions = [sent["content"].index(f"/{m}") for m in kept]
        assert positions == sorted(positions)  # still numbered in posting order
        for m in dropped:
            assert f"/{m}" not in sent["content"]
        stored = json.loads((await env.db.fetchone("SELECT value FROM meta WHERE key = ?",
                                                   (f"clip_poll:{WEEK}",)))["value"])
        assert [e["message_id"] for e in stored] == kept
    with_env(go, monkeypatch)


def test_more_than_ten_deleted_offline_clip_dropped(monkeypatch):
    async def go(env):
        await env.first_run()
        ids = await week_of_clips(env, list(range(101, 112)), [1] * 11)  # 11 clips
        del env.guild.clips.messages[ids[0]]  # deleted while the bot was off
        env.t = POLL_AT
        await env.cog.run_weekly()
        assert len(env.guild.clips.sent[0]["poll"].answers) == 10
        assert ids[0] not in [r["message_id"] for r in await env.clips()]
    with_env(go, monkeypatch)


def test_small_week_deleted_offline_clip_dropped(monkeypatch):
    async def go(env):
        await env.first_run()
        ids = await week_of_clips(env, [A, B, C])  # fits in one poll: no ranking needed
        del env.guild.clips.messages[ids[0]]  # deleted while the bot was off
        env.t = POLL_AT
        await env.cog.run_weekly()
        sent = env.guild.clips.sent[0]
        assert len(sent["poll"].answers) == 2
        assert f"/{ids[0]}" not in sent["content"]
        stored = json.loads((await env.db.fetchone("SELECT value FROM meta WHERE key = ?",
                                                   (f"clip_poll:{WEEK}",)))["value"])
        assert [e["message_id"] for e in stored] == ids[1:]
        assert ids[0] not in [r["message_id"] for r in await env.clips()]
    with_env(go, monkeypatch)


def test_poll_send_failure_retries_next_tick(monkeypatch):
    async def go(env):
        await env.first_run()
        await week_of_clips(env, [A, B])
        real_send = env.guild.clips.send

        async def boom(*a, **kw):
            raise discord.HTTPException(SimpleNamespace(status=500, reason="err"), "down")
        env.guild.clips.send = boom
        env.t = POLL_AT
        await Clips.weekly.coro(env.cog)  # logged, not raised
        assert clip_key(2026, 10, 4) not in await env.jobs()
        env.guild.clips.send = real_send
        env.t = POLL_AT + 300
        await env.cog.run_weekly()
        assert len(env.guild.clips.sent) == 1 and clip_key(2026, 10, 4) in await env.jobs()
    with_env(go, monkeypatch)


def test_restart_after_post_before_marking_done_does_not_repost(monkeypatch):
    async def go(env):
        await env.first_run()
        await week_of_clips(env, [A, B])
        env.t = POLL_AT
        await env.cog.run_weekly()
        # simulate a crash between storing the poll and marking the job done
        await env.db.execute("DELETE FROM jobs WHERE key = ?", (clip_key(2026, 10, 4),))
        restarted = Clips(env.bot)
        env.t = POLL_AT + 600
        await restarted.run_weekly()
        assert len(env.guild.clips.sent) == 1
        assert clip_key(2026, 10, 4) in await env.jobs()
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- winner
async def polled(env, uids=(A, B, C)):
    await env.first_run()
    await week_of_clips(env, list(uids))
    env.t = POLL_AT
    await env.cog.run_weekly()
    env.poll_id = env.guild.clips.sent[0]["id"]
    env.guild.clips.sent.clear()


def test_winner_announced_role_swapped_and_dispatched(monkeypatch):
    async def go(env):
        await polled(env)
        previous = env.member(D)
        previous.roles.append(env.guild.clip_role)
        env.set_votes({1: 2, 2: 5, 3: 1})
        env.t = POLL_AT + 24 * HOUR - 60
        await env.cog.check_polls()  # not over yet
        assert env.guild.clips.sent == []
        env.t = POLL_AT + 24 * HOUR
        await env.cog.check_polls()
        await env.cog.check_polls()  # done: nothing twice
        assert env.guild.role_log == [("remove", D), ("add", B)]
        assert env.guild.clip_role.members == [env.member(B)]
        (sent,) = env.guild.clips.sent
        assert f"<@{B}>" in sent["content"] and "#2" in sent["content"]
        stored = json.loads((await env.db.fetchone("SELECT value FROM meta WHERE key = ?",
                                                   (f"clip_poll:{WEEK}",)))["value"])
        assert f"https://discord.com/channels/{GUILD_ID}/{env.guild.clips.id}/{stored[1]['message_id']}" \
            in sent["content"]
        assert [o.id for o in sent["allowed_mentions"].users] == [B]
        assert sent["allowed_mentions"].everyone is False and sent["allowed_mentions"].roles is False
        assert env.bot.dispatched == [("clip_of_the_week", (WEEK, B))]
        (poll,) = await env.polls()
        assert poll["done"] == 1 and poll["winner_id"] == B
        assert env.guild.mod.sent == []
    with_env(go, monkeypatch)


def test_winner_tie_goes_to_earliest_and_keeps_role(monkeypatch):
    async def go(env):
        await polled(env)
        env.member(A).roles.append(env.guild.clip_role)  # last week's winner wins again
        env.set_votes({1: 4, 2: 1, 3: 4})
        env.t = POLL_AT + 24 * HOUR
        await env.cog.check_polls()
        assert env.bot.dispatched == [("clip_of_the_week", (WEEK, A))]
        assert env.guild.role_log == []  # already had it
    with_env(go, monkeypatch)


def test_zero_votes_no_winner_role_untouched(monkeypatch):
    async def go(env):
        await polled(env)
        env.member(D).roles.append(env.guild.clip_role)
        env.set_votes({1: 0, 2: 0, 3: 0})
        env.t = POLL_AT + 24 * HOUR
        await env.cog.check_polls()
        assert env.guild.clips.sent == [] and env.bot.dispatched == [] and env.guild.role_log == []
        (poll,) = await env.polls()
        assert poll["done"] == 1 and poll["winner_id"] is None
    with_env(go, monkeypatch)


def test_not_finalized_retries_then_gives_up_after_48h(monkeypatch):
    async def go(env):
        await polled(env)
        env.set_votes({1: 3}, finalized=False)
        ends = POLL_AT + 24 * HOUR
        env.t = ends + 5 * 60
        await env.cog.check_polls()
        assert (await env.polls())[0]["done"] == 0
        env.t = ends + 48 * HOUR
        await env.cog.check_polls()
        assert (await env.polls())[0]["done"] == 0
        env.t = ends + 48 * HOUR + 300
        await env.cog.check_polls()
        (poll,) = await env.polls()
        assert poll["done"] == 1 and poll["winner_id"] is None
        assert env.guild.clips.sent == [] and env.bot.dispatched == []
    with_env(go, monkeypatch)


def test_finalized_on_a_later_tick_wins(monkeypatch):
    async def go(env):
        await polled(env)
        env.set_votes({3: 2}, finalized=False)
        env.t = POLL_AT + 24 * HOUR
        await env.cog.check_polls()
        env.set_votes({3: 2}, finalized=True)
        env.t += 300
        await env.cog.check_polls()
        assert env.bot.dispatched == [("clip_of_the_week", (WEEK, C))]
    with_env(go, monkeypatch)


def test_poll_message_deleted_marks_done(monkeypatch):
    async def go(env):
        await polled(env)
        env.guild.clips.messages.clear()
        env.t = POLL_AT + 24 * HOUR
        await env.cog.check_polls()
        assert (await env.polls())[0]["done"] == 1 and env.bot.dispatched == []
    with_env(go, monkeypatch)


def test_role_missing_notes_mods_once_per_day_still_announces(monkeypatch):
    async def go(env):
        await polled(env)
        env.guild.roles = []
        env.set_votes({1: 1})
        env.t = POLL_AT + 24 * HOUR
        await env.cog.check_polls()
        assert len(env.guild.clips.sent) == 1 and env.bot.dispatched == [("clip_of_the_week", (WEEK, A))]
        assert env.guild.role_log == []
        (note,) = env.guild.mod.sent
        assert "no `Clip of the Week` role" in note["content"]
        await env.cog.on_ready()  # same local day: no second note
        assert len(env.guild.mod.sent) == 1
        env.t += DAY
        await env.cog.on_ready()  # next day: noted again at startup
        assert len(env.guild.mod.sent) == 2
    with_env(go, monkeypatch)



def test_role_above_bot_not_handed_out_still_announces(monkeypatch):
    async def go(env):
        await polled(env)
        env.guild.clip_role.position = 20  # above Front Desk's top role
        env.member(D).roles.append(env.guild.clip_role)
        env.set_votes({2: 1})
        env.t = POLL_AT + 24 * HOUR
        await env.cog.check_polls()
        assert env.guild.role_log == []
        assert env.member(D).roles == [env.guild.clip_role]
        assert "has to be above" in env.guild.mod.sent[0]["content"]
        assert env.bot.dispatched == [("clip_of_the_week", (WEEK, B))]
        assert len(env.guild.clips.sent) == 1
    with_env(go, monkeypatch)


def test_role_ok_at_startup_sends_no_note(monkeypatch):
    async def go(env):
        await env.cog.on_ready()
        assert env.guild.mod.sent == []
    with_env(go, monkeypatch)


def test_restart_idempotency_full_cycle(monkeypatch):
    """A new cog instance (restart) neither reposts the poll nor re-announces the winner."""
    async def go(env):
        await polled(env)
        env.set_votes({1: 1, 2: 3})
        env.t = POLL_AT + 24 * HOUR
        await env.cog.check_polls()
        restarted = Clips(env.bot)
        env.t += 600
        await restarted.run_weekly()
        await restarted.check_polls()
        assert len(env.guild.clips.sent) == 1
        assert env.bot.dispatched == [("clip_of_the_week", (WEEK, B))]
    with_env(go, monkeypatch)


def test_announce_failure_retries_next_tick(monkeypatch):
    async def go(env):
        await polled(env)
        env.set_votes({1: 2})
        real_send = env.guild.clips.send

        async def boom(*a, **kw):
            raise discord.HTTPException(SimpleNamespace(status=500, reason="err"), "down")
        env.guild.clips.send = boom
        env.t = POLL_AT + 24 * HOUR
        await Clips.winners.coro(env.cog)
        assert (await env.polls())[0]["done"] == 0
        env.guild.clips.send = real_send
        env.t += 300
        await env.cog.check_polls()
        assert (await env.polls())[0]["winner_id"] == A
        assert env.bot.dispatched == [("clip_of_the_week", (WEEK, A))]
    with_env(go, monkeypatch)


def test_votes_from_fresh_accounts_and_the_author_dont_count(monkeypatch):
    from logic import quests as Q

    async def go(env):
        await polled(env)
        stored = json.loads((await env.db.fetchone("SELECT value FROM meta WHERE key = ?",
                                                   (f"clip_poll:{WEEK}",)))["value"])
        author1 = stored[0]["user_id"]
        ends = POLL_AT + 24 * HOUR
        alts = [(((ends - 2 * 24 * HOUR) * 1000 - Q.DISCORD_EPOCH_MS) << 22) + i for i in range(3)]
        assert not any(Q.established(a, ends) for a in alts)
        env.set_votes({1: [*alts, author1], 2: [20_001], 3: []})
        env.t = ends
        await env.cog.check_polls()
        assert env.bot.dispatched == [("clip_of_the_week", (WEEK, stored[1]["user_id"]))]
    with_env(go, monkeypatch)
