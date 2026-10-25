"""Offline tests for cogs.realms: in-memory SQLite, fake Discord objects, a fake RCON
client, a fake status pinger and a fake log reader. No sockets, no files, no real clock
(the transports themselves are tested in test_realms.py)."""

import asyncio
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import config
import db as dbmod
import economy
from cogs import realms as cogmod
from cogs.realms import MESSAGES, Realms
from logic import quests
from logic import realms as R

GUILD_ID = 999
TZ = ZoneInfo("America/Los_Angeles")
T0 = int(datetime(2026, 10, 5, 8, 0, tzinfo=TZ).timestamp())  # a Monday morning
DAY = "2026-10-05"
PASSWORD = "hunter2-rcon-secret"
SECTION = "§"
P = "[12:34:56] [Server thread/INFO]: "
ALL_ON = {R.ENV_ADDRESS: "127.0.0.1:25565", R.ENV_RCON_PASSWORD: PASSWORD,
          R.ENV_LOG_PATH: "/srv/minecraft/logs/latest.log"}


def snowflake(created_at: int, n: int = 0) -> int:
    """A user id whose account was created at `created_at`."""
    return ((created_at * 1000 - quests.DISCORD_EPOCH_MS) << 22) + n


OWNER = snowflake(T0 - 900 * 86400)
U1, U2, U3, MOD = (snowflake(T0 - 400 * 86400, n) for n in (1, 2, 3, 4))
YOUNG = snowflake(T0 - 5 * 86400, 9)


def run(coro):
    return asyncio.run(coro)


def not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


class FakeRcon:
    """Answers like a vanilla server; records every command it was given."""

    def __init__(self):
        self.commands: list[str] = []
        self.online: list[str] = []
        self.whitelist: set[str] = set()
        self.unknown: set[str] = set()  # names Mojang does not know
        self.down = False
        self.refuse = False
        self.garble = False  # answer whitelist commands with something unexpected
        self.closed = False

    async def command(self, text: str) -> str:
        if self.refuse:
            raise cogmod.RconRefused()
        if self.down:
            raise cogmod.RconError("no answer (ConnectionRefusedError)")
        self.commands.append(text)
        if text == "list":
            return f"There are {len(self.online)} of a max of 20 players online: " + ", ".join(self.online)
        if text.startswith("whitelist "):
            _, action, name = text.split(" ")
            if self.garble:
                return "Unknown or incomplete command"
            if name in self.unknown:
                return "That player does not exist"
            if action == "add":
                if name.lower() in self.whitelist:
                    return "Player is already whitelisted"
                self.whitelist.add(name.lower())
                return f"Added {name} to the whitelist"
            if name.lower() not in self.whitelist:
                return "Player is not whitelisted"
            self.whitelist.discard(name.lower())
            return f"Removed {name} from the whitelist"
        return ""

    async def close(self) -> None:
        self.closed = True

    def sent(self, prefix: str) -> list[str]:
        return [c for c in self.commands if c.startswith(prefix)]


class FakeMessage:
    def __init__(self, channel, mid, kwargs):
        self.channel, self.id, self.kwargs = channel, mid, kwargs
        self.edits: list[dict] = []
        self.pinned = False
        self.deleted = False

    async def edit(self, **kwargs):
        if self.channel.fail_edit:
            raise self.channel.fail_edit
        if self.deleted:
            raise not_found()
        self.edits.append(kwargs)
        self.kwargs = {**self.kwargs, **kwargs}

    async def pin(self, **kwargs):
        if self.channel.fail_pin:
            raise self.channel.fail_pin
        self.pinned = True

    @property
    def text(self) -> str:
        embed = self.kwargs.get("embed")
        return embed.description if embed is not None else self.kwargs.get("content")


class FakeText:
    def __init__(self, cid, name, guild):
        self.id, self.name, self.guild = cid, name, guild
        self.messages: list[FakeMessage] = []
        self.fail_send = self.fail_edit = self.fail_pin = None
        self.muted: set[int] = set()  # members who can read here but not post (an overwrite, a muted role)

    def permissions_for(self, member):
        # as discord.py does it: a timed-out member is left with nothing but reading
        can = member.id not in self.muted and not member.is_timed_out()
        return discord.Permissions(view_channel=True, send_messages=can)

    async def send(self, content=None, **kwargs):
        if self.fail_send:
            raise self.fail_send
        msg = FakeMessage(self, 5000 + self.id * 100 + len(self.messages), dict(content=content, **kwargs))
        self.messages.append(msg)
        return msg

    def get_partial_message(self, mid):
        found = next((m for m in self.messages if m.id == mid), None)
        if found is None:
            found = FakeMessage(self, mid, {})
            found.deleted = True
        return found


class Role:
    def __init__(self, name):
        self.name = name


class FakeMember:
    def __init__(self, uid, guild, roles=(), bot=False, name=None, joined_days=200):
        self.id, self.guild, self.bot = uid, guild, bot
        self.mention = f"<@{uid}>"
        self.display_name = name or f"member{uid % 100}"
        self.roles = [Role(r) for r in roles]
        self.guild_permissions = SimpleNamespace(administrator=False)
        # in the server since `joined_days` before the tests' clock starts (None: Discord didn't say)
        self.joined_at = None if joined_days is None else datetime.fromtimestamp(T0 - joined_days * 86400, TZ)
        self.timed_out = False

    def is_timed_out(self):
        return self.timed_out


class FakeGuild:
    def __init__(self, gid=GUILD_ID, with_channel=True):
        self.id = gid
        self.owner_id = OWNER
        self.chunked = True
        self.realms = FakeText(1, config.REALMS_CHANNEL, self)
        self.general = FakeText(2, config.GENERAL_CHANNEL, self)
        self.mod_log = FakeText(3, config.MOD_LOG_CHANNEL, self)
        self.text_channels = [self.general, self.mod_log] + ([self.realms] if with_channel else [])
        self.members: dict[int, FakeMember] = {}
        self.uncached: dict[int, FakeMember] = {}  # in the server, but only the API knows
        self.fetches: list[int] = []
        self.fetch_fail = None

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        self.fetches.append(uid)
        if self.fetch_fail is not None:
            raise self.fetch_fail
        if uid not in self.uncached:
            raise not_found()
        return self.uncached[uid]


class FakeBot:
    def __init__(self, db, guild):
        self.db, self.guild_obj = db, guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID, tz=TZ)

    def get_guild(self, gid):
        return self.guild_obj if gid == GUILD_ID else None

    async def wait_until_ready(self):
        await asyncio.Event().wait()


class FakeInteraction:
    def __init__(self, user, guild):
        self.user, self.guild = user, guild
        self.calls: list[tuple[str, dict]] = []
        calls = self.calls

        class Response:
            async def send_message(self, content=None, **kwargs):
                calls.append(("send_message", dict(content=content, **kwargs)))

            async def defer(self, **kwargs):
                calls.append(("defer", kwargs))

        class Followup:
            async def send(self, content=None, **kwargs):
                calls.append(("followup", dict(content=content, **kwargs)))

        self.response, self.followup = Response(), Followup()

    def reply(self) -> dict:
        return [kw for kind, kw in self.calls if kind in ("send_message", "followup")][-1]


class ChatMessage:
    """A member's message in a channel, as on_message sees it."""

    def __init__(self, author, channel, content, **extra):
        self.author, self.channel, self.guild = author, channel, channel.guild
        self.content = self.clean_content = content
        self.type = extra.pop("type", discord.MessageType.default)
        self.webhook_id = extra.pop("webhook_id", None)
        self.attachments = extra.pop("attachments", [])
        self.reactions: list[str] = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


def status_json(names=(), online=None, version="Fabric 1.21.1", motd="Monkey SMP"):
    return {"version": {"name": version}, "description": {"text": motd},
            "players": {"max": 20, "online": len(names) if online is None else online,
                        "sample": [{"name": n, "id": "x"} for n in names]}}


