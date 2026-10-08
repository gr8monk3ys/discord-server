"""Pure rules for game news: source config, Steam and RSS/Atom parsing, link rebuilding,
plain-text summaries and which items to post."""

import json

import pytest

import config
from logic import news as N

NOW = 1_791_400_000  # 2026-10-07
DAY = 86400


# ---------------------------------------------------------------- sources
def test_shipped_sources_file_is_valid_and_matches_games():
    data = json.loads(N.SOURCES_FILE.read_text(encoding="utf-8"))
    roles = {g.role for g in config.GAMES}
    sources = N.load_sources(data, roles)
    named = [k for k in data if not k.startswith("_")]
    assert len(sources) == len(named) and named  # every entry is a real game and valid
    for key in named:
        assert data[key].get("comment"), key  # each says how it was verified
    by_game = {s.game: s for s in sources}
    assert by_game["Counter-Strike 2"].appid == 730
    assert by_game["Minecraft"].host == "www.minecraft.net"


def test_load_sources_skips_unknown_games_and_bad_entries():
    data = {
        "_comment": "x",
        "Valorant": {"steam_appid": 5},
        "Ghost": {"steam_appid": 7},  # not a game here
        "Counter-Strike 2": {"steam_appid": "730"},  # a string, not an int
        "Apex Legends": {"steam_appid": True},
        "Minecraft": {"rss": "http://www.minecraft.net/feed"},  # not https
        "Fortnite": {"rss": "https://user@fortnite.com/rss"},
        "Roblox": {"rss": "https://roblox.com:8443/rss"},
        "GTA Online": {"steam_appid": 1, "rss": "https://x.com/rss"},  # both: ambiguous
        "Wardogs": "nope",
        "League of Legends": {"rss": "https://news.example.com/feed.xml", "comment": "ok"},
    }
    roles = {g.role for g in config.GAMES}
    sources = N.load_sources(data, roles)
    assert [(s.game, s.kind) for s in sources] == [("Valorant", "steam"), ("League of Legends", "rss")]
    assert sources[0].key == "steam:5"
    assert sources[1].key == "rss:https://news.example.com/feed.xml"
    assert sources[1].host == "news.example.com"


def test_load_sources_rejects_non_dict():
    assert N.load_sources([], {"Minecraft"}) == []
    assert N.load_sources(None, {"Minecraft"}) == []


def test_steam_api_url_asks_for_official_items_only():
    url = N.steam_api_url(730)
    assert url.startswith("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?")
    assert "appid=730" in url and "count=5" in url and "maxlength=300" in url and "format=json" in url
    assert "feeds=steam_community_announcements" in url


# ---------------------------------------------------------------- Steam
def steam_body(*items, appid=730):
    return json.dumps({"appnews": {"appid": appid, "newsitems": list(items)}}).encode()


def steam_item(gid="1845383656394875", title="Counter-Strike 2 Update", feedname="steam_community_announcements",
               feed_type=1, date=NOW - 3600, contents="[p]Fixed [b]stuff[/b][/p]", **extra):
    return dict(gid=gid, title=title, url=f"https://steamstore-a.akamaihd.net/news/externalpost/x/{gid}",
                is_external_url=True, contents=contents, feedname=feedname, feed_type=feed_type, date=date,
                appid=730, **extra)


def test_parse_steam_keeps_official_items_and_rebuilds_links():
    body = steam_body(
        steam_item(),
        steam_item(gid="2", feedname="PC Gamer", feed_type=0, title="press"),
        steam_item(gid="3", feedname="weird", feed_type=1, title="official by type"),
        steam_item(gid="4", feedname="steam_community_announcements", feed_type=0, title="official by name"),
    )
    items = N.parse_steam(body, 730)
    assert [i.id for i in items] == ["1845383656394875", "3", "4"]
    first = items[0]
    assert first.link == "https://store.steampowered.com/news/app/730/view/1845383656394875"
    assert first.title == "Counter-Strike 2 Update"
    assert first.summary == "Fixed stuff"
    assert first.published == NOW - 3600


def test_parse_steam_drops_malformed_items():
    body = steam_body(
        steam_item(gid="12ab"),  # not digits
        steam_item(gid="9" * 25),  # too long
        steam_item(gid=123),  # not a string
        steam_item(gid="5", date="yesterday"),
        steam_item(gid="6", date=True),
        "junk",
        steam_item(gid="7", title=None),
    )
    items = N.parse_steam(body, 730)
    assert [i.id for i in items] == ["7"]
    assert items[0].title == "Untitled"


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'{"appnews": 3}', b'{"appnews": {"newsitems": 3}}',
                                  b"\xff\xfe"])
def test_parse_steam_raises_on_garbage(body):
    with pytest.raises(ValueError):
        N.parse_steam(body, 730)


def test_parse_steam_rejects_oversized_body():
    with pytest.raises(ValueError):
        N.parse_steam(b" " * (N.MAX_BODY + 1), 730)


