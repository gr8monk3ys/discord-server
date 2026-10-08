"""Offline tests for cogs.news: in-memory SQLite, fake Discord objects and a fake HTTP
layer. No network. Time is controlled by patching cogs.news.now."""

import asyncio
import json
from types import SimpleNamespace

import discord
from discord import app_commands

import config
import db as dbmod
from cogs import news as cogmod
from cogs.news import News
from logic import news as N

GUILD_ID = 999
OWNER, U1, MOD = 1, 11, 20
T0 = 1_791_400_000
HOUR, DAY = 3600, 86400
CS = N.Source("Counter-Strike 2", "steam", appid=730)
APEX = N.Source("Apex Legends", "steam", appid=1172470)
MC_URL = "https://www.minecraft.net/en-us/feeds/community-content/rss"
MC = N.Source("Minecraft", "rss", url=MC_URL, host="www.minecraft.net")


def run(coro):
    return asyncio.run(coro)


def game(role):
    return next(g for g in config.GAMES if g.role == role)


# ---------------------------------------------------------------- feed builders
def steam_item(gid, title="Counter-Strike 2 Update", age=HOUR, official=True, contents="Fixed [b]stuff[/b]",
               appid=730):
    return dict(gid=str(gid), title=title, url=f"https://steamstore-a.akamaihd.net/news/externalpost/x/{gid}",
                contents=contents, feedname="steam_community_announcements" if official else "PC Gamer",
                feed_type=1 if official else 0, date=T0 - age, appid=appid)


def steam_body(*items):
    return json.dumps({"appnews": {"appid": 730, "newsitems": list(items)}}).encode()


def rss_body(*entries):
    body = "".join(f"<item><title>{t}</title><description>{d}</description>"
                   f"<pubDate>{p}</pubDate><a10:link href=\"{h}\" /></item>" for t, d, p, h in entries)
    return ('<?xml version="1.0" encoding="utf-16"?><rss xmlns:a10="http://www.w3.org/2005/Atom" version="2.0">'
            f"<channel><title>Minecraft</title>{body}</channel></rss>").encode("utf-16-le")


