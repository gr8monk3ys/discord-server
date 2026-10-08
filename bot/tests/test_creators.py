"""Pure creator-spotlight rules: parsing what members link, YouTube feeds, Twitch streams."""

import html
import json

import pytest

from logic import creators as C

UC = "UC" + "a1B2c3D4e5F6g7H8i9J0k_"  # 24 chars
assert len(UC) == 24


# ---------------------------------------------------------------- youtube input
@pytest.mark.parametrize("text,expected", [
    (f"https://www.youtube.com/channel/{UC}", ("channel", UC)),
    (f"https://youtube.com/channel/{UC}/videos", ("channel", UC)),
    (f"https://m.youtube.com/channel/{UC}?si=abc", ("channel", UC)),
    (f"youtube.com/channel/{UC}", ("channel", UC)),
    (UC, ("channel", UC)),
    ("https://www.youtube.com/@Some.Creator_1", ("handle", "Some.Creator_1")),
    ("https://youtube.com/@cool-kid/featured", ("handle", "cool-kid")),
    ("@coolkid", ("handle", "coolkid")),
    ("  https://www.youtube.com/@coolkid?si=x  ", ("handle", "coolkid")),
])
def test_parse_youtube_accepts(text, expected):
    ref = C.parse_youtube(text)
    assert (ref.kind, ref.value) == expected


@pytest.mark.parametrize("text", [
    "", "hello", "https://evil.example/channel/" + UC, "https://www.youtube.com.evil.example/@x",
    "https://www.youtube.com/channel/UCshort", "https://www.youtube.com/channel/" + UC + "x",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/@", "@a",
    "https://www.youtube.com/@bad<script>", "javascript:alert(1)", "ftp://youtube.com/@abc",
    "https://user@youtube.com/@abc", "https://www.youtube.com:8080/@abc", "x" * 300,
])
def test_parse_youtube_rejects(text):
    assert C.parse_youtube(text) is None


def test_handle_url_and_feed_url():
    assert C.handle_url("coolkid") == "https://www.youtube.com/@coolkid"
    assert C.feed_url(UC) == f"https://www.youtube.com/feeds/videos.xml?channel_id={UC}"
    with pytest.raises(ValueError):
        C.feed_url("UC123")
    with pytest.raises(ValueError):
        C.handle_url("bad/handle")


def test_channel_url():
    assert C.channel_url(UC) == f"https://www.youtube.com/channel/{UC}"


# ---------------------------------------------------------------- channel page
def test_extract_channel_id_prefers_canonical_link():
    other = "UC" + "z" * 22
    html = (f'<html><head><link rel="canonical" href="https://www.youtube.com/channel/{UC}">'
            f'</head><body>"channelId":"{other}" "externalId":"{other}"</body></html>')
    assert C.extract_channel_id(html) == UC


def test_extract_channel_id_falls_back_to_external_then_channel_id():
    assert C.extract_channel_id(f'..."externalId":"{UC}"...') == UC
    assert C.extract_channel_id(f'..."channelId":"{UC}"...') == UC
    assert C.extract_channel_id(b'..."channelId":"' + UC.encode() + b'"...') == UC


@pytest.mark.parametrize("html", [
    "", "no ids here", '"channelId":"UCtooshort"', '"channelId":"XX' + "a" * 22 + '"',
    '"channelId":"' + UC + 'extra"',
])
def test_extract_channel_id_strict(html):
    assert C.extract_channel_id(html) is None


# ---------------------------------------------------------------- feed
def feed(*entries, title="Cool Kid"):
    body = "".join(
        f"<entry><id>yt:video:{vid}</id><yt:videoId>{vid}</yt:videoId><yt:channelId>{UC}</yt:channelId>"
        f"<title>{t}</title><link rel=\"alternate\" href=\"https://evil.example/{vid}\"/>"
        f"<published>2026-10-0{i + 1}T00:00:00+00:00</published></entry>"
        for i, (vid, t) in enumerate(entries))
    return (f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            f'xmlns="http://www.w3.org/2005/Atom"><title>{title}</title>{body}</feed>').encode()