class Env:
    def __init__(self, db, guild, cog, rcon):
        self.db, self.guild, self.cog, self.rcon = db, guild, cog, rcon
        self.t = T0
        self.ping: dict | Exception = status_json()
        self.pings = 0
        self.log_feed: list = []  # what the next log reads return: bytes, None or (bytes, reset)
        self.reads = 0

    # ---- the three injected seams
    async def pinger(self, host, port):
        self.pings += 1
        if isinstance(self.ping, Exception):
            raise self.ping
        return self.ping

    def reader(self, path, state):
        self.reads += 1
        assert path == ALL_ON[R.ENV_LOG_PATH]
        item = self.log_feed.pop(0) if self.log_feed else b""
        if item is None:
            return None
        data, reset = item if isinstance(item, tuple) else (item, False)
        return R.TailState(1, (state.pos or 0) + len(data)), data, reset

    # ---- actors
    def member(self, uid, roles=(), **kwargs):
        if uid not in self.guild.members:
            self.guild.members[uid] = FakeMember(uid, self.guild, roles, **kwargs)
        return self.guild.members[uid]

    def players(self, *names):
        """Who is on the Minecraft server (both what the ping and what RCON `list` say)."""
        self.rcon.online = list(names)
        self.ping = status_json(names)

    async def poll(self, advance=R.POLL_EVERY):
        self.t += advance
        await self.cog.poll_once()

    async def link(self, uid, name, wait=True):
        if wait:
            self.t += R.LINK_COOLDOWN
        inter = FakeInteraction(self.member(uid), self.guild)
        await Realms.link.callback(self.cog, inter, name)
        return inter.reply()

    async def unlink(self, uid):
        inter = FakeInteraction(self.member(uid), self.guild)
        await Realms.unlink.callback(self.cog, inter)
        return inter.reply()

    async def linked(self, uid, name):
        """A link the server confirmed, written directly (the relay tests aren't about linking)."""
        self.member(uid)
        await self.db.execute(
            "INSERT INTO realms_links (name_key, name, user_id, state, attempts, linked_at, updated_at, managed)"
            " VALUES (?, ?, ?, 'ok', 0, ?, ?, 1)", (R.name_key(name), name, uid, T0, T0))

    async def adopt(self, mod_uid, uid, name, roles=(config.MOD_ROLE,)):
        inter = FakeInteraction(self.member(mod_uid, roles=roles), self.guild)
        await Realms.adopt.callback(self.cog, inter, self.member(uid), name)
        return inter.reply()

    async def remove(self, mod_uid, uid, roles=(config.MOD_ROLE,)):
        inter = FakeInteraction(self.member(mod_uid, roles=roles), self.guild)
        await Realms.remove.callback(self.cog, inter, self.member(uid))
        return inter.reply()

    async def status(self, uid, roles=()):
        inter = FakeInteraction(self.member(uid, roles=roles), self.guild)
        await Realms.status_command.callback(self.cog, inter)
        return inter.reply()

    async def top(self, uid):
        inter = FakeInteraction(self.member(uid), self.guild)
        await Realms.top.callback(self.cog, inter)
        return inter.reply()

    async def say(self, uid, content, channel=None, **extra):
        author = extra.pop("author", None) or self.member(uid)
        msg = ChatMessage(author, channel or self.guild.realms, content, **extra)
        return await self.cog.relay_to_minecraft(msg), msg

    async def log(self, *lines, reset=False):
        self.log_feed.append(("".join(line + "\n" for line in lines).encode("utf-8"), reset))
        await self.cog.relay_once()

    # ---- reading state back
    async def links(self):
        rows = await self.db.fetchall("SELECT * FROM realms_links ORDER BY name_key")
        return [(r["name"], r["user_id"], r["state"]) for r in rows]

    async def seconds(self, uid, day=DAY):
        row = await self.db.fetchone("SELECT seconds FROM realms_playtime WHERE user_id = ? AND day = ?", (uid, day))
        return row["seconds"] if row else None

    async def balance(self, uid):
        return await economy.balance(self.db, uid)

    async def opt_out(self, uid):
        await self.db.execute("INSERT INTO privacy_optout (user_id, at) VALUES (?, ?)", (uid, self.t))

    def tellraws(self):
        return [json.loads(c[len("tellraw @a "):]) for c in self.rcon.sent("tellraw @a ")]

    @property
    def posts(self):
        return self.guild.realms.messages

    def mod_lines(self):
        return [m.kwargs["embed"].description for m in self.guild.mod_log.messages]


def with_env(fn, monkeypatch, environ=None, with_channel=True):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild(with_channel=with_channel)
        rcon = FakeRcon()
        env = Env(db, guild, None, rcon)
        env.cog = Realms(FakeBot(db, guild), environ=ALL_ON if environ is None else environ,
                         pinger=env.pinger, rcon=rcon, reader=env.reader)
        monkeypatch.setattr(cogmod, "now", lambda: env.t)
        try:
            await fn(env)
        finally:
            await db.close()
    run(go())


# ---------------------------------------------------------------- off until configured
def test_without_settings_the_bridge_is_off_and_quiet(monkeypatch, caplog):
    async def go(env):
        cog = env.cog
        assert not cog.setup.any_on and cog.rcon is env.rcon
        with caplog.at_level(logging.INFO):
            await cog.cog_load()
        assert not cog.poll_loop.is_running() and not cog.relay_loop.is_running()
        assert [r.levelname for r in caplog.records] == ["INFO"] and "the bridge is off" in caplog.text
        assert cog.health_text() == "off"
        for reply in (await env.link(U1, "Steve"), await env.status(U1), await env.top(U1)):
            assert reply["content"] == MESSAGES["off"] and reply["ephemeral"] is True
        assert (await env.unlink(U1))["content"] == MESSAGES["none"]
        assert (await env.say(U1, "hello"))[0] == "off"
        await cog.cog_unload()
        assert env.rcon.commands == [] and env.pings == 0 and env.reads == 0
        assert env.posts == [] and env.guild.mod_log.messages == []
    with_env(go, monkeypatch, environ={})


def test_real_environment_without_settings_builds_no_client(monkeypatch):
    async def go():
        for name in config.REALMS_ENV:
            monkeypatch.delenv(name, raising=False)
        cog = Realms(FakeBot(None, FakeGuild()))
        assert not cog.setup.any_on and cog.rcon is None
        for name, value in ALL_ON.items():
            monkeypatch.setenv(name, value)
        cog = Realms(FakeBot(None, FakeGuild()))  # nothing connects until a command is run
        assert isinstance(cog.rcon, cogmod.Rcon) and (cog.rcon.host, cog.rcon.port) == ("127.0.0.1", 25575)
        assert cog.rcon.writer is None and PASSWORD not in repr(cog.rcon) and PASSWORD not in repr(cog.setup)
        assert cog.pinger is cogmod.ping_server and cog.reader is cogmod.read_log
    run(go())


def test_loops_start_only_for_the_parts_that_are_on(monkeypatch):
    async def go(env):
        await env.cog.cog_load()
        assert env.cog.poll_loop.is_running() and not env.cog.relay_loop.is_running()
        await env.cog.cog_unload()
        assert env.rcon.closed
    with_env(go, monkeypatch, environ={R.ENV_ADDRESS: "127.0.0.1"})

    async def log_only(env):
        await env.cog.cog_load()
        assert not env.cog.poll_loop.is_running() and env.cog.relay_loop.is_running()
        await env.cog.cog_unload()
    with_env(log_only, monkeypatch, environ={R.ENV_LOG_PATH: ALL_ON[R.ENV_LOG_PATH]})


def test_bad_settings_are_reported_once_without_values(monkeypatch, caplog):
    async def go(env):
        with caplog.at_level(logging.INFO):
            await env.cog.cog_load()
            await env.cog.cog_unload()
        assert "REALMS_RCON_HOST refused" in caplog.text
        assert PASSWORD not in caplog.text and "203.0.113.9" not in caplog.text
        assert env.cog.setup.status_on and not env.cog.setup.rcon_on
        assert (await env.link(U1, "Steve"))["content"] == MESSAGES["no_whitelist"]
        assert env.rcon.commands == []
    with_env(go, monkeypatch, environ={R.ENV_ADDRESS: "127.0.0.1", R.ENV_RCON_HOST: "203.0.113.9",
                                       R.ENV_RCON_PASSWORD: PASSWORD})


# ---------------------------------------------------------------- status panel
def test_panel_is_posted_once_then_edited_in_place(monkeypatch):
    async def go(env):
        env.players("Steve")
        await env.poll()
        (panel,) = env.posts
        assert panel.pinned and panel.kwargs["allowed_mentions"].everyone is False
        assert "🟢 **Online** · 1 / 20 players" in panel.text and "Steve" in panel.text
        assert "/smp link" in panel.text and "Fabric 1.21.1" in panel.text
        assert await env.cog.meta_get(cogmod.PANEL_KEY) == f"{env.guild.realms.id}:{panel.id}"
        await env.poll()
        await env.poll()
        assert panel.edits == [] and len(env.posts) == 1  # nothing changed: no API calls
        env.players("Steve", "Alex")
        await env.poll()
        assert len(panel.edits) == 1 and len(env.posts) == 1 and "Alex, Steve" in panel.text
        assert env.pings == 4
    with_env(go, monkeypatch)


def test_panel_survives_a_restart_and_a_deleted_message(monkeypatch):
    async def go(env):
        await env.poll()
        (panel,) = env.posts
        again = Realms(env.cog.bot, environ=ALL_ON, pinger=env.pinger, rcon=env.rcon, reader=env.reader)
        await again.poll_once()
        assert len(env.posts) == 1 and len(panel.edits) == 1  # same message, found through meta
        await again.poll_once()
        assert len(panel.edits) == 1
        panel.deleted = True
        env.players("Steve")
        await again.poll_once()
        assert len(env.posts) == 2 and env.posts[1].pinned
        assert await again.meta_get(cogmod.PANEL_KEY) == f"{env.guild.realms.id}:{env.posts[1].id}"
    with_env(go, monkeypatch)


def test_offline_is_shown_once_and_polls_back_off(monkeypatch, caplog):
    async def go(env):
        env.players("Steve")
        await env.poll()
        (panel,) = env.posts
        env.ping = cogmod.PingError("ConnectionRefusedError")
        down_at = env.t + 120
        with caplog.at_level(logging.INFO):
            await env.poll(120)
            assert panel.edits == [] and env.cog.down_since is None  # one blip is not "offline"
            assert env.cog.next_poll_at == env.t + 30
            await env.poll(30)
            assert len(panel.edits) == 1 and panel.text.startswith(f"🔴 **Offline** since <t:{down_at}:R>")
            assert env.cog.health_text() == f"unreachable since <t:{down_at}:f>"
            delays = []
            for _ in range(6):
                delay = env.cog.next_poll_at - env.t
                delays.append(delay)
                await env.poll(delay)
            assert delays == [120, 240, 480, 900, 900, 900]
            assert len(panel.edits) == 1 and len(env.posts) == 1  # offline: not one more edit
        lines = [r.getMessage() for r in caplog.records]
        assert len([m for m in lines if "did not answer" in m]) == 1  # and not one more log line
        assert len([m for m in lines if "not answering" in m]) == 1
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert env.rcon.sent("list") == ["list"]  # RCON is left alone while the ping fails
        env.players()
        await env.poll(900)
        assert len(panel.edits) == 2 and "🟢 **Online**" in panel.text and env.cog.down_since is None
        assert env.cog.next_poll_at == env.t + R.POLL_EVERY and env.cog.health_text().startswith("ok (")
    with_env(go, monkeypatch)