# ---------------------------------------------------------------- fakes
class FakeHttp:
    """url -> (status, body, cut) or an exception; records every call."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def route(self, url, status=200, body=b"", cut=False):
        self.routes[url] = (status, body, cut)

    def fail(self, url, exc):
        self.routes[url] = exc

    async def get(self, url, *, max_bytes):
        self.calls.append(dict(url=url, max_bytes=max_bytes))
        hit = self.routes.get(url)
        if hit is None:
            raise AssertionError(f"unexpected GET {url}")
        if isinstance(hit, Exception):
            raise hit
        return hit


class HangingHttp:
    async def get(self, url, *, max_bytes):
        await asyncio.sleep(3600)


class FakeText:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class Role:
    def __init__(self, name):
        self.name = name


class FakeMember:
    def __init__(self, uid, guild, roles=()):
        self.id = uid
        self.guild = guild
        self.roles = [Role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=False)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.owner_id = OWNER
        self.games = {g.role: FakeText(g.channel_name) for g in config.GAMES}
        self.text_channels = [FakeText(config.GENERAL_CHANNEL), *self.games.values()]


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)

    def get_guild(self, gid):
        return self.guild if gid == GUILD_ID else None


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
    def __init__(self, user, guild):
        self.calls = []
        self.user = user
        self.guild = guild
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)

    def reply(self):
        return [kw for k, kw in self.calls if k in ("send_message", "followup")][-1]


class Env:
    def __init__(self, db, guild, http, cog):
        self.db, self.guild, self.http, self.cog = db, guild, http, cog
        self.t = T0

    def sent(self, role):
        return self.guild.games[role].sent

    async def seen(self, source):
        rows = await self.db.fetchall("SELECT item_id FROM news_seen WHERE source = ?", (source.key,))
        return {r["item_id"] for r in rows}

    async def news_test(self, game_name, roles=(config.MOD_ROLE,), uid=MOD):
        inter = FakeInteraction(FakeMember(uid, self.guild, roles), self.guild)
        await News.news_test.callback(self.cog, inter, game_name)
        return inter


def with_env(fn, monkeypatch, sources=(CS, MC), http=None):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        http_ = http or FakeHttp()
        cog = News(FakeBot(db, guild), http=http_, sources=list(sources))
        env = Env(db, guild, http_, cog)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


STEAM_CS = N.steam_api_url(730)
STEAM_APEX = N.steam_api_url(1172470)


# ---------------------------------------------------------------- seeding
def test_first_poll_seeds_without_posting(monkeypatch):
    async def go(env):
        env.http.route(STEAM_CS, body=steam_body(steam_item(1), steam_item(2)))
        env.http.route(MC_URL, body=rss_body(("A", "a", "Tue, 06 Oct 2026 20:00:00 Z",
                                              "https://www.minecraft.net/en-us/article/a")))
        assert await env.cog.poll() == 0
        assert env.sent("Counter-Strike 2") == [] and env.sent("Minecraft") == []
        assert await env.seen(CS) == {"1", "2", N.SEED_MARK}
        assert await env.seen(MC) == {"https://www.minecraft.net/en-us/article/a", N.SEED_MARK}
        assert await env.cog.poll() == 0  # same items again: nothing new
    with_env(go, monkeypatch)


def test_seed_marker_is_written_even_when_the_feed_is_empty(monkeypatch):
    async def go(env):
        env.http.route(STEAM_CS, body=steam_body())
        assert await env.cog.poll() == 0
        assert await env.seen(CS) == {N.SEED_MARK}
        env.http.route(STEAM_CS, body=steam_body(steam_item(5)))
        assert await env.cog.poll() == 1  # the first real item after an empty seed is news
    with_env(go, monkeypatch, sources=(CS,))


def test_new_item_after_seed_posts_an_embed(monkeypatch):
    async def go(env):
        env.http.route(STEAM_CS, body=steam_body(steam_item(1)))
        await env.cog.poll()
        env.http.route(STEAM_CS, body=steam_body(
            steam_item(2, title="@everyone [free](https://evil.example) *skins*", age=600,
                       contents="[p]Big <b>fix</b> https://evil.example/x[/p]"),
            steam_item(1)))
        assert await env.cog.poll() == 1
        (msg,) = env.sent("Counter-Strike 2")
        assert msg["allowed_mentions"].everyone is False and msg["allowed_mentions"].users is False
        assert msg["allowed_mentions"].roles is False
        assert not msg["content"]
        embed = msg["embed"]
        assert embed.url == "https://store.steampowered.com/news/app/730/view/2"
        assert "@​everyone" in embed.title and "\\[free]" in embed.title and "\\*skins\\*" in embed.title
        assert embed.description == "Big fix"
        assert "COUNTER-STRIKE 2" in embed.footer.text
        assert int(embed.timestamp.timestamp()) == T0 - 600
        assert await env.seen(CS) >= {"1", "2"}
        assert await env.cog.poll() == 0  # never reposted
        assert len(env.sent("Counter-Strike 2")) == 1
    with_env(go, monkeypatch, sources=(CS,))


def test_at_most_two_per_poll_newest_chosen_posted_oldest_first(monkeypatch):
    async def go(env):
        env.http.route(STEAM_CS, body=steam_body())
        await env.cog.poll()
        env.http.route(STEAM_CS, body=steam_body(
            steam_item(10, title="newest", age=60), steam_item(11, title="second", age=120),
            steam_item(12, title="third", age=180), steam_item(13, title="press", age=30, official=False)))
        assert await env.cog.poll() == 2
        assert [m["embed"].title for m in env.sent("Counter-Strike 2")] == ["second", "newest"]
        assert await env.seen(CS) == {N.SEED_MARK, "10", "11", "12"}  # the extra is marked seen quietly
        assert await env.cog.poll() == 0
    with_env(go, monkeypatch, sources=(CS,))


def test_old_items_are_never_posted(monkeypatch):
    async def go(env):
        env.http.route(STEAM_CS, body=steam_body())
        await env.cog.poll()
        env.http.route(STEAM_CS, body=steam_body(steam_item(20, age=8 * DAY)))
        assert await env.cog.poll() == 0
        assert env.sent("Counter-Strike 2") == []
    with_env(go, monkeypatch, sources=(CS,))


def test_seen_state_survives_a_restart(monkeypatch):
    async def go(env):
        env.http.route(STEAM_CS, body=steam_body(steam_item(1)))
        await env.cog.poll()
        fresh = News(env.cog.bot, http=env.http, sources=[CS])
        env.http.route(STEAM_CS, body=steam_body(steam_item(2, age=60), steam_item(1)))
        assert await fresh.poll() == 1
        assert [m["embed"].url.rsplit("/", 1)[1] for m in env.sent("Counter-Strike 2")] == ["2"]
    with_env(go, monkeypatch, sources=(CS,))


# ---------------------------------------------------------------- RSS
def test_rss_posts_only_links_on_the_feed_host(monkeypatch):
    async def go(env):
        env.http.route(MC_URL, body=rss_body())
        await env.cog.poll()
        env.http.route(MC_URL, body=rss_body(
            ("Snapshot 3", "Minecraft 26.4 Snapshot 3", "Tue, 06 Oct 2026 16:00:00 Z",
             "https://www.minecraft.net/en-us/article/snapshot-3"),
            ("Phish", "x", "Tue, 06 Oct 2026 17:00:00 Z", "https://evil.example/article")))
        assert await env.cog.poll() == 1
        (msg,) = env.sent("Minecraft")
        assert msg["embed"].url == "https://www.minecraft.net/en-us/article/snapshot-3"
        assert msg["embed"].title == "Snapshot 3"
    with_env(go, monkeypatch, sources=(MC,))


def test_rss_with_a_dtd_is_refused_and_nothing_posts(monkeypatch):
    async def go(env):
        env.http.route(MC_URL, body=b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "y">]><rss/>')
        assert await env.cog.poll() == 0
        assert await env.seen(MC) == set()  # a broken fetch doesn't count as the seed
    with_env(go, monkeypatch, sources=(MC,))


# ---------------------------------------------------------------- failures never raise
def test_network_failures_skip_that_source_only(monkeypatch):
    async def go(env):
        env.http.route(STEAM_APEX, body=steam_body())
        env.http.route(MC_URL, body=rss_body())
        for setup in (lambda: env.http.fail(STEAM_CS, OSError("boom")),
                      lambda: env.http.route(STEAM_CS, status=500, body=b"oops"),
                      lambda: env.http.route(STEAM_CS, body=b"x" * 10, cut=True),
                      lambda: env.http.route(STEAM_CS, body=b"<html>not json</html>")):
            setup()
            assert await env.cog.poll() == 0
        assert await env.seen(CS) == set()
        assert await env.seen(APEX) == {N.SEED_MARK} and await env.seen(MC) == {N.SEED_MARK}
        assert all(c["max_bytes"] == N.MAX_BODY for c in env.http.calls)
    with_env(go, monkeypatch, sources=(CS, APEX, MC))


def test_hanging_fetch_times_out(monkeypatch):
    async def go(env):
        monkeypatch.setattr(cogmod, "TOTAL_TIMEOUT", 0.05)
        assert await env.cog.poll() == 0
        assert await env.seen(CS) == set()
    with_env(go, monkeypatch, sources=(CS,), http=HangingHttp())


def test_send_failure_does_not_raise_or_repost(monkeypatch):
    async def go(env):
        env.http.route(STEAM_CS, body=steam_body())
        await env.cog.poll()
        env.guild.games["Counter-Strike 2"].fail = discord.HTTPException(SimpleNamespace(status=403, reason="x"), "no")
        env.http.route(STEAM_CS, body=steam_body(steam_item(3, age=60)))
        assert await env.cog.poll() == 0
        env.guild.games["Counter-Strike 2"].fail = None
        assert await env.cog.poll() == 0  # marked before sending: never posted twice
    with_env(go, monkeypatch, sources=(CS,))


def test_missing_channel_skips_without_fetching(monkeypatch):
    async def go(env):
        env.guild.text_channels = [c for c in env.guild.text_channels if c.name != game("Counter-Strike 2").channel_name]
        assert await env.cog.poll() == 0
        assert await env.cog.poll() == 0
        assert env.http.calls == []
    with_env(go, monkeypatch, sources=(CS,))


def test_no_guild_is_a_no_op(monkeypatch):
    async def go(env):
        env.cog.bot.settings.guild_id = 1
        assert await env.cog.poll() == 0
        assert env.http.calls == []
    with_env(go, monkeypatch)


def test_loop_body_swallows_errors(monkeypatch):
    async def go(env):
        async def boom():
            raise RuntimeError("db gone")
        env.cog.poll = boom
        await env.cog.news_loop.coro(env.cog)  # must not raise
    with_env(go, monkeypatch)


def test_old_seen_rows_are_pruned_but_the_seed_mark_stays(monkeypatch):
    async def go(env):
        env.http.route(STEAM_CS, body=steam_body(steam_item(1)))
        await env.cog.poll()
        env.t += N.SEEN_KEEP + DAY
        env.http.route(STEAM_CS, body=steam_body())
        await env.cog.poll()
        assert await env.seen(CS) == {N.SEED_MARK}
        env.http.route(STEAM_CS, body=steam_body(steam_item(1, age=-(N.SEEN_KEEP + DAY) + HOUR)))
        assert await env.cog.poll() == 1  # still seeded: a fresh item posts, no reseed
    with_env(go, monkeypatch, sources=(CS,))


# ---------------------------------------------------------------- /news test
def test_news_test_is_hidden_from_members_and_staff_gated(monkeypatch):
    perms = News.news.default_permissions
    assert perms is not None and perms.value == discord.Permissions(moderate_members=True).value
    assert News.news.guild_only

    async def go(env):
        inter = await env.news_test("Counter-Strike 2", roles=(), uid=U1)
        reply = inter.reply()
        assert reply["ephemeral"] is True and "Moderators" in reply["content"]
        assert env.http.calls == []
    with_env(go, monkeypatch)


def test_news_test_shows_latest_item_ephemerally_without_marking_it(monkeypatch):
    async def go(env):
        env.http.route(STEAM_CS, body=steam_body(steam_item(1, title="older", age=9 * DAY),
                                                 steam_item(2, title="latest", age=8 * DAY),
                                                 steam_item(3, title="press", age=60, official=False)))
        inter = await env.news_test("counter-strike 2")
        assert inter.calls[0] == ("defer", {"ephemeral": True, "thinking": True})
        reply = inter.reply()
        assert reply["ephemeral"] is True and reply["allowed_mentions"].everyone is False
        assert reply["embed"].title == "latest"
        assert reply["embed"].url == "https://store.steampowered.com/news/app/730/view/2"
        assert env.sent("Counter-Strike 2") == []
        assert await env.seen(CS) == set()
    with_env(go, monkeypatch)


def test_news_test_owner_allowed(monkeypatch):
    async def go(env):
        env.http.route(MC_URL, body=rss_body(("A", "a", "Tue, 06 Oct 2026 20:00:00 Z",
                                              "https://www.minecraft.net/en-us/article/a")))
        inter = await env.news_test("Minecraft", roles=(), uid=OWNER)
        assert inter.reply()["embed"].url == "https://www.minecraft.net/en-us/article/a"
    with_env(go, monkeypatch)


def test_news_test_unknown_game_empty_and_unreachable(monkeypatch):
    async def go(env):
        reply = (await env.news_test("Valorant")).reply()
        assert reply["ephemeral"] is True and "no news feed" in reply["content"].lower()
        env.http.route(STEAM_CS, body=steam_body(steam_item(1, official=False)))
        reply = (await env.news_test("Counter-Strike 2")).reply()
        assert reply["ephemeral"] is True and "nothing" in reply["content"].lower()
        env.http.fail(STEAM_CS, asyncio.TimeoutError())
        reply = (await env.news_test("Counter-Strike 2")).reply()
        assert reply["ephemeral"] is True and "couldn't reach" in reply["content"].lower()
    with_env(go, monkeypatch)


def test_news_test_autocomplete_lists_configured_games(monkeypatch):
    async def go(env):
        inter = FakeInteraction(FakeMember(MOD, env.guild, (config.MOD_ROLE,)), env.guild)
        choices = await env.cog.game_autocomplete(inter, "")
        assert [c.value for c in choices] == ["Counter-Strike 2", "Minecraft"]
        assert all(isinstance(c, app_commands.Choice) for c in choices)
        assert [c.value for c in await env.cog.game_autocomplete(inter, "mine")] == ["Minecraft"]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- wiring
def test_default_sources_come_from_the_shipped_file(monkeypatch):
    async def go(env):
        cog = News(env.cog.bot, http=env.http)
        assert cog.sources == N.read_sources({g.role for g in config.GAMES})
        assert cog.sources
    with_env(go, monkeypatch)


def test_unreadable_sources_file_means_no_news(monkeypatch, tmp_path):
    bad = tmp_path / "news_sources.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(N, "SOURCES_FILE", bad)
    cog = News(SimpleNamespace(), http=FakeHttp())
    assert cog.sources == []


def test_every_source_game_has_a_channel():
    roles = {g.role for g in config.GAMES}
    for source in N.read_sources(roles):
        assert game(source.game).channel_name


def test_real_http_client_is_capped_and_never_follows_redirects():
    http = cogmod.AiohttpHttp()
    assert "bot" not in cogmod.USER_AGENT.lower()  # minecraft.net stalls on bot-looking agents
    assert cogmod.USER_AGENT.startswith("Mozilla/5.0")
    assert http.follow_redirects is False
