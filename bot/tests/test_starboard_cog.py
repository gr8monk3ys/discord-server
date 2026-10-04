"""Offline integration tests for cogs.starboard.Starboard: a real in-memory SQLite
database plus lightweight fakes for the bot, guild, channels, messages and
reactions. No network. Time is controlled by patching cogs.starboard.now."""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

import config
import db as dbmod
from cogs import starboard as cogmod
from cogs.starboard import Starboard
from logic import starboard as S

GUILD_ID = 999
AUTHOR, A, B, C, D, BOTUSER = 1, 11, 12, 13, 14, 50
T0 = 1_790_000_000
DAY = 24 * 60 * 60
STAR = discord.PartialEmoji(name="⭐")


def run(coro):
    return asyncio.run(coro)


def http_error(cls=discord.HTTPException, status=500):
    return cls(SimpleNamespace(status=status, reason="boom"), "boom")


_seq = 0


def snowflake(ts: float) -> int:
    """A message id created at `ts` (unique: the low bits are a counter)."""
    global _seq
    _seq += 1
    return discord.utils.time_snowflake(datetime.fromtimestamp(ts, tz=timezone.utc)) + _seq


# ---------------------------------------------------------------- fakes
class FakeUser:
    def __init__(self, uid, bot=False):
        self.id = uid
        self.bot = bot
        self.name = f"user{uid}"
        self.display_name = f"User {uid}"
        self.display_avatar = SimpleNamespace(url=f"https://cdn/avatars/{uid}.png")
        self.mention = f"<@{uid}>"


class FakeReaction:
    def __init__(self, emoji="⭐"):
        self.emoji = emoji
        self.reactors = []

    async def users(self, limit=None):
        for u in list(self.reactors):
            await asyncio.sleep(0)  # yield, like a real paginated fetch
            yield u


class FakeMessage:
    def __init__(self, channel, author, content="", created=None, attachments=(), embeds=()):
        self.id = snowflake(created if created is not None else T0 - 60)
        self.channel = channel
        self.author = author
        self.content = content
        self.attachments = list(attachments)
        self.embeds = list(embeds)
        self.reactions = []
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{channel.id}/{self.id}"
        self.created_at = discord.utils.snowflake_time(self.id)

    def reaction(self, emoji="⭐"):
        r = next((r for r in self.reactions if r.emoji == emoji), None)
        if r is None:
            r = FakeReaction(emoji)
            self.reactions.append(r)
        return r


class FakePartial:
    def __init__(self, channel, mid):
        self.channel = channel
        self.id = mid

    async def edit(self, **kwargs):
        await asyncio.sleep(0)
        if self.channel.edit_fail is not None:
            raise self.channel.edit_fail
        if self.id not in self.channel.posted:
            raise http_error(discord.NotFound, 404)
        self.channel.edits.append((self.id, kwargs))

    async def delete(self):
        await asyncio.sleep(0)
        if self.id not in self.channel.posted:
            raise http_error(discord.NotFound, 404)
        self.channel.posted.discard(self.id)


class FakeText:
    _next = 300

    def __init__(self, name, category=None, nsfw=False, public=True):
        FakeText._next += 1
        self.public = public  # can @everyone see it
        self.id = FakeText._next
        self.name = name
        self.category = category
        self.nsfw = nsfw
        self.mention = f"<#{self.id}>"
        self.messages = {}
        self.sent = []
        self.posted = set()
        self.edits = []
        self.send_fail = None
        self.edit_fail = None
        self.fetches = 0

    def is_nsfw(self):
        return self.nsfw

    def permissions_for(self, target):
        return SimpleNamespace(view_channel=self.public)

    async def fetch_message(self, mid):
        self.fetches += 1
        await asyncio.sleep(0)
        if mid not in self.messages:
            raise http_error(discord.NotFound, 404)
        return self.messages[mid]

    async def send(self, content=None, **kwargs):
        await asyncio.sleep(0)
        if self.send_fail is not None:
            raise self.send_fail
        self.sent.append(dict(content=content, **kwargs))
        mid = 70_000 + len(self.sent)
        self.posted.add(mid)
        return SimpleNamespace(id=mid)

    def get_partial_message(self, mid):
        return FakePartial(self, mid)


