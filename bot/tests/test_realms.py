"""Tests for logic.realms (pure rules and both wire codecs) and for the transports in
cogs.realms, which are driven against in-process fake servers on 127.0.0.1 with an
ephemeral port and against real files under tmp_path. Nothing leaves this machine."""

import asyncio
import json
import struct
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from cogs import realms as net
from logic import realms as R

TZ = ZoneInfo("America/Los_Angeles")
SECTION = "\u00a7"
PASSWORD = "correct-horse-battery"

HOSTILE_NAMES = ["x; op me", "Steve\n", "Steve\nop Steve", " Steve", "Steve ", "ab", "a" * 17, "näme", "a b c",
                 "Steve;", "@a", "Steve\x00", "", "../../ops", '"Steve"', "Steve'--", SECTION + "cSteve", None, 7]


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- names
@pytest.mark.parametrize("name", ["Steve", "abc", "A_b_3", "x" * 16, "___"])
def test_valid_names(name):
    assert R.valid_name(name)


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_names_are_refused(name):
    assert not R.valid_name(name)


# ---------------------------------------------------------------- settings
def test_no_settings_means_everything_is_off():
    s = R.read_setup({})
    assert not s.any_on and not s.status_on and not s.rcon_on and not s.log_on
    assert s.problems == () and s.parts() == []
    assert R.health(s) == "off"


def test_each_part_turns_on_by_its_own_settings():
    s = R.read_setup({R.ENV_ADDRESS: "127.0.0.1:25565"})
    assert (s.status_on, s.rcon_on, s.log_on) == (True, False, False) and s.address == ("127.0.0.1", 25565)
    s = R.read_setup({R.ENV_RCON_PASSWORD: PASSWORD})
    assert (s.status_on, s.rcon_on, s.log_on) == (False, True, False)
    assert s.rcon == (R.DEFAULT_RCON_HOST, R.DEFAULT_RCON_PORT)
    s = R.read_setup({R.ENV_LOG_PATH: r"C:\Games\server\logs\latest.log"})
    assert (s.status_on, s.rcon_on, s.log_on) == (False, False, True)
    assert R.read_setup({R.ENV_LOG_PATH: "/srv/mc/logs/latest.log"}).log_on
    assert s.parts() == ["chat from Minecraft"]


@pytest.mark.parametrize("raw,want", [
    ("mc.example.net", ("mc.example.net", 25565)), ("localhost:25566", ("localhost", 25566)),
    ("[::1]:25565", ("::1", 25565)), ("[::1]", ("::1", 25565)), (" 10.0.0.5:1 ", ("10.0.0.5", 1)),
])
def test_address_forms(raw, want):
    assert R.parse_address(raw) == want


@pytest.mark.parametrize("raw", ["", "host:", "host:0", "host:70000", "host:port", "http://host", "host/path",
                                 "user@host", "ho st", "[::1", "[::1]25565", "[::1]:", "-host", "host:1:2",
                                 "host:\u0662\u0665"])
def test_bad_addresses(raw):
    assert R.parse_address(raw) is None


def test_a_bad_address_only_turns_the_panel_off():
    s = R.read_setup({R.ENV_ADDRESS: "http://nope", R.ENV_RCON_PASSWORD: PASSWORD})
    assert not s.status_on and s.rcon_on
    assert len(s.problems) == 1 and R.ENV_ADDRESS in s.problems[0] and "nope" not in s.problems[0]


@pytest.mark.parametrize("host", ["8.8.8.8", "mc.example.net", "0.0.0.0", "2001:4860:4860::8888", "169.254.1.1"])
def test_rcon_host_must_be_local(host):
    s = R.read_setup({R.ENV_RCON_HOST: host, R.ENV_RCON_PASSWORD: PASSWORD})
    assert not s.rcon_on and s.rcon is None and s.password == ""
    assert len(s.problems) == 1 and R.ENV_RCON_HOST in s.problems[0]
    assert host not in s.problems[0] and PASSWORD not in s.problems[0]


@pytest.mark.parametrize("host,want", [("localhost", "localhost"), ("127.0.0.1", "127.0.0.1"), ("[::1]", "::1"),
                                       ("192.168.1.20", "192.168.1.20"), ("10.1.2.3", "10.1.2.3")])
def test_rcon_host_local_addresses_are_accepted(host, want):
    s = R.read_setup({R.ENV_RCON_HOST: host, R.ENV_RCON_PORT: "25580", R.ENV_RCON_PASSWORD: PASSWORD})
    assert s.rcon_on and s.rcon == (want, 25580) and s.problems == ()


def test_rcon_needs_a_password_and_a_real_port():
    s = R.read_setup({R.ENV_RCON_HOST: "127.0.0.1"})
    assert not s.rcon_on and R.ENV_RCON_PASSWORD in s.problems[0]
    s = R.read_setup({R.ENV_RCON_PORT: "ninety", R.ENV_RCON_PASSWORD: PASSWORD})
    assert not s.rcon_on and R.ENV_RCON_PORT in s.problems[0]
    s = R.read_setup({R.ENV_RCON_PASSWORD: "x" * 2000})
    assert not s.rcon_on and R.ENV_RCON_PASSWORD in s.problems[0]


def test_the_password_is_never_in_a_repr_or_a_problem():
    s = R.read_setup({R.ENV_ADDRESS: "bad address", R.ENV_RCON_HOST: "127.0.0.1", R.ENV_RCON_PASSWORD: PASSWORD,
                      R.ENV_LOG_PATH: "relative.log"})
    assert s.rcon_on and s.password == PASSWORD
    for text in (repr(s), str(s), " ".join(s.problems), R.health(s), repr(net.Rcon("127.0.0.1", 1, PASSWORD))):
        assert PASSWORD not in text


