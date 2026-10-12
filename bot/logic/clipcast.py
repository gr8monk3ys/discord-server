"""Clipcast: the week's best member clips on the server's YouTube channel as Shorts, only
ever with the author's explicit yes for that clip. Pure: no Discord, no database, no network.

The life of a clip here (the `clipcast.status` column):

    asked ──yes──> approved ──upload──> uploading ──> published ──takedown──> removed
      │  └──no──> declined                  │  └─(no API)─> handed            └─> removal
      │                                     └─(can't tell)─> unsure
      └─ deleted / left / banned / privacy off ─> withdrawn      (also from approved)
      └─ a week without an answer ─> expired        staff /clipcast skip ─> skipped

Only `approved` rows are ever uploaded, and a row is approved only by its author pressing a
yes button for that clip or by their standing `/clipcast consent always`.
"""

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from logic import clips, helpdesk, textfilter
from logic import quests as Q
from logic.schedule import Weekly

# Asked right after the clip-of-the-week poll opens; posted two days later, so authors
# have 48 hours to answer before the first upload run (a later yes goes in the next run).
ASK_JOB = Weekly("clipcast-ask", weekday=6, hour=18, minute=30)  # Sundays 18:30 local
PUBLISH_JOB = Weekly("clipcast", weekday=1, hour=18, minute=30)  # Tuesdays 18:30 local

TOP_N = 3  # clips asked about, and posted, per week
COINS = 100  # to the author once their clip has stayed posted for PAYOUT_DAYS
REASON = "clipcast"  # ledger reason
BADGE_KEY = "clipcast"  # granted on a first post when logic/achievements.py has it
MAX_ATTEMPTS = 3  # failed uploads of one clip before it's parked for staff
MAX_BYTES = 200 * 1024 * 1024  # larger attachments are never downloaded
JOIN_URL = f"{helpdesk.SITE_URL}/join"

# ---------------------------------------------------------------- statuses
ASKED, APPROVED, DECLINED, WITHDRAWN, EXPIRED, SKIPPED = (
    "asked", "approved", "declined", "withdrawn", "expired", "skipped")
UPLOADING, PUBLISHED, HANDED, UNSURE, FAILED, REMOVED, REMOVAL = (
    "uploading", "published", "handed", "unsure", "failed", "removed", "removal")
ANSWERABLE = (ASKED, APPROVED, DECLINED)  # the author may still change their answer
PENDING = (ASKED, APPROVED)  # consent that can still be withdrawn before anything is posted
OUTSIDE = (PUBLISHED, HANDED, UNSURE, REMOVAL)  # is, or may be, outside Discord already
ATTENTION = (UNSURE, FAILED, REMOVAL)  # staff should look
SKIPPABLE = (ASKED, APPROVED, DECLINED, UNSURE, FAILED)

STATUS_WORDS = {
    ASKED: "waiting for an answer", APPROVED: "approved, waiting to be posted", DECLINED: "declined",
    WITHDRAWN: "withdrawn", EXPIRED: "expired without an answer", SKIPPED: "skipped by staff",
    UPLOADING: "uploading right now", PUBLISHED: "posted", HANDED: "handed to staff to post by hand",
    UNSURE: "upload result unknown", FAILED: "failed too many times", REMOVED: "taken down",
    REMOVAL: "takedown requested",
}

# ---------------------------------------------------------------- consent
NAME, ANON, NO = "name", "anon", "no"  # button answers; the first two are also credit styles
CHOICES = (NAME, ANON, NO)
ALWAYS, ASK, NEVER = "always", "ask", "never"  # standing preference
PREFS = (ALWAYS, ASK, NEVER)
VIA_BUTTON, VIA_STANDING = "button", "standing"


def answer(status: str, choice: str) -> tuple[str, str | None] | None:
    """(new status, credit style) when the author answers `choice`, or None if the clip is
    past answering. A yes can become a no (and back) until the clip is uploaded."""
    if status not in ANSWERABLE or choice not in CHOICES:
        return None
    return (DECLINED, None) if choice == NO else (APPROVED, choice)


def may_upload(row) -> bool:
    """The one gate in front of every upload: an approved row carrying a yes recorded from
    its own author. `row` is a mapping with the clipcast columns."""
    return (row["status"] == APPROVED and row["credit"] in (NAME, ANON) and row["answered_at"] is not None
            and row["answered_by"] == row["user_id"] and not row["video_id"])


