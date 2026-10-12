"""Tests for logic.gamelink: input validation, the Steam profile XML, stored data and
the lines shown on a profile. Pure: no network. The XML shape was checked against
steamcommunity.com/id/NAME?xml=1 on 2026-10-11; the people in it are made up."""

import json

import pytest

import config
from logic import gamelink as G

SID = "76561198000000001"
SID2 = "76561198000000002"


def game_xml(name, appid, recent, record):
    return (f"<mostPlayedGame><gameName><![CDATA[{name}]]></gameName>"
            f"<gameLink><![CDATA[https://steamcommunity.com/app/{appid}]]></gameLink>"
            f"<gameIcon><![CDATA[https://shared.fastly.steamstatic.com/x/{appid}.jpg]]></gameIcon>"
            f"<hoursPlayed>{recent}</hoursPlayed><hoursOnRecord>{record}</hoursOnRecord>"
            f"<statsName><![CDATA[{appid}]]></statsName></mostPlayedGame>")


def profile_xml(steam_id=SID, persona="Cool *Kid*", privacy="public", summary="hello<br>there", games=(),
                extra=""):
    body = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><profile>\n'
            f"\t<steamID64>{steam_id}</steamID64>\n\t<steamID><![CDATA[{persona}]]></steamID>\n"
            f"\t<onlineState>offline</onlineState>\n\t<privacyState>{privacy}</privacyState>\n"
            f"\t<visibilityState>3</visibilityState>\n\t<vacBanned>0</vacBanned>{extra}\n")
    if privacy == "public":
        body += f"\t<customURL><![CDATA[coolkid]]></customURL>\n\t<summary><![CDATA[{summary}]]></summary>\n"
        if games:
            body += "<mostPlayedGames>" + "".join(game_xml(*g) for g in games) + "</mostPlayedGames>"
    return (body + "</profile>").encode()


NOT_FOUND = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><response><error>'
             b"<![CDATA[The specified profile could not be found.]]></error></response>")
GAMES = (("Counter-Strike 2", 730, "10.4", "1,234"), ("WARDOGS", 1867240, "3.4", "54"),
         ("Escape Simulator 2", 2879840, "0.5", "22"))


# ---------------------------------------------------------------- input
@pytest.mark.parametrize("text, expected", [
    (SID, ("id", SID)),
    (f"  {SID}  ", ("id", SID)),
    ("coolkid", ("vanity", "coolkid")),
    ("Cool_Kid-99", ("vanity", "Cool_Kid-99")),
    ("ab", ("vanity", "ab")),
    ("https://steamcommunity.com/id/coolkid", ("vanity", "coolkid")),
    ("https://steamcommunity.com/id/coolkid/", ("vanity", "coolkid")),
    ("https://steamcommunity.com/id/coolkid/games/?tab=all", ("vanity", "coolkid")),
    ("http://steamcommunity.com/id/coolkid", ("vanity", "coolkid")),
    ("steamcommunity.com/id/coolkid", ("vanity", "coolkid")),
    ("https://www.steamcommunity.com/id/coolkid", ("vanity", "coolkid")),
    ("<https://steamcommunity.com/id/coolkid>", ("vanity", "coolkid")),
    (f"https://steamcommunity.com/profiles/{SID}", ("id", SID)),
    (f"https://steamcommunity.com/profiles/{SID}/?xml=1", ("id", SID)),
    (f"https://steamcommunity.com/id/{SID}", ("vanity", SID)),  # a vanity may be all digits
    ("1234", ("vanity", "1234")),
])
def test_parse_steam_accepts(text, expected):
    ref = G.parse_steam(text)
    assert ref is not None and (ref.kind, ref.value) == expected


