"""Creator spotlight: parsing what members link (a YouTube channel or a Twitch login),
reading YouTube upload feeds and Twitch Helix stream lists. Pure: no Discord, no network.

Everything that ends up in a post is rebuilt from validated ids (a watch URL from an
11-character video id, a channel URL from a UC id, a Twitch URL from a login), never
copied from a feed or from what a member typed.

Ownership: /creator link only starts a claim. The member puts a random code (FD-XXXXXX)
in their channel description or Twitch bio and runs /creator verify; only verified links
are announced, so nobody can claim a channel they don't control."""

import html
import json
import re
import secrets
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

PLATFORMS = ("youtube", "twitch")
MAX_LINKS = 2  # linked platforms per member
MAX_INPUT = 200
MAX_FEED_BYTES = 1024 * 1024  # a YouTube feed is ~15 entries, far below this
MAX_PAGE_BYTES = 2 * 1024 * 1024  # channel pages are big; the ids sit near the top
MAX_TITLE = 200
MAX_NEW_PER_POLL = 3  # uploads announced per channel per poll; older extras are marked seen
TOKEN_MARGIN = 5 * 60  # refresh the Twitch app token this long before it expires
STREAMS_BATCH = 100  # Helix accepts up to 100 user_id params per request
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L
CODE_LEN = 6
CODE_TTL = 24 * 3600  # a pending claim must be verified within a day
VERIFY_COOLDOWN = 60  # one /creator verify per member per minute
MAX_DESCRIPTION = 5000  # YouTube allows 1,000 characters, Twitch 300; anything longer is cut
MAX_DESCRIPTIONS = 6  # description candidates read from one page

CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
HANDLE = re.compile(r"[A-Za-z0-9._-]{3,30}")
TWITCH_LOGIN = re.compile(r"[a-z0-9_]{3,25}")
TWITCH_ID = re.compile(r"[0-9]{1,20}")
CODE = re.compile(rf"FD-[{CODE_ALPHABET}]{{{CODE_LEN}}}")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
# twitch.tv/<these> are site pages, not channels
TWITCH_RESERVED = {"directory", "videos", "settings", "downloads", "jobs", "p", "search", "subscriptions",
                   "inventory", "wallet", "drops", "friends", "messages", "store", "turbo", "prime"}

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


# ---------------------------------------------------------------- what members type
@dataclass(frozen=True)
class YouTubeRef:
    kind: str  # "channel" (value = UC id) or "handle" (value = handle without @)
    value: str


def _url_parts(text: str, hosts: set[str]):
    """(path segments) of an http(s) URL on one of `hosts`, else None. A bare
    host/path without a scheme is read as https."""
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.netloc.lower() not in hosts:
        return None  # also rejects user@host and :port forms
    return [p for p in parts.path.split("/") if p]


def parse_youtube(text: str) -> YouTubeRef | None:
    text = (text or "").strip()
    if not text or len(text) > MAX_INPUT:
        return None
    if CHANNEL_ID.fullmatch(text):
        return YouTubeRef("channel", text)
    if text.startswith("@"):
        return YouTubeRef("handle", text[1:]) if HANDLE.fullmatch(text[1:]) else None
    segments = _url_parts(text, YOUTUBE_HOSTS)
    if not segments:
        return None
    first = segments[0]
    if first == "channel" and len(segments) >= 2 and CHANNEL_ID.fullmatch(segments[1]):
        return YouTubeRef("channel", segments[1])
    if first.startswith("@") and HANDLE.fullmatch(first[1:]):
        return YouTubeRef("handle", first[1:])
    return None


def parse_twitch(text: str) -> str | None:
    """A Twitch login (lower case) from a channel URL, @login or bare login."""
    text = (text or "").strip()
    if not text or len(text) > MAX_INPUT:
        return None
    if "/" in text or "." in text:
        segments = _url_parts(text, TWITCH_HOSTS)
        if not segments:
            return None
        text = segments[0]
    login = text.removeprefix("@").lower()
    if not TWITCH_LOGIN.fullmatch(login) or login in TWITCH_RESERVED:
        return None
    return login


def link_problem(existing: Collection[str], platform: str) -> str | None:
    """None if this member may link `platform`; 'platform' (unknown) or 'limit'."""
    if platform not in PLATFORMS:
        return "platform"
    if platform not in existing and len(set(existing)) >= MAX_LINKS:
        return "limit"
    return None


