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
# The Realms (Minecraft bridge): status panel and two-way chat live in this channel.
REALMS_CHANNEL = "⛏️・realms"
REALMS_ENV = ("REALMS_ADDRESS", "REALMS_RCON_HOST", "REALMS_RCON_PORT", "REALMS_RCON_PASSWORD", "REALMS_LOG_PATH")


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


def game_by_key(key: str) -> Game | None:
    return next((g for g in GAMES if g.key == key), None)


def match_by_name(items, name: str):
    """First item whose .name matches `name`, ignoring emoji, case and separators."""
    target = slug(name)
    return next((i for i in items if slug(i.name) == target), None)


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