@pytest.mark.parametrize("text", [
    "", " ", "a", "x" * 33, "12345678901234567", "7656119800000000١","cool kid", "cool.kid", "cool\nkid",
    "kid!", "ünïcode", "https://evil.example/id/coolkid", "https://steamcommunity.com.evil.example/id/coolkid",
    "https://steamcommunity.com@evil.example/id/coolkid", "https://user@steamcommunity.com/id/coolkid",
    "https://steamcommunity.com:8443/id/coolkid", "ftp://steamcommunity.com/id/coolkid",
    "https://steamcommunity.com/", "https://steamcommunity.com/id/", "https://steamcommunity.com/app/730",
    "https://steamcommunity.com/groups/valve", "https://steamcommunity.com/profiles/coolkid",
    "https://steamcommunity.com/profiles/12345678901234567", "https://steamcommunity.com/id/a",
    "https://steamcommunity.com/id/co%6flkid", "https://store.steampowered.com/id/coolkid",
    "javascript:alert(1)", "https://[bad/id/x", "x" * 300, None, 5,
])
def test_parse_steam_refuses(text):
    assert G.parse_steam(text) is None


def test_urls_are_rebuilt_from_valid_ids_only():
    assert G.xml_url(G.Ref("id", SID)) == f"https://steamcommunity.com/profiles/{SID}?xml=1"
    assert G.xml_url(G.Ref("vanity", "coolkid")) == "https://steamcommunity.com/id/coolkid?xml=1"
    assert G.id_xml_url(SID) == f"https://steamcommunity.com/profiles/{SID}?xml=1"
    assert G.profile_url(SID) == f"https://steamcommunity.com/profiles/{SID}"
    for bad in (G.Ref("id", "123"), G.Ref("vanity", "a/b"), G.Ref("vanity", "x?y=1"), G.Ref("other", "coolkid")):
        with pytest.raises(ValueError):
            G.xml_url(bad)
    for bad in ("123", "7656119800000000x", None, f"{SID}/../x"):
        with pytest.raises(ValueError):
            G.profile_url(bad)


@pytest.mark.parametrize("location, expected", [
    ("https://steamcommunity.com/id/coolkid", "https://steamcommunity.com/id/coolkid?xml=1"),
    ("https://steamcommunity.com/id/coolkid/?xml=1", "https://steamcommunity.com/id/coolkid?xml=1"),
    (f"https://steamcommunity.com/profiles/{SID}?xml=1", f"https://steamcommunity.com/profiles/{SID}?xml=1"),
    ("/id/coolkid?xml=1", "https://steamcommunity.com/id/coolkid?xml=1"),
])
def test_redirect_target_follows_only_steam_profiles(location, expected):
    assert G.redirect_target(location) == expected


@pytest.mark.parametrize("location", [
    None, "", 5, "http://steamcommunity.com/id/coolkid", "https://evil.example/id/coolkid", "//evil.example/id/coolkid",
    "https://www.steamcommunity.com/id/coolkid", "https://steamcommunity.com:444/id/coolkid",
    "https://a@steamcommunity.com/id/coolkid", "https://steamcommunity.com/login/home/?goto=id%2Fcoolkid",
    "https://steamcommunity.com/id/coolkid/games", "https://steamcommunity.com/profiles/123",
    "https://steamcommunity.com/", "https://steamcommunity.com/id/cool kid", "https://x/" + "a" * 600,
    "https://[bad/id/x",
])
def test_redirect_target_refuses(location):
    assert G.redirect_target(location) is None


# ---------------------------------------------------------------- profile XML
def test_parse_public_profile():
    p = G.parse_profile(profile_xml(summary='Hi <b>all</b><br>code FD-7K2Q9X &amp; <a class="bb_link" '
                                            'href="https://x.example/">link</a>', games=GAMES))
    assert p.steam_id == SID and p.persona == "Cool *Kid*" and p.privacy == "public" and p.public
    assert p.summary == "Hi all code FD-7K2Q9X & link"
    assert G.code_in("FD-7K2Q9X", [p.summary])
    assert p.games == (G.Played("Counter-Strike 2", 730, 1234.0, 10.4), G.Played("WARDOGS", 1867240, 54.0, 3.4),
                       G.Played("Escape Simulator 2", 2879840, 22.0, 0.5))


def test_parse_private_profile_has_no_summary_or_games():
    for privacy in ("friendsonly", "private"):
        p = G.parse_profile(profile_xml(privacy=privacy))
        assert p.steam_id == SID and p.privacy == privacy and not p.public
        assert p.summary == "" and p.games == ()
    # a non-public profile that still carries a summary is not trusted with one
    body = profile_xml(privacy="private", extra="<summary>FD-7K2Q9X</summary>")
    assert G.parse_profile(body).summary == ""
    assert G.parse_profile(profile_xml(privacy="weird")).privacy == "unknown"