class FakeThread(FakeText):
    def __init__(self, name, parent, private=False):
        super().__init__(name)
        self.parent = parent
        self.private = private

    def is_private(self):
        return self.private

    def is_nsfw(self):
        return self.parent.is_nsfw()


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.staff = SimpleNamespace(id=1, name=config.STAFF_CATEGORY)
        self.chat = SimpleNamespace(id=2, name="01 · chat")
        self.general = FakeText("💬・general", category=self.chat)
        self.hall = FakeText(config.HALL_OF_FAME_CHANNEL, category=self.chat)
        self.mod = FakeText("🛡️・mod", category=self.staff)
        self.spicy = FakeText("🌶️・spicy", category=self.chat, nsfw=True)
        self.squad = FakeText("squad-chat", category=self.chat, public=False)
        self.text_channels = [self.general, self.hall, self.mod, self.spicy, self.squad]
        self.threads = []
        self.default_role = SimpleNamespace(id=self.id, name="@everyone")

    def thread(self, name, parent, private=False):
        t = FakeThread(name, parent, private)
        self.threads.append(t)
        return t

    def get_channel_or_thread(self, cid):
        return next((c for c in self.text_channels + self.threads if c.id == cid), None)

    async def fetch_channel(self, cid):
        raise http_error(discord.NotFound, 404)


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0
        self.users = {}

    def user(self, uid, bot=False):
        if uid not in self.users:
            self.users[uid] = FakeUser(uid, bot=bot)
        return self.users[uid]

    def message(self, channel=None, author=AUTHOR, **kw):
        channel = channel or self.guild.general
        m = FakeMessage(channel, self.user(author), **kw)
        channel.messages[m.id] = m
        return m

    def payload(self, message, uid, emoji=STAR, guild_id=GUILD_ID):
        return SimpleNamespace(guild_id=guild_id, channel_id=message.channel.id, message_id=message.id,
                               user_id=uid, emoji=emoji)

    async def star(self, message, *uids):
        for uid in uids:
            message.reaction().reactors.append(self.user(uid))
            await self.cog.on_raw_reaction_add(self.payload(message, uid))

    async def unstar(self, message, *uids):
        for uid in uids:
            r = message.reaction()
            r.reactors = [u for u in r.reactors if u.id != uid]
            await self.cog.on_raw_reaction_remove(self.payload(message, uid))

    async def rows(self):
        return [dict(r) for r in await self.db.fetchall("SELECT * FROM starboard ORDER BY rowid")]


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        bot = FakeBot(db, guild)
        env = Env(db, guild, bot, Starboard(bot))
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        env.user(BOTUSER, bot=True)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


def posts(env):
    return [s["embed"] for s in env.guild.hall.sent]


def last_edit_embed(env):
    return env.guild.hall.edits[-1][1]["embed"]


# ---------------------------------------------------------------- posting
def test_posts_once_at_threshold_with_author_text_and_jump_button(monkeypatch):
    async def go(env):
        m = env.message(content="that clutch was unreal")
        await env.star(m, A, B)
        assert env.guild.hall.sent == []
        await env.star(m, C)
        (sent,) = env.guild.hall.sent
        e = sent["embed"]
        assert e.author.name == "User 1" and e.author.icon_url == "https://cdn/avatars/1.png"
        assert e.description == "that clutch was unreal"
        assert e.footer.text == "#💬・general · ⭐ 3"
        assert e.image.url is None
        (button,) = sent["view"].children
        assert button.style is discord.ButtonStyle.link and button.url == m.jump_url
        assert button.label == "Jump to message"
        (row,) = await env.rows()
        assert row["message_id"] == m.id and row["channel_id"] == env.guild.general.id
        assert row["author_id"] == AUTHOR and row["board_message_id"] == 70_001 and row["stars"] == 3
        assert row["at"] == T0
    with_env(go, monkeypatch)


