"""Pure rules for levels: the curve, message XP, voice crediting, reward roles, ranking,
the announcement limiter and the rank card."""

import io
import random

import pytest
from PIL import Image

import config
from logic import levels as L


# ---------------------------------------------------------------- curve
def test_xp_to_next_is_mee6_curve():
    assert L.xp_to_next(0) == 100
    assert L.xp_to_next(1) == 155
    assert L.xp_to_next(2) == 220
    assert L.xp_to_next(10) == 5 * 100 + 500 + 100


def test_total_for_level_is_cumulative():
    assert L.total_for_level(0) == 0
    assert L.total_for_level(1) == 100
    assert L.total_for_level(2) == 255
    assert L.total_for_level(3) == 475
    assert L.total_for_level(5) == sum(L.xp_to_next(i) for i in range(5))


@pytest.mark.parametrize("xp,level", [(0, 0), (99, 0), (100, 1), (254, 1), (255, 2), (474, 2), (475, 3)])
def test_level_for(xp, level):
    assert L.level_for(xp) == level


def test_level_for_round_trips_every_threshold():
    for level in range(0, 120):
        start = L.total_for_level(level)
        assert L.level_for(start) == level
        if start:
            assert L.level_for(start - 1) == level - 1


def test_level_for_negative_is_zero():
    assert L.level_for(-5) == 0


def test_progress():
    assert L.progress(0) == (0, 0, 100)
    assert L.progress(130) == (1, 30, 155)
    assert L.progress(255) == (2, 0, 220)


def test_progress_fraction_bounds():
    assert L.fraction(0, 100) == 0.0
    assert L.fraction(50, 100) == 0.5
    assert L.fraction(150, 100) == 1.0
    assert L.fraction(5, 0) == 0.0


# ---------------------------------------------------------------- messages
def test_message_xp_is_15_to_25_with_injected_rng():
    rng = random.Random(1)
    values = {L.message_xp(rng) for _ in range(2000)}
    assert values == set(range(15, 26))


def test_message_xp_uses_given_rng():
    class Fixed:
        def randint(self, a, b):
            assert (a, b) == (15, 25)
            return 21
    assert L.message_xp(Fixed()) == 21


@pytest.mark.parametrize("content,ok", [("", False), ("hi", False), ("  hi  ", False), ("hey", True),
                                        ("gg wp", True), (None, False)])
def test_long_enough(content, ok):
    assert L.long_enough(content) is ok


def test_cooldown():
    assert L.off_cooldown(0, 1000)  # never chatted
    assert not L.off_cooldown(1000, 1000)
    assert not L.off_cooldown(1000, 1059)
    assert L.off_cooldown(1000, 1060)
    assert L.off_cooldown(1000, 5000)


# ---------------------------------------------------------------- voice
def test_voice_credit_whole_minutes_only():
    assert L.voice_credit(0, 0) == (0, 0)
    assert L.voice_credit(59, 0) == (0, 0)
    assert L.voice_credit(60, 0) == (10, 60)
    assert L.voice_credit(150, 0) == (20, 120)


def test_voice_credit_is_incremental_and_idempotent():
    gain, counted = L.voice_credit(600, 0)
    assert (gain, counted) == (100, 600)
    assert L.voice_credit(600, counted) == (0, 600)  # same total again: nothing
    gain2, counted2 = L.voice_credit(725, counted)
    assert (gain2, counted2) == (20, 720)
    assert L.voice_credit(725, counted2) == (0, 720)
    assert L.voice_credit(780, counted2) == (10, 780)


def test_voice_credit_total_went_down_resets_baseline_without_xp():
    # e.g. /privacy off deleted the sessions: the baseline follows the new total.
    assert L.voice_credit(120, 3600) == (0, 120)


# ---------------------------------------------------------------- roles
def test_reward_role():
    assert L.reward_role(0) is None
    assert L.reward_role(4) is None
    assert L.reward_role(5) == "Regular"
    assert L.reward_role(19) == "Veteran"
    assert L.reward_role(20) == "Elite"
    assert L.reward_role(99) == "Mythic"


def test_reward_role_uses_config():
    assert [name for _, name in L.LEVEL_ROLES] == [name for _, name in config.LEVEL_ROLES]


def test_role_changes_swap_lower_for_higher():
    add, remove = L.role_changes(10, ["Regular", "Squad"])
    assert add == "Veteran"
    assert remove == ["Regular"]


def test_role_changes_already_right():
    assert L.role_changes(12, ["Veteran", "PC"]) == (None, [])


