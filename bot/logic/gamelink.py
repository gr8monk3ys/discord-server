"""Linked game accounts: what a member may type for /link steam, the Steam community
profile XML (no API key), ownership codes, what is stored in game_accounts.data and the
"Game accounts" lines shown on /profile and /accounts. Pure: no Discord, no network.

Steam's public profile is read from https://steamcommunity.com/id/VANITY?xml=1 or
/profiles/STEAMID64?xml=1. Every URL fetched or shown is rebuilt from a validated id
(SteamID64: 17 digits starting 7656; vanity: 2-32 of A-Z a-z 0-9 _ -), a redirect is
followed only to another profile URL on steamcommunity.com, replies over 512 KB are
refused, and XML with a DTD or an entity declaration never reaches the parser.

Steam only lists the games played in the last two weeks (`mostPlayedGames`, when the
member's game details are public), each with its hours on record. Hours for the server's
own games (config.GAMES) are remembered from one refresh to the next, so they stay on the
profile after the game drops off Steam's recent list.

Persona and game names come from Steam, not from us: they are cut, shown escaped, and a
persona the text filter refuses is replaced by a neutral label."""

import html
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import config
from logic import creators as _claims
from logic import textfilter
from names import slug

PLATFORMS = ("steam",)
NAMES = {"steam": "Steam"}
DAY = 24 * 3600
MAX_INPUT = 200  # longest /link steam argument accepted
MAX_BODY = 512 * 1024  # a full public profile is ~8 KB
FETCH_TIMEOUT = 10
MAX_REDIRECTS = 2
MAX_PERSONA = 64  # Steam allows 32
MAX_GAME_NAME = 80
MAX_SUMMARY = 5000
MAX_GAMES = 6  # recent games kept per account
SHOWN_RECENT = 3  # recent games listed on a profile
REFRESH_EVERY = DAY  # each account is re-read at most this often
MAX_FAILS = 3  # consecutive failed refreshes before an account is left alone
REFRESH_BATCH = 5  # accounts re-read per loop run
FALLBACK_PERSONA = "Steam player"

STEAM_HOST = "steamcommunity.com"
STEAM_HOSTS = {STEAM_HOST, "www.steamcommunity.com"}
STEAM_ID = re.compile(r"7656[0-9]{13}")
VANITY = re.compile(r"[A-Za-z0-9_-]{2,32}")
APP_LINK = re.compile(r"https://steamcommunity\.com/app/([0-9]{1,10})/?")
HTML_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")

# Steam app names (as slugs) that are one of the server's games under another name:
# a slug that starts with the key counts toward the role on the right.
PREFIXES = {"callofduty": "Call of Duty", "grandtheftautov": "GTA Online"}

# Claim codes work exactly like /creator's: FD-XXXXXX, good for 24 hours, one check a minute.
CODE_TTL = _claims.CODE_TTL
VERIFY_COOLDOWN = _claims.VERIFY_COOLDOWN
new_code = _claims.new_code
code_in = _claims.code_in
expired = _claims.expired
cooldown_left = _claims.cooldown_left


class ProfileNotFound(ValueError):
    """Steam answered, and says there is no such profile."""


# ---------------------------------------------------------------- input and URLs
@dataclass(frozen=True)
class Ref:
    kind: str  # "id" (SteamID64) or "vanity"
    value: str


def _ref(kind_hint: str | None, value: str) -> Ref | None:
    if kind_hint != "vanity" and value.isdigit() and len(value) == 17:
        return Ref("id", value) if STEAM_ID.fullmatch(value) else None
    if kind_hint == "id":
        return None
    return Ref("vanity", value) if VANITY.fullmatch(value) else None


def parse_steam(text) -> Ref | None:
    """A SteamID64, a vanity name, or a steamcommunity.com/id/NAME or /profiles/ID link.
    Anything else (other hosts, other paths, ports, credentials) is None."""
    if not isinstance(text, str):
        return None
    text = text.strip().strip("<>").strip()
    if not text or len(text) > MAX_INPUT or any(ch.isspace() or not ch.isprintable() for ch in text):
        return None
    if "/" not in text and "." not in text and ":" not in text:
        return _ref(None, text)
    url = text if "://" in text else f"https://{text}"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or port is not None or "@" in parts.netloc:
        return None
    if (parts.hostname or "").lower() not in STEAM_HOSTS:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return None
    if segments[0] == "id":
        return _ref("vanity", segments[1])
    if segments[0] == "profiles":
        return _ref("id", segments[1])
    return None


def xml_url(ref: Ref) -> str:
    """The profile XML for a validated reference; raises ValueError for anything else."""
    if ref.kind == "id" and STEAM_ID.fullmatch(ref.value):
        return f"https://{STEAM_HOST}/profiles/{ref.value}?xml=1"
    if ref.kind == "vanity" and VANITY.fullmatch(ref.value):
        return f"https://{STEAM_HOST}/id/{ref.value}?xml=1"
    raise ValueError("not a Steam profile reference")


def id_xml_url(steam_id: str) -> str:
    return xml_url(Ref("id", steam_id))


