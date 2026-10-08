"""Offline tests for cogs.cards.Cards: a real in-memory SQLite database plus small fakes
for the bot, guild, channel, members and avatars. No network."""

import asyncio
import io
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

import config
import db as dbmod
from cogs import cards as cogmod
from cogs.cards import Cards
from logic import cards

GUILD_ID = 999
T0 = 1_790_000_000


def run(coro):
    return asyncio.run(coro)


def http_error(status=500):
    return discord.HTTPException(SimpleNamespace(status=status, reason="boom"), "boom")


def png_bytes(color=(200, 80, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- fakes
class FakeAsset:
    def __init__(self, data=None, error=None):
        self.data = png_bytes() if data is None else data
        self.error = error
        self.replaced = None

    def replace(self, **kwargs):
        self.replaced = kwargs
        return self

    async def read(self):
        if self.error is not None:
            raise self.error
        return self.data


class FakeText:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        file = kwargs.get("file")
        if file is not None:  # read it now, like discord.py does on upload
            kwargs["png"] = file.fp.read()
        self.sent.append(dict(content=content, **kwargs))


class FakeGuild:
    def __init__(self, gid=GUILD_ID):
        self.id = gid
        self.name = "Chill Gaming"
        self.member_count = 1234
        self.welcome = FakeText(config.WELCOME_CHANNEL)
        self.general = FakeText(config.GENERAL_CHANNEL)
        self.text_channels = [self.general, self.welcome]
        self.onboarding_on = True
        self.onboarding_error = None
        self.onboarding_calls = 0

    async def onboarding(self):
        self.onboarding_calls += 1
        if self.onboarding_error is not None:
            raise self.onboarding_error
        return SimpleNamespace(enabled=self.onboarding_on)


class FakeMember:
    def __init__(self, uid, guild, bot=False, completed=False, name="Alex", avatar=None):
        self.id = uid
        self.guild = guild
        self.bot = bot
        self.name = name.lower()
        self.display_name = name
        self.mention = f"<@{uid}>"
        self.flags = SimpleNamespace(completed_onboarding=completed)
        self.display_avatar = avatar or FakeAsset()

    def finished(self, done=True):
        m = FakeMember(self.id, self.guild, self.bot, done, self.display_name, self.display_avatar)
        m.name = self.name
        return m


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.settings = SimpleNamespace(guild_id=GUILD_ID)
        self.cogs = []

    async def add_cog(self, cog):
        self.cogs.append(cog)


class Env:
    def __init__(self, db, guild, bot, cog):
        self.db, self.guild, self.bot, self.cog = db, guild, bot, cog
        self.t = T0

    def restart(self):
        self.cog = Cards(self.bot)
        return self.cog

    async def finish(self, member):
        """Member completes Onboarding."""
        await self.cog.on_member_update(member, member.finished())

    async def meta(self, uid):
        return await self.db.fetchone("SELECT value FROM meta WHERE key = ?", (cards.card_key(uid),))


@pytest.fixture
def env_run(monkeypatch):
    def go(fn):
        async def body():
            db = dbmod.Database(":memory:")
            await db.connect()
            await db.migrate()
            guild = FakeGuild()
            bot = FakeBot(db)
            env = Env(db, guild, bot, Cards(bot))
            monkeypatch.setattr(cogmod, "now", lambda: env.t)
            try:
                await fn(env)
            finally:
                await db.close()
        run(body())
    return go


def assert_card(sent, member):
    assert sent["content"] == member.mention
    am = sent["allowed_mentions"]
    # Named but not pinged: the #general welcome is the one ping a newcomer gets.
    assert am.everyone is False and am.roles is False and am.users is False
    assert sent["file"].filename == "welcome.png"
    img = Image.open(io.BytesIO(sent["png"]))
    assert img.format == "PNG" and img.size == (1100, 400)


# ---------------------------------------------------------------- flow
def test_card_after_onboarding_completes(env_run):
    async def t(env):
        m = FakeMember(11, env.guild)
        await env.cog.on_member_join(m)
        assert env.guild.welcome.sent == []  # still in Onboarding
        await env.finish(m)
        assert len(env.guild.welcome.sent) == 1
        assert_card(env.guild.welcome.sent[0], m)
        assert m.display_avatar.replaced == {"size": 256, "format": "png"}
        assert env.guild.general.sent == []
        assert (await env.meta(11))["value"] == str(T0)
    env_run(t)


def test_card_on_join_without_onboarding(env_run):
    async def t(env):
        env.guild.onboarding_on = False
        m = FakeMember(11, env.guild)
        await env.cog.on_member_join(m)
        assert len(env.guild.welcome.sent) == 1
        assert_card(env.guild.welcome.sent[0], m)
    env_run(t)


def test_card_on_join_when_already_through_onboarding(env_run):
    async def t(env):
        m = FakeMember(11, env.guild, completed=True)
        await env.cog.on_member_join(m)
        assert len(env.guild.welcome.sent) == 1
    env_run(t)


def test_onboarding_check_is_cached_and_errors_assume_on(env_run):
    async def t(env):
        env.guild.onboarding_error = http_error()
        await env.cog.on_member_join(FakeMember(11, env.guild))
        assert env.guild.welcome.sent == []  # assumed on: wait for completion
        env.guild.onboarding_error = None
        env.guild.onboarding_on = False
        await env.cog.on_member_join(FakeMember(12, env.guild))
        await env.cog.on_member_join(FakeMember(13, env.guild))
        assert env.guild.onboarding_calls == 2  # error not cached, success cached
        assert len(env.guild.welcome.sent) == 2
    env_run(t)


def test_once_per_member_ever(env_run):
    async def t(env):
        m = FakeMember(11, env.guild)
        await env.finish(m)
        await env.finish(m)
        await env.cog.on_member_join(m.finished())
        env.restart()
        await env.finish(m)  # left, rejoined, onboarded again after a restart
        assert len(env.guild.welcome.sent) == 1
    env_run(t)


def test_racing_events_post_once(env_run):
    async def t(env):
        m = FakeMember(11, env.guild)
        await asyncio.gather(env.finish(m), env.finish(m), env.cog.on_member_join(m.finished()))
        assert len(env.guild.welcome.sent) == 1
    env_run(t)


def test_bots_and_other_servers_skipped(env_run):
    async def t(env):
        env.guild.onboarding_on = False
        await env.cog.on_member_join(FakeMember(50, env.guild, bot=True))
        await env.finish(FakeMember(51, env.guild, bot=True))
        other = FakeGuild(gid=1)
        other.onboarding_on = False
        await env.cog.on_member_join(FakeMember(11, other))
        await env.finish(FakeMember(12, other))
        assert env.guild.welcome.sent == [] and other.welcome.sent == []
        assert await env.meta(50) is None and await env.meta(11) is None
        assert await env.cog.post_card(FakeMember(52, env.guild, bot=True)) is False
    env_run(t)


@pytest.mark.parametrize("asset", [FakeAsset(error=http_error(404)), FakeAsset(data=b"not a png"),
                                   FakeAsset(error=asyncio.TimeoutError())])
def test_avatar_failure_falls_back_to_initial(env_run, asset):
    async def t(env):
        m = FakeMember(11, env.guild, avatar=asset)
        await env.finish(m)
        assert len(env.guild.welcome.sent) == 1
        img = Image.open(io.BytesIO(env.guild.welcome.sent[0]["png"])).convert("RGB")
        x, y = cards.AVATAR_CENTER
        r, g, b = img.getpixel((x - cards.AVATAR_SIZE // 2 + 20, y))
        assert g > r and g > b  # the accent initial circle
    env_run(t)


def test_avatar_is_drawn(env_run):
    async def t(env):
        m = FakeMember(11, env.guild, avatar=FakeAsset(data=png_bytes((250, 0, 0))))
        await env.finish(m)
        img = Image.open(io.BytesIO(env.guild.welcome.sent[0]["png"])).convert("RGB")
        r, g, b = img.getpixel(cards.AVATAR_CENTER)
        assert r > 200 and g < 60 and b < 60
    env_run(t)


@pytest.mark.parametrize("name", ["🎮💀🔥", "שלום עולם", "مرحبا", "x" * 40, "‮@everyone"])
def test_odd_names_still_post(env_run, name):
    async def t(env):
        m = FakeMember(11, env.guild, name=name)
        await env.finish(m)
        assert_card(env.guild.welcome.sent[0], m)
    env_run(t)


def test_rendering_runs_in_a_thread(env_run, monkeypatch):
    async def t(env):
        calls = []
        real = asyncio.to_thread

        async def spy(fn, *args):
            calls.append((fn, args))
            return await real(fn, *args)
        monkeypatch.setattr(cogmod.asyncio, "to_thread", spy)
        m = FakeMember(11, env.guild)
        await env.finish(m)
        assert calls and calls[0][0] is cards.render_card
        assert calls[0][1][:3] == ("Alex", 1234, "Chill Gaming")
        assert calls[0][1][4] == "alex"  # username as the fallback name
    env_run(t)


# ---------------------------------------------------------------- failures
def test_no_welcome_channel_does_not_claim(env_run):
    async def t(env):
        env.guild.text_channels.remove(env.guild.welcome)
        m = FakeMember(11, env.guild)
        await env.finish(m)
        assert await env.meta(11) is None
        env.guild.text_channels.append(env.guild.welcome)
        await env.finish(m)
        assert len(env.guild.welcome.sent) == 1
    env_run(t)


def test_send_failure_never_raises_and_releases_the_claim(env_run):
    async def t(env):
        env.guild.welcome.fail = discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no")
        m = FakeMember(11, env.guild)
        await env.finish(m)  # must not raise
        assert await env.meta(11) is None
        env.guild.welcome.fail = None
        await env.finish(m)
        assert len(env.guild.welcome.sent) == 1
    env_run(t)


def test_render_failure_never_raises(env_run, monkeypatch):
    async def t(env):
        def boom(*args):
            raise RuntimeError("render")
        monkeypatch.setattr(cogmod.cards, "render_card", boom)
        env.guild.onboarding_on = False
        await env.cog.on_member_join(FakeMember(11, env.guild))
        await env.finish(FakeMember(12, env.guild))
        assert env.guild.welcome.sent == []
        assert await env.meta(11) is None and await env.meta(12) is None
    env_run(t)


def test_member_count_missing_still_posts(env_run):
    async def t(env):
        env.guild.member_count = None
        await env.finish(FakeMember(11, env.guild))
        assert len(env.guild.welcome.sent) == 1
    env_run(t)


def test_setup_adds_the_cog():
    bot = FakeBot(None)
    run(cogmod.setup(bot))
    assert len(bot.cogs) == 1 and isinstance(bot.cogs[0], Cards)
