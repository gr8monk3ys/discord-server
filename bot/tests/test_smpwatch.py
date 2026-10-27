"""Pure tests for logic.smpwatch: settings, the SRV query and reply codec, picking a target,
and when the SMP Ping goes out. No network, no Discord."""

import struct
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import config
from logic import smpwatch as L

TZ = ZoneInfo("America/Los_Angeles")
HOUR = 3600
NAME = L.srv_name(L.DEFAULT_ADDRESS)


def ts(h, mi=0, day=7):
    return int(datetime(2026, 10, day, h, mi, tzinfo=TZ).timestamp())


# ---------------------------------------------------------------- settings
def test_defaults_point_at_the_public_address():
    s = L.read_settings({})
    assert s == L.Settings(True, "monkeysmp.lscaturchio.xyz", None, 3, ())
    assert s.address == "monkeysmp.lscaturchio.xyz" and L.MIN_ONLINE == 3


@pytest.mark.parametrize("word", ["off", "OFF", " 0 ", "false", "no"])
def test_off_words_turn_it_off(word):
    assert not L.read_settings({"SMP_WATCH": word}).enabled


@pytest.mark.parametrize("word", ["", "on", "yes", "1"])
def test_anything_else_leaves_it_on(word):
    assert L.read_settings({"SMP_WATCH": word}).enabled


def test_address_without_a_port_is_looked_up_and_with_one_is_used_as_is():
    s = L.read_settings({"SMP_WATCH_ADDRESS": " play.example.org "})
    assert (s.host, s.port, s.address, s.problems) == ("play.example.org", None, "play.example.org", ())
    s = L.read_settings({"SMP_WATCH_ADDRESS": "play.example.org:25570"})
    assert (s.host, s.port, s.address) == ("play.example.org", 25570, "play.example.org:25570")


@pytest.mark.parametrize("raw", ["not a host", "https://x.org", "x.org:", "x.org:99999", "[::1]:25565", "a@b.org"])
def test_bad_address_keeps_the_default_and_never_repeats_the_value(raw):
    s = L.read_settings({"SMP_WATCH_ADDRESS": raw})
    assert s.enabled and (s.host, s.port) == (L.DEFAULT_ADDRESS, None)
    assert len(s.problems) == 1 and "SMP_WATCH_ADDRESS" in s.problems[0] and raw not in s.problems[0]


@pytest.mark.parametrize("raw,want", [("2", 2), ("5", 5), ("50", 50), (" 4 ", 4)])
def test_min_online_in_range(raw, want):
    s = L.read_settings({"SMP_WATCH_MIN": raw})
    assert s.min_online == want and s.problems == ()


@pytest.mark.parametrize("raw", ["1", "51", "0", "-3", "three", "3.5", "٣"])
def test_min_online_out_of_range_keeps_three(raw):
    s = L.read_settings({"SMP_WATCH_MIN": raw})
    assert s.min_online == 3 and len(s.problems) == 1 and "SMP_WATCH_MIN" in s.problems[0]


# ---------------------------------------------------------------- DNS
def qname(name):
    return b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\x00"


def reply(qid, answers=(), rcode=0, tc=False, qr=True, name=NAME):
    flags = (0x8000 if qr else 0) | 0x0180 | (0x0200 if tc else 0) | rcode
    head = struct.pack(">HHHHHH", qid, flags, 1, len(answers), 0, 0) + qname(name) + struct.pack(">HH", 33, 1)
    for rtype, rdata in answers:
        head += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, 300, len(rdata)) + rdata
    return head


def srv(priority, weight, port, target):
    return 33, struct.pack(">HHH", priority, weight, port) + target


# The question name starts at 12; "_minecraft" (11 bytes) and "_tcp" (5) come first.
MONKEYSMP_AT = 12 + 11 + 5
PLAY = b"\x04play" + bytes([0xC0, MONKEYSMP_AT])  # "play" + a pointer into the question


def test_query_is_one_recursive_srv_question():
    q = L.srv_query(NAME, 0x12345)
    assert q[:12] == bytes.fromhex("2345 0100 0001 0000 0000 0000".replace(" ", ""))
    assert q[12:-4] == qname(NAME) and q[-4:] == b"\x00\x21\x00\x01"


@pytest.mark.parametrize("bad", ["a..b", "x" * 64 + ".org", "café.org", ""])
def test_query_refuses_names_it_cannot_encode(bad):
    with pytest.raises(L.DnsError):
        L.srv_query(bad, 1)


def test_reply_with_compressed_names_is_read():
    data = reply(7, [srv(0, 5, 25565, PLAY), (5, b"\xc0\x0c"), srv(10, 1, 25570, qname("backup.example.org"))])
    assert L.parse_srv_reply(data, 7) == [L.Srv(0, 5, 25565, "play.monkeysmp.lscaturchio.xyz"),
                                          L.Srv(10, 1, 25570, "backup.example.org")]  # the CNAME is skipped


def test_no_such_name_and_no_records_are_empty():
    assert L.parse_srv_reply(reply(7, rcode=3), 7) == []
    assert L.parse_srv_reply(reply(7), 7) == []


