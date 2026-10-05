"""Unit tests for logic.tempvoice: names, cooldowns, rename limits, owner choice
and command checks."""

import pytest

import config
from logic import tempvoice as T


def utf16(text):
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------- naming
def test_name_uses_game_when_playing():
    assert T.channel_name("Valorant", "Bob") == "🎮 Valorant"


def test_name_falls_back_to_display_name():
    assert T.channel_name(None, "Bob") == "🎮 Bob's squad"
    assert T.channel_name("", "Bob") == "🎮 Bob's squad"
    assert T.channel_name("   ", "Bob") == "🎮 Bob's squad"


def test_name_strips_control_chars_and_collapses_space():
    assert T.channel_name("Val\norant\x00", "Bob") == "🎮 Val orant"
    assert T.channel_name(None, "B\tob\x7f") == "🎮 B ob's squad"
    # Format chars (zero-width joiners) are kept: emoji sequences need them.
    assert T.channel_name("👨‍👩", "Bob") == "🎮 👨‍👩"


def test_name_with_empty_display_name():
    assert T.channel_name(None, "\x00\x01") == "🎮 Someone's squad"


def test_game_name_truncated_to_100():
    name = T.channel_name("x" * 300, "Bob")
    assert utf16(name) <= 100 and name.startswith("🎮 xxx")


def test_fallback_truncates_display_name_but_keeps_suffix():
    name = T.channel_name(None, "y" * 300)
    assert utf16(name) <= 100 and name.endswith("'s squad") and name.startswith("🎮 yyy")


def test_truncation_never_splits_surrogate_pairs():
    name = T.channel_name("😀" * 100, "Bob")
    assert utf16(name) <= 100
    name.encode("utf-8")  # no lone surrogates


def test_game_named_like_a_permanent_channel_uses_fallback():
    # A temp "🎮 Squad" would be mistaken for the permanent 🎮 Squad by name lookups.
    assert T.channel_name("Squad", "Bob") == "🎮 Bob's squad"
    assert T.channel_name("New Squad", "Bob") == "🎮 Bob's squad"
    assert T.channel_name("Lobby", "Bob") == "🎮 Bob's squad"


def test_clean_rename():
    assert T.clean_rename("  chill \n zone ") == "chill zone"
    assert T.clean_rename("\x00\x01  ") is None
    assert T.clean_rename(config.SQUAD_VOICE) is None
    assert T.clean_rename("squad") is None
    assert utf16(T.clean_rename("z" * 200)) == 100


# ---------------------------------------------------------------- cooldown
def test_cooldown_one_per_window_per_member():
    c = T.Cooldown(30)
    assert c.take(1, 100.0)
    assert not c.take(1, 129.9)
    assert c.take(2, 110.0)  # other members unaffected
    assert c.take(1, 130.0)
    assert not c.take(1, 131.0)


def test_cooldown_active_does_not_consume():
    c = T.Cooldown(30)
    assert not c.active(1, 0.0)
    c.take(1, 0.0)
    assert c.active(1, 10.0) and not c.active(1, 30.0)


# ---------------------------------------------------------------- rename limit
def test_rename_limit_two_per_ten_minutes():
    r = T.WindowLimit(2, 600)
    assert r.wait(7, 0) == 0
    r.record(7, 0)
    assert r.wait(7, 60) == 0
    r.record(7, 60)
    assert r.wait(7, 120) == 480  # oldest frees up at t=600
    assert r.wait(8, 120) == 0  # per channel
    assert r.wait(7, 600) == 0
    r.record(7, 600)
    assert r.wait(7, 610) == 50  # the t=60 one expires at 660


def test_rename_limit_forget():
    r = T.WindowLimit(2, 600)
    r.record(7, 0)
    r.record(7, 1)
    r.forget(7)
    assert r.wait(7, 2) == 0


def test_minutes_text():
    assert T.minutes(30) == "1 minute"
    assert T.minutes(60) == "1 minute"
    assert T.minutes(61) == "2 minutes"
    assert T.minutes(480) == "8 minutes"


# ---------------------------------------------------------------- owner choice
def test_next_owner_longest_present():
    assert T.next_owner([3, 4, 5], {3: 50.0, 4: 10.0, 5: 30.0}) == 4


def test_next_owner_excludes_leaver():
    assert T.next_owner([1, 3], {1: 0.0, 3: 5.0}, exclude=1) == 3


def test_next_owner_falls_back_to_any():
    assert T.next_owner([8, 9], {}) == 8


def test_next_owner_prefers_known_join_times():
    assert T.next_owner([8, 9], {9: 100.0}) == 9


def test_next_owner_ties_keep_order():
    assert T.next_owner([5, 4], {4: 1.0, 5: 1.0}) == 5


def test_next_owner_nobody_left():
    assert T.next_owner([], {1: 0.0}) is None
    assert T.next_owner([1], {1: 0.0}, exclude=1) is None


# ---------------------------------------------------------------- command checks
def test_authorize_not_in_temp():
    assert T.authorize("name", user_id=1, owner_id=None, owner_present=False) is T.Denied.NOT_IN_TEMP
    assert T.authorize("claim", user_id=1, owner_id=None, owner_present=False) is T.Denied.NOT_IN_TEMP


@pytest.mark.parametrize("action", ["name", "limit"])
def test_authorize_owner_only(action):
    assert T.authorize(action, user_id=1, owner_id=1, owner_present=True) is None
    assert T.authorize(action, user_id=2, owner_id=1, owner_present=True) is T.Denied.NOT_OWNER
    assert T.authorize(action, user_id=2, owner_id=1, owner_present=False) is T.Denied.NOT_OWNER


def test_authorize_claim():
    assert T.authorize("claim", user_id=2, owner_id=1, owner_present=False) is None
    assert T.authorize("claim", user_id=2, owner_id=1, owner_present=True) is T.Denied.OWNER_PRESENT
    assert T.authorize("claim", user_id=1, owner_id=1, owner_present=True) is T.Denied.ALREADY_OWNER


def test_authorize_unknown_action():
    with pytest.raises(ValueError):
        T.authorize("delete", user_id=1, owner_id=1, owner_present=True)
