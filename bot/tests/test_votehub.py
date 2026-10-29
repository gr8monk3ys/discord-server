"""Pure rules of the vote hub: link rows, next-bump time, the weekly podium and its refs."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord

from logic import votehub as V
from logic.quests import MIN_ACCOUNT_DAYS
from logic.schedule import latest_due

TZ = ZoneInfo("America/Los_Angeles")
DAY = 86400
NOW = int(datetime(2026, 10, 11, 17, 0, tzinfo=TZ).timestamp())  # a Sunday


def old(n=1):
    return discord.utils.time_snowflake(datetime(2020, 1, 1, tzinfo=timezone.utc)) + n


def young(n=1):
    return discord.utils.time_snowflake(datetime.fromtimestamp(NOW - 5 * DAY, timezone.utc)) + n


def test_urls_are_the_documented_ones():
    assert V.DISCORD_ME_URL == "https://discord.me/chill-gamers-hq"
    assert V.SHARE_URL == "https://discord.lscaturchio.xyz/join"
    assert V.TOPGG_URL == "" or V.link_url(V.TOPGG_URL)
    assert "discord.gg" not in V.SHARE_URL  # the invite code lives only in site/src/data/site-config.json
    assert V.REWARDS == (300, 200, 100)


def test_link_url_accepts_only_plain_https():
    assert V.link_url("https://top.gg/discord/servers/abc") == "https://top.gg/discord/servers/abc"
    assert V.link_url("  https://top.gg/x ") == "https://top.gg/x"
    for bad in ("", None, "http://top.gg/x", "top.gg/x", "https://a b", "https://x)[y](https://evil",
                "javascript:alert(1)", "https://" + "a" * 300):
        assert V.link_url(bad) is None, bad


def test_topgg_row_says_coming_soon_until_the_listing_exists():
    assert V.topgg_text("") == V.COMING_SOON
    assert "soon" in V.COMING_SOON.lower()
    assert V.topgg_text("not a url") == V.COMING_SOON
    text = V.topgg_text("https://top.gg/discord/servers/abc")
    assert "(https://top.gg/discord/servers/abc)" in text and "soon" not in text.lower()
    assert V.topgg_text() == (V.topgg_text(V.TOPGG_URL) if V.TOPGG_URL else V.COMING_SOON)


def test_next_bump_at():
    assert V.next_bump_at(None, None, 7200) is None
    assert V.next_bump_at(None, 1000, 7200) == 8200
    assert V.next_bump_at(9000, None, 7200) == 9000
    assert V.next_bump_at(9000, 1000, 7200) == 9000  # a bump by an unknown user set the reminder later


def test_bump_ready_text():
    assert V.bump_ready_text(None, NOW) == "Ready now."
    assert V.bump_ready_text(NOW, NOW) == "Ready now."
    assert V.bump_ready_text(NOW - 5, NOW) == "Ready now."
    assert V.bump_ready_text(NOW + 600, NOW) == f"Ready <t:{NOW + 600}:R>."


def test_disboard_text_shows_where_when_and_my_counts():
    text = V.disboard_text("<#5>", NOW + 60, NOW, 2, 7)
    assert "`/bump` in <#5>" in text and f"<t:{NOW + 60}:R>" in text
    assert "2 this week" in text and "7 in the past 30 days" in text
    text = V.disboard_text(None, None, NOW, 0, 0)
    assert "in the bot channel" in text and "Ready now." in text and "haven't bumped yet" in text


def test_other_rows():
    assert V.DISCORD_ME_URL in V.discord_me_text()
    assert f"<{V.SHARE_URL}>" in V.share_text()  # no preview embed
    assert "300/200/100" in V.rewards_text() and str(MIN_ACCOUNT_DAYS) in V.rewards_text()


def test_plural():
    assert V.plural(1, "bump") == "1 bump" and V.plural(3, "bump") == "3 bumps"


def test_rank_bumpers_orders_by_count_then_first_there_then_id():
    rows = [(1, 2, 500), (2, 5, 900), (3, 2, 400), (4, 2, 400), (5, 1, 10), (6, 0, 1)]
    assert V.rank_bumpers(rows) == [(2, 5), (3, 2), (4, 2)]
    assert V.rank_bumpers(rows, limit=10) == [(2, 5), (3, 2), (4, 2), (1, 2), (5, 1)]
    assert V.rank_bumpers([]) == []


def test_places_pay_only_established_accounts():
    a, b, c = old(1), young(2), old(3)
    podium = V.places([(a, 3, 10), (b, 2, 10), (c, 1, 10)], NOW)
    assert [(p.rank, p.user_id, p.bumps, p.coins) for p in podium] == [(1, a, 3, 300), (2, b, 2, 0), (3, c, 1, 100)]
    assert V.places([], NOW) == []
    assert len(V.places([(old(i), 1, i) for i in range(1, 9)], NOW)) == 3


def test_ref_is_support_week_place():
    period = latest_due(V.SUPPORT_JOB, NOW, TZ)
    assert period.key == "support:2026-W41" and period.scheduled_at == NOW
    assert V.week_of(period.key) == "2026-W41"
    assert V.ref(period.key, 1) == "support:2026-W41:1"
    assert V.ref(period.key, 3) == "support:2026-W41:3"


def test_job_is_sundays_at_five():
    assert (V.SUPPORT_JOB.weekday, V.SUPPORT_JOB.hour, V.SUPPORT_JOB.minute) == (6, 17, 0)
    period = latest_due(V.SUPPORT_JOB, NOW, TZ)
    assert period.window_end - period.window_start == 7 * DAY


def test_thanks_text():
    text = V.thanks_text([V.Place(1, 11, 4, 300), V.Place(2, 12, 1, 0)])
    assert "🥇 <@11>  4 bumps · +300 coins" in text
    assert f"🥈 <@12>  1 bump · coins need a {MIN_ACCOUNT_DAYS}+ day old account" in text
    assert "🥉" not in text and "`/vote`" in text
