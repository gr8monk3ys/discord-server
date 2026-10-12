"""logic.appeals: the code, the webhook payload, who may appeal when, and the text."""

import json

import config
from logic import appeals as A
from logic import moderation as M

# The shared vector: site/api/appeal.test.mjs asserts the same literal for the same inputs.
SECRET = "test-secret-for-appeal-vectors!!"
USER = 123456789012345678
CODE = "TD7YUL66OYCHIZTL"
DAY = A.DAY
T0 = 1_790_000_000


def payload(**over):
    data = {"v": 1, "u": str(USER), "c": CODE, "a": ["what happened", "why reconsider", ""]}
    data.update(over)
    return json.dumps(data)


# ---------------------------------------------------------------- the code
def test_code_matches_the_javascript_implementation():
    assert A.appeal_code(SECRET, USER) == CODE
    assert A.appeal_code(SECRET, 987654321098765432) == "TQRXMXY43NDFCQDZ"
    assert A.appeal_code(SECRET + "x", USER) != CODE
    assert len(CODE) == A.CODE_LEN


def test_code_is_checked_in_any_spelling_and_nothing_else_passes():
    assert A.code_matches(SECRET, USER, CODE)
    assert A.code_matches(SECRET, USER, " td7y-ul66 oych-iztl ")
    assert A.fmt_code(CODE) == "TD7Y-UL66-OYCH-IZTL"
    assert A.code_matches(SECRET, USER, A.fmt_code(CODE))
    for bad in (None, "", CODE[:-1], CODE + "A", "A" * 16, CODE[:-1] + "1", CODE[:-1] + "é"):
        assert not A.code_matches(SECRET, USER, bad), bad
    assert not A.code_matches(SECRET, USER + 1, CODE)
    assert not A.code_matches("another-secret-entirely", USER, CODE)


# ---------------------------------------------------------------- settings
def test_settings_are_off_without_a_real_secret():
    assert config.appeal_settings({}) is None
    assert config.appeal_settings({"APPEAL_SECRET": "   "}) is None
    assert config.appeal_settings({"APPEAL_SECRET": "too-short"}) is None
    s = config.appeal_settings({"APPEAL_SECRET": f"  {SECRET} "})
    assert s.secret == SECRET
    assert s.page_url == "https://discord.lscaturchio.xyz/appeal" and s.join_url.endswith("/join")
    assert (s.cooling, s.reappeal) == (7 * DAY, 90 * DAY)


def test_settings_overrides_and_bad_values_fall_back():
    s = config.appeal_settings({"APPEAL_SECRET": SECRET, "APPEAL_COOLING_DAYS": "0", "APPEAL_REAPPEAL_DAYS": "30",
                                "SITE_URL": "https://example.test/"})
    assert (s.cooling, s.reappeal, s.page_url) == (0, 30 * DAY, "https://example.test/appeal")
    s = config.appeal_settings({"APPEAL_SECRET": SECRET, "APPEAL_COOLING_DAYS": "soon", "APPEAL_REAPPEAL_DAYS": "-4",
                                "SITE_URL": "http://insecure.test"})
    assert (s.cooling, s.reappeal) == (7 * DAY, 90 * DAY)
    assert s.page_url == f"{config.SITE_URL}/appeal"
    assert config.appeal_settings({"APPEAL_SECRET": SECRET, "SITE_URL": "https://a.test/> <x"}).page_url \
        == f"{config.SITE_URL}/appeal"


# ---------------------------------------------------------------- the payload
def test_parse_payload_reads_the_functions_json():
    p = A.parse_payload(payload(c="td7y-ul66-oych-iztl"))
    assert p == A.Payload(USER, CODE, {"what": "what happened", "why": "why reconsider", "else": ""})


def test_parse_payload_reads_escaped_characters():
    # The function writes markdown characters as \u escapes; they are still the same JSON.
    raw = '{"v":1,"u":"%d","c":"%s","a":["\\u0040everyone \\u005bx\\u005d","b",""]}' % (USER, CODE)
    assert A.parse_payload(raw).answers["what"] == "@everyone [x]"


