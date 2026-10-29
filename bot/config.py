"""Settings and server names. Reads server/.env and server/layout.py so the bot
and the setup scripts always agree on games, roles and channels."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent
SERVER_DIR = BOT_DIR.parent / "server"
DATA_DIR = BOT_DIR / "data"

sys.path.insert(0, str(SERVER_DIR))
import layout  # noqa: E402
from names import slug  # noqa: E402


def layout_old_names(name: str) -> tuple[str, ...]:
    """The "was" names layout.py lists for a category or channel: what it is still called
    until public_mode.py renames it in place."""
    for cat in layout.CATEGORIES:
        for spec in (cat, *cat["channels"]):
            if spec["name"] == name:
                return tuple(spec.get("was", ()))
    return ()

# Channel and role names, as they appear in layout.py.
LFG_FORUM = "🎮・lfg"
LFG_ROLE = "LFG"
KEEPER_ROLE = "Keeper"
MOD_CHANNEL = "🛡️・mod"
GENERAL_CHANNEL = "💬・general"
STAFF_CATEGORY = "00 · staff"  # messages here are never counted
MOD_LOG_CHANNEL = "📋・mod-log"
BOT_COMMANDS_CHANNEL = "🤖・bot-commands"  # bump reminders go here
RULES_CHANNEL = "📌・rules"
MOD_ROLE = "Moderator"
SQUAD_ROLE = "Squad"
BUMPER_ROLE = "Bumper"  # opt-in: pinged when the Disboard bump is ready
RECRUITER_ROLE = "Recruiter"  # given to members whose invites brought in people who stayed
DISBOARD_BOT_ID = 302050872383242240
HALL_OF_FAME_CHANNEL = "⭐・hall-of-fame"
CLIPS_CHANNEL = "📸・clips"
GAMING_CHANNEL = "🕹️・gaming"  # free-games posts go here
ANNOUNCEMENTS_CHANNEL = "📣・announcements"
CLIP_ROLE = "Clip of the Week"
VOICE_CATEGORY = "05 · voice"
NEW_SQUAD_VOICE = "➕ New Squad"  # join to get your own temporary voice channel
HYPE_ROLE = "Hype"  # shop: 24 h hoisted role
SEASON_ROLE = "Season Champ"  # top 3 of the last finished season
# Phase 3 (autopilot)
COUNTING_CHANNEL = "🔢・counting"
SUGGESTIONS_FORUM = "💡・suggestions"
HELP_CHANNEL = "🆘・help"  # the "Contact the mods" ticket button lives here
BIRTHDAY_ROLE = "Birthday"
COUNTING_ROLE = "Counting Champ"
BACKUP_DIR = r"D:\Backups\front-desk"  # bulk data lives on D: (see fleet conventions)
# Growth features
WELCOME_CHANNEL = "👋・welcome"  # welcome cards go here
TOURNAMENTS_CHANNEL = "🏆・tournaments"
CREATORS_CHANNEL = "📺・creators"  # live / new-upload announcements
TOURNEY_ROLE = "Tournament Champ"
# Wave 2
ROLES_CHANNEL = "🎭・roles"  # self-assign role panel
PARTNERS_CHANNEL = "🤝・partners"  # approved partner servers
GAMENIGHT_ROLE = "Game Night"  # opt-in: pinged when a game night starts
LEVEL_ROLES = [(5, "Regular"), (10, "Veteran"), (20, "Elite"), (30, "Legend"), (50, "Mythic")]
PLATFORM_ROLES = ["PC", "PlayStation", "Xbox", "Switch", "Mobile"]
REGION_ROLES = ["NA", "EU", "LATAM", "Asia", "OCE"]
PLAYTIME_ROLES = ["Early Bird", "Night Owl", "Weekend Warrior"]
HEARTBEAT_FILE = DATA_DIR / "heartbeat"  # written every minute; server/watchdog.py reads it
# Wave 3
EVENT_ROLE = "Event Champ"  # winner of the current seasonal event
GAMES_CHANNEL = "🎲・games"  # daily word game results, challenge board
SQUAD_VOICE = "🎮 Squad"
LOBBY_VOICE = "🔊 Lobby"
MODES = ("Ranked", "Casual")
# Houses: (key, name, emoji, colour) and the role each house carries, from layout.py
HOUSES = layout.HOUSES
HOUSE_ROLES = layout.HOUSE_ROLES
# Wave 9: Welcome Crew and Supporter tier
CREW_ROLE = "Welcome Crew"  # opt-in with a bar (/crew): pinged when someone new arrives
SUPPORTER_ROLE = "Supporter"  # given by Ko-fi's Discord bot or Server Subscriptions; cosmetic perks
SUPPORTERS_CHANNEL = "💜・supporters"  # private lounge for the Supporter role
# Forums and Stage events
GUIDES_FORUM = "📚・guides"
SETUPS_FORUM = "🖥・setups"
STAGE_CHANNEL = "🎙・stage"  # watch parties, the monthly Town Hall
# Monkey SMP (Minecraft bridge): status panel and two-way chat live in this channel.
REALMS_CHANNEL = "⛏️・monkey-smp"
# Its old names: until public_mode.py renames the channel in place, the bridge keeps using it.
REALMS_CHANNEL_WAS = layout_old_names(REALMS_CHANNEL)
REALMS_ENV = ("REALMS_ADDRESS", "REALMS_RCON_HOST", "REALMS_RCON_PORT", "REALMS_RCON_PASSWORD", "REALMS_LOG_PATH")
# SMP watch (cogs/smpwatch.py): opt-in, pinged in the realms channel when 3+ are on Monkey SMP.
SMP_PING_ROLE = "SMP Ping"


@dataclass(frozen=True)
class Game:
    emoji: str
    channel: str
    role: str

    @property
    def key(self) -> str:
        """Stable id stored in the database: the role slug ('counterstrike2')."""
        return slug(self.role)

    @property
    def channel_name(self) -> str:
        return f"{self.emoji}・{self.channel}"


GAMES = [Game(*g) for g in layout.GAMES]
# Games the server used to list (layout.RETIRED_GAMES). Never offered: no choice, role panel
# entry, news feed or queue. Kept so old rows that name one still show its emoji and name.
RETIRED_GAMES = [Game(*g) for g in layout.RETIRED_GAMES]


def game_by_key(key: str) -> Game | None:
    """A game the server lists today. Use this for anything a member can start."""
    return next((g for g in GAMES if g.key == key), None)


def known_game_by_key(key: str) -> Game | None:
    """A listed or retired game, for showing rows written while the game was still listed."""
    return game_by_key(key) or next((g for g in RETIRED_GAMES if g.key == key), None)


def match_by_name(items, name: str):
    """First item whose .name matches `name`, ignoring emoji, case and separators."""
    target = slug(name)
    return next((i for i in items if slug(i.name) == target), None)


def match_by_names(items, names):
    """match_by_name for each name in turn: the first name that matches wins."""
    return next((found for name in names if (found := match_by_name(items, name)) is not None), None)


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: int
    tz: ZoneInfo


def load_settings() -> Settings:
    load_dotenv(SERVER_DIR / ".env")
    missing = [k for k in ("DISCORD_TOKEN", "GUILD_ID") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)} in {SERVER_DIR / '.env'} (see .env.example).")
    return Settings(
        token=os.environ["DISCORD_TOKEN"],
        guild_id=int(os.environ["GUILD_ID"]),
        tz=ZoneInfo(os.environ.get("TZ", "America/Los_Angeles")),
    )


def crew_env() -> tuple[str, str]:
    """Optional Welcome Crew settings, raw (logic/crew.py validates them and has the defaults):
    CREW_PING_MINUTES (gap between crew pings) and CREW_QUIET_HOURS (local hours, like "22-9")."""
    return os.environ.get("CREW_PING_MINUTES", ""), os.environ.get("CREW_QUIET_HOURS", "")


def support_url_env() -> str:
    """SUPPORT_URL, raw (logic/supporter.py validates it). Unset: /support says it isn't open."""
    return os.environ.get("SUPPORT_URL", "")
