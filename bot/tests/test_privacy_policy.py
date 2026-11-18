"""Every table the bot creates is covered by PRIVACY.md: a new table fails here until the policy
says what it stores and how long it is kept."""

import asyncio
from pathlib import Path

import db as dbmod

PRIVACY = Path(__file__).resolve().parents[2] / "PRIVACY.md"
NO_MEMBER_DATA = "no member data"
STORES = "What Front Desk stores"

# table -> the PRIVACY.md section (a "## " heading or a "**Lead.**" paragraph) that covers it.
COVERED_BY = {
    "meta": STORES,
    "jobs": NO_MEMBER_DATA,
    "privacy_optout": STORES,
    "lfg_posts": STORES,
    "lfg_members": STORES,
    "voice_sessions": STORES,
    "game_sessions": STORES,
    "message_counts": STORES,
    "invite_uses": STORES,
    "joins": STORES,
    "bumps": STORES,
    "welcomed": STORES,
    "reports": STORES,
    "starboard": STORES,
    "clips": STORES,
    "clip_polls": STORES,
    "temp_voice": STORES,
    "gamenights": STORES,
    "free_games": NO_MEMBER_DATA,
    "wallets": STORES,
    "ledger": STORES,
    "predictions": STORES,
    "prediction_bets": STORES,
    "blackjack_open": STORES,
    "perks": STORES,
    "seasons": STORES,
    "qotd_used": NO_MEMBER_DATA,
    "poll_used": NO_MEMBER_DATA,
    "counting": STORES,
    "counting_run": STORES,
    "birthdays": STORES,
    "cases": STORES,
    "reminders": STORES,
    "afk": STORES,
    "tickets": STORES,
    "tournaments": STORES,
    "tournament_entries": STORES,
    "tournament_matches": STORES,
    "achievements": STORES,
    "creators": STORES,
    "creator_seen": NO_MEMBER_DATA,
    "xp": STORES,
    "partners": STORES,
    "quest_steps": STORES,
    "creator_pending": STORES,
    "challenge_claims": STORES,
    "word_games": STORES,
    "mm_queue": STORES,
    "mm_matches": STORES,
    "news_seen": NO_MEMBER_DATA,
    "staff_apps": STORES,
    "event_drops": STORES,
    "event_claims": STORES,
    "game_accounts": STORES,
    "game_account_pending": STORES,
    "catchup_usage": STORES,
    "house_members": "Houses",
    "house_results": "Houses",
    "crew_arrivals": "Welcome Crew",
    "crew_greetings": "Welcome Crew",
    "supporters": "Supporter tier",
    "forum_posts": "Guides and setups",
    "hosted_events": STORES,
    "clipcast": "Clipcast (YouTube)",
    "clipcast_prefs": "Clipcast (YouTube)",
    "monkey_links": "Monkey SMP (Minecraft)",
    "monkey_playtime": "Monkey SMP (Minecraft)",
    "monkey_nudges": "Monkey SMP (Minecraft)",
    "appeals": "Bans and appeals",
    "appeal_bans": "Bans and appeals",
    "factions": STORES,
    "tebex_webhooks": STORES,
    "faction_recruit": "Recruitment board and referrals",
    "recruit_requests": "Recruitment board and referrals",
    "smp_seekers": "Recruitment board and referrals",
    "referral_codes": "Recruitment board and referrals",
    "referral_claims": "Recruitment board and referrals",
}


def schema_tables() -> set[str]:
    async def go():
        d = dbmod.Database(":memory:")
        await d.connect()
        try:
            await d.migrate()
            rows = await d.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
        finally:
            await d.close()
        return {r["name"] for r in rows if not r["name"].startswith("sqlite_")}
    return asyncio.run(go())


def test_every_table_is_covered_by_the_privacy_policy():
    tables = schema_tables()
    missing = sorted(tables - COVERED_BY.keys())
    assert not missing, [f"table {t} is new: say in PRIVACY.md what it stores and how long it is kept, "
                         f"then add it to COVERED_BY in {Path(__file__).name} (or map it to NO_MEMBER_DATA)"
                         for t in missing]
    gone = sorted(COVERED_BY.keys() - tables)
    assert not gone, f"no longer in the schema, remove from COVERED_BY: {gone}"


def test_every_mapped_section_exists_in_the_privacy_policy():
    text = PRIVACY.read_text(encoding="utf-8")
    sections = set(COVERED_BY.values()) - {NO_MEMBER_DATA}
    absent = sorted(s for s in sections if f"## {s}\n" not in text and f"**{s}.**" not in text)
    assert not absent, f"PRIVACY.md has no section named {absent}: fix the name in COVERED_BY or the policy"
