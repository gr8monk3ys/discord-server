"""Game news: which official feed each game reads, parsing the Steam news API and RSS/Atom
feeds, and choosing what to post. Pure: no Discord, no network.

Sources live in assets/news_sources.json, keyed by the game's role name. Steam items are
kept only when they come from the game's own announcements (feedname
steam_community_announcements or feed_type 1), so press articles Steam mirrors are skipped.
Every link posted is rebuilt: Steam links from the app id and the numeric item id, RSS
links only when they are https on the feed's own host. Summaries are plain text (BBCode,
HTML and bare URLs stripped). XML with a DTD is refused outright, so no entity tricks
reach the parser."""

import html
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

SOURCES_FILE = Path(__file__).resolve().parents[1] / "assets" / "news_sources.json"
POLL_MINUTES = 30
MAX_PER_POLL = 2  # posts per game per poll; extra new items are marked seen quietly
MAX_AGE = 7 * 86400  # never post anything older than this
MAX_BODY = 512 * 1024  # a Steam reply is ~3 KB and the Minecraft feed ~8 KB
MAX_TITLE = 200
MAX_SUMMARY = 300
MAX_ID = 200
SEEN_KEEP = 30 * 86400  # seen rows older than this can go: their items are past MAX_AGE
SEED_MARK = "__seeded__"  # news_seen row that says a source's backlog was already marked seen

STEAM_API = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAM_OFFICIAL = "steam_community_announcements"
STEAM_GID = re.compile(r"[0-9]{1,20}")
ATOM = "{http://www.w3.org/2005/Atom}"

BB_BLOCKS = re.compile(r"\[(img|previewyoutube|video)\b[^\]]*\].*?\[/\1\]", re.I | re.S)
BB_TAG = re.compile(r"\[/?[a-z0-9*]+(?:=[^\]]*)?\]", re.I)
HTML_TAG = re.compile(r"</?[a-z][^<>]*>", re.I)
STEAM_PLACEHOLDER = re.compile(r"\{STEAM_[A-Z_]+\}\S*")
GLUED = re.compile(r"([A-Za-z0-9)][.:!?])(?=[A-Z])")
BARE_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)


# ---------------------------------------------------------------- sources
@dataclass(frozen=True)
class Source:
    game: str  # role name, as in config.GAMES
    kind: str  # "steam" or "rss"
    appid: int | None = None
    url: str | None = None
    host: str | None = None

    @property
    def key(self) -> str:
        """The news_seen.source value."""
        return f"steam:{self.appid}" if self.kind == "steam" else f"rss:{self.url}"


def https_host(url) -> str | None:
    """The lower-cased host of a plain https URL (no user, no port), else None."""
    if not isinstance(url, str) or any(not ch.isprintable() or ch.isspace() for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or port is not None or "@" in parts.netloc:
        return None
    return host


def load_sources(data, games: Collection[str]) -> list[Source]:
    """Validated sources from the parsed JSON, in file order. Keys starting with '_' are
    comments; entries for unknown games or with a bad value are skipped."""
    if not isinstance(data, dict):
        return []
    out = []
    for game, spec in data.items():
        if not isinstance(game, str) or game.startswith("_") or game not in games or not isinstance(spec, dict):
            continue
        appid, url = spec.get("steam_appid"), spec.get("rss")
        if appid is not None and url is not None:
            continue
        if isinstance(appid, int) and not isinstance(appid, bool) and 0 < appid < 2**32:
            out.append(Source(game, "steam", appid=appid))
        elif url is not None and (host := https_host(url)):
            out.append(Source(game, "rss", url=url, host=host))
    return out


def read_sources(games: Collection[str], path: Path | None = None) -> list[Source]:
    """Sources from the shipped file; raises OSError/ValueError if it can't be read."""
    path = path or SOURCES_FILE
    return load_sources(json.loads(path.read_text(encoding="utf-8")), games)


def steam_api_url(appid: int) -> str:
    query = urlencode({"appid": appid, "count": 5, "maxlength": MAX_SUMMARY, "format": "json",
                       "feeds": STEAM_OFFICIAL})
    return f"{STEAM_API}?{query}"


def steam_link(appid: int, gid: str) -> str:
    return f"https://store.steampowered.com/news/app/{appid}/view/{gid}"


# ---------------------------------------------------------------- text
def _printable(text: str) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in text)


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    space = head.rfind(" ")
    if space > limit // 2:
        head = head[:space]
    return head.rstrip() + "…"


def clean_title(text) -> str:
    """One line of plain text, at most MAX_TITLE characters."""
    text = html.unescape(html.unescape(text if isinstance(text, str) else ""))
    text = " ".join(_printable(HTML_TAG.sub("", text)).split())
    return _cut(text, MAX_TITLE) if text else "Untitled"


def plain_text(text, limit: int = MAX_SUMMARY) -> str:
    """BBCode/HTML to one line of plain text with no URLs, at most `limit` characters."""
    if not isinstance(text, str) or not text:
        return ""
    text = BB_BLOCKS.sub(" ", text)
    text = STEAM_PLACEHOLDER.sub(" ", text)
    text = BB_TAG.sub(" ", text)
    text = HTML_TAG.sub(" ", text)
    text = html.unescape(html.unescape(text))  # feeds often escape twice (&amp;#39;)
    text = HTML_TAG.sub(" ", text)
    text = BARE_URL.sub(" ", text)
    text = GLUED.sub(r"\1 ", text)  # Steam's cut drops line breaks: "art.Added" -> "art. Added"
    text = " ".join(_printable(text).split()).strip("\\ ")
    return _cut(text, limit)


