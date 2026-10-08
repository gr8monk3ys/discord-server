"""Unit tests for logic.partners: invite parsing, invite checks, application rules,
text for posts, review cards and DMs, and the weekly sweep schedule."""

import json
from zoneinfo import ZoneInfo

import pytest

from logic import partners as P

OURS = 999
T0 = 1_790_000_000
DAY = 24 * 60 * 60
TZ = ZoneInfo("America/Los_Angeles")


def body(**kw) -> bytes:
    data = {"code": "abcDEF12", "type": 0, "expires_at": None,
            "guild": {"id": "123456", "name": "Cozy Corner"}, "approximate_member_count": 250}
    data.update(kw)
    return json.dumps(data).encode()


# ---------------------------------------------------------------- invite parsing
@pytest.mark.parametrize("text, code", [
    ("abcDEF12", "abcDEF12"),
    ("  abcDEF12  ", "abcDEF12"),
    ("discord.gg/abcDEF12", "abcDEF12"),
    ("https://discord.gg/abcDEF12", "abcDEF12"),
    ("http://discord.gg/abcDEF12/", "abcDEF12"),
    ("https://www.discord.gg/abcDEF12", "abcDEF12"),
    ("https://discord.com/invite/abcDEF12", "abcDEF12"),
    ("https://discordapp.com/invite/abcDEF12", "abcDEF12"),
    ("discord.com/invite/cozy-corner", "cozy-corner"),
    ("<https://discord.gg/abcDEF12>", "abcDEF12"),
])
def test_parse_invite_accepts(text, code):
    assert P.parse_invite(text) == code


@pytest.mark.parametrize("text", [
    "", "a", "has space", "abc/def", "https://evil.example/abcDEF12", "https://discord.gg/",
    "https://discord.gg/abc?x=1", "https://discord.gg/abc#frag", "https://user@discord.gg/abc",
    "https://discord.gg:8080/abc", "https://discord.com/channels/1/2", "https://discord.gg/abc/def",
    "x" * 40, "abc_def", "abc.def", "javascript:alert(1)", "../../users/@me",
])
def test_parse_invite_rejects(text):
    assert P.parse_invite(text) is None


def test_invite_url_is_rebuilt_from_code():
    assert P.invite_url("abcDEF12") == "https://discord.gg/abcDEF12"
    assert P.api_url("abcDEF12") == "https://discord.com/api/v10/invites/abcDEF12"
    with pytest.raises(ValueError):
        P.invite_url("bad code")
    with pytest.raises(ValueError):
        P.api_url("../x")


# ---------------------------------------------------------------- invite check
def test_check_invite_ok():
    c = P.check_invite(200, body(), OURS)
    assert c.problem is None
    assert (c.guild_id, c.guild_name, c.members) == (123456, "Cozy Corner", 250)
    assert c.definitive


def test_check_invite_not_found():
    c = P.check_invite(404, json.dumps({"code": 10006, "message": "Unknown Invite"}).encode(), OURS)
    assert c.problem == "not_found" and c.definitive
    assert P.check_invite(404, b"garbage", OURS).problem == "not_found"


def test_check_invite_temporary():
    c = P.check_invite(200, body(expires_at="2026-10-08T00:00:00+00:00"), OURS)
    assert c.problem == "temporary" and c.definitive


def test_check_invite_small():
    assert P.check_invite(200, body(approximate_member_count=19), OURS).problem == "small"
    assert P.check_invite(200, body(approximate_member_count=20), OURS).problem is None
    assert P.check_invite(200, body(approximate_member_count=None), OURS).problem == "small"


def test_check_invite_self():
    assert P.check_invite(200, body(guild={"id": str(OURS), "name": "Us"}), OURS).problem == "self"


def test_check_invite_not_a_server():
    assert P.check_invite(200, body(type=1, guild=None), OURS).problem == "not_server"
    assert P.check_invite(200, body(guild=None), OURS).problem == "not_server"
    assert P.check_invite(200, body(guild={"id": "abc", "name": "x"}), OURS).problem == "not_server"


@pytest.mark.parametrize("status, raw", [(429, b"{}"), (500, b""), (200, b"not json"), (200, b"[1,2]"),
                                         (401, b"{}"), (403, b"{}")])
def test_check_invite_unreachable(status, raw):
    c = P.check_invite(status, raw, OURS)
    assert c.problem == "unreachable" and not c.definitive


def test_check_invite_counts_must_be_ints():
    assert P.check_invite(200, body(approximate_member_count="500"), OURS).problem == "small"
    assert P.check_invite(200, body(approximate_member_count=True), OURS).problem == "small"


def test_check_invite_name_is_trimmed_and_capped():
    c = P.check_invite(200, body(guild={"id": "5", "name": "  " + "n" * 300 + " "}), OURS)
    assert c.guild_name == "n" * P.NAME_MAX


def test_dead_only_for_definitive_gone():
    assert P.is_dead(P.check_invite(404, b"{}", OURS))
    assert P.is_dead(P.check_invite(200, body(expires_at="2026-10-08T00:00:00+00:00"), OURS))
    assert not P.is_dead(P.check_invite(200, body(approximate_member_count=3), OURS))  # shrank: still alive
    assert not P.is_dead(P.check_invite(500, b"", OURS))
    assert not P.is_dead(P.check_invite(200, body(), OURS))