def test_parse_payload_refuses_everything_else():
    bad = [None, "", "not json", "[]", "null", '"text"', payload(v=2), payload(u=USER), payload(u="12"),
           payload(u="0" * 18), payload(u=f"{USER} "), payload(u="1" * 21), payload(c="short"), payload(c=None),
           payload(a="text"), payload(a=["a", "b"]), payload(a=["a", "b", "c", "d"]), payload(a=["a", 2, ""]),
           payload(a=["", "why", ""]), payload(a=["what", "  \n ", ""]), "x" * (A.PAYLOAD_MAX + 1)]
    for text in bad:
        assert A.parse_payload(text) is None, text


def test_answers_are_cleaned_and_capped():
    assert A.clean_answer("  a\tb\x00c\u200bd\u202ee  ", 100) == "a bcde"
    assert A.clean_answer("one\r\n\r\n\r\n\r\ntwo \n three", 100) == "one\n\ntwo\nthree"
    assert A.clean_answer(None, 10) == "" and A.clean_answer("\x07\u200d", 10) == ""
    assert A.clean_answer("x" * 50, 10) == "x" * 10
    p = A.parse_payload(payload(a=["w" * 5000, "y", "e" * 400]))
    assert p is None  # over the whole-payload cap
    p = A.parse_payload(payload(a=["w" * 900, "y", "e" * 400]))
    assert len(p.answers["what"]) == 600 and len(p.answers["else"]) == 300


def test_answers_round_trip_through_storage():
    answers = {"what": 'a "quoted" ünïcode\nline', "why": "b", "else": ""}
    assert A.load_answers(A.dump_answers(answers)) == answers
    assert A.load_answers("") == {} and A.load_answers("[1]") == {} and A.load_answers(None) == {}


def test_screen_answer_hides_links_invites_and_blocked_language():
    assert A.screen_answer("I was rude and I'm sorry.") == ("I was rude and I'm sorry.", None)
    assert A.screen_answer("") == ("", None)
    assert A.screen_answer("see https://evil.example/a") == (None, "a link")
    assert A.screen_answer("look at [this](http://x.example)") == (None, "a link")
    assert A.screen_answer("join discord.gg/abcdef") == (None, "an invite link")
    assert A.fit_field("") == "(blank)" and len(A.fit_field("x" * 3000)) == A.FIELD_MAX


# ---------------------------------------------------------------- who may appeal when
def row(status, created=T0, decided=None):
    return {"status": status, "created_at": created, "decided_at": decided}


def problem(**kw):
    base = dict(banned=True, rows=[], banned_at=T0 - 30 * DAY, now=T0, cooling=7 * DAY, reappeal=90 * DAY)
    base.update(kw)
    return A.ingest_problem(**base)


def test_must_be_banned():
    assert problem(banned=False) == ("not_banned", 0)
    assert problem() is None


def test_one_open_appeal():
    assert problem(rows=[row(A.OPEN)]) == ("open", 0)
    assert problem(rows=[row(A.EXPIRED, decided=T0 - DAY), row(A.CLOSED), row(A.ACCEPTED, decided=T0 - DAY)]) is None


def test_cooling_off_period():
    assert problem(banned_at=T0) == ("cooling", 7)
    assert problem(banned_at=T0 - 6 * DAY - 1) == ("cooling", 1)
    assert problem(banned_at=T0 - 7 * DAY) is None
    assert problem(banned_at=None) is None  # a ban the bot never saw
    assert problem(banned_at=T0, cooling=0) is None


def test_reappeal_cooldown_after_a_denial_only():
    assert problem(rows=[row(A.DENIED, decided=T0 - 10 * DAY)]) == ("cooldown", 80)
    assert problem(rows=[row(A.DENIED, decided=T0 - 90 * DAY)]) is None
    assert problem(rows=[row(A.DENIED, decided=T0 - 200 * DAY), row(A.DENIED, decided=T0 - DAY)]) == ("cooldown", 89)
    assert problem(rows=[row(A.DENIED, created=T0 - 2 * DAY)]) == ("cooldown", 88)  # decided_at missing
    assert problem(rows=[row(A.EXPIRED, decided=T0 - DAY)]) is None  # staff never answered: not their fault
    # An open appeal wins over everything else, cooling-off over the cooldown.
    assert problem(rows=[row(A.OPEN), row(A.DENIED, decided=T0)], banned_at=T0)[0] == "open"
    assert problem(rows=[row(A.DENIED, decided=T0)], banned_at=T0)[0] == "cooling"