@pytest.mark.parametrize("path", ["latest.log", "logs/latest.log", "..\\latest.log", "C:latest.log", "x" * 2000])
def test_log_path_must_be_absolute(path):
    s = R.read_setup({R.ENV_LOG_PATH: path})
    assert not s.log_on and R.ENV_LOG_PATH in s.problems[0]


# ---------------------------------------------------------------- cleaning
def test_clean_plain_strips_codes_controls_and_caps():
    assert R.clean_plain(f"{SECTION}aGreen {SECTION}lbold{SECTION}r  text\n\twith\r\nlines\x00\x1b[31m", 100) == \
        "Green bold text with lines [31m"
    assert R.clean_plain("a\u200bb\u202ec", 10) == "a b c"  # zero-width and bidi overrides become spaces
    assert R.clean_plain(SECTION, 10) == "" and R.clean_plain(None, 10) == "" and R.clean_plain(5, 10) == ""
    long = R.clean_plain("x" * 10_000, 50)
    assert len(long) == 50 and long.endswith("…")


def test_for_discord_escapes_markdown_and_mentions():
    out = R.for_discord("**bold** _it_ `code` ||spoiler|| @everyone @here <@123456789012345678> [x](http://a)")
    assert "@everyone" not in out and "@here" not in out and "<@123456789012345678>" not in out
    assert "\\*\\*bold\\*\\*" in out and "\\_it\\_" in out and "\\`code\\`" in out and "\\|\\|" in out


# ---------------------------------------------------------------- Server List Ping codec
@pytest.mark.parametrize("n,raw", [(0, b"\x00"), (1, b"\x01"), (127, b"\x7f"), (128, b"\x80\x01"),
                                   (255, b"\xff\x01"), (25565, b"\xdd\xc7\x01"), (2097151, b"\xff\xff\x7f"),
                                   (2147483647, b"\xff\xff\xff\xff\x07"), (-1, b"\xff\xff\xff\xff\x0f")])
def test_varint_bytes(n, raw):
    assert R.pack_varint(n) == raw
    assert R.read_varint(raw) == (n & 0xFFFFFFFF, len(raw))
    assert R.read_varint(b"zz" + raw + b"tail", 2) == (n & 0xFFFFFFFF, 2 + len(raw))


def test_varint_errors():
    with pytest.raises(R.Incomplete):
        R.read_varint(b"")
    with pytest.raises(R.Incomplete):
        R.read_varint(b"\x80\x80")
    with pytest.raises(ValueError) as too_long:
        R.read_varint(b"\x80\x80\x80\x80\x80\x01")
    assert not isinstance(too_long.value, R.Incomplete)


def test_handshake_bytes():
    assert R.handshake("localhost", 25565) == (
        b"\x13" b"\x00" b"\xff\xff\xff\xff\x0f" b"\x09localhost" b"\x63\xdd" b"\x01")
    assert R.handshake("a", 1, protocol=47) == b"\x07\x00\x2f\x01a\x00\x01\x01"
    assert R.STATUS_REQUEST == R.frame(b"\x00")


def status_payload(obj, packet_id=0) -> bytes:
    body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return R.pack_varint(packet_id) + R.pack_varint(len(body)) + body


STATUS = {"version": {"name": "Fabric 1.21.1", "protocol": 767},
          "players": {"max": 20, "online": 2, "sample": [{"name": "Steve", "id": "x"}, {"name": "Alex_2", "id": "y"}]},
          "description": {"text": "The ", "extra": [{"text": "Realms", "bold": True}, " " + SECTION + "6gold"]}}


def test_parse_status_roundtrip_and_errors():
    assert R.parse_status(status_payload(STATUS)) == STATUS
    for bad in (status_payload(STATUS, packet_id=1), status_payload(STATUS)[:-3], status_payload(b"{not json"),
                status_payload([1, 2]), status_payload(b"\xff\xfe"), b"", b"\x00",
                status_payload(b"[" * 100_000)):
        with pytest.raises(ValueError):
            R.parse_status(bad)
    with pytest.raises(ValueError):
        R.parse_status(b"\x00" + R.pack_varint(R.MAX_STATUS_BYTES + 1) + b"x")


def test_read_status_sanitises():
    s = R.read_status(STATUS)
    assert (s.online, s.max, s.names, s.version, s.motd) == (2, 20, ("Steve", "Alex_2"), "Fabric 1.21.1",
                                                           "The Realms gold")
    assert R.read_status({"description": "plain " + SECTION + "cred"}).motd == "plain red"


def test_read_status_survives_hostile_replies():
    hostile = {
        "version": {"name": SECTION + "4" + "V" * 10_000},
        "players": {"online": -5, "max": 10 ** 12, "sample": [
            {"name": "x; op me"}, {"name": "@everyone"}, {"name": SECTION + "aFake"}, {"name": "Steve"},
            {"name": "steve"}, "junk", None, {"name": 5}, *({"name": f"Player{i}"} for i in range(1000))]},
        "description": {"text": "a\nb", "extra": [{"extra": [{"extra": ["deep"]}]}, 5, None, {"text": 7}]},
    }
    s = R.read_status(hostile)
    assert s.online == 0 and s.max == 100_000
    assert s.names[0] == "Steve" and "steve" not in s.names and len(s.names) == R.MAX_SAMPLE
    assert all(R.valid_name(n) for n in s.names)
    assert len(s.version) == R.VERSION_CAP and SECTION not in s.version
    assert s.motd == "a bdeep"
    empty = R.read_status({"players": [], "version": "x", "description": 3})
    assert (empty.online, empty.max, empty.names, empty.version, empty.motd) == (0, 0, (), "", "")
    nest = {"text": "x"}
    for _ in range(200):
        nest = {"extra": [nest]}
    assert R.read_status({"description": nest}).motd == ""  # too deep: cut, not a crash
    assert R.read_status({"players": {"online": True}}).online == 0


