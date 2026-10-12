"""Module 11: operations autopilot. Pure: no Discord, no database, no files.

- Daily job planning with the same rules as logic/schedule.py's weekly `plan()`.
- Backup file naming and retention.
- The error monitor's sliding window and alert cooldown, plus token redaction.
- The "back online" decision.
- Config drift: comparing snapshot dicts built by server/snapshot_lib.py.
- Permission self-check: what each cog needs (channels, permissions, roles) and which
  of those the bot is missing.
"""

import hashlib
import re
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

import config

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


# ---------------------------------------------------------------- permission self-check
# What each cog needs to do its job, so a missing permission is reported in the mod log
# instead of failing quietly at 3 a.m. Names are discord.Permissions flag names. A need
# only counts while its cog is loaded.

VIEW = ("view_channel",)
POST = ("view_channel", "send_messages", "embed_links")
SERVER = "Server"  # problem group for server-wide permissions
ROLES = "Roles"  # problem group for roles the bot hands out
OK_DIGEST = "ok"


@dataclass(frozen=True)
class Need:
    cog: str  # extension name without "cogs."
    channel: str  # as in config.py
    perms: tuple[str, ...]
    kind: str = "text"  # text | forum | voice | category | stage


@dataclass(frozen=True)
class GuildNeed:
    perm: str
    cogs: tuple[str, ...]
    hint: str = ""


@dataclass(frozen=True)
class Problem:
    where: str  # channel name, SERVER or ROLES
    text: str


def _game_channels(cog: str) -> list[Need]:
    return [Need(cog, g.channel_name, POST) for g in config.GAMES]


CHANNEL_NEEDS: list[Need] = [
    # forum posts: Send Messages creates the post, Manage Threads locks and reopens it
    Need("lfg", config.LFG_FORUM, POST + ("send_messages_in_threads", "manage_threads"), "forum"),
    Need("community", config.GENERAL_CHANNEL, POST),
    Need("community", config.MOD_LOG_CHANNEL, POST),
    Need("starboard", config.HALL_OF_FAME_CHANNEL, POST + ("read_message_history",)),
    Need("clips", config.CLIPS_CHANNEL, POST + ("send_polls", "read_message_history")),
    Need("clips", config.MOD_CHANNEL, POST),
    Need("tempvoice", config.VOICE_CATEGORY, VIEW + ("manage_channels", "move_members", "connect"), "category"),
    Need("tempvoice", config.NEW_SQUAD_VOICE, VIEW + ("connect",), "voice"),
    Need("events", config.GAMING_CHANNEL, POST),
    Need("events", config.SQUAD_VOICE, VIEW + ("connect",), "voice"),  # voice events
    Need("events", config.LOBBY_VOICE, VIEW + ("connect",), "voice"),
    *_game_channels("events"),
    Need("shop", config.GENERAL_CHANNEL, POST + ("manage_messages",)),  # the Spotlight pin
    Need("shop", config.ANNOUNCEMENTS_CHANNEL, POST),  # season results
    # QOTD answers thread, daily poll
    Need("engagement", config.GENERAL_CHANNEL, POST + ("create_public_threads", "send_polls")),
    Need("engagement", config.COUNTING_CHANNEL, POST + ("add_reactions", "read_message_history")),
    Need("engagement", config.GAMING_CHANNEL, POST),
    Need("engagement", config.SQUAD_VOICE, VIEW + ("connect",), "voice"),
    *_game_channels("engagement"),
    Need("moderation", config.MOD_LOG_CHANNEL, POST),
    Need("ops", config.MOD_LOG_CHANNEL, POST),
    # tickets are private threads under the help channel
    Need("utility", config.HELP_CHANNEL, POST + ("create_private_threads", "send_messages_in_threads",
                                                 "manage_threads", "read_message_history")),
    Need("utility", config.SUGGESTIONS_FORUM, VIEW + ("manage_threads", "add_reactions", "read_message_history"),
         "forum"),
    Need("cards", config.WELCOME_CHANNEL, POST + ("attach_files",)),  # the card is an image
    Need("tournaments", config.TOURNAMENTS_CHANNEL, POST),
    Need("tournaments", config.MOD_LOG_CHANNEL, POST),
    Need("achievements", config.GENERAL_CHANNEL, POST),
    Need("creators", config.CREATORS_CHANNEL, POST),
    Need("creators", config.MOD_LOG_CHANNEL, POST),
    Need("levels", config.BOT_COMMANDS_CHANNEL, POST),
    Need("levels", config.MOD_LOG_CHANNEL, POST),
    Need("recap", config.ANNOUNCEMENTS_CHANNEL, POST),  # weekly recap, milestones
    Need("recap", config.MOD_LOG_CHANNEL, POST),
    Need("recap", config.SUGGESTIONS_FORUM, VIEW, "forum"),
    Need("selfroles", config.ROLES_CHANNEL, POST + ("read_message_history",)),
    Need("partners", config.PARTNERS_CHANNEL, POST),
    Need("partners", config.MOD_LOG_CHANNEL, POST),
    Need("quests", config.GENERAL_CHANNEL, POST),
    Need("challenges", config.GAMES_CHANNEL, POST),
    Need("wordgame", config.GAMES_CHANNEL, POST),
    Need("matchmaking", config.GAMES_CHANNEL, POST),
    Need("matchmaking", config.VOICE_CATEGORY, VIEW + ("manage_channels",), "category"),
    *_game_channels("news"),
    Need("staffapps", config.MOD_LOG_CHANNEL, POST),
    Need("vibes", config.GENERAL_CHANNEL, POST + ("read_message_history",)),
    Need("vibes", config.ANNOUNCEMENTS_CHANNEL, POST),  # member of the month
    Need("growth", config.BOT_COMMANDS_CHANNEL, POST),  # bump reminders
    Need("stats", config.GENERAL_CHANNEL, POST),  # weekly MVP
    Need("crew", config.GENERAL_CHANNEL, POST),  # the crew ping
    Need("crew", config.MOD_LOG_CHANNEL, POST),
    Need("supporter", config.GENERAL_CHANNEL, POST),  # the thank-you
    Need("supporter", config.MOD_LOG_CHANNEL, POST),
    # the bot reacts to new posts and reads the starter message to judge the reward
    Need("forums", config.GUIDES_FORUM, VIEW + ("add_reactions", "read_message_history"), "forum"),
    Need("forums", config.SETUPS_FORUM, VIEW + ("add_reactions", "read_message_history"), "forum"),
    # Stage events (Town Hall, watch parties): Discord wants these three in the stage channel.
    # Without the channel or one of them the Town Hall is skipped and watch parties use the Lobby.
    Need("events", config.STAGE_CHANNEL, VIEW + ("connect", "manage_channels", "mute_members", "move_members"),
         "stage"),
    Need("clipcast", config.CLIPS_CHANNEL, POST + ("read_message_history",)),  # consent fallback, "as seen on"
    Need("clipcast", config.MOD_LOG_CHANNEL, POST),  # picks, upload notes, takedown alerts
    # status panel (edited in place) and the chat relay; reactions say "not relayed"
    Need("realms", config.REALMS_CHANNEL, POST + ("read_message_history", "add_reactions")),
    Need("realms", config.MOD_LOG_CHANNEL, POST),  # whitelist changes
    # appeals: delete the raw webhook message, read missed ones after downtime, open a discussion thread
    Need("appeals", config.APPEALS_CHANNEL, POST + ("manage_messages", "read_message_history",
                                                    "create_public_threads", "send_messages_in_threads")),
    Need("appeals", config.MOD_LOG_CHANNEL, POST),
    Need("wrapped", config.ANNOUNCEMENTS_CHANNEL, POST + ("attach_files",)),  # the server-wide card is an image
    Need("houses", config.ANNOUNCEMENTS_CHANNEL, POST),  # House Cup results, the one-time introduction
]

