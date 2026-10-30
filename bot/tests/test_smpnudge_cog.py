"""cogs/monkey.py nudges: `/smp nudges`, last-seen from the player-list polls, and the DM pass.
Reuses the fakes from test_monkey_cog (in-memory SQLite, fake RCON, fake pinger, fake clock)."""

from types import SimpleNamespace

import discord

from cogs import monkey as cogmod
from cogs.monkey import MESSAGES, NUDGE_HINT, MonkeySmp
from logic import botm as B
from logic import smpnudge as N
from logic import smpwatch as W
from test_monkey_cog import ALL_ON, T0, TZ, U1, U2, U3, FakeInteraction, forbidden, status_json, with_env

ON = {**ALL_ON, N.ENV_ON: "on"}
NOON = T0 + 4 * N.HOUR  # T0 is 8 AM, inside the quiet hours


def server_error():
    return discord.HTTPException(SimpleNamespace(status=500, reason="Server Error"), "oops")


def dms(env, uid):
    """Give a member a DM channel the test can read; `env.dm_fail[uid]` makes the next sends raise."""
    member = env.member(uid)
    if not hasattr(member, "dms"):
        member.dms = []

        async def send(content=None, **kwargs):
            fail = getattr(env, "dm_fail", {}).get(uid)
            if fail is not None:
                raise fail
            member.dms.append(dict(content=content, **kwargs))
        member.send = send
    return member.dms


async def nudges(env, uid, value):
    inter = FakeInteraction(env.member(uid), env.guild)
    await MonkeySmp.nudges.callback(env.cog, inter, SimpleNamespace(value=value))
    return inter.reply()["content"]


async def row(env, uid):
    found = await env.db.fetchone("SELECT * FROM monkey_nudges WHERE user_id = ?", (uid,))
    return dict(found) if found is not None else None


async def opted_in_player(env, uid, name):
    """Linked, opted in, and seen in a poll at the current time."""
    await env.linked(uid, name)
    dms(env, uid)
    assert await nudges(env, uid, "on") == MESSAGES["nudges_on"]
    env.players(name)
    await env.poll()
    assert (await row(env, uid))["last_seen_at"] == env.t


async def pass_at(env, t):
    """Jump the clock to `t` with nobody on, then run one poll (which runs the nudge pass when due)."""
    env.players()
    env.t = t
    env.cog.last_nudge_at = 0
    await env.cog.poll_once()


# ---------------------------------------------------------------- off by default
def test_off_without_the_flag_saves_and_sends_nothing(monkeypatch):
    async def go(env):
        assert not env.cog.nudges_on
        await env.linked(U1, "Steve")
        assert await nudges(env, U1, "on") == MESSAGES["nudges_off_here"]
        assert await row(env, U1) is None
        await env.db.execute("INSERT INTO monkey_nudges (user_id, opted_at, last_seen_at) VALUES (?, ?, ?)",
                             (U1, T0, T0 - 30 * N.DAY))
        dm = dms(env, U1)
        env.players("Steve")
        await pass_at(env, NOON)
        assert dm == [] and (await row(env, U1))["last_seen_at"] == T0 - 30 * N.DAY  # not even recorded
        assert NUDGE_HINT not in (await env.link(U2, "Alex"))["content"]
    with_env(go, monkeypatch)


def test_link_reply_mentions_nudges_when_on(monkeypatch):
    async def go(env):
        assert (await env.link(U1, "Steve"))["content"].endswith(NUDGE_HINT)
    with_env(go, monkeypatch, environ=ON)


# ---------------------------------------------------------------- the command
def test_opt_in_needs_a_link_and_tracking(monkeypatch):
    async def go(env):
        assert await nudges(env, U1, "on") == MESSAGES["nudges_link"]
        await env.linked(U1, "Steve")
        await env.opt_out(U1)
        assert await nudges(env, U1, "on") == MESSAGES["nudges_privacy"]
        assert await row(env, U1) is None
        assert await nudges(env, U1, "off") == MESSAGES["nudges_were_off"]
    with_env(go, monkeypatch, environ=ON)