def test_parse_profile_tolerates_odd_games():
    xml = profile_xml(games=(("", 1, "1", "1"), ("Good", 5, "abc", "-3"), ("Odd link", "730/evil", "1.5", "2")))
    p = G.parse_profile(xml)
    assert p.games == (G.Played("Good", 5, 0.0, 0.0), G.Played("Odd link", None, 2.0, 1.5))
    many = profile_xml(games=tuple((f"Game {i}", i + 1, "1", "2") for i in range(20)))
    assert len(G.parse_profile(many).games) == G.MAX_GAMES
    long = G.parse_profile(profile_xml(persona="x" * 500, games=(("y" * 500, 5, "1", "2"),)))
    assert len(long.persona) == G.MAX_PERSONA and len(long.games[0].name) == G.MAX_GAME_NAME
    assert G.parse_profile(profile_xml(persona=" \t ")).persona == G.FALLBACK_PERSONA
    assert G.parse_profile(profile_xml(persona="a‮b\nc")).persona == "a b c"


def test_parse_profile_not_found_and_garbage():
    with pytest.raises(G.ProfileNotFound):
        G.parse_profile(NOT_FOUND)
    for body in (b"", b"<html>login</html>", b"<profile></profile>", b"<response/>", b"not xml", b"\xff\xfe<\x00p\x00",
                 "<profile><steamID64>７６５６１１９８０００００００００１</steamID64></profile>".encode(),
                 b"<profile><steamID64>12345678901234567</steamID64></profile>", b"\xc3\x28<profile/>",
                 b"<\x00p\x00r\x00o\x00", profile_xml() + b"<extra/>", "nope"):
        with pytest.raises(ValueError) as err:
            G.parse_profile(body)
        assert not isinstance(err.value, G.ProfileNotFound)
    assert G.parse_profile(b"\xef\xbb\xbf" + profile_xml()).steam_id == SID


def test_parse_profile_refuses_dtds_and_oversize():
    bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
            b"<profile><steamID64>" + SID.encode() + b"</steamID64><steamID>&lol2;</steamID></profile>")
    with pytest.raises(ValueError, match="DTD"):
        G.parse_profile(bomb)
    with pytest.raises(ValueError, match="DTD"):
        G.parse_profile(b'<!doctype x SYSTEM "file:///etc/passwd"><profile/>')
    with pytest.raises(ValueError, match="DTD"):
        G.parse_profile(b"<profile><!ENTITY x 'y'></profile>")
    with pytest.raises(ValueError, match="too large"):
        G.parse_profile(profile_xml(summary="x" * (G.MAX_BODY + 1)))
    assert G.MAX_BODY == 512 * 1024 and G.FETCH_TIMEOUT == 10


# ---------------------------------------------------------------- server games
def test_match_game_maps_steam_names_to_server_games():
    def role(name):
        return getattr(G.match_game(name), "role", None)

    assert role("Counter-Strike 2") == "Counter-Strike 2"
    assert role("Apex Legends") == "Apex Legends"
    assert role("Apex Legends™") == "Apex Legends"
    assert role("WARDOGS") == "Wardogs"
    assert role("Call of Duty®") == "Call of Duty"
    assert role("Call of Duty®: Black Ops III") == "Call of Duty"
    assert role("Grand Theft Auto V Legacy") == "GTA Online"
    assert role("Grand Theft Auto V Enhanced") == "GTA Online"
    for other in ("Counter-Strike", "Counter-Strike: Source", "Minecraft Dungeons", "Grand Theft Auto IV", "Apex",
                  "Escape Simulator 2", "", "™"):
        assert role(other) is None, other
    for target in G.PREFIXES.values():
        assert any(g.role == target for g in config.GAMES)


def test_server_hours_sums_apps_of_one_game():
    games = (G.Played("Grand Theft Auto V Legacy", 271590, 100.0, 1.0),
             G.Played("Grand Theft Auto V Enhanced", 3240220, 20.5, 2.0),
             G.Played("Counter-Strike 2", 730, 1234.0, 3.0), G.Played("Apex Legends", 1172470, 0.0, 0.0),
             G.Played("Other", 5, 9.0, 9.0))
    assert G.server_hours(games) == {"gtaonline": 120.5, "counterstrike2": 1234.0}


