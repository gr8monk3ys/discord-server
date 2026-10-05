"""Pure economy rules (logic/coins.py): no Discord, no database."""

import random
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from logic import coins as C

TZ = ZoneInfo("America/Los_Angeles")


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


# ---------------------------------------------------------------- /daily
@pytest.mark.parametrize("streak, amount", [(1, 100), (2, 120), (3, 140), (7, 220), (8, 240), (9, 240), (100, 240)])
def test_daily_amount(streak, amount):
    assert C.daily_amount(streak) == amount


def test_first_daily_starts_streak_at_one():
    claim = C.claim_daily(None, 0, ts(2026, 10, 4, 12), TZ)
    assert claim == C.DailyClaim(day="2026-10-04", streak=1, amount=100)


def test_daily_once_per_local_day():
    assert C.claim_daily("2026-10-04", 3, ts(2026, 10, 4, 23, 59), TZ) is None
    assert C.claim_daily("2026-10-04", 3, ts(2026, 10, 4, 0, 0), TZ) is None


def test_streak_continues_across_local_midnight():
    # 23:59 then 00:01 the next local day: two minutes apart, still a new day.
    first = C.claim_daily(None, 0, ts(2026, 10, 4, 23, 59), TZ)
    second = C.claim_daily(first.day, first.streak, ts(2026, 10, 5, 0, 1), TZ)
    assert (second.day, second.streak, second.amount) == ("2026-10-05", 2, 120)


def test_local_not_utc_day():
    # 2026-10-04 20:00 LA is already 10-05 in UTC; the local date is what counts.
    assert C.claim_daily("2026-10-04", 2, ts(2026, 10, 4, 20), TZ) is None
    claim = C.claim_daily("2026-10-03", 2, ts(2026, 10, 4, 20), TZ)
    assert claim.streak == 3


def test_streak_continues_nearly_48h_apart():
    # 00:01 one day, 23:59 the next: yesterday, so the streak continues.
    claim = C.claim_daily("2026-10-04", 5, ts(2026, 10, 5, 23, 59), TZ)
    assert (claim.streak, claim.amount) == (6, 200)


def test_gap_resets_streak():
    claim = C.claim_daily("2026-10-02", 7, ts(2026, 10, 4, 0, 1), TZ)
    assert (claim.streak, claim.amount) == (1, 100)


def test_streak_across_dst_changes():
    # Fall back: 2026-11-01 has 25 hours in Los Angeles.
    claim = C.claim_daily("2026-10-31", 7, ts(2026, 11, 1, 23, 30), TZ)
    assert (claim.day, claim.streak, claim.amount) == ("2026-11-01", 8, 240)
    claim = C.claim_daily("2026-11-01", 8, ts(2026, 11, 2, 0, 15), TZ)
    assert (claim.day, claim.streak) == ("2026-11-02", 9)
    # Spring forward: 2026-03-08 has 23 hours.
    claim = C.claim_daily("2026-03-07", 1, ts(2026, 3, 8, 23, 50), TZ)
    assert (claim.day, claim.streak) == ("2026-03-08", 2)
    claim = C.claim_daily("2026-03-08", 2, ts(2026, 3, 9, 0, 5), TZ)
    assert (claim.day, claim.streak) == ("2026-03-09", 3)


def test_current_streak_for_display():
    today = ts(2026, 10, 4, 12)
    assert C.current_streak("2026-10-04", 4, today, TZ) == 4
    assert C.current_streak("2026-10-03", 4, today, TZ) == 4  # still alive until tonight
    assert C.current_streak("2026-10-02", 4, today, TZ) == 0
    assert C.current_streak(None, 0, today, TZ) == 0


def test_day_bounds_follow_dst():
    start, end = C.day_bounds(ts(2026, 11, 1, 12), TZ)
    assert (start, end) == (ts(2026, 11, 1), ts(2026, 11, 2))
    assert end - start == 25 * 3600
    start, end = C.day_bounds(ts(2026, 3, 8, 12), TZ)
    assert end - start == 23 * 3600
    assert C.day_bounds(ts(2026, 10, 4), TZ)[0] == ts(2026, 10, 4)  # midnight belongs to its own day