# Clipcast (cogs/clipcast.py): optional YouTube upload settings. All three credentials unset
# means the feature posts nothing itself and lists the week's approved clips for staff.
CLIPCAST_ENV = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_PRIVACY")


def clipcast_env(environ=None) -> dict[str, str]:
    """The clipcast settings from the environment (or a mapping, in tests); missing ones are ''."""
    environ = os.environ if environ is None else environ
    return {key: str(environ.get(key) or "").strip() for key in CLIPCAST_ENV}
def realms_environ(environ=None) -> dict[str, str]:
    """The Minecraft bridge's settings (REALMS_ENV) as they are in the environment; server/.env
    was loaded by load_settings(). logic/realms.py validates them; all empty means the bridge is off."""
    environ = os.environ if environ is None else environ
    return {name: environ.get(name) or "" for name in REALMS_ENV}
# Live site data (cogs/sitedata.py): aggregates for the landing page, written here every 30
# minutes and pushed by server/publish_site_data.py. SITE_DATA=off stops the bot writing it.
SITE_DATA_FILE = DATA_DIR / "site" / "stats.json"


def site_data_enabled() -> bool:
    return os.environ.get("SITE_DATA", "on").strip().lower() not in {"off", "0", "false", "no"}
# Ban appeals (logic/appeals.py). Off unless APPEAL_SECRET is set in server/.env.
APPEALS_CHANNEL = "🔒・appeals"  # staff only; the website's webhook posts here
SITE_URL = "https://discord.lscaturchio.xyz"
APPEAL_SECRET_MIN = 16  # characters; site/api/appeal.js applies the same floor


