"""Tests for logic.textfilter: invites, links and blocked words in member text the bot reposts."""

from pathlib import Path

import pytest

from logic import textfilter as F

OWN = ("ourcode",)


def ok(text, **kw):
    kw.setdefault("invite_allowlist", OWN)
    return F.check(text, **kw) == (True, None)


def reason(text, **kw):
    kw.setdefault("invite_allowlist", OWN)
    allowed, why = F.check(text, **kw)
    assert allowed is False
    return why


# ---------------------------------------------------------------- basics
@pytest.mark.parametrize("text", [None, "", "   ", "mic pls, gold rank, chill vibes"])
def test_empty_and_plain_text_pass(text):
    assert ok(text)


def test_messages_never_echo_and_cover_every_reason():
    for why in (F.INVITE, F.LINK, F.WORD):
        assert F.message(why) and "{" not in F.message(why)
    assert F.message("unknown") == F.MESSAGES[F.WORD]


def test_check_all_returns_first_refusal():
    assert F.check_all(["fine", None, "also fine"], invite_allowlist=OWN) == (True, None)
    assert F.check_all(["fine", "discord.gg/raid", "cunt"], invite_allowlist=OWN) == (False, F.INVITE)
    assert F.check_all(["see example.com"], allow_links=False) == (False, F.LINK)


# ---------------------------------------------------------------- invites
@pytest.mark.parametrize("text", [
    "join discord.gg/raidserver",
    "https://discord.com/invite/abc-123",
    "discordapp.com/invite/xyz",
    "DISCORD.GG/Shout",
    "discord . gg / spaced",
    "discord(dot)gg/abc",
    "discord[.]gg/abc",
    "discord dot gg/abc",
    "dsc.gg/abc",
    "d1sc0rd.gg/abc",
    "discord​.gg/zerowidth",
    "ｄｉｓｃｏｒｄ．ｇｇ/fullwidth",
    "discord.gg/",
])
def test_foreign_invites_blocked_even_where_links_are_allowed(text):
    assert reason(text, allow_links=True) == F.INVITE


def test_own_invite_allowed_where_links_are_allowed():
    assert ok("come back any time: discord.gg/ourcode", allow_links=True)
    assert ok("https://discord.com/invite/OurCode", allow_links=True)  # case-insensitive


def test_own_invite_is_still_a_link_in_link_free_fields():
    assert reason("discord.gg/ourcode", allow_links=False) == F.LINK


def test_own_invite_next_to_a_foreign_one_is_blocked():
    assert reason("discord.gg/ourcode or discord.gg/other") == F.INVITE


def test_default_allowlist_comes_from_site_config():
    code = next(iter(F.OWN_INVITES))  # the repo's site/config.js has a real code
    assert F.check(f"discord.gg/{code}") == (True, None)
    assert F.check("discord.gg/someone-else") == (False, F.INVITE)


def test_load_own_invites_handles_missing_and_placeholder(tmp_path):
    assert F.load_own_invites(tmp_path / "nope.js") == frozenset()
    placeholder = tmp_path / "config.js"
    placeholder.write_text('window.SITE_CONFIG = { INVITE_CODE: "REPLACE_ME" };', encoding="utf-8")
    assert F.load_own_invites(placeholder) == frozenset()
    placeholder.write_text('window.SITE_CONFIG = { INVITE_CODE: "AbC-12" };', encoding="utf-8")
    assert F.load_own_invites(placeholder) == frozenset({"abc-12"})


# ---------------------------------------------------------------- links
@pytest.mark.parametrize("text", [
    "https://example.org/x",
    "http://bit.ly/abc",
    "check www.example.org",
    "free nitro at steamgift.ru",
    "grab it on example.com",
    "[click me](https://evil.example)",
    "[totally safe](<https://evil.example>)",
    "steam://run/440",
])
def test_links_blocked_only_when_disallowed(text):
    assert reason(text, allow_links=False) == F.LINK
    assert ok(text, allow_links=True)


@pytest.mark.parametrize("text", [
    "I'm in. To play later", "e.g. ranked", "9pm, gold+", "plat 3.5 kd", "u.s. servers", "1v1 me",
    "lol. gg", "gg wp", "need 2 more",
])
def test_ordinary_short_text_is_not_a_link(text):
    assert ok(text, allow_links=False)


# ---------------------------------------------------------------- blocked words
@pytest.mark.parametrize("text", [
    "cunt",
    "CUNT",
    "you cunt.",
    "c.u.n.t",
    "c u n t",
    "i am a c u n t",
    "c-u-n-t",
    "c_u_n_t",
    "cuuuunt",
    "kïkë",
    "n1gg3r",
    "niiiiggerrr",
    "n!gger".replace("!", "1"),
    "f@ggot",
    "f4gg0t$",
    "faggots",
    "niggaz",
    "wh0re",
    "$lut",
    "r4pe",
    "pornhub",
    "nіgger",  # Cyrillic і
    "slur,cunt,word",
    "ćunt",  # combining accent
    "c­unt",  # soft hyphen
])
def test_evasions_are_caught(text):
    assert reason(text) == F.WORD


@pytest.mark.parametrize("text", [
    "Scunthorpe United", "a classic match", "assassin main", "cockpit view in flight sim",
    "Niger and Nigeria", "spick and span", "therapist", "grape juice", "cocktail hour",
    "pass the class", "retardant gel", "shiitake", "analysis paralysis", "Dickens novel",
    "cum laude", "snigger", "skillet", "raccoon city", "peacock", "hancock", "pussycat dolls",
    "Penistone", "Arsenal fans", "essex", "sussex", "title", "glass cannon", "5v5 at 7pm",
    "1v1 me bro", "ace 4 4 4", "a b c", "I a m o k",
])
def test_ordinary_words_are_not_blocked(text):
    assert ok(text, allow_links=True)


def test_stretched_forms_cut_runs_to_one_or_two():
    assert "nigger" in F.stretched_forms("niiiggger")
    assert F.stretched_forms("cook") == {"cook", "cok"}
    assert "cock" not in F.stretched_forms("cooook")


def test_word_list_is_lowercase_and_allowlist_never_overlaps():
    assert all(w == w.lower() and w.isalpha() for w in F.BLOCKED_WORDS)
    assert not (F.BLOCKED_WORDS & F.ALLOWED_WORDS)


def test_huge_input_is_bounded():
    text = "a " * 50_000 + "cunt"
    assert ok(text)  # past MAX_SCAN: not scanned, and it returns quickly
    assert reason("x" * 100 + " cunt") == F.WORD


def test_textfilter_has_no_discord_imports():
    source = Path(F.__file__).read_text(encoding="utf-8")
    assert "import discord" not in source


def test_screen_logs_user_and_reason_but_never_the_text(caplog):
    caplog.set_level("INFO", logger="logic.textfilter")
    assert F.screen("lfg note", 42, ["fine", None]) is None
    assert not caplog.records
    reply = F.screen("lfg note", 42, ["hey cunt"])
    assert reply == F.MESSAGES[F.WORD]
    (rec,) = caplog.records
    assert rec.levelname == "INFO"
    line = rec.getMessage()
    assert "42" in line and F.WORD in line and "lfg note" in line and "cunt" not in line