def test_role_changes_removes_all_when_below_first_threshold():
    assert L.role_changes(0, ["Regular", "Elite"]) == (None, ["Elite", "Regular"])


def test_role_changes_removes_higher_after_reset():
    assert L.role_changes(6, ["Mythic"]) == ("Regular", ["Mythic"])


def test_role_changes_matches_names_loosely():
    # Role names on the server may carry emoji or different case.
    add, remove = L.role_changes(20, ["⭐ regular"])
    assert add == "Elite" and remove == ["⭐ regular"]


# ---------------------------------------------------------------- ranking
def test_rank_position_competition_ranking():
    scores = {1: 500, 2: 300, 3: 300, 4: 100, 5: 0}
    assert L.rank_position(scores, 1) == 1
    assert L.rank_position(scores, 2) == 2
    assert L.rank_position(scores, 3) == 2
    assert L.rank_position(scores, 4) == 4
    assert L.rank_position(scores, 5) is None  # no XP: unranked
    assert L.rank_position(scores, 99) is None


def test_top():
    scores = {1: 100, 2: 500, 3: 500, 4: 0, 5: 50}
    assert L.top(scores, 3) == [(1, 2, 500), (1, 3, 500), (3, 1, 100)]
    assert L.top({}, 10) == []


# ---------------------------------------------------------------- text
def test_level_up_text():
    assert L.level_up_text("<@1>", 3) == "<@1> reached **level 3**. GG!"
    text = L.level_up_text("<@1>", 5, "Regular")
    assert "**level 5**" in text and "**Regular**" in text


def test_level_up_text_escapes_role_markdown():
    assert "\\*" in L.level_up_text("<@1>", 5, "*Star*")


# ---------------------------------------------------------------- limiter
def test_limiter_per_member_gap():
    lim = L.Limiter(member_gap=300, channel_gap=0, per_minute=100)
    assert lim.allow(1, 10, 1000)
    assert not lim.allow(1, 10, 1100)
    assert lim.allow(1, 10, 1300)


def test_limiter_per_channel_gap():
    lim = L.Limiter(member_gap=0, channel_gap=30, per_minute=100)
    assert lim.allow(1, 10, 1000)
    assert not lim.allow(2, 10, 1010)
    assert lim.allow(2, 11, 1010)  # another channel is fine
    assert lim.allow(2, 10, 1030)


def test_limiter_global_per_minute():
    lim = L.Limiter(member_gap=0, channel_gap=0, per_minute=3)
    assert [lim.allow(u, u, 1000) for u in range(5)] == [True, True, True, False, False]
    assert lim.allow(9, 9, 1060)


def test_limiter_denied_does_not_consume():
    lim = L.Limiter(member_gap=300, channel_gap=30, per_minute=100)
    assert lim.allow(1, 10, 1000)
    assert not lim.allow(2, 10, 1005)  # channel busy
    assert lim.allow(2, 11, 1006)  # member 2 wasn't marked as announced


# ---------------------------------------------------------------- rank card
def _png(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def _avatar(color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def test_rank_card_is_valid_png_of_right_size():
    data = L.render_rank_card("Tester", "tester", _avatar(), level=7, rank=3, into=40, needed=445, total=2000)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    img = _png(data)
    assert img.size == (L.CARD_WIDTH, L.CARD_HEIGHT) == (900, 260)


def test_rank_card_without_avatar_or_rank_and_odd_names():
    for name in (None, "", "‮evil", "💀💀💀", "x" * 300, "ｆｕｌｌｗｉｄｔｈ", "שלום"):
        img = _png(L.render_rank_card(name, None, None, level=0, rank=None, into=0, needed=100, total=0))
        assert img.size == (900, 260)


def test_rank_card_bad_avatar_falls_back():
    img = _png(L.render_rank_card("A", None, b"not an image", level=1, rank=1, into=200, needed=155, total=300))
    assert img.size == (900, 260)


def test_rank_card_bar_fill_uses_pen_color():
    full = _png(L.render_rank_card("A", None, None, level=1, rank=1, into=150, needed=155, total=250)).convert("RGB")
    empty = _png(L.render_rank_card("A", None, None, level=1, rank=1, into=0, needed=155, total=100)).convert("RGB")
    x, y = L.BAR_RIGHT - 20, (L.BAR_TOP + L.BAR_BOTTOM) // 2
    assert full.getpixel((x, y)) == L.PEN
    assert empty.getpixel((x, y)) != L.PEN