def test_no_panel_before_the_first_answer_and_none_without_a_channel(monkeypatch):
    async def go(env):
        env.ping = cogmod.PingError("TimeoutError")
        await env.poll()
        assert env.posts == []
        await env.poll(30)
        assert len(env.posts) == 1 and env.posts[0].text.startswith("🔴 **Offline**")
    with_env(go, monkeypatch)

    async def no_channel(env):
        await env.poll()
        await env.poll()
        assert await env.cog.refresh_panel() == "missing"
        assert await env.cog.meta_get(cogmod.PANEL_KEY) is None
    with_env(no_channel, monkeypatch, with_channel=False)


def test_panel_failures_are_quiet_and_retried(monkeypatch, caplog):
    async def go(env):
        env.guild.realms.fail_send = forbidden()
        with caplog.at_level(logging.INFO):
            await env.poll()
            await env.poll()
            await env.poll()
        assert len([r for r in caplog.records if "posting the status panel failed" in r.getMessage()]) == 1
        env.guild.realms.fail_send = None
        env.guild.realms.fail_pin = forbidden()
        await env.poll()
        assert len(env.posts) == 1 and not env.posts[0].pinned  # the pin is a nicety
        env.guild.realms.fail_edit = forbidden()
        env.players("Steve")
        await env.poll()
        assert len(env.posts) == 1  # a failed edit never becomes a second panel
        env.guild.realms.fail_edit = None
        await env.poll()
        assert len(env.posts[0].edits) == 1 and "Steve" in env.posts[0].text
    with_env(go, monkeypatch)


def test_panel_never_shows_hostile_status_text(monkeypatch):
    async def go(env):
        env.ping = {"version": {"name": "**Paper** @everyone"},
                    "description": f"{SECTION}c@here [click](http://evil.example)\n# big",
                    "players": {"online": 3, "max": 20, "sample": [
                        {"name": "@everyone"}, {"name": "x; op me"}, {"name": "Real_One"}, {"name": "<@1>"}]}}
        env.rcon.down = True  # names come from the ping's sample
        await env.poll()
        text = env.posts[0].text
        assert "@everyone" not in text and "@here" not in text and "evil.example" not in text
        assert "Real\\_One and 2 more" in text and "op me" not in text and "\\*\\*Paper\\*\\*" in text
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- /smp link
def test_link_adds_to_the_whitelist(monkeypatch):
    async def go(env):
        reply = await env.link(U1, "Steve_01")
        assert reply["content"] == MESSAGES["linked"].format(name="Steve_01")
        assert reply["ephemeral"] is True and reply["allowed_mentions"].users is False
        assert env.rcon.commands == ["whitelist add Steve_01"] and env.rcon.whitelist == {"steve_01"}
        assert await env.links() == [("Steve_01", U1, "ok")]
        assert env.mod_lines() == [f"⛏️ <@{U1}> linked the Minecraft name `Steve_01`"]
        assert (await env.link(U1, "steve_01"))["content"] == MESSAGES["already"].format(name="Steve_01")
        assert env.rcon.commands == ["whitelist add Steve_01"]
    with_env(go, monkeypatch)


def test_hostile_names_never_reach_rcon_and_are_never_echoed(monkeypatch):
    async def go(env):
        names = ["x; op me", "Steve\nop Steve", "Steve op", "@a", "a" * 10_000, "", "ab", "Stève", "Steve\x00",
                 '{"text":"x"}', "../ops", " Steve ", "Steve;stop", "Steve\rstop", SECTION + "aSteve"]
        for name in names:
            reply = await env.link(U1, name)
            assert reply["content"] == MESSAGES["bad_name"], name
        assert env.rcon.commands == [] and await env.links() == [] and env.mod_lines() == []
    with_env(go, monkeypatch)


def test_link_needs_an_established_account(monkeypatch):
    async def go(env):
        reply = await env.link(YOUNG, "Steve")
        assert reply["content"] == MESSAGES["young"] and "30+" in reply["content"]
        assert env.rcon.commands == [] and await env.links() == []
    with_env(go, monkeypatch)


def test_link_needs_two_weeks_in_the_server(monkeypatch):
    async def go(env):
        assert R.LINK_MEMBER_DAYS == 14
        env.member(U1, joined_days=13)  # an old account that only just joined
        reply = await env.link(U1, "Steve")
        assert reply["content"] == MESSAGES["new_here"] and "14 days" in reply["content"]
        env.member(U2, joined_days=None)  # Discord didn't say when: not taken on trust
        assert (await env.link(U2, "Alex"))["content"] == MESSAGES["new_here"]
        assert env.rcon.commands == [] and await env.links() == [] and env.mod_lines() == []
        env.t = T0 + 2 * 86400  # now two weeks in
        assert (await env.link(U1, "Steve"))["content"] == MESSAGES["linked"].format(name="Steve")
    with_env(go, monkeypatch)


def test_a_name_somebody_else_whitelisted_cannot_be_linked_or_removed(monkeypatch):
    """Name squatting: a name put on the whitelist by hand is not the bot's to give or take."""
    async def go(env):
        env.rcon.whitelist = {"ownerfriend"}
        reply = await env.link(U1, "OwnerFriend")
        assert reply["content"] == MESSAGES["claimed"].format(name="OwnerFriend") and "ask a mod" in reply["content"]
        assert await env.links() == [] and env.rcon.whitelist == {"ownerfriend"}
        (line,) = env.mod_lines()  # one line, and it is not "linked"
        assert f"<@{U1}> tried to link `OwnerFriend`" in line and "Not linked" in line and "/smp adopt" in line
        # nothing the member can do now sends a remove for it
        assert (await env.unlink(U1))["content"] == MESSAGES["none"]
        await env.cog.on_member_ban(env.guild, SimpleNamespace(id=U1))
        await env.cog.on_member_remove(env.member(U1))
        await env.poll()
        assert env.rcon.sent("whitelist remove") == [] and env.rcon.whitelist == {"ownerfriend"}
        assert len(env.mod_lines()) == 1
    with_env(go, monkeypatch)


def test_a_squat_queued_while_the_server_is_down_never_becomes_a_removal(monkeypatch):
    async def go(env):
        env.rcon.whitelist = {"ownerfriend"}
        env.rcon.down = True
        assert (await env.link(U1, "OwnerFriend"))["content"] == MESSAGES["pending"].format(name="OwnerFriend")
        # unlinking (or leaving, or a ban) an unconfirmed link forgets it: no `whitelist remove` is queued
        reply = await env.unlink(U1)
        assert reply["content"] == MESSAGES["unlink_unconfirmed"].format(name="OwnerFriend")
        assert await env.links() == []
        assert (await env.link(U2, "OwnerFriend"))["content"] == MESSAGES["pending"].format(name="OwnerFriend")
        await env.cog.on_member_ban(env.guild, SimpleNamespace(id=U2))
        assert await env.links() == [] and "so the whitelist is unchanged" in env.mod_lines()[-1]
        assert (await env.link(U3, "OwnerFriend"))["content"] == MESSAGES["pending"].format(name="OwnerFriend")
        env.rcon.down = False
        await env.poll()  # the server answers "already whitelisted": refused, with one mod-log line
        assert await env.links() == [] and env.cog.waiting == 0
        assert f"<@{U3}> tried to link `OwnerFriend`" in env.mod_lines()[-1]
        await env.poll()
        assert env.rcon.sent("whitelist remove") == [] and env.rcon.whitelist == {"ownerfriend"}
        assert sum("tried to link" in line for line in env.mod_lines()) == 1
    with_env(go, monkeypatch)


def test_only_names_the_bot_added_are_marked_managed(monkeypatch):
    async def go(env):
        async def managed():
            return {r["name"]: r["managed"] for r in await env.db.fetchall("SELECT name, managed FROM realms_links")}
        env.rcon.down = True
        await env.link(U1, "Steve")
        assert await managed() == {"Steve": 0}
        env.rcon.down = False
        await env.poll()
        assert await managed() == {"Steve": 1}  # the server said "Added Steve"
        # a hand-made 'remove' row for a name the bot never added is dropped without a command
        await env.db.execute("INSERT INTO realms_links (name_key, name, user_id, state, attempts, linked_at,"
                             " updated_at) VALUES ('ghost', 'Ghost', ?, 'remove', 0, ?, ?)", (U3, T0, T0))
        env.rcon.whitelist.add("ghost")
        await env.poll()
        assert await managed() == {"Steve": 1} and env.rcon.sent("whitelist remove") == []
    with_env(go, monkeypatch)


def test_staff_adopt_a_hand_whitelisted_name_for_a_member(monkeypatch):
    async def go(env):
        env.rcon.whitelist = {"ownerfriend"}
        env.member(U1, joined_days=1)  # staff vouch: the two-week wait is not theirs to serve
        reply = await env.adopt(U2, U1, "OwnerFriend", roles=())
        assert "Only mods" in reply["content"] and await env.links() == [] and env.rcon.commands == []
        reply = await env.adopt(MOD, U1, "OwnerFriend")
        assert reply["content"].startswith(f"Linked `OwnerFriend` to <@{U1}>") and reply["ephemeral"] is True
        assert reply["allowed_mentions"].users is False
        assert await env.links() == [("OwnerFriend", U1, "ok")]
        assert env.mod_lines() == [
            f"⛏️ <@{MOD}> linked the Minecraft name `OwnerFriend` to <@{U1}> (`/smp adopt`: it was on the "
            "whitelist already, and is now managed like any other link)"]
        # from here on it is the bot's: leaving takes it off
        await env.cog.on_member_remove(env.member(U1))
        assert env.rcon.whitelist == set() and env.rcon.sent("whitelist remove") == ["whitelist remove OwnerFriend"]
    with_env(go, monkeypatch)


