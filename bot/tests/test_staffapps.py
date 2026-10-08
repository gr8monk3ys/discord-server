"""Pure rules for staff applications (logic/staffapps.py)."""

import json

import pytest

from logic import staffapps as S

DAY = S.DAY
NOW = 1_790_000_000


def row(status="pending", created=NOW - 10 * DAY, decided=None, **kw):
    r = {"id": kw.pop("id", 1), "status": status, "created_at": created, "decided_at": decided}
    r.update(kw)
    return r


def ok_member(**kw):
    base = dict(joined_at=NOW - 40 * DAY, created_at=NOW - 400 * DAY, timed_out=False,
                recent_cases=0, is_staff=False)
    base.update(kw)
    return base


# ---------------------------------------------------------------- questions
def test_five_questions_with_short_labels():
    assert len(S.QUESTIONS) == 5
    keys = [q.key for q in S.QUESTIONS]
    assert keys == ["age", "timezone", "experience", "voice", "why"]
    for q in S.QUESTIONS:
        assert len(q.label) <= 45  # Discord's limit for modal labels
        assert len(q.description) <= 100
        assert 1 <= q.max_length <= 4000


@pytest.mark.parametrize("text", ["yes", "Yes", " YES ", "yes.", "yes!", "Yes, I am"[:3]])
def test_age_confirmed(text):
    assert S.age_confirmed(text)


@pytest.mark.parametrize("text", ["", "no", "y", "yeah", "I'm 17", "yes I'm 16", "nope yes"])
def test_age_not_confirmed(text):
    assert not S.age_confirmed(text)


def test_clean_answer_trims_and_caps_blank_lines():
    assert S.clean_answer("  hi  ") == "hi"
    assert S.clean_answer("a\n\n\n\nb") == "a\n\nb"
    assert S.clean_answer(None) == ""
    assert S.clean_answer("x" * 5000, 100) == "x" * 100


def test_answers_problem():
    good = {"age": "yes", "timezone": "PST, evenings", "experience": "Modded a 200-person server for a year.",
            "voice": "Move them to separate channels and calm things down.", "why": "I like this place."}
    assert S.answers_problem(good) is None
    assert S.answers_problem({**good, "age": "no"}) == "age"
    assert S.answers_problem({**good, "why": "   "}) == "blank"
    assert S.answers_problem({k: v for k, v in good.items() if k != "voice"}) == "blank"


def test_answers_round_trip_json():
    answers = {"age": "yes", "timezone": "EU", "experience": "none", "voice": "calm", "why": "fun"}
    assert S.load_answers(S.dump_answers(answers)) == answers
    assert S.load_answers("not json") == {}
    assert S.load_answers(json.dumps([1, 2])) == {}


# ---------------------------------------------------------------- eligibility
def test_eligible_member():
    assert S.member_problem(**ok_member(), now=NOW) is None


def test_staff_cant_apply():
    assert S.member_problem(**ok_member(is_staff=True), now=NOW) == ("staff", 0)


def test_member_too_new():
    assert S.member_problem(**ok_member(joined_at=NOW - 10 * DAY), now=NOW) == ("member_age", 20)
    assert S.member_problem(**ok_member(joined_at=None), now=NOW) == ("member_age", S.MIN_MEMBER_DAYS)
    # exactly 30 days is enough
    assert S.member_problem(**ok_member(joined_at=NOW - 30 * DAY), now=NOW) is None


def test_account_too_new():
    assert S.member_problem(**ok_member(created_at=NOW - 89 * DAY), now=NOW) == ("account_age", 1)
    assert S.member_problem(**ok_member(created_at=NOW - 90 * DAY), now=NOW) is None


def test_timed_out_or_recent_cases():
    assert S.member_problem(**ok_member(timed_out=True), now=NOW) == ("timed_out", 0)
    assert S.member_problem(**ok_member(recent_cases=1), now=NOW) == ("record", 0)


def test_problem_order_staff_first():
    assert S.member_problem(**ok_member(is_staff=True, joined_at=NOW, timed_out=True), now=NOW)[0] == "staff"


def test_history_one_open_at_a_time():
    assert S.history_problem([row("pending")], NOW) == ("open", 0)
    assert S.history_problem([row("interview")], NOW) == ("open", 0)
    assert S.history_problem([row("approved")], NOW) is None
    assert S.history_problem([], NOW) is None


