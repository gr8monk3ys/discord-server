"""Moderation rules: who can be acted on, warning escalation, the anti-spam and
anti-raid trackers, the raid lockdown/restore plan and the text mods and members see.
Pure: no Discord, no database, so it's all unit-tested."""

import hashlib
import json
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import Enum

from logic import community

MIN = 60
HOUR = 60 * MIN
DAY = 24 * HOUR

REASON_MAX = 400  # audit-log reasons cap at 512
TIMEOUT_MAX_MIN = 28 * 24 * 60  # Discord's limit: 28 days
PURGE_MAX = 100
CASES_SHOWN = 10

# Escalation: warnings counted over this window.
WARN_WINDOW = 30 * DAY
ESCALATION = ((5, DAY), (3, HOUR))  # (warnings, timeout), strictest first

# Anti-spam.
RATE_COUNT, RATE_WINDOW = 6, 5  # 6 messages in 5 s
DUP_COUNT, DUP_WINDOW = 3, 30  # 3 identical messages in 30 s
MENTION_LIMIT = 5  # distinct user + role mentions in one message
SPAM_TIMEOUT = 10 * MIN
SPAM_COOLDOWN = 60  # after a hit, ignore that member for this long (in-flight messages)

# Anti-raid.
RAID_WEIGHT, RAID_WINDOW = 8, 60
YOUNG_ACCOUNT = 7 * DAY
RAID_LOCK = 30 * MIN
HIGH = 3  # discord.VerificationLevel.high


# ---------------------------------------------------------------- staff and targets
def is_staff(is_owner: bool, is_admin: bool, member_roles: Iterable) -> bool:
    """Owner, administrators, Keepers and Moderators (same rule as handling reports)."""
    return community.can_handle(is_owner, is_admin, member_roles)


class Problem(Enum):
    SELF = "self"
    BOT_SELF = "bot_self"
    STAFF = "staff"
    ABOVE_YOU = "above_you"
    ABOVE_BOT = "above_bot"


TARGET_REPLIES = {
    Problem.SELF: "You can't use this on yourself.",
    Problem.BOT_SELF: "I can't moderate myself.",
    Problem.STAFF: "That member is staff. Talk it over with the team instead.",
    Problem.ABOVE_YOU: "Their highest role isn't below yours, so you can't act on them.",
    Problem.ABOVE_BOT: "Their highest role isn't below mine. Move my role above theirs in Server Settings.",
}


def check_target(*, actor_id: int, target_id: int, bot_id: int, target_is_staff: bool,
                 actor_is_owner: bool, actor_top: int, target_top: int, bot_top: int) -> Problem | None:
    """Why `actor` may not moderate `target`, or None. `*_top` are top-role positions."""
    if target_id == actor_id:
        return Problem.SELF
    if target_id == bot_id:
        return Problem.BOT_SELF
    if target_is_staff:
        return Problem.STAFF
    if not actor_is_owner and target_top >= actor_top:
        return Problem.ABOVE_YOU
    if target_top >= bot_top:
        return Problem.ABOVE_BOT
    return None


def escalation(warns: int) -> int | None:
    """Automatic timeout (seconds) after a warning, given warnings in the last 30 days
    including this one: 3 or 4 -> 1 h, 5 or more -> 24 h."""
    for threshold, seconds in ESCALATION:
        if warns >= threshold:
            return seconds
    return None


# ---------------------------------------------------------------- anti-spam
class SpamReason(Enum):
    RATE = "rate"
    DUPLICATE = "duplicate"
    MENTIONS = "mentions"


SPAM_REASONS = {
    SpamReason.RATE: f"Anti-spam: {RATE_COUNT}+ messages in {RATE_WINDOW} s",
    SpamReason.DUPLICATE: f"Anti-spam: {DUP_COUNT}+ identical messages in {DUP_WINDOW} s",
    SpamReason.MENTIONS: f"Anti-spam: {MENTION_LIMIT}+ mentions in one message",
}


def content_key(content: str | None) -> str | None:
    """A short hash of the normalised text (never the text itself); None for empty."""
    norm = " ".join((content or "").split()).casefold()
    if not norm:
        return None
    return hashlib.blake2b(norm.encode(), digest_size=8).hexdigest()


@dataclass(frozen=True)
class SpamMessage:
    at: float
    message_id: int
    channel_id: int
    content_key: str | None
    mentions: int  # distinct user + role mentions


@dataclass(frozen=True)
class SpamHit:
    reason: SpamReason
    burst: list[SpamMessage]


class SpamTracker:
    """Per-member sliding windows, in memory only (nothing outlives 30 s)."""

    def __init__(self):
        self.users: dict[int, deque[SpamMessage]] = {}
        self.cooldown: dict[int, float] = {}
        self.last_sweep: float | None = None

    def sweep(self, now: float) -> None:
        for uid in [u for u, q in self.users.items() if not q or now - q[-1].at >= DUP_WINDOW]:
            del self.users[uid]
        for uid in [u for u, until in self.cooldown.items() if until <= now]:
            del self.cooldown[uid]
        self.last_sweep = now

    def add(self, user_id: int, m: SpamMessage) -> SpamHit | None:
        if self.last_sweep is None or m.at - self.last_sweep >= DUP_WINDOW:
            self.sweep(m.at)
        if self.cooldown.get(user_id, 0) > m.at:
            return None
        q = self.users.setdefault(user_id, deque())
        q.append(m)
        while q and m.at - q[0].at >= DUP_WINDOW:
            q.popleft()
        hit = None
        if m.mentions >= MENTION_LIMIT:
            hit = SpamHit(SpamReason.MENTIONS, [m])
        else:
            recent = [e for e in q if m.at - e.at < RATE_WINDOW]
            same = [e for e in q if m.content_key is not None and e.content_key == m.content_key]
            if len(recent) >= RATE_COUNT:
                hit = SpamHit(SpamReason.RATE, recent)
            elif len(same) >= DUP_COUNT:
                hit = SpamHit(SpamReason.DUPLICATE, same)
        if hit is not None:
            del self.users[user_id]
            self.cooldown[user_id] = m.at + SPAM_COOLDOWN
        return hit


