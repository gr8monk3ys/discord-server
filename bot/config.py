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
SQUAD_VOICE = "🎮 Squad"
LOBBY_VOICE = "🔊 Lobby"
MODES = ("Ranked", "Casual")


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
