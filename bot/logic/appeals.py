"""Ban appeal rules: the appeal code, reading the payload the website function posts
through the staff webhook, who may appeal when, status moves and the text staff and
banned members see. Pure: no Discord, no database, no network, no clock of its own.

The code is base32(HMAC-SHA256(APPEAL_SECRET, str(user_id)))[:16]: 80 bits that only
someone holding the secret can produce, so the website function (site/src/server/appeal.ts,
which computes the same thing) forwards nothing for a made-up user ID, and the bot
checks it again before it believes a webhook message. Callers escape member text
before it reaches a Discord message; nothing here does."""

import base64
import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from logic import textfilter
from logic.ops import Daily

DAY = 24 * 60 * 60
CODE_LEN = 16  # base32 characters: 80 bits
PAYLOAD_VERSION = 1
PAYLOAD_MAX = 4096  # an embed description; the function refuses to send more
STALE_DAYS = 30  # an open appeal nobody touched for this long is closed
STALE = STALE_DAYS * DAY
ANSWER_RETENTION = 180 * DAY  # answers are blanked this long after the decision
REASON_MAX = 300
FIELD_MAX = 1024  # embed field value limit
LIST_MAX = 3900  # stay under the 4096 embed description limit
RECENT_SHOWN = 10
NOTE_EVERY = DAY  # one mod-log note per member and reason per day about dropped appeals

EXPIRE_JOB = Daily("appeals_expire", 9, 0)

OPEN, ACCEPTED, DENIED, EXPIRED, CLOSED = "open", "accepted", "denied", "expired", "closed"
STATUS_WORDS = {OPEN: "open", ACCEPTED: "accepted", DENIED: "denied", EXPIRED: "closed (no decision in "
                f"{STALE_DAYS} days)", CLOSED: "closed (unbanned outside the appeal)"}


@dataclass(frozen=True)
class Question:
    key: str
    label: str
    max_length: int
    required: bool


# Keys, order and caps are mirrored in site/src/lib/appeal-form.ts and site/src/server/appeal.ts.
QUESTIONS = (
    Question("what", "What happened?", 600, True),
    Question("why", "Why should we reconsider?", 600, True),
    Question("else", "Anything else?", 300, False),
)
KEYS = tuple(q.key for q in QUESTIONS)


# ---------------------------------------------------------------- the code
def appeal_code(secret: str, user_id: int) -> str:
    digest = hmac.new(secret.encode("utf-8"), str(int(user_id)).encode("ascii"), hashlib.sha256).digest()
    return base64.b32encode(digest).decode("ascii")[:CODE_LEN]


def normalize_code(text: str | None) -> str | None:
    """Upper-case with spaces and dashes removed, or None if it can't be a code."""
    code = re.sub(r"[\s-]", "", str(text or "")).upper()
    return code if re.fullmatch(rf"[A-Z2-7]{{{CODE_LEN}}}", code) else None


def code_matches(secret: str, user_id: int, code: str | None) -> bool:
    given = normalize_code(code)
    if given is None:
        return False
    return hmac.compare_digest(appeal_code(secret, user_id).encode("ascii"), given.encode("ascii"))


def fmt_code(code: str) -> str:
    """ABCD-EFGH-IJKL-MNOP: easier to copy by hand; the form accepts it with or without dashes."""
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


# ---------------------------------------------------------------- the payload
@dataclass(frozen=True)
class Payload:
    user_id: int
    code: str
    answers: dict[str, str]