# ---------------------------------------------------------------- stored data
def test_fresh_data_round_trips_and_keeps_known_hours():
    first = G.parse_profile(profile_xml(games=GAMES))
    raw = G.fresh_data(first)
    data = G.load_data(raw)
    assert data["persona"] == "Cool *Kid*" and data["privacy"] == "public"
    assert data["hours"] == {"counterstrike2": 1234.0, "wardogs": 54.0}
    assert [g["name"] for g in data["recent"]] == ["Counter-Strike 2", "WARDOGS", "Escape Simulator 2"]
    assert data["recent"][0] == {"name": "Counter-Strike 2", "appid": 730, "hours": 1234.0, "recent": 10.4}
    assert data["fails"] == 0 and data["tried_at"] == 0
    # two weeks later only Apex is listed: the old hours stay, CS2 is not lost
    later = G.parse_profile(profile_xml(persona="New Name", games=(("Apex Legends", 1172470, "5", "60"),)))
    data = G.load_data(G.fresh_data(later, raw))
    assert data["persona"] == "New Name"
    assert data["hours"] == {"counterstrike2": 1234.0, "wardogs": 54.0, "apexlegends": 60.0}
    assert [g["name"] for g in data["recent"]] == ["Apex Legends"]
    # a newer number replaces the old one; without `old` nothing carries over
    newer = G.parse_profile(profile_xml(games=(("Counter-Strike 2", 730, "1", "1,300"),)))
    assert G.load_data(G.fresh_data(newer, raw))["hours"]["counterstrike2"] == 1300.0
    assert G.load_data(G.fresh_data(later))["hours"] == {"apexlegends": 60.0}


def test_load_data_survives_junk():
    empty = {"persona": G.FALLBACK_PERSONA, "privacy": "unknown", "recent": [], "hours": {}, "fails": 0,
             "tried_at": 0}
    for raw in (None, "", "{", "[]", "5", '"x"', "{}", 7):
        assert G.load_data(raw) == empty
    raw = json.dumps({"persona": 5, "privacy": "evil", "recent": ["x", {"name": ""}, {"name": "ok", "appid": "7",
                      "hours": "lots", "recent": -1}, {"name": "b", "appid": True, "hours": 2, "recent": 1e99}],
                      "hours": {"counterstrike2": 5, "notagame": 9, "wardogs": "x", "apexlegends": True},
                      "fails": "3", "tried_at": -5})
    assert G.load_data(raw) == {**empty, "recent": [{"name": "ok", "appid": None, "hours": 0.0, "recent": 0.0},
                                                    {"name": "b", "appid": None, "hours": 2.0, "recent": 0.0}],
                                "hours": {"counterstrike2": 5.0}}


def test_refresh_is_daily_and_stops_after_three_failures():
    t = 1_790_000_000
    raw = G.fresh_data(G.parse_profile(profile_xml(games=GAMES)))
    assert not G.due(raw, t, t) and not G.due(raw, t, t + G.DAY - 1)
    assert G.due(raw, t, t + G.DAY)
    one = G.failed_data(raw, t + G.DAY)
    assert G.load_data(one)["fails"] == 1 and G.load_data(one)["hours"] == G.load_data(raw)["hours"]
    assert not G.due(one, t, t + G.DAY + 3600)  # a failed try also waits a day
    assert G.due(one, t, t + 2 * G.DAY)
    two = G.failed_data(one, t + 2 * G.DAY)
    three = G.failed_data(two, t + 3 * G.DAY)
    assert not G.gave_up(two) and G.gave_up(three)
    assert not G.due(three, t, t + 400 * G.DAY)
    again = G.reset_data(three)
    assert not G.gave_up(again) and G.due(again, t, t + 4 * G.DAY)
    assert G.load_data(again)["hours"] == G.load_data(raw)["hours"]
    assert G.MAX_FAILS == 3 and G.REFRESH_EVERY == G.DAY