def test_history_cooldown_after_denial():
    assert S.history_problem([row("denied", decided=NOW - 10 * DAY)], NOW) == ("cooldown", 50)
    assert S.history_problem([row("denied", decided=NOW - 60 * DAY)], NOW) is None
    # partial days round up
    assert S.history_problem([row("denied", decided=NOW - 60 * DAY + 1)], NOW) == ("cooldown", 1)
    # decided_at missing falls back to created_at
    assert S.history_problem([row("denied", created=NOW - DAY, decided=None)], NOW) == ("cooldown", 59)
    # latest denial counts
    rows = [row("denied", decided=NOW - 100 * DAY), row("denied", decided=NOW - 5 * DAY)]
    assert S.history_problem(rows, NOW) == ("cooldown", 55)


def test_reply_texts():
    for code in S.REPLIES:
        assert S.reply(code, 3)
    assert "20 more days" in S.reply("member_age", 20)
    assert "1 more day" in S.reply("account_age", 1)
    assert "59 days" in S.reply("cooldown", 59)


# ---------------------------------------------------------------- who may press what
def test_can_decide_keeper_or_owner_only():
    assert S.can_decide(is_owner=True, role_names=[])
    assert S.can_decide(is_owner=False, role_names=["Keeper"])
    assert not S.can_decide(is_owner=False, role_names=["Moderator"])
    assert not S.can_decide(is_owner=False, role_names=[])


def test_can_interview_moderators_too():
    assert S.can_interview(is_owner=False, role_names=["Moderator"])
    assert S.can_interview(is_owner=False, role_names=["Keeper"])
    assert S.can_interview(is_owner=True, role_names=[])
    assert not S.can_interview(is_owner=False, role_names=["Squad"])


def test_allowed_per_action():
    assert S.allowed("interview", is_owner=False, role_names=["Moderator"])
    assert not S.allowed("approve", is_owner=False, role_names=["Moderator"])
    assert not S.allowed("deny", is_owner=False, role_names=["Moderator"])
    assert S.allowed("deny", is_owner=False, role_names=["Keeper"])
    assert not S.allowed("nuke", is_owner=True, role_names=[])


def test_transitions():
    assert S.can_move("pending", "interview")
    assert S.can_move("pending", "approve")
    assert S.can_move("interview", "approve")
    assert S.can_move("interview", "deny")
    assert not S.can_move("interview", "interview")
    assert not S.can_move("approved", "deny")
    assert not S.can_move("denied", "approve")
    assert S.target("approve") == "approved" and S.target("deny") == "denied" and S.target("interview") == "interview"


def test_role_grantable():
    assert S.role_grantable(role_position=5, bot_top=10) is None
    assert S.role_grantable(role_position=10, bot_top=10) == "above_bot"
    assert S.role_grantable(role_position=12, bot_top=10) == "above_bot"
    assert S.role_grantable(role_position=None, bot_top=10) == "no_role"


# ---------------------------------------------------------------- text
def test_days_ago():
    assert S.days_ago(NOW - 3 * DAY, NOW) == "3 days"
    assert S.days_ago(NOW - DAY, NOW) == "1 day"
    assert S.days_ago(None, NOW) == "unknown"


def test_status_text_none_and_open():
    assert "haven't applied" in S.status_text(None, NOW)
    t = S.status_text(row("pending", id=4), NOW)
    assert "#4" in t and "pending" in t
    assert "interview" in S.status_text(row("interview"), NOW)


def test_status_text_denied_shows_cooldown():
    t = S.status_text(row("denied", decided=NOW - 10 * DAY), NOW)
    assert "50 days" in t
    t = S.status_text(row("denied", decided=NOW - 70 * DAY), NOW)
    assert "apply again" in t


def test_status_text_approved():
    assert "approved" in S.status_text(row("approved", decided=NOW), NOW)


def test_dm_texts_name_the_guild():
    for kind in ("interview", "approved", "denied"):
        assert "My Server" in S.dm_text(kind, "My Server")


def test_list_lines():
    rows = [row("pending", id=1, user_id=5, created=NOW - DAY), row("interview", id=2, user_id=6, created=NOW)]
    text = S.list_lines(rows, lambda r: f"link{r['id']}")
    assert "#1" in text and "<@5>" in text and "link1" in text and "interview" in text.lower()
    assert S.list_lines([], lambda r: "") == "No open applications."


def test_list_lines_capped():
    rows = [row("pending", id=i, user_id=10**17 + i) for i in range(1, 200)]
    text = S.list_lines(rows, lambda r: "https://discord.com/channels/1/2/" + "3" * 18)
    assert len(text) <= 4000
    assert "more" in text


def test_fit_field():
    assert S.fit_field("") == "(blank)"
    assert len(S.fit_field("x" * 3000)) <= 1024
    assert S.fit_field("x" * 3000).endswith("…")