def test_author_self_star_and_bots_do_not_reach_threshold(monkeypatch):
    async def go(env):
        m = env.message(content="hi")
        await env.star(m, AUTHOR, BOTUSER, A, B)
        assert env.guild.hall.sent == [] and await env.rows() == []
        await env.star(m, C)
        assert posts(env)[0].footer.text.endswith("⭐ 3")
    with_env(go, monkeypatch)


def test_other_emoji_and_other_guilds_are_ignored(monkeypatch):
    async def go(env):
        m = env.message(content="hi")
        for uid in (A, B, C):
            m.reaction().reactors.append(env.user(uid))
        await env.cog.on_raw_reaction_add(env.payload(m, A, emoji=discord.PartialEmoji(name="🔥")))
        await env.cog.on_raw_reaction_add(env.payload(m, A, emoji=discord.PartialEmoji(name="⭐", id=42)))
        await env.cog.on_raw_reaction_add(env.payload(m, A, guild_id=1234))
        assert env.guild.general.fetches == 0 and env.guild.hall.sent == []
    with_env(go, monkeypatch)


def test_concurrent_adds_post_exactly_once(monkeypatch):
    async def go(env):
        m = env.message(content="burst")
        uids = [A, B, C, D, 15, 16]
        m.reaction().reactors.extend(env.user(u) for u in uids)
        await asyncio.gather(*(env.cog.on_raw_reaction_add(env.payload(m, u)) for u in uids))
        assert len(env.guild.hall.sent) == 1
        (row,) = await env.rows()
        assert row["stars"] == 6 and row["board_message_id"] == 70_001
    with_env(go, monkeypatch)


def test_db_claim_alone_prevents_double_post(monkeypatch):
    """Even bypassing the per-message lock, the upsert lets only one post through."""
    async def go(env):
        m = env.message(content="race")
        m.reaction().reactors.extend(env.user(u) for u in (A, B, C))
        await asyncio.gather(*(env.cog.refresh(GUILD_ID, m.channel.id, m.id) for _ in range(5)))
        assert len(env.guild.hall.sent) == 1
        (row,) = await env.rows()
        assert row["board_message_id"] == 70_001
    with_env(go, monkeypatch)


def test_locks_are_released_after_use(monkeypatch):
    async def go(env):
        m = env.message(content="x")
        await env.star(m, A, B, C)
        import gc
        gc.collect()
        assert m.id not in env.cog.locks
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- updates
def test_count_updates_on_add_and_remove(monkeypatch):
    async def go(env):
        m = env.message(content="gg")
        await env.star(m, A, B, C)
        await env.star(m, D)
        assert len(env.guild.hall.sent) == 1
        mid, kwargs = env.guild.hall.edits[-1]
        assert mid == 70_001 and kwargs["embed"].footer.text == "#💬・general · ⭐ 4"
        assert kwargs["view"].children[0].url == m.jump_url
        assert (await env.rows())[0]["stars"] == 4
        await env.unstar(m, A)
        assert last_edit_embed(env).footer.text.endswith("⭐ 3")
        assert (await env.rows())[0]["stars"] == 3
    with_env(go, monkeypatch)


def test_below_threshold_keeps_post_and_shows_count(monkeypatch):
    async def go(env):
        m = env.message(content="gg")
        await env.star(m, A, B, C)
        await env.unstar(m, A, B, C)
        assert len(env.guild.hall.sent) == 1  # never reposted, never deleted
        assert last_edit_embed(env).footer.text.endswith("⭐ 0")
        (row,) = await env.rows()
        assert row["stars"] == 0 and row["board_message_id"] == 70_001
        await env.star(m, A, B, C)  # back up: still the same post, just updated
        assert len(env.guild.hall.sent) == 1
        assert last_edit_embed(env).footer.text.endswith("⭐ 3")
    with_env(go, monkeypatch)


def test_unchanged_count_does_not_edit(monkeypatch):
    async def go(env):
        m = env.message(content="gg")
        await env.star(m, A, B, C)
        await env.star(m, AUTHOR)  # self-star: count stays 3
        assert env.guild.hall.edits == []
    with_env(go, monkeypatch)


