"""Discord Linked Roles: the metadata records Front Desk registers with Discord, and the plan
that takes an application from what it has to what it should have. Pure: no Discord, no network.

The definitions live in one file, site/api/linked-role/metadata.json, read here and by the
site's serverless functions (site/api/linked-role/_lib.js), so what is registered and what the
functions write for a member cannot drift apart. Each record there is a Discord "application
role connection metadata" object (type, key, name, description) plus one field of ours:
`service` (the Discord connection type the flag stands for) or `count` (the number of flags
that are on). Ours are stripped before anything is sent to Discord.

This is separate from /link steam (logic/gamelink.py). There a member proves a Steam profile
is theirs with a code and the bot remembers the profile. Here Discord itself vouches that the
member's Discord account has a verified connection; nothing is remembered by us and no account
name is read. See docs/LINKED_ROLES.md."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFINITIONS = Path(__file__).resolve().parents[2] / "site" / "api" / "linked-role" / "metadata.json"

# Discord's limits (developer docs, "Application Role Connection Metadata", read 2026-10-12).
MAX_RECORDS = 5
KEY = re.compile(r"[a-z0-9_]{1,50}")
MAX_NAME = 100
MAX_DESCRIPTION = 200
MAX_PLATFORM_NAME = 50
INTEGER_GREATER_THAN_OR_EQUAL = 2
BOOLEAN_EQUAL = 7
TYPES = range(1, 9)
SERVICE = re.compile(r"[a-z0-9-]{1,32}")
SENT = ("type", "key", "name", "description")  # the fields Discord gets and gives back
START_PATH = "/api/linked-role/start"
MAX_SHOWN = 120  # longest piece of Discord-supplied text printed in a diff


def problems(defs) -> list[str]:
    """Everything wrong with a definitions file; empty when it can be registered."""
    if not isinstance(defs, dict):
        return ["the file is not a JSON object"]
    out = []
    name = defs.get("platform_name")
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_PLATFORM_NAME:
        out.append(f"platform_name must be 1-{MAX_PLATFORM_NAME} characters")
    url = defs.get("verification_url")
    if not valid_url(url):
        out.append(f"verification_url must be an https URL ending in {START_PATH}")
    records = defs.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS:
        return [*out, f"records must be a list of 1-{MAX_RECORDS}"]
    keys = set()
    for i, r in enumerate(records):
        if not isinstance(r, dict):
            out.append(f"record {i} is not an object")
            continue
        key = r.get("key")
        label = key if isinstance(key, str) else f"record {i}"
        if not isinstance(key, str) or not KEY.fullmatch(key):
            out.append(f"{label}: key must be 1-50 of a-z, 0-9 and _")
        elif key in keys:
            out.append(f"{label}: duplicate key")
        else:
            keys.add(key)
        if not isinstance(r.get("name"), str) or not 1 <= len(r["name"]) <= MAX_NAME:
            out.append(f"{label}: name must be 1-{MAX_NAME} characters")
        if not isinstance(r.get("description"), str) or not 1 <= len(r["description"]) <= MAX_DESCRIPTION:
            out.append(f"{label}: description must be 1-{MAX_DESCRIPTION} characters")
        kind = r.get("type")
        if isinstance(kind, bool) or kind not in TYPES:
            out.append(f"{label}: type must be 1-8")
        service, count = r.get("service"), r.get("count")
        if (service is None) == (not count):
            out.append(f"{label}: needs exactly one of service or count")
        elif service is not None:
            if not isinstance(service, str) or not SERVICE.fullmatch(service):
                out.append(f"{label}: service is not a Discord connection type")
            if kind != BOOLEAN_EQUAL:
                out.append(f"{label}: a platform flag must be type {BOOLEAN_EQUAL} (boolean)")
        elif count is not True or kind != INTEGER_GREATER_THAN_OR_EQUAL:
            out.append(f"{label}: the count must be type {INTEGER_GREATER_THAN_OR_EQUAL} (at least N)")
        extra = sorted(set(r) - set(SENT) - {"service", "count"})
        if extra:
            out.append(f"{label}: unknown field {extra[0]}")
    return out


def valid_url(url) -> bool:
    """An https URL for this feature's start function, with nothing extra on it."""
    if not isinstance(url, str) or len(url) > 200 or any(ch.isspace() for ch in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or port is not None or "@" in parts.netloc:
        return False
    return parts.path == START_PATH and not parts.query and not parts.fragment


def load(path: Path = DEFINITIONS) -> dict:
    """The definitions file, checked; ValueError lists what is wrong with it."""
    defs = json.loads(Path(path).read_text(encoding="utf-8"))
    wrong = problems(defs)
    if wrong:
        raise ValueError("metadata.json: " + "; ".join(wrong))
    return defs


def records(defs: dict) -> list[dict]:
    """The records exactly as Discord takes them (our own fields removed)."""
    return [{k: r[k] for k in SENT} for r in defs["records"]]


def services(defs: dict) -> dict[str, str]:
    """metadata key -> Discord connection type, for the platform flags."""
    return {r["key"]: r["service"] for r in defs["records"] if r.get("service")}


# ---------------------------------------------------------------- plan
@dataclass
class Plan:
    lines: list[str] = field(default_factory=list)  # the diff, one line per record or setting
    put_records: list[dict] | None = None  # the full list to PUT, or None when nothing differs
    set_url: str | None = None  # the verification URL to set, or None when it is already right

    @property
    def changes(self) -> bool:
        return self.put_records is not None or self.set_url is not None


def _shown(value) -> str:
    """Discord-supplied text made safe for one line of terminal output."""
    text = value if isinstance(value, str) else repr(value)
    text = "".join(ch if ch.isprintable() else "?" for ch in text)
    return text if len(text) <= MAX_SHOWN else text[: MAX_SHOWN - 1] + "…"


def _clean(record) -> dict:
    return {k: record.get(k) for k in SENT} if isinstance(record, dict) else {}


def plan(current_records, current_url, defs: dict, url: str | None = None) -> Plan:
    """Compare what the application has with what it should have.

    `current_records` is the reply of GET .../role-connections/metadata, `current_url` the
    application's role_connections_verification_url. The PUT replaces the whole list, so a
    record Discord has and the file lacks is removed (and shown as such)."""
    want_url = defs["verification_url"] if url is None else url
    if not valid_url(want_url):
        raise ValueError(f"the verification URL must be an https URL ending in {START_PATH}")
    want = records(defs)
    have = [_clean(r) for r in current_records] if isinstance(current_records, list) else []
    have_by_key = {r["key"]: r for r in have if isinstance(r.get("key"), str)}
    out = Plan()
    for r in want:
        old = have_by_key.get(r["key"])
        if old is None:
            out.lines.append(f"  add     {r['key']}: {r['name']} (type {r['type']})")
        elif old == r:
            out.lines.append(f"  ok      {r['key']}")
        else:
            changed = [k for k in SENT if old.get(k) != r[k]]
            detail = ", ".join(f"{k} {_shown(old.get(k))!r} -> {r[k]!r}" for k in changed)
            out.lines.append(f"  change  {r['key']}: {detail}")
    wanted_keys = {r["key"] for r in want}
    for key in have_by_key:
        if key not in wanted_keys:
            out.lines.append(f"  remove  {_shown(key)} (not in metadata.json)")
    # Order is part of what members see in the role's requirements, so it counts as a difference.
    if have != want:
        out.put_records = want
        if all(line.startswith("  ok") for line in out.lines):
            out.lines.append("  reorder records to match metadata.json")
    if current_url == want_url:
        out.lines.append("  ok      verification URL")
    else:
        was = "not set" if not current_url else _shown(current_url)
        out.lines.append(f"  set     verification URL: {was} -> {want_url}")
        out.set_url = want_url
    return out
