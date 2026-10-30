"""logic/recruit.py: the recruitment board's rules."""

from logic import recruit as RC


def esc(text):
    return text.replace("*", "\\*").replace("@", "@​")


def test_off_unless_a_forum_is_named():
    assert not RC.read_settings({}).on
    assert not RC.read_settings({"RECRUIT_FORUM": "   "}).on
    s = RC.read_settings({"RECRUIT_FORUM": " 📯・recruiting "})
    assert s.on and s.forum == "📯・recruiting"


def test_tags_come_from_the_list_in_any_case_without_repeats():
    assert RC.parse_tags("Builders, PVP; builders,, chill") == (["builders", "pvp", "chill"], None)
    assert RC.parse_tags("") == ([], None)
    tags, problem = RC.parse_tags("builders, griefers")
    assert tags == [] and "`griefers` isn't one" in problem
    tags, problem = RC.parse_tags("builders, pvp, raiders, traders, farmers")
    assert tags == [] and "up to 4" in problem


def test_read_pitch_squashes_and_checks_lengths():
    entry, problem = RC.read_pitch({"pitch": "  River   traders\nwith a farm.  ", "tags": "chill", "timezone": "US Pacific"})
    assert problem is None
    assert entry == RC.Pitch(pitch="River traders with a farm.", tags=("chill",), timezone="US Pacific")
    assert "pitch must be" in RC.read_pitch({"pitch": "short", "timezone": "UTC"})[1]
    assert "timezone must be" in RC.read_pitch({"pitch": "A long enough pitch.", "timezone": "x"})[1]
    assert "isn't one" in RC.read_pitch({"pitch": "A long enough pitch.", "timezone": "UTC", "tags": "nope"})[1]


def test_tags_round_trip():
    assert RC.load_tags(RC.dump_tags(("builders", "night owls"))) == ["builders", "night owls"]
    assert RC.load_tags("") == [] and RC.load_tags(None) == []


def test_seeker_note_and_timezone():
    assert RC.read_seeker("  likes   redstone ", " EU ") == (("likes redstone", "EU"), None)
    assert RC.read_seeker("", "") == (("", ""), None)
    assert "note must be" in RC.read_seeker("x" * 201, "")[1]
    assert "timezone must be" in RC.read_seeker("", "x")[1]


def test_only_an_approved_faction_with_room_can_open():
    assert RC.can_open("approved", ["a", "b"]) is None
    assert "approved by staff" in RC.can_open("pending", ["a"])
    assert "roster is full" in RC.can_open("approved", ["a", "b", "c", "d"])


def test_join_request_rules():
    ok = dict(recruiting=True, is_leader=False, on_a_roster=False, last_request_at=None, now=100_000)
    assert RC.request_problem(**ok) is None
    assert "your own" in RC.request_problem(**{**ok, "is_leader": True})
    assert "isn't recruiting" in RC.request_problem(**{**ok, "recruiting": False})
    assert "already on a faction" in RC.request_problem(**{**ok, "on_a_roster": True})
    assert "already asked" in RC.request_problem(**{**ok, "last_request_at": 100_000 - 3600})
    assert RC.request_problem(**{**ok, "last_request_at": 100_000 - RC.REQUEST_COOLDOWN}) is None


def test_seekers_expire_and_who_may_browse():
    assert RC.seeker_fresh(0, RC.SEEKER_DAYS * 86400)
    assert not RC.seeker_fresh(0, RC.SEEKER_DAYS * 86400 + 1)
    assert RC.can_browse(is_staff=True, leads_approved=False)
    assert RC.can_browse(is_staff=False, leads_approved=True)
    assert not RC.can_browse(is_staff=False, leads_approved=False)


def test_texts_escape_member_text():
    dm = RC.request_dm("<@5>", "Alex_2", "I *love* @everyone", "EU", "The **Kings**", "https://x/1", esc)
    assert "<@5> (Minecraft: `Alex_2`)" in dm and "\\*love\\*" in dm and "@​everyone" in dm
    assert "The \\*\\*Kings\\*\\*" in dm and "https://x/1" in dm
    assert "Minecraft" not in RC.request_dm("<@5>", None, "", "", "K", None, esc)
    line = RC.seeker_line("<@5>", "Alex_2", "builds *big*", "EU", 1000, esc)
    assert line.startswith("<@5> · `Alex_2` · EU · since <t:1000:d>") and "\\*big\\*" in line
    assert RC.status_line(True, ["a"]) == "📯 Recruiting · 1 of 4 places taken"
    assert RC.status_line(False, ["a", "b"]).startswith("Not recruiting")