# ---------------------------------------------------------------- items
@dataclass(frozen=True)
class Item:
    id: str
    title: str
    link: str
    summary: str
    published: int  # unix time


def parse_steam(body: bytes, appid: int) -> list[Item]:
    """Official items from a GetNewsForApp reply. Raises ValueError on anything unexpected."""
    if len(body) > MAX_BODY:
        raise ValueError("reply too large")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("bad JSON") from None
    news = data.get("appnews") if isinstance(data, dict) else None
    entries = news.get("newsitems") if isinstance(news, dict) else None
    if not isinstance(entries, list):
        raise ValueError("no newsitems")
    items = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("feedname") != STEAM_OFFICIAL and e.get("feed_type") != 1:
            continue
        if e.get("appid", appid) != appid:
            continue
        gid, date = e.get("gid"), e.get("date")
        if not isinstance(gid, str) or not STEAM_GID.fullmatch(gid):
            continue
        if not isinstance(date, int) or isinstance(date, bool):
            continue
        items.append(Item(gid, clean_title(e.get("title")), steam_link(appid, gid), plain_text(e.get("contents")),
                          date))
    return items


def _decode_xml(body: bytes) -> str:
    """XML bytes to text: BOM or byte-pattern sniffing for UTF-16, else UTF-8. The XML
    declaration is dropped (the text is already decoded)."""
    if body.startswith(b"\xef\xbb\xbf"):
        enc, body = "utf-8", body[3:]
    elif body.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        raise ValueError("unsupported encoding")
    elif body.startswith(b"\xff\xfe"):
        enc, body = "utf-16-le", body[2:]
    elif body.startswith(b"\xfe\xff"):
        enc, body = "utf-16-be", body[2:]
    elif body.startswith(b"<\x00"):
        enc = "utf-16-le"
    elif body.startswith(b"\x00<"):
        enc = "utf-16-be"
    else:
        enc = "utf-8"
    try:
        text = body.decode(enc)
    except UnicodeDecodeError:
        raise ValueError("bad encoding") from None
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text)


def _safe_link(url, host: str) -> str | None:
    """`url` rebuilt as https://host/path?query when it is https on exactly `host`."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if https_host(url) != host:
        return None
    parts = urlsplit(url)
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def _rss_date(text) -> int | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _iso_date(text) -> int | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        dt = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _text(el) -> str:
    return "".join(el.itertext()) if el is not None else ""


def _rss_items(channel, host: str) -> Iterable[Item]:
    for entry in channel.findall("item"):
        link = _safe_link(entry.findtext("link"), host)
        if link is None:
            for a in entry.findall(f"{ATOM}link"):
                if a.get("rel", "alternate") == "alternate" and (link := _safe_link(a.get("href"), host)):
                    break
        published = _rss_date(entry.findtext("pubDate"))
        if link is None or published is None:
            continue
        guid = " ".join((entry.findtext("guid") or "").split())
        yield Item((guid or link)[:MAX_ID], clean_title(entry.findtext("title")), link,
                   plain_text(entry.findtext("description")), published)


def _atom_items(feed, host: str) -> Iterable[Item]:
    for entry in feed.findall(f"{ATOM}entry"):
        link = None
        for a in entry.findall(f"{ATOM}link"):
            if a.get("rel", "alternate") == "alternate" and (link := _safe_link(a.get("href"), host)):
                break
        published = _iso_date(entry.findtext(f"{ATOM}published")) or _iso_date(entry.findtext(f"{ATOM}updated"))
        if link is None or published is None:
            continue
        ident = " ".join((entry.findtext(f"{ATOM}id") or "").split())
        summary = _text(entry.find(f"{ATOM}summary")) or _text(entry.find(f"{ATOM}content"))
        yield Item((ident or link)[:MAX_ID], clean_title(_text(entry.find(f"{ATOM}title"))), link,
                   plain_text(summary), published)


def parse_rss(body: bytes, host: str) -> list[Item]:
    """Items from an RSS 2.0 or Atom feed whose links are https on `host`. Raises
    ValueError on anything unexpected, including any DTD."""
    if len(body) > MAX_BODY:
        raise ValueError("feed too large")
    text = _decode_xml(body)
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("feed has a DTD")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"bad feed: {exc}") from None
    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise ValueError("RSS without a channel")
        return list(_rss_items(channel, host))
    if root.tag == f"{ATOM}feed":
        return list(_atom_items(root, host))
    raise ValueError("not an RSS or Atom feed")


# ---------------------------------------------------------------- choosing
def select(items: Iterable[Item], seen: Collection[str], now: int, limit: int = MAX_PER_POLL,
           max_age: int = MAX_AGE) -> tuple[list[Item], list[Item]]:
    """(to post, to mark seen quietly). The newest `limit` unseen items no older than
    `max_age` are posted, oldest of them first so the newest ends up last in the channel;
    other unseen recent items are only marked seen."""
    fresh, ids = [], set()
    for i in sorted(items, key=lambda i: i.published, reverse=True):
        if i.id in seen or i.id in ids or now - i.published > max_age:
            continue
        ids.add(i.id)
        fresh.append(i)
    return list(reversed(fresh[:limit])), fresh[limit:]


def latest(items: Iterable[Item]) -> Item | None:
    return max(items, key=lambda i: i.published, default=None)