def test_claim_codes_match_creators_rules():
    code = G.new_code()
    assert code.startswith("FD-") and len(code) == 9
    assert G.code_in(code, [f"my page {code.lower()}!"]) and not G.code_in(code, [f"x{code}", None, ""])
    assert G.expired(0, G.CODE_TTL) and not G.expired(0, G.CODE_TTL - 1) and G.CODE_TTL == 24 * 3600
    assert G.cooldown_left(100, 130) == G.VERIFY_COOLDOWN - 30 and G.cooldown_left(None, 5) == 0


# ---------------------------------------------------------------- display
def test_escape_makes_markdown_and_mentions_inert():
    out = G.escape("[x](https://evil.example) **b** @everyone <@1> `c` _i_ #chan ||s||")
    for live in ("[x](", "**b**", "@everyone", "<@1>", "`c`", " _i_", " #chan", "||s||", "https://"):
        assert live not in out
    assert G.escape("plain name 42") == "plain name 42"


def test_safe_persona_filters():
    assert G.safe_persona("Cool Kid") == "Cool Kid"
    assert G.safe_persona("join discord.gg/abcdef") == G.FALLBACK_PERSONA
    assert G.safe_persona("free-skins.com") == G.FALLBACK_PERSONA
    assert G.safe_persona("") == G.FALLBACK_PERSONA and G.safe_persona(None) == G.FALLBACK_PERSONA
    assert len(G.safe_persona("y" * 500)) == G.MAX_PERSONA


def test_fmt_hours():
    assert G.fmt_hours(0.5) == "0.5 h" and G.fmt_hours(9.94) == "9.9 h"
    assert G.fmt_hours(54) == "54 h" and G.fmt_hours(1234.4) == "1,234 h"


def test_account_lines():
    raw = G.fresh_data(G.parse_profile(profile_xml(games=GAMES)))
    lines = G.account_lines("steam", SID, raw)
    cs2, wd = config.game_by_key("counterstrike2"), config.game_by_key("wardogs")
    assert lines == [
        f"**Steam** · Cool \\*Kid\\* · <https://steamcommunity.com/profiles/{SID}>",
        f"Here: {wd.emoji} Wardogs 54 h · {cs2.emoji} Counter-Strike 2 1,234 h",
        "Lately: Counter\\-Strike 2 10 h · WARDOGS 3.4 h · Escape Simulator 2 0.5 h",
    ]
    assert G.account_lines("steam", SID, raw, esc=str.upper)[0].startswith("**Steam** · COOL *KID* · <https://")
    private = G.account_lines("steam", SID, G.fresh_data(G.parse_profile(profile_xml(privacy="private"))))
    assert private == [f"**Steam** · Cool \\*Kid\\* · <https://steamcommunity.com/profiles/{SID}>",
                       "Profile is private on Steam, so no hours."]
    public_quiet = G.account_lines("steam", SID, G.fresh_data(G.parse_profile(profile_xml())))
    assert len(public_quiet) == 1
    # hours remembered from before still show when the profile went private later
    kept = G.fresh_data(G.parse_profile(profile_xml(privacy="private")), raw)
    assert G.account_lines("steam", SID, kept)[1].startswith("Here: ")


def test_account_lines_never_trust_stored_text():
    evil = json.dumps({"persona": "[click](https://evil.example) @everyone", "privacy": "public",
                       "recent": [{"name": "**Free** [nitro](https://evil.example)", "appid": 1, "hours": 5,
                                   "recent": 2}], "hours": {}})
    lines = G.account_lines("steam", SID, evil)
    text = "\n".join(lines)
    assert "evil.example" not in lines[0] and G.FALLBACK_PERSONA in lines[0]  # the filter replaced the persona
    assert "[nitro](" not in text and "**Free**" not in text and "@everyone" not in text
    assert G.account_lines("steam", "123", evil) == []
    assert G.account_lines("steam", f"{SID}>\n@everyone", evil) == []
    assert G.account_lines("xbox", SID, evil) == []
    assert G.account_lines("steam", SID, "not json")[0].startswith(f"**Steam** · {G.FALLBACK_PERSONA} · <")


def test_pending_line():
    assert "FD-7K2Q9X" in G.pending_line("steam", "FD-7K2Q9X") and "/link verify" in G.pending_line("steam", "X")