def test_adopt_refuses_bad_names_taken_names_and_an_unreachable_server(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        assert (await env.adopt(MOD, U2, "x; op me"))["content"] == MESSAGES["bad_name"]
        assert "is linked to" in (await env.adopt(MOD, U2, "steve"))["content"]
        assert "already has `Steve` linked" in (await env.adopt(MOD, U1, "Alex"))["content"]
        env.rcon.unknown = {"Nobody_Here"}
        assert (await env.adopt(MOD, U2, "Nobody_Here"))["content"] == MESSAGES["unknown"]
        env.rcon.down = True
        assert "can't reach the Minecraft server" in (await env.adopt(MOD, U2, "Alex"))["content"]
        env.rcon.down, env.rcon.garble = False, True
        assert (await env.adopt(MOD, U2, "Alex"))["content"] == MESSAGES["failed"]
        assert await env.links() == [("Steve", U1, "ok")] and len(env.mod_lines()) == 1
        bot_member = env.member(999_000, bot=True)
        inter = FakeInteraction(env.member(MOD, roles=(config.MOD_ROLE,)), env.guild)
        await Realms.adopt.callback(env.cog, inter, bot_member, "Alex")
        assert "Bots" in inter.reply()["content"]
    with_env(go, monkeypatch)


def test_one_name_per_member_and_one_member_per_name(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        assert (await env.link(U2, "STEVE"))["content"] == MESSAGES["taken"]  # names ignore case
        assert (await env.link(U1, "Alex"))["content"] == MESSAGES["have_one"].format(name="Steve")
        assert (await env.link(U2, "Alex"))["content"] == MESSAGES["linked"].format(name="Alex")
        assert env.rcon.sent("whitelist") == ["whitelist add Steve", "whitelist add Alex"]
        assert await env.links() == [("Alex", U2, "ok"), ("Steve", U1, "ok")]
    with_env(go, monkeypatch)


def test_link_has_a_cooldown(monkeypatch):
    async def go(env):
        env.rcon.unknown = {"Typo1", "Typo2"}
        assert (await env.link(U1, "Typo1"))["content"] == MESSAGES["unknown"]
        assert (await env.link(U1, "Typo2", wait=False))["content"] == MESSAGES["cooldown"]
        assert env.rcon.commands == ["whitelist add Typo1"]
        assert (await env.link(U1, "Typo2"))["content"] == MESSAGES["unknown"]
    with_env(go, monkeypatch)


def test_a_name_minecraft_does_not_know_is_not_kept(monkeypatch):
    async def go(env):
        env.rcon.unknown = {"Nobody_Here"}
        assert (await env.link(U1, "Nobody_Here"))["content"] == MESSAGES["unknown"]
        assert await env.links() == [] and env.mod_lines() == []
        assert (await env.link(U1, "Steve"))["content"] == MESSAGES["linked"].format(name="Steve")
    with_env(go, monkeypatch)


def test_link_while_the_server_is_down_waits_and_syncs_later(monkeypatch):
    async def go(env):
        env.rcon.down = True
        reply = await env.link(U1, "Steve")
        assert reply["content"] == MESSAGES["pending"].format(name="Steve")
        assert await env.links() == [("Steve", U1, "add")] and env.rcon.whitelist == set()
        assert "(waiting for the server)" in env.mod_lines()[0]
        assert "waiting for the server" in (await env.status(U1))["embed"].description
        await env.poll()  # still down
        assert await env.links() == [("Steve", U1, "add")]
        assert "RCON unreachable since" in env.cog.health_text()
        env.rcon.down = False
        await env.poll()
        assert await env.links() == [("Steve", U1, "ok")] and env.rcon.whitelist == {"steve"}
        assert env.cog.waiting == 0 and env.cog.health_text().startswith("ok (") and ";" not in env.cog.health_text()
    with_env(go, monkeypatch)


def test_an_unexpected_whitelist_answer_is_retried_then_slowed(monkeypatch):
    async def go(env):
        env.rcon.garble = True
        assert (await env.link(U1, "Steve"))["content"] == MESSAGES["pending"].format(name="Steve")
        for _ in range(10):
            await env.poll()
        assert len(env.rcon.sent("whitelist add")) == R.MAX_ATTEMPTS  # then once an hour, not every poll
        assert "1 whitelist change waiting" in env.cog.health_text()
        await env.poll(R.SLOW_RETRY)
        assert len(env.rcon.sent("whitelist add")) == R.MAX_ATTEMPTS + 1
        env.rcon.garble = False
        await env.poll(R.SLOW_RETRY)
        assert await env.links() == [("Steve", U1, "ok")]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- unlink, leave, ban
def test_unlink_removes_from_the_whitelist(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        reply = await env.unlink(U1)
        assert reply["content"] == MESSAGES["unlinked"].format(name="Steve") and reply["ephemeral"] is True
        assert env.rcon.sent("whitelist") == ["whitelist add Steve", "whitelist remove Steve"]
        assert env.rcon.whitelist == set() and await env.links() == []
        assert (await env.unlink(U1))["content"] == MESSAGES["none"]
        assert (await env.link(U1, "Alex"))["content"] == MESSAGES["linked"].format(name="Alex")
    with_env(go, monkeypatch)


def test_unlink_while_down_keeps_the_removal_until_it_is_done(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        env.rcon.down = True
        assert (await env.unlink(U1))["content"] == MESSAGES["unlink_pending"].format(name="Steve")
        assert await env.links() == [("Steve", U1, "remove")] and env.rcon.whitelist == {"steve"}
        # the member is free to link another name; the old one still comes off
        assert (await env.link(U1, "Alex"))["content"] == MESSAGES["pending"].format(name="Alex")
        assert await env.links() == [("Alex", U1, "add"), ("Steve", U1, "remove")]
        env.rcon.down = False
        await env.poll()
        assert await env.links() == [("Alex", U1, "ok")] and env.rcon.whitelist == {"alex"}
    with_env(go, monkeypatch)


def test_a_name_waiting_for_removal_can_be_claimed_again(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        env.rcon.down = True
        await env.unlink(U1)
        assert (await env.link(U2, "Steve"))["content"] == MESSAGES["pending"].format(name="Steve")
        assert await env.links() == [("Steve", U2, "add")]
        env.rcon.down = False
        await env.poll()
        assert await env.links() == [("Steve", U2, "ok")] and env.rcon.whitelist == {"steve"}
        assert env.rcon.sent("whitelist remove") == []
    with_env(go, monkeypatch)


def test_members_who_leave_or_are_banned_come_off_the_whitelist(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        await env.link(U2, "Alex")
        await env.cog.on_member_remove(env.member(U1))
        assert env.rcon.whitelist == {"alex"} and await env.links() == [("Alex", U2, "ok")]
        assert env.mod_lines()[-1] == f"⛏️ <@{U1}> left: `Steve` is off the Monkey SMP whitelist"
        banned = SimpleNamespace(id=U2)
        await env.cog.on_member_ban(env.guild, banned)
        await env.cog.on_member_remove(env.member(U2))  # Discord sends both for a ban
        assert env.rcon.whitelist == set() and await env.links() == []
        assert env.rcon.sent("whitelist remove") == ["whitelist remove Steve", "whitelist remove Alex"]
        assert env.mod_lines()[-1] == f"⛏️ <@{U2}> was banned: `Alex` is off the Monkey SMP whitelist"
        await env.cog.on_member_remove(env.member(U3))  # no link: nothing happens
        assert len(env.rcon.sent("whitelist remove")) == 2
    with_env(go, monkeypatch)


def test_other_servers_events_are_ignored_and_a_ban_while_down_waits(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        other = FakeGuild(gid=555)
        await env.cog.on_member_remove(FakeMember(U1, other))
        await env.cog.on_member_ban(other, SimpleNamespace(id=U1))
        assert await env.links() == [("Steve", U1, "ok")]
        env.rcon.down = True
        await env.cog.on_member_ban(env.guild, SimpleNamespace(id=U1))
        assert await env.links() == [("Steve", U1, "remove")]
        assert "comes off the Monkey SMP whitelist when the server answers" in env.mod_lines()[-1]
        env.rcon.down = False
        await env.poll()
        assert await env.links() == [] and env.rcon.whitelist == set()
    with_env(go, monkeypatch)


def test_sweep_catches_members_who_left_while_the_bot_was_off(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        await env.link(U2, "Alex")
        del env.guild.members[U1]
        env.guild.chunked = False
        assert await env.cog.sweep_departed() == 0  # a cold member cache must not unlink anyone
        assert len(await env.links()) == 2
        env.guild.chunked = True
        await env.poll(R.SWEEP_EVERY)  # the poll runs the sweep
        assert await env.links() == [("Alex", U2, "ok")] and env.rcon.whitelist == {"alex"}
    with_env(go, monkeypatch)


def test_staff_can_remove_a_link_and_members_cannot(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        reply = await env.remove(U2, U1, roles=())
        assert "Only mods" in reply["content"] and await env.links() == [("Steve", U1, "ok")]
        reply = await env.remove(MOD, U1)
        assert reply["content"] == f"Removed <@{U1}>'s Minecraft link." and reply["allowed_mentions"].users is False
        assert await env.links() == [] and f"was unlinked by <@{MOD}>" in env.mod_lines()[-1]
        assert "has no Minecraft link" in (await env.remove(MOD, U1))["content"]
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- Discord -> Minecraft
def test_discord_chat_reaches_minecraft_as_tellraw(monkeypatch):
    async def go(env):
        env.member(U1, name="Lori")
        result, msg = await env.say(U1, "anyone on?")
        assert result == "sent" and msg.reactions == []
        assert env.tellraws() == [["", {"text": "[Discord] ", "color": "aqua"}, {"text": "<Lori> anyone on?"}]]
    with_env(go, monkeypatch)


def test_bridge_uses_the_old_channel_name_until_the_rename(monkeypatch):
    async def go(env):
        old = FakeText(98, config.REALMS_CHANNEL_WAS[0], env.guild)
        env.guild.text_channels.append(old)
        assert env.cog.channel() is old  # not renamed yet
        env.guild.text_channels.append(env.guild.realms)
        assert env.cog.channel() is env.guild.realms  # the new name wins once it exists
    with_env(go, monkeypatch, with_channel=False)


def test_only_members_messages_in_the_bridge_channel_are_relayed(monkeypatch):
    async def go(env):
        bot_user = FakeMember(U2, env.guild, bot=True)
        assert (await env.say(U2, "beep", author=bot_user))[0] == "bot"
        assert (await env.say(U1, "hook", webhook_id=77))[0] == "bot"
        assert (await env.say(U1, "wrong room", channel=env.guild.general))[0] == "elsewhere"
        # Only the bridge channel itself, by ID: a same-named channel (or one under the old name) doesn't relay.
        twin = FakeText(99, config.REALMS_CHANNEL, env.guild)
        old = FakeText(98, config.REALMS_CHANNEL_WAS[0], env.guild)
        env.guild.text_channels += [twin, old]
        assert (await env.say(U1, "twin", channel=twin))[0] == "elsewhere"
        assert (await env.say(U1, "old", channel=old))[0] == "elsewhere"
        other = FakeGuild(gid=555)
        assert (await env.say(U1, "wrong server", channel=other.realms))[0] == "elsewhere"
        assert (await env.say(U1, "pinned", type=discord.MessageType.pins_add))[0] == "system"
        assert (await env.say(U1, "   \n "))[0] == "empty"
        assert env.rcon.commands == []
        assert (await env.say(U1, "", attachments=[object()]))[0] == "sent"
        assert env.tellraws()[0][2]["text"].endswith("[attachment]")
    with_env(go, monkeypatch)


def test_nothing_a_member_types_can_become_a_command(monkeypatch):
    async def go(env):
        env.member(U1, name='Ev"il\n/op me ' + SECTION + "k")
        texts = ['"}]\n/op me', "hi\n/stop", "a\r\nwhitelist add Evil", SECTION + "kobfuscated", "@a @e kill",
                 "x" * 10_000, "\\", '\\"', "\x00", "'; --", "😀" * 2000,
                 '{"clickEvent":{"action":"run_command","value":"/op me"}}']
        for text in texts:
            env.t += R.USER_GAP
            await env.say(U1, text)
        commands = env.rcon.sent("tellraw @a ")
        assert len(commands) == len(env.rcon.commands) == len(texts) - 1  # the lone NUL is an empty message
        for command in commands:
            assert command.isascii() and command.isprintable() and len(command) <= R.MAX_COMMAND_BYTES
            parts = json.loads(command[len("tellraw @a "):])
            assert len(parts) == 3 and set(parts[2]) == {"text"}
            assert parts[2]["text"].startswith('<Ev"il /op me> ')
            assert "\n" not in parts[2]["text"] and SECTION not in parts[2]["text"]
    with_env(go, monkeypatch)


def test_discord_chat_is_rate_limited_with_one_notice(monkeypatch):
    async def go(env):
        results = [await env.say(U1, f"spam {i}") for i in range(10)]
        assert [r for r, _ in results] == ["sent"] + ["limited"] * 9
        assert sum(len(m.reactions) for _, m in results) == 1 and results[1][1].reactions == [cogmod.SLOW_DOWN]
        assert len(env.rcon.commands) == 1
        env.t += R.USER_GAP
        assert (await env.say(U1, "calm now"))[0] == "sent"
        # everyone together is capped too
        env.t += 1000
        outcomes = [(await env.say(snowflake(T0 - 400 * 86400, 100 + i), "hi"))[0] for i in range(50)]
        assert outcomes.count("sent") == R.WINDOW_MAX and outcomes.count("limited") == 50 - R.WINDOW_MAX
    with_env(go, monkeypatch)


def test_chat_while_rcon_is_down_is_dropped_not_hammered(monkeypatch):
    async def go(env):
        env.rcon.down = True
        attempts = []

        async def counting(text):
            attempts.append(text)
            raise cogmod.RconError("no answer (ConnectionRefusedError)")

        env.rcon.command = counting
        first, msg = await env.say(U1, "hello?")
        assert first == "down" and msg.reactions == [cogmod.NOT_SENT] and len(attempts) == 1
        for i in range(5):
            env.t += R.USER_GAP
            result, later = await env.say(U1, f"hello {i}")
            assert result == "down" and later.reactions == []  # one notice a minute
        assert len(attempts) == 1  # no connection attempt per message
        env.t += R.RCON_RETRY
        await env.say(U1, "now?")
        assert len(attempts) == 2
    with_env(go, monkeypatch)


def test_chat_to_minecraft_needs_rcon(monkeypatch):
    async def go(env):
        assert (await env.say(U1, "hello"))[0] == "off"
        assert env.rcon.commands == []
    with_env(go, monkeypatch, environ={R.ENV_ADDRESS: "127.0.0.1", R.ENV_LOG_PATH: ALL_ON[R.ENV_LOG_PATH]})


def test_on_message_never_raises(monkeypatch, caplog):
    async def go(env):
        with caplog.at_level(logging.INFO):
            await env.cog.on_message(SimpleNamespace())  # nothing like a message
            await env.cog.on_message(SimpleNamespace())
        assert len(caplog.records) == 1 and PASSWORD not in caplog.text
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- Minecraft -> Discord
def test_minecraft_chat_reaches_discord_as_one_batched_message(monkeypatch):
    async def go(env):
        await env.linked(U1, "Steve")
        await env.log(P + "Steve joined the game", P + "<Steve> hello discord",
                      P + "Saving the game (this may take a moment!)", "garbage line",
                      P + "Steve has made the advancement [Stone Age]", P + "Steve was slain by Zombie",
                      P + "Steve left the game")
        (post,) = env.posts
        assert post.kwargs["content"].split("\n") == [
            "➕ **Steve** joined Monkey SMP", "**<Steve>** hello discord",
            "🏅 **Steve** has made the advancement **Stone Age**", "💀 **Steve** died",
            "➖ **Steve** left Monkey SMP"]
        mentions = post.kwargs["allowed_mentions"]
        assert mentions.everyone is False and mentions.users is False and mentions.roles is False
        await env.cog.relay_once()
        assert len(env.posts) == 1  # nothing new: nothing posted
    with_env(go, monkeypatch)


async def chat(env, *names):
    """What reaches Discord when these players each say something in Minecraft."""
    before = len(env.posts)
    await env.log(*(P + f"<{name}> hi from {name}" for name in names))
    return env.posts[-1].kwargs["content"] if len(env.posts) > before else ""


HINT = cogmod.UNLINKED_HINT


def test_only_a_confirmed_link_to_a_member_in_good_standing_is_relayed(monkeypatch):
    """The allow-list: no confirmed link, no relay. The game is not a way around Discord
    moderation, with the muted member's own name or with any other."""
    async def go(env):
        await env.link(U1, "Steve")
        await env.link(U2, "Alex")
        text = await chat(env, "Steve", "Alex", "Stranger")
        assert "hi from Steve" in text and "hi from Alex" in text
        assert "Stranger" not in text  # a name nobody linked does not relay
        env.member(U1).timed_out = True
        # the timed-out member's own name (however the server spells it), and the second,
        # unlinked account they switch to
        text = await chat(env, "Steve", "steve", "SteveAlt", "Alex")
        assert "teve" not in text and "hi from Alex" in text
        env.member(U1).timed_out = False
        assert "hi from Steve" in await chat(env, "Steve")
        assert env.guild.fetches == []  # cached members never cost an API call
    with_env(go, monkeypatch)


def test_deaths_and_advancements_go_through_the_same_gate_and_joins_do_not(monkeypatch):
    """A death line can carry an item the player renamed, an advancement title can come from a
    mod: both are a player's words. Join and leave lines are the bot's own around a name."""
    async def go(env):
        await env.linked(U1, "Steve")
        def lines(name):
            return [P + f"{name} joined the game", P + f"{name} was slain by Zombie using [free nitro]",
                    P + f"{name} has made the advancement [Totally Real Prize]", P + f"{name} left the game"]
        await env.log(*lines("Stranger"))
        assert env.posts[-1].kwargs["content"].split("\n")[:2] == [
            "➕ **Stranger** joined Monkey SMP", "➖ **Stranger** left Monkey SMP"]
        assert "nitro" not in env.posts[-1].kwargs["content"] and "Prize" not in env.posts[-1].kwargs["content"]
        await env.log(*lines("Steve"))
        text = env.posts[-1].kwargs["content"]
        assert "💀 **Steve** died" in text and "free nitro" not in text and "🏅 **Steve** has made the advancement" in text
        env.member(U1).timed_out = True
        await env.log(*lines("Steve"))
        assert env.posts[-1].kwargs["content"] == "➕ **Steve** joined Monkey SMP\n➖ **Steve** left Monkey SMP"
    with_env(go, monkeypatch)


def test_links_that_are_not_confirmed_and_managed_do_not_relay(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        env.rcon.down = True
        await env.link(U3, "Herobrine")  # waiting to be added
        await env.unlink(U1)  # waiting to come off the whitelist
        assert await env.links() == [("Herobrine", U3, "add"), ("Steve", U1, "remove")]
        assert all(not m.is_timed_out() for m in env.guild.members.values())  # everyone in good standing
        text = await chat(env, "Steve", "Herobrine")
        assert "hi from" not in text
        # a row that says 'ok' but was never the bot's own confirmed add is not enough either
        await env.db.execute("INSERT INTO realms_links (name_key, name, user_id, state, attempts, linked_at,"
                             " updated_at, managed) VALUES ('legacy', 'Legacy', ?, 'ok', 0, ?, ?, 0)", (U2, T0, T0))
        env.member(U2)
        assert "hi from" not in await chat(env, "Legacy")
        # once the server confirms the add, the same player's words come through
        env.rcon.down = False
        await env.poll()
        assert ("Herobrine", U3, "ok") in await env.links()
        assert "hi from Herobrine" in await chat(env, "Herobrine")
        # staff adopting a hand-whitelisted name makes it a confirmed link too
        env.rcon.whitelist.add("ownerfriend")
        assert (await env.adopt(MOD, U1, "OwnerFriend"))["content"].startswith("Linked `OwnerFriend`")
        assert "hi from OwnerFriend" in await chat(env, "OwnerFriend")
    with_env(go, monkeypatch)


def test_claiming_somebodys_name_neither_relays_nor_silences_anyone(monkeypatch):
    """A claim the server has not confirmed changes nothing for anybody: not for the claimed
    name (which never relayed) and not for the confirmed owner of another name."""
    async def go(env):
        await env.link(U1, "Steve")  # confirmed, in good standing
        env.rcon.whitelist.add("ownerfriend")  # whitelisted by hand: its player has no link
        env.rcon.down = True
        env.member(U3).timed_out = True  # the claimant is muted in Discord
        assert (await env.link(U3, "OwnerFriend"))["content"] == MESSAGES["pending"].format(name="OwnerFriend")
        text = await chat(env, "Steve", "OwnerFriend")
        assert "hi from Steve" in text and "OwnerFriend" not in text
        # the claimant leaving, being banned, or the claim being refused changes nothing either
        await env.cog.on_member_ban(env.guild, SimpleNamespace(id=U3))
        del env.guild.members[U3]
        assert "hi from Steve" in await chat(env, "Steve")
        env.rcon.down = False
        await env.poll()
        text = await chat(env, "Steve", "OwnerFriend")
        assert "hi from Steve" in text and "OwnerFriend" not in text
        assert await env.db.fetchall("SELECT 1 FROM meta WHERE key LIKE 'realms:removed:%'") == []  # no such record
    with_env(go, monkeypatch)


def test_a_banned_or_departed_members_session_is_not_relayed(monkeypatch):
    """Taking a name off the whitelist does not disconnect the player: before the removal is
    confirmed the row is not 'ok', after it there is no row, and neither relays."""
    async def go(env):
        await env.link(U1, "Steve")
        await env.link(U2, "Alex")
        env.rcon.down = True
        await env.cog.on_member_ban(env.guild, SimpleNamespace(id=U1))
        del env.guild.members[U1]
        assert ("Steve", U1, "remove") in await env.links()
        assert "Steve" not in await chat(env, "Steve", "Alex")
        env.rcon.down = False
        await env.poll()
        assert await env.links() == [("Alex", U2, "ok")]
        text = await chat(env, "Steve", "Alex")
        assert "Steve" not in text and "hi from Alex" in text
        # even a confirmed row whose member is gone (the leave event was missed) is not enough
        await env.linked(U3, "Ghost")
        del env.guild.members[U3]
        assert "Ghost" not in await chat(env, "Ghost", "Alex") and env.guild.fetches == [U3]
    with_env(go, monkeypatch)


def test_a_member_outside_the_cache_is_asked_from_the_api_with_short_memories(monkeypatch):
    async def go(env):
        await env.linked(U1, "Steve")
        member = env.guild.members.pop(U1)
        env.guild.chunked = False  # whatever the cache claims about itself
        # not a member: asked once, remembered for five minutes
        assert await chat(env, "Steve", "Steve", "Steve") == "" and env.guild.fetches == [U1]
        env.t += R.MEMBER_GONE_FOR - 1
        assert await chat(env, "Steve") == "" and env.guild.fetches == [U1]
        env.t += 1
        env.guild.uncached[U1] = member  # in the server after all, just not cached
        assert "hi from Steve" in await chat(env, "Steve") and len(env.guild.fetches) == 2
        # a member that was found is reused for a minute, no longer
        # They are timed out now. The object fetched earlier never hears of it (only cached
        # members are kept current), so what the API returns from here on is a new one.
        fresh = FakeMember(U1, env.guild)
        fresh.timed_out = True
        env.guild.uncached[U1] = fresh
        env.t += R.MEMBER_FRESH_FOR - 1
        assert not member.is_timed_out() and len(env.guild.fetches) == 2  # the stale copy still says "fine"
        env.t += 1  # the minute is up: fetched again, and the timeout is seen
        assert await chat(env, "Steve") == "" and len(env.guild.fetches) == 3
        assert R.MEMBER_FRESH_FOR == 60 and R.MEMBER_GONE_FOR == 300
        # Discord not answering is not a yes, and is not held against them for five minutes
        fresh.timed_out = False
        env.t += R.MEMBER_FRESH_FOR
        env.guild.fetch_fail = discord.HTTPException(SimpleNamespace(status=503, reason="x"), "down")
        assert await chat(env, "Steve") == "" and len(env.guild.fetches) == 4
        env.guild.fetch_fail = None
        env.t += R.MEMBER_FRESH_FOR
        assert "hi from Steve" in await chat(env, "Steve") and len(env.guild.fetches) == 5
        # a cached member is the gateway's word and is never fetched
        env.guild.members[U1] = member
        member.timed_out = False
        env.t += R.MEMBER_GONE_FOR
        assert "hi from Steve" in await chat(env, "Steve") and len(env.guild.fetches) == 5
    with_env(go, monkeypatch)


def test_the_unlinked_hint_is_rare_bot_worded_and_names_nobody(monkeypatch):
    async def go(env):
        await env.linked(U1, "Steve")
        assert "`/smp link`" in HINT and HINT.startswith("*") and HINT.endswith("*")
        # on its own when nothing else would be posted
        assert await chat(env, "Stranger") == HINT
        # not again for that name for six hours, however much they say
        assert await chat(env, "Stranger", "Stranger") == ""
        env.t += R.HINT_EVERY - 1
        assert await chat(env, "stranger") == ""
        # another unlinked name: one trailing line after the real chat, once for the pass
        text = await chat(env, "Other", "Steve", "Third", "Other")
        assert text.split("\n") == ["**<Steve>** hi from Steve", HINT]
        env.t += 1
        assert await chat(env, "Stranger") == HINT and R.HINT_EVERY == 6 * 3600
        # never for a linked member who is muted, and never with a name or their words in it
        env.member(U1).timed_out = True
        assert await chat(env, "Steve") == ""
        assert all(HINT == line or "Stranger" not in line
                   for p in env.posts for line in p.kwargs["content"].split("\n"))
        assert all(p.kwargs["allowed_mentions"].everyone is False for p in env.posts)
    with_env(go, monkeypatch)


def test_a_member_who_cannot_post_in_the_realms_channel_is_not_relayed(monkeypatch):
    """A timeout is not the only mute. What counts is Send Messages in the channel."""
    async def go(env):
        await env.link(U1, "Steve")
        await env.link(U2, "Alex")
        env.guild.realms.muted = {U1}  # a channel overwrite or a muted role; no timeout involved
        assert not env.member(U1).is_timed_out()
        text = await chat(env, "Steve", "Alex")
        assert "hi from Steve" not in text and "hi from Alex" in text
        env.guild.realms.muted = set()
        assert "hi from Steve" in await chat(env, "Steve")

        def broken(member):
            raise RuntimeError("no permissions today")
        env.guild.realms.permissions_for = broken  # can't tell: not relayed, and nothing blows up
        assert await chat(env, "Steve") == ""
    with_env(go, monkeypatch)


def test_discord_py_takes_send_messages_from_a_timed_out_member():
    """What the Send Messages test leans on, checked against the installed library: permissions_for
    leaves a timed-out member only View Channel and Read Message History."""
    import inspect

    from discord import abc
    source = inspect.getsource(abc.GuildChannel.permissions_for)
    assert "is_timed_out()" in source and "_timeout_mask()" in source
    left = discord.Permissions(discord.Permissions.all().value & discord.Permissions._timeout_mask())
    assert left == discord.Permissions(view_channel=True, read_message_history=True) and not left.send_messages


def test_the_recently_removed_record_is_gone(monkeypatch):
    """The allow-list needs no memory of removed names: without a confirmed link nothing relays."""
    async def go(env):
        await env.link(U1, "Steve")
        await env.unlink(U1)
        await env.poll(R.SWEEP_EVERY)
        assert await env.db.fetchall("SELECT key FROM meta WHERE key LIKE 'realms:%removed%'") == []
        for gone in ("removed_key", "removed_value", "removed_member", "REMOVED_KEEP", "REMOVED_PREFIX"):
            assert not hasattr(R, gone), gone
        for gone in ("silenced", "name_owner", "forget_old_removals"):
            assert not hasattr(env.cog, gone), gone
        # unlinking is the member's own choice, and it does end the relay: they said so in Discord
        assert "hi from Steve" not in await chat(env, "Steve")
    with_env(go, monkeypatch)


def test_minecraft_text_is_hostile_input(monkeypatch):
    async def go(env):
        await env.linked(U1, "Steve")
        await env.log(P + f"<Steve> {SECTION}4@everyone free nitro **now** <@&123456789012345678> ||x|| `y`",
                      P + "<Steve> join discord.gg/abcdef", P + "<Steve> http://evil.example/login",
                      P + "<Steve> [click me](https://evil.example)", P + "<x; op me> hi",
                      P + "<Steve> " + "A" * 10_000, P + "<Steve> " + "B" * 1000)
        (post,) = env.posts
        text = post.kwargs["content"]
        assert "@everyone" not in text and "<@&123456789012345678>" not in text and SECTION not in text
        assert "discord.gg" not in text and "evil.example" not in text and "op me" not in text
        assert "\\*\\*now\\*\\*" in text and "AAAA" not in text  # the 10k line was over the line cap
        assert text.count("\n") == 1 and len(text) < 800 and text.endswith("B…")
        assert post.kwargs["allowed_mentions"].everyone is False
    with_env(go, monkeypatch)


def test_a_chat_flood_cannot_make_the_bot_spam(monkeypatch):
    async def go(env):
        await env.linked(U1, "Steve")
        flood = [P + f"<Steve> spam number {i}" for i in range(2000)]
        await env.log(*flood)
        assert len(env.posts) == 1  # one message per pass, whatever arrived
        passes = 1
        while env.cog.batcher.lines:
            await env.cog.relay_once()
            passes += 1
        assert passes == len(env.posts) <= R.MAX_BACKLOG // R.MAX_BATCH_LINES + 1
        assert all(len(p.kwargs["content"]) <= 2000 for p in env.posts)
        assert env.posts[0].kwargs["content"].startswith(f"*… {2000 - R.MAX_BACKLOG} earlier lines skipped*")
        assert env.posts[-1].kwargs["content"].endswith("spam number 1999")
    with_env(go, monkeypatch)


def test_relayed_discord_chat_never_loops_back(monkeypatch):
    async def go(env):
        env.member(U1, name="Lori")
        await env.linked(U2, "Steve")
        await env.say(U1, "hello minecraft")
        # if the server did log the tellraw (or a player retyped it), it is dropped
        await env.log(P + "[Discord] <Lori> hello minecraft", P + "<Steve> [Discord] <Lori> hello minecraft")
        assert env.posts == []
        # and what the bot posts from Minecraft is a bot message, which is never sent to Minecraft
        await env.log(P + "<Steve> hi back")
        (post,) = env.posts
        me = FakeMember(U3, env.guild, bot=True)
        assert (await env.say(U3, post.kwargs["content"], author=me))[0] == "bot"
        assert len(env.rcon.sent("tellraw")) == 1
    with_env(go, monkeypatch)


def test_log_lines_split_across_reads_rotation_and_a_missing_file(monkeypatch, caplog):
    async def go(env):
        await env.linked(U1, "Steve")
        env.log_feed += [(P + "<Steve> first ha").encode(), b"lf\n" + (P + "<Steve> sec").encode()]
        await env.cog.relay_once()
        assert env.posts == []
        await env.cog.relay_once()
        assert env.posts[0].kwargs["content"] == "**<Steve>** first half"
        # the file was rotated: the half-read line is dropped, the new file is read from its top
        await env.log(P + "<Steve> after restart", reset=True)
        assert env.posts[1].kwargs["content"] == "**<Steve>** after restart"
        with caplog.at_level(logging.INFO):
            env.log_feed += [None, None, None]
            for _ in range(3):
                await env.cog.relay_once()
        assert len([r for r in caplog.records if "can't be read" in r.getMessage()]) == 1
        assert "log file not found" in env.cog.health_text()
        await env.log(P + "<Steve> back")
        assert env.posts[2].kwargs["content"] == "**<Steve>** back" and "log file" not in env.cog.health_text()
    with_env(go, monkeypatch)


def test_minecraft_chat_without_a_channel_or_with_send_failures(monkeypatch, caplog):
    async def go(env):
        await env.linked(U1, "Steve")
        env.guild.realms.fail_send = forbidden()
        with caplog.at_level(logging.INFO):
            for i in range(3):
                await env.log(P + f"<Steve> {i}")
        assert len([r for r in caplog.records if "posting Minecraft chat failed" in r.getMessage()]) == 1
        env.guild.realms.fail_send = None
        await env.log(P + "<Steve> works again")
        assert env.posts[-1].kwargs["content"] == "**<Steve>** works again"
    with_env(go, monkeypatch)

    async def no_channel(env):
        await env.linked(U1, "Steve")
        await env.log(*[P + f"<Steve> {i}" for i in range(100)])
        assert not env.cog.batcher.lines  # nowhere to post: nothing is hoarded
    with_env(no_channel, monkeypatch, with_channel=False)


def test_a_join_in_the_log_brings_the_next_poll_forward(monkeypatch):
    async def go(env):
        await env.poll()
        assert env.cog.next_poll_at == env.t + R.POLL_EVERY
        await env.log(P + "<Steve> chatting does not")
        assert env.cog.next_poll_at == env.t + R.POLL_EVERY
        await env.log(P + "Steve joined the game")
        assert env.cog.next_poll_at == env.t + R.TICK
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- playtime and coins
PAL = "Pal"  # someone else on the server (not linked): time only counts in company


async def play(env, minutes, *names):
    env.players(*names)
    for _ in range(minutes * 60 // R.POLL_EVERY):
        await env.poll()


def test_playtime_counts_between_polls_and_pays_per_half_hour(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        env.players("Steve", PAL)
        await env.poll()
        assert await env.seconds(U1) is None  # seen once: nothing is known yet
        await env.poll()
        assert await env.seconds(U1) == 120
        await play(env, 26, "Steve", PAL)
        assert await env.seconds(U1) == 28 * 60 and await env.balance(U1) == 0
        await env.poll()
        assert await env.seconds(U1) == 30 * 60 and await env.balance(U1) == R.SLOT_COINS
        rows = await env.db.fetchall("SELECT reason, ref, delta FROM ledger WHERE user_id = ?", (U1,))
        assert [(r["reason"], r["ref"], r["delta"]) for r in rows] == [("realms", f"realms:{U1}:{DAY}:0", 5)]
        await play(env, 30, "Steve", PAL)
        assert await env.balance(U1) == 2 * R.SLOT_COINS
    with_env(go, monkeypatch)


def test_playtime_has_a_hard_daily_cap(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        await play(env, 7 * 60, "Steve", PAL)  # 08:00 to 15:00, all the same local day
        assert await env.seconds(U1) > 6 * 3600
        assert await env.balance(U1) == R.SLOT_COINS * R.DAILY_SLOTS == 40
        rows = await env.db.fetchall("SELECT ref FROM ledger WHERE user_id = ? ORDER BY id", (U1,))
        assert [r["ref"] for r in rows] == [f"realms:{U1}:{DAY}:{slot}" for slot in range(R.DAILY_SLOTS)]
    with_env(go, monkeypatch)


def test_time_alone_on_the_server_counts_for_nothing(monkeypatch):
    async def go(env):
        assert R.MIN_PLAYERS == 2  # company, like voice coins
        await env.link(U1, "Steve")
        await play(env, 3 * 60, "Steve")  # three hours with nobody else on
        assert await env.seconds(U1) is None and await env.balance(U1) == 0
        env.players("Steve", PAL)
        await env.poll()  # company arrived at some point since the last poll: that stretch isn't counted
        assert await env.seconds(U1) is None
        await env.poll()
        assert await env.seconds(U1) == 120  # both seen in two polls in a row
        env.players("Steve", "Other")
        await env.poll()  # a different second player: nobody else was there for the whole stretch
        assert await env.seconds(U1) == 120
        env.players("Steve")
        await env.poll()  # the company left
        await env.poll()
        assert await env.seconds(U1) == 120 and await env.balance(U1) == 0
        rows = await env.db.fetchall("SELECT 1 FROM ledger WHERE reason = ?", (R.REASON,))
        assert rows == []
    with_env(go, monkeypatch)


def test_playtime_comes_from_the_servers_list_only(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        await env.link(U2, "Alex")
        await play(env, 10, "steve", "Stranger")  # the server's spelling may differ in case
        assert await env.seconds(U1) == 8 * 60 and await env.seconds(U2) is None
        rows = await env.db.fetchall("SELECT user_id FROM realms_playtime")
        assert [r["user_id"] for r in rows] == [U1]  # an unlinked player is nobody
        # saying so in chat, or in the log, earns nothing
        await env.say(U2, "Alex joined the game")
        await env.log(P + "<Alex> I am online, honest", P + "Alex joined the game")
        assert await env.seconds(U2) is None
    with_env(go, monkeypatch)


def test_a_gap_or_a_failed_poll_is_never_credited(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        await play(env, 4, "Steve", PAL)
        assert await env.seconds(U1) == 120
        await env.poll(R.MAX_GAP + 1)  # the bot was away for a while
        assert await env.seconds(U1) == 120
        await env.poll()
        assert await env.seconds(U1) == 240
        env.ping = cogmod.PingError("TimeoutError")
        await env.poll()
        env.players("Steve", PAL)
        await env.poll(30)  # back, but the chain was broken
        assert await env.seconds(U1) == 240
        await env.poll()
        assert await env.seconds(U1) == 360
        env.players(PAL)
        await env.poll()
        env.players("Steve", PAL)
        await env.poll()  # left and came back between polls: only seen-twice time counts
        assert await env.seconds(U1) == 360
    with_env(go, monkeypatch)


def test_opted_out_members_are_not_tracked_or_paid(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        await env.link(U2, "Alex")
        await play(env, 10, "Steve", "Alex")
        assert await env.seconds(U1) == await env.seconds(U2) == 8 * 60
        await env.opt_out(U1)
        await play(env, 60, "Steve", "Alex")
        assert await env.seconds(U1) is None and await env.balance(U1) == 0  # old rows are deleted too
        assert await env.seconds(U2) == 68 * 60 and await env.balance(U2) == 2 * R.SLOT_COINS
        top = (await env.top(U2))["embed"].description
        assert f"<@{U2}>" in top and f"<@{U1}>" not in top
        assert "isn't counted" in (await env.status(U1))["embed"].description
        assert await env.links() == [("Alex", U2, "ok"), ("Steve", U1, "ok")]  # the link itself is not tracking
    with_env(go, monkeypatch)


def test_paid_half_hours_are_not_paid_again_after_a_privacy_round_trip(monkeypatch):
    from cogs.stats import opt_out

    async def go(env):
        async def round_trip():
            await opt_out(env.db, U1, env.t)  # /privacy tracking:off ...
            await env.db.execute("DELETE FROM privacy_optout WHERE user_id = ?", (U1,))  # ... and on again
        await env.link(U1, "Steve")
        await play(env, 94, "Steve", PAL)
        assert await env.seconds(U1) == 92 * 60 and await env.balance(U1) == 3 * R.SLOT_COINS
        await round_trip()
        assert await env.seconds(U1) is None  # the playtime is deleted...
        await play(env, 94, "Steve", PAL)  # ...and the same three half hours, played again, pay nothing
        assert await env.balance(U1) == 3 * R.SLOT_COINS
        await play(env, 30, "Steve", PAL)
        assert await env.balance(U1) == 4 * R.SLOT_COINS  # only a half hour never paid before does
        # the daily cap holds across any number of round trips
        for _ in range(3):
            await round_trip()
            await play(env, 60, "Steve", PAL)
        await play(env, 5 * 60, "Steve", PAL)
        assert await env.balance(U1) == R.SLOT_COINS * R.DAILY_SLOTS
        rows = await env.db.fetchall("SELECT ref FROM ledger WHERE user_id = ? ORDER BY id", (U1,))
        assert [r["ref"] for r in rows] == [f"realms:{U1}:{DAY}:{slot}" for slot in range(R.DAILY_SLOTS)]
    with_env(go, monkeypatch)


def test_young_accounts_and_people_who_left_earn_nothing(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        env.member(YOUNG)
        await env.db.execute("INSERT INTO realms_links (name_key, name, user_id, state, attempts, linked_at,"
                             " updated_at) VALUES ('newbie', 'Newbie', ?, 'ok', 0, ?, ?)", (YOUNG, T0, T0))
        await env.db.execute("INSERT INTO realms_links (name_key, name, user_id, state, attempts, linked_at,"
                             " updated_at) VALUES ('ghost', 'Ghost', ?, 'ok', 0, ?, ?)", (U3, T0, T0))
        env.guild.chunked = False  # (so the sweep leaves the ghost's row for this test)
        await play(env, 70, "Steve", "Newbie", "Ghost")
        assert await env.balance(U1) == 2 * R.SLOT_COINS
        assert await env.seconds(YOUNG) == 68 * 60 and await env.balance(YOUNG) == 0
        assert await env.seconds(U3) is None and await env.balance(U3) == 0
    with_env(go, monkeypatch)


def test_pending_links_earn_nothing_until_the_server_confirmed_them(monkeypatch):
    async def go(env):
        env.rcon.garble = True
        await env.link(U1, "Steve")
        await play(env, 40, "Steve", PAL)
        assert await env.links() == [("Steve", U1, "add")] and await env.seconds(U1) is None
    with_env(go, monkeypatch)


def test_playtime_works_from_the_ping_sample_without_rcon(monkeypatch):
    async def go(env):
        await env.db.execute("INSERT INTO realms_links (name_key, name, user_id, state, attempts, linked_at,"
                             " updated_at) VALUES ('steve', 'Steve', ?, 'ok', 0, ?, ?)", (U1, T0, T0))
        env.member(U1)
        await play(env, 34, "Steve", PAL)
        assert await env.seconds(U1) == 32 * 60 and await env.balance(U1) == R.SLOT_COINS
        assert env.rcon.commands == []
    with_env(go, monkeypatch, environ={R.ENV_ADDRESS: "127.0.0.1"})


def test_rcon_only_setup_polls_through_rcon(monkeypatch):
    async def go(env):
        await env.link(U1, "Steve")
        await play(env, 34, "Steve", PAL)
        assert env.pings == 0 and env.posts == []  # no address: no ping, no panel
        assert await env.seconds(U1) == 32 * 60 and await env.balance(U1) == R.SLOT_COINS
        assert env.cog.health_text() == "ok (whitelist, chat to Minecraft)"
        env.rcon.down = True
        await env.poll()
        await env.poll(30)
        assert env.cog.health_text().startswith("unreachable since <t:")
    with_env(go, monkeypatch, environ={R.ENV_RCON_PASSWORD: PASSWORD})


# ---------------------------------------------------------------- /smp top and status
def test_top_ranks_this_weeks_playtime(monkeypatch):
    async def go(env):
        for uid in (U1, U2, U3):
            env.member(uid)
        rows = [(U1, "2026-10-05", 3600), (U1, "2026-10-06", 1800), (U2, "2026-10-05", 7200),
                (U3, "2026-10-04", 90_000),  # last week
                (MOD, "2026-10-05", 50_000)]  # not a member any more
        for uid, day, seconds in rows:
            await env.db.execute("INSERT INTO realms_playtime (user_id, day, seconds) VALUES (?, ?, ?)",
                                 (uid, day, seconds))
        env.t = T0 + 2 * 86400
        reply = await env.top(U3)
        assert reply["embed"].description == f"` 1` <@{U2}> · 2h 00m\n` 2` <@{U1}> · 1h 30m"
        assert reply["allowed_mentions"].users is False and "ephemeral" not in reply
        env.t = T0 + 7 * 86400  # next Monday: a fresh week
        assert (await env.top(U3))["embed"].description == "Nobody has played this week yet."
    with_env(go, monkeypatch)


def test_status_shows_the_server_the_members_link_and_playtime(monkeypatch):
    async def go(env):
        reply = await env.status(U1)
        text = reply["embed"].description
        assert reply["ephemeral"] is True and "Checking the server" in text and "no Minecraft name linked" in text
        assert cogmod.HEALTH_NAME not in text
        await env.link(U1, "Steve")
        await play(env, 64, "Steve", "Alex")
        text = (await env.status(U1))["embed"].description
        assert "🟢 **Online** · 2 / 20 players" in text and "Alex, Steve" in text and "**You**: `Steve`" in text
        assert "Played today 1h 02m, this week 1h 02m. Coins today: 10 of 40 (5 per half hour). Time counts while someone else is on too." in text
        staff = (await env.status(MOD, roles=(config.MOD_ROLE,)))["embed"].description
        assert f"**{cogmod.HEALTH_NAME}**: ok (status, whitelist, chat to Minecraft, chat from Minecraft)" in staff
        assert PASSWORD not in staff
    with_env(go, monkeypatch)


# ---------------------------------------------------------------- secrets and self-check
def test_the_rcon_password_never_reaches_logs_replies_or_health(monkeypatch, caplog):
    async def go(env):
        with caplog.at_level(logging.DEBUG):
            await env.cog.cog_load()
            await env.cog.cog_unload()
            env.rcon.refuse = True
            reply = await env.link(U1, "Steve")
            await env.poll()
            await env.say(U1, "hello")
            status = await env.status(MOD, roles=(config.MOD_ROLE,))
        health = env.cog.health_text()
        assert "RCON refused the password" in health
        assert len([r for r in caplog.records if "RCON is not answering" in r.getMessage()]) == 1
        seen = [caplog.text, reply["content"], status["embed"].description, health, repr(env.cog.setup),
                *env.mod_lines(), *(p.text or "" for p in env.posts)]
        assert all(PASSWORD not in text for text in seen)
        env.rcon.refuse = False
        await env.poll()
        assert "refused" not in env.cog.health_text() and await env.links() == [("Steve", U1, "ok")]
    with_env(go, monkeypatch)


def test_health_text_is_what_status_would_show(monkeypatch):
    async def go(env):
        assert env.cog.health_text() == "ok (status, whitelist, chat to Minecraft, chat from Minecraft)"
        await env.poll()
        assert env.cog.health_text() == "ok (status, whitelist, chat to Minecraft, chat from Minecraft)"
    with_env(go, monkeypatch)


def test_one_failing_step_does_not_stop_the_others(monkeypatch, caplog):
    async def go(env):
        async def boom(*args):
            raise RuntimeError("boom")

        env.cog.credit_playtime = boom
        with caplog.at_level(logging.ERROR):
            await env.poll()
        assert "realms: playtime failed" in caplog.text and len(env.posts) == 1  # the panel still went out
    with_env(go, monkeypatch)
