"""Rules for the Welcome Crew (logic/crew.py). Pure: no Discord, no database."""

from datetime import datetime
from zoneinfo import ZoneInfo

from logic import achievements as A
from logic import crew as C
from logic import quests as Q
from logic import shop

TZ = ZoneInfo("America/Los_Angeles")
OLD = 11  # a tiny snowflake: an account from 2015


def ts(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


NOW = ts(2026, 10, 7, 12, 0)


def young(now=NOW, days=5):
    """A snowflake for an account created `days` before `now`."""
    return ((now - days * C.DAY) * 1000 - Q.DISCORD_EPOCH_MS) << 22


# ---------------------------------------------------------------- the bar
def test_join_bar_needs_an_old_account_two_weeks_here_and_a_clean_record():
    ok = dict(user_id=OLD, joined_at=NOW - 14 * C.DAY, now=NOW, recent_cases=0)
    assert C.join_problem(**ok) is None
    assert "30 days old" in C.join_problem(**{**ok, "user_id": young()})
    assert "Come back in 1 day." in C.join_problem(**{**ok, "joined_at": NOW - 14 * C.DAY + 60})
    assert "Come back in 14 days." in C.join_problem(**{**ok, "joined_at": NOW})
    assert "14 days" in C.join_problem(**{**ok, "joined_at": None})
    assert "clean record" in C.join_problem(**{**ok, "recent_cases": 1})


def test_case_kinds_are_actions_against_the_member():
    assert set(C.CASE_KINDS) == {"warn", "timeout", "auto_timeout", "spam"}  # not untimeout, not purge


# ---------------------------------------------------------------- settings
def test_ping_gap_falls_back_to_the_default():
    assert C.ping_gap("45") == 45 * 60
    assert C.ping_gap(" 5 ") == 5 * 60
    for bad in (None, "", "soon", "0", "4", "-30", "100000", "1e3"):
        assert C.ping_gap(bad) == C.DEFAULT_GAP_MIN * 60, bad


def test_quiet_hours_parsing():
    assert C.quiet_hours("23-8") == (23, 8)
    assert C.quiet_hours("1-6") == (1, 6)
    assert C.quiet_hours("off") is None and C.quiet_hours(" NONE ") is None
    for bad in (None, "", "22", "22-9-1", "a-b", "25-3", "9-9", "-1-5"):
        assert C.quiet_hours(bad) == C.DEFAULT_QUIET, bad


def test_is_quiet_wraps_midnight():
    q = (22, 9)
    assert C.is_quiet(ts(2026, 10, 7, 22, 0), TZ, q) and C.is_quiet(ts(2026, 10, 7, 3, 0), TZ, q)
    assert C.is_quiet(ts(2026, 10, 7, 8, 59), TZ, q)
    assert not C.is_quiet(ts(2026, 10, 7, 9, 0), TZ, q) and not C.is_quiet(ts(2026, 10, 7, 21, 59), TZ, q)
    assert C.is_quiet(ts(2026, 10, 7, 2, 0), TZ, (1, 6)) and not C.is_quiet(ts(2026, 10, 7, 6, 0), TZ, (1, 6))
    assert not C.is_quiet(ts(2026, 10, 7, 3, 0), TZ, None)


# ---------------------------------------------------------------- the ping
def decide(**kw):
    base = dict(now=NOW, tz=TZ, last_ping_at=None, gap=30 * 60, quiet=C.DEFAULT_QUIET, crew_online=True, raid=False)
    return C.ping_decision(**{**base, **kw})


def test_ping_decision():
    assert decide() is C.Ping.SEND
    assert decide(crew_online=None) is C.Ping.SEND  # presence unknown: that check is skipped
    assert decide(crew_online=False) is C.Ping.WAIT
    assert decide(last_ping_at=NOW - 30 * 60 + 1) is C.Ping.WAIT
    assert decide(last_ping_at=NOW - 30 * 60) is C.Ping.SEND
    assert decide(now=ts(2026, 10, 7, 23, 0)) is C.Ping.WAIT
    assert decide(now=ts(2026, 10, 7, 23, 0), quiet=None) is C.Ping.SEND
    assert decide(raid=True) is C.Ping.DROP
    assert decide(raid=True, crew_online=False, last_ping_at=NOW) is C.Ping.DROP  # a raid always drops


def test_arrivals_go_stale():
    assert not C.is_stale(NOW - C.HOLD + 1, NOW) and C.is_stale(NOW - C.HOLD, NOW)


def test_ping_text():
    assert C.ping_text("<@&1>", ["<@5>"]) == "👋 <@&1> <@5> just joined. Come say hi!"
    assert C.ping_text("<@&1>", ["<@5>", "<@6>", "<@7>"]) == "👋 <@&1> <@5>, <@6> and <@7> are new here. Come say hi!"
    many = C.ping_text("<@&1>", [f"<@{i}>" for i in range(13)])
    assert many.count("<@") == 1 + C.NAMES_SHOWN and "and 3 more are new here" in many
    assert "@everyone" not in many and "@here" not in many


# ---------------------------------------------------------------- fresh joins and rewards
def test_fresh_join_or_rejoin():
    assert C.is_fresh(NOW, [])
    assert C.is_fresh(NOW, [None, NOW - 30 * C.DAY])
    assert not C.is_fresh(NOW, [NOW - 30 * C.DAY + 1])
    assert not C.is_fresh(NOW, [NOW - 400 * C.DAY, NOW - C.DAY])


def test_what_counts_as_a_greeting():
    ok = dict(newcomer_id=OLD, crew_id=12, arrived_at=NOW - 3600, fresh=True, now=NOW)
    assert C.counts(**ok)
    assert not C.counts(**{**ok, "crew_id": OLD})  # yourself
    assert not C.counts(**{**ok, "fresh": False})  # a rejoin
    assert not C.counts(**{**ok, "arrived_at": NOW - C.WINDOW})  # too late
    assert not C.counts(**{**ok, "arrived_at": NOW + 5})
    assert not C.counts(**{**ok, "newcomer_id": young()})  # a fresh alt as the newcomer
    assert not C.counts(**{**ok, "crew_id": young()})


def test_daily_cap_and_ref():
    assert C.coins_for(0) == C.COINS and C.coins_for(C.COINS * (C.DAILY_REWARDS - 1)) == C.COINS
    assert C.coins_for(C.COINS * C.DAILY_REWARDS) == 0
    assert C.ref(77) == "crew:greet:77"  # keyed on the newcomer only
    assert C.REASON not in shop.SEASON_REASONS


def test_day_bounds_are_local_midnights():
    start, end = C.day_bounds(ts(2026, 10, 7, 15, 30), TZ)
    assert (start, end) == (ts(2026, 10, 7), ts(2026, 10, 8))
    start, end = C.day_bounds(ts(2026, 11, 1, 12), TZ)  # the 25-hour day DST ends
    assert end - start == 25 * C.HOUR


def test_pick_greeter():
    hellos = C.Hellos()
    base = dict(newcomer_id=5, arrived_at=1000, crew_ids=[20, 21], reply_to=None, reply_sent_at=None,
                mentioned=[], hellos=hellos)
    pick = lambda **kw: C.pick_greeter(**{**base, **kw})  # noqa: E731
    assert pick() is None
    assert pick(reply_to=20, reply_sent_at=1500) == 20
    assert pick(reply_to=20, reply_sent_at=999) is None  # an old message from before they arrived
    assert pick(reply_to=20, reply_sent_at=None) is None
    assert pick(reply_to=30, reply_sent_at=1500) is None  # not on the crew
    assert pick(mentioned=[21]) is None  # a mention alone: the crew member never spoke to them
    hellos.note(21, 5, 1200)
    assert pick(mentioned=[30, 21]) == 21
    assert pick(mentioned=[21], reply_to=20, reply_sent_at=1500) == 20  # the reply wins
    assert pick(mentioned=[21], arrived_at=1300) is None  # that hello was before this arrival
    assert pick(newcomer_id=21, mentioned=[21]) is None  # never yourself
    hellos.prune(1200 + C.WINDOW)
    assert pick(mentioned=[21]) is None


# ---------------------------------------------------------------- text
def test_roster_text_and_order():
    assert "Be the first" in C.roster_text([])
    text = C.roster_text([("<@1>", 3), ("<@2>", 1), ("<@3>", None), ("<@4>", 0)])
    assert text.splitlines() == ["<@1> · 3 greetings this month", "<@2> · 1 greeting this month", "<@3>", "<@4>"]
    long = C.roster_text([(f"<@{i}>", None) for i in range(25)])
    assert len(long.splitlines()) == C.ROSTER_SHOWN + 1 and long.endswith("… and 5 more")
    assert C.roster_order({1: 2, 3: 5}, [3, 2, 1]) == [3, 1, 2]


def test_stats_and_digest_text():
    s = C.Stats(arrivals=4, pinged=3, pings=2, greeted=1, coins=40, top=((20, 1),))
    text = C.stats_text(s)
    assert "Newcomers: **4** (3 covered by 2 crew pings)" in text and "**1** (25%)" in text
    assert "Top: <@20> (1)" in text
    assert C.digest_text(s, 3) == "4 newcomers, 1 greeted by the crew (25%) · crew of 3"
    empty = C.Stats(0, 0, 0, 0, 0)
    assert C.digest_text(empty, 0) is None
    assert C.digest_text(empty, 2) == "0 newcomers, 0 greeted by the crew (n/a) · crew of 2"
    assert "Join the crew" in C.how_to_join() and str(C.COINS) in C.how_to_join()


def test_badge_is_in_the_catalogue_and_never_swept():
    badge = A.BY_KEY[C.BADGE_KEY]
    assert badge.name == "Welcoming Committee" and str(C.BADGE_AT) in badge.description
    assert badge.check(A.Facts()) is False