# ---------------------------------------------------------------- URLs we build
def handle_url(handle: str) -> str:
    if not HANDLE.fullmatch(handle):
        raise ValueError("bad handle")
    return f"https://www.youtube.com/@{handle}"


def channel_url(channel_id: str) -> str:
    if not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("bad channel id")
    return f"https://www.youtube.com/channel/{channel_id}"


def feed_url(channel_id: str) -> str:
    if not CHANNEL_ID.fullmatch(channel_id):
        raise ValueError("bad channel id")
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def watch_url(video_id: str) -> str:
    if not VIDEO_ID.fullmatch(video_id or ""):
        raise ValueError("bad video id")
    return f"https://www.youtube.com/watch?v={video_id}"


def twitch_url(login: str) -> str:
    if not TWITCH_LOGIN.fullmatch(login or ""):
        raise ValueError("bad login")
    return f"https://twitch.tv/{login}"


# ---------------------------------------------------------------- YouTube pages and feeds
_CANONICAL = re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"')
_EXTERNAL = re.compile(r'"externalId":"(UC[A-Za-z0-9_-]{22})"')
_CHANNEL = re.compile(r'"channelId":"(UC[A-Za-z0-9_-]{22})"')


def extract_channel_id(html: str | bytes) -> str | None:
    """The channel's own UC id from its page: the canonical link, else the page's
    externalId, else the first channelId (later ones can be other channels)."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    for pattern in (_CANONICAL, _EXTERNAL, _CHANNEL):
        m = pattern.search(html)
        if m and CHANNEL_ID.fullmatch(m.group(1)):
            return m.group(1)
    return None


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    description: str = field(default="", compare=False, repr=False)  # only read when verifying


@dataclass(frozen=True)
class Feed:
    title: str
    videos: list[Video]  # as the feed lists them: newest first


def parse_feed(data: bytes) -> Feed:
    """Parse a YouTube Atom feed. Raises ValueError on anything unexpected. A DTD is
    refused outright (feeds never carry one), so no entity tricks reach the parser."""
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("feed too large")
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("feed has a DTD")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"bad feed: {exc}") from None
    if root.tag != f"{ATOM}feed":
        raise ValueError("not an Atom feed")
    videos = []
    for entry in root.findall(f"{ATOM}entry"):
        vid = (entry.findtext(f"{YT}videoId") or "").strip()
        if VIDEO_ID.fullmatch(vid):
            desc = entry.findtext(f"{MEDIA}group/{MEDIA}description") or ""
            videos.append(Video(vid, clean_title(entry.findtext(f"{ATOM}title") or ""), desc[:MAX_DESCRIPTION]))
    return Feed(clean_title(root.findtext(f"{ATOM}title") or ""), videos)


def unseen(videos: list[Video], seen: Collection[str], limit: int = MAX_NEW_PER_POLL) -> list[Video]:
    """The newest `limit` videos not seen yet, oldest first (the order to post them)."""
    fresh = [v for v in videos if v.id not in seen][:limit]
    return list(reversed(fresh))


def clean_title(text: str) -> str:
    """One line, no control characters, at most MAX_TITLE characters."""
    text = "".join(ch if ch.isprintable() else " " for ch in text or "")
    text = " ".join(text.split())
    if not text:
        return "Untitled"
    return text if len(text) <= MAX_TITLE else text[: MAX_TITLE - 1] + "…"


# ---------------------------------------------------------------- Twitch Helix
@dataclass(frozen=True)
class Stream:
    id: str
    user_id: str
    login: str
    title: str
    game: str


def parse_streams(data) -> list[Stream]:
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for s in items:
        if not isinstance(s, dict) or s.get("type") != "live":
            continue
        sid, uid = str(s.get("id", "")), str(s.get("user_id", ""))
        login = str(s.get("user_login", "")).lower()
        if TWITCH_ID.fullmatch(sid) and TWITCH_ID.fullmatch(uid) and TWITCH_LOGIN.fullmatch(login):
            out.append(Stream(sid, uid, login, clean_title(str(s.get("title") or "")),
                              clean_title(str(s.get("game_name") or "")) if s.get("game_name") else ""))
    return out


def parse_user(data) -> tuple[str, str, str] | None:
    """(user id, login, display name) from a Helix /users reply, or None."""
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    u = items[0]
    uid, login = str(u.get("id", "")), str(u.get("login", "")).lower()
    if not TWITCH_ID.fullmatch(uid) or not TWITCH_LOGIN.fullmatch(login):
        return None
    return uid, login, clean_title(str(u.get("display_name") or login))


def streams_batches(user_ids: Iterable[str]) -> list[list[tuple[str, str]]]:
    ids = [u for u in user_ids if TWITCH_ID.fullmatch(u)]
    return [[("user_id", u) for u in ids[i:i + STREAMS_BATCH]] for i in range(0, len(ids), STREAMS_BATCH)]


def token_expires_at(now: int, expires_in) -> int:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        seconds = 3600
    return now + max(seconds - TOKEN_MARGIN, 60)


# ---------------------------------------------------------------- ownership verification
def new_code(choice: Callable[[str], str] = secrets.choice) -> str:
    """A fresh claim code like FD-7K2Q9X (about 30 bits from `secrets`)."""
    return "FD-" + "".join(choice(CODE_ALPHABET) for _ in range(CODE_LEN))


# Unicode hyphens and dashes that phones and text editors swap in for "-"
_DASHES = str.maketrans(dict.fromkeys("‐‑‒–—−﹣－", "-"))


def code_in(code: str, texts: Iterable[str | None]) -> bool:
    """True if `code` appears as a whole word (any case, any dash) in one of `texts`."""
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(code.upper())}(?![A-Z0-9])")
    for text in texts:
        if isinstance(text, str) and text and pattern.search(text[:MAX_DESCRIPTION].translate(_DASHES).upper()):
            return True
    return False


def expired(created_at: int, now: int) -> bool:
    return now - created_at >= CODE_TTL


def cooldown_left(last: int | None, now: int, period: int = VERIFY_COOLDOWN) -> int:
    """Seconds until the member may run /creator verify again (0 = now)."""
    if last is None:
        return 0
    return max(0, last + period - now)


_META = re.compile(r"<meta\s[^>]{0,4000}>", re.IGNORECASE)
_ATTR = re.compile(r"""([a-zA-Z:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_JSON_STRING = r'"((?:[^"\\]|\\.){0,20000})"'
# Only the channel's own description: its metadata renderer and the page header's preview.
_JSON_DESCRIPTIONS = (
    re.compile(r'"channelMetadataRenderer"\s*:\s*\{[^{}]{0,4000}?"description"\s*:\s*' + _JSON_STRING),
    re.compile(r'"descriptionPreviewViewModel"\s*:\s*\{\s*"description"\s*:\s*\{\s*"content"\s*:\s*'
               + _JSON_STRING),
)


def _cap(text: str) -> str:
    return text[:MAX_DESCRIPTION]


def youtube_descriptions(page: str | bytes) -> list[str]:
    """Every copy of the channel description on a channel page: the description and
    og:description meta tags and the description in the page's JSON. Other "description"
    keys (videos, other channels) are ignored. Each is cut to MAX_DESCRIPTION."""
    if isinstance(page, bytes):
        page = page[:MAX_PAGE_BYTES].decode("utf-8", "replace")
    page = page[:MAX_PAGE_BYTES]
    out: list[str] = []
    for tag in _META.finditer(page):
        attrs = {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
                 for m in _ATTR.finditer(tag.group(0))}
        if (attrs.get("name", "").lower() == "description"
                or attrs.get("property", "").lower() == "og:description") and attrs.get("content"):
            out.append(_cap(html.unescape(attrs["content"])))
    for pattern in _JSON_DESCRIPTIONS:
        m = pattern.search(page)
        if m:
            try:
                text = json.loads(f'"{m.group(1)}"')
            except ValueError:
                continue
            if isinstance(text, str) and text:
                out.append(_cap(text))
    return out[:MAX_DESCRIPTIONS]


def feed_descriptions(feed: Feed) -> list[str]:
    """Descriptions of the uploads in a feed (only the channel owner can write these)."""
    return [v.description for v in feed.videos if v.description]


def twitch_bio(data, expected_id: str) -> str | None:
    """The bio from a Helix /users reply, if the reply is for `expected_id`; else None."""
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    u = items[0]
    if str(u.get("id", "")) != expected_id:
        return None
    bio = u.get("description")
    return _cap(bio) if isinstance(bio, str) else ""