def test_parse_steam_rejects_another_apps_items():
    body = steam_body(steam_item(), steam_item(gid="8"))
    data = json.loads(body)
    data["appnews"]["newsitems"][0]["appid"] = 999
    items = N.parse_steam(json.dumps(data).encode(), 730)
    assert [i.id for i in items] == ["8"]


# ---------------------------------------------------------------- plain text
def test_plain_text_strips_bbcode_html_and_placeholders():
    raw = ("[h1]Big [i]news[/i][/h1]\n[img]{STEAM_CLAN_IMAGE}/1/x.png[/img]<p>Hello &amp; "
           "<a href='https://evil'>welcome</a></p>[url=https://x]link[/url] [list][*]one[*]two[/list]")
    assert N.plain_text(raw) == "Big news Hello & welcome link one two"


def test_plain_text_splits_sentences_steam_glued_together():
    raw = "\\Pet pages now have text art.Added play menu buttons.FIXED:Resolved an issue. v1.2 ok"
    assert N.plain_text(raw) == "Pet pages now have text art. Added play menu buttons. FIXED: Resolved an issue. v1.2 ok"


def test_plain_text_drops_bare_urls():
    assert N.plain_text("Read more at https://evil.example/x?y=1 now") == "Read more at now"
    assert N.plain_text("see www.evil.example/x") == "see"


def test_plain_text_truncates_and_handles_empty():
    assert N.plain_text("") == ""
    assert N.plain_text(None) == ""
    long = "word " * 200
    out = N.plain_text(long)
    assert len(out) <= N.MAX_SUMMARY and out.endswith("…")
    assert N.plain_text("a\x00b\x07c") == "a b c"


def test_plain_text_unescapes_double_encoded_entities_once_more():
    assert N.plain_text("the game&amp;#39;s modes") == "the game's modes"


def test_clean_title():
    assert N.clean_title("  A \n B  ") == "A B"
    assert N.clean_title("") == "Untitled"
    assert N.clean_title("&amp;amp; caps") == "& caps"
    assert len(N.clean_title("x" * 500)) == N.MAX_TITLE


# ---------------------------------------------------------------- RSS / Atom
MC = "https://www.minecraft.net/en-us/feeds/community-content/rss"


def rss(*items, encoding="utf-8", decl=True):
    body = "".join(items)
    head = f'<?xml version="1.0" encoding="{encoding}"?>' if decl else ""
    text = (f'{head}<rss xmlns:a10="http://www.w3.org/2005/Atom" version="2.0"><channel>'
            f"<title>Minecraft</title>{body}</channel></rss>")
    return text.encode(encoding)


def mc_item(slug="marketplace-oct", title="Marketplace Content: October 2026", date="Tue, 06 Oct 2026 20:00:00 Z",
            desc="Discover what&#8217;s new", link=None):
    link = link if link is not None else f'<a10:link href="https://www.minecraft.net/en-us/article/{slug}" />'
    return f"<item><title>{title}</title><description>{desc}</description><pubDate>{date}</pubDate>{link}</item>"


def test_parse_rss_minecraft_shape_in_utf16_without_bom():
    body = rss(mc_item(), mc_item(slug="snap-3", title="Minecraft 26.4 Snapshot 3"), encoding="utf-16-le")
    assert body[:2] == b"<\x00"
    items = N.parse_rss(body, "www.minecraft.net")
    assert [i.title for i in items] == ["Marketplace Content: October 2026", "Minecraft 26.4 Snapshot 3"]
    first = items[0]
    assert first.link == "https://www.minecraft.net/en-us/article/marketplace-oct"
    assert first.id == first.link
    assert first.summary == "Discover what’s new"
    assert first.published == 1791316800  # 2026-10-06 20:00 UTC


def test_parse_rss_utf16_with_bom_and_utf8_bom():
    for enc in ("utf-16", "utf-8-sig"):
        body = rss(mc_item(), encoding=enc)
        assert [i.title for i in N.parse_rss(body, "www.minecraft.net")] == ["Marketplace Content: October 2026"]


def test_parse_rss_plain_link_and_guid():
    item = ("<item><title>T</title><link>https://www.minecraft.net/a?b=1#frag</link>"
            "<guid isPermaLink='false'>abc-123</guid><pubDate>Tue, 06 Oct 2026 20:00:00 GMT</pubDate></item>")
    (i,) = N.parse_rss(rss(item), "www.minecraft.net")
    assert i.link == "https://www.minecraft.net/a?b=1"  # fragment dropped
    assert i.id == "abc-123"


@pytest.mark.parametrize("href", [
    "http://www.minecraft.net/x",  # not https
    "https://evil.example/x",  # wrong host
    "https://www.minecraft.net.evil.example/x",
    "https://user@www.minecraft.net/x",
    "https://www.minecraft.net:444/x",
    "javascript:alert(1)",
    "https://www.minecraft.net/a b",  # whitespace
    "https://www.minecraft.net/a)\n(b",
    "//www.minecraft.net/x",
    "",
])
def test_parse_rss_drops_links_off_the_expected_host(href):
    item = mc_item(link=f'<a10:link href="{href}" />')
    assert N.parse_rss(rss(item), "www.minecraft.net") == []