def removal_action(status: str) -> str | None:
    """What `/clipcast remove` does: 'withdraw' (nothing posted yet), 'takedown' (posted or
    possibly posted), 'busy' (mid-upload), or None (nothing to do)."""
    if status in PENDING:
        return "withdraw"
    if status in OUTSIDE:
        return "takedown"
    if status == UPLOADING:
        return "busy"
    return None


def after_failure(attempts: int) -> str:
    """Status after a clean upload failure: back in line until MAX_ATTEMPTS, then parked."""
    return FAILED if attempts >= MAX_ATTEMPTS else APPROVED


# ---------------------------------------------------------------- media
VIDEO_TYPES = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"}
DISCORD_CDN = ("cdn.discordapp.com", "media.discordapp.net")


def base_type(content_type: str | None) -> str:
    """'Video/MP4; codecs=avc1' -> 'video/mp4'."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def discord_hosted(url: str | None) -> bool:
    """An https link to Discord's own attachment CDN. Clips that are links to Medal, YouTube,
    Twitch and the rest are someone else's hosting and are never re-uploaded."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() in DISCORD_CDN


def media_problem(filename: str | None, content_type: str | None, size: int | None, url: str | None) -> str | None:
    """None if this attachment may be uploaded as is, else a short reason for staff."""
    if not discord_hosted(url):
        return "not a Discord attachment"
    ext = VIDEO_TYPES.get(base_type(content_type))
    if ext is None or not (filename or "").lower().endswith(ext):
        return "not an mp4, webm or mov video"
    if not isinstance(size, int) or size <= 0:
        return "unknown size"
    if size > MAX_BYTES:
        return "larger than the upload limit"
    return None


def pick_attachment(attachments) -> int | None:
    """Index of the first uploadable attachment. `attachments` is an iterable of
    (filename, content_type, size, url)."""
    for i, (filename, content_type, size, url) in enumerate(attachments):
        if media_problem(filename, content_type, size, url) is None:
            return i
    return None


def download_problem(status: int, content_type: str | None, size: int) -> str | None:
    """Check what the CDN actually sent, since the attachment's metadata came from the uploader."""
    if status != 200:
        return f"download failed (HTTP {int(status)})"
    if base_type(content_type) not in VIDEO_TYPES:
        return "download was not a video"
    if size <= 0:
        return "download was empty"
    if size > MAX_BYTES:
        return "larger than the upload limit"
    return None


# ---------------------------------------------------------------- member text
CAPTION_LEN = 60
NAME_LEN = 40
YT_TITLE_MAX = 100  # YouTube's limit
YT_DESCRIPTION_MAX = 4500  # under YouTube's 5000-byte limit
_URL = re.compile(r"<?https?://\S+", re.IGNORECASE)
_MENTION = re.compile(r"<(?:@[!&]?|#|a?:\w+:)\d+>")  # user, role, channel and custom-emoji tokens
_SPACE = re.compile(r"\s+")


def plain(text: str | None, limit: int) -> str:
    """Member text made safe for a YouTube title or description: no control characters, no
    angle brackets (YouTube rejects them), one line, cut to `limit`."""
    out = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    out = _SPACE.sub(" ", out.replace("<", "").replace(">", "")).strip()
    return out[:limit].rstrip()


def caption(content: str | None) -> str:
    """The clip's caption: what the member typed with the clip, minus links and mentions."""
    return plain(_MENTION.sub(" ", _URL.sub(" ", str(content or ""))), CAPTION_LEN)


def text_ok(text: str | None) -> bool:
    """Passes the bot-side filter with links refused (a title or a credit is no place for one)."""
    return textfilter.check(text, allow_links=False)[0]


def credit_name(display_name: str | None) -> str | None:
    """The name a member is credited under, or None if their display name can't be used
    (empty after cleaning, or it fails the text filter)."""
    name = plain(_MENTION.sub(" ", str(display_name or "")), NAME_LEN)
    return name if name and text_ok(name) else None


def video_title(clip_caption: str, server: str) -> str:
    server = plain(server, 40) or "our Discord"
    tail = f"Clip of the week from {server} #Shorts"
    head = plain(clip_caption, YT_TITLE_MAX - len(tail) - 3)
    return (f"{head} | {tail}" if head else tail)[:YT_TITLE_MAX]