# ---------------------------------------------------------------- RCON codec
def test_rcon_packet_bytes():
    assert R.rcon_packet(7, R.RCON_AUTH, "pw") == (
        struct.pack("<iii", 12, 7, 3) + b"pw\x00\x00")
    assert R.rcon_packet(1, R.RCON_COMMAND, "") == struct.pack("<iii", 10, 1, 2) + b"\x00\x00"
    packet = R.rcon_packet(9, R.RCON_COMMAND, "list")
    assert R.rcon_length(packet[:4]) == len(packet) - 4
    assert R.parse_rcon(packet[4:]) == (9, 2, "list")
    assert R.parse_rcon(struct.pack("<ii", -1, 2) + b"\x00\x00") == (-1, 2, "")


def test_rcon_packet_refuses_nul_and_long_bodies():
    with pytest.raises(ValueError):
        R.rcon_packet(1, R.RCON_COMMAND, "list\x00op x")
    with pytest.raises(ValueError):
        R.rcon_packet(1, R.RCON_COMMAND, "x" * (R.MAX_COMMAND_BYTES + 1))
    R.rcon_packet(1, R.RCON_COMMAND, "x" * R.MAX_COMMAND_BYTES)


@pytest.mark.parametrize("length", [-1, 0, 9, R.RCON_MAX_LENGTH + 1, 2 ** 31 - 1])
def test_rcon_length_caps(length):
    with pytest.raises(ValueError):
        R.rcon_length(struct.pack("<i", length))


def test_rcon_length_and_parse_reject_malformed():
    assert R.rcon_length(struct.pack("<i", 10)) == 10 and R.rcon_length(struct.pack("<i", 4106)) == 4106
    for bad in (b"", b"\x01\x02\x03"):
        with pytest.raises(ValueError):
            R.rcon_length(bad)
    for bad in (b"", b"short", struct.pack("<ii", 1, 0) + b"no terminator"):
        with pytest.raises(ValueError):
            R.parse_rcon(bad)


# ---------------------------------------------------------------- commands
def test_whitelist_command_only_takes_valid_names():
    assert R.whitelist_command("add", "Steve_1") == "whitelist add Steve_1"
    assert R.whitelist_command("remove", "Steve_1") == "whitelist remove Steve_1"
    for name in HOSTILE_NAMES:
        with pytest.raises(ValueError):
            R.whitelist_command("add", name)
    with pytest.raises(ValueError):
        R.whitelist_command("reload", "Steve")


def test_whitelist_result():
    assert R.whitelist_result("add", "Added Steve to the whitelist") == "ok"
    # not "ok": the name was there before this add, so somebody else put it on the whitelist
    assert R.whitelist_result("add", "Player is already whitelisted") == "already"
    assert R.whitelist_result("remove", "Player is already whitelisted") == "error"
    assert R.whitelist_result("add", "That player does not exist") == "unknown"
    assert R.whitelist_result("add", "") == "error"
    assert R.whitelist_result("add", "Removed Steve from the whitelist") == "error"
    assert R.whitelist_result("remove", "Removed Steve from the whitelist") == "ok"
    assert R.whitelist_result("remove", "Player is not whitelisted") == "ok"
    assert R.whitelist_result("remove", "That player does not exist") == "unknown"
    assert R.whitelist_result("remove", "Unknown or incomplete command") == "error"


def test_relay_gate_timings():
    # an API "not a member" may be reused for five minutes, a fetched member for one at most
    assert R.MEMBER_GONE_FOR == 5 * 60 and R.MEMBER_FRESH_FOR == 60 and R.HINT_EVERY == 6 * 3600
    assert not hasattr(R, "removed_key") and not hasattr(R, "REMOVED_KEEP")


def test_linking_needs_two_weeks_in_the_server():
    day = 86400
    assert R.LINK_MEMBER_DAYS == 14
    assert not R.member_long_enough(1000, 1000 + 14 * day - 1)
    assert R.member_long_enough(1000, 1000 + 14 * day)
    assert not R.member_long_enough(None, 10 ** 10)  # join time unknown: not taken on trust
    assert not R.member_long_enough(2000, 1000)  # a clock that makes no sense


def test_parse_list():
    assert R.parse_list("There are 2 of a max of 20 players online: Steve, Alex_2") == ["Steve", "Alex_2"]
    assert R.parse_list("There are 0 of a max of 20 players online: ") == []
    assert R.parse_list("There are 0 of a max 20 players online:") == []
    assert R.parse_list(f"{SECTION}6There are 3 of a max of 20 players online: Steve, x; op me, @everyone, steve") == \
        ["Steve"]
    assert R.parse_list("Unknown command") is None and R.parse_list("") is None and R.parse_list(None) is None


def tellraw_parts(command: str):
    prefix = "tellraw @a "
    assert command.startswith(prefix)
    return json.loads(command[len(prefix):])


HOSTILE_TEXTS = [
    'he said "hi" and \\ left', "line one\nline two\r\n/op me", '"}]\n/op me', "\\\"}],{\"text\":\"x\"",
    SECTION + "4" + SECTION + "lRED", "@everyone @a @e[type=player]", "x" * 10_000, "😀" * 5000, "\x00\x1b\x7f",
    '{"text":"x","clickEvent":{"action":"run_command","value":"/op me"}}', "'; DROP TABLE wallets; --",
]


@pytest.mark.parametrize("text", HOSTILE_TEXTS, ids=[f"text{i}" for i in range(len(HOSTILE_TEXTS))])
def test_tellraw_keeps_hostile_text_inside_one_json_string(text):
    command = R.tellraw_command('Na"me\n' + SECTION + "c", text)
    assert len(command.encode("utf-8")) <= R.MAX_COMMAND_BYTES
    assert command.isascii() and command.isprintable() and "\n" not in command and SECTION not in command
    parts = tellraw_parts(command)
    assert len(parts) == 3 and parts[0] == "" and parts[1] == {"text": "[Discord] ", "color": "aqua"}
    assert set(parts[2]) == {"text"} and isinstance(parts[2]["text"], str)  # no click or hover events
    line = parts[2]["text"]
    assert line.startswith('<Na"me> ') and "\n" not in line and SECTION not in line
    assert len(line) <= R.CHAT_CAP + R.SENDER_CAP + 3
    R.rcon_packet(1, R.RCON_COMMAND, command)  # sendable as one packet


