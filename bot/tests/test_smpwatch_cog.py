"""Offline tests for cogs.smpwatch: a real in-memory SQLite database, a fake guild, a fake
status pinger and a fake SRV lookup. No network."""

import asyncio
import struct
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
from cogs import smpwatch as cogmod
from cogs.monkey import PingError
from cogs.smpwatch import SmpWatch
from logic import smpwatch as L

TZ = ZoneInfo("America/Los_Angeles")
GUILD_ID = 999
MIN = 60
HOUR = 3600
SRV_NAME = "_minecraft._tcp.monkeysmp.lscaturchio.xyz"
TARGET = ("play.monkeysmp.lscaturchio.xyz", 25565)


def run(coro):
    return asyncio.run(coro)


def ts(h, mi=0, day=7):
    return int(datetime(2026, 10, day, h, mi, tzinfo=TZ).timestamp())


class FakeText:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self.fail = None

    async def send(self, content=None, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append(dict(content=content, **kwargs))


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.smp = FakeText(config.MONKEY_CHANNEL)
        self.text_channels = [FakeText("💬・general"), self.smp]
        self.role = SimpleNamespace(id=4242, name=config.SMP_PING_ROLE, mention="<@&4242>")
        self.roles = [SimpleNamespace(id=1, name="Game Night", mention="<@&1>"), self.role]


class FakeBot:
    def __init__(self, db, guild):
        self.db = db
        self.guild = guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, guild_id):
        return self.guild if guild_id == GUILD_ID else None


class Env:
    """The Minecraft side: how many are on, whether the server answers, what DNS says."""

    def __init__(self, db, guild, bot):
        self.db, self.guild, self.bot = db, guild, bot
        self.t = ts(19)
        self.online = 0
        self.down = None  # an exception the pinger raises
        self.records = [L.Srv(0, 5, TARGET[1], TARGET[0] + ".")]
        self.dns_fail = False
        self.pings, self.lookups = [], []
        self.cog = self.new_cog()

    def new_cog(self, environ=None):
        return SmpWatch(self.bot, environ=environ or {}, pinger=self.pinger, lookup=self.lookup)

    async def pinger(self, host, port):
        self.pings.append((host, port))
        if self.down is not None:
            raise self.down
        return {"players": {"online": self.online, "max": 20, "sample": [{"name": "Steve", "id": "x"}]}}

    async def lookup(self, name):
        self.lookups.append(name)
        if self.dns_fail:
            raise L.DnsError("timeout")
        return list(self.records)

    async def checks(self, n, every=L.CHECK_EVERY):
        for _ in range(n):
            await self.cog.run_once()
            self.t += every

    @property
    def sent(self):
        return self.guild.smp.sent

    async def state(self):
        return L.load(await self.db.meta_get(L.STATE_KEY))


def with_env(fn, monkeypatch):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild()
        env = Env(db, guild, FakeBot(db, guild))
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- the ping
def test_three_on_posts_one_line_that_pings_only_the_role(monkeypatch):
    async def body(env):
        env.online = 2
        await env.checks(3)
        assert env.sent == [] and await env.db.meta_get(L.STATE_KEY) is None  # nothing to save
        env.online = 3
        posted_at = env.t
        await env.checks(1)
        assert len(env.sent) == 1
        msg = env.sent[0]
        assert msg["content"] == L.ping_text("<@&4242>", 3, "monkeysmp.lscaturchio.xyz")
        allowed = msg["allowed_mentions"]
        assert allowed.everyone is False and allowed.users is False and allowed.roles == [env.guild.role]
        assert "Steve" not in msg["content"]  # a count, never names
        assert await env.state() == L.State(posted_at, armed=False)
        assert env.pings[-1] == TARGET
    with_env(body, monkeypatch)


def test_a_long_session_is_one_ping_and_a_new_crowd_later_gets_another(monkeypatch):
    async def body(env):
        env.online = 4
        await env.checks(30)  # 2.5 hours on
        assert len(env.sent) == 1
        env.online = 0
        await env.checks(2)  # everyone left: armed again
        env.online = 5
        await env.checks(3)  # back within four hours of the first ping
        assert len(env.sent) == 1
        env.t = ts(23, 30)  # the cooldown is over but it's quiet hours
        await env.checks(1)
        assert len(env.sent) == 1
        env.t = ts(9, day=8)  # still on in the morning
        await env.checks(1)
        assert len(env.sent) == 2 and "**5 on Monkey SMP now.**" in env.sent[1]["content"]
    with_env(body, monkeypatch)


