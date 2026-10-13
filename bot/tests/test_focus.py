"""Tests for logic.focus and cogs.focus: in-memory SQLite and fake Discord objects.
No network and no real clock."""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import config
import db as dbmod
import main
from cogs import focus as cogmod
from cogs import tournaments as tournaments_cog
from cogs.focus import Focus
from cogs.realms import Realms
from logic import focus as F
from logic import ops

GUILD_ID = 999
T0 = 1_800_000_000
TOURNEY, REALMS = config.TOURNAMENTS_CHANNEL, config.REALMS_CHANNEL


# ---------------------------------------------------------------- logic
def test_the_allowlist_is_exactly_what_layout_declares_event_driven():
    assert set(F.EVENT_CHANNELS) == set(config.layout.FOCUS_EVENT_CHANNELS) == {TOURNEY, REALMS}
    assert F.TOURNAMENT_LIVE == tournaments_cog.ACTIVE
    opt_in = F.opt_in_names()
    assert opt_in and not {config.slug(n) for n in opt_in} & {config.slug(n) for n in F.EVENT_CHANNELS}
    core = {config.slug(n) for n in config.layout.ONBOARDING_DEFAULT_CHANNELS}
    assert not core & {config.slug(n) for n in (*opt_in, *F.EVENT_CHANNELS)}
    assert len(core) >= F.MIN_DEFAULTS


@pytest.mark.parametrize("live, in_defaults, marked, is_focused, want", [
    (True, False, False, True, F.ADD),
    (True, True, False, True, F.NOTHING),  # already a default (by hand): leave it
    (True, True, True, True, F.NOTHING),  # we added it earlier and it is still on
    (False, True, True, True, F.REMOVE),
    (False, True, False, True, F.NOTHING),  # a default somebody else chose is never removed
    (False, False, True, True, F.FORGET),  # someone removed it for us
    (False, False, False, True, F.NOTHING),
    (None, False, False, True, F.NOTHING),  # unknown: no change either way
    (None, True, True, True, F.NOTHING),
    (True, False, False, False, F.NOTHING),  # not focused (before apply, after --revert): inert
    (False, True, True, False, F.FORGET),  # reverted while we had it: keep it, drop the marker
    (False, True, False, False, F.NOTHING),
])
def test_decide(live, in_defaults, marked, is_focused, want):
    assert F.decide(live, in_defaults, marked, is_focused) == want


def test_focused_means_most_opt_in_channels_are_not_defaults():
    assert F.focused({1, 2, 3}, {7, 8}) is True
    assert F.focused({1, 2, 7}, {7, 8}) is False  # half of them are defaults: not the short list
    assert F.focused({1, 2}, set()) is True
    # one reserve channel in the core (focus_mode.py's fallback) or one the owner promoted
    # by hand does not switch the module off
    assert F.focused({1, 2, 7}, {7, 8, 9, 10}) is True
    assert F.focused({1, 7, 8, 9, 10}, {7, 8, 9, 10}) is False  # after --revert


def test_hold_only_delays_changes():
    assert F.held(F.ADD, T0, T0 + F.HOLD - 1) is True
    assert F.held(F.REMOVE, T0, T0 + F.HOLD) is False
    assert F.held(F.ADD, None, T0) is False
    assert F.held(F.FORGET, T0, T0 + 1) is False and F.held(F.NOTHING, T0, T0 + 1) is False


def test_apply_actions_keeps_every_other_id_and_respects_the_minimum():
    base = [5, 3, 1, 2, 4, 6, 7]
    assert F.apply_actions(base, {}) is None
    assert F.apply_actions(base, {9: F.ADD}) == [1, 2, 3, 4, 5, 6, 7, 9]
    assert F.apply_actions([*base, 9], {9: F.REMOVE}) == [1, 2, 3, 4, 5, 6, 7]
    assert F.apply_actions(base, {7: F.REMOVE}) is None  # would leave 6: Discord needs 7
    assert F.apply_actions(base, {3: F.ADD}) is None  # already there
    assert F.apply_actions([*base, 9], {9: F.REMOVE, 10: F.ADD}) == [1, 2, 3, 4, 5, 6, 7, 10]


