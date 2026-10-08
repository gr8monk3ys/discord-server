"""Pure rules for the newer coin sinks in logic/shop.py: Gift Hype, Spotlight and the weekly raffle."""

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from logic import quests as Q
from logic import shop as S

TZ = ZoneInfo("America/Los_Angeles")


def ts(y, m, d, h=0, mi=0, s=0):
    return int(datetime(y, m, d, h, mi, s, tzinfo=TZ).timestamp())


def snowflake(created: int) -> int:
    return (created * 1000 - Q.DISCORD_EPOCH_MS) << 22


# ---------------------------------------------------------------- items
def test_new_items_prices_and_old_ones_unchanged():
    assert (S.ITEMS["gift"].price, S.ITEMS["gift"].duration) == (500, S.DAY)
    assert (S.ITEMS["spotlight"].price, S.ITEMS["spotlight"].duration) == (1_500, 7 * S.DAY)
    assert S.SPOTLIGHT_PIN == S.DAY
    assert [k for k in S.ITEMS] == ["color", "hype", "shoutout", "gift", "spotlight"]


def test_gift_error():
    assert "friend:" in S.gift_error(1, None, False)
    assert "yourself" in S.gift_error(1, 1, False)
    assert "Bots" in S.gift_error(1, 2, True)
    assert S.gift_error(1, 2, False) is None


# ---------------------------------------------------------------- spotlight
def test_spotlight_round_trip_and_bad_rows():
    s = S.Spotlight(1, 2, 3, 4)
    assert S.spotlight_load(S.spotlight_dump(s)) == s
    for bad in (None, "", "nope", "{}", '{"user": "x", "channel": 1, "message": 1, "expires": 1}', "[1]"):
        assert S.spotlight_load(bad) is None


def test_spotlight_busy():
    s = S.Spotlight(1, 2, 3, 100)
    assert S.spotlight_busy(s, 99) and not S.spotlight_busy(s, 100) and not S.spotlight_busy(None, 0)


# ---------------------------------------------------------------- raffle: timing
def test_next_draw_is_the_coming_sunday_8pm():
    wed = ts(2026, 10, 7, 15)  # Wednesday of 2026-W41
    p = S.next_draw(wed, TZ)
    assert p.key == "raffle:2026-W41" and p.scheduled_at == ts(2026, 10, 11, 20)
    assert S.next_draw(ts(2026, 10, 11, 19, 59, 59), TZ).key == "raffle:2026-W41"
    after = S.next_draw(ts(2026, 10, 11, 20), TZ)  # bought on the draw second: next week
    assert after.key == "raffle:2026-W42" and after.scheduled_at == ts(2026, 10, 18, 20)
    assert S.next_draw(ts(2026, 10, 12, 0, 1), TZ).key == "raffle:2026-W42"  # Monday


def test_draw_at_inverts_the_key_across_dst_and_year_end():
    assert S.draw_at("raffle:2026-W41", TZ) == ts(2026, 10, 11, 20)
    assert S.draw_at("raffle:2026-W44", TZ) == ts(2026, 11, 1, 20)  # DST ends that morning
    assert S.draw_at("raffle:2026-W53", TZ) == ts(2027, 1, 3, 20)
    for t in (ts(2026, 12, 30, 9), ts(2027, 3, 10), ts(2026, 6, 1)):
        p = S.next_draw(t, TZ)
        assert S.draw_at(p.key, TZ) == p.scheduled_at


def test_refs_and_key_of():
    key = "raffle:2026-W41"
    assert S.ticket_ref(key, 5, 99) == "raffle:2026-W41:buy:5:99"
    assert S.win_ref(key) == "raffle:2026-W41:win" and S.refund_ref(key, 5) == "raffle:2026-W41:refund:5"
    assert S.tickets_like(key) == "raffle:2026-W41:buy:%"
    assert S.key_of(S.ticket_ref(key, 5, 99)) == key
    for other in (None, "", S.win_ref(key), S.refund_ref(key, 5), "shop:1", "quest:1:buy:1"):
        assert S.key_of(other) is None


def test_due_waits_for_grace_skips_done_and_sorts():
    w41, w42 = "raffle:2026-W41", "raffle:2026-W42"
    draw = ts(2026, 10, 11, 20)
    assert S.due([w41], set(), draw + S.DRAW_GRACE - 1, TZ) == []
    assert S.due([w41, w41], set(), draw + S.DRAW_GRACE, TZ) == [w41]
    assert S.due([w41], {w41}, draw + 999, TZ) == []
    late = ts(2026, 10, 19)
    assert S.due([w42, w41], set(), late, TZ) == [w41, w42]


# ---------------------------------------------------------------- raffle: tickets and prize
def test_ticket_error_limits():
    assert S.ticket_error(0, 1) is None and S.ticket_error(0, 10) is None and S.ticket_error(7, 3) is None
    assert "at least 1" in S.ticket_error(0, 0)
    assert "maximum 10" in S.ticket_error(10, 1)
    assert "3 or fewer" in S.ticket_error(7, 4)


def test_young_accounts_cannot_enter():
    now = ts(2026, 10, 7)
    assert S.can_enter(snowflake(now - 31 * S.DAY), now)
    assert not S.can_enter(snowflake(now - 29 * S.DAY), now)


@pytest.mark.parametrize("pot,won", [(0, 0), (100, 80), (1000, 800), (1050, 840), (130, 104), (101, 80)])
def test_prize_is_80_percent_rounded_down(pot, won):
    assert S.prize(pot) == won


def test_pick_winner_is_weighted_and_order_independent():
    rows = [(3, 1), (1, 3), (2, 0)]
    a = [S.pick_winner(rows, random.Random(i)) for i in range(400)]
    b = [S.pick_winner(list(reversed(rows)), random.Random(i)) for i in range(400)]
    assert a == b
    assert set(a) == {1, 3}  # zero tickets never win
    assert a.count(1) > 2 * a.count(3)
    with pytest.raises(ValueError):
        S.pick_winner([(1, 0)], random.Random(1))


def test_fmt_chance():
    assert S.fmt_chance(3, 12) == "25%" and S.fmt_chance(0, 0) == "0%"
