"""Pure Welcomers rules: who counts as new, when the ping goes out, what it says."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from logic import welcomers as W

TZ = ZoneInfo("America/Los_Angeles")
QUIET = (22, 9)


def ts(h, mi=0, day=7):
    return int(datetime(2026, 10, day, h, mi, tzinfo=TZ).timestamp())


def reason(**kw):
    t = kw.pop("now", ts(19))
    args = dict(now=t, tz=TZ, quiet=QUIET, is_bot=False, joined_at=t - 2 * W.DAY, opted_out=False,
                pinged_before=False, been_in_voice=False, still_there=True, welcomers=3, host_there=False,
                last_ping_at=None)
    args.update(kw)
    return W.skip_reason(**args)


def test_a_newcomer_alone_in_voice_in_the_evening_gets_a_ping():
    assert reason() is None


def test_newcomer_window_is_fourteen_days():
    t = ts(19)
    assert W.NEW_DAYS == 14
    assert W.is_newcomer(t, t)
    assert W.is_newcomer(t - 14 * W.DAY + 1, t)
    assert not W.is_newcomer(t - 14 * W.DAY, t)
    assert not W.is_newcomer(None, t)
    assert not W.is_newcomer(t + 60, t)  # a join time in the future is not trusted
    assert reason(joined_at=t - 20 * W.DAY) == W.NOT_NEW
    assert reason(joined_at=None) == W.NOT_NEW


def test_hold():
    assert not W.held(1000, 1000 + W.HOLD - 1)
    assert W.held(1000, 1000 + W.HOLD)


@pytest.mark.parametrize("kw, why", [
    (dict(is_bot=True), W.BOT),
    (dict(opted_out=True), W.OPTED_OUT),
    (dict(pinged_before=True), W.PINGED_BEFORE),
    (dict(been_in_voice=True), W.BEEN_IN_VOICE),
    (dict(still_there=False), W.LEFT),
    (dict(welcomers=0), W.NO_WELCOMERS),
    (dict(host_there=True), W.HOST_THERE),
])
def test_each_rule_on_its_own_stops_the_ping(kw, why):
    assert reason(**kw) == why


def test_a_bot_is_never_pinged_for_even_when_everything_else_fits():
    assert reason(is_bot=True, joined_at=None) == W.BOT


def test_quiet_hours_from_the_crew_setting():
    assert reason(now=ts(22, 30)) == W.QUIET
    assert reason(now=ts(8, 59)) == W.QUIET
    assert reason(now=ts(9, 0)) is None
    assert reason(now=ts(23), quiet=None) is None  # CREW_QUIET_HOURS=off


def test_cooldown_turns_a_burst_into_one_ping():
    t = ts(19)
    assert W.COOLDOWN == 15 * W.MIN
    assert reason(now=t, last_ping_at=t - W.COOLDOWN + 1) == W.COOLING
    assert reason(now=t, last_ping_at=t - W.COOLDOWN) is None


def test_ping_text_names_role_newcomer_and_channel_in_one_line():
    text = W.ping_text("<@&1>", "<@2>", "<#3>")
    assert text == "👋 <@&1> <@2> is new and just joined <#3>. Come say hi."
    assert "\n" not in text


def test_mark_keys_are_per_member_and_share_a_prefix():
    assert W.mark_key(42) == "welcomers:pinged:42"
    assert W.mark_key(42).startswith(W.MARK_PREFIX)
    assert W.LAST_PING_KEY.startswith("welcomers:") and not W.LAST_PING_KEY.startswith(W.MARK_PREFIX)