def test_tellraw_plain_message_and_defaults():
    assert tellraw_parts(R.tellraw_command("Lori", "gg wp"))[2] == {"text": "<Lori> gg wp"}
    assert tellraw_parts(R.tellraw_command("", "hi"))[2] == {"text": "<someone> hi"}
    assert R.discord_text("  hi\nthere  ") == "hi there"
    assert R.discord_text("", attachments=1) == "[attachment]" and R.discord_text(None) == ""
    assert len(R.discord_text("y" * 10_000)) == R.CHAT_CAP


# ---------------------------------------------------------------- log parser
P = "[12:34:56] [Server thread/INFO]: "


@pytest.mark.parametrize("line,want", [
    (P + "<Steve> hello there", R.Event(R.CHAT, "Steve", "hello there")),
    (P + "[Not Secure] <Alex_2> hi", R.Event(R.CHAT, "Alex_2", "hi")),
    (P + "<Steve> ", R.Event(R.CHAT, "Steve", "")),
    (P + "Steve joined the game", R.Event(R.JOIN, "Steve")),
    (P + "Steve left the game\r", R.Event(R.LEAVE, "Steve")),
    (P + "Steve was slain by Zombie", R.Event(R.DEATH, "Steve", "was slain by Zombie")),
    (P + "Steve fell from a high place", R.Event(R.DEATH, "Steve", "fell from a high place")),
    (P + "Steve drowned", R.Event(R.DEATH, "Steve", "drowned")),
    (P + "Steve tried to swim in lava to escape Blaze", R.Event(R.DEATH, "Steve", "tried to swim in lava to escape Blaze")),
    (P + "Steve was shot by Skeleton using [Bow of Doom]",
     R.Event(R.DEATH, "Steve", "was shot by Skeleton using [Bow of Doom]")),
    (P + "Steve has made the advancement [Stone Age]",
     R.Event(R.ADVANCEMENT, "Steve", "Stone Age", "made the advancement")),
    (P + "Steve has completed the challenge [How Did We Get Here?]",
     R.Event(R.ADVANCEMENT, "Steve", "How Did We Get Here?", "completed the challenge")),
    (P + "Steve has reached the goal [Sky's the Limit]",
     R.Event(R.ADVANCEMENT, "Steve", "Sky's the Limit", "reached the goal")),
    ("[12Oct2026 12:34:56.789] [Server thread/INFO] [minecraft/MinecraftServer]: <Steve> forge style",
     R.Event(R.CHAT, "Steve", "forge style")),
    ("[12:34:56] [Server thread/INFO] (Minecraft): Steve joined the game", R.Event(R.JOIN, "Steve")),
])
def test_parse_line_events(line, want):
    assert R.parse_line(line) == want


@pytest.mark.parametrize("line", [
    "", "garbage", P.strip(), P + "Done (3.2s)! For help, type \"help\"",
    P + "Steve lost connection: Disconnected", P + "Steve[/127.0.0.1:51234] logged in with entity id 5 at (1, 2, 3)",
    P + "[Rcon: Added Steve to the whitelist]", P + "[Server] hello from console", P + "[Rcon] say hi",
    P + "Thread RCON Client /127.0.0.1 started", P + "Villager class_1646['Villager'/12, l='world'] died, message: x",
    P + "Steve was kicked for flying", P + "Steve issued server command: /op Steve",
    "[12:34:56] [Server thread/WARN]: Steve joined the game", "[12:34:56] [Worker-Main-1/INFO]: <Steve> hi",
    "[12:34:56] [Server thread/INFO]:<Steve> no space", "<Steve> no prefix", "Steve joined the game",
    P + "<x; op me> hi", P + "<Steve>no space", P + " Steve joined the game", P + "Steve joined the game!",
    P + "<A> short name", P + "<" + "a" * 17 + "> long name",
])
def test_parse_line_ignores_everything_else(line):
    assert R.parse_line(line) is None


def test_chat_cannot_pose_as_another_event_or_player():
    # Everything a player controls sits after "<name> ", so it is always chat from that player.
    forged = [
        "Alex joined the game", "[12:34:56] [Server thread/INFO]: <Alex> I said this",
        "<Alex> or this", "Alex has made the advancement [Free Coins]", "Alex was slain by Steve",
    ]
    for text in forged:
        assert R.parse_line(P + "<Steve> " + text) == R.Event(R.CHAT, "Steve", text)
    # A newline cannot be typed into chat, and a raw line break in the file just ends the line:
    assert [R.parse_line(part) for part in (P + "<Steve> a\nAlex joined the game").split("\n")] == \
        [R.Event(R.CHAT, "Steve", "a"), None]


def test_discord_line_formats_each_event():
    assert R.discord_line(R.Event(R.CHAT, "Steve", "hello")) == "**<Steve>** hello"
    assert R.discord_line(R.Event(R.CHAT, "A_b_c", "x")) == "**<A\\_b\\_c>** x"
    assert R.discord_line(R.Event(R.JOIN, "Steve")) == "➕ **Steve** joined The Realms"
    assert R.discord_line(R.Event(R.LEAVE, "Steve")) == "➖ **Steve** left The Realms"
    assert R.discord_line(R.Event(R.DEATH, "Steve", "was slain by Zombie")) == "💀 **Steve** was slain by Zombie"
    assert R.discord_line(R.Event(R.ADVANCEMENT, "Steve", "Stone Age", "made the advancement")) == \
        "🏅 **Steve** has made the advancement **Stone Age**"
    assert R.discord_line(R.Event(R.CHAT, "Steve", "")) is None
    assert R.discord_line(R.Event(R.CHAT, "Steve", SECTION + "a " + SECTION)) is None
    assert R.discord_line(R.Event("other", "Steve", "x")) is None