def test_log_lines_are_plain_and_name_the_reason():
    add = F.log_line(TOURNEY, F.TOURNAMENT, F.ADD)
    gone = F.log_line(REALMS, F.REALMS, F.REMOVE)
    assert TOURNEY in add and "a tournament is open or running" in add
    assert REALMS in gone and "opt-in again" in gone and "offline" in gone
    assert "—" not in add + gone and "@" not in add + gone


def test_marker_keys_are_per_channel():
    assert F.marker_key(TOURNEY) != F.marker_key(REALMS)
    assert F.marker_key(TOURNEY).startswith("focus:added:")


def test_registered_and_checked_by_the_permission_self_check():
    assert ("cogs.focus", set()) in main.MODULES
    assert any(n.perm == "manage_guild" and "focus" in n.cogs for n in ops.GUILD_NEEDS)
    assert Focus.__cog_app_commands__ == []  # nothing for members to run


# ---------------------------------------------------------------- Realms.is_online
def test_realms_is_online_is_none_until_it_knows():
    def cog(**state):
        c = Realms.__new__(Realms)
        c.setup = SimpleNamespace(status_on=state.pop("status_on", True), rcon_on=False)
        c.down_since, c.next_poll_at, c.failures = None, 0, 0
        for k, v in state.items():
            setattr(c, k, v)
        return c
    assert cog(status_on=False).is_online() is None  # not set up
    assert cog().is_online() is None  # not polled yet
    assert cog(next_poll_at=T0).is_online() is None  # the first poll has started, not answered
    assert cog(next_poll_at=T0, reached_at=T0).is_online() is True
    assert cog(next_poll_at=T0, reached_at=T0, failures=1).is_online() is None  # one missed poll may be a blip
    assert cog(next_poll_at=T0, failures=3, down_since=T0 - 60).is_online() is False


# ---------------------------------------------------------------- cog fakes
class FakeChannel:
    def __init__(self, cid, name):
        self.id, self.name = cid, name
        self.sent = []
        self.edits = []  # anything that would change the channel itself

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))

    async def set_permissions(self, *a, **kw):
        self.edits.append(("set_permissions", a, kw))

    async def edit(self, **kw):
        self.edits.append(("edit", kw))


class FakeGuild:
    def __init__(self, names, defaults):
        self.id = GUILD_ID
        self.by_name = {n: FakeChannel(100 + i, n) for i, n in enumerate(names)}
        self.mod_log = FakeChannel(50, config.MOD_LOG_CHANNEL)
        self.default_ids = {self.by_name[n].id for n in defaults}
        self.enabled = True
        self.reads = 0
        self.edits = []
        self.read_error = None
        self.edit_error = None

    @property
    def channels(self):
        return [*self.by_name.values(), self.mod_log]

    @property
    def text_channels(self):
        return self.channels

    def ids(self, *names):
        return {self.by_name[n].id for n in names}

    async def onboarding(self):
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return SimpleNamespace(enabled=self.enabled, default_channel_ids=set(self.default_ids))

    async def edit_onboarding(self, **kw):
        if self.edit_error is not None:
            raise self.edit_error
        assert set(kw) == {"default_channels", "reason"}  # never prompts, enabled or mode
        self.edits.append([c.id for c in kw["default_channels"]])
        self.default_ids = {c.id for c in kw["default_channels"]}


class FakeRealms:
    def __init__(self):
        self.online = None

    def is_online(self):
        return self.online


class FakeBot:
    def __init__(self, db, guild):
        self.db, self.guild_obj = db, guild
        self.settings = SimpleNamespace(guild_id=GUILD_ID)
        self.cogs = {"Tournaments": object(), "Realms": FakeRealms()}

    def get_guild(self, gid):
        return self.guild_obj if gid == GUILD_ID else None

    def get_cog(self, name):
        return self.cogs.get(name)


def http_error(status=400):
    return discord.HTTPException(SimpleNamespace(status=status, reason="Bad Request"), "refused")


CORE = list(config.layout.ONBOARDING_DEFAULT_CHANNELS)
EVERYTHING = list(config.layout.ONBOARDING_EVERYTHING)