def test_local_date():
    assert C.local_date(ts(2026, 10, 4, 23, 59), TZ) == date(2026, 10, 4)


# ---------------------------------------------------------------- voice
def test_voice_tick():
    assert C.voice_tick(0) == 0
    assert C.voice_tick(299) == 0
    assert C.voice_tick(300) == 1
    assert C.voice_tick(1_700_000_123) == 1_700_000_123 // 300


def test_voice_earners_need_two_humans():
    H = lambda uid: (uid, False)  # noqa: E731
    B = lambda uid: (uid, True)  # noqa: E731
    assert C.voice_earners([[H(1)]]) == []  # alone
    assert C.voice_earners([[H(1), B(50)]]) == []  # a bot isn't company
    assert C.voice_earners([[H(1), H(2), B(50)]]) == [1, 2]  # and never earns
    assert C.voice_earners([[H(1)], [H(2)]]) == []  # alone in separate channels
    assert C.voice_earners([[H(1), H(2)], [H(3)]]) == [1, 2]
    assert C.voice_earners([]) == []


def test_voice_earners_skip_opted_out_but_count_them_as_company():
    assert C.voice_earners([[(1, False), (2, False)]], opted_out={2}) == [1]
    assert C.voice_earners([[(1, False), (2, False)]], opted_out={1, 2}) == []


# ---------------------------------------------------------------- caps
def test_message_cap():
    assert C.message_payout(0) == 1
    assert C.message_payout(C.MESSAGE_DAILY_CAP - 1) == 1
    assert C.message_payout(C.MESSAGE_DAILY_CAP) == 0
    assert C.message_payout(C.MESSAGE_DAILY_CAP + 10) == 0


def test_clip_cap():
    assert [C.clip_payout(n * C.CLIP_COINS) for n in range(5)] == [25, 25, 25, 0, 0]


def test_refs():
    assert C.daily_ref("2026-10-04", 7) == "daily:2026-10-04:7"
    assert C.voice_ref(7, 123) == "voice:7:123"
    assert C.clip_ref(555) == "clip:555"
    assert C.lfg_ref(9, 7) == "lfg:9:7"
    assert C.mvp_ref("mvp:2026-W40", 7) == "mvp:2026-W40:7"
    assert C.mvp_ref("2026-W40", 7) == "mvp:2026-W40:7"
    assert C.clip_week_ref("2026-W40") == "clipweek:2026-W40"


# ---------------------------------------------------------------- /give
def test_give_validation():
    assert C.give_error(1, 2, False, 1) is None
    assert C.give_error(1, 1, False, 10) is not None
    assert C.give_error(1, 2, True, 10) is not None
    assert C.give_error(1, 2, False, 0) is not None
    assert C.give_error(1, 2, False, -5) is not None


# ---------------------------------------------------------------- /coinflip
def test_bet_bounds():
    assert C.bet_error(9, 1000) is not None
    assert C.bet_error(10, 1000) is None
    assert C.bet_error(1000, 1000) is None
    assert C.bet_error(1001, 1000) is not None  # more than you have
    assert C.bet_error(5000, 9000) is None
    assert C.bet_error(5001, 9000) is not None  # over the table limit
    assert C.bet_error(10, 9) is not None


def test_flip_is_deterministic_with_seeded_rng():
    a = [C.flip(random.Random(42)) for _ in range(5)]
    b = [C.flip(random.Random(42)) for _ in range(5)]
    assert a == b and set(a) <= {"heads", "tails"}
    many = {C.flip(random.Random(seed)) for seed in range(50)}
    assert many == {"heads", "tails"}


def test_coinflip_net():
    assert C.coinflip_net(100, won=True) == 100
    assert C.coinflip_net(100, won=False) == -100


def test_capped():
    assert C.capped(2, 0, 120) == 2
    assert C.capped(2, 118, 120) == 2
    assert C.capped(2, 119, 120) == 0


def test_deafened_neither_earn_nor_count():
    assert C.voice_earners([[(1, False), (2, False, True)]]) == []
    assert C.voice_earners([[(1, False), (2, False, False), (3, False, True)]]) == [1, 2]
