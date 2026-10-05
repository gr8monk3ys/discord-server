"""Module 11: operations autopilot. Pure: no Discord, no database, no files.

- Daily job planning with the same rules as logic/schedule.py's weekly `plan()`.
- Backup file naming and retention.
- The error monitor's sliding window and alert cooldown, plus token redaction.
- The "back online" decision.
- Config drift: comparing snapshot dicts built by server/snapshot_lib.py.
"""

import re
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

DAY = timedelta(days=1)

# ---------------------------------------------------------------- daily schedule


@dataclass(frozen=True)
class Daily:
    name: str  # e.g. "backup"
    hour: int
    minute: int


@dataclass(frozen=True)
class DailyPeriod:
    key: str  # f"{name}:YYYY-MM-DD" of the local scheduled date
    scheduled_at: int  # unix seconds
    local_date: date


@dataclass(frozen=True)
class DailyPlan:
    run: DailyPeriod | None  # the one period to run now
    mark_done: list[DailyPeriod]  # record as done without running


def daily_occurrence(job: Daily, local_date: date, tz: tzinfo) -> DailyPeriod:
    at = int(datetime.combine(local_date, time(job.hour, job.minute), tzinfo=tz).timestamp())
    return DailyPeriod(f"{job.name}:{local_date.isoformat()}", at, local_date)


def latest_due_daily(job: Daily, now: int, tz: tzinfo) -> DailyPeriod:
    """The most recent period with scheduled_at <= now."""
    today = datetime.fromtimestamp(now, tz).date()
    p = daily_occurrence(job, today, tz)
    return p if p.scheduled_at <= now else daily_occurrence(job, today - DAY, tz)


def plan_daily(job: Daily, now: int, tz: tzinfo, done_keys: set[str], first_seen: int | None) -> DailyPlan:
    """Same rules as schedule.plan(): only the latest due period can run; the very first
    sighting (or a latest period older than first_seen) only marks it done; when running,
    older missed periods since first_seen are marked done, oldest first. The caller marks
    the run done only after it succeeds."""
    latest = latest_due_daily(job, now, tz)
    if latest.key in done_keys:
        return DailyPlan(None, [])
    if first_seen is None or latest.scheduled_at < first_seen:
        return DailyPlan(None, [latest])
    missed: list[DailyPeriod] = []
    p = daily_occurrence(job, latest.local_date - DAY, tz)
    while p.scheduled_at >= first_seen:
        if p.key not in done_keys:
            missed.append(p)
        p = daily_occurrence(job, p.local_date - DAY, tz)
    return DailyPlan(latest, missed[::-1])


# ---------------------------------------------------------------- backups

BACKUP_PREFIX = "front_desk-"
BACKUP_RE = re.compile(r"^front_desk-(\d{4}-\d{2}-\d{2})\.db$")
KEEP_BACKUPS = 14


def backup_name(local_date: date) -> str:
    return f"{BACKUP_PREFIX}{local_date.isoformat()}.db"


def backups_to_prune(names, keep: int = KEEP_BACKUPS) -> list[str]:
    """Backup file names beyond the newest `keep`, newest first. Other files are ignored."""
    dated = []
    for n in names:
        m = BACKUP_RE.match(n)
        if m:
            try:
                dated.append((date.fromisoformat(m.group(1)), n))
            except ValueError:
                continue
    dated.sort(reverse=True)
    return [n for _, n in dated[keep:]]


# ---------------------------------------------------------------- redaction

# Bot/user tokens: base64 user id . timestamp . HMAC; MFA tokens: "mfa." + long string.
TOKEN_RE = re.compile(r"(?<![\w-])[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,}")
MFA_RE = re.compile(r"mfa\.[\w-]{20,}")
MAX_LINE = 300


def redact(text: str) -> str:
    return MFA_RE.sub("[redacted]", TOKEN_RE.sub("[redacted]", text))


def first_line(message: str) -> str:
    line = (message or "").strip().splitlines()[0].strip() if (message or "").strip() else ""
    line = redact(line)
    if len(line) > MAX_LINE:
        line = line[: MAX_LINE - 1] + "…"
    return line or "(no message)"


# ---------------------------------------------------------------- error monitor

WINDOW = 10 * 60
THRESHOLD = 5  # alert when a logger has MORE than this many errors in WINDOW
ALERT_COOLDOWN = 60 * 60