@pytest.mark.parametrize("data", [
    reply(8, [srv(0, 5, 25565, PLAY)]),  # another query's reply
    reply(7, [srv(0, 5, 25565, PLAY)], qr=False),  # a query, not a reply
    reply(7, [srv(0, 5, 25565, PLAY)], tc=True),  # truncated: a UDP answer we can't trust
    reply(7, rcode=2),  # server failure
    reply(7, [srv(0, 5, 25565, PLAY)])[:-3],  # cut short
    b"\x00\x07" + b"\x00" * 6,  # shorter than a header
    reply(7, [srv(0, 5, 25565, b"\x04play\x40")]),  # a label type DNS doesn't use
])
def test_unusable_replies_raise(data):
    with pytest.raises(L.DnsError):
        L.parse_srv_reply(data, 7)


def test_a_pointer_loop_stops():
    head = reply(7)
    at = len(head)
    data = head[:6] + b"\x00\x01" + head[8:] + bytes([0xC0 | (at >> 8), at & 0xFF]) + b"\x00" * 10
    with pytest.raises(L.DnsError):
        L.parse_srv_reply(data, 7)


def test_pick_takes_the_lowest_priority_then_the_heaviest_weight():
    records = [L.Srv(10, 100, 1, "far.example.org."), L.Srv(0, 1, 25566, "light.example.org"),
               L.Srv(0, 9, 25565, "play.example.org."), L.Srv(0, 50, 0, "bad-port.example.org")]
    assert L.pick_srv(records) == ("play.example.org", 25565)


@pytest.mark.parametrize("records", [[], [L.Srv(0, 0, 25565, ".")], [L.Srv(0, 0, 25565, "")],
                                     [L.Srv(0, 0, 25565, "bad name.org")]])
def test_pick_none_when_nothing_is_usable(records):
    assert L.pick_srv(records) is None


def test_fallback_is_the_name_on_the_default_port():
    assert L.fallback("monkeysmp.lscaturchio.xyz") == ("monkeysmp.lscaturchio.xyz", 25565)


def test_resolv_conf_ipv4_nameservers_in_order():
    text = "# comment\nnameserver 10.0.0.2\nsearch lan\nnameserver ::1\nnameserver 1.1.1.1 # x\nnameserver\n"
    assert L.resolv_conf_servers(text) == ["10.0.0.2", "1.1.1.1"]


# ---------------------------------------------------------------- when to ping
def test_state_round_trips_and_bad_values_reset():
    s = L.State(ts(19), armed=False)
    assert L.load(L.dump(s)) == s
    for raw in (None, "", "not json", "[1, 2]", '{"last_post_at": true}', '{"last_post_at": "1"}'):
        assert L.load(raw) == L.State()


def decide(state, online, t, **kw):
    return L.decide(state, online, t, TZ, **kw)


def test_three_on_pings_once_and_keeps_when():
    go, state = decide(L.State(), 3, ts(19))
    assert go and state == L.State(ts(19), armed=False)
    assert decide(L.State(), 2, ts(19)) == (False, L.State())


def test_one_long_evening_is_one_ping():
    state, sent = L.State(), []
    for minute in range(0, 6 * 60, 5):  # 17:00 to 22:55, 3 to 6 on the whole time
        t = ts(17) + minute * 60
        go, state = decide(state, 3 + minute % 4, t)
        sent += [t] if go else []
    assert sent == [ts(17)]


def test_a_new_crowd_after_a_quiet_spell_and_the_cooldown_gets_a_new_ping():
    _, state = decide(L.State(), 4, ts(12))
    _, state = decide(state, 1, ts(13))  # seen below: armed again
    assert state.armed
    assert decide(state, 4, ts(15, 59))[0] is False  # inside four hours
    go, state = decide(state, 4, ts(16))
    assert go and state == L.State(ts(16), armed=False)


def test_four_hours_alone_does_not_re_arm():
    _, state = decide(L.State(), 3, ts(10))
    assert decide(state, 3, ts(20)) == (False, state)


def test_a_failed_check_changes_nothing():
    state = L.State(ts(10), armed=False)
    assert decide(state, None, ts(20)) == (False, state)
    assert decide(L.State(), None, ts(20)) == (False, L.State())


@pytest.mark.parametrize("h", [22, 23, 0, 3, 8])
def test_quiet_hours_hold_the_ping_until_morning(h):
    t = ts(h, 30, day=7 if h >= 22 else 8)
    assert decide(L.State(), 5, t) == (False, L.State())
    go, _ = decide(L.State(), 5, ts(9, day=8))
    assert go
    assert decide(L.State(), 5, t, quiet=None)[0]


def test_min_online_is_a_parameter():
    assert decide(L.State(), 4, ts(19), min_online=5) == (False, L.State())
    assert decide(L.State(), 5, ts(19), min_online=5)[0]


def test_ping_text_has_the_count_the_role_and_the_address_only():
    text = L.ping_text("<@&123>", 4, "monkeysmp.lscaturchio.xyz")
    assert text.startswith("⛏️ <@&123> **4 on Monkey SMP now.**")
    assert "`monkeysmp.lscaturchio.xyz`" in text
    assert config.ROLES_CHANNEL in text and config.SMP_PING_ROLE in text
    assert "@everyone" not in text and "@here" not in text