def test_drop_notes_are_limited_to_one_a_day_per_member_and_reason():
    notes = A.DropNotes()
    assert notes.should_note(1, "open", T0)
    assert not any(notes.should_note(1, "open", T0 + i) for i in range(1, 500))
    assert notes.should_note(1, "cooldown", T0 + 5) and notes.should_note(2, "open", T0 + 5)
    assert not notes.should_note(1, "open", T0 + DAY - 1)
    assert notes.should_note(1, "open", T0 + DAY)
    assert len(notes.last) <= 3
    for reason in ("not_banned", "open", "cooling", "cooldown"):
        assert "<@5>" in A.drop_note(reason, "<@5>", 3)
    assert "3 more days" in A.drop_note("cooling", "<@5>", 3) and "1 more day " in A.drop_note("cooldown", "x", 1)
    assert "webhook" in A.drop_note("forged", "")


def test_moves_and_staleness():
    assert A.can_move(A.OPEN, "accept") and A.can_move(A.OPEN, "deny")
    for status in (A.ACCEPTED, A.DENIED, A.EXPIRED, A.CLOSED):
        assert not A.can_move(status, "accept") and not A.can_move(status, "deny")
    assert not A.can_move(A.OPEN, "nuke")
    assert not A.is_stale(T0, T0 + A.STALE - 1) and A.is_stale(T0, T0 + A.STALE)
    assert A.EXPIRE_JOB.name == "appeals_expire"


# ---------------------------------------------------------------- text
def test_ban_notice_has_everything_needed_to_appeal():
    text = A.ban_notice(page_url="https://example.test/appeal", user_id=USER, code=CODE, banned_at=T0,
                        cooling=7 * DAY, reappeal=90 * DAY)
    assert "<https://example.test/appeal>" in text and f"`{USER}`" in text and "`TD7Y-UL66-OYCH-IZTL`" in text
    assert f"<t:{T0 + 7 * DAY}:D>" in text and "7 days" in text and "90 days" in text
    now = A.ban_notice(page_url="u", user_id=USER, code=CODE, banned_at=T0, cooling=0, reappeal=DAY)
    assert "reconsider now" in now and "<t:" not in now and "1 day." in now


def test_ban_dm_text_says_nothing_about_appeals_when_they_are_off():
    off = M.ban_dm_text("Server", "spam")
    assert off == "You were banned from **Server**.\n**Reason** spam"
    assert "ppeal" not in off
    assert M.ban_dm_text("Server", None) == "You were banned from **Server**."
    on = M.ban_dm_text("Server", "spam", "APPEAL INFO")
    assert on.endswith("\nAPPEAL INFO") and "**Reason** spam" in on


def test_can_ban_needs_staff_and_the_permission():
    assert M.can_ban(True, True)
    assert not M.can_ban(True, False) and not M.can_ban(False, True) and not M.can_ban(False, False)
    assert M.KIND_LABELS["ban"]


def test_outcome_dms():
    yes = A.outcome_dm(A.ACCEPTED, "Server", "", "https://example.test/join", 90 * DAY)
    assert "accepted" in yes and "<https://example.test/join>" in yes
    no = A.outcome_dm(A.DENIED, "Server", "Too soon.", "j", 90 * DAY)
    assert "denied" in no and "**Reason** Too soon." in no and "90 days" in no
    assert "Reason" not in A.outcome_dm(A.DENIED, "Server", "", "j", 90 * DAY)


def test_list_text():
    assert A.list_text([], [], lambda r: "") == "**Open**\nNo open appeals.\n\n**Recent decisions**\nNone yet."
    open_rows = [{"id": 3, "user_id": 30, "created_at": T0}]
    recent = [{"id": 2, "user_id": 20, "status": A.DENIED, "decided_at": T0, "decided_by": 9},
              {"id": 1, "user_id": 10, "status": A.EXPIRED, "decided_at": T0, "decided_by": None}]
    text = A.list_text(open_rows, recent, lambda r: f"https://x/{r['id']}")
    assert "`#3` <@30>" in text and "[card](https://x/3)" in text
    assert "`#2` <@20> · denied" in text and "<@9>" in text and "automatic" in text
    many = [{"id": i, "user_id": i, "created_at": T0} for i in range(1, 400)]
    long = A.list_text(many, recent, lambda r: "https://discord.com/channels/1/2/3")
    assert len(long) < 4096 and "more." in long and "Recent decisions" in long