def test_discord_line_treats_minecraft_text_as_hostile():
    out = R.discord_line(R.Event(R.CHAT, "Steve", f"{SECTION}c@everyone {SECTION}l**free** <@&123456789012345678> "
                                                  "@here `x` ||y|| > quote\ttab"))
    assert out.startswith("**<Steve>** ")
    assert "@everyone" not in out and "@here" not in out and "<@&123456789012345678>" not in out
    assert SECTION not in out and "\t" not in out and "\\*\\*free\\*\\*" in out
    long = R.discord_line(R.Event(R.CHAT, "Steve", "z" * 10_000))
    assert len(long) <= R.MC_TEXT_CAP + 20
    # invites, links of any kind and blocked words are dropped whole, never posted
    for text in ("join discord.gg/abcdef now", "free nitro at http://evil.example", "[click](https://evil.example)",
                 "visit evil.com today", "discord (dot) gg / abcdef"):
        assert R.discord_line(R.Event(R.CHAT, "Steve", text)) is None, text
    assert R.discord_line(R.Event(R.DEATH, "Steve", "was slain by Zombie using [discord.gg/abcdef]")) is None
    assert R.discord_line(R.Event(R.ADVANCEMENT, "Steve", "http://evil.example", "made the advancement")) is None


def test_relayed_lines_never_come_back():
    assert R.discord_line(R.Event(R.CHAT, "Steve", "[Discord] <Lori> hi")) is None
    assert R.discord_line(R.Event(R.CHAT, "Steve", f"{SECTION}b[DISCORD]{SECTION}r <Lori> hi")) is None
    assert R.BRIDGE_TAG in tellraw_parts(R.tellraw_command("Lori", "hi"))[1]["text"]


# ---------------------------------------------------------------- line splitter and tail position
def test_line_splitter_handles_partial_lines_and_crlf():
    s = R.LineSplitter()
    assert s.feed(b"one\r\ntwo\nthr") == ["one", "two"]
    assert s.feed(b"ee") == []
    assert s.feed(b"\n") == ["three"]
    assert s.feed(b"") == [] and s.feed(b"\n\n") == ["", ""]
    assert s.feed(b"caf\xc3\xa9 \xff\n") == ["café \ufffd"]  # bad bytes are replaced, never an error
    s.feed(b"half")
    s.reset()
    assert s.feed(b" line\n") == [" line"]


def test_line_splitter_drops_oversized_lines_whole():
    s = R.LineSplitter(max_line=10)
    assert s.feed(b"ok\n" + b"x" * 11 + b"\nfine\n") == ["ok", "fine"]
    assert s.feed(b"y" * 8) == [] and s.feed(b"y" * 8) == []  # 16 bytes and no newline yet
    assert s.carry == b"" and s.skipping
    assert s.feed(b"y" * 50) == [] and s.carry == b""  # still the same line: memory stays bounded
    assert s.feed(b"tail of it\nnext\n") == ["next"]
    assert not s.skipping


def test_tail_start_rules():
    fresh = R.TailState()
    assert R.tail_start(fresh, 5, 1000) == (1000, True)  # first sight: never replay history
    at = R.TailState(file_id=5, pos=1000)
    assert R.tail_start(at, 5, 1000) == (1000, False)
    assert R.tail_start(at, 5, 1500) == (1000, False)
    assert R.tail_start(at, 5, 400) == (0, True)  # truncated
    assert R.tail_start(at, 6, 1500) == (0, True)  # rotated: a new file with the same name
    assert R.tail_start(at, 6, 0) == (0, True)
    assert R.tail_start(at, 5, 1000 + R.MAX_BEHIND + 1) == (1000 + R.MAX_BEHIND + 1, True)  # flood: skip ahead
    assert R.tail_start(at, 6, R.MAX_BEHIND + 1) == (R.MAX_BEHIND + 1, True)


# ---------------------------------------------------------------- batching and rate limits
def test_batcher_joins_lines_and_caps_a_message():
    b = R.Batcher()
    assert b.take() is None
    for i in range(20):
        b.add(f"line {i}")
    first = b.take().split("\n")
    assert first == [f"line {i}" for i in range(R.MAX_BATCH_LINES)]
    assert b.take().split("\n") == [f"line {i}" for i in range(15, 20)] and b.take() is None
    for _ in range(10):
        b.add("y" * 400)
    out = b.take()
    assert len(out) <= R.MAX_BATCH_CHARS and out.count("\n") == 3
    b = R.Batcher()
    b.add("z" * 10_000)
    assert len(b.take()) == R.MAX_BATCH_CHARS // 2


def test_batcher_flood_is_bounded_and_says_what_it_skipped():
    b = R.Batcher()
    for i in range(10_000):
        b.add(f"spam {i}")
    assert len(b.lines) == R.MAX_BACKLOG and b.dropped == 10_000 - R.MAX_BACKLOG
    messages = []
    while (m := b.take()) is not None:
        messages.append(m)
        assert len(m) <= R.MAX_BATCH_CHARS and m.count("\n") < R.MAX_BATCH_LINES
    assert len(messages) <= R.MAX_BACKLOG // R.MAX_BATCH_LINES + 1
    assert messages[0].startswith(f"*… {10_000 - R.MAX_BACKLOG} earlier lines skipped*")
    assert messages[-1].endswith("spam 9999") and b.dropped == 0