def profile_url(steam_id: str) -> str:
    """The link shown to members, always rebuilt from the SteamID64."""
    if not isinstance(steam_id, str) or not STEAM_ID.fullmatch(steam_id):
        raise ValueError("not a SteamID64")
    return f"https://{STEAM_HOST}/profiles/{steam_id}"


def redirect_target(location) -> str | None:
    """Where a redirect may be followed: only to a profile on steamcommunity.com over https,
    and the URL is rebuilt, never copied. None means stop."""
    if not isinstance(location, str) or len(location) > 500:
        return None
    if any(ch.isspace() or not ch.isprintable() for ch in location):
        return None
    if location.startswith("/") and not location.startswith("//"):
        location = f"https://{STEAM_HOST}{location}"
    try:
        parts = urlsplit(location)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or "@" in parts.netloc:
        return None
    if (parts.hostname or "").lower() != STEAM_HOST:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) != 2 or segments[0] not in ("id", "profiles"):
        return None
    ref = _ref("vanity" if segments[0] == "id" else "id", segments[1])
    return xml_url(ref) if ref else None


# ---------------------------------------------------------------- the profile XML
@dataclass(frozen=True)
class Played:
    name: str
    appid: int | None
    hours: float  # on record
    recent: float  # last two weeks


@dataclass(frozen=True)
class Profile:
    steam_id: str
    persona: str
    privacy: str  # "public", "friendsonly", "private" or "unknown"
    summary: str  # plain text; empty unless the profile is public
    games: tuple[Played, ...]

    @property
    def public(self) -> bool:
        return self.privacy == "public"


def _one_line(text, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return text[:limit].rstrip()


def _plain(text, limit: int) -> str:
    """Steam's summary HTML as plain text (tags become spaces)."""
    if not isinstance(text, str):
        return ""
    text = html.unescape(HTML_TAG.sub(" ", text[: limit * 4]))
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())[:limit]


def _hours(text) -> float:
    """'1,234.5' -> 1234.5; anything odd is 0."""
    if not isinstance(text, str):
        return 0.0
    text = text.strip().replace(",", "")
    if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,3})?", text):
        return 0.0
    return float(text)


def _games(root) -> tuple[Played, ...]:
    out = []
    for node in root.iterfind("mostPlayedGames/mostPlayedGame"):
        name = _one_line(node.findtext("gameName"), MAX_GAME_NAME)
        if not name:
            continue
        link = APP_LINK.fullmatch((node.findtext("gameLink") or "").strip())
        out.append(Played(name, int(link.group(1)) if link else None, _hours(node.findtext("hoursOnRecord")),
                          _hours(node.findtext("hoursPlayed"))))
        if len(out) >= MAX_GAMES:
            break
    return tuple(out)


def parse_profile(body: bytes) -> Profile:
    """The profile in a `?xml=1` reply. Raises ProfileNotFound when Steam says there is no
    such profile and ValueError for anything unexpected (too large, not UTF-8, a DTD, not
    a profile, no valid SteamID64)."""
    if not isinstance(body, (bytes, bytearray)):
        raise ValueError("not bytes")
    if len(body) > MAX_BODY:
        raise ValueError("profile too large")
    body = bytes(body)
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    if b"\x00" in body[:64] or body.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("unsupported encoding")  # Steam always sends UTF-8
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("bad encoding") from None
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)  # already decoded: drop the declaration
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("profile has a DTD")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"bad profile XML: {exc}") from None
    if root.tag == "response":
        if root.find("error") is not None:
            raise ProfileNotFound("no such profile")
        raise ValueError("unexpected response")
    if root.tag != "profile":
        raise ValueError("not a profile")
    steam_id = (root.findtext("steamID64") or "").strip()
    if not STEAM_ID.fullmatch(steam_id):
        raise ValueError("profile without a SteamID64")
    privacy = (root.findtext("privacyState") or "").strip().lower()
    if privacy not in ("public", "friendsonly", "private"):
        privacy = "unknown"
    return Profile(steam_id=steam_id,
                   persona=_one_line(root.findtext("steamID"), MAX_PERSONA) or FALLBACK_PERSONA,
                   privacy=privacy,
                   summary=_plain(root.findtext("summary"), MAX_SUMMARY) if privacy == "public" else "",
                   games=_games(root) if privacy == "public" else ())


# ---------------------------------------------------------------- the server's games
def match_game(app_name: str) -> config.Game | None:
    """The server game (config.GAMES) a Steam app name stands for, if any."""
    key = slug(app_name or "")
    if not key:
        return None
    for game in config.GAMES:
        if game.key == key:
            return game
    for prefix, role in PREFIXES.items():
        if key.startswith(prefix):
            return config.game_by_key(slug(role))
    return None


def server_hours(games) -> dict[str, float]:
    """Hours on record per server game key, summed over the apps that count toward it."""
    out: dict[str, float] = {}
    for g in games:
        game = match_game(g.name)
        if game is not None and g.hours > 0:
            out[game.key] = round(out.get(game.key, 0.0) + g.hours, 1)
    return out


