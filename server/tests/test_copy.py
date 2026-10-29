"""The words the server shows to strangers: Discord's length limits, and the positioning
(a small co-op and PvE server) in the description, the welcome screen, the welcome post and
docs/LISTING.md. No Discord, no network."""

import re
from pathlib import Path

import layout

ROOT = Path(__file__).resolve().parents[2]
LISTING = (ROOT / "docs" / "LISTING.md").read_text(encoding="utf-8")

RETIRED = ("Valorant", "Call of Duty", "CoD", "Fortnite", "League of Legends", "Counter-Strike", "CS2",
           "GTA", "Roblox")
HOUSES = ("Fern", "Kestrel", "Otter")


def blocks(text):
    """The fenced copy-paste blocks of the listing kit, in order: short, then long."""
    return [b.strip("\n") for b in re.findall(r"```\n(.*?)```", text, re.S)]


def clean(text, where):
    for gone in RETIRED:
        assert gone not in text, f"{gone} in {where}"
    assert "—" not in text, f"em dash in {where}"
    assert "18+" not in text, f"18+ in {where}"


def test_server_description_fits_discovery_and_names_what_is_played():
    text = layout.PUBLIC["description"]
    assert len(text) <= 120
    assert "co-op and PvE" in text and "Risk of Rain 2" in text and "Pacific" in text
    clean(text, "the server description")


def test_welcome_screen_text_fits_discords_limit():
    text = layout.WELCOME_SCREEN["description"]
    assert len(text) <= 140
    assert "co-op and PvE" in text and "Lobby" in text
    clean(text, "the welcome screen")


def test_welcome_post_says_what_a_new_member_does():
    post = layout.POSTS["welcome"]
    text = post["description"]
    assert len(text) <= 4096 and len(post["title"]) <= 256  # Discord's embed limits
    for want in ("co-op and PvE", "US Pacific", "{#rules}", "{#general}", "Lobby", "Monkey SMP", "{#help}", *HOUSES):
        assert want in text, want
    clean(text, "the welcome post")
    # only placeholders setup_server.expand() knows how to fill, for roles that exist
    roles = {r["name"] for r in layout.ROLES}
    assert set(re.findall(r"\{@([^}]+)\}", text)) <= roles


def test_listing_short_description_is_the_server_description():
    short, long = blocks(LISTING)[:2]
    assert short == layout.PUBLIC["description"]
    stated = re.search(r"## Short description \((\d+) characters", LISTING)
    assert stated and int(stated.group(1)) == len(short) <= 120
    clean(short, "the short listing description")
    clean(long, "the long listing description")


def test_listing_long_description_fits_discord_me():
    long = blocks(LISTING)[1]
    assert len(long) <= 1025  # discord.me's description field, the tightest of the sites
    stated = re.search(r"## Long description \((\d[\d,]*) characters", LISTING)
    assert stated and int(stated.group(1).replace(",", "")) == len(long)
    for want in ("co-op and PvE", "Risk of Rain 2", "Gears of War", "Monkey SMP", "US Pacific", *HOUSES,
                 "https://discord.lscaturchio.xyz/"):
        assert want in long, want


def test_listing_tags_match_the_games_played_here():
    tags = re.findall(r"^\d+\. `([^`]+)`$", LISTING, re.M)
    assert len(tags) == 10 and len(set(tags)) == 10
    assert tags[:2] == ["gaming", "co-op"]
    every = " ".join(re.findall(r"`([^`]+)`", LISTING))
    for gone in ("valorant", "fortnite", "call of duty", "tournaments"):
        assert gone not in every, gone
    assert "18+" not in tags and "nsfw" not in tags
