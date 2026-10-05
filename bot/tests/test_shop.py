"""Pure rules for the coin shop and monthly seasons (logic/shop.py)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from logic import coins as C
from logic import shop as S

TZ = ZoneInfo("America/Los_Angeles")
KEEPER = 0xF1C40F
MOD = 0x3498DB


def ts(y, m, d, h=0, mi=0):
    return int(datetime(y, m, d, h, mi, tzinfo=TZ).timestamp())


# ---------------------------------------------------------------- items
def test_prices_and_durations():
    assert (S.ITEMS["color"].price, S.ITEMS["color"].duration) == (2_000, 30 * S.DAY)
    assert (S.ITEMS["hype"].price, S.ITEMS["hype"].duration) == (500, S.DAY)
    assert (S.ITEMS["shoutout"].price, S.ITEMS["shoutout"].duration) == (300, S.DAY)


def test_extend_adds_to_remaining_time_or_starts_now():
    assert S.extend(None, 100, 50) == 150
    assert S.extend(90, 100, 50) == 150  # already expired
    assert S.extend(130, 100, 50) == 180  # 30 s left + 50


def test_role_name_is_prefixed_member_name_capped():
    assert S.role_name("Lorenzo") == "Colour · Lorenzo"
    assert S.role_name("x" * 40) == "Colour · " + "x" * 32
    assert S.role_name("  a   b ") == "Colour · a b"
    assert S.role_name("   ") == "Colour · member"


def test_role_name_can_never_impersonate_a_real_role():
    """Security: a member nicknamed after a staff role must not get a role the bot
    would match as that staff role (matching ignores emoji and punctuation)."""
    import config
    from names import slug
    real = {slug(n) for n in (config.KEEPER_ROLE, config.MOD_ROLE, config.SQUAD_ROLE, config.HYPE_ROLE,
                              config.SEASON_ROLE, config.CLIP_ROLE, config.LFG_ROLE, config.RECRUITER_ROLE,
                              config.BUMPER_ROLE, *(g.role for g in config.GAMES))}
    for nick in ("Keeper", "🔑 keeper", "MODERATOR", "Moderator!", "Squad", "Season Champ", "Valorant"):
        assert slug(S.role_name(nick)) not in real


# ---------------------------------------------------------------- colours
@pytest.mark.parametrize("text,value", [("#3BA55D", 0x3BA55D), ("#ff00ff", 0xFF00FF), ("  #ABCDEF ", 0xABCDEF),
                                        ("3BA55D", 0x3BA55D)])
def test_parse_color_ok(text, value):
    assert S.parse_color(text) == value


@pytest.mark.parametrize("text", ["", "red", "#12345", "#1234567", "#GGGGGG", "#12 345", "0x123456", None])
def test_parse_color_rejects_bad_format(text):
    assert S.parse_color(text) is None
    value, err = S.check_color(text)
    assert value is None and "hex" in err


def test_luminance_extremes():
    assert S.luminance(0x000000) == 0
    assert S.luminance(0xFFFFFF) == pytest.approx(1)


@pytest.mark.parametrize("text", ["#000000", "#111111", "#333333", "#1A0A2E", "#0000FF"])
def test_check_color_rejects_near_invisible(text):
    value, err = S.check_color(text)
    assert value is None and "dark" in err


@pytest.mark.parametrize("text", ["#555555", "#FFFFFF", "#3BA55D", "#FF66AA"])
def test_check_color_allows_readable(text):
    assert S.check_color(text, [KEEPER, MOD]) == (S.parse_color(text), None)


@pytest.mark.parametrize("text", ["#F1C40F", "#F0C010", "#EEC820", "#3498DB", "#3A9AE0"])
def test_check_color_rejects_staff_lookalikes(text):
    value, err = S.check_color(text, [KEEPER, MOD])
    assert value is None and "staff" in err


def test_staff_role_without_colour_is_ignored():
    # 0 = "no colour" on Discord: it must not block every greyish pick
    assert S.check_color("#555555", [0]) == (0x555555, None)


# ---------------------------------------------------------------- shoutouts
@pytest.mark.parametrize("text", ["gg everyone, great raid tonight", "x" * 140, "café ☕ <:pog:123>"])
def test_shoutout_ok(text):
    assert S.shoutout_error(text) is None


@pytest.mark.parametrize("text,word", [
    ("", "Write"), ("   ", "Write"), ("x" * 141, "140"),
    ("line one\nline two", "one line"), ("a\rb", "one line"),
    ("join https://evil.example", "link"), ("http://x.y", "link"), ("visit WWW.example.com", "link"),
    ("discord.gg/abc", "link"), ("discord.com/invite/abc", "link"),
    ("hi <@123>", "mention"), ("hi <@&123>", "mention"), ("see <#55>", "mention"),
    ("@everyone look", "mention"), ("@HERE look", "mention"),
])
def test_shoutout_rejected(text, word):
    assert word in S.shoutout_error(text)


def test_fmt_wait():
    assert S.fmt_wait(5) == "1m"
    assert S.fmt_wait(3 * S.HOUR + 5 * 60) == "3h 05m"


# ---------------------------------------------------------------- seasons
def test_season_reasons_count_activity_not_gambling_or_transfers():
    for r in (C.DAILY, C.VOICE, C.MESSAGE, C.CLIP, C.LFG, C.MVP, C.CLIP_WEEK, "trivia"):
        assert r in S.SEASON_REASONS
    for r in (C.GIVE, C.COINFLIP, "slots", "blackjack", "predict", "shop", "season"):
        assert r not in S.SEASON_REASONS


def test_month_key_uses_local_time():
    # 2026-10-01 05:00 UTC is still Sept 30 in Los Angeles
    assert S.month_key(int(datetime(2026, 10, 1, 5, tzinfo=ZoneInfo("UTC")).timestamp()), TZ) == "2026-09"
    assert S.month_key(ts(2026, 10, 1, 0, 0), TZ) == "2026-10"


def test_previous_key_wraps_year():
    assert S.previous_key("2026-10") == "2026-09"
    assert S.previous_key("2027-01") == "2026-12"


def test_month_bounds_are_local_midnights():
    assert S.month_bounds("2026-10", TZ) == (ts(2026, 10, 1), ts(2026, 11, 1))
    assert S.month_bounds("2026-12", TZ) == (ts(2026, 12, 1), ts(2027, 1, 1))


def test_month_bounds_across_dst():
    start, end = S.month_bounds("2026-03", TZ)  # spring forward: one hour short
    assert end - start == 31 * S.DAY - S.HOUR
    start, end = S.month_bounds("2026-11", TZ)  # fall back: one hour long
    assert end - start == 30 * S.DAY + S.HOUR
    assert S.month_bounds("2026-10", TZ)[1] == S.month_bounds("2026-11", TZ)[0]  # back to back


def test_days_left_counts_today():
    assert S.days_left(ts(2026, 10, 4, 12), TZ) == 28
    assert S.days_left(ts(2026, 10, 31, 23, 59), TZ) == 1
    assert S.days_left(ts(2026, 2, 1), TZ) == 28


def test_month_name():
    assert S.month_name("2026-09") == "September 2026"


def test_standings_order_and_ties():
    rows = [(5, 100, 50), (3, 300, 90), (4, 100, 20), (2, 100, 20), (9, 0, 1)]
    table = S.standings(rows)
    assert [(s.rank, s.user_id, s.points) for s in table] == [
        (1, 3, 300),
        (2, 2, 100),  # same points and first earning as 4: lower id first
        (3, 4, 100),
        (4, 5, 100),  # same points, started earning later
    ]
    assert S.find(table, 5).rank == 4 and S.find(table, 9) is None


def test_bonus_ref():
    assert S.bonus_ref("2026-09", 1) == "season:2026-09:1"
    assert S.BONUSES == (1_000, 500, 250)


def test_season_plan_first_run_marks_previous_done():
    assert S.season_plan(ts(2026, 10, 4), TZ, set()) == S.SeasonPlan(None, "2026-09")


def test_season_plan_runs_finished_month_once():
    t = ts(2026, 11, 1, 0, 5)
    assert S.season_plan(t, TZ, {"2026-09"}) == S.SeasonPlan("2026-10", None)
    assert S.season_plan(t, TZ, {"2026-09", "2026-10"}) == S.SeasonPlan(None, None)


def test_season_plan_skips_old_gaps():
    # bot was off all of October: only October (the just-finished month) is posted in November
    assert S.season_plan(ts(2026, 11, 2), TZ, {"2026-08"}) == S.SeasonPlan("2026-10", None)