ROLE_HOLDERS = ("clips", "engagement", "growth", "levels", "selfroles", "shop", "tournaments", "crew", "supporter",
                "houses")
EVENT_MAKERS = ("engagement", "events", "tournaments")
CREATE_EVENTS_HINT = "Server Settings -> Roles -> Front Desk -> turn on Create Events"
GUILD_NEEDS: list[GuildNeed] = [
    GuildNeed("manage_roles", ROLE_HOLDERS),
    GuildNeed("manage_channels", ("matchmaking", "tempvoice", "utility")),
    GuildNeed("manage_events", EVENT_MAKERS),
    # Discord split this from Manage Events: without it creating an event is a 403.
    GuildNeed("create_events", EVENT_MAKERS, CREATE_EVENTS_HINT),
    GuildNeed("moderate_members", ("moderation",)),
    GuildNeed("manage_messages", ("moderation",)),
    GuildNeed("manage_guild", ("growth",)),  # reading invites
    GuildNeed("move_members", ("tempvoice",)),
    GuildNeed("ban_members", ("appeals", "moderation"),  # /ban, checking a ban, unbanning on Accept
              "Server Settings -> Roles -> Front Desk -> turn on Ban Members"),
]

# Roles the bot hands out: each must exist and sit below the bot's top role. Moderator is
# left out on purpose: it sits above Front Desk and staffapps says "give it by hand".
ROLE_NEEDS: list[tuple[str, str]] = [
    *((name, "levels") for _, name in config.LEVEL_ROLES),
    *((name, "selfroles") for name in (*config.PLATFORM_ROLES, *config.REGION_ROLES, *config.PLAYTIME_ROLES,
                                       config.GAMENIGHT_ROLE, config.LFG_ROLE, config.BUMPER_ROLE,
                                       *(g.role for g in config.GAMES))),
    (config.BUMPER_ROLE, "growth"),
    (config.RECRUITER_ROLE, "growth"),
    (config.CLIP_ROLE, "clips"),
    (config.HYPE_ROLE, "shop"),
    (config.SEASON_ROLE, "shop"),
    (config.BIRTHDAY_ROLE, "engagement"),
    (config.COUNTING_ROLE, "engagement"),
    (config.TOURNEY_ROLE, "tournaments"),
    # Supporter is left out on purpose: the bot only reacts to it, and a Server Subscriptions
    # role is managed by Discord and may sit anywhere.
    (config.CREW_ROLE, "crew"),
    # Like every role above, reported until public_mode.py has made them (houses work without).
    *((name, "houses") for name in config.HOUSE_ROLES.values()),
]

