"""Creator spotlight: parsing what members link (a YouTube channel or a Twitch login),
reading YouTube upload feeds and Twitch Helix stream lists. Pure: no Discord, no network.

Everything that ends up in a post is rebuilt from validated ids (a watch URL from an
11-character video id, a channel URL from a UC id, a Twitch URL from a login), never
copied from a feed or from what a member typed."""

import re
import xml.etree.ElementTree as ET
from collections.abc import Collection, Iterable
from dataclasses import dataclass
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

CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
HANDLE = re.compile(r"[A-Za-z0-9._-]{3,30}")
TWITCH_LOGIN = re.compile(r"[a-z0-9_]{3,25}")
TWITCH_ID = re.compile(r"[0-9]{1,20}")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
# twitch.tv/<these> are site pages, not channels
TWITCH_RESERVED = {"directory", "videos", "settings", "downloads", "jobs", "p", "search", "subscriptions",
                   "inventory", "wallet", "drops", "friends", "messages", "store", "turbo", "prime"}

ATOM = "{http://www.w3.org/2005/Atom}"
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
            videos.append(Video(vid, clean_title(entry.findtext(f"{ATOM}title") or "")))
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