class ErrorMonitor:
    """Counts ERROR records per logger in a sliding window. Not thread-safe by itself:
    the caller holds a lock."""

    def __init__(self, window: int = WINDOW, threshold: int = THRESHOLD, cooldown: int = ALERT_COOLDOWN,
                 last_alert: dict[str, int] | None = None):
        self.window = window
        self.threshold = threshold
        self.cooldown = cooldown
        self.last_alert: dict[str, int] = dict(last_alert or {})
        self.recent: dict[str, deque] = {}
        self.totals: dict[str, int] = {}

    def _trim(self, name: str, t: int) -> deque:
        q = self.recent.setdefault(name, deque())
        while q and q[0] <= t - self.window:
            q.popleft()
        return q

    def record(self, name: str, t: int) -> bool:
        """Count one error; True when this one should trigger an alert."""
        q = self._trim(name, t)
        q.append(t)
        self.totals[name] = self.totals.get(name, 0) + 1
        if len(q) <= self.threshold:
            return False
        last = self.last_alert.get(name)
        if last is not None and t - last < self.cooldown:
            return False
        self.last_alert[name] = t
        return True

    def count(self, name: str, t: int) -> int:
        return len(self._trim(name, t))

    def counts(self, t: int) -> dict[str, int]:
        out = {n: self.count(n, t) for n in list(self.recent)}
        return {n: c for n, c in out.items() if c}


# ---------------------------------------------------------------- back online

DOWN_THRESHOLD = 30 * 60
ONLINE_NOTE_COOLDOWN = 6 * 60 * 60


def should_note_online(heartbeat: int | None, now: int, last_note: int | None) -> bool:
    """Post "back online" when the last heartbeat is more than 30 min old, at most every 6 h."""
    if heartbeat is None or now - heartbeat <= DOWN_THRESHOLD:
        return False
    return last_note is None or now - last_note >= ONLINE_NOTE_COOLDOWN


# ---------------------------------------------------------------- drift


@dataclass(frozen=True)
class Drift:
    added: list[str]
    removed: list[str]
    changed: list[tuple[str, list[str]]]  # (name, changed field names)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def _unique(name: str, seen: dict) -> str:
    n = seen.get(name, 0) + 1
    seen[name] = n
    return name if n == 1 else f"{name} ({n})"


def flatten_channels(snapshot: dict) -> dict[str, dict]:
    """channels_snapshot() output -> {name: channel dict + "category"}; categories included
    (without their child list). Duplicate names become "name (2)" in sidebar order."""
    out: dict[str, dict] = {}
    seen: dict[str, int] = {}
    for cat in snapshot.get("categories", []):
        entry = {k: v for k, v in cat.items() if k != "channels"}
        entry["category"] = None
        key = _unique(cat["name"], seen)
        out[key] = {**entry, "name": key}
        for ch in cat.get("channels", []):
            k = _unique(ch["name"], seen)
            out[k] = {**ch, "name": k, "category": cat["name"]}
    for ch in snapshot.get("uncategorized", []):
        k = _unique(ch["name"], seen)
        out[k] = {**ch, "name": k, "category": None}
    return out


def diff_named(old: list[dict], new: list[dict]) -> Drift:
    """Compare two lists of dicts keyed by "name" (first wins for duplicates)."""
    before: dict[str, dict] = {}
    after: dict[str, dict] = {}
    for src, dst in ((old, before), (new, after)):
        for item in src:
            dst.setdefault(item["name"], item)
    added = sorted(n for n in after if n not in before)
    removed = sorted(n for n in before if n not in after)
    changed = []
    for n in sorted(set(before) & set(after)):
        a, b = before[n], after[n]
        fields = sorted(k for k in set(a) | set(b) if k != "name" and a.get(k) != b.get(k))
        if fields:
            changed.append((n, fields))
    return Drift(added, removed, changed)


def _safe(text: str) -> str:
    """Names are shown in an embed: no markdown, no pings."""
    text = re.sub(r"([\\*_~`|>])", r"\\\1", str(text))
    return text.replace("@", "@​")


def drift_summary(roles: Drift, channels: Drift, limit: int = 3800) -> str:
    """Short text for the mod log; "" when nothing changed."""
    lines: list[str] = []
    for title, d in (("Roles", roles), ("Channels", channels)):
        if not d:
            continue
        lines.append(f"**{title}**")
        lines += [f"+ {_safe(n)}" for n in d.added]
        lines += [f"- {_safe(n)}" for n in d.removed]
        lines += [f"~ {_safe(n)} ({', '.join(f)})" for n, f in d.changed]
    out: list[str] = []
    used = 0  # characters so far, newlines included
    for i, line in enumerate(lines):
        last = i == len(lines) - 1
        tail = f"… and {len(lines) - i} more"
        # Always leave room for the tail unless this is the final line.
        if used + len(line) + (0 if last else 1 + len(tail)) > limit:
            out.append(tail)
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


# ---------------------------------------------------------------- formatting


def fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"  # pragma: no cover


def fmt_uptime(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"