def test_rate_limit_per_member_and_overall():
    r = R.RateLimit()
    assert r.allow(1, 100) and not r.allow(1, 100) and not r.allow(1, 101)
    assert r.allow(2, 101) and r.allow(1, 102)
    r = R.RateLimit()
    assert sum(r.allow(uid, 1000) for uid in range(100)) == R.WINDOW_MAX
    assert not r.allow(500, 1000 + R.WINDOW - 1) and r.allow(500, 1000 + R.WINDOW)
    assert len(r.last) <= R.WINDOW_MAX + 1  # old entries are forgotten


# ---------------------------------------------------------------- polling, playtime, health
def test_poll_delay_backs_off_and_recovers():
    assert [R.poll_delay(n) for n in range(8)] == [120, 30, 120, 240, 480, 900, 900, 900]
    assert R.poll_delay(-1) == R.POLL_EVERY


def test_retry_due():
    assert R.retry_due(0, 100, 100) and R.retry_due(R.MAX_ATTEMPTS - 1, 100, 100)
    assert not R.retry_due(R.MAX_ATTEMPTS, 100, 100 + R.SLOW_RETRY - 1)
    assert R.retry_due(R.MAX_ATTEMPTS, 100, 100 + R.SLOW_RETRY)


def test_credit_seconds_needs_two_close_polls():
    assert R.credit_seconds(None, 1000) == 0
    assert R.credit_seconds(1000, 1120) == 120 and R.credit_seconds(1000, 1000 + R.MAX_GAP) == R.MAX_GAP
    assert R.credit_seconds(1000, 1000 + R.MAX_GAP + 1) == 0  # the bot was away: nothing is assumed
    assert R.credit_seconds(1000, 1000) == 0 and R.credit_seconds(1000, 900) == 0


def test_slots_refs_and_caps():
    assert [R.slots(s) for s in (-5, 0, 1799, 1800, 3599, 3600, 14_400, 10 ** 9)] == [0, 0, 0, 1, 1, 2, 8, 8]
    assert R.SLOT_COINS * R.DAILY_SLOTS == 40
    assert R.ref(42, "2026-10-12", 3) == "realms:42:2026-10-12:3"
    assert R.ref(42, "2026-10-12", 3) != R.ref(42, "2026-10-13", 3) != R.ref(43, "2026-10-13", 3)


def test_days_weeks_and_durations():
    monday_noon = 1_791_831_600  # Mon 2026-10-12 12:00 Pacific
    assert R.day_key(monday_noon, TZ) == "2026-10-12"
    assert R.week_start(monday_noon, TZ) == date(2026, 10, 12)
    assert R.week_start(monday_noon + 6 * 86400, TZ) == date(2026, 10, 12)
    assert R.week_start(monday_noon - 13 * 3600, TZ) == date(2026, 10, 5)  # Sunday night
    assert [R.fmt_duration(s) for s in (0, 59, 60, 3599, 3600, 12_000, -5)] == \
        ["0m", "0m", "1m", "59m", "1h 00m", "3h 20m", "0m"]


def test_panel_text_and_signature():
    s = R.Status(2, 20, ("Steve", "A_b_c"), "Fabric 1.21.1", "The Realms")
    text = R.panel_text(s, ["Steve", "A_b_c"], None)
    assert text.splitlines() == ["🟢 **Online** · 2 / 20 players", "A\\_b\\_c, Steve", "Version: Fabric 1.21.1",
                                 "*The Realms*"]
    assert "Nobody is on" in R.panel_text(R.Status(0, 20, (), "", ""), [], None)
    assert "isn't sharing" in R.panel_text(R.Status(3, 20, (), "", ""), [], None)
    many = [f"Player{i:02}" for i in range(30)]
    assert "and 10 more" in R.panel_text(R.Status(30, 40, (), "", ""), many, None)
    assert "and 3 more" in R.panel_text(R.Status(5, 20, (), "", ""), ["Steve", "Alex"], None)
    assert R.panel_text(None, [], 1_790_000_000) == "🔴 **Offline** since <t:1790000000:R>"
    assert R.panel_text(None, [], None) == "🔴 **Offline**"
    bad_motd = R.Status(0, 20, (), "", "join discord.gg/abcdef")
    assert "discord.gg" not in R.panel_text(bad_motd, [], None)
    assert R.panel_signature(s, ["Steve"], None) != R.panel_signature(s, ["Steve", "Alex"], None)
    assert R.panel_signature(s, ["Steve"], None) == R.panel_signature(s, ["Steve"], None)
    assert R.panel_signature(None, [], 5) != R.panel_signature(None, [], 6) != R.panel_signature(s, [], None)


def test_health_line():
    full = R.read_setup({R.ENV_ADDRESS: "127.0.0.1", R.ENV_RCON_PASSWORD: PASSWORD,
                         R.ENV_LOG_PATH: "/srv/mc/logs/latest.log"})
    assert R.health(full) == "ok (status, whitelist, chat to Minecraft, chat from Minecraft)"
    assert R.health(full, down_since=1_790_000_000) == "unreachable since <t:1790000000:f>"
    assert R.health(full, rcon_down_since=5) == \
        "ok (status, whitelist, chat to Minecraft, chat from Minecraft); RCON unreachable since <t:5:f>"
    noisy = R.health(full, rcon_down_since=5, rcon_refused=True, log_missing=True, waiting=2)
    assert "RCON refused the password" in noisy and "log file not found" in noisy
    assert "2 whitelist changes waiting" in noisy and PASSWORD not in noisy
    half = R.read_setup({R.ENV_ADDRESS: "127.0.0.1", R.ENV_RCON_HOST: "8.8.8.8", R.ENV_RCON_PASSWORD: PASSWORD})
    assert R.health(half).startswith("ok (status); setting problem: REALMS_RCON_HOST refused")