def with_env(fn, monkeypatch, defaults=None):
    async def go():
        db = dbmod.Database(":memory:")
        await db.connect()
        await db.migrate()
        guild = FakeGuild(EVERYTHING, CORE if defaults is None else defaults)
        bot = FakeBot(db, guild)
        env = SimpleNamespace(db=db, guild=guild, bot=bot, cog=Focus(bot), t=T0, realms=bot.cogs["Realms"])
        monkeypatch.setattr(cogmod, "now", lambda: env.t)

        async def tournament(status):
            await db.execute("DELETE FROM tournaments")
            if status:
                await db.execute("INSERT INTO tournaments (name, size, status, created_by, created_at) "
                                 "VALUES ('Cup', 8, ?, 1, ?)", (status, T0))
        env.tournament = tournament

        async def marked(name):
            return await env.cog.marked(name)
        env.marked = marked
        try:
            await fn(env)
        finally:
            await db.close()
    asyncio.run(go())


def log_lines(env):
    return [kw["embed"].description for _, kw in env.guild.mod_log.sent]


# ---------------------------------------------------------------- cog
def test_startup_with_nothing_live_reads_once_and_changes_nothing(monkeypatch):
    async def go(env):
        await env.cog.run_once()
        assert env.guild.reads == 1 and env.guild.edits == [] and log_lines(env) == []
        env.t += F.TICK
        await env.cog.run_once()
        assert env.guild.reads == 1  # nothing changed: no Discord call at all
        env.t += F.RECHECK
        await env.cog.run_once()
        assert env.guild.reads == 2 and env.guild.edits == []  # the hourly look
    with_env(go, monkeypatch)


def test_tournament_shows_while_open_or_running_and_goes_back_after(monkeypatch):
    async def go(env):
        await env.cog.run_once()
        await env.tournament("signup")
        env.t += F.TICK
        await env.cog.run_once()
        assert env.guild.default_ids == env.guild.ids(*CORE, TOURNEY)
        assert await env.marked(TOURNEY) and not await env.marked(REALMS)
        assert len(log_lines(env)) == 1 and TOURNEY in log_lines(env)[0]
        await env.tournament("running")
        env.t += F.HOLD
        await env.cog.run_once()
        assert len(env.guild.edits) == 1  # still live: nothing to do
        await env.tournament("done")
        env.t += F.TICK
        await env.cog.run_once()
        assert env.guild.default_ids == env.guild.ids(*CORE)
        assert not await env.marked(TOURNEY)
        assert len(env.guild.edits) == 2 and len(log_lines(env)) == 2 and "opt-in again" in log_lines(env)[1]
        for edit in env.guild.edits:  # every other default went back exactly as it was
            assert set(edit) - env.guild.ids(TOURNEY) == env.guild.ids(*CORE)
        for ch in env.guild.channels:  # and no channel or permission was ever edited
            assert ch.edits == []
    with_env(go, monkeypatch)


def test_both_at_once_is_one_edit_and_two_log_lines(monkeypatch):
    async def go(env):
        await env.tournament("signup")
        env.realms.online = True
        await env.cog.run_once()
        assert env.guild.edits == [sorted(env.guild.ids(*CORE, TOURNEY, REALMS))]
        assert len(log_lines(env)) == 2
    with_env(go, monkeypatch)


def test_restart_while_live_keeps_the_channel_and_removes_it_later(monkeypatch):
    async def go(env):
        env.realms.online = True
        await env.cog.run_once()
        assert env.guild.default_ids == env.guild.ids(*CORE, REALMS)
        again = Focus(env.bot)  # a restart: memory is gone, the marker is in SQLite
        env.t += 5
        await again.run_once()
        assert len(env.guild.edits) == 1
        env.realms.online = False
        env.t += F.TICK
        await again.run_once()
        assert env.guild.default_ids == env.guild.ids(*CORE) and not await env.marked(REALMS)
    with_env(go, monkeypatch)


def test_restart_after_it_ended_restores_the_declared_state(monkeypatch):
    async def go(env):
        env.realms.online = True
        await env.cog.run_once()
        env.realms.online = False  # the server went down while the bot was off
        again = Focus(env.bot)
        env.t += 30
        await again.run_once()
        assert env.guild.default_ids == env.guild.ids(*CORE)
    with_env(go, monkeypatch)


