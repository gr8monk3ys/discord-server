"""Staff application rules: the five questions, who may apply, who may press which
review button, status moves, and the text of replies, DMs and the /apps list.
Pure: no Discord, no database, no network. Callers escape member text before it
reaches any of the text builders here."""

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

import config

DAY = 24 * 60 * 60
MIN_MEMBER_DAYS = 30  # in the server at least this long
MIN_ACCOUNT_DAYS = 90  # Discord account at least this old
RECORD_DAYS = 60  # no warnings or timeouts in this window
COOLDOWN_DAYS = 60  # after a denial
RECORD_WINDOW = RECORD_DAYS * DAY
COOLDOWN = COOLDOWN_DAYS * DAY
# Case kinds (cases table, cogs/moderation.py) that count against an applicant.
RECORD_KINDS = ("warn", "timeout", "auto_timeout", "spam")

OPEN = ("pending", "interview")  # one of these at a time per member
FIELD_MAX = 1024  # embed field value limit
LIST_MAX = 3900  # stay under the 4096 embed description limit


@dataclass(frozen=True)
class Question:
    key: str
    label: str  # <= 45 characters (Discord modal label limit)
    description: str  # <= 100 characters
    max_length: int
    paragraph: bool
    placeholder: str = ""


QUESTIONS = (
    Question("age", "Are you 18 or older?", "Staff must be adults. Type yes to confirm.", 10, False, "yes"),
    Question("timezone", "Timezone and hours you're active", "For example: Pacific, weekday evenings 6-11pm",
             100, False),
    Question("experience", "Moderation experience", "Servers you've helped run, roles you held, or none yet.",
             1000, True),
    Question("voice", "Handling a heated argument in voice",
             "Two members are yelling at each other in voice. What do you do?", 1000, True),
    Question("why", "Why do you want to help?", "What would you like to do for this server?", 1000, True),
)
KEYS = tuple(q.key for q in QUESTIONS)
LABELS = {q.key: q.label for q in QUESTIONS}

REPLIES = {
    "staff": "You're already on the staff team.",
    "member_age": "Staff applications open after 30 days in the server. Come back in {n} more {days}.",
    "account_age": "Your Discord account needs to be at least 90 days old to apply. Try again in {n} more {days}.",
    "timed_out": "You can't apply while you're timed out.",
    "record": "You can't apply with a warning or timeout in the last 60 days.",
    "open": "You already have an application in. Check it with `/apply status`.",
    "cooldown": "Your last application was turned down recently. You can apply again in {n} {days}.",
    "age": "Staff must be 18 or older. Type **yes** in the first box to confirm you are.",
    "blank": "Please answer every question.",
}

STATUS_WORDS = {"pending": "pending review", "interview": "at the interview stage",
                "approved": "approved", "denied": "not accepted"}


# ---------------------------------------------------------------- answers
def clean_answer(text: str | None, limit: int = 1000) -> str:
    """Trim, keep paragraphs but no runs of blank lines, cap the length."""
    text = re.sub(r"\n{3,}", "\n\n", (text or "").replace("\r\n", "\n")).strip()
    return text[:limit]


def age_confirmed(text: str | None) -> bool:
    """Only a plain yes counts ("Yes", "yes!"), not "yeah" or "yes I'm 16"."""
    return re.sub(r"[^a-z]", "", (text or "").lower()) == "yes" and len((text or "").split()) == 1


def answers_problem(answers: Mapping[str, str]) -> str | None:
    if any(not (answers.get(k) or "").strip() for k in KEYS):
        return "blank"
    if not age_confirmed(answers["age"]):
        return "age"
    return None


def dump_answers(answers: Mapping[str, str]) -> str:
    return json.dumps({k: answers.get(k, "") for k in KEYS}, ensure_ascii=False)