# ---------------------------------------------------------------- anti-raid
def is_young(created_at: float, now: float) -> bool:
    return now - created_at < YOUNG_ACCOUNT


class RaidTracker:
    """Weighted joins over the last minute, in memory. add() is True when a raid starts."""

    def __init__(self):
        self.joins: deque[tuple[float, int]] = deque()

    def add(self, at: float, young: bool) -> bool:
        self.joins.append((at, 2 if young else 1))
        while self.joins and at - self.joins[0][0] >= RAID_WINDOW:
            self.joins.popleft()
        if sum(w for _, w in self.joins) >= RAID_WEIGHT:
            self.joins.clear()
            return True
        return False


@dataclass(frozen=True)
class RaidState:
    """What the bot changed during a raid, persisted in meta so a restart still restores."""
    until: int  # restore at this time
    prev_level: int  # verification level before the raid
    prev_invites_until: int | None  # an invite pause that was already running
    raised: bool  # whether the bot raised the verification level
    started: int

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, text: str | None) -> "RaidState | None":
        try:
            return cls(**json.loads(text))
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class Restore:
    level: int | None  # verification level to set, or None to leave it
    invites_until: int | None  # invite pause to put back, or None to lift it


def lockdown(now: int, prev_level: int, prev_invites_until: int | None) -> RaidState:
    return RaidState(until=now + RAID_LOCK, prev_level=prev_level, prev_invites_until=prev_invites_until,
                     raised=prev_level < HIGH, started=now)


def extend(state: RaidState, now: int) -> RaidState:
    """Another wave during a lockdown: keep the original 'before' values, push the deadline."""
    return replace(state, until=now + RAID_LOCK)


def restore_plan(state: RaidState, now: int, current_level: int) -> Restore | None:
    """None until the lockdown ends. The level goes back only if it's still the High the
    bot set (a mod who changed it meanwhile wins)."""
    if now < state.until:
        return None
    level = state.prev_level if state.raised and current_level == HIGH else None
    prev = state.prev_invites_until
    return Restore(level=level, invites_until=prev if prev is not None and prev > now else None)


# ---------------------------------------------------------------- text
KIND_LABELS = {
    "warn": "⚠️ Warning",
    "timeout": "⏳ Timeout",
    "untimeout": "✅ Timeout removed",
    "auto_timeout": "⏳ Automatic timeout",
    "spam": "🚫 Anti-spam timeout",
    "purge": "🧹 Purge",
}


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_duration(seconds: int) -> str:
    if seconds < HOUR:
        return f"{max(1, seconds // MIN)} min"
    if seconds >= 2 * DAY and seconds % DAY == 0:
        return f"{seconds // DAY} days"
    hours, rest = divmod(seconds, HOUR)
    return f"{hours} h" + (f" {rest // MIN} min" if rest >= MIN else "")


def dm_text(kind: str, guild_name: str, reason: str, duration: int | None, warns: int | None = None) -> str:
    """What the member is told in a DM."""
    if kind == "warn":
        lines = [f"You received a warning in **{guild_name}**.", f"**Reason** {reason}"]
        if warns:
            lines.append(f"That's {warns} warning{'s' if warns != 1 else ''} in the last 30 days. "
                         "3 means an automatic 1 h timeout, 5 means 24 h.")
        return "\n".join(lines)
    return "\n".join([f"You were timed out in **{guild_name}** for {fmt_duration(duration or 0)}.",
                      f"**Reason** {reason}",
                      "You can read but not chat until it ends. Questions? Message a Moderator afterwards."])


def case_line(case_id: int, kind: str, target: str, mod: str | None, reason: str, duration: int | None) -> str:
    """One mod-log line. `target` and `mod` are pre-rendered (mentions); mod None means automatic."""
    label = KIND_LABELS.get(kind, kind)
    by = mod if mod else "Front Desk (automatic)"
    extra = f" · {fmt_duration(duration)}" if duration else ""
    return f"**Case #{case_id}** {label}{extra}\n**Member** {target}\n**By** {by}\n**Reason** {reason}"


NO_CASES = "No cases for this member."


def cases_text(rows: Sequence[Mapping]) -> str:
    """The /cases list: newest first."""
    if not rows:
        return NO_CASES
    out = []
    for r in sorted(rows, key=lambda r: r["id"], reverse=True):
        extra = f" · {fmt_duration(r['duration'])}" if r["duration"] else ""
        by = f"<@{r['mod_id']}>" if r["mod_id"] else "automatic"
        out.append(f"`#{r['id']}` {KIND_LABELS.get(r['kind'], r['kind'])}{extra} · <t:{r['at']}:R> · {by}\n"
                   f"> {clip(r['reason'], 200)}")
    return "\n".join(out)