# ---------------------------------------------------------------- ping transport against a fake server
async def serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def ping_handler(reply: bytes | None, seen: list, hold: bool = False):
    async def handle(reader, writer):
        try:
            data = b""
            while not data.endswith(R.STATUS_REQUEST):
                chunk = await reader.read(1024)
                if not chunk:
                    break
                data += chunk
            seen.append(data)
            if reply is not None:
                writer.write(reply)
                await writer.drain()
            if hold:
                await asyncio.Event().wait()
        except ConnectionError:
            pass
        finally:
            writer.close()
    return handle


def test_ping_against_a_fake_server():
    async def go():
        seen = []
        server, port = await serve(ping_handler(R.frame(status_payload(STATUS)), seen))
        async with server:
            raw = await net.ping_server("127.0.0.1", port)
        assert raw == STATUS and R.read_status(raw).names == ("Steve", "Alex_2")
        assert seen == [R.handshake("127.0.0.1", port) + R.STATUS_REQUEST]
    run(go())


def test_ping_reads_a_reply_that_arrives_in_pieces():
    async def go():
        reply = R.frame(status_payload({**STATUS, "favicon": "x" * 70_000}))

        async def handle(reader, writer):
            await reader.read(1024)
            for i in range(0, len(reply), 7001):
                writer.write(reply[i:i + 7001])
                await writer.drain()
                await asyncio.sleep(0)
            writer.close()

        server, port = await serve(handle)
        async with server:
            assert (await net.ping_server("127.0.0.1", port))["players"]["online"] == 2
    run(go())


@pytest.mark.parametrize("reply,why", [
    (R.pack_varint(R.MAX_STATUS_BYTES + 1) + b"x" * 64, "ValueError"),  # announced size over the cap
    (R.pack_varint(0), "ValueError"),
    (b"\xff\xff\xff\xff\xff\xff", "ValueError"),  # a varint that never ends
    (R.frame(status_payload(STATUS))[:-10], "IncompleteReadError"),  # cut off mid-reply
    (R.frame(status_payload(b"not json at all")), "ValueError"),
    (R.frame(status_payload(STATUS, packet_id=3)), "ValueError"),
    (b"HTTP/1.1 400 Bad Request\r\n\r\n", "IncompleteReadError"),
    (b"", "IncompleteReadError"),
])
def test_ping_refuses_bad_replies(reply, why):
    async def go():
        server, port = await serve(ping_handler(reply, []))
        async with server:
            with pytest.raises(net.PingError) as err:
                await net.ping_server("127.0.0.1", port)
        assert str(err.value) == why
    run(go())


def test_ping_times_out_on_a_silent_server_and_fails_fast_on_a_closed_port():
    async def go():
        server, port = await serve(ping_handler(None, [], hold=True))
        async with server:
            with pytest.raises(net.PingError) as err:
                await net.ping_server("127.0.0.1", port, timeout=0.2)
            assert str(err.value) == "TimeoutError"
        with pytest.raises(net.PingError):  # the fake server is closed now
            await net.ping_server("127.0.0.1", port, timeout=2)
    run(go())


# ---------------------------------------------------------------- RCON transport against a fake server
def raw_packet(request_id: int, kind: int, body: str = "") -> bytes:
    """Built by hand (not with logic.realms), so the test checks the codec, not itself."""
    data = body.encode("utf-8")
    return struct.pack("<iii", len(data) + 10, request_id, kind) + data + b"\x00\x00"


class FakeRconServer:
    def __init__(self, password=PASSWORD, replies=None, mode="normal", source_style=False):
        self.password = password
        self.replies = replies or {}
        self.mode = mode
        self.source_style = source_style
        self.connections = 0
        self.commands: list[str] = []
        self.auth_attempts: list[str] = []
        self.ids: list[int] = []

    async def handle(self, reader, writer):
        self.connections += 1
        authed = False
        try:
            while True:
                (length,) = struct.unpack("<i", await reader.readexactly(4))
                payload = await reader.readexactly(length)
                request_id, kind = struct.unpack("<ii", payload[:8])
                assert payload[-2:] == b"\x00\x00"
                body = payload[8:-2].decode("utf-8")
                self.ids.append(request_id)
                if kind == 3:
                    self.auth_attempts.append(body)
                    authed = body == self.password
                    if self.source_style:
                        writer.write(raw_packet(request_id, 0))
                    writer.write(raw_packet(request_id if authed else -1, 2))
                elif kind == 2 and authed:
                    self.commands.append(body)
                    if self.mode == "silent":
                        continue
                    if self.mode == "huge":
                        writer.write(struct.pack("<i", 50_000_000) + b"x" * 64)
                    elif self.mode == "tiny":
                        writer.write(struct.pack("<i", 4) + b"abcd")
                    elif self.mode == "stale":  # leftovers of an earlier split reply come first
                        writer.write(raw_packet(request_id - 100, 0, "old") + raw_packet(request_id, 0, "fresh"))
                    else:
                        writer.write(raw_packet(request_id, 0, self.replies.get(body, "")))
                await writer.drain()
                if self.mode == "hangup" and kind == 2:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def with_rcon(fn, **kwargs):
    async def go():
        fake = FakeRconServer(**kwargs)
        server, port = await serve(fake.handle)
        async with server:
            client = net.Rcon("127.0.0.1", port, PASSWORD, timeout=2)
            try:
                await fn(fake, client, port)
            finally:
                await client.close()
    run(go())


def test_rcon_signs_in_once_and_runs_commands():
    async def go(fake, client, port):
        assert await client.command("list") == "There are 0 of a max of 20 players online: "
        assert await client.command("whitelist add Steve") == "Added Steve to the whitelist"
        assert await client.command("tellraw @a [\"\"]") == ""
        assert fake.auth_attempts == [PASSWORD] and fake.connections == 1  # one connection, kept
        assert fake.commands == ["list", "whitelist add Steve", "tellraw @a [\"\"]"]
        assert len(set(fake.ids)) == len(fake.ids) == 4 and all(i > 0 for i in fake.ids)
    with_rcon(go, replies={"list": "There are 0 of a max of 20 players online: ",
                           "whitelist add Steve": "Added Steve to the whitelist"})