# ---------------------------------------------------------------- game_accounts.data
def load_data(raw) -> dict:
    """The stored JSON as a dict with every key present and well-typed; junk becomes empty."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    def number(value, cap: float) -> float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= cap
        return round(float(value), 1) if ok else 0.0

    recent = []
    raw_recent = data.get("recent")
    for g in raw_recent if isinstance(raw_recent, list) else []:
        if not isinstance(g, dict):
            continue
        name = _one_line(g.get("name"), MAX_GAME_NAME)
        if not name:
            continue
        appid = g.get("appid")
        recent.append({"name": name,
                       "appid": appid if isinstance(appid, int) and not isinstance(appid, bool) and appid > 0 else None,
                       "hours": number(g.get("hours"), 1e7), "recent": number(g.get("recent"), 1e7)})
    known = {g.key for g in config.GAMES}
    raw_hours = data.get("hours")
    hours = {k: number(v, 1e7) for k, v in (raw_hours if isinstance(raw_hours, dict) else {}).items()
             if k in known and number(v, 1e7) > 0}
    fails, tried = data.get("fails"), data.get("tried_at")
    privacy = data.get("privacy")
    return {"persona": _one_line(data.get("persona"), MAX_PERSONA) or FALLBACK_PERSONA,
            "privacy": privacy if privacy in ("public", "friendsonly", "private") else "unknown",
            "recent": recent[:MAX_GAMES], "hours": hours,
            "fails": fails if isinstance(fails, int) and not isinstance(fails, bool) and fails > 0 else 0,
            "tried_at": tried if isinstance(tried, int) and not isinstance(tried, bool) and tried > 0 else 0}


def fresh_data(profile: Profile, old=None) -> str:
    """JSON for game_accounts.data after a successful read. Server-game hours seen before
    are kept (Steam only lists the last two weeks); a game Steam reports again is updated.
    Pass the old data only when it belongs to the same Steam account."""
    hours = dict(load_data(old)["hours"]) if old is not None else {}
    hours.update(server_hours(profile.games))
    recent = [{"name": g.name, "appid": g.appid, "hours": g.hours, "recent": g.recent} for g in profile.games]
    return json.dumps({"persona": profile.persona, "privacy": profile.privacy, "recent": recent[:MAX_GAMES],
                       "hours": hours, "fails": 0, "tried_at": 0}, separators=(",", ":"), sort_keys=True)


def failed_data(old, now: int) -> str:
    """The same data with one more consecutive failure, tried at `now`."""
    data = load_data(old)
    data["fails"] += 1
    data["tried_at"] = now
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def reset_data(old) -> str:
    """The same data with the failure count cleared (the member linked again)."""
    data = load_data(old)
    data["fails"], data["tried_at"] = 0, 0
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def gave_up(raw) -> bool:
    return load_data(raw)["fails"] >= MAX_FAILS


def due(raw, updated_at: int, now: int) -> bool:
    """Time to re-read this account: at most once a day (counting failed tries), and not
    at all after MAX_FAILS consecutive failures."""
    data = load_data(raw)
    if data["fails"] >= MAX_FAILS:
        return False
    return now - max(updated_at, data["tried_at"]) >= REFRESH_EVERY


# ---------------------------------------------------------------- display
def escape(text: str) -> str:
    """Discord markdown, mentions and link previews made inert."""
    text = re.sub(r"([\\*_~`|>\[\]()#:@<-])", r"\\\1", text)
    return text.replace("@", "@​") if "@" in text else text


def safe_persona(name) -> str:
    """The persona name to show: one line, cut, and replaced when the text filter says no
    (invites, links, slurs)."""
    name = _one_line(name, MAX_PERSONA)
    if not name:
        return FALLBACK_PERSONA
    ok, _ = textfilter.check(name, allow_links=False)
    return name if ok else FALLBACK_PERSONA


def fmt_hours(hours: float) -> str:
    return f"{hours:,.1f} h" if hours < 10 else f"{hours:,.0f} h"


def account_lines(platform: str, account_id: str, raw, esc: Callable[[str], str] = escape) -> list[str]:
    """The lines for one verified account: who, hours in the server's games, and what
    they've played lately. Unknown platforms and bad ids give no lines."""
    if platform != "steam" or not isinstance(account_id, str) or not STEAM_ID.fullmatch(account_id):
        return []
    data = load_data(raw)
    lines = [f"**Steam** · {esc(safe_persona(data['persona']))} · <{profile_url(account_id)}>"]
    ours = [(g, data["hours"][g.key]) for g in config.GAMES if g.key in data["hours"]]
    if ours:
        lines.append("Here: " + " · ".join(f"{g.emoji} {g.role} {fmt_hours(h)}" for g, h in ours))
    lately = [g for g in data["recent"] if g["recent"] > 0][:SHOWN_RECENT]
    if lately:
        lines.append("Lately: " + " · ".join(f"{esc(g['name'])} {fmt_hours(g['recent'])}" for g in lately))
    elif data["privacy"] != "public":
        lines.append("Profile is private on Steam, so no hours.")
    return lines


def pending_line(platform: str, code: str) -> str:
    name = NAMES.get(platform, platform)
    return (f"Your {name} link is waiting: put `{code}` in your Steam profile summary and run "
            f"`/link verify`.")