def test_deleted_board_post_is_not_reposted(monkeypatch):
    async def go(env):
        m = env.message(content="gg")
        await env.star(m, A, B, C)
        env.guild.hall.posted.clear()  # a mod deleted the hall post
        await env.star(m, D)
        assert len(env.guild.hall.sent) == 1
        assert (await env.rows())[0]["stars"] == 4
    with_env(go, monkeypatch)


def test_failed_edit_is_retried_on_next_change(monkeypatch):
    async def go(env):
        m = env.message(content="gg")
        await env.star(m, A, B, C)
        env.guild.hall.edit_fail = http_error()
        await env.star(m, D)
        assert (await env.rows())[0]["stars"] == 3
        env.guild.hall.edit_fail = None
        await env.star(m, 15)
        assert last_edit_embed(env).footer.text.endswith("⭐ 5")
        assert (await env.rows())[0]["stars"] == 5
    with_env(go, monkeypatch)


def test_failed_post_releases_claim_so_it_can_post_later(monkeypatch):
    async def go(env):
        m = env.message(content="gg")
        env.guild.hall.send_fail = http_error()
        await env.star(m, A, B, C)  # listener swallows the error
        assert await env.rows() == []
        env.guild.hall.send_fail = None
        await env.star(m, D)
        assert len(env.guild.hall.sent) == 1
        assert (await env.rows())[0]["stars"] == 4
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- skips
def test_skips_hall_staff_staff_threads_and_nsfw(monkeypatch):
    async def go(env):
        g = env.guild
        staff_thread = g.thread("case 12", g.mod)
        nsfw_thread = g.thread("after dark", g.spicy)
        for channel in (g.hall, g.mod, staff_thread, g.spicy, nsfw_thread):
            m = env.message(channel=channel, content="x")
            await env.star(m, A, B, C)
            assert channel.fetches == 0
        assert g.hall.sent == [] and await env.rows() == []
    with_env(go, monkeypatch)


def test_private_channels_and_threads_never_reach_the_hall(monkeypatch):
    """Security: three stars in the friends-only squad chat (or a private thread)
    must not repost that message into the public hall of fame."""
    async def go(env):
        g = env.guild
        private_thread = g.thread("secret plans", g.general, private=True)
        thread_in_private = g.thread("squad stuff", g.squad)
        for channel in (g.squad, private_thread, thread_in_private):
            m = env.message(channel=channel, content="not for the public")
            await env.star(m, A, B, C)
        assert g.hall.sent == [] and await env.rows() == []
    with_env(go, monkeypatch)


def test_thread_in_a_normal_channel_is_posted_with_thread_name(monkeypatch):
    async def go(env):
        t = env.guild.thread("patch notes", env.guild.general)
        m = env.message(channel=t, content="lol")
        await env.star(m, A, B, C)
        assert posts(env)[0].footer.text == "#patch notes · ⭐ 3"
    with_env(go, monkeypatch)


def test_skips_bot_authors(monkeypatch):
    async def go(env):
        m = env.message(author=BOTUSER, content="beep")
        await env.star(m, A, B, C)
        assert env.guild.hall.sent == []
    with_env(go, monkeypatch)


def test_skips_messages_older_than_14_days_without_fetching(monkeypatch):
    async def go(env):
        old = env.message(content="ancient", created=T0 - 15 * DAY)
        await env.star(old, A, B, C)
        assert env.guild.general.fetches == 0 and env.guild.hall.sent == []
        recent = env.message(content="fresh", created=T0 - 13 * DAY)
        await env.star(recent, A, B, C)
        assert len(env.guild.hall.sent) == 1
    with_env(go, monkeypatch)


def test_existing_post_still_updates_after_14_days(monkeypatch):
    async def go(env):
        m = env.message(content="classic", created=T0 - 13 * DAY)
        await env.star(m, A, B, C)
        env.t += 5 * DAY
        await env.star(m, D)
        assert last_edit_embed(env).footer.text.endswith("⭐ 4")
    with_env(go, monkeypatch)