def test_parse_rss_host_match_ignores_case():
    item = mc_item(link='<a10:link href="https://WWW.Minecraft.NET/en-us/article/x" />')
    (i,) = N.parse_rss(rss(item), "www.minecraft.net")
    assert i.link == "https://www.minecraft.net/en-us/article/x"


def test_parse_rss_skips_items_without_a_date():
    items = N.parse_rss(rss(mc_item(date=""), mc_item(slug="b", date="not a date"), mc_item(slug="c")),
                        "www.minecraft.net")
    assert [i.link.rsplit("/", 1)[1] for i in items] == ["c"]


def test_parse_atom_feed():
    body = ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>News</title>'
            '<entry><id>tag:x,2026:1</id><title type="html">Patch &lt;b&gt;1.2&lt;/b&gt;</title>'
            '<link rel="alternate" href="https://news.example.com/p/1"/>'
            '<link rel="enclosure" href="https://evil.example/e"/>'
            '<published>2026-10-06T20:00:00Z</published><summary>Hi there</summary></entry>'
            '<entry><id>2</id><title>No date</title><link href="https://news.example.com/p/2"/></entry>'
            '<entry><id>3</id><title>Updated only</title><link href="https://news.example.com/p/3"/>'
            '<updated>2026-10-05T10:00:00+02:00</updated><content>Body</content></entry>'
            "</feed>").encode()
    items = N.parse_rss(body, "news.example.com")
    assert [(i.id, i.title, i.link, i.published, i.summary) for i in items] == [
        ("tag:x,2026:1", "Patch 1.2", "https://news.example.com/p/1", 1791316800, "Hi there"),
        ("3", "Updated only", "https://news.example.com/p/3", 1791316800 - DAY - 12 * 3600, "Body"),
    ]


@pytest.mark.parametrize("body", [
    b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel></channel></rss>',
    b'<!doctype rss><rss/>',
    b'<rss><!ENTITY x "y"></rss>',
    '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE rss [<!ENTITY a "b">]><rss/>'.encode("utf-16-le"),
    '<!DOCTYPE rss><rss/>'.encode("utf-16"),
    '<!DOCTYPE rss><rss/>'.encode("utf-16-be"),
])
def test_parse_rss_refuses_dtds_in_any_encoding(body):
    with pytest.raises(ValueError, match="DTD"):
        N.parse_rss(body, "www.minecraft.net")


@pytest.mark.parametrize("body", [b"", b"<html><body>hi</body></html>", b"<rss><channel>", b"\xff\xfe\x00",
                                  "<rss/>".encode("utf-32"), b"<rss>\xff</rss>"])
def test_parse_rss_raises_on_garbage(body):
    with pytest.raises(ValueError):
        N.parse_rss(body, "www.minecraft.net")


def test_parse_rss_rejects_oversized_body():
    with pytest.raises(ValueError):
        N.parse_rss(b" " * (N.MAX_BODY + 1), "www.minecraft.net")


def test_parse_rss_caps_ids():
    item = (f"<item><title>T</title><guid>{'g' * 500}</guid><link>https://www.minecraft.net/x</link>"
            "<pubDate>Tue, 06 Oct 2026 20:00:00 GMT</pubDate></item>")
    (i,) = N.parse_rss(rss(item), "www.minecraft.net")
    assert len(i.id) <= N.MAX_ID


# ---------------------------------------------------------------- choosing
def item(i, age):
    return N.Item(id=str(i), title=f"t{i}", link=f"https://store.steampowered.com/news/app/1/view/{i}",
                  summary="", published=NOW - age)


def test_select_newest_two_unseen_within_a_week_posted_oldest_first():
    items = [item(1, 5 * 3600), item(2, 1 * 3600), item(3, 3 * 3600), item(4, 8 * DAY), item(5, 2 * 3600)]
    post, quiet = N.select(items, seen={"2"}, now=NOW)
    assert [i.id for i in post] == ["3", "5"]  # newest unseen are 5 then 3; posted oldest first
    assert [i.id for i in quiet] == ["1"]  # unseen but over the per-poll cap: marked seen without posting


def test_select_nothing_new():
    items = [item(1, 3600), item(2, 9 * DAY)]
    assert N.select(items, seen={"1"}, now=NOW) == ([], [])


def test_select_age_limit_is_inclusive_at_seven_days():
    post, _ = N.select([item(1, N.MAX_AGE), item(2, N.MAX_AGE + 1)], seen=set(), now=NOW)
    assert [i.id for i in post] == ["1"]


def test_select_dedupes_repeated_ids():
    post, quiet = N.select([item(1, 10), item(1, 20)], seen=set(), now=NOW)
    assert [i.id for i in post] == ["1"] and quiet == []


def test_latest_ignores_age_and_seen():
    assert N.latest([item(1, 9 * DAY), item(2, 10 * DAY)]).id == "1"
    assert N.latest([]) is None
