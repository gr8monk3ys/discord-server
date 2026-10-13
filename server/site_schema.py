"""Schema and leak guard for the landing page's live data (stats.json).

The bot writes this document (bot/logic/sitedata.py) and server/publish_site_data.py pushes
it to a public branch, so both sides check it with the same code. Pure: no I/O.

The document is AGGREGATES ONLY. `validate()` is a whitelist: a key, type or game name it
does not know is a problem, so a new field cannot reach the public page without a change
here. On top of the shape it refuses anything that could identify a member: a number or a
run of digits long enough to be a Discord ID (15 digits and up), an `@` or `<...>` mention,
a link or an invite. Problems name the place, never the value.

    {"v": 1, "generated_at": "2026-10-12T18:30:00Z",
     "members": {"total": 15, "online": 6},
     "week": {"messages": 1240, "voice_hours": 34},
     "events": [{"title": "Overwatch game night", "game": "Overwatch",
                 "start": "2026-10-17T03:00:00Z", "kind": "gamenight"}],
     "games": [{"name": "Overwatch", "hours": 12}],
     "tournaments": {"open": 1, "running": 0, "finished": 3},
     "matchmaking": {"matches_week": 4, "queued": 3},
     "seasonal": {"name": "Spooky Season", "emoji": "🎃", "ends": "2026-11-01T07:00:00Z"},
     "houses": [{"name": "Ember", "points": 120}],
     "hall": {"starred": 12, "clips": 30, "badges": 87}}

Every key but `v` and `generated_at` is optional.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone

import layout

VERSION = 1
FILE_NAME = "stats.json"
MAX_BYTES = 8192  # the whole file; a real one is well under 2 KB
MAX_COUNT = 10_000_000  # far above anything this server counts, far below an ID
MAX_EVENTS = 8
MAX_GAMES = 5
MAX_HOUSES = 8
MAX_TITLE = 80
MAX_NAME = 40
MAX_EMOJI = 16
KINDS = ("gamenight", "tournament", "event")
GAME_NAMES = frozenset(g[2] for g in layout.GAMES)

TOP_KEYS = ("v", "generated_at", "members", "week", "events", "games", "tournaments", "matchmaking",
            "seasonal", "houses", "hall")

ID_DIGITS = 15  # Discord IDs are 17-20 digits today; 15 leaves a margin
_ID_LIKE = re.compile(r"\d{%d,}" % ID_DIGITS)
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_LINK = re.compile(r"://|\bwww\.|discord\s*\.\s*gg|discord(?:app)?\s*\.\s*com\s*/\s*invite|dsc\s*\.\s*gg", re.I)
_MARKUP = re.compile(r"[@<>`\\]")  # mentions, custom emoji, HTML, code spans, escapes
# Control and invisible direction characters. U+200D (zero-width joiner) stays: emoji use it.
_CONTROL = re.compile(r"[\x00-\x1f\x7f​‌‎‏‪-‮⁦-⁩]")


def iso(ts: int | float) -> str:
    """Unix seconds as the UTC timestamp the document uses."""
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text) -> int | None:
    """Unix seconds for a document timestamp; None when it is not one."""
    if not isinstance(text, str) or not _ISO.fullmatch(text):
        return None
    try:
        return int(datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def text_problem(text: str) -> str | None:
    """Why a string must not be published, or None. Never includes the string."""
    if _ID_LIKE.search(text):
        return "looks like a Discord ID"
    if _MARKUP.search(text):
        return "contains a mention or markup"
    if _LINK.search(text):
        return "contains a link"
    if _CONTROL.search(text):
        return "contains control characters"
    return None


def find_leaks(data) -> list[str]:
    """ID-like numbers, mentions and links anywhere in `data`, keys included."""
    problems: list[str] = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(k, f"{path}.<key>")
                walk(v, f"{path}.{k}" if isinstance(k, str) and not text_problem(k) else f"{path}.?")
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, int):
            if len(str(abs(node))) >= ID_DIGITS:
                problems.append(f"{path}: looks like a Discord ID")
        elif isinstance(node, float):
            problems.append(f"{path}: not a whole number")
        elif isinstance(node, str):
            why = text_problem(node)
            if why:
                problems.append(f"{path}: {why}")
        else:
            problems.append(f"{path}: unexpected type")

    walk(data, "$")
    return problems


# ---------------------------------------------------------------- shape
def _count(out: list[str], node, path: str) -> None:
    if isinstance(node, bool) or not isinstance(node, int) or not 0 <= node <= MAX_COUNT:
        out.append(f"{path}: not a count")


def _text(out: list[str], node, path: str, limit: int) -> None:
    if not isinstance(node, str) or not node.strip() or len(node) > limit or node != node.strip():
        out.append(f"{path}: not a short text")


def _time(out: list[str], node, path: str) -> None:
    if parse_iso(node) is None:
        out.append(f"{path}: not a UTC timestamp")


def _keys(out: list[str], node, path: str, required: tuple, optional: tuple = ()) -> bool:
    """True when `node` is an object with all `required` keys and nothing unknown."""
    if not isinstance(node, dict):
        out.append(f"{path}: not an object")
        return False
    ok = True
    for k in node:
        if k not in required and k not in optional:
            out.append(f"{path}: unknown key")
            ok = False
    for k in required:
        if k not in node:
            out.append(f"{path}.{k}: missing")
            ok = False
    return ok


def _counts(out: list[str], node, path: str, required: tuple, optional: tuple = ()) -> None:
    if _keys(out, node, path, required, optional):
        if not node:
            out.append(f"{path}: empty")
        for k, v in node.items():
            _count(out, v, f"{path}.{k}")


def _list(out: list[str], node, path: str, limit: int) -> bool:
    if not isinstance(node, list) or not 1 <= len(node) <= limit:
        out.append(f"{path}: not a list of 1 to {limit}")
        return False
    return True


def _game(out: list[str], node, path: str, game_names) -> None:
    if not isinstance(node, str) or node not in game_names:
        out.append(f"{path}: not a game from layout.py")


def validate(doc, game_names=GAME_NAMES) -> list[str]:
    """Every reason `doc` must not be published; an empty list means it is fine."""
    out: list[str] = []
    if not isinstance(doc, dict):
        return ["$: not an object"]
    for k in doc:
        if k not in TOP_KEYS:
            out.append("$: unknown key")
    if doc.get("v") != VERSION or isinstance(doc.get("v"), bool):
        out.append("$.v: unsupported version")
    _time(out, doc.get("generated_at"), "$.generated_at")

    if "members" in doc:
        _counts(out, doc["members"], "$.members", ("total",), ("online",))
    if "week" in doc:
        _counts(out, doc["week"], "$.week", (), ("messages", "voice_hours"))
    if "events" in doc and _list(out, doc["events"], "$.events", MAX_EVENTS):
        for i, e in enumerate(doc["events"]):
            p = f"$.events[{i}]"
            if _keys(out, e, p, ("title", "start", "kind"), ("game",)):
                _text(out, e["title"], f"{p}.title", MAX_TITLE)
                _time(out, e["start"], f"{p}.start")
                if e["kind"] not in KINDS:
                    out.append(f"{p}.kind: unknown kind")
                if "game" in e:
                    _game(out, e["game"], f"{p}.game", game_names)
    if "games" in doc and _list(out, doc["games"], "$.games", MAX_GAMES):
        for i, g in enumerate(doc["games"]):
            p = f"$.games[{i}]"
            if _keys(out, g, p, ("name", "hours")):
                _game(out, g["name"], f"{p}.name", game_names)
                _count(out, g["hours"], f"{p}.hours")
    if "tournaments" in doc:
        _counts(out, doc["tournaments"], "$.tournaments", (), ("open", "running", "finished"))
    if "matchmaking" in doc:
        _counts(out, doc["matchmaking"], "$.matchmaking", (), ("matches_week", "queued"))
    if "seasonal" in doc:
        s = doc["seasonal"]
        if _keys(out, s, "$.seasonal", ("name", "ends"), ("emoji",)):
            _text(out, s["name"], "$.seasonal.name", MAX_NAME)
            _time(out, s["ends"], "$.seasonal.ends")
            if "emoji" in s:
                _text(out, s["emoji"], "$.seasonal.emoji", MAX_EMOJI)
    if "houses" in doc and _list(out, doc["houses"], "$.houses", MAX_HOUSES):
        for i, h in enumerate(doc["houses"]):
            p = f"$.houses[{i}]"
            if _keys(out, h, p, ("name", "points")):
                _text(out, h["name"], f"{p}.name", MAX_NAME)
                _count(out, h["points"], f"{p}.points")
    if "hall" in doc:
        _counts(out, doc["hall"], "$.hall", (), ("starred", "clips", "badges"))

    out.extend(find_leaks(doc))
    return out


# ---------------------------------------------------------------- text form
def dumps(doc) -> str:
    """Stable text: sorted keys, so the same data is always the same bytes."""
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def check_text(text: str, game_names=GAME_NAMES) -> tuple[dict | None, list[str]]:
    """(document, problems) for a stats.json file's text. The document is None when the
    text is too big or not JSON."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        return None, [f"$: larger than {MAX_BYTES} bytes"]
    try:
        doc = json.loads(text)
    except ValueError:
        return None, ["$: not JSON"]
    return (doc if isinstance(doc, dict) else None), validate(doc, game_names)


def content_key(doc: dict) -> str:
    """The document without the parts that change on every run (`generated_at` and the
    online count), so the publisher can tell "nothing new" from "worth a push"."""
    core = copy.deepcopy(doc)
    core.pop("generated_at", None)
    if isinstance(core.get("members"), dict):
        core["members"].pop("online", None)
    return dumps(core)