def load_answers(raw: str | None) -> dict:
    try:
        data = json.loads(raw or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- eligibility
def _days_left(since: int, need: int, now: int) -> int:
    return max(1, math.ceil((since + need - now) / DAY))


def member_problem(*, joined_at: int | None, created_at: int, timed_out: bool, recent_cases: int,
                   is_staff: bool, now: int) -> tuple[str, int] | None:
    """(problem, days) if this member can't apply; checks that don't need their application history."""
    if is_staff:
        return ("staff", 0)
    if joined_at is None:
        return ("member_age", MIN_MEMBER_DAYS)
    if now - joined_at < MIN_MEMBER_DAYS * DAY:
        return ("member_age", _days_left(joined_at, MIN_MEMBER_DAYS * DAY, now))
    if now - created_at < MIN_ACCOUNT_DAYS * DAY:
        return ("account_age", _days_left(created_at, MIN_ACCOUNT_DAYS * DAY, now))
    if timed_out:
        return ("timed_out", 0)
    if recent_cases:
        return ("record", 0)
    return None


def denial_left(rows: Iterable[Mapping], now: int) -> int:
    """Seconds left on the cooldown after this member's latest denial (0 if none)."""
    denials = [r["decided_at"] or r["created_at"] for r in rows if r["status"] == "denied"]
    return max(0, max(denials) + COOLDOWN - now) if denials else 0


def history_problem(rows: Iterable[Mapping], now: int) -> tuple[str, int] | None:
    """One open application at a time, and COOLDOWN_DAYS after the latest denial."""
    rows = list(rows)
    if any(r["status"] in OPEN for r in rows):
        return ("open", 0)
    left = denial_left(rows, now)
    if left > 0:
        return ("cooldown", math.ceil(left / DAY))
    return None


def reply(code: str, n: int = 0) -> str:
    return REPLIES[code].format(n=n, days="day" if n == 1 else "days")


# ---------------------------------------------------------------- review buttons
def _has(role_names: Iterable[str], name: str) -> bool:
    return config.match_by_name([_N(r) for r in role_names], name) is not None


@dataclass(frozen=True)
class _N:
    name: str


def can_decide(*, is_owner: bool, role_names: Iterable[str]) -> bool:
    """Approve / Deny: Keepers and the owner only."""
    return is_owner or _has(role_names, config.KEEPER_ROLE)


def can_interview(*, is_owner: bool, role_names: Iterable[str]) -> bool:
    role_names = list(role_names)
    return can_decide(is_owner=is_owner, role_names=role_names) or _has(role_names, config.MOD_ROLE)


def allowed(action: str, *, is_owner: bool, role_names: Iterable[str]) -> bool:
    if action == "interview":
        return can_interview(is_owner=is_owner, role_names=role_names)
    if action in ("approve", "deny"):
        return can_decide(is_owner=is_owner, role_names=role_names)
    return False


TARGET = {"approve": "approved", "deny": "denied", "interview": "interview"}
MOVES = {"pending": {"interview", "approve", "deny"}, "interview": {"approve", "deny"}}


def target(action: str) -> str:
    return TARGET[action]


def can_move(status: str, action: str) -> bool:
    return action in MOVES.get(status, ())


def role_grantable(*, role_position: int | None, bot_top: int) -> str | None:
    """None if the bot can hand out the role; else why not. Never reorders anything."""
    if role_position is None:
        return "no_role"
    if role_position >= bot_top:
        return "above_bot"
    return None


# ---------------------------------------------------------------- text
def plural(n: int, unit: str) -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


def days_ago(ts: int | None, now: int) -> str:
    if ts is None:
        return "unknown"
    return plural(max(0, (now - ts) // DAY), "day")


def fit_field(text: str) -> str:
    if not text:
        return "(blank)"
    return text if len(text) <= FIELD_MAX else text[:FIELD_MAX - 1] + "…"


def status_text(row: Mapping | None, now: int) -> str:
    """/apply status for the applicant's latest application."""
    if row is None:
        return "You haven't applied yet. Use `/apply staff` to send an application."
    head = f"Application #{row['id']} (sent <t:{row['created_at']}:R>) is **{STATUS_WORDS.get(row['status'], row['status'])}**."
    if row["status"] == "pending":
        return head + " The staff team will review it and I'll DM you the result."
    if row["status"] == "interview":
        return head + " A staff member will reach out to you."
    if row["status"] == "approved":
        return head + " Welcome to the team!"
    left = denial_left([row], now)
    if left > 0:
        return head + f" You can apply again in {plural(math.ceil(left / DAY), 'day')}."
    return head + " You can apply again with `/apply staff`."


def dm_text(kind: str, guild_name: str) -> str:
    """`guild_name` must already be escaped."""
    if kind == "interview":
        return (f"Thanks for applying to the staff team on **{guild_name}**! Your application moved to the "
                "interview stage: a staff member will reach out to you soon.")
    if kind == "approved":
        return (f"Good news: your staff application on **{guild_name}** was approved. Welcome to the team! "
                "Check the staff channels for what's next.")
    return (f"Thanks for applying to the staff team on **{guild_name}**. We aren't taking your application "
            f"forward this time. You're welcome to apply again in {COOLDOWN_DAYS} days, and thank you for "
            "being part of the community.")


def list_lines(rows: Iterable[Mapping], link: Callable[[Mapping], str]) -> str:
    """Staff /apps list: open applications, oldest first."""
    rows = list(rows)
    if not rows:
        return "No open applications."
    out: list[str] = []
    size = 0
    for i, r in enumerate(rows):
        tag = " · 🎙️ interview" if r["status"] == "interview" else ""
        url = link(r)
        line = f"`#{r['id']}` <@{r['user_id']}> · <t:{r['created_at']}:R>{tag}" + (f" · [card]({url})" if url else "")
        if size + len(line) + 40 > LIST_MAX:
            out.append(f"…and {len(rows) - i} more.")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)