def test_unknown_changes_nothing(monkeypatch):
    async def go(env):
        env.realms.online = True
        await env.cog.run_once()
        env.realms.online = None  # a missed poll, or the realms cog not loaded
        env.t += F.HOLD + 1
        await env.cog.run_once()
        del env.bot.cogs["Realms"], env.bot.cogs["Tournaments"]
        env.t += F.HOLD + 1
        await env.cog.run_once()
        assert env.guild.default_ids == env.guild.ids(*CORE, REALMS) and len(env.guild.edits) == 1
    with_env(go, monkeypatch)


def test_a_flapping_server_changes_at_most_once_per_hold(monkeypatch):
    async def go(env):
        env.realms.online = True
        await env.cog.run_once()
        for i in range(1, 6):  # down, up, down, up, down inside the hold
            env.realms.online = i % 2 == 0
            env.t += F.TICK
            await env.cog.run_once()
        assert len(env.guild.edits) == 1
        env.t = T0 + F.HOLD
        await env.cog.run_once()
        assert len(env.guild.edits) == 2 and env.guild.default_ids == env.guild.ids(*CORE)
    with_env(go, monkeypatch)


def test_inert_before_focus_is_applied_and_after_a_revert(monkeypatch):
    async def go(env):
        await env.tournament("signup")
        env.realms.online = True
        await env.cog.run_once()
        await env.tournament(None)
        env.realms.online = False
        env.t += F.HOLD + 1
        await env.cog.run_once()
        assert env.guild.edits == [] and log_lines(env) == []
        assert env.guild.default_ids == env.guild.ids(*EVERYTHING)
    with_env(go, monkeypatch, defaults=EVERYTHING)


def test_revert_while_live_keeps_the_channel_for_good(monkeypatch):
    async def go(env):
        await env.tournament("signup")
        await env.cog.run_once()
        assert await env.marked(TOURNEY)
        env.guild.default_ids = env.guild.ids(*EVERYTHING)  # focus_mode.py --revert --apply
        await env.tournament(None)
        env.t += F.HOLD + 1
        await env.cog.run_once()
        assert env.guild.default_ids == env.guild.ids(*EVERYTHING) and len(env.guild.edits) == 1
        assert not await env.marked(TOURNEY)
    with_env(go, monkeypatch)


def test_a_default_set_by_hand_is_never_removed(monkeypatch):
    async def go(env):
        await env.cog.run_once()
        env.t += F.RECHECK
        await env.cog.run_once()
        assert env.guild.edits == [] and TOURNEY in [n for n in EVERYTHING if env.guild.by_name[n].id in env.guild.default_ids]
    with_env(go, monkeypatch, defaults=[*CORE, TOURNEY])


def test_onboarding_off_or_unreadable_or_refused_degrades_quietly(monkeypatch):
    async def go(env):
        env.realms.online = True
        env.guild.enabled = False
        await env.cog.run_once()
        assert env.guild.edits == []
        env.guild.enabled = True
        env.guild.read_error = http_error(500)
        env.t += F.RECHECK
        await env.cog.run_once()
        assert env.guild.edits == []
        env.guild.read_error, env.guild.edit_error = None, http_error()
        env.t += F.RETRY - 1
        await env.cog.run_once()
        assert env.guild.reads == 2  # backing off: not asked again yet
        env.t += 1
        await env.cog.run_once()
        assert env.guild.edits == [] and not await env.marked(REALMS) and log_lines(env) == []
        env.guild.edit_error = None
        env.t += F.RETRY
        await env.cog.run_once()
        assert env.guild.default_ids == env.guild.ids(*CORE, REALMS) and await env.marked(REALMS)
    with_env(go, monkeypatch)


def test_missing_channels_and_mod_log_are_fine(monkeypatch):
    async def go(env):
        del env.guild.by_name[REALMS]
        env.guild.mod_log.name = "elsewhere"
        env.realms.online = True
        await env.tournament("signup")
        await env.cog.run_once()
        assert env.guild.default_ids == env.guild.ids(*CORE, TOURNEY)
        env.bot.guild_obj = None
        env.t += F.RECHECK
        await env.cog.run_once()  # no guild: nothing happens, nothing raises
    with_env(go, monkeypatch)