PERMISSION_NAMES = sorted({p for n in CHANNEL_NEEDS for p in n.perms} | {n.perm for n in GUILD_NEEDS})


def perm_label(perm: str) -> str:
    return perm.replace("_", " ").title()


def _who(cogs) -> str:
    return ", ".join(sorted(set(cogs)))


def channel_problems(channel_perms, needs=None, loaded=None) -> list[Problem]:
    """`channel_perms`: channel name -> the permission names the bot has there (None or
    absent: no such channel). `needs`: the requirements table. `loaded`: cog names that
    are running (None = all). One problem per missing channel or missing permission,
    naming the cogs that need it, in table order."""
    needs = CHANNEL_NEEDS if needs is None else needs
    wanted: dict[str, dict[str, list[str]]] = {}  # channel -> perm -> cogs
    users: dict[str, list[str]] = {}
    for need in needs:
        if loaded is not None and need.cog not in loaded:
            continue
        users.setdefault(need.channel, []).append(need.cog)
        perms = wanted.setdefault(need.channel, {})
        for perm in need.perms:
            perms.setdefault(perm, []).append(need.cog)
    out: list[Problem] = []
    for channel, perms in wanted.items():
        have = channel_perms.get(channel)
        if have is None:
            out.append(Problem(channel, f"channel not found (needed by {_who(users[channel])})"))
            continue
        for perm, cogs in perms.items():
            if perm not in have:
                out.append(Problem(channel, f"missing {perm_label(perm)} ({_who(cogs)})"))
    return out


def guild_problems(guild_perms, needs=None, loaded=None) -> list[Problem]:
    """Server-wide permissions the bot's roles don't grant."""
    needs = GUILD_NEEDS if needs is None else needs
    out = []
    for need in needs:
        cogs = [c for c in need.cogs if loaded is None or c in loaded]
        if cogs and need.perm not in guild_perms:
            hint = f": {need.hint}" if need.hint else ""
            out.append(Problem(SERVER, f"missing {perm_label(need.perm)} ({_who(cogs)}){hint}"))
    return out


def role_problems(role_positions, top_position: int, needs=None, loaded=None) -> list[Problem]:
    """`role_positions`: role name -> position (None or absent: no such role)."""
    needs = ROLE_NEEDS if needs is None else needs
    users: dict[str, list[str]] = {}
    for name, cog in needs:
        if loaded is None or cog in loaded:
            users.setdefault(name, []).append(cog)
    out = []
    for name, cogs in users.items():
        position = role_positions.get(name)
        if position is None:
            out.append(Problem(ROLES, f"{name}: role not found ({_who(cogs)})"))
        elif position >= top_position:
            out.append(Problem(ROLES, f"{name}: has to sit below Front Desk's role ({_who(cogs)})"))
    return out


def problems_digest(problems) -> str:
    """Stable id of a set of problems, so the same set is only reported once."""
    if not problems:
        return OK_DIGEST
    lines = sorted(f"{p.where}\x1f{p.text}" for p in problems)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:32]


def problems_report(problems, limit: int = 3800) -> str:
    """Mod-log text grouped by channel; "" when there are none."""
    groups: dict[str, list[str]] = {}
    for p in problems:
        groups.setdefault(p.where, []).append(p.text)
    lines: list[str] = []
    for where, texts in groups.items():
        lines.append(f"**{_safe(where)}**")
        lines += [f"- {_safe(t)}" for t in texts]
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        last = i == len(lines) - 1
        tail = f"… and {len(lines) - i} more lines"
        if used + len(line) + (0 if last else 1 + len(tail)) > limit:
            out.append(tail)
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def permissions_status(count: int) -> str:
    """The /status line."""
    if count == 0:
        return "OK"
    return f"{count} problem{'' if count == 1 else 's'}, see mod-log"