def video_description(credit: str | None, server: str, join_url: str = JOIN_URL) -> str:
    """Bot-authored; the only member text in it is the already-cleaned credit name."""
    server = plain(server, 60) or "our Discord"
    who = f"Clip by {plain(credit, NAME_LEN)}" if credit else "Clip by one of our members"
    lines = [f"{who}, posted with their permission.", "",
             f"One of this week's best clips from {server}. Come play with us: {join_url}", "",
             "#Shorts #gaming #clips"]
    return "\n".join(lines)[:YT_DESCRIPTION_MAX]


# ---------------------------------------------------------------- selection
@dataclass(frozen=True)
class Candidate:
    """One clip from the week and everything the cog found out about it."""

    message_id: int
    user_id: int
    posted_at: int
    reactions: int = 0
    present: bool = True  # the author is still in the server
    tracking: bool = True  # not opted out with /privacy
    pref: str = ASK  # their standing preference
    media: str | None = None  # media_problem() of its best attachment
    caption_ok: bool = True


def why_not(c: Candidate) -> str | None:
    """Why a clip can't be shortlisted, or None."""
    if not c.present:
        return "left"
    if not c.tracking:
        return "privacy"
    if c.pref == NEVER:
        return "never"
    if not Q.established(c.user_id, c.posted_at):
        return "new account"
    if c.media is not None:
        return c.media
    if not c.caption_ok:
        return "caption"
    return None


def top(pool: list[clips.Clip], limit: int = TOP_N) -> list[clips.Clip]:
    """The best `limit` clips, at most one per author, by the ranking the clip-of-the-week
    poll uses (`clips.poll_entries`: most reactions, ties to the earlier post). One per
    author keeps the channel varied and stops one member filling every slot."""
    by_author: dict[int, list[clips.Clip]] = {}
    for c in pool:
        by_author.setdefault(c.user_id, []).append(c)
    best = [clips.poll_entries(mine, 1)[0] for mine in by_author.values()]
    return clips.poll_entries(best, limit)


def shortlist(candidates: list[Candidate], limit: int = TOP_N) -> list[Candidate]:
    """The clips to ask about this week (or approve, for a standing yes)."""
    ok = {c.message_id: c for c in candidates if why_not(c) is None}
    pool = [clips.Clip(c.message_id, c.user_id, c.posted_at, c.reactions) for c in ok.values()]
    return [ok[c.message_id] for c in top(pool, limit)]


def coin_ref(message_id: int) -> str:
    return f"clipcast:{message_id}"


PAYOUT_DAYS = 7  # the clip has to stay posted this long before it pays


def payout_due(published_at: int | None, now: int) -> bool:
    """COINS are paid late, not taken back early: a clip that comes down in its first
    PAYOUT_DAYS never pays, so post-collect-remove earns nothing (and there are no coins out
    there to be spent or given away before a clawback could reach them)."""
    return published_at is not None and now - published_at >= PAYOUT_DAYS * 24 * 60 * 60


def pays(user_id: int, at: int, tracking: bool, backers: int) -> bool:
    """Coins only for established, tracked authors whose clip at least one other established
    member reacted to, so nobody farms the slot alone or with fresh alts."""
    return tracking and backers >= 1 and Q.established(user_id, at)


_LINK = re.compile(r"/channels/\d+/\d+/(\d{15,20})\b")
_ID = re.compile(r"\d{15,20}")


def parse_clip_ref(text: str | None) -> int | None:
    """A clip's message id from a message link or a bare id."""
    text = (text or "").strip()
    found = _LINK.search(text)
    if found:
        return int(found.group(1))
    return int(text) if _ID.fullmatch(text) else None


