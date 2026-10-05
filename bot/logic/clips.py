"""Module 3: clip of the week. Pure: no Discord, no database.

Which messages count as clips, which clips go in the weekly poll, and who won it."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_ANSWERS = 10  # Discord polls allow at most 10 answers
ANSWER_LEN = 55  # Discord's limit for a poll answer
GIVE_UP_AFTER = 48 * 60 * 60  # stop waiting for a poll to finalize this long after it ended

# Hosts whose links count as clips (subdomains like www./m. included).
CLIP_HOSTS = ("youtube.com", "youtu.be", "medal.tv", "streamable.com", "outplayed.tv", "kick.com")

# Discord users wrap links in <...> to suppress the embed; also trim trailing punctuation.
_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}'\""


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _is_clip(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.strip("/")
    if _host_matches(host, "twitch.tv"):
        # Only clips: clips.twitch.tv/<slug> or twitch.tv/<channel>/clip/<slug>.
        if host == "clips.twitch.tv":
            return bool(path)
        segments = path.split("/")
        return len(segments) >= 3 and segments[1] == "clip"
    if any(_host_matches(host, d) for d in CLIP_HOSTS):
        return bool(path) or bool(parts.query)
    return False


def clip_link(text: str | None) -> str | None:
    """The first link in `text` to a supported clip site, or None."""
    for match in _URL.finditer(text or ""):
        url = match.group(0).rstrip(_TRAILING)
        if _is_clip(url):
            return url
    return None


def is_video(content_type: str | None) -> bool:
    return bool(content_type) and content_type.lower().startswith("video/")


def clip_url(content: str | None, attachments) -> str | None:
    """The clip in a message: the first video attachment, else the first clip link.
    `attachments` is an iterable of (url, content_type)."""
    for url, content_type in attachments:
        if is_video(content_type):
            return url
    return clip_link(content)


@dataclass(frozen=True)
class Clip:
    message_id: int
    user_id: int
    posted_at: int
    reactions: int = 0


def poll_entries(clips: list[Clip], limit: int = MAX_ANSWERS) -> list[Clip]:
    """The clips that go in the poll, in posting order (answer #1 is the earliest).
    With more than `limit`, the ones with the most reactions; ties go to the earlier post."""
    chosen = list(clips)
    if len(chosen) > limit:
        chosen = sorted(chosen, key=lambda c: (-c.reactions, c.posted_at, c.message_id))[:limit]
    return sorted(chosen, key=lambda c: (c.posted_at, c.message_id))


def answer_text(number: int, name: str) -> str:
    return f"#{number} · {name}"[:ANSWER_LEN]


def week_of(job_key: str) -> str:
    """'clips:2026-W40' -> '2026-W40'."""
    return job_key.split(":", 1)[1]


def winner_index(entry_count: int, votes: dict[int, int]) -> int | None:
    """0-based index of the winning entry. `votes` maps answer number (1-based, the
    order answers were added) to vote count. Most votes wins, ties go to the earliest
    entry; no votes means no winner."""
    best, best_votes = None, 0
    for number in range(1, entry_count + 1):
        n = votes.get(number, 0)
        if n > best_votes:
            best, best_votes = number - 1, n
    return best


def give_up(now: int, ends_at: int) -> bool:
    return now > ends_at + GIVE_UP_AFTER