def test_deleted_message_and_unknown_channel_do_nothing(monkeypatch):
    async def go(env):
        m = env.message(content="gone")
        m.reaction().reactors.extend(env.user(u) for u in (A, B, C))
        del env.guild.general.messages[m.id]
        await env.cog.on_raw_reaction_add(env.payload(m, A))
        p = env.payload(m, A)
        p.channel_id = 123456
        await env.cog.on_raw_reaction_add(p)
        assert env.guild.hall.sent == [] and await env.rows() == []
    with_env(go, monkeypatch)


def test_missing_hall_channel_logs_once_and_does_nothing(monkeypatch, caplog):
    async def go(env):
        env.guild.text_channels.remove(env.guild.hall)
        m = env.message(content="x")
        with caplog.at_level(logging.WARNING, logger=cogmod.log.name):
            await env.star(m, A, B, C, D)
        warnings = [r for r in caplog.records if config.HALL_OF_FAME_CHANNEL in r.getMessage()]
        assert len(warnings) == 1
        assert await env.rows() == [] and env.guild.general.fetches == 0
        env.guild.text_channels.append(env.guild.hall)  # recreated: works again
        await env.star(m, 15)
        assert len(env.guild.hall.sent) == 1
    with_env(go, monkeypatch)


def test_listener_never_raises(monkeypatch):
    async def go(env):
        async def boom(*args):
            raise RuntimeError("boom")
        monkeypatch.setattr(env.cog, "refresh", boom)
        m = env.message(content="x")
        await env.cog.on_raw_reaction_add(env.payload(m, A))
        await env.cog.on_raw_reaction_remove(env.payload(m, A))
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- content
def test_long_text_is_truncated(monkeypatch):
    async def go(env):
        m = env.message(content="a" * 1500)
        await env.star(m, A, B, C)
        d = posts(env)[0].description
        assert len(d) == S.TEXT_LIMIT and d.endswith("…")
    with_env(go, monkeypatch)


def test_first_image_attachment_becomes_embed_image(monkeypatch):
    async def go(env):
        atts = [SimpleNamespace(filename="log.txt", content_type="text/plain", url="https://cdn/log.txt"),
                SimpleNamespace(filename="shot.png", content_type="image/png", url="https://cdn/shot.png"),
                SimpleNamespace(filename="two.png", content_type="image/png", url="https://cdn/two.png")]
        m = env.message(content="look", attachments=atts)
        await env.star(m, A, B, C)
        assert posts(env)[0].image.url == "https://cdn/shot.png"
    with_env(go, monkeypatch)


def test_image_embed_used_and_empty_text_handled(monkeypatch):
    async def go(env):
        e = SimpleNamespace(type="image", url="https://i.example/cat.png",
                            thumbnail=SimpleNamespace(url=None), image=SimpleNamespace(url=None))
        m = env.message(content="", embeds=[e])
        await env.star(m, A, B, C)
        post = posts(env)[0]
        assert post.image.url == "https://i.example/cat.png"
        assert post.description is None
    with_env(go, monkeypatch)


def test_no_text_no_image_still_posts_with_placeholder(monkeypatch):
    async def go(env):
        m = env.message(content="   ")
        await env.star(m, A, B, C)
        assert posts(env)[0].description == cogmod.NO_TEXT
    with_env(go, monkeypatch)


def test_deleting_the_original_removes_the_hall_post(monkeypatch):
    """Moderation: deleting a starred message must not leave its copy in the hall."""
    async def go(env):
        g = env.guild
        m = env.message(channel=g.general, content="something a mod removes")
        await env.star(m, A, B, C)
        assert len(g.hall.sent) == 1 and g.hall.posted
        await env.cog.on_raw_message_delete(SimpleNamespace(message_id=m.id, guild_id=g.id, channel_id=g.general.id))
        assert not g.hall.posted and await env.rows() == []
        # Deleting again (or an unknown message) is a no-op.
        await env.cog.on_raw_bulk_message_delete(SimpleNamespace(message_ids={m.id, 1}, guild_id=g.id, channel_id=1))
    with_env(go, monkeypatch)
