"""Rules for the Supporter tier (logic/supporter.py). Pure: no Discord, no database."""

from logic import shop
from logic import supporter as S


# ---------------------------------------------------------------- the support link
def test_support_url_accepts_https_on_the_allowlist():
    assert S.support_url("https://ko-fi.com/someone") == "https://ko-fi.com/someone"
    assert S.support_url("  https://www.patreon.com/some_page  ") == "https://www.patreon.com/some_page"
    assert S.support_url("HTTPS://Ko-Fi.com/Someone") == "https://ko-fi.com/Someone"
    assert S.support_url("https://discord.com:443/servers/x?tab=subs#top") == "https://discord.com/servers/x?tab=subs"
    assert S.support_url("https://ko-fi.com") == "https://ko-fi.com/"


def test_support_url_refuses_everything_else():
    bad = [
        None, "", "   ", "ko-fi.com/someone", "http://ko-fi.com/someone", "ftp://ko-fi.com/x",
        "javascript:alert(1)", "https://evil.example/ko-fi.com", "https://ko-fi.com.evil.example/x",
        "https://notko-fi.com/x", "https://sub.ko-fi.com/x", "https://ko-fi.com@evil.example/x",
        "https://user:pw@ko-fi.com/x", "https://ko-fi.com:8443/x", "https://ko-fi.com:abc/x",
        "https://ko-fi.com/a b", "https://ko-fi.com/x\nhttps://evil.example", "https://ko-fi.com\\@evil.example",
        "https://ko-fі.com/x",  # a Cyrillic letter in the host
        "https://[ko-fi.com/x", "https://ko-fi.com/" + "a" * S.URL_MAX, "https://discord.gg/abc", "https:///ko-fi.com",
    ]
    for raw in bad:
        assert S.support_url(raw) is None, raw


# ---------------------------------------------------------------- stipend and thanks
def test_stipend_is_small_and_never_a_season_reason():
    assert S.STIPEND_COINS <= 500  # no more than the booster stipend (docs/ECONOMY.md)
    assert S.STIPEND_REASON not in shop.SEASON_REASONS
    assert S.stipend_ref("2026-11", 7) == "supporterstipend:2026-11:7"
    assert (S.STIPEND_JOB.day, S.STIPEND_JOB.hour, S.STIPEND_JOB.minute) == (1, 12, 15)


def test_thanked_once_then_again_only_after_a_long_break():
    assert S.should_thank(None, 1000)
    assert not S.should_thank(1000, 1000 + S.RETHANK_AFTER - 1)
    assert S.should_thank(1000, 1000 + S.RETHANK_AFTER)


def test_burst_caps_public_thanks():
    b = S.Burst(limit=2, window=600)
    assert b.allow(0) and b.allow(10) and not b.allow(20) and not b.allow(599)
    assert b.allow(600)  # the first one aged out
    assert not b.allow(605) and b.allow(610)


def test_text():
    assert S.thanks_text("<@5>", 250) == ("💜 <@5> is now a **Supporter** of the server. Thank you! "
                                         "**250 coins** are in your wallet as a thank-you.")
    assert S.thanks_text("<@5>", 0) == "💜 <@5> is now a **Supporter** of the server. Thank you!"
    assert S.flair(1_700_000_000) == "💜 **Supporter** since <t:1700000000:D>" and S.flair(None) == "💜 **Supporter**"
    perks = S.perks_text()
    assert "/profile" in perks and "250 coins" in perks and "not an advantage" in perks