# ---------------------------------------------------------------- YouTube settings
ENV_ID, ENV_SECRET, ENV_REFRESH, ENV_PRIVACY = (
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN", "YOUTUBE_PRIVACY")
PRIVACY_LEVELS = ("unlisted", "private", "public")
DEFAULT_PRIVACY = "unlisted"  # the owner reviews before making anything public


@dataclass(frozen=True)
class Setup:
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    privacy: str = DEFAULT_PRIVACY

    @property
    def missing(self) -> list[str]:
        """Names (never values) of the settings that aren't set."""
        pairs = ((ENV_ID, self.client_id), (ENV_SECRET, self.client_secret), (ENV_REFRESH, self.refresh_token))
        return [name for name, value in pairs if not value]

    @property
    def configured(self) -> bool:
        return not self.missing

    def __repr__(self) -> str:  # never let a credential reach a log line or a traceback
        return f"Setup(configured={self.configured}, privacy={self.privacy!r})"


def read_setup(environ) -> Setup:
    """YouTube settings from a mapping (the environment). Never raises; an unknown
    YOUTUBE_PRIVACY falls back to unlisted."""
    def get(key: str) -> str:
        return str(environ.get(key) or "").strip()
    privacy = get(ENV_PRIVACY).lower()
    return Setup(get(ENV_ID), get(ENV_SECRET), get(ENV_REFRESH),
                 privacy if privacy in PRIVACY_LEVELS else DEFAULT_PRIVACY)


# ---------------------------------------------------------------- YouTube wire format
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
UPLOAD_HOST = "www.googleapis.com"
UPLOAD_PATH = "/upload/youtube/v3/videos"
GAMING_CATEGORY = "20"
TOKEN_MARGIN = 60  # refresh this many seconds before the token runs out
# Reasons in a 403/429/400 reply that mean "stop for now", not "this clip is bad".
QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded",
                           "uploadLimitExceeded", "uploadRateLimitExceeded"})
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,20}")
_REASON = re.compile(r"[A-Za-z0-9_.-]{1,40}")


def _json(body: bytes | None):
    try:
        return json.loads((body or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def token_form(setup: Setup) -> dict[str, str]:
    return {"client_id": setup.client_id, "client_secret": setup.client_secret,
            "refresh_token": setup.refresh_token, "grant_type": "refresh_token"}


def parse_token(body: bytes, now: int) -> tuple[str, int] | None:
    """(access token, time to refresh it by) from the token endpoint's reply."""
    data = _json(body)
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        return None
    lifetime = data.get("expires_in")
    lifetime = lifetime if isinstance(lifetime, int) and not isinstance(lifetime, bool) and lifetime > 0 else 0
    return token, now + max(lifetime - TOKEN_MARGIN, 0)


def error_reason(body: bytes | None) -> str:
    """The machine-readable reason in a Google error reply, or ''. The reply is outside
    input: only a short identifier-shaped value is ever kept, never the message text."""
    data = _json(body)
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    reason = None
    if isinstance(error, str):  # the OAuth endpoint: {"error": "invalid_grant"}
        reason = error
    elif isinstance(error, dict):
        items = error.get("errors")
        first = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        reason = first.get("reason") or error.get("status")
    return reason if isinstance(reason, str) and _REASON.fullmatch(reason) else ""


def is_quota(status: int, body: bytes | None) -> bool:
    return status == 429 or (status in (400, 403) and error_reason(body) in QUOTA_REASONS)


def describe(step: str, status: int, body: bytes | None = None) -> str:
    """A secret-free one-liner for /clipcast status: 'upload start: HTTP 403 (forbidden)'."""
    reason = error_reason(body)
    return f"{step}: HTTP {int(status)}" + (f" ({reason})" if reason else "")


def video_resource(title: str, description: str, privacy: str) -> dict:
    return {
        "snippet": {"title": title, "description": description, "categoryId": GAMING_CATEGORY},
        "status": {"privacyStatus": privacy if privacy in PRIVACY_LEVELS else DEFAULT_PRIVACY,
                   "selfDeclaredMadeForKids": False},
    }


def safe_upload_url(location: str | None) -> bool:
    """The resumable session address must be Google's upload endpoint before the access
    token and the file are sent to it."""
    try:
        parts = urlsplit(location or "")
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == UPLOAD_HOST and parts.port in (None, 443)
            and not parts.username and parts.path == UPLOAD_PATH)


def video_id(body: bytes | None) -> str | None:
    data = _json(body)
    vid = data.get("id") if isinstance(data, dict) else None
    return vid if isinstance(vid, str) and _VIDEO_ID.fullmatch(vid) else None


def watch_url(vid: str) -> str:
    return f"https://youtu.be/{vid}"


# ---------------------------------------------------------------- staff text
def credit_label(credit: str | None, name: str | None) -> str:
    return f"credited as {name}" if credit == NAME and name else "anonymous"


def queue_counts(statuses) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in statuses:
        out[s] = out.get(s, 0) + 1
    return out


def queue_line(counts: dict[str, int]) -> str:
    parts = [f"{counts[s]} {STATUS_WORDS[s]}" for s in (ASKED, APPROVED, UPLOADING, *ATTENTION) if counts.get(s)]
    return "; ".join(parts) if parts else "empty"