@dataclass(frozen=True)
class AppealSettings:
    secret: str
    page_url: str  # the form banned members fill in
    join_url: str
    cooling: int  # seconds after a ban before an appeal is read
    reappeal: int  # seconds after a denial before the next one is read


def _env_days(env, key: str, default: int) -> int:
    try:
        days = int(str(env.get(key, "")).strip() or default)
    except ValueError:
        return default
    return days if 0 <= days <= 3650 else default


def appeal_settings(env=None) -> AppealSettings | None:
    """None (the whole feature is off) unless APPEAL_SECRET is set and long enough.
    Optional: APPEAL_COOLING_DAYS (default 7), APPEAL_REAPPEAL_DAYS (default 90) and
    SITE_URL (default the public site; https only)."""
    env = os.environ if env is None else env
    secret = str(env.get("APPEAL_SECRET", "")).strip()
    if len(secret) < APPEAL_SECRET_MIN:
        return None
    site = str(env.get("SITE_URL", "")).strip().rstrip("/")
    if not site.startswith("https://") or any(c.isspace() or c in "<>" for c in site):
        site = SITE_URL
    day = 24 * 60 * 60
    return AppealSettings(secret=secret, page_url=f"{site}/appeal", join_url=f"{site}/join",
                          cooling=_env_days(env, "APPEAL_COOLING_DAYS", 7) * day,
                          reappeal=_env_days(env, "APPEAL_REAPPEAL_DAYS", 90) * day)


# Tonight (cogs/tonight.py): one line in general while people are in voice. TONIGHT=off turns it off.
def tonight_enabled(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return str(environ.get("TONIGHT") or "on").strip().lower() not in {"off", "0", "false", "no"}


# Welcomers (cogs/welcomers.py): opt-in on the 🎭・roles panel; pinged in general when someone new
# joins a public voice channel for the first time.
WELCOMER_ROLE = "Welcomer"