def test_opt_in_and_out(monkeypatch):
    async def go(env):
        await env.linked(U1, "Steve")
        assert await nudges(env, U1, "on") == MESSAGES["nudges_on"]
        saved = await row(env, U1)
        assert saved["opted_at"] == env.t and saved["last_seen_at"] is None and saved["nudged_for"] is None
        assert await nudges(env, U1, "off") == MESSAGES["nudges_off"]
        assert await row(env, U1) is None
    with_env(go, monkeypatch, environ=ON)


def test_opt_in_seeds_last_seen_from_playtime(monkeypatch):
    async def go(env):
        await env.linked(U1, "Steve")
        await env.db.execute("INSERT INTO monkey_playtime (user_id, day, seconds) VALUES (?, ?, ?)",
                             (U1, "2026-10-03", 3600))
        await nudges(env, U1, "on")
        assert (await row(env, U1))["last_seen_at"] == N.day_end("2026-10-03", TZ)
    with_env(go, monkeypatch, environ=ON)


# ---------------------------------------------------------------- last seen
def test_only_opted_in_linked_tracked_members_are_recorded(monkeypatch):
    async def go(env):
        await env.linked(U1, "Steve")
        await env.linked(U2, "Alex")
        await env.linked(U3, "Notch")
        await nudges(env, U1, "on")
        await nudges(env, U3, "on")
        await env.opt_out(U3)
        env.players("Steve", "Alex", "Notch")
        await env.poll()
        assert (await row(env, U1))["last_seen_at"] == env.t
        assert await row(env, U2) is None  # never opted in: nothing kept
        assert await row(env, U3) is None  # tracking off: the row went
    with_env(go, monkeypatch, environ=ON)


def test_unlinking_forgets_the_nudge_setting(monkeypatch):
    async def go(env):
        await opted_in_player(env, U1, "Steve")
        await env.unlink(U1)
        assert await row(env, U1) is None
    with_env(go, monkeypatch, environ=ON)


# ---------------------------------------------------------------- the DM pass
def test_one_nudge_per_lapse(monkeypatch):
    async def go(env):
        env.t = NOON
        await opted_in_player(env, U1, "Steve")
        seen = env.t
        dm = dms(env, U1)

        await pass_at(env, seen + N.AWAY - N.HOUR)
        assert dm == []  # not a week yet

        await pass_at(env, seen + N.AWAY + N.HOUR)
        assert len(dm) == 1
        text = dm[0]["content"]
        assert "7 days since you were on Monkey SMP" in text and W.DEFAULT_ADDRESS in text
        assert dm[0]["allowed_mentions"].everyone is False
        assert (await row(env, U1))["nudged_for"] == seen

        await pass_at(env, seen + 20 * N.DAY)
        assert len(dm) == 1  # still away: no second nudge

        # they play again, then lapse again: one more
        await env.linked(U2, "Alex")
        env.players("Steve", "Alex")
        await env.poll()
        back = env.t
        await pass_at(env, back + 3 * N.DAY)
        assert len(dm) == 1
        await pass_at(env, back + N.AWAY + N.HOUR)
        assert len(dm) == 2
    with_env(go, monkeypatch, environ=ON)


def test_opted_out_member_gets_nothing(monkeypatch):
    async def go(env):
        env.t = NOON
        await opted_in_player(env, U1, "Steve")
        dm = dms(env, U1)
        await nudges(env, U1, "off")
        await pass_at(env, env.t + 10 * N.DAY)
        assert dm == []
    with_env(go, monkeypatch, environ=ON)


def test_tracking_opt_out_stops_nudges(monkeypatch):
    async def go(env):
        env.t = NOON
        await opted_in_player(env, U1, "Steve")
        dm = dms(env, U1)
        await env.opt_out(U1)
        await pass_at(env, env.t + 10 * N.DAY)
        assert dm == [] and await row(env, U1) is None
    with_env(go, monkeypatch, environ=ON)


