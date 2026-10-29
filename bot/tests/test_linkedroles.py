"""Linked Roles metadata: the shared definitions file and the registration plan."""

import copy
import json
import re

import pytest

from logic import linkedroles


def defs():
    return copy.deepcopy(linkedroles.load())


# ------------------------------------------------------------ the real file
def test_the_shipped_definitions_are_valid_and_small():
    d = linkedroles.load()
    assert linkedroles.problems(d) == []
    assert 1 <= len(d["records"]) <= linkedroles.MAX_RECORDS
    assert d["platform_name"] == "Front Desk"
    assert linkedroles.valid_url(d["verification_url"])


def test_records_sent_to_discord_carry_only_discords_fields():
    sent = linkedroles.records(linkedroles.load())
    assert all(set(r) == {"type", "key", "name", "description"} for r in sent)
    assert [r["key"] for r in sent] == ["steam", "xbox", "playstation", "epicgames", "platforms"]


def test_platform_flags_are_booleans_and_the_count_is_at_least_n():
    d = linkedroles.load()
    assert linkedroles.services(d) == {
        "steam": "steam",
        "xbox": "xbox",
        "playstation": "playstation",
        "epicgames": "epicgames",
    }
    by_key = {r["key"]: r for r in d["records"]}
    assert all(by_key[k]["type"] == linkedroles.BOOLEAN_EQUAL for k in linkedroles.services(d))
    assert by_key["platforms"]["type"] == linkedroles.INTEGER_GREATER_THAN_OR_EQUAL


def test_the_functions_read_the_same_file_and_use_the_same_paths():
    """site/src/server/linked-role/lib.ts imports metadata.json; nothing is defined twice."""
    folder = linkedroles.DEFINITIONS.parent
    code = "".join((folder / name).read_text(encoding="utf-8") for name in ("lib.ts", "flow.ts"))
    assert 'import definitions from "./metadata.json";' in code
    # The TypeScript never spells a metadata key or platform name itself.
    d = linkedroles.load()
    for key in linkedroles.services(d):
        assert not re.search(rf"""["']{key}["']""", code), key
    assert linkedroles.START_PATH == "/api/linked-role/start"
    routes = folder.parents[1] / "app" / "api" / "linked-role"
    assert (routes / "start" / "route.ts").exists() and (routes / "callback" / "route.ts").exists()
    assert 'CALLBACK_PATH = "/api/linked-role/callback"' in code


# ------------------------------------------------------------ validation
@pytest.mark.parametrize(
    "edit, expected",
    [
        (lambda d: d.update(platform_name=""), "platform_name"),
        (lambda d: d.update(platform_name="x" * 51), "platform_name"),
        (lambda d: d.update(verification_url="http://example.com/api/linked-role/start"), "verification_url"),
        (lambda d: d.update(records=[]), "records"),
        (lambda d: d["records"].append(dict(d["records"][0], key="sixth")), "records"),
        (lambda d: d["records"][0].update(key="Steam"), "key must be"),
        (lambda d: d["records"][1].update(key="steam"), "duplicate key"),
        (lambda d: d["records"][0].update(name=""), "name must be"),
        (lambda d: d["records"][0].update(description="x" * 201), "description must be"),
        (lambda d: d["records"][0].update(type=9), "type must be"),
        (lambda d: d["records"][0].update(type=True), "type must be"),
        (lambda d: d["records"][0].update(type=2), "platform flag must be"),
        (lambda d: d["records"][0].update(service="Not A Service"), "not a Discord connection type"),
        (lambda d: d["records"][0].pop("service"), "exactly one of"),
        (lambda d: d["records"][0].update(count=True), "exactly one of"),
        (lambda d: d["records"][4].update(type=7), "the count must be"),
        (lambda d: d["records"][0].update(username="someone"), "unknown field username"),
        (lambda d: d["records"].__setitem__(0, "steam"), "not an object"),
    ],
)
def test_problems_names_what_is_wrong(edit, expected):
    d = defs()
    edit(d)
    assert any(expected in p for p in linkedroles.problems(d)), linkedroles.problems(d)


def test_problems_on_something_that_is_not_an_object():
    assert linkedroles.problems([]) == ["the file is not a JSON object"]


def test_load_refuses_a_bad_file(tmp_path):
    d = defs()
    d["records"][0]["key"] = "BAD KEY"
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(ValueError, match="key must be"):
        linkedroles.load(path)


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://example.com/api/linked-role/start", True),
        ("http://example.com/api/linked-role/start", False),
        ("https://example.com/api/linked-role/start?x=1", False),
        ("https://example.com/api/linked-role/start#x", False),
        ("https://example.com/api/linked-role/callback", False),
        ("https://user@example.com/api/linked-role/start", False),
        ("https://example.com:8443/api/linked-role/start", False),
        ("https://example.com:port/api/linked-role/start", False),
        ("https://example.com/api/linked-role/start ", False),
        ("https:///api/linked-role/start", False),
        ("", False),
        (None, False),
    ],
)
def test_valid_url(url, ok):
    assert linkedroles.valid_url(url) is ok


# ------------------------------------------------------------ plan
def test_plan_for_a_fresh_application_adds_everything():
    d = linkedroles.load()
    p = linkedroles.plan([], None, d)
    assert p.put_records == linkedroles.records(d)
    assert p.set_url == d["verification_url"]
    assert p.changes
    assert [line.split()[0] for line in p.lines] == ["add"] * 5 + ["set"]
    assert "not set" in p.lines[-1]


def test_plan_is_empty_when_discord_already_matches():
    d = linkedroles.load()
    # Discord may send fields we do not manage (localizations): they are not a difference.
    current = [dict(r, name_localizations=None) for r in linkedroles.records(d)]
    p = linkedroles.plan(current, d["verification_url"], d)
    assert not p.changes
    assert p.put_records is None and p.set_url is None
    assert all(line.startswith("  ok") for line in p.lines)


def test_plan_shows_changed_removed_and_reordered_records():
    d = linkedroles.load()
    current = linkedroles.records(d)
    current[0]["description"] = "old words"
    current.append({"type": 7, "key": "legacy", "name": "Legacy", "description": "gone"})
    p = linkedroles.plan(current, d["verification_url"], d)
    assert p.put_records == linkedroles.records(d)
    assert p.set_url is None
    text = "\n".join(p.lines)
    assert "change  steam: description 'old words' ->" in text
    assert "remove  legacy" in text

    swapped = linkedroles.records(d)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    p = linkedroles.plan(swapped, d["verification_url"], d)
    assert p.put_records == linkedroles.records(d)
    assert p.lines[-2] == "  reorder records to match metadata.json"


def test_plan_takes_a_url_override_and_refuses_a_bad_one():
    d = linkedroles.load()
    other = "https://preview.example.com/api/linked-role/start"
    p = linkedroles.plan(linkedroles.records(d), d["verification_url"], d, other)
    assert p.set_url == other and p.put_records is None
    with pytest.raises(ValueError, match="https URL"):
        linkedroles.plan([], None, d, "https://example.com/elsewhere")


def test_plan_survives_odd_replies_and_keeps_them_on_one_line():
    d = linkedroles.load()
    p = linkedroles.plan({"message": "401: Unauthorized"}, 5, d)
    assert p.put_records == linkedroles.records(d)

    nasty = [{"type": 7, "key": "steam", "name": "x\n\x1b[31mred" + "y" * 500, "description": None}, "junk", {"key": 3}]
    p = linkedroles.plan(nasty, "https://old.example.com/\nline", d)
    for line in p.lines:
        assert "\n" not in line and "\x1b" not in line
        assert len(line) < 400