def test_the_state_outlives_a_restart(monkeypatch):
    async def body(env):
        env.online = 3
        await env.checks(1)
        env.cog = env.new_cog()  # the bot restarted mid-session
        await env.checks(5)
        assert len(env.sent) == 1
    with_env(body, monkeypatch)


def test_no_role_or_no_channel_posts_and_saves_nothing_until_they_exist(monkeypatch, caplog):
    async def body(env):
        env.online = 3
        roles = env.guild.roles
        env.guild.roles = roles[:1]
        await env.checks(3)
        assert env.sent == [] and await env.db.meta_get(L.STATE_KEY) is None
        assert sum("SMP Ping" in r.getMessage() for r in caplog.records) == 1  # said once, not every check
        env.guild.roles = roles
        channels = env.guild.text_channels
        env.guild.text_channels = channels[:1]
        await env.checks(2)
        assert env.sent == []
        env.guild.text_channels = channels
        await env.checks(1)
        assert len(env.sent) == 1
    caplog.set_level("INFO", logger=cogmod.__name__)
    with_env(body, monkeypatch)


def test_the_channel_is_found_under_its_old_name(monkeypatch):
    async def body(env):
        assert config.MONKEY_CHANNEL_WAS
        env.guild.smp.name = config.MONKEY_CHANNEL_WAS[0]
        env.online = 3
        await env.checks(1)
        assert len(env.sent) == 1
    with_env(body, monkeypatch)


def test_a_failed_send_is_retried_at_the_next_check(monkeypatch):
    async def body(env):
        env.online = 3
        env.guild.smp.fail = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
        await env.checks(2)
        assert env.sent == [] and await env.db.meta_get(L.STATE_KEY) is None
        env.guild.smp.fail = None
        await env.checks(1)
        assert len(env.sent) == 1
    with_env(body, monkeypatch)


def test_no_guild_does_nothing(monkeypatch):
    async def body(env):
        env.bot.settings = SimpleNamespace(guild_id=1, tz=TZ)
        env.online = 3
        await env.checks(2)
        assert env.sent == [] and await env.db.meta_get(L.STATE_KEY) is None
    with_env(body, monkeypatch)


# ---------------------------------------------------------------- the server and DNS
def test_a_server_that_does_not_answer_changes_nothing(monkeypatch):
    async def body(env):
        env.online = 3
        await env.checks(1)
        before = await env.state()
        env.online = 0
        env.down = PingError("timed out")
        await env.checks(3)  # down is not "nobody on": it must not re-arm
        env.down = None
        env.online = 3
        env.t += 5 * HOUR
        await env.checks(1)
        assert len(env.sent) == 1 and await env.state() == before
    with_env(body, monkeypatch)


def test_the_srv_answer_is_cached_for_an_hour_and_dropped_when_a_ping_fails(monkeypatch):
    async def body(env):
        await env.checks(12)  # one hour of checks
        assert env.lookups == [SRV_NAME] and set(env.pings) == {TARGET}
        await env.checks(1)
        assert env.lookups == [SRV_NAME] * 2
        env.down = PingError("refused")
        await env.checks(2)
        assert env.lookups == [SRV_NAME] * 3  # the check after a failed ping looks it up again
    with_env(body, monkeypatch)


def test_no_srv_record_or_no_dns_falls_back_to_the_name_on_25565(monkeypatch):
    async def body(env):
        env.records = [L.Srv(0, 0, 25565, ".")]
        await env.checks(1)
        assert env.pings[-1] == ("monkeysmp.lscaturchio.xyz", 25565)
        env.t += 2 * HOUR
        env.dns_fail = True
        await env.checks(1)
        assert env.pings[-1] == ("monkeysmp.lscaturchio.xyz", 25565)
    with_env(body, monkeypatch)