def test_every_problem_has_a_reply():
    for problem in ("not_found", "temporary", "small", "self", "not_server", "unreachable", "invite_format",
                    "name", "description_short", "description_long", "description_mentions",
                    "description_invite", "name_mentions", "pending", "duplicate"):
        assert P.REPLIES[problem]


# ---------------------------------------------------------------- text checks
def test_clean_description_one_paragraph():
    assert P.clean_text("  hello\n\nthere   friend \t ok ") == "hello there friend ok"


@pytest.mark.parametrize("text", [
    "join us @everyone for fun", "hey @here we are cool", "ask <@123> about it", "ask <@!123> about it",
    "ping <@&456> please", "also @EVERYONE caps",
])
def test_description_mentions_rejected(text):
    desc = text + " " + "x" * P.DESC_MIN
    assert P.description_problem(P.clean_text(desc)) == "description_mentions"


def test_description_with_invite_rejected():
    desc = "A cozy place to hang out and play games together. Also discord.gg/other123 for more"
    assert P.description_problem(desc) == "description_invite"
    desc = "A cozy place to hang out and play games together. discord.com/invite/other"
    assert P.description_problem(desc) == "description_invite"


def test_description_lengths():
    assert P.description_problem("too short") == "description_short"
    assert P.description_problem("x" * (P.DESC_MAX + 1)) == "description_long"
    assert P.description_problem("A chill gaming community for night owls and weekend squads.") is None


def test_name_problem():
    assert P.name_problem("") == "name"
    assert P.name_problem("   ") == "name"
    assert P.name_problem("Cozy Corner") is None
    assert P.name_problem("Cozy @everyone") == "name_mentions"
    assert P.name_problem("x" * (P.NAME_MAX + 1)) == "name"


def test_names_match_ignores_case_and_symbols():
    assert P.names_match("Cozy Corner", "cozy-corner!")
    assert P.names_match("🎮 Cozy Corner", "Cozy Corner")
    assert not P.names_match("Cozy Corner", "Totally Different")


# ---------------------------------------------------------------- application rules
def row(status, created_at=T0, decided_at=None, code="abc", user_id=1):
    return {"status": status, "created_at": created_at, "decided_at": decided_at, "invite_code": code,
            "user_id": user_id}


def test_application_open_when_nothing_before():
    assert P.application_problem([], T0) is None


def test_one_pending_per_member():
    assert P.application_problem([row("pending")], T0) == ("pending", 0)


def test_cooldown_after_denial():
    rows = [row("denied", decided_at=T0 - 10 * DAY)]
    problem, days = P.application_problem(rows, T0)
    assert problem == "cooldown" and days == 20
    assert P.application_problem([row("denied", decided_at=T0 - 30 * DAY)], T0) is None
    # rounds up: 1 second left is still one day
    problem, days = P.application_problem([row("denied", decided_at=T0 - 30 * DAY + 1)], T0)
    assert (problem, days) == ("cooldown", 1)


def test_cooldown_uses_latest_denial():
    rows = [row("denied", decided_at=T0 - 40 * DAY), row("denied", decided_at=T0 - 2 * DAY)]
    assert P.application_problem(rows, T0)[0] == "cooldown"


def test_approved_or_removed_do_not_block():
    assert P.application_problem([row("approved"), row("removed"), row("dead")], T0) is None


def test_duplicate_code():
    assert P.duplicate([row("approved", code="abc")], "abc")
    assert P.duplicate([row("pending", code="abc")], "abc")
    assert not P.duplicate([row("denied", code="abc"), row("dead", code="abc")], "abc")
    assert not P.duplicate([row("approved", code="abc")], "ABC")  # invite codes are case-sensitive
    assert not P.duplicate([row("approved", code="abc")], "xyz")


def test_cooldown_reply_formats_days():
    assert "20 days" in P.reply("cooldown", 20)
    assert "1 day" in P.reply("cooldown", 1) and "1 days" not in P.reply("cooldown", 1)


# ---------------------------------------------------------------- text
def test_post_lines_with_and_without_link():
    alive = P.post_lines("Cozy Corner", "A chill place.", 250, "abcDEF12")
    assert "https://discord.gg/abcDEF12" in alive and "250 members" in alive and "A chill place." in alive
    dead = P.post_lines("Cozy Corner", "A chill place.", None, "abcDEF12", dead=True)
    assert "discord.gg" not in dead and "expired" in dead.lower()


def test_review_lines_flag_name_mismatch():
    same = P.review_lines(7, "<@1>", "Cozy Corner", "Cozy Corner", 250, "abcDEF12", "desc")
    assert "doesn't match" not in same
    diff = P.review_lines(7, "<@1>", "Cozy Corner", "Other Place", 250, "abcDEF12", "desc")
    assert "doesn't match" in diff and "Other Place" in diff
    assert "<https://discord.gg/abcDEF12>" in same  # no embed preview of the invite


def test_dm_texts():
    assert "approved" in P.dm_text("approved", "Cozy Corner", "Lorenzo's Server").lower()
    denied = P.dm_text("denied", "Cozy Corner", "Lorenzo's Server")
    assert "30 days" in denied


# ---------------------------------------------------------------- schedule
def test_sweep_job_is_weekly_monday_noon():
    assert (P.SWEEP_JOB.weekday, P.SWEEP_JOB.hour, P.SWEEP_JOB.minute) == (0, 12, 0)
    assert P.SWEEP_JOB.name == "partners"