def test_rcon_accepts_the_source_style_sign_in():
    async def go(fake, client, port):
        assert await client.command("list") == "ok"
    with_rcon(go, replies={"list": "ok"}, source_style=True)


def test_rcon_wrong_password_is_refused_without_leaking_it(caplog):
    async def go(fake, client, port):
        with pytest.raises(net.RconRefused) as err:
            await client.command("list")
        assert isinstance(err.value, net.RconError)
        assert PASSWORD not in str(err.value) and PASSWORD not in repr(err.value)
        assert err.value.__cause__ is None and err.value.__context__ is None or PASSWORD not in repr(
            err.value.__context__)
        assert PASSWORD not in repr(client) and PASSWORD not in str(vars(client).keys())
        assert fake.commands == [] and client.writer is None  # the command was never sent
        with pytest.raises(net.RconRefused):
            await client.command("list")
        assert fake.connections == 2
    with caplog.at_level("DEBUG"):
        with_rcon(go, password="something-else")
    assert PASSWORD not in caplog.text


def test_rcon_reconnects_after_the_server_drops_the_connection():
    async def go(fake, client, port):
        assert await client.command("list") == "ok"
        await asyncio.sleep(0.05)  # let the hang-up arrive
        assert await client.command("list") == "ok"
        assert fake.connections == 2 and fake.auth_attempts == [PASSWORD, PASSWORD]
    with_rcon(go, replies={"list": "ok"}, mode="hangup")


def test_rcon_skips_stale_packets():
    async def go(fake, client, port):
        assert await client.command("list") == "fresh"
    with_rcon(go, mode="stale")


@pytest.mark.parametrize("mode,why", [("huge", "ValueError"), ("tiny", "ValueError")])
def test_rcon_refuses_packets_outside_the_size_limits(mode, why):
    async def go(fake, client, port):
        with pytest.raises(net.RconError) as err:
            await client.command("list")
        assert str(err.value) == f"no answer ({why})" and client.writer is None
    with_rcon(go, mode=mode)


def test_rcon_times_out_and_recovers():
    async def go(fake, client, port):
        client.timeout = 0.2
        with pytest.raises(net.RconError) as err:
            await client.command("list")
        assert str(err.value) == "no answer (TimeoutError)" and client.writer is None
        fake.mode = "normal"
        client.timeout = 2
        assert await client.command("list") == "back"
    with_rcon(go, mode="silent", replies={"list": "back"})


def test_rcon_never_sends_an_unsendable_command():
    async def go(fake, client, port):
        for bad in ("x" * (R.MAX_COMMAND_BYTES + 1), "list\x00op Steve"):
            with pytest.raises(net.RconError) as err:
                await client.command(bad)
            assert str(err.value) == "that command cannot be sent"
        assert fake.connections == 0
    with_rcon(go)


def test_rcon_closed_port_is_an_error_not_a_crash():
    async def go():
        server, port = await serve(FakeRconServer().handle)
        server.close()
        await server.wait_closed()
        client = net.Rcon("127.0.0.1", port, PASSWORD, timeout=2)
        with pytest.raises(net.RconError) as err:
            await client.command("list")
        assert str(err.value).startswith("no answer (") and PASSWORD not in str(err.value)
    run(go())


def test_rcon_commands_do_not_interleave():
    async def go(fake, client, port):
        replies = await asyncio.gather(*(client.command(f"say {i}") for i in range(20)))
        assert replies == [f"r{i}" for i in range(20)] and fake.connections == 1
    with_rcon(go, replies={f"say {i}": f"r{i}" for i in range(20)})


# ---------------------------------------------------------------- log reader against real files
def test_read_log_tails_from_the_end_and_follows_appends(tmp_path):
    path = tmp_path / "latest.log"
    assert net.read_log(str(path), R.TailState()) is None  # no file yet: quiet
    path.write_bytes(b"old line 1\nold line 2\n")
    state, data, reset = net.read_log(str(path), R.TailState())
    assert data == b"" and reset and state.pos == 22  # history is not replayed
    with open(path, "ab") as f:
        f.write(b"new line\npart")
    state, data, reset = net.read_log(str(path), state)
    assert data == b"new line\npart" and not reset and state.pos == 35
    state2, data, reset = net.read_log(str(path), state)
    assert data == b"" and not reset and state2 == state


def test_read_log_survives_truncation_and_rotation(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"x" * 500)
    state, _, _ = net.read_log(str(path), R.TailState())
    path.write_bytes(b"fresh start\n")  # truncated in place
    state, data, reset = net.read_log(str(path), state)
    assert data == b"fresh start\n" and reset and state.pos == 12
    path.rename(tmp_path / "2026-10-12-1.log")  # rotated: a new file takes the name
    assert net.read_log(str(path), state) is None
    path.write_bytes(b"after the restart, a longer first line\n")
    state, data, reset = net.read_log(str(path), state)
    assert data == b"after the restart, a longer first line\n" and reset


def test_read_log_reads_in_bounded_chunks_and_skips_a_flood(tmp_path):
    path = tmp_path / "latest.log"
    path.write_bytes(b"")
    state, _, _ = net.read_log(str(path), R.TailState())
    with open(path, "ab") as f:
        f.write(b"a" * (R.TAIL_CHUNK + 100))
    state, data, reset = net.read_log(str(path), state)
    assert len(data) == R.TAIL_CHUNK and not reset
    state, data, reset = net.read_log(str(path), state)
    assert len(data) == 100
    with open(path, "ab") as f:
        f.write(b"b" * (R.MAX_BEHIND + 1))
    state, data, reset = net.read_log(str(path), state)
    assert data == b"" and reset and state.pos == path.stat().st_size
    assert net.read_log(str(tmp_path), state) is None  # a directory, not a file