def clean_answer(text: str | None, limit: int) -> str:
    """Hostile input: drop control and invisible format characters, keep paragraphs but no
    runs of blank lines, cap the length."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)  # tabs and odd spaces become one space before the strip below
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:limit].strip()


def parse_payload(text: str | None) -> Payload | None:
    """The JSON the website function puts in the webhook embed:
    {"v": 1, "u": "<user id>", "c": "<code>", "a": [what, why, else]}. None for anything else."""
    if not text or len(text) > PAYLOAD_MAX:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != PAYLOAD_VERSION:
        return None
    uid, code, raw = data.get("u"), data.get("c"), data.get("a")
    if not isinstance(uid, str) or not re.fullmatch(r"[1-9][0-9]{14,19}", uid):
        return None
    if not isinstance(code, str) or normalize_code(code) is None:
        return None
    if not isinstance(raw, list) or len(raw) != len(QUESTIONS) or not all(isinstance(a, str) for a in raw):
        return None
    answers = {q.key: clean_answer(a, q.max_length) for q, a in zip(QUESTIONS, raw)}
    if any(q.required and not answers[q.key] for q in QUESTIONS):
        return None
    return Payload(int(uid), normalize_code(code), answers)


def dump_answers(answers: Mapping[str, str]) -> str:
    return json.dumps({k: answers.get(k, "") for k in KEYS}, ensure_ascii=False)


def load_answers(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


HIDDEN = {textfilter.INVITE: "an invite link", textfilter.LINK: "a link", textfilter.WORD: "blocked language"}


def screen_answer(text: str) -> tuple[str | None, str | None]:
    """(text, None) if staff may see it as typed, else (None, what it contained). Links are
    never shown: an appeal is the one place a banned person can put text in front of staff."""
    ok, reason = textfilter.check(text, allow_links=False)
    return (text, None) if ok else (None, HIDDEN.get(reason, "blocked language"))


def fit_field(text: str) -> str:
    if not text:
        return "(blank)"
    return text if len(text) <= FIELD_MAX else text[:FIELD_MAX - 1] + "…"


# ---------------------------------------------------------------- who may appeal when
DROP_NOTES = {
    "forged": "⚠️ A message in the appeals channel failed verification and was deleted. Nothing was "
              "posted. If this repeats, the webhook URL has probably leaked: delete the webhook, make "
              "a new one and update `APPEAL_WEBHOOK_URL` in Vercel.",
    "not_banned": "🔕 Dropped a ban appeal from {who}: they aren't banned.",
    "open": "🔕 Dropped a ban appeal from {who}: they already have one open. Further copies are dropped quietly.",
    "cooling": "🔕 Dropped a ban appeal from {who}: their cooling-off period isn't over ({n} more {days}).",
    "cooldown": "🔕 Dropped a ban appeal from {who}: their last appeal was denied recently "
                "({n} more {days} until they can appeal again).",
}


def ingest_problem(*, banned: bool, rows: Iterable[Mapping], banned_at: int | None, now: int,
                   cooling: int, reappeal: int) -> tuple[str, int] | None:
    """(reason, days left) if this appeal must be dropped, else None. `rows` are the member's
    earlier appeals; `banned_at` is None for bans the bot never saw (no cooling-off then)."""
    if not banned:
        return ("not_banned", 0)
    rows = list(rows)
    if any(r["status"] == OPEN for r in rows):
        return ("open", 0)
    if banned_at is not None and now < banned_at + cooling:
        return ("cooling", math.ceil((banned_at + cooling - now) / DAY))
    denials = [r["decided_at"] or r["created_at"] for r in rows if r["status"] == DENIED]
    if denials and now < max(denials) + reappeal:
        return ("cooldown", math.ceil((max(denials) + reappeal - now) / DAY))
    return None


def drop_note(reason: str, who: str, n: int = 0) -> str:
    return DROP_NOTES[reason].format(who=who, n=n, days="day" if n == 1 else "days")


class DropNotes:
    """Rate limit for the mod-log notes about dropped appeals, in memory: a banned member
    resubmitting a valid code a hundred times produces one line a day, not a hundred."""

    def __init__(self):
        self.last: dict[tuple[int, str], int] = {}

    def should_note(self, user_id: int, reason: str, now: int) -> bool:
        for key in [k for k, at in self.last.items() if now - at >= NOTE_EVERY]:
            del self.last[key]
        if (user_id, reason) in self.last:
            return False
        self.last[(user_id, reason)] = now
        return True


TARGET = {"accept": ACCEPTED, "deny": DENIED}


def can_move(status: str, action: str) -> bool:
    return status == OPEN and action in TARGET


def is_stale(touched_at: int, now: int) -> bool:
    return now - touched_at >= STALE


# ---------------------------------------------------------------- text
def plural(n: int, unit: str) -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


def ban_notice(*, page_url: str, user_id: int, code: str, banned_at: int, cooling: int, reappeal: int) -> str:
    """The appeal part of the ban DM. Sent before the ban: afterwards the bot can't reach them."""
    opens = banned_at + cooling
    when = f"from <t:{opens}:D> ({plural(math.ceil(cooling / DAY), 'day')} from now)" if cooling > 0 else "now"
    return "\n".join([
        f"**Appealing** You can ask the staff team to reconsider {when}. Appeals sent earlier are discarded.",
        f"Appeal page: <{page_url}>",
        f"Your user ID: `{user_id}`",
        f"Your appeal code: `{fmt_code(code)}`",
        "Keep this message. The code is the only way to appeal, and I can't message you again once you're "
        f"banned. One appeal at a time; after a denial you can appeal again in {plural(reappeal // DAY, 'day')}.",
    ])


def outcome_dm(kind: str, guild_name: str, reason: str, join_url: str, reappeal: int) -> str:
    """`guild_name` and `reason` must already be escaped."""
    if kind == ACCEPTED:
        return (f"Your ban appeal on **{guild_name}** was accepted and the ban is lifted. "
                f"You can come back with <{join_url}>. Please read the rules again when you do.")
    lines = [f"Your ban appeal on **{guild_name}** was reviewed and denied."]
    if reason:
        lines.append(f"**Reason** {reason}")
    lines.append(f"You can appeal again in {plural(reappeal // DAY, 'day')} with the same code.")
    return "\n".join(lines)


NO_DM_NOTE = "I couldn't DM them (normal for a banned user): they only find out by trying the invite."


def list_text(open_rows: Iterable[Mapping], recent_rows: Iterable[Mapping], link: Callable[[Mapping], str]) -> str:
    """Staff /appeals list: open appeals oldest first, then the latest decisions."""
    open_rows, recent_rows = list(open_rows), list(recent_rows)
    out = ["**Open**"]
    size = 0
    for i, r in enumerate(open_rows):
        url = link(r)
        line = f"`#{r['id']}` <@{r['user_id']}> · <t:{r['created_at']}:R>" + (f" · [card]({url})" if url else "")
        if size + len(line) > LIST_MAX - 1200:
            out.append(f"…and {len(open_rows) - i} more.")
            break
        out.append(line)
        size += len(line) + 1
    if not open_rows:
        out.append("No open appeals.")
    out.append("\n**Recent decisions**")
    for r in recent_rows[:RECENT_SHOWN]:
        by = f"<@{r['decided_by']}>" if r["decided_by"] else "automatic"
        when = f"<t:{r['decided_at']}:R>" if r["decided_at"] else "unknown"
        out.append(f"`#{r['id']}` <@{r['user_id']}> · {STATUS_WORDS.get(r['status'], r['status'])} · {when} · {by}")
    if not recent_rows:
        out.append("None yet.")
    return "\n".join(out)
