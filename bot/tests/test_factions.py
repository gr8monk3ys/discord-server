"""logic/factions.py: the registration rules from the SMP repo's SEASONS.md."""

import pytest

from logic import factions as F

GOOD = {"name": "The River Kings", "members": "Steve, Alex_2,  notch",
        "banner": "Blue field, a gold crown over two wavy white lines.",
        "charter": "We hold the river and trade with anyone.\n\nWe raid the Red Monkeys and nobody else."}


def test_settings_defaults_and_env():
    s = F.read_settings({}, "🚩・smp-forum")
    assert s == F.Settings(forum="🚩・smp-forum", reserved=(), season="1")
    s = F.read_settings({"FACTIONS_FORUM": " factions ", "FACTIONS_RESERVED": "Red Monkeys, ,  The  Vault ",
                         "FACTIONS_SEASON": "2"}, "x")
    assert s == F.Settings(forum="factions", reserved=("Red Monkeys", "The Vault"), season="2")


def test_a_good_entry_is_cleaned():
    entry, problem = F.read_entry(GOOD)
    assert problem is None
    assert entry.name == "The River Kings" and entry.members == ("Steve", "Alex_2", "notch")
    assert entry.charter == "We hold the river and trade with anyone. We raid the Red Monkeys and nobody else."
    assert entry.key == "theriverkings"


@pytest.mark.parametrize("name,ok", [
    ("Ab", False), ("A" * 33, False), ("Red Monkeys", True), ("Les Créateurs", True), ("Rock & Roll", True),
    ("**bold**", False), ("@everyone", False), ("-dash", False), ("a - -", False), ("O'Neil's Lot", True),
])
def test_name_rules(name, ok):
    assert (F.name_problem(F.clean_name(name)) is None) == ok


def test_champion_name_and_near_misses_are_held():
    held = ("Red Monkeys",)
    for name in ("Red Monkeys", "red monkeys", "Red Monkey", "Red Monkees", "RedMonkeyz", "Red-Monkeys!"[:-1]):
        assert "champion" in F.name_problem(name, held), name
    assert F.name_problem("Red Donkeys Two", held) is None
    assert F.name_problem("Blue Lions", held) is None


def test_distance():
    assert F.distance("kitten", "sitting") == 3
    assert F.distance("", "abc") == 3
    assert F.distance("abc", "abc") == 0


@pytest.mark.parametrize("members,problem", [
    ("", "List your members"),
    ("a1b, c2d, e3f, g4h, i5j", "at most 4"),
    ("Steve, no", "isn't a Minecraft Java name"),
    ("Steve, bad-name", "isn't a Minecraft Java name"),
    ("Steve, steve", "listed twice"),
    ("@Steve;Alex\nNotch", None),
])
def test_member_rules(members, problem):
    names = F.parse_members(members)
    found = F.members_problem(names)
    assert (found is None) if problem is None else (problem in found)


def test_lengths():
    _, problem = F.read_entry({**GOOD, "banner": "red"})
    assert "banner description" in problem
    _, problem = F.read_entry({**GOOD, "charter": "short"})
    assert "charter" in problem
    assert F.charter_problem("x" * (F.CHARTER_LEN[1] + 1)).startswith("The charter must be")


def test_clash_with_other_registrations():
    entry, _ = F.read_entry(GOOD)
    other = {"name": "The River Kings", "name_key": "theriverkings", "members": '["Herobrine"]'}
    assert "already a faction called" in F.clash(entry, [other])
    other = {"name": "Blue Lions", "name_key": "bluelions", "members": '["herobrine", "STEVE"]'}
    assert "`steve` is already on **Blue Lions**" in F.clash(entry, [other])
    other = {"name": "Blue Lions", "name_key": "bluelions", "members": '["herobrine"]'}
    assert F.clash(entry, [other]) is None


def test_members_round_trip_and_bad_json():
    assert F.load_members(F.dump_members(("a", "b"))) == ["a", "b"]
    assert F.load_members("nope") == [] and F.load_members('{"a": 1}') == []