def test_an_address_with_a_port_skips_dns(monkeypatch):
    async def body(env):
        env.cog = env.new_cog({"SMP_WATCH_ADDRESS": "mc.example.org:25570", "SMP_WATCH_MIN": "2"})
        env.online = 2
        await env.checks(1)
        assert env.lookups == [] and env.pings == [("mc.example.org", 25570)]
        assert "`mc.example.org:25570`" in env.sent[0]["content"]
    with_env(body, monkeypatch)


def test_an_unexpected_error_is_logged_not_raised(monkeypatch, caplog):
    async def body(env):
        env.down = RuntimeError("boom")
        await env.cog.run_once()
        assert any("smpwatch: run_once failed" in r.getMessage() for r in caplog.records)
    with_env(body, monkeypatch)


def srv_reply(packet, target=b"\x04play\x09monkeysmp\x0blscaturchio\x03xyz\x00"):
    """A resolver's answer to `packet` (one SRV record), keeping its id and question."""
    rdata = struct.pack(">HHH", 0, 5, 25565) + target
    head = packet[:2] + b"\x81\x80" + b"\x00\x01\x00\x01\x00\x00\x00\x00" + packet[12:]
    return head + b"\xc0\x0c" + struct.pack(">HHIH", 33, 1, 300, len(rdata)) + rdata


def test_lookup_tries_the_next_resolver_and_matches_the_query_id(monkeypatch):
    asked = []

    def exchange(server, packet, timeout):
        asked.append(server)
        if server == "10.0.0.2":
            raise TimeoutError
        if server == "1.1.1.1":
            return srv_reply(b"\xff\xff" + packet[2:])  # someone else's id
        return srv_reply(packet)

    monkeypatch.setattr(cogmod, "resolvers", lambda: ["10.0.0.2", "1.1.1.1", "8.8.8.8"])
    monkeypatch.setattr(cogmod, "udp_exchange", exchange)
    monkeypatch.setattr(cogmod.secrets, "randbelow", lambda n: 0x1234)
    found = run(cogmod.lookup_srv(SRV_NAME))
    assert asked == ["10.0.0.2", "1.1.1.1", "8.8.8.8"]
    assert L.pick_srv(found) == TARGET


def test_lookup_raises_when_no_resolver_answers(monkeypatch):
    def exchange(server, packet, timeout):
        raise OSError("unreachable")

    monkeypatch.setattr(cogmod, "resolvers", lambda: ["a", "b", "c", "d", "e"])
    monkeypatch.setattr(cogmod, "udp_exchange", exchange)
    try:
        run(cogmod.lookup_srv(SRV_NAME))
    except L.DnsError as exc:
        assert str(exc) == "OSError"
    else:
        raise AssertionError("expected DnsError")


def test_resolvers_are_the_systems_then_the_public_ones(monkeypatch, tmp_path):
    conf = tmp_path / "resolv.conf"
    conf.write_text("nameserver 10.0.0.2\nnameserver 8.8.8.8\n", encoding="utf-8")
    monkeypatch.setattr(cogmod, "RESOLV_CONF", str(conf))
    assert cogmod.resolvers() == ["10.0.0.2", "8.8.8.8", "1.1.1.1"]
    monkeypatch.setattr(cogmod, "RESOLV_CONF", str(tmp_path / "missing"))
    assert cogmod.resolvers() == ["1.1.1.1", "8.8.8.8"]


# ---------------------------------------------------------------- wiring
def test_cog_starts_the_loop_unless_turned_off(monkeypatch):
    async def body(env):
        for environ, want in (({}, ["tick"]), ({"SMP_WATCH": "off"}, [])):
            started = []
            cog = env.new_cog(environ)
            monkeypatch.setattr(cog.tick, "start", lambda: started.append("tick"))
            await cog.cog_load()
            assert started == want
    with_env(body, monkeypatch)


def test_registered_without_privileged_intents_or_commands():
    import main
    assert ("cogs.smpwatch", set()) in main.MODULES
    assert not SmpWatch.__cog_app_commands__
    assert L.CHECK_EVERY == 300 and L.COOLDOWN == 4 * HOUR and L.QUIET == (22, 9)