def test_closed_dms_are_recorded_and_not_retried(monkeypatch):
    async def go(env):
        env.t = NOON
        await opted_in_player(env, U1, "Steve")
        seen = env.t
        env.dm_fail = {U1: forbidden()}
        await pass_at(env, seen + N.AWAY + N.HOUR)
        closed = await row(env, U1)
        assert closed["dm_closed_at"] == env.t
        env.dm_fail = {}
        tries = []
        member = env.member(U1)
        old_send = member.send

        async def counting(*a, **k):
            tries.append(1)
            await old_send(*a, **k)
        member.send = counting
        await pass_at(env, seen + 9 * N.DAY)
        assert tries == []  # stopped trying
        # turning it on again clears the refusal; the lapse it failed on is not retried
        assert await nudges(env, U1, "on") == MESSAGES["nudges_on"]
        assert (await row(env, U1))["dm_closed_at"] is None
        await pass_at(env, seen + 10 * N.DAY)
        assert tries == []
    with_env(go, monkeypatch, environ=ON)


def test_a_passing_discord_error_is_retried(monkeypatch):
    async def go(env):
        env.t = NOON
        await opted_in_player(env, U1, "Steve")
        seen = env.t
        dm = dms(env, U1)
        env.dm_fail = {U1: server_error()}
        await pass_at(env, seen + N.AWAY + N.HOUR)
        assert dm == [] and (await row(env, U1))["nudged_for"] is None
        env.dm_fail = {}
        await pass_at(env, seen + N.AWAY + 2 * N.HOUR)
        assert len(dm) == 1
    with_env(go, monkeypatch, environ=ON)


def test_no_nudges_in_quiet_hours_or_while_the_server_is_down(monkeypatch):
    async def go(env):
        env.t = NOON
        await opted_in_player(env, U1, "Steve")
        seen = env.t
        dm = dms(env, U1)
        await pass_at(env, seen + N.AWAY + 11 * N.HOUR)  # 11 PM
        assert dm == []
        env.ping = cogmod.PingError("TimeoutError")
        env.rcon.down = True
        env.cog.down_since = env.t  # the panel already says offline
        env.t = seen + 8 * N.DAY
        env.cog.last_nudge_at = 0
        await env.cog.poll_once()
        assert dm == []
    with_env(go, monkeypatch, environ=ON)


def test_an_incomplete_player_list_holds_nudges_back(monkeypatch):
    async def go(env):
        env.t = NOON
        await env.linked(U1, "Steve")
        dms(env, U1)
        await nudges(env, U1, "on")
        env.ping = status_json(["Steve"], online=1)
        await env.poll()
        seen = env.t
        env.ping = status_json([], online=1)  # a hidden sample: somebody is on, the list doesn't say who
        env.t = seen + N.DAY
        await env.cog.poll_once()
        await pass_at(env, seen + N.AWAY + N.HOUR)
        assert env.member(U1).dms == []
    with_env(go, monkeypatch, environ={k: v for k, v in ON.items() if k != "MONKEY_RCON_PASSWORD"})


def test_the_dm_says_what_happened(monkeypatch):
    async def go(env):
        env.t = NOON
        await opted_in_player(env, U1, "Steve")
        seen = env.t
        # others played since (U2), a faction was approved, Base of the Month was picked
        await env.db.execute("INSERT INTO monkey_playtime (user_id, day, seconds) VALUES (?, ?, ?)",
                             (U2, "2026-10-08", 5 * 3600))
        await env.db.execute(
            "INSERT INTO factions (season, name, name_key, leader_id, members, banner, charter, status, created_at,"
            " decided_at) VALUES ('s1', 'Iron *Pact*', 'iron pact', ?, '[]', 'b', 'c', 'approved', ?, ?)",
            (U2, seen, seen + N.DAY))
        await env.db.meta_set(B.STATE_KEY, B.dump(B.State(month="2026-11", winners=(U2,))))
        dm = dms(env, U1)
        await pass_at(env, seen + N.AWAY + N.HOUR)
        text = dm[0]["content"]
        assert "- 1 other player put in 5 hours" in text
        assert "- New faction: Iron \\*Pact\\*" in text
        assert "Base of the Month for November 2026" in text
        assert env.guild.smp.mention in text
    with_env(go, monkeypatch, environ=ON)
