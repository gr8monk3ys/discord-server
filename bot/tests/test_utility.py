"""Unit tests for logic.utility: when-parsing, reminder limits, AFK text, suggestion
tags, stat-channel names and ticket names."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from logic import utility as U

TZ = ZoneInfo("America/Los_Angeles")
MIN, HOUR, DAY = 60, 3600, 86400


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


NOW = ts(2026, 10, 7, 15, 0)  # a Wednesday, 3pm local


# ---------------------------------------------------------------- relative
@pytest.mark.parametrize("text,delta", [
    ("10m", 10 * MIN), ("10 min", 10 * MIN), ("10 minutes", 10 * MIN), ("2h", 2 * HOUR),
    ("2 hours", 2 * HOUR), ("1d", DAY), ("1 day", DAY), ("1w", 7 * DAY), ("in 10m", 10 * MIN),
    ("1h30m", 90 * MIN), ("1d 6h", 30 * HOUR), ("1h, 30m", 90 * MIN), ("1 hour and 5 minutes", 65 * MIN),
    ("90s", 90), ("  2H  ", 2 * HOUR), ("30d", 30 * DAY),
])
def test_relative(text, delta):
    assert U.parse_when(text, NOW, TZ) == NOW + delta


# ---------------------------------------------------------------- absolute
@pytest.mark.parametrize("text,due", [
    ("tomorrow", ts(2026, 10, 8, 9)),
    ("tomorrow 9am", ts(2026, 10, 8, 9)),
    ("tomorrow at 9am", ts(2026, 10, 8, 9)),
    ("9am tomorrow", ts(2026, 10, 8, 9)),
    ("tomorrow 9", ts(2026, 10, 8, 9)),
    ("tomorrow 21:30", ts(2026, 10, 8, 21, 30)),
    ("tmrw 6:15pm", ts(2026, 10, 8, 18, 15)),
    ("9pm", ts(2026, 10, 7, 21)),  # later today
    ("at 9pm", ts(2026, 10, 7, 21)),
    ("9am", ts(2026, 10, 8, 9)),  # already passed today: tomorrow
    ("12pm", ts(2026, 10, 8, 12)),
    ("12am", ts(2026, 10, 8, 0)),
    ("noon", ts(2026, 10, 8, 12)),
    ("midnight", ts(2026, 10, 8, 0)),
    ("today 5pm", ts(2026, 10, 7, 17)),
    ("tonight", ts(2026, 10, 7, 20)),
    ("tonight 11pm", ts(2026, 10, 7, 23)),
    ("friday 18:30", ts(2026, 10, 9, 18, 30)),
    ("next fri 8pm", ts(2026, 10, 9, 20)),
    ("wednesday 5pm", ts(2026, 10, 7, 17)),  # today, still ahead
    ("wednesday 9am", ts(2026, 10, 14, 9)),  # today but passed: next week
    ("monday", ts(2026, 10, 12, 9)),
])
def test_absolute(text, due):
    assert U.parse_when(text, NOW, TZ) == due


def test_absolute_across_dst_uses_local_wall_clock():
    before = ts(2026, 10, 31, 15)  # DST ends Nov 1 in Los Angeles
    assert U.parse_when("tomorrow 9am", before, TZ) == ts(2026, 11, 1, 9)
    assert ts(2026, 11, 1, 9) - before == 19 * HOUR  # 18 h of clock + the extra hour


@pytest.mark.parametrize("text", ["", "   ", "soon", "9", "13pm", "25:00", "9:75", "10 parsecs",
                                  "tomorrow banana", "10mo", "yesterday", "-5m"])
def test_unreadable(text):
    with pytest.raises(U.WhenError):
        U.parse_when(text, NOW, TZ)


def test_too_soon_and_too_far():
    with pytest.raises(U.WhenError, match="less than a minute"):
        U.parse_when("30s", NOW, TZ)
    with pytest.raises(U.WhenError, match="less than a minute"):
        U.parse_when("0m", NOW, TZ)
    with pytest.raises(U.WhenError, match="30 days"):
        U.parse_when("31d", NOW, TZ)
    with pytest.raises(U.WhenError, match="30 days"):
        U.parse_when("5w", NOW, TZ)
    assert U.parse_when("1m", NOW, TZ) == NOW + MIN


def test_error_message_quotes_input_and_gives_examples():
    with pytest.raises(U.WhenError) as e:
        U.parse_when("whenever", NOW, TZ)
    assert '"whenever"' in str(e.value) and "10m" in str(e.value)


# ---------------------------------------------------------------- reminder rules
def test_check_reminder():
    assert U.check_reminder("buy milk", 0) is None
    assert U.check_reminder("buy milk", 9) is None
    assert "10 reminders" in U.check_reminder("buy milk", 10)
    assert U.check_reminder("x" * 200, 0) is None
    assert "200" in U.check_reminder("x" * 201, 0)
    assert U.check_reminder("   ", 0) is not None


def test_reminder_wording():
    msg = U.reminder_message(7, "stretch", 1000)
    assert msg.startswith("⏰ <@7>") and "stretch" in msg and "<t:1000:R>" in msg
    assert U.reminder_line(3, 2000, "a\nb") == "`#3`  <t:2000:R>  a b"
    assert U.reminder_line(3, 2000, "x" * 200).endswith("…")


def test_choice_label_fits_autocomplete():
    label = U.choice_label(12, NOW + 2 * HOUR + 5 * MIN, NOW, "buy milk")
    assert label == "#12 · in 2h 5m · buy milk"
    assert len(U.choice_label(12, NOW + DAY, NOW, "y" * 300)) <= 100


@pytest.mark.parametrize("seconds,text", [(0, "1m"), (59, "1m"), (5 * MIN, "5m"), (HOUR, "1h"),
                                          (HOUR + 60, "1h 1m"), (DAY, "1d"), (DAY + 3 * HOUR, "1d 3h")])
def test_span(seconds, text):
    assert U.span(seconds) == text


# ---------------------------------------------------------------- AFK
def test_afk_notice():
    assert U.afk_notice("Sam", "dinner", 100) == "💤 Sam is AFK: dinner (since <t:100:R>)"
    assert U.afk_notice("Sam", None, 100) == "💤 Sam is AFK (since <t:100:R>)"
    assert U.afk_notice("Sam", "   ", 100) == "💤 Sam is AFK (since <t:100:R>)"
    assert "Welcome back, Sam" in U.welcome_back("Sam", 100)


def test_afk_cooldown():
    assert U.afk_due(None, NOW)
    assert not U.afk_due(NOW - 599, NOW)
    assert U.afk_due(NOW - 600, NOW)


# ---------------------------------------------------------------- suggestions
def test_retag_replaces_status_and_keeps_other_tags():
    assert U.retag(["Idea"], "accepted") == ["Accepted"]
    assert U.retag([], "done") == ["Done"]
    assert U.retag(["accepted", "Bot"], "denied") == ["Denied", "Bot"]
    assert U.retag(["Done", "A", "B", "C", "D", "E"], "accepted") == ["Accepted", "A", "B", "C", "D"]


def test_status_text():
    assert U.status_text("accepted", "<@1>", None) == "✅ **Accepted** by <@1>"
    assert U.status_text("done", "<@1>", " shipped ") == "🎉 **Done** by <@1>\n> shipped"


# ---------------------------------------------------------------- stat channels
def test_stat_names():
    assert U.stat_name("members", 1234) == "👥 Members: 1,234"
    assert U.stat_name("online", 7) == "🟢 Online: 7"
    assert U.is_stat_channel("👥 Members: 12", "members")
    assert U.is_stat_channel("Members: 0", "members")
    assert not U.is_stat_channel("👥 Members: 12", "online")
    assert not U.is_stat_channel("🔊 Lobby", "members")


def test_rename_due():
    assert not U.rename_due("a", "a", None, NOW)
    assert U.rename_due("a", "b", None, NOW)
    assert not U.rename_due("a", "b", NOW - 599, NOW)
    assert U.rename_due("a", "b", NOW - 600, NOW)


# ---------------------------------------------------------------- tickets
def test_ticket_name():
    assert U.ticket_name("sam_99") == "ticket-sam_99"
    assert U.ticket_name("Weird Name!!") == "ticket-weird-name"
    assert U.ticket_name("") == "ticket-member"
    assert len(U.ticket_name("x" * 300)) == 100


def test_can_close_ticket():
    assert U.can_close_ticket(1, 1, False)
    assert not U.can_close_ticket(2, 1, False)
    assert U.can_close_ticket(2, 1, True)