def test_parse_feed_reads_videos_newest_first_as_given():
    f = C.parse_feed(feed(("dQw4w9WgXcQ", "Never &amp; gonna"), ("abcdefghijk", "Second")))
    assert f.title == "Cool Kid"
    assert [(v.id, v.title) for v in f.videos] == [("dQw4w9WgXcQ", "Never & gonna"), ("abcdefghijk", "Second")]


def test_parse_feed_drops_entries_with_bad_ids():
    f = C.parse_feed(feed(("bad id here", "x"), ("abc", "short"), ("abcdefghijk", "ok")))
    assert [v.id for v in f.videos] == ["abcdefghijk"]


def test_parse_feed_rejects_doctype_entities_and_garbage():
    evil = (b'<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY x "boom">]>'
            b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&x;</title></feed>')
    for data in (evil, b"not xml", b"", b"<html><body>hi</body></html>"):
        with pytest.raises(ValueError):
            C.parse_feed(data)


def test_parse_feed_rejects_oversized():
    with pytest.raises(ValueError):
        C.parse_feed(b" " * (C.MAX_FEED_BYTES + 1))


def test_parse_feed_empty_channel():
    f = C.parse_feed(feed())
    assert f.videos == [] and f.title == "Cool Kid"


def test_watch_url_only_for_valid_ids():
    assert C.watch_url("dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    for bad in ("", "short", "dQw4w9WgXcQ1", "dQw4w9WgX/Q", "../../../aa"):
        with pytest.raises(ValueError):
            C.watch_url(bad)


def test_unseen_oldest_first_and_capped():
    vids = [C.Video(f"vid{i:08d}", f"t{i}") for i in range(6)]  # newest first
    out = C.unseen(vids, {"vid00000001"}, limit=3)
    assert [v.id for v in out] == ["vid00000003", "vid00000002", "vid00000000"]
    assert C.unseen(vids, {v.id for v in vids}) == []


# ---------------------------------------------------------------- twitch
@pytest.mark.parametrize("text,expected", [
    ("https://www.twitch.tv/Cool_Kid", "cool_kid"),
    ("https://twitch.tv/coolkid/videos", "coolkid"),
    ("twitch.tv/coolkid", "coolkid"),
    ("m.twitch.tv/coolkid", "coolkid"),
    ("CoolKid", "coolkid"),
    ("@coolkid", "coolkid"),
])
def test_parse_twitch_accepts(text, expected):
    assert C.parse_twitch(text) == expected


@pytest.mark.parametrize("text", [
    "", "ab", "a" * 26, "https://evil.example/coolkid", "https://twitch.tv/", "cool kid", "cool-kid",
    "https://twitch.tv.evil.example/coolkid", "https://twitch.tv/directory", "https://www.twitch.tv/videos",
])
def test_parse_twitch_rejects(text):
    assert C.parse_twitch(text) is None


def test_twitch_url():
    assert C.twitch_url("coolkid") == "https://twitch.tv/coolkid"
    with pytest.raises(ValueError):
        C.twitch_url("bad/login")


def test_parse_streams_keeps_valid_live_streams():
    data = {"data": [
        {"id": "4011", "user_id": "77", "user_login": "CoolKid", "title": "ranked grind", "game_name": "Valorant",
         "type": "live"},
        {"id": "nope", "user_id": "78", "user_login": "x_y_z_", "title": "t", "type": "live"},
        {"id": "4012", "user_id": "79", "user_login": "bad login", "title": "t", "type": "live"},
        {"id": "4013", "user_id": "80", "user_login": "rerun_guy", "title": "t", "type": ""},
        "junk",
    ]}
    (s,) = C.parse_streams(data)
    assert (s.id, s.user_id, s.login, s.title, s.game) == ("4011", "77", "coolkid", "ranked grind", "Valorant")
    assert C.parse_streams({}) == [] and C.parse_streams(None) == [] and C.parse_streams({"data": "x"}) == []


def test_parse_users():
    data = {"data": [{"id": "77", "login": "CoolKid", "display_name": "CoolKid"}]}
    assert C.parse_user(data) == ("77", "coolkid", "CoolKid")
    assert C.parse_user({"data": []}) is None
    assert C.parse_user({"data": [{"id": "x", "login": "coolkid"}]}) is None


def test_streams_query_batches_of_100():
    ids = [str(i) for i in range(1, 251)]
    batches = C.streams_batches(ids)
    assert [len(b) for b in batches] == [100, 100, 50]
    assert batches[0][0] == ("user_id", "1")


def test_token_expiry_with_margin():
    assert C.token_expires_at(1000, 3600) == 1000 + 3600 - C.TOKEN_MARGIN
    assert C.token_expires_at(1000, None) == 1000 + 3600 - C.TOKEN_MARGIN


# ---------------------------------------------------------------- linking rules
def test_link_problem():
    assert C.link_problem([], "youtube") is None
    assert C.link_problem(["youtube"], "youtube") is None  # replacing
    assert C.link_problem(["youtube"], "twitch") is None
    assert C.link_problem(["youtube", "twitch"], "twitch") is None
    assert C.link_problem([], "tiktok") == "platform"
    assert C.link_problem(["a", "b"], "youtube") == "limit"


def test_clean_title():
    assert C.clean_title("  hi\u0000 there\n\nfriend ") == "hi there friend"
    assert len(C.clean_title("x" * 500)) == C.MAX_TITLE
    assert C.clean_title("x" * 500).endswith("…")
    assert C.clean_title("") == "Untitled"


# ---------------------------------------------------------------- ownership verification
def test_new_code_shape_and_alphabet():
    codes = {C.new_code() for _ in range(200)}
    assert len(codes) > 190  # random, not a counter
    for code in codes:
        assert C.CODE.fullmatch(code)
        assert code.startswith("FD-") and len(code) == 3 + C.CODE_LEN
        assert not set(code[3:]) & set("01OIL")  # nothing easy to misread


def test_new_code_uses_given_choice():
    picks = iter("7K2Q9X")
    assert C.new_code(choice=lambda alphabet: next(picks)) == "FD-7K2Q9X"


@pytest.mark.parametrize("text", [
    "my code FD-7K2Q9X thanks", "FD-7K2Q9X", "fd-7k2q9x", "(FD-7K2Q9X)", "line\nFD-7K2Q9X\nline",
    "FD‑7K2Q9X", "FD–7K2Q9X",
])
def test_code_in_finds_code(text):
    assert C.code_in("FD-7K2Q9X", [text])


@pytest.mark.parametrize("texts", [
    [], [""], ["FD-7K2Q9"], ["FD-7K2Q9XY"], ["XFD-7K2Q9X"], ["FD 7K2Q9X"], ["FD-7K2Q9Z"], [None],
])
def test_code_in_rejects(texts):
    assert not C.code_in("FD-7K2Q9X", texts)


def test_code_in_any_of_several_texts():
    assert C.code_in("FD-7K2Q9X", ["nope", "", "about me FD-7K2Q9X"])


def test_pending_expiry():
    assert not C.expired(1000, 1000 + C.CODE_TTL - 1)
    assert C.expired(1000, 1000 + C.CODE_TTL)
    assert C.expired(1000, 1000 + C.CODE_TTL * 5)


def test_cooldown_left():
    assert C.cooldown_left(None, 5000) == 0
    assert C.cooldown_left(5000, 5000) == C.VERIFY_COOLDOWN
    assert C.cooldown_left(5000, 5000 + 15) == C.VERIFY_COOLDOWN - 15
    assert C.cooldown_left(5000, 5000 + C.VERIFY_COOLDOWN) == 0


def channel_page(description="", og=None, meta=None, channel_id=UC):
    og = description if og is None else og
    meta = description if meta is None else meta
    data = json.dumps({"metadata": {"channelMetadataRenderer": {
        "title": "Cool Kid", "description": description, "externalId": channel_id}}})
    return (f'<html><head><link rel="canonical" href="https://www.youtube.com/channel/{channel_id}">'
            f'<meta name="description" content="{html.escape(meta)}">'
            f'<meta content="{html.escape(og)}" property="og:description">'
            f'</head><body><script>var ytInitialData = {data};</script></body></html>')


def test_youtube_descriptions_reads_meta_og_and_json():
    page = channel_page("hello & welcome\nFD-7K2Q9X")
    texts = C.youtube_descriptions(page)
    assert "hello & welcome\nFD-7K2Q9X" in texts  # from the JSON, newlines kept
    assert any(t.startswith("hello & welcome") for t in texts)
    assert C.code_in("FD-7K2Q9X", C.youtube_descriptions(page.encode()))


def test_youtube_descriptions_each_source_alone():
    assert C.code_in("FD-7K2Q9X", C.youtube_descriptions(channel_page("", og="FD-7K2Q9X")))
    assert C.code_in("FD-7K2Q9X", C.youtube_descriptions(channel_page("", meta="FD-7K2Q9X")))
    only_json = channel_page("x FD-7K2Q9X", og="", meta="")
    assert C.code_in("FD-7K2Q9X", C.youtube_descriptions(only_json))


def test_youtube_descriptions_new_header_view_model():
    page = '{"descriptionPreviewViewModel":{"description":{"content":"bio \u0026 FD-7K2Q9X"}}}'
    assert C.code_in("FD-7K2Q9X", C.youtube_descriptions(page))


def test_youtube_descriptions_ignores_other_description_keys():
    """A "description" elsewhere on the page (another channel, a video) is not the channel's."""
    page = '{"videoRenderer":{"description":"FD-7K2Q9X"}} <meta name="keywords" content="FD-7K2Q9X">'
    assert C.youtube_descriptions(page) == []


def test_youtube_descriptions_caps_size():
    huge = "x" * (C.MAX_DESCRIPTION * 3) + " FD-7K2Q9X"
    texts = C.youtube_descriptions(channel_page(huge))
    assert texts and all(len(t) <= C.MAX_DESCRIPTION for t in texts)
    assert not C.code_in("FD-7K2Q9X", texts)
    assert C.youtube_descriptions("") == [] and C.youtube_descriptions(b"\xff\xfe junk") == []


def test_youtube_descriptions_survives_broken_json_escapes():
    page = '"channelMetadataRenderer":{"title":"x","description":"bad \q escape FD-7K2Q9X"}'
    assert isinstance(C.youtube_descriptions(page), list)


def test_parse_feed_keeps_video_descriptions_without_changing_equality():
    data = (f'<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            f'xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom"><title>t</title>'
            f'<entry><yt:videoId>abcdefghijk</yt:videoId><title>v</title>'
            f'<media:group><media:description>code FD-7K2Q9X</media:description></media:group></entry>'
            f'</feed>').encode()
    (v,) = C.parse_feed(data).videos
    assert v.description == "code FD-7K2Q9X"
    assert v == C.Video("abcdefghijk", "v")
    assert C.code_in("FD-7K2Q9X", C.feed_descriptions(C.parse_feed(data)))


def test_twitch_bio():
    data = {"data": [{"id": "77", "login": "coolkid", "description": "streams FD-7K2Q9X"}]}
    assert C.twitch_bio(data, "77") == "streams FD-7K2Q9X"
    assert C.twitch_bio(data, "78") is None  # a different account answered
    assert C.twitch_bio({"data": [{"id": "77", "description": None}]}, "77") == ""
    assert C.twitch_bio({"data": []}, "77") is None and C.twitch_bio(None, "77") is None
    long = {"data": [{"id": "77", "description": "y" * (C.MAX_DESCRIPTION + 50)}]}
    assert len(C.twitch_bio(long, "77")) == C.MAX_DESCRIPTION
